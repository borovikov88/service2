"""Read-only provider facts, isolated from CRM/finance and from local-calendar claims."""
from datetime import datetime, time as wall_time, timedelta
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
import hashlib
import json
import re
import time
import uuid
from zoneinfo import ZoneInfo

from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.db.models import F
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from .communication_models import ChannelConnection
from .communication_avito import AvitoError, access_token, authorized_account_id
from .avito_statistics_models import AvitoStatisticsRun, AvitoDailyStatistic
from .avito_services_workplace_setup import _binding
from .avito_status_monitor import subscriber_allowed
from . import avito_workspace

KEY = 'avito_daily_statistics_v1'
ZONE = ZoneInfo('Asia/Barnaul')
ACCOUNT = '446312402'
MAX_ROWS = 200
BACKFILL_DAYS = 30
COUNTERS = ('impressions', 'views', 'contacts', 'favorites', 'contactsShowPhone',
            'contactsMessenger', 'contactsShowPhoneAndMessenger')
MONEY = ('allSpending', 'spending', 'presenceSpending', 'promoSpending', 'restSpending', 'commission')
LABELS = {'impressions': 'Показы', 'views': 'Просмотры', 'contacts': 'Контакты Авито',
          'favorites': 'Избранное', 'contactsShowPhone': 'Просмотры телефона',
          'contactsMessenger': 'Обращения в чат', 'contactsShowPhoneAndMessenger': 'Телефон и чат',
          'allSpending': 'Все расходы: деньги и бонусы', 'spending': 'Денежные расходы',
          'presenceSpending': 'Размещение и целевые действия', 'promoSpending': 'Продвижение',
          'restSpending': 'Прочие расходы', 'commission': 'Комиссия'}
ERRORS = {
    'statistics_binding_changed': 'Привязка аккаунта, организации, ключей или права изменились.',
    'statistics_data_invalid': 'Статистика не прошла проверку структуры или показателей.',
    'statistics_total_unavailable': 'Авито не подтвердил общее число строк. Полнота не установлена.',
    'statistics_limit': 'Ответ превышает безопасный предел 200 объявлений.',
    'statistics_unavailable': 'Не удалось получить статистику. Сохранённые факты не заменены.',
    'statistics_incomplete': 'Сбор следующей страницы ещё не завершён.',
    'provider_cooldown': 'Действует общее ограничение частоты статистики Авито.',
}


def _date(value):
    try:
        parsed = parse_datetime(value) if isinstance(value, str) and len(value) <= 80 else None
        return parsed if parsed is not None and timezone.is_aware(parsed) else None
    except (TypeError, ValueError, OverflowError):
        return None


def _scope(connection):
    return bool(connection.pk == 4 and str(connection.external_id) == ACCOUNT
        and connection.is_active and connection.channel.is_active and connection.channel.kind == 'avito')


def _state(connection):
    metadata = connection.settings if isinstance(connection.settings, dict) else {}
    value = metadata.get(KEY)
    return dict(value) if isinstance(value, dict) else {}


def _stamp(connection):
    return hashlib.sha256(json.dumps(_binding(connection), sort_keys=True).encode()).hexdigest()


def _allowed(connection, state):
    actor = get_user_model().objects.filter(pk=state.get('actor_id'), is_active=True).first() if type(state.get('actor_id')) is int else None
    return bool(_scope(connection) and actor and _binding(connection).get('credential_stamp')
        and state.get('account_id') == ACCOUNT
        and state.get('organization_id') == connection.channel.organization_id
        and state.get('channel_id') == connection.channel_id
        and state.get('binding_digest') == _stamp(connection)
        and subscriber_allowed(actor, connection.channel.organization))


def configure(connection, actor, action):
    if action not in {'enable', 'disable', 'retry'}:
        raise ValueError('Неизвестное действие.')
    with transaction.atomic():
        connection = ChannelConnection.objects.select_for_update().select_related('channel').get(pk=connection.pk)
        if not _scope(connection) or not subscriber_allowed(actor, connection.channel.organization):
            raise PermissionDenied
        metadata = dict(connection.settings) if isinstance(connection.settings, dict) else {}
        state = _state(connection)
        if action == 'disable':
            state.update(enabled=False, generation=uuid.uuid4().hex)
        elif action == 'retry':
            if state.get('enabled') is not True or not _allowed(connection, state):
                raise ValueError('Сначала включите сбор для текущего подключения.')
            state['next_due_at'] = timezone.now().isoformat()
        elif state.get('enabled') is not True or not _allowed(connection, state):
            if not _binding(connection).get('credential_stamp'):
                raise ValueError('Сначала сохраните ключи подключения Авито.')
            state = {'enabled': True, 'generation': uuid.uuid4().hex, 'actor_id': actor.pk,
                'account_id': ACCOUNT, 'organization_id': connection.channel.organization_id,
                'channel_id': connection.channel_id, 'binding_digest': _stamp(connection),
                'next_due_at': timezone.now().isoformat(), 'last_error_code': ''}
        metadata[KEY] = state
        connection.settings = metadata
        connection.save(update_fields=['settings'])
    return state


def collection_cutoff(now=None):
    # A source day timezone is not specified by the available date-only contract.
    # The two-day lag is visible; it is NOT relabelled as Barnaul's yesterday.
    return timezone.localdate(now or timezone.now(), timezone=ZONE) - timedelta(days=2)


def requested_windows(now=None):
    yesterday = timezone.localdate(now or timezone.now(), timezone=ZONE) - timedelta(days=1)
    return [
        ('Вчера', yesterday, yesterday),
        ('Предыдущий день', yesterday - timedelta(days=1), yesterday - timedelta(days=1)),
        ('Последние 7 полных дней', yesterday - timedelta(days=6), yesterday),
        ('Предыдущие 7 дней', yesterday - timedelta(days=13), yesterday - timedelta(days=7)),
        ('30 полных дней', yesterday - timedelta(days=29), yesterday),
    ]


def _label(row, key, maximum):
    value = row.get(key)
    return value[:maximum] if isinstance(value, str) else ''



KNOWN_CITIES = ('Барнаул', 'Бийск', 'Горно-Алтайск')


def _city(row):
    explicit = _label(row, 'City', 200).strip()
    if explicit:
        return explicit, 'published_city_field'
    address = row.get('Address')
    if not isinstance(address, str) or len(address) > 2000:
        return '', 'unknown'
    found = set()
    for token in address.split(','):
        token = token.strip()
        match = re.fullmatch(r'(?:г\.?\s*|город\s+)?(Барнаул|Бийск|Горно-Алтайск)', token, re.IGNORECASE)
        if match:
            found.add(next(city for city in KNOWN_CITIES if city.casefold() == match[1].casefold()))
        elif re.fullmatch(r'(?:г\.?\s*|город\s+)[А-Яа-яЁё-]+', token, re.IGNORECASE):
            return '', 'unknown'  # An explicit different city makes the address ambiguous.
    return (next(iter(found)), 'published_address_exact_token') if len(found) == 1 else ('', 'unknown')


def _dimensions(connection):
    from .avito_feed_models import AvitoServicesFeed
    from .avito_services_feed import bound, entries
    feed = AvitoServicesFeed.objects.filter(connection=connection).first()
    if feed is None or not bound(feed, connection) or not feed.enabled:
        return {}
    output = {}
    for row in entries(feed.published):
        service_field = 'ServiceType' if row.get('ServiceType') else 'Title'
        city, city_source = _city(row)
        output[row['AvitoId']] = {
            'title': _label(row, 'Title', 300),
            'service_label': _label(row, service_field, 300),
            'service_source': service_field,
            # Explicit city or an unambiguous full comma-delimited known-city token.
            'city_label': city, 'city_source': city_source,
            'published_revision': feed.published_revision,
            'dimension_observed_at': timezone.now().isoformat(),
        }
    return output


def _choose_day(connection, now):
    latest = collection_cutoff(now)
    completed = set(AvitoStatisticsRun.objects.filter(
        connection=connection, account_id=ACCOUNT,
        organization_id_at_collection=connection.channel.organization_id, state='success', pagination_complete=True,
        day__range=(latest - timedelta(days=BACKFILL_DAYS-1), latest)).values_list('day', flat=True))
    return next((latest - timedelta(days=n) for n in range(BACKFILL_DAYS)
                 if latest - timedelta(days=n) not in completed), None)


def _reserve(connection_id):
    now = timezone.now()
    with transaction.atomic():
        connection = ChannelConnection.objects.select_for_update().select_related('channel').get(pk=connection_id)
        state = _state(connection)
        if not _allowed(connection, state) or state.get('enabled') is not True:
            return None
        due, until = _date(state.get('next_due_at')), _date(state.get('lease_until'))
        if (due and due > now) or (until and until > now):
            return None
        run = AvitoStatisticsRun.objects.filter(pk=state.get('run_id'), connection=connection,
            account_id=ACCOUNT, organization_id_at_collection=connection.channel.organization_id,
            state='collecting', binding_digest=state['binding_digest']).first()
        if run and (now - run.started_at > timedelta(days=1) or run.day > collection_cutoff(now)):
            run.state, run.error_code, run.finished_at = 'failed', 'statistics_incomplete', now
            run.save(update_fields=['state', 'error_code', 'finished_at'])
            run = None
        if run is None:
            day = _choose_day(connection, now)
            if day is None:
                tomorrow = timezone.localdate(now, timezone=ZONE) + timedelta(days=1)
                state['next_due_at'] = datetime.combine(tomorrow, wall_time(8), tzinfo=ZONE).isoformat()
                metadata = dict(connection.settings)
                metadata[KEY] = state
                connection.settings = metadata
                connection.save(update_fields=['settings'])
                return None
            run = AvitoStatisticsRun.objects.create(connection=connection, account_id=ACCOUNT,
                organization_id_at_collection=connection.channel.organization_id, day=day,
                binding_digest=state['binding_digest'], dimensions=_dimensions(connection))
        token = uuid.uuid4().hex
        state.update(run_id=run.pk, lease=token, lease_until=(now+timedelta(seconds=120)).isoformat())
        metadata = dict(connection.settings)
        metadata[KEY] = state
        connection.settings = metadata
        connection.save(update_fields=['settings'])
        return connection, state, run, token


def _normalized(row):
    identifier = row.get('id')
    if type(identifier) is not int or not 0 < identifier < 10**20:
        raise AvitoError('statistics_data_invalid')
    raw = {metric['slug']: metric.get('value') for metric in row.get('metrics', [])}
    output = {}
    for key in COUNTERS + MONEY:
        value = raw.get(key)
        if value is None:
            output[key] = None
            continue
        try:
            number = Decimal(str(value))
        except (InvalidOperation, ValueError):
            raise AvitoError('statistics_data_invalid') from None
        if not number.is_finite() or (key in COUNTERS and (number < 0 or number != number.to_integral())):
            raise AvitoError('statistics_data_invalid')
        output[key] = str(int(number)) if key in COUNTERS else format(number, '.2f')
    return {'id': str(identifier), 'metrics': output}


def collect_next(connection_id=4):
    """One API page per invocation. No sleeping or silent external retry."""
    if connection_id != 4:
        raise ValueError('statistics_binding_changed')
    reserved = _reserve(connection_id)
    if reserved is None:
        return 'skipped'
    connection, state, run, lease = reserved
    error, page, retry_at = '', None, None
    try:
        token = access_token(connection)
        if authorized_account_id(connection) != ACCOUNT:
            raise AvitoError('statistics_binding_changed')
        page = avito_workspace.fetch_statistics(token, ACCOUNT, start=run.day.isoformat(),
            end=run.day.isoformat(), grouping='item', offset=run.next_offset)
        if authorized_account_id(connection) != ACCOUNT:
            raise AvitoError('statistics_binding_changed')
        rows = [_normalized(row) for row in page['rows']]
        total = page.get('total')
        if total is not None and (type(total) is not int or not 0 <= total <= MAX_ROWS):
            raise AvitoError('statistics_limit')
        previous = run.staged_rows
        if (not isinstance(previous, list) or len(previous) > MAX_ROWS
                or len(previous) != run.next_offset
                or len({row['id'] for row in previous + rows}) != len(previous + rows)
                or run.expected_total is not None and total != run.expected_total
                or total is not None and len(previous + rows) > total
                or len(previous + rows) > MAX_ROWS):
            raise AvitoError('statistics_data_invalid')
    except avito_workspace.AvitoCooldownError as exc:
        error, retry_at = 'provider_cooldown', exc.retry_at
    except Exception as exc:
        code = str(exc)
        error = code if code in ERRORS or re.fullmatch(r'provider_http_[1-5][0-9]{2}', code) else 'statistics_unavailable'
    with transaction.atomic():
        current = ChannelConnection.objects.select_for_update().select_related('channel').get(pk=connection_id)
        metadata, active, now = dict(current.settings), _state(current), timezone.now()
        same = (active.get('enabled') is True and active.get('lease') == lease
            and active.get('run_id') == run.pk
            and _date(active.get('lease_until')) is not None and _date(active['lease_until']) > now
            and active.get('generation') == state.get('generation')
            and _allowed(current, active) and _stamp(current) == run.binding_digest)
        if not same:
            return 'failed'  # Never transfer old account data or overwrite another lease.
        locked = AvitoStatisticsRun.objects.select_for_update().get(pk=run.pk)
        if (locked.state != 'collecting' or locked.day != run.day
                or locked.binding_digest != run.binding_digest
                or locked.next_offset != run.next_offset or locked.staged_rows != run.staged_rows
                or locked.dimensions != run.dimensions):
            return 'failed'
        active.update(lease=None, lease_until=None, last_checked_at=now.isoformat(), last_error_code=error)
        if error == 'provider_cooldown':
            active['next_due_at'] = retry_at.isoformat()
            status = 'skipped'
        elif error:
            locked.state, locked.error_code, locked.finished_at = 'failed', error, now
            active.update(run_id=None, next_due_at=(now+timedelta(minutes=15)).isoformat())
            status = 'failed'
        else:
            combined = locked.staged_rows + rows
            locked.staged_rows, locked.received_rows = combined, len(combined)
            locked.expected_total = total
            locked.page_provenance = locked.page_provenance + [{
                'offset': locked.next_offset, 'rows': len(rows),
                'source_timestamp': page.get('source_timestamp', '')[:80],
                'received_at': now.isoformat(), 'date_from': locked.day.isoformat(),
                'date_to': locked.day.isoformat(), 'grouping': 'item',
                'unit_contract': 'stats_v2_confirmed_metrics_v1',
                'source_money_unit': 'kopeck', 'stored_money_unit': 'RUB',
                'contact_semantics': 'provider_counter_not_unique_clients'}]
            locked.next_offset = len(combined)
            if total is None:
                locked.state, locked.error_code, locked.finished_at = 'partial', 'statistics_total_unavailable', now
                active.update(run_id=None, last_error_code=locked.error_code,
                              next_due_at=(now+timedelta(hours=1)).isoformat())
                status = 'failed'
            elif len(combined) == total:
                locked.state, locked.pagination_complete, locked.finished_at = 'success', True, now
                observed = {row['id']: row['metrics'] for row in combined}
                previous_ids = set(AvitoDailyStatistic.objects.filter(
                    connection=current, account_id=ACCOUNT,
                    organization_id_at_collection=current.channel.organization_id,
                    day=locked.day).values_list('avito_id', flat=True))
                for identifier in sorted(set(observed) | set(locked.dimensions) | previous_ids):
                    dimensions = locked.dimensions.get(identifier, {})
                    AvitoDailyStatistic.objects.update_or_create(
                        connection=current, account_id=ACCOUNT,
                        organization_id_at_collection=current.channel.organization_id,
                        day=locked.day, avito_id=identifier,
                        defaults={'source_run': locked, 'metrics': observed.get(identifier, dict.fromkeys(COUNTERS + MONEY)),
                            'provider_row_present': identifier in observed, 'fetched_at': now,
                            **{key: dimensions.get(key, '') for key in
                               ('title', 'service_label', 'service_source', 'city_label')}})
                active.update(run_id=None, last_error_code='', last_success_at=now.isoformat(),
                              next_due_at=(now+timedelta(minutes=1)).isoformat())
                status = 'success'
            elif not rows or len(rows) != avito_workspace.PAGE_SIZE:
                locked.state, locked.error_code, locked.finished_at = 'failed', 'statistics_incomplete', now
                active.update(run_id=None, last_error_code=locked.error_code,
                              next_due_at=(now+timedelta(minutes=15)).isoformat())
                status = 'failed'
            else:
                active.update(next_due_at=(now+timedelta(minutes=1)).isoformat())
                status = 'skipped'
        locked.save()
        metadata[KEY] = active
        current.settings = metadata
        current.save(update_fields=['settings'])
    return status


def scan_due(*, deadline, limit=1, observer=None):
    output = {'success': 0, 'failed': 0, 'skipped': 0}
    if time.monotonic() + 20 > deadline:
        output['skipped'] = 1
        return output
    try:
        status = collect_next(4)
    except ChannelConnection.DoesNotExist:
        status = 'skipped'
    except Exception:
        status = 'failed'
    output[status] += 1
    if observer:
        observer(4, 'statistics_daily', status)
    return output


def ratio(metrics, numerator, denominator, scale=Decimal('1')):
    top, bottom = metrics.get(numerator), metrics.get(denominator)
    if top is None or bottom is None:
        return None
    try:
        top, bottom = Decimal(str(top)), Decimal(str(bottom))
    except (InvalidOperation, ValueError):
        return None
    if not top.is_finite() or not bottom.is_finite():
        return None
    return format((top / bottom * scale).quantize(Decimal('.01'), rounding=ROUND_HALF_UP), '.2f') if bottom > 0 else None


def metric_display(metrics):
    return [{'key': key, 'label': LABELS[key], 'value': metrics.get(key),
             'unit': '₽' if key in MONEY else ''} for key in COUNTERS + MONEY] + [
        {'key': 'ctr', 'label': 'CTR расчётный: просмотры / показы', 'value': ratio(metrics, 'views', 'impressions', Decimal('100')), 'unit': '%'},
        {'key': 'conversion', 'label': 'Контакты / просмотры', 'value': ratio(metrics, 'contacts', 'views', Decimal('100')), 'unit': '%'},
        {'key': 'cpl_cash', 'label': 'Денежные расходы / контакт', 'value': ratio(metrics, 'spending', 'contacts'), 'unit': '₽/контакт'},
    ]


def _expected_ids(run):
    """Known manifest at collection, not a reconstructed historic active inventory."""
    if run is None:
        return set()
    dimensions = run.dimensions if isinstance(run.dimensions, dict) else {}
    staged = run.staged_rows if isinstance(run.staged_rows, list) else []
    return {str(key) for key in dimensions if str(key).isdigit()} | {
        str(row['id']) for row in staged
        if isinstance(row, dict) and str(row.get('id', '')).isdigit()}


def source_periods(rows, completed_days, now=None, *, day_runs=None):
    """Provider-date sums with separate page, ad-row and calendar coverage."""
    end = collection_cutoff(now)
    windows = [
        ('Последняя дата источника', end, end),
        ('Предыдущая дата источника', end-timedelta(days=1), end-timedelta(days=1)),
        ('7 дат источника', end-timedelta(days=6), end),
        ('Предыдущие 7 дат источника', end-timedelta(days=13), end-timedelta(days=7)),
        ('30 дат источника', end-timedelta(days=29), end),
    ]
    day_runs = day_runs or {}
    output = []
    for label, start, finish in windows:
        days = [start+timedelta(days=i) for i in range((finish-start).days+1)]
        missing_days = sum(day not in completed_days for day in days)
        selected = [row for row in rows if start <= row.day <= finish
                    and row.day in day_runs and row.source_run_id == day_runs[row.day].pk]
        expected, cohort_days = set(), []
        for day in days:
            run = day_runs.get(day)
            if run is None:
                continue
            known = _expected_ids(run) | {row.avito_id for row in selected if row.day == day}
            expected.update((day, identifier) for identifier in known)
            cohort_days.append(known)
        reported = {(row.day, row.avito_id) for row in selected if row.provider_row_present}
        stored = {(row.day, row.avito_id) for row in selected}
        missing_rows = len(expected - reported)
        metrics, metric_coverage = {}, {}
        for slug in COUNTERS + MONEY:
            values = [row.metrics.get(slug) if row.provider_row_present else None for row in selected]
            metric_coverage[slug] = sum(value is not None for value in values)
            metrics[slug] = (format(sum(Decimal(value) for value in values), 'f')
                if values and not missing_days and not missing_rows
                and expected == stored and all(value is not None for value in values) else None)
        shown = metric_display(metrics)
        for item in shown:
            item['known_cells'] = metric_coverage.get(item['key'])
        output.append({'label': label, 'from': start, 'to': finish,
            'source': 'avito_stats_v2_items', 'provider_timezone': 'unknown',
            'missing_days': missing_days, 'completed_days': len(days)-missing_days,
            'total_days': len(days), 'pages_complete': not missing_days,
            'metrics': shown, 'metric_values': {item['key']: item['value'] for item in shown},
            'metric_coverage': metric_coverage, 'reported_cells': len(reported),
            'expected_cells': len(expected), 'stored_cells': len(stored),
            'missing_ad_cells': missing_rows, 'expected_cells_known': not missing_days,
            'cohort_ids': sorted({identifier for _, identifier in expected}),
            'cohort_stable': bool(cohort_days) and not missing_days
                and all(ids == cohort_days[0] for ids in cohort_days),
            'run_ids': [day_runs[day].pk for day in days if day in day_runs],
            'received_at': max((day_runs[day].finished_at for day in days
                if day in day_runs and day_runs[day].finished_at is not None), default=None),
            'provider_calendar_aligned': False})
    return output


def source_comparisons(periods):
    """Deltas compare provider date labels only; no local-calendar conversion."""
    output = []
    for title, current, previous in (
            ('Дата источника с предыдущей', periods[0], periods[1]),
            ('7 дат источника с предыдущими 7', periods[2], periods[3])):
        same_cohort = (current['cohort_stable'] and previous['cohort_stable']
            and current['cohort_ids'] == previous['cohort_ids'])
        reason = ('missing_days' if current['missing_days'] or previous['missing_days']
            else 'cohort_changed' if not same_cohort
            else 'missing_ad_rows' if current['missing_ad_cells'] or previous['missing_ad_cells']
            else '')
        metrics = []
        for item in current['metrics']:
            key = item['key']
            value, before = item['value'], previous['metric_values'].get(key)
            comparable = not reason and value is not None and before is not None
            delta = format(Decimal(value)-Decimal(before), 'f') if comparable else None
            relative = (ratio({'current': Decimal(value)-Decimal(before), 'previous': before},
                'current', 'previous', Decimal('100')) if comparable else None)
            metrics.append({**item, 'current': value, 'previous': before,
                'delta': delta, 'delta_unit': 'п.п.' if item['unit'] == '%' else item['unit'],
                'relative_change': relative, 'comparable': comparable})
        output.append({'label': title, 'current': current, 'previous': previous,
            'reason': reason, 'same_cohort': same_cohort, 'metrics': metrics,
            'provider_calendar_aligned': False})
    return output


def _source_history(connection, now=None):
    """Shared bounded DB read; callers establish the current connection scope."""
    cutoff = collection_cutoff(now)
    rows = list(AvitoDailyStatistic.objects.filter(connection=connection, account_id=ACCOUNT,
        organization_id_at_collection=connection.channel.organization_id,
        day__range=(cutoff-timedelta(days=29), cutoff),
        source_run__connection=connection, source_run__account_id=ACCOUNT,
        source_run__organization_id_at_collection=connection.channel.organization_id,
        source_run__state='success', source_run__pagination_complete=True,
        source_run__day=F('day')).select_related('source_run')
        .order_by('-day', 'avito_id')[:MAX_ROWS*BACKFILL_DAYS*2])
    complete_runs = AvitoStatisticsRun.objects.filter(
        connection=connection, account_id=ACCOUNT,
        organization_id_at_collection=connection.channel.organization_id, state='success', pagination_complete=True,
        day__range=(cutoff-timedelta(days=29), cutoff)).order_by('-pk')
    day_runs = {}
    for run in complete_runs:
        day_runs.setdefault(run.day, run)
    return cutoff, rows, day_runs


def source_period_evidence(connection, now=None):
    """Closed metadata for the protected setup receipt; never metric values."""
    with transaction.atomic():
        current = ChannelConnection.objects.select_for_update().select_related('channel').filter(pk=connection.pk).first()
        if current is None or not _scope(current) or not _allowed(current, _state(current)):
            return {'schema_version': 1, 'state': 'blocked', 'code': 'statistics_binding_changed'}
        cutoff, rows, day_runs = _source_history(current, now)
        periods = source_periods(rows, set(day_runs), now, day_runs=day_runs)
        comparisons = source_comparisons(periods)
        names = ('day', 'previous_day', 'seven_days', 'previous_seven_days', 'thirty_days')
        windows = []
        for index, period in enumerate(periods):
            comparison = comparisons[index // 2] if index < 4 else None
            windows.append({'key': names[index], 'from': period['from'].isoformat(),
                'to': period['to'].isoformat(), 'missing_dates': period['missing_days'],
                'expected_ad_cells': period['expected_cells'],
                'reported_ad_cells': period['reported_cells'],
                'pages_complete': period['pages_complete'],
                'ad_rows_complete': bool(period['expected_cells']) and period['pages_complete']
                    and not period['missing_ad_cells']
                    and period['stored_cells'] == period['expected_cells'],
                'comparison_applicable': comparison is not None,
                'same_cohort': comparison['same_cohort'] if comparison is not None else False,
                'comparison_available': bool(comparison is not None and not comparison['reason']
                    and comparison['metrics'] and all(item['comparable'] for item in comparison['metrics']))})
        return {'schema_version': 1, 'state': 'observed', 'source': 'avito_stats_v2_items',
            'cutoff': cutoff.isoformat(), 'provider_timezone': 'unknown',
            'barnaul_calendar_confirmed': False, 'completed_dates': len(day_runs),
            'expected_dates': BACKFILL_DAYS,
            'comparison_rule': 'all_requested_metrics_known', 'periods': windows}


def display(connection, actor, now=None):
    from .communication_services import organization_access, conversation_capability
    if (not _scope(connection) or organization_access(actor, connection.channel.organization) is None
            or not conversation_capability(actor, 'can_manage_channels', connection.channel.organization)):
        return None
    state = _state(connection)
    cutoff, rows, day_runs = _source_history(connection, now)
    completed_days = set(day_runs)
    latest = AvitoStatisticsRun.objects.filter(connection=connection, account_id=ACCOUNT,
        organization_id_at_collection=connection.channel.organization_id).order_by('-pk').first()
    output = {'enabled': state.get('enabled') is True and _allowed(connection, state),
        'can_configure': subscriber_allowed(actor, connection.channel.organization),
        'last_success_at': _date(state.get('last_success_at')),
        'next_due_at': _date(state.get('next_due_at')),
        'error': ERRORS.get(state.get('last_error_code'), 'Ошибка чтения статистики.') if state.get('last_error_code') else '',
        'provider_timezone': 'unknown', 'calendar': str(ZONE), 'lag_days': 2,
        'cutoff': collection_cutoff(now), 'latest_run': latest,
        'aligned_comparisons_available': False,
        'windows': [{'label': label, 'from': start, 'to': end} for label, start, end in requested_windows(now)],
        'rows': [], 'groups': [], 'changes': [], 'total_cells': len(rows),
        'provider_periods': source_periods(rows, completed_days, now, day_runs=day_runs)}
    output['provider_comparisons'] = source_comparisons(output['provider_periods'])
    output['backfill'] = {'from': cutoff-timedelta(days=BACKFILL_DAYS-1), 'to': cutoff,
        'completed_days': len(completed_days), 'remaining_days': BACKFILL_DAYS-len(completed_days),
        'total_days': BACKFILL_DAYS, 'oldest_complete_day': min(completed_days, default=None),
        'latest_complete_day': max(completed_days, default=None)}
    output['bonus_spending'] = {'value': None, 'unit': '',
        'reason': 'Единица spendingBonus официально не подтверждена; бонусы не вычисляются из разницы расходов.'}
    for row in rows[:100]:
        output['rows'].append({'day': row.day, 'avito_id': row.avito_id, 'title': row.title,
            'service': row.service_label, 'service_source': row.service_source, 'city': row.city_label,
            'city_source': row.source_run.dimensions.get(row.avito_id, {}).get('city_source', 'unknown'),
            'fetched_at': row.fetched_at, 'metrics': metric_display(row.metrics if row.provider_row_present else {}),
            'provider_row_present': row.provider_row_present, 'run': row.source_run})
    # These are provider date labels, not converted Barnaul calendar days.
    grouped = {}
    for row in rows:
        key = (row.day, row.service_label, row.service_source, row.city_label)
        grouped.setdefault(key, []).append(row)
    for (day, service, service_source, city), items in sorted(grouped.items()):
        metrics = {}
        for slug in COUNTERS + MONEY:
            values = [item.metrics.get(slug) if item.provider_row_present else None for item in items]
            metrics[slug] = (format(sum(Decimal(value) for value in values), 'f')
                if all(value is not None for value in values) else None)
        output['groups'].append({'day': day, 'service': service,
            'service_source': service_source, 'city': city, 'ads': len(items),
            'reported_ads': sum(item.provider_row_present for item in items),
            'metrics': metric_display(metrics)})
    output['groups_total'] = len(output['groups'])
    output['groups'] = output['groups'][:100]
    from .avito_feed_models import AvitoServicesFeed
    feed = AvitoServicesFeed.objects.filter(connection=connection, account_id=ACCOUNT,
        organization_id_at_import=connection.channel.organization_id).first()
    if feed:
        output['changes'] = list(feed.versions.filter(
            action__in=('edit', 'publish', 'restore', 'policy')).order_by('-created_at')[:30])
        from .avito_promotion_observations import KIND, FIELD, SOURCE, _digest
        binding_hash = _digest(_binding(connection))
        events = feed.versions.filter(action='verify', changes__0__kind=KIND,
            changes__0__field=FIELD, changes__0__source=SOURCE,
            changes__0__binding_sha256=binding_hash,
            changes__0__scope__connection_id=connection.pk,
            changes__0__scope__account_id=ACCOUNT,
            changes__0__scope__organization_id=connection.channel.organization_id
        ).order_by('-pk').values('pk', 'created_at', 'changes')[:30]
        output['promotion_observations'] = []
        for event in events:
            changes = event['changes']
            if not isinstance(changes, list) or len(changes) != 1:
                continue
            change = changes[0]
            after = change.get('after')
            if (not isinstance(after, dict) or not isinstance(after.get('services'), list)
                    or change.get('purchase_confirmed') is not False):
                continue
            output['promotion_observations'].append({
                'event_id': event['pk'], 'observed_at': _date(change.get('observed_at')),
                'avito_id': change.get('avito_id'), 'baseline': change.get('baseline') is True,
                'services': after['services'], 'source': SOURCE})
    from .avito_crm_coverage import coverage
    output['crm'] = coverage(connection, actor, now)
    return output


def setup_once():
    """Called only by the existing protected exact-main workplace setup."""
    from django.db import connection as database
    required = {AvitoStatisticsRun._meta.db_table, AvitoDailyStatistic._meta.db_table}
    if not required.issubset(set(database.introspection.table_names())):
        return {'state': 'blocked', 'code': 'statistics_tables_unavailable',
                'connection_id': 4, 'account_id': ACCOUNT}
    perform_first_read = False
    with transaction.atomic():
        connection = ChannelConnection.objects.select_for_update().select_related('channel').filter(pk=4).first()
        if connection is None or not _scope(connection):
            return {'state': 'blocked', 'code': 'statistics_binding_changed',
                    'connection_id': 4, 'account_id': ACCOUNT}
        from .avito_services_workplace_setup import _actor
        actor = _actor(connection)
        metadata = dict(connection.settings) if isinstance(connection.settings, dict) else {}
        # Presence includes an explicit opt-out. Every later deploy preserves it.
        if KEY not in metadata:
            configure(connection, actor, 'enable')
            connection.refresh_from_db()
            metadata = dict(connection.settings)
            metadata['avito_daily_statistics_setup_once'] = {
                'configured_at': timezone.now().isoformat(),
                'first_collection_attempted': True, 'mechanism': 'protected_deploy_first_read'}
            connection.settings = metadata
            connection.save(update_fields=['settings'])
            perform_first_read = True
    result = 'not_requested'
    if perform_first_read:
        try:
            result = collect_next(4)
        except Exception:
            result = 'failed'
    connection.refresh_from_db()
    state = _state(connection)
    if not _allowed(connection, state):
        return {'state': 'blocked', 'code': 'statistics_binding_changed',
                'connection_id': 4, 'account_id': ACCOUNT}
    latest = AvitoStatisticsRun.objects.filter(connection=connection, account_id=ACCOUNT,
        organization_id_at_collection=connection.channel.organization_id).order_by('-pk').first()
    from .avito_runtime import checks
    background = next((item for item in checks(connection) if item['check'] == 'statistics_daily'), {})
    code = state.get('last_error_code', '')
    code = code if isinstance(code, str) and (code in ERRORS or code == '' or
        re.fullmatch(r'provider_http_[1-5][0-9]{2}', code)) else 'statistics_unavailable'
    return {'state': 'configured' if perform_first_read else 'preserved',
        'connection_id': 4, 'account_id': ACCOUNT, 'enabled': state.get('enabled') is True,
        'first_read_result': result, 'first_read_mechanism': 'protected_deploy_first_read',
        'source_day': latest.day.isoformat() if latest else None,
        'received_rows': latest.received_rows if latest else None,
        'pagination_complete': latest.pagination_complete if latest else False,
        'provider_timezone': 'unknown', 'barnaul_calendar_confirmed': False,
        'error_code': code,
        'source_period_evidence': source_period_evidence(connection),
        'natural_background_verified': background.get('last_worker_verified') is True
            and background.get('last_worker_outcome') == 'success',
        'natural_background_checked_at': background.get('last_worker_checked_at')}
