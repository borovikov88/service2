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


def _origin():
    from .avito_services_publication import public_origin
    return public_origin()


def _parse_url(value):
    if not isinstance(value, str) or len(value) > 2000 or any(ord(c) < 33 or c.isspace() for c in value):
        raise ValueError('Некорректная ссылка на фотографию.')
    try:
        parts = urlsplit(value)
        host = (parts.hostname or '').encode('idna').decode('ascii').lower()
        port = parts.port if parts.port is not None else (443 if parts.scheme == 'https' else 80)
        if (parts.scheme not in {'https', 'http'} or not host or parts.username is not None
                or parts.password is not None or parts.fragment or '%' in host or port != (443 if parts.scheme == 'https' else 80)):
            raise ValueError
    except (ValueError, UnicodeError):
        raise ValueError('Разрешены ссылки HTTP/HTTPS без пароля, фрагмента и нестандартного порта.') from None
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


def _resolve(host, port, deadline):
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        literal = None
    if literal is not None:
        if not _public_ip(host):
            raise ValueError('Локальные и служебные адреса фотографий запрещены.')
        return host
    if host.rstrip('.').endswith(('.localhost', '.local', '.internal')) or host.rstrip('.') == 'localhost':
        raise ValueError('Локальные и служебные адреса фотографий запрещены.')
    if not _DNS_SLOTS.acquire(blocking=False):
        raise ValueError('Проверка фотографий занята. Повторите позже.')
    result = queue.Queue(maxsize=1)
    def lookup():
        try:
            result.put((True, socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)))
        except (OSError, ValueError):
            result.put((False, None))
        finally:
            _DNS_SLOTS.release()
    threading.Thread(target=lookup, daemon=True).start()
    try:
        ok, answers = result.get(timeout=max(.05, min(3, deadline - time.monotonic())))
    except queue.Empty:
        raise ValueError('Не удалось вовремя проверить адрес фотографии.') from None
    if not ok or not answers or len(answers) > 32:
        raise ValueError('Не удалось проверить адрес фотографии.')
    addresses = [entry[4][0] for entry in answers]
    if not all(_public_ip(address) for address in addresses):
        raise ValueError('Локальные и служебные адреса фотографий запрещены.')
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


def _download(value, deadline):
    parts, host, port = _parse_url(value)
    if time.monotonic() >= deadline:
        raise ValueError('Проверка фотографий заняла слишком много времени. Загрузите изображения в Service2.')
    address = _resolve(host, port, deadline)
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
    try:
        if aborted.is_set() or time.monotonic() >= deadline:
            raise ValueError('Проверка фотографий заняла слишком много времени.')
        connection.connect()
        if aborted.is_set() or time.monotonic() >= deadline:
            raise ValueError('Проверка фотографий заняла слишком много времени.')
        path = quote(parts.path or '/', safe="/%:@!$&'()*+,;=-._~")
        if parts.query:
            path += '?' + quote(parts.query, safe="/%?:@!$&'()*+,;=-._~")
        authority = '[' + host + ']' if ':' in host else host
        connection.request('GET', path, headers={'Host': authority,
            'Accept': 'image/jpeg,image/png,image/webp', 'User-Agent': 'Service2-AvitoImageCheck/1'})
        response = connection.getresponse()
        if response.status != 200:
            raise ValueError('Фотография недоступна: нужен прямой URL с ответом HTTP 200 без перенаправлений.')
        content_type = response.getheader('Content-Type', '').split(';', 1)[0].strip().lower()
        if content_type not in {'image/jpeg', 'image/png', 'image/webp'}:
            raise ValueError('Ссылка не возвращает фотографию JPEG, PNG или WebP.')
        length = response.getheader('Content-Length')
        if length is not None and (len(length) > 10 or not length.isascii() or not length.isdecimal() or not 0 < int(length) <= MAX_BYTES):
            raise ValueError('Некорректный или слишком большой размер фотографии.')
        chunks, received = [], 0
        while True:
            if time.monotonic() >= deadline:
                raise ValueError('Проверка фотографий заняла слишком много времени. Загрузите изображения в Service2.')
            chunk = response.read1(min(65536, MAX_BYTES + 1 - received))
            if not chunk:
                break
            chunks.append(chunk)
            received += len(chunk)
            if received > MAX_BYTES:
                raise ValueError('Размер фотографии должен быть не больше 8 МБ.')
        if length is not None and received != int(length):
            raise ValueError('Фотография получена не полностью. Повторите проверку или загрузите файл.')
        if time.monotonic() >= deadline:
            raise ValueError('Проверка фотографий заняла слишком много времени.')
        return b''.join(chunks)
    except (OSError, http.client.HTTPException, UnicodeError):
        raise ValueError('Не удалось скачать фотографию по указанной ссылке.') from None
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


def _read_checks(connection):
    from .communication_models import ChannelConnection
    current = ChannelConnection.objects.select_related('channel').get(pk=connection.pk)
    if not _cache_scope(connection, current):
        raise ValueError('Подключение изменилось во время проверки фотографий.')
    settings = current.settings if isinstance(current.settings, dict) else {}
    state = settings.get('avito_services_image_checks', {})
    if (not isinstance(state, dict) or state.get('account_id') != connection.external_id
            or state.get('organization_id') != connection.channel.organization_id
            or not isinstance(state.get('checks'), dict) or len(state['checks']) > 512):
        return {}
    return {key: value for key, value in state['checks'].items()
            if isinstance(key, str) and re.fullmatch(r'[0-9a-f]{64}', key) and _fresh_check(value)}


def _save_checks(connection, checks):
    if not checks:
        return
    from .communication_models import ChannelConnection
    with transaction.atomic():
        current = ChannelConnection.objects.select_for_update().select_related('channel').get(pk=connection.pk)
        if not _cache_scope(connection, current):
            raise ValueError('Подключение изменилось во время проверки фотографий.')
        settings = dict(current.settings) if isinstance(current.settings, dict) else {}
        previous = settings.get('avito_services_image_checks', {})
        if (isinstance(previous, dict) and previous.get('account_id') == connection.external_id
                and previous.get('organization_id') == connection.channel.organization_id
                and isinstance(previous.get('checks'), dict) and len(previous['checks']) <= 512):
            saved = {key: value for key, value in previous['checks'].items()
                     if isinstance(key, str) and re.fullmatch(r'[0-9a-f]{64}', key) and _fresh_check(value)}
        else:
            saved = {}
        saved.update(checks)
        saved = dict(sorted(saved.items(), key=lambda pair: pair[1]['checked_at'])[-512:])
        settings['avito_services_image_checks'] = {
            'account_id': connection.external_id,
            'organization_id': connection.channel.organization_id, 'checks': saved}
        current.settings = settings
        current.save(update_fields=['settings'])


def validate_snapshot_media(connection, sheets, *, seconds=30):
    """All URLs checked locally or within15min; successful bounded progress survives retry."""
    deadline, received, checked = time.monotonic() + min(30, seconds), 0, set()
    cache, updates = _read_checks(connection), {}
    urls = []
    for sheet in sheets:
        for row in sheet['rows']:
            urls.extend(validate_photo_scope(connection, row.get('ImageUrls', '')).split(' | '))
    try:
        for url in urls:
            if url in checked:
                continue
            if time.monotonic() >= deadline:
                raise ValueError('Проверка фотографий достигла лимита времени. Результаты сохранены; повторите публикацию для оставшихся ссылок.')
            asset = _asset(connection, url)
            if asset is not None:
                _read_asset(asset)
            else:
                key = hashlib.sha256(url.encode('utf-8')).hexdigest()
                if key not in cache:
                    raw = _download(url, deadline)
                    _image(raw, normalize=False)
                    updates[key] = {'checked_at': timezone.now().isoformat(),
                                    'bytes': len(raw), 'sha256': hashlib.sha256(raw).hexdigest()}
                    received += len(raw)
            checked.add(url)
            if received > 32 * 1024 * 1024:
                raise ValueError('Проверка фотографий достигла лимита загрузки. Результаты сохранены; повторите публикацию для оставшихся ссылок.')
    finally:
        _save_checks(connection, updates)


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
