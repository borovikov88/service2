"""Independent combined-night API audit. All catalogue facts/prices are synthetic."""
from copy import deepcopy
from decimal import Decimal
from unittest.mock import patch

from django.test import TestCase
from django.urls import reverse

from pool_service.tests import test_pool_estimates as base
from pool_service.tests.test_estimate_primary import PRIMARY, fixture
from pool_service.tests.test_estimate_catalog import feed, offer
from pool_service.services import estimate_catalog as catalog
from pool_service.services.pool_estimates import make_row


def rows(snapshot, tier="economy"):
    return base.block(snapshot, variant=tier)["rows"]


def role(snapshot, kind, tier="economy"):
    return next(r for r in rows(snapshot, tier) if r["kind"] == kind)


class NightIntegrationTests(TestCase):
    post = base.PoolEstimateViewsTests.post

    def setUp(self):
        base.PoolEstimateViewsTests.setUp(self)
        self.catalog = fixture()
        self.catalog["products"].update(catalog.parse_catalog(feed(
            offer(11, "Насос Synthetic unknown", "70"),
            offer(12, "Фильтр Synthetic unknown", "80"),
        ))["products"])
        self.patcher = patch("pool_service.estimate_views.get_catalog", return_value=self.catalog)
        self.patcher.start()
        self.addCleanup(self.patcher.stop)
        self.values = {**PRIMARY, "primary_pump_connection": "", "primary_valve_connection": ""}
        self.snapshot = self.api(action="generate", inputs=self.values)["snapshot"]

    def api(self, **data):
        response = self.post(**data)
        self.assertIn(response.status_code, (200, 201), response.content[:1000])
        return response.json()

    def load(self, project, version=1):
        response = self.client.get(self.url, {
            "organization": self.org.pk, "id": project, "version": version})
        self.assertEqual(response.status_code, 200)
        return response.json()["snapshot"]

    def save_snapshot(self, snapshot, **data):
        return self.api(action="save", snapshot=snapshot, expected_revision=0, **data)

    def test_linked_order_manual_price_versions_and_print_all_quotes(self):
        original = deepcopy(self.snapshot)
        before = self.save_snapshot(original)
        changed = deepcopy(original)
        for tier in changed["variants"]:
            equipment = rows(changed, tier)
            heater = role(changed, "heater", tier)
            heater.update(quantity="2", auto_quantity=False, selection_locked=True)
            primary = role(changed, "circulation_pump", tier)
            primary.update(price="12.34", unit_price_manual=True)
            # Preserve IDs and links while putting each child before its parent.
            equipment.reverse()
        expected_order = {tier: [r["id"] for r in rows(changed, tier)]
                          for tier in changed["variants"]}
        result = self.api(action="recalculate", snapshot=changed, inputs=changed["inputs"])["snapshot"]
        for tier in result["variants"]:
            self.assertEqual([r["id"] for r in rows(result, tier)], expected_order[tier])
            heater = role(result, "heater", tier)
            for kind in ("circulation_pump", "solenoid_valve"):
                child = role(result, kind, tier)
                self.assertEqual(child["heating_parent"], heater["id"])
                self.assertEqual(Decimal(child["quantity"]), 2)
            self.assertEqual(role(result, "circulation_pump", tier)["amount"], "24.00")
            expected_catalog_sum = Decimal("1176.00") if tier == "economy" else Decimal("1774.00")
            self.assertEqual(sum(Decimal(r["amount"]) for r in rows(result, tier)
                                 if r.get("catalog")), expected_catalog_sum)
        second = self.api(action="save", snapshot=result, id=before["id"], expected_revision=1)
        self.assertEqual(second["revision"], 2)
        self.assertEqual(self.load(before["id"], 1), original)
        self.assertEqual(self.load(before["id"], 2), result)
        html = self.client.get(reverse("pool_estimate_print", args=[before["id"], 2])).content.decode()
        self.assertIn("Не подтверждены", html)
        self.assertIn(">12</td>", html)
        self.assertLess(html.index(role(result, "circulation_pump")["name"]),
                        html.index(role(result, "heater")["name"]))
        self.assertEqual(self.post(action="save", snapshot=result, id=before["id"],
                                   expected_revision=1).status_code, 409)

    def test_unknown_warnings_survive_api_roundtrip_without_totals_drift(self):
        first = self.save_snapshot(self.snapshot)
        loaded = self.load(first["id"])
        validated = self.api(action="validate", snapshot=loaded)["snapshot"]
        recalculated = self.api(action="recalculate", snapshot=validated,
                                inputs=validated["inputs"])["snapshot"]
        for tier in loaded["variants"]:
            self.assertEqual(recalculated["variants"][tier]["total"], loaded["variants"][tier]["total"])
            for kind in ("pump", "filter"):
                item = role(recalculated, kind, tier)
                self.assertIn("производительность", item["selection_note"])
                self.assertIn("Проверить", item["compatibility"]["message"])
                self.assertIsNone(item["specs"]["flow" if kind == "pump" else "filter_flow"])
        self.assertEqual(self.load(first["id"]), loaded)

    def test_deleted_custom_rows_and_empty_block_do_not_reappear(self):
        changed = deepcopy(self.snapshot)
        for tier in changed["variants"]:
            options = base.block(changed, "options", tier)
            options["rows"] = [make_row("night_custom", "Deleted synthetic item", "шт.", "1")]
            options["rows"][0]["price"] = "99.00"
        first = self.save_snapshot(changed)
        loaded = self.load(first["id"])
        for tier in loaded["variants"]:
            base.block(loaded, "options", tier)["rows"] = []
        validated = self.api(action="validate", snapshot=loaded)["snapshot"]
        for tier in validated["variants"]:
            self.assertEqual(base.block(validated, "options", tier)["rows"], [])
        self.api(action="save", snapshot=validated, id=first["id"], expected_revision=1)
        empty_loaded = self.load(first["id"], 2)
        for tier in empty_loaded["variants"]:
            self.assertEqual(base.block(empty_loaded, "options", tier)["rows"], [])
        result = self.api(action="recalculate", snapshot=empty_loaded, inputs=empty_loaded["inputs"])["snapshot"]
        for tier in result["variants"]:
            options = base.block(result, "options", tier)["rows"]
            # Existing add_works replenishes only these excluded optional templates.
            self.assertEqual({r["id"] for r in options}, {"work_countercurrent", "work_waterfall"})
            self.assertTrue(all(r["excluded"] and r["amount"] is None for r in options))
            self.assertEqual(Decimal(self.load(first["id"])["variants"][tier]["total"]) -
                             Decimal(result["variants"][tier]["total"]), Decimal("99.00"))
        self.api(action="save", snapshot=result, id=first["id"], expected_revision=2)
        html = self.client.get(reverse("pool_estimate_print", args=[first["id"], 3])).content.decode()
        self.assertNotIn("Deleted synthetic item", html)
