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
