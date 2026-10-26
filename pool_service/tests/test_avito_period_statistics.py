"""Synthetic period/cache contracts; no provider or company financial fixtures."""
from copy import deepcopy
from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch
from zoneinfo import ZoneInfo

from django.core.exceptions import PermissionDenied
from django.db import connection as database
from django.test import Client, SimpleTestCase, TestCase
from django.urls import reverse
from django.utils import timezone

from pool_service import avito_period_statistics as periods
from pool_service.communication_avito import AvitoError
from pool_service.communication_models import AvitoCredential, ChannelConnection, CommunicationChannel
from pool_service.models import Organization, OrganizationAccess
from pool_service.tests.test_avito_management import AvitoManagementTests


def page(start, end, *, ids=(91, 92), total=None, offset=0, missing=()):
    return {"date_from": start, "date_to": end, "grouping": "item", "offset": offset,
        "limit": 50, "total": len(ids) if total is None else total,
        "rows": [{"id": identifier, "label": "PRIVATE provider label",
            "metrics": [{"slug": slug, "value": None if slug in missing else
                ("12.00" if slug in periods.MONEY else "17" if slug == "impressions"
                 else "7" if slug == "views" else "2"),
                "unit": "₽" if slug in periods.MONEY else ""}
                for slug in periods.FIELDS], "private": "PRIVATE"}
            for identifier in ids], "private": "PRIVATE"}


class PeriodCacheTests(SimpleTestCase):
    def setUp(self):
        self.query = periods._query("2026-10-01", "2026-10-07", "item")
        self.binding = {"account_id": periods.ACCOUNT, "organization_id": 1,
                        "channel_id": 1, "credential_stamp": "opaque"}
        self.book = periods._book({}, self.binding)
        self.at = timezone.now().isoformat()

    def test_optional_total_is_incomplete_and_old_report_is_unchanged(self):
        data = page("2026-10-01", "2026-10-07")
        self.assertTrue(periods._merge(self.book, data, self.query, self.at))
        previous = deepcopy(self.book["reports"])
        partial = {**data, "total": None}
        self.assertFalse(periods._merge(self.book, partial, self.query, self.at))
        self.assertEqual(self.book["reports"], previous)
        self.assertEqual(self.book["latest_attempt"]["status"], "incomplete")
        self.assertEqual(self.book["latest_attempt"]["code"], "period_incomplete")

    def test_pages_require_explicit_total_contiguous_offsets_and_unique_ids(self):
        first = page("2026-10-01", "2026-10-07", ids=tuple(range(1, 51)), total=51)
        second = page("2026-10-01", "2026-10-07", ids=(51,), total=51, offset=50)
        self.assertFalse(periods._merge(self.book, second, self.query, self.at))
        self.assertEqual(self.book["reports"], [])
        self.assertFalse(periods._merge(self.book, first, self.query, self.at))
        self.assertTrue(periods._merge(self.book, second, self.query, self.at))
        self.assertEqual(self.book["reports"][0]["page_offsets"], [0, 50])
        self.assertEqual(len(self.book["reports"][0]["rows"]), 51)
        other = periods._book({}, self.binding)
        periods._merge(other, first, self.query, self.at)
        with self.assertRaises(AvitoError):
            periods._merge(other, {**second, "rows": first["rows"][:1]}, self.query, self.at)

    def test_empty_is_explicit_and_never_becomes_zero_ad_metrics(self):
        data = page("2026-10-01", "2026-10-07", ids=(), total=0)
        self.assertTrue(periods._merge(self.book, data, self.query, self.at))
        self.assertTrue(all(value is None for value in periods._aggregate(self.book["reports"][0]).values()))
        for invalid in (True, "0", -1):
            with self.subTest(total=invalid), self.assertRaises(AvitoError):
                periods._merge(periods._book({}, self.binding), {**data, "total": invalid}, self.query, self.at)

    def test_unknown_metric_and_units_do_not_block_known_metrics_or_divide_by_zero(self):
        data = page("2026-10-01", "2026-10-07", ids=(91,), missing=("commission",))
        for metric in data["rows"][0]["metrics"]:
            if metric["slug"] == "contacts":
                metric["value"] = "0"
        periods._merge(self.book, data, self.query, self.at)
        values = periods._aggregate(self.book["reports"][0])
        self.assertEqual(values["views"], Decimal("7"))
        self.assertIsNone(values["commission"])
        self.assertIsNone(values["cpl_cash"])
        self.assertEqual(values["conversion"], Decimal("0.00"))
        self.assertNotIn("PRIVATE", str(self.book))
        report = deepcopy(self.book["reports"][0])
        report["rows"][0]["metrics"]["views"] = "PRIVATE"
        self.assertIsNone(periods._report(report)["rows"][0]["metrics"]["views"])

    def test_binding_tamper_and_unknown_attempt_fields_cannot_export_payload(self):
        periods._merge(self.book, page("2026-10-01", "2026-10-07"), self.query, self.at)
        saved = {periods.KEY: self.book}
        changed = {**self.binding, "credential_stamp": "different"}
        self.assertFalse(periods._book(saved, changed)["reports"])
        self.book["latest_attempt"]["private"] = "PRIVATE"
        cleaned = periods._book(saved, self.binding)
        self.assertEqual(cleaned["latest_attempt"], {})
        self.book["owner_attempts"] = {periods._key(self.query): {
            "date_from": "2026-09-24", "date_to": "2026-09-30", "grouping": "item",
            "offset": 0, "checked_at": self.at, "status": "complete", "code": ""}}
        self.assertEqual(periods._book(saved, self.binding)["owner_attempts"], {})
        bad = deepcopy(self.book["reports"][0])
        bad["page_offsets"] = [False]
        with self.assertRaises(AvitoError):
            periods._report(bad)


class PeriodRefreshTests(TestCase):
    def setUp(self):
        AvitoManagementTests.setUp(self)
        self.goods = self.connection
        self.service = ChannelConnection.objects.create(pk=4, channel=self.channel,
            external_id=periods.ACCOUNT, name="Synthetic services")
        AvitoCredential.objects.create(connection=self.service,
            client_id_encrypted="encrypted-period-client", client_secret_encrypted="encrypted-period-secret")
        today = timezone.localdate(timezone=ZoneInfo("Asia/Novosibirsk"))
        self.end = (today-timedelta(days=1)).isoformat()
        self.start = (today-timedelta(days=7)).isoformat()
        self.previous_end = (today-timedelta(days=8)).isoformat()
        self.previous_start = (today-timedelta(days=14)).isoformat()
        self.token = patch("pool_service.avito_period_statistics.access_token", return_value="PRIVATE_TOKEN").start()
        self.identity = patch("pool_service.avito_workspace._get", return_value={"id": periods.ACCOUNT}).start()
        self.fetch = patch("pool_service.avito_workspace.fetch_statistics").start()
        self.addCleanup(patch.stopall)
        self.fetch.return_value = page(self.start, self.end)

    def refresh(self, **kwargs):
        return periods.refresh(self.service, self.owner, start=self.start, end=self.end, **kwargs)

    def saved(self):
        self.service.refresh_from_db()
        return self.service.settings["avito_workspace"]

    def test_shared_reader_saves_separate_periods_and_preserves_old_on_failure(self):
        self.assertEqual(self.refresh()["status"], "ok")
        first = deepcopy(self.saved()["sections"]["statistics"])
        self.fetch.side_effect = AvitoError("statistics_fields_unavailable")
        result = periods.refresh(self.service, self.owner, start=self.previous_start, end=self.previous_end)
        saved = self.saved()
        self.assertEqual(result["status"], "warning")
        self.assertEqual(saved["sections"]["statistics"]["data"], first["data"])
        self.assertEqual(saved["sections"]["statistics"]["success_at"], first["success_at"])
        view = periods.display(self.service, self.owner)
        self.assertEqual(view["reports"][0]["date_from"], self.start)
        self.assertEqual(view["latest_attempt"]["date_from"], self.previous_start)
        self.assertEqual(view["latest_attempt"]["status"], "incomplete")
        self.assertNotIn("PRIVATE", str(saved))

    def test_two_periods_compare_exact_id_sets_not_just_counts(self):
        self.refresh()
        self.fetch.return_value = page(self.previous_start, self.previous_end)
        periods.refresh(self.service, self.owner, start=self.previous_start, end=self.previous_end)
        view = periods.display(self.service, self.owner)
        self.assertEqual(len(view["reports"]), 2)
        self.assertTrue(view["comparisons"][0]["same_ids"])
        self.assertTrue(all(row["delta"] == "0" or row["delta"] == "0.00"
                            for row in view["comparisons"][0]["metrics"] if row["delta"] is not None))
        self.fetch.return_value = page(self.previous_start, self.previous_end, ids=(91, 93))
        periods.refresh(self.service, self.owner, start=self.previous_start, end=self.previous_end)
        comparison = periods.display(self.service, self.owner)["comparisons"][0]
        self.assertFalse(comparison["same_ids"])
        self.assertTrue(all(row["delta"] is None for row in comparison["metrics"]))

    def test_api_runs_outside_new_transactions_and_stale_lease_cannot_publish(self):
        baseline = len(database.atomic_blocks)
        def external(*args, **kwargs):
            self.assertEqual(len(database.atomic_blocks), baseline)
            current = ChannelConnection.objects.get(pk=4)
            current.settings["avito_workspace_refresh_lease"] = "newer-request"
            current.save(update_fields=["settings"])
            return page(self.start, self.end)
        self.fetch.side_effect = external
        result = self.refresh()
        self.assertEqual(result["code"], "period_scope_changed")
        self.service.refresh_from_db()
        self.assertNotIn("avito_workspace", self.service.settings)

    def test_revoked_rights_and_changed_credentials_never_save_external_result(self):
        def revoked(*args, **kwargs):
            OrganizationAccess.objects.filter(user=self.owner, organization=self.org).update(role="manager")
            return page(self.start, self.end)
        self.fetch.side_effect = revoked
        with self.assertRaises(PermissionDenied):
            self.refresh()
        self.service.refresh_from_db()
        self.assertNotIn("avito_workspace", self.service.settings)
        OrganizationAccess.objects.filter(user=self.owner, organization=self.org).update(role="owner")
        self.service.settings = {}
        self.service.save(update_fields=["settings"])
        def changed(*args, **kwargs):
            AvitoCredential.objects.filter(connection=self.service).update(client_secret_encrypted="changed-secret")
            return page(self.start, self.end)
        self.fetch.side_effect = changed
        self.assertEqual(self.refresh()["code"], "period_scope_changed")
        self.service.refresh_from_db()
        self.assertNotIn("avito_workspace", self.service.settings)

    def test_foreign_account_or_actor_and_get_no_provider_calls(self):
        for actor, conn in ((self.manager, self.service), (self.owner, self.goods)):
            with self.subTest(actor=actor.pk, conn=conn.pk), self.assertRaises((PermissionDenied, AvitoError)):
                periods.refresh(conn, actor, start=self.start, end=self.end)
        self.fetch.assert_not_called()
        self.client.force_login(self.owner)
        self.client.get(self.url + "?account=4")
        self.fetch.assert_not_called()
        self.assertEqual(periods.display(self.goods, self.owner)["state"], "not_applicable")

    def test_existing_csrf_post_path_calls_shared_reader_and_metadata_is_private(self):
        self.client.force_login(self.owner)
        response = self.client.post(reverse("avito_refresh_data", args=[4]), {
            "section": "statistics", "stats_date_from": self.start, "stats_date_to": self.end, "grouping": "item"})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.fetch.call_count, 1)
        self.assertEqual(len(self.saved()[periods.KEY]["reports"]), 1)
        from pool_service.avito_statistics_models import AvitoStatisticsRun, AvitoDailyStatistic
        self.assertEqual(AvitoStatisticsRun.objects.count(), 0)
        self.assertEqual(AvitoDailyStatistic.objects.count(), 0)
        with patch("pool_service.avito_services_workplace_setup._actor", return_value=self.owner):
            receipt = periods.setup_evidence()
        self.assertEqual(receipt["reports"][0]["ids"], ["91", "92"])
        self.assertIn("spending", receipt["reports"][0]["known_metrics"])
        self.assertEqual(set(receipt["reports"][0]), {
            "date_from", "date_to", "grouping", "received_at", "pages_complete",
            "group_count", "ids", "known_metrics"})
        self.assertNotIn("PRIVATE", str(receipt))
        self.assertNotIn("metric_values", str(receipt))

    def test_owner_operator_one_page_per_invocation_and_cooldown_resumes(self):
        approved = ((self.start, self.end), (self.previous_start, self.previous_end))
        with patch.object(periods, "OWNER_PERIODS", approved), patch(
                "pool_service.avito_services_workplace_setup._actor", return_value=self.owner):
            self.fetch.side_effect = AvitoError("provider_cooldown")
            self.assertEqual(periods.prepare_owner_periods_once()["state"], "pending")
            self.assertEqual(self.fetch.call_count, 1)
            self.fetch.side_effect = lambda token, account, **kw: page(kw["start"], kw["end"])
            self.assertEqual(periods.prepare_owner_periods_once()["state"], "read_attempted")
            self.assertEqual(self.fetch.call_count, 2)
            self.assertEqual(periods.prepare_owner_periods_once()["state"], "read_attempted")
            self.assertEqual(self.fetch.call_count, 3)
            self.assertEqual(periods.prepare_owner_periods_once()["state"], "already_checked")
            self.assertEqual(self.fetch.call_count, 3)


    def test_organization_change_during_read_cannot_transfer_report(self):
        foreign = Organization.objects.create(name="Synthetic other organization")
        foreign_channel = CommunicationChannel.objects.create(organization=foreign, kind="avito", name="Synthetic foreign")
        def move(*args, **kwargs):
            ChannelConnection.objects.filter(pk=4).update(channel=foreign_channel)
            return page(self.start, self.end)
        self.fetch.side_effect = move
        with self.assertRaises((AvitoError, PermissionDenied)):
            self.refresh()
        self.service.refresh_from_db()
        self.assertNotIn("avito_workspace", self.service.settings)
        self.client.force_login(self.owner)
        self.assertEqual(self.client.get(self.url + "?account=4").status_code, 404)

    def test_invalid_canonical_offset_is_safe_redirect_not_provider_request(self):
        self.client.force_login(self.owner)
        response = self.client.post(reverse("avito_refresh_data", args=[4]), {
            "section": "statistics", "stats_date_from": self.start, "stats_date_to": self.end,
            "grouping": "item", "offset": "1"})
        self.assertEqual(response.status_code, 302)
        self.fetch.assert_not_called()


    def test_new_scoped_post_preserves_csrf_boundary(self):
        csrf_client = Client(enforce_csrf_checks=True)
        csrf_client.force_login(self.owner)
        response = csrf_client.post(reverse("avito_refresh_data", args=[4]), {
            "section": "statistics", "stats_date_from": self.start, "stats_date_to": self.end, "grouping": "item"})
        self.assertEqual(response.status_code, 403)
        self.fetch.assert_not_called()


    def test_owner_ledger_uses_exact_attempt_and_not_an_interleaved_manual_request(self):
        original_refresh = periods.refresh
        calls = []
        def interleaved(*args, **kwargs):
            self.fetch.return_value = page(self.start, self.end, ids=tuple(range(1, 51)), total=51)
            result = original_refresh(*args, **kwargs)
            calls.append(result["attempt"])
            self.fetch.return_value = page(self.previous_start, self.previous_end)
            original_refresh(self.service, self.owner, start=self.previous_start, end=self.previous_end)
            return result
        with patch.object(periods, "OWNER_PERIODS", ((self.start, self.end),)), patch(
                "pool_service.avito_services_workplace_setup._actor", return_value=self.owner), patch.object(
                periods, "refresh", side_effect=interleaved):
            periods.prepare_owner_periods_once()
        book = self.saved()[periods.KEY]
        key = periods._key(periods._query(self.start, self.end, "item"))
        self.assertEqual(calls[0]["date_from"], self.start)
        self.assertEqual(book["latest_attempt"]["date_from"], self.previous_start)
        self.assertNotIn(key, book["owner_attempts"])
        self.assertEqual(book["reports"][0]["date_from"], self.previous_start)
