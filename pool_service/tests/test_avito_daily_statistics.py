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
            # Isolate daily-fact replacement from the fair missing-date selector.
            with patch.object(daily, '_choose_day', return_value=first.day):
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
        with patch.object(daily, '_choose_day', return_value=run.day), \
                patch.object(avito_workspace, 'fetch_statistics', return_value=self.page(total=None)):
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
            'expected_dates', 'comparison_rule', 'periods', 'decision_report', 'gap_diagnostics'})
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
        # Gap diagnostics may disclose allowlisted metric CODES, never values or raw fields.
        for private in ('PRIVATE', 'private.invalid', '125.99', 'raw_url', 'provider-clock', 'title'):
            self.assertNotIn(private, serialized)
        without_gaps = {key: value for key, value in evidence.items() if key != 'gap_diagnostics'}
        for private in ('metrics', 'spending', 'contacts'):
            self.assertNotIn(private, json.dumps(without_gaps).replace('all_requested_metrics_known', ''))
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



    def priority_manifest(self):
        from pool_service.avito_statistics_gaps import TARGET
        self.feed.published[0]['rows'][0]['AvitoId'] = TARGET
        self.feed.save(update_fields=['published'])
        return TARGET

    def gap_evidence(self, now=None):
        return daily.source_period_evidence(self.connection, now or self.now)['gap_diagnostics']

    def test_gap_evidence_is_closed_scoped_metadata_and_rolls_at_local_midnight(self):
        from pool_service.avito_statistics_gaps import TARGET, STATES
        self.priority_manifest()
        metrics = dict.fromkeys(daily.COUNTERS+daily.MONEY, 0)
        with patch.object(avito_workspace, 'fetch_statistics',
                return_value=self.page((int(TARGET),), total=1, metrics=metrics)):
            self.assertEqual(daily.collect_next(), 'success')
        with patch.object(avito_workspace, 'fetch_statistics') as fetch:
            evidence = self.gap_evidence()
        fetch.assert_not_called()
        self.assertEqual(set(evidence), {'schema_version', 'state', 'connection_id', 'account_id',
            'avito_id', 'source', 'provider_timezone', 'from', 'to', 'request_scope',
            'unit_contract', 'manifest_is_request_filter', 'completed_dates', 'expected_dates',
            'states', 'last_attempt', 'dates'})
        self.assertEqual(set(evidence['last_attempt']), {'state', 'run_id', 'source_date',
            'offset', 'phase', 'exception_class', 'error_code', 'outcome', 'started_at', 'finished_at'})
        self.assertEqual(evidence['request_scope'], 'all_account_items_no_item_filter')
        self.assertFalse(evidence['manifest_is_request_filter'])
        self.assertEqual((evidence['from'], evidence['to']), ('2026-09-24', '2026-10-23'))
        self.assertEqual(len(evidence['dates']), 30)
        self.assertEqual(set(evidence['states']), set(STATES))
        item = evidence['dates'][0]
        self.assertEqual(set(item), {'date', 'date_from', 'date_to', 'grouping',
            'latest_complete_run_id', 'completed_at', 'pages_complete',
            'request_provenance_consistent', 'pages', 'expected_total', 'received_rows',
            'expected_manifest_member', 'provider_row_present', 'stored_row_present',
            'stored_row_matches_run', 'stored_row_consistent', 'known_metric_count',
            'missing_metric_codes', 'zero_metric_codes', 'state', 'latest_attempt_run_id',
            'latest_attempt_state', 'latest_attempt_error_code', 'latest_attempt_finished_at'})
        self.assertEqual(item['state'], 'known')
        self.assertEqual(set(item['zero_metric_codes']), set(daily.COUNTERS+daily.MONEY))
        self.assertEqual(item['missing_metric_codes'], [])
        for item in evidence['dates']:
            self.assertLessEqual(set(item['missing_metric_codes']+item['zero_metric_codes']),
                                 set(daily.COUNTERS+daily.MONEY))
            self.assertTrue(all(set(page) == {'offset', 'rows'} for page in item['pages']))
        midnight = datetime(2026, 10, 25, 17, 1, tzinfo=dt_timezone.utc)
        rolled = self.gap_evidence(midnight)
        self.assertEqual((rolled['from'], rolled['to']), ('2026-09-25', '2026-10-24'))
        self.assertEqual(rolled['dates'][0]['state'], 'no_completed_request')
        self.assertEqual(rolled['dates'][1]['state'], 'known')

    def test_gap_pages_complete_distinguishes_omitted_row_missing_fact_and_mismatch(self):
        target = self.priority_manifest()
        with patch.object(avito_workspace, 'fetch_statistics', return_value=self.page((12,), total=1)):
            self.assertEqual(daily.collect_next(), 'success')
        item = self.gap_evidence()['dates'][0]
        self.assertTrue(item['pages_complete'])
        self.assertTrue(item['expected_manifest_member'])
        self.assertFalse(item['provider_row_present'])
        self.assertTrue(item['stored_row_consistent'])
        self.assertEqual(item['state'], 'provider_omitted_row')
        self.assertEqual(item['known_metric_count'], 0)
        fact = AvitoDailyStatistic.objects.get(avito_id=target)
        fact.metrics['views'] = '0'
        fact.save(update_fields=['metrics'])
        self.assertEqual(self.gap_evidence()['dates'][0]['state'], 'local_fact_mismatch')
        fact.delete()
        self.assertEqual(self.gap_evidence()['dates'][0]['state'], 'local_fact_missing')

    def test_gap_known_zero_does_not_fill_missing_metric_and_wrong_run_is_rejected(self):
        target = self.priority_manifest()
        with patch.object(avito_workspace, 'fetch_statistics',
                return_value=self.page((int(target),), total=1, metrics={'views': 0})):
            daily.collect_next()
        item = self.gap_evidence()['dates'][0]
        self.assertEqual(item['state'], 'provider_metrics_missing')
        self.assertEqual(item['zero_metric_codes'], ['views'])
        self.assertIn('contacts', item['missing_metric_codes'])
        self.assertEqual(item['known_metric_count'], 1)
        original = AvitoStatisticsRun.objects.get()
        stale = AvitoStatisticsRun.objects.create(connection=self.connection, account_id=daily.ACCOUNT,
            organization_id_at_collection=self.org.pk, binding_digest=original.binding_digest,
            day=original.day-timedelta(days=1), state='success', pagination_complete=True)
        AvitoDailyStatistic.objects.filter(avito_id=target).update(source_run=stale)
        self.assertEqual(self.gap_evidence()['dates'][0]['state'], 'local_fact_mismatch')

    def test_gap_uses_latest_completed_request_not_newer_failed_attempt_or_unsafe_provenance(self):
        target = self.priority_manifest()
        with patch.object(avito_workspace, 'fetch_statistics',
                return_value=self.page((int(target),), total=1)):
            daily.collect_next()
        complete = AvitoStatisticsRun.objects.get()
        failed = AvitoStatisticsRun.objects.create(connection=self.connection, account_id=daily.ACCOUNT,
            organization_id_at_collection=self.org.pk, binding_digest=complete.binding_digest,
            day=complete.day, state='failed', error_code='statistics_unavailable')
        self.connection.refresh_from_db()
        state = self.connection.settings[daily.KEY]
        state.update(last_error_code='statistics_unavailable', last_checked_at=self.now.isoformat())
        state.pop('last_attempt', None)
        self.connection.save(update_fields=['settings'])
        item = self.gap_evidence()['dates'][0]
        self.assertEqual(item['latest_complete_run_id'], complete.pk)
        self.assertEqual(item['latest_attempt_run_id'], failed.pk)
        self.assertEqual(item['state'], 'provider_metrics_missing')
        self.assertEqual(self.gap_evidence()['last_attempt']['phase'], 'unknown')
        complete.page_provenance[0].update(raw_body='PRIVATE', raw_url='https://PRIVATE.invalid')
        complete.save(update_fields=['page_provenance'])
        self.assertNotIn('PRIVATE', str(self.gap_evidence()))
        complete.page_provenance[0]['offset'] = 50
        complete.save(update_fields=['page_provenance'])
        self.assertEqual(self.gap_evidence()['dates'][0]['state'], 'request_provenance_inconsistent')

    def test_gap_binding_and_organization_isolation_do_not_read_history(self):
        from pool_service import avito_statistics_gaps as gaps
        with patch.object(gaps.AvitoStatisticsRun.objects, 'filter') as runs:
            self.assertEqual(gaps.evidence(self.goods)['state'], 'blocked')
            self.connection.refresh_from_db()
            self.connection.settings[daily.KEY]['organization_id'] = self.org.pk+1
            self.connection.save(update_fields=['settings'])
            self.assertEqual(daily.source_period_evidence(self.connection)['state'], 'blocked')
            self.assertEqual(gaps.evidence(self.connection)['state'], 'blocked')
        runs.assert_not_called()
        self.assertIsNone(daily.display(self.connection, User.objects.create_user('gap-outsider')))

    def test_future_failure_telemetry_is_safe_preserves_completed_facts_and_retry_timing(self):
        target = self.priority_manifest()
        with patch.object(avito_workspace, 'fetch_statistics',
                return_value=self.page((int(target),), total=1)):
            daily.collect_next()
        complete = AvitoStatisticsRun.objects.get()
        before = list(AvitoDailyStatistic.objects.filter(source_run=complete).values())
        class PrivateFailure(Exception):
            def __str__(self):
                raise AssertionError('Never stringify unexpected provider failures')
        for exc, expected, classification in (
                (daily.AvitoRetryableError('provider_unavailable'), 'provider_unavailable', 'AvitoRetryableError'),
                (daily.AvitoError('provider_http_403'), 'provider_http_403', 'AvitoError'),
                (PrivateFailure('PRIVATE raw response'), 'statistics_unavailable', 'unexpected_exception')):
            self.retry()
            with patch.object(avito_workspace, 'fetch_statistics', side_effect=exc) as fetch:
                self.assertEqual(daily.collect_next(), 'failed')
            fetch.assert_called_once()
            self.connection.refresh_from_db()
            state = self.connection.settings[daily.KEY]
            self.assertEqual(state['last_error_code'], expected)
            self.assertEqual(state['next_due_at'], (self.now+timedelta(minutes=15)).isoformat())
            attempt = state['last_attempt']
            self.assertEqual((attempt['phase'], attempt['exception_class']), ('fetch_statistics', classification))
            self.assertEqual(attempt['offset'], 0)
            self.assertNotIn('PRIVATE', str(state))
            self.assertEqual(self.gap_evidence()['last_attempt']['error_code'], expected)
        self.assertEqual(list(AvitoDailyStatistic.objects.filter(source_run=complete).values()), before)
        self.assertEqual(self.gap_evidence()['dates'][0]['latest_complete_run_id'], complete.pk)

    def test_gap_rejects_other_binding_runs_and_malformed_attempt_metadata(self):
        target = self.priority_manifest()
        with patch.object(avito_workspace, 'fetch_statistics',
                return_value=self.page((int(target),), total=1)):
            daily.collect_next()
        complete = AvitoStatisticsRun.objects.get()
        foreign = AvitoStatisticsRun.objects.create(connection=self.connection, account_id=daily.ACCOUNT,
            organization_id_at_collection=self.org.pk, binding_digest='different-generation',
            day=complete.day, state='success', pagination_complete=True,
            dimensions={'PRIVATE': {}}, staged_rows=[{'id': 'PRIVATE'}])
        self.connection.refresh_from_db()
        self.connection.settings[daily.KEY]['last_attempt']['phase'] = ['PRIVATE']
        self.connection.save(update_fields=['settings'])
        receipt = self.gap_evidence()
        self.assertEqual(receipt['dates'][0]['latest_complete_run_id'], complete.pk)
        self.assertNotEqual(receipt['dates'][0]['latest_attempt_run_id'], foreign.pk)
        self.assertEqual(receipt['last_attempt']['state'], 'unconfirmed')
        self.assertNotIn('PRIVATE', str(receipt))

    def test_lost_lease_does_not_transfer_failure_telemetry(self):
        self.connection.refresh_from_db()
        baseline = deepcopy(self.connection.settings)
        def lose_lease(*args, **kwargs):
            current = ChannelConnection.objects.get(pk=4)
            current.settings[daily.KEY].update(lease='other-worker',
                last_attempt={'sentinel': 'newer-worker-evidence'})
            current.save(update_fields=['settings'])
            raise daily.AvitoError('provider_http_403')
        with patch.object(avito_workspace, 'fetch_statistics', side_effect=lose_lease):
            self.assertEqual(daily.collect_next(), 'failed')
        self.connection.refresh_from_db()
        self.assertEqual(self.connection.settings[daily.KEY]['last_attempt'],
                         {'sentinel': 'newer-worker-evidence'})
        self.assertEqual(self.connection.settings[daily.KEY]['last_error_code'],
                         baseline[daily.KEY]['last_error_code'])
        self.assertEqual(AvitoStatisticsRun.objects.get().state, 'collecting')
        self.assertFalse(AvitoDailyStatistic.objects.exists())



    def seed_completed_dates_except(self, missing, now=None):
        cutoff = daily.collection_cutoff(now or self.now)
        digest = daily._stamp(self.connection)
        AvitoStatisticsRun.objects.bulk_create([
            AvitoStatisticsRun(connection=self.connection, account_id=daily.ACCOUNT,
                organization_id_at_collection=self.org.pk, day=cutoff-timedelta(days=offset),
                binding_digest=digest, state='success', pagination_complete=True)
            for offset in range(daily.BACKFILL_DAYS)
            if cutoff-timedelta(days=offset) not in missing])

    def failed_date_attempt(self, day, **overrides):
        values = {'connection': self.connection, 'account_id': daily.ACCOUNT,
            'organization_id_at_collection': self.org.pk, 'day': day,
            'binding_digest': daily._stamp(self.connection), 'state': 'failed',
            'error_code': 'statistics_unavailable'}
        values.update(overrides)
        return AvitoStatisticsRun.objects.create(**values)

    def test_failed_september_26_cannot_starve_untried_september_25_on_native_ticks(self):
        now = datetime(2026, 10, 25, 18, 30, tzinfo=dt_timezone.utc)
        september_26 = daily.collection_cutoff(now)-timedelta(days=28)
        september_25 = daily.collection_cutoff(now)-timedelta(days=29)
        self.assertEqual((september_26.isoformat(), september_25.isoformat()),
                         ('2026-09-26', '2026-09-25'))
        self.seed_completed_dates_except({september_26, september_25}, now)
        self.failed_date_attempt(september_26)
        self.assertEqual(daily._choose_day(self.connection, now), september_25)
        with patch.object(daily.timezone, 'now', return_value=now), \
                patch.object(avito_workspace, 'fetch_statistics', return_value=self.page()) as fetch:
            self.assertEqual(daily.collect_next(), 'success')
            self.assertEqual(daily.collect_next(), 'skipped')
        fetch.assert_called_once_with('opaque', daily.ACCOUNT, start='2026-09-25',
            end='2026-09-25', grouping='item', offset=0)
        self.assertTrue(AvitoStatisticsRun.objects.filter(day=september_25,
            state='success', pagination_complete=True).exists())
        self.assertFalse(AvitoStatisticsRun.objects.filter(day=september_26, state='success').exists())
        later = now+timedelta(minutes=1)
        with patch.object(daily.timezone, 'now', return_value=later), \
                patch.object(avito_workspace, 'fetch_statistics',
                    side_effect=daily.AvitoError('provider_http_400')) as fetch:
            self.assertEqual(daily.collect_next(), 'failed')
            self.assertEqual(daily.collect_next(), 'skipped')
        fetch.assert_called_once_with('opaque', daily.ACCOUNT, start='2026-09-26',
            end='2026-09-26', grouping='item', offset=0)
        self.connection.refresh_from_db()
        self.assertEqual(self.connection.settings[daily.KEY]['next_due_at'],
                         (later+timedelta(minutes=15)).isoformat())

    def test_failed_dates_retry_fairly_by_last_attempt_sequence_with_clock_ties(self):
        cutoff = daily.collection_cutoff(self.now)
        days = [cutoff-timedelta(days=offset) for offset in range(3)]
        self.seed_completed_dates_except(set(days))
        # pk order is a durable attempt sequence, including equal wall clocks.
        self.failed_date_attempt(days[2])
        self.failed_date_attempt(days[0])
        self.failed_date_attempt(days[1])
        self.assertEqual(daily._choose_day(self.connection, self.now), days[2])
        self.failed_date_attempt(days[2])
        self.assertEqual(daily._choose_day(self.connection, self.now), days[0])
        self.failed_date_attempt(days[0])
        self.assertEqual(daily._choose_day(self.connection, self.now), days[1])

    def test_new_missing_date_has_priority_over_retries_but_active_page_cursor_resumes_first(self):
        first = self.page(tuple(range(11, 61)), total=51)
        with patch.object(avito_workspace, 'fetch_statistics', return_value=first) as fetch:
            self.assertEqual(daily.collect_next(), 'skipped')
        fetch.assert_called_once()
        run = AvitoStatisticsRun.objects.get()
        self.assertEqual(run.next_offset, 50)
        # Plenty of dates remain untried, but a valid in-progress page owns the cursor.
        with patch.object(daily.timezone, 'now', return_value=self.now+timedelta(minutes=1)), \
                patch.object(daily, '_choose_day', side_effect=AssertionError('resume existing run')), \
                patch.object(avito_workspace, 'fetch_statistics',
                    return_value=self.page((61,), total=51)) as fetch:
            self.assertEqual(daily.collect_next(), 'success')
        fetch.assert_called_once_with('opaque', daily.ACCOUNT, start=run.day.isoformat(),
            end=run.day.isoformat(), grouping='item', offset=50)
        self.assertEqual(AvitoStatisticsRun.objects.count(), 1)
        run.refresh_from_db()
        self.assertTrue(run.pagination_complete)
        self.assertEqual(run.received_rows, 51)

    def test_foreign_attempt_history_cannot_change_date_priority(self):
        cutoff = daily.collection_cutoff(self.now)
        self.seed_completed_dates_except({cutoff, cutoff-timedelta(days=1)})
        for overrides in ({'binding_digest': 'previous-generation'},
                {'account_id': 'foreign-account'}, {'organization_id_at_collection': self.org.pk+1},
                {'connection': self.goods}, {'source': 'unrelated-source'}):
            with self.subTest(overrides=overrides):
                self.failed_date_attempt(cutoff, **overrides)
                self.assertEqual(daily._choose_day(self.connection, self.now), cutoff)
        self.failed_date_attempt(cutoff)
        self.assertEqual(daily._choose_day(self.connection, self.now), cutoff-timedelta(days=1))
        self.goods.refresh_from_db()
        self.assertEqual(self.goods.settings, {'preserve': 'goods sentinel'})

    def test_all_complete_dates_keep_next_morning_schedule_and_new_cutoff_becomes_due(self):
        self.seed_completed_dates_except(set())
        self.assertIsNone(daily._choose_day(self.connection, self.now))
        with patch.object(avito_workspace, 'fetch_statistics') as fetch:
            self.assertEqual(daily.collect_next(), 'skipped')
        fetch.assert_not_called()
        self.connection.refresh_from_db()
        self.assertEqual(daily._date(self.connection.settings[daily.KEY]['next_due_at']),
            datetime(2026, 10, 26, 8, tzinfo=daily.ZONE))
        tomorrow = self.now+timedelta(days=1)
        self.assertEqual(daily._choose_day(self.connection, tomorrow), daily.collection_cutoff(tomorrow))



    def test_statistics_structural_reason_is_persisted_without_payload_or_accepting_empty_groups(self):
        def rejected(*args, **kwargs):
            return avito_workspace.statistics_data(
                {'result': {'groupings': None, 'dataTotalCount': 0}, 'PRIVATE': 'PRIVATE'},
                start='2026-10-23', end='2026-10-23', grouping='item', offset=0)
        with patch.object(avito_workspace, 'fetch_statistics', side_effect=rejected) as fetch:
            self.assertEqual(daily.collect_next(), 'failed')
        fetch.assert_called_once()
        self.connection.refresh_from_db()
        attempt = self.connection.settings[daily.KEY]['last_attempt']
        self.assertEqual(attempt['error_code'], 'provider_data_invalid')
        self.assertEqual(attempt['statistics_reason'], 'grouping_missing_with_explicit_zero_total')
        self.assertEqual(attempt['exception_class'], 'AvitoError')
        self.assertEqual(attempt['phase'], 'fetch_statistics')
        self.assertFalse(AvitoStatisticsRun.objects.get().pagination_complete)
        self.assertFalse(AvitoDailyStatistic.objects.exists())
        evidence = self.gap_evidence()
        self.assertEqual(evidence['last_attempt']['statistics_reason'],
                         'grouping_missing_with_explicit_zero_total')
        self.assertNotIn('PRIVATE', str(self.connection.settings))
        self.assertNotIn('PRIVATE', str(evidence))
        self.goods.refresh_from_db()
        self.assertEqual(self.goods.settings, {'preserve': 'goods sentinel'})

    def test_statistics_reason_requires_exact_code_exception_type_and_allowlisted_string(self):
        for error, reason in ((daily.AvitoError('provider_data_invalid'), 'PRIVATE'),
                (daily.AvitoError('provider_data_invalid'), ['result_type']),
                (daily.AvitoError('provider_http_400'), 'result_type'),
                (RuntimeError('provider_data_invalid'), 'result_type')):
            error.statistics_reason = reason
            self.retry()
            with patch.object(avito_workspace, 'fetch_statistics', side_effect=error):
                self.assertEqual(daily.collect_next(), 'failed')
            self.connection.refresh_from_db()
            self.assertNotIn('statistics_reason', self.connection.settings[daily.KEY]['last_attempt'])
            self.assertNotIn('PRIVATE', str(self.connection.settings))
        # Previously persisted attempts without the new field remain valid.
        self.retry()
        with patch.object(avito_workspace, 'fetch_statistics',
                side_effect=daily.AvitoError('provider_data_invalid')):
            daily.collect_next()
        self.assertEqual(self.gap_evidence()['last_attempt']['state'], 'observed')
        self.assertNotIn('statistics_reason', self.gap_evidence()['last_attempt'])

    def test_saved_unknown_or_misbound_statistics_reason_is_rejected_without_echoing_it(self):
        with patch.object(avito_workspace, 'fetch_statistics',
                side_effect=daily.AvitoError('provider_data_invalid')):
            daily.collect_next()
        self.connection.refresh_from_db()
        for reason, code in (('PRIVATE', 'provider_data_invalid'),
                (['result_type'], 'provider_data_invalid'), ('result_type', 'provider_http_403')):
            self.connection.settings[daily.KEY]['last_attempt'].update(
                statistics_reason=reason, error_code=code)
            self.connection.save(update_fields=['settings'])
            attempt = self.gap_evidence()['last_attempt']
            self.assertEqual(attempt['state'], 'unconfirmed')
            self.assertNotIn('statistics_reason', attempt)
            self.assertNotIn('PRIVATE', str(attempt))

    def test_lost_lease_cannot_persist_even_an_allowlisted_statistics_reason(self):
        def rejected_after_lease_change(*args, **kwargs):
            current = ChannelConnection.objects.get(pk=4)
            current.settings[daily.KEY].update(lease='other-worker',
                last_attempt={'sentinel': 'newer-worker-evidence'})
            current.save(update_fields=['settings'])
            return avito_workspace.statistics_data({'result': None},
                start='2026-10-23', end='2026-10-23', grouping='item', offset=0)
        with patch.object(avito_workspace, 'fetch_statistics', side_effect=rejected_after_lease_change):
            self.assertEqual(daily.collect_next(), 'failed')
        self.connection.refresh_from_db()
        self.assertEqual(self.connection.settings[daily.KEY]['last_attempt'],
                         {'sentinel': 'newer-worker-evidence'})
        self.assertEqual(AvitoStatisticsRun.objects.get().state, 'collecting')
        self.assertFalse(AvitoDailyStatistic.objects.exists())


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
