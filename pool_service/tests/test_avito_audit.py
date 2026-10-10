from unittest.mock import patch

from django.contrib.auth.models import User
from django.test import Client, SimpleTestCase, TestCase
from django.urls import reverse

from pool_service import avito_audit
from pool_service.communication_avito import AvitoError
from pool_service.communication_models import AvitoCredential, ChannelConnection, CommunicationChannel
from pool_service.models import Notification, Organization, OrganizationAccess


def listing(identifier, title="Насос", status="active"):
    return {"id": str(identifier), "title": title, "status": status}


class AvitoAuditReportTests(SimpleTestCase):
    def test_same_title_is_only_candidate_and_archived_listings_are_excluded(self):
        rows = [listing(1, " Насос  А "), listing(2, "насос а"), listing(3, "Насос А", "old"), listing(4, "Насос Б")]
        report = avito_audit.build_report({row["id"]: row for row in rows}, 2)
        self.assertEqual((report["total"], report["pages"]), (4, 2))
        self.assertEqual(report["duplicate_candidate_count"], 2)
        self.assertEqual(report["duplicate_groups"][0]["ids"], ["1", "2"])
        self.assertEqual({row["status"]: row["count"] for row in report["counts"]}["old"], 1)
        self.assertTrue(report["complete"])

    def test_empty_completed_scan_and_blank_titles(self):
        report = avito_audit.build_report({}, 1)
        self.assertEqual((report["total"], report["duplicate_group_count"]), (0, 0))
        self.assertTrue(report["complete"])
        rows = [listing(1, ""), listing(2, " ")]
        self.assertEqual(avito_audit.build_report({row["id"]: row for row in rows}, 1)["duplicate_candidate_count"], 0)

    def test_candidate_samples_are_bounded_without_truncating_totals(self):
        rows = [listing(group * 100 + item, f"Товар {group}") for group in range(30) for item in range(12)]
        report = avito_audit.build_report({row["id"]: row for row in rows}, 8)
        self.assertEqual((report["duplicate_group_count"], report["duplicate_candidate_count"]), (30, 360))
        self.assertEqual(len(report["duplicate_groups"]), 25)
        self.assertEqual(report["duplicate_groups_omitted"], 5)
        self.assertEqual(len(report["duplicate_groups"][0]["ids"]), 10)
        self.assertEqual(report["duplicate_groups"][0]["omitted"], 2)

    def test_fetch_uses_full_scan_with_shorter_web_budget(self):
        with patch.object(avito_audit.avito_status_monitor, "full_scan", return_value=({}, 1)) as scan:
            self.assertTrue(avito_audit.fetch_report("connection")["complete"])
        scan.assert_called_once_with("connection", scan_seconds=60)


class AvitoAuditViewTests(TestCase):
    def setUp(self):
        self.org = Organization.objects.create(name="Audit test")
        self.owner = User.objects.create_user("audit-owner")
        self.manager = User.objects.create_user("audit-manager")
        OrganizationAccess.objects.create(organization=self.org, user=self.owner, role="owner")
        OrganizationAccess.objects.create(organization=self.org, user=self.manager, role="manager")
        self.channel = CommunicationChannel.objects.create(organization=self.org, kind="avito", name="Avito")
        self.connection = ChannelConnection.objects.create(channel=self.channel, external_id="123", name="Goods")
        AvitoCredential.objects.create(connection=self.connection, client_id_encrypted="test", client_secret_encrypted="test")
        self.url = reverse("avito_dashboard")
        self.refresh_url = reverse("avito_refresh_data", args=[self.connection.pk])
        self.client.force_login(self.owner)
        token = patch("pool_service.avito_management.access_token", return_value="test-token")
        token.start()
        self.addCleanup(token.stop)
        profile = patch("pool_service.avito_workspace._get", return_value={"id": 123})
        profile.start()
        self.addCleanup(profile.stop)

    def saved_audit(self):
        self.connection.refresh_from_db()
        return self.connection.settings["avito_workspace"]["sections"]["audit"]

    def test_complete_scan_is_persisted_and_get_never_scans(self):
        rows = {str(i): listing(i) for i in (1, 2)}
        with patch.object(avito_audit.avito_status_monitor, "full_scan", return_value=(rows, 1)) as scan:
            self.assertEqual(self.client.post(self.refresh_url, {"section": "audit"}).status_code, 302)
            scan.assert_called_once_with(self.connection, scan_seconds=60)
            response = self.client.get(self.url)
            self.assertEqual(scan.call_count, 1)
        self.assertContains(response, "Полный обход доступного через API списка")
        self.assertContains(response, "совпадение названия не подтверждает дубль")
        self.assertContains(response, "Не проверены — нужен источник остатков")
        self.assertEqual(self.saved_audit()["data"]["total"], 2)
        self.assertFalse(Notification.objects.exists())
        self.assertNotIn("avito_workspace_refresh_lease", self.connection.settings)

    def test_first_incomplete_scan_does_not_save_zero_or_complete(self):
        with patch.object(avito_audit, "fetch_report", side_effect=AvitoError("monitor_scan_limit")):
            self.client.post(self.refresh_url, {"section": "audit"})
        state = self.saved_audit()
        self.assertEqual(state["code"], "monitor_scan_limit")
        self.assertNotIn("data", state)
        response = self.client.get(self.url)
        self.assertContains(response, "Полный список ещё не проверен")
        self.assertNotContains(response, "Полный обход доступного через API списка:")

    def test_failed_refresh_keeps_previous_success_time_and_inventory(self):
        data = avito_audit.build_report({"1": listing(1)}, 1)
        old = {"status": "ok", "data": data, "success_at": "2026-10-01T10:00:00+00:00"}
        self.connection.settings = {"avito_workspace": {"account_id": "123", "sections": {"audit": old}}}
        self.connection.save(update_fields=["settings"])
        with patch.object(avito_audit, "fetch_report", side_effect=AvitoError("monitor_pagination_invalid")):
            self.client.post(self.refresh_url, {"section": "audit"})
        state = self.saved_audit()
        self.assertEqual(state["data"], data)
        self.assertEqual(state["success_at"], old["success_at"])
        self.assertTrue(state["stale"])
        self.assertContains(self.client.get(self.url), "текущая проверка не завершена")

    def test_provider_error_text_is_not_saved(self):
        with patch.object(avito_audit, "fetch_report", side_effect=AvitoError("secret-payload")):
            self.client.post(self.refresh_url, {"section": "audit"})
        self.assertEqual(self.saved_audit()["code"], "provider_error")
        self.assertNotIn("secret-payload", str(self.connection.settings))

    def test_post_csrf_permissions_and_organization_scope(self):
        with patch.object(avito_audit, "fetch_report") as scan:
            self.assertEqual(self.client.get(self.refresh_url).status_code, 405)
            csrf_client = Client(enforce_csrf_checks=True)
            csrf_client.force_login(self.owner)
            self.assertEqual(csrf_client.post(self.refresh_url, {"section": "audit"}).status_code, 403)
            self.client.force_login(self.manager)
            self.assertEqual(self.client.post(self.refresh_url, {"section": "audit"}).status_code, 403)
            other = Organization.objects.create(name="Other audit")
            other_channel = CommunicationChannel.objects.create(organization=other, kind="avito", name="Other")
            other_connection = ChannelConnection.objects.create(channel=other_channel, external_id="999", name="Other")
            self.client.force_login(self.owner)
            self.assertEqual(self.client.post(reverse("avito_refresh_data", args=[other_connection.pk]), {"section": "audit"}).status_code, 404)
        scan.assert_not_called()

    def test_account_mismatch_and_change_during_scan_cannot_save_report(self):
        with patch("pool_service.avito_workspace._get", return_value={"id": 999}), patch.object(avito_audit, "fetch_report") as scan:
            self.client.post(self.refresh_url, {"section": "audit"})
            scan.assert_not_called()
        self.assertEqual(self.saved_audit()["code"], "provider_account_mismatch")
        def changed(connection):
            ChannelConnection.objects.filter(pk=connection.pk).update(external_id="999")
            return avito_audit.build_report({}, 1)
        with patch.object(avito_audit, "fetch_report", side_effect=changed):
            self.client.post(self.refresh_url, {"section": "audit"})
        self.connection.refresh_from_db()
        self.assertNotIn("data", self.connection.settings["avito_workspace"]["sections"]["audit"])
        self.assertNotIn("avito_workspace_refresh_lease", self.connection.settings)

    def test_general_refresh_does_not_start_full_audit(self):
        with patch.object(avito_audit, "fetch_report") as scan, patch("pool_service.avito_workspace.fetch_section", return_value={}):
            self.client.post(self.refresh_url, {"section": "all"})
        scan.assert_not_called()
