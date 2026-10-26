"""One protected, bounded wallet read using the existing Avito reader."""
import os
import re
import uuid
from datetime import timedelta
from django.db import transaction
from django.utils import timezone
from pool_service import avito_workspace as api
from pool_service.avito_mcp_policy import ACCOUNT_ID, can_access_avito_mcp, scoped_connection
from pool_service.avito_mcp_reader import CACHE_SCOPE_KEY, cache_binding, _section, _when
from pool_service.communication_avito import AvitoError, access_token, _provider_root
from pool_service.communication_models import ChannelConnection

KEY = "avito_assistant_wallet_read_v1"


def _error(exc):
    code = str(exc) if isinstance(exc, AvitoError) else ""
    known = {"provider_data_invalid", "provider_account_mismatch", "provider_unavailable",
        "provider_credentials_missing", "provider_credentials_invalid", "provider_token_missing",
        "provider_account_id_invalid", "provider_account_id_missing", "provider_invalid_response",
        "provider_invalid_json", "provider_response_too_large"}
    return code if code in known or re.fullmatch(r"provider_http_[1-5][0-9]{2}", code) else "wallet_read_failed"


def prepare_wallet_once():
    """No grant or privilege provisioning; safe receipt contains no sums."""
    from pool_service.avito_services_workplace_setup import _actor
    if os.environ.get("SERVICE2_AVITO_DEPLOY_SETUP") != "1" or _provider_root() != "https://api.avito.ru":
        return {"state": "blocked", "code": "protected_setup_required", "request_performed": False}
    source = scoped_connection()
    if source is None:
        return {"state": "blocked", "code": "scope_unavailable", "request_performed": False}
    lease = uuid.uuid4().hex
    with transaction.atomic():
        current = ChannelConnection.objects.select_for_update().select_related("channel__organization").get(pk=4)
        actor = _actor(current)
        binding = cache_binding(current)
        if current != scoped_connection() or not can_access_avito_mcp(actor, current.channel.organization):
            return {"state": "blocked", "code": "scope_unavailable", "request_performed": False}
        existing, status = _section(current, "balance")
        if status == "available" and existing.get("status") == "ok" and _when(existing.get("success_at")):
            return {"state": "already_available", "received_at": _when(existing["success_at"]), "request_performed": False}
        metadata = dict(current.settings) if type(current.settings) is dict else {}
        previous = metadata.get(KEY)
        if type(previous) is dict and previous.get("binding") == binding:
            return {"state": "already_attempted", "code": previous.get("code") if previous.get("code") in
                    {"wallet_read_failed", "wallet_scope_changed", "wallet_lease_lost", "wallet_lease_expired"} else _error(AvitoError(previous.get("code", ""))) if previous.get("code") else "",
                    "received_at": _when(previous.get("finished_at")), "request_performed": False}
        until = api_date(metadata.get("avito_workspace_refresh_until"))
        if until and until > timezone.now():
            return {"state": "pending", "code": "workspace_busy", "request_performed": False}
        try:
            # Conservative shared database budget for this provider/account read.
            api.enforce_rate(ACCOUNT_ID, "balance", seconds=60)
        except api.AvitoCooldownError:
            return {"state": "pending", "code": "provider_cooldown", "request_performed": False}
        metadata[KEY] = {"binding": binding, "state": "claimed", "started_at": timezone.now().isoformat()}
        metadata["avito_workspace_refresh_lease"] = lease
        metadata["avito_workspace_refresh_until"] = (timezone.now()+timedelta(minutes=3)).isoformat()
        current.settings = metadata
        current.save(update_fields=["settings"])
    code, data, request_performed, wallet_performed = "", None, None, False
    try:
        # All external requests are outside the reserve/commit transactions.
        token = access_token(current)
        request_performed = True
        if api.profile_data(api._get(token, "/core/v1/accounts/self"))["id"] != ACCOUNT_ID:
            raise AvitoError("provider_account_mismatch")
        wallet_performed = True
        data = api.fetch_section("balance", token, ACCOUNT_ID)
        if api.profile_data(api._get(token, "/core/v1/accounts/self"))["id"] != ACCOUNT_ID:
            raise AvitoError("provider_account_mismatch")
    except Exception as exc:
        code = _error(exc)
        if request_performed is None and code in {"provider_credentials_missing", "provider_credentials_invalid"}:
            request_performed = False
    with transaction.atomic():
        fresh = ChannelConnection.objects.select_for_update().select_related("channel__organization").get(pk=4)
        metadata = dict(fresh.settings) if type(fresh.settings) is dict else {}
        if metadata.get("avito_workspace_refresh_lease") != lease:
            return {"state": "unconfirmed", "code": "wallet_lease_lost", "request_performed": request_performed,
                    "wallet_request_performed": wallet_performed}
        lease_until = api_date(metadata.get("avito_workspace_refresh_until"))
        if lease_until is None or lease_until <= timezone.now():
            code = "wallet_lease_expired"
        metadata.pop("avito_workspace_refresh_lease", None)
        metadata.pop("avito_workspace_refresh_until", None)
        try:
            current_actor = _actor(fresh)
            valid = (actor.pk == current_actor.pk and cache_binding(fresh) == binding
                and scoped_connection() == fresh and can_access_avito_mcp(current_actor, fresh.channel.organization))
        except Exception:
            valid = False
        if not valid:
            code = "wallet_scope_changed"
        finished = timezone.now().isoformat()
        # Retain every previous successful payload and time on any failure.
        if not code:
            saved = metadata.get("avito_workspace")
            saved = dict(saved) if type(saved) is dict and saved.get("account_id") == ACCOUNT_ID else {"account_id": ACCOUNT_ID}
            sections = dict(saved.get("sections")) if type(saved.get("sections")) is dict else {}
            sections["balance"] = {"status": "ok", "data": data, "checked_at": finished,
                "success_at": finished, "stale": False, "code": "", "detail": "",
                CACHE_SCOPE_KEY: binding}
            saved["sections"] = sections
            metadata["avito_workspace"] = saved
        metadata[KEY] = {"binding": binding, "state": "failed" if code else "success",
                         "finished_at": finished, "code": code,
                         "request_performed": request_performed, "wallet_request_performed": wallet_performed}
        fresh.settings = metadata
        fresh.save(update_fields=["settings"])
    return {"state": "failed" if code else "read_confirmed", "code": code,
            "received_at": finished, "request_performed": request_performed,
            "wallet_request_performed": wallet_performed}


def api_date(value):
    from django.utils.dateparse import parse_datetime
    try:
        result = parse_datetime(value) if type(value) is str else None
        return result if result is not None and timezone.is_aware(result) else None
    except (ValueError, TypeError, OverflowError):
        return None
