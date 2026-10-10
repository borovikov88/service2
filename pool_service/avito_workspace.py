"""Bounded, read-only Avito account snapshots.

Only allowlisted display fields are retained, never raw provider responses.
Provider writes and undocumented endpoints are not included. Read-only Item
contracts follow the user-supplied OpenAPI specification, October 2026.
"""
import hashlib
import json
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

from django.db import transaction
from django.utils import timezone
from urllib.parse import urlencode, urlsplit

from pool_service.communication_avito import AvitoError, _json_request, _json_list_request, _provider_root
from pool_service.communication_models import AvitoApiThrottle

PAGE_SIZE = 50
ITEM_STATUSES = {
    "active": "Активно", "removed": "Снято", "old": "Истекло",
    "blocked": "Заблокировано", "rejected": "Отклонено",
}
BASIC_SECTIONS = ("profile", "balance", "items", "autoload")
SECTIONS = (*BASIC_SECTIONS, "statistics", "item_detail", "prices", "calls", "audit", "source_audit", "autoload_report")


DIAGNOSTIC_KEYS = {"token", "account", "messenger", "webhook", "items", "balance", "autoload"}


def diagnostic_account(connection, *, has_credentials=False):
    """Saved, non-secret diagnostics for a caller that already checked scope."""
    data = connection.settings if isinstance(connection.settings, dict) else {}
    rows = data.get("avito_api_results", [])
    if not isinstance(rows, list):
        rows = []
    return {
        "connection": connection, "has_credentials": has_credentials,
        "checked_at": data.get("avito_api_checked_at") or "",
        "results": [row for row in rows if isinstance(row, dict) and isinstance(row.get("key"), str) and row.get("key") in DIAGNOSTIC_KEYS][:7],
        "actual_id": data.get("avito_api_account_id", ""),
        "webhook_status": data.get("avito_webhook_status", "not_connected"),
        "webhook_received": data.get("avito_webhook_last_received_at", ""),
        "webhook_error": data.get("avito_webhook_error", ""),
        "pull_error": data.get("avito_pull_last_error", ""),
    }


def _text(value, limit=200):
    if not isinstance(value, (str, int, float)) or isinstance(value, bool):
        return ""
    return "".join(c for c in str(value) if c >= " " and c != "\x7f")[:limit]


def _identifier(value):
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        return ""
    value = str(value).strip()
    return value if len(value) <= 32 and value.isascii() and value.isdecimal() else ""


def _number(value):
    if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
        return None
    try:
        result = Decimal(str(value))
        if not result.is_finite() or abs(result) > Decimal("1000000000000000"):
            return None
        return format(result.quantize(Decimal("0.01")), "f")
    except (InvalidOperation, ValueError):
        return None


def _count(value):
    return value if type(value) is int and 0 <= value <= 10**12 else None


def _avito_url(value):
    if not isinstance(value, str) or len(value) > 2048:
        return ""
    try:
        parts = urlsplit(value)
        if (parts.scheme == "https" and parts.hostname in {"www.avito.ru", "avito.ru"}
                and not parts.username and not parts.password and not parts.fragment
                and parts.port in (None, 443)):
            # Query strings may contain tracking or sensitive parameters.
            return f"https://{parts.hostname}{parts.path}"
    except ValueError:
        pass
    return ""


def _get(token, path):
    return _json_request(
        f"{_provider_root()}{path}",
        headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
        timeout=8,
    )


def profile_data(response):
    identifier = _identifier(response.get("id"))
    if not identifier:
        raise AvitoError("provider_account_id_invalid")
    return {
        "id": identifier,
        "name": _text(response.get("name")),
        "profile_url": _avito_url(response.get("profile_url")),
    }


def balance_data(response):
    data = {"real": _number(response.get("real")), "bonus": _number(response.get("bonus")), "currency": "RUB"}
    if data["real"] is None and data["bonus"] is None:
        raise AvitoError("provider_data_invalid")
    return data


def items_data(response, *, page, status):
    resources = response.get("resources")
    if not isinstance(resources, list):
        raise AvitoError("provider_data_invalid")
    rows = []
    for raw in resources[:PAGE_SIZE]:
        if not isinstance(raw, dict) or not _identifier(raw.get("id")):
            raise AvitoError("provider_data_invalid")
        category = raw.get("category")
        category = category.get("name") if isinstance(category, dict) else category
        state = _text(raw.get("status"), 32)
        rows.append({
            "id": _identifier(raw.get("id")), "title": _text(raw.get("title"), 300),
            "status": state, "status_label": ITEM_STATUSES.get(state, state or "Не указан"),
            "price": _number(raw.get("price")), "category": _text(category),
            "address": _text(raw.get("address"), 300), "url": _avito_url(raw.get("url")),
            "updated_at": _text(raw.get("updated_at"), 50),
        })
    meta = response.get("meta")
    total = _count(meta.get("total")) if isinstance(meta, dict) else None
    return {
        "rows": rows, "page": page, "per_page": PAGE_SIZE, "total": total,
        "has_previous": page > 1,
        "has_next": page * PAGE_SIZE < total if total is not None else len(resources) >= PAGE_SIZE,
        "status": status,
    }


def autoload_data(response):
    if "upload_id" in response and isinstance(response.get("stats"), dict) and "sections" in response["stats"]:
        from pool_service.avito_autoload_report import summary_for_workspace
        return summary_for_workspace(response)
    # v4 contracts vary by API entitlement. Accept only known display fields;
    # unfamiliar successful payloads must not masquerade as an empty report.
    raw = response
    for key in ("upload", "result"):
        if isinstance(raw.get(key), dict):
            raw = raw[key]
    identifier = _text(raw.get("id") or raw.get("upload_id"), 80)
    status = _text(raw.get("status"), 80)
    started = _text(raw.get("started_at") or raw.get("created_at"), 50)
    finished = _text(raw.get("finished_at") or raw.get("completed_at"), 50)
    labels = {
        "total": "Всего", "processed": "Обработано", "published": "Опубликовано",
        "success": "Успешно", "errors": "Ошибок", "warnings": "Предупреждений",
        "failed": "Не удалось", "active": "Активных", "removed": "Снято",
    }
    stats = raw.get("statistics", raw.get("stats", {}))
    counters = []
    if isinstance(stats, dict):
        counters = [{"label": label, "value": stats[key]} for key, label in labels.items()
                    if _count(stats.get(key)) is not None]
    if not any((identifier, status, started, finished, counters)):
        raise AvitoError("provider_data_invalid")
    return {"id": identifier, "status": status, "started_at": started,
            "finished_at": finished, "counters": counters}


def fetch_section(section, token, account_id, *, page=1, status="", profile=None):
    if section == "profile":
        return profile_data(profile if profile is not None else _get(token, "/core/v1/accounts/self"))
    if section == "balance":
        return balance_data(_get(token, f"/core/v1/accounts/{account_id}/balance/"))
    if section == "items":
        enforce_rate(account_id, "items", seconds=3)
        query = {"per_page": PAGE_SIZE, "page": page, "status": status or ",".join(ITEM_STATUSES)}
        return items_data(_get(token, "/core/v1/items?" + urlencode(query)), page=page, status=status)
    if section == "autoload":
        return autoload_data(_get(token, "/autoload/v4/uploads/last_successful"))
    raise ValueError("Unsupported Avito workspace section")


class AvitoCooldownError(AvitoError):
    def __init__(self, retry_at):
        super().__init__("provider_cooldown")
        self.retry_at = retry_at


def enforce_rate(account_id, method, *, seconds=60):
    """A real database lock, shared by workers/users/duplicate connections."""
    key = hashlib.sha256(f"avito:{account_id}:{method}".encode()).hexdigest()
    now = timezone.now()
    with transaction.atomic():
        AvitoApiThrottle.objects.get_or_create(key=key, defaults={"next_allowed_at": now})
        budget = AvitoApiThrottle.objects.select_for_update().get(key=key)
        if budget.next_allowed_at > now:
            raise AvitoCooldownError(budget.next_allowed_at)
        budget.next_allowed_at = now + timedelta(seconds=seconds)
        budget.save(update_fields=["next_allowed_at"])


def _post(token, path, payload, *, array=False):
    request = _json_list_request if array else _json_request
    return request(
        f"{_provider_root()}{path}", method="POST",
        headers={"Authorization": f"Bearer {token}", "Accept": "application/json", "Content-Type": "application/json"},
        data=json.dumps(payload).encode(), timeout=8,
    )


# Units here follow the supplied Avito OpenAPI Item contract. Only metrics whose
# descriptions explicitly say "kopeks" are converted. Missing values stay None.
METRICS = (
    ("impressions", "Показы", ""), ("views", "Просмотры", ""),
    ("contacts", "Контакты", ""), ("favorites", "Добавления в избранное", ""),
    ("contactsShowPhone", "Посмотрели телефон", ""),
    ("contactsMessenger", "Написали в чат", ""),
    ("contactsShowPhoneAndMessenger", "Телефон и чат", ""),
    ("contactsSbcDiscount", "Откликнулись на скидку", ""),
    ("viewsToContactsConversion", "Просмотры → контакты", "%"),
    ("impressionsToViewsConversion", "Показы → просмотры", "%"),
    ("averageViewCost", "Средняя цена просмотра", "ед. API; валюта не уточнена"),
    ("averageContactCost", "Средняя цена контакта", "ед. API; валюта не уточнена"),
    ("clickPackages", "Целевые просмотры", ""), ("jobContacts", "Отклики на вакансии", ""),
    ("viewsToOrderedItemsConversion", "Просмотры → заказы", "%"),
    ("orderedItems", "Заказано товаров", ""), ("orderedItemsPrice", "Стоимость заказанных товаров", "kopeks"),
    ("deliveredItems", "Доставлено товаров", ""), ("deliveredItemsPrice", "Стоимость доставленных товаров", "kopeks"),
    ("bookingPlacedCount", "Заявки на бронирование", ""), ("bookingPlacedPrice", "Стоимость заявок", "kopeks"),
    ("bookingApprovedCount", "Подтверждено бронирований", ""), ("bookingApprovedPrice", "Стоимость подтверждённых бронирований", "kopeks"),
    ("bookingAcceptedCount", "Бронирования с заселением", ""), ("bookingAcceptedPrice", "Стоимость заселений", "kopeks"),
    ("allSpending", "Все расходы: деньги и бонусы", "kopeks"),
    ("spending", "Денежные расходы на объявления", "kopeks"),
    ("presenceSpending", "Размещение и целевые действия", "kopeks"),
    ("promoSpending", "Расходы на продвижение", "kopeks"),
    ("restSpending", "Остальные расходы", "kopeks"),
    ("commission", "Комиссия", "kopeks"), ("spendingBonus", "Списано бонусов", "ед. API"),
    ("activeItems", "Активные объявления за период", ""),
    ("newActiveItems", "Новые и опубликованные заново", ""),
    ("oldActiveItems", "Активны с прошлого периода", ""),
)


DERIVED_RATIOS = {
    "viewsToContactsConversion": ("contacts", "views", "Контакты / просмотры × 100"),
    "impressionsToViewsConversion": ("views", "impressions", "Просмотры / показы × 100"),
}
UNVERIFIED_UNITS = {"viewsToOrderedItemsConversion", "averageViewCost", "averageContactCost", "spendingBonus"}


def statistics_for_display(data):
    """Normalize new AND already-saved snapshots without any provider request.

    Avito's live percentage scale differs from its schema's prose. Never label
    the raw value as percent. Only two directly matching count ratios are derived;
    orders are not unique users, so that conversion and ambiguous costs stay unknown.
    """
    if not isinstance(data, dict):
        return {}
    result = dict(data)
    rows = []
    for original in data.get("rows", []) if isinstance(data.get("rows"), list) else []:
        if not isinstance(original, dict) or not isinstance(original.get("metrics"), list):
            continue
        metrics = [dict(x) for x in original["metrics"] if isinstance(x, dict) and isinstance(x.get("slug"), str)]
        values = {x["slug"]: x.get("value") for x in metrics}
        for metric in metrics:
            slug = metric["slug"]
            if slug in DERIVED_RATIOS:
                numerator, denominator, formula = DERIVED_RATIOS[slug]
                top, bottom = _number(values.get(numerator)), _number(values.get(denominator))
                value = None
                if top is not None and bottom is not None and Decimal(top) >= 0 and Decimal(bottom) > 0:
                    value = format((Decimal(top) / Decimal(bottom) * 100).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP), ".2f")
                metric.update(value=value, unit="%", source="calculated", note=f"Расчёт Service2: {formula}")
            elif slug in UNVERIFIED_UNITS:
                metric.update(value=None, unit="", source="unavailable", note="Единица показателя API не подтверждена")
        rows.append({**original, "metrics": metrics})
    result["rows"] = rows
    result["metric_labels"] = [
        {**x, "unit": "" if x.get("slug") in UNVERIFIED_UNITS else x.get("unit", "")}
        for x in data.get("metric_labels", []) if isinstance(x, dict) and isinstance(x.get("slug"), str)
    ] if isinstance(data.get("metric_labels"), list) else []
    result["normalization_version"] = 2
    return result


def statistics_data(response, *, start, end, grouping, offset):
    result = response.get("result")
    groups = result.get("groupings") if isinstance(result, dict) else None
    if not isinstance(groups, list) or len(groups) > PAGE_SIZE:
        raise AvitoError("provider_data_invalid")
    rows = []
    labels = [{"slug": slug, "label": label, "unit": "₽" if unit == "kopeks" else unit} for slug, label, unit in METRICS]
    seen = set()
    if grouping == "totals" and len(groups) > 1:
        raise AvitoError("provider_data_invalid")
    for group in groups:
        if not isinstance(group, dict) or not isinstance(group.get("metrics"), list):
            raise AvitoError("provider_data_invalid")
        group = dict(group)
        if grouping == "totals" and "id" not in group:
            group["id"] = 0
        if type(group.get("id")) is not int:
            raise AvitoError("provider_data_invalid")
        if group["id"] in seen:
            raise AvitoError("provider_data_invalid")
        seen.add(group["id"])
        values = {}
        for item in group["metrics"]:
            if not isinstance(item, dict) or not isinstance(item.get("slug"), str):
                raise AvitoError("provider_data_invalid")
            if item["slug"] in values:
                raise AvitoError("provider_data_invalid")
            values[item["slug"]] = item.get("value")
        metrics = []
        for slug, label, unit in METRICS:
            value = _number(values.get(slug))
            if value is not None:
                number = Decimal(value)
                if unit == "kopeks":
                    value = format(number / 100, ".2f")
                elif unit == "" and number == number.to_integral():
                    value = str(int(number))
            metrics.append({"slug": slug, "label": label, "value": value, "unit": "₽" if unit == "kopeks" else unit})
        label = "Итого за период" if grouping == "totals" else f"Объявление {group['id']}" if grouping == "item" else f"ID группы {group['id']}"
        rows.append({"id": group["id"], "label": label, "metrics": metrics})
    total = _count(result.get("dataTotalCount"))
    return statistics_for_display({"rows": rows, "metric_labels": labels, "date_from": start, "date_to": end,
            "grouping": grouping, "offset": offset, "limit": PAGE_SIZE, "total": total,
            "has_previous": offset > 0,
            "has_next": offset + PAGE_SIZE < total if total is not None else len(rows) == PAGE_SIZE,
            "source_timestamp": _text(result.get("timestamp"), 80)})


def fetch_statistics(token, account_id, *, start, end, grouping, offset=0):
    enforce_rate(account_id, "statistics-v2")
    response = _post(token, f"/stats/v2/accounts/{account_id}/items", {
        "dateFrom": start, "dateTo": end, "metrics": [row[0] for row in METRICS],
        "grouping": grouping, "limit": PAGE_SIZE, "offset": offset,
    })
    return statistics_data(response, start=start, end=end, grouping=grouping, offset=offset)


PROMOTION_LABELS = {
    "highlight": "Выделение объявления", "xl": "XL-объявление",
    "stickerpack_x1": "1 значок на XL-объявлении",
    "stickerpack_x2": "2 значка на XL-объявлении",
    "stickerpack_x3": "3 значка на XL-объявлении",
    **{f"x{multiple}_{days}": f"До {multiple} раз больше просмотров · {days} {'день' if days == 1 else 'дней'}"
       for multiple in (2, 5, 10, 15, 20) for days in (1, 7)},
}


def item_detail_for_display(data):
    if not isinstance(data, dict):
        return {}
    status = _text(data.get("status"), 32)
    services = data.get("vas", [])
    return {**data, "status_label": ITEM_STATUSES.get(status, status or "Не указан"),
            "vas": [{**row, "label": PROMOTION_LABELS.get(_text(row.get("id"), 100), "Услуга Авито")}
                    for row in services if isinstance(row, dict)] if isinstance(services, list) else []}


def prices_for_display(data):
    if not isinstance(data, dict):
        return {}
    services = []
    for row in data.get("services", []) if isinstance(data.get("services"), list) else []:
        if not isinstance(row, dict):
            continue
        price, old = _number(row.get("price")), _number(row.get("price_old"))
        slug = _text(row.get("slug"), 100)
        services.append({**row, "price": price, "price_old": old,
                         "show_old_price": price is not None and old is not None and price != old,
                         "label": PROMOTION_LABELS.get(slug, "Услуга Авито")})
    return {**data, "services": services}


def fetch_item_detail(token, account_id, item_id):
    response = _get(token, f"/core/v1/accounts/{account_id}/items/{item_id}/")
    status = response.get("status")
    if not isinstance(status, str):
        raise AvitoError("provider_data_invalid")
    if status in {"not_found", "another_user"}:
        raise AvitoError("provider_item_unavailable")
    if status not in ITEM_STATUSES:
        raise AvitoError("provider_data_invalid")
    services = response.get("vas") or []
    if not isinstance(services, list) or len(services) > 100:
        raise AvitoError("provider_data_invalid")
    vas = []
    for service in services:
        if not isinstance(service, dict):
            raise AvitoError("provider_data_invalid")
        schedule = service.get("schedule") or []
        vas.append({"id": _text(service.get("vas_id"), 80), "finish_time": _text(service.get("finish_time"), 50),
                    "schedule": [_text(x, 50) for x in schedule[:50]] if isinstance(schedule, list) else []})
    return {"item_id": item_id, "status": status, "start_time": _text(response.get("start_time"), 50),
            "finish_time": _text(response.get("finish_time"), 50), "autoload_item_id": _text(response.get("autoload_item_id"), 100),
            "url": _avito_url(response.get("url")), "vas": vas}


def fetch_prices(token, account_id, item_id):
    response = _post(token, f"/core/v1/accounts/{account_id}/vas/prices", {"itemIds": [int(item_id)]}, array=True)
    if len(response) != 1 or not isinstance(response[0], dict) or _identifier(response[0].get("itemId")) != item_id:
        raise AvitoError("provider_data_invalid")
    raw = response[0]
    services, stickers = raw.get("vas"), raw.get("stickers", [])
    if not isinstance(services, list) or not isinstance(stickers, list) or len(services) > 100 or len(stickers) > 100:
        raise AvitoError("provider_data_invalid")
    if any(not isinstance(x, dict) for x in services + stickers):
        raise AvitoError("provider_data_invalid")
    return {"item_id": item_id,
            "services": [{"slug": _text(x.get("slug"), 100), "price": _number(x.get("price")), "price_old": _number(x.get("priceOld"))} for x in services],
            "stickers": [{"id": _text(x.get("id"), 50), "title": _text(x.get("title")), "description": _text(x.get("description"), 500)} for x in stickers]}


def fetch_calls(token, account_id, *, start, end, item_ids):
    response = _post(token, f"/core/v1/accounts/{account_id}/calls/stats/", {
        "dateFrom": start, "dateTo": end, "itemIds": [int(x) for x in item_ids],
    })
    result = response.get("result")
    items = result.get("items") if isinstance(result, dict) else None
    if not isinstance(items, list) or len(items) > 1000:
        raise AvitoError("provider_calls_response_invalid")
    names = {"calls": "calls", "answered": "answered", "new": "new", "newAnswered": "new_answered"}
    by_item = {}
    allowed_ids = {*item_ids, "0"}
    incomplete = False
    for item in items:
        if not isinstance(item, dict) or not _identifier(item.get("itemId")):
            raise AvitoError("provider_calls_item_invalid")
        item_id = _identifier(item["itemId"])
        if item_id not in allowed_ids:
            raise AvitoError("provider_calls_item_out_of_scope")
        target = by_item.setdefault(item_id, {"item_id": item_id, "date": f"{start} — {end}",
                                             **{name: 0 for name in names.values()}})
        days = item.get("days")
        if days is not None and not isinstance(days, list):
            raise AvitoError("provider_calls_days_invalid")
        # days and every CallsStatsDay property are optional in the supplied
        # schema. Missing data is unknown, never an invented zero or HTTP 500.
        if not days:
            incomplete = True
            target.update({name: None for name in names.values()})
            continue
        for day in days:
            if not isinstance(day, dict):
                raise AvitoError("provider_calls_day_invalid")
            when = day.get("date")
            if when is None:
                incomplete = True
                target.update({name: None for name in names.values()})
                continue
            try:
                when = date.fromisoformat(when).isoformat()
            except (ValueError, TypeError):
                raise AvitoError("provider_calls_date_invalid")
            if not start <= when <= end:
                raise AvitoError("provider_calls_date_out_of_scope")
            for source, field in names.items():
                value = day.get(source)
                if value is None:
                    incomplete = True
                    target[field] = None
                elif _count(value) is None:
                    raise AvitoError("provider_calls_counter_invalid")
                elif target[field] is not None:
                    target[field] += value
    rows = list(by_item.values())
    totals = {}
    for name in names.values():
        totals[name] = sum(row[name] for row in rows) if rows and all(row[name] is not None for row in rows) else None
    return {"date_from": start, "date_to": end, "item_count": len(item_ids), "rows": rows,
            "includes_unattributed": "0" in by_item, "incomplete": incomplete, "totals": totals}
