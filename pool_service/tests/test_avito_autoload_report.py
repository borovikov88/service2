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
        with patch.object(report_api, "fetch_report", side_effect=AvitoError("autoload_report_changed")):
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
