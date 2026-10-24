import json
import io
from unittest.mock import patch

from django.contrib.auth.models import User
from django.core.management import call_command
from django.test import SimpleTestCase, TestCase

from pool_service import avito_readonly_audit as audit
from pool_service.communication_avito import AvitoError
from pool_service.communication_models import AvitoCredential, ChannelConnection, CommunicationAccess, CommunicationChannel
from pool_service.models import Notification, Organization, OrganizationAccess
from scripts import avito_readonly_receipt as receipt


def feed(extra=""):
    return ('<Ads><Ad><Id>1_2_3</Id><Title>Лагуна 2,0 х 1,25</Title><Price>41990</Price><Availability>Под заказ</Availability></Ad>' + extra + '</Ads>').encode()

def report():
    return {"complete": True, "upload_finished": True, "upload_id": "900", "upload_status": "success_warning",
            "total": 2, "counts": {"error": 1, "warning": 1},
            "rows": [{"ad_id": "1_2_3", "avito_id": 101, "messages": [{"type": "error", "code": 7,
                      "title": "Категория", "description": "Исправьте категорию."}]},
                     {"ad_id": "1_9_3", "avito_id": 102, "messages": []}]}


class ReadonlyReceiptTests(SimpleTestCase):
    def test_xml_link_uses_exact_autoload_id_and_never_price_or_title(self):
        index, source = audit.feed_index(feed())
        linked = audit.item_link("101", {"status": "active", "autoload_item_id": "1_2_3"}, index)
        old = audit.item_link("102", {"status": "active", "autoload_item_id": "1_9_3"}, index)
        self.assertEqual(linked["link_state"], "exact_xml_match")
        self.assertEqual(linked["xml_matches"][0]["price"], "41990")
        self.assertEqual(old["link_state"], "not_in_current_xml")
        self.assertEqual(source["laguna_candidates"], linked["xml_matches"])
        self.assertNotIn("Title", str(source))

    def test_duplicate_xml_id_is_ambiguous_and_unknown_source_is_not_empty(self):
        index, _ = audit.feed_index(feed("<Ad><Id>1_2_3</Id><Price>46990</Price></Ad>"))
        detail = {"status": "active", "autoload_item_id": "1_2_3"}
        self.assertEqual(audit.item_link("101", detail, index)["link_state"], "xml_id_ambiguous")
        self.assertEqual(audit.item_link("101", detail, None)["link_state"], "xml_unavailable")
        self.assertEqual(audit.item_link("101", dict(detail, autoload_item_id=""), index)["link_state"], "no_autoload_id")

    def test_invalid_item_ids_and_unsafe_xml_ids_are_rejected_or_hidden(self):
        for value in ("", "101,101", "101;echo secret", " 101", "1," * 11, "1/../../"):
            with self.assertRaises(ValueError):
                audit.parse_item_ids(value)
        self.assertIsNone(audit.public_xml_id("client_secret=PRIVATE"))
        index, source = audit.feed_index(feed("<Ad><Id>PRIVATE CONTACT</Id><Price>NaN</Price></Ad>"))
        self.assertIsNone(index["PRIVATE CONTACT"][0]["xml_id"])
        self.assertIsNone(index["PRIVATE CONTACT"][0]["price"])
        self.assertNotIn("PRIVATE CONTACT", str(source))
        for amount in ("0E+999999999", "1e-999999", "NaN", "Infinity", "-1"):
            with self.assertRaises(ValueError):
                receipt.price(amount)

    def test_provider_error_and_message_projection_withhold_credentials(self):
        self.assertEqual(audit.safe_error(RuntimeError("PRIVATE PASSWORD")), "audit_read_failed")
        self.assertEqual(audit.safe_error(AvitoError("provider_http_403")), "provider_http_403")
        raw = report()
        raw["rows"][0]["messages"][0]["description"] = "client_secret = PRIVATE email@invalid.test"
        result = audit.report_summary(raw)
        self.assertNotIn("PRIVATE", json.dumps(result))
        self.assertEqual(receipt.report(result)["total"], 2)
        result["counts"]["error"] = 3
        with self.assertRaises(ValueError):
            receipt.report(result)

    def test_receipt_normalization_drops_unrelated_private_fields(self):
        value = {"schema_version": 1, "status": "partial", "checked_at": "2026-10-13T12:00:00+07:00",
                 "source": {"error": "source_unavailable", "SECRET": "PRIVATE"},
                 "connections": [{"connection_id": 1, "error": "provider_http_403", "SECRET": "PRIVATE"}],
                 "SECRET": "PRIVATE"}
        result = receipt.normalize(value)
        self.assertNotIn("PRIVATE", json.dumps(result))
        self.assertFalse(result["full_account_listing_scan_verified"])
        self.assertFalse(result["account_feed_url_verified"])


class ReadonlyAvitoOperatorTests(TestCase):
    def setUp(self):
        self.org = Organization.objects.create(name="Synthetic Avito audit")
        self.owner = User.objects.create_user("audit-owner")
        OrganizationAccess.objects.create(organization=self.org, user=self.owner, role="owner")
        channel = CommunicationChannel.objects.create(organization=self.org, kind="avito", name="Avito")
        self.connection = ChannelConnection.objects.create(channel=channel, external_id="123", name="Synthetic")
        AvitoCredential.objects.create(connection=self.connection, client_id_encrypted="synthetic-id", client_secret_encrypted="synthetic-secret")
        self.index, _ = audit.feed_index(feed())
        advance = patch.object(audit.avito_advance, "fetch", return_value={"kopeks": 1000, "amount": "10.00", "currency": "RUB", "source": "cpa_v3"})
        advance.start()
        self.addCleanup(advance.stop)

    def collect_connection(self, detail=None, upload=None, account=None):
        with patch.object(audit, "authorized_account_id", side_effect=account or ["123", "123"]), \
             patch.object(audit, "access_token", return_value="synthetic-token"), \
             patch.object(audit, "_provider_root", return_value="https://api.avito.ru"), \
             patch.object(audit, "fetch_profile_source", return_value=(
                 {"complete": True, "autoload_enabled": True, "shop_feed_source": "other_or_unknown",
                  "shop_feed_diagnostics": []}, "synthetic-fingerprint")), \
             patch.object(audit, "fetch_detail", side_effect=detail or [
                 {"status": "active", "autoload_item_id": "1_2_3"},
                 {"status": "old", "autoload_item_id": "1_9_3"}]), \
             patch.object(audit.avito_autoload_report, "fetch_report", side_effect=upload or [report()]), \
             patch.object(audit.avito_status_monitor, "full_scan", return_value=({"101": {"status": "active", "title": "Synthetic pool"}, "102": {"status": "active", "title": "synthetic   pool"}}, 1)):

            return audit.collect_connection(self.connection, ["101", "102"], self.index)

    def test_failed_live_webhook_read_does_not_hide_successful_storage_proof(self):
        from pool_service import avito_services_workplace_setup as setup
        proof = {"source": "passenger_http", "checked": True, "cached": True,
                 "outside_checkout": True, "file_private": True, "directory_private": True,
                 "readable": True, "synthetic_only": True}
        with patch.object(setup, "webhook_evidence", side_effect=AvitoError("provider_http_403")), \
             patch.object(setup, "storage_probe_evidence", return_value=proof):
            result = self.collect_connection()
        self.assertEqual({"error": "provider_http_403"}, result["webhook_evidence"])
        self.assertEqual(proof, result["storage_probe"])
        self.assertEqual(2, result["autoload_report"]["total"])
        self.assertTrue(receipt.connection(result)["storage_probe"]["checked"])

    def test_failed_storage_diagnostic_does_not_hide_independent_webhook_readback(self):
        from pool_service import avito_services_workplace_setup as setup
        webhook = {"binding_valid": True, "registration_confirmed": True,
                   "delivery_observed": False, "live_provider_checked": True, "live_registered": True}
        with patch.object(setup, "webhook_evidence", return_value=webhook), \
             patch.object(setup, "storage_probe_evidence", side_effect=RuntimeError("PRIVATE")):
            result = self.collect_connection()
        self.assertEqual(webhook, result["webhook_evidence"])
        self.assertEqual("storage_probe_failed", result["storage_probe"]["error"])
        self.assertEqual(2, result["autoload_report"]["total"])
        self.assertNotIn("PRIVATE", str(receipt.connection(result)))

    def test_full_scan_and_report_codes_are_complete_sanitized_aggregates(self):
        result = self.collect_connection()
        scan = result["listing_scan"]
        self.assertTrue(scan["complete"])
        self.assertEqual(2, scan["counts"]["active"])
        self.assertEqual(["101", "102"], scan["duplicate_candidates"][0]["item_ids"])
        self.assertNotIn("Synthetic pool", json.dumps(scan))
        raw = report()
        raw["started_at"] = "2026-10-13T09:00:00+00:00"
        raw["rows"][0]["messages"] *= 2
        summary = audit.report_summary(raw)
        self.assertEqual("2026-10-13T16:00:00+07:00", summary["started_at"])
        self.assertEqual([{"type": "error", "code": 7, "rows": 1}], summary["message_code_counts"])
        normalized = receipt.report(summary)
        self.assertEqual(summary["message_code_counts"], normalized["message_code_counts"])
        self.assertEqual(summary["started_at"], normalized["started_at"])
        scan["total"] = 3
        with self.assertRaises(ValueError):
            receipt.listing_scan(scan)

    def test_incomplete_listing_scan_remains_unknown_and_does_not_hide_report(self):
        with patch.object(audit, "listing_summary", side_effect=AvitoError("monitor_scan_limit")):
            result = self.collect_connection()
        self.assertEqual({"error": "monitor_scan_limit"}, result["listing_scan"])
        self.assertEqual(2, result["autoload_report"]["total"])
        self.assertNotIn("counts", result["listing_scan"])
        self.assertFalse(Notification.objects.exists())

    def test_real_scope_exact_ids_report_and_local_user_state_preserved(self):
        settings_before = self.connection.settings.copy()
        result = self.collect_connection()
        self.assertEqual(result["account_id"], "123")
        self.assertEqual(result["items"][0]["report_xml_ids"], ["1_2_3"])
        self.assertEqual(result["items"][1]["link_state"], "not_in_current_xml")
        self.assertEqual(result["autoload_report"]["counts"]["error"], 1)
        self.connection.refresh_from_db()
        self.assertEqual(self.connection.settings, settings_before)
        self.assertFalse(Notification.objects.exists())

    def test_mismatch_or_revoked_owner_withholds_all_results(self):
        result = self.collect_connection(account=["999"])
        self.assertEqual(result, {"connection_id": self.connection.pk, "error": "provider_account_mismatch"})
        CommunicationAccess.objects.filter(organization=self.org).update(can_manage_channels=False)
        with patch.object(audit, "authorized_account_id") as read:
            self.assertIn("error", audit.collect_connection(self.connection, ["101"], self.index))
        read.assert_not_called()

    def test_key_change_during_read_discards_details_and_report(self):
        def changed(*_args, **_kwargs):
            AvitoCredential.objects.filter(connection=self.connection).update(client_secret_encrypted="changed-secret")
            return report()
        result = self.collect_connection(upload=changed)
        self.assertEqual(result, {"connection_id": self.connection.pk, "error": "provider_account_mismatch"})

    def test_changes_during_final_self_read_discard_every_result(self):
        for mutation in ("keys", "capability"):
            with self.subTest(mutation=mutation):
                CommunicationAccess.objects.filter(organization=self.org).update(can_manage_channels=True)
                AvitoCredential.objects.filter(connection=self.connection).update(client_secret_encrypted="synthetic-secret")
                calls = [0]
                def final_self(_connection):
                    calls[0] += 1
                    if calls[0] == 2:
                        if mutation == "keys":
                            AvitoCredential.objects.filter(connection=self.connection).update(client_secret_encrypted="rotated-secret")
                        else:
                            CommunicationAccess.objects.filter(organization=self.org).update(can_manage_channels=False)
                    return "123"
                result = self.collect_connection(account=final_self)
                self.assertEqual(result, {"connection_id": self.connection.pk, "error": "provider_account_mismatch"})

    def test_duplicate_report_exposes_only_partial_metadata_without_reconciliation(self):
        from pool_service.tests.test_avito_autoload_report import item, page, upload
        with patch.object(audit.avito_workspace, "_get", side_effect=[
            upload(), page([item(), item()]), upload()
        ]), patch.object(audit.avito_workspace, "enforce_rate"):
            with self.assertRaises(audit.avito_autoload_report.ReportDuplicateRows) as caught:
                audit.avito_autoload_report.fetch_report("token", "123")
        result = self.collect_connection(upload=[caught.exception])
        raw = result["autoload_report"]
        self.assertEqual("autoload_report_duplicate_rows", raw["error"])
        self.assertEqual("2026-10-10T17:00:00+07:00", raw["duplicate_diagnostics"]["started_at"])
        self.assertFalse(raw["duplicate_diagnostics"]["complete"])
        self.assertEqual(1, raw["duplicate_diagnostics"]["duplicate_extra_rows"])
        self.assertNotIn("counts", raw)
        self.assertEqual({"error": "audit_report_unavailable"}, result["active_source_audit"])
        self.assertEqual(raw, receipt.report(raw))
        self.connection.refresh_from_db()
        self.assertEqual({}, self.connection.settings)
        self.assertFalse(Notification.objects.exists())

    def test_provider_denial_is_not_fabricated_no_link_or_successful_zero_report(self):
        result = self.collect_connection(detail=[AvitoError("provider_http_403"), {"status": "old", "autoload_item_id": ""}],
                                         upload=[AvitoError("provider_http_403")])
        self.assertEqual(result["items"][0], {"item_id": "101", "error": "provider_http_403"})
        self.assertEqual(result["autoload_report"], {"error": "provider_http_403"})

    def test_nonofficial_root_is_blocked_before_decryption_or_provider_read(self):
        with patch.object(audit, "_provider_root", return_value="https://attacker.invalid"), patch.object(audit, "access_token") as token, patch.object(audit, "authorized_account_id") as account:
            result = audit.collect_connection(self.connection, ["101"], self.index)
        self.assertEqual(result["error"], "audit_provider_root_invalid")
        token.assert_not_called()
        account.assert_not_called()

    def test_profile_source_drift_is_unknown_and_does_not_rebind_inventory(self):
        # Override the helper's normal synthetic fixture inside its own call.
        count = 0
        def side_effect(*args):
            nonlocal count
            count += 1
            return ({"complete": True, "autoload_enabled": True,
                     "shop_feed_source": "shop_aggregate_file", "shop_feed_diagnostics": []}, str(count))
        with patch.object(audit, "authorized_account_id", side_effect=["123", "123"]), \
             patch.object(audit, "access_token", return_value="synthetic-token"), \
             patch.object(audit, "_provider_root", return_value="https://api.avito.ru"), \
             patch.object(audit, "fetch_profile_source", side_effect=side_effect), \
             patch.object(audit, "fetch_detail", return_value={"status": "active", "autoload_item_id": "1_2_3"}), \
             patch.object(audit.avito_autoload_report, "fetch_report", return_value=report()), \
             patch.object(audit.avito_status_monitor, "full_scan", return_value=({"101": {"status": "active", "title": "Synthetic"}}, 1)):
            value = audit.collect_connection(self.connection, ["101"], self.index)
        self.assertEqual({"error": "audit_profile_source_changed"}, value["configured_feed_source"])
        self.assertTrue(value["listing_scan"]["complete"])

    def test_collect_fails_closed_on_more_than_two_connections_without_network(self):
        for index in range(2):
            connection = ChannelConnection.objects.create(channel=self.connection.channel, external_id=str(200 + index), name="Extra")
            AvitoCredential.objects.create(connection=connection, client_id_encrypted="synthetic", client_secret_encrypted="synthetic")
        with patch.object(audit.avito_source_audit, "_read_xml") as source, patch.object(audit, "authorized_account_id") as account:
            result = audit.collect(["101"])
        self.assertEqual(result["reason"], "audit_connection_limit")
        source.assert_not_called()
        account.assert_not_called()

    def test_cached_configuration_reports_local_settings_without_claiming_webhook_delivery(self):
        self.connection.settings = {
            "avito_webhook_status": "connected", "avito_webhook_checked_at": "2026-10-20T10:00:00+07:00",
            "avito_webhook_last_received_at": "2026-10-20T10:00:00+07:00",
            "avito_webhook_error": "PRIVATE", "client_secret": "PRIVATE",
        }
        self.connection.save(update_fields=["settings"])
        with patch.object(audit.avito_autoload_auto, "scheduler_state", return_value={"ready": True}):
            audit.avito_autoload_auto.configure(self.connection, self.owner, "enable")
        from pool_service.avito_feed_models import AvitoServicesFeed
        snapshot = [{"rows": [{"ImageUrls": " | ".join(
            f"https://photos.invalid/synthetic-{i}.jpg" for i in range(11))}]}]
        AvitoServicesFeed.objects.create(connection=self.connection, account_id="123",
            organization_id_at_import=self.org.pk, draft=snapshot, published=snapshot,
            enabled=True, revision=2, published_revision=2)
        with patch.object(audit, "access_token") as token:
            flags = audit.cached_configuration_state(self.connection)
        token.assert_not_called()
        self.assertTrue(flags["autoload_auto_scope_valid"])
        self.assertTrue(flags["autoload_auto_enabled"])
        self.assertTrue(flags["services_feed_account_bound"])
        self.assertTrue(flags["services_feed_revision_current"])
        self.assertTrue(flags["services_feed_legacy_photo_limit_exceeded"])
        self.assertEqual("connected", flags["webhook_cache_status"])
        self.assertFalse(flags["webhook_scope_known"])
        self.assertIsNone(flags["webhook_verified_cached"])
        self.assertIsNone(flags["webhook_received_cached"])
        projected = receipt.cached_configuration(dict(flags, public_key="PRIVATE",
            callback_url="https://private.invalid/PRIVATE", expires_on="PRIVATE"))
        self.assertFalse(projected["live_provider_verified"])
        self.assertNotIn("PRIVATE", json.dumps(projected))
        self.assertNotIn("2026-", json.dumps(projected))
        self.assertNotIn("photos.invalid", json.dumps(projected))

    def test_cached_configuration_empty_malformed_and_stale_scopes_are_unknown(self):
        flags = audit.cached_configuration_state(self.connection)
        self.assertFalse(flags["autoload_auto_configured"])
        self.assertFalse(flags["services_feed_configured"])
        self.assertEqual("unknown", flags["webhook_cache_status"])
        self.connection.settings = {"avito_autoload_auto": "PRIVATE", "avito_webhook_status": ["PRIVATE"]}
        self.connection.save(update_fields=["settings"])
        flags = audit.cached_configuration_state(self.connection)
        self.assertIsNone(flags["autoload_auto_configured"])
        self.assertIsNone(flags["autoload_auto_enabled"])
        self.assertIsNone(flags["autoload_auto_scope_valid"])
        self.assertEqual("unknown", flags["webhook_cache_status"])
        self.connection.settings = {"avito_autoload_auto": {"enabled": True, "account_id": "999"}}
        self.connection.save(update_fields=["settings"])
        flags = audit.cached_configuration_state(self.connection)
        self.assertTrue(flags["autoload_auto_enabled"])
        self.assertFalse(flags["autoload_auto_scope_valid"])
        ChannelConnection.objects.filter(pk=self.connection.pk).update(external_id="999")
        self.assertEqual({"error": "audit_configuration_scope_changed"},
                         audit.cached_configuration_state(self.connection))

    def test_cached_photo_inspection_uses_the_feed_separators_without_exporting_urls(self):
        rows = [{"ImageUrls": "\n".join("https://photos.invalid/synthetic-%s.jpg" % i for i in range(11))}]
        self.assertTrue(audit._legacy_photos_exceed_limit([{"rows": rows}]))
        rows[0]["ImageUrls"] = "https://photos.invalid/normal.jpg"
        self.assertFalse(audit._legacy_photos_exceed_limit([{"rows": rows}]))
        self.assertIsNone(audit._legacy_photos_exceed_limit([{"rows": ["PRIVATE"]}]))

    def test_cached_feed_drift_does_not_expose_other_account_snapshot_flags(self):
        from pool_service.avito_feed_models import AvitoServicesFeed
        feed = AvitoServicesFeed.objects.create(connection=self.connection, account_id="999",
            organization_id_at_import=self.org.pk, draft=[{"rows": [{"ImageUrls": "PRIVATE"}]}],
            published=[{"rows": [{"ImageUrls": "PRIVATE"}]}], enabled=True)
        flags = audit.cached_configuration_state(self.connection)
        self.assertTrue(flags["services_feed_configured"])
        self.assertFalse(flags["services_feed_account_bound"])
        for key in ("services_feed_enabled", "services_feed_revision_current",
                    "services_feed_legacy_photo_limit_exceeded"):
            self.assertIsNone(flags[key])
        feed.account_id = "123"
        feed.draft = "PRIVATE"
        feed.save(update_fields=["account_id", "draft"])
        self.assertIsNone(audit.cached_configuration_state(self.connection)["services_feed_legacy_photo_limit_exceeded"])

    def test_cached_configuration_projection_is_backward_compatible_and_rejects_false_proof(self):
        flags = audit.cached_configuration_state(self.connection)
        for changes in ({"live_provider_verified": True}, {"webhook_scope_known": True},
                        {"webhook_verified_cached": True}, {"autoload_auto_enabled": 1},
                        {"webhook_cache_status": "PRIVATE"}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                receipt.cached_configuration(flags | changes)
        old = self.collect_connection()
        self.assertIn("cached_configuration", old)
        old.pop("cached_configuration")
        self.assertEqual({"error": "audit_cached_configuration_not_requested"},
                         receipt.connection(old)["cached_configuration"])


class FullListingProjectionTests(SimpleTestCase):
    def test_full_scan_flags_are_per_scope_and_stock_audit_is_separate(self):
        scan = audit.listing_summary({"101": {"status": "active", "title": "client_secret PRIVATE"}}, 1)
        value = {"schema_version": 1, "status": "partial", "checked_at": "2026-10-13T12:00:00+07:00",
                 "source": {"error": "source_unavailable"},
                 "connections": [{"connection_id": 1, "account_id": "123", "items": [],
                                  "autoload_report": {"error": "provider_http_401"}, "listing_scan": scan}]}
        normalized = receipt.normalize(value)
        self.assertTrue(normalized["full_account_listing_scan_verified"])
        self.assertFalse(normalized["listing_stock_reconciliation_verified"])
        self.assertNotIn("PRIVATE", json.dumps(normalized))
        value["connections"].append({"connection_id": 2, "error": "provider_http_401"})
        self.assertFalse(receipt.normalize(value)["full_account_listing_scan_verified"])

    def test_error_samples_prioritize_errors_after_first_page_and_codes_count_all_rows(self):
        raw = report()
        alarm = {"type": "alarm", "code": 1134, "title": "ТН ВЭД", "description": "Документы"}
        raw["rows"] = [{"ad_id": f"1_{i}_3", "avito_id": 200 + i, "messages": [alarm]} for i in range(30)]
        raw["rows"].append({"ad_id": "1_99_3", "avito_id": 999,
                            "messages": [{"type": "error", "code": 1150, "title": "Удалено", "description": "Причина"}]})
        raw["counts"] = {"error": 1, "alarm": 30}
        raw["total"] = 31
        projected = receipt.report(audit.report_summary(raw))
        self.assertEqual("1_99_3", projected["critical_samples"][0]["xml_id"])
        self.assertEqual([{"type": "alarm", "code": 1134, "rows": 30},
                          {"type": "error", "code": 1150, "rows": 1}], projected["message_code_counts"])
        projected["message_code_counts"][0]["rows"] = 31
        with self.assertRaises(ValueError):
            receipt.report(projected)


class DuplicateReceiptProjectionTests(SimpleTestCase):
    def diagnostic(self):
        return {"complete": False, "upload_id": "123", "upload_status": "success_warning",
                "started_at": "2026-10-10T17:00:00+07:00", "read_rows": 51, "pages": 2,
                "unique_identity_pairs": 50, "duplicate_extra_rows": 1,
                "same_page_duplicate_rows": 0, "cross_page_duplicate_rows": 1,
                "identical_duplicate_rows": 1, "different_duplicate_rows": 0,
                "samples": [{"first_row": 1, "repeated_row": 51, "first_page": 1,
                             "repeated_page": 2, "identical_payload": True}]}

    def test_private_fields_and_business_counts_are_not_projected(self):
        diagnostic = self.diagnostic()
        diagnostic["PRIVATE"] = "SECRET"
        diagnostic["samples"][0]["PRIVATE"] = "SECRET"
        projected = receipt.report({"error": "autoload_report_duplicate_rows",
                                    "duplicate_diagnostics": diagnostic, "counts": {"error": 51},
                                    "complete": True, "rows": ["SECRET"]})
        self.assertNotIn("SECRET", json.dumps(projected))
        self.assertNotIn("counts", projected)
        self.assertNotIn("rows", projected)
        self.assertFalse(projected["duplicate_diagnostics"]["complete"])
        self.assertEqual({"error": "provider_http_403"}, receipt.report({
            "error": "provider_http_403", "duplicate_diagnostics": diagnostic}))

    def test_inconsistent_unbounded_or_non_numeric_diagnostics_fail_closed(self):
        variants = [
            {"complete": True}, {"upload_id": "PRIVATE"}, {"upload_status": "PRIVATE"},
            {"started_at": "2026-10-10T17:00:00"}, {"read_rows": True},
            {"read_rows": 5001}, {"unique_identity_pairs": 49}, {"duplicate_extra_rows": 0},
            {"same_page_duplicate_rows": 1},
            {"same_page_duplicate_rows": 1, "cross_page_duplicate_rows": 0},
            {"identical_duplicate_rows": 0},
            {"identical_duplicate_rows": 0, "different_duplicate_rows": 1}, {"pages": 1},
            {"samples": []}, {"samples": self.diagnostic()["samples"] * 6},
            {"samples": [{"first_row": 51, "repeated_row": 51, "first_page": 2,
                          "repeated_page": 2, "identical_payload": True}]},
            {"samples": [{"first_row": 1, "repeated_row": 51, "first_page": 1,
                          "repeated_page": 1, "identical_payload": True}]},
            {"samples": [{"first_row": 1, "repeated_row": 51, "first_page": 1,
                          "repeated_page": 2, "identical_payload": "SECRET"}]},
        ]
        for changes in variants:
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                receipt.report({"error": "autoload_report_duplicate_rows",
                                "duplicate_diagnostics": self.diagnostic() | changes})

    def test_second_repeat_on_same_new_page_is_not_counted_as_another_cross_page_repeat(self):
        diagnostic = self.diagnostic()
        diagnostic.update(read_rows=52, duplicate_extra_rows=2, same_page_duplicate_rows=1,
                          cross_page_duplicate_rows=1, identical_duplicate_rows=2)
        diagnostic["samples"].append({"first_row": 1, "repeated_row": 52, "first_page": 1,
                                      "repeated_page": 2, "identical_payload": True})
        value = {"error": "autoload_report_duplicate_rows", "duplicate_diagnostics": diagnostic}
        self.assertEqual(diagnostic, receipt.report(value)["duplicate_diagnostics"])
        diagnostic["same_page_duplicate_rows"] = 0
        diagnostic["cross_page_duplicate_rows"] = 2
        with self.assertRaises(ValueError):
            receipt.report(value)
        diagnostic["same_page_duplicate_rows"] = 1
        diagnostic["cross_page_duplicate_rows"] = 1
        diagnostic["samples"].reverse()
        with self.assertRaises(ValueError):
            receipt.report(value)


class ReportReadStrategyProjectionTests(SimpleTestCase):
    def test_only_fixed_read_strategies_are_projected_without_query_or_source_ids(self):
        raw = report()
        raw["read_strategy"] = "shop_xml_id_queries"
        projected = receipt.report(audit.report_summary(raw))
        self.assertEqual("shop_xml_id_queries", projected["read_strategy"])
        raw["read_strategy"] = "PRIVATE_URL"
        with self.assertRaises(ValueError):
            receipt.report(audit.report_summary(raw))


class AdvanceAccessPrivacyTests(SimpleTestCase):
    def test_availability_receipt_never_publishes_financial_amounts(self):
        raw = {"available": True, "source": "cpa_v3", "amount_logged": False,
               "kopeks": 123456789, "amount": "1234567.89", "client_secret": "PRIVATE"}
        self.assertEqual({"available": True, "source": "cpa_v3", "amount_logged": False},
                         receipt.advance_access(raw))
        self.assertNotIn("PRIVATE", str(receipt.advance_access(raw)))
        self.assertNotIn("123456", str(receipt.advance_access(raw)))
        for value in ({"available": True, "source": "PRIVATE", "amount_logged": False},
                      {"available": True, "source": "cpa_v3", "amount_logged": True}):
            with self.assertRaises(ValueError):
                receipt.advance_access(value)


def release_publication():
    return {"configured": True, "binding_present": False, "snapshot_valid": True,
        "current_publication": False, "draft_matches_published": True,
        "identity_sets_match": True, "photo_limit_ok": True, "http_checked": False,
        "revision": 15, "draft_revision": 17, "published_revision": 15, "history_version_id": None, "created_at": None,
        "checked_at": "2026-10-24T12:00:00+00:00", "draft_count": 2,
        "published_count": 2, "snapshot_count": 2, "max_photos": 1,
        "http_matches": None, "http_code": "publication_missing",
        "facts": {"file_published": False, "upload_found": False,
                  "source_confirmed": False, "rows_processed": False, "live_content_verified": False}}


def release_chain():
    return {"source": "avito_webhook_chain_v1", "state": "awaiting_first",
        "generation_bound": False, "authenticated_event": False, "conversation_bound": False,
        "message_persisted": False, "event_result": None, "received_at": None,
        "owner_notification_state": "awaiting_first", "owner_notification_recorded": False,
        "owner_notification_error": None, "push_delivery_confirmed": False}


def release_raw_fields():
    return {"source": "avito_item_detail_raw_fields", "endpoint_kind": "core_v1_account_item",
        "checked_at": "2026-10-24T12:00:00+00:00", "owned_status_verified": True,
        "sample_count": 1, "live_content_verified": False,
        "fields": {key: {spelling: {"present": False, "type": "missing", "count": None}
                         for spelling in ("lowercase", "capitalized")}
                   for key in ("title", "description", "images", "photos")}}


def release_photo():
    return {"source": "existing_services_draft_image", "scope_valid": True,
        "source_in_published": True, "photo_count": 1, "readable": True, "format": "JPEG",
        "width": 20, "height": 10, "bytes": 100, "checked_at": "2026-10-24T12:00:00+00:00"}


class ScopedServicesReleaseAuditTests(TestCase):
    def setUp(self):
        from pool_service.avito_feed_models import AvitoServicesFeed
        self.org = Organization.objects.create(name="Synthetic scoped services")
        self.owner = User.objects.create_user("scoped-audit-owner")
        OrganizationAccess.objects.create(organization=self.org, user=self.owner, role="owner")
        CommunicationAccess.objects.update_or_create(
            organization=self.org, user=self.owner, defaults={"can_manage_channels": True})
        channel = CommunicationChannel.objects.create(organization=self.org, kind="avito", name="Services")
        self.target = ChannelConnection.objects.create(
            pk=4, channel=channel, external_id="446312402", name="Services",
            settings={"private_setting": "PRIVATE"})
        AvitoCredential.objects.create(connection=self.target,
            client_id_encrypted="synthetic", client_secret_encrypted="synthetic")
        for pk in (1, 2, 3):
            other = ChannelConnection.objects.create(pk=pk, channel=channel, external_id=str(1000 + pk),
                name="Other", settings={"preserve": pk})
            AvitoCredential.objects.create(connection=other,
                client_id_encrypted="synthetic", client_secret_encrypted="synthetic")
        sheets = [{"headers": ["Id", "AvitoId", "ImageUrls"], "rows": [
            {"Id": "1_2_3", "AvitoId": "101", "Category": "Предложение услуг", "ImageUrls": "https://photos.invalid/approved.jpg"},
            {"Id": "1_9_3", "AvitoId": "102", "Category": "Предложение услуг", "ImageUrls": "https://photos.invalid/approved.jpg"}]}]
        self.feed = AvitoServicesFeed.objects.create(connection=self.target, account_id="446312402",
            organization_id_at_import=self.org.pk, draft=sheets, published=sheets,
            revision=15, published_revision=15, enabled=True, published_photo_limit=10)

    def test_other_connections_never_enter_scoped_reader_and_shop_sources_are_not_requested(self):
        before = list(ChannelConnection.objects.order_by("pk").values_list("pk", "settings"))
        with patch.object(audit, "collect_connection", return_value={"connection_id": 4, "error": "provider_http_403"}) as read, \
             patch.object(audit.avito_source_audit, "_read_xml") as shop, \
             patch.object(audit, "authorized_account_id") as provider:
            result = audit.collect_services()
        self.assertEqual([{"connection_id": 4, "error": "provider_http_403"}], result["connections"])
        self.assertEqual("services_readonly", result["scope"]["kind"])
        self.assertEqual(4, read.call_args.args[0].pk)
        self.assertEqual(([], None), read.call_args.args[1:])
        self.assertEqual({"release_evidence": True}, read.call_args.kwargs)
        read.assert_called_once()
        shop.assert_not_called()
        provider.assert_not_called()
        self.assertEqual(before, list(ChannelConnection.objects.order_by("pk").values_list("pk", "settings")))
        self.assertFalse(Notification.objects.exists())

    def test_scope_account_revision_and_missing_binding_fail_before_provider_access(self):
        with patch.object(audit, "collect_connection") as read, \
             patch.object(audit, "authorized_account_id") as provider, \
             patch.object(audit.avito_source_audit, "_read_xml") as shop:
            for args in ({"connection_id": 1}, {"account_id": "1001"}, {"revision": 16}, {"connection_id": True}):
                self.assertEqual("audit_scope_invalid", audit.collect_services(**args)["reason"])
            self.target.external_id = "1001"
            self.target.save(update_fields=["external_id"])
            self.assertEqual("audit_services_connection_unavailable", audit.collect_services()["reason"])
            self.target.external_id = "446312402"
            self.target.save(update_fields=["external_id"])
            self.feed.organization_id_at_import += 1
            self.feed.save(update_fields=["organization_id_at_import"])
            self.assertEqual("audit_services_binding_invalid", audit.collect_services()["reason"])
        read.assert_not_called()
        provider.assert_not_called()
        shop.assert_not_called()

    def test_feed_binding_drift_during_account_read_never_falls_back_to_goods_item_requests(self):
        from pool_service import avito_input_copy
        with patch.object(avito_input_copy, "bound_services_feed", side_effect=[self.feed, None]), \
             patch.object(audit, "_provider_root", return_value="https://api.avito.ru"), \
             patch.object(audit, "authorized_account_id", return_value="446312402"), \
             patch.object(audit, "access_token", return_value="synthetic-token"), \
             patch.object(audit, "fetch_profile_source", return_value=({"error": "provider_http_403"}, None)), \
             patch.object(audit.avito_advance, "fetch", return_value={}), \
             patch.object(audit, "fetch_detail") as detail, \
             patch.object(audit, "raw_content_probe") as probe:
            result = audit.collect_services(["7234733937"])
        self.assertEqual([{"connection_id": 4, "error": "provider_account_mismatch"}], result["connections"])
        detail.assert_not_called()
        probe.assert_not_called()

    def test_scoped_command_rejects_other_accounts_before_collector(self):
        from pool_service.management.commands import audit_avito_readonly as command
        from django.core.management.base import CommandError
        with patch.object(command, "collect_services", return_value={"status": "blocked"}) as collect:
            output = io.StringIO()
            call_command("audit_avito_readonly", stdout=output)
            collect.assert_called_once_with([], connection_id=4, account_id="446312402", revision=15)
            for args in ({"connection_id": 1}, {"account_id": "1001"}, {"revision": 16}):
                with self.assertRaises(CommandError):
                    call_command("audit_avito_readonly", stdout=io.StringIO(), **args)
            self.assertEqual(1, collect.call_count)

    def test_fresh_scoped_snapshot_preserves_settings_and_discards_every_proof_on_account_drift(self):
        from pool_service import avito_services_workplace_setup as setup
        helpers = {"publication_release": release_publication(),
                   "webhook_chain": release_chain(), "approved_photo": release_photo()}
        before = list(ChannelConnection.objects.order_by("pk").values_list("pk", "settings"))
        for final_account in ("446312402", "1001"):
            with self.subTest(final_account=final_account), \
                 patch.object(audit, "_provider_root", return_value="https://api.avito.ru"), \
                 patch.object(audit, "authorized_account_id", side_effect=["446312402", final_account]), \
                 patch.object(audit, "access_token", return_value="synthetic-token"), \
                 patch.object(audit, "fetch_profile_source", return_value=(
                     {"complete": True, "autoload_enabled": True, "shop_feed_source": "other_or_unknown",
                      "shop_feed_diagnostics": []}, "synthetic-private-fingerprint")), \
                 patch.object(audit.avito_advance, "fetch", return_value={}), \
                 patch.object(audit.avito_autoload_report, "fetch_report", return_value=report()), \
                 patch.object(audit.avito_status_monitor, "full_scan", return_value=(
                     {"101": {"status": "active"}, "102": {"status": "active"}}, 1)), \
                 patch.object(audit, "fetch_detail") as detail, \
                 patch.object(audit, "raw_content_probe", return_value=(
                     "101", {"status": "active", "autoload_item_id": "1_2_3"}, release_raw_fields())), \
                 patch.object(audit, "_services_release_evidence", return_value=helpers), \
                 patch.object(setup, "webhook_evidence", return_value={
                     "binding_valid": False, "registration_confirmed": False, "delivery_observed": False,
                     "live_provider_checked": False, "live_registered": None}), \
                 patch.object(setup, "storage_probe_evidence", return_value={
                     "source": "passenger_http", "checked": False, "cached": True,
                     "error": "storage_probe_not_run"}):
                value = audit.collect_services(["7234733937"])
            detail.assert_not_called()  # Foreign goods ID is never queried.
            row = value["connections"][0]
            if final_account == "446312402":
                self.assertEqual("awaiting_first", row["webhook_chain"]["state"])
                self.assertEqual(2, row["publication_release"]["snapshot_count"])
                self.assertTrue(row["approved_photo"]["source_in_published"])
                self.assertEqual(1, row["services_coverage"]["skipped_out_of_scope"])
                self.assertNotIn("private-fingerprint", str(value))
            else:
                self.assertEqual({"connection_id": 4, "error": "provider_account_mismatch"}, row)
            self.assertEqual(before, list(ChannelConnection.objects.order_by("pk").values_list("pk", "settings")))
            self.assertFalse(Notification.objects.exists())

    def test_release_helpers_survive_independent_failure_and_never_enter_legacy_collection(self):
        from pool_service import avito_services_publication as publication
        from pool_service import avito_webhook_chain as chain
        from pool_service import avito_services_media_livecheck as media
        with patch.object(publication, "publication_release_check", side_effect=RuntimeError("PRIVATE")), \
             patch.object(chain, "summary", return_value=release_chain()), \
             patch.object(media, "media_release_source", return_value=release_photo()):
            value = audit._services_release_evidence(self.target)
        self.assertEqual({"error": "audit_publication_release_unavailable"}, value["publication_release"])
        self.assertEqual("awaiting_first", value["webhook_chain"]["state"])
        self.assertTrue(value["approved_photo"]["source_in_published"])
        self.assertNotIn("PRIVATE", str(value))
        with patch.object(audit, "binding") as binding, \
             patch.object(audit, "access_token") as token, \
             patch.object(audit, "_services_release_evidence") as helpers:
            value = audit.collect_connection(ChannelConnection.objects.get(pk=1), [], None, release_evidence=True)
        self.assertEqual({"connection_id": 1, "error": "provider_account_mismatch"}, value)
        binding.assert_not_called()
        token.assert_not_called()
        helpers.assert_not_called()


class ServicesReleaseProjectionTests(SimpleTestCase):
    def test_publication_and_photo_projection_never_exports_urls_hashes_values_or_actors(self):
        for projector, raw in ((receipt.publication_release, release_publication()),
                               (receipt.approved_photo, release_photo()),
                               (receipt.webhook_chain, release_chain())):
            raw.update(private_url="https://private.invalid/PRIVATE", sha256="PRIVATE", actor_id=9876)
            result = projector(raw)
            self.assertNotIn("PRIVATE", str(result))
            self.assertNotIn("actor_id", result)
        bad = release_publication()
        bad["facts"]["live_content_verified"] = True
        with self.assertRaises(ValueError):
            receipt.publication_release(bad)
        for key, value in (("width", 0), ("height", 20000001), ("bytes", 8 * 1024 * 1024 + 1),
                           ("format", "SVG"), ("source_in_published", False), ("photo_count", 11)):
            with self.subTest(key=key), self.assertRaises(ValueError):
                receipt.approved_photo(release_photo() | {key: value})

    def test_message_or_owner_notification_cannot_be_inferred_from_registration_or_push_flag(self):
        self.assertFalse(receipt.webhook_chain(release_chain())["message_persisted"])
        for change in ({"push_delivery_confirmed": True}, {"state": "accepted_persisted"},
                       {"message_persisted": True}, {"owner_notification_recorded": True},
                       {"received_at": "2026-10-24T12:00:00"}, {"owner_notification_error": "PRIVATE"}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                receipt.webhook_chain(release_chain() | change)

    def test_scoped_receipt_rejects_foreign_or_multiple_accounts_and_ignores_shop_values(self):
        scoped = {"schema_version": 1, "status": "partial", "checked_at": "2026-10-24T12:00:00+07:00",
            "scope": {"kind": "services_readonly", "connection_id": 4, "account_id": "446312402", "revision": 15},
            "prepared_owner_page": "/avito/4/services-feed/",
            "source": {"scope": "services", "state": "not_applicable",
                       "reason": "services_have_no_physical_stock", "private_url": "PRIVATE"},
            "connections": [{"connection_id": 4, "error": "provider_http_403"}]}
        result = receipt.normalize(scoped)
        self.assertNotIn("PRIVATE", str(result))
        for change in (
            {"connections": [{"connection_id": 1, "error": "provider_http_403"}]},
            {"connections": scoped["connections"] * 2},
            {"scope": scoped["scope"] | {"account_id": "1001"}},
        ):
            with self.subTest(change=change), self.assertRaises(ValueError):
                receipt.normalize(scoped | change)


class RawItemContentBoundaryTests(SimpleTestCase):
    def test_raw_provider_presence_is_projected_before_normalizer_and_values_never_escape(self):
        from types import SimpleNamespace
        feed_value = SimpleNamespace(published=[{"rows": [{"AvitoId": "101"}]}])
        raw = {"status": "active", "autoload_item_id": "1_2_3", "title": "PRIVATE TITLE",
               "Description": "PRIVATE DESCRIPTION", "photos": ["https://private.invalid/PRIVATE"],
               "images": {"PRIVATE": "PRIVATE"}, "client_secret": "PRIVATE"}
        with patch.object(audit, "_item_detail_slot"), \
             patch.object(audit.avito_workspace, "_get", return_value=raw) as get:
            item_id, detail, value = audit.raw_content_probe("synthetic-token", "446312402", feed_value)
        get.assert_called_once_with("synthetic-token", "/core/v1/accounts/446312402/items/101/")
        projected = receipt.raw_content_fields(value)
        self.assertTrue(projected["fields"]["title"]["lowercase"]["present"])
        self.assertEqual("string", projected["fields"]["description"]["capitalized"]["type"])
        self.assertEqual(1, projected["fields"]["photos"]["lowercase"]["count"])
        self.assertEqual(1, projected["fields"]["images"]["lowercase"]["count"])
        self.assertFalse(projected["live_content_verified"])
        self.assertNotIn("PRIVATE", json.dumps(projected))
        self.assertNotIn("https://", json.dumps(projected))
        self.assertEqual({"status": "active", "autoload_item_id": "1_2_3"}, detail)

    def test_foreign_item_or_unbounded_structure_cannot_confirm_field_boundary(self):
        from types import SimpleNamespace
        feed_value = SimpleNamespace(published=[{"rows": [{"AvitoId": "101"}]}])
        for raw in ({"status": "another_user"}, {"status": "not_found"}, {"status": "unknown"},
                    {"status": "active", "photos": ["PRIVATE"] * 1001}):
            with self.subTest(status=raw["status"]), patch.object(audit, "_item_detail_slot"), \
                 patch.object(audit.avito_workspace, "_get", return_value=raw) as get:
                with self.assertRaises(AvitoError):
                    audit.raw_content_probe("synthetic-token", "446312402", feed_value)
                self.assertEqual(1, get.call_count)
        value = release_raw_fields()
        value["fields"]["title"]["lowercase"] = {"present": True, "type": "missing", "count": None}
        with self.assertRaises(ValueError):
            receipt.raw_content_fields(value)
