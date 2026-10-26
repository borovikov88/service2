"""Bounded period snapshots in the existing Avito workspace; never daily facts.

The reader is the same manual, rate-limited own-account statistics endpoint.
A complete period report and a latest request attempt are separate records.
"""
from copy import deepcopy
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
import json
import hashlib
import re
import uuid

from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from . import avito_workspace as api
from .avito_daily_statistics import COUNTERS, MONEY, LABELS
from .avito_services_workplace_setup import _binding
from .communication_avito import AvitoError, access_token
from .communication_models import ChannelConnection
from .communication_services import organization_access, conversation_capability

KEY = "period_reports"
ACCOUNT = "446312402"
SOURCE = "own_ads_stats_v2_period"
UNIT = "workspace_stats_v2_display_rub_v2"
FIELDS = COUNTERS + MONEY
MAX_ROWS, MAX_REPORTS, MAX_BYTES = 200, 6, 512 * 1024
GROUPINGS = {"item", "totals", "day", "week", "month"}
DETAILS = {
    "statistics_fields_unavailable": "В ответе отсутствуют необязательные поля. Данных недостаточно для подтверждения результата; прежний отчёт сохранён.",
    "statistics_total_unavailable": "Авито не передал общее число групп. Полнота нового периода неизвестна; прежний отчёт сохранён.",
    "period_incomplete": "Получена неполная страница или Авито не подтвердил общее число групп. Прежние отчёты сохраняют свои периоды и время.",
    "statistics_provider_error": "Авито явно сообщил об ошибке запроса. Предыдущие успешные отчёты сохранены.",
    "provider_data_invalid": "Типы или структура полученных полей не прошли проверку. Предыдущие отчёты сохранены.",
    "provider_cooldown": "Действует ограничение частоты запросов. Данные не запрашиваются повторно автоматически.",
}
ERRORS = {"period_scope_changed", "period_data_invalid", "period_incomplete", "period_limit",
          "statistics_fields_unavailable", "statistics_total_unavailable", "statistics_provider_error", "provider_data_invalid",
          "provider_account_mismatch", "provider_cooldown", "provider_unavailable",
          "provider_response_too_large", "provider_invalid_json", "provider_invalid_response"}


def _day(value):
    try:
        return date.fromisoformat(value) if type(value) is str and len(value) == 10 else None
    except ValueError:
        return None


def _time(value):
    try:
        parsed = parse_datetime(value) if type(value) is str and len(value) <= 80 else None
        return parsed if parsed is not None and timezone.is_aware(parsed) else None
    except (ValueError, TypeError, OverflowError):
        return None


def _value(value, *, counter=False):
    if type(value) not in (str, int, float, Decimal) or type(value) is bool:
        return None
    try:
        number = Decimal(str(value))
        if (not number.is_finite() or number < 0 or number > Decimal("1000000000000000")
                or (counter and number != number.to_integral())):
            return None
        return format(number, "f")
    except (InvalidOperation, ValueError):
        return None


def _actor(actor, connection):
    current = get_user_model().objects.filter(pk=getattr(actor, "pk", None), is_active=True).first()
    if (current is None or not getattr(actor, "is_authenticated", False)
            or organization_access(current, connection.channel.organization) is None
            or not conversation_capability(current, "can_manage_channels", connection.channel.organization)):
        raise PermissionDenied
    return current


def _current(connection_id, actor, *, locked=False):
    query = ChannelConnection.objects.select_related("channel")
    if locked:
        query = query.select_for_update()
    current = query.filter(pk=connection_id).first()
    if (current is None or current.pk != 4 or str(current.external_id) != ACCOUNT
            or not current.is_active or not current.channel.is_active or current.channel.kind != "avito"):
        raise AvitoError("period_scope_changed")
    _actor(actor, current)
    binding = _binding(current)
    if not binding.get("credential_stamp"):
        raise AvitoError("period_scope_changed")
    return current, binding


def _query(start, end, grouping):
    first, last = _day(start), _day(end)
    if first is None or last is None or first > last or type(grouping) is not str or grouping not in GROUPINGS:
        raise AvitoError("period_data_invalid")
    return {"date_from": start, "date_to": end, "grouping": grouping}


def _key(query):
    return "|".join(query[key] for key in ("date_from", "date_to", "grouping"))


def _rows(rows, *, grouping, compact=False):
    if type(rows) is not list or len(rows) > MAX_ROWS:
        raise AvitoError("period_limit")
    result, seen = [], set()
    for row in rows:
        identifier = row.get("id") if type(row) is dict else None
        text = str(identifier) if type(identifier) in (str, int) else ""
        if (not re.fullmatch(r"[0-9]{1,32}", text) or str(int(text)) != text
                or (grouping == "item" and int(text) <= 0) or text in seen):
            raise AvitoError("period_data_invalid")
        seen.add(text)
        raw = row.get("metrics")
        if compact:
            if type(raw) is not dict or set(raw) != set(FIELDS):
                raise AvitoError("period_data_invalid")
            values = {slug: _value(raw.get(slug), counter=slug in COUNTERS) for slug in FIELDS}
        else:
            if type(raw) is not list:
                raise AvitoError("period_data_invalid")
            indexed = {}
            for metric in raw:
                if type(metric) is not dict or type(metric.get("slug")) is not str:
                    raise AvitoError("period_data_invalid")
                slug = metric["slug"]
                if slug in indexed:
                    raise AvitoError("period_data_invalid")
                indexed[slug] = metric
            values = {}
            for slug in FIELDS:
                metric = indexed.get(slug, {})
                expected = "₽" if slug in MONEY else ""
                values[slug] = (_value(metric.get("value"), counter=slug in COUNTERS)
                                if metric.get("unit") == expected else None)
        result.append({"id": text, "metrics": values})
    return result


def _page(data, query, received_at):
    if type(data) is not dict or any(data.get(k) != v for k, v in query.items()):
        raise AvitoError("period_data_invalid")
    offset, limit, total = data.get("offset"), data.get("limit"), data.get("total")
    if type(offset) is not int or offset < 0 or offset > MAX_ROWS or offset % api.PAGE_SIZE:
        raise AvitoError("period_data_invalid")
    if type(limit) is not int or limit != api.PAGE_SIZE or _time(received_at) is None:
        raise AvitoError("period_data_invalid")
    rows = _rows(data.get("rows"), grouping=query["grouping"])
    if len(rows) > api.PAGE_SIZE:
        raise AvitoError("period_data_invalid")
    if total is None:
        return {"offset": offset, "total": None, "rows": rows, "received_at": received_at}
    if type(total) is not int or total < 0:
        raise AvitoError("period_data_invalid")
    if total > MAX_ROWS:
        raise AvitoError("period_limit")
    if offset + len(rows) > total:
        raise AvitoError("period_data_invalid")
    return {"offset": offset, "total": total, "rows": rows, "received_at": received_at}


def _display_page(data, query):
    """Closed normalized page, not a raw provider or previous request payload."""
    rows = _rows(data["rows"], grouping=query["grouping"])
    offset, total = data["offset"], data.get("total")
    labels = [{"slug": slug, "label": LABELS[slug], "unit": "₽" if slug in MONEY else ""} for slug in FIELDS]
    return {**query, "offset": offset, "limit": api.PAGE_SIZE, "total": total,
            "rows": [{"id": int(row["id"]), "label": ("Объявление "+row["id"]) if query["grouping"] == "item" else "Группа "+row["id"],
                "metrics": [{"slug": slug, "label": LABELS[slug], "value": row["metrics"][slug],
                             "unit": "₽" if slug in MONEY else ""} for slug in FIELDS]} for row in rows],
            "metric_labels": labels, "has_previous": offset > 0,
            "has_next": offset+len(rows) < total if type(total) is int else None,
            "source_timestamp": "", "normalization_version": 2}


def _report(value):
    keys = {"date_from", "date_to", "grouping", "received_at", "rows", "total", "page_offsets",
            "pages_complete", "unit_contract"}
    if type(value) is not dict or set(value) != keys:
        raise AvitoError("period_data_invalid")
    query = _query(value["date_from"], value["date_to"], value["grouping"])
    rows = _rows(value["rows"], grouping=query["grouping"], compact=True)
    total = value["total"]
    if (type(total) is not int or not 0 <= total <= MAX_ROWS or len(rows) != total
            or value["pages_complete"] is not True or value["unit_contract"] != UNIT
            or _time(value["received_at"]) is None
            or type(value["page_offsets"]) is not list
            or any(type(offset) is not int for offset in value["page_offsets"])
            or value["page_offsets"] != list(range(0, max(total, 1), api.PAGE_SIZE))
            or _time(value["received_at"]) > timezone.now()+timedelta(seconds=30)):
        raise AvitoError("period_data_invalid")
    return {**query, "received_at": value["received_at"], "rows": rows, "total": total,
            "pages_complete": True, "unit_contract": UNIT, "page_offsets": list(value["page_offsets"])}


def _safe_attempt(value):
    keys = {"date_from", "date_to", "grouping", "offset", "checked_at", "status", "code"}
    if type(value) is not dict or set(value) != keys:
        return {}
    try:
        query = _query(value["date_from"], value["date_to"], value["grouping"])
    except AvitoError:
        return {}
    code = value["code"]
    if (type(value["offset"]) is not int or not 0 <= value["offset"] <= MAX_ROWS
            or value["offset"] % api.PAGE_SIZE or _time(value["checked_at"]) is None
            or type(value["status"]) is not str or value["status"] not in {"complete", "incomplete", "error", "pending"}
            or type(code) is not str or (code and code not in ERRORS
                and re.fullmatch(r"provider_http_[1-5][0-9]{2}", code) is None)):
        return {}
    return {**query, **{key: value[key] for key in ("offset", "checked_at", "status", "code")}}


def _pending(value):
    result = {}
    if type(value) is not dict:
        return result
    for key, stage in list(value.items())[:1]:
        if type(stage) is not dict or type(stage.get("query")) is not dict:
            continue
        try:
            query = _query(stage["query"].get("date_from"), stage["query"].get("date_to"),
                           stage["query"].get("grouping"))
            if key != _key(query) or _time(stage.get("started_at")) is None or type(stage.get("pages")) is not list:
                continue
            pages = []
            for raw in stage["pages"][:MAX_ROWS // api.PAGE_SIZE]:
                rows = _rows(raw.get("rows"), grouping=query["grouping"], compact=True)
                native = {**query, "offset": raw.get("offset"), "total": raw.get("total"),
                          "limit": api.PAGE_SIZE, "rows": [
                              {"id": row["id"], "metrics": [{"slug": slug, "value": val,
                               "unit": "₽" if slug in MONEY else ""} for slug, val in row["metrics"].items()]}
                              for row in rows]}
                pages.append(_page(native, query, raw.get("received_at")))
            if pages:
                result[key] = {"query": query, "started_at": stage["started_at"], "pages": pages}
        except (AvitoError, KeyError, TypeError, AttributeError):
            continue
    return result


def _book(saved, binding):
    candidate = saved.get(KEY) if type(saved) is dict else None
    if (type(candidate) is not dict or candidate.get("schema_version") != 1
            or candidate.get("binding") != binding
            or len(json.dumps(candidate, ensure_ascii=False).encode()) > MAX_BYTES):
        return {"schema_version": 1, "binding": deepcopy(binding), "reports": [], "pending": {}, "latest_attempt": {}, "owner_attempts": {}}
    try:
        reports = [_report(row) for row in candidate.get("reports", [])[:MAX_REPORTS]]
    except (AvitoError, TypeError):
        reports = []
    # Staging is private and revalidated before completion; latest metadata is closed.
    pending = _pending(candidate.get("pending"))
    owner_attempts = candidate.get("owner_attempts")
    owner_attempts = {key: _safe_attempt(row) for key, row in owner_attempts.items()
                      if type(key) is str and len(key) <= 40 and _safe_attempt(row)} if type(owner_attempts) is dict else {}
    return {"schema_version": 1, "binding": deepcopy(binding), "reports": reports,
            "pending": deepcopy(pending), "latest_attempt": _safe_attempt(candidate.get("latest_attempt")),
            "owner_attempts": owner_attempts}


def _safe_code(error):
    value = error.args[0] if isinstance(error, AvitoError) and len(error.args) == 1 else ""
    return value if type(value) is str and (value in ERRORS or re.fullmatch(r"provider_http_[1-5][0-9]{2}", value)) else "period_data_invalid"


def _attempt(book, query, *, offset, received_at, status, code=""):
    book["latest_attempt"] = {**query, "offset": offset, "checked_at": received_at, "status": status, "code": code}


def _merge(book, data, query, received_at):
    page = _page(data, query, received_at)
    offset = data["offset"]
    key = _key(query)
    if page["total"] is None:
        book["pending"] = {key: {"query": query, "started_at": received_at, "pages": [page]}}
        _attempt(book, query, offset=offset, received_at=received_at, status="incomplete", code="period_incomplete")
        return False
    previous = book["pending"].get(key)
    pages = []
    if offset and type(previous) is dict and previous.get("query") == query:
        first = _time(previous.get("started_at"))
        if first is not None and timedelta(0) <= _time(received_at)-first <= timedelta(minutes=15):
            for raw in previous.get("pages", [])[:MAX_ROWS // api.PAGE_SIZE]:
                if type(raw) is not dict:
                    raise AvitoError("period_data_invalid")
                native = {**query, "offset": raw.get("offset"), "limit": api.PAGE_SIZE,
                          "total": raw.get("total"), "rows": [
                              {"id": row["id"], "metrics": [
                                  {"slug": slug, "value": value, "unit": "₽" if slug in MONEY else ""}
                                  for slug, value in row["metrics"].items()]}
                              for row in _rows(raw.get("rows"), grouping=query["grouping"], compact=True)]}
                pages.append(_page(native, query, raw.get("received_at")))
    pages = [row for row in pages if row is not None and row["total"] is not None and row["offset"] != offset] + [page]
    pages.sort(key=lambda row: row["offset"])
    if any(row["total"] != page["total"] for row in pages):
        pages = [page]
    pending = {"query": query, "started_at": pages[0]["received_at"], "pages": pages}
    book["pending"] = {key: pending}
    rows = [row for entry in pages for row in entry["rows"]]
    offsets = [entry["offset"] for entry in pages]
    expected = list(range(0, max(page["total"], 1), api.PAGE_SIZE))
    complete = offsets == expected and len(rows) == page["total"]
    if complete:
        # Duplicate IDs across pages cannot establish complete coverage.
        rows = _rows(rows, grouping=query["grouping"], compact=True)
        report = {**query, "received_at": received_at, "rows": rows, "total": page["total"],
                  "pages_complete": True, "page_offsets": offsets, "unit_contract": UNIT}
        book["reports"] = ([report] + [r for r in book["reports"] if _key(r) != key])[:MAX_REPORTS]
        book["pending"] = {}
    _attempt(book, query, offset=offset, received_at=received_at,
             status="complete" if complete else "incomplete", code="" if complete else "period_incomplete")
    while len(json.dumps(book, ensure_ascii=False).encode()) > MAX_BYTES and len(book["reports"]) > 1:
        book["reports"].pop()
    if len(json.dumps(book, ensure_ascii=False).encode()) > MAX_BYTES:
        book["pending"] = {}
        raise AvitoError("period_limit")
    return complete


def refresh(connection, actor, *, start, end, grouping="item", offset=0):
    """The Services branch of the existing explicit refresh; one provider page."""
    query = _query(start, end, grouping)
    today = timezone.localdate(timezone=timezone.get_fixed_timezone(7 * 60))
    if not (today-timedelta(days=269) <= _day(start) <= _day(end) <= today):
        raise AvitoError("period_data_invalid")
    if type(offset) is not int or not 0 <= offset <= MAX_ROWS or offset % api.PAGE_SIZE:
        raise AvitoError("period_data_invalid")
    lease, now = uuid.uuid4().hex, timezone.now()
    with transaction.atomic():
        current, binding = _current(getattr(connection, "pk", None), actor, locked=True)
        metadata = dict(current.settings) if type(current.settings) is dict else {}
        until = _time(metadata.get("avito_workspace_refresh_until"))
        if until is not None and until > now:
            return {"status": "busy", "code": "workspace_busy", "detail": "Обновление аккаунта уже выполняется."}
        metadata.update(avito_workspace_refresh_lease=lease,
                        avito_workspace_refresh_until=(now+timedelta(minutes=3)).isoformat())
        current.settings = metadata
        current.save(update_fields=["settings"])
    data, error, received_at = None, "", timezone.now().isoformat()
    try:
        token = access_token(current)
        account = api.profile_data(api._get(token, "/core/v1/accounts/self"))["id"]
        if account != ACCOUNT:
            raise AvitoError("provider_account_mismatch")
        data = api.fetch_statistics(token, account, start=start, end=end, grouping=grouping, offset=offset)
        received_at = timezone.now().isoformat()
    except Exception as exc:
        error = _safe_code(exc)
        received_at = timezone.now().isoformat()
    with transaction.atomic():
        current, final_binding = _current(getattr(connection, "pk", None), actor, locked=True)
        metadata = dict(current.settings) if type(current.settings) is dict else {}
        if metadata.get("avito_workspace_refresh_lease") != lease:
            return {"status": "blocked", "code": "period_scope_changed", "detail": "Результат устаревшего запроса не сохранён."}
        metadata.pop("avito_workspace_refresh_lease", None)
        metadata.pop("avito_workspace_refresh_until", None)
        if final_binding != binding:
            current.settings = metadata
            current.save(update_fields=["settings"])
            return {"status": "blocked", "code": "period_scope_changed", "detail": "Привязка изменилась; результат не сохранён."}
        saved = metadata.get("avito_workspace")
        saved = deepcopy(saved) if type(saved) is dict and saved.get("account_id") == ACCOUNT else {
            "version": 1, "account_id": ACCOUNT, "sections": {}}
        sections = saved.get("sections")
        sections = dict(sections) if type(sections) is dict else {}
        previous = sections.get("statistics")
        previous = deepcopy(previous) if type(previous) is dict else {}
        book = _book(saved, binding)
        complete, page_usable = False, False
        if not error:
            try:
                candidate = deepcopy(book)
                complete = _merge(candidate, data, query, received_at)
                book = candidate
                page_usable = type(data.get("total")) is int
                if not complete and not page_usable:
                    error = "period_incomplete"
            except Exception as exc:
                error = _safe_code(exc)
        if error:
            _attempt(book, query, offset=offset, received_at=received_at, status=("incomplete" if error in {"period_incomplete", "statistics_fields_unavailable", "statistics_total_unavailable"}
                             else "pending" if error == "provider_cooldown" else "error"), code=error)
            sections["statistics"] = {**previous, "status": "warning", "stale": True, "checked_at": received_at,
                "code": error, "detail": DETAILS.get(error, "Новый запрос не подтвердил полный результат. Сохранённый отчёт сохраняет свои прежние даты и время.")}
        else:
            sections["statistics"] = {"status": "ok", "stale": False, "checked_at": received_at,
                "success_at": received_at, "code": "", "detail": "", "data": _display_page(data, query)}
        saved.update(sections=sections)
        saved[KEY] = book
        metadata["avito_workspace"] = saved
        current.settings = metadata
        current.save(update_fields=["settings"])
    return {"status": "ok" if complete else "warning", "code": error or ("" if complete else "period_incomplete"),
        "detail": "Периодный отчёт сохранён." if complete else DETAILS.get(error, "Полнота нового периода не подтверждена; прежние отчёты сохранены.")}


def _aggregate(report):
    values = {}
    for slug in FIELDS:
        cells = [row["metrics"][slug] for row in report["rows"]]
        values[slug] = (sum(Decimal(value) for value in cells)
                        if cells and all(value is not None for value in cells) else None)
    for slug, top, bottom, scale in (("ctr", "views", "impressions", 100),
            ("conversion", "contacts", "views", 100), ("cpl_cash", "spending", "contacts", 1)):
        numerator, denominator = values[top], values[bottom]
        values[slug] = ((numerator/denominator*scale).quantize(Decimal(".01"), rounding=ROUND_HALF_UP)
                        if numerator is not None and denominator is not None and denominator > 0 else None)
    return values


def display(connection, actor):
    if connection is None or connection.pk != 4 or str(connection.external_id) != ACCOUNT:
        return {"state": "not_applicable", "reports": [], "comparisons": []}
    try:
        current, binding = _current(connection.pk, actor)
        saved = current.settings.get("avito_workspace", {}) if type(current.settings) is dict else {}
        book = _book(saved, binding)
        reports = sorted(book["reports"], key=lambda r: (r["date_to"], r["date_from"]), reverse=True)
        output = []
        for report in reports:
            values = _aggregate(report)
            row = {**report, "ids": [entry["id"] for entry in report["rows"]],
                "group_count": len(report["rows"]), "received_at_display": _time(report["received_at"]), "metrics": [
                    {"slug": slug, "label": LABELS.get(slug, {"ctr":"CTR","conversion":"Просмотр → контакт","cpl_cash":"Стоимость обращения, деньги"}.get(slug, slug)),
                     "value": format(value, "f") if value is not None else None,
                     "unit": "₽" if slug in MONEY or slug == "cpl_cash" else "%" if slug in {"ctr", "conversion"} else ""}
                    for slug, value in values.items()], "metric_values": values,
                "confirmed_empty": report["total"] == 0, "provider_timezone": "unknown",
                "daily_history_independent": True, "atomic_snapshot_confirmed": False}
            output.append(row)
        comparisons = []
        for current_report in output:
            previous = next((r for r in output if r["grouping"] == current_report["grouping"] == "item"
                and _day(r["date_to"])+timedelta(days=1) == _day(current_report["date_from"])
                and _day(r["date_to"])-_day(r["date_from"]) == _day(current_report["date_to"])-_day(current_report["date_from"])), None)
            if previous is None:
                continue
            same_ids = set(current_report["ids"]) == set(previous["ids"])
            comparisons.append({"current": current_report, "previous": previous, "same_ids": same_ids,
                "metrics": [{"slug": metric["slug"], "label": metric["label"], "unit": metric["unit"],
                    "current": metric["value"], "previous": next(x["value"] for x in previous["metrics"] if x["slug"] == metric["slug"]),
                    "delta": (format(current_report["metric_values"][metric["slug"]]-previous["metric_values"][metric["slug"]], "f")
                        if same_ids and current_report["metric_values"][metric["slug"]] is not None
                        and previous["metric_values"][metric["slug"]] is not None else None)}
                    for metric in current_report["metrics"]]})
        final, final_binding = _current(current.pk, actor)
        if final_binding != binding:
            raise AvitoError("period_scope_changed")
        return {"state": "available" if output else "empty", "reports": output, "comparisons": comparisons,
            "latest_attempt": {**book["latest_attempt"], "checked_at_display": _time(book["latest_attempt"].get("checked_at"))}, "provider_timezone": "unknown",
            "source": SOURCE, "binding_digest": hashlib.sha256(json.dumps(binding, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest(),
            "binding_verified": True, "daily_history_independent": True}
    except (AvitoError, PermissionDenied, ValueError, TypeError, KeyError):
        return {"state": "unavailable", "reports": [], "comparisons": []}


def setup_evidence():
    """Protected-operator DB read; no amounts, raw payloads, titles or client data."""
    from .avito_services_workplace_setup import _actor as setup_actor, _connection
    try:
        connection = _connection(4)
        result = display(connection, setup_actor(connection))
        return {"state": result["state"], "source": SOURCE, "connection_id": 4, "account_id": ACCOUNT,
            "provider_timezone": "unknown", "daily_history_independent": True,
            "reports": [{"date_from": row["date_from"], "date_to": row["date_to"], "grouping": row["grouping"],
                "received_at": row["received_at"], "pages_complete": True, "group_count": row["group_count"],
                "ids": row["ids"], "known_metrics": [metric["slug"] for metric in row["metrics"] if metric["value"] is not None]}
                for row in result["reports"]],
            "latest_attempt": _safe_attempt({key: value for key, value in result.get("latest_attempt", {}).items() if key != "checked_at_display"})}
    except Exception:
        return {"state": "unavailable", "source": SOURCE, "connection_id": 4, "account_id": ACCOUNT,
                "code": "period_scope_changed", "reports": []}


OWNER_PERIODS = (("2026-10-19", "2026-10-25"), ("2026-10-12", "2026-10-18"))


def prepare_owner_periods_once():
    """Approved reader, at most one rate-limited page per protected invocation.

    A later normal release resumes a cooldown or the next approved period.
    Financial values stay private; no Cron or additional collector is created.
    """
    from .avito_services_workplace_setup import _actor as setup_actor, _connection
    try:
        source = _connection(4)
        actor = setup_actor(source)
        current, binding = _current(source.pk, actor)
        saved = current.settings.get("avito_workspace", {}) if type(current.settings) is dict else {}
        book = _book(saved, binding)
        selected = None
        for start, end in OWNER_PERIODS:
            query = _query(start, end, "item")
            key = _key(query)
            if any(_key(row) == key for row in book["reports"]) or key in book["owner_attempts"]:
                continue
            selected = query
            break
        if selected is None:
            return {"state": "already_checked", "evidence": setup_evidence()}
        key, offset = _key(selected), 0
        pending = book["pending"].get(key)
        if type(pending) is dict and type(pending.get("pages")) is list:
            offsets = [row.get("offset") for row in pending["pages"] if type(row) is dict]
            if (offsets and all(type(value) is int for value in offsets) and 0 in offsets
                    and all(type(row.get("total")) is int for row in pending["pages"])):
                offset = max(offsets)+api.PAGE_SIZE
        result = refresh(current, actor, start=selected["date_from"], end=selected["date_to"],
                         grouping="item", offset=offset)
        # A cooldown/busy reservation is not a failed provider page.
        if result.get("code") not in {"provider_cooldown", "workspace_busy", "period_scope_changed"}:
            with transaction.atomic():
                current, after = _current(source.pk, actor, locked=True)
                if after != binding:
                    raise AvitoError("period_scope_changed")
                metadata = dict(current.settings)
                saved = deepcopy(metadata.get("avito_workspace", {}))
                book = _book(saved, after)
                attempt = _safe_attempt(book["latest_attempt"])
                unknown_total = any(row.get("total") is None for stage in book["pending"].values() for row in stage["pages"])
                if attempt and (result.get("code") != "period_incomplete" or not book["pending"] or unknown_total):
                    book["owner_attempts"][key] = attempt
                    saved[KEY] = book
                    metadata["avito_workspace"] = saved
                    current.settings = metadata
                    current.save(update_fields=["settings"])
        return {"state": "pending" if result.get("code") in {"provider_cooldown", "workspace_busy"} else "read_attempted",
                "date_from": selected["date_from"], "date_to": selected["date_to"],
                "status": result.get("status"), "code": result.get("code"), "evidence": setup_evidence()}
    except Exception:
        return {"state": "unavailable", "code": "period_scope_changed", "evidence": setup_evidence()}
