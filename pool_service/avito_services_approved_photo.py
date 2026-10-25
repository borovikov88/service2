"""One owner-authorised photo change through the ordinary Services publisher.

No provider write, forced upload, source-setting change or customer message.
The stable feed remains the current source; immutable binding is evidence only.
"""
from copy import deepcopy
import hashlib
import json
import re
import secrets
import time
from urllib.parse import urlsplit

from django.db import transaction
from django.test import RequestFactory
from django.urls import reverse
from django.utils import timezone

from .avito_feed_models import AvitoServicesFeed
from .avito_media_models import AvitoServicesImage
from .communication_models import ChannelConnection
from .avito_services_workplace_setup import _actor, _binding, _same
from .avito_services_publication import public_origin, publication_release_check

GENERATION = 'services-approved-photo18-20261025'
MARKER = '[avito-services-approved-photo18-20261025]'
KEY = 'avito_services_approved_photo18_v1'
CONNECTION_ID = 4
ACCOUNT_ID = '446312402'
TARGET_ID = '8343962135'
WATER_ID = '8346503274'
DRAFT_REVISION = 18
PREVIOUS_REVISION = 15
SOURCE_VERSION_ID = 6
ASSET_KEY_SHA256 = '9416751d05f7db36dc6f666b35e3bc2e4c3ab4d8cc39308f68506802d27faa1d'
CODES = frozenset({
    'approved_photo_prepared', 'approved_photo_published', 'approved_photo_verified',
    'approved_photo_scope_invalid', 'approved_photo_actor_invalid',
    'approved_photo_changes_invalid', 'approved_photo_history_invalid',
    'approved_photo_asset_invalid', 'approved_photo_preview_invalid',
    'approved_photo_binding_changed', 'approved_photo_busy',
    'approved_photo_prior_result_unknown', 'approved_photo_preflight_failed',
    'approved_photo_public_readback_failed', 'approved_photo_internal_error',
    'approved_photo_source_not_current_stable',
})


class Blocked(ValueError):
    pass


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
        separators=(',', ':')).encode()).hexdigest()


def _result():
    return {'schema_version': 1, 'kind': 'avito_services_approved_photo',
        'generation': GENERATION, 'connection_id': CONNECTION_ID,
        'account_id': ACCOUNT_ID, 'target_avito_id': TARGET_ID,
        'expected_draft_revision': DRAFT_REVISION,
        'expected_previous_published_revision': PREVIOUS_REVISION,
        'checked_at': timezone.now().isoformat(), 'state': 'blocked',
        'code': 'approved_photo_scope_invalid', 'version_id': None,
        'published_revision': None, 'published_at': None, 'readback_checked_at': None, 'active_count': None,
        'asset_storage_verified': False, 'authorized_preview_verified': False,
        'xml_public_verified': None, 'image_public_verified': None,
        'legacy_diagnostics': [], 'retained_references': [],
        'provider_report': {'state': 'not_checked', 'upload_id': None, 'started_at': None,
            'complete': None, 'upload_status': '', 'target': None},
        'source_mode': 'stable_current', 'source_matches_current_stable': None,
        'configured_source_format': 'unknown', 'configured_source_public_verified': None,
        'facts': dict.fromkeys(('file_published', 'upload_found', 'source_confirmed',
                              'rows_processed', 'live_content_verified'), False)}


def _base(*, locked=False):
    from .communication_services import organization_access, conversation_capability
    from .communication_avito import _provider_root
    if _provider_root() != 'https://api.avito.ru':
        raise Blocked('approved_photo_scope_invalid')
    connections = ChannelConnection.objects.select_related('channel')
    feeds = AvitoServicesFeed.objects
    if locked:
        connections, feeds = connections.select_for_update(), feeds.select_for_update()
    connection = connections.filter(pk=CONNECTION_ID).first()
    if (connection is None or connection.external_id != ACCOUNT_ID
            or not connection.is_active or not connection.channel.is_active
            or connection.channel.kind != 'avito'):
        raise Blocked('approved_photo_scope_invalid')
    binding = _binding(connection)
    if not _same(connection, binding):
        raise Blocked('approved_photo_scope_invalid')
    try:
        actor = _actor(connection)
        access = organization_access(actor, connection.channel.organization)
        if (not actor.is_active or access is None or not conversation_capability(
                actor, 'can_manage_channels', connection.channel.organization)):
            raise ValueError
    except Exception:
        raise Blocked('approved_photo_actor_invalid') from None
    feed = feeds.filter(connection=connection, account_id=ACCOUNT_ID,
        organization_id_at_import=connection.channel.organization_id).first()
    if feed is None or not feed.enabled:
        raise Blocked('approved_photo_scope_invalid')
    return connection, actor, binding, feed


def _asset(connection, url):
    from .avito_services_media import _asset as scoped_asset
    asset = scoped_asset(connection, url)
    if (asset is None or hashlib.sha256(str(asset.public_key).encode()).hexdigest() != ASSET_KEY_SHA256
            or asset.width != 1536 or asset.height != 1152):
        raise Blocked('approved_photo_asset_invalid')
    return asset


def _approved_shape(connection, feed):
    from .avito_services_feed import entries, images
    if feed.revision != DRAFT_REVISION or feed.published_revision != PREVIOUS_REVISION:
        raise Blocked('approved_photo_changes_invalid')
    previous, current = entries(feed.published), entries(feed.draft)
    if (len(previous) != 11 or len(current) != 11
            or len({(row['Id'], row['AvitoId']) for row in previous}) != 11
            or {(row['Id'], row['AvitoId']) for row in current}
                != {(row['Id'], row['AvitoId']) for row in previous}):
        raise Blocked('approved_photo_changes_invalid')
    old_rows = [row for row in previous if row['AvitoId'] == TARGET_ID]
    new_rows = [row for row in current if row['AvitoId'] == TARGET_ID]
    if len(old_rows) != 1 or len(new_rows) != 1:
        raise Blocked('approved_photo_changes_invalid')
    old, new = old_rows[0], new_rows[0]
    old_images = images(old['ImageUrls']).split(' | ')
    new_images = images(new['ImageUrls']).split(' | ')
    if (len(old_images) != 10 or len(new_images) != 10 or old_images[:9] != new_images[:9]
            or old_images[9] == new_images[9]):
        raise Blocked('approved_photo_changes_invalid')
    expected = deepcopy(feed.published)
    for sheet in expected:
        for row in sheet['rows']:
            if row['AvitoId'] == TARGET_ID:
                row['ImageUrls'] = new['ImageUrls']
    if feed.draft != expected:
        raise Blocked('approved_photo_changes_invalid')
    source = feed.versions.filter(pk=SOURCE_VERSION_ID, action='edit',
        revision=DRAFT_REVISION).first()
    if (source is None or source.snapshot != feed.draft or len(source.changes) != 1
            or source.changes[0].get('avito_id') != TARGET_ID
            or source.changes[0].get('field') != 'ImageUrls'):
        raise Blocked('approved_photo_history_invalid')
    return _asset(connection, new_images[9])


def _stamp(connection, actor, binding, feed):
    return _digest({'binding': binding, 'actor_id': actor.pk, 'feed_id': feed.pk,
        'public_key': str(feed.public_key), 'draft': feed.draft, 'published': feed.published,
        'revision': feed.revision, 'published_revision': feed.published_revision,
        'photo_limit': feed.published_photo_limit})


def _preview(connection, actor, asset):
    from .avito_services_media import _read_asset, _image, services_image_preview, MAX_BYTES
    raw = _read_asset(asset)
    _image(raw, normalize=False)
    origin = public_origin()
    request = RequestFactory().get(reverse('avito_services_image_preview',
        args=[connection.pk, asset.public_key]), secure=True, HTTP_HOST=urlsplit(origin).netloc)
    request.user = actor
    response = services_image_preview(request, connection.pk, asset.public_key)
    try:
        if response.status_code != 200 or response.get('Content-Type') != 'image/jpeg':
            raise ValueError
        chunks, total = [], 0
        for chunk in response.streaming_content:
            total += len(chunk)
            if total > MAX_BYTES:
                raise ValueError
            chunks.append(chunk)
        preview = b''.join(chunks)
        if preview != raw or hashlib.sha256(preview).hexdigest() != asset.sha256:
            raise ValueError
    except Exception:
        raise Blocked('approved_photo_preview_invalid') from None
    finally:
        response.close()



def _profile_current(connection, feed):
    """Match the exact existing provider source in-process, never log its URL."""
    from .communication_avito import access_token, authorized_account_id
    from .avito_profile_source import fetch_profile_source, profile_data
    if authorized_account_id(connection) != ACCOUNT_ID:
        raise Blocked('approved_photo_scope_invalid')
    expected = {}
    for name, kind in (('avito_services_feed_public', 'xml'), ('avito_services_feed_public_xlsx', 'xlsx')):
        url = public_origin() + reverse(name, args=[feed.public_key])
        expected[profile_data({'autoload_enabled': True, 'feeds_data': [{'feed_url': url}]})[1]] = kind
    value, fingerprint = fetch_profile_source(access_token(connection), ACCOUNT_ID)
    valid = value.get('complete') is True and value.get('autoload_enabled') is True and fingerprint in expected
    return {'matches': valid, 'format': expected[fingerprint] if valid else 'unknown'}

def _reserve(expected_sha):
    with transaction.atomic():
        connection, actor, binding, feed = _base(locked=True)
        metadata = dict(connection.settings) if isinstance(connection.settings, dict) else {}
        marker = metadata.get(KEY)
        if marker is not None:
            if (not isinstance(marker, dict) or marker.get('generation') != GENERATION
                    or marker.get('binding') != binding or marker.get('actor_id') != actor.pk):
                raise Blocked('approved_photo_binding_changed')
            if marker.get('state') in {'published', 'verified'}:
                return connection, actor, binding, feed, marker, True
            # Never take over an incomplete invocation or blindly republish.
            if marker.get('state') != 'failed':
                raise Blocked('approved_photo_prior_result_unknown')
        asset = _approved_shape(connection, feed)
        token = secrets.token_hex(16)
        marker = {'generation': GENERATION, 'binding': binding, 'actor_id': actor.pk,
            'expected_sha': expected_sha, 'token': token, 'state': 'reserved',
            'source_stamp': _stamp(connection, actor, binding, feed),
            'asset_id': asset.pk, 'asset_sha256': asset.sha256,
            'created_at': timezone.now().isoformat(), 'version_id': None}
        metadata[KEY] = marker
        connection.settings = metadata
        connection.save(update_fields=['settings'])
        return connection, actor, binding, feed, marker, False


def _guard(marker):
    def check(connection, feed):
        current, actor, binding, active = _base()
        saved = current.settings.get(KEY) if isinstance(current.settings, dict) else None
        if (current.pk != connection.pk or active.pk != feed.pk
                or binding != marker['binding'] or actor.pk != marker['actor_id']
                or not isinstance(saved, dict) or saved.get('token') != marker['token']
                or saved.get('state') != 'reserved'
                or _stamp(current, actor, binding, feed) != marker['source_stamp']):
            raise Blocked('approved_photo_binding_changed')
        asset = _approved_shape(current, feed)
        if asset.pk != marker['asset_id'] or asset.sha256 != marker['asset_sha256']:
            raise Blocked('approved_photo_asset_invalid')
        return True
    return check


def _published(marker):
    def save(connection, feed, version):
        if not transaction.get_connection().in_atomic_block:
            raise Blocked('approved_photo_internal_error')
        metadata = dict(connection.settings)
        saved = metadata.get(KEY)
        if (not isinstance(saved, dict) or saved.get('token') != marker['token']
                or saved.get('state') != 'reserved' or _binding(connection) != marker['binding']
                or version.feed_id != feed.pk or version.action != 'publish'
                or version.source_version_id != SOURCE_VERSION_ID
                or version.revision != DRAFT_REVISION
                or feed.published_revision != DRAFT_REVISION
                or len(version.observations) != 11
                or any(row.get('status') != 'active' or row.get('included') is not True
                       for row in version.observations)
                or version.snapshot != feed.published or feed.published != feed.draft):
            raise Blocked('approved_photo_binding_changed')
        saved = {**saved, 'state': 'published', 'version_id': version.pk,
            'file_sha256': version.file_digest, 'published_at': version.created_at.isoformat(),
            'asset_storage_verified': True, 'authorized_preview_verified': True}
        metadata[KEY] = saved
        connection.settings = metadata
        connection.save(update_fields=['settings'])
    return save


def _current_publication(marker):
    from .avito_services_feed import entries, images
    from .avito_services_xml import xml_bytes
    connection, actor, binding, feed = _base()
    saved = connection.settings.get(KEY) if isinstance(connection.settings, dict) else None
    if (not isinstance(saved, dict) or saved.get('token') != marker['token']
            or saved.get('state') not in {'published', 'verified'}
            or saved.get('binding') != binding or saved.get('actor_id') != actor.pk
            or feed.published_revision != DRAFT_REVISION):
        raise Blocked('approved_photo_binding_changed')
    version = feed.versions.filter(pk=saved.get('version_id'), action='publish',
        revision=DRAFT_REVISION).first()
    if (version is None or version.source_version_id != SOURCE_VERSION_ID
            or version.snapshot != feed.published or version.file_digest != saved.get('file_sha256')):
        raise Blocked('approved_photo_binding_changed')
    target = next((row for row in entries(version.snapshot) if row['AvitoId'] == TARGET_ID), None)
    if target is None:
        raise Blocked('approved_photo_binding_changed')
    asset = _asset(connection, images(target['ImageUrls']).split(' | ')[9])
    if (asset.pk != saved.get('asset_id') or asset.sha256 != saved.get('asset_sha256')
            or asset.published_in_feed_at is None):
        raise Blocked('approved_photo_asset_invalid')
    content = xml_bytes(version.snapshot)
    if hashlib.sha256(content).hexdigest() != saved['file_sha256']:
        raise Blocked('approved_photo_binding_changed')
    return connection, feed, version, asset, content



def _capture_report(connection, feed, version, marker):
    from .communication_avito import access_token, authorized_account_id
    from .avito_autoload_report import fetch_report
    from .avito_services_publication import _date, correlate_report
    result = {'state': 'unavailable', 'upload_id': None, 'started_at': None,
              'complete': None, 'upload_status': '', 'target': None}
    try:
        if authorized_account_id(connection) != ACCOUNT_ID:
            return result
        report = fetch_report(access_token(connection), ACCOUNT_ID, kind='last_successful',
            include_input_proof=True, input_proof_connection=connection, allow_shop_recovery=False)
        started = _date(report.get('started_at'))
        if started is None:
            return result
        result.update(upload_id=report['upload_id'], started_at=started.isoformat(),
            complete=report.get('complete') is True, upload_status=report.get('upload_status', ''))
        target = next((row for row in report.get('rows', [])
                       if str(row.get('avito_id')) == TARGET_ID), None)
        if target is not None:
            result['target'] = {'avito_id': TARGET_ID, 'status': target.get('avito_status'),
                'messages': [{'type': row['type'], 'code': row['code']}
                             for row in target.get('messages', [])]}
        if started <= version.created_at:
            # An older upload is displayed as historical, never attached as a
            # new version's application or inserted into its attempt history.
            result['state'] = 'predates_publication'
        elif report.get('complete') is not True or report.get('upload_finished') is not True:
            result['state'] = 'later_unfinished'
        else:
            result['state'] = 'later_completed'
            with transaction.atomic():
                current, actor, binding, locked = _base(locked=True)
                saved = current.settings.get(KEY)
                if (not isinstance(saved, dict) or saved.get('token') != marker['token']
                        or saved.get('version_id') != version.pk
                        or locked.published_revision != DRAFT_REVISION):
                    raise Blocked('approved_photo_binding_changed')
                correlate_report(current, report)
    except Blocked:
        raise
    except Exception:
        pass
    return result

def _readback(marker, result):
    from .avito_input_copy import _download_url, MAX_SECONDS
    from .avito_services_media import _download
    connection, feed, version, asset, content = _current_publication(marker)
    result.update(state='published', code='approved_photo_published', version_id=version.pk,
        published_revision=DRAFT_REVISION, published_at=version.created_at.isoformat(), active_count=11,
        asset_storage_verified=True, authorized_preview_verified=True)
    result['facts'] = publication_release_check(connection, revision=DRAFT_REVISION)['facts']
    try:
        source = _profile_current(connection, feed)
        result['source_matches_current_stable'] = source['matches']
        result['configured_source_format'] = source['format']
    except Exception:
        result['source_matches_current_stable'] = None
    try:
        url = public_origin() + reverse('avito_services_feed_public', args=[feed.public_key])
        part = urlsplit(url)
        raw = _download_url(part, part.hostname, time.monotonic() + MAX_SECONDS)
        result['xml_public_verified'] = raw == content
    except Exception:
        result['xml_public_verified'] = False
    try:
        raw = _download(asset.public_url, time.monotonic() + 10)
        result['image_public_verified'] = hashlib.sha256(raw).hexdigest() == asset.sha256
    except Exception:
        result['image_public_verified'] = False
    if result['configured_source_format'] == 'xml':
        result['configured_source_public_verified'] = result['xml_public_verified']
    elif result['configured_source_format'] == 'xlsx':
        try:
            from .avito_services_feed import parse_export
            from .avito_services_xml import xml_bytes
            url = public_origin() + reverse('avito_services_feed_public_xlsx', args=[feed.public_key])
            part = urlsplit(url)
            workbook = _download_url(part, part.hostname, time.monotonic() + MAX_SECONDS)
            result['configured_source_public_verified'] = xml_bytes(parse_export(workbook)) == content
        except Exception:
            result['configured_source_public_verified'] = False
    result['provider_report'] = _capture_report(connection, feed, version, marker)
    result['facts'] = publication_release_check(connection, revision=DRAFT_REVISION)['facts']
    result['readback_checked_at'] = timezone.now().isoformat()
    # A concurrent later publication must not inherit this readback.
    _current_publication(marker)
    if (result['xml_public_verified'] and result['image_public_verified']
            and result['source_matches_current_stable'] is True
            and result['configured_source_public_verified'] is True):
        result.update(state='verified', code='approved_photo_verified')
    else:
        result['code'] = 'approved_photo_public_readback_failed'
    with transaction.atomic():
        current, actor, binding, locked_feed = _base(locked=True)
        metadata = dict(current.settings)
        saved = metadata.get(KEY)
        if (not isinstance(saved, dict) or saved.get('token') != marker['token']
                or saved.get('version_id') != version.pk or saved.get('binding') != binding):
            raise Blocked('approved_photo_binding_changed')
        metadata[KEY] = {**saved, 'state': result['state'],
            'readback_checked_at': result['readback_checked_at'],
            'xml_public_verified': result['xml_public_verified'],
            'image_public_verified': result['image_public_verified']}
        current.settings = metadata
        current.save(update_fields=['settings'])
    return result


def _failed(marker, code):
    if marker is None:
        return
    with transaction.atomic():
        connection = ChannelConnection.objects.select_for_update().filter(pk=CONNECTION_ID).first()
        if connection is None:
            return
        metadata = dict(connection.settings) if isinstance(connection.settings, dict) else {}
        saved = metadata.get(KEY)
        if (not isinstance(saved, dict) or saved.get('token') != marker.get('token')
                or saved.get('state') in {'published', 'verified'}):
            return
        metadata[KEY] = {**saved, 'state': 'failed', 'code': code,
            'finished_at': timezone.now().isoformat()}
        connection.settings = metadata
        connection.save(update_fields=['settings'])


def run_approved_photo_publication(expected_sha):
    from .avito_services_feed import publish_services_snapshot, entries
    from .avito_services_media import diagnose_snapshot_media
    result, marker = _result(), None
    if not isinstance(expected_sha, str) or not re.fullmatch('[0-9a-f]{40}', expected_sha):
        return result
    try:
        connection, actor, binding, feed, marker, repeated = _reserve(expected_sha)
        if repeated:
            return _readback(marker, result)
        asset = _approved_shape(connection, feed)
        _preview(connection, actor, asset)
        result.update(asset_storage_verified=True, authorized_preview_verified=True)
        source = _profile_current(connection, feed)
        result['source_matches_current_stable'] = source['matches']
        result['configured_source_format'] = source['format']
        if result['source_matches_current_stable'] is not True:
            raise Blocked('approved_photo_source_not_current_stable')
        result['legacy_diagnostics'] = diagnose_snapshot_media(
            connection, feed.published, target_ids=(TARGET_ID, WATER_ID), seconds=10)
        published = publish_services_snapshot(connection, actor,
            expected_revision=DRAFT_REVISION, expected_guard=_guard(marker),
            published_callback=_published(marker))
        observations = published['observations']
        if (len(observations) != 11 or any(row.get('status') != 'active' for row in observations)
                or len(entries(published['feed'].published)) != 11):
            # This check is also enforced by the atomic callback below; retain
            # defensive failure here without any second publication attempt.
            raise Blocked('approved_photo_preflight_failed')
        result['active_count'] = 11
        result['retained_references'] = (published.get('media') or {}).get('retained', [])
        return _readback(marker, result)
    except Exception as exc:
        code = str(exc) if isinstance(exc, Blocked) and str(exc) in CODES else 'approved_photo_preflight_failed'
        result.update(state='blocked' if isinstance(exc, ValueError) else 'unknown', code=code)
        # A committed publication is authoritative even if later HTTP failed.
        if marker is not None:
            try:
                _current_publication(marker)
                return _readback(marker, result)
            except Exception:
                pass
        _failed(marker, code)
        return result
