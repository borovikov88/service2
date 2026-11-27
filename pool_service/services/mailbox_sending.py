"""Explicit staff-initiated SMTP only. A receipt is reserved before handing anything to SMTP."""

import hashlib
import imaplib
import smtplib
import ssl
from email import policy
from email.message import EmailMessage
from email.utils import format_datetime

from django.core.exceptions import PermissionDenied, ValidationError
from django.core.files.base import ContentFile
from django.db import transaction
from django.utils import timezone

from pool_service.communication_secrets import CommunicationSecretError, decrypt_secret
from pool_service.mailbox_models import CrmMailbox, MailSubmission
from pool_service.models import Organization
from . import mailbox_imap as provider
from .mailbox_access import mailbox_role
from .mailbox_mime import smtp_address, header_data, parse_content
from .mailbox_sync import PROVIDER_ERRORS, store_letter


def deliver(box, actor, key, fields, uploads, parent=None):
    if parent and parent.mailbox_id != box.pk:
        raise PermissionDenied
    message = EmailMessage(policy=policy.SMTP)
    message["From"] = smtp_address(box.address)
    message["To"] = ", ".join(map(smtp_address, fields["to"]))
    if fields["cc"]:
        message["Cc"] = ", ".join(map(smtp_address, fields["cc"]))
    message["Subject"] = fields["subject"]
    at = timezone.now()
    message["Date"] = format_datetime(at)
    message["Message-ID"] = f"<{key}@{box.address.rsplit('@', 1)[1]}>"
    if parent and parent.message_id:
        message["In-Reply-To"] = parent.message_id
        message["References"] = " ".join((parent.reference_ids + [parent.message_id])[-12:])
    message.set_content(fields["body"])
    if len(uploads) > 10 or sum(f.size for f in uploads) > 20 * 1024 * 1024:
        raise ValidationError("Можно приложить до 10 файлов общим размером до 20 МБ.")
    for upload in uploads:
        message.add_attachment(
            upload.read(), maintype="application", subtype="octet-stream", filename=upload.name
        )
    raw = message.as_bytes()
    with transaction.atomic():
        Organization.objects.select_for_update().get(pk=box.organization_id)
        box = CrmMailbox.objects.select_for_update().filter(pk=box.pk, enabled=True).first()
        if box is None:
            raise ValidationError("Ящик отключён. Обновите страницу.")
        if not mailbox_role(actor, box.organization):
            raise PermissionDenied
        receipt, fresh = MailSubmission.objects.get_or_create(
            token=key, defaults={"mailbox": box, "actor": actor}
        )
        if not fresh:
            if receipt.mailbox_id != box.pk or receipt.actor_id != actor.pk:
                raise PermissionDenied
            return receipt
    # An ambiguous SMTP result is never retried automatically.
    attempted = False
    try:
        if uploads:
            receipt.attachment_source.save(f"{key}.eml", ContentFile(raw))
        with smtplib.SMTP_SSL("smtp.mail.ru", 465, timeout=15, context=ssl.create_default_context()) as smtp:
            smtp.login(box.address, decrypt_secret(box.credential))
            attempted = True
            refused = smtp.sendmail(
                smtp_address(box.address),
                list(dict.fromkeys(map(smtp_address, fields["to"] + fields["cc"]))),
                raw,
            )
            receipt.status = "partial" if refused else "sent"
    except (OSError, smtplib.SMTPException, CommunicationSecretError, UnicodeError):
        if receipt.status not in ("partial", "sent"):
            receipt.status = "uncertain" if attempted else "failed"
            receipt.save(update_fields=["status"])
            return receipt
    with transaction.atomic():
        Organization.objects.select_for_update().get(pk=box.organization_id)
        letter = store_letter(
            box,
            header_data(raw, at),
            hashlib.sha256(raw).hexdigest(),
            commercial=True,
            historical=False,
            sent_folder=True,
        )
        letter.body, letter.attachments = parse_content(raw)
        letter.body_loaded = True
        letter.save(update_fields=["body", "attachments", "body_loaded"])
        receipt.letter = letter
        receipt.save(update_fields=["letter", "status"])
        if parent:
            parent.needs_attention = False
            parent.processed_at = timezone.now()
            parent.save(update_fields=["needs_attention", "processed_at"])
    # Some providers save sent mail automatically. Append only if not already stored.
    try:
        with provider.mailbox_session(box) as client:
            sent = next((wire for wire, _, role in provider.folders(client) if role == "sent"), None)
            if sent:
                provider.select(client, sent)
                found = provider.ok(
                    client.uid("SEARCH", None, "HEADER", "Message-ID", '"' + str(message["Message-ID"]) + '"')
                )
                if not any(found):
                    wire = '"' + sent.replace("\\", "\\\\").replace('"', '\\"') + '"'
                    provider.ok(client.append(wire, "(\\Seen)", imaplib.Time2Internaldate(at), raw))
    except PROVIDER_ERRORS:
        pass  # The local accepted receipt remains authoritative for the sender.
    return receipt
