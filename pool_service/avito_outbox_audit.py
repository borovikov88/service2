"""Bounded read-only diagnostics for one approved Services outbox incident.

No customer text, conversation identifier, peer identifier, provider message id
or arbitrary provider payload may leave this module. This does not send or
change a queued message.
"""
import hashlib
import re
import shutil
import subprocess
from datetime import datetime, timedelta, timezone as datetime_timezone

from django.utils import timezone

from .avito_readonly_audit import binding
from .avito_webhook_chain import summary as webhook_summary
from .communication_models import ChannelConnection, ConversationMessage
from scripts.avito_outbox_receipt import normalize

CONNECTION_ID = 4
ACCOUNT_ID = "446312402"
# The operator-selected incident window is UTC; no customer identity is stored.
INCIDENT_START = datetime(2026, 10, 25, 1, 27, tzinfo=datetime_timezone.utc)
INCIDENT_END = INCIDENT_START + timedelta(minutes=1)
SAFE_ERROR_CODES = {
    "", "provider_unavailable", "provider_account_mismatch", "provider_credentials_missing",
    "provider_credentials_invalid", "provider_token_missing", "provider_invalid_json",
    "provider_invalid_response", "provider_response_too_large",
    "provider_reconcile_incomplete", "provider_reconcile_invalid", "provider_reconcile_ambiguous",
    "scope_invalid", "legacy_error_unclassified", "audit_provider_unknown", "audit_binding_changed",
    "provider_delivery_unknown", "attachments_require_provider_upload", "empty_message",
    "outbox_scope_invalid", "outbox_sender_access_revoked", "outbox_internal_error", "outbox_legacy_held",
    "outbox_transaction_open", "outbox_binding_changed",
    "provider_send_unknown", "delivery_unknown", "worker_unavailable",
}


def safe_error(value):
    if isinstance(value, str) and (value in SAFE_ERROR_CODES or re.fullmatch(r"provider_http_[1-5][0-9]{2}", value)):
        return value
    return "legacy_error_unclassified" if value else ""


def scheduler_inspection():
    """Inspect only this hosting user's CLI table; panel/system jobs stay unknown."""
    result = {"source": "hosting_user_crontab_cli", "state": "unavailable",
              "services_outbox_entry_present": None, "other_outbox_entry_present": None,
              "account_monitor_entry_present": None, "panel_jobs_verified": False}
    executable = shutil.which("crontab", path="/usr/local/bin:/usr/bin:/bin")
    if not executable:
        return result
    try:
        table = subprocess.run([executable, "-l"], capture_output=True, timeout=5,
            env={"PATH": "/usr/local/bin:/usr/bin:/bin", "LC_ALL": "C"})
    except (OSError, subprocess.SubprocessError):
        return result | {"state": "failed"}
    if table.returncode or len(table.stdout) > 1024 * 1024 or b"\0" in table.stdout:
        # A nonzero CLI result is not proof that no panel job exists.
        return result | {"state": "failed"}
    try:
        lines = table.stdout.decode("utf-8").splitlines()
    except UnicodeError:
        return result | {"state": "failed"}
    services = other = monitor = False
    for line in lines:
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        monitor |= "run_avito_status_monitor" in line or "monitor_avito_statuses" in line or "avito_monitor_cron.py" in line
        if any(token in line for token in ("send_avito_outbox", "run_avito_outbox", "avito_outbox_cron.py")):
            # This is presence evidence only, not a parser proving executable
            # semantics, provider access, success or absence of another job.
            if re.search(r"--connection-id(?:[ =]+)4(?:\s|$)", line):
                services = True
            else:
                other = True
    return result | {"state": "checked", "services_outbox_entry_present": services,
                     "other_outbox_entry_present": other, "account_monitor_entry_present": monitor}


def collect(*, connection_id=CONNECTION_ID, account_id=ACCOUNT_ID):
    if type(connection_id) is not int or connection_id != CONNECTION_ID or account_id != ACCOUNT_ID:
        raise ValueError("scope_invalid")
    from .avito_outbox import outbox_diagnostics, reconcile_message
    from .avito_outbox_original_recovery import BODY_SHA, CONVERSATION_UUID_SHA
    connection = ChannelConnection.objects.select_related("channel").filter(
        pk=CONNECTION_ID, external_id=ACCOUNT_ID, is_active=True,
        channel__kind="avito", channel__is_active=True, avito_credential__isnull=False,
    ).first()
    if connection is None:
        raise ValueError("scope_invalid")
    original_binding = binding(connection)
    if original_binding[2] != ACCOUNT_ID:
        raise ValueError("scope_invalid")
    envelope = {"schema_version": 1, "source": "services_outbox_incident_readonly",
        "checked_at": timezone.now().isoformat(), "connection_id": CONNECTION_ID, "account_id": ACCOUNT_ID,
        "incident_start": INCIDENT_START.isoformat(), "incident_end": INCIDENT_END.isoformat(),
        "scheduler": scheduler_inspection(), "worker": outbox_diagnostics(connection),
        "webhook_chain": webhook_summary(connection), "candidate_count": 0, "message": None,
        "reconciliation": {"state": "not_checked", "error_code": "", "complete": False,
                           "pages": 0, "messages_checked": 0, "provider_id_present": False},
        "customer_post_performed": False, "queue_rows_changed": False}
    rows = list(ConversationMessage.objects.select_related("conversation__connection__channel").filter(
        direction=ConversationMessage.DIRECTION_OUT,
        conversation__connection_id=CONNECTION_ID,
        conversation__organization_id=connection.channel.organization_id,
        created_at__gte=INCIDENT_START, created_at__lt=INCIDENT_END,
    ).order_by("pk")[:2])
    envelope["candidate_count"] = len(rows)
    if len(rows) == 1:
        message = rows[0]
        envelope["message"] = {"local_id": message.pk, "created_at": message.created_at.isoformat(),
            "status": message.delivery_status, "attempts": message.delivery_attempts,
            "has_saved_error": bool(message.delivery_error), "error_code": safe_error(message.delivery_error),
            "external_id_present": bool(message.external_id), "accepted_at": message.delivered_at.isoformat() if message.delivered_at else None,
            "sent_by_present": message.sent_by_id is not None,
            "body_sha256": hashlib.sha256(message.body.encode("utf-8")).hexdigest(),
            "requested_tuple_matches": (hashlib.sha256(message.body.encode()).hexdigest() == BODY_SHA
                and hashlib.sha256(str(message.conversation.uuid).encode()).hexdigest() == CONVERSATION_UUID_SHA),
            "post_evidence": ("accepted_local" if message.external_id and message.delivery_status == "delivered"
                else "attempted_or_unknown" if message.delivery_attempts or message.delivery_status == "sending"
                else "not_claimed" if message.delivery_status == "pending" and not message.external_id
                else "not_recorded")}
        before = (message.delivery_status, message.delivery_attempts, message.delivery_error,
                  message.external_id, message.body, message.conversation_id)
        proof = (reconcile_message(CONNECTION_ID, message.pk) if envelope["message"]["requested_tuple_matches"] else
            {"state": "not_checked", "error_code": "", "complete": False,
             "pages": 0, "messages_checked": 0, "provider_id": None})
        envelope["reconciliation"] = {
            "state": proof.get("state"), "error_code": safe_error(proof.get("error_code")),
            "complete": proof.get("complete"), "pages": proof.get("pages", 0),
            "messages_checked": proof.get("messages_checked", 0),
            "provider_id_present": bool(proof.get("provider_id"))}
        fresh = ConversationMessage.objects.filter(pk=message.pk, conversation__connection_id=CONNECTION_ID).first()
        if fresh is None or before != (fresh.delivery_status, fresh.delivery_attempts, fresh.delivery_error,
                                      fresh.external_id, fresh.body, fresh.conversation_id):
            envelope["reconciliation"] = {"state": "unknown", "error_code": "audit_binding_changed",
                "complete": False, "pages": 0, "messages_checked": 0, "provider_id_present": False}
    if binding(connection) != original_binding:
        raise ValueError("audit_binding_changed")
    return normalize(envelope)
