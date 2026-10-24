import json
import subprocess
import sys
from copy import deepcopy
from pathlib import Path
from unittest import TestCase

from pool_service.avito_report_messages import classify, groups, message_diagnostics
from scripts.avito_message_diagnostics import project_diagnostics, shape
from scripts import avito_readonly_receipt as receipt


def raw_message(**extra):
    return {"type": "warning", "code": 1218, "title": "Привязка существующего объявления",
            "description": "Ничего исправлять не нужно.", **extra}


class MessageDiagnosticsTests(TestCase):
    def test_empty_missing_and_nontext_source_fields_are_distinct_without_values(self):
        raw = {"type": "warning", "code": 1218, "description": {"text": "PRIVATE"},
               "message": "PRIVATE", "body": "PRIVATE", "ad_title": "PRIVATE"}
        normalized = {"type": "warning", "code": 1218, "title": "", "description": ""}
        result = project_diagnostics(message_diagnostics(raw, normalized))
        self.assertFalse(result["raw_title"]["present"])
        self.assertEqual("missing", result["raw_title"]["type"])
        self.assertEqual({"present": True, "type": "object", "length": 1, "capped": False}, result["raw_description"])
        self.assertTrue(result["alternate_text_keys"]["message"])
        self.assertEqual(0, result["normalized_description_length"])
        self.assertNotIn("PRIVATE", json.dumps(result))
        self.assertNotIn("body", result)
        self.assertNotIn("ad_title", result)

    def test_empty_raw_text_is_not_misreported_as_redaction(self):
        diagnostic = message_diagnostics(raw_message(title="", description=""),
            {"type": "warning", "code": 1218, "title": "", "description": ""})
        result = project_diagnostics(diagnostic)
        self.assertTrue(result["raw_title"]["present"])
        self.assertEqual(0, result["raw_title"]["length"])
        self.assertFalse(result["title_entirely_withheld"])
        self.assertFalse(result["normalized_title_fully_redacted"])
        self.assertEqual("empty", result["system_title_policy"])

    def test_static_system_title_is_allowlisted_and_raw_ad_title_is_not_a_source(self):
        result = project_diagnostics(message_diagnostics(raw_message(), raw_message()))
        self.assertEqual("Привязка существующего объявления", result["system_title"])
        self.assertEqual("safe", result["system_title_policy"])
        ad = {"title": "Описание услуги", "Description": "PRIVATE", "body": "PRIVATE"}
        result = project_diagnostics(message_diagnostics(ad, ad))
        self.assertIsNone(result["system_title"])
        self.assertFalse(result["provider_message_shape_valid"])
        self.assertEqual("not_system_message", result["system_title_policy"])
        self.assertNotIn("PRIVATE", json.dumps(result))

    def test_dynamic_title_values_are_withheld_instead_of_becoming_public_labels(self):
        titles = ("Ошибка: Александр", "Объявление 123", "Описание «PRIVATE»",
                  "Цена 100", "Контакты user@example.invalid", "Ошибка https://private.invalid",
                  "client_secret PRIVATE", "Описание /home/PRIVATE/file",
                  "Описание " + chr(96) + "PRIVATE" + chr(96))
        for title in titles:
            with self.subTest(title=title):
                result = project_diagnostics(message_diagnostics(raw_message(title=title), raw_message(title=title)))
                self.assertIsNone(result["system_title"])
                self.assertTrue(result["title_entirely_withheld"])
                self.assertNotIn("PRIVATE", json.dumps(result))
                self.assertNotIn("private.invalid", json.dumps(result))

    def test_local_semantic_flags_survive_full_redaction_without_disclosing_description(self):
        raw = raw_message(title="client_secret PRIVATE",
            description="Привязка существующего объявления выполнена по AvitoId. Ничего исправлять не нужно. PRIVATE")
        normalized = {"type": "warning", "code": 1218, "title": "[текст с учётными данными скрыт]",
                      "description": "[текст с учётными данными скрыт]"}
        normalized["diagnostics"] = message_diagnostics(raw, normalized)
        result = groups([{"messages": [normalized]}])[0]
        self.assertEqual("existing_listing_link", result["category"])
        self.assertIn("AvitoId", result["fields"])
        self.assertTrue(result["diagnostics"]["normalized_description_fully_redacted"])
        self.assertTrue(result["diagnostics"]["title_entirely_withheld"])
        self.assertNotIn("PRIVATE", json.dumps(result))
        self.assertNotIn("source_semantics", result["diagnostics"])

    def test_binding_noun_stem_remains_conservative_about_negation_and_instructions(self):
        self.assertEqual("existing_listing_link", classify(raw_message(
            description="Существующее объявление ранее опубликовано."))["category"])
        for description in ("Привязка существующего объявления не выполнена.",
                            "Не удалось выполнить привязку существующего объявления.",
                            "Привязка существующего объявления: укажите AvitoId.",
                            "Привязка не поддерживается для существующего объявления."):
            with self.subTest(description=description):
                self.assertNotEqual("existing_listing_link", classify(raw_message(description=description))["category"])
        self.assertEqual("review_required", classify(raw_message(title="", description=""))["category"])

    def test_per_code_sample_consistency_is_explicit_and_never_claims_raw_text_equality(self):
        first, second = raw_message(), raw_message(title="")
        first["diagnostics"] = message_diagnostics(first, first)
        second["diagnostics"] = message_diagnostics(second, second)
        result = groups([{"messages": [first]}, {"messages": [second]}])[0]
        self.assertEqual(2, result["rows"])
        self.assertFalse(result["diagnostics_consistent"])
        self.assertNotIn("_message_count", result)
        missing = groups([{"messages": [first]}, {"messages": [raw_message()]}])[0]
        self.assertFalse(missing["diagnostics_consistent"])

    def test_projection_rejects_unsafe_title_and_drops_private_extra_fields(self):
        diagnostic = message_diagnostics(raw_message(), raw_message())
        diagnostic["raw_description"]["raw_value"] = "PRIVATE"
        diagnostic["source_semantics"]["unrecognised"] = "PRIVATE"
        diagnostic["extra"] = "PRIVATE"
        self.assertNotIn("PRIVATE", json.dumps(project_diagnostics(diagnostic)))
        for replacement in ("Александр", "Объявление 123", "client_secret PRIVATE"):
            bad = deepcopy(diagnostic)
            bad["system_title"] = replacement
            bad["system_title_policy"] = "safe"
            with self.assertRaises(ValueError):
                project_diagnostics(bad)
        bad = deepcopy(diagnostic)
        bad["raw_title"]["length"] = True
        with self.assertRaises(ValueError):
            project_diagnostics(bad)

    def test_shape_lengths_are_bounded_and_nonstring_values_are_not_stringified(self):
        result = shape("PRIVATE" * 10000)
        self.assertEqual(50000, result["length"])
        self.assertTrue(result["capped"])
        self.assertEqual("boolean", shape(True)["type"])
        self.assertIsNone(shape(True)["length"])
        self.assertEqual("null", shape(None)["type"])
        self.assertNotIn("PRIVATE", json.dumps(result))

    def test_standalone_receipt_import_works_with_scripts_only_on_application_path(self):
        root = Path(__file__).resolve().parents[2]
        command = ("import sys; sys.path.insert(0, " + repr(str(root / "scripts")) +
                   "); import avito_readonly_receipt; print(avito_readonly_receipt.FRAME.strip())")
        result = subprocess.run([sys.executable, "-I", "-c", command], cwd=root,
                                capture_output=True, text=True, timeout=10, check=True)
        self.assertEqual("AVITO_READONLY_AUDIT_RECEIPT", result.stdout.strip())

    def test_input_copy_projection_excludes_urls_hashes_and_private_bindings(self):
        proof = {"source": "provider_report_input_copy", "state": "verified_xml",
                 "code": "input_copy_xml_verified", "content_kind": "xml",
                 "identity_count": 11, "byte_count": 1000,
                 "file_sha256": "PRIVATE", "identity_sha256": "PRIVATE",
                 "account_id": "PRIVATE", "url": "https://private.invalid/?token=PRIVATE"}
        projected = receipt.input_copy_proof(proof)
        self.assertNotIn("PRIVATE", json.dumps(projected))
        self.assertEqual("verified_xml", projected["state"])
        for changed in ({"identity_count": 0}, {"content_kind": "xlsx"},
                        {"state": "unavailable"}, {"byte_count": True}):
            with self.subTest(changed=changed), self.assertRaises(ValueError):
                receipt.input_copy_proof(dict(proof, **changed))

    def test_optional_copy_recheck_failure_preserves_successful_complete_report(self):
        proof = {"source": "provider_report_input_copy", "state": "unavailable",
                 "code": "input_copy_report_recheck_failed", "content_kind": "unknown",
                 "identity_count": 0, "byte_count": 0, "provider_error": "PRIVATE"}
        source = {"upload_id": "613521535", "upload_status": "success_warning",
                  "complete": True, "finished": True, "total": 11,
                  "counts": {"error": 0, "warning": 11, "alarm": 0, "info": 11},
                  "critical_samples": [], "input_copy_proof": proof}
        result = receipt.report(source)
        self.assertTrue(result["complete"])
        self.assertTrue(result["finished"])
        self.assertEqual("success_warning", result["upload_status"])
        self.assertNotIn("error", result)
        self.assertEqual("unavailable", result["input_copy_proof"]["state"])
        self.assertEqual("input_copy_report_recheck_failed", result["input_copy_proof"]["code"])
        self.assertNotIn("PRIVATE", json.dumps(result))

    def test_services_inventory_is_explicitly_not_applicable_and_cannot_expose_shop_data(self):
        value = {"scope": "services", "state": "not_applicable",
                 "reason": "services_have_no_physical_stock", "shop_quantity": "PRIVATE"}
        self.assertEqual({"scope": "services", "state": "not_applicable",
                          "reason": "services_have_no_physical_stock"}, receipt.inventory(value))
        with self.assertRaises(ValueError):
            receipt.inventory(dict(value, reason="PRIVATE"))
        item = {"item_id": "11", "status": "active", "link_state": "services_feed_match",
                "autoload_xml_id": "1_2_3", "xml_matches": [], "report_xml_ids": []}
        self.assertEqual("services_feed_match", receipt.item(item)["link_state"])
        with self.assertRaises(ValueError):
            receipt.item(dict(item, xml_matches=[{"xml_id": "PRIVATE"}]))

    def test_services_coverage_distinguishes_unknown_provider_counts_from_zero(self):
        value = {"source": "service2_services_feed", "complete": False,
                 "draft_count": 11, "published_count": 11, "requested_count": 2,
                 "checked_count": 0, "skipped_out_of_scope": 2,
                 "active_count": None, "active_covered": None, "active_missing": None,
                 "published_inactive": None, "private": "PRIVATE"}
        result = receipt.services_coverage(value)
        self.assertIsNone(result["active_count"])
        self.assertNotIn("PRIVATE", json.dumps(result))
        complete = dict(value, complete=True, active_count=11, active_covered=11,
                        active_missing=0, published_inactive=0)
        self.assertEqual(0, receipt.services_coverage(complete)["active_missing"])
        with self.assertRaises(ValueError):
            receipt.services_coverage(dict(complete, active_missing=1))
