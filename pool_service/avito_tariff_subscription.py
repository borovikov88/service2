"""Services subscription evidence, separate from the manual tariff calendar.

The user identified an agency-only read endpoint. Its complete wire contract
is still required; this module must not guess pagination or perform that POST.
"""
from dataclasses import dataclass
from datetime import datetime, timedelta
import re

from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from .communication_models import ChannelConnection
from .communication_services import organization_access, conversation_capability

KEY = "avito_tariff_subscription"
SOURCE = "avito_promo_agency_clients_subscription"
METHOD = "POST /api/1/agency/clients"
DOCUMENTATION_URL = "https://developers.avito.ru/api-catalog"
CONTRACT_STATUS = "method_contract_unavailable"
SOURCE_CHECKED_ON = "2026-10-25"
STALE_AFTER = timedelta(hours=48)
ERRORS = {
    "method_contract_unavailable": "Не подтверждён полный формат запроса, ответа и страниц агентского метода. Запрос не выполнялся.",
    "access_unverified": "Доступ этого подключения к агентскому API ещё не подтверждён.",
    "tariff_scope_changed": "Подключение, ключи или права изменились; прежний результат скрыт.",
    "tariff_pages_incomplete": "Получены не все страницы. Статус подписки неизвестен.",
    "tariff_ambiguous": "Получены повторные или противоречивые записи. Подписка требует сверки.",
    "tariff_account_mismatch": "Источник не подтверждает выбранный аккаунт услуг.",
    "tariff_category_mismatch": "Полученная подписка не относится к категории services.",
    "tariff_data_invalid": "Полученные поля подписки не прошли проверку.",
    "provider_credentials_missing": "Ключи подключения отсутствуют; тариф через API не подтверждён.",
    "provider_http_401": "Авито отказал в авторизации метода; срок тарифа неизвестен.",
    "provider_http_403": "Нет подтверждённого доступа к агентскому методу; это не означает окончание тарифа.",
    "provider_http_402": "Авито ограничил доступ к методу; причина и срок тарифа требуют уточнения.",
    "provider_http_429": "Ограничена частота запросов; предыдущий успешный результат сохранён.",
    "tariff_read_failed": "Не удалось проверить подписку. Предыдущий результат сохранён.",
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


def _time(value):
    try:
        result = value if isinstance(value, datetime) else parse_datetime(value) if isinstance(value, str) else None
        return result if result is not None and timezone.is_aware(result) else None
    except (TypeError, ValueError, OverflowError):
        return None


def normalize_services_subscription(rows, *, account_id, account_matches, pages_complete, fetched_at):
    """Normalize already scoped evidence after a future verified wire adapter.

    No raw agency clients or date strings are retained. A timestamp or status
    without verified account/category/page coverage cannot become a success.
    expiredTo's format/time zone is not supplied: its presence is recorded only.
    """
    if account_id != "446312402":
        raise SubscriptionError("tariff_account_mismatch")
    if pages_complete is not True:
        raise SubscriptionError("tariff_pages_incomplete")
    if type(account_matches) is not int or account_matches < 0:
        raise SubscriptionError("tariff_data_invalid")
    if account_matches > 1 or not isinstance(rows, list) or len(rows) > 1:
        raise SubscriptionError("tariff_ambiguous")
    stamp, now = _time(fetched_at), timezone.now()
    if stamp is None or stamp > now or now - stamp > timedelta(minutes=10):
        raise SubscriptionError("tariff_data_invalid")
    if account_matches == 0:
        if rows:
            raise SubscriptionError("tariff_account_mismatch")
        return SubscriptionObservation(account_id, "account_not_returned", None, None, "absent", stamp)
    if not rows or rows == [None]:
        return SubscriptionObservation(account_id, "subscription_missing", None, None, "absent", stamp)
    row = rows[0]
    if not isinstance(row, dict):
        raise SubscriptionError("tariff_data_invalid")
    if row.get("category") != "services":
        raise SubscriptionError("tariff_category_mismatch")
    active = row.get("isActive") if type(row.get("isActive")) is bool else None
    level = row.get("level")
    level = level if isinstance(level, str) and re.fullmatch(r"[a-z][a-z0-9_]{0,31}", level) else None
    expiry = "date_unconfirmed" if row.get("expiredTo") is not None else "absent"
    state = "active" if active is True else "inactive" if active is False else "status_unknown"
    return SubscriptionObservation(account_id, state, active, level, expiry, stamp)


def _scope(connection_id, actor, *, lock=False):
    query = ChannelConnection.objects.select_related("channel", "avito_credential")
    if lock:
        query = query.select_for_update()
    current = query.filter(pk=connection_id).first()
    if (current is None or current.pk != 4 or str(current.external_id) != "446312402"
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


def refresh(connection, actor):
    """Record the exact contract blocker; no token or provider request is made.

    Repeated identical blocked clicks do not manufacture fresh API evidence or
    continually replace the attempt timestamp. No cron is added/enabled.
    """
    with transaction.atomic():
        current, binding = _scope(connection.pk, actor, lock=True)
        value = _saved(current)
        if value.get("binding") != binding:
            value = {"schema_version": 1, "binding": binding}
        code = CONTRACT_STATUS
        if value.get("last_attempt_code") != code:
            value.update(last_attempt_at=timezone.now().isoformat(), last_attempt_code=code,
                request_performed=False)
            _write(current, value)
    return {"code": code, "detail": ERRORS[code], "request_performed": False}



def _valid_data(data):
    if not isinstance(data, dict) or set(data) != {"state", "is_active", "level", "expiry_state"}:
        return False
    state, active, level = data["state"], data["is_active"], data["level"]
    if (not isinstance(state, str) or not isinstance(data["expiry_state"], str)
            or data["expiry_state"] not in {"absent", "date_unconfirmed"}):
        return False
    if level is not None and (not isinstance(level, str) or not re.fullmatch(r"[a-z][a-z0-9_]{0,31}", level)):
        return False
    if state == "active":
        return active is True
    if state == "inactive":
        return active is False
    if state == "status_unknown":
        return active is None
    return (state in {"account_not_returned", "subscription_missing"} and active is None
        and level is None and data["expiry_state"] == "absent")


def record_observation(connection, actor, observation, source_binding):
    """Reserved for the verified agency reader; never a client-supplied POST."""
    if CONTRACT_STATUS != "verified":
        raise SubscriptionError("method_contract_unavailable")
    if not isinstance(observation, SubscriptionObservation):
        raise SubscriptionError("tariff_data_invalid")
    with transaction.atomic():
        current, binding = _scope(connection.pk, actor, lock=True)
        if binding != source_binding or observation.account_id != str(current.external_id):
            raise SubscriptionError("tariff_scope_changed")
        if not binding.get("credential_stamp"):
            raise SubscriptionError("provider_credentials_missing")
        # Revalidate freshness and the closed normalized state before saving.
        stamp = _time(observation.fetched_at)
        data = {"state": observation.state, "is_active": observation.is_active,
            "level": observation.level, "expiry_state": observation.expiry_state}
        if (stamp is None or stamp > timezone.now() or timezone.now() - stamp > timedelta(minutes=10)
                or not _valid_data(data)):
            raise SubscriptionError("tariff_data_invalid")
        value = _saved(current) if _saved(current).get("binding") == binding else {}
        value.update(schema_version=1, binding=binding, source=SOURCE, category="services",
            last_success_at=stamp.isoformat(), last_attempt_at=stamp.isoformat(),
            last_attempt_code="", request_performed=True,
            data=data)
        _write(current, value)
    return value


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
    code = code if isinstance(code, str) and (code in ERRORS or code == "") else "tariff_read_failed"
    stamp = _time(value.get("last_success_at")) if bound and value.get("source") == SOURCE and value.get("category") == "services" else None
    data = value.get("data") if stamp is not None and isinstance(value.get("data"), dict) else {}
    states = {"active": "Подписка активна по явному ответу API",
        "inactive": "Подписка неактивна по явному ответу API; причина окончания не установлена",
        "status_unknown": "API не подтвердил активность подписки",
        "account_not_returned": "Аккаунт не найден в полном ответе агентского API; статус подписки неизвестен",
        "subscription_missing": "Подписка услуг не возвращена; статус неизвестен"}
    state = data.get("state")
    if stamp is not None and (not _valid_data(data) or stamp > timezone.now()):
        code = "tariff_data_invalid"
        stamp, data, state = None, {}, None
    elif state not in states:
        stamp, data, state = None, {}, None
    # Dates remain independent. No comparison is possible before expiredTo's
    # format/time zone is verified; manual reminders keep their original source.
    from .avito_tariff_reminder import display_state as manual_state
    manual = manual_state(current, actor)
    stale = bool(stamp and timezone.now() - stamp > STALE_AFTER)
    attempt_state = ("method_contract_unavailable" if code == "method_contract_unavailable"
        else "unavailable" if code in {"provider_http_401", "provider_http_402", "provider_http_403"}
        else "access_unverified" if code == "access_unverified" else "error" if code else "")
    return {"allowed": True, "code": code or "", "detail": ERRORS.get(code, ""),
        "contract_status": CONTRACT_STATUS, "method": METHOD, "source": SOURCE,
        "source_checked_on": SOURCE_CHECKED_ON, "documentation_url": DOCUMENTATION_URL,
        "state": attempt_state or ("stale" if stale else state or "access_unverified"),
        "observed_state": state,
        "state_label": states.get(state, "Фактический тариф через API не подтверждён"),
        "is_active": data.get("is_active"), "level": data.get("level"),
        "expiry_state": data.get("expiry_state", "absent"),
        "last_success_at": stamp, "last_attempt_at": _time(value.get("last_attempt_at")) if bound else None,
        "stale": stale,
        "manual_date_present": bool(manual.get("expires_on")), "dates_comparable": False,
        "support_draft": support_question(code or "access_unverified")}


def support_question(code):
    safe = code if isinstance(code, str) and code in ERRORS else "tariff_read_failed"
    return ("Подскажите, доступно ли чтение подписки категории services для аккаунта 446312402 "
        "через сохранённое подключение client_credentials обычного аккаунта? "
        "Для POST /api/1/agency/clients с extra.subscription=true нужен доступ Авито Promo агентства. "
        "Просим подтвердить применимость метода, обязательные поля requestBody, схему всех страниц ответа, "
        "тип isActive и формат/часовой пояс expiredTo; либо указать официальный read-only метод "
        "для собственного аккаунта без создания агентства, приглашений, переводов и платных действий. "
        "Безопасный код текущей диагностики Service2: " + safe + ". Это черновик, обращение не отправлено.")


def setup_evidence(actor):
    """Read-only protected setup hook. Does not refresh or make a provider POST."""
    current, _binding = _scope(4, actor)
    value = display_state(current, actor)
    return {"state": "blocked" if CONTRACT_STATUS != "verified" else "ready",
        "code": value["code"] or CONTRACT_STATUS, "connection_id": 4,
        "account_id": "446312402", "contract_status": CONTRACT_STATUS,
        "source": SOURCE, "source_checked_on": SOURCE_CHECKED_ON,
        "last_success_at": value["last_success_at"].isoformat() if value["last_success_at"] else None,
        "request_performed": False, "manual_date_changed": False}
