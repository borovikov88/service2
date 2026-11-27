"""Incremental header sync. Front-of-folder and archive cursors survive restarts independently."""

import hashlib
import imaplib
import re
import time
from datetime import timedelta
from uuid import uuid4

from django.db import transaction
from django.db.models import Exists, F, OuterRef, Q
from django.utils import timezone

from pool_service.communication_secrets import CommunicationSecretError
from pool_service.mailbox_models import (
    CrmMailbox,
    MailFolder,
    MailLetter,
    MailLocation,
    MailPeer,
    MailSubmission,
)
from pool_service.models import Organization
from . import mailbox_imap as provider
from .mailbox_mime import digest, display_address, fingerprint, header_data
from .mailbox_routing import automatic_link

WINDOW = 80
PROVIDER_ERRORS = (OSError, imaplib.IMAP4.error, CommunicationSecretError, provider.MailUnavailable)


def store_letter(box, data, fallback, *, commercial, historical, sent_folder=False):
    own_address = display_address(box.address)
    key = fingerprint(data, fallback)
    letter, created = MailLetter.objects.get_or_create(
        mailbox=box,
        fingerprint=key,
        defaults={
            **data,
            "message_key": digest(data["message_id"]) if data["message_id"] else "",
            "historical": historical,
            "commercial": commercial,
            "outgoing": sent_folder,
            "needs_attention": commercial and not historical and not sent_folder,
        },
    )
    promoted = commercial and not letter.commercial
    confirmed_sent = sent_folder and not letter.outgoing
    if promoted or confirmed_sent:
        letter.commercial = letter.commercial or commercial
        letter.outgoing = letter.outgoing or sent_folder
        letter.needs_attention = not (letter.historical or letter.outgoing or letter.processed_at)
        letter.save(update_fields=["commercial", "outgoing", "needs_attention"])
    if created or promoted or confirmed_sent:
        peers = sorted(
            {
                addr
                for addr in [data["sender"], *data["recipients"], *data["copies"]]
                if addr and addr != own_address
            }
        )
        if created:
            MailPeer.objects.bulk_create([MailPeer(address=addr, letter=letter) for addr in peers])
        if len(peers) == 1 and (letter.outgoing or data["sender"] != own_address):
            automatic_link(letter, peers[0])
            if letter.outgoing and commercial and data["reference_ids"]:
                # A confirmed Sent copy from any mail client also settles the referenced incoming message.
                box.letters.filter(
                    message_key__in=[digest(ref) for ref in data["reference_ids"]],
                    message_id__in=data["reference_ids"],
                    peers__address=peers[0],
                    outgoing=False,
                    needs_attention=True,
                ).update(needs_attention=False, processed_at=timezone.now())
    return letter


def _own_lease(box, token):
    if not CrmMailbox.objects.select_for_update().filter(pk=box.pk, enabled=True, sync_token=token).exists():
        raise provider.MailUnavailable("Сеанс синхронизации завершён.")


def _reconcile_letters(box, ids):
    copies = MailLocation.objects.filter(
        letter=OuterRef("pk"), folder__mailbox=box, folder__available=True, generation=F("folder__generation")
    ).exclude(folder__role__in=("junk", "trash", "drafts"))
    sent = MailSubmission.objects.filter(letter=OuterRef("pk"), mailbox=box, status__in=("sent", "partial"))
    box.letters.filter(pk__in=ids, commercial=True).filter(~Exists(copies), ~Exists(sent)).update(
        commercial=False, needs_attention=False
    )


def _missing_uids(client, folder, generation):
    known = MailLocation.objects.filter(folder=folder, generation=generation).order_by("uid")
    page = list(known.filter(uid__gt=folder.audit_uid).values_list("uid", flat=True)[:100])
    if not page:
        page = list(known.values_list("uid", flat=True)[:100])
    if not page:
        return set(), 0
    recent = known.order_by("-uid").values_list("uid", flat=True)[:30]
    checked = set(page).union(recent)
    rows = provider.ok(client.uid("FETCH", ",".join(str(uid) for uid in sorted(checked)), "(UID)"))
    present = {
        int(uid) for row in rows if isinstance(row, bytes) for uid in re.findall(rb"\bUID (\d+)\b", row)
    }
    return checked - present, page[-1]


def _scan(box, token, client, folder):
    generation, next_uid = provider.select(client, folder.wire_name)
    newest = max(0, next_uid - 1)
    reset = generation != folder.generation
    live = newest if reset else folder.latest_uid
    archive = newest if reset else folder.backfill_uid
    ranges = []
    if live < newest:
        end = min(live + WINDOW, newest)
        ranges.append((live + 1, end))
        live = end
    if archive:
        start = max(1, archive - WINDOW + 1)
        ranges.append((start, archive))
        archive = start - 1
    missing, audit_uid = (set(), 0) if reset else _missing_uids(client, folder, generation)
    rows = []
    for start, end in ranges:
        rows.extend(provider.headers(client, start, end))
    prepared = []
    for uid, size, arrived, header in rows:
        data = header_data(header, arrived)
        fallback = ""
        if not data["message_id"]:
            # For oversize content we cannot prove two copies are identical. Keep them separate.
            fallback = f"{folder.key}:{generation}:{uid}"
            if size <= provider.MAX_RAW:
                raw = provider.selected_message(client, uid)
                if raw is None:  # expunged between FETCH commands
                    continue
                fallback = hashlib.sha256(raw).hexdigest()
        prepared.append((uid, size, arrived, data, fallback))
    with transaction.atomic():
        Organization.objects.select_for_update().get(pk=box.organization_id)
        _own_lease(box, token)
        obsolete = MailLocation.objects.filter(folder=folder)
        obsolete = (
            obsolete.exclude(generation=generation)
            if reset
            else obsolete.filter(generation=generation, uid__in=missing)
        )
        removed = list(obsolete.values_list("letter_id", flat=True))
        obsolete.delete()
        for uid, size, arrived, data, fallback in prepared:
            letter = store_letter(
                box,
                data,
                fallback,
                commercial=folder.role not in ("junk", "trash", "drafts"),
                historical=arrived < box.connected_at,
                sent_folder=folder.role == "sent",
            )
            MailLocation.objects.get_or_create(
                folder=folder, generation=generation, uid=uid, defaults={"letter": letter, "size": size}
            )
        folder.generation, folder.latest_uid, folder.backfill_uid = generation, live, archive
        folder.scanned_at, folder.audit_uid = timezone.now(), audit_uid
        folder.save(update_fields=["generation", "latest_uid", "backfill_uid", "scanned_at", "audit_uid"])
        if removed:
            _reconcile_letters(box, removed)


def sync_mailbox(box_id, *, seconds=35):
    token = uuid4()
    now = timezone.now()
    if (
        not CrmMailbox.objects.filter(pk=box_id, enabled=True)
        .filter(Q(lease_until__isnull=True) | Q(lease_until__lte=now))
        .update(sync_token=token, lease_until=now + timedelta(minutes=3))
    ):
        return False
    box = CrmMailbox.objects.select_related("organization").get(pk=box_id)
    deadline = time.monotonic() + seconds
    try:
        with provider.mailbox_session(box) as client:
            discovered = provider.folders(client)
            with transaction.atomic():
                _own_lease(box, token)
                keys = []
                for wire, name, role in discovered:
                    key = digest(wire)
                    keys.append(key)
                    MailFolder.objects.update_or_create(
                        mailbox=box,
                        key=key,
                        defaults={"wire_name": wire, "name": name, "role": role, "available": True},
                    )
                retired = box.folders.filter(available=True).exclude(key__in=keys)
                changed = retired.update(available=False)
                # Retired folders cannot contribute a working copy to the CRM.
                archived = MailLocation.objects.filter(folder__mailbox=box, folder__available=False)
                if changed:
                    _reconcile_letters(box, archived.values("letter_id"))
            # Equal rotation prevents a large archive from starving the smaller folders.
            ordered = box.folders.filter(available=True).order_by(F("scanned_at").asc(nulls_first=True), "pk")
            for folder in ordered:
                if time.monotonic() >= deadline:
                    break
                _scan(box, token, client, folder)
        CrmMailbox.objects.filter(pk=box.pk, sync_token=token).update(
            last_sync_at=timezone.now(), sync_error=""
        )
        return True
    except PROVIDER_ERRORS:
        # Exceptions from remote servers can include sensitive headers and credentials.
        CrmMailbox.objects.filter(pk=box.pk, sync_token=token).update(
            sync_error="Проверьте соединение и пароль приложения."
        )
        return False
    finally:
        CrmMailbox.objects.filter(pk=box.pk, sync_token=token).update(sync_token=None, lease_until=None)
