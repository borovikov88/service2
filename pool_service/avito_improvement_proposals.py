"""Private, unapplied improvement projects over the existing Services feed history."""
from copy import deepcopy
from datetime import date, datetime, timedelta
import hashlib
import json
import os
import re

from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from django.utils.html import strip_tags

from .avito_feed_models import AvitoServicesFeed, AvitoServicesFeedVersion
from .avito_services_feed import ContentForm, _publication_connection, bound, entries
from .avito_services_history import record_version
from .avito_services_publication import publication_context, publication_facts
from .avito_services_workplace_setup import _binding

KIND = 'avito_improvement_proposal_v1'
CAMPAIGN = 'services_owner_cycle_20261025'
SETUP_KEY = 'avito_improvement_cycle_20261025'
ACCOUNT = '446312402'
TARGETS = {
    '8346503274': ('Барнаул', 'water'),
    '8343962135': ('Барнаул', 'pools_hammams'),
    '8457366990': ('Бийск', 'pools'),
}
MAX_STATUS_AGE = timedelta(hours=4)
MAX_ANALYTICS_AGE = timedelta(hours=24)
METRICS = ('impressions', 'views', 'contacts', 'favorites', 'contactsShowPhone',
           'contactsMessenger', 'contactsShowPhoneAndMessenger', 'allSpending',
           'spending', 'presenceSpending', 'promoSpending', 'restSpending',
           'commission', 'ctr', 'conversion', 'cpl_cash')
ERRORS = {
    'scope_changed': 'Привязка подключения или права изменились. Проекты не сохранены.',
    'source_unavailable': 'Нет проверенного текущего опубликованного снимка Service2.',
    'revision_changed': 'Версия изменилась. Обновите страницу перед подготовкой проектов.',
    'analysis_unavailable': 'Нет доступного подтверждения получения аналитики для текущего подключения.',
    'unavailable': 'Не удалось подготовить проекты. Опубликованное содержимое не изменено.',
}
SKIPS = {
    'not_in_source': 'Объявление отсутствует в текущем согласованном снимке.',
    'inactive_or_unknown': 'Нет свежего подтверждения активного статуса и принадлежности.',
    'owner_withdrawn': 'Владелец запретил возврат объявления.',
    'draft_changed': 'Заголовок черновика уже отличается от опубликованного снимка.',
    'service_or_city_unknown': 'Услуга или город не подтверждены исходным текстом и адресом.',
    'no_structural_issue': 'Подходящее изменение заголовка не обосновано.',
    'analytics_unknown': 'Нет свежего подтверждения получения данных по объявлению.',
}
HYPOTHESIS = 'Гипотеза ясности услуги и географии; поисковые частотности не использованы.'
INSUFFICIENT = [
    'Не завершены 14 дат источника после отдельного подтверждения публикации владельцем.',
    'Менее 10 обращений с подтверждённой оценкой качества в существующей CRM.',
    'Неизвестны источники или оценки части обращений, либо неполно покрытие аналитики.',
    'Одновременно изменены другие поля, цена или продвижение.',
    'Не подтверждены календарные границы или сопоставимость состава объявлений.',
]


class ProposalError(ValueError):
    pass


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
        separators=(',', ':')).encode()).hexdigest()


def _time(value):
    try:
        stamp = value if isinstance(value, datetime) else parse_datetime(value) if isinstance(value, str) else None
        return stamp if stamp is not None and timezone.is_aware(stamp) else None
    except (TypeError, ValueError, OverflowError):
        return None


def _fresh(value, now, maximum):
    stamp = _time(value)
    return stamp if stamp is not None and timedelta(0) <= now - stamp <= maximum else None


def _pilot(connection):
    return connection.pk == 4 and str(connection.external_id) == ACCOUNT


def _scope(connection):
    return {'connection_id': connection.pk, 'account_id': ACCOUNT,
            'organization_id': connection.channel.organization_id}


def _source(connection, feed):
    context = publication_context(feed)
    if (not bound(feed, connection) or not feed.enabled or context is None
            or context['facts'].get('file_published') is not True
            or context['version'].revision != feed.published_revision
            or context['version'].snapshot != feed.published):
        raise ProposalError('source_unavailable')
    return context


def decision_evidence(connection, actor, identifiers, now=None):
    # The existing report is installed by the earlier analytics release.
    from .avito_decision_report import decision_evidence as report_evidence
    return report_evidence(connection, actor, identifiers, now=now)


def _safe_analysis(raw, identifier, publication_revision, now):
    if (not isinstance(raw, dict) or raw.get('schema_version') != 1
            or raw.get('connection_id') != 4 or raw.get('account_id') != ACCOUNT
            or raw.get('source') != 'saved_avito_stats_v2_items'):
        return None
    cutoff = raw.get('cutoff')
    if not isinstance(cutoff, str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}', cutoff):
        return None
    ads = raw.get('ads')
    row = ads.get(identifier) if isinstance(ads, dict) else None
    if (not isinstance(row, dict) or row.get('current_published') is not True
            or row.get('publication_revision') != publication_revision):
        return None
    received = _fresh(row.get('received_at'), now, MAX_ANALYTICS_AGE)
    digest = row.get('basis_digest')
    start, end = row.get('from'), row.get('to')
    if (received is None or not isinstance(digest, str) or not re.fullmatch(r'[0-9a-f]{64}', digest)
            or not all(isinstance(day, str) and re.fullmatch(r'\d{4}-\d{2}-\d{2}', day) for day in (start, end))):
        return None
    try:
        first, last, latest = (date.fromisoformat(value) for value in (start, end, cutoff))
    except ValueError:
        return None
    expected, reported = row.get('expected_days'), row.get('reported_days')
    if (latest > now.date() or type(expected) is not int or type(reported) is not int
            or not 0 <= reported <= expected <= 30 or not first <= last <= latest
            or (last - first).days + 1 != expected):
        return None
    known = row.get('metrics_known')
    return {'source': 'service2_saved_avito_statistics', 'cutoff': cutoff,
        'from': start, 'to': end, 'received_at': received.isoformat(),
        'expected_days': expected, 'reported_days': reported,
        'pages_complete': row.get('pages_complete') is True,
        'ad_rows_complete': row.get('ad_rows_complete') is True,
        'metrics_known': {key: isinstance(known, dict) and known.get(key) is True for key in METRICS},
        'low_or_unknown_volume': row.get('low_or_unknown_volume') is not False,
        'basis_digest': digest, 'provider_timezone': 'unknown',
        'barnaul_calendar_confirmed': False, 'performance_ranked': False}


def _status(connection, feed, context, row, now):
    candidates = []
    for observation in feed.latest_observations if isinstance(feed.latest_observations, list) else []:
        if (not isinstance(observation, dict) or observation.get('avito_id') != row['AvitoId']
                or observation.get('id') != row['Id'] or observation.get('source') != 'avito_item_detail'):
            continue
        checked = _fresh(observation.get('checked_at'), now, MAX_STATUS_AGE)
        end = _time(observation.get('finish_time'))
        if checked:
            candidates.append((checked, observation.get('status') == 'active' and not (end and end <= now),
                {'source': 'avito_item_detail', 'observed_at': checked.isoformat(), 'checked_at': checked.isoformat()}))
    publication = context['version'].publication
    for attempt in publication.attempts.filter(upload_status__in=('success', 'success_warning')
            ).order_by('-checked_at', '-pk')[:10]:
        checked = _fresh(attempt.checked_at, now, MAX_STATUS_AGE)
        observed = _fresh(attempt.report_started_at, now, MAX_STATUS_AGE)
        if not checked or not observed or publication_facts(publication, attempt).get('rows_processed') is not True:
            continue
        matches = [item for item in attempt.rows if isinstance(item, dict)
            and item.get('avito_id') == row['AvitoId'] and item.get('xml_id') == row['Id']]
        if len(matches) == 1:
            item = matches[0]
            end = _time(item.get('finish_time'))
            candidates.append((observed, item.get('status') == 'active' and item.get('has_error') is False and not (end and end <= now),
                {'source': 'completed_autoload_report_status', 'observed_at': observed.isoformat(), 'checked_at': checked.isoformat(),
                 'upload_id': attempt.upload_id,
                 'exact_input_version_confirmed': attempt.source_confirmed is True}))
    if not candidates:
        return None
    latest = max(item[0] for item in candidates)
    matching = [item for item in candidates if item[0] == latest]
    return matching[0][2] if all(item[1] for item in matching) else None


def _text(value):
    if not isinstance(value, str):
        return ''
    value = re.sub(r'<(style|script)\b[^>]*>.*?</\1\s*>', '', value[:32767], flags=re.I | re.S)
    return re.sub(r'\s+', ' ', strip_tags(value)).strip()


def _candidate(row, city, service):
    from .avito_daily_statistics import _city
    address_row = {'Address': row.get('Address')}
    found, _ = _city(address_row)
    explicit = row.get('City')
    if found != city or isinstance(explicit, str) and explicit.strip() and explicit.strip().casefold() != city.casefold():
        return None
    original = row.get('Title')
    if not isinstance(original, str) or '<' in original or '>' in original:
        return None
    current, description = _text(original), _text(row.get('Description'))
    material = (current + ' ' + description).casefold()
    water = bool(re.search(r'водоочист|водоподгот|очистк[а-яё]*\s+вод', material))
    pools = bool(re.search(r'бассейн[а-яё]*', material) and re.search(r'строитель[а-яё]*', material))
    hammams = bool(re.search(r'хамм?ам[а-яё]*', material))
    if not {'water': water, 'pools': pools, 'pools_hammams': pools and hammams}[service]:
        return None
    mentioned = {known for known in ('Барнаул', 'Бийск', 'Горно-Алтайск') if known.casefold() in current.casefold()}
    if mentioned - {city}:
        return None
    city_present = city in mentioned
    punctuation = bool(re.search(r',\S|\s{2,}', original))
    if city_present and not punctuation:
        return False
    proposed = re.sub(r',(?=\S)', ', ', re.sub(r'\s+', ' ', original).strip())
    if not city_present:
        proposed += ' · ' + city
    from django.core.exceptions import ValidationError
    try:
        proposed = ContentForm.base_fields['title'].clean(proposed)
    except ValidationError:
        return False
    if original == proposed:
        return False
    issue = 'В заголовке не названа география, подтверждённая адресом.' if not city_present else 'В заголовке слитая пунктуация.'
    return proposed, issue


def _key(scope, version, identifier, before, after, binding):
    return _digest({'scope': scope, 'source_version': version.pk, 'avito_id': identifier,
        'field': 'Title', 'before': before, 'after': after, 'binding': binding})


def prepare(connection, actor, *, expected_revision=None, expected_source=None):
    """Save only local projects. All provider reads, publication and draft writes are absent."""
    now = timezone.now()
    with transaction.atomic():
        current, actor = _publication_connection(connection.pk, actor, locked=True)
        if not _pilot(current):
            raise PermissionDenied
        feed = AvitoServicesFeed.objects.select_for_update().select_related('connection__channel').get(connection=current)
        context = _source(current, feed)
        version = context['version']
        if (expected_revision is not None and str(expected_revision) != str(feed.revision)
                or expected_source is not None and str(expected_source) != str(version.pk)):
            raise ProposalError('revision_changed')
        binding = _digest(_binding(current))
        if not _binding(current).get('credential_stamp'):
            raise ProposalError('scope_changed')
        before_source = (deepcopy(feed.published), feed.published_revision, deepcopy(feed.draft), feed.revision, version.pk)
        raw = decision_evidence(current, actor, list(TARGETS), now=now)
        # Recheck authority after the shared report helper and before appending events.
        current, actor = _publication_connection(current.pk, actor, locked=True)
        feed.refresh_from_db()
        if (not _pilot(current) or binding != _digest(_binding(current)) or not bound(feed, current)
                or before_source != (feed.published, feed.published_revision, feed.draft, feed.revision, _source(current, feed)['version'].pk)):
            raise ProposalError('scope_changed')
        source_rows = {row['AvitoId']: row for row in entries(feed.published)}
        draft_rows = {row['AvitoId']: row for row in entries(feed.draft)}
        withdrawn = set(feed.listing_intents.filter(account_id=ACCOUNT,
            organization_id=current.channel.organization_id, owner_withdrawn=True).values_list('avito_id', flat=True))
        output = []
        for identifier, (city, service) in TARGETS.items():
            result = {'avito_id': identifier, 'state': 'skipped', 'code': '', 'event_id': None}
            row = source_rows.get(identifier)
            draft = draft_rows.get(identifier)
            if not row or not draft or row.get('Id') != draft.get('Id'):
                result['code'] = 'not_in_source'
            elif identifier in withdrawn:
                result['code'] = 'owner_withdrawn'
            elif row.get('Title') != draft.get('Title'):
                result['code'] = 'draft_changed'
            else:
                status = _status(current, feed, context, row, now)
                analysis = _safe_analysis(raw, identifier, feed.published_revision, now)
                candidate = _candidate(row, city, service)
                if status is None:
                    result['code'] = 'inactive_or_unknown'
                elif analysis is None:
                    result['code'] = 'analytics_unknown'
                elif candidate is None:
                    result['code'] = 'service_or_city_unknown'
                elif candidate is False:
                    result['code'] = 'no_structural_issue'
                else:
                    after, issue = candidate
                    key = _key(_scope(current), version, identifier, row['Title'], after, binding)
                    existing = feed.versions.filter(action='verify', changes__0__kind=KIND,
                        changes__0__key=key).order_by('-pk').first()
                    if existing:
                        result.update(state='existing', event_id=existing.pk)
                    else:
                        change = {'kind': KIND, 'schema_version': 1, 'field': 'improvement_project',
                            'campaign': CAMPAIGN, 'key': key, 'scope': _scope(current),
                            'avito_id': identifier, 'id': row['Id'],
                            'before': {'Title': row['Title']}, 'after': {'Title': after},
                            'source': {'publication_version_id': version.pk, 'publication_revision': version.revision,
                                'file_sha256': version.file_digest, 'field_sha256': _digest(row['Title']),
                                'binding_sha256': binding, 'source': 'published_service2_snapshot',
                                'actual_avito_fields_verified': False},
                            'proposal': {'state': 'prepared_not_applied', 'variable': 'Title',
                                'issue': issue, 'hypothesis': HYPOTHESIS, 'analysis': analysis, 'status_evidence': status,
                                'wordstat': {'state': 'unavailable', 'frequency': None, 'region_frequency': None,
                                    'source': 'yandex_wordstat', 'queried_at': None},
                                'evaluation': {'state': 'not_started', 'source_dates': 14,
                                    'start_trigger': 'separate_owner_publication_and_calendar_confirmation',
                                    'primary_metric': 'qualified_inquiry_share',
                                    'metric_label': 'Доля целевых обращений по подтверждённым оценкам существующей CRM',
                                    'minimum_classified_inquiries': 10, 'guard_is_statistical_significance': False,
                                    'conditions_insufficient': INSUFFICIENT}}}
                        event = record_version(feed, 'verify', feed.draft, feed.draft, actor=actor,
                            reason='Проект улучшения, не применён: гипотеза ясности заголовка; частотности не получены.',
                            source_version=version, extra_changes=[change])
                        result.update(state='created', event_id=event.pk)
            output.append(result)
        return {'schema_version': 1, 'connection_id': 4, 'account_id': ACCOUNT,
            'state': 'prepared', 'source_version_id': version.pk, 'publication_revision': version.revision,
            'results': output, 'draft_changed': False, 'published_changed': False,
            'provider_action_performed': False}


def is_proposal(event):
    values = getattr(event, 'changes', None)
    return bool(getattr(event, 'action', None) == 'verify' and isinstance(values, list) and len(values) == 1
        and isinstance(values[0], dict) and values[0].get('kind') == KIND and values[0].get('schema_version') == 1)


def page_context(connection, actor):
    current, actor = _publication_connection(connection.pk, actor)
    if not _pilot(current):
        raise PermissionDenied
    feed = AvitoServicesFeed.objects.select_related('connection__channel').get(connection=current)
    source = _source(current, feed)
    output = []
    for event in feed.versions.filter(action='verify', changes__0__kind=KIND).select_related(
            'actor', 'source_version').order_by('-pk')[:50]:
        if not is_proposal(event):
            continue
        change = event.changes[0]
        if (change.get('scope') != _scope(current) or change.get('avito_id') not in TARGETS
                or not isinstance(change.get('source'), dict) or not isinstance(change.get('proposal'), dict)
                or not all(isinstance(change['proposal'].get(key), dict) for key in ('analysis', 'status_evidence', 'evaluation', 'wordstat'))
                or event.source_version_id is None or event.source_version.feed_id != feed.pk
                or event.source_version.action != 'publish'
                or type(change['source'].get('publication_version_id')) is not int
                or event.source_version_id != change['source'].get('publication_version_id')
                or not isinstance(change.get('before'), dict) or not isinstance(change.get('after'), dict)
                or set(change['before']) != {'Title'} or set(change['after']) != {'Title'}
                or not all(isinstance(value.get('Title'), str) and len(value['Title']) <= 100
                           for value in (change['before'], change['after']))):
            continue
        if (change['source'].get('file_sha256') != event.source_version.file_digest
                or change['source'].get('publication_revision') != event.source_version.revision
                or change['source'].get('field_sha256') != _digest(change['before']['Title'])
                or not isinstance(change['source'].get('binding_sha256'), str)
                or change.get('key') != _key(_scope(current), event.source_version, change['avito_id'],
                    change['before']['Title'], change['after']['Title'], change['source']['binding_sha256'])):
            continue
        valid = (event.source_version_id == source['version'].pk
            and change['source'].get('binding_sha256') == _digest(_binding(current))
            and change['source'].get('file_sha256') == source['version'].file_digest)
        output.append({'event': event, 'avito_id': change['avito_id'], 'before': change['before']['Title'],
            'proposed': change['after']['Title'], 'project': change.get('proposal', {}),
            'source_revision': change['source'].get('publication_revision'),
            'current_source': valid, 'draft_unchanged_since_project': event.snapshot == feed.draft,
            'analysis_received_at': _time(change['proposal'].get('analysis', {}).get('received_at')),
            'status_observed_at': _time(change['proposal'].get('status_evidence', {}).get('observed_at')),
            'status_checked_at': _time(change['proposal'].get('status_evidence', {}).get('checked_at'))})
    return {'connection': current, 'feed': feed, 'source_version': source['version'],
        'projects': output, 'target_ids': list(TARGETS), 'error': ''}


def setup_owner_cycle_once():
    """Only the existing protected exact-main setup attempts this authorised cycle."""
    if os.environ.get('SERVICE2_AVITO_DEPLOY_SETUP') != '1':
        return {'state': 'blocked', 'code': 'deployment_origin_unverified', 'connection_id': 4}
    try:
        from .avito_services_workplace_setup import _connection, _actor
        connection = _connection(4)
        actor = _actor(connection)
        with transaction.atomic():
            current, actor = _publication_connection(4, actor, locked=True)
            if not _pilot(current) or not _binding(current).get('credential_stamp'):
                raise ProposalError('scope_changed')
            binding = _digest(_binding(current))
            metadata = dict(current.settings) if isinstance(current.settings, dict) else {}
            prior = metadata.get(SETUP_KEY)
            if isinstance(prior, dict):
                if prior.get('campaign') != CAMPAIGN or prior.get('binding_sha256') != binding:
                    raise ProposalError('scope_changed')
                rows = prior.get('results')
                rows = rows if isinstance(rows, list) else []
                return {'state': 'already_attempted', 'attempt_state': prior.get('state') if prior.get('state') in {'prepared', 'blocked'} else 'blocked',
                    'code': prior.get('code') if prior.get('code') in {'', *ERRORS} else 'unavailable',
                    'connection_id': 4, 'account_id': ACCOUNT,
                    'source_version_id': prior.get('source_version_id') if type(prior.get('source_version_id')) is int else None,
                    'publication_revision': prior.get('publication_revision') if type(prior.get('publication_revision')) is int else None,
                    'results': [{'avito_id': row['avito_id'], 'state': row['state'], 'code': row['code'],
                        'event_id': row['event_id']} for row in rows
                        if isinstance(row, dict) and row.get('avito_id') in TARGETS
                        and row.get('state') in {'created', 'existing', 'skipped'}
                        and row.get('code') in {'', *SKIPS}
                        and (row.get('event_id') is None or type(row.get('event_id')) is int)][:3],
                    'provider_action_performed': False}
            try:
                result = prepare(current, actor)
            except ProposalError as error:
                code = str(error) if str(error) in ERRORS else 'unavailable'
                result = {'state': 'blocked', 'code': code, 'results': []}
            except PermissionDenied:
                # Recheck below; a loss of authority never authorizes a marker write.
                result = {'state': 'blocked', 'code': 'scope_changed', 'results': []}
            except Exception:
                result = {'state': 'blocked', 'code': 'unavailable', 'results': []}
            current, actor = _publication_connection(4, actor, locked=True)
            if not _pilot(current) or binding != _digest(_binding(current)):
                raise ProposalError('scope_changed')
            metadata = dict(current.settings) if isinstance(current.settings, dict) else {}
            metadata[SETUP_KEY] = {'campaign': CAMPAIGN, 'attempted_at': timezone.now().isoformat(),
                'binding_sha256': binding, 'state': result['state'], 'code': result.get('code', ''),
                'source_version_id': result.get('source_version_id'),
                'publication_revision': result.get('publication_revision'), 'results': result['results']}
            current.settings = metadata
            current.save(update_fields=['settings'])
            return {**result, 'connection_id': 4, 'account_id': ACCOUNT, 'provider_action_performed': False}
    except PermissionDenied:
        code = 'scope_changed'
    except ProposalError as error:
        code = str(error) if str(error) in ERRORS else 'unavailable'
    except Exception:
        code = 'unavailable'
    return {'state': 'blocked', 'code': code, 'connection_id': 4, 'account_id': ACCOUNT,
            'provider_action_performed': False}
