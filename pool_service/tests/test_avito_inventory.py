from django.test import SimpleTestCase
from pool_service.avito_inventory import build_inventory
from pool_service import avito_source_audit
from scripts.avito_readonly_receipt import inventory

class ActiveInventoryTests(SimpleTestCase):
    def test_full_partition_unknown_and_preorder_are_separate(self):
        items = {str(i): {"status": "active", "title": "PRIVATE"} for i in range(1, 6)}
        items["9"] = {"status": "old", "title": "PRIVATE"}
        upload = {"complete": True, "rows": [
            {"avito_id": 1, "ad_id": "1_1_43"}, {"avito_id": 2, "ad_id": "1_2_43"},
            {"avito_id": 3, "ad_id": "1_3_43"}, {"avito_id": 5, "ad_id": "1_5_43"}]}
        index = {"1_1_43": [{"xml_id": "1_1_43", "availability": "Под заказ"}],
                 "1_2_43": [{"xml_id": "1_2_43", "availability": "В наличии"}],
                 "1_5_43": [{"xml_id": "1_5_43"}, {"xml_id": "1_5_43"}]}
        result = build_inventory(items, upload, index, {"1_1_43": 0, "1_2_43": 0})
        self.assertEqual(5, result["total"])
        self.assertEqual(2, result["quantity_verified_rows"])
        self.assertEqual(2, result["nonpositive_shop_quantity_rows"])
        self.assertEqual(1, result["in_stock_nonpositive_rows"])
        self.assertEqual(1, result["preorder_rows"])
        self.assertEqual("not_in_xml", result["rows"][2]["link_state"])
        self.assertIsNone(result["rows"][2]["shop_quantity"])
        self.assertEqual("not_in_last_report", result["rows"][3]["link_state"])
        self.assertEqual("ambiguous", result["rows"][4]["link_state"])
        self.assertNotIn("PRIVATE", str(result))
        self.assertFalse(result["saved_shop_stock_comparison_complete"])
        self.assertFalse(result["physical_stock_verified"])

    def test_duplicate_stock_rows_cannot_prove_quantity(self):
        payload = b'<items><item><id>1_1_43</id><stock>3</stock></item><item><id>1_1_43</id><stock>0</stock></item></items>'
        values, total, invalid = avito_source_audit.stock_values(payload)
        self.assertEqual(({}, 2, 1), (values, total, invalid))
        result = build_inventory({"1": {"status": "active"}}, {"complete": True, "rows": [{"avito_id": 1, "ad_id": "1_1_43"}]},
                                 {"1_1_43": [{"xml_id": "1_1_43", "availability": "В наличии"}]}, values)
        self.assertIsNone(result["rows"][0]["shop_quantity"])
        self.assertEqual(0, result["quantity_verified_rows"])

    def test_denial_or_incomplete_source_does_not_produce_zero_counts(self):
        self.assertEqual({"error": "audit_report_unavailable"}, build_inventory({}, None, {}))
        self.assertEqual({"error": "source_unavailable"}, build_inventory({}, {"complete": True}, None))
        result = build_inventory({"1": {"status": "active"}}, {"complete": True, "rows": [{"avito_id": 1, "ad_id": "1_1_43"}]},
                                 {"1_1_43": [{"xml_id": "1_1_43", "availability": "Под заказ"}]}, None, "source_http_invalid")
        self.assertEqual("source_http_invalid", result["stock_error"])
        self.assertIsNone(result["rows"][0]["shop_quantity"])
        result["counts"]["report_xml_match"] = 0
        with self.assertRaises(ValueError):
            inventory(result)

    def test_shared_report_xml_id_is_ambiguous_even_if_other_ad_is_inactive(self):
        index = {"1_1_43": [{"xml_id": "1_1_43", "availability": "В наличии"}]}
        upload = {"complete": True, "rows": [{"avito_id": 101, "ad_id": "1_1_43"},
                                            {"avito_id": 102, "ad_id": "1_1_43"}]}
        for other_status in ("active", "old"):
            with self.subTest(other_status=other_status):
                result = build_inventory({"101": {"status": "active"}, "102": {"status": other_status}},
                                         upload, index, {"1_1_43": 3})
                self.assertTrue(all(row["link_state"] == "ambiguous" for row in result["rows"]))
                self.assertTrue(all(row["shop_quantity"] is None for row in result["rows"]))
                self.assertEqual(0, result["quantity_verified_rows"])
