"""Closed DB-only evidence for one priority announcement; no provider requests."""
from datetime import timedelta
from decimal import Decimal, InvalidOperation

from django.db.models import Max, Q

from .avito_statistics_models import AvitoStatisticsRun, AvitoDailyStatistic

TARGET = '8450664985'
PHASES = {'authorization', 'fetch_statistics', 'verify_account', 'validate_response', 'pagination'}
EXCEPTIONS = {'AvitoCooldownError', 'AvitoRetryableError', 'AvitoError', 'ValueError',
              'TypeError', 'KeyError', 'RuntimeError', 'InvalidOperation', 'unexpected_exception'}
STATES = {
    'no_completed_request': 'Нет завершённого сбора этой даты: отсутствие показателей пока не объясняется ответом Авито.',
    'not_in_known_manifest': 'Объявления нет в известном наборе этой даты; его прежняя активность не установлена.',
    'provider_omitted_row': 'Страницы получены полностью, но Авито не вернул строку этого объявления. Это не нулевые показатели.',
    'local_fact_missing': 'Завершённый ответ сохранён, но ожидаемая суточная запись отсутствует в Service2.',
    'local_fact_mismatch': 'Суточная запись Service2 не совпадает с выбранным завершённым сбором; показатели не подтверждены.',
    'request_provenance_inconsistent': 'Сохранённая пагинация не подтверждает непрерывный запрос всех страниц.',
    'provider_metrics_missing': 'Строка Авито есть, но часть показателей не возвращена или не подтверждена.',
    'known': 'Строка и сохранённые показатели подтверждены; известные нули отличаются от отсутствующих значений.',
}


def _iso(value):
    return value.isoformat() if value is not None else None


def _count(value, maximum=200):
    return value if type(value) is int and 0 <= value <= maximum else None


def _known(value, counter):
    if not isinstance(value, str):
        return False
    try:
        number = Decimal(value)
        return number.is_finite() and (not counter or number >= 0 and number == number.to_integral())
    except (InvalidOperation, ValueError):
        return False


def _pages(run):
    """Export exact saved request labels/offsets, never arbitrary provenance keys."""
    if run is None:
        return [], False
    pages = run.page_provenance
    if not isinstance(pages, list) or len(pages) > 200:
        return [], False
    output, next_offset, consistent = [], 0, True
    for page in pages:
        if not isinstance(page, dict):
            return [], False
        offset, rows = _count(page.get('offset')), _count(page.get('rows'), 50)
        valid = (offset is not None and rows is not None
            and page.get('date_from') == run.day.isoformat()
            and page.get('date_to') == run.day.isoformat()
            and page.get('grouping') == 'item'
            and page.get('unit_contract') == 'stats_v2_confirmed_metrics_v1'
            and page.get('source_money_unit') == 'kopeck' and page.get('stored_money_unit') == 'RUB')
        if not valid:
            consistent = False
            continue
        output.append({'offset': offset, 'rows': rows})
        consistent = consistent and offset == next_offset
        next_offset = offset + rows
    return output, bool(output) and consistent and next_offset == run.received_rows == run.expected_total


def _attempt(state, runs, daily):
    """Only current-binding runs can back new telemetry; old generic failures stay unknown."""
    raw = state.get('last_attempt')
    if not isinstance(raw, dict):
        return {'state': 'historical_unknown', 'phase': 'unknown',
            'error_code': daily._error_code(state.get('last_error_code')) if state.get('last_error_code') else '',
            'finished_at': _iso(daily._date(state.get('last_checked_at')))}
    run = next((item for item in runs if item.pk == raw.get('run_id')), None)
    if (run is None or type(raw.get('run_id')) is not int
            or raw.get('source_date') != run.day.isoformat()
            or not isinstance(raw.get('phase'), str) or raw['phase'] not in PHASES
            or not isinstance(raw.get('exception_class'), str) or raw['exception_class'] not in EXCEPTIONS | {''}
            or not isinstance(raw.get('outcome'), str) or raw['outcome'] not in {'success', 'failed', 'skipped'}
            or _count(raw.get('offset')) is None
            or daily._date(raw.get('started_at')) is None or daily._date(raw.get('finished_at')) is None):
        return {'state': 'unconfirmed', 'phase': 'unknown', 'error_code': 'statistics_unavailable'}
    return {'state': 'observed', 'run_id': run.pk, 'source_date': run.day.isoformat(),
        'offset': raw['offset'], 'phase': raw['phase'], 'exception_class': raw['exception_class'],
        'error_code': daily._error_code(raw['error_code']) if raw.get('error_code') else '',
        'outcome': raw['outcome'], 'started_at': daily._date(raw['started_at']).isoformat(),
        'finished_at': daily._date(raw['finished_at']).isoformat()}


def evidence(connection, now=None):
    """Caller has established UI permission or protected receipt scope; binding is rechecked."""
    from . import avito_daily_statistics as daily
    state = daily._state(connection)
    if not daily._scope(connection) or not daily._allowed(connection, state):
        return {'schema_version': 1, 'state': 'blocked', 'code': 'statistics_binding_changed'}
    cutoff = daily.collection_cutoff(now)
    start = cutoff - timedelta(days=daily.BACKFILL_DAYS-1)
    scoped = AvitoStatisticsRun.objects.filter(connection=connection, account_id=daily.ACCOUNT,
        source='avito_stats_v2_items',
        organization_id_at_collection=connection.channel.organization_id,
        binding_digest=daily._stamp(connection), day__range=(start, cutoff))
    # At most two runs per source date, even after many failed or repeated attempts.
    references = scoped.values('day').annotate(latest_id=Max('pk'),
        complete_id=Max('pk', filter=Q(state='success', pagination_complete=True)))
    identifiers = {item[key] for item in references for key in ('latest_id', 'complete_id')
        if item[key] is not None}
    runs = list(scoped.filter(pk__in=identifiers).order_by('-pk'))
    latest, complete = {}, {}
    for run in runs:
        latest.setdefault(run.day, run)
        if run.state == 'success' and run.pagination_complete is True:
            complete.setdefault(run.day, run)
    facts = list(AvitoDailyStatistic.objects.filter(connection=connection, account_id=daily.ACCOUNT,
        organization_id_at_collection=connection.channel.organization_id, avito_id=TARGET,
        day__range=(start, cutoff)).select_related('source_run'))
    by_day = {item.day: item for item in facts}
    codes = daily.COUNTERS + daily.MONEY
    dates = []
    for offset in range(daily.BACKFILL_DAYS):
        day = cutoff-timedelta(days=offset)
        run, attempt, fact = complete.get(day), latest.get(day), by_day.get(day)
        pages, provenance_ok = _pages(run)
        staged = run.staged_rows if run and isinstance(run.staged_rows, list) else []
        returned = [item for item in staged if isinstance(item, dict) and item.get('id') == TARGET]
        manifest_member = TARGET in daily._expected_ids(run) if run else None
        provider_present = bool(returned) if run else None
        matches = bool(run and fact and fact.source_run_id == run.pk)
        raw = returned[0].get('metrics') if len(returned) == 1 else None
        raw = raw if isinstance(raw, dict) else {}
        expected = {key: raw.get(key) if provider_present else None for key in codes}
        stored = fact.metrics if fact and isinstance(fact.metrics, dict) else {}
        consistent = bool(matches and len(returned) <= 1
            and fact.provider_row_present == provider_present
            and all(stored.get(key) == expected[key] for key in codes))
        known = [key for key in codes if consistent and provider_present
            and _known(expected[key], key in daily.COUNTERS)]
        zeros = [key for key in known if Decimal(expected[key]) == 0]
        code = ('no_completed_request' if run is None
            else 'request_provenance_inconsistent' if not provenance_ok
            else 'not_in_known_manifest' if not manifest_member
            else 'local_fact_missing' if fact is None
            else 'local_fact_mismatch' if not consistent
            else 'provider_omitted_row' if not provider_present
            else 'provider_metrics_missing' if len(known) != len(codes)
            else 'known')
        dates.append({'date': day.isoformat(), 'date_from': day.isoformat(), 'date_to': day.isoformat(),
            'grouping': 'item', 'latest_complete_run_id': run.pk if run else None,
            'completed_at': _iso(run.finished_at) if run else None,
            'pages_complete': run is not None, 'request_provenance_consistent': provenance_ok,
            'pages': pages, 'expected_total': _count(run.expected_total) if run else None,
            'received_rows': _count(run.received_rows) if run else None,
            'expected_manifest_member': manifest_member, 'provider_row_present': provider_present,
            'stored_row_present': fact is not None, 'stored_row_matches_run': matches,
            'stored_row_consistent': consistent, 'known_metric_count': len(known),
            'missing_metric_codes': [key for key in codes if key not in known],
            'zero_metric_codes': zeros, 'state': code,
            'latest_attempt_run_id': attempt.pk if attempt else None,
            'latest_attempt_state': attempt.state if attempt and attempt.state in
                {'collecting', 'success', 'partial', 'failed'} else None,
            'latest_attempt_error_code': daily._error_code(attempt.error_code) if attempt and attempt.error_code else '',
            'latest_attempt_finished_at': _iso(attempt.finished_at) if attempt else None})
    return {'schema_version': 1, 'state': 'observed', 'connection_id': connection.pk,
        'account_id': daily.ACCOUNT, 'avito_id': TARGET, 'source': 'avito_stats_v2_items',
        'provider_timezone': 'unknown', 'from': start.isoformat(), 'to': cutoff.isoformat(),
        'request_scope': 'all_account_items_no_item_filter', 'unit_contract': 'stats_v2_confirmed_metrics_v1',
        'manifest_is_request_filter': False,
        'completed_dates': len(complete), 'expected_dates': daily.BACKFILL_DAYS,
        'states': {code: sum(item['state'] == code for item in dates) for code in STATES},
        'last_attempt': _attempt(state, runs, daily), 'dates': dates}


def display(receipt):
    """Human labels are UI-only; the protected receipt remains closed metadata."""
    if receipt.get('state') != 'observed':
        return receipt
    from . import avito_daily_statistics as daily
    attempt = receipt['last_attempt']
    return {**receipt, 'last_attempt': {**attempt,
        'finished_at': daily._date(attempt.get('finished_at'))},
        'dates': [{**item, 'explanation': STATES[item['state']],
            'completed_at': daily._date(item['completed_at']),
            'latest_attempt_finished_at': daily._date(item['latest_attempt_finished_at'])}
            for item in receipt['dates']]}
