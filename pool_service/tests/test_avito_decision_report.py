"""Safe synthetic decision-report facts; no provider calls or real company values."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone as dt_timezone
from types import SimpleNamespace
from unittest.mock import patch

from django.contrib.auth.models import User
from django.core.exceptions import PermissionDenied
from django.template.loader import render_to_string
from django.test import SimpleTestCase, TestCase

from pool_service import avito_daily_statistics as daily
from pool_service import avito_decision_report as report
from pool_service.communication_models import ChannelConnection, CommunicationChannel
from pool_service.models import Organization, OrganizationAccess

NOW = datetime(2026, 10, 25, 11, tzinfo=dt_timezone.utc)


def facts(days=14, ids=('11', '12')):
    cutoff, rows, runs = daily.collection_cutoff(NOW), [], {}
    for offset in range(days):
        day = cutoff-timedelta(days=offset)
        run = SimpleNamespace(pk=offset+1, dimensions={identifier: {} for identifier in ids},
            staged_rows=[{'id': identifier} for identifier in ids], finished_at=NOW)
        runs[day] = run
        for identifier in ids:
            metrics = dict.fromkeys(daily.COUNTERS+daily.MONEY, '0')
            metrics.update(impressions='100', views='20', contacts='2', spending='100.00')
            rows.append(SimpleNamespace(day=day, avito_id=identifier, source_run_id=run.pk,
                provider_row_present=True, metrics=metrics, title='Synthetic service',
                service_label='Synthetic service type', city_label='Test city'))
    return rows, runs


class AdDecisionReportTests(SimpleTestCase):
    def test_ad_periods_have_exact_source_boundaries_coverage_and_rub_ratios(self):
        rows, runs = facts()
        result = report.build(rows, runs, NOW)
        ad = result['ads'][0]
        self.assertEqual(ad['avito_id'], '11')
        self.assertEqual([(period['from'].isoformat(), period['to'].isoformat()) for period in ad['periods']],
            [('2026-10-23', '2026-10-23'), ('2026-10-22', '2026-10-22'),
             ('2026-10-17', '2026-10-23'), ('2026-10-10', '2026-10-16'),
             ('2026-09-24', '2026-10-23')])
        current = ad['current']
        self.assertEqual((current['reported_cells'], current['expected_cells']), (7, 7))
        self.assertTrue(current['pages_complete'])
        self.assertEqual(current['received_at'], NOW)
        self.assertEqual(current['metric_values']['spending'], '700.00')
        self.assertEqual(current['metric_values']['ctr'], '20.00')
        self.assertEqual(current['metric_values']['conversion'], '10.00')
        self.assertEqual(current['metric_values']['cpl_cash'], '50.00')
        self.assertFalse(result['barnaul_calendar_confirmed'])
        self.assertEqual(result['completed_dates'], 14)

    def test_current_window_freshness_does_not_inherit_a_recent_old_backfill(self):
        rows, runs = facts()
        earlier = NOW-timedelta(days=2)
        cutoff = daily.collection_cutoff(NOW)
        for day, run in runs.items():
            run.finished_at = earlier
        # The collector has just backfilled an older date outside the selected seven.
        runs[cutoff-timedelta(days=10)].finished_at = NOW
        result = report.build(rows, runs, NOW)
        self.assertEqual(result['account_current']['received_at'], earlier)
        self.assertEqual(result['received_at'], earlier)
        self.assertTrue(all(ad['current']['received_at'] == earlier for ad in result['ads']))
        self.assertEqual(result['periods'][4]['received_at'], NOW)

    def test_one_unknown_ad_does_not_hide_a_covered_ad_or_create_zero(self):
        rows, runs = facts()
        for row in rows:
            if row.avito_id == '12':
                row.provider_row_present = False
                row.metrics['views'] = '999'
        result = report.build(rows, runs, NOW)
        ads = {ad['avito_id']: ad for ad in result['ads']}
        self.assertIsNone(result['account_current']['metric_values']['views'])
        self.assertEqual(ads['11']['current']['metric_values']['views'], '140')
        self.assertEqual(ads['11']['comparisons'][1]['reason'], '')
        self.assertIsNone(ads['12']['current']['metric_values']['views'])
        self.assertTrue(ads['12']['current']['pages_complete'])
        self.assertEqual(ads['12']['current']['reported_cells'], 0)
        self.assertFalse(ads['12']['comparisons'][1]['metrics'][0]['comparable'])

    def test_missing_metric_blocks_only_its_own_ad_values_and_derived_ratios(self):
        rows, runs = facts()
        rows[0].metrics['contacts'] = None
        current = report.build(rows, runs, NOW)['ads'][0]['current']
        self.assertEqual(current['metric_values']['views'], '140')
        self.assertEqual(current['metric_coverage']['contacts'], 6)
        for key in ('contacts', 'conversion', 'cpl_cash'):
            self.assertIsNone(current['metric_values'][key])

    def test_zero_denominator_and_invalid_saved_counter_are_not_a_zero_rate(self):
        rows, runs = facts(ids=('11',))
        for row in rows:
            row.metrics.update(impressions='0', views='0', contacts='0', spending='0.00')
        current = report.build(rows, runs, NOW)['ads'][0]['current']
        self.assertEqual(current['metric_values']['views'], '0')
        for key in ('ctr', 'conversion', 'cpl_cash'):
            self.assertIsNone(current['metric_values'][key])
        rows[0].metrics['views'] = '-1'
        self.assertIsNone(report.build(rows, runs, NOW)['ads'][0]['current']['metric_values']['views'])
        rows[0].metrics['views'] = True
        self.assertIsNone(report.build(rows, runs, NOW)['ads'][0]['current']['metric_values']['views'])

    def test_missing_source_date_stale_fact_or_duplicate_never_becomes_complete(self):
        rows, runs = facts(ids=('11',))
        runs.pop(daily.collection_cutoff(NOW)-timedelta(days=1))
        current = report.build(rows, runs, NOW)['ads'][0]['current']
        self.assertFalse(current['pages_complete'])
        self.assertIsNone(current['metric_values']['views'])
        rows, runs = facts(ids=('11',))
        rows[0].source_run_id = 999
        current = report.build(rows, runs, NOW)['ads'][0]['current']
        self.assertEqual(current['reported_cells'], 6)
        self.assertIsNone(current['metric_values']['views'])
        rows, runs = facts(ids=('11',))
        rows.append(deepcopy(rows[0]))
        self.assertEqual(report.build(rows, runs, NOW)['ads'][0]['current']['reported_cells'], 6)

    def test_unknown_manifest_membership_does_not_claim_historic_activity(self):
        rows, runs = facts(ids=('11',))
        run = runs[daily.collection_cutoff(NOW)]
        run.dimensions, run.staged_rows = {}, []
        current = report.build(rows, runs, NOW)['ads'][0]['current']
        self.assertEqual(current['reported_cells'], 7)
        self.assertEqual(current['known_manifest_days'], 6)
        self.assertIsNone(current['metric_values']['views'])

    def test_small_counts_do_not_produce_an_efficiency_or_stable_trend_action(self):
        rows, runs = facts(ids=('11',))
        for row in rows:
            row.metrics.update(views='1', contacts='0')
        result = report.build(rows, runs, NOW)
        self.assertTrue(result['ads'][0]['low_volume'])
        self.assertEqual(result['actions'], [])

    def test_actions_are_bounded_unique_and_grounded_in_the_actual_ids(self):
        rows, runs = facts(ids=('11', '12', '13'))
        for row in rows:
            if row.avito_id == '13':
                row.provider_row_present = False
            elif row.avito_id == '11':
                row.metrics.update(views='200', contacts='20', spending='100')
            elif row.avito_id == '12':
                row.metrics.update(views='100', contacts='10',
                    spending='200' if row.day >= daily.collection_cutoff(NOW)-timedelta(days=6) else '100')
        result = report.build(rows, runs, NOW)
        self.assertEqual([action['code'] for action in result['actions']],
            ['review_contact_quality', 'inspect_missing_statistics', 'review_descriptive_change'])
        self.assertEqual({action['avito_id'] for action in result['actions']}, {'11', '12', '13'})
        self.assertTrue(all(action['causality_confirmed'] is False for action in result['actions']))
        self.assertTrue(all(action['review_on'].isoformat() == '2026-10-26' for action in result['actions']))
        for row in rows:
            if row.avito_id == '12':
                row.metrics['contacts'] = '1'
        self.assertNotIn('review_descriptive_change', [action['code'] for action in report.build(rows, runs, NOW)['actions']])

    def test_no_history_has_no_invented_ids_or_actions_and_rendering_is_escaped(self):
        result = report.build([], {}, NOW)
        self.assertEqual(result['ads'], [])
        self.assertEqual(result['actions'], [])
        rows, runs = facts()
        rows[0].title = '<script>UNSAFE</script>'
        content = render_to_string('pool_service/avito/_decision_report.html',
            {'report': report.build(rows, runs, NOW)})
        self.assertNotIn('<script>UNSAFE</script>', content)
        self.assertIn('&lt;script&gt;UNSAFE&lt;/script&gt;', content)
        self.assertIn('Управленческий отчёт', content)
        self.assertIn('Списанные бонусы остаются неизвестными', content)

    @patch('pool_service.avito_workspace.fetch_statistics', side_effect=AssertionError('No API in a report'))
    def test_closed_evidence_has_counts_and_action_ids_without_metric_values_or_text(self, fetch):
        rows, runs = facts()
        rows[0].title = 'PRIVATE'
        rows[0].metrics['spending'] = '1234567.89'
        evidence = report.evidence(report.build(rows, runs, NOW))
        self.assertEqual(evidence['ad_count'], 2)
        self.assertEqual(evidence['provider_timezone'], 'unknown')
        self.assertNotIn('PRIVATE', str(evidence))
        self.assertNotIn('1234567.89', str(evidence))
        self.assertNotIn('metrics', str(evidence))
        self.assertTrue(all(set(action) == {'avito_id', 'code', 'basis'} for action in evidence['actions']))
        fetch.assert_not_called()


class DecisionEvidencePermissionsTests(TestCase):
    def setUp(self):
        self.org = Organization.objects.create(name='Decision evidence organization')
        self.foreign_org = Organization.objects.create(name='Foreign decision organization')
        self.owner = User.objects.create_user('decision-owner')
        self.manager = User.objects.create_user('decision-manager')
        self.foreign = User.objects.create_user('decision-foreign-owner')
        OrganizationAccess.objects.create(organization=self.org, user=self.owner, role='owner')
        OrganizationAccess.objects.create(organization=self.org, user=self.manager, role='manager')
        OrganizationAccess.objects.create(organization=self.foreign_org, user=self.foreign, role='owner')
        channel = CommunicationChannel.objects.create(organization=self.org, kind='avito', name='Services')
        self.connection = ChannelConnection.objects.create(pk=4, channel=channel,
            external_id=daily.ACCOUNT, name='Services')
        self.goods = ChannelConnection.objects.create(pk=1, channel=channel, external_id='goods')
        self.rows, self.runs = facts()

    def test_denied_actor_other_connection_and_invalid_binding_do_not_read_history(self):
        with patch.object(daily, '_source_history') as history, patch.object(daily, '_allowed', return_value=True):
            for connection, actor in ((self.connection, self.manager),
                    (self.connection, self.foreign), (self.goods, self.owner), (self.connection, None)):
                with self.assertRaises(PermissionDenied):
                    report.decision_evidence(connection, actor, ['11'], NOW)
        history.assert_not_called()
        with patch.object(daily, '_source_history') as history, patch.object(daily, '_allowed', return_value=False):
            with self.assertRaises(PermissionDenied):
                report.decision_evidence(self.connection, self.owner, ['11'], NOW)
        history.assert_not_called()

    @patch('pool_service.avito_workspace.fetch_statistics', side_effect=AssertionError('No API in evidence'))
    def test_basis_tracks_saved_provenance_not_request_time_and_never_contains_values(self, fetch):
        with patch.object(daily, '_allowed', return_value=True), \
                patch.object(daily, '_source_history', return_value=(daily.collection_cutoff(NOW), self.rows, self.runs)), \
                patch.object(daily, '_dimensions', return_value={'11': {'published_revision': 18}}):
            first = report.decision_evidence(self.connection, self.owner, ['11', '99'], NOW)
            second = report.decision_evidence(self.connection, self.owner, ['11'], NOW+timedelta(minutes=1))
        ad = first['ads']['11']
        self.assertTrue(ad['current_published'])
        self.assertTrue(ad['ad_rows_complete'])
        self.assertTrue(ad['metrics_known']['views'])
        self.assertEqual(ad['basis_digest'], second['ads']['11']['basis_digest'])
        self.assertFalse(first['ads']['99']['available'])
        self.assertFalse(first['ads']['99']['current_published'])
        self.assertTrue(all(value is False for value in first['ads']['99']['metrics_known'].values()))
        for private in ('Synthetic service', '100.00', '700.00'):
            self.assertNotIn(private, str(first))
        fetch.assert_not_called()
        for bad in ([], ['bad'], [True], ['0'], ['1']*21):
            with self.assertRaises(ValueError):
                report.decision_evidence(self.connection, self.owner, bad, NOW)



class ComparableSubsetTests(SimpleTestCase):
    def subset(self, rows, runs, pair=1):
        result = report.build(rows, runs, NOW)
        selected = result['comparable_subsets'][pair]
        return result, selected, {item['key']: item for item in selected['metrics']}

    def test_omitted_ad_leaves_full_account_unknown_but_known_subset_comparable_per_metric(self):
        rows, runs = facts()
        for row in rows:
            row.metrics['commission'] = None
            if row.avito_id == '12':
                row.provider_row_present = False
        result, pair, metrics = self.subset(rows, runs)
        self.assertIsNone(result['account_current']['metric_values']['views'])
        self.assertEqual(metrics['views']['included_ids'], ['11'])
        self.assertEqual(metrics['views']['excluded_ids'], ['12'])
        self.assertEqual((metrics['views']['current'], metrics['views']['previous'],
                          metrics['views']['delta']), ('140', '140', '0'))
        self.assertTrue(metrics['views']['available'])
        self.assertTrue(metrics['contacts']['available'])
        self.assertTrue(metrics['spending']['available'])
        self.assertEqual(metrics['commission']['included_ids'], [])
        self.assertIsNone(metrics['commission']['current'])
        self.assertEqual((pair['current_from'].isoformat(), pair['previous_from'].isoformat()),
                         ('2026-10-17', '2026-10-10'))
        self.assertEqual((metrics['views']['current_cells'], metrics['views']['previous_cells']), (7, 7))
        self.assertFalse(pair['barnaul_calendar_confirmed'])

    def test_metric_inputs_define_their_own_same_cohort_not_a_shared_contact_or_commission_gate(self):
        rows, runs = facts()
        for row in rows:
            row.metrics['commission'] = None
            if row.avito_id == '12':
                row.metrics['spending'] = '900.00'
        rows[1].metrics['contacts'] = None
        _, _, metrics = self.subset(rows, runs)
        self.assertEqual(metrics['views']['included_ids'], ['11', '12'])
        self.assertEqual(metrics['spending']['included_ids'], ['11', '12'])
        self.assertEqual(metrics['ctr']['included_ids'], ['11', '12'])
        self.assertEqual(metrics['contacts']['included_ids'], ['11'])
        self.assertEqual(metrics['conversion']['included_ids'], ['11'])
        self.assertEqual(metrics['cpl_cash']['included_ids'], ['11'])
        self.assertEqual(metrics['cpl_cash']['current'], '50.00')
        self.assertEqual(metrics['spending']['current'], '7000.00')
        self.assertEqual(metrics['cpl_cash']['unit'], '₽/контакт')

    def test_ratios_use_weighted_sums_of_same_ids_and_keep_zero_inputs(self):
        rows, runs = facts()
        for row in rows:
            if row.avito_id == '12':
                row.metrics.update(impressions='1000',
                    views='900' if row.day >= daily.collection_cutoff(NOW)-timedelta(days=6) else '100')
        _, _, metrics = self.subset(rows, runs)
        self.assertEqual(metrics['ctr']['current'], '83.64')
        self.assertEqual(metrics['ctr']['previous'], '10.91')
        self.assertEqual(metrics['ctr']['delta'], '72.73')
        self.assertEqual(metrics['ctr']['delta_unit'], 'п.п.')
        # An ad with zero denominator can still contribute known zero inputs
        # to an aggregate ratio with a positive total denominator.
        for row in rows:
            if row.avito_id == '12':
                row.metrics.update(impressions='0', views='0', contacts='0', spending='0.00')
        _, _, metrics = self.subset(rows, runs)
        self.assertEqual(metrics['ctr']['included_ids'], ['11', '12'])
        self.assertEqual(metrics['ctr']['current'], '20.00')
        self.assertEqual(metrics['cpl_cash']['current'], '50.00')

    def test_zero_is_known_but_zero_denominators_and_low_volume_do_not_create_a_trend(self):
        rows, runs = facts()
        for row in rows:
            row.metrics.update(impressions='0', views='0', contacts='0', spending='0.00')
        _, _, metrics = self.subset(rows, runs)
        self.assertEqual(metrics['views']['included_ids'], ['11', '12'])
        self.assertEqual(metrics['views']['current'], '0')
        self.assertEqual(metrics['views']['delta'], '0')
        self.assertIsNone(metrics['views']['relative_change'])
        for key in ('ctr', 'conversion', 'cpl_cash'):
            self.assertIsNone(metrics[key]['current'])
            self.assertIsNone(metrics[key]['delta'])
            self.assertFalse(metrics[key]['available'])
        self.assertTrue(all(item['low_or_unknown_volume'] for item in metrics.values()))

    def test_changing_account_manifest_keeps_only_exact_same_known_ids_across_both_periods(self):
        rows, runs = facts()
        boundary = daily.collection_cutoff(NOW)-timedelta(days=6)
        for day, run in runs.items():
            if day < boundary:
                run.dimensions.pop('12')
                run.staged_rows = [{'id': '11'}]
        rows = [row for row in rows if row.avito_id != '12' or row.day >= boundary]
        _, pair, metrics = self.subset(rows, runs)
        self.assertTrue(pair['source_cohort_changed'])
        self.assertEqual(metrics['views']['included_ids'], ['11'])
        self.assertEqual(metrics['views']['excluded_ids'], ['12'])
        self.assertEqual(metrics['views']['current'], metrics['views']['previous'])

    def test_missing_date_stale_fact_and_duplicate_never_enter_comparable_subset(self):
        rows, runs = facts()
        runs.pop(daily.collection_cutoff(NOW)-timedelta(days=10))
        _, pair, metrics = self.subset(rows, runs)
        self.assertFalse(pair['known_cohort_complete'])
        self.assertEqual(pair['previous_completed_dates'], 6)
        self.assertEqual(metrics['views']['included_ids'], [])
        self.assertIsNone(metrics['views']['delta'])
        for damaged in ('stale', 'duplicate'):
            rows, runs = facts()
            if damaged == 'stale':
                rows[1].source_run_id = 999
            else:
                rows.append(deepcopy(rows[1]))
            _, _, metrics = self.subset(rows, runs)
            self.assertEqual(metrics['views']['included_ids'], ['11'])
            self.assertEqual(metrics['views']['excluded_ids'], ['12'])

    @patch('pool_service.avito_workspace.fetch_statistics', side_effect=AssertionError('No API in subset'))
    def test_day_pair_bounds_and_private_ui_explicitly_label_partial_result(self, fetch):
        rows, runs = facts()
        rows[0].title = '<script>PRIVATE</script>'
        for row in rows:
            if row.avito_id == '12':
                row.provider_row_present = False
        result, pair, metrics = self.subset(rows, runs, pair=0)
        self.assertEqual((pair['current_from'].isoformat(), pair['current_to'].isoformat(),
                          pair['previous_from'].isoformat(), pair['previous_to'].isoformat()),
                         ('2026-10-23', '2026-10-23', '2026-10-22', '2026-10-22'))
        self.assertEqual(metrics['views']['current'], '20')
        content = render_to_string('pool_service/avito/_decision_report.html', {'report': result})
        self.assertIn('Сопоставимая часть аккаунта', content)
        self.assertIn('не полный итог аккаунта', content)
        self.assertIn('Включено 1/2 известных ID', content)
        self.assertNotIn('<script>PRIVATE</script>', content)
        fetch.assert_not_called()

    def test_closed_subset_evidence_excludes_amounts_titles_and_rejects_wrong_scope(self):
        from pool_service.avito_comparable_subsets import evidence
        rows, runs = facts()
        rows[0].title = 'PRIVATE'
        rows[0].metrics['spending'] = '1234567.89'
        result = report.build(rows, runs, NOW)
        saved = evidence(result['comparable_subsets'], connection_id=4, account_id=daily.ACCOUNT)
        self.assertEqual(set(saved), {'schema_version', 'state', 'connection_id', 'account_id',
            'source', 'provider_timezone', 'barnaul_calendar_confirmed', 'pairs'})
        self.assertEqual(len(saved['pairs']), 2)
        for pair in saved['pairs']:
            self.assertEqual(set(pair), {'key', 'current_from', 'current_to', 'previous_from',
                'previous_to', 'current_completed_dates', 'current_dates',
                'previous_completed_dates', 'previous_dates', 'known_cohort_complete',
                'source_cohort_changed', 'truncated', 'metrics'})
            for item in pair['metrics']:
                self.assertEqual(set(item), {'key', 'included_ids', 'excluded_ids', 'included_ads',
                    'excluded_ads', 'excluded_ids_truncated', 'available', 'low_or_unknown_volume'})
        for private in ('PRIVATE', '1234567.89', 'current_received_at', 'delta', 'title', 'city', 'commission_value'):
            self.assertNotIn(private, str(saved))
        self.assertEqual(evidence(result['comparable_subsets'], connection_id=1,
            account_id=daily.ACCOUNT)['state'], 'blocked')
        self.assertEqual(evidence(result['comparable_subsets'], connection_id=4,
            account_id='other-account')['state'], 'blocked')



class ComparableSubsetSetupTests(TestCase):
    def setUp(self):
        DecisionEvidencePermissionsTests.setUp(self)

    def test_setup_receipt_is_scoped_db_only_and_exports_no_values(self):
        from pool_service.avito_comparable_subsets import safe_setup_evidence
        with patch('pool_service.avito_services_workplace_setup._actor', return_value=self.owner), \
                patch.object(daily, '_allowed', return_value=True), \
                patch.object(daily, '_source_history',
                    return_value=(daily.collection_cutoff(NOW), self.rows, self.runs)), \
                patch('pool_service.avito_workspace.fetch_statistics') as fetch:
            result = safe_setup_evidence(NOW)
        self.assertEqual(result['state'], 'observed')
        self.assertEqual(result['connection_id'], 4)
        self.assertEqual(result['account_id'], daily.ACCOUNT)
        self.assertEqual(len(result['pairs']), 2)
        for private in ('Synthetic service', '100.00', '700.00', 'current_received_at',
                        'delta', 'title', 'city'):
            self.assertNotIn(private, str(result))
        fetch.assert_not_called()

    def test_setup_denies_other_org_manager_or_missing_actor_before_history(self):
        from pool_service.avito_comparable_subsets import safe_setup_evidence
        with patch.object(daily, '_allowed', return_value=True), \
                patch.object(daily, '_source_history') as history:
            for actor in (self.foreign, self.manager, None):
                with patch('pool_service.avito_services_workplace_setup._actor', return_value=actor):
                    result = safe_setup_evidence(NOW)
                    self.assertEqual(result, {'schema_version': 1, 'state': 'unavailable',
                                              'code': 'statistics_subset_unavailable'})
        history.assert_not_called()
        ChannelConnection.objects.filter(pk=4).update(external_id='foreign-account')
        with patch.object(daily, '_source_history') as history:
            self.assertEqual(safe_setup_evidence(NOW)['state'], 'unavailable')
        history.assert_not_called()

    def test_setup_discards_result_after_credential_stamp_or_actor_rights_change(self):
        from pool_service.avito_comparable_subsets import safe_setup_evidence
        with patch('pool_service.avito_services_workplace_setup._actor', return_value=self.owner), \
                patch.object(daily, '_allowed', return_value=True), \
                patch.object(daily, '_stamp', side_effect=['initial-binding', 'changed-binding']), \
                patch.object(daily, '_source_history',
                    return_value=(daily.collection_cutoff(NOW), self.rows, self.runs)):
            self.assertEqual(safe_setup_evidence(NOW)['state'], 'unavailable')
        def revoked(*args, **kwargs):
            User.objects.filter(pk=self.owner.pk).update(is_active=False)
            return daily.collection_cutoff(NOW), self.rows, self.runs
        with patch('pool_service.avito_services_workplace_setup._actor', return_value=self.owner), \
                patch.object(daily, '_allowed', return_value=True), \
                patch.object(daily, '_source_history', side_effect=revoked):
            result = safe_setup_evidence(NOW)
        self.assertEqual(result['state'], 'unavailable')
        self.assertNotIn('pairs', result)

    def test_setup_discards_result_after_organization_move_and_sanitizes_failure(self):
        from pool_service.avito_comparable_subsets import safe_setup_evidence
        def moved(*args, **kwargs):
            CommunicationChannel.objects.filter(pk=self.connection.channel_id).update(
                organization=self.foreign_org)
            return daily.collection_cutoff(NOW), self.rows, self.runs
        with patch('pool_service.avito_services_workplace_setup._actor', return_value=self.owner), \
                patch.object(daily, '_allowed', return_value=True), \
                patch.object(daily, '_source_history', side_effect=moved):
            self.assertEqual(safe_setup_evidence(NOW)['state'], 'unavailable')
        CommunicationChannel.objects.filter(pk=self.connection.channel_id).update(organization=self.org)
        with patch('pool_service.avito_services_workplace_setup._actor',
                side_effect=RuntimeError('PRIVATE authorization payload')):
            result = safe_setup_evidence(NOW)
        self.assertEqual(result, {'schema_version': 1, 'state': 'unavailable',
                                 'code': 'statistics_subset_unavailable'})
        self.assertNotIn('PRIVATE', str(result))


    def test_setup_captures_one_clock_before_history_and_build_across_local_midnight(self):
        from pool_service.avito_comparable_subsets import safe_setup_evidence
        before = datetime(2026, 10, 25, 16, 59, 59, tzinfo=dt_timezone.utc)
        after = before+timedelta(seconds=2)
        observed = []
        def history(connection, now=None):
            observed.append(now or daily.timezone.now())
            return daily.collection_cutoff(observed[-1]), self.rows, self.runs
        with patch('pool_service.avito_services_workplace_setup._actor', return_value=self.owner), \
                patch.object(daily, '_allowed', return_value=True), \
                patch.object(daily, '_source_history', side_effect=history), \
                patch.object(daily.timezone, 'now', side_effect=[before, after]) as clock:
            result = safe_setup_evidence()
        self.assertEqual(result['state'], 'observed')
        self.assertEqual(observed, [before])
        self.assertEqual(result['pairs'][0]['current_to'], '2026-10-23')
        self.assertEqual(clock.call_count, 1)
