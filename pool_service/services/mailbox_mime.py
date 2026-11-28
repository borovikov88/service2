"""Parse email as untrusted content. Keep search text, presentation and downloadable files separate."""

import base64
import binascii
import hashlib
import quopri
import re
from datetime import timezone as utc
from email import policy
from email.errors import MessageError
from email.message import Message
from email.parser import BytesParser
from email.utils import getaddresses, parsedate_to_datetime
from html.parser import HTMLParser
from io import BytesIO
from pathlib import PurePath
from urllib.parse import urlsplit

from django.core.exceptions import ValidationError
from django.core.validators import validate_email
from PIL import Image

from .mailbox_html import clean_letter, has_text

CONTENT_VERSION = 2
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


def _web_url(value):
    if not value or len(value) > 4096 or any(c.isspace() or ord(c) < 32 for c in value):
        return ""
    try:
        url = urlsplit(value)
        return value if url.scheme.lower() in ("http", "https") and url.hostname and not url.username else ""
    except ValueError:
        return ""


class _TextLimitReached(Exception):
    pass


class PlainHTML(HTMLParser):
    def __init__(self, limit):
        super().__init__(convert_charrefs=True)
        self.text = []
        self.remaining = limit
        self.hidden = 0
        self.link = ""

    def _append(self, value):
        if not value.strip():
            if not self.text or self.text[-1].endswith(("\n", " ")):
                return
        value = value[:self.remaining]
        self.text.append(value)
        self.remaining -= len(value)
        if not self.remaining:
            raise _TextLimitReached

    def handle_starttag(self, tag, attrs):
        if tag in ("head", "script", "style", "template"):
            self.hidden += 1
        elif not self.hidden:
            attrs = dict(attrs)
            if tag in ("br", "p", "div", "li", "tr", "blockquote"):
                self._append("\n")
            elif tag == "a":
                self.link = _web_url(attrs.get("href"))
            elif tag == "img" and attrs.get("alt"):
                self._append(attrs["alt"])

    def handle_endtag(self, tag):
        if tag in ("head", "script", "style", "template") and self.hidden:
            self.hidden -= 1
        elif not self.hidden and tag == "a" and self.link:
            if not self.text or self.text[-1].strip() != self.link:
                self._append(" " + self.link + " ")
            self.link = ""
        elif not self.hidden and tag in ("p", "div", "li", "tr", "blockquote"):
            self._append("\n")

    def handle_data(self, data):
        if not self.hidden:
            self._append(re.sub(r"\s+", " ", data))


def _part_text(part, partial):
    if partial:
        encoding = (part.get("Content-Transfer-Encoding") or "").strip().lower()
        if encoding in ("base64", "quoted-printable"):
            payload = part.get_payload().encode("ascii", "replace")
            if encoding == "base64":
                encoded = b"".join(payload.split())
                # Python's tolerant email decoder can return raw base64 for a cut quartet.
                try:
                    data = base64.b64decode(encoded[:len(encoded) // 4 * 4])
                except binascii.Error:
                    return ""
            else:
                data = quopri.decodestring(re.sub(rb"=[0-9a-fA-F]?$", b"", payload))
            try:
                return data.decode(part.get_content_charset() or "utf-8", "replace")
            except LookupError:
                return data.decode("utf-8", "replace")
    try:
        return part.get_content()
    except (LookupError, UnicodeError, KeyError):
        return (part.get_payload(decode=True) or b"").decode("utf-8", "replace")


def _body_text(mail, *, partial=False, limit=MAX_BODY):
    for subtype in ("html", "plain"):
        part = mail.get_body(preferencelist=(subtype,))
        if part is None:
            continue
        text = _part_text(part, partial)
        if subtype == "html":
            html = PlainHTML(limit)
            try:
                # A stylesheet or quoted markup can be much larger than the visible message.
                html.feed(text)
                html.close()
            except (_TextLimitReached, ValueError, AssertionError):
                pass
            text = "\n".join(line for row in "".join(html.text).splitlines() if (line := " ".join(row.split())))
        if text.strip(" \r\n\t\xa0\u200b\ufeff"):
            return text[:limit]
    return ""


_GREETING = re.compile(
    r"^(?:здравствуй(?:те)?|добр(?:ый (?:день|вечер)|ое утро|ого (?:дня|времени суток))|"
    r"привет(?:ствую)?|уважаемые коллеги|hello|hi|good (?:morning|afternoon|evening))\b",
    re.IGNORECASE,
)
_SALUTATION = re.compile(r"^(?:коллеги|друзья|партн[её]ры|[А-ЯЁA-Z][а-яёa-z-]+(?: [А-ЯЁA-Z][а-яёa-z-]+)?)[!.:,\n]")


def body_preview(text):
    text = (text or "")[:4096].replace("\r\n", "\n").replace("\u200b", "").strip()
    addressee = re.match(r"^[А-ЯЁA-Z][а-яёa-z-]+(?: [А-ЯЁA-Z][а-яёa-z-]+)?,\s*", text)
    if addressee and _GREETING.match(text[addressee.end():]):
        text = text[addressee.end():]
    greeting = _GREETING.match(text)
    if greeting:
        tail = text[greeting.end():].lstrip(" \t")
        if tail.startswith(","):
            tail = tail[1:].lstrip(" \t")
            tail = _SALUTATION.sub("", tail, count=1)
        text = tail.lstrip(" ,.:!—–-\n\t")
    return " ".join(text.split())[:220]


def preview_from_prefix(raw, *, partial=False):
    mail = BytesParser(policy=policy.default).parsebytes(raw)
    return body_preview(_body_text(mail, partial=partial, limit=4096))


def parse_content(raw, download_index=None):
    mail = raw if isinstance(raw, Message) else BytesParser(policy=policy.default).parsebytes(raw)
    text = _body_text(mail) if download_index is None else ""
    files = []
    for index, part in enumerate(mail.walk()):
        if part.is_multipart() and part.get_content_type() != "message/rfc822":
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


def inline_images(mail):
    """Resolve inline raster images from this message only, with a bounded response size."""
    images = {}
    seen = set()
    budget = 6 * 1024 * 1024
    for part in mail.walk():
        cid = str(part.get("Content-ID") or "").strip().strip("<>")
        if not cid or part.get_content_maintype() != "image":
            continue
        if cid in seen:
            images.pop(cid, None)
            continue
        seen.add(cid)
        if len(images) >= 32 or budget <= 0:
            continue
        data = part.get_payload(decode=True) or b""
        if not data or len(data) > min(budget, 2 * 1024 * 1024):
            continue
        try:
            with Image.open(BytesIO(data), formats=("PNG", "JPEG", "GIF", "WEBP")) as image:
                if image.width * image.height > 20_000_000:
                    continue
                mime = Image.MIME[image.format]
                image.verify()
        except (OSError, SyntaxError, ValueError, Image.DecompressionBombError):
            continue
        images[cid] = f"data:{mime};base64," + base64.b64encode(data).decode("ascii")
        budget -= len(data)
    return images


def cache_content(letter, raw):
    mail = BytesParser(policy=policy.default).parsebytes(raw)
    letter.body, letter.attachments = parse_content(mail)
    part = mail.get_body(preferencelist=("html",))
    letter.body_html = clean_letter(_part_text(part, False)) if part else ""
    if letter.body_html and not has_text(letter.body_html):
        alternate = mail.get_body(preferencelist=("plain",))
        if alternate:
            plain = _part_text(alternate, False)
            if plain.strip():
                letter.body = plain[:MAX_BODY]
        if "<img " not in letter.body_html:
            letter.body_html = ""
    letter.preview = body_preview(letter.body)
    letter.has_attachments = bool(letter.attachments)
    letter.body_loaded = True
    letter.content_version = CONTENT_VERSION
    letter.save(update_fields=["body", "body_html", "attachments", "has_attachments", "body_loaded", "preview", "content_version"])
    return mail
