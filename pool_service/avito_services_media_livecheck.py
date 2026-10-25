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
    transport = {}
    raw = media._read_asset(asset) if asset else media._download(
        source, time.monotonic() + 10, diagnostics=transport)
    media._image(raw, normalize=False)
    with Image.open(BytesIO(raw)) as image:
        format_name, dimensions = image.format, image.size
    from .avito_services_feed import entries
    attached = any(item['Id'] == row['Id'] and item['AvitoId'] == row['AvitoId']
        and source in item.get('ImageUrls', '').split(' | ') for item in entries(published))
    evidence = {'scope_valid': True, 'source': SOURCE, 'source_in_published': attached,
        'photo_count': len(row['ImageUrls'].split(' | ')), 'readable': True,
        'format': format_name, 'width': dimensions[0], 'height': dimensions[1],
        'bytes': len(raw), 'checked_at': timezone.now().isoformat(),
        'https_upgrade_followed': transport.get('https_upgrade_followed')}
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


# A file-input check is independent of the external-link/import r1 generation.
FILE_GENERATION = 'services-stored-filecheck-20261025-v1'
FILE_SALT = 'avito-services-stored-filecheck-v1'
FILE_KEY = 'avito_services_stored_filecheck_v1'
FILE_MARKER = '[avito-services-filecheck-20261025]'
FILE_CODES = frozenset({
    'file_check_scope_invalid', 'file_check_external_reserved', 'file_check_context_changed',
    'file_check_needs_owner_original', 'file_check_needs_second_photo', 'file_check_busy',
    'file_check_failed', 'file_check_completed', 'file_check_result_unknown',
    'file_check_release_denied',
})


class FileCheckError(ValueError):
    def __init__(self, code):
        super().__init__(code if code in FILE_CODES else 'file_check_failed')


def _file_require(condition, code='file_check_failed'):
    if not condition:
        raise FileCheckError(code)


def _external_marker(connection):
    value = connection.settings if isinstance(connection.settings, dict) else {}
    marker = value.get('avito_services_release_operation_v1')
    if marker is None:
        return None
    _file_require(isinstance(marker, dict), 'file_check_external_reserved')
    # A file check never takes over, resets, or completes another generation.
    _file_require('operation_id' in marker and marker.get('operation_id') is None
        and marker.get('phase') in {'failed', 'blocked'}
        and marker.get('lease_until') is None, 'file_check_external_reserved')
    return deepcopy(marker)


def _file_scope(connection, actor_id=None):
    from .avito_services_feed import bound, connection_for, entries, images
    current = ChannelConnection.objects.select_related('channel', 'avito_credential').get(pk=connection.pk)
    _file_require(current.pk == 4 and current.external_id == '446312402',
                  'file_check_scope_invalid')
    actor, binding = media._storage_probe_context(current, actor_id)
    scoped = connection_for(_request('GET', actor), 4)
    feed = AvitoServicesFeed.objects.get(connection=scoped)
    _file_require(bound(feed, scoped) and feed.enabled, 'file_check_scope_invalid')
    external = _external_marker(current)
    assets = list(AvitoServicesImage.objects.filter(connection=scoped,
        account_id=scoped.external_id, organization_id_at_upload=scoped.channel.organization_id))
    _file_require(len(assets) <= media.MAX_ASSETS, 'file_check_scope_invalid')
    by_url = {asset.public_url: asset for asset in assets}
    draft_rows, published_rows = list(entries(feed.draft)), list(entries(feed.published))
    _file_require(0 < len(draft_rows) <= 200 and len(published_rows) <= 200,
                  'file_check_scope_invalid')
    published = {(row['Id'], row['AvitoId']): row for row in published_rows}
    candidates, approved_ids, referenced_ids = [], set(), set()
    for row in draft_rows:
        urls = images(row.get('ImageUrls', ''), max_count=10).split(' | ')
        shared = published.get((row['Id'], row['AvitoId']))
        if shared is None:
            continue
        published_urls = images(shared.get('ImageUrls', ''), max_count=10).split(' | ')
        for value in urls:
            asset = by_url.get(value)
            if asset is None:
                continue
            referenced_ids.add(asset.pk)
            if value not in published_urls or asset.published_in_feed_at is None:
                continue
            approved_ids.add(asset.pk)
            if len(set(urls)) >= 2:
                candidates.append((row, asset))
    # Prefer a non-cover source; every selected source is already approved.
    candidates.sort(key=lambda item: (item[0]['ImageUrls'].split(' | ')[0] == item[1].public_url,
                                     item[0]['AvitoId'], item[1].pk))
    fingerprint = _digest({'draft': feed.draft, 'published': feed.published,
        'revision': feed.revision, 'published_revision': feed.published_revision,
        'photo_limit': feed.published_photo_limit, 'public_key': str(feed.public_key),
        'external': external})
    counts = {'asset_count': len(assets), 'draft_referenced_asset_count': len(referenced_ids),
        'approved_shared_asset_count': len(approved_ids), 'cover_order_candidate_count': len(candidates)}
    return scoped, actor, binding, feed, candidates, fingerprint, external, counts


def local_file_inventory(connection):
    """DB-only; never opens a file, resolves DNS, downloads, or changes a row."""
    result = {'schema_version': 1, 'kind': 'avito_services_local_file_inventory',
        'connection_id': 4, 'account_id': '446312402', 'checked_at': timezone.now().isoformat(),
        'state': 'blocked', 'code': 'file_check_scope_invalid', 'operation_state': 'not_started',
        'draft_matches_published': None, 'draft_revision': None,
        'published_revision': None, 'asset_count': None, 'draft_referenced_asset_count': None,
        'approved_shared_asset_count': None, 'cover_order_candidate_count': None,
        'source_kind': 'stored_normalized_jpeg', 'raw_original_retained': False,
        'browser_clicks_verified': False, 'external_http_checked': False,
        'external_generation_required': True}
    try:
        current, actor, binding, feed, candidates, fingerprint, external, counts = _file_scope(connection)
        result.update(counts, draft_revision=feed.revision, published_revision=feed.published_revision,
                      draft_matches_published=feed.draft == feed.published)
        previous = current.settings.get(FILE_KEY)
        if previous is not None:
            if not isinstance(previous, dict) or previous.get('generation') != FILE_GENERATION:
                result.update(operation_state='invalid', code='file_check_busy')
            elif previous.get('state') == 'completed':
                proof = file_check_receipt(current, previous.get('operation_id'))
                result.update(operation_state='completed', state='completed' if proof else 'blocked',
                    code='file_check_completed' if proof else 'file_check_context_changed')
            else:
                result.update(operation_state='pending', code='file_check_result_unknown')
            return result
        if not counts['approved_shared_asset_count']:
            result['code'] = 'file_check_needs_owner_original'
        elif not candidates:
            result['code'] = 'file_check_needs_second_photo'
        else:
            result.update(state='ready', code='')
    except FileCheckError as error:
        result['code'] = str(error)
    except Exception:
        pass
    return result


def _file_result(state='blocked', code='file_check_failed'):
    return {'schema_version': 1, 'kind': 'avito_services_file_input_check',
        'generation': FILE_GENERATION, 'connection_id': 4, 'account_id': '446312402',
        'checked_at': timezone.now().isoformat(), 'state': state, 'code': code,
        'source_kind': 'stored_normalized_jpeg', 'raw_original_retained': False,
        'draft_matches_published': None, 'file_input_processed': False, 'asset_reused': None, 'new_storage_write': None,
        'private_preview_verified': False, 'cover_order_saved': False, 'editor_reopened': False,
        'original_draft_restored': False, 'published_unchanged': False,
        'browser_clicks_verified': False, 'provider_content_verified': False,
        'external_http_checked': False, 'external_generation_required': True,
        'revision_before': None, 'revision_after': None, 'first_version_id': None,
        'second_version_id': None, 'restore_version_id': None}


def _file_payload(request):
    _file_require(request.content_type == 'application/json' and len(request.body) <= 4096)
    value = json.loads(request.body)
    _file_require(isinstance(value, dict) and set(value) == {'token'}
        and isinstance(value['token'], str) and len(value['token']) <= 3000)
    payload = signing.loads(value['token'], salt=FILE_SALT, max_age=120)
    _file_require(isinstance(payload, dict) and set(payload) == {
        'generation', 'actor', 'binding', 'revision', 'fingerprint', 'avito_id',
        'asset_id', 'asset_sha256', 'nonce'})
    _file_require(payload['generation'] == FILE_GENERATION
        and type(payload['actor']) is int and payload['actor'] > 0
        and type(payload['revision']) is int and payload['revision'] > 0
        and type(payload['asset_id']) is int and payload['asset_id'] > 0)
    for key in ('binding', 'fingerprint', 'asset_sha256'):
        _file_require(isinstance(payload[key], str) and re.fullmatch(r'[0-9a-f]{64}', payload[key]))
    _file_require(isinstance(payload['avito_id'], str)
        and re.fullmatch(r'[0-9]{1,24}', payload['avito_id'])
        and isinstance(payload['nonce'], str) and re.fullmatch(r'[0-9a-f]{32}', payload['nonce']))
    return payload, hashlib.sha256(value['token'].encode()).hexdigest()


def _locked_file_connection(connection_id=4):
    from .communication_models import CommunicationChannel
    reference = ChannelConnection.objects.only('channel_id').get(pk=connection_id)
    CommunicationChannel.objects.select_for_update().get(pk=reference.channel_id)
    connection = ChannelConnection.objects.select_for_update().select_related(
        'channel', 'avito_credential').get(pk=connection_id)
    _file_require(connection.channel_id == reference.channel_id, 'file_check_context_changed')
    return connection


def _reserve_file_check(connection):
    """One generation may reserve one signed request; interrupted HTTP is reconciled."""
    with transaction.atomic():
        current = _locked_file_connection(connection.pk)
        current, actor, binding, feed, candidates, fingerprint, external, counts = _file_scope(current)
        feed = AvitoServicesFeed.objects.select_for_update().get(pk=feed.pk)
        settings = dict(current.settings or {})
        prior = settings.get(FILE_KEY)
        if prior is not None:
            _file_require(isinstance(prior, dict), 'file_check_busy')
            if prior.get('state') == 'completed':
                return None, file_check_receipt(current, prior.get('operation_id'))
            raise FileCheckError('file_check_result_unknown')
        _file_require(counts['approved_shared_asset_count'] > 0, 'file_check_needs_owner_original')
        _file_require(bool(candidates), 'file_check_needs_second_photo')
        row, asset = candidates[0]
        payload = {'generation': FILE_GENERATION, 'actor': actor.pk, 'binding': binding,
            'revision': feed.revision, 'fingerprint': fingerprint, 'avito_id': row['AvitoId'],
            'asset_id': asset.pk, 'asset_sha256': asset.sha256, 'nonce': uuid.uuid4().hex}
        token = signing.dumps(payload, salt=FILE_SALT)
        operation_id = hashlib.sha256(token.encode()).hexdigest()
        settings[FILE_KEY] = {'generation': FILE_GENERATION, 'state': 'pending',
            'operation_id': operation_id, 'actor': actor.pk, 'binding': binding,
            'revision_before': feed.revision, 'before_sha256': _digest(feed.draft),
            'published_sha256': _digest(feed.published), 'fingerprint': fingerprint,
            'created_at': timezone.now().isoformat()}
        current.settings = settings
        current.save(update_fields=['settings'])
        return token, None


def _public_file_result(value):
    """Treat database receipts as untrusted; never export additional/private values."""
    from django.utils.dateparse import parse_datetime
    if not isinstance(value, dict) or set(value) != set(_file_result()):
        return None
    fixed = {'schema_version': 1, 'kind': 'avito_services_file_input_check',
        'generation': FILE_GENERATION, 'connection_id': 4, 'account_id': '446312402',
        'source_kind': 'stored_normalized_jpeg', 'raw_original_retained': False,
        'browser_clicks_verified': False, 'provider_content_verified': False,
        'external_http_checked': False, 'external_generation_required': True}
    if any(value.get(key) != expected or type(value.get(key)) is not type(expected)
           for key, expected in fixed.items()):
        return None
    if value.get('state') not in {'blocked', 'unknown', 'completed'} or value.get('code') not in FILE_CODES:
        return None
    try:
        stamp = parse_datetime(value['checked_at']) if isinstance(value.get('checked_at'), str) else None
        if stamp is None or not timezone.is_aware(stamp):
            return None
    except (TypeError, ValueError):
        return None
    positives = ('file_input_processed', 'private_preview_verified', 'cover_order_saved',
        'editor_reopened', 'original_draft_restored', 'published_unchanged')
    if any(type(value.get(key)) is not bool for key in positives):
        return None
    if value['state'] == 'completed':
        if (value['code'] != 'file_check_completed' or any(value[key] is not True for key in positives)
                or type(value.get('draft_matches_published')) is not bool):
            return None
        if (type(value.get('asset_reused')) is not bool or type(value.get('new_storage_write')) is not bool
                or value['asset_reused'] == value['new_storage_write']):
            return None
        for key in ('revision_before', 'revision_after', 'first_version_id', 'second_version_id', 'restore_version_id'):
            if type(value.get(key)) is not int or not 0 < value[key] < 2**63:
                return None
        if (value['revision_after'] != value['revision_before'] + 3
                or len({value[key] for key in ('first_version_id', 'second_version_id', 'restore_version_id')}) != 3):
            return None
    return deepcopy(value)


def file_check_receipt(connection, operation_id):
    """Exact durable proof only; a local result never completes external r1."""
    try:
        current, actor, binding, feed, candidates, fingerprint, external, counts = _file_scope(connection)
        marker = current.settings.get(FILE_KEY)
        if (not isinstance(marker, dict) or marker.get('generation') != FILE_GENERATION
                or marker.get('state') != 'completed' or marker.get('operation_id') != operation_id
                or marker.get('actor') != actor.pk or marker.get('binding') != binding
                or marker.get('after_sha256') != _digest(feed.draft)
                or marker.get('published_sha256') != _digest(feed.published)
                or marker.get('revision_after') != feed.revision):
            return None
        result = _public_file_result(marker.get('result'))
        if result is None or result['state'] != 'completed':
            return None
        for offset, key in enumerate(('first_version_id', 'second_version_id', 'restore_version_id'), start=1):
            if not feed.versions.filter(pk=result[key], action='edit',
                    revision=result['revision_before'] + offset).exists():
                return None
        restored = feed.versions.get(pk=result['restore_version_id'])
        if (restored.snapshot != feed.draft
                or result['draft_matches_published'] != (feed.draft == feed.published)):
            return None
        return result
    except Exception:
        return None


def _reopen_file_editor(actor, connection, avito_id, expected):
    from html.parser import HTMLParser
    from .avito_services_feed import services_feed_edit
    class Photos(HTMLParser):
        def __init__(self):
            super().__init__(convert_charrefs=True)
            self.active, self.parts = False, []
        def handle_starttag(self, tag, attrs):
            if tag == 'textarea' and dict(attrs).get('name') == 'image_urls':
                self.active = True
        def handle_endtag(self, tag):
            if tag == 'textarea':
                self.active = False
        def handle_data(self, data):
            if self.active:
                self.parts.append(data)
    request = _request('GET', actor)
    request.session = {}
    response = services_feed_edit(request, connection.pk, avito_id)
    _file_require(response.status_code == 200)
    parser = Photos()
    parser.feed(response.content.decode('utf-8'))
    actual = ' | '.join(line.strip() for line in ''.join(parser.parts).strip().splitlines() if line.strip())
    _file_require(actual == expected)
    return True


def _run_file_check(payload, operation_id, new_files):
    """Exercise FILES only; any failed save/reopen/restore rolls back all draft writes."""
    from .avito_services_feed import entries
    locked = _locked_file_connection()
    connection, actor, binding, feed, candidates, fingerprint, external, counts = _file_scope(
        locked, payload['actor'])
    feed = AvitoServicesFeed.objects.select_for_update().get(pk=feed.pk)
    prior = connection.settings.get(FILE_KEY)
    _file_require(isinstance(prior, dict) and prior.get('operation_id') == operation_id)
    if prior.get('state') == 'completed':
        receipt = file_check_receipt(connection, operation_id)
        _file_require(receipt is not None, 'file_check_context_changed')
        return receipt
    _file_require(prior.get('state') == 'pending'
        and prior.get('actor') == actor.pk and prior.get('binding') == binding
        and feed.revision == payload['revision'] == prior.get('revision_before')
        and fingerprint == payload['fingerprint'] == prior.get('fingerprint'),
        'file_check_context_changed')
    selected = next(((row, asset) for row, asset in candidates
        if row['AvitoId'] == payload['avito_id'] and asset.pk == payload['asset_id']
        and asset.sha256 == payload['asset_sha256']), None)
    _file_require(selected is not None, 'file_check_context_changed')
    row, source_asset = selected
    raw = media._read_asset(source_asset)
    _file_require(raw and len(raw) == source_asset.byte_size and len(raw) <= media.MAX_BYTES)
    with Image.open(BytesIO(raw)) as source_image:
        _file_require(source_image.format == 'JPEG'
            and source_image.size == (source_asset.width, source_asset.height))
    before, published = deepcopy(feed.draft), deepcopy(feed.published)
    published_state = (feed.enabled, feed.published_revision, feed.published_photo_limit, feed.public_key)
    before_external = deepcopy(external)
    existing = set(AvitoServicesImage.objects.filter(connection=connection).values_list('pk', flat=True))
    upload = _request('POST', actor, {'revision': str(feed.revision), 'avito_id': row['AvitoId'],
        'photos': SimpleUploadedFile('approved-stored-photo.jpg', raw, content_type='image/jpeg')})
    response = media.services_image_upload(upload, connection.pk)
    created = list(AvitoServicesImage.objects.filter(connection=connection).exclude(pk__in=existing))
    new_files.extend((asset.file.storage, asset.file.name) for asset in created)
    _file_require(response.status_code == 200)
    values = json.loads(response.content)
    _file_require(isinstance(values, dict) and len(values.get('images', [])) == 1)
    uploaded = media._asset(connection, values['images'][0]['url'])
    _file_require(uploaded is not None)
    preview = media.services_image_preview(_request('GET', actor), 4, uploaded.public_key)
    try:
        _file_require(preview.status_code == 200 and preview['Content-Type'] == 'image/jpeg')
        preview_raw = b''.join(preview.streaming_content)
        _file_require(len(preview_raw) == uploaded.byte_size
            and hashlib.sha256(preview_raw).hexdigest() == uploaded.sha256)
    finally:
        preview.file_to_stream.close()
    original = row['ImageUrls'].split(' | ')
    others = [value for value in original if value != uploaded.public_url]
    _file_require(bool(others), 'file_check_needs_second_photo')
    covered = list(dict.fromkeys([uploaded.public_url] + others))[:10]
    alternate = list(dict.fromkeys([others[0], uploaded.public_url] + others[1:]))[:10]
    first_order, second_order = ((covered, alternate) if ' | '.join(covered) != row['ImageUrls']
                                 else (alternate, covered))
    _file_require(len(first_order) >= 2 and first_order != second_order
        and ' | '.join(first_order) != row['ImageUrls'])
    first = _save(actor, connection, feed, row, ' | '.join(first_order),
        'Проверка файловой загрузки: обложка и порядок согласованных фотографий; без публикации')
    _reopen_file_editor(actor, connection, row['AvitoId'], ' | '.join(first_order))
    second = _save(actor, connection, feed, row, ' | '.join(second_order),
        'Проверка файловой загрузки: смена обложки и порядка; без публикации')
    _reopen_file_editor(actor, connection, row['AvitoId'], ' | '.join(second_order))
    restored = _save(actor, connection, feed, row, row['ImageUrls'],
        'Восстановление исходного черновика после файловой проверки; без публикации')
    _reopen_file_editor(actor, connection, row['AvitoId'], row['ImageUrls'])
    _file_require(feed.draft == before and feed.published == published
        and (feed.enabled, feed.published_revision, feed.published_photo_limit, feed.public_key) == published_state
        and feed.revision == payload['revision'] + 3)
    connection, final_actor, final_binding, final_feed, _, _, final_external, _ = _file_scope(
        connection, payload['actor'])
    _file_require(final_actor.pk == actor.pk and final_binding == binding
        and final_feed.pk == feed.pk and final_external == before_external,
        'file_check_context_changed')
    result = _file_result('completed', 'file_check_completed')
    result.update(draft_matches_published=before == published,
        file_input_processed=True, asset_reused=uploaded.pk in existing,
        new_storage_write=uploaded.pk not in existing, private_preview_verified=True,
        cover_order_saved=True, editor_reopened=True, original_draft_restored=True,
        published_unchanged=True, revision_before=payload['revision'], revision_after=feed.revision,
        first_version_id=first.pk, second_version_id=second.pk, restore_version_id=restored.pk)
    settings = dict(connection.settings or {})
    settings[FILE_KEY] = {**prior, 'state': 'completed', 'revision_after': feed.revision,
        'after_sha256': _digest(feed.draft), 'result': result, 'finished_at': timezone.now().isoformat()}
    connection.settings = settings
    connection.save(update_fields=['settings'])
    return result


@csrf_exempt
@never_cache
@require_POST
def services_media_filecheck(request):
    try:
        payload, operation_id = _file_payload(request)
    except Exception:
        return JsonResponse({'error': 'file_check_denied'}, status=403)
    new_files = []
    try:
        with transaction.atomic():
            result = _run_file_check(payload, operation_id, new_files)
    except Exception:
        for storage, name in new_files:
            try:
                storage.delete(name)
            except OSError:
                pass
        return JsonResponse({'error': 'file_check_failed'}, status=409)
    return JsonResponse(result)



def _authorize_file_operation(expected_sha):
    import os
    import subprocess
    _file_require(os.environ.get('SERVICE2_AVITO_FILECHECK') == '1'
        and isinstance(expected_sha, str) and re.fullmatch(r'[0-9a-f]{40}', expected_sha),
        'file_check_release_denied')
    checks = (
        (['git', 'rev-parse', 'HEAD'], lambda value: value.strip() == expected_sha),
        (['git', 'show', '-s', '--format=%B', 'HEAD'], lambda value: FILE_MARKER in value),
        (['git', 'ls-remote', '--exit-code', 'origin', 'refs/heads/main'],
         lambda value: value.split() == [expected_sha, 'refs/heads/main']),
    )
    for command, valid in checks:
        result = subprocess.run(command, capture_output=True, timeout=20)
        _file_require(result.returncode == 0 and valid(result.stdout.decode('utf-8')),
                      'file_check_release_denied')
    result = subprocess.run(['git', 'diff', '--quiet', 'HEAD', '--', 'pool_service',
        'service_site', 'scripts', '.github', 'manage.py'], capture_output=True, timeout=5)
    _file_require(result.returncode == 0, 'file_check_release_denied')


def run_stored_file_check(connection, *, perform=False, expected_sha=None):
    """No implicit activation or external image download; one exact owned POST."""
    if not perform:
        return local_file_inventory(connection)
    try:
        _authorize_file_operation(expected_sha)
        token, receipt = _reserve_file_check(connection)
        if token is None:
            _file_require(receipt is not None, 'file_check_context_changed')
            return receipt
        operation_id = hashlib.sha256(token.encode()).hexdigest()
        from .avito_services_release_operation import _post_photo
        try:
            _post_photo(token, endpoint='avito_services_media_filecheck')
        except Exception:
            # A timeout is not evidence that the request did not commit.
            pass
        return file_check_receipt(connection, operation_id) or _file_result(
            'unknown', 'file_check_result_unknown')
    except FileCheckError as error:
        return _file_result('blocked', str(error))
    except Exception:
        return _file_result('unknown', 'file_check_result_unknown')
