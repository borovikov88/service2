"""Link immutable source URLs to later provider reports without inventing content parity."""
from copy import deepcopy
from datetime import timedelta
import hashlib
import json
import re
import time
from urllib.parse import urlsplit

from django.conf import settings
from django.db import transaction
from django.db.models import Q, TextField
from django.db.models.functions import Cast, Length
from django.urls import reverse
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from .avito_feed_models import AvitoServicesFeed, AvitoServicesFeedVersion
from .avito_publication_models import AvitoServicesPublication, AvitoServicesPublicationAttempt

STATE_LABELS = {
    'waiting': 'Ожидается загрузка после публикации',
    'report_predates_publication': 'Отчёт старше публикации; новая версия не подтверждена',
    'report_unfinished': 'Загрузка ещё обрабатывается',
    'report_source_unknown': 'Источник отчёта не передан; версия файла не подтверждена',
    'report_source_conflict': 'Адрес источника и проверенная XML-копия противоречат друг другу',
    'report_source_mismatch': 'API отчёта вернул другой адрес источника; версия не подтверждена',
    'report_identity_mismatch': 'Состав или привязки объявлений в отчёте отличаются',
    'candidate_finished': 'Последующая загрузка сопоставлена; точная версия файла не подтверждена',
    'source_version_confirmed': 'Авито обработал источник этой неизменяемой версии',
    'source_copy_confirmed': 'Входная XML-копия Авито совпадает с этой версией файла',
    'source_copy_item_errors': 'XML-копия версии подтверждена; есть ошибки отдельных объявлений',
    'source_copy_ambiguous': 'Входная копия совпадает с несколькими версиями; точная публикация не определена',
    'source_copy_history_unknown': 'История опубликованных файлов неполна; точная версия копии не подтверждена',
    'source_copy_mismatch': 'Входная копия не совпадает с подходящей опубликованной версией',
    'upload_failed': 'Загрузка завершилась ошибкой; применение не подтверждено',
    'item_errors': 'Источник версии подтверждён; есть ошибки отдельных объявлений',
}

def public_origin():
    value = getattr(settings, 'AVITO_SERVICES_PUBLIC_ORIGIN', 'https://service2.aqualine22.ru')
    if not isinstance(value, str) or len(value) > 250:
        raise ValueError('Не настроен допустимый публичный HTTPS-адрес Service2.')
    try:
        part = urlsplit(value)
        valid = (part.scheme == 'https' and part.hostname and part.username is None
                 and part.password is None and part.port in (None, 443)
                 and part.path in ('', '/') and not part.query and not part.fragment
                 and not any(c.isspace() for c in value))
    except ValueError:
        valid = False
    if not valid:
        raise ValueError('Не настроен допустимый публичный HTTPS-адрес Service2.')
    return value.rstrip('/')

def _sha(value):
    return hashlib.sha256(value.encode('utf-8')).hexdigest()

def version_url(version):
    return public_origin() + reverse('avito_services_feed_version_public',
        args=[version.feed.public_key, version.pk])

def bind_publication(feed, version):
    if (version.feed_id != feed.pk or version.action != 'publish'
            or not re.fullmatch(r'[0-9a-f]{64}', version.file_digest)):
        raise ValueError('Нельзя связать источник с этой версией.')
    return AvitoServicesPublication.objects.create(version=version, account_id=feed.account_id,
        organization_id=feed.organization_id_at_import, file_sha256=version.file_digest,
        immutable_source_sha256=_sha(version_url(version)),
        stable_source_sha256=_sha(public_origin() + reverse('avito_services_feed_public', args=[feed.public_key])))

def _date(value):
    if not isinstance(value, str):
        return None
    try:
        result = parse_datetime(value)
    except (ValueError, TypeError):
        return None
    return result if result is not None and timezone.is_aware(result) else None

def _rows(sheets):
    return [row for sheet in sheets for row in sheet['rows']]

def _binding_valid(publication, connection):
    feed = publication.version.feed
    return (connection.is_active and connection.channel.is_active and connection.channel.kind == 'avito'
            and feed.connection_id == connection.pk and feed.enabled
            and feed.account_id == connection.external_id == publication.account_id
            and feed.organization_id_at_import == connection.channel.organization_id == publication.organization_id
            and publication.version.action == 'publish'
            and publication.file_sha256 == publication.version.file_digest)

def _report_rows(report):
    rows = report.get('rows')
    if not isinstance(rows, list) or len(rows) > 200:
        raise ValueError('publication_report_invalid')
    result, ids, avito_ids = [], set(), set()
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError('publication_report_invalid')
        xml_id, avito_id = row.get('ad_id'), row.get('avito_id')
        if (not isinstance(xml_id, str) or not 1 <= len(xml_id) <= 100 or xml_id in ids
                or (avito_id is not None and (type(avito_id) is not int or not 0 < avito_id < 10**32))
                or (avito_id is not None and avito_id in avito_ids)):
            raise ValueError('publication_report_invalid')
        ids.add(xml_id)
        if avito_id is not None:
            avito_ids.add(avito_id)
        status = row.get('avito_status')
        if status is not None and status not in {'active', 'old', 'archived', 'removed', 'blocked', 'rejected'}:
            raise ValueError('publication_report_invalid')
        messages = row.get('messages', [])
        if not isinstance(messages, list) or len(messages) > 50:
            raise ValueError('publication_report_invalid')
        projected_messages = []
        for message in messages:
            if (not isinstance(message, dict) or message.get('type') not in {'error', 'alarm', 'warning', 'info'}
                    or type(message.get('code')) is not int):
                raise ValueError('publication_report_invalid')
            projected_messages.append({'type': message['type'], 'code': message['code']})
        projected_messages.sort(key=lambda message: (message['type'], message['code']))
        result.append({'xml_id': xml_id, 'avito_id': str(avito_id) if avito_id is not None else None,
            'status': status, 'finish_time': row.get('avito_date_end') if _date(row.get('avito_date_end')) else None,
            'has_error': status in {'rejected', 'blocked'} or any(m['type'] in {'error', 'alarm'} for m in projected_messages),
            'messages': projected_messages})
    return sorted(result, key=lambda row: (row['xml_id'], row['avito_id'] or ''))


def _verified_copy_digest(connection, report, rows, started):
    from .avito_input_copy import identity_sha256, MAX_BYTES
    proof = report.get('input_copy_proof')
    if not isinstance(proof, dict):
        return None
    if (proof.get('source') != 'provider_report_input_copy' or proof.get('state') != 'verified_xml'
            or proof.get('code') != 'input_copy_xml_verified' or proof.get('content_kind') != 'xml'
            or type(proof.get('connection_id')) is not int or proof['connection_id'] != connection.pk
            or proof.get('account_id') != connection.external_id
            or type(proof.get('organization_id')) is not int
            or proof['organization_id'] != connection.channel.organization_id
            or proof.get('upload_id') != report.get('upload_id')
            or _date(proof.get('report_started_at')) != started
            or type(proof.get('identity_count')) is not int or proof['identity_count'] != len(rows)
            or type(proof.get('byte_count')) is not int or not 0 < proof['byte_count'] <= MAX_BYTES
            or not isinstance(proof.get('file_sha256'), str)
            or not re.fullmatch(r'[0-9a-f]{64}', proof['file_sha256'])
            or any(row['avito_id'] is None for row in rows)):
        return None
    pairs = {(row['xml_id'], row['avito_id']) for row in rows}
    if proof.get('identity_sha256') != identity_sha256(pairs):
        return None
    return proof['file_sha256']

# Metadata reads are capped before any JSON snapshot is transferred/serialized.
# Character count also bounds UTF-8 bytes (at most four bytes per character).
PROVENANCE_HISTORY_LIMIT = 32
PROVENANCE_CHARACTER_BUDGET = 1024 * 1024


def _copy_history_provenance(feed, started, copy_digest):
    """Unbound and incompletely reproducible file history cannot disappear.

    legacy_published.created_at is capture time, not the original publication
    time; all such same-feed records remain possible predecessors. Normal
    publish events have an actual event timestamp and must precede this upload.
    """
    eligible = AvitoServicesFeedVersion.objects.filter(feed=feed).filter(
        Q(action='publish', created_at__lt=started) | Q(action='legacy_published'))
    metadata = list(eligible.annotate(
        snapshot_characters=Length(Cast('snapshot', output_field=TextField()))).order_by('pk').values(
            'pk', 'file_digest', 'snapshot_characters', 'publication__pk',
            'publication__file_sha256', 'publication__account_id',
            'publication__organization_id')[:PROVENANCE_HISTORY_LIMIT + 1])
    unknown = len(metadata) > PROVENANCE_HISTORY_LIMIT
    unbound_same, spent = 0, 0
    from .avito_services_xml import xml_bytes
    for event in metadata[:PROVENANCE_HISTORY_LIMIT]:
        digest, size = event['file_digest'], event['snapshot_characters']
        if (not isinstance(digest, str) or not re.fullmatch(r'[0-9a-f]{64}', digest)
                or type(size) is not int or size <= 0
                or size > PROVENANCE_CHARACTER_BUDGET - spent):
            unknown = True
            continue
        spent += size
        # Only this bounded JSON value is loaded. Historical legacy snapshots
        # retain the former 30-photo contract; they are never truncated/re-saved.
        snapshot = eligible.filter(pk=event['pk']).values_list('snapshot', flat=True).first()
        try:
            reproduced = hashlib.sha256(xml_bytes(snapshot, max_images=30)).hexdigest()
        except (ValueError, KeyError, TypeError):
            unknown = True
            continue
        if reproduced != digest:
            unknown = True
            continue
        if event['publication__pk'] is not None:
            if (event['publication__file_sha256'] != digest
                    or event['publication__account_id'] != feed.account_id
                    or event['publication__organization_id'] != feed.organization_id_at_import):
                unknown = True
        elif digest == copy_digest:
            unbound_same = min(2, unbound_same + 1)
    return unbound_same, unknown


def correlate_report(connection, report):
    """DB-only hook after authenticated provider/account/lease checks; never provider writes."""
    if (not isinstance(report, dict) or report.get('complete') is not True
            or report.get('kind') not in {'current', 'last_successful'}):
        return None
    upload_id = report.get('upload_id')
    if (not isinstance(upload_id, str) or not re.fullmatch(r'[0-9]{1,32}', upload_id)
            or int(upload_id) <= 0):
        return None
    started = _date(report.get('started_at'))
    if started is None or started > timezone.now() + timedelta(minutes=5):
        return None
    try:
        rows = _report_rows(report)
    except ValueError:
        return None
    if type(report.get('total')) is not int or report['total'] != len(rows):
        return None
    with transaction.atomic():
        # Same lock order as editor/publication. A source/account move invalidates old bindings.
        from .communication_models import ChannelConnection
        current = ChannelConnection.objects.select_for_update().select_related('channel').get(pk=connection.pk)
        if (current.external_id != connection.external_id or current.channel_id != connection.channel_id
                or current.channel.organization_id != connection.channel.organization_id):
            return None
        feed = AvitoServicesFeed.objects.select_for_update().filter(connection=current).first()
        if not feed:
            return None
        publications = AvitoServicesPublication.objects.select_related('version__feed__connection__channel').filter(version__feed=feed)
        latest = publications.order_by('-version__created_at', '-pk').first()
        if latest is None or not _binding_valid(latest, current):
            return None
        sources = report.get('source_url_sha256')
        if not isinstance(sources, list) or len(sources) > 50 or any(
                not isinstance(value, str) or not re.fullmatch(r'[0-9a-f]{64}', value) for value in sources):
            sources = []
        copy_digest = _verified_copy_digest(current, report, rows, started)
        copy_matches = list(publications.filter(file_sha256=copy_digest, version__created_at__lt=started)
                            .order_by('-version__created_at', '-pk')[:2]) if copy_digest else []
        unbound_same, history_unknown = (_copy_history_provenance(feed, started, copy_digest)
                                        if copy_digest else (0, False))
        copy_count = min(2, len(copy_matches) + unbound_same)
        copy_ambiguous = copy_count > 1
        copy_confirmed = len(copy_matches) == 1 and not copy_ambiguous and not history_unknown
        exact = list(publications.filter(immutable_source_sha256=sources[0])[:2]) if len(sources) == 1 else []
        if len(exact) > 1:
            return None  # Ambiguous internal source binding never becomes proof.
        publication = (exact[0] if exact else copy_matches[0] if copy_matches else
            publications.filter(version__created_at__lt=started).order_by('-version__created_at', '-pk').first() or latest)
        if not _binding_valid(publication, current):
            return None
        from .avito_services_xml import xml_bytes
        try:
            snapshot_digest = hashlib.sha256(xml_bytes(publication.version.snapshot, max_images=10)).hexdigest()
        except (ValueError, KeyError, TypeError):
            return None
        if snapshot_digest != publication.file_sha256:
            return None
        expected = {(r['Id'], r['AvitoId']) for r in _rows(publication.version.snapshot)}
        received = {(r['xml_id'], r['avito_id']) for r in rows}
        identities_match = expected == received and len(rows) == len(expected)
        immutable = sources == [publication.immutable_source_sha256]
        stable = sources == [publication.stable_source_sha256]
        copy_conflict = bool(immutable and copy_digest and copy_digest != publication.file_sha256)
        finished = report.get('upload_finished') is True
        status = report.get('upload_status')
        if status not in {'processing', 'success', 'success_warning', 'error', None}:
            return None
        source_confirmed = False
        if started <= publication.version.created_at:
            state = 'report_predates_publication'
        elif not finished:
            state = 'report_unfinished'
        elif copy_conflict:
            state = 'report_source_conflict'
        elif copy_ambiguous and not immutable:
            state = 'source_copy_ambiguous'
        elif copy_digest and history_unknown and not immutable:
            state = 'source_copy_history_unknown'
        elif copy_digest and not copy_confirmed and not immutable:
            state = 'source_copy_mismatch'
        elif not sources and not copy_confirmed:
            state = 'report_source_unknown'
        elif not (immutable or stable or copy_confirmed):
            state = 'report_source_mismatch'
        elif not identities_match:
            state = 'report_identity_mismatch'
        elif status == 'error':
            state = 'upload_failed'
        elif status not in {'success', 'success_warning'}:
            state = 'report_unfinished'
        elif copy_confirmed and not immutable:
            state, source_confirmed = ('source_copy_item_errors' if any(row['has_error'] for row in rows)
                                       else 'source_copy_confirmed'), True
        elif immutable:
            state, source_confirmed = ('item_errors' if any(row['has_error'] for row in rows)
                                       else 'source_version_confirmed'), True
        else:
            state = 'candidate_finished'
        fingerprint_data = {'upload_id': upload_id, 'started_at': started.isoformat(),
            'upload_status': status, 'finished': finished,
            # Exact immutable identity is meaningful even when copy bytes are
            # known; rotating provider-copy URLs are not proof and are ignored.
            'sources': sources if not copy_digest or immutable else [],
            'input_copy_sha256': copy_digest,
            'copy_provenance': {'same_versions': copy_count, 'history_unknown': history_unknown}
                if copy_digest else None,
            'rows': rows}
        digest = _sha(json.dumps(fingerprint_data, ensure_ascii=False, sort_keys=True, separators=(',', ':')))
        attempt, _ = AvitoServicesPublicationAttempt.objects.get_or_create(
            publication=publication, upload_id=upload_id, report_sha256=digest,
            defaults={'report_started_at': started, 'state': state, 'upload_status': status or '',
                'source_confirmed': source_confirmed, 'content_verified': False, 'rows': rows})
        return attempt

def publication_context(feed):
    publication = AvitoServicesPublication.objects.select_related('version__feed').filter(
        version__feed=feed).order_by('-version__created_at', '-pk').first()
    if publication is None or not _binding_valid(publication, feed.connection):
        return None
    attempt = publication.attempts.order_by('-report_started_at', '-pk').first()
    return {'version': publication.version, 'url': version_url(publication.version),
        'state': attempt.state if attempt else 'waiting',
        'label': STATE_LABELS.get(attempt.state if attempt else 'waiting', STATE_LABELS['waiting']),
        'attempt': attempt, 'rows': attempt.rows if attempt else [],
        'content_verified': False, 'source_confirmed': attempt.source_confirmed if attempt else False,
        'facts': publication_facts(publication, attempt)}

def _snapshot_data(sheets, *, max_images=10):
    """Validate locally; never contact an image URL or return private fields."""
    from .avito_services_feed import images
    from .avito_services_xml import xml_bytes
    content = xml_bytes(sheets, max_images=max_images)
    rows = _rows(sheets)
    pairs = {(row['Id'], row['AvitoId']) for row in rows}
    maximum = max(len(images(row['ImageUrls'], max_count=max_images).split(' | ')) for row in rows)
    return content, pairs, maximum


def publication_facts(publication, attempt=None):
    """Independent evidence, not a single 'applied' flag.

    rows_processed concerns the found provider upload. Without source_confirmed
    it must not be presented as processing this particular local version.
    """
    facts = dict.fromkeys(('file_published', 'upload_found', 'source_confirmed',
                           'rows_processed', 'live_content_verified'), False)
    try:
        content, expected, _ = _snapshot_data(publication.version.snapshot)
        facts['file_published'] = bool(
            _binding_valid(publication, publication.version.feed.connection)
            and hashlib.sha256(content).hexdigest() == publication.file_sha256)
    except (ValueError, KeyError, TypeError):
        return facts
    if not facts['file_published'] or attempt is None or attempt.publication_id != publication.pk:
        return facts
    fresh = attempt.report_started_at is not None and attempt.report_started_at > publication.version.created_at
    facts['upload_found'] = fresh
    if not fresh:
        return facts
    facts['source_confirmed'] = attempt.source_confirmed is True
    try:
        rows = attempt.rows
        received = {(row['xml_id'], row['avito_id']) for row in rows}
        matches = len(rows) == len(expected) and received == expected
    except (KeyError, TypeError):
        matches = False
    facts['rows_processed'] = bool(matches and attempt.upload_status in {'success', 'success_warning'}
        and attempt.state not in {'report_unfinished', 'report_predates_publication', 'upload_failed'})
    # The provider methods used here do not return live description/photo order.
    return facts


def publication_release_check(connection, *, revision=15, check_http=False):
    """Read-only, bounded and safe to project into a protected diagnostic receipt.

    HTTP parity proves our immutable endpoint, never Avito ingestion. This is
    not permission to publish and never substitutes for publish-time checks.
    No token, raw URL, content, identifier set, author or hash is returned.
    """
    from .communication_models import ChannelConnection
    from .avito_services_xml import xml_bytes
    result = {'configured': False, 'binding_present': False, 'snapshot_valid': None,
        'snapshot_source': 'unavailable', 'current_publication': None,
        'draft_matches_published': None, 'identity_sets_match': None,
        'photo_limit_ok': None, 'http_checked': False,
        'revision': revision if type(revision) is int and 0 < revision < 2**31 else 0,
        'draft_revision': None, 'published_revision': None,
        'history_version_id': None, 'created_at': None, 'checked_at': timezone.now().isoformat(),
        'draft_count': None, 'published_count': None, 'snapshot_count': None, 'max_photos': None,
        'http_matches': None, 'http_code': 'not_checked',
        'facts': dict.fromkeys(('file_published', 'upload_found', 'source_confirmed',
                               'rows_processed', 'live_content_verified'), False)}
    if result['revision'] == 0 or type(check_http) is not bool:
        result['http_code'] = 'scope_invalid'
        return result
    current = ChannelConnection.objects.select_related('channel').filter(pk=connection.pk).first()
    if (current is None or not current.is_active or not current.channel.is_active
            or current.channel.kind != 'avito' or current.external_id != connection.external_id
            or current.channel_id != connection.channel_id
            or current.channel.organization_id != connection.channel.organization_id):
        result['http_code'] = 'scope_invalid'
        return result
    feed = AvitoServicesFeed.objects.select_related('connection__channel').filter(
        connection=current, account_id=current.external_id,
        organization_id_at_import=current.channel.organization_id).first()
    if feed is None:
        result['http_code'] = 'publication_missing'
        return result
    result.update(configured=True, draft_revision=feed.revision,
                  published_revision=feed.published_revision)
    for name, sheets in (('draft_count', feed.draft), ('published_count', feed.published)):
        try:
            count = len(_rows(sheets))
            if count <= 200:
                result[name] = count
        except (ValueError, KeyError, TypeError):
            pass
    published_content, published_pairs, published_maximum = None, None, None
    try:
        published_content, published_pairs, published_maximum = _snapshot_data(
            feed.published, max_images=feed.published_photo_limit)
        result['facts']['file_published'] = bool(feed.enabled)
    except (ValueError, KeyError, TypeError):
        pass
    try:
        if published_content is not None:
            result['draft_matches_published'] = (
                xml_bytes(feed.draft, max_images=30) == published_content)
    except (ValueError, KeyError, TypeError):
        pass

    def inspect_snapshot(sheets, source, *, digest=None, maximum=10):
        result['snapshot_source'] = source
        try:
            content, pairs, count = _snapshot_data(sheets, max_images=maximum)
            result.update(snapshot_count=len(pairs), max_photos=count, photo_limit_ok=count <= 10)
            result['snapshot_valid'] = digest is None or hashlib.sha256(content).hexdigest() == digest
            result['identity_sets_match'] = None if published_pairs is None else pairs == published_pairs
            result['current_publication'] = (None if published_content is None else bool(
                result['snapshot_valid'] and feed.enabled and feed.published_revision == revision
                and content == published_content))
            return content
        except (ValueError, KeyError, TypeError):
            result['snapshot_valid'] = False
            return None

    publication = AvitoServicesPublication.objects.select_related('version__feed__connection__channel').filter(
        version__feed=feed, version__revision=revision).order_by('-version__created_at', '-pk').first()
    if publication is None:
        # Missing history is missing provenance, not invalidity of the actual
        # locally published file. Never invent its original publication time.
        version = feed.versions.filter(revision=revision,
            action__in=('publish', 'legacy_published')).order_by('-created_at', '-pk').first()
        if version is None:
            inspect_snapshot(feed.published, 'current_published_without_history',
                             maximum=feed.published_photo_limit)
        else:
            result.update(history_version_id=version.pk, created_at=version.created_at.isoformat())
            inspect_snapshot(version.snapshot, 'legacy_history' if version.action == 'legacy_published'
                             else 'published_history', digest=version.file_digest, maximum=30)
        result['http_code'] = 'publication_missing'
        return result
    result.update(binding_present=True, history_version_id=publication.version_id,
                  created_at=publication.version.created_at.isoformat())
    if not _binding_valid(publication, current):
        result['http_code'] = 'scope_invalid'
        return result
    content = inspect_snapshot(publication.version.snapshot, 'published_history',
                               digest=publication.file_sha256)
    if content is None or result['snapshot_valid'] is not True:
        result['http_code'] = 'snapshot_invalid'
        return result
    attempt = publication.attempts.order_by('-report_started_at', '-pk').first()
    locally_published = result['facts']['file_published']
    result['facts'] = publication_facts(publication, attempt)
    result['facts']['file_published'] = locally_published
    if not check_http:
        return result
    try:
        url = version_url(publication.version)
        part = urlsplit(url)
        host = part.hostname.encode('idna').decode('ascii').lower()
        # URL is generated solely from this scoped immutable binding and the
        # canonical server origin; request data can never select a target.
        if _sha(url) != publication.immutable_source_sha256:
            raise ValueError
    except (ValueError, UnicodeError):
        result['http_code'] = 'scope_invalid'
        return result
    result['http_checked'] = True
    try:
        from .avito_input_copy import _download_url, MAX_SECONDS
        raw = _download_url(part, host, time.monotonic() + MAX_SECONDS)
        result['http_matches'] = raw == content
        result['http_code'] = 'verified' if result['http_matches'] else 'http_mismatch'
    except Exception:
        # Provider URLs, transport exceptions and content never enter receipts.
        result['http_matches'], result['http_code'] = False, 'http_unavailable'
    # Recheck current account/org/feed + bytes after the network request. This
    # does not lock the database across HTTP or authorize a later mutation.
    refreshed = AvitoServicesPublication.objects.select_related(
        'version__feed__connection__channel').filter(pk=publication.pk).first()
    valid = refreshed is not None and _binding_valid(refreshed, refreshed.version.feed.connection)
    if valid:
        fresh_feed = refreshed.version.feed
        valid = (fresh_feed.public_key == feed.public_key
                 and refreshed.immutable_source_sha256 == publication.immutable_source_sha256
                 and fresh_feed.revision == feed.revision
                 and fresh_feed.published_revision == feed.published_revision
                 and fresh_feed.published_photo_limit == feed.published_photo_limit
                 and fresh_feed.draft == feed.draft and fresh_feed.published == feed.published)
    if valid:
        fresh_connection = refreshed.version.feed.connection
        valid = (fresh_connection.external_id == current.external_id
                 and fresh_connection.channel_id == current.channel_id
                 and fresh_connection.channel.organization_id == current.channel.organization_id)
    try:
        valid = valid and _sha(version_url(refreshed.version)) == refreshed.immutable_source_sha256
        fresh_content = _snapshot_data(refreshed.version.snapshot)[0] if valid else None
        valid = valid and fresh_content == content and refreshed.file_sha256 == publication.file_sha256
    except (ValueError, KeyError, TypeError):
        valid = False
    if not valid:
        result['http_matches'], result['http_code'] = False, 'scope_changed'
        result['facts'] = dict.fromkeys(result['facts'], False)
    return result


def archive_context(row, observation, intent, report_row=None):
    """Expose reason provenance, never infer a zero-cost purchase entitlement."""
    status = (observation or {}).get('status')
    owner_stopped = intent is not None and intent.avito_id == row['AvitoId'] and intent.owner_withdrawn
    if owner_stopped:
        code, label, source = 'owner_withdrawn', 'Владелец запретил автоматический возврат', 'owner_mark'
    elif status == 'removed':
        code, label, source = 'removed', 'Снято; инициатор не передан API', 'avito_item_detail'
    elif status in {'blocked', 'rejected'}:
        code, label, source = status, 'Заблокировано' if status == 'blocked' else 'Отклонено', 'avito_item_detail'
    elif status == 'old':
        reported_end = _date((report_row or {}).get('finish_time'))
        if ((report_row or {}).get('status') == 'old' and reported_end is not None
                and reported_end <= timezone.now()):
            code, label, source = 'provider_term_passed', 'Известный срок размещения прошёл; причина архива не подтверждена', 'autoload_report'
        else:
            code, label, source = 'archive_unknown', 'Архив; причина не подтверждена', 'avito_item_detail'
    else:
        code, label, source = ('active', 'Активно', 'avito_item_detail') if status == 'active' else (
            'unknown', 'Статус не подтверждён', '')
    steps = {
        'owner_withdrawn': 'Возврат запрещён владельцем в Service2. Снятие этой отметки не запускает публикацию и не подтверждает бесплатность.',
        'blocked': 'Откройте причину блокировки в существующем объявлении Авито и выполните требуемое исправление или обращение в поддержку.',
        'rejected': 'Откройте причину отклонения в существующем объявлении Авито, исправьте её и дождитесь повторной проверки.',
        'removed': 'Уточните в кабинете Авито причину снятия и доступность повторной публикации этого же объявления; инициатор не передан API.',
        'unknown': 'Сначала обновите статус. До подтверждения статуса и условий повторная публикация заблокирована.',
    }
    manual_step = steps.get(code, (
        f'В кабинете Авито откройте «Мои объявления» → «Архив», найдите существующее объявление №{row["AvitoId"]} '
        'и проверьте причину архива. Для повторного размещения используйте эту же карточку, не создавайте копию. '
        'Перед подтверждением проверьте показанную дополнительную стоимость именно повторного размещения: '
        'при явных 0 ₽ владелец может продолжить; при оплате или неясных условиях сначала требуется согласование. '
        'После действия в Service2 нажмите «Проверить статусы и сроки»: нужны тот же AvitoId, активный статус '
        'и новый подтверждённый срок. Расходы по тарифу, целевым действиям и продвижению проверяются отдельно.'))
    return {'code': code, 'label': label, 'source': source, 'owner_withdrawn': owner_stopped,
        'can_reactivate': False, 'cost': None,
        'limitation': 'Для этого аккаунта не подтверждены доступный официальный способ повторного размещения услуги с тем же ID и проверка нулевой дополнительной стоимости до действия.',
        'manual_step': manual_step}


def annotate_reactivation(previous, current):
    """Confirm only an observed same-identity old→active transition with a later provider term."""
    old = {row.get('avito_id'): row for row in (previous if isinstance(previous, list) else []) if isinstance(row, dict)}
    result = deepcopy(current)
    for row in result:
        before = old.get(row.get('avito_id'))
        if not before or before.get('id') != row.get('id') or row.get('status') != 'active':
            continue
        prior_end, next_end = _date(before.get('finish_time')), _date(row.get('finish_time'))
        prior_checked, checked = _date(before.get('checked_at')), _date(row.get('checked_at'))
        if (before.get('status') == 'old' and prior_end and next_end and prior_checked and checked
                and prior_end <= prior_checked < checked and next_end > max(prior_end, checked)
                and before.get('finish_time_source') and row.get('finish_time_source')):
            row['reactivation'] = {'confirmed': True, 'confirmed_at': row['checked_at'],
                'previous_finish_time': before['finish_time'], 'finish_time': row['finish_time'],
                'source': 'same_identity_provider_status_and_term', 'actor': None,
                'additional_cost': None, 'service2_executed': False}
        elif (before.get('status') == 'active' and prior_end == next_end and next_end is not None
                and isinstance(before.get('reactivation'), dict)
                and before['reactivation'].get('confirmed') is True):
            row['reactivation'] = deepcopy(before['reactivation'])
    return result
