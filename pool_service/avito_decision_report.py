"""Decision reports over saved Avito facts; no provider I/O or CRM attribution."""
from datetime import timedelta
from decimal import Decimal, InvalidOperation
import hashlib
import json

from django.core.exceptions import PermissionDenied
from django.utils import timezone

WINDOW_KEYS = ('day', 'previous_day', 'seven_days', 'previous_seven_days', 'thirty_days')
CORE = ('impressions', 'views', 'contacts', 'spending')
MIN_VIEWS = 100
MIN_CONTACTS = 10
ACTION_CODES = {'review_contact_quality', 'inspect_missing_statistics', 'review_descriptive_change'}


def _number(value, *, counter=False):
    if value is None or isinstance(value, bool):
        return None
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None
    if not result.is_finite() or counter and (result < 0 or result != result.to_integral()):
        return None
    return result


def _ad_period(identifier, rows, runs, period):
    from . import avito_daily_statistics as daily
    start, end = period['from'], period['to']
    days = [start + timedelta(days=i) for i in range((end-start).days+1)]
    selected, duplicates = {}, set()
    for row in rows:
        if row.avito_id != identifier or row.day not in days or row.day not in runs:
            continue
        if row.source_run_id != runs[row.day].pk:
            continue
        if row.day in selected:
            duplicates.add(row.day)
        else:
            selected[row.day] = row
    for day in duplicates:
        selected.pop(day, None)
    reported = {day for day, row in selected.items() if row.provider_row_present}
    known_manifest_days = {day for day in days if day in runs and identifier in daily._expected_ids(runs[day])}
    missing_days = sum(day not in runs for day in days)
    values, coverage = {}, {}
    for key in daily.COUNTERS + daily.MONEY:
        known = [_number(selected[day].metrics.get(key), counter=key in daily.COUNTERS)
            for day in days if day in reported]
        coverage[key] = sum(value is not None for value in known)
        values[key] = (format(sum(known), 'f') if not missing_days
            and len(reported) == len(days) and len(known_manifest_days) == len(days)
            and coverage[key] == len(days) else None)
    metrics = daily.metric_display(values)
    for item in metrics:
        item['known_cells'] = coverage.get(item['key'])
    return {'key': period['key'], 'label': period['label'], 'from': start, 'to': end,
        'source': 'avito_stats_v2_items', 'provider_timezone': 'unknown',
        'completed_days': len(days)-missing_days, 'total_days': len(days),
        'missing_days': missing_days, 'pages_complete': not missing_days,
        # An expected row is a requirement for this comparison, not a historic active claim.
        'expected_cells': len(days), 'reported_cells': len(reported), 'stored_cells': len(selected),
        'missing_ad_cells': len(days)-len(reported), 'known_manifest_days': len(known_manifest_days),
        'cohort_ids': [identifier], 'cohort_stable': not missing_days and len(known_manifest_days) == len(days),
        'metrics': metrics, 'metric_values': {item['key']: item['value'] for item in metrics},
        'metric_coverage': coverage,
        'received_at': max((runs[day].finished_at for day in days
            if day in runs and runs[day].finished_at is not None), default=None),
        'run_ids': [runs[day].pk for day in days if day in runs],
        'provider_calendar_aligned': False}


def _action(ad, code, issue, reason, next_step, basis, review_on):
    current = ad['periods'][2]
    return {'avito_id': ad['avito_id'], 'code': code, 'issue': issue,
        'reason': reason, 'next_step': next_step, 'basis': basis,
        'from': current['from'], 'to': current['to'], 'review_on': review_on,
        'source': 'saved_avito_stats_v2_items', 'causality_confirmed': False}


def _actions(ads, now):
    from . import avito_daily_statistics as daily
    review_on = timezone.localdate(now, timezone=daily.ZONE) + timedelta(days=1)
    actions, used = [], set()
    # Contacts are provider events, never orders or unique people.
    contacted = [ad for ad in ads if _number(ad['periods'][2]['metric_values'].get('contacts')) is not None
        and Decimal(ad['periods'][2]['metric_values']['contacts']) > 0]
    contacted.sort(key=lambda ad: (-Decimal(ad['periods'][2]['metric_values']['contacts']), ad['avito_id']))
    if contacted:
        ad = contacted[0]
        actions.append(_action(ad, 'review_contact_quality', 'Подтвердить качество обращений',
            'За все даты выбранного периода источника известны контакты этого объявления; их качество и результат в CRM ещё требуют проверки.',
            'Оценить связанные обращения в CRM и указать результат и следующий шаг. Не считать контакты заказами.',
            'known_contacts', review_on))
        used.add(ad['avito_id'])
    gaps = [ad for ad in ads if ad['avito_id'] not in used
        and ad['periods'][2]['pages_complete'] and ad['periods'][2]['missing_ad_cells']]
    gaps.sort(key=lambda ad: (-ad['periods'][2]['missing_ad_cells'], ad['avito_id']))
    if gaps:
        ad = gaps[0]
        actions.append(_action(ad, 'inspect_missing_statistics', 'Проверить неполные строки статистики',
            'Все даты периода обработаны, но Авито вернул не все строки выбранного объявления. Причина отсутствия строки неизвестна.',
            'Сверить состояние объявления и доступную статистику. До сверки оставить отсутствующие показатели неизвестными.',
            'missing_provider_rows', review_on))
        used.add(ad['avito_id'])
    for ad in ads:
        if ad['avito_id'] in used:
            continue
        pair = ad['comparisons'][1]
        if pair['reason']:
            continue
        current, previous = pair['current']['metric_values'], pair['previous']['metric_values']
        cv, pv = _number(current.get('views')), _number(previous.get('views'))
        cc, pc = _number(current.get('contacts')), _number(previous.get('contacts'))
        cost, before_cost = _number(current.get('cpl_cash')), _number(previous.get('cpl_cash'))
        if (cv is None or pv is None or cc is None or pc is None or cost is None or before_cost is None
                or cv < MIN_VIEWS or pv < MIN_VIEWS or cc < MIN_CONTACTS or pc < MIN_CONTACTS
                or before_cost <= 0 or cost < before_cost * Decimal('1.5')):
            continue
        actions.append(_action(ad, 'review_descriptive_change', 'Разобрать описательный рост стоимости контакта',
            'В двух полных сопоставимых периодах не менее 100 просмотров и 10 контактов в каждом; денежные расходы на контакт выросли минимум на 50%. Это диагностический порог, а не доказательство устойчивой тенденции.',
            'Проверить качество обращений, даты правок и продвижения. Затем подготовить одну проверяемую гипотезу без автоматической публикации или покупки.',
            'covered_volume_and_cpl_change', review_on))
        break
    return actions[:3]


def build(rows, day_runs, now=None):
    from . import avito_daily_statistics as daily
    now = now or timezone.now()
    end = daily.collection_cutoff(now)
    raw_periods = daily.source_periods(rows, set(day_runs), now, day_runs=day_runs)
    periods = [{**period, 'key': key} for key, period in zip(WINDOW_KEYS, raw_periods)]
    identifiers = {row.avito_id for row in rows}
    for run in day_runs.values():
        identifiers.update(daily._expected_ids(run))
    identifiers = sorted(identifier for identifier in identifiers
        if isinstance(identifier, str) and identifier.isascii() and identifier.isdecimal() and len(identifier) <= 20)
    visible_ids = identifiers[:daily.MAX_ROWS]
    ads = []
    for identifier in visible_ids:
        saved = [row for row in rows if row.avito_id == identifier]
        latest = max(saved, key=lambda row: row.day, default=None)
        ad_periods = [_ad_period(identifier, rows, day_runs, period) for period in periods]
        current = ad_periods[2]
        views, contacts = _number(current['metric_values'].get('views')), _number(current['metric_values'].get('contacts'))
        ads.append({'avito_id': identifier, 'title': getattr(latest, 'title', ''),
            'service': getattr(latest, 'service_label', ''), 'city': getattr(latest, 'city_label', ''),
            'periods': ad_periods, 'current': current,
            'comparisons': daily.source_comparisons(ad_periods),
            'low_volume': views is None or views < MIN_VIEWS or contacts is None or contacts < MIN_CONTACTS,
            'core_metrics_known': all(current['metric_values'].get(key) is not None for key in CORE)})
    current = periods[2]
    actions = _actions(ads, now)
    return {'schema_version': 1, 'source': 'avito_stats_v2_items', 'provider_timezone': 'unknown',
        'barnaul_calendar_confirmed': False, 'from': end-timedelta(days=29), 'to': end,
        'periods': periods, 'account_current': current, 'ads': ads,
        'ad_count': len(identifiers), 'displayed_ad_count': len(ads), 'truncated': len(identifiers) > len(ads),
        'completed_dates': len(day_runs), 'expected_dates': daily.BACKFILL_DAYS,
        'fully_reported_ads_7': sum(ad['current']['pages_complete'] and not ad['current']['missing_ad_cells']
            and ad['current']['known_manifest_days'] == ad['current']['total_days'] for ad in ads),
        'core_metrics_known_ads_7': sum(ad['core_metrics_known'] for ad in ads),
        'low_volume_ads': sum(ad['low_volume'] for ad in ads), 'actions': actions,
        'received_at': current['received_at'],
        'limitations': [
            'Часовой пояс суточных групп Авито не подтверждён; используются даты источника.',
            'Отсутствующая строка или показатель остаются неизвестными. Полнота страниц не подтверждает полноту показателей.',
            'Контакты Авито не равны уникальным людям, заявкам, заказам или выручке.',
            'Порог 100 просмотров и 10 контактов служит осторожным фильтром объёма, а не статистическим доказательством.',
            'Качество и продажи оцениваются по подтверждённым связям существующей CRM.',
        ]}


def evidence(report):
    """Fixed metadata only: suitable for the existing protected setup log."""
    return {'schema_version': 1, 'ad_count': report['ad_count'],
        'displayed_ad_count': report['displayed_ad_count'], 'truncated': report['truncated'],
        'completed_dates': report['completed_dates'], 'expected_dates': report['expected_dates'],
        'fully_reported_ads_7': report['fully_reported_ads_7'],
        'core_fields_known_ads_7': report['core_metrics_known_ads_7'],
        'low_volume_ads': report['low_volume_ads'], 'source': report['source'],
        'provider_timezone': 'unknown', 'barnaul_calendar_confirmed': False,
        'actions': [{'avito_id': item['avito_id'], 'code': item['code'], 'basis': {'known_contacts': 'quality_review_ready', 'missing_provider_rows': 'source_coverage',
                'covered_volume_and_cpl_change': 'descriptive_change'}[item['basis']]}
            for item in report['actions'] if item['code'] in ACTION_CODES][:3]}


def decision_evidence(connection, actor, identifiers, now=None):
    """Authorized, DB-only metadata for draft/recommendation evidence.

    current_published confirms membership in the bound published snapshot only.
    It does not confirm an active provider status or exact provider input bytes.
    """
    from . import avito_daily_statistics as daily
    from .communication_models import ChannelConnection
    from .communication_services import organization_access, conversation_capability
    if not isinstance(identifiers, (list, tuple)) or not 1 <= len(identifiers) <= 20:
        raise ValueError('Invalid announcement identifiers')
    requested = []
    for identifier in identifiers:
        if (type(identifier) not in (str, int) or not str(identifier).isascii()
                or not str(identifier).isdecimal() or not 0 < len(str(identifier)) <= 20 or int(str(identifier)) <= 0):
            raise ValueError('Invalid announcement identifier')
        if str(identifier) not in requested:
            requested.append(str(identifier))
    current = ChannelConnection.objects.select_related('channel').filter(pk=connection.pk).first()
    if (current is None or not getattr(actor, 'is_authenticated', False) or not getattr(actor, 'is_active', False) or not daily._scope(current)
            or organization_access(actor, current.channel.organization) is None
            or not conversation_capability(actor, 'can_manage_channels', current.channel.organization)
            or not daily._allowed(current, daily._state(current))):
        raise PermissionDenied
    now = now or timezone.now()
    cutoff, rows, day_runs = daily._source_history(current, now)
    report = build(rows, day_runs, now)
    indexed = {ad['avito_id']: ad for ad in report['ads']}
    published = daily._dimensions(current)
    result = {}
    for identifier in requested:
        ad = indexed.get(identifier)
        period = ad['current'] if ad else None
        revision = published.get(identifier, {}).get('published_revision')
        value = {'available': ad is not None, 'current_published': identifier in published,
            'publication_revision': revision if type(revision) is int and revision > 0 else None,
            'from': (cutoff-timedelta(days=6)).isoformat(), 'to': cutoff.isoformat(),
            'pages_complete': bool(period and period['pages_complete']),
            'reported_days': period['reported_cells'] if period else 0,
            'expected_days': 7,
            'ad_rows_complete': bool(period and period['pages_complete']
                and not period['missing_ad_cells'] and period['known_manifest_days'] == period['total_days']),
            'metrics_known': {key: bool(period and period['metric_values'].get(key) is not None)
                for key in daily.COUNTERS+daily.MONEY+('ctr', 'conversion', 'cpl_cash')},
            'low_or_unknown_volume': ad['low_volume'] if ad else True,
            'received_at': period['received_at'].isoformat() if period and period['received_at'] else None,
            'run_ids': period['run_ids'] if period else [],
            'limitations': ['provider_timezone_unknown', 'no_order_attribution',
                'published_membership_is_not_active_status']}
        basis = {'connection_id': current.pk, 'account_id': daily.ACCOUNT,
            'organization_id': current.channel.organization_id, 'avito_id': identifier, **value}
        value['basis_digest'] = hashlib.sha256(json.dumps(basis, sort_keys=True).encode()).hexdigest()
        result[identifier] = value
    return {'schema_version': 1, 'source': 'saved_avito_stats_v2_items',
        'connection_id': current.pk, 'account_id': daily.ACCOUNT,
        'generated_at': now.isoformat(), 'cutoff': cutoff.isoformat(),
        'provider_timezone': 'unknown', 'barnaul_calendar_confirmed': False, 'ads': result}
