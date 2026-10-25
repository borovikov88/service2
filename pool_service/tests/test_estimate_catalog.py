"""Synthetic catalogue fixtures only; no shop requests or production data."""
from copy import deepcopy
from decimal import Decimal
import json
from unittest.mock import patch
from xml.sax.saxutils import escape

from django.contrib.auth.models import User
from django.core.cache import cache
from django.test import SimpleTestCase, TestCase
from django.urls import reverse

from pool_service.estimate_models import PoolEstimateVersion
from pool_service.models import Organization, OrganizationAccess
from pool_service.services import estimate_catalog as catalog
from pool_service.services.pool_estimates import EstimateInputError, evaluate, generate, recalculate
from pool_service.tests.test_pool_estimates import INPUTS, block


def offer(pid, name, price="100.10", brand="Aquaviva", available="true", description="Насос с префильтром для систем фильтрации", currency="RUB", url=None):
    category = '<param name="Наличие для подбора">own_stock</param>'
    if name.startswith('Насос ') and not name.startswith('Насос циркуляцион'):
        category += '<param name="Категория подбора">Насосы для бассейнов</param>'
    elif name.startswith(('Фильтр ', 'Песочный фильтр ')):
        category += '<param name="Категория подбора">Фильтры</param>'
    return f'<offer id="{pid}" available="{available}"><url>{escape(url or f"https://shop.aqualine22.ru/product-{pid}.html")}</url><price>{price}</price><currencyId>{currency}</currencyId><name>{escape(name)}</name><vendor>{brand}</vendor><description>{escape(description)}</description>{category}</offer>'


def feed(*offers):
    return ('<yml_catalog date="2026-10-19 00:00"><shop><offers>' + ''.join(offers) + '</offers></shop></yml_catalog>').encode()


def fixture():
    return catalog.parse_catalog(feed(
        offer(1, "Насос Aquaviva Test 8 (220V, 8m3/h*8m)"),
        offer(2, "Фильтр Aquaviva Test 10 (10 м3/ч)", price="200.20"),
        offer(3, "Насос Hayward Test 10 (220V, 10m3/h*8m)", brand="Hayward"),
        offer(4, "Фильтр Hayward Test 12 (12 м3/ч)", brand="Hayward"),
        offer(5, "Насос Aquaviva Test 16 (220V, 16m3/h*8m)", price="150"),
        offer(6, "Фильтр Aquaviva Test 20 (20 м3/ч)", price="250"),
        offer(7, "Электронагреватель Aquaviva Test (220V, 6 кВт)", price="300"),
    ), "2026-10-19T00:00:00+00:00")


class CatalogEngineTests(SimpleTestCase):
    def setUp(self):
        self.catalog = fixture()
        cache.clear()

    def tearDown(self):
        cache.clear()

    def test_shop_price_decimal_and_point_are_not_maximum_curve(self):
        p = self.catalog["products"]["1"]
        self.assertEqual((p["price"], p["flow"], p["head"], p["power"]), ("100.10", "8", "8", "230"))
        p = catalog.parse_catalog(feed(offer(9, "Насос Aquaviva Test (220В, 8 м3/ч, Hmax 10м)")))["products"]["9"]
        self.assertEqual(p["nominal_flow"], "8")
        self.assertIsNone(p["head"])
        self.assertIsNone(p["flow"])

    def test_xxe_foreign_urls_currency_and_bad_prices_rejected(self):
        with self.assertRaises(catalog.CatalogUnavailable):
            catalog.parse_catalog(b'<!DOCTYPE x [<!ENTITY a SYSTEM "file:///etc/passwd">]><x/>')
        for encoding in ("utf-16", "utf-16-le", "utf-16-be", "utf-32-le", "utf-32-be"):
            with self.subTest(encoding=encoding), self.assertRaises(catalog.CatalogUnavailable):
                catalog.parse_catalog('<!DOCTYPE x [<!ENTITY a "expanded">]><x>&a;</x>'.encode(encoding))
        for kwargs in ({"url": "https://example.invalid/product"}, {"price": "NaN"}, {"price": "0"}, {"currency": "USD"}):
            with self.subTest(kwargs=kwargs), self.assertRaises(catalog.CatalogUnavailable):
                catalog.parse_catalog(feed(offer(1, "Test", **kwargs)))
        for url in ("javascript:alert(1)", "https://shop.aqualine22.ru@evil.invalid/", "https://shop.aqualine22.ru:444/", "https://shop.aqualine22.ru/\nfoo"):
            self.assertEqual(catalog.safe_shop_url(url), "")

    def test_price_tiers_with_technically_comparable_catalogue_selection(self):
        s, count = catalog.auto_select(generate(INPUTS), self.catalog)
        self.assertEqual(count, 6)
        self.assertEqual(block(s)["rows"][0]["catalog"]["id"], "1")
        self.assertEqual(block(s, variant="standard")["rows"][0]["brand"], "Hayward")
        self.assertIsNotNone(block(s, variant="premium")["rows"][0]["price"])
        self.assertEqual(sum(Decimal(r["amount"] or 0) for r in block(s)["rows"] if r["kind"] != "work"), Decimal("57800.00"))  # equipment plus 32 m² × 1800 PVC materials

    def test_spa_unavailable_wrong_voltage_and_wrong_head_not_auto_selected(self):
        f = catalog.parse_catalog(feed(
            offer(1, "Насос Aquaviva Cheap (220В, 8 м3/ч)", price="1", description="Для СПА и гидромассажа, без префильтра"),
            offer(2, "Насос Aquaviva Unavailable (220V, 8m3/h*8m)", price="2", available="false"),
            offer(3, "Насос Aquaviva ThreePhase (380V, 8m3/h*8m)", price="3"),
            offer(4, "Насос Aquaviva NoPoint (220V, 8m3/h)", price="4"),
            offer(5, "Насос Aquaviva Good (220V, 8m3/h*8m)", price="5"),
        ))
        s, _ = catalog.auto_select(generate({**INPUTS, "power": "230", "required_head": "8"}), f)
        self.assertEqual(block(s)["rows"][0]["catalog"]["id"], "5")
        s, _ = catalog.auto_select(generate({**INPUTS, "power": "230", "required_head": "10"}), f)
        self.assertEqual(block(s)["rows"][0]["catalog"]["id"], "4")
        self.assertIn("рабочая Q–H точка", block(s)["rows"][0]["selection_note"])

    def test_recalculation_replaces_only_untouched_auto_and_sizes_filter_for_pump(self):
        s, _ = catalog.auto_select(generate(INPUTS), self.catalog)
        s = recalculate(s, {**INPUTS, "length": "16"})
        s, _ = catalog.auto_select(s, self.catalog)
        self.assertEqual([r["catalog"]["id"] for r in block(s)["rows"][:2]], ["5", "6"])
        self.assertTrue(any("не подтверждена" in w for w in s["variants"]["economy"]["warnings"]))

    def test_auto_preserves_manual_prices_exclusions_existing_and_multiple_pumps(self):
        for edit in ({"excluded": True}, {"existing": True}, {"price": "55.55"}, {"quantity": "2"}):
            s, _ = catalog.auto_select(generate(INPUTS), self.catalog)
            row = block(s)["rows"][0]; row.update(edit)
            s = recalculate(s, {**INPUTS, "length": "16"})
            s, _ = catalog.auto_select(s, self.catalog)
            self.assertEqual(block(s)["rows"][0]["catalog"]["id"], "1")
        s = generate(INPUTS); block(s)["rows"][0]["quantity"] = "2"
        s, _ = catalog.auto_select(s, self.catalog)
        self.assertIsNone(block(s)["rows"][1]["price"])

    def test_manual_placeholder_name_brand_specs_and_lock_are_preserved(self):
        for edit in ({"name": "Ручной согласованный насос"}, {"brand": "Manual"}, {"specs": {"flow": "20", "head": "10"}}, {"selection_locked": True}):
            s = generate(INPUTS); block(s)["rows"][0].update(edit)
            result, _ = catalog.auto_select(s, self.catalog)
            self.assertIsNone(block(result)["rows"][0]["price"])

    @patch.object(catalog, "build_opener")
    def test_download_stops_at_deadline_using_single_chunk_reads(self, opener):
        response = opener.return_value.open.return_value.__enter__.return_value
        response.status = 200
        response.read1.return_value = b"a"
        with patch.object(catalog.time, "monotonic", side_effect=[0, 26]), self.assertRaises(catalog.CatalogUnavailable):
            catalog._download()
        response.read.assert_not_called()

    def test_heater_added_after_initial_calculation_and_exclusion_preserved(self):
        s = recalculate(generate(INPUTS), {**INPUTS, "heating": "electric", "heating_power": "6", "power": "230"})
        s, _ = catalog.auto_select(s, self.catalog)
        heater = next(r for r in block(s)["rows"] if r["kind"] == "heater")
        self.assertEqual(heater["catalog"]["id"], "7")
        heater["excluded"] = True
        s, _ = catalog.auto_select(s, self.catalog)
        self.assertEqual(sum(r["kind"] == "heater" for r in block(s)["rows"]), 1)
        self.assertTrue(next(r for r in block(s)["rows"] if r["kind"] == "heater")["excluded"])

    def test_same_model_keeps_price_until_explicit_refresh(self):
        original, _ = catalog.auto_select(generate(INPUTS), self.catalog)
        c = deepcopy(self.catalog); c["products"]["1"]["price"] = "999.99"
        auto, _ = catalog.auto_select(original, c)
        self.assertEqual(block(auto)["rows"][0]["price"], "100.10")
        fresh, count, _ = catalog.refresh_prices(original, c, "economy")
        self.assertEqual(count, 2)
        self.assertEqual(block(fresh)["rows"][0]["price"], "999.99")
        self.assertEqual(block(original)["rows"][0]["price"], "100.10")
        for field, value in (("price", "50.00"), ("excluded", True), ("existing", True)):
            changed = deepcopy(original); block(changed)["rows"][0][field] = value
            updated, _, _ = catalog.refresh_prices(changed, c, "economy")
            self.assertEqual(block(updated)["rows"][0]["price"], block(changed)["rows"][0]["price"])

    def test_missing_products_keep_frozen_price_with_warning(self):
        s, _ = catalog.auto_select(generate(INPUTS), self.catalog)
        c = deepcopy(self.catalog); del c["products"]["1"]
        s, _, _ = catalog.refresh_prices(s, c, "economy")
        self.assertEqual(block(s)["rows"][0]["price"], "100.10")
        self.assertTrue(any("исчез" in w for w in s["variants"]["economy"]["warnings"]))

    def test_catalog_metadata_cannot_inject_links_or_crash_date_rendering(self):
        s, _ = catalog.auto_select(generate(INPUTS), self.catalog)
        for field, value in (("url", "javascript:alert(1)"), ("checked_at", "tomorrow"), ("selection", []), ("price", "NaN")):
            bad = deepcopy(s); block(bad)["rows"][0]["catalog"][field] = value
            with self.subTest(field=field), self.assertRaises(EstimateInputError):
                evaluate(bad)

    @patch.object(catalog, "_download")
    def test_cache_and_failure_throttle_do_not_fabricate_prices(self, download):
        download.return_value = feed(offer(1, "Test"))
        catalog.get_catalog(); catalog.get_catalog()
        self.assertEqual(download.call_count, 1)
        cache.clear(); download.reset_mock(); download.side_effect = OSError("synthetic offline")
        for _ in range(2):
            with self.assertRaises(catalog.CatalogUnavailable):
                catalog.get_catalog()
        self.assertEqual(download.call_count, 1)


class CatalogViewsTests(TestCase):
    def setUp(self):
        self.org = Organization.objects.create(name="Synthetic catalogue tenant")
        self.other = Organization.objects.create(name="Synthetic other tenant")
        self.user = User.objects.create_user("catalogue-owner")
        OrganizationAccess.objects.create(user=self.user, organization=self.org, role="owner")
        self.client.force_login(self.user)
        self.url = reverse("pool_estimate_api")
        self.catalog = fixture()
        self.mock = patch("pool_service.estimate_views.get_catalog", return_value=self.catalog)
        self.fetch = self.mock.start(); self.addCleanup(self.mock.stop)

    def post(self, **data):
        return self.client.post(self.url, json.dumps({"organization": self.org.pk, **data}), content_type="application/json")

    def test_tenant_checks_before_catalog_network(self):
        response = self.post(action="catalog_search", organization=self.other.pk, inputs=INPUTS)
        self.assertEqual(response.status_code, 403)
        self.fetch.assert_not_called()

    def test_generation_apply_price_refresh_and_historical_version(self):
        s = self.post(action="generate", inputs=INPUTS).json()["snapshot"]
        s = self.post(action="catalog_apply", snapshot=s, variant="economy", block="equipment", row="pump", product_id="5").json()["snapshot"]
        self.assertEqual(block(s)["rows"][0]["catalog"]["selection"], "manual")
        first = self.post(action="save", snapshot=s, expected_revision=0).json()
        self.catalog["products"]["5"]["price"] = "500"
        s = self.post(action="catalog_prices", snapshot=s, variant="economy").json()["snapshot"]
        saved = self.post(action="save", snapshot=s, id=first["id"], expected_revision=1)
        self.assertEqual(saved.status_code, 201)
        old = PoolEstimateVersion.objects.get(estimate_id=first["id"], number=1)
        self.assertEqual(block(old.snapshot)["rows"][0]["price"], "150.00")
        self.assertEqual(block(saved.json()["snapshot"])["rows"][0]["price"], "500.00")

    def test_search_and_invalid_catalog_payloads(self):
        response = self.post(action="catalog_search", inputs=INPUTS, q="Hayward", kind="pump")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["total"], 1)
        for data in ({"variant": []}, {"variant": "economy", "product_id": []}, {"variant": "economy", "product_id": "999"}):
            response = self.post(action="catalog_apply", snapshot=generate(INPUTS), **data)
            self.assertEqual(response.status_code, 400)

    def test_offline_generation_remains_editable_with_explicit_error(self):
        self.fetch.side_effect = catalog.CatalogUnavailable("synthetic unavailable")
        response = self.post(action="generate", inputs=INPUTS)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["catalog_error"], "synthetic unavailable")
        self.assertIsNone(block(response.json()["snapshot"])["rows"][0]["price"])

    def test_heating_finish_history_and_one_print_price_date(self):
        from pool_service.tests.test_estimate_heating_finish import HEATING
        inputs = {**HEATING, "finish": "film"}
        s = self.post(action="generate", inputs=inputs).json()["snapshot"]
        self.assertEqual(s["heating"]["power"], "2.784")
        self.assertEqual(next(r for r in block(s)["rows"] if r["kind"] == "heater")["price"], "300.00")
        first = self.post(action="save", snapshot=s, expected_revision=0).json()
        tile = self.post(action="recalculate", snapshot=s, inputs={**inputs, "finish": "tile"}).json()["snapshot"]
        self.assertEqual(self.post(action="save", snapshot=tile, id=first["id"], expected_revision=1).status_code, 201)
        self.fetch.reset_mock()
        old = self.client.get(self.url, {"organization": self.org.pk, "id": first["id"], "version": 1}).json()["snapshot"]
        self.assertEqual(old["inputs"]["finish"], "film")
        self.assertEqual(old["heating"]["power"], "2.784")
        printed = self.client.get(reverse("pool_estimate_print", args=[first["id"], 1]))
        self.assertContains(printed, "Дата цен: 19.10.2026", count=1)
        self.fetch.assert_not_called()

    def test_embedded_profile_is_per_quote_and_history_and_tenants_are_protected(self):
        from pool_service.tests.test_estimate_skimmer import stock, fitting
        self.fetch.return_value = stock()
        s = self.post(action="generate", inputs={**INPUTS,"finish":"film"}).json()["snapshot"]
        first = self.post(action="save", snapshot=s, expected_revision=0).json()
        changed = self.post(action="embedded_profile", snapshot=s, variant="economy", profile="steel304")
        self.assertEqual(changed.status_code,200)
        changed = changed.json()["snapshot"]
        self.assertEqual(fitting(changed,"skimmer")["catalog"]["id"],"106")
        self.assertEqual(changed["variants"]["standard"],s["variants"]["standard"])
        self.assertEqual(self.post(action="save",snapshot=changed,id=first["id"],expected_revision=1).status_code,201)
        self.fetch.reset_mock()
        old = self.client.get(self.url,{"organization":self.org.pk,"id":first["id"],"version":1}).json()["snapshot"]
        self.assertEqual(fitting(old,"skimmer")["catalog"]["id"],"101")
        self.assertEqual(old["variants"]["economy"]["embedded_profile"],"plastic_budget")
        self.assertEqual(self.post(action="embedded_profile",snapshot=s,variant="economy",profile="steel304",organization=self.other.pk).status_code,403)
        self.fetch.assert_not_called()


    def test_roll_conversion_survives_save_load_recalculate_and_print(self):
        from pool_service.tests.test_estimate_liner import stock, film
        self.fetch.return_value=stock()
        s=self.post(action='generate',inputs={**INPUTS,'finish':'film','bowl_height':'1.7'}).json()['snapshot']
        amount=film(s)['amount']
        saved=self.post(action='save',snapshot=s,expected_revision=0).json()
        loaded=self.client.get(self.url,{'organization':self.org.pk,'id':saved['id'],'version':1}).json()['snapshot']
        self.assertEqual(film(loaded)['amount'],amount)
        changed=self.post(action='recalculate',snapshot=loaded,inputs=loaded['inputs']).json()['snapshot']
        self.assertEqual(film(changed)['amount'],amount)
        printed=self.client.get(reverse('pool_estimate_print',args=[saved['id'],1]))
        self.assertContains(printed,'Цена рулона 100001 ₽')
        from pool_service.templatetags.estimate_numbers import estimate_money
        self.assertContains(printed,estimate_money(amount))


    def test_custom_roll_selection_normalizes_save_load_lineage(self):
        from pool_service.tests.test_estimate_liner import stock, film
        self.fetch.return_value=stock()
        s=generate(INPUTS)
        row=film(s)
        row.update(id='550e8400-e29b-41d4-a716-446655440000',finish_type='',basis='',auto_quantity=False,quantity='79')
        selected=self.post(action='catalog_apply',snapshot=s,variant='economy',block='finish',row=row['id'],product_id='1')
        self.assertEqual(selected.status_code,200)
        s=selected.json()['snapshot']
        find=lambda snapshot:next(r for r in block(snapshot,'finish')['rows'] if r['id']==row['id'])
        self.assertEqual(find(s)['amount'],'189995.00')
        saved=self.post(action='save',snapshot=s,expected_revision=0).json()
        loaded=self.client.get(self.url,{'organization':self.org.pk,'id':saved['id'],'version':1}).json()['snapshot']
        self.assertEqual(find(loaded)['catalog']['roll_area'],'41.58')
        self.assertEqual(find(loaded)['amount'],'189995.00')
        self.assertEqual(find(loaded)['finish_type'],'')


    def test_construction_quantization_version_survives_save_and_load(self):
        from pool_service.tests.test_estimate_construction import NEW, row
        for version in (None, 1, 2):
            with self.subTest(version=version):
                s = self.post(action='generate', inputs=NEW).json()['snapshot']
                if version != 2:
                    for tier in s['variants']:
                        row(s,'material_rebar',tier).update(unit='кг',basis='pkg_rebar_mass',price='90.00',
                            construction_price={'retail':'75','markup':'20','divisor':'1'})
                if version is None: s.pop('construction_quantity_version')
                else: s['construction_quantity_version'] = version
                s = evaluate(s)
                saved = self.post(action='save', snapshot=s, expected_revision=0).json()
                loaded = self.client.get(self.url, {
                    'organization': self.org.pk, 'id': saved['id'], 'version': 1,
                }).json()['snapshot']
                self.assertEqual(loaded.get('construction_quantity_version'), version)
                self.assertEqual(loaded['variants'], s['variants'])
                self.assertEqual(row(loaded,'material_sand')['amount'], '6726.00' if version is None else '6840.00')
                self.assertEqual(row(loaded,'material_rebar')['unit'], 'т' if version == 2 else 'кг')
                self.assertEqual(row(loaded,'material_rebar')['amount'],
                    {None:'113337.00',1:'113355.00',2:'135000.00'}[version])
                printed = self.client.get(reverse('pool_estimate_print', args=[saved['id'],1]))
                if version:
                    self.assertContains(printed,'Стоимость материала рассчитана по сметному количеству.')
                else: self.assertNotContains(printed,'Стоимость материала рассчитана по сметному количеству.')
                if version == 2:
                    self.assertContains(printed,'1,5 т')
                    self.assertContains(printed,'135000')

    def test_construction_manual_quantity_precision_survives_save_and_load(self):
        from pool_service.tests.test_estimate_construction import NEW, row
        s = self.post(action='generate', inputs=NEW).json()['snapshot']
        row(s, 'material_sand').update(quantity='5.9136', auto_quantity=False)
        row(s, 'material_rebar').update(quantity='1.234567', auto_quantity=False, price='80000', unit_price_manual=True)
        s = evaluate(s)
        before = deepcopy(row(s, 'material_sand'))
        saved = self.post(action='save', snapshot=s, expected_revision=0).json()
        loaded = self.client.get(self.url, {
            'organization': self.org.pk, 'id': saved['id'], 'version': 1,
        }).json()['snapshot']
        self.assertEqual(row(loaded, 'material_sand')['quantity'], before['quantity'])
        self.assertEqual(row(loaded, 'material_sand')['amount'], before['amount'])
        self.assertEqual(loaded['variants']['economy']['total'], s['variants']['economy']['total'])
        self.assertFalse(row(loaded, 'material_sand')['auto_quantity'])
        self.assertEqual(row(loaded, 'material_rebar')['quantity'], '1.234567')
        self.assertEqual(row(loaded, 'material_rebar')['amount'], '96000.00')
        self.assertEqual(row(loaded, 'material_rebar')['unit'], 'т')

    def test_construction_package_print_preserves_equipment_name_brand_and_model(self):
        from pool_service.tests.test_estimate_construction import NEW, row
        s = self.post(action='generate', inputs=NEW).json()['snapshot']
        for tier in s['variants']:
            row(s, 'pump', tier).update(
                name='Насос фильтрации', brand='SyntheticBrand', model='SyntheticModel-42',
            )
        saved = self.post(action='save', snapshot=s, expected_revision=0).json()
        printed = self.client.get(reverse('pool_estimate_print', args=[saved['id'], 1]))
        self.assertContains(
            printed, '<td>Насос фильтрации · SyntheticModel-42<br><small>SyntheticBrand', count=3,
        )

    def test_construction_package_print_blank_prices_shared_rates_and_tenant(self):
        from pool_service.tests.test_estimate_construction import NEW, row
        created=self.post(action='generate',inputs=NEW)
        self.assertEqual(created.status_code,200)
        s=created.json()['snapshot']
        self.assertEqual(row(s,'bowl_package')['price'],'20000.00')
        changed=self.post(action='work_price',snapshot=s,variant='economy',block='construction',row='bowl_package',price='0',all=False)
        self.assertEqual(changed.status_code,200)
        s=changed.json()['snapshot']
        for tier in s['variants']:self.assertEqual(row(s,'bowl_package',tier)['price'],'0.00')
        saved=self.post(action='save',snapshot=s,expected_revision=0).json()
        loaded=self.client.get(self.url,{'organization':self.org.pk,'id':saved['id'],'version':1}).json()['snapshot']
        self.assertEqual(row(loaded,'material_supplies')['amount'],row(s,'material_supplies')['amount'])
        printed=self.client.get(reverse('pool_estimate_print',args=[saved['id'],1]))
        self.assertContains(printed,'Материалы для строительства чаши')
        self.assertContains(printed,'data-price="delivery_concrete"></td>')
        self.assertContains(printed,'data-price="excavation_package"></td>')
        self.assertContains(printed,'возможности подъезда техники')
        self.assertNotContains(printed,'обязательно проверить с конструктором')
        forbidden=self.post(action='work_price',snapshot=s,organization=self.other.pk,variant='economy',block='construction',row='bowl_package',price='100')
        self.assertEqual(forbidden.status_code,403)

    def test_pvc_catalog_replacement_refresh_manual_overrides_and_history(self):
        from .test_estimate_pvc import row
        self.catalog=catalog.parse_catalog(feed(offer(99,'Комплект ПВХ Synthetic','999',description='')))
        self.fetch.return_value=self.catalog
        for identity,bid in [('piping','equipment'),('pvc_embedded','embedded')]:
            with self.subTest(identity=identity):
                self.catalog['products']['99']['price']='999.00'
                s=self.post(action='generate',inputs=INPUTS).json()['snapshot']
                chosen=self.post(action='catalog_apply',snapshot=s,variant='economy',block=bid,row=identity,product_id='99').json()['snapshot']
                self.assertEqual(row(chosen,identity)['price'],'999.00')
                self.assertFalse(row(chosen,identity)['unit_price_manual'])
                self.assertIn('оценка ПВХ по площади не применяется',row(chosen,identity)['construction_quantity_note'])
                saved=self.post(action='save',snapshot=chosen,expected_revision=0).json()
                self.catalog['products']['99']['price']='1200.00'
                recalculated=self.post(action='recalculate',snapshot=chosen,inputs={**chosen['inputs'],'length':'9'}).json()['snapshot']
                self.assertEqual(row(recalculated,identity)['price'],'999.00')
                self.assertFalse(row(recalculated,identity)['unit_price_manual'])
                refreshed=self.post(action='catalog_prices',snapshot=recalculated,variant='economy').json()
                self.assertEqual((refreshed['updated'],refreshed['skipped']),(1,0))
                self.assertEqual(row(refreshed['snapshot'],identity)['amount'],'1200.00')
                old=self.client.get(self.url,{'organization':self.org.pk,'id':saved['id'],'version':1}).json()['snapshot']
                self.assertEqual(row(old,identity)['price'],'999.00')
                self.assertFalse(row(old,identity)['unit_price_manual'])
                for manual in ['0.00',None,'123.45']:
                    for explicit_flag in [False,True]:
                        overridden=deepcopy(chosen)
                        row(overridden,identity).update(price=manual,unit_price_manual=explicit_flag)
                        recalculated=self.post(action='recalculate',snapshot=overridden,inputs=overridden['inputs']).json()['snapshot']
                        response=self.post(action='catalog_prices',snapshot=recalculated,variant='economy').json()
                        self.assertEqual((response['updated'],response['skipped']),(0,1))
                        self.assertEqual(row(response['snapshot'],identity)['price'],manual)
                legacy=deepcopy(chosen);legacy.pop('pvc_area_version')
                historical=self.post(action='recalculate',snapshot=legacy,inputs={**legacy['inputs'],'length':'10'}).json()['snapshot']
                self.assertEqual(row(historical,identity)['price'],'999.00')
                self.assertNotIn('pvc_area_version',historical)

    def test_pvc_explicit_catalog_source_survives_equal_area_and_repeated_refresh(self):
        from .test_estimate_pvc import row
        self.catalog=catalog.parse_catalog(feed(offer(99,'Комплект ПВХ Synthetic','999',description='')))
        self.fetch.return_value=self.catalog
        for identity,bid,area_price in [('piping','equipment','57600.00'),('pvc_embedded','embedded','48000.00')]:
            for selected_price in ['999.00',area_price]:
                for manual_before in ['0.00',None,'123.45']:
                    with self.subTest(identity=identity,selected_price=selected_price,manual_before=manual_before):
                        self.catalog['products']['99']['price']=selected_price
                        s=generate(INPUTS)
                        row(s,identity).update(price=manual_before,unit_price_manual=True)
                        s=self.post(action='catalog_apply',snapshot=s,variant='economy',block=bid,row=identity,product_id='99').json()['snapshot']
                        self.assertFalse(row(s,identity)['unit_price_manual'])
                        s=self.post(action='recalculate',snapshot=s,inputs={**s['inputs'],'length':'9'}).json()['snapshot']
                        for _ in range(2):
                            s=self.post(action='validate',snapshot=s).json()['snapshot']
                            self.assertEqual(row(s,identity)['price'],selected_price)
                            self.assertFalse(row(s,identity)['unit_price_manual'])
                        for updated_price in ['1200.00','1300.00']:
                            self.catalog['products']['99']['price']=updated_price
                            response=self.post(action='catalog_prices',snapshot=s,variant='economy').json()
                            self.assertEqual((response['updated'],response['skipped']),(1,0))
                            s=response['snapshot']
                            self.assertEqual(row(s,identity)['price'],updated_price)
                            self.assertFalse(row(s,identity)['unit_price_manual'])
