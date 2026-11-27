"""Parse email as untrusted content. Only plain text and explicit file downloads reach CRM."""

import base64
import binascii
import hashlib
import re
from datetime import timezone as utc
from email import policy
from email.errors import MessageError
from email.parser import BytesParser
from email.utils import getaddresses, parsedate_to_datetime
from html.parser import HTMLParser
from pathlib import PurePath

from django.core.exceptions import ValidationError
from django.core.validators import validate_email

MAX_BODY = 512_000
MAX_RAW = 64 * 1024 * 1024
ID_PATTERN = re.compile(r"<[^\s<>\x00-\x1f]{1,996}>")


def digest(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def folder_label(wire_name):
    def decode(match):
        value = match.group(1)
        if not value:
            return "&"
        try:
            return base64.b64decode(value.replace(",", "/") + "=" * (-len(value) % 4)).decode("utf-16-be")
        except (ValueError, binascii.Error):
            return match.group(0)

    return re.sub(r"&([A-Za-z0-9+,]*)-", decode, wire_name)


def smtp_address(address):
    local, sep, domain = address.rpartition("@")
    if not sep or not local or not domain:
        raise ValidationError("Введите электронный адрес с именем и доменом.")
    if not local.isascii():
        raise ValidationError("До знака @ допускаются только латинские символы.")
    try:
        return local + "@" + domain.encode("idna").decode("ascii")
    except UnicodeError as exc:
        raise ValidationError("Некорректный домен электронной почты.") from exc


def display_address(address):
    local, sep, domain = address.strip().casefold().rpartition("@")
    if domain.isascii():
        try:
            domain = domain.encode("ascii").decode("idna")
        except UnicodeError:
            pass
    return local + sep + domain


def addresses(values):
    result = []
    try:
        parsed = getaddresses(values)
    except (ValueError, IndexError, UnicodeError):
        return []
    for name, addr in parsed:
        addr = display_address(addr)
        try:
            validate_email(addr)
        except ValidationError:
            continue
        if len(addr) <= 254:
            result.append((name.strip()[:255], addr))
    return result


def header_data(raw, internal_date):
    mail = BytesParser(policy=policy.default).parsebytes(raw, headersonly=True)

    def values(name):
        try:
            return [str(value) for value in mail.get_all(name, [])]
        except (MessageError, IndexError, ValueError, UnicodeError):
            return []

    senders = addresses(values("From"))
    sender_name, sender = senders[0] if len(senders) == 1 else ("", "")
    to = list(dict.fromkeys(a for _, a in addresses(values("To"))))
    cc = list(dict.fromkeys(a for _, a in addresses(values("Cc"))))
    replies = addresses(values("Reply-To"))
    refs = ID_PATTERN.findall(" ".join(values("References"))) + ID_PATTERN.findall(
        " ".join(values("In-Reply-To"))
    )
    subject = " ".join(values("Subject"))
    ids = ID_PATTERN.findall(" ".join(values("Message-ID")))
    try:
        at = parsedate_to_datetime(" ".join(values("Date")))
        if not at or not at.tzinfo:
            at = internal_date
        at = at.astimezone(utc.utc)
        if at.year < 1000:
            at = internal_date
    except (ValueError, OverflowError, TypeError):
        at = internal_date
    return {
        "subject": " ".join(subject.split())[:998],
        "message_id": ids[0] if len(ids) == 1 else "",
        "reference_ids": list(dict.fromkeys(refs[-25:])),
        "sender": sender,
        "sender_name": sender_name,
        "recipients": to,
        "copies": cc,
        "reply_to": replies[0][1] if len(replies) == 1 else "",
        "sent_at": at,
    }


def fingerprint(data, fallback):
    if not data["message_id"]:
        return digest("location:" + fallback)
    return digest(
        "\n".join((data["message_id"], data["sender"], data["subject"], ",".join(data["recipients"])))
    )


class PlainHTML(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.text = []
        self.hidden = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("head", "script", "style"):
            self.hidden += 1
        elif not self.hidden and tag in ("br", "p", "div", "li", "tr", "blockquote"):
            self.text.append("\n")

    def handle_endtag(self, tag):
        if tag in ("head", "script", "style") and self.hidden:
            self.hidden -= 1

    def handle_data(self, data):
        if not self.hidden:
            self.text.append(data)


def parse_content(raw, download_index=None):
    mail = BytesParser(policy=policy.default).parsebytes(raw)
    body_part = mail.get_body(preferencelist=("plain", "html"))
    text = ""
    if body_part:
        try:
            text = body_part.get_content()
        except (LookupError, UnicodeError, KeyError):
            text = (body_part.get_payload(decode=True) or b"").decode("utf-8", "replace")
        if body_part.get_content_type() == "text/html":
            html = PlainHTML()
            try:
                html.feed(text[: MAX_BODY * 2])
                html.close()
            except (ValueError, AssertionError):
                pass  # Preserve the text already parsed from a malformed HTML message.
            text = "".join(html.text)
    files = []
    for index, part in enumerate(mail.walk()):
        if part is body_part or (part.is_multipart() and part.get_content_type() != "message/rfc822"):
            continue
        filename = part.get_filename()
        if (
            not filename
            and part.get_content_disposition() != "attachment"
            and part.get_content_maintype() == "text"
        ):
            continue
        filename = PurePath((filename or "Вложение").replace("\\", "/")).name
        filename = "".join(c for c in filename if c.isprintable()).strip()[:200] or "Вложение"
        files.append({"index": index, "name": filename})
        if index == download_index:
            content = part.get_payload(decode=True)
            if content is None and part.get_content_type() == "message/rfc822" and part.get_payload():
                content = part.get_payload(0).as_bytes()
            return filename, content or b""
    if download_index is not None:
        raise ValueError("attachment missing")
    return text[:MAX_BODY], files
