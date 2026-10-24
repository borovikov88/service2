"""One scoped normal publication of the unchanged approved services snapshot.

Future operator only: requires both merge markers and a real current r1 photo
receipt. No provider writes, forced uploads, reactivation or paid actions.
"""
from copy import deepcopy
from datetime import timedelta
import hashlib
import json
import re
import secrets

from django.db import transaction
from django.core.exceptions import PermissionDenied
from django.http import Http404
from django.test import RequestFactory
from django.utils import timezone

from .communication_models import ChannelConnection
from .communication_avito import _provider_root, AvitoError
from .avito_feed_models import AvitoServicesFeed, AvitoServicesFeedVersion
from .avito_publication_models import AvitoServicesPublication
from .avito_services_publication import _binding_valid, publication_release_check, public_origin
from .avito_services_workplace_setup import KEY as SETUP_KEY, _actor, _binding, _same
from .avito_services_xml import xml_bytes

GENERATION = 'services-source-pin-r2-20261024'
KEY = 'avito_services_source_pin_r2'
R1_MARKER = '[avito-services-release-v15-20261024]'
R2_MARKER = '[avito-services-source-pin-r2-20261024]'
CONNECTION_ID = 4
ACCOUNT_ID = '446312402'
SOURCE_REVISION = 15
CODES = frozenset({
    'source_pin_generation_invalid', 'source_pin_scope_invalid', 'source_pin_actor_invalid',
    'source_pin_source_changed', 'source_pin_pending_draft', 'source_pin_photo_required',
    'source_pin_busy', 'source_pin_inventory_changed', 'source_pin_binding_changed',
    'source_pin_already_bound', 'source_pin_published', 'source_pin_already_published',
    'source_pin_http_unconfirmed', 'source_pin_provider_unavailable',
    'source_pin_internal_error', 'source_pin_ledger_invalid',
})


class SourcePinError(ValueError):
    pass


def _result(state='blocked', code='source_pin_scope_invalid'):
    return {'schema_version': 1, 'kind': 'avito_services_source_pin', 'generation': GENERATION,
        'connection_id': CONNECTION_ID, 'account_id': ACCOUNT_ID, 'source_revision': SOURCE_REVISION,
        'checked_at': timezone.now().isoformat(), 'state': state, 'code': code,
        'same_content': None, 'all_ids_active': None, 'excluded_count': None,
        'photo_before_publication': None, 'publication': None}


def _hash(value):
    return hashlib.sha256(value).hexdigest()


def _json_hash(value):
    return _hash(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode())


def _pairs_hash(pairs):
    return _json_hash([list(pair) for pair in sorted(pairs)])


def _request(actor):
    from urllib.parse import urlsplit
    part = urlsplit(public_origin())
    request = RequestFactory().post('/', secure=True, HTTP_HOST=part.netloc)
    request.user = actor
    return request


def _context(*, locked=False):
    query = ChannelConnection.objects.select_related('channel')
    if locked:
        query = query.select_for_update()
    connection = query.filter(pk=CONNECTION_ID).first()
    if (connection is None or connection.external_id != ACCOUNT_ID
            or _provider_root() != 'https://api.avito.ru'):
        raise SourcePinError('source_pin_scope_invalid')
    binding = _binding(connection)
    if not _same(connection, binding):
        raise SourcePinError('source_pin_scope_invalid')
    setup = connection.settings.get(SETUP_KEY) if isinstance(connection.settings, dict) else None
    if (not isinstance(setup, dict) or setup.get('automatic_reports_configured_once') is not True
            or any(setup.get(key) != value for key, value in binding.items())):
        raise SourcePinError('source_pin_binding_changed')
    try:
        actor = _actor(connection)
    except Exception:
        raise SourcePinError('source_pin_actor_invalid') from None
    # Preserve the ordinary editor's scope and channel-management capability.
    from .avito_services_feed import connection_for, bound
    scoped = connection_for(_request(actor), CONNECTION_ID)
    feeds = AvitoServicesFeed.objects
    if locked:
        feeds = feeds.select_for_update()
    feed = feeds.filter(connection=connection).first()
    if feed is None or not bound(feed, scoped) or not feed.enabled:
        raise SourcePinError('source_pin_scope_invalid')
    return connection, actor, feed, binding


def _snapshot(feed):
    if feed.published_revision != SOURCE_REVISION:
        raise SourcePinError('source_pin_source_changed')
    if feed.draft != feed.published:
        raise SourcePinError('source_pin_pending_draft')
    try:
        content = xml_bytes(feed.published, max_images=10)
        if xml_bytes(feed.draft, max_images=10) != content:
            raise SourcePinError('source_pin_pending_draft')
    except ValueError as exc:
        if isinstance(exc, SourcePinError):
            raise
        raise SourcePinError('source_pin_pending_draft') from None
    from .avito_services_feed import entries
    pairs = frozenset((row['Id'], row['AvitoId']) for row in entries(feed.published))
    return content, pairs


def _expected(connection, actor, feed, binding):
    content, pairs = _snapshot(feed)
    return {'binding': binding, 'actor_id': actor.pk, 'feed_id': feed.pk,
        'public_key': str(feed.public_key), 'draft_revision': feed.revision,
        'published_revision': feed.published_revision, 'photo_limit': feed.published_photo_limit,
        'draft_sha256': _json_hash(feed.draft), 'published_sha256': _json_hash(feed.published),
        'file_sha256': _hash(content), 'pairs_sha256': _pairs_hash(pairs), 'row_count': len(pairs)}


def _photo_before_publication(connection, expected_sha):
    from . import avito_services_release_operation as r1
    from .avito_services_media_livecheck import media_livecheck_receipt
    current, actor, binding, stamp = r1._context()
    marker = current.settings.get(r1.KEY)
    proof = None
    if r1._matches(marker, binding, stamp) and isinstance(marker.get('operation_id'), str):
        proof = r1._validated_photo(current, marker, media_livecheck_receipt(current, marker['operation_id']))
    if proof is None:
        # The existing r1 command's semantics/guards remain unchanged. A pending
        # operation is reconciled there; it never mints/posts a replacement.
        result = r1.run_release_operation(expected_sha, r1.GENERATION)
        if result.get('state') != 'completed':
            raise SourcePinError('source_pin_photo_required')
        current, actor, binding, stamp = r1._context()
        marker = current.settings.get(r1.KEY)
        if not r1._matches(marker, binding, stamp) or not isinstance(marker.get('operation_id'), str):
            raise SourcePinError('source_pin_photo_required')
        proof = r1._validated_photo(current, marker, media_livecheck_receipt(current, marker['operation_id']))
    if proof is None:
        raise SourcePinError('source_pin_photo_required')
    return proof


class _Approval:
    def __init__(self, expected, lease, photo):
        self.expected, self.lease, self.photo = expected, lease, photo
        self.all_ids_active, self.excluded_count = None, None

    def assert_current(self, request, connection, feed, *, locked):
        current_binding = _binding(connection)
        expected = self.expected
        try:
            actor = _actor(connection)
        except Exception:
            raise SourcePinError('source_pin_actor_invalid') from None
        if (not _same(connection, expected['binding']) or current_binding != expected['binding']
                or actor.pk != expected['actor_id'] or request.user.pk != actor.pk
                or feed.pk != expected['feed_id'] or str(feed.public_key) != expected['public_key']
                or feed.revision != expected['draft_revision']
                or feed.published_revision != expected['published_revision']
                or feed.published_photo_limit != expected['photo_limit']
                or _json_hash(feed.draft) != expected['draft_sha256']
                or _json_hash(feed.published) != expected['published_sha256']):
            raise SourcePinError('source_pin_binding_changed')
        content, pairs = _snapshot(feed)
        if _hash(content) != expected['file_sha256'] or _pairs_hash(pairs) != expected['pairs_sha256']:
            raise SourcePinError('source_pin_source_changed')
        marker = connection.settings.get(KEY) if isinstance(connection.settings, dict) else None
        if (not isinstance(marker, dict) or marker.get('generation') != GENERATION
                or marker.get('lease') != self.lease or marker.get('binding') != current_binding
                or marker.get('version_id') is not None):
            raise SourcePinError('source_pin_binding_changed')
        if AvitoServicesPublication.objects.filter(version__feed=feed,
                version__revision=SOURCE_REVISION).exists():
            raise SourcePinError('source_pin_binding_changed')

    def assert_inventory(self, verified, content):
        from .avito_services_feed import entries
        rows = entries(verified['sheets'])
        expected_count = self.expected['row_count']
        pairs = {(row['Id'], row['AvitoId']) for row in rows}
        observations = verified.get('observations')
        if not isinstance(observations, list):
            raise SourcePinError('source_pin_inventory_changed')
        self.excluded_count = sum(row.get('included') is not True or row.get('status') != 'active'
                                  for row in observations if isinstance(row, dict))
        observed = {(row.get('id'), row.get('avito_id')) for row in observations if isinstance(row, dict)}
        complete = (len(rows) == len(pairs) == len(observed) == expected_count == len(observations)
                    and pairs == observed and _pairs_hash(pairs) == self.expected['pairs_sha256']
                    and self.excluded_count == 0
                    and all(isinstance(row, dict) for row in observations)
                    and _hash(content) == self.expected['file_sha256'])
        self.all_ids_active = complete
        if not complete:
            raise SourcePinError('source_pin_inventory_changed')

    def before_publish_locked(self, connection, feed):
        # Snapshot capture is NOW; no claim about the historical publication
        # timestamp and no immutable legacy binding are created.
        if not feed.versions.filter(action__in=('publish', 'legacy_published'),
                published_revision=SOURCE_REVISION).exists():
            AvitoServicesFeedVersion.objects.create(feed=feed, revision=SOURCE_REVISION,
                published_revision=SOURCE_REVISION, action='legacy_published',
                actor_id=self.expected['actor_id'], snapshot=deepcopy(feed.published),
                file_digest=self.expected['file_sha256'],
                reason='Захват опубликованного снимка 15 перед обычной публикацией; исходная дата неизвестна')

    def record_locked(self, connection, feed, version, publication):
        if (version.feed_id != self.expected['feed_id'] or version.action != 'publish'
                or version.file_digest != self.expected['file_sha256']
                or publication.file_sha256 != self.expected['file_sha256']
                or _hash(xml_bytes(version.snapshot)) != self.expected['file_sha256']
                or _json_hash(feed.published) != self.expected['published_sha256']
                or _json_hash(feed.draft) != self.expected['draft_sha256']):
            raise SourcePinError('source_pin_source_changed')
        connection.refresh_from_db(fields=['settings'])
        metadata = dict(connection.settings)
        marker = metadata.get(KEY)
        if (not isinstance(marker, dict) or marker.get('lease') != self.lease
                or marker.get('binding') != self.expected['binding']
                or marker.get('version_id') is not None):
            raise SourcePinError('source_pin_binding_changed')
        marker = dict(marker)
        marker.update(phase='published', version_id=version.pk, published_revision=version.revision,
            created_at=version.created_at.isoformat(), lease_until=None,
            photo_before_publication=deepcopy(self.photo), all_ids_active=True, excluded_count=0)
        metadata[KEY] = marker
        connection.settings = metadata
        connection.save(update_fields=['settings'])


def _historical_photo(feed, version, marker):
    """Project the already-validated r1 event, scoped to this publication."""
    from .avito_services_release_operation import _source, _date
    value = marker.get('photo_before_publication')
    if not isinstance(value, dict):
        raise SourcePinError('source_pin_ledger_invalid')
    positive = ('upload_saved', 'private_preview_verified', 'cover_order_saved',
                'original_draft_restored', 'published_unchanged')
    negative = ('image_publicly_available', 'browser_clicks_verified', 'provider_content_verified')
    checked = _date(value.get('checked_at'))
    if (value.get('connection_id') != CONNECTION_ID or value.get('account_id') != ACCOUNT_ID
            or checked is None or checked > version.created_at + timedelta(minutes=5)
            or any(value.get(key) is not True for key in positive)
            or any(value.get(key) is not False for key in negative)
            or type(value.get('revision_before')) is not int
            or type(value.get('revision_after')) is not int
            or value['revision_after'] != value['revision_before'] + 2
            or value['revision_after'] != version.revision):
        raise SourcePinError('source_pin_ledger_invalid')
    for key, revision in (('edit_version_id', value['revision_before'] + 1),
                          ('restore_version_id', value['revision_after'])):
        if (type(value.get(key)) is not int or value[key] <= 0
                or not feed.versions.filter(pk=value[key], revision=revision, action='edit').exists()):
            raise SourcePinError('source_pin_ledger_invalid')
    restored = feed.versions.get(pk=value['restore_version_id'])
    if _json_hash(restored.snapshot) != marker.get('expected', {}).get('draft_sha256'):
        raise SourcePinError('source_pin_ledger_invalid')
    try:
        source = _source(value.get('source'))
    except Exception:
        raise SourcePinError('source_pin_ledger_invalid') from None
    return {'connection_id': CONNECTION_ID, 'account_id': ACCOUNT_ID,
        'checked_at': value['checked_at'], 'source': source,
        **dict.fromkeys(positive, True), **dict.fromkeys(negative, False),
        **{key: value[key] for key in ('revision_before', 'revision_after',
                                      'edit_version_id', 'restore_version_id')}}


def _reconcile(connection, feed, marker, *, repeated):
    result = _result()
    if (not isinstance(marker, dict) or marker.get('generation') != GENERATION
            or marker.get('binding') != _binding(connection)
            or type(marker.get('version_id')) is not int):
        raise SourcePinError('source_pin_ledger_invalid')
    publication = AvitoServicesPublication.objects.select_related(
        'version__feed__connection__channel').filter(
            version_id=marker['version_id'], version__feed=feed).first()
    if publication is None or not _binding_valid(publication, connection):
        raise SourcePinError('source_pin_binding_changed')
    version = publication.version
    if (feed.published_revision != version.revision
            or marker.get('published_revision') != version.revision
            or marker.get('expected', {}).get('file_sha256') != publication.file_sha256
            or _hash(xml_bytes(version.snapshot)) != publication.file_sha256
            or xml_bytes(feed.published) != xml_bytes(version.snapshot)):
        raise SourcePinError('source_pin_binding_changed')
    # This is the historical r1 result validated before this publication; it is
    # never relabelled as a current browser/Avito card check.
    result.update(state='already_published' if repeated else 'published',
        code='source_pin_already_published' if repeated else 'source_pin_published',
        same_content=True, all_ids_active=True, excluded_count=0,
        photo_before_publication=_historical_photo(feed, version, marker))
    result['publication'] = publication_release_check(
        connection, revision=version.revision, check_http=True)
    if result['publication'].get('http_matches') is not True:
        result['code'] = 'source_pin_http_unconfirmed'
    return result


def _reserve(expected_sha, photo):
    with transaction.atomic():
        connection, actor, feed, binding = _context(locked=True)
        metadata = dict(connection.settings)
        previous = metadata.get(KEY)
        if isinstance(previous, dict) and previous.get('version_id') is not None:
            return connection, actor, feed, previous, None
        expected = _expected(connection, actor, feed, binding)
        if previous:
            if (not isinstance(previous, dict) or previous.get('generation') != GENERATION
                    or previous.get('binding') != binding or previous.get('expected') != expected):
                raise SourcePinError('source_pin_binding_changed')
            from .avito_services_release_operation import _date
            until = _date(previous.get('lease_until'))
            if until is not None and until > timezone.now():
                raise SourcePinError('source_pin_busy')
        lease = secrets.token_hex(16)
        marker = {'generation': GENERATION, 'phase': 'preflight', 'binding': binding,
            'actor_id': actor.pk, 'expected_sha': expected_sha, 'expected': expected,
            'version_id': None, 'lease': lease,
            'lease_until': (timezone.now() + timedelta(seconds=120)).isoformat(),
            'photo_before_publication': deepcopy(photo)}
        metadata[KEY] = marker
        connection.settings = metadata
        connection.save(update_fields=['settings'])
        return connection, actor, feed, marker, lease


def run_source_pin(expected_sha, generation):
    result = _result()
    if (generation != GENERATION or not isinstance(expected_sha, str)
            or not re.fullmatch(r'[0-9a-f]{40}', expected_sha)):
        result['code'] = 'source_pin_generation_invalid'
        return result
    from .avito_services_feed import publish_services_snapshot, PublicationConflict
    approval = None
    lease = None
    try:
        connection, actor, feed, binding = _context()
        marker = connection.settings.get(KEY)
        if marker is not None and (not isinstance(marker, dict)
                or marker.get('generation') != GENERATION or marker.get('binding') != binding):
            raise SourcePinError('source_pin_binding_changed')
        if isinstance(marker, dict) and marker.get('version_id') is None:
            from .avito_services_release_operation import _date
            until = _date(marker.get('lease_until'))
            if until is not None and until > timezone.now():
                raise SourcePinError('source_pin_busy')
        # Own completed ledger is reconciled BEFORE r1's deliberately strict
        # published15 context. Repeated calls can never republish identical bytes.
        if isinstance(marker, dict) and marker.get('version_id') is not None:
            return _reconcile(connection, feed, marker, repeated=True)
        if feed.published_revision != SOURCE_REVISION:
            raise SourcePinError('source_pin_source_changed')
        existing = AvitoServicesPublication.objects.select_related(
            'version__feed__connection__channel').filter(
                version__feed=feed, version__revision=SOURCE_REVISION).order_by('-pk').first()
        if existing is not None:
            if not _binding_valid(existing, connection):
                raise SourcePinError('source_pin_binding_changed')
            result.update(state='already_bound', code='source_pin_already_bound',
                publication=publication_release_check(connection, revision=SOURCE_REVISION, check_http=True))
            return result
        _snapshot(feed)
        result['same_content'] = True
        photo = _photo_before_publication(connection, expected_sha)
        result['photo_before_publication'] = photo
        connection, actor, feed, marker, lease = _reserve(expected_sha, photo)
        if lease is None:
            return _reconcile(connection, feed, marker, repeated=True)
        approval = _Approval(marker['expected'], lease, photo)
        publish_services_snapshot(_request(actor), CONNECTION_ID,
            revision=marker['expected']['draft_revision'], approval=approval)
        connection, actor, feed, binding = _context()
        return _reconcile(connection, feed, connection.settings.get(KEY), repeated=False)
    except SourcePinError as exc:
        code = str(exc) if str(exc) in CODES else 'source_pin_internal_error'
        result.update(state='busy' if code == 'source_pin_busy' else 'blocked', code=code)
        if code == 'source_pin_pending_draft':
            result['same_content'] = False
    except PublicationConflict:
        result.update(state='blocked', code='source_pin_binding_changed')
    except PermissionDenied:
        result.update(state='blocked', code='source_pin_actor_invalid')
    except Http404:
        result.update(state='blocked', code='source_pin_scope_invalid')
    except AvitoError:
        result.update(state='failed', code='source_pin_provider_unavailable')
    except Exception:
        result.update(state='unknown', code='source_pin_internal_error')
    if approval is not None:
        result.update(all_ids_active=approval.all_ids_active, excluded_count=approval.excluded_count)
    if lease is not None:
        # A commit-side exception is resolved from the durable result first.
        # No replacement publication is attempted. Otherwise release only our
        # uncommitted lease, preserving every unrelated settings field.
        try:
            connection, actor, feed, binding = _context()
            marker = connection.settings.get(KEY)
            if isinstance(marker, dict) and marker.get('version_id') is not None:
                return _reconcile(connection, feed, marker, repeated=True)
            with transaction.atomic():
                connection, actor, feed, binding = _context(locked=True)
                metadata = dict(connection.settings)
                marker = metadata.get(KEY)
                if (isinstance(marker, dict) and marker.get('lease') == lease
                        and marker.get('version_id') is None and marker.get('binding') == binding):
                    marker = dict(marker, phase=result['state'], lease_until=None)
                    metadata[KEY] = marker
                    connection.settings = metadata
                    connection.save(update_fields=['settings'])
        except Exception:
            pass
    return result
