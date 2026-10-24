"""Protected, one-generation services release check; never publishes an Avito file."""
from datetime import timedelta
import hashlib
import http.client
import json
import re
import secrets
import socket
import threading
import time
from urllib.parse import urlsplit

from django.db import transaction
from django.urls import reverse
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from .communication_avito import authorized_account_id, _provider_root
from .communication_models import ChannelConnection
from .avito_feed_models import AvitoServicesFeed
from .avito_services_publication import public_origin, publication_release_check
from .avito_services_workplace_setup import KEY as SETUP_KEY, _actor, _binding, _same, _origin

GENERATION = 'services-v15-20261024-r1'
KEY = 'avito_services_release_operation_v1'
CONNECTION_ID = 4
ACCOUNT_ID = '446312402'
PUBLISHED_REVISION = 15
LEASE_SECONDS = 120
CODES = frozenset({
    'release_scope_invalid', 'release_generation_invalid', 'release_actor_invalid',
    'release_account_mismatch', 'release_preflight_failed', 'release_photo_source_unavailable',
    'release_busy', 'release_photo_pending', 'release_completed', 'release_photo_failed',
    'release_photo_unknown', 'release_binding_changed', 'release_media_receipt_invalid',
    'release_internal_error',
})


class Blocked(ValueError):
    pass


def _result(state='blocked', code='release_scope_invalid'):
    return {'schema_version': 1, 'kind': 'avito_services_release_operation',
        'generation': GENERATION, 'connection_id': CONNECTION_ID, 'account_id': ACCOUNT_ID,
        'expected_published_revision': PUBLISHED_REVISION,
        'checked_at': timezone.now().isoformat(), 'state': state, 'code': code,
        'publication_preflight': None, 'photo_source': None, 'photo_proof': None}


def _date(value):
    try:
        parsed = parse_datetime(value) if isinstance(value, str) else None
        return parsed if parsed is not None and timezone.is_aware(parsed) else None
    except ValueError:
        return None


def _valid_sheets(sheets):
    if not isinstance(sheets, list):
        return False
    ids, avito_ids = set(), set()
    for sheet in sheets:
        if not isinstance(sheet, dict) or not isinstance(sheet.get('rows'), list):
            return False
        for row in sheet['rows']:
            if (not isinstance(row, dict) or row.get('Category') != 'Предложение услуг'
                    or not isinstance(row.get('Id'), str) or not 0 < len(row['Id']) <= 100
                    or not isinstance(row.get('AvitoId'), str)
                    or not re.fullmatch(r'[1-9][0-9]{0,19}', row['AvitoId'])
                    or row['Id'] in ids or row['AvitoId'] in avito_ids):
                return False
            ids.add(row['Id'])
            avito_ids.add(row['AvitoId'])
            if len(ids) > 200:
                return False
    return bool(ids)


def _context(*, locked=False):
    query = ChannelConnection.objects.select_related('channel')
    if locked:
        query = query.select_for_update()
    connection = query.filter(pk=CONNECTION_ID).first()
    if (connection is None or _provider_root() != 'https://api.avito.ru'
            or connection.external_id != ACCOUNT_ID):
        raise Blocked('release_scope_invalid')
    binding = _binding(connection)
    if not _same(connection, binding):
        raise Blocked('release_scope_invalid')
    metadata = connection.settings if isinstance(connection.settings, dict) else {}
    setup = metadata.get(SETUP_KEY)
    if (not isinstance(setup, dict) or setup.get('automatic_reports_configured_once') is not True
            or any(setup.get(key) != value for key, value in binding.items())):
        raise Blocked('release_actor_invalid')
    try:
        actor = _actor(connection)
    except Exception:
        raise Blocked('release_actor_invalid') from None
    feeds = AvitoServicesFeed.objects
    if locked:
        feeds = feeds.select_for_update()
    feed = feeds.filter(connection=connection, account_id=ACCOUNT_ID,
        organization_id_at_import=connection.channel.organization_id).first()
    if (feed is None or not feed.enabled or feed.published_revision != PUBLISHED_REVISION
            or not _valid_sheets(feed.draft) or not _valid_sheets(feed.published)):
        raise Blocked('release_scope_invalid')
    stamp = hashlib.sha256(json.dumps({
        'binding': binding, 'actor_id': actor.pk, 'public_key': str(feed.public_key),
        'published_revision': feed.published_revision,
        'photo_limit': feed.published_photo_limit, 'draft': feed.draft, 'published': feed.published,
    }, sort_keys=True, ensure_ascii=False, separators=(',', ':')).encode()).hexdigest()
    return connection, actor, binding, stamp


def _matches(marker, binding, stamp):
    return (isinstance(marker, dict) and marker.get('generation') == GENERATION
            and marker.get('binding') == binding and marker.get('source_stamp') == stamp)


def _save(lease, binding, stamp, *, require_original_revision=False, **updates):
    """Reload settings after HTTP: preserve the endpoint's independent receipt."""
    with transaction.atomic():
        connection, actor, current_binding, current_stamp = _context(locked=True)
        metadata = dict(connection.settings)
        marker = metadata.get(KEY)
        if (current_binding != binding or current_stamp != stamp or
                not _matches(marker, binding, stamp) or marker.get('lease') != lease):
            raise Blocked('release_binding_changed')
        if require_original_revision:
            feed = AvitoServicesFeed.objects.get(connection=connection)
            if feed.revision != marker.get('draft_revision'):
                raise Blocked('release_binding_changed')
        marker = dict(marker)
        marker.update(updates)
        metadata[KEY] = marker
        connection.settings = metadata
        connection.save(update_fields=['settings'])
        return connection


def _post_photo(token):
    """POST only a signed capability to our fixed canonical first-party endpoint."""
    from .avito_services_media import _resolve, _PinnedHTTPS, _public_ip
    origin = _origin(public_origin())
    parts = urlsplit(origin)
    host = parts.hostname.encode('idna').decode('ascii').lower()
    path = reverse('avito_services_media_livecheck')
    deadline = time.monotonic() + 45
    address = _resolve(host, 443, deadline)
    if not _public_ip(address):
        raise ValueError
    client = _PinnedHTTPS(host, 443, address, min(5, max(.05, deadline - time.monotonic())))
    client.auto_open = 0
    response = None
    aborted = threading.Event()
    def abort():
        aborted.set()
        sock = getattr(client, '_pinned_socket', None) or client.sock
        if sock is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        client.close()
    timer = threading.Timer(max(.05, deadline - time.monotonic()), abort)
    timer.daemon = True
    timer.start()
    try:
        client.connect()
        client.sock.settimeout(max(.05, deadline - time.monotonic()))
        if aborted.is_set():
            raise ValueError
        body = json.dumps({'token': token}, separators=(',', ':')).encode()
        client.request('POST', path, body=body, headers={'Host': host,
            'Content-Type': 'application/json', 'Accept': 'application/json',
            'Accept-Encoding': 'identity', 'User-Agent': 'Service2-Protected-Release-Check/1'})
        response = client.getresponse()
        if (response.status != 200
                or response.getheader('Content-Encoding', 'identity').lower() not in ('', 'identity')):
            raise ValueError
        raw = response.read(8193)
        if len(raw) > 8192 or aborted.is_set() or time.monotonic() >= deadline:
            raise ValueError
        value = json.loads(raw)
        if not isinstance(value, dict):
            raise ValueError
        return value
    finally:
        timer.cancel()
        if response is not None:
            response.close()
        client.close()


def _digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
        separators=(',', ':')).encode()).hexdigest()


def _selected_id(feed):
    from .avito_services_feed import entries
    for row in entries(feed.draft):
        published = next((item for item in entries(feed.published)
            if item['Id'] == row['Id'] and item['AvitoId'] == row['AvitoId']), None)
        if published and any(url in published.get('ImageUrls', '').split(' | ')
                for url in row.get('ImageUrls', '').split(' | ') if url):
            return row['AvitoId']
    raise Blocked('release_photo_source_unavailable')


def _source(value):
    names = {'scope_valid', 'source', 'source_in_published', 'photo_count', 'readable',
             'format', 'width', 'height', 'bytes', 'checked_at'}
    if (not isinstance(value, dict) or set(value) != names
            or value['scope_valid'] is not True or value['source_in_published'] is not True
            or value['readable'] is not True or value['source'] != 'existing_services_draft_image'
            or value['format'] not in {'JPEG', 'PNG', 'WEBP'}
            or _date(value['checked_at']) is None):
        raise Blocked('release_photo_source_unavailable')
    for name, maximum in (('photo_count', 10), ('width', 20_000_000),
                          ('height', 20_000_000), ('bytes', 8 * 1024 * 1024)):
        if type(value[name]) is not int or not 0 < value[name] <= maximum:
            raise Blocked('release_photo_source_unavailable')
    return dict(value)


def _reserve(expected_sha):
    with transaction.atomic():
        connection, actor, binding, stamp = _context(locked=True)
        feed = AvitoServicesFeed.objects.get(connection=connection)
        metadata = dict(connection.settings)
        previous = metadata.get(KEY)
        now = timezone.now()
        if previous:
            if not _matches(previous, binding, stamp):
                raise Blocked('release_binding_changed')
            expiry = _date(previous.get('lease_until'))
            if expiry is not None and expiry > now:
                raise Blocked('release_busy')
            marker = dict(previous)
        else:
            marker = {'generation': GENERATION, 'binding': binding, 'source_stamp': stamp,
                'actor_id': actor.pk, 'expected_sha': expected_sha, 'created_at': now.isoformat(),
                'draft_revision': feed.revision, 'before_draft_sha256': _digest(feed.draft),
                'published_sha256': _digest(feed.published), 'avito_id': _selected_id(feed),
                'operation_id': None, 'phase': 'preflight', 'public_receipt': _result()}
        lease = secrets.token_hex(16)
        marker.update(lease=lease, lease_until=(now + timedelta(seconds=LEASE_SECONDS)).isoformat())
        metadata[KEY] = marker
        connection.settings = metadata
        connection.save(update_fields=['settings'])
        return connection, actor, binding, stamp, lease, marker


def _validated_photo(connection, marker, value):
    """Only durable exact-operation proof + current content/revision/history can complete."""
    if not isinstance(value, dict):
        return None
    feed = AvitoServicesFeed.objects.get(connection=connection)
    positive = ('upload_saved', 'private_preview_verified', 'cover_order_saved',
                'original_draft_restored', 'published_unchanged')
    negative = ('image_publicly_available', 'browser_clicks_verified', 'provider_content_verified')
    if (value.get('connection_id') != CONNECTION_ID or value.get('account_id') != ACCOUNT_ID
            or value.get('operation_id') != marker.get('operation_id')
            or value.get('avito_id') != marker.get('avito_id')
            or any(value.get(key) is not True for key in positive)
            or any(value.get(key) is not False for key in negative)
            or _date(value.get('checked_at')) is None
            or value.get('before_draft_sha256') != marker.get('before_draft_sha256')
            or value.get('after_draft_sha256') != marker.get('before_draft_sha256')
            or value.get('after_draft_sha256') != _digest(feed.draft)
            or value.get('published_sha256') != marker.get('published_sha256')
            or value.get('published_sha256') != _digest(feed.published)
            or type(value.get('revision_before')) is not int
            or value['revision_before'] != marker.get('draft_revision')
            or type(value.get('revision_after')) is not int
            or value['revision_after'] != value['revision_before'] + 2
            or value['revision_after'] != feed.revision):
        raise Blocked('release_media_receipt_invalid')
    for key, revision in (('edit_version_id', value['revision_before'] + 1),
                          ('restore_version_id', value['revision_after'])):
        if (type(value.get(key)) is not int or value[key] <= 0
                or not feed.versions.filter(pk=value[key], action='edit', revision=revision).exists()):
            raise Blocked('release_media_receipt_invalid')
    restored = feed.versions.get(pk=value['restore_version_id'])
    if restored.snapshot != feed.draft:
        raise Blocked('release_media_receipt_invalid')
    return {'connection_id': CONNECTION_ID, 'account_id': ACCOUNT_ID,
        'checked_at': value['checked_at'], 'source': _source(value.get('source')),
        **{key: True for key in positive}, **{key: False for key in negative},
        **{key: value[key] for key in ('revision_before', 'revision_after',
                                      'edit_version_id', 'restore_version_id')}}


def run_release_operation(expected_sha, release_generation):
    """One generation may POST once; uncertain/repeated runs only reconcile.

    completed confirms this photo draft/restore operation, never file ingestion
    or live card content at Avito. Publication facts remain independent.
    """
    result = _result()
    if (release_generation != GENERATION or not isinstance(expected_sha, str)
            or not re.fullmatch(r'[0-9a-f]{40}', expected_sha)):
        result['code'] = 'release_generation_invalid'
        return result
    lease = binding = stamp = marker = None
    try:
        connection, actor, binding, stamp, lease, marker = _reserve(expected_sha)
        result['publication_preflight'] = publication_release_check(
            connection, revision=PUBLISHED_REVISION, check_http=True)
        if marker.get('operation_id') is not None:
            # Never mint a new capability or replay HTTP after durable pending.
            saved = marker.get('public_receipt')
            if isinstance(saved, dict):
                source = saved.get('photo_source')
                if source is not None:
                    result['photo_source'] = _source(source)
        else:
            if authorized_account_id(connection) != ACCOUNT_ID:
                raise Blocked('release_account_mismatch')
            preflight = result['publication_preflight']
            if not (preflight.get('configured') is True
                    and preflight.get('published_revision') == PUBLISHED_REVISION):
                raise Blocked('release_preflight_failed')
            from .avito_services_media_livecheck import (
                media_release_source, make_media_livecheck_token)
            result['photo_source'] = _source(media_release_source(connection))
            current, current_actor, current_binding, current_stamp = _context()
            feed = AvitoServicesFeed.objects.get(connection=current)
            if (current_binding != binding or current_stamp != stamp or current_actor.pk != actor.pk
                    or feed.revision != marker['draft_revision']):
                raise Blocked('release_binding_changed')
            token = make_media_livecheck_token(current, current_actor, marker['avito_id'])
            operation_id = hashlib.sha256(token.encode('utf-8')).hexdigest()
            result.update(state='unknown', code='release_photo_pending')
            # Durable generation and operation_id exist BEFORE any POST.
            _save(lease, binding, stamp, require_original_revision=True, operation_id=operation_id,
                  phase='pending', public_receipt=result)
            marker = dict(marker, operation_id=operation_id)
            try:
                _post_photo(token)
            except Exception:
                pass  # No retry: timeout is not evidence the operation did not commit.
        from .avito_services_media_livecheck import media_livecheck_receipt
        current, current_actor, current_binding, current_stamp = _context()
        if current_binding != binding or current_stamp != stamp or current_actor.pk != actor.pk:
            raise Blocked('release_binding_changed')
        proof = _validated_photo(current, marker,
                                 media_livecheck_receipt(current, marker['operation_id']))
        if proof is None:
            result.update(state='unknown', code='release_photo_unknown')
        else:
            result.update(state='completed', code='release_completed', photo_proof=proof)
        _save(lease, binding, stamp, phase=result['state'], public_receipt=result,
              lease_until=None)
        return result
    except Blocked as exc:
        code = str(exc) if str(exc) in CODES else 'release_internal_error'
        result.update(state='busy' if code == 'release_busy' else 'blocked', code=code)
    except Exception:
        result.update(state='unknown' if marker and marker.get('operation_id') else 'failed',
                      code='release_internal_error')
    if lease is not None:
        try:
            _save(lease, binding, stamp, phase=result['state'], public_receipt=result,
                  lease_until=None)
        except Exception:
            pass
    return result
