"""Synthetic layout regressions. No remote data, provider requests or configuration writes."""
from copy import deepcopy
from datetime import timedelta
from html.parser import HTMLParser
from urllib.parse import parse_qs, urlsplit
from unittest.mock import patch

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from pool_service.communication_models import ChannelConnection, CommunicationChannel
from pool_service.models import Organization, OrganizationAccess


class AccountTabs(HTMLParser):
    def __init__(self, html):
        super().__init__()
        self.links = {}
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "a" and attrs.get("data-owner-account"):
            self.links[attrs["data-owner-account"]] = attrs


class MarketingOwnerLayoutTests(TestCase):
    def setUp(self):
        self.now = timezone.now()
        self.organization = Organization.objects.create(
            name="Synthetic owner layout", paid_until=self.now + timedelta(days=30))
        self.owner = User.objects.create_user("synthetic-owner-layout")
        OrganizationAccess.objects.create(user=self.owner, organization=self.organization, role="owner")
        channel = CommunicationChannel.objects.create(
            organization=self.organization, kind="avito", name="Synthetic channel")
        self.goods = ChannelConnection.objects.create(pk=1, channel=channel, name="Товары", external_id="700001")
        self.services = ChannelConnection.objects.create(pk=4, channel=channel, name="Услуги", external_id="446312402")
        self.other_org = Organization.objects.create(name="Synthetic unrelated owner")
        other_channel = CommunicationChannel.objects.create(
            organization=self.other_org, kind="avito", name="Synthetic unrelated channel")
        self.foreign = ChannelConnection.objects.create(
            channel=other_channel, name="Synthetic unrelated account", external_id="700002")
        self.client.force_login(self.owner)
        self.url = reverse("avito_dashboard")

    def test_authorized_tabs_replace_account_panel_preserve_period_and_clear_ad_and_search(self):
        today = timezone.localdate()
        first, last = (today - timedelta(days=14)).isoformat(), (today - timedelta(days=8)).isoformat()
        goods_before, services_before = deepcopy(self.goods.settings), deepcopy(self.services.settings)
        with patch("pool_service.avito_management.access_token") as token, patch("pool_service.avito_workspace._get") as provider:
            response = self.client.get(self.url, {
                "account": "1", "owner_period": "custom", "owner_from": first, "owner_to": last,
                "date_from": first, "date_to": last, "owner_ad": "91", "q": "Synthetic query"})
        self.assertEqual(response.status_code, 200)
        html = response.content.decode()
        tabs = AccountTabs(html).links
        self.assertEqual(set(tabs), {"1", "4"})
        self.assertEqual(tabs["1"].get("aria-current"), "page")
        self.assertNotEqual(tabs["4"].get("aria-current"), "page")
        self.assertIn("data-owner-account-tabs", html)
        self.assertNotIn('id="avito-account"', html)
        self.assertNotIn("avito-account-picker", html)
        self.assertNotIn("avito-account-bar", html)
        self.assertLess(html.index("data-owner-account-tabs"), html.index("data-avito-owner"))
        params = parse_qs(urlsplit(tabs["4"]["href"]).query)
        self.assertEqual(params["account"], ["4"])
        self.assertEqual(params["owner_period"], ["custom"])
        self.assertEqual(params["owner_from"], [first])
        self.assertEqual(params["owner_to"], [last])
        self.assertEqual(params["date_from"], [first])
        self.assertEqual(params["date_to"], [last])
        self.assertNotIn("owner_ad", params)
        self.assertNotIn("q", params)
        self.assertIn('name="csrfmiddlewaretoken"', html)
        self.assertIn(reverse("avito_refresh_data", args=[1]), html)
        token.assert_not_called()
        provider.assert_not_called()
        self.goods.refresh_from_db()
        self.services.refresh_from_db()
        self.assertEqual(self.goods.settings, goods_before)
        self.assertEqual(self.services.settings, services_before)

    def test_services_tab_is_absent_when_connection_belongs_to_another_organization(self):
        self.services.channel = self.foreign.channel
        self.services.save(update_fields=["channel"])
        response = self.client.get(self.url, {"account": "1"})
        tabs = AccountTabs(response.content.decode()).links
        self.assertEqual(set(tabs), {"1"})
        self.assertEqual(self.client.get(self.url, {"account": "4"}).status_code, 404)

    def test_other_authorized_connections_remain_reachable(self):
        extra = ChannelConnection.objects.create(
            channel=self.goods.channel, external_id="700003", name="Synthetic additional account")
        response = self.client.get(self.url, {"account": "1"})
        self.assertEqual(set(AccountTabs(response.content.decode()).links), {"1", "4", str(extra.pk)})

    def test_no_history_is_compact_not_five_unknown_cards(self):
        response = self.client.get(self.url, {"account": "1"})
        self.assertContains(response, "data-owner-no-statistics")
        self.assertNotContains(response, "data-owner-kpi=")
        self.assertNotContains(response, 'data-owner-ad-form')
        self.assertNotContains(response, "Нет сопоставимых исходных данных или положительного знаменателя.")

    def summary(self, *, zero=False, fallback=False):
        metrics = [
            {"key": "views", "label": "Просмотры", "value": "0" if zero else None, "unit": ""},
            {"key": "contacts", "label": "Контакты Авито", "value": "0" if zero else None, "unit": ""},
            {"key": "spending", "label": "Денежные расходы", "value": "0" if zero else None, "unit": "₽"},
            {"key": "cost_view", "label": "₽ / просмотр", "value": None, "unit": "₽"},
            {"key": "cost_contact", "label": "₽ / контакт", "value": None, "unit": "₽"}]
        return {
            "available": zero, "selected_ad": "", "ad_choices": [], "ads": [],
            "period": {"key": "30", "from": "2025-01-01", "to": "2025-01-30", "label": "Synthetic selected month"},
            "kpis": metrics,
            "chart": {"labels": ["2025-01-01"], "views": [0 if zero else None], "contacts": [0 if zero else None],
                      "spending": [0 if zero else None], "cost_view": [None], "cost_contact": [None]},
            "source": {"kind": "daily", "coverage_label": "Synthetic coverage", "completed_dates": 0},
            "fallback": {
                "from": "2024-12-20", "to": "2024-12-26", "received_at": self.now-timedelta(days=3),
                "coverage_label": "Synthetic separate period", "kpis": [
                    {"key": "views", "label": "Synthetic fallback views", "value": "13", "unit": ""}]
            } if fallback else None}

    def test_cached_listing_choices_do_not_create_an_empty_analytics_filter(self):
        data = self.summary()
        data["ad_choices"] = [{"id": "91", "title": "Synthetic cached listing"}]
        with patch("pool_service.avito_owner_dashboard.display", return_value=data):
            response = self.client.get(self.url, {"account": "1"})
        self.assertNotContains(response, "data-owner-ad-form")

    def test_partial_daily_history_keeps_per_ad_inspection_available(self):
        data = self.summary()
        data["ad_choices"] = [{"id": "91", "title": "Synthetic observed listing"}]
        data["source"]["completed_dates"] = 7
        with patch("pool_service.avito_owner_dashboard.display", return_value=data):
            response = self.client.get(self.url, {"account": "4"})
        self.assertContains(response, "data-owner-ad-form")

    def test_known_zero_metrics_keep_the_metric_cards(self):
        with patch("pool_service.avito_owner_dashboard.display", return_value=self.summary(zero=True)):
            response = self.client.get(self.url, {"account": "1"})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "data-owner-kpi=", count=5)
        self.assertContains(response, "<strong>0</strong>", html=True)
        self.assertNotContains(response, "data-owner-no-statistics")

    def test_available_period_is_shown_before_charts_without_renaming_selected_period(self):
        with patch("pool_service.avito_owner_dashboard.display", return_value=self.summary(fallback=True)):
            response = self.client.get(self.url, {"account": "4"})
        html = response.content.decode()
        self.assertEqual(response.status_code, 200)
        self.assertIn("2024-12-20", html)
        self.assertIn("2024-12-26", html)
        self.assertIn("Synthetic selected month", html)
        self.assertIn("Synthetic fallback views", html)
        self.assertLess(html.index("data-owner-period-fallback"), html.index("avito-owner-charts"))
        self.assertNotIn("data-owner-kpi=", html)
