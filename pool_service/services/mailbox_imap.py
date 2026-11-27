"""Read-only IMAP operations for hosted Mail.ru / VK WorkSpace mailboxes."""

import imaplib
import re
import ssl
from contextlib import contextmanager
from email.utils import parsedate_to_datetime

from django.utils import timezone

from pool_service.communication_secrets import decrypt_secret
from .mailbox_mime import MAX_RAW, folder_label

HOST = "imap.mail.ru"
HEADER_FIELDS = "MESSAGE-ID FROM TO CC SUBJECT DATE REFERENCES IN-REPLY-TO REPLY-TO"
FOLDER_REPLY = re.compile(rb'^\(([^)]*)\)\s+(?:"(?:\\.|[^"])*"|NIL)\s+(.*)$', re.I)
SPECIAL = {
    "\\inbox": "inbox",
    "\\sent": "sent",
    "\\drafts": "drafts",
    "\\junk": "junk",
    "\\trash": "trash",
    "\\archive": "archive",
    "\\all": "all",
}
COMMON = {
    "inbox": "inbox",
    "входящие": "inbox",
    "отправленные": "sent",
    "sent": "sent",
    "спам": "junk",
    "spam": "junk",
    "корзина": "trash",
    "trash": "trash",
    "черновики": "drafts",
    "drafts": "drafts",
    "архив": "archive",
}


class MailUnavailable(Exception):
    pass


def ok(reply):
    status, data = reply
    if status != "OK":
        raise MailUnavailable("Сервер почты не выполнил запрос.")
    return data


@contextmanager
def connect(address, password):
    client = imaplib.IMAP4_SSL(HOST, 993, ssl_context=ssl.create_default_context(), timeout=12)
    try:
        ok(client.login(address, password))
        yield client
    finally:
        try:
            client.logout()
        except (OSError, imaplib.IMAP4.error):
            pass


def mailbox_session(box):
    return connect(box.address, decrypt_secret(box.credential))


def folders(client):
    items = []
    for row in ok(client.list()) or []:
        if not row:
            continue
        head, literal = row if isinstance(row, tuple) else (row, None)
        match = FOLDER_REPLY.match(head)
        if not match:
            continue
        flags = match[1].decode("ascii").lower().split()
        if "\\noselect" in flags:
            continue
        wire = literal if literal is not None else match[2]
        if wire.startswith(b'"') and wire.endswith(b'"'):
            wire = re.sub(rb"\\(.)", rb"\1", wire[1:-1])
        try:
            wire = wire.decode("ascii")
        except UnicodeError:
            continue
        if not wire or len(wire) > 1024 or any(ord(c) < 32 for c in wire):
            continue
        name = folder_label(wire)[:500]
        role = next((SPECIAL[f] for f in flags if f in SPECIAL), COMMON.get(name.casefold(), ""))
        items.append((wire, name, role))
    if not items:
        raise MailUnavailable("Папки почтового ящика недоступны.")
    return items


def select(client, wire):
    escaped = '"' + wire.replace("\\", "\\\\").replace('"', '\\"') + '"'
    ok(client.select(escaped, readonly=True))

    def number(key):
        _, values = client.response(key)
        if not values or not values[0] or not values[0].isdigit():
            raise MailUnavailable("Не удалось определить версию папки.")
        return int(values[0])

    return number("UIDVALIDITY"), number("UIDNEXT")


def fetched_literals(rows):
    # imaplib puts FETCH properties after a literal into a separate bytes entry.
    meta = literal = None
    for row in rows:
        if isinstance(row, tuple):
            if literal is not None:
                yield meta, literal
            meta, literal = row
        elif literal is not None and isinstance(row, bytes):
            meta += b" " + row
    if literal is not None:
        yield meta, literal


def headers(client, start, end):
    rows = ok(
        client.uid(
            "FETCH",
            f"{start}:{end}",
            f"(UID RFC822.SIZE INTERNALDATE BODY.PEEK[HEADER.FIELDS ({HEADER_FIELDS})])",
        )
    )
    for meta, header in fetched_literals(rows):
        uid = re.search(rb"\bUID (\d+)\b", meta)
        size = re.search(rb"\bRFC822\.SIZE (\d+)\b", meta)
        date = re.search(rb'INTERNALDATE "([^"]+)"', meta)
        if not uid or not size:
            raise MailUnavailable("Не удалось прочитать заголовки письма.")
        at = timezone.now()
        if date:
            try:
                at = parsedate_to_datetime(date[1].decode("ascii").replace("-", " ", 2).strip())
            except (TypeError, ValueError, OverflowError):
                pass
        if not at or not at.tzinfo:
            at = timezone.now()
        yield int(uid[1]), int(size[1]), at, header


def selected_message(client, uid):
    rows = ok(client.uid("FETCH", str(uid), f"(UID BODY.PEEK[]<0.{MAX_RAW + 1}>)"))
    for meta, raw in fetched_literals(rows):
        if re.search(rb"\bUID " + str(uid).encode() + rb"\b", meta):
            if len(raw) > MAX_RAW:
                raise MailUnavailable("Письмо больше 64 МБ.")
            return raw
    return None


def raw_message(letter):
    locations = letter.locations.filter(
        folder__mailbox_id=letter.mailbox_id, folder__available=True
    ).select_related("folder")
    with mailbox_session(letter.mailbox) as client:
        for location in locations:
            if location.size > MAX_RAW:
                continue
            generation, _ = select(client, location.folder.wire_name)
            if generation == location.generation:
                raw = selected_message(client, location.uid)
                if raw is not None:
                    return raw
    raise MailUnavailable("Письмо недоступно для загрузки из этой папки.")
