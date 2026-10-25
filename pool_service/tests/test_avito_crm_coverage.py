from copy import deepcopy
from datetime import timedelta

from django.test import TestCase

from pool_service.avito_crm_coverage import coverage
from pool_service.communication_models import Conversation
from pool_service.models import Client, Organization
from pool_service.tests import test_avito_daily_statistics as daily_tests


class CrmCoverageTests(TestCase):
    setUp = daily_tests.DailyHistoryTests.setUp

    def context(self):
        from pool_service.avito_services_feed import entries
        self.feed.latest_observations = [
            {'id': row['Id'], 'avito_id': row['AvitoId'], 'status': 'active',
             'source': 'avito_item_detail', 'checked_at': self.now.isoformat()}
            for row in entries(self.feed.published)]
        self.feed.save(update_fields=['latest_observations'])

    def conversation(self, external, *, item='11', client=None, synced=True, connection=None):
        return Conversation.objects.create(connection=connection or self.connection, organization=self.org,
            external_id=external, participant_name='Synthetic participant', client=client,
            provider_context={'item': {'id': item}}, context_synced_at=self.now if synced else None)

    def test_explicit_ad_dialog_client_links_are_counted_separately_without_sales_inference(self):
        self.context()
        client = Client.objects.create(organization=self.org, name='Synthetic client')
        self.conversation('one', client=client)
        self.conversation('two', client=client)
        self.conversation('unknown', item='999', client=client)
        result = coverage(self.connection, self.owner, self.now)
        self.assertEqual(result['total_dialogs'], 3)
        self.assertEqual(result['ad_source_confirmed_dialogs'], 2)
        self.assertEqual(result['ad_source_unknown_dialogs'], 1)
        self.assertEqual(result['dialogs_with_explicit_client'], 3)
        self.assertEqual(result['distinct_client_cards'], 1)
        self.assertEqual(result['ad_dialog_client_chains'], 2)
        self.assertIsNone(result['call_ad_links'])
        self.assertIsNone(result['lead_links'])
        self.assertIsNone(result['order_links'])
        self.assertIsNone(result['payment_links'])
        self.assertIsNone(result['revenue'])

    def test_missing_context_sync_or_stale_ownership_is_not_a_confirmed_source(self):
        self.context()
        self.conversation('unsynced', synced=False)
        result = coverage(self.connection, self.owner, self.now)
        self.assertEqual(result['ad_source_confirmed_dialogs'], 0)
        row = Conversation.objects.get()
        row.context_synced_at = self.now
        row.save(update_fields=['context_synced_at'])
        result = coverage(self.connection, self.owner, self.now+timedelta(days=2))
        self.assertEqual(result['ad_source_confirmed_dialogs'], 0)

    def test_foreign_client_and_goods_connection_never_leak_into_chain(self):
        self.context()
        foreign = Client.objects.create(organization=Organization.objects.create(name='Foreign'), name='PRIVATE')
        self.conversation('foreign-client', client=foreign)
        self.conversation('goods-dialog', connection=self.goods)
        result = coverage(self.connection, self.owner, self.now)
        self.assertEqual(result['total_dialogs'], 1)
        self.assertEqual(result['distinct_client_cards'], 0)
        self.assertEqual(result['ad_dialog_client_chains'], 0)
        self.assertNotIn('PRIVATE', str(result))
        self.assertIsNone(coverage(self.goods, self.owner, self.now))

    def test_title_phone_and_anonymous_context_do_not_create_ad_source(self):
        self.context()
        row = self.conversation('unconfirmed', item=True)
        row.participant_phone = '0000000000'
        row.provider_context = {'item': {'id': True, 'title': 'Same title', 'url': 'https://avito.ru/11'}}
        row.save(update_fields=['participant_phone', 'provider_context'])
        self.assertEqual(coverage(self.connection, self.owner, self.now)['ad_source_confirmed_dialogs'], 0)

    def test_channel_manager_and_client_card_acl_are_required(self):
        self.context()
        self.conversation('one')
        self.assertIsNone(coverage(self.connection, self.manager, self.now))
        self.feed.account_id = 'foreign'
        self.feed.save(update_fields=['account_id'])
        result = coverage(self.connection, self.owner, self.now)
        self.assertEqual(result['ad_source_confirmed_dialogs'], 0)
