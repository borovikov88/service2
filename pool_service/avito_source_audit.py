"""Bounded read-only checks of the shop's existing Avito XML publisher."""
from collections import Counter
from html.parser import HTMLParser
import http.client
import ipaddress
import re
import socket
import ssl
import time
import xml.etree.ElementTree as ET

from pool_service.communication_avito import AvitoError

HOST = "shop.aqualine22.ru"
FEED_PATH = "/avito/avitofeed.xml"
STOCK_PATH = "/index.php?route=extension/module/avitofeed&stock"
FEED_URL = f"https://{HOST}{FEED_PATH}"
MAX_BYTES = 2 * 1024 * 1024
MAX_ITEMS = 5000
MAX_SAMPLES = 25
REQUEST_SECONDS = 20
ID_PATTERN = re.compile(r"[A-Za-z0-9_-]{1,100}\Z")

ERRORS = {
    "source_unavailable": "Источник сайта недоступен. Проверка не завершена; предыдущий снимок сохранён, если был.",
    "source_address_invalid": "Источник сайта не разрешился в публичный адрес. Запрос не выполнен.",
    "source_http_invalid": "Источник сайта вернул ответ, отличный от HTTP 200, или сжатый ответ. Перенаправления не выполняются.",
    "source_limit": "XML превышает ограничение размера, количества элементов или времени. Полнота проверки не подтверждена.",
    "source_xml_invalid": "Источник вернул некорректный XML или неподдерживаемую структуру. Полнота проверки не подтверждена.",
}


def failure_detail(code):
    return ERRORS.get(code, ERRORS["source_unavailable"])


class _PinnedHTTPS(http.client.HTTPSConnection):
    """Connect to the validated IP while checking TLS for the original host."""
    def __init__(self, address, timeout):
        super().__init__(HOST, timeout=timeout, context=ssl.create_default_context())
        self.address = address

    def connect(self):
        transport = socket.create_connection((self.address, 443), self.timeout)
        try:
            self.sock = self._context.wrap_socket(transport, server_hostname=HOST)
        except Exception:
            transport.close()
            raise


def _read_xml(path):
    # Callers cannot supply a host, URL, port or alternative generator route.
    if path not in {FEED_PATH, STOCK_PATH}:
        raise AvitoError("source_address_invalid")
    deadline = time.monotonic() + REQUEST_SECONDS
    try:
        addresses = list(dict.fromkeys(row[4][0] for row in socket.getaddrinfo(
            HOST, 443, type=socket.SOCK_STREAM,
        )))
        if not addresses or any(
            not ipaddress.ip_address(value).is_global
            or ipaddress.ip_address(value).is_multicast
            for value in addresses
        ):
            raise AvitoError("source_address_invalid")
        connection = _PinnedHTTPS(addresses[0], timeout=min(5, REQUEST_SECONDS))
        try:
            if time.monotonic() >= deadline:
                raise AvitoError("source_limit")
            connection.request("GET", path, headers={
                "Accept": "application/xml", "Accept-Encoding": "identity",
                "User-Agent": "Service2-Avito-source-audit/1",
            })
            transport = connection.sock
            transport.settimeout(max(0.01, deadline - time.monotonic()))
            response = connection.getresponse()
            if response.status != 200 or response.getheader("Content-Encoding", "identity") != "identity":
                raise AvitoError("source_http_invalid")
            declared = response.getheader("Content-Length")
            if declared is not None and (not declared.isdecimal() or int(declared) > MAX_BYTES):
                raise AvitoError("source_limit")
            chunks, size = [], 0
            while True:
                # read1 may close fp (and the last socket owner) on the final
                # Content-Length bytes. Never touch that closed transport.
                if response.isclosed():
                    break
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise AvitoError("source_limit")
                transport.settimeout(remaining)
                chunk = response.read1(min(32768, MAX_BYTES + 1 - size))
                if not chunk:
                    break
                size += len(chunk)
                if size > MAX_BYTES:
                    raise AvitoError("source_limit")
                chunks.append(chunk)
            payload = b"".join(chunks)
            if declared is not None and len(payload) != int(declared):
                raise AvitoError("source_xml_invalid")
            return payload
        finally:
            connection.close()
    except AvitoError:
        raise
    except (OSError, ValueError, http.client.HTTPException):
        raise AvitoError("source_unavailable") from None


def _root(payload, tag):
    if not isinstance(payload, bytes) or len(payload) > MAX_BYTES:
        raise AvitoError("source_limit")
    try:
        text = payload.decode("utf-8-sig")
        # UTF-8 only: declarations and entities cannot hide in UTF-16 bytes.
        if "<!DOCTYPE" in text.upper() or "<!ENTITY" in text.upper():
            raise ValueError
        root = ET.fromstring(text)
        if root.tag != tag or len(list(root.iter())) > 100000 or len(root) > MAX_ITEMS:
            raise ValueError
        expected = "Ad" if tag == "Ads" else "item"
        if any(child.tag != expected for child in root):
            raise ValueError
        return root
    except (UnicodeError, ValueError, ET.ParseError):
        raise AvitoError("source_xml_invalid") from None


def _text(parent, tag):
    element = parent.find(tag)
    return "".join(element.itertext()).strip() if element is not None else ""


def _identifier(value):
    return value if ID_PATTERN.fullmatch(value) else ""


class _Description(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []

    def handle_data(self, data):
        self.parts.append(data)


def _has_description(value):
    parser = _Description()
    try:
        parser.feed(value)
    except AssertionError:
        raise AvitoError("source_xml_invalid") from None
    return bool("".join(parser.parts).strip())


def build_report(feed_payload):
    root = _root(feed_payload, "Ads")
    counts = Counter()
    ids = Counter()
    samples = []
    for ad in root:
        raw_id = _text(ad, "Id")
        identifier = _identifier(raw_id)
        if identifier:
            ids[identifier] += 1
        issues = []
        checks = (
            ("missing_id", not identifier),
            ("missing_title", not _text(ad, "Title")),
            ("missing_description", not _has_description(_text(ad, "Description"))),
            ("missing_images", not any(image.get("url", "").strip() for image in ad.findall("Images/Image"))),
            ("missing_tnved", not _text(ad, "TNVEDCode")),
        )
        for key, missing in checks:
            if missing:
                counts[key] += 1
                issues.append(key)
        if issues and len(samples) < MAX_SAMPLES:
            samples.append({"xml_id": identifier, "issues": issues})
    duplicates = sorted((identifier, count) for identifier, count in ids.items() if count > 1)
    return {
        "version": 1, "complete": True, "source_url": FEED_URL,
        "total": len(root), "unique_ids": len(ids),
        "duplicate_id_count": len(duplicates),
        "duplicate_rows": sum(count - 1 for _, count in duplicates),
        "duplicate_ids": [{"xml_id": identifier, "count": count} for identifier, count in duplicates[:MAX_SAMPLES]],
        "duplicate_ids_omitted": max(0, len(duplicates) - MAX_SAMPLES),
        **{key: counts[key] for key in (
            "missing_id", "missing_title", "missing_description", "missing_images", "missing_tnved",
        )},
        "samples": samples,
    }, set(ids)


def stock_report(payload, feed_ids):
    root = _root(payload, "items")
    values = {}
    invalid_ids = set()
    invalid_rows = 0
    for item in root:
        id_nodes, stock_nodes = item.findall("id"), item.findall("stock")
        identifiers = {
            value for node in id_nodes
            if (value := _identifier("".join(node.itertext()).strip()))
        }
        if (len(id_nodes) != 1 or len(stock_nodes) != 1 or len(identifiers) != 1
                or len(id_nodes[0]) or len(stock_nodes[0])):
            invalid_rows += 1
            invalid_ids.update(identifiers)
            for identifier in identifiers:
                values.pop(identifier, None)
            continue
        identifier = next(iter(identifiers))
        raw = (stock_nodes[0].text or "").strip()
        if identifier in values or identifier in invalid_ids or not re.fullmatch(r"-?[0-9]{1,9}", raw):
            invalid_ids.add(identifier)
            values.pop(identifier, None)
            invalid_rows += 1
            continue
        values[identifier] = int(raw)
    matched = feed_ids & values.keys()
    nonpositive = sorted(identifier for identifier in matched if values[identifier] <= 0)
    return {
        "complete": True, "total": len(root), "matched": len(matched),
        "unknown": len(feed_ids - matched),
        "invalid_rows": invalid_rows,
        "nonpositive_count": len(nonpositive),
        "nonpositive_ids": nonpositive[:MAX_SAMPLES],
        "nonpositive_omitted": max(0, len(nonpositive) - MAX_SAMPLES),
    }


def fetch_report():
    report, ids = build_report(_read_xml(FEED_PATH))
    try:
        report["stock"] = stock_report(_read_xml(STOCK_PATH), ids)
    except AvitoError as exc:
        code = str(exc) if str(exc) in ERRORS else "source_unavailable"
        report["stock"] = {"complete": False, "code": code, "detail": failure_detail(code)}
    return report
