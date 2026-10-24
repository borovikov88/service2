"""One-time scoped setup of the owner's services connection; no ad or payment writes."""
import hashlib
import secrets
from urllib.parse import urlsplit

from django.conf import settings
from django.http.request import validate_host
from django.contrib.auth.models import User
from django.db import transaction
from django.test import RequestFactory
from django.utils import timezone

from . import avito_advance, avito_autoload_auto
from .avito_runtime import when, safe_code
from .communication_avito import (AvitoError, authorized_account_id, _provider_root,
    verify_messenger_access, webhook_subscriptions, subscribe_webhook)
from .communication_models import ChannelConnection

KEY = "avito_services_workplace_v1"
WEBHOOK_KEY = "avito_webhook_binding_v1"


def _connection(pk):
    return ChannelConnection.objects.select_related("channel__organization").get(pk=pk)


def _binding(connection):
    return {"account_id": str(connection.external_id), "channel_id": connection.channel_id,
            "organization_id": connection.channel.organization_id,
            "credential_stamp": avito_autoload_auto._credential_stamp(connection)}


def _same(connection, binding):
    return bool(connection.is_active and connection.channel.is_active and
        connection.channel.kind == "avito" and _binding(connection) == binding)


def _actor(connection):
    metadata = connection.settings if isinstance(connection.settings, dict) else {}
    marker = metadata.get(KEY)
    if isinstance(marker, dict):
        from .avito_status_monitor import subscriber_allowed
        actor_id = marker.get("actor_id")
        actor = User.objects.filter(pk=actor_id, is_active=True).first() if type(actor_id) is int else None
        if (not _same(connection, {key: marker.get(key) for key in _binding(connection)})
                or actor is None or not subscriber_allowed(actor, connection.channel.organization)):
            raise AvitoError("services_setup_actor_unverified")
        return actor
    state = avito_advance._state(connection)
    if not state.get("enabled") or not avito_advance._allowed(connection, state):
        raise AvitoError("services_setup_actor_unverified")
    return User.objects.get(pk=state["actor_id"], is_active=True)


def _origin(value):
    """Use the dedicated trusted Avito origin, never the unrelated SITE_URL."""
    from .avito_services_publication import public_origin
    try:
        candidate = public_origin()
        parsed = urlsplit(candidate)
        valid = (parsed.scheme == "https" and parsed.hostname and not parsed.username and
            not parsed.password and not parsed.query and not parsed.fragment and
            parsed.path in ("", "/") and parsed.port in (None, 443) and
            validate_host(parsed.hostname, settings.ALLOWED_HOSTS))
    except (TypeError, ValueError):
        valid = False
    if not valid:
        raise AvitoError("services_setup_origin_invalid")
    if not isinstance(value, str) or value.rstrip("/") != candidate:
        raise AvitoError("services_setup_origin_mismatch")
    return candidate


def _origin_diagnostics(origin):
    """Boolean-only proof: no generic URL, callback, path or token is exported."""
    generic = getattr(settings, "SITE_URL", "")
    return {"source": "avito_services_public_origin", "cli_origin_verified": True,
            "generic_site_url_differs": bool(isinstance(generic, str) and generic
                                            and generic.rstrip("/") != origin)}


def _request(origin):
    parts = urlsplit(origin)
    return RequestFactory().get("/", secure=True, HTTP_HOST=parts.netloc)


def _token_stamp(connection):
    return hashlib.sha256(connection.api_token_hash.encode()).hexdigest()


def _registered(connection, urls, origin):
    from .communication_views import _avito_subscription_token
    try:
        origin = _origin(origin)
    except AvitoError:
        return False
    request = _request(origin)
    return any(connection.check_api_token(token) for token in
        (_avito_subscription_token(request, connection, url) for url in urls) if token)


def _save_webhook_evidence(connection_id, binding, origin, expected_hash, *, verified, error=""):
    with transaction.atomic():
        connection = ChannelConnection.objects.select_for_update().select_related("channel").get(pk=connection_id)
        if not _same(connection, binding) or connection.api_token_hash != expected_hash:
            raise AvitoError("services_setup_binding_changed")
        _actor(connection)
        metadata = dict(connection.settings) if isinstance(connection.settings, dict) else {}
        metadata["avito_webhook_status"] = "connected" if verified else "error"
        metadata["avito_webhook_checked_at"] = timezone.now().isoformat()
        metadata["avito_webhook_error"] = error
        previous = metadata.get(WEBHOOK_KEY)
        previous = previous if isinstance(previous, dict) else {}
        stamp = _token_stamp(connection)
        # Preserve the first confirmed window only while all binding parts match.
        same_window = all(previous.get(key) == value for key, value in binding.items()) and previous.get("token_stamp") == stamp
        metadata[WEBHOOK_KEY] = {**binding, "origin": origin, "token_stamp": stamp,
            "verified": verified, "verified_at": previous.get("verified_at") if same_window and previous.get("verified") else timezone.now().isoformat(),
            "last_delivery_at": previous.get("last_delivery_at") if same_window else None,
            "chain": previous.get("chain") if same_window else None,
            "last_error_code": error}
        if verified:
            marker = dict(metadata.get(KEY) or {})
            marker["webhook_confirmed_once"] = True
            metadata[KEY] = marker
        connection.settings = metadata
        connection.save(update_fields=["settings"])


def ensure_webhook(connection_id, binding, origin):
    """Register only the Service2 callback, then authoritative readback; no replies."""
    from .communication_views import _avito_callback_url, _avito_subscription_token
    origin = _origin(origin)
    connection = _connection(connection_id)
    if not _same(connection, binding):
        raise AvitoError("services_setup_binding_changed")
    _actor(connection)
    if authorized_account_id(connection) != binding["account_id"]:
        raise AvitoError("provider_account_mismatch")
    verify_messenger_access(connection, binding["account_id"])
    existing = webhook_subscriptions(connection)
    if _registered(connection, existing, origin):
        _save_webhook_evidence(connection_id, binding, origin, connection.api_token_hash, verified=True)
        return
    marker = connection.settings.get(KEY, {})
    if marker.get("webhook_attempted"):
        # Previous POST may have succeeded. Do not blindly issue another write.
        _save_webhook_evidence(connection_id, binding, origin, connection.api_token_hash,
            verified=False, error="provider_webhook_not_confirmed")
        return
    raw_token = secrets.token_urlsafe(32)
    callback_url = _avito_callback_url(_request(origin), connection, raw_token)
    # Validate the actual constructed URL before saving a token hash or posting.
    # The existing parser checks HTTPS, exact canonical host, path, token and no query/fragment.
    if _avito_subscription_token(_request(origin), connection, callback_url) != raw_token:
        raise AvitoError("services_setup_callback_origin_mismatch")
    with transaction.atomic():
        locked = ChannelConnection.objects.select_for_update().select_related("channel").get(pk=connection_id)
        if not _same(locked, binding) or locked.api_token_hash != connection.api_token_hash:
            raise AvitoError("services_setup_binding_changed")
        _actor(locked)
        metadata = dict(locked.settings)
        marker = dict(metadata.get(KEY) or {})
        marker["webhook_attempted"] = True
        metadata[KEY] = marker
        locked.settings = metadata
        locked.set_api_token(raw_token)
        locked.save(update_fields=["api_token_hash", "settings"])
        connection.api_token_hash = locked.api_token_hash
    error = None
    try:
        subscribe_webhook(connection, callback_url)
    except AvitoError as exc:
        # A timeout is not proof registration failed. Reconcile the provider list.
        error = exc
    try:
        confirmed = _registered(connection, webhook_subscriptions(connection), origin)
    except AvitoError as exc:
        confirmed, error = False, error or exc
    if authorized_account_id(connection) != binding["account_id"]:
        raise AvitoError("provider_account_mismatch")
    if not confirmed:
        # Keep the new hash when registration is ambiguous; an accepted callback
        # can still be authenticated. A later setup only reconciles subscriptions.
        code = safe_setup_error(error or AvitoError("provider_webhook_not_confirmed"))
        _save_webhook_evidence(connection_id, binding, origin, connection.api_token_hash, verified=False, error=code)
        return
    _save_webhook_evidence(connection_id, binding, origin, connection.api_token_hash, verified=True)


def stamp_incoming(connection, metadata, result, *, expected_hash, message_present, message=None):
    """Called only after existing token auth + account-validated ingest succeeded."""
    if not message_present or result not in {"created", "duplicate"} or connection.api_token_hash != expected_hash:
        return
    evidence = metadata.get(WEBHOOK_KEY)
    if (not isinstance(evidence, dict) or evidence.get("verified") is not True
            or any(evidence.get(key) != value for key, value in _binding(connection).items())
            or evidence.get("token_stamp") != _token_stamp(connection)):
        return
    verified_at = when(evidence.get("verified_at"))
    if verified_at is None or verified_at > timezone.now():
        return
    if connection.pk == 4:
        from .avito_webhook_chain import capture
        if not capture(connection, metadata, result, message):
            return
        evidence = metadata[WEBHOOK_KEY]
    metadata[WEBHOOK_KEY] = {**evidence, "last_delivery_at": timezone.now().isoformat()}


def webhook_evidence(connection, *, live=False):
    evidence = connection.settings.get(WEBHOOK_KEY) if isinstance(connection.settings, dict) else None
    try:
        origin_valid = isinstance(evidence, dict) and _origin(evidence.get("origin")) == evidence.get("origin")
    except AvitoError:
        origin_valid = False
    valid = bool(origin_valid and isinstance(evidence, dict) and _same(connection, evidence and
        {key: evidence.get(key) for key in _binding(connection)}) and
        evidence.get("token_stamp") == _token_stamp(connection))
    registered = bool(valid and evidence.get("verified") is True)
    delivery = when(evidence.get("last_delivery_at")) if valid else None
    verified_at = when(evidence.get("verified_at")) if valid else None
    result = {"binding_valid": valid, "registration_confirmed": registered,
        "delivery_observed": bool(registered and verified_at and delivery and verified_at <= delivery <= timezone.now()),
        "live_provider_checked": False, "live_registered": None}
    if connection.pk == 4:
        from .avito_webhook_chain import summary
        result["delivery_observed"] = bool(result["delivery_observed"] and summary(connection)["authenticated_event"])
    if live:
        if _provider_root() != "https://api.avito.ru" or not valid:
            return result
        urls = webhook_subscriptions(connection)
        result.update(live_provider_checked=True, live_registered=_registered(connection, urls, evidence["origin"]))
    return result


def safe_setup_error(exc):
    code = str(exc) if isinstance(exc, AvitoError) else ""
    allowed = {"services_setup_actor_unverified", "services_setup_binding_changed",
        "services_setup_origin_mismatch", "services_setup_origin_invalid", "services_setup_worker_unverified",
        "services_setup_callback_origin_mismatch",
        "provider_webhook_not_confirmed", "provider_webhook_rejected", "provider_subscriptions_invalid",
        "provider_account_mismatch", "provider_unavailable"}
    return code if code in allowed else safe_code(code) if code else "services_setup_internal_error"


PROBE_FLAGS = ("outside_checkout", "file_private", "directory_private", "readable", "synthetic_only")


def passenger_storage_probe(connection_id, binding, origin):
    """Actual Passenger UID handles the synthetic write; caller holds no DB lock."""
    import json
    from urllib.error import HTTPError
    from urllib.request import Request, HTTPRedirectHandler, build_opener
    from django.urls import reverse
    from .avito_services_media import make_storage_probe_token
    connection = _connection(connection_id)
    if not _same(connection, binding):
        return {"source": "passenger_http", "checked": False, "error": "storage_probe_binding_changed"}
    try:
        actor = _actor(connection)
    except Exception:
        return {"source": "passenger_http", "checked": False, "error": "storage_probe_binding_changed"}
    marker = connection.settings.get(KEY, {})
    checkpoint = (marker.get("installed_at"), marker.get("actor_id"), marker.get("automatic_reports_configured_once"))
    cached = marker.get("storage_probe")
    if isinstance(cached, dict) and all(cached.get(flag) is True for flag in PROBE_FLAGS):
        return {"source": "passenger_http", "checked": True, "cached": True,
                **{flag: True for flag in PROBE_FLAGS}}
    error, flags = "", {}
    try:
        if authorized_account_id(connection) != binding["account_id"]:
            raise AvitoError("provider_account_mismatch")
        token = make_storage_probe_token(connection, actor)
        target = origin + reverse("avito_services_media_probe")
        class NoRedirect(HTTPRedirectHandler):
            def redirect_request(self, req, fp, code, msg, headers, newurl):
                return None
        request = Request(target, method="POST", data=json.dumps({"token": token}).encode(),
            headers={"Content-Type": "application/json", "Accept": "application/json",
                     "User-Agent": "Service2-Protected-Storage-Probe"})
        with build_opener(NoRedirect()).open(request, timeout=10) as response:
            if response.geturl() != target or response.status != 200:
                raise ValueError
            raw = response.read(2049)
        if len(raw) > 2048:
            raise ValueError
        payload = json.loads(raw)
        if not isinstance(payload, dict) or not all(payload.get(flag) is True for flag in PROBE_FLAGS):
            raise ValueError
        flags = {flag: True for flag in PROBE_FLAGS}
    except HTTPError as exc:
        error = f"storage_probe_http_{exc.code}" if exc.code in {301, 302, 303, 307, 308, 401, 403, 404, 405, 429, 500, 502, 503, 504} else "storage_probe_http_failed"
    except Exception:
        error = "storage_probe_failed"
    # The HTTP request above completed outside any connection/settings transaction.
    with transaction.atomic():
        current = ChannelConnection.objects.select_for_update().select_related("channel").get(pk=connection_id)
        try:
            valid_actor = _actor(current).pk == actor.pk
        except Exception:
            valid_actor = False
        if not _same(current, binding) or not valid_actor:
            return {"source": "passenger_http", "checked": False, "error": "storage_probe_binding_changed"}
        metadata = dict(current.settings)
        marker = dict(metadata.get(KEY, {}))
        if (marker.get("installed_at"), marker.get("actor_id"), marker.get("automatic_reports_configured_once")) != checkpoint:
            return {"source": "passenger_http", "checked": False, "error": "storage_probe_binding_changed"}
        if error:
            marker["storage_probe_last_error"] = error
        else:
            marker["storage_probe"] = {"source": "passenger_http",
                "checked_at": timezone.now().isoformat(), **flags}
            marker.pop("storage_probe_last_error", None)
        metadata[KEY] = marker
        current.settings = metadata
        current.save(update_fields=["settings"])
    return {"source": "passenger_http", "checked": not error, "cached": False,
            **flags, **({"error": error} if error else {})}


def storage_probe_evidence(connection):
    marker = connection.settings.get(KEY) if isinstance(connection.settings, dict) else None
    if not isinstance(marker, dict) or not _same(connection, {key: marker.get(key) for key in _binding(connection)}):
        return {"source": "passenger_http", "checked": False, "cached": True, "error": "storage_probe_binding_changed"}
    saved = marker.get("storage_probe")
    if isinstance(saved, dict) and saved.get("source") == "passenger_http" and all(saved.get(flag) is True for flag in PROBE_FLAGS):
        return {"source": "passenger_http", "checked": True, "cached": True,
                **{flag: True for flag in PROBE_FLAGS}}
    code = marker.get("storage_probe_last_error", "storage_probe_not_run")
    allowed = {"storage_probe_binding_changed", "storage_probe_not_run", "storage_probe_failed", "storage_probe_http_failed"}
    if code not in allowed and not __import__("re").fullmatch(r"storage_probe_http_(?:301|302|303|307|308|401|403|404|405|429|500|502|503|504)", str(code)):
        code = "storage_probe_failed"
    return {"source": "passenger_http", "checked": False, "cached": True, "error": code}


def setup(connection_id, account_id, base_url):
    connection = _connection(connection_id)
    if _provider_root() != "https://api.avito.ru" or str(connection.external_id) != account_id:
        raise AvitoError("provider_account_mismatch")
    binding = _binding(connection)
    if not _same(connection, binding):
        raise AvitoError("services_setup_binding_changed")
    metadata = connection.settings if isinstance(connection.settings, dict) else {}
    marker = metadata.get(KEY)
    origin = _origin(base_url)
    if marker:
        if not isinstance(marker, dict) or any(marker.get(key) != value for key, value in binding.items()):
            raise AvitoError("services_setup_binding_changed")
        actor = User.objects.filter(pk=marker.get("actor_id"), is_active=True).first()
        from .avito_status_monitor import subscriber_allowed
        if actor is None or not subscriber_allowed(actor, connection.channel.organization):
            raise AvitoError("services_setup_actor_unverified")
        if marker.get("webhook_confirmed_once"):
            return {"setup": "already_applied", "connection_id": connection_id,
                    "origin": _origin_diagnostics(origin),
                    "automatic_reports_enabled": avito_autoload_auto._state(connection).get("enabled") is True,
                    "webhook": webhook_evidence(connection),
                    "storage_probe": passenger_storage_probe(connection_id, binding, origin)}
    else:
        actor = _actor(connection)
        if authorized_account_id(connection) != account_id:
            raise AvitoError("provider_account_mismatch")
        with transaction.atomic():
            current = ChannelConnection.objects.select_for_update().select_related("channel").get(pk=connection_id)
            if not _same(current, binding):
                raise AvitoError("services_setup_binding_changed")
            actor = _actor(current)
            if not current.settings.get(KEY):
                from .avito_scheduler import scheduler_state
                if not scheduler_state().get("worker_ready"):
                    raise AvitoError("services_setup_worker_unverified")
                avito_autoload_auto.configure(current, actor, "enable")
                current.refresh_from_db()
                metadata = dict(current.settings)
                metadata[KEY] = {**binding, "actor_id": actor.pk,
                    "installed_at": timezone.now().isoformat(),
                    "automatic_reports_configured_once": True, "webhook_attempted": False,
                    "webhook_confirmed_once": False}
                current.settings = metadata
                current.save(update_fields=["settings"])
    webhook_error = ""
    try:
        ensure_webhook(connection_id, binding, origin)
    except Exception as exc:
        webhook_error = safe_setup_error(exc)
    current = _connection(connection_id)
    result = {"setup": "applied", "connection_id": connection_id,
        "origin": _origin_diagnostics(origin),
        "automatic_reports_enabled": avito_autoload_auto._state(current).get("enabled") is True,
        "webhook": webhook_evidence(current), "webhook_error": webhook_error,
        "background_report_success": False,
        "storage_probe": passenger_storage_probe(connection_id, binding, origin)}
    return result
