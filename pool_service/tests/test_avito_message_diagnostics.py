import json
import subprocess
import sys
from copy import deepcopy
from pathlib import Path
from unittest import TestCase

from pool_service.avito_report_messages import classify, groups, message_diagnostics
from scripts.avito_message_diagnostics import (project_diagnostics, shape, system_title_skeleton,
                                                SKELETON_PLACEHOLDER, SYSTEM_WORDS)
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

    def test_redacted_system_title_retains_only_canonical_words_and_negation_in_order(self):
        title = "Объявление НЕ найдено: AvitoId 1234567890"
        result = project_diagnostics(message_diagnostics(raw_message(title=title), raw_message(title=title)))
        self.assertIsNone(result["system_title"])
        self.assertEqual("объявление не найдено avitoid [скрыто]", result["system_title_redacted"])
        self.assertEqual("redacted", result["system_title_redacted_policy"])
        self.assertNotIn("1234567890", result["system_title_redacted"])
        self.assertTrue(all(part in SYSTEM_WORDS or part == SKELETON_PLACEHOLDER
                            for part in result["system_title_redacted"].split()))

    def test_redacted_title_hides_whole_quotes_contacts_paths_and_unknown_values(self):
        values = ("PrivateName", "1234567890", "private-error", "123-error", "error_private_id",
                  "https://private.invalid/error/id",
                  "user@private.invalid", "id.error", "/home/private/error/id", r"C:\private\error\id",
                  "«Объявление AvitoId»", '"Объявление AvitoId"', "'Объявление AvitoId'",
                  chr(96) + "Объявление AvitoId" + chr(96), "“Объявление AvitoId”",
                  "‹Объявление AvitoId›", "„Объявление AvitoId“", "‘Объявление AvitoId’",
                  "<Объявление AvitoId>", "[Объявление AvitoId]", "{Объявление AvitoId}")
        for value in values:
            with self.subTest(value=value):
                skeleton, policy = system_title_skeleton("Параметр " + value)
                self.assertEqual("параметр [скрыто]", skeleton)
                self.assertEqual("redacted", policy)
                self.assertNotIn("private", skeleton.casefold())
                self.assertNotIn("объявление", skeleton)
                self.assertNotIn("avitoid", skeleton)

    def test_skeleton_hides_assignment_and_identifier_values_including_allowed_word_fragments(self):
        for title in ("AvitoId=error", "AvitoId:error", "AvitoId#error"):
            with self.subTest(title=title):
                self.assertEqual(("avitoid [скрыто]", "redacted"), system_title_skeleton(title))
        self.assertEqual(("параметр [скрыто]", "redacted"), system_title_skeleton("Параметр=error"))

    def test_nested_mixed_and_escaped_quoted_values_cannot_leak_approved_tail_words(self):
        values = ("«Объявление «Id» Цена»", "[Объявление [Id] Цена]",
                  "{Объявление {Id} Цена}", "<Объявление <Id> Цена>",
                  "«Объявление [Id] Цена»", '"Объявление «Id» Цена"',
                  '"Описание \\" Цена"')
        for value in values:
            with self.subTest(value=value):
                self.assertEqual(("параметр [скрыто]", "redacted"),
                                 system_title_skeleton("Параметр " + value))
                result = project_diagnostics(message_diagnostics(
                    raw_message(title="Параметр " + value), raw_message(title="Параметр " + value)))
                self.assertEqual("параметр [скрыто]", result["system_title_redacted"])

    def test_skeleton_suppresses_complete_title_on_credential_marker_even_inside_quotes(self):
        for title in ("Описание client_secret PRIVATE", "Описание «access_token PRIVATE»",
                      "Ошибка Bearer PRIVATE", "Параметр пароль PRIVATE"):
            with self.subTest(title=title):
                result = project_diagnostics(message_diagnostics(raw_message(title=title), raw_message(title=title)))
                self.assertIsNone(result["system_title_redacted"])
                self.assertEqual("sensitive_marker", result["system_title_redacted_policy"])
                self.assertNotIn("PRIVATE", json.dumps(result))

    def test_synthetic_92_character_warning_diagnostic_does_not_guess_numeric_code_meaning(self):
        title = "Объявление: " + "q" * (92 - len("Объявление: "))
        entry = raw_message(title=title, description="")
        entry["diagnostics"] = message_diagnostics(entry, entry)
        result = groups([{"messages": [entry]}])[0]
        self.assertEqual(92, result["diagnostics"]["raw_title"]["length"])
        self.assertEqual(0, result["diagnostics"]["raw_description"]["length"])
        self.assertEqual("объявление [скрыто]", result["diagnostics"]["system_title_redacted"])
        self.assertEqual("review_required", result["category"])
        self.assertFalse(result["binding_evidence"])
        self.assertFalse(result["action_evidence"])
        self.assertNotIn("qqq", json.dumps(result))

    def test_skeleton_projection_rejects_forged_value_fragments_policy_or_noncanonical_tokens(self):
        diagnostic = message_diagnostics(raw_message(title="Параметр PRIVATE"), raw_message(title="Параметр PRIVATE"))
        for skeleton in ("параметр PrivateName", "параметр 123", "параметр user@example.invalid",
                         "параметр https://private.invalid", "Параметр [скрыто]",
                         "параметр [скрыто] [скрыто]", "[скрыто]", "параметр [PRIVATE]"):
            with self.subTest(skeleton=skeleton):
                bad = dict(diagnostic, system_title_redacted=skeleton)
                with self.assertRaises(ValueError):
                    project_diagnostics(bad)
        with self.assertRaises(ValueError):
            project_diagnostics(dict(diagnostic, system_title_redacted_policy="safe"))

    def test_old_cached_diagnostics_and_ad_rows_do_not_gain_title_skeleton_evidence(self):
        old = message_diagnostics(raw_message(), raw_message())
        old.pop("system_title_redacted")
        old.pop("system_title_redacted_policy")
        self.assertNotIn("system_title_redacted", project_diagnostics(old))
        ad = {"title": "Объявление: PrivateName", "description": "PRIVATE"}
        result = project_diagnostics(message_diagnostics(ad, ad))
        self.assertIsNone(result["system_title_redacted"])
        self.assertEqual("not_system_message", result["system_title_redacted_policy"])

    def test_skeleton_input_and_output_are_bounded_and_nontext_is_not_stringified(self):
        for value in ("объявление " * 30, "объявление " * 200, "q" * 2001):
            self.assertEqual((None, "too_long"), system_title_skeleton(value))
        self.assertEqual((None, "non_text"), system_title_skeleton({"PRIVATE": "error"}))
        self.assertEqual((None, "all_withheld"), system_title_skeleton("PRIVATE 123"))
        self.assertEqual(("параметр [скрыто]", "redacted"),
                         system_title_skeleton('Параметр «Объявление AvitoId'))
        self.assertEqual(("параметр [скрыто]", "redacted"),
                         system_title_skeleton("Параметр １２３abc"))

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
