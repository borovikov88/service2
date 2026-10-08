"""Bounded, read-only Avito account snapshots.

Only allowlisted display fields are retained, never raw provider responses.
Provider writes, remote statistics and undocumented endpoints are not included.
"""
from decimal import Decimal, InvalidOperation
from urllib.parse import urlencode, urlsplit

from pool_service.communication_avito import AvitoError, _json_request, _provider_root

PAGE_SIZE = 50
ITEM_STATUSES = {
    "active": "Активно", "removed": "Снято", "old": "Истекло",
    "blocked": "Заблокировано", "rejected": "Отклонено",
}
SECTIONS = ("profile", "balance", "items", "autoload")


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
        query = {"per_page": PAGE_SIZE, "page": page}
        if status:
            query["status"] = status
        return items_data(_get(token, "/core/v1/items?" + urlencode(query)), page=page, status=status)
    if section == "autoload":
        return autoload_data(_get(token, "/autoload/v4/uploads/last_successful"))
    raise ValueError("Unsupported Avito workspace section")
