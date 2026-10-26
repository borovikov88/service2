"""Append-only period evidence for an unapplied existing Services experiment."""
from copy import deepcopy
from decimal import Decimal, InvalidOperation
import os
import re

from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.utils import timezone

from . import avito_improvement_proposals as projects
from . import avito_daily_statistics as daily
from .avito_feed_models import AvitoServicesFeed
from .avito_services_feed import entries
from .avito_services_history import record_version

KIND = 'avito_water_experiment_baseline_v1'
WATER_ID = '8346503274'
PERIODS = (('2026-10-19', '2026-10-25'), ('2026-10-12', '2026-10-18'))
UNIT = 'workspace_stats_v2_display_rub_v2'
FIELDS = daily.COUNTERS + daily.MONEY
MAX_REPORT_AGE = projects.MAX_ANALYTICS_AGE


class BaselineUnavailable(ValueError):
    pass


def wordstat_observation():
    # Owner's browser observation, not a new tool read or a frequency measurement.
    return {'state': 'historical_partial_owner_observation', 'source': 'yandex_wordstat',
        'evidence_source': 'owner_browser_report', 'observed_on': '2026-10-26',
        'observed_at': None, 'observed_precision': 'date',
        'date_from': '2026-09-08', 'date_to': '2026-10-07',
        'regions': ['Алтайский край', 'Республика Алтай'],
        'partly_unavailable': True, 'frequency': None,
        'current_seasonality_confirmed': False, 'assistant_live_verified': False}


def period_reports(connection, actor):
    """Use the existing, bound period store. No provider requests are made."""
    from .avito_period_statistics import display
    return display(connection, actor)


def _number(value, counter=False):
    if value is None:
        return None
    if type(value) not in (str, int, float, Decimal):
        raise BaselineUnavailable('period_reports_unavailable')
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise BaselineUnavailable('period_reports_unavailable') from None
    if (not number.is_finite() or number < 0
            or counter and number != number.to_integral_value()):
        raise BaselineUnavailable('period_reports_unavailable')
    return str(number)


def _report(report, bounds, now):
    if (type(report) is not dict or report.get('date_from') != bounds[0]
            or report.get('date_to') != bounds[1] or report.get('grouping') != 'item'
            or report.get('pages_complete') is not True or report.get('unit_contract') != UNIT):
        raise BaselineUnavailable('period_reports_unavailable')
    received = projects._fresh(report.get('received_at'), now, MAX_REPORT_AGE)
    ids, rows, offsets = report.get('ids'), report.get('rows'), report.get('page_offsets')
    if (received is None or type(ids) is not list or not 1 <= len(ids) <= 200
            or any(type(value) is not str or not re.fullmatch(r'[1-9][0-9]{0,19}', value) for value in ids)
            or len(set(ids)) != len(ids) or type(rows) is not list or len(rows) != len(ids)
            or type(report.get('total')) is not int or report['total'] != len(ids)
            or type(report.get('group_count')) is not int or report['group_count'] != len(ids)
            or type(offsets) is not list or not offsets or offsets[0] != 0
            or any(type(value) is not int or value < 0 for value in offsets)
            or len(offsets) > 200 or offsets != sorted(set(offsets)) or offsets[-1] >= len(ids)):
        raise BaselineUnavailable('period_reports_unavailable')
    normalized = []
    for row in rows:
        if type(row) is not dict or type(row.get('id')) is not str or row['id'] not in ids or type(row.get('metrics')) is not dict:
            raise BaselineUnavailable('period_reports_unavailable')
        normalized.append({'id': row['id'], 'metrics': {
            key: _number(row['metrics'].get(key), key in daily.COUNTERS) for key in FIELDS}})
    if {row['id'] for row in normalized} != set(ids) or len({row['id'] for row in normalized}) != len(normalized):
        raise BaselineUnavailable('period_reports_unavailable')
    selected = [row for row in normalized if row['id'] == WATER_ID]
    if len(selected) != 1:
        raise BaselineUnavailable('period_reports_unavailable')
    core = {'date_from': bounds[0], 'date_to': bounds[1], 'grouping': 'item',
        'received_at': received.isoformat(), 'pages_complete': True, 'unit_contract': UNIT,
        'total': len(ids), 'ids': sorted(ids), 'page_offsets': list(offsets),
        'rows': sorted(normalized, key=lambda row: row['id'])}
    return {'source': 'own_ads_stats_v2_period', 'date_from': bounds[0], 'date_to': bounds[1],
        'grouping': 'item', 'received_at': received.isoformat(), 'pages_complete': True,
        'unit_contract': UNIT, 'group_count': len(ids), 'ids': sorted(ids),
        'page_offsets': list(offsets), 'report_digest': projects._digest(core),
        'avito_id': WATER_ID, 'period_row_present': True,
        'metrics': selected[0]['metrics'],
        'metrics_known': {key: selected[0]['metrics'][key] is not None for key in FIELDS},
        'provider_timezone': 'unknown', 'daily_coverage_known': False}


def _evaluation(value):
    if (type(value) is not dict or value.get('state') != 'not_started'
            or value.get('source_dates') != 14 or value.get('primary_metric') != 'qualified_inquiry_share'
            or value.get('minimum_classified_inquiries') != 10):
        raise BaselineUnavailable('project_unavailable')
    keys = ('state', 'source_dates', 'start_trigger', 'primary_metric', 'metric_label',
            'minimum_classified_inquiries', 'guard_is_statistical_significance', 'conditions_insufficient')
    return {key: deepcopy(value[key]) for key in keys if key in value}


def _context(connection, actor, *, locked=False):
    current, actor = projects._publication_connection(connection.pk, actor, locked=locked)
    if not projects._pilot(current):
        raise PermissionDenied
    query = AvitoServicesFeed.objects.select_related('connection__channel')
    if locked:
        query = query.select_for_update()
    feed = query.get(connection=current)
    publication = projects._source(current, feed)
    binding = projects._digest(projects._binding(current))
    if not projects._binding(current).get('credential_stamp'):
        raise PermissionDenied
    return current, actor, feed, publication, binding


def _feed_state(feed, publication):
    return (feed.revision, feed.published_revision, deepcopy(feed.draft),
            deepcopy(feed.published), publication['version'].pk)


def _parent(current, actor, feed, publication, project_id):
    # The existing project validator owns Title, source identity and permissions.
    context = projects.page_context(current, actor)
    candidates = [item for item in context['projects']
        if item['avito_id'] == WATER_ID and item['event'].pk == project_id]
    if len(candidates) != 1:
        raise BaselineUnavailable('project_unavailable')
    item = candidates[0]
    source = {row['AvitoId']: row for row in entries(feed.published)}
    draft = {row['AvitoId']: row for row in entries(feed.draft)}
    row, draft_row = source.get(WATER_ID), draft.get(WATER_ID)
    if (not item['current_source'] or not item['draft_unchanged_since_project']
            or item['event'].source_version_id != publication['version'].pk or not row or not draft_row
            or row.get('Id') != draft_row.get('Id') or row.get('Title') != item['before']
            or draft_row.get('Title') != item['before']):
        raise BaselineUnavailable('project_unavailable')
    return item, row


def _key(change):
    return projects._digest({key: value for key, value in change.items() if key != 'key'})


def prepare(connection, actor, *, expected_project_id=9):
    """Append evidence only. The existing project and all feed contents stay immutable."""
    with transaction.atomic():
        current, actor, feed, publication, binding = _context(connection, actor, locked=True)
        before = _feed_state(feed, publication)
        item, row = _parent(current, actor, feed, publication, expected_project_id)
        raw = period_reports(current, actor)
        now = timezone.now()
        if (type(raw) is not dict or raw.get('state') != 'available'
                or raw.get('source') != 'own_ads_stats_v2_period'
                or raw.get('binding_digest') != binding or type(raw.get('reports')) is not list):
            raise BaselineUnavailable('period_reports_unavailable')
        snapshots = []
        for bounds in PERIODS:
            matches = [report for report in raw['reports'] if type(report) is dict
                and (report.get('date_from'), report.get('date_to')) == bounds and report.get('grouping') == 'item']
            if len(matches) != 1:
                raise BaselineUnavailable('period_reports_unavailable')
            snapshots.append(_report(matches[0], bounds, now))
        latest, actor, checked_feed, checked_publication, checked_binding = _context(current, actor, locked=True)
        if checked_binding != binding or before != _feed_state(checked_feed, checked_publication):
            raise PermissionDenied
        evaluation = _evaluation(item['project']['evaluation'])
        change = {'kind': KIND, 'schema_version': 1, 'field': 'experiment_period_baseline',
            'scope': projects._scope(latest), 'binding_sha256': binding,
            'parent_project_event_id': item['event'].pk, 'avito_id': WATER_ID, 'id': row['Id'],
            'publication_version_id': publication['version'].pk,
            'publication_revision': publication['version'].revision,
            'source_file_sha256': publication['version'].file_digest,
            'variable': 'Title', 'state': 'prepared_not_applied',
            'current_period': snapshots[0], 'previous_period': snapshots[1],
            'same_report_id_set': snapshots[0]['ids'] == snapshots[1]['ids'],
            'evaluation': evaluation,
            'quality_baseline': {'state': 'requires_manager_classification', 'value': None},
            'wordstat': wordstat_observation(), 'provider_content_verified': False}
        change['key'] = _key(change)
        existing = checked_feed.versions.filter(action='verify', changes__0__kind=KIND,
            changes__0__key=change['key']).order_by('-pk').first()
        if existing:
            event, state = existing, 'existing'
        else:
            event = record_version(checked_feed, 'verify', checked_feed.draft, checked_feed.draft,
                actor=actor, reason='База первого проекта: периодные отчёты; изменение объявления не применено.',
                source_version=checked_publication['version'], extra_changes=[change])
            state = 'prepared'
        return {'state': state, 'connection_id': 4, 'account_id': projects.ACCOUNT,
            'avito_id': WATER_ID, 'parent_project_event_id': item['event'].pk,
            'history_event_id': event.pk, 'publication_revision': publication['version'].revision,
            'periods': [{'from': value['date_from'], 'to': value['date_to'],
                'received_at': value['received_at'], 'pages_complete': True,
                'group_count': value['group_count'], 'period_row_present': True,
                'known_metric_codes': [key for key in FIELDS if value['metrics_known'][key]]}
                for value in snapshots],
            'variable': 'Title', 'evaluation_state': 'not_started', 'source_dates': 14,
            'minimum_classified_inquiries': 10, 'quality_baseline_known': False,
            'daily_rows_created': False, 'draft_changed': False, 'published_changed': False,
            'provider_action_performed': False, 'titles_exported': False}


def _saved(change, event, current, item, source, binding, now):
    if (type(change) is not dict or change.get('kind') != KIND or change.get('schema_version') != 1
            or change.get('scope') != projects._scope(current) or change.get('binding_sha256') != binding
            or change.get('parent_project_event_id') != item['event'].pk or change.get('avito_id') != WATER_ID
            or change.get('publication_version_id') != source['version'].pk
            or event.source_version_id != source['version'].pk
            or change.get('publication_revision') != source['version'].revision
            or change.get('source_file_sha256') != source['version'].file_digest
            or change.get('state') != 'prepared_not_applied' or change.get('variable') != 'Title'
            or change.get('provider_content_verified') is not False or change.get('key') != _key(change)):
        return None
    try:
        evaluation = _evaluation(change.get('evaluation'))
        periods = []
        # Revalidate saved private metric slots before rendering.
        for key, bounds in zip(('current_period', 'previous_period'), PERIODS):
            value = change.get(key)
            if type(value) is not dict or value.get('date_from') != bounds[0] or value.get('date_to') != bounds[1]:
                return None
            metrics = value.get('metrics')
            if type(metrics) is not dict:
                return None
            clean = {slug: _number(metrics.get(slug), slug in daily.COUNTERS) for slug in FIELDS}
            if (type(value.get('group_count')) is not int or not 1 <= value['group_count'] <= 200
                    or type(value.get('ids')) is not list or len(value['ids']) != value['group_count']
                    or any(type(identifier) is not str or not re.fullmatch(r'[1-9][0-9]{0,19}', identifier) for identifier in value['ids'])
                    or len(set(value['ids'])) != len(value['ids']) or WATER_ID not in value['ids']
                    or not isinstance(value.get('report_digest'), str) or not re.fullmatch(r'[0-9a-f]{64}', value['report_digest'])
                    or value.get('source') != 'own_ads_stats_v2_period' or value.get('grouping') != 'item'
                    or value.get('unit_contract') != UNIT or value.get('pages_complete') is not True
                    or value.get('daily_coverage_known') is not False or value.get('period_row_present') is not True
                    or value.get('avito_id') != WATER_ID or projects._time(value.get('received_at')) is None):
                return None
            periods.append({'date_from': bounds[0], 'date_to': bounds[1],
                'received_at': projects._time(value['received_at']), 'group_count': value.get('group_count'),
                'pages_complete': True, 'daily_coverage_known': False,
                'metrics': daily.metric_display(clean)})
        if change.get('wordstat') != wordstat_observation():
            return None
    except (BaselineUnavailable, TypeError, ValueError):
        return None
    return {'state': 'available', 'history_event_id': event.pk, 'created_at': event.created_at,
        'actor': event.actor, 'parent_project_event_id': item['event'].pk, 'periods': periods,
        'same_report_id_set': change.get('same_report_id_set') is True,
        'evaluation': evaluation, 'quality_baseline_known': False,
        'is_stale': any(projects._fresh(period['received_at'], now, MAX_REPORT_AGE) is None for period in periods),
        'provider_content_verified': False}


def decorate_context(context, actor):
    """Read supplementary history only; never append on GET."""
    current, actor, feed, source, binding = _context(context['connection'], actor)
    before = _feed_state(feed, source)
    now = timezone.now()
    context['wordstat_observation'] = wordstat_observation()
    for item in context['projects']:
        if item['avito_id'] != WATER_ID:
            continue
        item['period_baseline'] = None
        if not item['current_source']:
            continue
        for event in feed.versions.filter(action='verify', changes__0__kind=KIND).select_related(
                'actor', 'source_version').order_by('-pk')[:50]:
            changes = event.changes
            if type(changes) is not list or len(changes) != 1:
                continue
            found = _saved(changes[0], event, current, item, source, binding, now)
            if found:
                item['period_baseline'] = found
                break
    latest, actor, latest_feed, latest_source, latest_binding = _context(current, actor)
    if binding != latest_binding or before != _feed_state(latest_feed, latest_source):
        raise PermissionDenied
    context['connection'], context['feed'] = latest, latest_feed
    return context


def setup_once():
    """Existing protected deploy operator may prepare DB-only evidence after reports exist."""
    result = {'state': 'unavailable', 'code': 'period_reports_unavailable',
        'connection_id': 4, 'account_id': projects.ACCOUNT, 'provider_action_performed': False}
    if os.environ.get('SERVICE2_AVITO_DEPLOY_SETUP') != '1':
        return {**result, 'code': 'deployment_origin_unverified'}
    try:
        from .avito_services_workplace_setup import _actor, _connection
        connection = _connection(4)
        return prepare(connection, _actor(connection))
    except PermissionDenied:
        return {**result, 'code': 'scope_changed'}
    except BaselineUnavailable as exc:
        code = exc.args[0] if exc.args and exc.args[0] in {'project_unavailable', 'period_reports_unavailable'} else 'baseline_unavailable'
        return {**result, 'code': code}
    except Exception:
        return {**result, 'code': 'baseline_unavailable'}
