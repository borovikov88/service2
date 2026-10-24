"""Personal advance-balance alerts using the existing bounded hosting worker."""
import hashlib
import json
import uuid
from datetime import timedelta

from django.contrib.auth.models import User
from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from pool_service import avito_workspace
from pool_service.avito_scheduler import scheduler_state
from pool_service.avito_status_monitor import subscriber_allowed
from pool_service.communication_avito import AvitoError, access_token
from pool_service.communication_models import AvitoCredential, ChannelConnection, CommunicationChannel

KEY = "avito_advance_monitor"
INTERVAL = timedelta(minutes=15)
RETRY = timedelta(minutes=15)
SCAN_RESERVE_SECONDS = 50
BINDING_ERROR = "advance_binding_changed"
THRESHOLD_KOPEKS = 10000


def parse_balance(response):
    if (not isinstance(response, dict) or response.get("error") is not None
            or type(response.get("balance")) is not int
            or abs(response["balance"]) > 10**14):
        raise AvitoError("advance_data_invalid")
    from decimal import Decimal
    kopeks = response["balance"]
    return {"kopeks": kopeks, "amount": format(Decimal(kopeks) / 100, ".2f"),
            "currency": "RUB", "source": "cpa_v3"}


def for_display(value):
    if not isinstance(value, dict) or value.get("source") != "cpa_v3" or value.get("currency") != "RUB":
        return {}
    try:
        result = parse_balance({"balance": value.get("kopeks")})
    except AvitoError:
        return {}
    return result if value.get("amount") == result["amount"] else {}


def fetch(token, account_id):
    from pool_service.communication_avito import _json_request, _provider_root
    if _provider_root() != "https://api.avito.ru" or not avito_workspace._identifier(account_id):
        raise AvitoError("advance_provider_invalid")
    # POST is the provider's read-only balance contract: body {}, no paid action.
    avito_workspace.enforce_rate(account_id, "cpa_balance", seconds=60)
    response = _json_request(
        "https://api.avito.ru/cpa/v3/balanceInfo", method="POST",
        headers={"Authorization": f"Bearer {token}", "Accept": "application/json",
                 "Content-Type": "application/json", "X-Source": "Service2"},
        data=b"{}", timeout=8,
    )
    return parse_balance(response)


def _notify_transition(connection, state, data):
    from django.urls import reverse
    from pool_service.services.notifications import notify_users
    zone = "empty" if data["kopeks"] <= 0 else "low" if data["kopeks"] <= THRESHOLD_KOPEKS else "healthy"
    if state.get("last_zone") == zone:
        return
    if zone != "healthy":
        user = User.objects.get(pk=state["actor_id"])
        notify_users(
            [user], organization=connection.channel.organization, kind="communication",
            level="danger" if zone == "empty" else "warning", send_push=False,
            title="Авито: аванс закончился" if zone == "empty" else "Авито: аванс заканчивается",
            message=f"{connection.name}: остаток аванса {data['amount']} ₽. Пополните аванс в Авито.",
            action_url=reverse("avito_dashboard") + f"?account={connection.pk}#avito-advance",
            dedupe_key=f"avito-advance:{connection.pk}:{state['generation']}:{uuid.uuid4().hex}:{zone}",
        )
    state["last_zone"] = zone


def observe_manual(connection, data, credential_stamp):
    # Caller holds the connection lock after a scoped, identity-checked save.
    state = _state(connection)
    if (state.get("enabled") is True and credential_stamp == state.get("credential_stamp")
            and _allowed(connection, state)):
        _notify_transition(connection, state, data)
        metadata = _metadata(connection)
        metadata[KEY] = state
        connection.settings = metadata
        connection.save(update_fields=["settings"])


def _metadata(connection):
    return dict(connection.settings) if isinstance(connection.settings, dict) else {}


def _state(connection):
    value = _metadata(connection).get(KEY)
    return dict(value) if isinstance(value, dict) else {}


def _date(value):
    try:
        result = parse_datetime(value) if isinstance(value, str) else None
    except ValueError:
        return None
    return result if result and timezone.is_aware(result) else None


def _credential_stamp(connection):
    # updated_at also changes on access-token rotation; bind only the client keys.
    values = AvitoCredential.objects.filter(connection=connection).values_list(
        "pk", "client_id_encrypted", "client_secret_encrypted").first()
    return hashlib.sha256(json.dumps(values).encode()).hexdigest() if values else ""


def _allowed(connection, state):
    actor = User.objects.filter(pk=state.get("actor_id")).first() if type(state.get("actor_id")) is int else None
    return bool(actor and connection.is_active and connection.channel.is_active
        and connection.channel.kind == CommunicationChannel.KIND_AVITO
        and state.get("organization_id") == connection.channel.organization_id
        and state.get("channel_id") == connection.channel_id
        and state.get("account_id") == str(connection.external_id)
        and avito_workspace._identifier(connection.external_id)
        and state.get("credential_stamp") and _credential_stamp(connection) == state["credential_stamp"]
        and subscriber_allowed(actor, connection.channel.organization))


def _scheduler_state():
    # Subscription needs a proven completed worker, not success of unrelated
    # listing/report checks. Crashes, timeouts, skips and stale proof fail closed.
    state = scheduler_state()
    return {**state, "ready": state.get("worker_ready", state["ready"])}


def configure(connection, user, action):
    """Self-subscribe only; never accept another recipient or change their alerts."""
    if action not in ("enable", "disable", "retry"):
        raise ValueError("Неизвестное действие.")
    with transaction.atomic():
        connection = ChannelConnection.objects.select_for_update().select_related("channel").get(pk=connection.pk)
        if connection.channel.kind != CommunicationChannel.KIND_AVITO or not subscriber_allowed(user, connection.channel.organization):
            raise PermissionDenied
        metadata, state, now = _metadata(connection), _state(connection), timezone.now()
        if state.get("actor_id") is not None and state.get("actor_id") != user.pk:
            raise PermissionDenied
        if action == "disable":
            state.update(enabled=False, generation=uuid.uuid4().hex)
        elif action == "retry":
            if state.get("enabled") is not True or not _allowed(connection, state):
                raise ValueError("Сначала включите автоматическую проверку.")
            state["next_due_at"] = now.isoformat()
        else:
            stamp = _credential_stamp(connection)
            if not stamp or not connection.is_active or not connection.channel.is_active or not avito_workspace._identifier(connection.external_id):
                raise ValueError("Нужно активное подключение Авито с корректным ID и сохранёнными ключами.")
            # Re-enabling does not bypass scheduler readiness or change any Avito state.
            if state.get("enabled") is not True or not _allowed(connection, state):
                if not _scheduler_state()["ready"]:
                    raise ValueError("Сначала подтвердите запуск существующего серверного расписания.")
                state = {"enabled": True, "generation": uuid.uuid4().hex, "actor_id": user.pk,
                         "organization_id": connection.channel.organization_id, "channel_id": connection.channel_id,
                         "account_id": str(connection.external_id), "credential_stamp": stamp,
                         "next_due_at": now.isoformat(), "last_error_code": "", "last_error_detail": ""}
        metadata[KEY] = state
        connection.settings = metadata
        connection.save(update_fields=["settings"])
    return state


def display_state(connection, user):
    if not connection or not subscriber_allowed(user, connection.channel.organization):
        return {}
    state = _state(connection)
    if state.get("actor_id") is not None and state.get("actor_id") != user.pk:
        return {}
    return {"allowed": True, "enabled": state.get("enabled") is True,
            "needs_reactivation": state.get("enabled") is True and not _allowed(connection, state),
            "next_due_at": _date(state.get("next_due_at")),
            "last_success_at": _date(state.get("last_success_at")),
            "last_error_detail": state.get("last_error_detail", ""),
            "scheduler": _scheduler_state()}


def _failure(exc):
    # Reuse the existing fixed provider/error mapping; never save exception text.
    from pool_service.avito_management import _workspace_failure
    if str(exc) == BINDING_ERROR:
        return {"status": "warning", "code": BINDING_ERROR,
                "detail": "Права, подключение или ключи изменились. Требуется повторное включение."}
    return _workspace_failure("advance", exc)


def _disable(connection, state):
    state.update(enabled=False, last_error_code=BINDING_ERROR,
                 last_error_detail=_failure(AvitoError(BINDING_ERROR))["detail"])
    metadata = _metadata(connection)
    metadata[KEY] = state
    saved = metadata.get("avito_workspace")
    if isinstance(saved, dict) and saved.get("account_id") == str(connection.external_id):
        sections = saved.get("sections")
        if isinstance(sections, dict) and isinstance(sections.get("advance"), dict):
            sections["advance"] = {**sections["advance"], **_failure(AvitoError(BINDING_ERROR)), "stale": True}
    connection.settings = metadata
    connection.save(update_fields=["settings"])


def scan_connection(connection_id):
    lease, now = uuid.uuid4().hex, timezone.now()
    with transaction.atomic():
        connection = ChannelConnection.objects.select_for_update().select_related("channel").filter(pk=connection_id).first()
        if connection is None:
            return "skipped"
        state = _state(connection)
        due = _date(state.get("next_due_at"))
        if state.get("enabled") is not True or due is None or due > now:
            return "skipped"
        if not _allowed(connection, state):
            _disable(connection, state)
            return "failed"
        metadata = _metadata(connection)
        until = _date(metadata.get("avito_workspace_refresh_until"))
        if until and until > now:
            return "skipped"
        generation = state.get("generation")
        if not isinstance(generation, str) or not generation:
            _disable(connection, state)
            return "failed"
        metadata["avito_workspace_refresh_lease"] = lease
        metadata["avito_workspace_refresh_until"] = (now + timedelta(minutes=3)).isoformat()
        connection.settings = metadata
        connection.save(update_fields=["settings"])

    try:
        token = access_token(connection)
        actual_id = avito_workspace.profile_data(avito_workspace._get(token, "/core/v1/accounts/self"))["id"]
        if actual_id != str(connection.external_id):
            raise AvitoError("provider_account_mismatch")
        data = fetch(token, actual_id)
        final_id = avito_workspace.profile_data(avito_workspace._get(token, "/core/v1/accounts/self"))["id"]
        if final_id != actual_id:
            raise AvitoError("provider_account_mismatch")
        finished = timezone.now()
        update = {"status": "ok", "code": "", "detail": "", "data": data,
                  "checked_at": finished.isoformat(), "success_at": finished.isoformat(), "stale": False}
    except Exception as exc:
        # The provider client uses fixed AvitoError identifiers. Other errors stay generic.
        finished = timezone.now()
        failure = _failure(exc if isinstance(exc, AvitoError) else AvitoError("advance_read_failed"))
        update = {**failure, "checked_at": finished.isoformat(), "stale": True}

    with transaction.atomic():
        current = ChannelConnection.objects.select_for_update().select_related("channel").filter(pk=connection_id).first()
        if current is None:
            return "skipped"
        metadata, state = _metadata(current), _state(current)
        if metadata.get("avito_workspace_refresh_lease") != lease:
            return "skipped"
        until = _date(metadata.get("avito_workspace_refresh_until"))
        if until is None or until <= timezone.now():
            update = {**_failure(AvitoError("provider_unavailable")), "checked_at": finished.isoformat(), "stale": True}
        metadata.pop("avito_workspace_refresh_lease", None)
        metadata.pop("avito_workspace_refresh_until", None)
        current.settings = metadata
        if state.get("enabled") is not True or state.get("generation") != generation:
            current.save(update_fields=["settings"])
            return "skipped"
        if not _allowed(current, state):
            _disable(current, state)
            return "failed"
        saved = metadata.get("avito_workspace")
        saved = dict(saved) if isinstance(saved, dict) and saved.get("account_id") == state["account_id"] else {
            "version": 1, "account_id": state["account_id"], "sections": {}}
        sections = dict(saved["sections"]) if isinstance(saved.get("sections"), dict) else {}
        previous = sections.get("advance")
        previous = previous if isinstance(previous, dict) else {}
        if update["status"] != "ok":
            update = {**previous, **update}
        sections["advance"] = update
        saved["sections"] = sections
        metadata["avito_workspace"] = saved
        state.update(last_checked_at=finished.isoformat(), last_error_code=update["code"],
                     last_error_detail=update["detail"])
        if update["status"] == "ok":
            _notify_transition(current, state, data)
            state["last_success_at"] = finished.isoformat()
            periods = max(0, (finished - due) // INTERVAL) + 1
            state["next_due_at"] = (due + periods * INTERVAL).isoformat()
        else:
            state["next_due_at"] = (finished + RETRY).isoformat()
        metadata[KEY] = state
        current.settings = metadata
        current.save(update_fields=["settings"])
    return "success" if update["status"] == "ok" else "failed"


def scan_due(*, deadline, limit=2, observer=None):
    import time
    counts = {"success": 0, "failed": 0, "skipped": 0}
    ids = list(ChannelConnection.objects.filter(
        channel__kind=CommunicationChannel.KIND_AVITO,
        settings__avito_advance_monitor__enabled=True,
        settings__avito_advance_monitor__next_due_at__lte=timezone.now().isoformat(),
    ).order_by("settings__avito_advance_monitor__next_due_at", "pk").values_list("pk", flat=True)[:limit])
    for connection_id in ids:
        if deadline - time.monotonic() < SCAN_RESERVE_SECONDS:
            break
        failed_with_exception = False
        try:
            outcome = scan_connection(connection_id)
        except Exception:
            failed_with_exception = True
            outcome = "failed"  # fixed aggregates only; expired leases recover on a later tick
        counts[outcome] += 1
        if observer is not None:
            observer(connection_id, "advance", outcome, exception=failed_with_exception)
    return counts
