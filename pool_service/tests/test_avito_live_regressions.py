from copy import deepcopy
from unittest.mock import patch

from django.test import SimpleTestCase, TestCase

from pool_service import avito_workspace
from pool_service.communication_avito import AvitoError
from pool_service.tests import test_avito_management


class AvitoMetricUnitsTests(SimpleTestCase):
    @staticmethod
    def cached_statistics():
        # Anonymous numerical regression fixture; no account or listing identity.
        values = {
            "impressions": "2163", "views": "247", "contacts": "7", "favorites": "21",
            "viewsToContactsConversion": "283.00", "impressionsToViewsConversion": "1141.00",
            "viewsToOrderedItemsConversion": "555.00", "averageViewCost": "987.00",
            "averageContactCost": "654321.00", "spendingBonus": "400.00",
        }
        return {"grouping": "totals", "date_from": "2026-10-01", "date_to": "2026-10-08",
                "rows": [{"id": 0, "label": "Итого", "metrics": [
                    {"slug": slug, "label": label, "value": values.get(slug), "unit": unit}
                    for slug, label, unit in avito_workspace.METRICS
                ]}], "metric_labels": []}

    def test_cached_percentages_use_explicit_matching_group_counts(self):
        original = self.cached_statistics()
        untouched = deepcopy(original)
        data = avito_workspace.statistics_for_display(original)
        values = {x["slug"]: x for x in data["rows"][0]["metrics"]}
        self.assertEqual(values["viewsToContactsConversion"]["value"], "2.83")
        self.assertEqual(values["impressionsToViewsConversion"]["value"], "11.42")
        self.assertEqual(values["viewsToContactsConversion"]["source"], "calculated")
        self.assertIn("Service2", values["viewsToContactsConversion"]["note"])
        for slug in avito_workspace.UNVERIFIED_UNITS:
            self.assertIsNone(values[slug]["value"])
            self.assertEqual(values[slug]["unit"], "")
        self.assertEqual(original, untouched)
        self.assertEqual(avito_workspace.statistics_for_display(data), data)

    def test_new_response_ignores_ambiguous_raw_percent_scale(self):
        response = {"result": {"groupings": [{"id": 0, "metrics": [
            {"slug": "contacts", "value": 7}, {"slug": "views", "value": 247},
            {"slug": "impressions", "value": 2163}, {"slug": "viewsToContactsConversion", "value": 283},
            {"slug": "impressionsToViewsConversion", "value": 1141}, {"slug": "spending", "value": 12599},
        ]}]}}
        result = avito_workspace.statistics_data(response, start="2026-10-01", end="2026-10-08", grouping="totals", offset=0)
        values = {x["slug"]: x["value"] for x in result["rows"][0]["metrics"]}
        self.assertEqual(values["viewsToContactsConversion"], "2.83")
        self.assertEqual(values["impressionsToViewsConversion"], "11.42")
        self.assertEqual(values["spending"], "125.99")

    def test_missing_or_zero_denominator_does_not_invent_percentage(self):
        for denominator in (None, "0", "-1", "NaN"):
            result = avito_workspace.statistics_for_display({"rows": [{"metrics": [
                {"slug": "contacts", "value": "0"}, {"slug": "views", "value": denominator},
                {"slug": "viewsToContactsConversion", "value": "999"},
            ]}]})
            self.assertIsNone(result["rows"][0]["metrics"][2]["value"])
        result = avito_workspace.statistics_for_display({"rows": [{"metrics": [
            {"slug": "contacts", "value": "0"}, {"slug": "views", "value": "10"},
            {"slug": "viewsToContactsConversion", "value": "999"},
        ]}]})
        self.assertEqual(result["rows"][0]["metrics"][2]["value"], "0.00")

    def test_each_group_is_derived_independently(self):
        rows = []
        for views, contacts in ((10, 1), (30, 9)):
            rows.append({"metrics": [{"slug": "views", "value": views}, {"slug": "contacts", "value": contacts},
                                     {"slug": "viewsToContactsConversion", "value": 999}]})
        result = avito_workspace.statistics_for_display({"rows": rows})
        self.assertEqual([x["metrics"][2]["value"] for x in result["rows"]], ["10.00", "30.00"])


class AvitoSparseCallTests(SimpleTestCase):
    def fetch(self, items):
        with patch("pool_service.avito_workspace._post", return_value={"result": {"items": items}}):
            return avito_workspace.fetch_calls("token", "123", start="2026-10-01", end="2026-10-08", item_ids=["11", "12"])

    def test_missing_optional_counters_are_unknown_but_known_counts_survive(self):
        result = self.fetch([{"itemId": 11, "employeeId": 0, "days": [{"date": "2026-10-01", "calls": 3, "answered": 2}]}])
        self.assertEqual(result["totals"]["calls"], 3)
        self.assertEqual(result["totals"]["answered"], 2)
        self.assertIsNone(result["totals"]["new"])
        self.assertIsNone(result["totals"]["new_answered"])
        self.assertTrue(result["incomplete"])

    def test_missing_days_and_dates_do_not_become_zero(self):
        for row in ({"itemId": 11}, {"itemId": 11, "days": []}, {"itemId": 11, "days": [{"calls": 2}]}):
            result = self.fetch([row])
            self.assertTrue(result["incomplete"])
            self.assertTrue(all(value is None for value in result["totals"].values()))

    def test_unknown_contribution_prevents_misleading_complete_sum(self):
        result = self.fetch([
            {"itemId": 11, "days": [{"date": "2026-10-01", "calls": 2, "answered": 1, "new": 0, "newAnswered": 0}]},
            {"itemId": 12, "days": [{"date": "2026-10-01", "answered": 1, "new": 0, "newAnswered": 0}]},
        ])
        self.assertIsNone(result["totals"]["calls"])
        self.assertEqual(result["totals"]["answered"], 2)
        self.assertEqual(result["rows"][0]["calls"], 2)

    def test_invalid_contract_fields_have_safe_specific_diagnostics(self):
        cases = [
            ([{"itemId": 999}], "provider_calls_item_out_of_scope"),
            ([{"itemId": 11, "days": "PRIVATE"}], "provider_calls_days_invalid"),
            ([{"itemId": 11, "days": [{"date": "PRIVATE"}]}], "provider_calls_date_invalid"),
            ([{"itemId": 11, "days": [{"date": "2026-09-01"}]}], "provider_calls_date_out_of_scope"),
            ([{"itemId": 11, "days": [{"date": "2026-10-01", "calls": "PRIVATE"}]}], "provider_calls_counter_invalid"),
        ]
        for items, code in cases:
            with self.assertRaisesMessage(AvitoError, code):
                self.fetch(items)


class AvitoSavedMetricsRenderTests(TestCase):
    setUp = test_avito_management.AvitoManagementTests.setUp

    def test_saved_inflated_percentages_disappear_without_network_or_resave(self):
        snapshot = AvitoMetricUnitsTests.cached_statistics()
        self.connection.settings = {"avito_workspace": {"account_id": "123", "sections": {
            "statistics": {"status": "ok", "data": snapshot, "success_at": "2026-10-08T00:00:00+00:00"},
        }}}
        self.connection.save(update_fields=["settings"])
        before = deepcopy(self.connection.settings)
        self.client.force_login(self.owner)
        with patch("pool_service.avito_workspace._post") as post, patch("pool_service.avito_workspace._get") as get:
            response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "2.83")
        self.assertContains(response, "11.42")
        self.assertContains(response, "Расчёт Service2")
        self.assertNotContains(response, "283.00")
        self.assertNotContains(response, "1141.00")
        self.assertNotContains(response, "654321.00")
        post.assert_not_called()
        get.assert_not_called()
        self.connection.refresh_from_db()
        self.assertEqual(self.connection.settings, before)


class AvitoCompactUiScopeTests(TestCase):
    setUp = test_avito_management.AvitoManagementTests.setUp

    def seed_items(self):
        self.connection.settings = {"avito_workspace": {"account_id": "123", "sections": {
            "items": {"status": "ok", "data": {"rows": [{"id": "11", "title": "Item one"}, {"id": "12", "title": "Item two"}], "page": 1, "status": ""}},
            "item_detail": {"status": "ok", "data": {"item_id": "11", "status": "active", "autoload_item_id": "OLD-OTHER-ITEM"}},
            "prices": {"status": "ok", "data": {"item_id": "11", "services": [{"slug": "highlight", "price": "40.00", "price_old": 40}], "stickers": []}},
        }}}
        self.connection.save(update_fields=["settings"])

    def test_diagnostics_only_on_scoped_settings_no_network_or_nested_forms(self):
        from html.parser import HTMLParser
        class Forms(HTMLParser):
            def __init__(self):
                super().__init__()
                self.current = None
                self.forms = []
                self.nested = False
            def handle_starttag(self, tag, attrs):
                attrs = dict(attrs)
                if tag == "form":
                    self.nested = self.nested or self.current is not None
                    self.current = {"action": attrs.get("action", ""), "inputs": []}
                    self.forms.append(self.current)
                elif tag == "input" and self.current is not None:
                    self.current["inputs"].append(attrs.get("name"))
            def handle_endtag(self, tag):
                if tag == "form":
                    self.current = None
        self.client.force_login(self.owner)
        with patch("pool_service.avito_management.access_token") as token, patch("pool_service.avito_workspace._get") as get:
            dashboard = self.client.get(self.url)
            settings_page = self.client.get(self.settings_url)
        self.assertNotContains(dashboard, "Проверить API")
        self.assertContains(settings_page, 'id="avito-api-diagnostics"')
        self.assertContains(settings_page, "Проверить API")
        self.assertIn("no-store", settings_page["Cache-Control"])
        parsed = Forms()
        parsed.feed(settings_page.content.decode())
        self.assertFalse(parsed.nested)
        forms = [x for x in parsed.forms if x["action"] == self.check_url]
        self.assertEqual(len(forms), 1)
        self.assertEqual(forms[0]["inputs"], ["csrfmiddlewaretoken"])
        token.assert_not_called()
        get.assert_not_called()

    def test_settings_permission_and_organization_isolation_remain_enforced(self):
        from django.urls import reverse
        from pool_service.models import Organization
        from pool_service.communication_models import ChannelConnection, CommunicationChannel
        self.client.force_login(self.manager)
        self.assertEqual(self.client.get(self.settings_url).status_code, 403)
        foreign = CommunicationChannel.objects.create(organization=Organization.objects.create(name="Foreign settings"), kind="avito", name="Foreign")
        connection = ChannelConnection.objects.create(channel=foreign, external_id="999", name="Foreign private")
        self.client.force_login(self.owner)
        self.assertEqual(self.client.get(reverse("communication_connection_edit", args=[connection.pk])).status_code, 404)

    @patch("pool_service.avito_management.access_token", return_value="opaque")
    @patch("pool_service.avito_workspace._get", side_effect=[{"id": 123}, {"status": "active", "autoload_item_id": "CURRENT-ITEM"}])
    def test_detail_post_targets_inline_row_with_visible_anchor(self, _get, _token):
        from django.urls import reverse
        self.seed_items()
        self.client.force_login(self.owner)
        response = self.client.post(reverse("avito_refresh_data", args=[self.connection.pk]), {"section": "item_detail", "item_id": "12"})
        self.assertEqual(response["Location"], self.url + f"?account={self.connection.pk}&item_id=12#avito-item-12")
        page = self.client.get(response["Location"])
        html = page.content.decode()
        self.assertTrue(page.context["detail_in_items"])
        self.assertEqual(page.context["expanded_item_id"], "12")
        self.assertLess(html.index('data-avito-item-id="12"'), html.index('id="avito-item-12"'))
        self.assertLess(html.index('id="avito-item-12"'), html.index("</tbody>", html.index('data-avito-item-id="12"')))
        self.assertContains(page, "CURRENT-ITEM")
        self.assertNotContains(page, "OLD-OTHER-ITEM")
        self.assertContains(page, "Активно")

    @patch("pool_service.avito_management.access_token", return_value="opaque")
    @patch("pool_service.avito_workspace._get", side_effect=[{"id": 123}, AvitoError("provider_http_403")])
    def test_failed_new_detail_does_not_present_previous_item_as_selected(self, _get, _token):
        from django.urls import reverse
        self.seed_items()
        self.client.force_login(self.owner)
        response = self.client.post(reverse("avito_refresh_data", args=[self.connection.pk]), {"section": "item_detail", "item_id": "12"})
        page = self.client.get(response["Location"])
        self.assertContains(page, 'id="avito-item-12"')
        self.assertContains(page, "provider_http_403")
        self.assertNotContains(page, "OLD-OTHER-ITEM")
        self.assertNotContains(page, "40.00")
        self.connection.refresh_from_db()
        detail = self.connection.settings["avito_workspace"]["sections"]["item_detail"]
        self.assertEqual(detail["requested_item_id"], "12")
        self.assertEqual(detail["data"]["item_id"], "11")

    def test_same_price_is_not_struck_and_promotion_names_are_documented(self):
        self.seed_items()
        self.client.force_login(self.owner)
        page = self.client.get(self.url + "?item_id=11")
        self.assertContains(page, "Цены платного продвижения в Авито")
        self.assertContains(page, "Выделение объявления")
        self.assertNotContains(page, "text-decoration-line-through")
        services = avito_workspace.prices_for_display({"services": [
            {"slug": "highlight", "price": "40.00", "price_old": 40},
            {"slug": "xl", "price": "40.00", "price_old": "50.00"},
            {"slug": "unknown-future-service", "price": "5", "price_old": None},
        ]})["services"]
        self.assertFalse(services[0]["show_old_price"])
        self.assertTrue(services[1]["show_old_price"])
        self.assertEqual(services[1]["label"], "XL-объявление")
        self.assertEqual(services[2]["label"], "Услуга Авито")
