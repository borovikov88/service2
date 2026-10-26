"""Synthetic period evidence; no company amounts or customer records."""
from copy import deepcopy
from datetime import timedelta
from unittest.mock import patch

from django.contrib.auth.models import User
from django.core.exceptions import PermissionDenied
from django.test import TestCase, override_settings

from pool_service import avito_experiment_baseline as baseline
from pool_service import avito_improvement_proposals as projects
from pool_service.avito_feed_models import AvitoServicesFeed
from pool_service.communication_models import ChannelConnection
from pool_service.models import OrganizationAccess
from pool_service.tests.test_avito_improvement_proposals import ProposalFixture


@override_settings(ALLOWED_HOSTS=['testserver'], AVITO_SERVICES_PUBLIC_ORIGIN='https://service2.aqualine22.ru')
class WaterExperimentBaselineTests(ProposalFixture, TestCase):
    def setUp(self):
        super().setUp()
        self.prepare()
        self.project = self.events().get(changes__0__avito_id=baseline.WATER_ID)
        ids = sorted(projects.TARGETS)
        self.reports = {'state': 'available', 'source': 'own_ads_stats_v2_period',
            'binding_digest': projects._digest(projects._binding(self.connection)), 'reports': []}
        for index, bounds in enumerate(baseline.PERIODS):
            rows = []
            for identifier in ids:
                metrics = {key: '0' for key in baseline.FIELDS}
                metrics.update(impressions='20', views='4' if index == 0 else '2',
                    contacts='1', spending='2.50' if index == 0 else '1.25',
                    allSpending='2.50' if index == 0 else '1.25', commission=None)
                rows.append({'id': identifier, 'metrics': metrics, 'private_raw_customer': 'PRIVATE_CUSTOMER'})
            self.reports['reports'].append({'date_from': bounds[0], 'date_to': bounds[1],
                'grouping': 'item', 'received_at': (self.now-timedelta(minutes=5+index)).isoformat(),
                'pages_complete': True, 'unit_contract': baseline.UNIT, 'total': len(ids),
                'group_count': len(ids), 'ids': ids, 'page_offsets': [0], 'rows': rows,
                'private_raw_response': 'PRIVATE_CUSTOMER'})
        helper = patch.object(baseline, 'period_reports', side_effect=lambda *a, **kw: deepcopy(self.reports))
        self.period_read = helper.start()
        self.addCleanup(helper.stop)

    def supplements(self):
        return self.feed.versions.filter(action='verify', changes__0__kind=baseline.KIND)

    def prepare_baseline(self):
        return baseline.prepare(self.connection, self.owner, expected_project_id=self.project.pk)

    def test_append_only_idempotent_evidence_keeps_project_contents_and_criteria(self):
        parent_before = deepcopy(self.project.changes)
        before = (deepcopy(self.feed.draft), deepcopy(self.feed.published),
            self.feed.revision, self.feed.published_revision, self.feed.updated_at,
            deepcopy(self.feed.latest_observations))
        first, second = self.prepare_baseline(), self.prepare_baseline()
        self.assertEqual(first['state'], 'prepared')
        self.assertEqual(second['state'], 'existing')
        self.assertEqual(first['history_event_id'], second['history_event_id'])
        self.assertEqual(self.supplements().count(), 1)
        self.feed.refresh_from_db()
        self.project.refresh_from_db()
        self.assertEqual(self.project.changes, parent_before)
        self.assertEqual(before, (self.feed.draft, self.feed.published,
            self.feed.revision, self.feed.published_revision, self.feed.updated_at,
            self.feed.latest_observations))
        event = self.supplements().get()
        change = event.changes[0]
        self.assertEqual(event.snapshot, self.feed.draft)
        self.assertEqual(event.actor_id, self.owner.pk)
        self.assertEqual(event.source_version_id, self.source.pk)
        self.assertEqual(change['parent_project_event_id'], self.project.pk)
        self.assertEqual(change['evaluation'], parent_before[0]['proposal']['evaluation'])
        self.assertEqual(change['state'], 'prepared_not_applied')
        self.assertFalse(change['provider_content_verified'])
        self.assertEqual(change['quality_baseline'], {'state': 'requires_manager_classification', 'value': None})
        self.assertNotIn('PRIVATE_CUSTOMER', str(event.changes))
        self.assertNotIn('PRIVATE_CUSTOMER', str(first))
        self.assertNotIn('metrics', first['periods'][0])
        self.assertFalse(first['daily_rows_created'])
        self.provider.assert_not_called()

    def test_period_aggregate_never_becomes_daily_coverage_or_unknown_metric_zero(self):
        self.prepare_baseline()
        current = self.supplements().get().changes[0]['current_period']
        self.assertTrue(current['pages_complete'])
        self.assertTrue(current['period_row_present'])
        self.assertFalse(current['daily_coverage_known'])
        self.assertNotIn('reported_days', current)
        self.assertNotIn('expected_days', current)
        self.assertIsNone(current['metrics']['commission'])
        self.assertFalse(current['metrics_known']['commission'])
        self.assertEqual(current['metrics']['favorites'], '0')
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Сохранённая база первого эксперимента')
        self.assertContains(response, 'не подтверждают наличие каждой суточной строки')
        self.assertContains(response, 'Историческое наблюдение Wordstat')
        self.assertContains(response, 'Основная метрика')
        self.assertNotContains(response, 'PRIVATE_CUSTOMER')
        self.provider.assert_not_called()

    def test_missing_partial_or_stale_report_creates_no_marker_and_can_later_retry(self):
        good = deepcopy(self.reports)
        cases = []
        missing = deepcopy(good); missing['reports'].pop(); cases.append(missing)
        partial = deepcopy(good); partial['reports'][0]['pages_complete'] = False; cases.append(partial)
        total = deepcopy(good); total['reports'][0].pop('total'); cases.append(total)
        stale = deepcopy(good); stale['reports'][0]['received_at'] = (self.now-timedelta(days=2)).isoformat(); cases.append(stale)
        absent = deepcopy(good); absent['reports'][0]['rows'][0]['metrics'].update(spending=True); cases.append(absent)
        wrong_binding = deepcopy(good); wrong_binding['binding_digest'] = 'b'*64; cases.append(wrong_binding)
        for value in cases:
            self.reports = value
            with self.subTest(value=value['reports'][0].get('received_at')):
                with self.assertRaises(baseline.BaselineUnavailable):
                    self.prepare_baseline()
            self.assertFalse(self.supplements().exists())
        self.reports = good
        self.assertEqual(self.prepare_baseline()['state'], 'prepared')
        self.assertEqual(self.supplements().count(), 1)
        self.provider.assert_not_called()

    def test_permissions_account_org_binding_and_source_are_checked_before_commit(self):
        foreign = User.objects.create_user('baseline-foreign-owner')
        OrganizationAccess.objects.create(organization=self.foreign_org, user=foreign, role='owner')
        with self.assertRaises(PermissionDenied):
            baseline.prepare(self.connection, foreign, expected_project_id=self.project.pk)
        with self.assertRaises(PermissionDenied):
            baseline.prepare(self.goods, self.owner, expected_project_id=self.project.pk)
        self.period_read.assert_not_called()
        good = deepcopy(self.reports)
        def changed_binding(*args, **kwargs):
            ChannelConnection.objects.filter(pk=4).update(settings={'generation': 1})
            return deepcopy(good)
        self.period_read.side_effect = changed_binding
        with self.assertRaises(PermissionDenied):
            self.prepare_baseline()
        self.assertFalse(self.supplements().exists())
        def changed_source(*args, **kwargs):
            draft = deepcopy(self.feed.draft)
            draft[0]['rows'][0]['Title'] = 'Изменённая синтетическая услуга'
            AvitoServicesFeed.objects.filter(pk=self.feed.pk).update(draft=draft)
            return deepcopy(good)
        self.period_read.side_effect = changed_source
        with self.assertRaises(PermissionDenied):
            self.prepare_baseline()
        self.assertFalse(self.supplements().exists())
        self.goods.refresh_from_db()
        self.assertEqual(self.goods.settings, {'preserve': 'goods'})
        self.provider.assert_not_called()

    def test_saved_snapshot_retains_its_period_and_time_when_report_refresh_fails(self):
        first = self.prepare_baseline()
        original = deepcopy(self.supplements().get().changes)
        self.reports = {'state': 'unavailable', 'reports': []}
        with self.assertRaises(baseline.BaselineUnavailable):
            self.prepare_baseline()
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        item = next(item for item in response.context['projects'] if item['avito_id'] == baseline.WATER_ID)
        saved = item['period_baseline']
        self.assertEqual(saved['history_event_id'], first['history_event_id'])
        self.assertEqual([(p['date_from'], p['date_to']) for p in saved['periods']], list(baseline.PERIODS))
        self.assertEqual(self.supplements().count(), 1)
        self.assertEqual(self.supplements().get().changes, original)
        with patch.object(baseline.timezone, 'now', return_value=self.now+timedelta(days=2)):
            stale = self.client.get(self.url)
        self.assertEqual(stale.status_code, 200)
        self.assertContains(stale, 'Получение базы устарело')
        self.assertNotContains(stale, 'PRIVATE_CUSTOMER')
        self.provider.assert_not_called()

    def test_wordstat_is_owner_observed_historical_partial_and_not_frequency_or_auth_status(self):
        self.prepare_baseline()
        wordstat = self.supplements().get().changes[0]['wordstat']
        self.assertEqual(wordstat['evidence_source'], 'owner_browser_report')
        self.assertEqual(wordstat['observed_on'], '2026-10-26')
        self.assertIsNone(wordstat['observed_at'])
        self.assertEqual(wordstat['observed_precision'], 'date')
        self.assertEqual((wordstat['date_from'], wordstat['date_to']), ('2026-09-08', '2026-10-07'))
        self.assertEqual(wordstat['regions'], ['Алтайский край', 'Республика Алтай'])
        self.assertTrue(wordstat['partly_unavailable'])
        self.assertIsNone(wordstat['frequency'])
        self.assertFalse(wordstat['current_seasonality_confirmed'])
        self.assertFalse(wordstat['assistant_live_verified'])
        self.assertNotIn('authorization', wordstat)
        self.assertNotIn('expired', wordstat)
        self.provider.assert_not_called()
