"""Verified agency subscription reader; independent from the manual calendar.

Public contract: owner-exported swagger (3).json, Avito Promo, 2026-10-26.
A valid agency clients response proves method access, not subscription activity.
Only the selected services client's normalized evidence is retained.
"""
from dataclasses import dataclass
from datetime import datetime, timedelta
import hashlib
import hmac
import json
import re
import uuid
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from .communication_avito import AvitoError, access_token, authorized_account_id, _provider_root, _json_request
from .communication_models import ChannelConnection
from .communication_services import organization_access, conversation_capability
from .avito_workspace import enforce_rate

KEY = "avito_tariff_subscription"
SOURCE = "avito_promo_agency_clients_subscription"
METHOD = "POST /api/1/agency/clients"
PATH = "/api/1/agency/clients"
DOCUMENTATION_URL = "https://developers.avito.ru/api-catalog"
CONTRACT_STATUS = "verified"
CONTRACT_VERSION = "owner_promo_agency_clients_2026_10_26_v1"
SOURCE_CHECKED_ON = "2026-10-26"
STALE_AFTER = timedelta(hours=48)
PAGE_LIMIT = 100
MAX_PAGES = 3
MAX_CLIENTS = PAGE_LIMIT * MAX_PAGES
PROGRESS_TTL = timedelta(minutes=10)
LEASE_TTL = timedelta(seconds=90)
LEVELS = frozenset({"basic", "extended", "maximal", "ultra"})
ACCOUNT = "446312402"
ZONE = ZoneInfo("Asia/Barnaul")
ERRORS = {
    "method_contract_unavailable": "Контракт метода ещё не подтверждён; запрос не выполнялся.",
    "access_unverified": "Формат метода подтверждён; агентский доступ подключения ещё не проверен.",
    "already_attempted": "Ограниченная проверка уже выполнялась для текущего подключения и контракта.",
    "tariff_scope_changed": "Подключение, ключи или права изменились; прежний результат скрыт.",
    "tariff_pages_incomplete": "Получена часть страниц; подписка пока не подтверждена. Продолжите чтение после паузы.",
    "tariff_reader_limit": "Список превышает безопасный предел чтения; полнота и подписка не подтверждены.",
    "tariff_read_busy": "Предыдущее чтение ещё выполняется. Второй запрос не запущен.",
    "tariff_ambiguous": "Повторные или противоречивые записи; подписка требует сверки.",
    "tariff_account_mismatch": "Ключи или ответ не подтверждают выбранный аккаунт услуг.",
    "tariff_category_mismatch": "Подписка выбранного клиента не относится к services.",
    "tariff_data_invalid": "Поля ответа или подписки не прошли проверку.",
    "provider_credentials_missing": "Ключи подключения отсутствуют; тариф через API не подтверждён.",
    "provider_credentials_invalid": "Не удалось использовать сохранённые ключи подключения.",
    "provider_http_401": "Авито отказал в авторизации; срок и активность тарифа неизвестны.",
    "provider_http_403": "Доступ к агентскому методу не подтверждён; это не означает окончание тарифа.",
    "provider_http_402": "Авито ограничил доступ к методу; срок тарифа неизвестен.",
    "provider_http_429": "Ограничена частота запросов; предыдущий успешный результат сохранён.",
    "provider_cooldown": "Действует пауза отдельного чтения агентских клиентов; предыдущий результат сохранён.",
    "tariff_read_failed": "Не удалось получить подписку; предыдущий успешный результат сохранён.",
}


class SubscriptionError(ValueError):
    pass


@dataclass(frozen=True)
class SubscriptionObservation:
    account_id: str
    state: str
    is_active: bool | None
    level: str | None
    expiry_state: str
    fetched_at: datetime
    expires_at: datetime | None = None


def _time(value):
    try:
        result = value if isinstance(value, datetime) else parse_datetime(value) if isinstance(value, str) else None
        return result if result is not None and timezone.is_aware(result) else None
    except (TypeError, ValueError, OverflowError):
        return None


def _expiry(value):
    # RFC3339 with a known offset or Z, never a naive datetime or inferred zone.
    if (type(value) is not str or len(value) > 64
            or not re.fullmatch(r"\d{4}-\d{2}-\d{2}[Tt]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:[Zz]|[+-]\d{2}:\d{2})", value)
            or value.endswith("-00:00")):
        raise SubscriptionError("tariff_data_invalid")
    canonical = value[:10] + "T" + value[11:]
    if canonical.endswith("z"):
        canonical = canonical[:-1] + "Z"
    result = _time(canonical)
    if result is None:
        raise SubscriptionError("tariff_data_invalid")
    try:
        result.astimezone(ZONE)
    except (ValueError, OverflowError):
        raise SubscriptionError("tariff_data_invalid") from None
    return result


def _client_id(value):
    if type(value) not in (int, str):
        raise SubscriptionError("tariff_data_invalid")
    result = str(value)
    if not re.fullmatch(r"[1-9][0-9]{0,31}", result):
        raise SubscriptionError("tariff_data_invalid")
    return result


def normalize_services_subscription(rows, *, account_id, account_matches, pages_complete, fetched_at):
    """Normalize only the exact target after every required page is verified."""
    if account_id != ACCOUNT:
        raise SubscriptionError("tariff_account_mismatch")
    if pages_complete is not True:
        raise SubscriptionError("tariff_pages_incomplete")
    if type(account_matches) is not int or account_matches < 0:
        raise SubscriptionError("tariff_data_invalid")
    if account_matches > 1 or type(rows) is not list or len(rows) > 1:
        raise SubscriptionError("tariff_ambiguous")
    stamp, now = _time(fetched_at), timezone.now()
    if stamp is None or stamp > now or now - stamp > timedelta(minutes=10):
        raise SubscriptionError("tariff_data_invalid")
    if account_matches == 0:
        if rows:
            raise SubscriptionError("tariff_account_mismatch")
        return SubscriptionObservation(account_id, "account_not_returned", None, None, "absent", stamp)
    if rows == [None]:
        return SubscriptionObservation(account_id, "subscription_missing", None, None, "absent", stamp)
    if len(rows) != 1 or type(rows[0]) is not dict:
        raise SubscriptionError("tariff_data_invalid")
    row = rows[0]
    if row.get("category") != "services":
        raise SubscriptionError("tariff_category_mismatch")
    if type(row.get("isActive")) is not bool or type(row.get("level")) is not str or row["level"] not in LEVELS:
        raise SubscriptionError("tariff_data_invalid")
    expires = _expiry(row["expiredTo"]) if "expiredTo" in row else None
    return SubscriptionObservation(account_id, "active" if row["isActive"] else "inactive",
        row["isActive"], row["level"], "present" if expires is not None else "absent", stamp, expires)


def _scope(connection_id, actor, *, lock=False):
    if not getattr(actor, "is_authenticated", False):
        raise PermissionDenied
    query = ChannelConnection.objects.select_related("channel", "avito_credential")
    if lock:
        query = query.select_for_update()
    current = query.filter(pk=connection_id).first()
    if (current is None or current.pk != 4 or str(current.external_id) != ACCOUNT
            or current.channel.kind != "avito" or not current.is_active or not current.channel.is_active):
        raise PermissionDenied
    user = get_user_model().objects.filter(pk=getattr(actor, "pk", None), is_active=True).first()
    if (user is None or organization_access(user, current.channel.organization) is None
            or not conversation_capability(user, "can_manage_channels", current.channel.organization)):
        raise PermissionDenied
    from .avito_services_workplace_setup import _binding
    return current, _binding(current)


def _saved(connection):
    metadata = connection.settings if isinstance(connection.settings, dict) else {}
    value = metadata.get(KEY)
    return dict(value) if isinstance(value, dict) else {}


def _write(connection, value):
    metadata = dict(connection.settings) if isinstance(connection.settings, dict) else {}
    metadata[KEY] = value
    connection.settings = metadata
    connection.save(update_fields=["settings"])


def _code(exc):
    code = exc.args[0] if isinstance(exc, (AvitoError, SubscriptionError)) and len(exc.args) == 1 else None
    return code if type(code) is str and code in ERRORS else "tariff_read_failed"


def _initial_progress(now):
    return {"started_at": now.isoformat(), "offset": 0, "total": None, "pages": 0,
            "fingerprints": [], "target_matches": 0, "target_subscription": None}


def _progress(value, now):
    raw = value.get("progress")
    if not isinstance(raw, dict):
        return _initial_progress(now)
    stamp = _time(raw.get("started_at"))
    if stamp is None or stamp > now or now-stamp > PROGRESS_TTL:
        return _initial_progress(now)
    keys = {"started_at", "offset", "total", "pages", "fingerprints", "target_matches", "target_subscription"}
    if set(raw) != keys:
        raise SubscriptionError("tariff_data_invalid")
    offset, pages, total, fingerprints, matches = (raw["offset"], raw["pages"], raw["total"],
        raw["fingerprints"], raw["target_matches"])
    if (type(offset) is int and offset == 0 and type(pages) is int and pages == 0
            and total is None and fingerprints == [] and type(matches) is int and matches == 0
            and raw["target_subscription"] is None):
        return dict(raw, fingerprints=[])
    if (type(offset) is not int or type(pages) is not int or not 0 <= pages < MAX_PAGES
            or offset != pages*PAGE_LIMIT or type(total) is not int or not offset < total <= MAX_CLIENTS
            or type(fingerprints) is not list or len(fingerprints) != offset
            or any(type(item) is not str or re.fullmatch(r"[0-9a-f]{64}", item) is None for item in fingerprints)
            or len(set(fingerprints)) != len(fingerprints) or type(matches) is not int or matches not in (0, 1)):
        raise SubscriptionError("tariff_data_invalid")
    target = raw["target_subscription"]
    if matches == 0 and target is not None:
        raise SubscriptionError("tariff_data_invalid")
    if matches == 1:
        if (target is not None and (type(target) is not dict or set(target) not in (
                {"category", "level", "isActive"}, {"category", "level", "isActive", "expiredTo"}))):
            raise SubscriptionError("tariff_data_invalid")
        normalize_services_subscription([target], account_id=ACCOUNT, account_matches=1,
            pages_complete=True, fetched_at=now)
    return dict(raw, fingerprints=list(fingerprints))


def _reserve(connection, actor, *, once=False):
    now = timezone.now()
    with transaction.atomic():
        current, binding = _scope(connection.pk, actor, lock=True)
        value = _saved(current)
        if value.get("binding") != binding:
            value = {"schema_version": 2, "binding": binding}
        if once and value.get("probe_contract") == CONTRACT_VERSION:
            return None, {"code": "already_attempted", "request_performed": False}
        lease_until = _time(value.get("lease_until"))
        if value.get("lease") and lease_until and lease_until > now:
            return None, {"code": "tariff_read_busy", "detail": ERRORS["tariff_read_busy"], "request_performed": False}
        try:
            progress = _progress(value, now)
        except SubscriptionError:
            value.update(last_attempt_at=now.isoformat(), last_attempt_code="tariff_data_invalid",
                last_attempt_phase="reserve", request_performed=False, progress=None, lease=None, lease_until=None)
            if once:
                value.update(probe_contract=CONTRACT_VERSION, probe_at=now.isoformat())
            _write(current, value)
            return None, {"code": "tariff_data_invalid", "detail": ERRORS["tariff_data_invalid"], "request_performed": False}
        lease = uuid.uuid4().hex
        value.update(schema_version=2, binding=binding, lease=lease,
            lease_until=(now+LEASE_TTL).isoformat(), progress=progress,
            pages_received=progress["pages"], target_matches=progress["target_matches"],
            pagination_complete=False)
        if once:
            # Reserved before any token/read request. Never rerun an uncertain
            # or denied probe on every deploy; explicit UI refresh remains possible.
            value.update(probe_contract=CONTRACT_VERSION, probe_at=now.isoformat())
        _write(current, value)
    return (current, binding, lease, progress), None


def _envelope(response, progress):
    if (type(response) is not dict or response.get("status") != "ok"
            or type(response.get("result")) is not dict):
        raise SubscriptionError("tariff_data_invalid")
    result = response["result"]
    clients, total = result.get("clients"), result.get("total")
    if type(clients) is not list or len(clients) > PAGE_LIMIT or type(total) is not int or total < 0:
        raise SubscriptionError("tariff_data_invalid")
    return clients, total


def _merge_page(clients, total, progress, binding):
    next_progress = dict(progress, total=total, fingerprints=list(progress["fingerprints"]),
                         pages=progress["pages"]+1)
    seen = set(next_progress["fingerprints"])
    key = str(binding["credential_stamp"]).encode()
    for client in clients:
        if type(client) is not dict or not {"clientId", "name", "status", "linkType", "linkTime"} <= set(client):
            raise SubscriptionError("tariff_data_invalid")
        identifier = _client_id(client["clientId"])
        # Proof only: no other client's identifier, name or record is retained.
        message = f"{SOURCE}:{binding['organization_id']}:{binding['channel_id']}:{ACCOUNT}:{identifier}".encode()
        fingerprint = hmac.new(key, message, hashlib.sha256).hexdigest()
        if fingerprint in seen:
            raise SubscriptionError("tariff_ambiguous")
        seen.add(fingerprint)
        next_progress["fingerprints"].append(fingerprint)
        if identifier == ACCOUNT:
            next_progress["target_matches"] += 1
            if next_progress["target_matches"] > 1:
                raise SubscriptionError("tariff_ambiguous")
            if "subscription" in client and type(client["subscription"]) is not dict:
                raise SubscriptionError("tariff_data_invalid")
            subscription = client.get("subscription")
            observed = normalize_services_subscription([subscription], account_id=ACCOUNT,
                account_matches=1, pages_complete=True, fetched_at=timezone.now())
            next_progress["target_subscription"] = ({"category": "services", "level": observed.level,
                "isActive": observed.is_active, **({"expiredTo": observed.expires_at.isoformat()}
                if observed.expires_at is not None else {})} if subscription is not None else None)
    next_progress["offset"] += len(clients)
    return next_progress


def _valid_data(data):
    if type(data) is not dict or set(data) not in (
            {"state", "is_active", "level", "expiry_state"},
            {"state", "is_active", "level", "expiry_state", "expires_at"}):
        return False
    state, active, level, expiry = data["state"], data["is_active"], data["level"], data["expiry_state"]
    if type(state) is not str or type(expiry) is not str or expiry not in {"absent", "present", "date_unconfirmed"}:
        return False
    if level is not None and (type(level) is not str or level not in LEVELS):
        return False
    expiry_value = data.get("expires_at")
    try:
        if expiry == "present":
            if _expiry(expiry_value) is None:
                return False
        elif expiry_value is not None:
            return False
    except SubscriptionError:
        return False
    if state == "active":
        return active is True and level in LEVELS
    if state == "inactive":
        return active is False and level in LEVELS
    return (state in {"account_not_returned", "subscription_missing"} and active is None
        and level is None and expiry == "absent")


def _lease_matches(value, lease, now):
    until = _time(value.get("lease_until"))
    return value.get("lease") == lease and until is not None and until > now


def record_observation(connection, actor, observation, source_binding, *, expected_lease=None, value_extra=None):
    """Store a normalized complete target snapshot after fresh scope checks."""
    if CONTRACT_STATUS != "verified":
        raise SubscriptionError("method_contract_unavailable")
    if (not isinstance(observation, SubscriptionObservation)
            or observation.expiry_state not in {"absent", "present"}):
        raise SubscriptionError("tariff_data_invalid")
    with transaction.atomic():
        current, binding = _scope(connection.pk, actor, lock=True)
        value = _saved(current)
        if (binding != source_binding or observation.account_id != str(current.external_id)
                or expected_lease is not None and not _lease_matches(value, expected_lease, timezone.now())):
            raise SubscriptionError("tariff_scope_changed")
        if not binding.get("credential_stamp"):
            raise SubscriptionError("provider_credentials_missing")
        stamp = _time(observation.fetched_at)
        data = {"state": observation.state, "is_active": observation.is_active,
            "level": observation.level, "expiry_state": observation.expiry_state,
            "expires_at": observation.expires_at.isoformat() if observation.expires_at else None}
        now = timezone.now()
        if stamp is None or stamp > now or now-stamp > timedelta(minutes=10) or not _valid_data(data):
            raise SubscriptionError("tariff_data_invalid")
        value = value if value.get("binding") == binding else {}
        if value_extra is not None:
            if (type(value_extra) is not dict or set(value_extra) != {"pages_received", "pagination_complete", "target_matches"}
                    or type(value_extra["pages_received"]) is not int or not 1 <= value_extra["pages_received"] <= MAX_PAGES
                    or value_extra["pagination_complete"] is not True
                    or type(value_extra["target_matches"]) is not int or value_extra["target_matches"] not in (0, 1)):
                raise SubscriptionError("tariff_data_invalid")
            value.update(value_extra)
        value.update(schema_version=2, binding=binding, source=SOURCE, category="services",
            last_success_at=stamp.isoformat(), last_attempt_at=now.isoformat(), last_attempt_code="",
            last_attempt_phase="agency_clients", request_performed=True, access_state="confirmed",
            access_checked_at=now.isoformat(), access_last_success_at=now.isoformat(),
            lease=None, lease_until=None, progress=None, data=data)
        _write(current, value)
    return value


def _clients_page(token, offset):
    # Reuse the existing strict transport to reject redirects with credentials.
    # This documented POST is read-only; ambiguous read failures remain unknown.
    return _json_request(f"{_provider_root()}{PATH}", method="POST",
        headers={"Authorization": f"Bearer {token}", "Accept": "application/json",
                 "Content-Type": "application/json"},
        data=json.dumps({"limit": PAGE_LIMIT, "offset": offset,
                         "extra": {"subscription": True}}).encode(),
        timeout=8, ambiguous_transport=True)


def _refresh(connection, actor, *, once=False):
    """Read one documented page; HTTP runs outside the reserve/commit transactions."""
    if CONTRACT_STATUS != "verified":
        raise SubscriptionError("method_contract_unavailable")
    reserved, early = _reserve(connection, actor, once=once)
    if early is not None:
        return early
    current, binding, lease, progress = reserved
    phase, performed, confirmed, next_progress, code = "authorization", False, False, None, ""
    try:
        root = urlsplit(_provider_root())
        if (root.scheme != "https" or root.hostname != "api.avito.ru" or root.port not in (None, 443)
                or root.username or root.password or root.query or root.fragment or root.path not in ("", "/")):
            raise SubscriptionError("tariff_read_failed")
        if not binding.get("credential_stamp"):
            raise SubscriptionError("provider_credentials_missing")
        token = access_token(current)
        phase = "account"
        if authorized_account_id(current) != ACCOUNT:
            raise SubscriptionError("tariff_account_mismatch")
        # Separate conservative agency budget; never borrowed from Item statistics.
        enforce_rate(ACCOUNT, "agency_clients_subscription", seconds=60)
        phase, performed = "agency_clients", True
        response = _clients_page(token, progress["offset"])
        clients, total = _envelope(response, progress)
        confirmed = True
        if total > MAX_CLIENTS or progress["pages"] >= MAX_PAGES:
            raise SubscriptionError("tariff_reader_limit")
        if (progress["total"] is not None and total != progress["total"]
                or len(clients) != min(PAGE_LIMIT, max(0, total-progress["offset"]))):
            raise SubscriptionError("tariff_pages_incomplete")
        next_progress = _merge_page(clients, total, progress, binding)
    except Exception as exc:
        code = _code(exc)
    now = timezone.now()
    if not code and next_progress["offset"] == next_progress["total"]:
        try:
            observation = normalize_services_subscription(
                [next_progress["target_subscription"]] if next_progress["target_matches"] else [],
                account_id=ACCOUNT, account_matches=next_progress["target_matches"], pages_complete=True, fetched_at=now)
            saved = record_observation(current, actor, observation, binding, expected_lease=lease,
                value_extra={"pages_received": next_progress["pages"], "pagination_complete": True,
                    "target_matches": next_progress["target_matches"]})
        except SubscriptionError as exc:
            exc.request_performed = performed
            raise
        return {"code": "", "request_performed": True, "state": saved["data"]["state"]}
    if not code:
        code = "tariff_pages_incomplete"
    with transaction.atomic():
        latest, latest_binding = _scope(current.pk, actor, lock=True)
        value = _saved(latest)
        if latest_binding != binding or value.get("binding") != binding or not _lease_matches(value, lease, now):
            error = SubscriptionError("tariff_scope_changed")
            error.request_performed = performed
            raise error
        value.update(last_attempt_at=now.isoformat(), last_attempt_code=code, last_attempt_phase=phase,
            request_performed=performed, lease=None, lease_until=None)
        if confirmed:
            value.update(access_state="confirmed", access_checked_at=now.isoformat(),
                access_last_success_at=now.isoformat())
        elif phase == "agency_clients" and code == "provider_http_403":
            value.update(access_state="denied", access_checked_at=now.isoformat())
        elif code in {"provider_http_401", "provider_credentials_missing", "provider_credentials_invalid"}:
            value.update(access_state="authorization_error", access_checked_at=now.isoformat())
        elif code != "provider_cooldown" and value.get("access_state") != "confirmed":
            value.update(access_state="unverified", access_checked_at=now.isoformat())
        if next_progress is not None:
            value.update(progress=next_progress, pages_received=next_progress["pages"],
                pagination_complete=False, target_matches=next_progress["target_matches"])
        elif code != "provider_cooldown":
            value.update(progress=None, pagination_complete=False)
        _write(latest, value)
    return {"code": code, "detail": ERRORS[code], "request_performed": performed}


def refresh(connection, actor, *, once=False):
    """Stable UI/operation contract; expected read conflicts never become 500.

    PermissionDenied intentionally propagates to the normal authorization boundary.
    """
    try:
        result = _refresh(connection, actor, once=once)
    except SubscriptionError as exc:
        code = _code(exc)
        result = {"code": code, "request_performed": getattr(exc, "request_performed", False) is True}
    if "detail" not in result:
        result["detail"] = (ERRORS.get(result["code"], ERRORS["tariff_read_failed"])
            if result["code"] else "Чтение агентского списка завершено. Статус выбранной подписки показан отдельно.")
    return result


def display_state(connection, actor):
    if connection is None or connection.pk != 4:
        return {}
    try:
        current, binding = _scope(connection.pk, actor)
    except PermissionDenied:
        return {}
    value = _saved(current)
    bound = value.get("binding") == binding
    code = value.get("last_attempt_code", "access_unverified") if bound else "access_unverified"
    code = code if type(code) is str and (code in ERRORS or code == "") else "tariff_read_failed"
    if CONTRACT_STATUS == "verified" and code == "method_contract_unavailable":
        # Preserve the historical record while reflecting the now-known contract.
        code = "access_unverified"
    stamp = _time(value.get("last_success_at")) if bound and value.get("source") == SOURCE and value.get("category") == "services" else None
    data = value.get("data") if stamp is not None and type(value.get("data")) is dict else {}
    states = {"active": "Подписка активна по явному ответу API",
        "inactive": "Подписка неактивна по явному ответу API; причина не установлена",
        "account_not_returned": "Аккаунт не возвращён полным списком; подписка неизвестна",
        "subscription_missing": "Подписка услуг не возвращена; активность неизвестна"}
    state = data.get("state")
    if stamp is not None and (not _valid_data(data) or stamp > timezone.now()):
        code, stamp, data, state = "tariff_data_invalid", None, {}, None
    elif state not in states:
        stamp, data, state = None, {}, None
    from .avito_tariff_reminder import display_state as manual_state
    manual = manual_state(current, actor)
    api_expiry = _time(data.get("expires_at"))
    manual_date = manual.get("expires_on")
    comparable = bool(api_expiry and manual_date and manual.get("confirmed_at_display"))
    conflict = bool(comparable and api_expiry.astimezone(ZONE).date() != manual_date)
    stale = bool(stamp and timezone.now()-stamp > STALE_AFTER)
    access = value.get("access_state") if bound else "unverified"
    access = access if type(access) is str and access in {"confirmed", "denied", "authorization_error", "unverified"} else "unverified"
    attempt_state = ("unavailable" if code in {"provider_http_401", "provider_http_402", "provider_http_403"}
        else "access_unverified" if code == "access_unverified"
        else "partial" if code in {"tariff_pages_incomplete", "tariff_reader_limit"}
        else "error" if code else "")
    return {"allowed": True, "code": code or "", "detail": ERRORS.get(code, ""),
        "contract_status": CONTRACT_STATUS, "contract_version": CONTRACT_VERSION, "method": METHOD, "source": SOURCE,
        "source_checked_on": SOURCE_CHECKED_ON, "documentation_url": DOCUMENTATION_URL,
        "state": attempt_state or ("stale" if stale else state or "access_unverified"),
        "observed_state": state, "observed_state_label": states.get(state, ""),
        "state_label": ERRORS.get(code) if code else states.get(state, "Фактический тариф через API не подтверждён"),
        "is_active": data.get("is_active"), "level": data.get("level"),
        "expiry_state": data.get("expiry_state", "absent"), "expires_at": api_expiry,
        "last_success_at": stamp, "last_attempt_at": _time(value.get("last_attempt_at")) if bound else None,
        "stale": stale, "access_state": access,
        "manual_date_present": bool(manual_date), "manual_date": manual_date,
        "manual_source": manual.get("confirmation_source_label", ""),
        "manual_confirmed_at_display": manual.get("confirmed_at_display", ""),
        "dates_comparable": comparable, "dates_conflict": conflict,
        "pages_received": value.get("pages_received") if bound and type(value.get("pages_received")) is int else 0,
        "pagination_complete": value.get("pagination_complete") is True if bound else False,
        "support_draft": support_question(code or "access_unverified")}


def support_question(code):
    safe = code if type(code) is str and code in ERRORS else "tariff_read_failed"
    return ("Какой официальный read-only метод позволяет собственному аккаунту услуг 446312402 "
        "получить уровень, активность и доступную дату окончания подписки services? "
        "Поддерживается ли существующий client_credentials и какие минимальные права нужны? "
        "Агентский POST /api/1/agency/clients с extra.subscription=true вернул HTTP 403. "
        "Формат его запроса известен; требуется применимый способ для собственного аккаунта "
        "без создания агентства, приглашений и переводов. Код диагностики Service2: "
        + safe + ". Черновик, обращение не отправлено.")


def setup_evidence(actor):
    """DB-only metadata; never calls the reader and never exports level/date/clients."""
    current, _binding = _scope(4, actor)
    shown = display_state(current, actor)
    value = _saved(current)
    bound = value.get("binding") == _binding
    matches = value.get("target_matches") if bound else None
    return {"state": shown["state"], "code": shown["code"], "connection_id": 4,
        "account_id": ACCOUNT, "contract_status": CONTRACT_STATUS,
        "source": SOURCE, "source_checked_on": SOURCE_CHECKED_ON, "access_state": shown["access_state"],
        "last_success_at": shown["last_success_at"].isoformat() if shown["last_success_at"] else None,
        "last_attempt_at": shown["last_attempt_at"].isoformat() if shown["last_attempt_at"] else None,
        "access_last_success_at": (_time(value.get("access_last_success_at")).isoformat()
            if bound and _time(value.get("access_last_success_at")) else None),
        "request_performed": False, "last_attempt_request_performed": value.get("request_performed") is True if bound else False,
        "pages_received": shown["pages_received"], "pagination_complete": shown["pagination_complete"],
        "target_matches": matches if type(matches) is int and matches in (0, 1) else None,
        "api_date_present": bool(shown["expires_at"]), "manual_date_changed": False}


def probe_once():
    """Only the existing protected exact-main operator may call this helper.

    The per-binding contract ledger is claimed before HTTP. Later deployments
    only read evidence, including after denied or indeterminate first probes.
    """
    from .avito_services_workplace_setup import _actor, _connection
    current = _connection(4)
    actor = _actor(current)
    result = refresh(current, actor, once=True)
    if result["code"] == "tariff_scope_changed":
        return {"state": "blocked", "code": "tariff_scope_changed", "connection_id": 4,
            "account_id": ACCOUNT, "contract_status": CONTRACT_STATUS,
            "probe_state": "attempted", "request_performed": result.get("request_performed") is True,
            "manual_date_changed": False}
    receipt = setup_evidence(actor)
    receipt.update(probe_state="already_attempted" if result["code"] == "already_attempted" else "attempted",
                   request_performed=result.get("request_performed") is True)
    return receipt
