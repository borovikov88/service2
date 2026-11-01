"""Goods statistics: account isolation, pagination and owner-authorized setup."""
from copy import deepcopy
from unittest.mock import Mock, patch

from django.core.exceptions import PermissionDenied
from django.test import TestCase, Client
from django.urls import reverse
from django.template.loader import render_to_string

from pool_service import avito_daily_statistics as daily, avito_workspace
from pool_service.avito_statistics_models import AvitoDailyStatistic, AvitoStatisticsRun
from pool_service.communication_models import AvitoCredential, ChannelConnection, CommunicationChannel
from pool_service.models import Organization
from pool_service.tests import test_avito_daily_statistics as history_tests


class GoodsDailyHistoryTests(TestCase):
    def setUp(self):
        # Reuse the existing synthetic setup without inheriting/rerunning its tests.
        history_tests.DailyHistoryTests.setUp(self)
        self.goods.external_id = daily.ACCOUNTS[1]
        self.goods.name = 'Goods test'
        self.goods.save(update_fields=['external_id', 'name'])
        AvitoCredential.objects.create(connection=self.goods,
            client_id_encrypted='goods-synthetic-id', client_secret_encrypted='goods-synthetic-secret')
        daily.configure(self.goods, self.owner, 'enable')
        self.goods.refresh_from_db()
        provider = patch.object(daily, 'authorized_account_id', side_effect=lambda connection: str(connection.external_id))
        provider.start()
        self.addCleanup(provider.stop)

    page = history_tests.DailyHistoryTests.page

    def test_more_than_200_goods_resume_all_pages_and_leave_services_untouched(self):
        self.connection.refresh_from_db()
        before = deepcopy(self.connection.settings)
        for offset in range(0, 232, avito_workspace.PAGE_SIZE):
            with patch.object(avito_workspace, 'fetch_statistics', return_value=self.page(
                    range(1000+offset, 1000+min(offset+avito_workspace.PAGE_SIZE, 232)), total=232)) as fetch:
                result = daily.collect_next(1)
            fetch.assert_called_once_with('opaque', daily.ACCOUNTS[1], start='2026-10-23',
                end='2026-10-23', grouping='item', offset=offset)
            self.assertEqual(result, 'success' if offset == 200 else 'skipped')
            if offset < 200:
                self.assertFalse(AvitoDailyStatistic.objects.filter(connection=self.goods).exists())
                daily.configure(self.goods, self.owner, 'retry')
        run = AvitoStatisticsRun.objects.get(connection=self.goods)
        self.assertEqual((run.account_id, run.received_rows, run.pagination_complete), (daily.ACCOUNTS[1], 232, True))
        self.assertEqual(AvitoDailyStatistic.objects.filter(connection=self.goods, account_id=daily.ACCOUNTS[1]).count(), 232)
        self.assertFalse(AvitoDailyStatistic.objects.filter(connection=self.connection).exists())
        self.connection.refresh_from_db()
        self.assertEqual(self.connection.settings, before)
        view = daily.display(self.goods, self.owner, self.now)
        self.assertEqual(view['total_cells'], 232)
        self.assertEqual(view['backfill']['completed_days'], 1)
        self.assertEqual(view['connection_id'], 1)
        self.assertIsNone(view['crm'])
        html = render_to_string('pool_service/avito/_daily_statistics.html', {'daily': view, 'connection': self.goods})
        self.assertIn('Goods test', html)
        self.assertNotIn('подключение «Услуги» №4', html)
        self.assertIn('/avito/1/statistics-daily/', html)

    def test_owner_dashboard_reads_goods_day_without_services_rows(self):
        with patch.object(avito_workspace, 'fetch_statistics', return_value=self.page((11,), total=1)):
            self.assertEqual(daily.collect_next(1), 'success')
            self.assertEqual(daily.collect_next(4), 'success')
        from pool_service.avito_owner_dashboard import display
        dashboard = display(self.goods, self.owner, period='custom', date_from='2026-10-23',
            date_to='2026-10-23', now=self.now)
        self.assertEqual(dashboard['account_id'], daily.ACCOUNTS[1])
        cards = {item['key']: item['value'] for item in dashboard['kpis']}
        self.assertEqual(cards['views'], '10')
        self.assertEqual(cards['spending'], '125.99')
        self.assertEqual(daily.display(self.goods, self.owner, self.now)['total_cells'], 1)

    def test_worker_services_both_accounts_and_preserves_explicit_opt_out(self):
        observer = Mock()
        with patch.object(avito_workspace, 'fetch_statistics', return_value=self.page()):
            summary = daily.scan_due(deadline=daily.time.monotonic()+60, observer=observer)
        self.assertEqual(summary, {'success': 2, 'failed': 0, 'skipped': 0})
        self.assertEqual({call.args[0] for call in observer.call_args_list}, {1, 4})
        daily.configure(self.goods, self.owner, 'disable')
        daily.configure(self.connection, self.owner, 'retry')
        with patch.object(avito_workspace, 'fetch_statistics', return_value=self.page()) as fetch:
            daily.scan_due(deadline=daily.time.monotonic()+60)
        self.assertEqual(fetch.call_args.args[1], daily.ACCOUNT)
        fetch.assert_called_once()

    def test_wrong_provider_account_and_mid_read_rebinding_never_store_goods(self):
        with patch.object(daily, 'authorized_account_id', return_value=daily.ACCOUNT), \
                patch.object(avito_workspace, 'fetch_statistics') as fetch:
            self.assertEqual(daily.collect_next(1), 'failed')
        fetch.assert_not_called()
        self.assertFalse(AvitoDailyStatistic.objects.exists())
        daily.configure(self.goods, self.owner, 'retry')
        def rebind(*args, **kwargs):
            ChannelConnection.objects.filter(pk=1).update(external_id='unbound-account')
            return self.page()
        with patch.object(avito_workspace, 'fetch_statistics', side_effect=rebind):
            self.assertEqual(daily.collect_next(1), 'failed')
        self.assertFalse(AvitoDailyStatistic.objects.exists())

    def test_owner_controls_are_csrf_and_organization_protected_and_redirect_to_goods(self):
        url = reverse('avito_daily_statistics_control', args=[1])
        self.client.force_login(self.manager)
        self.assertEqual(self.client.post(url, {'action': 'disable'}).status_code, 403)
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.owner)
        self.assertEqual(client.post(url, {'action': 'disable'}).status_code, 403)
        self.client.force_login(self.owner)
        response = self.client.post(url, {'action': 'disable'})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, '/avito/?account=1#avito-analytics-details')
        other_org = Organization.objects.create(name='Other statistics org')
        other_channel = CommunicationChannel.objects.create(organization=other_org, kind='avito', name='Other')
        ChannelConnection.objects.filter(pk=1).update(channel=other_channel)
        self.assertEqual(self.client.post(url, {'action': 'enable'}).status_code, 404)

    def test_account_pair_mismatch_and_unknown_connections_are_rejected(self):
        self.goods.external_id = 'unbound-account'
        self.goods.save(update_fields=['external_id'])
        with self.assertRaises(PermissionDenied):
            daily.configure(self.goods, self.owner, 'enable')
        with self.assertRaises(ValueError):
            daily.collect_next(2)

    def test_goods_limit_stays_bounded_without_relaxing_services_limit(self):
        with patch.object(avito_workspace, 'fetch_statistics', return_value=self.page(total=1001)):
            self.assertEqual(daily.collect_next(1), 'failed')
        with patch.object(avito_workspace, 'fetch_statistics', return_value=self.page(total=201)):
            self.assertEqual(daily.collect_next(4), 'failed')
        self.assertFalse(AvitoDailyStatistic.objects.exists())
