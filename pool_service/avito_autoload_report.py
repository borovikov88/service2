"""Bounded read-only Avito Autoload v4 reports; aliases are checked twice."""
import csv
import io
import json
import re
import time
from collections import Counter

from django.utils import timezone
from django.utils.dateparse import parse_datetime

from pool_service.communication_avito import AvitoError

KINDS = ("current", "last_successful")
PAGE_SIZE = 50
MAX_ITEMS = 5000
MAX_SECONDS = 55
MAX_BYTES = 2 * 1024 * 1024
STATUSES = {
    "active": "Активно", "old": "Истёк срок", "blocked": "Заблокировано",
    "rejected": "Отклонено", "archived": "В архиве", "removed": "Удалено навсегда",
}
UPLOAD_STATUSES = {"processing", "success", "success_warning", "error", None}
MESSAGE_TYPES = {"error", "warning", "alarm", "info"}
ERRORS = {
    "autoload_report_invalid": "Авито вернул неподдерживаемую структуру отчёта. Полнота данных не подтверждена.",
    "autoload_report_changed": "Загрузка изменилась во время чтения. Согласованный снимок не сохранён; повторите проверку.",
    "autoload_report_limit": "Отчёт не получен целиком в пределах размера, количества или времени. Предыдущий снимок сохранён, если был.",
    "autoload_report_pagination": "Пагинация отчёта неполная или противоречивая. Полнота данных не подтверждена.",
}


def _invalid():
    raise AvitoError("autoload_report_invalid")


def _text(value, limit=200):
    if not isinstance(value, str) or len(value) > 50000:
        _invalid()
    text = " ".join(value.split())
    text = "".join(char for char in text if char >= " " and char != "\x7f")
    # Provider messages can echo quoted JSON credentials, including spaced values.
    # Discard the whole marked text rather than guessing the value's delimiter.
    if re.search(r"(?i)\b(access_token|client_secret|refresh_token|password)\b", text):
        return "[текст с учётными данными скрыт]"
    # Descriptions can echo rejected contact fields or signed URLs.
    text = re.sub(r"https?://\S+", "[ссылка скрыта]", text, flags=re.I)
    text = re.sub(r"[\w.+%-]+@[\w.-]+\.[A-Za-z]{2,}", "[email скрыт]", text)
    text = re.sub(r"\+?[78](?:[\s()-]*[0-9]){10}(?![0-9])", "[телефон скрыт]", text)
    text = re.sub(r"(?i)\bBearer\s+\S+", "Bearer [скрыто]", text)
    text = re.sub(r"(?i)\b(access_token|client_secret|refresh_token|password)\s*[:=]\s*\S+", r"\1=[скрыто]", text)
    return text[:limit]


def _count(value):
    if type(value) is not int or not 0 <= value <= MAX_ITEMS:
        _invalid()
    return value


def _date(value, *, nullable=False):
    if nullable and value is None:
        return ""
    if not isinstance(value, str) or len(value) > 50:
        _invalid()
    try:
        parsed = parse_datetime(value)
    except ValueError:
        _invalid()
    if parsed is None or timezone.is_naive(parsed):
        _invalid()
    return parsed.isoformat()


def _tree(node, depth=0):
    if not isinstance(node, dict) or depth > 2:
        _invalid()
    result = {"slug": _text(node.get("slug"), 100), "title": _text(node.get("title"), 200),
              "count": _count(node.get("count")), "sections": []}
    if depth < 2:
        children = node.get("sections")
        if not isinstance(children, list) or len(children) > 100:
            _invalid()
        result["sections"] = [_tree(child, depth + 1) for child in children]
    return result


def summary_data(raw):
    if not isinstance(raw, dict):
        _invalid()
    identifier = raw.get("upload_id")
    status = raw.get("status")
    if type(identifier) is not int or not 0 < identifier < 10**32 or (status is not None and not isinstance(status, str)) or status not in UPLOAD_STATUSES:
        _invalid()
    events = raw.get("events")
    if not isinstance(events, list) or len(events) > 50 or not isinstance(raw.get("feed_urls"), list):
        _invalid()
    normalized_events = []
    for event in events:
        if not isinstance(event, dict) or type(event.get("code")) is not int:
            _invalid()
        normalized_events.append({"code": event["code"], "type": _text(event.get("type"), 50),
                                  "description": _text(event.get("description"), 3000)})
    return {"id": str(identifier), "status": status or "", "started_at": _date(raw.get("started_at"), nullable=True),
            "stats": _tree(raw.get("stats")), "events": normalized_events}


def summary_for_workspace(raw):
    data = summary_data(raw)
    counters = []
    def visit(node, parents=()):
        label = " / ".join((*parents, node["title"]))
        counters.append({"label": label, "value": node["count"]})
        for child in node["sections"]:
            visit(child, (*parents, node["title"]))
    visit(data["stats"])
    return {key: data[key] for key in ("id", "status", "started_at")} | {"finished_at": "", "counters": counters}


def _item(raw):
    if not isinstance(raw, dict):
        _invalid()
    identifier = raw.get("ad_id")
    if (not isinstance(identifier, str) or not 1 <= len(identifier) <= 100
            or identifier != identifier.strip() or any(char < " " or char == "\x7f" for char in identifier)):
        _invalid()
    avito_id, status = raw.get("avito_id"), raw.get("avito_status")
    if avito_id is not None and (type(avito_id) is not int or not 0 <= avito_id < 10**32):
        _invalid()
    if status is not None and (not isinstance(status, str) or status not in STATUSES):
        _invalid()
    section = raw.get("section")
    if not isinstance(section, dict):
        _invalid()
    messages = raw.get("messages")
    if not isinstance(messages, list) or len(messages) > 50:
        _invalid()
    normalized = []
    for message in messages:
        if (not isinstance(message, dict) or not isinstance(message.get("type"), str)
                or message["type"] not in MESSAGE_TYPES or type(message.get("code")) is not int):
            _invalid()
        description = message.get("description")
        normalized.append({
            "type": message["type"], "code": message["code"], "title": _text(message.get("title"), 200),
            "description": _text(description, 3000), "updated_at": _date(message.get("updated_at")),
            "truncated": message.get("truncated") is True or len(description) > 3000,
        })
    return {"ad_id": identifier, "avito_id": avito_id, "avito_status": status,
            "avito_date_end": _date(raw.get("avito_date_end"), nullable=True) or None,
            "feed_name": _text(raw["feed_name"], 200) if raw.get("feed_name") is not None else "",
            "status_label": STATUSES.get(status, "Не передан"),
            "section": {"slug": _text(section.get("slug"), 100), "title": _text(section.get("title"), 200)},
            "messages": normalized}


def _page(raw, page, expected=None):
    if not isinstance(raw, dict) or not isinstance(raw.get("meta"), dict) or not isinstance(raw.get("items"), list):
        raise AvitoError("autoload_report_pagination")
    meta, rows = raw["meta"], raw["items"]
    if any(type(meta.get(key)) is not int for key in ("page", "pages", "perPage", "total")):
        raise AvitoError("autoload_report_pagination")
    total, pages = meta["total"], meta["pages"]
    if (meta["page"] != page or meta["perPage"] != PAGE_SIZE or not 0 <= total <= MAX_ITEMS
            or (total == 0 and pages not in (0, 1))
            or (total > 0 and pages != (total + PAGE_SIZE - 1) // PAGE_SIZE)
            or (expected is not None and (total, pages) != expected)
            or len(rows) != min(PAGE_SIZE, max(0, total - (page - 1) * PAGE_SIZE))):
        raise AvitoError("autoload_report_pagination")
    return [_item(row) for row in rows], (total, pages)


def fetch_report(token, account_id, *, kind="last_successful"):
    from pool_service import avito_workspace
    if kind not in KINDS:
        _invalid()
    deadline = time.monotonic() + MAX_SECONDS
    def get(path):
        while True:
            if time.monotonic() >= deadline:
                raise AvitoError("autoload_report_limit")
            try:
                avito_workspace.enforce_rate(account_id, "autoload_report", seconds=3)
                break
            except avito_workspace.AvitoCooldownError as exc:
                wait = max(0.05, (exc.retry_at - timezone.now()).total_seconds())
                if time.monotonic() + wait >= deadline:
                    raise AvitoError("autoload_report_limit") from None
                time.sleep(min(wait, 3))
        return avito_workspace._get(token, path)

    base = f"/autoload/v4/uploads/{kind}"
    before = summary_data(get(base))
    rows, expected, page, stored_bytes = [], None, 1, 0
    while True:
        batch, expected = _page(get(f"{base}/items?perPage={PAGE_SIZE}&page={page}"), page, expected)
        stored_bytes += sum(len(json.dumps(row, ensure_ascii=False).encode()) for row in batch)
        if stored_bytes > MAX_BYTES:
            raise AvitoError("autoload_report_limit")
        rows.extend(batch)
        if page >= max(1, expected[1]):
            break
        page += 1
    after = summary_data(get(base))
    if before != after:
        raise AvitoError("autoload_report_changed")
    counts = Counter()
    for row in rows:
        for message_type in MESSAGE_TYPES:
            if any(message["type"] == message_type for message in row["messages"]):
                counts[message_type] += 1
    result = {"version": 1, "complete": True, "kind": kind, "upload_id": before["id"],
              "upload_status": before["status"], "upload_finished": before["status"] in {"success", "success_warning", "error"},
              "started_at": before["started_at"], "stats": before["stats"], "events": before["events"],
              "total": len(rows), "pages": page, "counts": dict(counts), "rows": rows}
    if len(json.dumps(result, ensure_ascii=False).encode()) > MAX_BYTES:
        raise AvitoError("autoload_report_limit")
    return result


def csv_text(report, snapshot_time):
    if (not isinstance(report, dict) or report.get("version") != 1 or report.get("complete") is not True
            or report.get("kind") not in KINDS or not re.fullmatch(r"[0-9]{1,32}", str(report.get("upload_id", "")))
            or not isinstance(report.get("rows"), list) or len(report["rows"]) > MAX_ITEMS):
        _invalid()
    rows = [_item(row) for row in report["rows"]]
    def cell(value):
        text = "" if value is None else str(value)
        return "'" + text if text.lstrip().startswith(("=", "+", "-", "@")) else text
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["XML ID", "ID Авито", "Статус", "Раздел", "Тип", "Код", "Сообщение", "Описание",
                     "Актуальность сообщения", "Текст усечён", "Загрузка", "Режим", "Статус загрузки", "Снимок Service2",
                     "Файл", "Конец оплаченного периода"])
    for row in rows:
        for message in row["messages"] or [{}]:
            values = [row["ad_id"], row["avito_id"], row["status_label"], row["section"]["title"],
                      message.get("type"), message.get("code"), message.get("title"), message.get("description"),
                      message.get("updated_at"), message.get("truncated", False), report["upload_id"], report["kind"],
                      report.get("upload_status", ""), snapshot_time, row["feed_name"], row["avito_date_end"]]
            writer.writerow([cell(value) for value in values])
    events = report.get("events", [])
    if not isinstance(events, list) or len(events) > 50:
        _invalid()
    for event in events:
        if not isinstance(event, dict) or type(event.get("code")) is not int:
            _invalid()
        writer.writerow([cell(value) for value in (
            "", "", "", "Вся загрузка", _text(event.get("type"), 50), event["code"],
            "Событие загрузки", _text(event.get("description"), 3000), "", False,
            report["upload_id"], report["kind"], report.get("upload_status", ""), snapshot_time, "", "",
        )])
    return "\ufeff" + output.getvalue()
