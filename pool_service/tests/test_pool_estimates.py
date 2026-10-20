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


INPUTS = {"title": "Синтетический тест", "length": "8", "width": "4", "depth_start": "1.5", "depth_end": "1.5", "turnover_hours": "6", "location": "indoor", "heating": "none", "power": "unknown"}


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
        self.assertEqual(self.snapshot["geometry"], {"volume": "48.000", "surface": "32.000", "perimeter": "24.000", "finish_area": "68.000", "finish_net": "68.000", "finish_stock": "79.000", "required_flow": "8.000"})
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
        self.assertContains(response, "12.34")
        self.assertContains(response, "&lt;script&gt;")
        self.assertNotContains(response, '<script>alert(')
        self.assertNotContains(response, "Excluded synthetic filter")
        self.assertNotContains(response, "Материалы для чаши")
        self.assertContains(response, "Предварительный расчёт")

    def test_search_is_limited_to_current_organization(self):
        self.save()
        self.client.force_login(self.outsider)
        response = self.client.get(self.url, {"organization": self.other_org.pk})
        self.assertEqual(response.json()["projects"], [])
