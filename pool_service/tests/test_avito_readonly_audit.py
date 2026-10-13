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

    def collect_connection(self, detail=None, upload=None, account=None):
        with patch.object(audit, "authorized_account_id", side_effect=account or ["123", "123"]), \
             patch.object(audit, "access_token", return_value="synthetic-token"), \
             patch.object(audit, "_provider_root", return_value="https://api.avito.ru"), \
             patch.object(audit, "fetch_detail", side_effect=detail or [
                 {"status": "active", "autoload_item_id": "1_2_3"},
                 {"status": "old", "autoload_item_id": "1_9_3"}]), \
             patch.object(audit.avito_autoload_report, "fetch_report", side_effect=upload or [report()]):
            return audit.collect_connection(self.connection, ["101", "102"], self.index)

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

    def test_collect_fails_closed_on_more_than_two_connections_without_network(self):
        for index in range(2):
            connection = ChannelConnection.objects.create(channel=self.connection.channel, external_id=str(200 + index), name="Extra")
            AvitoCredential.objects.create(connection=connection, client_id_encrypted="synthetic", client_secret_encrypted="synthetic")
        with patch.object(audit.avito_source_audit, "_read_xml") as source, patch.object(audit, "authorized_account_id") as account:
            result = audit.collect(["101"])
        self.assertEqual(result["reason"], "audit_connection_limit")
        source.assert_not_called()
        account.assert_not_called()
