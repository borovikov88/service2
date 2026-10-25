"""Validated, account-bound image assets and bounded public-image checks for Avito."""
from io import BytesIO
import hashlib
import http.client
import ipaddress
import queue
import re
import socket
import threading
import time
from urllib.parse import quote, unquote, urlsplit
import warnings
from zoneinfo import ZoneInfo

from PIL import Image, ImageOps, UnidentifiedImageError
from django.contrib.auth.decorators import login_required
from django.core.files.base import ContentFile
from django.db import transaction, connection as db_connection
from django.http import FileResponse, Http404, JsonResponse
from django.shortcuts import get_object_or_404
from django.urls import reverse
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET, require_POST

from .avito_media_models import AvitoServicesImage
from .avito_feed_models import AvitoServicesFeed

MAX_BYTES = 8 * 1024 * 1024
MAX_PIXELS = 20_000_000
MAX_ASSETS = 1000
MAX_SCOPE_BYTES = 512 * 1024 * 1024
PUBLIC_PREFIX = '/api/communications/avito/services-media/'
_DNS_SLOTS = threading.BoundedSemaphore(4)

TRANSPORT_REASONS = frozenset({
    'url_invalid', 'url_policy', 'private_address', 'dns_busy', 'dns_timeout',
    'dns_failed', 'dns_private_address', 'dns_unavailable', 'deadline', 'http_status',
    'content_type', 'content_length_invalid', 'content_length_limit', 'body_limit',
    'incomplete_body', 'tls_certificate', 'tls_error', 'io_timeout', 'io_error',
    'http_protocol', 'request_encoding',
    'upgrade_source_scheme', 'upgrade_location_missing', 'upgrade_location_multiple',
    'upgrade_location_invalid', 'upgrade_location_controls', 'upgrade_location_fragment',
    'upgrade_location_userinfo', 'upgrade_target_scheme', 'upgrade_target_port',
    'upgrade_target_host', 'upgrade_target_path', 'upgrade_target_query', 'upgrade_second_hop',
})
TRANSPORT_PHASES = frozenset({'url', 'dns', 'connect', 'request', 'headers', 'body'})
TRANSPORT_FIELDS = ('reason', 'phase', 'http_status', 'source_scheme', 'path_ascii',
    'dns_family', 'dns_answer_count', 'selected_ip_family', 'response_format',
    'declared_bytes', 'received_bytes', 'io_errno', 'https_upgrade_followed', 'host_relation')
TRANSPORT_FORMATS = frozenset({'jpeg', 'png', 'webp', 'avif', 'html', 'json', 'octet_stream', 'other'})



class _PhotoTransportError(ValueError):
    """Fixed machine-readable diagnostics; user-facing messages remain unchanged."""
    def __init__(self, message, *, reason, phase, http_status=None):
        if reason not in TRANSPORT_REASONS or phase not in TRANSPORT_PHASES:
            raise ValueError('invalid photo transport identifier')
        super().__init__(message)
        self.reason, self.phase = reason, phase
        self.http_status = http_status if type(http_status) is int and 100 <= http_status <= 599 else None


def _transport_metadata(values):
    result = {}
    values = values if isinstance(values, dict) else {}
    for key, allowed in (
            ('source_scheme', {'http', 'https'}), ('dns_family', {'ipv4', 'ipv6', 'mixed'}),
            ('selected_ip_family', {'ipv4', 'ipv6'}), ('response_format', TRANSPORT_FORMATS),
            ('host_relation', {'same', 'www_alias', 'other', 'unknown'})):
        value = values.get(key)
        if isinstance(value, str) and value in allowed:
            result[key] = value
    for key in ('path_ascii', 'https_upgrade_followed'):
        if type(values.get(key)) is bool:
            result[key] = values[key]
    for key, lower, upper in (('dns_answer_count', 1, 32),
            ('declared_bytes', 0, MAX_BYTES + 1), ('received_bytes', 0, MAX_BYTES + 1),
            ('io_errno', 0, 4095)):
        value = values.get(key)
        if type(value) is int and lower <= value <= upper:
            result[key] = value
    return result


def photo_transport_failure(error):
    """Never export exception text, URL, response headers or body."""
    result = {'reason': None, 'phase': None, 'http_status': None}
    if isinstance(error, _PhotoTransportError):
        result.update(reason=error.reason, phase=error.phase, http_status=error.http_status)
        result.update(_transport_metadata(getattr(error, 'diagnostics', {})))
    return result


def _response_format(content_type):
    return {'image/jpeg': 'jpeg', 'image/png': 'png', 'image/webp': 'webp',
        'image/avif': 'avif', 'text/html': 'html', 'application/json': 'json',
        'application/octet-stream': 'octet_stream'}.get(content_type, 'other')


def _origin():
    from .avito_services_publication import public_origin
    return public_origin()


def _parse_url(value):
    if not isinstance(value, str) or len(value) > 2000 or any(ord(c) < 33 or c.isspace() for c in value):
        raise _PhotoTransportError('Некорректная ссылка на фотографию.', reason='url_invalid', phase='url')
    try:
        parts = urlsplit(value)
        host = (parts.hostname or '').encode('idna').decode('ascii').lower()
        port = parts.port if parts.port is not None else (443 if parts.scheme == 'https' else 80)
        if (parts.scheme not in {'https', 'http'} or not host or parts.username is not None
                or parts.password is not None or parts.fragment or '%' in host or port != (443 if parts.scheme == 'https' else 80)):
            raise ValueError
    except (ValueError, UnicodeError):
        raise _PhotoTransportError('Разрешены ссылки HTTP/HTTPS без пароля, фрагмента и нестандартного порта.', reason='url_policy', phase='url') from None
    return parts, host, port


def _public_ip(value):
    try:
        address = ipaddress.ip_address(value)
        if isinstance(address, ipaddress.IPv6Address) and (
                address.sixtofour is not None or address.teredo is not None
                or address in ipaddress.ip_network('64:ff9b::/96')
                or address in ipaddress.ip_network('64:ff9b:1::/48')
                or address in ipaddress.ip_network('::/96')):
            return False
        address = getattr(address, 'ipv4_mapped', None) or address
        return bool(address.is_global and not (address.is_multicast or address.is_reserved
                    or address.is_unspecified or address.is_loopback or address.is_link_local
                    or getattr(address, 'is_site_local', False)))
    except ValueError:
        return False


def _resolve(host, port, deadline, *, diagnostics=None):
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        literal = None
    if literal is not None:
        if not _public_ip(host):
            raise _PhotoTransportError('Локальные и служебные адреса фотографий запрещены.', reason='private_address', phase='dns')
        if diagnostics is not None:
            diagnostics.update(dns_family='ipv6' if ':' in host else 'ipv4', dns_answer_count=1)
        return host
    if host.rstrip('.').endswith(('.localhost', '.local', '.internal')) or host.rstrip('.') == 'localhost':
        raise _PhotoTransportError('Локальные и служебные адреса фотографий запрещены.', reason='private_address', phase='dns')
    if not _DNS_SLOTS.acquire(blocking=False):
        raise _PhotoTransportError('Проверка фотографий занята. Повторите позже.', reason='dns_busy', phase='dns')
    result = queue.Queue(maxsize=1)
    def lookup():
        try:
            result.put((True, socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)))
        except (OSError, ValueError):
            result.put((False, None))
        finally:
            _DNS_SLOTS.release()
    try:
        threading.Thread(target=lookup, daemon=True).start()
    except RuntimeError:
        _DNS_SLOTS.release()
        raise _PhotoTransportError('Не удалось проверить адрес фотографии.', reason='dns_unavailable', phase='dns') from None
    try:
        ok, answers = result.get(timeout=max(.05, min(3, deadline - time.monotonic())))
    except queue.Empty:
        raise _PhotoTransportError('Не удалось вовремя проверить адрес фотографии.', reason='dns_timeout', phase='dns') from None
    if not ok or not answers or len(answers) > 32:
        raise _PhotoTransportError('Не удалось проверить адрес фотографии.', reason='dns_failed', phase='dns')
    addresses = [entry[4][0] for entry in answers]
    if not all(_public_ip(address) for address in addresses):
        raise _PhotoTransportError('Локальные и служебные адреса фотографий запрещены.', reason='dns_private_address', phase='dns')
    if diagnostics is not None:
        families = {'ipv6' if ':' in address else 'ipv4' for address in addresses}
        diagnostics.update(dns_family=next(iter(families)) if len(families) == 1 else 'mixed',
                           dns_answer_count=len(answers))
    return addresses[0]


def _numeric_socket(address, port, timeout):
    sock = socket.socket(socket.AF_INET6 if ':' in address else socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.settimeout(timeout)
        sock.connect((address, port))
        return sock
    except Exception:
        sock.close()
        raise


class _PinnedHTTP(http.client.HTTPConnection):
    def __init__(self, host, port, address, timeout):
        super().__init__(host, port, timeout=timeout)
        self.address = address

    def connect(self):
        self.sock = _numeric_socket(self.address, self.port, self.timeout)
        self._pinned_socket = self.sock


class _PinnedHTTPS(http.client.HTTPSConnection):
    def __init__(self, host, port, address, timeout):
        super().__init__(host, port, timeout=timeout)
        self.address = address

    def connect(self):
        raw = _numeric_socket(self.address, self.port, self.timeout)
        try:
            self.sock = self._context.wrap_socket(raw, server_hostname=self.host, do_handshake_on_connect=False)
            self._pinned_socket = self.sock
            self.sock.do_handshake()
        except Exception:
            raw.close()
            raise


class _PhotoInputError(ValueError):
    pass


def _image(raw, *, normalize=True):
    if not raw or len(raw) > MAX_BYTES:
        raise ValueError('Размер фотографии должен быть не больше 8 МБ.')
    try:
        with warnings.catch_warnings():
            warnings.simplefilter('error', Image.DecompressionBombWarning)
            with Image.open(BytesIO(raw)) as image:
                if (image.format not in {'JPEG', 'PNG', 'WEBP'} or not 0 < image.width <= 10000
                        or not 0 < image.height <= 10000 or image.width * image.height > MAX_PIXELS
                        or getattr(image, 'n_frames', 1) != 1):
                    raise _PhotoInputError('Нужно обычное JPEG, PNG или WebP: до 20 мегапикселей, без анимации.')
                image.verify()
            with Image.open(BytesIO(raw)) as image:
                image.load()
                size = image.size
                if not normalize:
                    return raw, size
                image = ImageOps.exif_transpose(image).convert('RGBA')
                background = Image.new('RGB', image.size, 'white')
                background.paste(image, mask=image.getchannel('A'))
                out = BytesIO()
                background.save(out, format='JPEG', quality=88)
                encoded = out.getvalue()
                if len(encoded) > MAX_BYTES:
                    raise _PhotoInputError('Фотография после обработки больше 8 МБ. Уменьшите её размер.')
                return encoded, background.size
    except _PhotoInputError:
        raise
    except (UnidentifiedImageError, OSError, SyntaxError, ValueError, Image.DecompressionBombError,
            Image.DecompressionBombWarning):
        raise ValueError('Файл не является доступной фотографией JPEG, PNG или WebP.') from None


def _image_host_relation(source, target):
    """Only identity or a single www DNS alias; no suffix/registry/IP folding."""
    if source == target:
        return 'same'
    if target == 'www.' + source:
        base = source
    elif source == 'www.' + target:
        base = target
    else:
        return 'other'
    if (base == 'www' or base.startswith('www.') or base.endswith('.') or len(base) > 253
            or re.fullmatch(r'[0-9.]+', base)
            or re.fullmatch(r'(?:0x[0-9a-f]+|[0-9]+)(?:\.(?:0x[0-9a-f]+|[0-9]+)){0,3}', base)
            or not all(re.fullmatch(r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?', label)
                       for label in base.split('.'))):
        return 'other'
    try:
        ipaddress.ip_address(base)
    except ValueError:
        return 'www_alias'
    return 'other'


def _image_https_upgrade(parts, host, port, response, diagnostics, *, upgraded):
    """One image-only canonical upgrade, never general redirect handling."""
    if response.status not in (301, 308):
        return None
    def reject(reason):
        raise _PhotoTransportError(
            'Фотография недоступна: нужен прямой URL с ответом HTTP 200 без перенаправлений.',
            reason=reason, phase='headers', http_status=response.status)
    if upgraded:
        reject('upgrade_second_hop')
    if parts.scheme != 'http' or port != 80:
        reject('upgrade_source_scheme')
    diagnostics['host_relation'] = 'unknown'
    locations = [value for key, value in response.getheaders() if key.lower() == 'location']
    if not locations:
        reject('upgrade_location_missing')
    if len(locations) != 1:
        reject('upgrade_location_multiple')
    value = locations[0]
    if not isinstance(value, str) or not value or len(value) > 2000:
        reject('upgrade_location_invalid')
    if any(ord(char) < 33 or 127 <= ord(char) <= 159 or char.isspace() for char in value):
        reject('upgrade_location_controls')
    if '#' in value:
        reject('upgrade_location_fragment')
    try:
        target = urlsplit(value)
        target_host = (target.hostname or '').encode('idna').decode('ascii').lower()
        target_port = target.port
    except (ValueError, UnicodeError):
        reject('upgrade_location_invalid')
    if target.username is not None or target.password is not None:
        reject('upgrade_location_userinfo')
    if target.scheme != 'https' or not target.netloc or not target_host:
        reject('upgrade_target_scheme')
    diagnostics['host_relation'] = _image_host_relation(host, target_host)
    if target_port not in (None, 443):
        reject('upgrade_target_port')
    if diagnostics['host_relation'] not in {'same', 'www_alias'}:
        reject('upgrade_target_host')
    if target.path != parts.path:
        reject('upgrade_target_path')
    if target.query != parts.query:
        reject('upgrade_target_query')
    # Apply the unchanged URL policy as well, before any second DNS/GET.
    try:
        _parse_url(value)
    except ValueError:
        reject('upgrade_location_invalid')
    return value


def _download(value, deadline, *, diagnostics=None):
    observed = {}
    try:
        return _download_image(value, deadline, observed)
    except _PhotoTransportError as error:
        error.diagnostics = observed
        raise
    finally:
        if diagnostics is not None:
            # This observer contains only closed, sanitized metadata.
            diagnostics.update(_transport_metadata(observed))


def _download_image(value, deadline, diagnostics, *, upgraded=False):
    parts, host, port = _parse_url(value)
    diagnostics.setdefault('source_scheme', parts.scheme)
    diagnostics.setdefault('path_ascii', parts.path.isascii())
    diagnostics.setdefault('https_upgrade_followed', False)
    if time.monotonic() >= deadline:
        raise _PhotoTransportError('Проверка фотографий заняла слишком много времени. Загрузите изображения в Service2.', reason='deadline', phase='url')
    address = _resolve(host, port, deadline, diagnostics=diagnostics)
    diagnostics['selected_ip_family'] = 'ipv6' if ':' in address else 'ipv4'
    timeout = max(.05, min(3, deadline - time.monotonic()))
    connection = (_PinnedHTTPS if parts.scheme == 'https' else _PinnedHTTP)(host, port, address, timeout)
    response = None
    aborted = threading.Event()
    connection.auto_open = 0
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
    phase, status = 'connect', None
    try:
        if aborted.is_set() or time.monotonic() >= deadline:
            raise _PhotoTransportError('Проверка фотографий заняла слишком много времени.', reason='deadline', phase=phase)
        connection.connect()
        if aborted.is_set() or time.monotonic() >= deadline:
            raise _PhotoTransportError('Проверка фотографий заняла слишком много времени.', reason='deadline', phase=phase)
        phase = 'request'
        path = quote(parts.path or '/', safe="/%:@!$&'()*+,;=-._~")
        if parts.query:
            path += '?' + quote(parts.query, safe="/%?:@!$&'()*+,;=-._~")
        authority = '[' + host + ']' if ':' in host else host
        connection.request('GET', path, headers={'Host': authority,
            'Accept': 'image/jpeg,image/png,image/webp', 'User-Agent': 'Service2-AvitoImageCheck/1'})
        if upgraded:
            diagnostics['https_upgrade_followed'] = True
        phase = 'headers'
        response = connection.getresponse()
        status = response.status
        content_type = response.getheader('Content-Type', '').split(';', 1)[0].strip().lower()
        diagnostics['response_format'] = _response_format(content_type)
        target = _image_https_upgrade(parts, host, port, response, diagnostics, upgraded=upgraded)
        if target is not None:
            # Release the HTTP response/socket before the fresh target DNS lookup.
            # Never consume a redirect body or transfer any response cookies.
            response.close()
            response = None
            timer.cancel()
            connection.close()
            for key in ('dns_family', 'dns_answer_count', 'selected_ip_family',
                        'response_format', 'declared_bytes', 'received_bytes', 'io_errno'):
                diagnostics.pop(key, None)
            return _download_image(target, deadline, diagnostics, upgraded=True)
        if response.status != 200:
            raise _PhotoTransportError('Фотография недоступна: нужен прямой URL с ответом HTTP 200 без перенаправлений.', reason='http_status', phase=phase, http_status=status)
        if content_type not in {'image/jpeg', 'image/png', 'image/webp'}:
            raise _PhotoTransportError('Ссылка не возвращает фотографию JPEG, PNG или WebP.', reason='content_type', phase=phase, http_status=status)
        length = response.getheader('Content-Length')
        if length is not None:
            if len(length) <= 10 and length.isascii() and length.isdecimal():
                diagnostics['declared_bytes'] = min(MAX_BYTES + 1, int(length))
            if len(length) > 10 or not length.isascii() or not length.isdecimal() or int(length) <= 0:
                raise _PhotoTransportError('Некорректный или слишком большой размер фотографии.', reason='content_length_invalid', phase=phase, http_status=status)
            if int(length) > MAX_BYTES:
                raise _PhotoTransportError('Некорректный или слишком большой размер фотографии.', reason='content_length_limit', phase=phase, http_status=status)
        phase = 'body'
        chunks, received = [], 0
        diagnostics['received_bytes'] = 0
        while True:
            if time.monotonic() >= deadline:
                raise _PhotoTransportError('Проверка фотографий заняла слишком много времени. Загрузите изображения в Service2.', reason='deadline', phase=phase, http_status=status)
            chunk = response.read1(min(65536, MAX_BYTES + 1 - received))
            if not chunk:
                break
            chunks.append(chunk)
            received += len(chunk)
            diagnostics['received_bytes'] = min(MAX_BYTES + 1, received)
            if received > MAX_BYTES:
                raise _PhotoTransportError('Размер фотографии должен быть не больше 8 МБ.', reason='body_limit', phase=phase, http_status=status)
        if length is not None and received != int(length):
            raise _PhotoTransportError('Фотография получена не полностью. Повторите проверку или загрузите файл.', reason='incomplete_body', phase=phase, http_status=status)
        if time.monotonic() >= deadline:
            raise _PhotoTransportError('Проверка фотографий заняла слишком много времени.', reason='deadline', phase=phase, http_status=status)
        return b''.join(chunks)
    except (OSError, http.client.HTTPException, UnicodeError) as error:
        import ssl
        errno = getattr(error, 'errno', None)
        if type(errno) is int and 0 <= errno <= 4095:
            diagnostics['io_errno'] = errno
        reason = ('deadline' if aborted.is_set() or time.monotonic() >= deadline else
            'tls_certificate' if isinstance(error, ssl.SSLCertVerificationError) else
            'tls_error' if isinstance(error, ssl.SSLError) else
            'io_timeout' if isinstance(error, TimeoutError) else
            'http_protocol' if isinstance(error, http.client.HTTPException) else
            'request_encoding' if isinstance(error, UnicodeError) else 'io_error')
        raise _PhotoTransportError('Не удалось скачать фотографию по указанной ссылке.',
            reason=reason, phase=phase, http_status=status) from None
    finally:
        timer.cancel()
        if response is not None:
            response.close()
        connection.close()


def _asset(connection, value):
    parts, host, port = _parse_url(value)
    own_host = urlsplit(_origin()).hostname
    looks_own = unquote(parts.path).startswith(PUBLIC_PREFIX) or host.rstrip('.') == own_host
    if not looks_own:
        return None
    match = re.fullmatch(re.escape(PUBLIC_PREFIX) + r'([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})/photo\.jpg', parts.path)
    if not match:
        raise ValueError('Недоступная фотография Service2 для этого аккаунта.')
    asset = AvitoServicesImage.objects.filter(public_key=match[1], connection=connection,
        organization_id_at_upload=connection.channel.organization_id, account_id=connection.external_id,
        public_url=value).first()
    if asset is None:
        raise ValueError('Недоступная фотография Service2 для этого аккаунта.')
    return asset


def _asset_path(asset):
    expected = f'avito_services_images/{asset.organization_id_at_upload}/{asset.connection_id}/{asset.public_key.hex}.jpg'
    if asset.file.name != expected:
        raise ValueError('Сохранённая фотография недоступна.')
    if not asset.file.storage.exists(asset.file.name):
        raise ValueError('Сохранённая фотография недоступна.')


def _read_asset(asset):
    _asset_path(asset)
    try:
        with asset.file.storage.open(asset.file.name, 'rb') as stream:
            raw = stream.read(MAX_BYTES + 1)
    except (OSError, ValueError):
        raise ValueError('Сохранённая фотография недоступна.') from None
    if hashlib.sha256(raw).hexdigest() != asset.sha256:
        raise ValueError('Сохранённая фотография повреждена. Загрузите её заново.')
    _image(raw, normalize=False)
    return raw


def validate_photo_scope(connection, value, *, max_count=10):
    from .avito_services_feed import images
    cleaned = images(value, max_count=max_count)
    for url in cleaned.split(' | '):
        asset = _asset(connection, url)
        if asset is not None:
            _asset_path(asset)
        else:
            _, host, _ = _parse_url(url)
            try:
                literal = ipaddress.ip_address(host)
            except ValueError:
                literal = None
            if literal is not None and not _public_ip(host):
                raise ValueError('Локальные и служебные адреса фотографий запрещены.')
    return cleaned


def _cache_scope(connection, current):
    return (current.is_active and current.channel.is_active and current.channel.kind == 'avito'
        and current.external_id == connection.external_id and current.channel_id == connection.channel_id
        and current.channel.organization_id == connection.channel.organization_id)


def _fresh_check(value):
    if not isinstance(value, dict):
        return False
    try:
        checked = parse_datetime(value.get('checked_at', ''))
        return (checked is not None and timezone.is_aware(checked)
                and 0 <= (timezone.now() - checked).total_seconds() <= 900
                and type(value.get('bytes')) is int and 0 < value['bytes'] <= MAX_BYTES
                and isinstance(value.get('sha256'), str)
                and re.fullmatch(r'[0-9a-f]{64}', value['sha256']) is not None)
    except (TypeError, ValueError):
        return False


def _fresh_failure(value):
    if not isinstance(value, dict) or type(value.get('http_status')) is not int or value['http_status'] not in {403, 429}:
        return False
    try:
        checked = parse_datetime(value.get('checked_at', ''))
        return checked is not None and timezone.is_aware(checked) and 0 <= (timezone.now() - checked).total_seconds() <= 900
    except (TypeError, ValueError):
        return False


def _read_checks(connection, *, failures=False):
    from .communication_models import ChannelConnection
    current = ChannelConnection.objects.select_related('channel').get(pk=connection.pk)
    if not _cache_scope(connection, current):
        raise ValueError('Подключение изменилось во время проверки фотографий.')
    settings = current.settings if isinstance(current.settings, dict) else {}
    state = settings.get('avito_services_image_checks', {})
    field, valid = ('failures', _fresh_failure) if failures else ('checks', _fresh_check)
    if (not isinstance(state, dict) or state.get('account_id') != connection.external_id
            or state.get('organization_id') != connection.channel.organization_id
            or not isinstance(state.get(field), dict) or len(state[field]) > 512):
        return {}
    return {key: value for key, value in state[field].items()
            if isinstance(key, str) and re.fullmatch(r'[0-9a-f]{64}', key) and valid(value)}


def _save_checks(connection, checks, *, failures=None):
    if not checks and not failures:
        return
    from .communication_models import ChannelConnection
    with transaction.atomic():
        current = ChannelConnection.objects.select_for_update().select_related('channel').get(pk=connection.pk)
        if not _cache_scope(connection, current):
            raise ValueError('Подключение изменилось во время проверки фотографий.')
        settings = dict(current.settings) if isinstance(current.settings, dict) else {}
        previous = settings.get('avito_services_image_checks', {})
        scope_valid = (isinstance(previous, dict) and previous.get('account_id') == connection.external_id
                       and previous.get('organization_id') == connection.channel.organization_id)
        saved = {}
        for field, valid, updates in (('checks', _fresh_check, checks), ('failures', _fresh_failure, failures or {})):
            old = previous.get(field) if scope_valid else None
            values = {key: value for key, value in old.items()
                      if isinstance(key, str) and re.fullmatch(r'[0-9a-f]{64}', key) and valid(value)
                      } if isinstance(old, dict) and len(old) <= 512 else {}
            values.update(updates)
            saved[field] = dict(sorted(values.items(), key=lambda pair: pair[1]['checked_at'])[-512:])
        settings['avito_services_image_checks'] = {
            'account_id': connection.external_id,
            'organization_id': connection.channel.organization_id, **saved}
        current.settings = settings
        current.save(update_fields=['settings'])


class SnapshotMediaError(ValueError):
    """Location-specific, safe diagnostics; never include a URL or response body."""
    def __init__(self, row, number, url, error, *, cached=False, local=False):
        identifier = row.get('AvitoId')
        identifier = identifier if isinstance(identifier, str) and re.fullmatch(r'[0-9]{1,32}', identifier) else 'неизвестно'
        transport = photo_transport_failure(error)
        status, reason = transport['http_status'], transport['reason']
        reason = reason or ('image_invalid' if isinstance(error, _PhotoInputError) else
                            'local_image_unavailable' if local else 'photo_validation_failed')
        checked = timezone.now()
        item_id = row.get('Id', '')
        item_id = item_id if isinstance(item_id, str) and re.fullmatch(r'[A-Za-z0-9_.:-]{1,200}', item_id) else ''
        self.diagnostic = {'avito_id': identifier, 'item_id': item_id, 'photo_number': number, 'checked_at': checked.isoformat(),
            'reason': reason, 'http_status': status, 'cached': cached,
            'url_sha256': hashlib.sha256(url.encode('utf-8')).hexdigest() if isinstance(url, str) else ''}
        if status == 403:
            detail = 'Источник запрещает доступ (HTTP 403); причина авторизации не подтверждена.'
        elif status == 401:
            detail = 'Источник требует авторизацию (HTTP 401).'
        elif status == 429:
            detail = 'Источник ограничил частоту запросов (HTTP 429).'
        elif status is not None and 300 <= status < 400:
            detail = 'Источник перенаправляет запрос; допустимый прямой адрес не подтверждён.'
        elif reason == 'image_invalid':
            detail = 'Данные не являются допустимой фотографией JPEG, PNG или WebP.'
        elif reason == 'local_image_unavailable':
            detail = 'Зарегистрированный файл недоступен или повреждён в хранилище Service2.'
        else:
            detail = 'Фотография не прошла проверку безопасности, формата, размера или доступности.'
        if cached:
            detail += ' Повторный запрос временно удержан; сохранена недавняя причина отказа.'
        stamp = timezone.localtime(checked, ZoneInfo('Asia/Barnaul')).strftime('%d.%m.%Y %H:%M:%S')
        super().__init__(f'Объявление {identifier}, фото №{number}, проверка {stamp} (Барнаул): {detail}')


def _published_references(connection, sheets, observations):
    """Local provenance only; does not claim Avito cached/fetched an image.

    Called only with a fresh, complete owned-active inventory from the normal
    publisher. Retention is exact row AND position AND URL, never a domain bypass.
    Other callers and connections retain the strict download/decode policy.
    """
    from .avito_services_feed import bound, entries, images
    if (connection.pk != 4 or connection.external_id != '446312402'
            or not connection.is_active or not connection.channel.is_active
            or connection.channel.kind != 'avito' or not isinstance(observations, list)):
        return set()
    feed = AvitoServicesFeed.objects.filter(connection=connection, enabled=True).first()
    if feed is None or not bound(feed, connection) or not feed.published or feed.published_revision < 1:
        return set()
    verified = {}
    now = timezone.now()
    for item in observations:
        if not isinstance(item, dict):
            return set()
        try:
            checked = parse_datetime(item.get('checked_at', ''))
        except (TypeError, ValueError):
            return set()
        if (item.get('source') != 'avito_item_detail' or checked is None or timezone.is_naive(checked)
                or not 0 <= (now - checked).total_seconds() <= 90):
            return set()
        if item.get('status') != 'active':
            if item.get('status') in {'old', 'removed', 'rejected', 'blocked'} and item.get('included') is False:
                continue
            return set()
        if item.get('included') is not True:
            return set()
        pair = (item.get('id'), item.get('avito_id'))
        if not all(isinstance(part, str) and part for part in pair):
            return set()
        if pair in verified:
            return set()
        verified[pair] = True
    candidate = entries(sheets)
    pairs = [(row.get('Id'), row.get('AvitoId')) for row in candidate]
    if (not all(all(isinstance(part, str) and part for part in pair) for pair in pairs)
            or len(set(pairs)) != len(pairs) or set(pairs) != set(verified)):
        return set()
    published = entries(feed.published)
    old_pairs = [(row.get('Id'), row.get('AvitoId')) for row in published]
    if (not all(all(isinstance(part, str) and part for part in pair) for pair in old_pairs)
            or len(set(old_pairs)) != len(old_pairs)):
        return set()
    previous = {pair: images(row.get('ImageUrls', ''), max_count=30).split(' | ')
                for pair, row in zip(old_pairs, published)}
    retained = set()
    for row in candidate:
        pair = (row.get('Id'), row.get('AvitoId'))
        old = previous.get(pair, [])
        for offset, url in enumerate(images(row.get('ImageUrls', '')).split(' | ')):
            if offset < len(old) and old[offset] == url:
                retained.add((pair[0], pair[1], offset + 1, url))
    return retained


def validate_snapshot_media(connection, sheets, *, seconds=30, verified_observations=None):
    """Validate new images; preserve only proven unchanged published references.

    A retained reference is syntax/public-DNS checked without an external GET.
    It is NOT fresh availability, provider ingestion or live-card content proof.
    Registered draft assets are read and decoded privately, never self-fetched.
    """
    from .avito_services_feed import images
    deadline, received, checked, checked_hosts = time.monotonic() + min(30, seconds), 0, set(), set()
    cache, failures, updates, negative = _read_checks(connection), _read_checks(connection, failures=True), {}, {}
    retained = _published_references(connection, sheets, verified_observations)
    result = {'checked_at': timezone.now().isoformat(), 'retained': [], 'checked_count': 0, 'local_count': 0}
    locations = []
    for sheet in sheets:
        for row in sheet['rows']:
            try:
                urls = validate_photo_scope(connection, row.get('ImageUrls', '')).split(' | ')
            except ValueError as error:
                raise SnapshotMediaError(row, 1, '', error) from None
            locations.extend((row, number, url) for number, url in enumerate(urls, 1))
    try:
        for row, number, url in locations:
            if time.monotonic() >= deadline:
                raise SnapshotMediaError(row, number, url, _PhotoTransportError('Лимит времени проверки.', reason='deadline', phase='body'))
            asset, cached = None, False
            try:
                asset = _asset(connection, url)
                key = hashlib.sha256(url.encode('utf-8')).hexdigest()
                if asset is not None:
                    if ('asset', url) not in checked:
                        _read_asset(asset)
                        checked.add(('asset', url))
                    result['local_count'] += 1
                elif (row.get('Id'), row.get('AvitoId'), number, url) in retained:
                    _, host, port = _parse_url(url)
                    if (host, port) not in checked_hosts:
                        _resolve(host, port, deadline)
                        checked_hosts.add((host, port))
                    result['retained'].append({'avito_id': row['AvitoId'], 'photo_number': number,
                        'code': 'published_reference_retained', 'url_sha256': key})
                elif ('external', url) not in checked:
                    if key in failures:
                        cached = True
                        raise _PhotoTransportError('Недавний отказ источника.',
                            reason='http_status', phase='headers', http_status=failures[key]['http_status'])
                    if key not in cache:
                        raw = _download(url, deadline)
                        try:
                            _image(raw, normalize=False)
                        except ValueError:
                            raise _PhotoInputError('Изображение не декодируется.') from None
                        updates[key] = {'checked_at': timezone.now().isoformat(),
                                        'bytes': len(raw), 'sha256': hashlib.sha256(raw).hexdigest()}
                        received += len(raw)
                    checked.add(('external', url))
                    result['checked_count'] += 1
                if received > 32 * 1024 * 1024:
                    raise ValueError('image_total_limit')
            except ValueError as error:
                transport = photo_transport_failure(error)
                if not cached and transport.get('http_status') in {403, 429}:
                    negative[key] = {'checked_at': timezone.now().isoformat(), 'http_status': transport['http_status']}
                raise SnapshotMediaError(row, number, url, error, cached=cached, local=asset is not None) from None
    finally:
        _save_checks(connection, updates, failures=negative)
    return result

def diagnose_snapshot_media(connection, sheets, *, target_ids, seconds=10):
    """Bounded first-old-photo probe, never a publication or an upload.

    At most two selected ads and one GET per hostname. A second URL on that
    host is explicitly unprobed, not assigned the first URL's error. No raw
    URL/body/header is returned. Existing authenticated editor has the URL.
    """
    from .avito_services_feed import entries, images, bound
    if connection.pk != 4 or connection.external_id != '446312402':
        raise ValueError('Недопустимая область диагностики фотографий.')
    feed = AvitoServicesFeed.objects.filter(connection=connection, enabled=True).first()
    if (feed is None or not bound(feed, connection) or not connection.is_active
            or not connection.channel.is_active or connection.channel.kind != 'avito'):
        raise ValueError('Привязка источника фотографий не подтверждена.')
    if (not isinstance(target_ids, (list, tuple, set)) or not 1 <= len(target_ids) <= 2
            or not all(isinstance(value, str) and re.fullmatch(r'[0-9]{1,32}', value) for value in target_ids)):
        raise ValueError('Выберите до двух объявлений для диагностики.')
    baseline = {(row['Id'], row['AvitoId']): row for row in entries(feed.published)}
    deadline, hosts, result, updates = time.monotonic() + min(10, max(1, seconds)), set(), [], {}
    failures = _read_checks(connection, failures=True)
    try:
        for row in entries(sheets):
            if row.get('AvitoId') not in target_ids:
                continue
            previous = baseline.get((row.get('Id'), row.get('AvitoId')))
            if previous is None:
                raise ValueError('Объявление отсутствует в опубликованном источнике.')
            old = images(previous.get('ImageUrls', ''), max_count=30).split(' | ')
            for number, url in enumerate(images(row.get('ImageUrls', '')).split(' | '), 1):
                if number > len(old) or old[number - 1] != url:
                    continue
                cached, host = False, ''
                try:
                    if _asset(connection, url) is not None:
                        continue
                    _, host, _ = _parse_url(url)
                    key = hashlib.sha256(url.encode('utf-8')).hexdigest()
                    if host in hosts:
                        result.append({'avito_id': row['AvitoId'], 'photo_number': number,
                            'checked_at': timezone.now().isoformat(), 'url_sha256': key, 'source_host': host,
                            'reason': 'not_probed_same_host', 'http_status': None, 'cached': False})
                        break
                    hosts.add(host)
                    if key in failures:
                        cached = True
                        raise _PhotoTransportError('Недавний отказ источника.',
                            reason='http_status', phase='headers', http_status=failures[key]['http_status'])
                    raw = _download(url, deadline)
                    try:
                        _image(raw, normalize=False)
                    except ValueError:
                        raise _PhotoInputError('Изображение не декодируется.') from None
                    result.append({'avito_id': row['AvitoId'], 'photo_number': number,
                        'checked_at': timezone.now().isoformat(), 'url_sha256': key, 'source_host': host,
                        'reason': 'available_image', 'http_status': 200, 'cached': False})
                except ValueError as error:
                    report = SnapshotMediaError(row, number, url, error, cached=cached).diagnostic
                    report['source_host'] = host
                    result.append(report)
                    if not cached and report['http_status'] in {403, 429}:
                        updates[report['url_sha256']] = {'checked_at': report['checked_at'],
                                                       'http_status': report['http_status']}
                break
    finally:
        _save_checks(connection, {}, failures=updates)
    return result


def mark_snapshot_media_published(connection, sheets):
    from .avito_services_feed import bound
    if not db_connection.in_atomic_block:
        raise ValueError('Публикация фотографий должна выполняться вместе с публикацией файла.')
    feed = AvitoServicesFeed.objects.filter(connection=connection, enabled=True).first()
    if feed is None or not bound(feed, connection) or feed.published != sheets:
        raise ValueError('Фотографии не связаны с опубликованным файлом.')
    keys = []
    for sheet in sheets:
        for row in sheet['rows']:
            for url in validate_photo_scope(connection, row['ImageUrls']).split(' | '):
                asset = _asset(connection, url)
                if asset is not None:
                    keys.append(asset.pk)
    AvitoServicesImage.objects.filter(pk__in=keys, published_in_feed_at__isnull=True).update(
        published_in_feed_at=timezone.now())


def editor_media_context(connection, row):
    from .avito_services_feed import images
    result = []
    for url in images(row.get('ImageUrls', ''), max_count=30).split(' | '):
        try:
            asset = _asset(connection, url)
            preview = reverse('avito_services_image_preview', args=[connection.pk, asset.public_key]) if asset else ''
        except ValueError:
            preview = ''
        result.append({'url': url, 'preview_url': preview})
    return {'photo_initial': result,
        'photo_upload_url': reverse('avito_services_image_upload', args=[connection.pk])}


def _asset_response(asset):
    return {'url': asset.public_url,
        'preview_url': reverse('avito_services_image_preview', args=[asset.connection_id, asset.public_key]),
        'width': asset.width, 'height': asset.height}


@login_required
@never_cache
@require_POST
def services_image_upload(request, connection_id):
    from .avito_services_feed import connection_for, bound, entries
    connection = connection_for(request, connection_id)
    feed = get_object_or_404(AvitoServicesFeed, connection=connection)
    if not bound(feed, connection):
        raise Http404
    if str(feed.revision) != request.POST.get('revision'):
        return JsonResponse({'error': 'Черновик изменился. Обновите страницу.'}, status=409)
    if request.POST.get('avito_id') not in {row['AvitoId'] for row in entries(feed.draft)}:
        raise Http404
    files = request.FILES.getlist('photos')
    source_url = request.POST.get('image_url', '').strip()
    if bool(files) == bool(source_url) or len(files) > 10:
        return JsonResponse({'error': 'Выберите до 10 файлов или одну ссылку на фотографию.'}, status=400)
    created_files = []
    try:
        source_asset = _asset(connection, source_url) if source_url else None
        if files:
            raw_images = [_image(upload.read(MAX_BYTES + 1)) for upload in files]
        elif source_asset is not None:
            raw_images = [(_read_asset(source_asset), (source_asset.width, source_asset.height))]
        else:
            raw_images = [_image(_download(source_url, time.monotonic() + 10))]
        with transaction.atomic():
            from .communication_models import ChannelConnection
            ChannelConnection.objects.select_for_update().get(pk=connection.pk)
            connection = connection_for(request, connection_id)
            feed = AvitoServicesFeed.objects.select_for_update().get(pk=feed.pk)
            if not bound(feed, connection):
                raise Http404
            if str(feed.revision) != request.POST.get('revision'):
                return JsonResponse({'error': 'Черновик изменился. Обновите страницу.'}, status=409)
            if request.POST.get('avito_id') not in {row['AvitoId'] for row in entries(feed.draft)}:
                raise Http404
            stored = []
            scoped = AvitoServicesImage.objects.filter(connection=connection,
                account_id=connection.external_id, organization_id_at_upload=connection.channel.organization_id)
            count = scoped.count()
            total = sum(scoped.values_list('byte_size', flat=True))
            for raw, size in raw_images:
                digest = hashlib.sha256(raw).hexdigest()
                asset = scoped.filter(sha256=digest).first()
                if asset is None:
                    if count >= MAX_ASSETS or total + len(raw) > MAX_SCOPE_BYTES:
                        raise ValueError('Достигнут лимит хранилища фотографий подключения.')
                    asset = AvitoServicesImage(connection=connection, account_id=connection.external_id,
                        organization_id_at_upload=connection.channel.organization_id, uploaded_by=request.user,
                        sha256=digest, byte_size=len(raw), width=size[0], height=size[1])
                    asset.public_url = _origin() + reverse('avito_services_image_public', args=[asset.public_key])
                    name = f'avito_services_images/{asset.organization_id_at_upload}/{connection.pk}/{asset.public_key.hex}.jpg'
                    saved = asset.file.storage.save(name, ContentFile(raw))
                    created_files.append((asset.file.storage, saved))
                    if saved != name:
                        raise ValueError('Не удалось сохранить фотографию. Повторите загрузку.')
                    asset.file.name = saved
                    asset.save()
                    count += 1
                    total += len(raw)
                else:
                    _read_asset(asset)
                stored.append(_asset_response(asset))
        return JsonResponse({'images': stored})
    except ValueError as error:
        for storage, name in created_files:
            storage.delete(name)
        return JsonResponse({'error': str(error)}, status=400)
    except Exception:
        for storage, name in created_files:
            storage.delete(name)
        raise


@login_required
@never_cache
@require_GET
def services_image_preview(request, connection_id, public_key):
    from .avito_services_feed import connection_for
    connection = connection_for(request, connection_id)
    asset = get_object_or_404(AvitoServicesImage, public_key=public_key, connection=connection,
        account_id=connection.external_id, organization_id_at_upload=connection.channel.organization_id)
    return _image_response(asset)


@require_GET
def services_image_public(request, public_key):
    from .avito_services_feed import bound
    asset = get_object_or_404(AvitoServicesImage.objects.select_related('connection__channel'),
                             public_key=public_key, published_in_feed_at__isnull=False)
    connection = asset.connection
    feed = AvitoServicesFeed.objects.filter(connection=connection, enabled=True).first()
    if (not feed or not bound(feed, connection) or not connection.is_active
            or not connection.channel.is_active or connection.channel.kind != 'avito'
            or asset.account_id != connection.external_id
            or asset.organization_id_at_upload != connection.channel.organization_id):
        raise Http404
    return _image_response(asset)


def _image_response(asset):
    try:
        _asset_path(asset)
        if asset.file.storage.size(asset.file.name) != asset.byte_size:
            raise ValueError('Фотография повреждена.')
        stream = asset.file.storage.open(asset.file.name, 'rb')
    except (ValueError, OSError):
        raise Http404 from None
    response = FileResponse(stream, content_type='image/jpeg')
    response['Content-Length'] = asset.byte_size
    response['X-Content-Type-Options'] = 'nosniff'
    response['X-Robots-Tag'] = 'noindex, nofollow, noarchive'
    response['Cache-Control'] = 'private, no-store'
    return response


def storage_probe():
    """Protected operator smoke: fixed synthetic pixels, no asset row or public URL."""
    import os
    from pathlib import Path
    import uuid
    from django.conf import settings
    from .avito_media_storage import AvitoImageStorage
    storage = AvitoImageStorage()
    identifier = uuid.uuid4().hex
    path = f'avito_storage_checks/{identifier}.jpg'
    raw = BytesIO()
    Image.new('RGB', (8, 8), 'white').save(raw, format='JPEG')
    content = raw.getvalue()
    saved = None
    try:
        saved = storage.save(path, ContentFile(content))
        with storage.open(saved, 'rb') as source:
            readable = source.read(len(content) + 1) == content
        resolved = Path(storage.path(saved)).resolve()
        checkout = Path(settings.BASE_DIR).resolve()
        return {'outside_checkout': checkout not in resolved.parents and resolved != checkout,
            'file_private': os.stat(resolved).st_mode & 0o777 == 0o600,
            'directory_private': os.stat(resolved.parent).st_mode & 0o777 == 0o700,
            'readable': readable, 'synthetic_only': True}
    finally:
        if saved:
            storage.delete(saved)


def _storage_probe_context(connection, expected_actor=None):
    """Local signed-operator scope only; does not authorize browser uploads."""
    from .avito_services_workplace_setup import KEY, _actor, _binding
    if (connection.pk != 4 or connection.external_id != '446312402' or not connection.is_active
            or not connection.channel.is_active or connection.channel.kind != 'avito'):
        raise ValueError('media_probe_scope_invalid')
    settings = connection.settings if isinstance(connection.settings, dict) else {}
    marker = settings.get(KEY)
    if not isinstance(marker, dict) or marker.get('automatic_reports_configured_once') is not True:
        raise ValueError('media_probe_setup_missing')
    actor = _actor(connection)
    if expected_actor is not None and actor.pk != expected_actor:
        raise ValueError('media_probe_actor_changed')
    import json
    canonical = json.dumps(_binding(connection), sort_keys=True, separators=(',', ':')).encode('utf-8')
    return actor, hashlib.sha256(canonical).hexdigest()


def make_storage_probe_token(connection, actor):
    """Mint on the protected operator server; never log or return this token to users."""
    import uuid
    from django.core import signing
    from .communication_models import ChannelConnection
    if type(getattr(actor, 'pk', None)) is not int or not actor.is_active:
        raise ValueError('media_probe_actor_invalid')
    current = ChannelConnection.objects.select_related('channel', 'avito_credential').get(pk=connection.pk)
    owner, binding = _storage_probe_context(current, actor.pk)
    return signing.dumps({'connection': current.pk, 'actor': owner.pk, 'binding': binding,
                          'nonce': uuid.uuid4().hex}, salt='avito-media-probe')


from django.views.decorators.csrf import csrf_exempt


@csrf_exempt
@never_cache
@require_POST
def services_media_probe(request):
    """One-purpose120s signed POST: synthetic8x8storage smoke in the real web UID."""
    import json
    from django.core import signing
    from .communication_models import ChannelConnection
    if request.content_type != 'application/json' or len(request.body) > 4096:
        return JsonResponse({'error': 'media_probe_denied'}, status=403)
    try:
        body = json.loads(request.body)
        token = body.get('token') if isinstance(body, dict) else None
        if not isinstance(token, str) or len(token) > 3000:
            raise ValueError
        payload = signing.loads(token, salt='avito-media-probe', max_age=120)
        if (not isinstance(payload, dict) or set(payload) != {'connection', 'actor', 'binding', 'nonce'}
                or type(payload['connection']) is not int or payload['connection'] != 4
                or type(payload['actor']) is not int or payload['actor'] <= 0
                or not isinstance(payload['nonce'], str)
                or re.fullmatch(r'[0-9a-f]{32}', payload['nonce']) is None
                or not isinstance(payload['binding'], str)
                or re.fullmatch(r'[0-9a-f]{64}', payload['binding']) is None):
            raise ValueError
        with transaction.atomic():
            current = ChannelConnection.objects.select_for_update().select_related(
                'channel', 'avito_credential').get(pk=4)
            owner, binding = _storage_probe_context(current, payload['actor'])
            if binding != payload['binding']:
                raise ValueError
            try:
                return JsonResponse(storage_probe())
            except Exception:
                return JsonResponse({'error': 'media_storage_probe_failed'}, status=503)
    except Exception:
        # Invalid signatures/scope/setup/roles never expose tokens or identity details.
        return JsonResponse({'error': 'media_probe_denied'}, status=403)
