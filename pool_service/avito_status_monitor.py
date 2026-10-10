"""Scheduled, read-only listing status checks. Only complete scans can commit."""
import re
import time
import uuid
from datetime import timedelta
from urllib.parse import urlencode

from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.urls import reverse
from django.utils import timezone

from pool_service import avito_workspace as api
from pool_service.communication_avito import AvitoError, access_token
from pool_service.communication_models import (
    AvitoListingStatus, AvitoStatusMonitor, ChannelConnection, CommunicationChannel,
)
from pool_service.communication_services import conversation_capability
from pool_service.models import OrganizationAccess
from pool_service.avito_scheduler import panel_command, scheduler_state
from pool_service.services.notifications import notify_users

MAX_PAGES = 100
SCAN_SECONDS = 420
LEASE_SECONDS = 600
INTERVAL = timedelta(hours=1)
BAD_STATUSES = frozenset({"removed", "old", "blocked", "rejected"})
ERRORS = {
    "monitor_scan_limit": "Полный обход не завершён в пределах лимита; прежние статусы сохранены.",
    "monitor_pagination_invalid": "Неполная или противоречивая пагинация Авито; прежние статусы сохранены.",
    "monitor_status_invalid": "Авито вернул неизвестный статус объявления; прежние статусы сохранены.",
    "monitor_owner_unavailable": "Получатель или его доступ изменился. Требуется повторное включение тем же пользователем.",
    "monitor_connection_changed": "Подключение изменилось или отключено. Требуется повторное включение получателем.",
    "monitor_interrupted": "Предыдущая проверка прервалась; выполняется повторная попытка.",
    "monitor_error": "Проверка не завершена; прежние статусы сохранены.",
}


def subscriber_allowed(user, organization):
    return bool(user.is_authenticated and user.is_active and OrganizationAccess.objects.filter(
        user=user, organization=organization, role__in=("owner", "admin"),
    ).exists() and conversation_capability(user, "can_manage_channels", organization))


def configure(connection, user, action):
    """Self-subscribe with existing org admin rights; never accept a recipient field."""
    with transaction.atomic():
        connection = ChannelConnection.objects.select_for_update().select_related("channel").get(pk=connection.pk)
        if connection.channel.kind != CommunicationChannel.KIND_AVITO or not subscriber_allowed(user, connection.channel.organization):
            raise PermissionDenied
        monitor = AvitoStatusMonitor.objects.select_for_update().filter(connection=connection).first()
        if monitor and monitor.recipient_id != user.pk:
            raise PermissionDenied
        needs_activation = (monitor is None or not monitor.enabled
                            or monitor.organization_id != connection.channel.organization_id
                            or monitor.account_id != connection.external_id)
        if action == "enable" and needs_activation and not scheduler_state()["ready"]:
            raise ValueError("Сначала подтвердите успешный запуск серверного расписания в панели хостинга.")
        if monitor is None:
            if action != "enable":
                raise ValueError("Контроль ещё не включён.")
            monitor = AvitoStatusMonitor.objects.create(
                connection=connection, recipient=user,
                organization_id=connection.channel.organization_id, account_id=connection.external_id,
            )
        if monitor.recipient_id != user.pk:
            # Even another owner/admin cannot change or take over this person's
            # subscription, including while it is disabled or needs reactivation.
            raise PermissionDenied
        now = timezone.now()
        if action == "disable":
            monitor.enabled = False
            monitor.lease_token = None
            monitor.lease_until = None
        elif action == "enable":
            if not connection.is_active or not connection.channel.is_active or not api._identifier(connection.external_id):
                raise ValueError("Подключение должно быть активно и иметь корректный ID Авито.")
            if (not monitor.enabled or monitor.recipient_id != user.pk
                    or monitor.organization_id != connection.channel.organization_id
                    or monitor.account_id != connection.external_id):
                monitor.recipient = user
                monitor.organization_id = connection.channel.organization_id
                monitor.account_id = connection.external_id
                monitor.enabled = True
                monitor.baseline_at = None
                monitor.listings.all().delete()
                monitor.generation = uuid.uuid4()
                monitor.failure_count = 0
                monitor.last_error_code = ""
                monitor.next_due_at = now
                monitor.lease_token = None
                monitor.lease_until = None
        elif action == "retry":
            if not monitor.enabled or monitor.recipient_id != user.pk:
                raise PermissionDenied
            # Queue a bounded server retry; never bypass the provider throttle.
            monitor.next_due_at = min(monitor.next_due_at, now)
        else:
            raise ValueError("Неизвестное действие.")
        monitor.save()
        return monitor


def _scope_error(monitor, connection):
    if (not connection.is_active or not connection.channel.is_active
            or connection.channel.kind != CommunicationChannel.KIND_AVITO
            or connection.channel.organization_id != monitor.organization_id
            or connection.external_id != monitor.account_id):
        return "monitor_connection_changed"
    if not subscriber_allowed(monitor.recipient, monitor.organization):
        return "monitor_owner_unavailable"
    return ""


def _safe_error(exc):
    from pool_service.avito_management import SAFE_ERRORS
    code = str(exc)
    return code if code in ERRORS or code in SAFE_ERRORS or re.fullmatch(r"provider_http_[1-5][0-9]{2}", code) else "monitor_error"


def error_detail(code):
    from pool_service.avito_management import _failure
    return ERRORS.get(code) or (_failure("items", AvitoError(code))["detail"] if code else "")


def _page(token, account_id, page, deadline):
    # Shared database budget with manual refreshes/diagnostics: <=20/minute,
    # below the supplied Item contract's 25/minute, including other workers.
    for _ in range(10):
        if time.monotonic() >= deadline:
            raise AvitoError("monitor_scan_limit")
        try:
            api.enforce_rate(account_id, "items", seconds=3)
            break
        except api.AvitoCooldownError as exc:
            wait = max(0.05, (exc.retry_at - timezone.now()).total_seconds() + 0.05)
            if wait > 10 or time.monotonic() + wait >= deadline:
                raise AvitoError("provider_cooldown") from exc
            time.sleep(wait)
    else:
        raise AvitoError("provider_cooldown")
    return api._get(token, "/core/v1/items?" + urlencode({
        "page": page, "per_page": api.PAGE_SIZE, "status": ",".join(api.ITEM_STATUSES),
    }))


def full_scan(connection, *, scan_seconds=SCAN_SECONDS):
    deadline = time.monotonic() + min(scan_seconds, SCAN_SECONDS)
    token = access_token(connection)
    profile = api.profile_data(api._get(token, "/core/v1/accounts/self"))
    account_id = profile["id"]
    if account_id != connection.external_id:
        raise AvitoError("provider_account_mismatch")
    items = {}
    expected_total = None
    for page in range(1, MAX_PAGES + 1):
        response = _page(token, account_id, page, deadline)
        resources = response.get("resources")
        meta = response.get("meta", {})
        if not isinstance(resources, list) or len(resources) > api.PAGE_SIZE or not isinstance(meta, dict):
            raise AvitoError("monitor_pagination_invalid")
        for key, expected in (("page", page), ("per_page", api.PAGE_SIZE)):
            if key in meta and (type(meta[key]) is not int or meta[key] != expected):
                raise AvitoError("monitor_pagination_invalid")
        total = meta.get("total")
        if "total" in meta and (type(total) is not int or total < 0 or total > MAX_PAGES * api.PAGE_SIZE):
            raise AvitoError("monitor_pagination_invalid")
        if page == 1:
            expected_total = total
        elif total != expected_total:
            raise AvitoError("monitor_pagination_invalid")
        parsed = api.items_data(response, page=page, status="")
        for item in parsed["rows"]:
            if item["id"] in items:
                raise AvitoError("monitor_pagination_invalid")
            if item["status"] not in api.ITEM_STATUSES:
                raise AvitoError("monitor_status_invalid")
            items[item["id"]] = {key: item[key] for key in ("id", "status", "title")}
        if time.monotonic() >= deadline:
            raise AvitoError("monitor_scan_limit")
        if expected_total is not None:
            if len(items) > expected_total or (len(resources) < api.PAGE_SIZE and len(items) != expected_total):
                raise AvitoError("monitor_pagination_invalid")
            if len(items) == expected_total:
                return items, page
        elif len(resources) < api.PAGE_SIZE:
            return items, page
    raise AvitoError("monitor_scan_limit")


def _fail(monitor, code, *, disable=False):
    now = timezone.now()
    monitor.last_failure_at = now
    monitor.last_error_code = code
    monitor.failure_count += 1
    monitor.next_due_at = now + timedelta(minutes=min(60, 15 * 2 ** min(monitor.failure_count - 1, 2)))
    monitor.lease_token = None
    monitor.lease_until = None
    if disable:
        monitor.enabled = False
    monitor.save()


def scan_monitor(monitor_id):
    """Claim, read outside locks, then atomically save snapshots + notifications."""
    reference = AvitoStatusMonitor.objects.filter(pk=monitor_id).values("connection_id").first()
    if not reference:
        return "skipped", 0
    with transaction.atomic():
        connection = ChannelConnection.objects.select_for_update().select_related("channel").get(pk=reference["connection_id"])
        monitor = AvitoStatusMonitor.objects.select_for_update().select_related("recipient", "organization").get(pk=monitor_id)
        now = timezone.now()
        if not monitor.enabled or monitor.next_due_at > now or (monitor.lease_until and monitor.lease_until > now):
            return "skipped", 0
        code = _scope_error(monitor, connection)
        if code:
            _fail(monitor, code, disable=True)
            return "failed", 0
        if monitor.lease_token:
            monitor.last_failure_at = now
            monitor.last_error_code = "monitor_interrupted"
        lease = uuid.uuid4()
        monitor.lease_token = lease
        monitor.lease_until = now + timedelta(seconds=LEASE_SECONDS)
        monitor.last_started_at = now
        monitor.save()
    try:
        items, page_count = full_scan(connection)
        code = ""
    except Exception as exc:
        # No remote payloads/tracebacks/credentials enter state or job logs.
        items, page_count, code = {}, 0, _safe_error(exc)
    try:
        with transaction.atomic():
            current = ChannelConnection.objects.select_for_update().select_related("channel").get(pk=connection.pk)
            monitor = AvitoStatusMonitor.objects.select_for_update().select_related("recipient", "organization").get(pk=monitor_id)
            if monitor.lease_token != lease or not monitor.enabled:
                return "skipped", 0
            scope_code = _scope_error(monitor, current)
            if scope_code:
                _fail(monitor, scope_code, disable=True)
                return "failed", 0
            if code or monitor.lease_until <= timezone.now():
                _fail(monitor, code or "monitor_scan_limit")
                return "failed", 0
            now = timezone.now()
            baseline = monitor.baseline_at is None
            saved = {row.item_id: row for row in AvitoListingStatus.objects.filter(monitor=monitor)}
            notifications = 0
            for item_id, item in items.items():
                previous = saved.get(item_id)
                if previous is None:
                    previous = AvitoListingStatus(monitor=monitor, item_id=item_id, status=item["status"])
                elif previous.status != item["status"]:
                    previous.sequence += 1
                    if not baseline and item["status"] in BAD_STATUSES:
                        created = notify_users(
                            [monitor.recipient], organization=monitor.organization,
                            kind="communication", level="warning", send_push=False,
                            title=f"Авито: {api.ITEM_STATUSES[item['status']]}",
                            message=f"{current.name}: {item['title'] or 'Объявление'} (ID {item_id}). "
                                    f"{api.ITEM_STATUSES[previous.status]} → {api.ITEM_STATUSES[item['status']]}. "
                                    "Изменение обнаружено при полной проверке Авито.",
                            action_url=reverse("avito_dashboard") + "?" + urlencode({"account": current.pk}),
                            dedupe_key=f"avito-status:{monitor.pk}:{monitor.generation.hex}:{item_id}:{previous.sequence}",
                        )
                        notifications += len(created)
                previous.status = item["status"]
                previous.last_seen_at = now
                previous.save()
            # Absence, even from a complete scan, is never interpreted as a status.
            monitor.baseline_at = monitor.baseline_at or now
            monitor.last_success_at = now
            monitor.last_error_code = ""
            monitor.failure_count = 0
            monitor.last_item_count = len(items)
            monitor.last_page_count = page_count
            # Advance the scheduled due-time anchor, not completion time. A
            # few seconds of work must not miss the next hourly scheduler tick.
            periods = max(0, (now - monitor.next_due_at) // INTERVAL) + 1
            monitor.next_due_at += periods * INTERVAL
            monitor.lease_token = None
            monitor.lease_until = None
            monitor.save()
        return "baseline" if baseline else "success", notifications
    except Exception:
        # Commit-time failure rolls back snapshots and notifications together.
        # Retain a sanitized failure without releasing somebody else's lease.
        with transaction.atomic():
            monitor = AvitoStatusMonitor.objects.select_for_update().get(pk=monitor_id)
            if monitor.lease_token == lease and monitor.enabled:
                _fail(monitor, "monitor_error")
        return "failed", 0




def display_state(connection, user):
    if connection is None or not subscriber_allowed(user, connection.channel.organization):
        return {}
    monitor = AvitoStatusMonitor.objects.filter(connection=connection).first()
    if monitor and monitor.recipient_id != user.pk:
        return {}
    return {"allowed": True, "monitor": monitor, "scheduler": scheduler_state(),
            "panel_command": panel_command(),
            "error": error_detail(monitor.last_error_code) if monitor else "",
            "running": bool(monitor and monitor.lease_until and monitor.lease_until > timezone.now())}
