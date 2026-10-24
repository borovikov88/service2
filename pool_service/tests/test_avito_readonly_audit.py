import json
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
