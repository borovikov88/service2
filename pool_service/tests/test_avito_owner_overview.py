"""Synthetic operational summaries and read-only rendered production-receipt boundary."""
from datetime import timedelta
from unittest.mock import patch
from django.contrib.auth.models import User
from django.db import transaction
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from pool_service.models import Organization, OrganizationAccess
from pool_service.communication_models import CommunicationChannel, ChannelConnection
from pool_service.avito_owner_overview import build
from pool_service.avito_owner_dashboard_probe import _reads_only, runtime_evidence


class OwnerOverviewTests(TestCase):
    def setUp(self):
        self.now = timezone.now()
        self.org = Organization.objects.create(name="Synthetic owner overview", paid_until=self.now+timedelta(days=30))
        self.owner = User.objects.create_user("synthetic-overview-owner")
        OrganizationAccess.objects.create(user=self.owner, organization=self.org, role="owner")
        self.channel = CommunicationChannel.objects.create(organization=self.org, kind="avito", name="Synthetic")
        self.goods = ChannelConnection.objects.create(pk=1, channel=self.channel, external_id="12345", name="Synthetic goods")
        self.services = ChannelConnection.objects.create(pk=4, channel=self.channel, external_id="446312402", name="Synthetic services")

    def test_unknown_is_not_zero_and_partial_listing_page_is_not_full_account(self):
        operations, actions = build(self.goods, self.owner, {"items": {"data": {"rows": [{"status": "active"}]}}})
        self.assertIsNone(operations["active_count"])
        self.assertEqual(actions, [])

    def test_complete_audit_zero_and_preorder_are_preserved(self):
        workspace = {"audit": {"checked_at": self.now.isoformat(), "data": {"complete": True, "total": 0,
            "counts": [{"status": "active", "count": 0}],
            "active_source_audit": {"complete": True, "preorder_rows": 7, "in_stock_nonpositive_rows": 0},
            "duplicate_candidate_count": 0}}}
        operations, actions = build(self.goods, self.owner, workspace, now=self.now)
        self.assertEqual(operations["active_count"], 0)
        self.assertEqual(actions, [])

    def test_only_real_stock_mismatch_and_suspected_duplicates_create_tasks(self):
        workspace = {"audit": {"checked_at": self.now.isoformat(), "data": {"complete": True,
            "active_source_audit": {"complete": True, "in_stock_nonpositive_rows": 2, "preorder_rows": 7},
            "duplicate_candidate_count": 1}}}
        _, actions = build(self.goods, self.owner, workspace, now=self.now)
        self.assertEqual(len(actions), 2)
        self.assertIn("под заказ", actions[0]["detail"])
        self.assertIn("Возможные", actions[1]["label"])

    def test_malformed_report_messages_do_not_crash_or_become_incident(self):
        workspace = {"autoload_report": {"data": {"complete": True, "rows": [{"messages": None}]}}}
        self.assertEqual(build(self.goods, self.owner, workspace)[1], [])

    def test_foreign_actor_cannot_read_operational_counts(self):
        other_org = Organization.objects.create(name="Synthetic other")
        other = User.objects.create_user("synthetic-other-overview")
        OrganizationAccess.objects.create(user=other, organization=other_org, role="owner")
        operations, actions = build(self.goods, other, {"advance": {"data": {"kopeks": 0}}})
        self.assertIsNone(operations["active_count"])
        self.assertEqual(actions, [])

    def test_full_get_reads_saved_data_only_and_keeps_goods_settings(self):
        self.client.force_login(self.owner)
        before = self.goods.settings
        with patch("pool_service.avito_management.access_token") as token, patch("pool_service.avito_workspace._get") as get:
            response = self.client.get(reverse("avito_dashboard"), {"account": 1, "owner_period": "7"})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "data-avito-owner")
        self.assertEqual(response.context["owner_dashboard"]["period"]["key"], "7")
        self.assertNotContains(response, "Редактор услуг")
        token.assert_not_called()
        get.assert_not_called()
        self.goods.refresh_from_db()
        self.assertEqual(self.goods.settings, before)

    def test_refresh_keeps_selected_owner_period_even_on_failure(self):
        from urllib.parse import parse_qs, urlsplit
        self.client.force_login(self.owner)
        with patch("pool_service.avito_management.access_token") as token:
            response = self.client.post(reverse("avito_refresh_data", args=[1]), {
                "section": "invalid-section", "owner_period": "custom",
                "owner_from": "2030-01-01", "owner_to": "2030-01-07", "owner_ad": "1234567890"})
        self.assertEqual(response.status_code, 302)
        params = parse_qs(urlsplit(response["Location"]).query)
        self.assertEqual(params["owner_period"], ["custom"])
        self.assertEqual(params["owner_from"], ["2030-01-01"])
        self.assertEqual(params["owner_to"], ["2030-01-07"])
        self.assertEqual(params["owner_ad"], ["1234567890"])
        token.assert_not_called()

    def test_unknown_crm_source_is_a_review_task_not_a_confirmed_order(self):
        with patch("pool_service.avito_crm_coverage.coverage", return_value={"ad_source_unknown_dialogs": 2}):
            _, actions = build(self.services, self.owner, {})
        self.assertEqual(len(actions), 1)
        self.assertIn("источник", actions[0]["label"])
        self.assertIn("не подтверждает заказ", actions[0]["detail"])
        self.assertEqual(actions[0]["url"], reverse("avito_crm_attribution", args=[4]))

    def test_read_only_probe_refuses_writes_before_execution(self):
        with self.assertRaises(RuntimeError):
            with transaction.atomic():
                with _reads_only():
                    ChannelConnection.objects.filter(pk=1).update(name="Do not change")
        self.goods.refresh_from_db()
        self.assertEqual(self.goods.name, "Synthetic goods")

    @override_settings(ALLOWED_HOSTS=["service2.aqualine22.ru", "testserver"])
    def test_private_runtime_receipt_renders_both_scoped_screens_without_content(self):
        with patch("pool_service.avito_services_workplace_setup._actor", return_value=self.owner), \
                patch("pool_service.avito_workspace._get") as provider:
            result = runtime_evidence()
        self.assertEqual(result["state"], "rendered_saved_data")
        self.assertEqual({row["connection_id"] for row in result["screens"]}, {1, 4})
        self.assertFalse(result["database_write_performed"])
        self.assertNotIn("Synthetic goods", str(result))
        self.assertNotIn("Денежные расходы", str(result))
        provider.assert_not_called()
