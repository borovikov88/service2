import json
from datetime import timedelta
from http.client import IncompleteRead
from unittest.mock import MagicMock, patch

from django.test import SimpleTestCase, TestCase
from django.urls import reverse
from django.utils import timezone

from pool_service import avito_workspace
from pool_service.communication_avito import (
    AvitoAmbiguousDeliveryError, AvitoError, AvitoRetryableError,
    _json_list_request, _json_request,
)
from pool_service.communication_models import AvitoApiThrottle, ChannelConnection, CommunicationChannel
from pool_service.tests.test_avito_management import AvitoManagementTests
from pool_service.tests.test_avito_workspace import AvitoWorkspaceViewTests


class AvitoRemoteParsingTests(SimpleTestCase):
    def test_v2_missing_metrics_stay_unknown_and_kopeks_convert_once(self):
        data = avito_workspace.statistics_data({"result": {"groupings": [{"metrics": [
            {"slug": "contacts", "value": 0}, {"slug": "spending", "value": 12599},
            {"slug": "averageViewCost", "value": 12.34},
        ]}], "dataTotalCount": 1}}, start="2026-10-01", end="2026-10-08", grouping="totals", offset=0)
        metrics = {x["slug"]: x for x in data["rows"][0]["metrics"]}
        self.assertIsNone(metrics["views"]["value"])
        self.assertEqual(metrics["contacts"]["value"], "0")
        self.assertEqual(metrics["spending"]["value"], "125.99")
        self.assertEqual(metrics["spending"]["unit"], "₽")
        self.assertIsNone(metrics["averageViewCost"]["value"])
        self.assertNotEqual(metrics["averageViewCost"]["unit"], "₽")

    def test_v2_empty_differs_from_malformed(self):
        result = avito_workspace.statistics_data({"result": {"groupings": []}}, start="2026-10-01", end="2026-10-08", grouping="item", offset=0)
        self.assertEqual(result["rows"], [])
        self.assertIsNone(result["total"])
        for response in ({}, {"result": {}}, {"result": {"groupings": [{"id": "not-an-int", "metrics": []}]}}):
            with self.assertRaises(AvitoError):
                avito_workspace.statistics_data(response, start="2026-10-01", end="2026-10-08", grouping="item", offset=0)

    @patch("pool_service.avito_workspace.enforce_rate")
    @patch("pool_service.avito_workspace._post", return_value={"result": {"groupings": []}})
    def test_v2_request_matches_supplied_contract(self, post, rate):
        avito_workspace.fetch_statistics("token", "123", start="2026-10-01", end="2026-10-08", grouping="item", offset=50)
        token, path, body = post.call_args.args
        self.assertEqual(path, "/stats/v2/accounts/123/items")
        self.assertEqual((body["limit"], body["offset"], body["grouping"]), (50, 50, "item"))
        self.assertIn("views", body["metrics"])
        self.assertNotIn("uniqViews", body["metrics"])
        rate.assert_called_once_with("123", "statistics-v2")

    @patch("pool_service.avito_workspace.enforce_rate")
    @patch("pool_service.avito_workspace._get", return_value={"resources": []})
    def test_all_statuses_explicitly_override_avito_active_default(self, get, _rate):
        avito_workspace.fetch_section("items", "token", "123")
        from urllib.parse import parse_qs, urlsplit
        query = parse_qs(urlsplit(get.call_args.args[1]).query)
        self.assertEqual(query["status"], [",".join(avito_workspace.ITEM_STATUSES)])

    @patch("pool_service.avito_workspace._get", return_value={"status": "another_user"})
    def test_foreign_item_is_not_a_successful_detail(self, _get):
        with self.assertRaisesMessage(AvitoError, "provider_item_unavailable"):
            avito_workspace.fetch_item_detail("token", "123", "11")

    @patch("pool_service.avito_workspace._get")
    def test_item_detail_status_must_be_text(self, get):
        for value in ([], {}, None, True):
            get.return_value = {"status": value}
            with self.assertRaisesMessage(AvitoError, "provider_data_invalid"):
                avito_workspace.fetch_item_detail("token", "123", "11")

    @patch("pool_service.avito_workspace._post", return_value=[{"itemId": 11, "vas": [{"slug": "xl", "price": 199.95, "priceOld": 299}], "stickers": []}])
    def test_prices_are_read_only_rubles_and_match_requested_item(self, post):
        data = avito_workspace.fetch_prices("token", "123", "11")
        self.assertEqual(data["services"][0]["price"], "199.95")
        self.assertEqual(post.call_args.args[1:], ("/core/v1/accounts/123/vas/prices", {"itemIds": [11]}))
        self.assertTrue(post.call_args.kwargs["array"])
        with self.assertRaises(AvitoError):
            avito_workspace.fetch_prices("token", "123", "12")

    @patch("pool_service.avito_workspace._post")
    def test_calls_empty_is_unknown_and_valid_zero_is_zero(self, post):
        post.return_value = {"result": {"items": []}}
        empty = avito_workspace.fetch_calls("token", "123", start="2026-10-01", end="2026-10-08", item_ids=["11"])
        self.assertIsNone(empty["totals"]["calls"])
        post.return_value = {"result": {"items": [{"itemId": 11, "employeeId": 0, "days": [{"date": "2026-10-01", "calls": 0, "answered": 0, "new": 0, "newAnswered": 0}]}]}}
        zero = avito_workspace.fetch_calls("token", "123", start="2026-10-01", end="2026-10-08", item_ids=["11"])
        self.assertEqual(zero["totals"]["calls"], 0)
        self.assertNotIn("employeeId", str(zero))

    @patch("pool_service.avito_workspace._post")
    def test_calls_reject_out_of_scope_ids_and_dates(self, post):
        for item_id, when in [(999, "2026-10-01"), (11, "2026-09-01")]:
            post.return_value = {"result": {"items": [{"itemId": item_id, "days": [{"date": when, "calls": 1, "answered": 1, "new": 1, "newAnswered": 1}]}]}}
            with self.assertRaises(AvitoError):
                avito_workspace.fetch_calls("token", "123", start="2026-10-01", end="2026-10-08", item_ids=["11"])

    @patch("pool_service.communication_avito.urlopen")
    def test_truncated_transport_is_sanitized_and_sends_stay_ambiguous(self, urlopen):
        response = MagicMock()
        response.read.side_effect = IncompleteRead(b"PRIVATE_BODY", 100)
        urlopen.return_value.__enter__.return_value = response
        for request in (_json_request, _json_list_request):
            with self.assertRaisesMessage(AvitoRetryableError, "provider_unavailable"):
                request("https://api.avito.ru/synthetic")
        with self.assertRaisesMessage(AvitoAmbiguousDeliveryError, "provider_delivery_unknown"):
            _json_request("https://api.avito.ru/synthetic", ambiguous_transport=True)


class AvitoAnalyticsViewTests(AvitoWorkspaceViewTests):
    # Retain inherited security regressions while exercising the new endpoints.
    @patch("pool_service.avito_management.access_token", return_value="token")
    @patch("pool_service.avito_workspace._get", return_value={"id": 123})
    @patch("pool_service.avito_workspace._post", return_value={"result": {"groupings": [{"id": 0, "metrics": [{"slug": "views", "value": 10}]}]}})
    def test_statistics_refresh_is_manual_and_scoped(self, post, _get, _token):
        self.client.force_login(self.owner)
        self.client.post(self.refresh_url(), {"section": "statistics", "grouping": "totals"})
        result = self.report()["statistics"]
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["data"]["grouping"], "totals")
        self.assertEqual(post.call_count, 1)
        post.reset_mock()
        self.assertEqual(self.client.get(self.url).status_code, 200)
        post.assert_not_called()

    @patch("pool_service.avito_management.access_token")
    def test_invalid_statistics_period_and_unlisted_item_never_call_api(self, token):
        self.client.force_login(self.owner)
        for data in ({"section": "statistics", "stats_date_from": "2020-01-01"},
                     {"section": "statistics", "stats_date_from": "2050-01-01"},
                     {"section": "statistics", "grouping": "invalid"},
                     {"section": "item_detail", "item_id": "999"},
                     {"section": "prices", "item_id": "999"}, {"section": "calls"}):
            self.client.post(self.refresh_url(), data)
        token.assert_not_called()

    @patch("pool_service.avito_management.access_token", return_value="token")
    def test_truncated_items_mark_old_snapshot_stale_and_autoload_continues(self, _token):
        self.client.force_login(self.owner)
        with patch("pool_service.avito_workspace._get", side_effect=self.provider):
            self.client.post(self.refresh_url(), {"section": "all"})
        previous = self.report()["items"]["data"]
        real_get = avito_workspace._get
        def interrupted(token, path):
            return real_get(token, path) if "/items?" in path else self.provider(token, path)
        response = MagicMock()
        response.read.side_effect = IncompleteRead(b"PRIVATE", 100)
        with patch("pool_service.avito_workspace._get", side_effect=interrupted), patch("pool_service.communication_avito.urlopen") as urlopen:
            urlopen.return_value.__enter__.return_value = response
            result = self.client.post(self.refresh_url(), {"section": "all"})
        self.assertEqual(result.status_code, 302)
        report = self.report()
        self.assertTrue(report["items"]["stale"])
        self.assertEqual(report["items"]["code"], "provider_unavailable")
        self.assertEqual(report["items"]["data"], previous)
        self.assertEqual(report["autoload"]["status"], "ok")
        self.assertNotIn("PRIVATE", str(report))

    @patch("pool_service.avito_management.access_token", return_value="token")
    def test_malformed_detail_keeps_old_snapshot_and_marks_stale(self, _token):
        self.client.force_login(self.owner)
        with patch("pool_service.avito_workspace._get", side_effect=self.provider):
            self.client.post(self.refresh_url(), {"section": "items"})
        with patch("pool_service.avito_workspace._get", side_effect=[{"id": 123}, {"status": "active"}]):
            self.client.post(self.refresh_url(), {"section": "item_detail", "item_id": "11"})
        old = self.report()["item_detail"]["data"]
        with patch("pool_service.avito_workspace._get", side_effect=[{"id": 123}, {"status": []}]):
            response = self.client.post(self.refresh_url(), {"section": "item_detail", "item_id": "11"})
        self.assertEqual(response.status_code, 302)
        actual = self.report()["item_detail"]
        self.assertEqual(actual["data"], old)
        self.assertTrue(actual["stale"])
        self.assertEqual(actual["code"], "provider_data_invalid")


class AvitoApiThrottleTests(TestCase):
    setUp = AvitoManagementTests.setUp

    def test_same_account_method_is_shared_but_other_methods_and_accounts_are_independent(self):
        avito_workspace.enforce_rate("123", "statistics-v2")
        with self.assertRaises(avito_workspace.AvitoCooldownError):
            avito_workspace.enforce_rate("123", "statistics-v2")
        avito_workspace.enforce_rate("123", "items", seconds=3)
        avito_workspace.enforce_rate("456", "statistics-v2")
        self.assertEqual(AvitoApiThrottle.objects.count(), 3)
        AvitoApiThrottle.objects.update(next_allowed_at=timezone.now() - timedelta(seconds=1))
        avito_workspace.enforce_rate("123", "statistics-v2")

    @patch("pool_service.avito_management.access_token", return_value="token")
    @patch("pool_service.avito_workspace._get", return_value={"id": 123})
    @patch("pool_service.avito_workspace._post", return_value={"result": {"groupings": []}})
    def test_duplicate_connections_share_rate_limit(self, post, _get, _token):
        duplicate_channel = CommunicationChannel.objects.create(organization=self.org, name="Second", kind="avito")
        duplicate = ChannelConnection.objects.create(channel=duplicate_channel, name="Second", external_id="123")
        self.client.force_login(self.owner)
        for connection in (self.connection, duplicate):
            self.client.post(reverse("avito_refresh_data", args=[connection.pk]), {"section": "statistics"})
        self.assertEqual(post.call_count, 1)
        duplicate.refresh_from_db()
        self.assertEqual(duplicate.settings["avito_workspace"]["sections"]["statistics"]["code"], "provider_cooldown")
