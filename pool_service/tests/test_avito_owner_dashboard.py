"""Synthetic owner-summary cases; never requests Avito or changes provider data."""
from datetime import datetime, timedelta, timezone as dt_timezone
from unittest.mock import patch

from django.contrib.auth.models import User
from django.test import SimpleTestCase, TestCase

from pool_service import avito_owner_dashboard as owner, avito_daily_statistics as daily
from pool_service.avito_statistics_models import AvitoDailyStatistic, AvitoStatisticsRun
from pool_service.communication_models import ChannelConnection, CommunicationChannel, AvitoCredential
from pool_service.models import Organization, OrganizationAccess


class OwnerMathTests(SimpleTestCase):
    def test_ratio_of_sums_not_average_daily_cost(self):
        result = owner._values({"views": "7", "contacts": "2", "spending": "17.50"})
        self.assertEqual(result["cost_view"], owner.Decimal("2.50"))
        self.assertEqual(result["cost_contact"], owner.Decimal("8.75"))
        self.assertIsNone(owner._values({"views": "0", "contacts": "0", "spending": "17.50"})["cost_contact"])
        self.assertEqual(owner._values({"views": "7", "contacts": "0", "spending": "0"})["cost_view"], 0)

    def test_unknown_bad_types_and_ambiguous_money_never_become_zero(self):
        for value in (None, True, "NaN", "Infinity", "-1", {}, []):
            self.assertIsNone(owner._number(value))
        self.assertIsNone(owner._number("1.5", counter=True))
        self.assertIsNone(owner._values({"views": "7", "contacts": None, "spending": None})["cost_view"])

    def test_window_keeps_existing_lag_and_rejects_unbounded_or_current_dates(self):
        now = datetime(2030, 6, 15, 5, tzinfo=dt_timezone.utc)
        result = owner._window("7", "", "", now)
        self.assertEqual((result["from"], result["to"]), ("2030-06-07", "2030-06-13"))
        result = owner._window("custom", "2030-01-01", "2030-06-13", now)
        self.assertTrue(result["error"])
        self.assertEqual(result["key"], "30")
        self.assertEqual(owner._window("custom", "2030-06-01", "2030-06-13", now)["key"], "custom")


class OwnerSummaryTests(TestCase):
    def setUp(self):
        self.now = datetime(2030, 6, 15, 5, tzinfo=dt_timezone.utc)
        self.day = daily.collection_cutoff(self.now)
        self.org = Organization.objects.create(name="Synthetic dashboard organization")
        self.other_org = Organization.objects.create(name="Other dashboard organization")
        self.actor = User.objects.create_user("dashboard-owner")
        self.other = User.objects.create_user("dashboard-other")
        OrganizationAccess.objects.create(organization=self.org, user=self.actor, role="owner")
        OrganizationAccess.objects.create(organization=self.other_org, user=self.other, role="owner")
        channel = CommunicationChannel.objects.create(organization=self.org, kind="avito", name="Synthetic")
        self.connection = ChannelConnection.objects.create(pk=4, channel=channel,
            external_id=daily.ACCOUNT, name="Synthetic services")
        self.goods = ChannelConnection.objects.create(pk=1, channel=channel,
            external_id="123456", name="Synthetic goods", settings={"sentinel": "unchanged"})
        for connection in (self.connection, self.goods):
            AvitoCredential.objects.create(connection=connection,
                client_id_encrypted="synthetic-id", client_secret_encrypted="synthetic-secret")
        self.provider = patch("pool_service.avito_workspace._post", side_effect=AssertionError("GET must not call provider"))
        self.provider.start()
        self.addCleanup(self.provider.stop)

    def fact(self, *, day=None, connection=None, identifier="91", views="7", contacts="2",
             spending="17.50", present=True, binding=None, dimensions=None, state="success", complete=True):
        connection = connection or self.connection
        day = day or self.day
        run = AvitoStatisticsRun.objects.create(connection=connection, account_id=str(connection.external_id),
            organization_id_at_collection=connection.channel.organization_id, day=day,
            binding_digest=binding or daily._stamp(connection), state=state,
            pagination_complete=complete, expected_total=1, received_rows=1,
            finished_at=self.now, dimensions=dimensions if dimensions is not None else {identifier: {}})
        AvitoDailyStatistic.objects.update_or_create(connection=connection, account_id=str(connection.external_id),
            organization_id_at_collection=connection.channel.organization_id, day=day, avito_id=identifier,
            defaults={"source_run": run, "metrics": {"views": views, "contacts": contacts, "spending": spending},
                "provider_row_present": present, "title": "Synthetic ad", "fetched_at": self.now})
        return run

    def read(self, **kwargs):
        return owner.display(self.connection, self.actor, period="custom",
            date_from=self.day.isoformat(), date_to=self.day.isoformat(), now=self.now, **kwargs)

    def test_known_metrics_units_and_zero_contacts(self):
        self.fact(contacts="0")
        result = self.read()
        cards = {item["key"]: item for item in result["kpis"]}
        self.assertEqual(cards["spending"]["value"], "17.50")
        self.assertEqual(cards["cost_view"]["value"], "2.50")
        self.assertIsNone(cards["cost_contact"]["value"])
        self.assertEqual(result["chart"]["contacts"], [0.0])
        self.assertEqual(result["source"]["provider_timezone"], "unknown")

    def test_missing_metric_blocks_only_that_metric_and_ratios(self):
        self.fact(contacts=None)
        result = self.read()
        cards = {item["key"]: item["value"] for item in result["kpis"]}
        self.assertEqual(cards["views"], "7")
        self.assertEqual(cards["cost_view"], "2.50")
        self.assertIsNone(cards["contacts"])
        self.assertIsNone(cards["cost_contact"])

    def test_missing_expected_ad_row_and_provider_absence_are_unknown(self):
        self.fact(dimensions={"91": {}, "92": {}})
        result = self.read()
        self.assertTrue(result["source"]["pages_complete"])
        self.assertEqual(result["source"]["reported_ads"], 1)
        self.assertEqual(result["source"]["expected_ads"], 2)
        self.assertTrue(all(card["value"] is None for card in result["kpis"]))
        self.assertEqual(result["chart"]["views"], [None])
        AvitoDailyStatistic.objects.update(provider_row_present=False)
        self.assertEqual(self.read()["source"]["reported_ads"], 0)

    def test_newest_successful_run_and_binding_isolation(self):
        old = self.fact(views="999")
        self.fact(views="7")
        self.fact(views="999", binding="other-generation", identifier="92")
        result = self.read()
        self.assertEqual(result["chart"]["views"], [7.0])
        self.assertNotIn("999", str(result["kpis"]))
        self.assertEqual(AvitoStatisticsRun.objects.get(pk=old.pk).state, "success")

    def test_account_organization_and_access_are_isolated(self):
        self.fact()
        self.fact(connection=self.goods, views="999")
        other_channel = CommunicationChannel.objects.create(organization=self.other_org, kind="avito", name="Other")
        foreign = ChannelConnection.objects.create(channel=other_channel, external_id=daily.ACCOUNT)
        self.fact(connection=foreign, views="888")
        self.assertEqual(self.read()["chart"]["views"], [7.0])
        self.assertIsNone(owner.display(self.connection, self.other, now=self.now))
        self.goods.refresh_from_db()
        self.assertEqual(self.goods.settings, {"sentinel": "unchanged"})

    def test_missing_dates_have_null_chart_points_and_no_period_total(self):
        self.fact()
        result = owner.display(self.connection, self.actor, period="7", now=self.now)
        self.assertEqual(len(result["chart"]["labels"]), 7)
        self.assertEqual(result["chart"]["views"], [None]*6 + [7.0])
        self.assertFalse(result["source"]["pages_complete"])
        self.assertIsNone(result["kpis"][0]["value"])

    def test_stale_credential_generation_is_not_read(self):
        self.fact()
        credential = self.connection.avito_credential
        credential.client_secret_encrypted = "different-synthetic-secret"
        credential.save()
        self.assertEqual(self.read()["chart"]["views"], [None])

    def test_disabled_actor_is_rechecked(self):
        self.fact()
        User.objects.filter(pk=self.actor.pk).update(is_active=False)
        self.assertIsNone(self.read())

    def test_saved_period_does_not_repair_daily_points_or_relabel_requested_dates(self):
        self.fact()
        saved = {"from": (self.day-timedelta(days=6)).isoformat(), "to": self.day.isoformat(),
            "values": owner._values({"views": "21", "contacts": "3", "spending": "18"}),
            "known": {"views": 1, "contacts": 1, "spending": 1}, "expected": 1,
            "received_at": self.now-timedelta(days=3), "pages_complete": True,
            "source": "synthetic_saved_period"}
        with patch.object(owner, "_fallback", return_value=saved):
            result = owner.display(self.connection, self.actor, period="30", now=self.now)
        self.assertEqual(result["source"]["total_dates"], 30)
        self.assertEqual(result["fallback"]["received_at"], self.now-timedelta(days=3))
        self.assertIsNone(result["kpis"][0]["value"])
        self.assertEqual(sum(value is not None for value in result["chart"]["views"]), 1)

    def cache(self, *, total=1, offset=0, money_unit="₽", account=None):
        state = {"account_id": str(self.goods.external_id) if account is None else account,
            "sections": {"statistics": {"checked_at": self.now.isoformat(),
                "success_at": (self.now-timedelta(days=2)).isoformat(), "data": {
                    "date_from": self.day.isoformat(), "date_to": self.day.isoformat(),
                    "grouping": "item", "total": total, "offset": offset,
                    "rows": [{"id": 91, "metrics": [
                        {"slug": "views", "value": "7", "unit": ""},
                        {"slug": "contacts", "value": "2", "unit": ""},
                        {"slug": "spending", "value": "17.50", "unit": money_unit}]}]}}}}
        self.goods.settings = {"sentinel": "unchanged", "avito_workspace": state}
        self.goods.save()
        return owner._manual_snapshot(self.goods)

    def test_goods_complete_saved_snapshot_preserves_rub_and_success_time(self):
        result = self.cache()
        self.assertEqual(result["values"]["cost_view"], owner.Decimal("2.50"))
        self.assertEqual(result["received_at"], self.now-timedelta(days=2))
        result = owner.display(self.goods, self.actor, period="custom",
            date_from=self.day.isoformat(), date_to=self.day.isoformat(), now=self.now)
        self.assertEqual(result["source"]["kind"], "period")
        self.assertEqual(result["kpis"][0]["value"], "7")
        self.assertEqual(result["chart"]["views"], [None])

    def test_partial_pages_wrong_account_and_ambiguous_currency_rejected(self):
        self.assertIsNone(self.cache(total=214))
        self.assertIsNone(self.cache(offset=50))
        self.assertIsNone(self.cache(account="wrong-account"))
        self.assertIsNone(self.cache(money_unit="ед. API")["values"]["spending"])

    def test_concurrent_credential_change_discards_summary(self):
        self.fact()
        def changed(*args, **kwargs):
            AvitoCredential.objects.filter(connection=self.connection).update(
                client_secret_encrypted="rotated-synthetic-secret")
            return None
        with patch.object(owner, "_fallback", side_effect=changed):
            self.assertIsNone(self.read())

    def test_partial_new_attempt_does_not_hide_saved_success(self):
        self.fact()
        AvitoStatisticsRun.objects.create(connection=self.connection, account_id=daily.ACCOUNT,
            organization_id_at_collection=self.org.pk, day=self.day,
            binding_digest=daily._stamp(self.connection), state="partial", pagination_complete=False)
        self.assertEqual(self.read()["chart"]["views"], [7.0])

    def test_exact_weekly_report_precedes_newer_single_date_snapshot(self):
        first = self.day-timedelta(days=6)
        def report(start, end, views):
            values = owner._values({"views": views, "contacts": "2", "spending": "17.50"})
            return {"date_from": start.isoformat(), "date_to": end.isoformat(),
                "metric_values": values, "group_count": 1, "metrics": [
                    {"slug": key, "known_groups": 1} for key in owner.CORE],
                "received_at_display": self.now, "ids": ["91"],
                "ads": [{"avito_id": "91", "metric_values": values}]}
        reports = [report(self.day, self.day, "1"), report(first, self.day, "7")]
        with patch.object(owner.periods, "display", return_value={"reports": reports}):
            result = owner.display(self.connection, self.actor, period="7", now=self.now)
        self.assertEqual(result["source"]["kind"], "period")
        self.assertEqual(result["kpis"][0]["value"], "7")
        self.assertEqual(result["chart"]["views"], [None]*7)
        self.assertIsNone(result["fallback"])
        self.assertEqual(result["ads"][0]["id"], "91")
        self.assertEqual(result["ads"][0]["views"], "7")
        self.assertIn("Период", result["ads"][0]["coverage_label"])
        self.assertEqual({card["key"]: card["known"] for card in result["kpis"]},
            {"views": 1, "contacts": 1, "spending": 1, "cost_view": 1, "cost_contact": 1})
        with patch.object(owner.periods, "display", return_value={"reports": reports}):
            result = owner.display(self.connection, self.actor, period="30", now=self.now)
        self.assertEqual(result["fallback"]["from"], first.isoformat())
        self.assertEqual(result["fallback"]["kpis"][0]["value"], "7")

    def test_future_success_timestamp_is_not_a_completed_observation(self):
        run = self.fact()
        AvitoStatisticsRun.objects.filter(pk=run.pk).update(finished_at=self.now+timedelta(days=1))
        self.assertEqual(self.read()["chart"]["views"], [None])

    def test_malformed_cached_item_titles_cannot_break_valid_period_summary(self):
        self.cache()
        self.goods.settings["avito_workspace"]["sections"]["items"] = ["invalid"]
        self.goods.save()
        result = owner.display(self.goods, self.actor, period="custom",
            date_from=self.day.isoformat(), date_to=self.day.isoformat(), now=self.now)
        self.assertEqual(result["ads"][0]["id"], "91")
        self.assertEqual(result["ads"][0]["title"], "")
        self.assertEqual(result["ads"][0]["views"], "7")
