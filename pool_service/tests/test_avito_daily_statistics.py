"""Daily-history tests use safe response substitutes, never a real provider write."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone as dt_timezone
from unittest.mock import patch

from django.contrib.auth.models import User
from django.core.exceptions import PermissionDenied
from django.test import TestCase, SimpleTestCase
from django.urls import reverse

from pool_service import avito_daily_statistics as daily, avito_workspace
from pool_service.avito_statistics_models import AvitoStatisticsRun, AvitoDailyStatistic
from pool_service.avito_feed_models import AvitoServicesFeed
from pool_service.avito_services_feed import parse_export
from pool_service.communication_models import ChannelConnection, CommunicationChannel, AvitoCredential
from pool_service.models import Organization, OrganizationAccess
from pool_service.tests.test_avito_services_feed import export_bytes


class DailyCalendarTests(SimpleTestCase):
    def test_local_midnight_excludes_today_and_does_not_disguise_two_day_lag_as_yesterday(self):
        now = datetime(2026, 10, 24, 17, 5, tzinfo=dt_timezone.utc)
        self.assertEqual(daily.collection_cutoff(now).isoformat(), '2026-10-23')
        windows = daily.requested_windows(now)
        self.assertEqual(windows[0][1].isoformat(), '2026-10-24')
        self.assertEqual(windows[2][1].isoformat(), '2026-10-18')
        self.assertEqual(windows[3][2].isoformat(), '2026-10-17')
        self.assertEqual(windows[4][1].isoformat(), '2026-09-25')

    def test_ratios_distinguish_unknown_zero_and_positive_denominator(self):
        self.assertIsNone(daily.ratio({'views': '0', 'contacts': '1'}, 'contacts', 'views'))
        self.assertIsNone(daily.ratio({'views': '1', 'contacts': None}, 'contacts', 'views'))
        self.assertEqual(daily.ratio({'views': '4', 'contacts': '1'}, 'contacts', 'views', daily.Decimal('100')), '25.00')
        self.assertEqual(daily.ratio({'spending': '125.99', 'contacts': '2'}, 'spending', 'contacts'), '63.00')

    def test_currency_not_double_converted_and_ambiguous_units_not_stored(self):
        source = avito_workspace.statistics_data({'result': {'groupings': [{'id': 11, 'metrics': [
            {'slug': 'spending', 'value': 12599}, {'slug': 'views', 'value': 0},
            {'slug': 'averageContactCost', 'value': 999}, {'slug': 'spendingBonus', 'value': 300},
        ]}], 'dataTotalCount': 1}}, start='2026-10-23', end='2026-10-23', grouping='item', offset=0)
        item = daily._normalized(source['rows'][0])
        self.assertEqual(item['metrics']['spending'], '125.99')
        self.assertEqual(item['metrics']['views'], '0')
        self.assertIsNone(item['metrics']['contacts'])
        self.assertNotIn('averageContactCost', item['metrics'])
        self.assertNotIn('spendingBonus', item['metrics'])


class DailyCityTests(SimpleTestCase):
    def test_explicit_city_is_preferred_and_exact_address_token_retains_provenance(self):
        self.assertEqual(daily._city({'City': 'Барнаул', 'Address': 'г. Бийск'}),
                         ('Барнаул', 'published_city_field'))
        for text, expected in (('Алтайский край, г. Барнаул, ул. Ленина, 1', 'Барнаул'),
                ('Республика Алтай, город Горно-Алтайск, проспект 1', 'Горно-Алтайск'),
                ('Алтайский край, Бийск', 'Бийск')):
            with self.subTest(text=text):
                self.assertEqual(daily._city({'Address': text}),
                                 (expected, 'published_address_exact_token'))

    def test_streets_titles_ambiguous_and_other_cities_never_become_a_known_city(self):
        for row in ({'Title': 'Строительство Барнаул'}, {'Address': 'ул. Барнаула, 1'},
                {'Address': 'улица Барнаул, 1'}, {'Address': 'Барнаул, Бийск'},
                {'Address': 'г. Барнаул, г. Новосибирск'}, {'Address': 'г. Барнаул, Г. Новосибирск'},
                {'Address': 'Барнаул, Город Новосибирск'}, {'Address': 'Новосибирск'},
                {'Address': 'Барнаульский район'}, {'Address': 123}):
            with self.subTest(row=row):
                self.assertEqual(daily._city(row), ('', 'unknown'))


class DailyHistoryTests(TestCase):
    def setUp(self):
        self.now = datetime(2026, 10, 25, 7, tzinfo=dt_timezone.utc)
        clock = patch.object(daily.timezone, 'now', return_value=self.now)
        clock.start()
        self.addCleanup(clock.stop)
        self.org = Organization.objects.create(name='Daily source test')
        self.owner = User.objects.create_user('daily-owner')
        self.manager = User.objects.create_user('daily-manager')
        OrganizationAccess.objects.create(organization=self.org, user=self.owner, role='owner')
        OrganizationAccess.objects.create(organization=self.org, user=self.manager, role='manager')
        channel = CommunicationChannel.objects.create(organization=self.org, kind='avito', name='Services')
        self.connection = ChannelConnection.objects.create(pk=4, channel=channel,
            external_id=daily.ACCOUNT, name='Services')
        self.goods = ChannelConnection.objects.create(pk=1, channel=channel, external_id='goods',
            settings={'preserve': 'goods sentinel'})
        self.credential = AvitoCredential.objects.create(connection=self.connection,
            client_id_encrypted='synthetic-encrypted-id', client_secret_encrypted='synthetic-encrypted-secret')
        sheets = parse_export(export_bytes(('11', '12')))
        sheets[0]['rows'][0]['City'] = 'Барнаул'
        sheets[0]['rows'][1]['Address'] = 'An address is not a verified city'
        self.feed = AvitoServicesFeed.objects.create(connection=self.connection, account_id=daily.ACCOUNT,
            organization_id_at_import=self.org.pk, draft=deepcopy(sheets), published=sheets,
            revision=18, published_revision=18, enabled=True)
        for target, value in (('access_token', 'opaque'), ('authorized_account_id', daily.ACCOUNT)):
            p = patch.object(daily, target, return_value=value)
            p.start()
            self.addCleanup(p.stop)
        daily.configure(self.connection, self.owner, 'enable')

    def page(self, ids=(11, 12), *, total=2, metrics=None):
        return avito_workspace.statistics_data({'result': {
            'groupings': [{'id': identifier, 'metrics': [
                {'slug': key, 'value': value} for key, value in
                ({'impressions': 100, 'views': 10, 'contacts': 1, 'spending': 12599} if metrics is None else metrics).items()
            ]} for identifier in ids], 'dataTotalCount': total, 'timestamp': 'provider-clock'}},
            start='2026-10-23', end='2026-10-23', grouping='item', offset=0)

    def retry(self):
        self.connection.refresh_from_db()
        daily.configure(self.connection, self.owner, 'retry')

    def test_one_page_saves_daily_facts_source_provenance_and_exact_dimensions_without_goods_change(self):
        with patch.object(avito_workspace, 'fetch_statistics', return_value=self.page()) as fetch:
            self.assertEqual(daily.collect_next(4), 'success')
        fetch.assert_called_once_with('opaque', daily.ACCOUNT, start='2026-10-23',
                                     end='2026-10-23', grouping='item', offset=0)
        run = AvitoStatisticsRun.objects.get()
        self.assertTrue(run.pagination_complete)
        self.assertEqual(run.provider_timezone, 'unknown')
        self.assertEqual(run.calendar_status, 'unknown')
        self.assertEqual(run.requested_calendar, 'Asia/Barnaul')
        self.assertEqual(run.page_provenance[0]['source_timestamp'], 'provider-clock')
        self.assertEqual(AvitoDailyStatistic.objects.get(avito_id='11').city_label, 'Барнаул')
        self.assertEqual(AvitoDailyStatistic.objects.get(avito_id='12').city_label, '')
        self.assertEqual(AvitoDailyStatistic.objects.get(avito_id='11').metrics['spending'], '125.99')
        self.goods.refresh_from_db()
        self.assertEqual(self.goods.settings, {'preserve': 'goods sentinel'})

    def test_missing_provider_ad_and_metric_are_none_while_reported_zero_stays_zero(self):
        with patch.object(avito_workspace, 'fetch_statistics',
                return_value=self.page(ids=(11,), total=1, metrics={'views': 0})):
            self.assertEqual(daily.collect_next(), 'success')
        seen, missing = AvitoDailyStatistic.objects.get(avito_id='11'), AvitoDailyStatistic.objects.get(avito_id='12')
        self.assertEqual(seen.metrics['views'], '0')
        self.assertIsNone(seen.metrics['contacts'])
        self.assertFalse(missing.provider_row_present)
        self.assertTrue(all(value is None for value in missing.metrics.values()))

    def test_no_duplicate_rows_when_a_day_is_safely_recollected(self):
        with patch.object(avito_workspace, 'fetch_statistics', return_value=self.page()):
            daily.collect_next()
            first = AvitoStatisticsRun.objects.get()
            first.state = 'failed'
            first.save(update_fields=['state'])
            self.retry()
            daily.collect_next()
        self.assertEqual(AvitoStatisticsRun.objects.count(), 2)
        self.assertEqual(AvitoDailyStatistic.objects.count(), 2)
        self.assertTrue(AvitoDailyStatistic.objects.exclude(source_run=first).count() == 2)

    def test_pages_resume_one_request_at_a_time_with_exact_offsets(self):
        first = self.page(tuple(range(11, 61)), total=51)
        second = self.page((61,), total=51)
        with patch.object(avito_workspace, 'fetch_statistics', side_effect=[first, second]) as fetch:
            self.assertEqual(daily.collect_next(), 'skipped')
            self.assertEqual(AvitoDailyStatistic.objects.count(), 0)
            self.retry()
            self.assertEqual(daily.collect_next(), 'success')
        self.assertEqual([call.kwargs['offset'] for call in fetch.call_args_list], [0, 50])
        self.assertEqual(AvitoDailyStatistic.objects.count(), 51)
        self.assertEqual(AvitoStatisticsRun.objects.get().received_rows, 51)

    def test_missing_total_never_marks_complete(self):
        with patch.object(avito_workspace, 'fetch_statistics', return_value=self.page(total=None)):
            self.assertEqual(daily.collect_next(), 'failed')
        self.assertFalse(AvitoStatisticsRun.objects.get().pagination_complete)
        self.assertEqual(AvitoStatisticsRun.objects.get().state, 'partial')
        self.assertEqual(AvitoDailyStatistic.objects.count(), 0)

    def test_parallel_lease_rejects_second_worker_without_provider_io(self):
        reserved = daily._reserve(4)
        self.assertIsNotNone(reserved)
        with patch.object(avito_workspace, 'fetch_statistics') as fetch:
            self.assertEqual(daily.collect_next(), 'skipped')
        fetch.assert_not_called()

    def test_scope_binding_or_rights_change_during_request_discards_received_data(self):
        def changed(*args, **kwargs):
            AvitoCredential.objects.filter(pk=self.credential.pk).update(client_secret_encrypted='rotated')
            return self.page()
        with patch.object(avito_workspace, 'fetch_statistics', side_effect=changed):
            self.assertEqual(daily.collect_next(), 'failed')
        self.assertEqual(AvitoDailyStatistic.objects.count(), 0)

    def test_wrong_account_response_never_requests_statistics(self):
        with patch.object(daily, 'authorized_account_id', return_value='foreign'), \
                patch.object(avito_workspace, 'fetch_statistics') as fetch:
            self.assertEqual(daily.collect_next(), 'failed')
        fetch.assert_not_called()
        self.assertEqual(AvitoDailyStatistic.objects.count(), 0)

    def test_cooldown_is_deferred_without_retry_or_failed_run(self):
        with patch.object(avito_workspace, 'fetch_statistics', side_effect=avito_workspace.AvitoCooldownError(
                self.now + timedelta(minutes=1))) as fetch:
            self.assertEqual(daily.collect_next(), 'skipped')
        self.assertEqual(fetch.call_count, 1)
        self.assertEqual(AvitoStatisticsRun.objects.get().state, 'collecting')
        self.assertEqual(AvitoDailyStatistic.objects.count(), 0)

    def test_other_account_has_no_controls_data_or_collection(self):
        with self.assertRaises(ValueError):
            daily.collect_next(1)
        with self.assertRaises(PermissionDenied):
            daily.configure(self.goods, self.owner, 'enable')
        self.assertIsNone(daily.display(self.goods, self.owner))
        with self.assertRaises(PermissionDenied):
            daily.configure(self.connection, self.manager, 'enable')

    def test_display_is_db_only_marks_calendar_unaligned_and_never_claims_unique_customers(self):
        with patch.object(avito_workspace, 'fetch_statistics', return_value=self.page()):
            daily.collect_next()
        with patch.object(avito_workspace, 'fetch_statistics') as fetch:
            state = daily.display(self.connection, self.owner, self.now)
        fetch.assert_not_called()
        self.assertFalse(state['aligned_comparisons_available'])
        self.assertEqual(state['windows'][0]['from'].isoformat(), '2026-10-24')
        self.assertEqual(state['provider_periods'][0]['from'].isoformat(), '2026-10-23')
        self.assertEqual(state['provider_periods'][2]['missing_days'], 6)

    def test_controls_require_owner_and_csrf(self):
        url = reverse('avito_daily_statistics_control', args=[4])
        self.client.force_login(self.manager)
        self.assertEqual(self.client.post(url, {'action': 'disable'}).status_code, 403)
        self.client.force_login(self.owner)
        self.assertEqual(self.client.get(url).status_code, 405)
        from django.test import Client
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.owner)
        self.assertEqual(client.post(url, {'action': 'disable'}).status_code, 403)


    def test_disable_or_cursor_change_during_provider_read_never_publishes_the_page(self):
        def disable(*args, **kwargs):
            daily.configure(self.connection, self.owner, 'disable')
            return self.page()
        with patch.object(avito_workspace, 'fetch_statistics', side_effect=disable):
            self.assertEqual(daily.collect_next(), 'failed')
        self.assertEqual(AvitoDailyStatistic.objects.count(), 0)
        daily.configure(self.connection, self.owner, 'enable')
        def change_cursor(*args, **kwargs):
            AvitoStatisticsRun.objects.filter(state='collecting').update(next_offset=50)
            return self.page()
        with patch.object(avito_workspace, 'fetch_statistics', side_effect=change_cursor):
            self.assertEqual(daily.collect_next(), 'failed')
        self.assertEqual(AvitoDailyStatistic.objects.count(), 0)


    def test_cross_page_duplicate_ids_reject_the_whole_run(self):
        pages = [self.page(tuple(range(11, 61)), total=51), self.page((11,), total=51)]
        with patch.object(avito_workspace, 'fetch_statistics', side_effect=pages):
            self.assertEqual(daily.collect_next(), 'skipped')
            self.retry()
            self.assertEqual(daily.collect_next(), 'failed')
        run = AvitoStatisticsRun.objects.get()
        self.assertFalse(run.pagination_complete)
        self.assertEqual(run.error_code, 'statistics_data_invalid')
        self.assertEqual(AvitoDailyStatistic.objects.count(), 0)

    def test_changing_total_rejects_the_whole_run(self):
        pages = [self.page(tuple(range(11, 61)), total=51), self.page((61,), total=52)]
        with patch.object(avito_workspace, 'fetch_statistics', side_effect=pages):
            self.assertEqual(daily.collect_next(), 'skipped')
            self.retry()
            self.assertEqual(daily.collect_next(), 'failed')
        self.assertFalse(AvitoStatisticsRun.objects.get().pagination_complete)
        self.assertEqual(AvitoDailyStatistic.objects.count(), 0)

    def test_partial_replacement_does_not_overwrite_prior_complete_daily_facts(self):
        with patch.object(avito_workspace, 'fetch_statistics', return_value=self.page()):
            self.assertEqual(daily.collect_next(), 'success')
        original = list(AvitoDailyStatistic.objects.values('avito_id', 'metrics', 'source_run_id'))
        run = AvitoStatisticsRun.objects.get()
        run.state = 'failed'
        run.save(update_fields=['state'])
        self.retry()
        with patch.object(avito_workspace, 'fetch_statistics', return_value=self.page(total=None)):
            self.assertEqual(daily.collect_next(), 'failed')
        self.assertEqual(list(AvitoDailyStatistic.objects.values('avito_id', 'metrics', 'source_run_id')), original)


    def clear_daily_setup(self):
        self.connection.refresh_from_db()
        self.connection.settings.pop(daily.KEY, None)
        self.connection.save(update_fields=['settings'])

    def test_protected_setup_enables_and_reads_one_page_once_without_claiming_cron(self):
        self.clear_daily_setup()
        with patch('pool_service.avito_services_workplace_setup._actor', return_value=self.owner), \
                patch.object(avito_workspace, 'fetch_statistics', return_value=self.page()) as fetch:
            first = daily.setup_once()
            second = daily.setup_once()
        self.assertEqual(fetch.call_count, 1)
        self.assertEqual(first['state'], 'configured')
        self.assertEqual(second['state'], 'preserved')
        self.assertEqual(first['first_read_result'], 'success')
        self.assertFalse(first['natural_background_verified'])
        self.assertFalse(first['barnaul_calendar_confirmed'])
        self.assertNotIn('spending', first)

    def test_setup_preserves_explicit_optout_on_every_deploy(self):
        daily.configure(self.connection, self.owner, 'disable')
        with patch('pool_service.avito_services_workplace_setup._actor', return_value=self.owner), \
                patch.object(avito_workspace, 'fetch_statistics') as fetch:
            first = daily.setup_once()
            second = daily.setup_once()
        fetch.assert_not_called()
        self.assertFalse(first['enabled'])
        self.assertFalse(second['enabled'])

    def test_setup_read_failure_is_safe_and_not_retried_by_later_deploy(self):
        from pool_service.communication_avito import AvitoError
        self.clear_daily_setup()
        with patch('pool_service.avito_services_workplace_setup._actor', return_value=self.owner), \
                patch.object(avito_workspace, 'fetch_statistics', side_effect=AvitoError('provider_http_403')) as fetch:
            first = daily.setup_once()
            repeated = daily.setup_once()
        self.assertEqual(fetch.call_count, 1)
        self.assertEqual(first['error_code'], 'provider_http_403')
        self.assertEqual(repeated['state'], 'preserved')
        self.assertFalse(first['pagination_complete'])
        self.assertEqual(AvitoDailyStatistic.objects.count(), 0)


    def test_address_city_dimensions_are_current_scoped_source_metadata(self):
        self.feed.published[0]['rows'][0].pop('City', None)
        self.feed.published[0]['rows'][0]['Address'] = 'Алтайский край, Барнаул, ул. Ленина, 1'
        self.feed.save(update_fields=['published'])
        self.goods.settings = {'preserve': 'goods sentinel', 'Address': 'г. Бийск'}
        self.goods.save(update_fields=['settings'])
        with patch.object(avito_workspace, 'fetch_statistics', return_value=self.page()):
            self.assertEqual(daily.collect_next(), 'success')
        run = AvitoStatisticsRun.objects.get()
        self.assertEqual(run.dimensions['11']['city_label'], 'Барнаул')
        self.assertEqual(run.dimensions['11']['city_source'], 'published_address_exact_token')
        self.assertEqual(run.dimensions['12']['city_label'], '')
        self.assertEqual(run.dimensions['11']['published_revision'], 18)

    def test_display_separates_backfill_pages_known_ads_calendar_and_bonus_units_without_api(self):
        with patch.object(avito_workspace, 'fetch_statistics', return_value=self.page((11,), total=1)):
            self.assertEqual(daily.collect_next(), 'success')
        with patch.object(avito_workspace, 'fetch_statistics') as fetch:
            state = daily.display(self.connection, self.owner, self.now)
        fetch.assert_not_called()
        self.assertEqual(state['backfill']['completed_days'], 1)
        self.assertEqual(state['backfill']['remaining_days'], 29)
        period = state['provider_periods'][0]
        self.assertTrue(period['pages_complete'])
        self.assertEqual((period['reported_cells'], period['expected_cells']), (1, 2))
        self.assertEqual(period['missing_ad_cells'], 1)
        self.assertFalse(period['provider_calendar_aligned'])
        self.assertIsNone(period['metric_values']['views'])
        self.assertIsNone(state['bonus_spending']['value'])
        self.goods.refresh_from_db()
        self.assertEqual(self.goods.settings, {'preserve': 'goods sentinel'})
        from django.template.loader import render_to_string
        html = render_to_string('pool_service/avito/_daily_statistics.html',
            {'daily': state, 'connection': self.connection})
        self.assertIn('Списано бонусов: нет подтверждённой единицы.', html)
        self.assertIn('Сравнение доступных дат источника', html)

    def test_foreign_run_scope_cannot_supply_daily_metrics_or_completed_days(self):
        with patch.object(avito_workspace, 'fetch_statistics', return_value=self.page()):
            self.assertEqual(daily.collect_next(), 'success')
        run = AvitoStatisticsRun.objects.get()
        for field, foreign in (('account_id', 'another-account'),
                ('organization_id_at_collection', self.org.pk+1000),
                ('connection_id', self.goods.pk)):
            original = getattr(run, field)
            with self.subTest(field=field):
                AvitoStatisticsRun.objects.filter(pk=run.pk).update(**{field: foreign})
                state = daily.display(self.connection, self.owner, self.now)
                self.assertEqual(state['rows'], [])
                self.assertEqual(state['backfill']['completed_days'], 0)
                self.assertIsNone(state['provider_periods'][0]['metric_values']['views'])
            AvitoStatisticsRun.objects.filter(pk=run.pk).update(**{field: original})

    def test_fact_day_must_match_its_completed_source_run(self):
        with patch.object(avito_workspace, 'fetch_statistics', return_value=self.page()):
            self.assertEqual(daily.collect_next(), 'success')
        run = AvitoStatisticsRun.objects.get()
        run.day -= timedelta(days=1)
        run.save(update_fields=['day'])
        state = daily.display(self.connection, self.owner, self.now)
        self.assertEqual(state['rows'], [])
        self.assertIsNone(state['provider_periods'][0]['metric_values']['views'])


    def test_setup_period_evidence_is_closed_db_only_and_not_a_claim_of_complete_ads(self):
        with patch.object(avito_workspace, 'fetch_statistics', return_value=self.page((11,), total=1)):
            self.assertEqual(daily.collect_next(), 'success')
        run = AvitoStatisticsRun.objects.get()
        run.dimensions['11'].update(title='PRIVATE source title', client_secret='PRIVATE')
        run.page_provenance.append({'raw_url': 'https://private.invalid/PRIVATE'})
        run.save(update_fields=['dimensions', 'page_provenance'])
        self.connection.refresh_from_db()
        settings_before = deepcopy(self.connection.settings)
        with patch('pool_service.avito_services_workplace_setup._actor', return_value=self.owner), \
                patch.object(daily, 'collect_next') as collect, \
                patch.object(avito_workspace, 'fetch_statistics') as fetch, \
                patch.object(daily, 'display', side_effect=AssertionError('full display is not receipt metadata')), \
                patch('pool_service.avito_crm_coverage.coverage', side_effect=AssertionError('CRM is not receipt metadata')):
            first, second = daily.setup_once(), daily.setup_once()
        collect.assert_not_called()
        fetch.assert_not_called()
        self.connection.refresh_from_db()
        self.assertEqual(self.connection.settings, settings_before)
        self.assertEqual(first['first_read_result'], 'not_requested')
        evidence = first['source_period_evidence']
        self.assertEqual(evidence, second['source_period_evidence'])
        self.assertEqual(set(evidence), {'schema_version', 'state', 'source', 'cutoff',
            'provider_timezone', 'barnaul_calendar_confirmed', 'completed_dates',
            'expected_dates', 'comparison_rule', 'periods'})
        self.assertEqual((evidence['completed_dates'], evidence['expected_dates']), (1, 30))
        self.assertEqual(evidence['cutoff'], '2026-10-23')
        self.assertEqual(evidence['provider_timezone'], 'unknown')
        self.assertFalse(evidence['barnaul_calendar_confirmed'])
        self.assertEqual(len(evidence['periods']), 5)
        expected_keys = {'key', 'from', 'to', 'missing_dates', 'expected_ad_cells',
            'reported_ad_cells', 'pages_complete', 'ad_rows_complete', 'comparison_applicable',
            'same_cohort', 'comparison_available'}
        for period in evidence['periods']:
            self.assertEqual(set(period), expected_keys)
        period = evidence['periods'][0]
        self.assertTrue(period['pages_complete'])
        self.assertEqual((period['reported_ad_cells'], period['expected_ad_cells']), (1, 2))
        self.assertFalse(period['ad_rows_complete'])
        self.assertFalse(period['comparison_available'])
        import json
        serialized = json.dumps(evidence)
        for private in ('PRIVATE', 'private.invalid', 'metrics', 'spending', 'contacts', 'title'):
            self.assertNotIn(private, serialized.replace('all_requested_metrics_known', ''))
        self.assertFalse(evidence['periods'][4]['comparison_applicable'])

    def test_period_evidence_requires_current_binding_and_never_reads_other_scope(self):
        with patch.object(daily, '_source_history') as history:
            self.assertEqual(daily.source_period_evidence(self.goods)['code'], 'statistics_binding_changed')
            self.connection.refresh_from_db()
            self.connection.settings[daily.KEY]['account_id'] = 'foreign-account'
            self.connection.save(update_fields=['settings'])
            self.assertEqual(daily.source_period_evidence(self.connection)['code'], 'statistics_binding_changed')
        history.assert_not_called()

    def test_period_evidence_requires_all_requested_values_for_its_comparison_boolean(self):
        raw = dict.fromkeys(daily.COUNTERS, 1)
        raw.update(dict.fromkeys(daily.MONEY, 1234567))
        with patch.object(avito_workspace, 'fetch_statistics', return_value=self.page(metrics=raw)):
            self.assertEqual(daily.collect_next(), 'success')
            self.retry()
            self.assertEqual(daily.collect_next(), 'success')
        evidence = daily.source_period_evidence(self.connection, self.now)
        self.assertTrue(evidence['periods'][0]['same_cohort'])
        self.assertTrue(evidence['periods'][0]['comparison_available'])
        self.assertTrue(evidence['periods'][0]['ad_rows_complete'])
        self.assertNotIn('12345.67', str(evidence))
        row = AvitoDailyStatistic.objects.filter(day=daily.collection_cutoff(self.now)).first()
        row.metrics['contacts'] = None
        row.save(update_fields=['metrics'])
        after = daily.source_period_evidence(self.connection, self.now)['periods'][0]
        self.assertTrue(after['pages_complete'])
        self.assertTrue(after['ad_rows_complete'])
        self.assertFalse(after['comparison_available'])


class DailySourceComparisonTests(SimpleTestCase):
    now = datetime(2026, 10, 25, 7, tzinfo=dt_timezone.utc)

    def facts(self, days=14):
        from types import SimpleNamespace
        cutoff, rows, runs = daily.collection_cutoff(self.now), [], {}
        for offset in range(days):
            day = cutoff-timedelta(days=offset)
            run = SimpleNamespace(pk=offset+1, dimensions={'11': {}},
                staged_rows=[{'id': '11'}], finished_at=self.now)
            runs[day] = run
            rows.append(SimpleNamespace(day=day, avito_id='11', source_run_id=run.pk,
                provider_row_present=True, metrics=dict.fromkeys(daily.COUNTERS+daily.MONEY, '0')))
        return rows, runs

    def periods(self, rows, runs):
        return daily.source_periods(rows, set(runs), self.now, day_runs=runs)

    def test_source_bounds_and_comparable_deltas_do_not_claim_barnaul_days(self):
        rows, runs = self.facts()
        rows[0].metrics.update(impressions='100', views='20', contacts='2', spending='200')
        rows[1].metrics.update(impressions='100', views='10', contacts='1', spending='100')
        periods = self.periods(rows, runs)
        self.assertEqual((periods[0]['from'].isoformat(), periods[1]['to'].isoformat()),
                         ('2026-10-23', '2026-10-22'))
        self.assertEqual((periods[2]['from'].isoformat(), periods[2]['to'].isoformat()),
                         ('2026-10-17', '2026-10-23'))
        self.assertEqual((periods[3]['from'].isoformat(), periods[3]['to'].isoformat()),
                         ('2026-10-10', '2026-10-16'))
        self.assertEqual(periods[4]['from'].isoformat(), '2026-09-24')
        self.assertEqual(periods[4]['missing_days'], 16)
        result = daily.source_comparisons(periods)[0]
        metrics = {item['key']: item for item in result['metrics']}
        self.assertEqual(metrics['views']['delta'], '10')
        self.assertEqual(metrics['views']['relative_change'], '100.00')
        self.assertEqual(metrics['ctr']['delta'], '10.00')
        self.assertEqual(metrics['ctr']['delta_unit'], 'п.п.')
        self.assertEqual(metrics['cpl_cash']['delta'], '0.00')
        self.assertFalse(result['provider_calendar_aligned'])
        self.assertEqual(periods[0]['source'], 'avito_stats_v2_items')

    def test_unknown_metric_and_zero_baseline_never_become_a_percentage(self):
        rows, runs = self.facts()
        rows[0].metrics.update(views='10', contacts=None)
        rows[1].metrics.update(views='0', contacts='0')
        result = daily.source_comparisons(self.periods(rows, runs))[0]
        metrics = {item['key']: item for item in result['metrics']}
        self.assertTrue(metrics['views']['comparable'])
        self.assertEqual(metrics['views']['delta'], '10')
        self.assertIsNone(metrics['views']['relative_change'])
        self.assertIsNone(metrics['contacts']['delta'])
        self.assertIsNone(metrics['conversion']['delta'])
        self.assertIsNone(metrics['cpl_cash']['relative_change'])

    def test_pages_complete_is_not_ad_coverage_even_if_missing_fact_still_contains_values(self):
        rows, runs = self.facts()
        rows[0].provider_row_present = False
        rows[0].metrics['views'] = '99'
        period = self.periods(rows, runs)[0]
        self.assertTrue(period['pages_complete'])
        self.assertEqual(period['missing_ad_cells'], 1)
        self.assertEqual(period['metric_coverage']['views'], 0)
        self.assertIsNone(period['metric_values']['views'])
        deleted = self.periods(rows[1:], runs)[0]
        self.assertEqual(deleted['expected_cells'], 1)
        self.assertEqual(deleted['stored_cells'], 0)
        self.assertIsNone(deleted['metric_values']['views'])

    def test_changed_ad_cohort_and_missing_date_block_deltas_without_filling_zeros(self):
        rows, runs = self.facts()
        rows[0].avito_id = '12'
        runs[rows[0].day].dimensions = {'12': {}}
        runs[rows[0].day].staged_rows = [{'id': '12'}]
        current = daily.source_comparisons(self.periods(rows, runs))[0]
        self.assertEqual(current['reason'], 'cohort_changed')
        self.assertTrue(all(item['delta'] is None for item in current['metrics']))
        runs.pop(rows[1].day)
        current = daily.source_comparisons(self.periods(rows, runs))[0]
        self.assertEqual(current['reason'], 'missing_days')
        self.assertTrue(all(item['delta'] is None for item in current['metrics']))

    def test_one_missing_metric_does_not_hide_other_known_metrics(self):
        rows, runs = self.facts()
        rows[0].metrics['contacts'] = None
        period = self.periods(rows, runs)[0]
        self.assertEqual(period['metric_values']['views'], '0')
        self.assertEqual(period['metric_coverage']['views'], 1)
        self.assertEqual(period['metric_coverage']['contacts'], 0)
        self.assertIsNone(period['metric_values']['contacts'])
        self.assertIsNone(period['metric_values']['conversion'])
