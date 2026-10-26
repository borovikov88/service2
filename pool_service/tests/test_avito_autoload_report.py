import csv
import io
from html.parser import HTMLParser
from unittest.mock import patch

from django.contrib.auth.models import User
from django.test import Client, SimpleTestCase, TestCase
from django.urls import reverse

from pool_service import avito_autoload_report as report_api, avito_workspace
from pool_service.communication_avito import AvitoError
from pool_service.communication_models import ChannelConnection, CommunicationChannel
from pool_service.models import Notification, Organization, OrganizationAccess


def upload(identifier=123, status="success_warning"):
    return {"upload_id": identifier, "status": status, "started_at": "2026-10-10T10:00:00Z",
            "stats": {"slug": "sections_stats", "title": "Обработано объявлений", "count": 2, "sections": [
                {"slug": "error", "title": "Не удалось опубликовать", "count": 1, "sections": []},
                {"slug": "successful", "title": "Опубликовано", "count": 1, "sections": []},
            ]}, "events": [], "feed_urls": [{"name": "feed", "url": "https://example.com/private?token=secret"}]}


def item(identifier="1_2_3", status="rejected", messages=None):
    if messages is None:
        messages = [{"code": 123, "type": "error", "title": "Категория",
                     "description": "Проверьте категорию товара.", "updated_at": "2026-10-09T12:00:00Z"}]
    return {"ad_id": identifier, "avito_id": 777, "avito_status": status,
            "section": {"slug": "error_rejected", "title": "Отклонено"}, "messages": messages}


def page(rows, number=1, total=None):
    total = len(rows) if total is None else total
    return {"items": rows, "meta": {"page": number, "perPage": 50, "pages": max(1, (total + 49) // 50), "total": total}}


def saved_report():
    with patch.object(avito_workspace, "_get", side_effect=[upload(), page([item()]), upload()]), patch.object(avito_workspace, "enforce_rate"):
        return report_api.fetch_report("token", "123")


class AutoloadReportTests(SimpleTestCase):
    def test_row_mismatch_diagnostic_distinguishes_order_time_and_payload_without_values(self):
        from copy import deepcopy
        first = report_api._item(item("PRIVATE_XML"))
        second_message = dict(first["messages"][0], code=456)
        first["messages"].append(second_message)
        cases = []
        changed = deepcopy(first)
        changed["messages"].reverse()
        cases.append((changed, {"messages": 1, "message_order_only": 1}))
        changed = deepcopy(first)
        changed["messages"][0]["updated_at"] = "2026-10-10T12:00:00+00:00"
        cases.append((changed, {"messages": 1, "message_time_only": 1}))
        changed = deepcopy(first)
        changed["messages"][0]["description"] = "PRIVATE_MESSAGE"
        changed["feed_name"] = "PRIVATE_FEED"
        cases.append((changed, {"feed_name": 1, "messages": 1}))
        for changed, expected in cases:
            with self.subTest(expected=expected):
                counts = report_api.row_difference_counts([first], {(first["ad_id"], first["avito_id"]): changed})
                self.assertEqual(expected, counts)
                failure = report_api.ReportChanged("queried_rows_changed", row_differences=counts)
                self.assertNotIn("PRIVATE", str(vars(failure)) + report_api.failure_detail(failure))
        self.assertEqual({"missing_identity": 1}, report_api.row_difference_counts([first], {}))

    def test_mismatch_failure_projection_rejects_untrusted_fields_counts_and_other_guards(self):
        from pool_service.avito_management import _workspace_failure
        error = report_api.ReportChanged("queried_rows_changed")
        error.row_differences = {"PRIVATE": 1, "feed_name": "PRIVATE", "messages": True,
                                 "avito_status": -1, "section": 51, "avito_date_end": 2}
        projected = _workspace_failure("autoload_report", error)
        self.assertEqual("queried_rows_changed", projected["consistency_guard"])
        self.assertIn("срок размещения — 2", projected["detail"])
        self.assertNotIn("PRIVATE", str(projected))
        self.assertNotIn("сообщения Авито —", projected["detail"])
        self.assertNotIn("Различия", report_api.failure_detail(report_api.ReportChanged(
            "summary_snapshot_changed", row_differences={"messages": 1})))

    def test_full_v4_pages_and_alias_are_checked_and_no_feed_urls_saved(self):
        rows = [item(str(i)) for i in range(55)]
        with patch.object(avito_workspace, "_get", side_effect=[upload(), page(rows[:50], total=55), page(rows[50:], 2, 55), upload()]) as get, patch.object(avito_workspace, "enforce_rate") as rate:
            data = report_api.fetch_report("token", "123", kind="current")
        self.assertEqual((data["total"], data["pages"], data["counts"]["error"]), (55, 2, 55))
        self.assertTrue(data["complete"])
        self.assertTrue(data["upload_finished"])
        self.assertEqual(get.call_args_list[0].args[1], "/autoload/v4/uploads/current")
        self.assertEqual(get.call_args_list[2].args[1], "/autoload/v4/uploads/current/items?perPage=50&page=2")
        self.assertEqual(get.call_args_list[-1].args[1], "/autoload/v4/uploads/current")
        self.assertNotIn("private", str(data))
        self.assertEqual(rate.call_count, 4)

    def test_repeated_identity_within_or_across_pages_blocks_complete_report(self):
        rows = [item(str(i)) for i in range(50)]
        variants = [
            [page([item(), item()])],
            [page(rows, total=51), page([rows[0]], 2, 51)],
            [page([item(messages=[]), item()])],
        ]
        for pages in variants:
            with self.subTest(pages=len(pages)), patch.object(
                avito_workspace, "_get", side_effect=[upload(), *pages, upload()]
            ), patch.object(avito_workspace, "enforce_rate"), self.assertRaisesMessage(
                AvitoError, "autoload_report_duplicate_rows"
            ):
                report_api.fetch_report("token", "123")

    def test_distinct_avito_ids_are_retained_as_separate_records(self):
        first, second = item(), item()
        second["avito_id"] = 778
        with patch.object(avito_workspace, "_get", side_effect=[
            upload(), page([first, second]), upload()
        ]), patch.object(avito_workspace, "enforce_rate"):
            data = report_api.fetch_report("token", "123")
        self.assertEqual(data["counts"]["error"], 2)
        self.assertEqual([row["avito_id"] for row in data["rows"]], [777, 778])

    def test_cached_duplicate_report_is_not_exported_as_complete(self):
        data = saved_report()
        data["rows"].append(dict(data["rows"][0]))
        with self.assertRaisesMessage(AvitoError, "autoload_report_duplicate_rows"):
            report_api.csv_text(data, "2026-10-10T11:00:00Z")

    def test_processing_snapshot_is_provisional_and_errors_are_not_upload_failure(self):
        with patch.object(avito_workspace, "_get", side_effect=[upload(status="processing"), page([item()]), upload(status="processing")]), patch.object(avito_workspace, "enforce_rate"):
            data = report_api.fetch_report("token", "123", kind="current")
        self.assertTrue(data["complete"])
        self.assertFalse(data["upload_finished"])
        self.assertEqual(data["counts"]["error"], 1)

    def test_alias_change_or_summary_change_does_not_return_success(self):
        changed = upload()
        changed["stats"]["count"] = 3
        for after in (upload(124), changed):
            with self.subTest(after=after), patch.object(avito_workspace, "_get", side_effect=[upload(), page([item()]), after]), patch.object(avito_workspace, "enforce_rate"), self.assertRaisesMessage(AvitoError, "autoload_report_changed"):
                report_api.fetch_report("token", "123")

    def test_incomplete_page_invalid_meta_deadline_and_size_limits_fail(self):
        invalid = [page([], total=2), {"items": [], "meta": {"page": 1, "perPage": True, "pages": 1, "total": 0}}]
        for payload in invalid:
            with patch.object(avito_workspace, "_get", side_effect=[upload(), payload]), patch.object(avito_workspace, "enforce_rate"), self.assertRaises(AvitoError):
                report_api.fetch_report("token", "123")
        with patch.object(report_api.time, "monotonic", side_effect=[0, 56]), self.assertRaisesMessage(AvitoError, "autoload_report_limit"):
            report_api.fetch_report("token", "123")
        with patch.object(report_api, "MAX_BYTES", 20), patch.object(avito_workspace, "_get", side_effect=[upload(), page([item()])]), patch.object(avito_workspace, "enforce_rate"), self.assertRaisesMessage(AvitoError, "autoload_report_limit"):
            report_api.fetch_report("token", "123")

    def test_optional_status_id_and_archived_removed_are_distinct(self):
        self.assertEqual(report_api._item(item(status="archived"))["status_label"], "В архиве")
        self.assertEqual(report_api._item(item(status="removed"))["status_label"], "Удалено навсегда")
        row = item(status=None)
        row["avito_id"] = None
        normalized = report_api._item(row)
        self.assertIsNone(normalized["avito_id"])
        self.assertEqual(normalized["status_label"], "Не передан")

    def test_unhashable_and_malformed_types_are_domain_errors(self):
        for status in ([], {}, True):
            with self.subTest(status=status), self.assertRaises(AvitoError):
                report_api.summary_data(upload(status=status))
            with self.assertRaises(AvitoError):
                report_api._item(item(status=status))
        row = item()
        row["messages"][0]["type"] = []
        with self.assertRaises(AvitoError):
            report_api._item(row)
        row = item()
        row["messages"][0]["updated_at"] = "2026-10-10T10:00:00"
        with self.assertRaises(AvitoError):
            report_api._item(row)

    def test_messages_redact_contacts_urls_credentials_and_mark_truncation(self):
        row = item()
        row["messages"][0]["description"] = "Email a@example.com +79991234567 https://x.test/?secret=1 Bearer private-token client_secret=private-key " + "x" * 4000
        message = report_api._item(row)["messages"][0]
        self.assertTrue(message["truncated"])
        self.assertLessEqual(len(message["description"]), 3000)
        for secret in ("a@example.com", "79991234567", "secret=1", "private-token", "private-key"):
            self.assertNotIn(secret, message["description"])

    def test_quoted_credentials_in_messages_and_global_events_are_not_saved(self):
        secrets = [
            '{"client_secret": "private key with spaces", "access_token":"private-token"}',
            "'refresh_token' = 'private token'",
            'password: "private password with spaces"',
        ]
        for text in secrets:
            with self.subTest(text=text):
                row = item()
                row["messages"][0]["description"] = text
                summary = upload()
                summary["events"] = [{"code": 7, "type": "error", "description": text}]
                with patch.object(avito_workspace, "_get", side_effect=[summary, page([row]), summary]), patch.object(avito_workspace, "enforce_rate"):
                    data = report_api.fetch_report("token", "123")
                self.assertNotIn("private", str(data))
                self.assertNotIn("private", report_api.csv_text(data, "2026-10-10T11:00:00Z"))

    def test_csv_uses_cached_full_rows_with_formula_guard_and_global_events(self):
        data = saved_report()
        data["rows"][0]["ad_id"] = "=1+1"
        data["rows"][0]["messages"][0]["title"] = "+SUM(1,2)"
        data["events"] = [{"code": 5, "type": "error", "description": "Ошибка файла"}]
        with patch.object(avito_workspace, "_get") as get:
            text = report_api.csv_text(data, "2026-10-10T11:00:00Z")
        get.assert_not_called()
        self.assertTrue(text.startswith("\ufeff"))
        rows = list(csv.reader(io.StringIO(text.lstrip("\ufeff"))))
        self.assertEqual(rows[1][0], "'=1+1")
        self.assertEqual(rows[1][6], "'+SUM(1,2)")
        self.assertEqual(rows[-1][3], "Вся загрузка")
        self.assertEqual(rows[-1][7], "Ошибка файла")

    def test_real_v4_summary_tree_is_displayed_without_double_summing(self):
        data = avito_workspace.autoload_data(upload())
        self.assertEqual(data["id"], "123")
        self.assertEqual([row["value"] for row in data["counters"]], [2, 1, 1])


class AutoloadReportViewTests(TestCase):
    def setUp(self):
        self.org = Organization.objects.create(name="Autoload test")
        self.owner = User.objects.create_user("autoload-owner")
        self.manager = User.objects.create_user("autoload-manager")
        OrganizationAccess.objects.create(organization=self.org, user=self.owner, role="owner")
        OrganizationAccess.objects.create(organization=self.org, user=self.manager, role="manager")
        channel = CommunicationChannel.objects.create(organization=self.org, kind="avito", name="Avito")
        self.connection = ChannelConnection.objects.create(channel=channel, external_id="123", name="Goods")
        self.refresh = reverse("avito_refresh_data", args=[self.connection.pk])
        self.download = reverse("avito_download_report", args=[self.connection.pk])
        self.client.force_login(self.owner)
        for target, result in (("pool_service.avito_management.access_token", "test-token"), ("pool_service.avito_workspace._get", {"id": 123})):
            mock = patch(target, return_value=result)
            mock.start()
            self.addCleanup(mock.stop)

    def state(self):
        self.connection.refresh_from_db()
        return self.connection.settings["avito_workspace"]["sections"]["autoload_report"]

    def sample(self):
        return {"version": 1, "complete": True, "kind": "current", "upload_id": "123", "upload_status": "processing",
                "upload_finished": False, "total": 1, "pages": 1, "counts": {"error": 1},
                "rows": [report_api._item(item())], "events": []}

    def test_refresh_download_and_get_have_no_provider_writes_or_get_fetch(self):
        with patch.object(report_api, "fetch_report", return_value=self.sample()) as fetch:
            self.client.post(self.refresh, {"section": "autoload_report", "autoload_kind": "current"})
            fetch.assert_called_once_with("test-token", "123", kind="current")
            response = self.client.get(reverse("avito_dashboard"))
            csv_response = self.client.get(self.download)
            self.assertEqual(fetch.call_count, 1)
        self.assertContains(response, "Скачать отчёт CSV")
        self.assertContains(response, "предварительный снимок")
        self.assertContains(response, "Критическая ошибка")
        self.assertEqual(csv_response.status_code, 200)
        self.assertIn("Категория", csv_response.content.decode("utf-8-sig"))
        self.assertIn("attachment", csv_response["Content-Disposition"])
        self.assertIn("no-store", csv_response["Cache-Control"])
        self.assertFalse(Notification.objects.exists())

    def test_cached_duplicate_report_has_no_counters_or_download_and_get_is_readonly(self):
        data = self.sample()
        data["rows"].append(dict(data["rows"][0]))
        data["total"] = 2
        self.connection.settings = {"avito_workspace": {"account_id": "123", "sections": {
            "autoload_report": {"data": data, "success_at": "2026-10-10T11:00:00Z", "status": "ok"}
        }}}
        self.connection.save(update_fields=["settings"])
        previous = self.connection.settings
        with patch.object(report_api, "fetch_report") as fetch:
            response = self.client.get(reverse("avito_dashboard"))
            self.assertEqual(self.client.get(self.download).status_code, 404)
        fetch.assert_not_called()
        self.assertContains(response, "В отчёте Авито повторяются строки")
        self.assertNotContains(response, "Весь доступный список загрузки:")
        self.connection.refresh_from_db()
        self.assertEqual(self.connection.settings, previous)

    def test_kind_is_sent_even_when_submit_handler_disables_buttons(self):
        class Forms(HTMLParser):
            def __init__(self):
                super().__init__()
                self.forms, self.current = [], None

            def handle_starttag(self, tag, attrs):
                attrs = dict(attrs)
                if tag == "form":
                    self.current = {"action": attrs.get("action"), "fields": {}}
                elif tag == "input" and self.current is not None and attrs.get("name") and "disabled" not in attrs:
                    self.current["fields"][attrs["name"]] = attrs.get("value", "")
                # Buttons disabled by avito-dashboard.js are not successful controls.

            def handle_endtag(self, tag):
                if tag == "form" and self.current is not None:
                    self.forms.append(self.current)
                    self.current = None

        response = self.client.get(reverse("avito_dashboard"))
        parser = Forms()
        parser.feed(response.content.decode())
        forms = [form for form in parser.forms if form["action"] == self.refresh
                 and form["fields"].get("section") == "autoload_report"]
        self.assertEqual([form["fields"].get("autoload_kind") for form in forms], ["current", "last_successful"])
        for form in forms:
            with patch.object(report_api, "fetch_report", return_value=self.sample()) as fetch:
                self.client.post(form["action"], form["fields"])
                fetch.assert_called_once_with("test-token", "123", kind=form["fields"]["autoload_kind"])

    def test_failed_refresh_keeps_previous_data_and_success_time(self):
        with patch.object(report_api, "fetch_report", return_value=self.sample()):
            self.client.post(self.refresh, {"section": "autoload_report"})
        old = self.state()
        for code in ("autoload_report_changed", "autoload_report_duplicate_rows"):
            with self.subTest(code=code), patch.object(report_api, "fetch_report", side_effect=AvitoError(code)):
                self.client.post(self.refresh, {"section": "autoload_report"})
            new = self.state()
            self.assertEqual(new["data"], old["data"])
            self.assertEqual(new["success_at"], old["success_at"])
            self.assertTrue(new["stale"])

    def test_csrf_permissions_organization_account_binding_and_download_methods(self):
        csrf = Client(enforce_csrf_checks=True)
        csrf.force_login(self.owner)
        self.assertEqual(csrf.post(self.refresh, {"section": "autoload_report"}).status_code, 403)
        self.assertEqual(self.client.post(self.download).status_code, 405)
        self.assertEqual(self.client.get(self.download).status_code, 404)
        self.client.force_login(self.manager)
        self.assertEqual(self.client.post(self.refresh, {"section": "autoload_report"}).status_code, 403)
        self.assertEqual(self.client.get(self.download).status_code, 403)
        foreign = Organization.objects.create(name="Foreign autoload")
        channel = CommunicationChannel.objects.create(organization=foreign, kind="avito", name="Other")
        connection = ChannelConnection.objects.create(channel=channel, external_id="999")
        self.client.force_login(self.owner)
        self.assertEqual(self.client.get(reverse("avito_download_report", args=[connection.pk])).status_code, 404)
        self.connection.settings = {"avito_workspace": {"account_id": "999", "sections": {"autoload_report": {"data": self.sample()}}}}
        self.connection.save(update_fields=["settings"])
        self.assertEqual(self.client.get(self.download).status_code, 404)

    def test_wrong_provider_account_and_change_during_fetch_cannot_save(self):
        with patch.object(avito_workspace, "_get", return_value={"id": 999}), patch.object(report_api, "fetch_report") as fetch:
            self.client.post(self.refresh, {"section": "autoload_report"})
        fetch.assert_not_called()
        def changed(*args, **kwargs):
            ChannelConnection.objects.filter(pk=self.connection.pk).update(external_id="999")
            return self.sample()
        with patch.object(report_api, "fetch_report", side_effect=changed):
            self.client.post(self.refresh, {"section": "autoload_report"})
        self.connection.refresh_from_db()
        self.assertNotIn("data", self.connection.settings["avito_workspace"]["sections"]["autoload_report"])

    def test_general_refresh_does_not_start_full_report(self):
        with patch.object(report_api, "fetch_report") as fetch, patch.object(avito_workspace, "fetch_section", return_value={}):
            self.client.post(self.refresh, {"section": "all"})
        fetch.assert_not_called()

class ShopFeedSourceTests(SimpleTestCase):
    def test_only_known_source_kind_is_saved_without_url_or_tokens(self):
        self.assertEqual("shop_aggregate_file", report_api.shop_feed_source(
            [{"name": "PRIVATE", "url": "https://shop.aqualine22.ru/avito/avitofeed.xml"}]))
        self.assertEqual("shop_profile_43_file", report_api.shop_feed_source(
            [{"url": "https://shop.aqualine22.ru/avito/avitofeed_43.xml"}]))
        for value in ("https://shop.aqualine22.ru/avito/avitofeed.xml?token=PRIVATE",
                      "https://other.example/avito/avitofeed.xml", "https://user:PRIVATE@shop.aqualine22.ru/avito/avitofeed.xml"):
            self.assertEqual("other_or_unknown", report_api.shop_feed_source([{"url": value}]))
        self.assertEqual("other_or_unknown", report_api.shop_feed_source([
            {"url": "https://shop.aqualine22.ru/avito/avitofeed.xml"},
            {"url": "https://shop.aqualine22.ru/avito/avitofeed_43.xml"}]))


class FeedSourceDiagnosticsTests(SimpleTestCase):
    def test_structural_facts_never_expose_urls_names_or_values(self):
        feed = {"name": "PRIVATE_NAME", "url": "https://shop.aqualine22.ru/avito/avitofeed.xml?token=PRIVATE_VALUE"}
        result = report_api.feed_source_diagnostics([feed])
        self.assertEqual("aggregate_file", result[0]["path_kind"])
        self.assertTrue(result[0]["query_present"])
        self.assertEqual("shop", result[0]["host_kind"])
        self.assertNotIn("PRIVATE", str(result))
        self.assertNotIn("https://", str(result))
        self.assertEqual("other_or_unknown", report_api.shop_feed_source([feed]))
        self.assertEqual("string", report_api.feed_source_diagnostics(["https://example.invalid/PRIVATE"])[0]["shape"])
        self.assertEqual("unsupported", report_api.feed_source_diagnostics([{"private": "SECRET"}])[0]["shape"])
    def test_receipt_projection_rejects_untrusted_diagnostic_fields(self):
        from scripts.avito_readonly_receipt import report
        value = {"upload_id": "1", "upload_status": "success", "complete": True,
                 "finished": True, "total": 0, "critical_samples": [],
                 "shop_feed_diagnostics": report_api.feed_source_diagnostics(
                     [{"url": "https://shop.aqualine22.ru/avito/avitofeed.xml"}])}
        self.assertEqual("aggregate_file", report(value)["shop_feed_diagnostics"][0]["path_kind"])
        value["shop_feed_diagnostics"][0]["host_kind"] = "PRIVATE_URL"
        with self.assertRaises(ValueError):
            report(value)


class DuplicateReportDiagnosticTests(SimpleTestCase):
    def fetch_duplicate(self, pages):
        with patch.object(avito_workspace, "_get", side_effect=[upload(), *pages, upload()]), \
             patch.object(avito_workspace, "enforce_rate"):
            try:
                report_api.fetch_report("token", "123")
            except report_api.ReportDuplicateRows as error:
                return error.diagnostics
        self.fail("Duplicate report must remain blocked")

    def test_within_page_equal_and_changed_rows_are_distinguished(self):
        first = item("PRIVATE_XML")
        changed = item("PRIVATE_XML", messages=[])
        first["avito_id"] = changed["avito_id"] = 987654321
        first["feed_name"] = changed["feed_name"] = "PRIVATE_FEED"
        diagnostic = self.fetch_duplicate([page([first, first, changed])])
        self.assertEqual((3, 1, 2), (diagnostic["read_rows"], diagnostic["unique_identity_pairs"],
                                   diagnostic["duplicate_extra_rows"]))
        self.assertEqual((2, 0), (diagnostic["same_page_duplicate_rows"], diagnostic["cross_page_duplicate_rows"]))
        self.assertEqual((1, 1), (diagnostic["identical_duplicate_rows"], diagnostic["different_duplicate_rows"]))
        self.assertEqual([True, False], [row["identical_payload"] for row in diagnostic["samples"]])
        self.assertFalse(diagnostic["complete"])
        def all_keys(value):
            if isinstance(value, dict):
                for key, child in value.items():
                    yield key
                    yield from all_keys(child)
            elif isinstance(value, list):
                for child in value:
                    yield from all_keys(child)
        keys = set(all_keys(diagnostic))
        for private in ("messages", "ad_id", "avito_id", "feed_name", "feed_urls", "counts", "rows"):
            self.assertNotIn(private, keys)
        self.assertNotIn("PRIVATE", str(diagnostic))
        self.assertNotIn("987654321", str(diagnostic))

    def test_page_overlap_has_exact_positions_and_bounded_samples(self):
        rows = [item(str(index)) for index in range(50)]
        diagnostic = self.fetch_duplicate([
            page(rows, total=57), page(rows[:7], 2, 57)
        ])
        self.assertEqual((57, 50, 7, 2), (diagnostic["read_rows"], diagnostic["unique_identity_pairs"],
                                       diagnostic["duplicate_extra_rows"], diagnostic["pages"]))
        self.assertEqual((0, 7), (diagnostic["same_page_duplicate_rows"], diagnostic["cross_page_duplicate_rows"]))
        self.assertEqual(5, len(diagnostic["samples"]))
        self.assertEqual({"first_row": 1, "repeated_row": 51, "first_page": 1,
                          "repeated_page": 2, "identical_payload": True}, diagnostic["samples"][0])

    def test_null_identity_still_blocks_and_alias_drift_has_no_duplicate_receipt(self):
        first = item()
        first["avito_id"] = None
        self.assertEqual(1, self.fetch_duplicate([page([first, first])])["duplicate_extra_rows"])
        with patch.object(avito_workspace, "_get", side_effect=[
            upload(), page([first, first]), upload(identifier=124)
        ]), patch.object(avito_workspace, "enforce_rate"):
            with self.assertRaisesMessage(AvitoError, "autoload_report_changed") as caught:
                report_api.fetch_report("token", "123")
        self.assertNotIsInstance(caught.exception, report_api.ReportDuplicateRows)
        self.assertFalse(hasattr(caught.exception, "diagnostics"))

    def test_repeated_identity_twice_on_new_page_has_one_cross_and_one_same_page_repeat(self):
        rows = [item(str(index)) for index in range(50)]
        diagnostic = self.fetch_duplicate([page(rows, total=52), page([rows[0], rows[0]], 2, 52)])
        self.assertEqual((1, 1), (diagnostic["cross_page_duplicate_rows"],
                               diagnostic["same_page_duplicate_rows"]))
        self.assertEqual([51, 52], [sample["repeated_row"] for sample in diagnostic["samples"]])


class ShopXmlIdReportTests(SimpleTestCase):
    def rows(self):
        result = [item(f"1_{index}_3") for index in range(55)]
        for index, row in enumerate(result):
            row["avito_id"] = 1000 + index
        return result

    def xml(self, count=55, suffix=""):
        return ("<Ads>" + "".join(f"<Ad><Id>1_{index}_3</Id></Ad>" for index in range(count))
                + suffix + "</Ads>").encode()

    def shop_upload(self, identifier=123):
        value = upload(identifier)
        value["feed_urls"] = [{"url": "https://shop.aqualine22.ru/avito/avitofeed.xml"}]
        return value

    def fetch(self, responses, payloads=None, kind="last_successful", profiles=None):
        from pool_service import avito_source_audit, avito_profile_source
        proof = avito_profile_source.profile_data({"autoload_enabled": True, "feeds_data": [
            {"feed_url": "https://shop.aqualine22.ru/avito/avitofeed.xml"}]})
        with patch.object(avito_workspace, "_get", side_effect=responses) as get, \
             patch.object(avito_workspace, "enforce_rate"), \
             patch.object(avito_profile_source, "fetch_profile_source", side_effect=profiles or [proof, proof]), \
             patch.object(avito_source_audit, "_read_xml", side_effect=payloads or [self.xml(), self.xml()]) as source:
            result = report_api.fetch_report("token", "123", kind=kind)
        return result, get, source

    def test_scoped_services_opt_out_prevents_nested_shop_reads_even_for_shop_looking_summary(self):
        from pool_service import avito_source_audit, avito_profile_source
        rows = self.rows()
        with patch.object(avito_workspace, "_get", side_effect=[
                self.shop_upload(), page(rows[:50], total=55), page(rows[50:], 2, 55), self.shop_upload()
             ]) as get, patch.object(avito_workspace, "enforce_rate"), \
             patch.object(avito_profile_source, "fetch_profile_source") as profile, \
             patch.object(avito_source_audit, "_read_xml") as source:
            result = report_api.fetch_report("token", "446312402", allow_shop_recovery=False)
        self.assertTrue(result["complete"])
        self.assertEqual("provider_pages", result["read_strategy"])
        self.assertEqual(4, get.call_count)
        profile.assert_not_called()
        source.assert_not_called()

    def test_complete_id_queries_cover_every_row_without_provider_page_overlap(self):
        from urllib.parse import parse_qs, urlsplit
        rows = self.rows()
        result, get, source = self.fetch([
            self.shop_upload(), page(rows[:50], total=55),
            page(rows[:50]), page(rows[50:]), self.shop_upload()
        ])
        self.assertTrue(result["complete"])
        self.assertEqual(55, result["total"])
        self.assertEqual(55, result["counts"]["error"])
        self.assertEqual("shop_xml_id_queries", result["read_strategy"])
        self.assertEqual([row["ad_id"] for row in rows], [row["ad_id"] for row in result["rows"]])
        queries = [parse_qs(urlsplit(call.args[1]).query) for call in get.call_args_list[2:-1]]
        self.assertEqual([50, 5], [len(query["query"][0].split(",")) for query in queries])
        self.assertTrue(all(query["page"] == ["1"] and query["perPage"] == ["50"] for query in queries))
        self.assertEqual(2, source.call_count)

    def test_ignored_filter_missing_or_foreign_ids_and_duplicate_records_block(self):
        rows = self.rows()
        missing = rows[:49]
        foreign = [dict(rows[0], ad_id="1_999_3"), *rows[1:50]]
        duplicate = [rows[0], rows[0], *rows[2:50]]
        duplicate_distinct_listing = [dict(rows[0], avito_id=9999), rows[0], *rows[2:50]]
        variants = [page(rows[:50], total=55), page(missing), page(foreign),
                    page(duplicate), page(duplicate_distinct_listing)]
        for response in variants:
            with self.subTest(response_total=response["meta"]["total"]), self.assertRaises(AvitoError):
                self.fetch([self.shop_upload(), page(rows[:50], total=55), response])

    def test_feed_upload_or_probe_drift_cannot_become_complete(self):
        rows = self.rows()
        responses = [self.shop_upload(), page(rows[:50], total=55),
                     page(rows[:50]), page(rows[50:]), self.shop_upload()]
        with self.assertRaisesMessage(AvitoError, "autoload_report_changed") as caught:
            self.fetch(responses, [self.xml(), self.xml(suffix="<Ad><Id>1_99_3</Id></Ad>")])
        self.assertEqual("shop_xml_snapshot_changed", caught.exception.consistency_guard)
        with self.assertRaisesMessage(AvitoError, "autoload_report_changed") as caught:
            self.fetch([*responses[:-1], self.shop_upload(124)])
        self.assertEqual("summary_snapshot_changed", caught.exception.consistency_guard)
        changed = [dict(rows[0], messages=[]), *rows[1:50]]
        with self.assertRaisesMessage(AvitoError, "autoload_report_changed") as caught:
            self.fetch([self.shop_upload(), page(rows[:50], total=55), page(changed), page(rows[50:])])
        self.assertEqual("queried_rows_changed", caught.exception.consistency_guard)
        self.assertEqual({"messages": 1}, caught.exception.row_differences)

    def test_other_source_current_upload_or_feed_cardinality_mismatch_keep_guarded_pages(self):
        from pool_service import avito_source_audit
        rows = self.rows()
        processing = self.shop_upload()
        processing["status"] = "processing"
        for header, kind, count in ((processing, "last_successful", 55),
                                    (upload(), "last_successful", 55),
                                    (self.shop_upload(), "current", 55),
                                    (self.shop_upload(), "last_successful", 54)):
            with self.subTest(kind=kind, count=count):
                result, get, source = self.fetch([
                    header, page(rows[:50], total=55), page(rows[50:], 2, 55), header
                ], [self.xml(count)], kind)
                self.assertEqual("provider_pages", result["read_strategy"])
                self.assertEqual(4, get.call_count)
                self.assertTrue(all("query=" not in call.args[1] for call in get.call_args_list))
                self.assertEqual(1 if count == 54 else 0, source.call_count)
        result, _, _ = self.fetch([
            self.shop_upload(), page(rows[:50], total=55), page(rows[50:], 2, 55), self.shop_upload()
        ], [AvitoError("source_unavailable")])
        self.assertEqual("provider_pages", result["read_strategy"])
        with self.assertRaisesMessage(AvitoError, "autoload_report_duplicate_rows"):
            self.fetch([self.shop_upload(), page(rows[:50], total=55), page(rows[:5], 2, 55), self.shop_upload()],
                       [self.xml(54)])

    def test_limits_and_malformed_feed_never_weaken_duplicate_guard(self):
        rows = self.rows()
        bad_xml = b"<Ads><Ad><Id>PRIVATE</Id></Ad></Ads>"
        result, _, _ = self.fetch([
            self.shop_upload(), page(rows[:50], total=55), page(rows[50:], 2, 55), self.shop_upload()
        ], [bad_xml])
        self.assertEqual("provider_pages", result["read_strategy"])
        with patch.object(report_api, "MAX_BYTES", 1), self.assertRaisesMessage(AvitoError, "autoload_report_limit"):
            self.fetch([self.shop_upload(), page(rows[:50], total=55), page(rows[:50])])

    def test_processed_copy_uses_current_profile_and_profile_drift_or_denial_stays_closed(self):
        from pool_service import avito_profile_source
        rows = self.rows()
        header = self.shop_upload()
        header["feed_urls"] = [{"url": "https://autoload.avito.ru/processed.xml?token=PRIVATE"}]
        queries = [header, page(rows[:50], total=55), page(rows[:50]), page(rows[50:]), header]
        result, _, _ = self.fetch(queries)
        self.assertEqual("shop_xml_id_queries", result["read_strategy"])
        self.assertEqual("other_or_unknown", result["shop_feed_source"])
        self.assertNotIn("PRIVATE", str(result))
        proof = avito_profile_source.profile_data({"autoload_enabled": True, "feeds_data": [
            {"feed_url": "https://shop.aqualine22.ru/avito/avitofeed.xml"}]})
        with self.assertRaisesMessage(AvitoError, "autoload_report_changed") as caught:
            self.fetch(queries, profiles=[proof, (proof[0], "different-private-fingerprint")])
        self.assertEqual("profile_snapshot_changed", caught.exception.consistency_guard)
        self.assertNotIn("private", str(vars(caught.exception)))
        legacy = [header, page(rows[:50], total=55), page(rows[50:], 2, 55), header]
        variants = [AvitoError("provider_http_403"),
                    avito_profile_source.profile_data({"autoload_enabled": False, "feeds_data": []}),
                    avito_profile_source.profile_data({"autoload_enabled": True, "feeds_data": [
                        {"feed_url": "https://shop.aqualine22.ru/avito/avitofeed.xml"},
                        {"feed_url": "https://other.invalid/private"}]})]
        for profile in variants:
            with self.subTest(profile_type=type(profile).__name__):
                data, _, source = self.fetch(legacy, profiles=[profile])
                self.assertEqual("provider_pages", data["read_strategy"])
                source.assert_not_called()

    def test_late_last_query_does_not_start_second_source_read(self):
        from pool_service import avito_source_audit
        clock = [54.0]
        row = item("1_0_3")
        def late_query(_path):
            clock[0] = 56.0
            return page([row])
        with patch.object(report_api.time, "monotonic", side_effect=lambda: clock[0]), \
             patch.object(avito_source_audit, "_read_xml") as source, \
             self.assertRaisesMessage(AvitoError, "autoload_report_limit"):
            report_api._query_shop_rows(late_query, "/autoload/v4/uploads/last_successful",
                                       (["1_0_3"], "synthetic-digest"), [report_api._item(row)], 1, 55.0)
        source.assert_not_called()


class SharedAvitoReadDeadlineTests(SimpleTestCase):
    def test_expired_source_or_profile_deadline_blocks_before_network(self):
        from pool_service import avito_source_audit, avito_profile_source
        with patch.object(report_api.time, "monotonic", return_value=100.0), \
             patch.object(avito_source_audit.socket, "getaddrinfo") as dns, \
             patch.object(avito_workspace, "_get") as get, \
             patch.object(avito_workspace, "enforce_rate") as rate:
            with self.assertRaisesMessage(AvitoError, "source_limit"):
                avito_source_audit._read_xml(avito_source_audit.FEED_PATH, deadline=99.0)
            with self.assertRaisesMessage(AvitoError, "audit_provider_cooldown"):
                avito_profile_source.fetch_profile_source("token", "123", deadline=99.0)
        dns.assert_not_called()
        get.assert_not_called()
        rate.assert_not_called()

    def test_source_transport_timeout_uses_shared_remaining_budget(self):
        from unittest.mock import MagicMock
        from pool_service import avito_source_audit
        connection = MagicMock()
        response = connection.getresponse.return_value
        response.status = 200
        response.getheader.side_effect = lambda name, default=None: {
            "Content-Encoding": "identity", "Content-Length": "0"}.get(name, default)
        response.isclosed.return_value = True
        with patch.object(report_api.time, "monotonic", return_value=100.0), \
             patch.object(avito_source_audit.socket, "getaddrinfo",
                          return_value=[(2, 1, 6, "", ("93.184.216.34", 443))]), \
             patch.object(avito_source_audit, "_PinnedHTTPS", return_value=connection) as transport:
            self.assertEqual(b"", avito_source_audit._read_xml(avito_source_audit.FEED_PATH, deadline=101.0))
        transport.assert_called_once_with("93.184.216.34", timeout=1.0)
