"""Read authenticated provider input copies without exposing their private URLs or XML."""
from io import BytesIO
import hashlib
import http.client
import json
import re
import socket
import threading
import time
from urllib.parse import quote, urlsplit
from xml.etree import ElementTree as ET
from zipfile import ZipFile, BadZipFile
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from datetime import timedelta

from .avito_feed_models import AvitoServicesFeed
from .communication_models import ChannelConnection

MAX_BYTES = 4 * 1024 * 1024
MAX_ITEMS = 200
MAX_SECONDS = 8
MAX_XML_NODES = 50000
MAX_XML_DEPTH = 20
SOURCE = 'provider_report_input_copy'
FIXED_CODES = {
    'input_copy_xml_verified', 'input_copy_scope_unavailable', 'input_copy_report_ineligible',
    'input_copy_url_count', 'input_copy_url_invalid', 'input_copy_host_not_allowed',
    'input_copy_address_unavailable', 'input_copy_budget_exhausted', 'input_copy_redirect_denied',
    'input_copy_encoding_unsupported', 'input_copy_size_invalid', 'input_copy_too_large',
    'input_copy_incomplete', 'input_copy_transport_failed', 'input_copy_xml_forbidden',
    'input_copy_xml_invalid', 'input_copy_xml_structure', 'input_copy_xml_limit',
    'input_copy_not_xml', 'input_copy_identity_mismatch', 'input_copy_source_changed',
    'input_copy_report_changed', 'input_copy_report_recheck_failed', 'input_copy_check_failed',
}

class CopyUnavailable(ValueError):
    def __init__(self, code, *, content_kind='unknown', unsupported=False):
        super().__init__(code)
        self.code, self.content_kind, self.unsupported = code, content_kind, unsupported

def unavailable(code, *, content_kind='unknown', unsupported=False):
    return {'source': SOURCE, 'state': 'unsupported' if unsupported else 'unavailable',
        'code': code, 'content_kind': content_kind, 'identity_count': 0, 'byte_count': 0}

def bound_services_feed(connection, account_id):
    """Identify a services source independently of opt-in, so goods audits never apply."""
    if connection is None or str(getattr(connection, 'external_id', '')) != str(account_id):
        return None
    current = ChannelConnection.objects.select_related('channel').filter(pk=connection.pk).first()
    if (current is None or not current.is_active or not current.channel.is_active
            or current.channel.kind != 'avito' or current.external_id != str(account_id)
            or current.external_id != connection.external_id or current.channel_id != connection.channel_id
            or current.channel.organization_id != connection.channel.organization_id):
        return None
    feed = AvitoServicesFeed.objects.select_related('connection__channel').filter(connection=current,
        account_id=current.external_id, organization_id_at_import=current.channel.organization_id).first()
    if feed is None or not isinstance(feed.draft, list):
        return None
    count = 0
    for sheet in feed.draft:
        if not isinstance(sheet, dict) or not isinstance(sheet.get('rows'), list):
            return None
        for row in sheet['rows']:
            count += 1
            if count > MAX_ITEMS or not isinstance(row, dict) or row.get('Category') != 'Предложение услуг':
                return None
    return feed if count > 0 else None

def services_input_scope(connection, account_id):
    """The optional network read needs existing explicit, still-authorized enrollment."""
    feed = bound_services_feed(connection, account_id)
    if feed is None or not feed.enabled or not isinstance(feed.published, list) or not any(
            isinstance(sheet, dict) and isinstance(sheet.get('rows'), list) and sheet['rows']
            for sheet in feed.published):
        return False
    current = feed.connection
    from . import avito_autoload_auto
    from .avito_services_workplace_setup import _actor, _binding, _same
    metadata = current.settings if isinstance(current.settings, dict) else {}
    marker = metadata.get('avito_services_workplace_v1')
    if (not isinstance(marker, dict) or marker.get('automatic_reports_configured_once') is not True
            or not _same(current, {key: marker.get(key) for key in _binding(current)})):
        return False
    state = avito_autoload_auto._state(current)
    if state.get('enabled') is not True or not avito_autoload_auto._allowed(current, state):
        return False
    try:
        _actor(current)
    except Exception:
        return False
    return True

def identity_sha256(pairs):
    return hashlib.sha256(json.dumps(sorted(pairs), ensure_ascii=False,
        separators=(',', ':')).encode('utf-8')).hexdigest()

def _expected_pairs(report):
    rows = report.get('rows')
    upload_id = report.get('upload_id')
    try:
        started = parse_datetime(report.get('started_at')) if isinstance(report.get('started_at'), str) else None
    except ValueError:
        started = None
    if (report.get('kind') not in {'current', 'last_successful'}
            or not isinstance(upload_id, str) or not re.fullmatch(r'[1-9][0-9]{0,31}', upload_id)
            or started is None or timezone.is_naive(started) or started > timezone.now() + timedelta(minutes=5)
            or report.get('complete') is not True or report.get('upload_finished') is not True
            or report.get('upload_status') not in {'success', 'success_warning'}
            or not isinstance(rows, list) or not 1 <= len(rows) <= MAX_ITEMS
            or type(report.get('total')) is not int or report['total'] != len(rows)):
        raise CopyUnavailable('input_copy_report_ineligible')
    pairs, xml_ids, avito_ids = set(), set(), set()
    for row in rows:
        xml_id, avito_id = (row.get('ad_id'), row.get('avito_id')) if isinstance(row, dict) else (None, None)
        if (not isinstance(xml_id, str) or not 1 <= len(xml_id) <= 100 or xml_id != xml_id.strip()
                or any(ord(c) < 32 for c in xml_id) or xml_id in xml_ids
                or type(avito_id) is not int or not 0 < avito_id < 10**32 or avito_id in avito_ids):
            raise CopyUnavailable('input_copy_report_ineligible')
        pairs.add((xml_id, str(avito_id)))
        xml_ids.add(xml_id)
        avito_ids.add(avito_id)
    return pairs

def _copy_url(summary):
    feeds = summary.get('feed_urls') if isinstance(summary, dict) else None
    if not isinstance(feeds, list) or len(feeds) != 1:
        raise CopyUnavailable('input_copy_url_count')
    entry = feeds[0]
    value = entry.get('url') if isinstance(entry, dict) else entry if isinstance(entry, str) else None
    if not isinstance(value, str) or not 1 <= len(value) <= 2000 or any(
            ord(c) < 33 or c.isspace() for c in value):
        raise CopyUnavailable('input_copy_url_invalid')
    try:
        part = urlsplit(value)
        host = (part.hostname or '').encode('idna').decode('ascii').lower()
        if (part.scheme != 'https' or part.username is not None or part.password is not None
                or part.fragment or part.port not in (None, 443) or not host or '%' in host):
            raise ValueError
    except (ValueError, UnicodeError):
        raise CopyUnavailable('input_copy_url_invalid') from None
    if not any(host == domain or host.endswith('.' + domain) for domain in ('avito.ru', 'avito.st')):
        raise CopyUnavailable('input_copy_host_not_allowed')
    return part, host

def _download(summary, deadline):
    # The first-party report URL guard is deliberately separate from transport.
    part, host = _copy_url(summary)
    return _download_url(part, host, deadline)


def _download_url(part, host, deadline):
    """Internal transport: callers must validate a fixed, trusted source first.

    Report copies use _copy_url's Avito-domain allowlist. Immutable source checks
    use a server-generated version_url, never a URL accepted from a request.
    No caller can supply Authorization or ambient cookies.
    """
    from .avito_services_media import _resolve, _PinnedHTTPS, _public_ip
    if time.monotonic() >= deadline:
        raise CopyUnavailable('input_copy_budget_exhausted')
    try:
        address = _resolve(host, 443, deadline)
    except (ValueError, OSError):
        raise CopyUnavailable('input_copy_address_unavailable') from None
    if not _public_ip(address):
        raise CopyUnavailable('input_copy_address_unavailable')
    timeout = max(.05, min(3, deadline - time.monotonic()))
    connection = _PinnedHTTPS(host, 443, address, timeout)
    connection.auto_open = 0
    response, aborted = None, threading.Event()
    def abort():
        aborted.set()
        sock = getattr(connection, '_pinned_socket', None) or connection.sock
        if sock is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        connection.close()
    timer = threading.Timer(max(.05, deadline - time.monotonic()), abort)
    timer.daemon = True
    timer.start()
    try:
        if aborted.is_set() or time.monotonic() >= deadline:
            raise CopyUnavailable('input_copy_budget_exhausted')
        connection.connect()
        if aborted.is_set() or time.monotonic() >= deadline:
            raise CopyUnavailable('input_copy_budget_exhausted')
        path = quote(part.path or '/', safe="/%:@!$&'()*+,;=-._~")
        if part.query:
            path += '?' + quote(part.query, safe="/%?:@!$&'()*+,;=-._~")
        connection.request('GET', path, headers={'Host': host,
            'Accept': 'application/xml,text/xml,application/octet-stream',
            'Accept-Encoding': 'identity', 'User-Agent': 'Service2-AvitoInputCheck/1'})
        response = connection.getresponse()
        if 300 <= response.status < 400:
            raise CopyUnavailable('input_copy_redirect_denied')
        if response.status != 200:
            status = response.status if type(response.status) is int and 100 <= response.status <= 599 else 500
            raise CopyUnavailable('input_copy_http_' + str(status))
        if response.getheader('Content-Encoding', 'identity').strip().lower() not in ('', 'identity'):
            raise CopyUnavailable('input_copy_encoding_unsupported', unsupported=True)
        length = response.getheader('Content-Length')
        if length is not None and (not length.isascii() or not length.isdecimal()
                or len(length) > 10 or not 0 < int(length) <= MAX_BYTES):
            raise CopyUnavailable('input_copy_size_invalid')
        chunks, received = [], 0
        while True:
            if aborted.is_set() or time.monotonic() >= deadline:
                raise CopyUnavailable('input_copy_budget_exhausted')
            chunk = response.read1(min(65536, MAX_BYTES + 1 - received))
            if not chunk:
                break
            chunks.append(chunk)
            received += len(chunk)
            if received > MAX_BYTES:
                raise CopyUnavailable('input_copy_too_large')
        if length is not None and received != int(length):
            raise CopyUnavailable('input_copy_incomplete')
        if aborted.is_set() or time.monotonic() >= deadline:
            raise CopyUnavailable('input_copy_budget_exhausted')
        return b''.join(chunks)
    except (OSError, http.client.HTTPException, UnicodeError):
        raise CopyUnavailable('input_copy_transport_failed') from None
    finally:
        timer.cancel()
        if response is not None:
            response.close()
        connection.close()

def _xml_pairs(raw, deadline):
    if not raw or len(raw) > MAX_BYTES:
        raise CopyUnavailable('input_copy_size_invalid')
    if time.monotonic() >= deadline:
        raise CopyUnavailable('input_copy_budget_exhausted')
    if raw.startswith(b'PK\x03\x04'):
        kind = 'other'
        try:
            with ZipFile(BytesIO(raw)) as archive:
                names = archive.namelist()
                if len(names) <= 500 and '[Content_Types].xml' in names and 'xl/workbook.xml' in names:
                    kind = 'xlsx'
        except (BadZipFile, OSError):
            pass
        raise CopyUnavailable('input_copy_not_xml', content_kind=kind, unsupported=True)
    if raw.startswith(b'\xd0\xcf\x11\xe0'):
        raise CopyUnavailable('input_copy_not_xml', content_kind='other', unsupported=True)
    safe_scan = raw.replace(b'\x00', b'').upper()
    if b'<!DOCTYPE' in safe_scan or b'<!ENTITY' in safe_scan:
        raise CopyUnavailable('input_copy_xml_forbidden', content_kind='xml')
    try:
        events = ET.iterparse(BytesIO(raw), events=('start', 'end'))
        depth, nodes = 0, 0
        for event, _node in events:
            if time.monotonic() >= deadline:
                raise CopyUnavailable('input_copy_budget_exhausted')
            if event == 'start':
                depth, nodes = depth + 1, nodes + 1
                if depth > MAX_XML_DEPTH or nodes > MAX_XML_NODES:
                    raise CopyUnavailable('input_copy_xml_limit', content_kind='xml')
            else:
                depth -= 1
        root = events.root
    except ET.ParseError:
        raise CopyUnavailable('input_copy_not_xml', content_kind='other', unsupported=True) from None
    if (root.tag != 'Ads' or root.get('formatVersion') != '3' or root.get('target') != 'Avito.ru'
            or not 1 <= len(root) <= MAX_ITEMS or any(ad.tag != 'Ad' for ad in root)):
        raise CopyUnavailable('input_copy_xml_structure', content_kind='xml', unsupported=True)
    pairs, xml_ids, avito_ids = set(), set(), set()
    for ad in root:
        ids, provider_ids = ad.findall('Id'), ad.findall('AvitoId')
        if len(ids) != 1 or len(provider_ids) != 1:
            raise CopyUnavailable('input_copy_identity_mismatch', content_kind='xml')
        xml_id, avito_id = ids[0].text, provider_ids[0].text
        if (not isinstance(xml_id, str) or not 1 <= len(xml_id) <= 100 or xml_id != xml_id.strip()
                or xml_id in xml_ids or not isinstance(avito_id, str)
                or not re.fullmatch(r'[1-9][0-9]{0,31}', avito_id) or avito_id in avito_ids):
            raise CopyUnavailable('input_copy_identity_mismatch', content_kind='xml')
        pairs.add((xml_id, avito_id))
        xml_ids.add(xml_id)
        avito_ids.add(avito_id)
    return pairs

def fetch_input_proof(summary, report, connection, account_id, *, deadline):
    """Optional provider read; every failure preserves the validated report."""
    if not services_input_scope(connection, account_id):
        return unavailable('input_copy_scope_unavailable')
    try:
        expected = _expected_pairs(report)
        until = min(deadline, time.monotonic() + MAX_SECONDS)
        if until - time.monotonic() < .1:
            raise CopyUnavailable('input_copy_budget_exhausted')
        raw = _download(summary, until)
        received = _xml_pairs(raw, until)
        if expected != received:
            raise CopyUnavailable('input_copy_identity_mismatch', content_kind='xml')
        if not services_input_scope(connection, account_id):
            return unavailable('input_copy_scope_unavailable')
        return {'source': SOURCE, 'state': 'verified_xml', 'code': 'input_copy_xml_verified',
            'content_kind': 'xml', 'identity_count': len(received), 'byte_count': len(raw),
            'file_sha256': hashlib.sha256(raw).hexdigest(), 'identity_sha256': identity_sha256(received),
            'connection_id': connection.pk, 'account_id': str(account_id),
            'organization_id': connection.channel.organization_id,
            'upload_id': report['upload_id'], 'report_started_at': report['started_at']}
    except CopyUnavailable as error:
        return unavailable(error.code, content_kind=error.content_kind, unsupported=error.unsupported)
    except (TypeError, ValueError, KeyError):
        return unavailable('input_copy_report_ineligible')
