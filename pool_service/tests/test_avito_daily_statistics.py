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
                {'Address': 'г. Барнаул, г. Новосибирск'}, {'Address': 'Новосибирск'},
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
