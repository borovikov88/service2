import json
import logging
import os
import shutil
import subprocess
import sys
import threading
from datetime import datetime, timedelta, timezone as datetime_timezone
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urljoin, urlsplit, urlunsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from django.conf import settings
from django.db import transaction
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from pool_service.communication_models import (
    ChannelConnection,
    CommunicationChannel,
    PhoneCall,
    TelephonyConnection,
)
from pool_service.services.employee_identity_sync import (
    EmployeeIdentitySyncError,
    _megafon_api_credentials,
    resolve_call_employee,
)


logger = logging.getLogger(__name__)


MAX_INTERNAL_HISTORY_RESPONSE_BYTES = 2 * 1024 * 1024
MAX_INTERNAL_HISTORY_ROWS = 500
DEFAULT_INTERNAL_LOOKBACK_HOURS = 24
INTERNAL_HISTORY_WINDOW_HOURS = 2
INTERNAL_HISTORY_OVERLAP_MINUTES = 5


class MegafonInternalCallSyncError(Exception):
    pass


def internal_call_sync_due(organization, *, max_age_minutes=10):
    """Return True when a configured MegaFon internal-history cursor is stale."""
    telephony_ids = set(
        TelephonyConnection.objects.filter(
            organization=organization,
            is_active=True,
        ).values_list("external_id", flat=True)
    )
    if not telephony_ids:
        return False

    providers = ChannelConnection.objects.filter(
        channel__organization=organization,
        channel__kind=CommunicationChannel.KIND_MEGAFON,
        channel__is_active=True,
        is_active=True,
        external_id__in=telephony_ids,
    ).only("settings")
    threshold = timezone.now() - timedelta(minutes=max_age_minutes)
    configured = False

    for provider in providers:
        settings_data = provider.settings if isinstance(provider.settings, dict) else {}
        if not (
            str(settings_data.get("megafon_api_base_url") or "").strip()
            and str(settings_data.get("megafon_api_key_encrypted") or "").strip()
        ):
            continue
        configured = True
        raw = str(
            settings_data.get("megafon_internal_history_synced_through") or ""
        ).strip()
        synced_through = parse_datetime(raw) if raw else None
        if synced_through is None:
            return True
        if timezone.is_naive(synced_through):
            synced_through = synced_through.replace(tzinfo=datetime_timezone.utc)
        if synced_through < threshold:
            return True

    return False


def _sync_python_executable(base_dir):
    production_python = os.path.join(
        os.path.dirname(base_dir),
        "venv",
        "bin",
        "python",
    )
    if os.path.isfile(production_python) and os.access(production_python, os.X_OK):
        return production_python
    return sys.executable


def _reap_sync_worker(process):
    try:
        return_code = process.wait()
        if return_code not in (0, None):
            logger.warning(
                "Call recording sync worker exited with status %s",
                return_code,
            )
    except Exception:
        logger.exception("Failed while reaping call recording sync worker")


def start_call_recording_sync_worker(*, limit=100):
    """Start one detached recording/internal-call sync; shell lock deduplicates it."""
    base_dir = str(settings.BASE_DIR)
    worker_script = os.path.join(base_dir, "scripts", "run_call_recording_sync.sh")
    bash = shutil.which("bash")
    if not bash or not os.path.isfile(worker_script):
        logger.error("Call recording sync worker launcher is unavailable")
        return False

    env = os.environ.copy()
    env["SERVICE2_PYTHON"] = _sync_python_executable(base_dir)
    try:
        process = subprocess.Popen(
            [bash, worker_script, str(max(1, min(int(limit), 500)))],
            cwd=base_dir,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
            close_fds=True,
        )
    except (OSError, TypeError, ValueError):
        logger.exception("Failed to start call recording sync worker")
        return False

    threading.Thread(
        target=_reap_sync_worker,
        args=(process,),
        daemon=True,
        name="service2-call-recording-sync-reaper",
    ).start()
    return True


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _api_origin(endpoint):
    try:
        parts = urlsplit(endpoint)
        port = parts.port
    except ValueError as exc:
        raise MegafonInternalCallSyncError(
            "Некорректный адрес АТС МегаФона."
        ) from exc
    hostname = (parts.hostname or "").lower()
    if (
        parts.scheme != "https"
        or not hostname
        or not (hostname == "megapbx.ru" or hostname.endswith(".megapbx.ru"))
        or port not in (None, 443)
        or parts.username
        or parts.password
    ):
        raise MegafonInternalCallSyncError(
            "Адрес АТС МегаФона не прошёл проверку."
        )
    return urlunsplit((parts.scheme, parts.netloc, "", "", ""))


def _read_json(url, api_key):
    request = Request(
        url,
        headers={
            "Accept": "application/json",
            "X-API-KEY": api_key,
            "User-Agent": "Service2-MegaFon-Internal-Calls/1.0",
        },
        method="GET",
    )
    timeout = float(
        getattr(settings, "COMMUNICATION_MEGAFON_API_TIMEOUT_SECONDS", 10)
    )
    try:
        with build_opener(_NoRedirect()).open(request, timeout=timeout) as response:
            raw = response.read(MAX_INTERNAL_HISTORY_RESPONSE_BYTES + 1)
            if response.status != 200:
                raise MegafonInternalCallSyncError(
                    f"МегаФон вернул HTTP {response.status}."
                )
    except HTTPError as exc:
        raise MegafonInternalCallSyncError(
            f"МегаФон вернул HTTP {exc.code} при загрузке внутренних звонков."
        ) from exc
    except (URLError, TimeoutError, OSError) as exc:
        raise MegafonInternalCallSyncError(
            "МегаФон не ответил при загрузке внутренних звонков."
        ) from exc

    if len(raw) > MAX_INTERNAL_HISTORY_RESPONSE_BYTES:
        raise MegafonInternalCallSyncError(
            "Ответ истории внутренних звонков МегаФона слишком большой."
        )
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise MegafonInternalCallSyncError(
            "МегаФон вернул некорректный JSON истории внутренних звонков."
        ) from exc
    return data


def _normalize_provider_key(value):
    value = str(value or "").strip().casefold()
    if not value:
        return ""
    return value


def _user_aliases(item):
    aliases = set()
    for field in ("login", "ext", "telnum", "mobile"):
        raw = item.get(field)
        if isinstance(raw, int) and not isinstance(raw, bool):
            raw = str(raw)
        if not isinstance(raw, str):
            continue
        value = _normalize_provider_key(raw)
        if not value:
            continue
        aliases.add(value)
        if "@" in value:
            aliases.add(value.split("@", 1)[0])
    return aliases


def _load_users(origin, api_key):
    query = urlencode({"limit": 500})
    data = _read_json(f"{origin}/crmapi/v1/users?{query}", api_key)
    if isinstance(data, dict):
        items = data.get("items")
    elif isinstance(data, list):
        items = data
    else:
        items = None
    if not isinstance(items, list):
        raise MegafonInternalCallSyncError(
            "МегаФон вернул неожиданный список сотрудников."
        )
    aliases = {}
    for item in items:
        if not isinstance(item, dict):
            continue
        login = item.get("login")
        name = item.get("name")
        ext = item.get("ext")
        if isinstance(ext, int) and not isinstance(ext, bool):
            ext = str(ext)
        if not isinstance(login, str) or not isinstance(name, str):
            continue
        if not isinstance(ext, str):
            ext = ""
        normalized = {
            "login": login.strip(),
            "name": name.strip(),
            "ext": ext.strip(),
        }
        for alias in _user_aliases(item):
            aliases.setdefault(alias, normalized)
    return aliases


def _provider_user_info(users, provider_value, fallback_name=""):
    raw = str(provider_value or "").strip()
    key = _normalize_provider_key(raw)
    candidates = [key]
    if "@" in key:
        candidates.append(key.split("@", 1)[0])
    for candidate in candidates:
        info = users.get(candidate)
        if info:
            return info
    return {
        "login": raw,
        "name": str(fallback_name or "").strip(),
        "ext": "",
    }


def _parse_started_at(value):
    raw = str(value or "").strip()
    parsed = parse_datetime(raw)
    if parsed is None:
        for value_format in ("%Y-%m-%d %H:%M:%S", "%Y%m%dT%H%M%SZ"):
            try:
                parsed = datetime.strptime(raw, value_format)
                break
            except ValueError:
                continue
    if parsed is None:
        raise MegafonInternalCallSyncError(
            "МегаФон вернул некорректное время внутреннего звонка."
        )
    if timezone.is_naive(parsed):
        parsed = parsed.replace(tzinfo=datetime_timezone.utc)
    return parsed.astimezone(datetime_timezone.utc)


def _bounded_int(value, *, maximum):
    try:
        result = int(value or 0)
    except (TypeError, ValueError) as exc:
        raise MegafonInternalCallSyncError(
            "МегаФон вернул некорректную длительность внутреннего звонка."
        ) from exc
    if result < 0 or result > maximum:
        raise MegafonInternalCallSyncError(
            "МегаФон вернул некорректную длительность внутреннего звонка."
        )
    return result


def _normalize_recording_ref(origin, value):
    raw = str(value or "").strip()
    if not raw:
        return ""
    absolute = urljoin(origin + "/", raw)
    try:
        parts = urlsplit(absolute)
        port = parts.port
    except ValueError:
        return ""
    hostname = (parts.hostname or "").lower()
    if (
        parts.scheme != "https"
        or not hostname
        or not (hostname == "megapbx.ru" or hostname.endswith(".megapbx.ru"))
        or port not in (None, 443)
        or parts.username
        or parts.password
    ):
        return ""
    return absolute[:500]


def _remember_recording_host(telephony, recording_ref):
    if not recording_ref:
        return
    try:
        hostname = (urlsplit(recording_ref).hostname or "").lower()
    except ValueError:
        return
    if not hostname:
        return
    hosts = telephony.recording_allowed_hosts
    if not isinstance(hosts, list):
        hosts = []
    normalized = [
        str(item).strip().lower()
        for item in hosts
        if isinstance(item, str) and item.strip()
    ]
    if hostname in normalized:
        return
    normalized.append(hostname)
    TelephonyConnection.objects.filter(pk=telephony.pk).update(
        recording_allowed_hosts=normalized
    )
    telephony.recording_allowed_hosts = normalized


def _participant_display(profile, user, info, fallback_name):
    if profile is not None:
        return profile.display_name or str(profile)
    if user is not None:
        return user.get_full_name() or user.username
    return (
        str(info.get("name") or "").strip()
        or str(fallback_name or "").strip()
        or str(info.get("login") or "").strip()
    )


def _resolve_participant(organization, telephony, users, provider_value, fallback_name):
    info = _provider_user_info(users, provider_value, fallback_name)
    provider_user = str(info.get("login") or provider_value or "").strip()[:255]
    extension = str(info.get("ext") or "").strip()[:64]
    profile, user = resolve_call_employee(
        organization,
        telephony,
        extension,
        provider_user,
        lock_identity=True,
    )
    return profile, user, provider_user, extension, info


def _ingest_internal_row(telephony, users, origin, row):
    if not isinstance(row, dict):
        raise MegafonInternalCallSyncError(
            "МегаФон вернул некорректную запись внутреннего звонка."
        )

    uid = str(row.get("uid") or "").strip()
    from_value = str(row.get("from") or "").strip()
    to_value = str(row.get("to") or "").strip()
    from_name = str(row.get("from_name") or "").strip()
    to_name = str(row.get("to_name") or "").strip()
    status = str(row.get("status") or "").strip().casefold()

    if not uid or len(uid) > 240:
        raise MegafonInternalCallSyncError(
            "МегаФон вернул некорректный идентификатор внутреннего звонка."
        )
    if not from_value or not to_value:
        raise MegafonInternalCallSyncError(
            "МегаФон не указал обоих участников внутреннего звонка."
        )
    if len(from_value) > 255 or len(to_value) > 255:
        raise MegafonInternalCallSyncError(
            "МегаФон вернул слишком длинный идентификатор участника."
        )
    if len(from_name) > 500 or len(to_name) > 500:
        raise MegafonInternalCallSyncError(
            "МегаФон вернул слишком длинное имя участника."
        )

    started_at = _parse_started_at(row.get("start"))
    duration = _bounded_int(
        row.get("duration"),
        maximum=7 * 24 * 60 * 60,
    )
    recording_ref = _normalize_recording_ref(origin, row.get("record"))
    external_id = f"inner:{uid}"

    with transaction.atomic():
        existing = (
            PhoneCall.objects.select_for_update()
            .select_related(
                "employee",
                "employee_profile",
                "peer_employee",
                "peer_employee_profile",
            )
            .filter(
                connection=telephony,
                external_id=external_id,
            )
            .first()
        )

        caller_profile = existing.employee_profile if existing else None
        caller_user = existing.employee if existing else None
        caller_provider_user = existing.provider_user if existing else ""
        caller_extension = existing.provider_extension if existing else ""
        caller_info = _provider_user_info(users, from_value, from_name)
        if not (
            existing
            and (existing.employee_profile_id or existing.employee_id)
        ):
            (
                resolved_profile,
                resolved_user,
                resolved_provider_user,
                resolved_extension,
                caller_info,
            ) = _resolve_participant(
                telephony.organization,
                telephony,
                users,
                from_value,
                from_name,
            )
            caller_profile = resolved_profile
            caller_user = resolved_user
            caller_provider_user = (
                resolved_provider_user
                or caller_provider_user
                or from_value
            )
            caller_extension = resolved_extension or caller_extension

        peer_profile = existing.peer_employee_profile if existing else None
        peer_user = existing.peer_employee if existing else None
        peer_provider_user = existing.peer_provider_user if existing else ""
        peer_extension = existing.peer_provider_extension if existing else ""
        peer_info = _provider_user_info(users, to_value, to_name)
        if not (
            existing
            and (existing.peer_employee_profile_id or existing.peer_employee_id)
        ):
            (
                resolved_peer_profile,
                resolved_peer_user,
                resolved_peer_provider_user,
                resolved_peer_extension,
                peer_info,
            ) = _resolve_participant(
                telephony.organization,
                telephony,
                users,
                to_value,
                to_name,
            )
            peer_profile = resolved_peer_profile
            peer_user = resolved_peer_user
            peer_provider_user = (
                resolved_peer_provider_user
                or peer_provider_user
                or to_value
            )
            peer_extension = resolved_peer_extension or peer_extension

        peer_name = _participant_display(
            peer_profile,
            peer_user,
            peer_info,
            to_name,
        )[:255]

        call, created = PhoneCall.objects.update_or_create(
            connection=telephony,
            external_id=external_id,
            defaults={
                "organization": telephony.organization,
                "source_kind": PhoneCall.SOURCE_TELEPHONY,
                "employee": caller_user,
                "employee_profile": caller_profile,
                "peer_employee": peer_user,
                "peer_employee_profile": peer_profile,
                "provider_user": caller_provider_user or from_value,
                "provider_extension": caller_extension,
                "peer_provider_user": peer_provider_user or to_value,
                "peer_provider_extension": peer_extension,
                "client": None,
                "contact_name": peer_name,
                "phone_number": "",
                "direction": PhoneCall.DIRECTION_INTERNAL,
                "started_at": started_at,
                "duration_seconds": duration,
                "result": (
                    PhoneCall.RESULT_ANSWERED
                    if status == "success"
                    else PhoneCall.RESULT_MISSED
                ),
                "recording_ref": (
                    recording_ref
                    or (existing.recording_ref if existing else "")
                ),
            },
        )

        if recording_ref and not call.recording_file:
            call.recording_status = PhoneCall.RECORDING_PENDING
            call.recording_error = ""
            call.save(update_fields=["recording_status", "recording_error"])

    _remember_recording_host(telephony, recording_ref)
    return call, created


def _cursor_start(provider, now, lookback_hours):
    settings_data = dict(provider.settings or {})
    cursor_raw = str(
        settings_data.get("megafon_internal_history_synced_through") or ""
    ).strip()
    cursor = parse_datetime(cursor_raw) if cursor_raw else None
    if cursor is not None and timezone.is_naive(cursor):
        cursor = cursor.replace(tzinfo=datetime_timezone.utc)
    floor = now - timedelta(hours=lookback_hours)
    if cursor is None:
        return floor
    return max(
        floor,
        cursor.astimezone(datetime_timezone.utc)
        - timedelta(minutes=INTERNAL_HISTORY_OVERLAP_MINUTES),
    )


def sync_megafon_internal_calls(
    telephony,
    *,
    lookback_hours=DEFAULT_INTERNAL_LOOKBACK_HOURS,
):
    try:
        provider, endpoint, api_key = _megafon_api_credentials(telephony)
    except EmployeeIdentitySyncError as exc:
        raise MegafonInternalCallSyncError(
            "; ".join(exc.messages)
        ) from exc

    origin = _api_origin(endpoint)
    users = _load_users(origin, api_key)
    now = timezone.now().astimezone(datetime_timezone.utc)
    start = _cursor_start(provider, now, lookback_hours)

    checked = 0
    created = 0
    updated = 0
    cursor = start

    while cursor < now:
        window_end = min(
            cursor + timedelta(hours=INTERNAL_HISTORY_WINDOW_HOURS),
            now,
        )
        query = urlencode(
            {
                "start": cursor.strftime("%Y%m%dT%H%M%SZ"),
                "end": window_end.strftime("%Y%m%dT%H%M%SZ"),
                "type": "all",
                "limit": MAX_INTERNAL_HISTORY_ROWS,
            }
        )
        data = _read_json(
            f"{origin}/crmapi/v1/history/inner/json?{query}",
            api_key,
        )
        if data is None:
            rows = []
        elif isinstance(data, list):
            rows = data
        else:
            raise MegafonInternalCallSyncError(
                "МегаФон вернул неожиданный формат истории внутренних звонков."
            )
        if len(rows) >= MAX_INTERNAL_HISTORY_ROWS:
            raise MegafonInternalCallSyncError(
                "История внутренних звонков достигла лимита окна; "
                "синхронизация остановлена без сдвига курсора."
            )

        for row in rows:
            _call, was_created = _ingest_internal_row(
                telephony,
                users,
                origin,
                row,
            )
            checked += 1
            if was_created:
                created += 1
            else:
                updated += 1

        cursor = window_end

    settings_data = dict(provider.settings or {})
    settings_data["megafon_internal_history_synced_through"] = now.isoformat()
    settings_data["megafon_internal_history_last_result"] = "success"
    settings_data["megafon_internal_history_last_count"] = checked
    provider.settings = settings_data
    provider.save(update_fields=["settings"])

    return {
        "checked": checked,
        "created": created,
        "updated": updated,
        "synced_through": now,
    }
