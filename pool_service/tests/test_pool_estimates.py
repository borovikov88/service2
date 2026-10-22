"""Synthetic estimates only: arithmetic, scope, immutable history and tenant isolation."""
from copy import deepcopy
from decimal import Decimal
import json

from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.test import Client, SimpleTestCase, TestCase
from django.urls import reverse

from pool_service.estimate_models import PoolEstimate, PoolEstimateVersion
from pool_service.models import Organization, OrganizationAccess
from pool_service.services.pool_estimates import EstimateInputError, evaluate, generate, recalculate


INPUTS = {"construction_scheme": "legacy","title": "Синтетический тест", "length": "8", "width": "4", "depth_start": "1.5", "depth_end": "1.5", "turnover_hours": "6", "location": "indoor", "heating": "none", "power": "unknown"}


def block(snapshot, key="equipment", variant="economy"):
    return next(b for b in snapshot["variants"][variant]["blocks"] if b["id"] == key)


class PoolEstimateEngineTests(SimpleTestCase):
    def setUp(self):
        self.snapshot = generate(INPUTS)
        # Isolate arithmetic tests from the default labor catalogue.
        for v in self.snapshot["variants"].values():
            for b in v["blocks"]:
                for r in b["rows"]: r["price"] = None
        self.snapshot = evaluate(self.snapshot)

    def test_geometry_and_three_preferences(self):
        self.assertEqual({k:v for k,v in self.snapshot["geometry"].items() if not k.startswith("tech_")}, {"volume": "48.000", "surface": "32.000", "perimeter": "24.000", "finish_area": "68.000", "finish_net": "68.000", "finish_stock": "79.000", "required_flow": "8.000"})
        self.assertEqual([v["preferred_brand"] for v in self.snapshot["variants"].values()], ["Дешёвый подходящий", "Средний подходящий", "Дорогой подходящий"])
        sloped = generate({**INPUTS, "depth_end": "2.5"})
        self.assertEqual(sloped["geometry"]["volume"], "64.000")
        self.assertGreater(Decimal(sloped["geometry"]["finish_area"]), Decimal("80"))

    def test_unknown_is_not_zero_and_client_total_is_discarded(self):
        self.assertIsNone(block(self.snapshot)["rows"][0]["price"])
        count = self.snapshot["variants"]["economy"]["missing_prices"]
        block(self.snapshot)["rows"][0]["price"] = "0"
        self.snapshot["variants"]["economy"]["total"] = "999999"
        result = evaluate(self.snapshot)["variants"]["economy"]
        self.assertEqual(result["total"], "0.00")
        self.assertEqual(result["missing_prices"], count - 1)

    def test_money_rounds_per_line_with_decimal(self):
        row = block(self.snapshot)["rows"][0]
        row.update(price="0.05", quantity="0.1")
        self.assertEqual(evaluate(self.snapshot)["variants"]["economy"]["total"], "0.01")

    def test_excluded_blocks_and_existing_equipment_not_charged(self):
        row = block(self.snapshot)["rows"][0]
        row.update(price="123.45", quantity="2")
        self.assertEqual(evaluate(self.snapshot)["variants"]["economy"]["total"], "246.90")
        row["excluded"] = True
        self.assertEqual(evaluate(self.snapshot)["variants"]["economy"]["total"], "0.00")
        row["excluded"] = False
        row["existing"] = True
        self.assertEqual(evaluate(self.snapshot)["variants"]["economy"]["total"], "0.00")
        row["existing"] = False
        block(self.snapshot)["excluded"] = True
        self.assertEqual(evaluate(self.snapshot)["variants"]["economy"]["total"], "0.00")

    def test_recalculate_preserves_exclusions_manual_quantity_prices(self):
        manual = block(self.snapshot, "finish")["rows"][0]
        manual.update(quantity="99", auto_quantity=False, price="10", excluded=True)
        block(self.snapshot, "construction")["excluded"] = True
        result = recalculate(self.snapshot, {**INPUTS, "length": "10"})
        saved = block(result, "finish")["rows"][0]
        self.assertEqual(saved["quantity"], "99.000")
        self.assertEqual(saved["price"], "10.00")
        self.assertTrue(saved["excluded"])
        self.assertTrue(block(result, "construction")["excluded"])
        self.assertEqual(block(result, "finish")["rows"][1]["quantity"], "82.000")

    def test_existing_filter_still_warns_after_pump_replacement(self):
        self.snapshot["inputs"]["required_head"] = "10"
        pump, filt = block(self.snapshot)["rows"][:2]
        pump["specs"].update(flow="14", head="10")
        filt.update(existing=True, excluded=True)
        filt["specs"]["filter_flow"] = "10"
        warnings = evaluate(self.snapshot)["variants"]["economy"]["warnings"]
        self.assertTrue(any("выше допустимого" in text for text in warnings))
        pump["specs"]["head"] = "5"
        warnings = evaluate(self.snapshot)["variants"]["economy"]["warnings"]
        self.assertTrue(any("не подтверждена" in text for text in warnings))
        self.assertFalse(any("выше допустимого" in text for text in warnings))

    def test_unknown_head_and_multiple_pumps_are_not_claimed_compatible(self):
        pump = block(self.snapshot)["rows"][0]
        pump["quantity"] = "2"
        warnings = evaluate(self.snapshot)["variants"]["economy"]["warnings"]
        self.assertTrue(any("схемы" in text for text in warnings))

    def test_enum_and_identifier_types_rejected_as_validation_errors(self):
        for key in ("location", "heating", "power"):
            for value in ([], {}, None, 1):
                with self.subTest(key=key, value=value), self.assertRaises(EstimateInputError):
                    generate({**INPUTS, key: value})
        for key in ("kind", "basis", "id"):
            for value in ([], {}, None, 1):
                snapshot = deepcopy(self.snapshot)
                block(snapshot)["rows"][0][key] = value
                with self.subTest(key=key, value=value), self.assertRaises(EstimateInputError):
                    evaluate(snapshot)
        snapshot = deepcopy(self.snapshot)
        block(snapshot)["id"] = []
        with self.assertRaises(EstimateInputError):
            evaluate(snapshot)

    def test_invalid_numbers_and_structure_rejected(self):
        for value in ("NaN", "Infinity", "-1", True, [], "1e9999"):
            with self.subTest(value=value), self.assertRaises(EstimateInputError):
                generate({**INPUTS, "length": value})
        for field, value in (("price", "-0.01"), ("quantity", "-0.001"), ("excluded", "false")):
            snapshot = deepcopy(self.snapshot)
            block(snapshot)["rows"][0][field] = value
            with self.subTest(field=field), self.assertRaises(EstimateInputError):
                evaluate(snapshot)
        block(self.snapshot)["rows"].append(deepcopy(block(self.snapshot)["rows"][0]))
        with self.assertRaises(EstimateInputError):
            evaluate(self.snapshot)


class PoolEstimateViewsTests(TestCase):
    def setUp(self):
        self.org = Organization.objects.create(name="Estimate tenant test")
        self.other_org = Organization.objects.create(name="Other tenant test")
        self.owner = User.objects.create_user("estimate-owner", password="test")
        self.outsider = User.objects.create_user("estimate-outsider", password="test")
        self.worker = User.objects.create_user("estimate-installer", password="test")
        OrganizationAccess.objects.create(organization=self.org, user=self.owner, role="owner")
        OrganizationAccess.objects.create(organization=self.org, user=self.worker, role="installer")
        OrganizationAccess.objects.create(organization=self.other_org, user=self.outsider, role="owner")
        self.client.force_login(self.owner)
        self.url = reverse("pool_estimate_api")
        self.snapshot = generate(INPUTS)

    def post(self, **data):
        return self.client.post(self.url, json.dumps({"organization": self.org.pk, **data}), content_type="application/json")

    def save(self, **overrides):
        return self.post(**{"action": "save", "snapshot": self.snapshot, "expected_revision": 0, **overrides})

    def test_lighting_api_save_load_off_on_and_deleted_row(self):
        from .test_estimate_lighting import rows, fixture
        from pool_service.services.estimate_catalog import auto_select
        from unittest.mock import patch
        stock=fixture()
        catalog_read=self.enterContext(patch('pool_service.estimate_views.get_catalog',return_value=stock))
        self.snapshot=auto_select(generate({**INPUTS,'lighting':'yes','light_count':'2'}),stock)[0]
        target=next(b for b in self.snapshot['variants']['economy']['blocks'] if b['id']=='embedded')
        target['rows'].remove(rows(self.snapshot)['light_box'])
        saved=self.save().json()
        loaded=self.client.get(self.url,{'organization':self.org.pk,'id':saved['id']}).json()['snapshot']
        self.assertNotIn('light_box',rows(loaded))
        off=self.post(action='recalculate',snapshot=loaded,inputs={**loaded['inputs'],'lighting':'no'}).json()['snapshot']
        on=self.post(action='recalculate',snapshot=off,inputs={**off['inputs'],'lighting':'yes'}).json()['snapshot']
        self.assertNotIn('light_box',rows(on))
        self.assertEqual(catalog_read.call_count,2)
        self.assertEqual(rows(on)['pool_light']['catalog']['id'],rows(loaded)['pool_light']['catalog']['id'])
        original=self.client.get(self.url,{'organization':self.org.pk,'id':saved['id'],'version':1}).json()['snapshot']
        self.assertEqual(original,loaded)

    def test_unified_work_order_roundtrip_print_and_variant_order(self):
        from unittest.mock import patch
        from pool_service.services.estimate_catalog import CatalogUnavailable
        from pool_service.services.estimate_layout import sections
        self.snapshot=generate({**INPUTS,'finish':'film'})
        variant=self.snapshot['variants']['economy']
        ids=[r['id'] for r in sections(variant)[1]['blocks'][3]['rows']]
        variant['view_order']={'installation_work':list(reversed(ids))}
        saved=self.save().json()
        loaded=self.client.get(self.url,{'organization':self.org.pk,'id':saved['id']}).json()['snapshot']
        with patch('pool_service.estimate_views.get_catalog',side_effect=CatalogUnavailable('Synthetic offline')):
            recalculated=self.post(action='recalculate',snapshot=loaded,inputs=loaded['inputs']).json()['snapshot']
        actual=[r['id'] for r in sections(recalculated['variants']['economy'])[1]['blocks'][3]['rows']]
        self.assertEqual(actual,list(reversed(ids)))
        printed=self.client.get(reverse('pool_estimate_print',args=[saved['id'],1])).content.decode()
        active=[r['id'] for r in sections(loaded['variants']['economy'],client=True)[1]['blocks'][3]['rows']]
        self.assertEqual(sorted(active,key=lambda rid:printed.index('data-row="'+rid+'"')),active)
        self.assertLess(printed.index('<h2>Эконом</h2>'),printed.index('<h2>Стандарт</h2>'))
        self.assertLess(printed.index('<h2>Стандарт</h2>'),printed.index('<h2>Премиум</h2>'))

    def test_rgb_controller_history_roundtrip_is_hermetic_and_idempotent(self):
        from unittest.mock import patch
        from pool_service.services.estimate_catalog import auto_select
        from .test_estimate_lighting_color import fixture
        from .test_estimate_lighting import rows
        stock=fixture();self.enterContext(patch('pool_service.estimate_views.get_catalog',return_value=stock))
        self.snapshot=auto_select(generate({**INPUTS,'lighting':'yes','light_color':'rgb','light_count':'2'}),stock)[0]
        first=self.save().json()
        loaded=self.client.get(self.url,{'organization':self.org.pk,'id':first['id']}).json()['snapshot']
        self.assertEqual(rows(loaded)['light_controller']['catalog']['id'],'958')
        white=self.post(action='recalculate',snapshot=loaded,inputs={**loaded['inputs'],'light_color':'white'}).json()['snapshot']
        self.assertTrue(rows(white)['light_controller']['excluded'])
        rgb=self.post(action='recalculate',snapshot=white,inputs={**white['inputs'],'light_color':'rgb'}).json()['snapshot']
        self.assertEqual(rgb['variants']['economy']['total'],loaded['variants']['economy']['total'])
        for tier,v in rgb['variants'].items():
            self.assertEqual(len([r for b in v['blocks'] for r in b['rows'] if r['kind']=='light_controller']),1)
        original=self.client.get(self.url,{'organization':self.org.pk,'id':first['id'],'version':1}).json()['snapshot']
        self.assertEqual(original,loaded)
        printed=self.client.get(reverse('pool_estimate_print',args=[first['id'],1])).content.decode()
        self.assertIn('https://shop.aqualine22.ru/product-955.html',printed)

    def test_entire_construction_scope_persists_without_changing_other_quotes(self):
        from .test_estimate_construction import NEW
        self.snapshot=generate(NEW)
        for block in self.snapshot['variants']['economy']['blocks']:
            if block['id'] in {'construction','construction_materials'}:block.update(excluded=True,reason='Исключено пользователем')
        saved=self.save().json()
        loaded=self.client.get(self.url,{'organization':self.org.pk,'id':saved['id']}).json()['snapshot']
        for tier,v in loaded['variants'].items():
            for block in v['blocks']:
                if block['id'] in {'construction','construction_materials'}:
                    self.assertEqual(block['excluded'],tier=='economy')
                    if tier=='economy':self.assertEqual(block['subtotal'],'0.00')

    def test_row_order_survives_save_load_recalculate_and_print(self):
        self.snapshot = generate({**INPUTS, 'heating_types':['exchanger','electric'],
                                  'heating':'exchanger', 'construction_scheme':'package',
                                  'freeboard':'0.15','wall_thickness':'0.2','slab_thickness':'0.2'})
        before = deepcopy(self.snapshot)
        rows = block(self.snapshot)['rows']
        for i, row in enumerate(rows):
            row.update(price='123.45', quantity='2', auto_quantity=False)
            row['name'] = 'Order marker ' + str(i)
        rows[1]['excluded'] = True
        rows.reverse()
        expected = [r['id'] for r in rows]
        first = self.save().json()
        loaded = self.client.get(self.url, {'organization':self.org.pk,'id':first['id']}).json()['snapshot']
        recalculated = recalculate(loaded, loaded['inputs'])
        self.assertEqual([r['id'] for r in block(recalculated)['rows']], expected)
        self.assertEqual(block(recalculated)['rows'], block(loaded)['rows'])
        self.assertEqual(recalculated['variants']['standard'], before['variants']['standard'])
        self.assertEqual(recalculated['variants']['economy']['total'], loaded['variants']['economy']['total'])
        html = self.client.get(reverse('pool_estimate_print',args=[first['id'],1])).content.decode()
        from pool_service.services.estimate_layout import group_for
        source=block(self.snapshot)
        for group in {group_for(source,r) for r in rows}:
            names=[r['name'] for r in rows if not r['excluded'] and group_for(source,r)==group]
            self.assertEqual(sorted(names,key=html.index),names)
        self.assertNotIn(next(r['name'] for r in rows if r['excluded']),html)

    def test_work_upgrade_and_rate_versions_keep_tenant_boundary(self):
        first = self.save().json()
        frozen = deepcopy(PoolEstimateVersion.objects.get(number=1).snapshot)
        response = self.post(action='add_works', snapshot=self.snapshot)
        self.assertEqual(response.status_code, 200)
        self.snapshot = response.json()['snapshot']
        for v in self.snapshot['variants'].values():
            next(r for b in v['blocks'] for r in b['rows'] if r.get('work_code')=='equipment_work')['price']='123.45'
        self.assertEqual(self.save(id=first['id'], expected_revision=1).status_code, 201)
        self.assertEqual(PoolEstimateVersion.objects.get(number=1).snapshot, frozen)
        self.client.force_login(self.outsider)
        self.assertEqual(self.post(action='add_works', snapshot=self.snapshot).status_code, 403)

    def test_page_and_nav(self):
        response = self.client.get(reverse("pool_estimate_page"))
        self.assertContains(response, "Расчёт бассейна")
        self.assertContains(response, "Исключённые строки и блоки")
        self.assertContains(response, 'data-api="' + self.url + '"')

    def test_versions_immutable_and_stale_save_conflicts(self):
        first = self.save().json()
        self.assertEqual(first["revision"], 1)
        self.snapshot["inputs"]["title"] = "Changed synthetic title"
        second = self.save(id=first["id"], expected_revision=1).json()
        self.assertEqual(second["revision"], 2)
        self.assertEqual(PoolEstimateVersion.objects.get(number=1).snapshot["inputs"]["title"], INPUTS["title"])
        self.assertEqual(self.save(id=first["id"], expected_revision=1).status_code, 409)
        self.assertEqual(PoolEstimateVersion.objects.count(), 2)
        version = PoolEstimateVersion.objects.get(number=1)
        with self.assertRaises(ValidationError):
            version.save()
        old = self.client.get(self.url, {"organization": self.org.pk, "id": first["id"], "version": 1}).json()
        self.assertEqual(old["loaded_version"], 1)
        self.assertEqual(old["revision"], 2)
        third = self.save(id=first["id"], expected_revision=2, snapshot=old["snapshot"]).json()
        self.assertEqual(third["revision"], 3)

    def test_all_endpoints_enforce_tenant_access(self):
        first = self.save().json()
        self.client.force_login(self.outsider)
        self.assertEqual(self.post(action="generate", inputs=INPUTS).status_code, 403)
        self.assertEqual(self.client.get(self.url, {"organization": self.org.pk}).status_code, 403)
        self.assertEqual(self.client.get(self.url, {"organization": self.other_org.pk, "id": first["id"]}).status_code, 404)
        self.assertEqual(self.post(action="save", organization=self.other_org.pk, id=first["id"], expected_revision=1, snapshot=self.snapshot).status_code, 404)
        self.assertEqual(self.client.get(reverse("pool_estimate_print", args=[first["id"], 1])).status_code, 404)

    def test_installer_denied_page_and_save(self):
        self.client.force_login(self.worker)
        self.assertEqual(self.client.get(reverse("pool_estimate_page")).status_code, 403)
        self.assertEqual(self.save().status_code, 403)

    def test_csrf_and_login_required(self):
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.owner)
        self.assertEqual(client.post(self.url, json.dumps({"organization": self.org.pk, "action": "generate", "inputs": INPUTS}), content_type="application/json").status_code, 403)
        self.client.logout()
        self.assertEqual(self.client.get(reverse("pool_estimate_page")).status_code, 302)

    def test_malformed_payload_does_not_create_versions(self):
        for payload in ([], None, {"organization": self.org.pk, "action": "save", "snapshot": {}}):
            response = self.client.post(self.url, json.dumps(payload), content_type="application/json")
            self.assertEqual(response.status_code, 400)
        self.assertEqual(PoolEstimate.objects.count(), 0)

    def test_print_uses_frozen_prices_and_escapes_text(self):
        pump, filt = block(self.snapshot)["rows"][:2]
        pump.update(name='<script>alert("test")</script>', price="12.34")
        filt.update(name="Excluded synthetic filter", excluded=True, price="999")
        first = self.save().json()
        response = self.client.get(reverse("pool_estimate_print", args=[first["id"], 1]))
        self.assertContains(response, ">12</td>")
        self.assertContains(response, "&lt;script&gt;")
        self.assertNotContains(response, '<script>alert(')
        self.assertNotContains(response, "Excluded synthetic filter")
        self.assertContains(response, "Этап 1. Строительство чаши")
        self.assertContains(response, "Предварительный расчёт")

    def test_search_is_limited_to_current_organization(self):
        self.save()
        self.client.force_login(self.outsider)
        response = self.client.get(self.url, {"organization": self.other_org.pk})
        self.assertEqual(response.json()["projects"], [])

    def test_manager_author_history_and_direct_tenant_access(self):
        manager=User.objects.create_user('estimate-manager-author',first_name='Synthetic',last_name='Manager')
        OrganizationAccess.objects.create(organization=self.org,user=manager,role='manager')
        self.client.force_login(manager)
        self.assertEqual(self.client.get(reverse('pool_estimate_page')).status_code,200)
        from pool_service.services.estimate_catalog import CatalogUnavailable
        from unittest.mock import patch
        with patch('pool_service.estimate_views.get_catalog',side_effect=CatalogUnavailable('Test feed unavailable')):
            generated=self.post(action='generate',inputs=INPUTS)
        self.assertEqual(generated.status_code,200)
        self.snapshot=generated.json()['snapshot']
        first=self.save().json()
        self.assertEqual(first['created_by']['id'],manager.pk)
        self.assertEqual(first['versions'][0]['created_by']['id'],manager.pk)
        listed=self.client.get(self.url,{'organization':self.org.pk}).json()['projects']
        self.assertEqual(listed[0]['created_by']['id'],manager.pk)
        saved_version=deepcopy(first['versions'][0])
        self.client.force_login(self.owner)
        second=self.save(id=first['id'],expected_revision=1).json()
        self.assertEqual(second['created_by']['id'],manager.pk)
        self.assertEqual(second['versions'][0]['created_by']['id'],self.owner.pk)
        self.assertEqual(second['versions'][1],saved_version)
        self.client.force_login(manager)
        old=self.client.get(self.url,{'organization':self.org.pk,'id':first['id'],'version':1})
        self.assertEqual(old.status_code,200)
        self.assertEqual(old.json()['versions'][1],saved_version)
        self.assertEqual(self.post(action='validate',snapshot=self.snapshot).status_code,200)
        self.assertEqual(self.post(action='recalculate',snapshot=self.snapshot,inputs=INPUTS,organization=self.other_org.pk).status_code,403)
        self.assertEqual(self.save(organization=self.other_org.pk).status_code,403)
        self.assertEqual(self.client.get(self.url,{'organization':self.other_org.pk,'id':first['id']}).status_code,403)
        printed=self.client.get(reverse('pool_estimate_print',args=[first['id'],1]))
        self.assertEqual(printed.status_code,200)
        self.assertNotContains(printed,'Synthetic Manager')
        third=self.save(id=first['id'],expected_revision=2,created_by=self.owner.pk).json()
        self.assertEqual(third['versions'][0]['created_by']['id'],manager.pk)
        self.assertEqual(third['versions'][2],saved_version)
        self.client.force_login(self.outsider)
        self.assertEqual(self.client.get(self.url,{'organization':self.other_org.pk,'id':first['id']}).status_code,404)
        self.assertEqual(self.save(id=first['id'],expected_revision=2,organization=self.other_org.pk).status_code,404)
        self.assertEqual(self.client.get(reverse('pool_estimate_print',args=[first['id'],1])).status_code,404)
        PoolEstimateVersion.objects.filter(estimate_id=first['id'],number=1).update(created_by=None)
        self.client.force_login(manager)
        unknown=self.client.get(self.url,{'organization':self.org.pk,'id':first['id']}).json()
        self.assertEqual(next(v for v in unknown['versions'] if v['number']==1)['created_by'],{'id':None,'name':'Автор не указан'})

    def test_explicit_unbound_round_trip_save_load_and_recalculate(self):
        from unittest.mock import patch
        from pool_service.services.estimate_catalog import CatalogUnavailable
        self.snapshot=generate({**INPUTS,'heating':'exchanger'})
        rows=block(self.snapshot,variant='premium')['rows']
        parent=next(r for r in rows if r['kind']=='heater')
        parent['quantity']='2'
        valve=next(r for r in rows if r['kind']=='solenoid_valve')
        valve.update(heating_parent='',heating_unbound=True,quantity='2',price='100')
        with patch('pool_service.estimate_views.get_catalog',side_effect=CatalogUnavailable('Synthetic offline catalog')):
            recalculated=self.post(action='recalculate',snapshot=self.snapshot,inputs=self.snapshot['inputs']).json()['snapshot']
            validated=self.post(action='validate',snapshot=recalculated).json()['snapshot']
            saved=self.save(snapshot=validated).json()
            loaded=self.client.get(self.url,{'organization':self.org.pk,'id':saved['id'],'version':1}).json()['snapshot']
            again=self.post(action='recalculate',snapshot=loaded,inputs=loaded['inputs']).json()['snapshot']
            for snapshot in (recalculated,validated,loaded,again):
                valves=[r for r in block(snapshot,variant='premium')['rows'] if r['kind']=='solenoid_valve']
                self.assertEqual(len(valves),1)
                self.assertEqual(valves[0]['heating_parent'],'')
                self.assertTrue(valves[0]['heating_unbound'])
                self.assertIsNone(valves[0]['quantity'])
                self.assertIsNone(valves[0]['amount'])
            target=next(r for r in block(again,variant='premium')['rows'] if r['kind']=='solenoid_valve')
            target.update(heating_parent=parent['id'],heating_unbound=False)
            restored=self.post(action='recalculate',snapshot=again,inputs=again['inputs']).json()['snapshot']
            valves=[r for r in block(restored,variant='premium')['rows'] if r['kind']=='solenoid_valve']
            self.assertEqual(len(valves),1)
            self.assertEqual(valves[0]['quantity'],'2.000')
            self.assertEqual(valves[0]['amount'],'200.00')
            self.assertIsNone(next(r for r in block(loaded,variant='premium')['rows'] if r['kind']=='solenoid_valve')['amount'])
