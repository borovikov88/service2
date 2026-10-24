"""Narrow operator proof using a real existing service photo; never publishes a file.

The signed request executes current permissioned upload/preview/editor views in the
web process. It is server HTTP-view proof, not browser-click or Avito-content proof.
"""
from copy import deepcopy
import hashlib
from io import BytesIO
import json
import re
import time
import uuid

from PIL import Image
from django.core import signing
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import transaction
from django.http import Http404, JsonResponse
from django.test import RequestFactory
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.cache import never_cache
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from . import avito_services_media as media
from .avito_feed_models import AvitoServicesFeed
from .avito_media_models import AvitoServicesImage
from .communication_models import ChannelConnection

SALT = 'avito-services-live-photo-check-v1'
SOURCE = 'existing_services_draft_image'
RECEIPT_KEY = 'avito_services_media_livecheck_v1'


class LiveMediaCheckError(ValueError):
    pass


def _require(condition):
    if not condition:
        raise LiveMediaCheckError('live_media_check_failed')


def _digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
        separators=(',', ':')).encode('utf-8')).hexdigest()


def _context(connection, actor_id, avito_id):
    from .avito_services_feed import bound, entries, ContentForm, connection_for
    owner, binding = media._storage_probe_context(connection, actor_id)
    # The normal editor's current organization and channel-management rights
    # remain authoritative even if the signed operator marker is valid.
    request = _request('GET', owner)
    scoped = connection_for(request, connection.pk)
    feed = AvitoServicesFeed.objects.get(connection=scoped)
    _require(bound(feed, scoped))
    _require(isinstance(avito_id, str) and re.fullmatch(r'[0-9]{1,24}', avito_id))
    row = next((r for r in entries(feed.draft) if r['AvitoId'] == avito_id), None)
    _require(row is not None)
    urls = media.validate_photo_scope(scoped, row['ImageUrls']).split(' | ')
    # Round-trip must preserve the source content exactly, including whitespace.
    form = ContentForm({'title': row['Title'], 'description': row['Description'],
        'image_urls': row['ImageUrls'], 'reason': ''})
    _require(form.is_valid())
    _require((form.cleaned_data['title'], form.cleaned_data['description'],
              form.cleaned_data['image_urls']) ==
             (row['Title'], row['Description'], row['ImageUrls']))
    published_row = next((item for item in entries(feed.published)
        if item['Id'] == row['Id'] and item['AvitoId'] == row['AvitoId']), None)
    _require(published_row is not None)
    allowed = [value for value in urls if value in published_row.get('ImageUrls', '').split(' | ')]
    _require(bool(allowed))
    source = next((value for value in allowed if media._asset(scoped, value) is None), allowed[0])
    fingerprint = _digest({'draft': feed.draft, 'published': feed.published,
        'enabled': feed.enabled, 'published_revision': feed.published_revision,
        'published_photo_limit': feed.published_photo_limit})
    return scoped, owner, binding, feed, row, source, fingerprint


def _request(method, actor, data=None):
    from urllib.parse import urlsplit
    origin = urlsplit(media._origin())
    factory = RequestFactory()
    if method == 'POST':
        request = factory.post('/', data or {}, secure=True, HTTP_HOST=origin.netloc)
    else:
        request = factory.get('/', secure=True, HTTP_HOST=origin.netloc)
    request.user = actor
    return request


def _source(connection, row, source, published):
    asset = media._asset(connection, source)
    raw = media._read_asset(asset) if asset else media._download(source, time.monotonic() + 10)
    media._image(raw, normalize=False)
    with Image.open(BytesIO(raw)) as image:
        format_name, dimensions = image.format, image.size
    from .avito_services_feed import entries
    attached = any(item['Id'] == row['Id'] and item['AvitoId'] == row['AvitoId']
        and source in item.get('ImageUrls', '').split(' | ') for item in entries(published))
    evidence = {'scope_valid': True, 'source': SOURCE, 'source_in_published': attached,
        'photo_count': len(row['ImageUrls'].split(' | ')), 'readable': True,
        'format': format_name, 'width': dimensions[0], 'height': dimensions[1],
        'bytes': len(raw), 'checked_at': timezone.now().isoformat()}
    return raw, evidence


def media_livecheck_source(connection, actor, avito_id):
    """Read-only, allowlisted photo evidence. No source URL, content or digest returned."""
    _require(type(getattr(actor, 'pk', None)) is int and actor.is_active)
    current = ChannelConnection.objects.select_related('channel', 'avito_credential').get(pk=connection.pk)
    current, owner, binding, feed, row, source, fingerprint = _context(current, actor.pk, avito_id)
    raw, evidence = _source(current, row, source, feed.published)
    return evidence


def media_release_source(connection):
    """Read-only release receipt: choose an existing photo shared by draft and file."""
    from .avito_services_feed import entries
    from .avito_services_workplace_setup import _actor
    current = ChannelConnection.objects.select_related('channel', 'avito_credential').get(pk=connection.pk)
    actor = _actor(current)
    feed = AvitoServicesFeed.objects.get(connection=current)
    for row in entries(feed.draft):
        published = next((item for item in entries(feed.published)
            if item['Id'] == row['Id'] and item['AvitoId'] == row['AvitoId']), None)
        if published and any(url in published.get('ImageUrls', '').split(' | ')
                             for url in row.get('ImageUrls', '').split(' | ') if url):
            return media_livecheck_source(current, actor, row['AvitoId'])
    raise LiveMediaCheckError('live_media_source_unavailable')


def make_media_livecheck_token(connection, actor, avito_id):
    """Protected-server helper only. Never log the token or expose it in a URL."""
    _require(type(getattr(actor, 'pk', None)) is int and actor.is_active)
    current = ChannelConnection.objects.select_related('channel', 'avito_credential').get(pk=connection.pk)
    current, owner, binding, feed, row, source, fingerprint = _context(current, actor.pk, avito_id)
    return signing.dumps({'connection': current.pk, 'actor': owner.pk, 'binding': binding,
        'revision': feed.revision, 'fingerprint': fingerprint, 'avito_id': row['AvitoId'],
        'source_sha256': hashlib.sha256(source.encode()).hexdigest(),
        'nonce': uuid.uuid4().hex}, salt=SALT)


def _payload(request):
    _require(request.content_type == 'application/json' and len(request.body) <= 4096)
    body = json.loads(request.body)
    _require(isinstance(body, dict) and set(body) == {'token'}
        and isinstance(body['token'], str) and len(body['token']) <= 3000)
    value = signing.loads(body['token'], salt=SALT, max_age=120)
    _require(isinstance(value, dict) and set(value) == {'connection', 'actor', 'binding',
        'revision', 'fingerprint', 'avito_id', 'source_sha256', 'nonce'})
    _require(type(value['connection']) is int and value['connection'] == 4
        and type(value['actor']) is int and value['actor'] > 0
        and type(value['revision']) is int and value['revision'] > 0)
    for key in ('binding', 'fingerprint', 'source_sha256'):
        _require(isinstance(value[key], str) and re.fullmatch(r'[0-9a-f]{64}', value[key]))
    _require(isinstance(value['nonce'], str) and re.fullmatch(r'[0-9a-f]{32}', value['nonce']))
    _require(isinstance(value['avito_id'], str) and re.fullmatch(r'[0-9]{1,24}', value['avito_id']))
    return value


def _save(actor, connection, feed, row, photo_urls, reason):
    from .avito_services_feed import services_feed_edit
    request = _request('POST', actor, {'revision': str(feed.revision),
        'title': row['Title'], 'description': row['Description'],
        'image_urls': photo_urls, 'reason': reason})
    response = services_feed_edit(request, connection.pk, row['AvitoId'])
    _require(response.status_code == 302)
    feed.refresh_from_db()
    return feed.versions.filter(action='edit', revision=feed.revision).get()


def _receipt(connection, actor, binding, operation_id):
    from .avito_services_feed import bound, connection_for
    scoped = connection_for(_request('GET', actor), connection.pk)
    feed = AvitoServicesFeed.objects.get(connection=scoped)
    _require(bound(feed, scoped))
    metadata = connection.settings if isinstance(connection.settings, dict) else {}
    state = metadata.get(RECEIPT_KEY)
    records = state.get('receipts') if isinstance(state, dict) else None
    if not isinstance(records, list) or len(records) > 8:
        return None
    for record in records:
        if (isinstance(record, dict) and record.get('operation_id') == operation_id
                and record.get('binding') == binding and record.get('actor_id') == actor.pk
                and record.get('feed_id') == feed.pk and isinstance(record.get('result'), dict)):
            return deepcopy(record['result'])
    return None


def media_livecheck_receipt(connection, operation_id):
    """Reconcile only the exact submitted token hash; never retries or mutates."""
    _require(isinstance(operation_id, str) and re.fullmatch(r'[0-9a-f]{64}', operation_id))
    current = ChannelConnection.objects.select_related('channel', 'avito_credential').get(pk=connection.pk)
    actor, binding = media._storage_probe_context(current)
    return _receipt(current, actor, binding, operation_id)


def _save_receipt(connection, actor, binding, feed, operation_id, result):
    # The caller still holds the connection/feed locks and the draft transaction.
    connection.refresh_from_db(fields=['settings'])
    metadata = dict(connection.settings) if isinstance(connection.settings, dict) else {}
    state = metadata.get(RECEIPT_KEY)
    previous = state.get('receipts') if isinstance(state, dict) else None
    previous = [item for item in previous if isinstance(item, dict)] if isinstance(previous, list) else []
    metadata[RECEIPT_KEY] = {'receipts': previous[-7:] + [{
        'operation_id': operation_id, 'actor_id': actor.pk, 'binding': binding,
        'feed_id': feed.pk, 'result': deepcopy(result)}]}
    connection.settings = metadata
    connection.save(update_fields=['settings'])


def _run(payload, new_files, operation_id):
    """All draft/history writes are one transaction; a failure leaves no changed draft."""
    from .avito_services_feed import entries
    connection = ChannelConnection.objects.select_for_update().select_related(
        'channel', 'avito_credential').get(pk=4)
    actor, binding = media._storage_probe_context(connection, payload['actor'])
    _require(binding == payload['binding'])
    previous = _receipt(connection, actor, binding, operation_id)
    if previous is not None:
        return previous
    connection, actor, binding, feed, row, source, fingerprint = _context(
        connection, payload['actor'], payload['avito_id'])
    feed = AvitoServicesFeed.objects.select_for_update().get(pk=feed.pk)
    _require(binding == payload['binding'] and feed.revision == payload['revision']
        and fingerprint == payload['fingerprint']
        and hashlib.sha256(source.encode()).hexdigest() == payload['source_sha256'])
    before, published = deepcopy(feed.draft), deepcopy(feed.published)
    published_state = (feed.enabled, feed.published_revision, feed.published_photo_limit)
    source_raw, source_evidence = _source(connection, row, source, published)
    original = row['ImageUrls'].split(' | ')
    existing = set(AvitoServicesImage.objects.filter(connection=connection).values_list('pk', flat=True))
    upload = _request('POST', actor, {'revision': str(feed.revision), 'avito_id': row['AvitoId'],
        'photos': SimpleUploadedFile('existing-service-photo', source_raw, content_type='application/octet-stream')})
    response = media.services_image_upload(upload, connection.pk)
    # Capture only files created by this request for cleanup if the outer atomic
    # transaction rolls back. Never delete a reused immutable asset.
    created = list(AvitoServicesImage.objects.filter(connection=connection).exclude(pk__in=existing))
    new_files.extend((asset.file.storage, asset.file.name) for asset in created)
    _require(response.status_code == 200)
    result = json.loads(response.content)
    _require(len(result['images']) == 1)
    image = result['images'][0]
    asset = media._asset(connection, image['url'])
    _require(asset is not None and asset.published_in_feed_at is None)
    preview = media.services_image_preview(_request('GET', actor), connection.pk, asset.public_key)
    try:
        _require(preview.status_code == 200 and preview['Content-Type'] == 'image/jpeg')
        preview_raw = b''.join(preview.streaming_content)
        _require(len(preview_raw) == asset.byte_size
            and hashlib.sha256(preview_raw).hexdigest() == asset.sha256)
    finally:
        # This FileResponse belongs to an internal view call, not a completed
        # HTTP request. Response.close() would emit request_finished and could
        # close the database connection in the enclosing draft transaction.
        preview.file_to_stream.close()
    try:
        media.services_image_public(_request('GET', actor), asset.public_key)
    except Http404:
        pass
    else:
        raise LiveMediaCheckError('live_media_check_failed')
    ordered = [asset.public_url] + [value for value in original if value != asset.public_url][:9]
    _require(' | '.join(ordered) != row['ImageUrls'])
    staged = deepcopy(before)
    target = next(item for item in entries(staged) if item['AvitoId'] == row['AvitoId'])
    target['ImageUrls'] = ' | '.join(ordered)
    edit = _save(actor, connection, feed, row, target['ImageUrls'],
                 'Проверка реальной фотографии: загрузка, превью и порядок; без публикации')
    _require(feed.draft == staged and feed.published == published)
    restore = _save(actor, connection, feed, row, row['ImageUrls'],
                    'Восстановление исходного черновика после проверки фотографии; без публикации')
    _require(feed.draft == before and feed.published == published
        and (feed.enabled, feed.published_revision, feed.published_photo_limit) == published_state)
    asset.refresh_from_db()
    _require(asset.published_in_feed_at is None)
    result = {'connection_id': 4, 'account_id': '446312402', 'avito_id': row['AvitoId'],
        'operation_id': operation_id,
        'checked_at': timezone.now().isoformat(), 'source': source_evidence,
        'upload_saved': True, 'private_preview_verified': True, 'cover_order_saved': True,
        'original_draft_restored': True, 'published_unchanged': True,
        'image_publicly_available': False, 'browser_clicks_verified': False,
        'provider_content_verified': False, 'before_draft_sha256': _digest(before),
        'after_draft_sha256': _digest(feed.draft), 'published_sha256': _digest(published),
        'source_content_sha256': hashlib.sha256(source_raw).hexdigest(),
        'revision_before': payload['revision'], 'revision_after': feed.revision,
        'edit_version_id': edit.pk, 'restore_version_id': restore.pk}
    _save_receipt(connection, actor, binding, feed, operation_id, result)
    return result


@csrf_exempt
@never_cache
@require_POST
def services_media_livecheck(request):
    """Only a signed, current, fixed-scope existing-photo save-and-restore action."""
    try:
        payload = _payload(request)
    except Exception:
        return JsonResponse({'error': 'live_media_check_denied'}, status=403)
    new_files = []
    try:
        with transaction.atomic():
            token = json.loads(request.body)['token']
            operation_id = hashlib.sha256(token.encode('utf-8')).hexdigest()
            result = _run(payload, new_files, operation_id)
    except Exception:
        for storage, name in new_files:
            try:
                storage.delete(name)
            except OSError:
                pass
        return JsonResponse({'error': 'live_media_check_failed'}, status=409)
    return JsonResponse(result)
