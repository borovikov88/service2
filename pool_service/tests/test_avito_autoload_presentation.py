import copy
import csv
import io
from html.parser import HTMLParser
from types import SimpleNamespace
from unittest.mock import patch

from django.template.loader import render_to_string
from django.test import SimpleTestCase

from pool_service import avito_autoload_report as report_api
from pool_service.avito_autoload_presentation import for_display, service_notice


def connection(pk=4, account="446312402", kind="avito"):
    return SimpleNamespace(pk=pk, external_id=account, channel=SimpleNamespace(kind=kind))


def message(code=1218, kind="warning", title="Существующее объявление привязано",
            description="Сопоставлено по AvitoId. Ничего исправлять не нужно."):
    return {"code": code, "type": kind, "title": title, "description": description,
            "updated_at": "2026-10-25T08:00:00Z"}


def row(identifier, messages):
    return report_api._item({"ad_id": str(identifier), "avito_id": 1000 + identifier,
        "avito_status": "active", "section": {"slug": "active", "title": "Опубликовано"},
        "messages": messages})


def report(rows, events=None):
    return {"version": 1, "complete": True, "kind": "last_successful",
        "upload_id": "123", "upload_status": "success_warning", "upload_finished": True,
        "counts": {kind: sum(any(m["type"] == kind for m in r["messages"]) for r in rows)
                   for kind in ("error", "warning", "alarm", "info")},
        "rows": rows, "total": len(rows), "pages": 1, "events": events or []}


def current_fixture():
    return report([row(i, [message(), message(code=3002, kind="info", title="Информация",
        description="Объявление обработано.")]) for i in range(11)])


def render(data, conn=None):
    return render_to_string("pool_service/avito/_autoload_report.html", {
        "selected": {"connection": conn or connection(), "has_credentials": True},
        "workspace": {"autoload_report": {"status": "ok", "data": data}},
        "autoload_auto_state": {"allowed": False},
        "autoload_priority_rows": [], "autoload_other_rows": data["rows"],
    })


class Counters(HTMLParser):
    def __init__(self):
        super().__init__()
        self.classes = {}

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if "data-avito-report-counter" in attrs:
            self.classes[attrs["data-avito-report-counter"]] = attrs.get("class", "")


class ServicesAutoloadPresentationTests(SimpleTestCase):
    def test_eleven_completed_bindings_are_neutral_and_all_raw_messages_remain_available(self):
        data = current_fixture()
        original = copy.deepcopy(data)
        csv_before = report_api.csv_text(data, "2026-10-25T10:00:00Z")
        with patch("pool_service.avito_workspace._get") as get:
            view = for_display(connection(), data)
            html = render(data)
        get.assert_not_called()
        self.assertEqual(data, original)
        self.assertEqual(view["counts"], {"error": 0, "warning": 0, "alarm": 0,
            "info": 11, "service_notice": 11})
        self.assertEqual(len(view["service_rows"]), 11)
        self.assertEqual(view["priority_rows"], [])
        self.assertFalse(view["has_attention"])
        self.assertEqual(html.count("Служебное уведомление о привязке · 1218"), 11)
        self.assertEqual(html.count("Исходный уровень Авито: warning · Код: 1218"), 11)
        self.assertEqual(html.count("Информация · 3002"), 11)
        self.assertIn("Служебные уведомления о привязке · 11 объявлений", html)
        parsed = Counters()
        parsed.feed(html)
        self.assertNotIn("text-warning", parsed.classes["warning"])
        self.assertNotIn("text-warning", parsed.classes["service"])
        self.assertNotIn("Завершена с предупреждениями", html)
        self.assertEqual(csv_before, report_api.csv_text(data, "2026-10-25T10:00:00Z"))
        exported = list(csv.DictReader(io.StringIO(csv_before.lstrip("\ufeff"))))
        warnings = [r for r in exported if r["Код"] == "1218"]
        self.assertEqual(len(warnings), 11)
        self.assertTrue(all(r["Тип"] == "warning" for r in warnings))
        self.assertTrue(all(r["Сообщение"] == "Существующее объявление привязано" for r in warnings))
        self.assertTrue(all(r["Описание"] == original["rows"][0]["messages"][0]["description"] for r in warnings))

    def test_code_alone_unknown_warning_failed_and_conditional_links_are_not_demoted(self):
        variants = [
            message(title="Неизвестное предупреждение", description=""),
            message(title="Не удалось привязать существующее объявление", description="Укажите AvitoId."),
            message(title="Существующее объявление будет привязано", description=""),
            message(title="Объявление связано с объявлением на Авито", description="Но исправьте Description."),
            message(code=9999),
            message(kind="error"),
            message(kind="alarm"),
        ]
        for value in variants:
            with self.subTest(value=value):
                data = report([row(1, [value])])
                view = for_display(connection(), data)
                self.assertTrue(view["has_attention"])
                self.assertEqual(len(view["priority_rows"]), 1)
                self.assertEqual(view["counts"]["service_notice"], 0)
                self.assertFalse(service_notice(data["rows"][0]["messages"][0]))

    def test_mixed_warning_remains_visible_and_benign_message_retains_its_own_label(self):
        data = report([row(1, [message(), message(code=777, title="Неизвестное предупреждение",
            description="Нужна проверка.")])])
        view = for_display(connection(), data)
        self.assertEqual(view["counts"]["warning"], 1)
        self.assertEqual(view["counts"]["service_notice"], 1)
        self.assertEqual(view["service_rows"], [])
        html = render(data)
        self.assertIn('data-avito-report-group="priority"', html)
        self.assertIn("Предупреждение · 777", html)
        self.assertIn("Служебное уведомление о привязке · 1218", html)
        parsed = Counters()
        parsed.feed(html)
        self.assertIn("text-warning", parsed.classes["warning"])

    def test_global_warning_is_not_hidden_by_benign_listing_messages(self):
        data = current_fixture()
        data["events"] = [{"type": "warning", "code": 1218, "description": "Неизвестное событие загрузки"}]
        view = for_display(connection(), data)
        self.assertTrue(view["has_attention"])
        html = render(data)
        self.assertIn("Событие загрузки требует проверки · 1218", html)
        self.assertIn("Неизвестное событие загрузки", html)
        self.assertIn("Завершена с предупреждениями", html)

    def test_source_semantics_survive_safe_redaction_without_exposing_raw_payload(self):
        normalized = row(1, [message(description="Существующее объявление привязано. "
            "Ничего исправлять не нужно. https://private.invalid/a +79991234567")])["messages"][0]
        self.assertTrue(service_notice(normalized))
        data = report([row(2, [message()])])
        data["rows"][0]["messages"] = [normalized]
        html = render(data)
        self.assertNotIn("private.invalid", html)
        self.assertNotIn("79991234567", html)
        self.assertIn("Служебное уведомление о привязке", html)

    def test_missing_source_semantics_and_conflicting_visible_action_remain_conservative(self):
        unknown = message(title="Неизвестное сообщение", description="")
        self.assertFalse(service_notice(unknown))
        value = row(1, [message()])["messages"][0]
        value["title"] = "Не удалось привязать существующее объявление"
        self.assertFalse(service_notice(value))
        invalid = dict(message(title="Неизвестно", description=""), diagnostics={
            "source_semantics": {"category": "existing_listing_link", "binding_evidence": True}})
        self.assertFalse(service_notice(invalid))

    def test_message_html_is_escaped_and_original_safe_text_is_kept_in_details(self):
        value = message(title="<script>alert(1)</script> Существующее объявление привязано",
            description="<b>Сопоставлено по AvitoId.</b> Ничего исправлять не нужно.")
        html = render(report([row(1, [value])]))
        self.assertNotIn("<script>", html)
        self.assertNotIn("<b>Сопоставлено", html)
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", html)
        self.assertIn("&lt;b&gt;Сопоставлено по AvitoId.&lt;/b&gt;", html)

    def test_other_connections_accounts_or_channels_keep_existing_report_presentation(self):
        data = current_fixture()
        original = copy.deepcopy(data)
        for conn in (connection(pk=1), connection(account="other"), connection(kind="email")):
            with self.subTest(conn=conn):
                self.assertFalse(for_display(conn, data)["applicable"])
                html = render(data, conn)
                self.assertIn("Ошибки автозагрузки по объявлениям", html)
                self.assertIn("Завершена с предупреждениями", html)
                self.assertNotIn("data-avito-service-notices", html)
        self.assertEqual(data, original)

    def test_unfinished_upload_is_not_presented_as_completed_by_message_classification(self):
        data = current_fixture()
        data.update(upload_status="processing", upload_finished=False)
        html = render(data)
        self.assertIn("В процессе", html)
        self.assertIn("Загрузка ещё не подтверждена завершённой", html)
        self.assertNotIn("Завершена; служебные уведомления", html)
