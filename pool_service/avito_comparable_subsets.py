"""Explicit comparable subsets of saved facts; full-account totals stay separate."""
from decimal import Decimal

from . import avito_daily_statistics as daily
from .avito_decision_report import _number, MIN_VIEWS, MIN_CONTACTS

INPUTS = {'ctr': ('views', 'impressions'), 'conversion': ('contacts', 'views'),
          'cpl_cash': ('spending', 'contacts')}
PAIR_KEYS = ('day_to_previous_day', 'seven_to_previous_seven')


def _identifier(value):
    return isinstance(value, str) and value.isascii() and value.isdecimal() and 0 < len(value) <= 20


def _count(value, maximum=daily.MAX_ROWS*daily.BACKFILL_DAYS):
    return value if type(value) is int and 0 <= value <= maximum else None


def _sum(ads, index, key):
    values = [_number(ad['periods'][index]['metric_values'].get(key), counter=key in daily.COUNTERS)
              for ad in ads]
    return format(sum(values), 'f') if values and all(value is not None for value in values) else None


def _volume(ads, current, previous):
    for index in (current, previous):
        views, contacts = _number(_sum(ads, index, 'views')), _number(_sum(ads, index, 'contacts'))
        if views is None or contacts is None or views < MIN_VIEWS or contacts < MIN_CONTACTS:
            return True
    return False


def build(ads, periods, *, truncated=False):
    """Each metric uses identical IDs and all required source dates on both sides."""
    output = []
    descriptions = daily.metric_display({})
    for pair_key, title, current_index, previous_index in (
            (PAIR_KEYS[0], 'Сопоставимая часть: дата источника с предыдущей', 0, 1),
            (PAIR_KEYS[1], 'Сопоставимая часть: 7 дат с предыдущими 7', 2, 3)):
        current, previous = periods[current_index], periods[previous_index]
        known_ids = {identifier for period in (current, previous)
                     for identifier in period['cohort_ids'] if _identifier(identifier)}
        metrics = []
        for description in descriptions:
            key = description['key']
            required = INPUTS.get(key, (key,))
            included = [ad for ad in ads if ad['avito_id'] in known_ids
                and all(_number(ad['periods'][index]['metric_values'].get(field),
                    counter=field in daily.COUNTERS) is not None
                    for index in (current_index, previous_index) for field in required)]
            included_ids = sorted(ad['avito_id'] for ad in included)
            excluded_ids = sorted(known_ids-set(included_ids))
            amounts = []
            for index in (current_index, previous_index):
                inputs = {field: _sum(included, index, field) for field in required}
                # Ratios use sums of the same inputs/IDs, never per-ad averages.
                amounts.append(next(item['value'] for item in daily.metric_display(inputs)
                                    if item['key'] == key))
            value, before = amounts
            available = bool(included_ids) and value is not None and before is not None
            delta = format(Decimal(value)-Decimal(before), 'f') if available else None
            relative = daily.ratio({'change': delta, 'before': before},
                'change', 'before', Decimal('100')) if available else None
            metrics.append({'key': key, 'label': description['label'], 'unit': description['unit'],
                'current': value, 'previous': before, 'delta': delta,
                'delta_unit': 'п.п.' if description['unit'] == '%' else description['unit'],
                'relative_change': relative, 'available': available,
                'included_ids': included_ids, 'excluded_ids': excluded_ids[:daily.MAX_ROWS],
                'included_ads': len(included_ids), 'excluded_ads': len(excluded_ids),
                'excluded_ids_truncated': len(excluded_ids) > daily.MAX_ROWS,
                'known_ads': len(known_ids), 'required_inputs': list(required),
                'current_cells': len(included_ids)*current['total_days'],
                'previous_cells': len(included_ids)*previous['total_days'],
                'low_or_unknown_volume': _volume(included, current_index, previous_index)})
        output.append({'key': pair_key, 'label': title, 'source': 'avito_stats_v2_items',
            'provider_timezone': 'unknown', 'barnaul_calendar_confirmed': False,
            'current_from': current['from'], 'current_to': current['to'],
            'previous_from': previous['from'], 'previous_to': previous['to'],
            'current_completed_dates': current['completed_days'], 'current_dates': current['total_days'],
            'previous_completed_dates': previous['completed_days'], 'previous_dates': previous['total_days'],
            'current_received_at': current['received_at'], 'previous_received_at': previous['received_at'],
            'known_cohort_complete': current['pages_complete'] and previous['pages_complete'],
            'source_cohort_changed': current['cohort_ids'] != previous['cohort_ids']
                or not current['cohort_stable'] or not previous['cohort_stable'],
            'truncated': truncated, 'metrics': metrics})
    return output


def evidence(comparisons, *, connection_id, account_id):
    """Pure closed metadata; the protected caller must validate actor/org/binding."""
    if type(connection_id) is not int or connection_id != 4 or account_id != '446312402':
        return {'schema_version': 1, 'state': 'blocked', 'code': 'statistics_binding_changed'}
    allowed = {item['key'] for item in daily.metric_display({})}
    pairs = []
    for pair in comparisons:
        if not isinstance(pair.get('key'), str) or pair['key'] not in PAIR_KEYS:
            continue
        metrics = []
        for item in pair.get('metrics', []):
            if not isinstance(item.get('key'), str) or item['key'] not in allowed:
                continue
            included = [identifier for identifier in item['included_ids'] if _identifier(identifier)][:daily.MAX_ROWS]
            excluded = [identifier for identifier in item['excluded_ids'] if _identifier(identifier)][:daily.MAX_ROWS]
            metrics.append({'key': item['key'], 'included_ids': included, 'excluded_ids': excluded,
                'included_ads': len(included), 'excluded_ads': _count(item['excluded_ads']),
                'excluded_ids_truncated': item['excluded_ids_truncated'] is True,
                'available': item['available'] is True,
                'low_or_unknown_volume': item['low_or_unknown_volume'] is not False})
        pairs.append({'key': pair['key'], 'current_from': pair['current_from'].isoformat(),
            'current_to': pair['current_to'].isoformat(), 'previous_from': pair['previous_from'].isoformat(),
            'previous_to': pair['previous_to'].isoformat(),
            'current_completed_dates': _count(pair['current_completed_dates'], 30),
            'current_dates': _count(pair['current_dates'], 30),
            'previous_completed_dates': _count(pair['previous_completed_dates'], 30),
            'previous_dates': _count(pair['previous_dates'], 30),
            'known_cohort_complete': pair['known_cohort_complete'] is True,
            'source_cohort_changed': pair['source_cohort_changed'] is True,
            'truncated': pair['truncated'] is True, 'metrics': metrics})
    return {'schema_version': 1, 'state': 'observed', 'connection_id': 4, 'account_id': '446312402',
        'source': 'saved_avito_stats_v2_items_comparable_subset',
        'provider_timezone': 'unknown', 'barnaul_calendar_confirmed': False, 'pairs': pairs}


def safe_setup_evidence(now=None):
    """DB-only scoped receipt for the existing protected exact-main setup."""
    from . import avito_decision_report
    from .communication_models import ChannelConnection
    from .communication_services import organization_access, conversation_capability
    from .avito_services_workplace_setup import _actor

    unavailable = {'schema_version': 1, 'state': 'unavailable',
                   'code': 'statistics_subset_unavailable'}

    def authorized(connection, actor):
        return bool(connection is not None and getattr(actor, 'is_authenticated', False)
            and getattr(actor, 'is_active', False) and daily._scope(connection)
            and organization_access(actor, connection.channel.organization) is not None
            and conversation_capability(actor, 'can_manage_channels', connection.channel.organization)
            and daily._allowed(connection, daily._state(connection)))

    try:
        current = ChannelConnection.objects.select_related('channel').filter(pk=4).first()
        if current is None or not daily._scope(current):
            return unavailable
        actor = _actor(current)
        if not authorized(current, actor):
            return unavailable
        before = (daily._stamp(current), current.channel.organization_id,
                  current.channel_id, str(current.external_id))
        _, rows, day_runs = daily._source_history(current, now)
        report = avito_decision_report.build(rows, day_runs, now)
        output = evidence(report['comparable_subsets'], connection_id=current.pk,
                          account_id=str(current.external_id))
        current.refresh_from_db()
        actor.refresh_from_db()
        if (not authorized(current, actor)
                or before != (daily._stamp(current), current.channel.organization_id,
                              current.channel_id, str(current.external_id))):
            return unavailable
        return output
    except Exception:
        # Neither exception text/causes nor partial private report data is exposed.
        return unavailable
