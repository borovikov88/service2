"""Synthetic records only: read-only project receipts and existing CRM triage."""
from copy import deepcopy
from datetime import timedelta
import json
import uuid
from unittest.mock import patch

from django.core.exceptions import PermissionDenied
from django.test import TestCase, override_settings
from django.urls import reverse

from pool_service import avito_owner_review as review
from pool_service import crm_source_links as links
from pool_service.avito_feed_models import AvitoServicesFeedVersion
from pool_service.avito_services_publication import bind_publication
from pool_service.communication_models import Conversation, CommunicationAccess, PhoneCall, TelephonyConnection
from pool_service.crm_source_models import CrmSourceLink
from pool_service.models import CrmItem, DataAuditLog, ServiceTask
from pool_service.tests.test_avito_improvement_proposals import ProposalFixture


@override_settings(ALLOWED_HOSTS=['testserver'], AVITO_SERVICES_PUBLIC_ORIGIN='https://service2.aqualine22.ru')
class OwnerReviewTests(ProposalFixture, TestCase):
    def setUp(self):
        super().setUp()
        # Fixed production event identities are tested with synthetic titles.
        AvitoServicesFeedVersion.objects.create(pk=8, feed=self.feed, revision=18,
            action='verify', snapshot=deepcopy(self.feed.draft), changes=[])
        create_version = AvitoServicesFeedVersion.objects.create
        def create_fixture_project(**values):
            changes = values.get('changes')
            if (values.get('action') == 'verify' and isinstance(changes, list) and len(changes) == 1
                    and changes[0].get('kind') == review.projects.KIND
                    and changes[0].get('avito_id') in review.PROJECTS):
                # MySQL rollback does not rewind AUTO_INCREMENT; reserve only
                # the two exact synthetic events required by the runtime contract.
                values['pk'] = review.PROJECTS[changes[0]['avito_id']]
            return create_version(**values)
        with patch.object(AvitoServicesFeedVersion.objects, 'create', side_effect=create_fixture_project):
            self.prepare()
        self.company = patch('pool_service.crm_source_links.company_has_access', return_value=True)
        self.company.start(); self.addCleanup(self.company.stop)
        self.feed.latest_observations = [{'id': row['Id'], 'avito_id': row['AvitoId'],
            'status': 'active', 'source': 'avito_item_detail',
            'checked_at': (self.now-timedelta(minutes=5)).isoformat()}
            for row in self.feed.published[0]['rows']]
        self.feed.save(update_fields=['latest_observations'])
        self.dialog = self.make_dialog('water-fixture', review.WATER_ID)

    def make_dialog(self, external, item=None, **extra):
        return Conversation.objects.create(organization=self.org, connection=self.connection,
            external_id=external, participant_name='PRIVATE PARTICIPANT',
            provider_context={'item': {'id': item}} if item else {},
            context_synced_at=self.now-timedelta(minutes=10), **extra)

    def readonly_baseline(self):
        self.feed.refresh_from_db(); self.connection.refresh_from_db(); self.goods.refresh_from_db()
        return (deepcopy(self.feed.draft), deepcopy(self.feed.published), self.feed.revision,
            self.feed.published_revision, self.feed.updated_at, deepcopy(self.connection.settings),
            deepcopy(self.goods.settings), AvitoServicesFeedVersion.objects.count(),
            CrmSourceLink.objects.count(), CrmItem.objects.count(), ServiceTask.objects.count(),
            DataAuditLog.objects.count())

    def test_fixed_two_project_receipt_is_readonly_and_contains_no_literal_titles(self):
        before = self.readonly_baseline()
        result = review.project_receipt(self.connection, self.owner)
        self.assertEqual([(row['avito_id'], row['event_id']) for row in result['projects']],
            [(review.WATER_ID, 9), ('8343962135', 10)])
        for row in result['projects']:
            self.assertEqual(row['state'], 'available')
            self.assertTrue(row['current_source_matches'])
            self.assertTrue(row['current_local_title_matches'])
            self.assertTrue(row['fresh_active_status_confirmed'])
            self.assertFalse(row['provider_content_verified'])
            self.assertEqual(row['evaluation']['state'], 'not_started')
            self.assertEqual(row['evaluation']['source_dates'], 14)
            self.assertEqual(row['baseline']['reported_days'], 3)
        self.assertNotIn('Тестовая', json.dumps(result, ensure_ascii=False))
        self.assertEqual(self.readonly_baseline(), before)
        self.provider.assert_not_called()

    def test_fixed_receipt_does_not_substitute_other_project_or_claim_local_title_match(self):
        # Keep event identity; published title changes in a different local publication.
        changed = deepcopy(self.feed.published)
        changed[0]['rows'][0]['Title'] = 'OTHER PRIVATE TITLE'
        self.feed.published = changed
        self.feed.save(update_fields=['published'])
        with self.assertRaises(review.projects.ProposalError):
            review.project_receipt(self.connection, self.owner)

    def test_history_only_publication_replacement_discards_stale_receipt(self):
        original = review.projects._publication_connection
        calls = []
        def changing_source(*args, **kwargs):
            result = original(*args, **kwargs)
            calls.append(True)
            if len(calls) == 3:  # Before, existing page context, final authority read.
                version = AvitoServicesFeedVersion.objects.create(feed=self.feed, revision=18,
                    published_revision=18, action='publish', snapshot=deepcopy(self.feed.published),
                    file_digest=self.source.file_digest)
                bind_publication(self.feed, version)
            return result
        with patch.object(review.projects, '_publication_connection', side_effect=changing_source):
            with self.assertRaises(review.projects.ProposalError):
                review.project_receipt(self.connection, self.owner)
        self.provider.assert_not_called()

    def test_safe_baseline_projects_only_typed_dates_booleans_and_known_flags(self):
        baseline = review._baseline({'from': 'PRIVATE', 'to': '2026-10-20',
            'received_at': 'PRIVATE', 'expected_days': 'PRIVATE', 'reported_days': True,
            'provider_timezone': 'PRIVATE', 'metrics_known': {'views': 'PRIVATE', 'PRIVATE': 123}})
        self.assertNotIn('PRIVATE', json.dumps(baseline))
        self.assertIsNone(baseline['from'])
        self.assertIsNone(baseline['expected_days'])
        self.assertFalse(baseline['metrics_known']['views'])

    def test_queue_uses_provider_association_and_existing_readonly_manager_url(self):
        before = self.readonly_baseline()
        self.make_dialog('unknown-fixture')
        self.make_dialog('different-ad-fixture', '8343962135')
        queue = review.water_quality_queue(self.owner, self.connection)
        self.assertEqual(queue['awaiting_quality_inspected'], 1)
        self.assertEqual(queue['unknown_source_inspected'], 1)
        row = queue['rows'][0]
        self.assertEqual(row['id'], str(self.dialog.uuid))
        self.assertEqual(row['source_method'], 'stored_provider_context')
        self.assertEqual(row['manager_url'], reverse('avito_crm_attribution', args=[4])
            + '?conversation=' + str(self.dialog.uuid))
        self.assertNotIn('PRIVATE', json.dumps(queue))
        self.assertEqual(self.readonly_baseline(), before)
        self.provider.assert_not_called()

    def test_assessed_sources_leave_queue_and_explicit_unknown_is_not_water(self):
        item = CrmItem.objects.create(organization=self.org, direction='service', title='Synthetic CRM request', stage='srv_new')
        link, _ = links.save_link(self.owner, 4, 'conversation', self.dialog.uuid,
            item_id=item.pk, quality='target', ad_mode='auto', operation=uuid.uuid4(), expected_revision=0)
        queue = review.water_quality_queue(self.owner, self.connection)
        self.assertEqual(queue['awaiting_quality_inspected'], 0)
        self.assertEqual(queue['assessed_water_sources_inspected'], 1)
        links.save_link(self.owner, 4, 'conversation', self.dialog.uuid,
            item_id=item.pk, quality='unknown', ad_mode='unknown', operation=uuid.uuid4(), expected_revision=link.revision)
        queue = review.water_quality_queue(self.owner, self.connection)
        self.assertEqual(queue['awaiting_quality_inspected'], 0)
        self.assertEqual(queue['unknown_source_inspected'], 1)

    def test_missing_stale_or_future_owned_source_does_not_become_water(self):
        self.feed.latest_observations = []
        self.feed.save(update_fields=['latest_observations'])
        self.assertEqual(review.water_quality_queue(self.owner, self.connection)['rows'], [])

    def test_foreign_org_account_and_revoked_conversation_rights_do_not_leak(self):
        foreign = Conversation.objects.create(organization=self.foreign_org, connection=self.connection,
            external_id='foreign-fixture', participant_name='FOREIGN PRIVATE',
            provider_context={'item': {'id': review.WATER_ID}}, context_synced_at=self.now)
        self.assertNotIn(str(foreign.uuid), json.dumps(review.water_quality_queue(self.owner, self.connection)))
        with self.assertRaises(PermissionDenied):
            review.project_receipt(self.goods, self.owner)
        with self.assertRaises(PermissionDenied):
            review.water_quality_queue(self.owner, self.goods)
        CommunicationAccess.objects.filter(user=self.owner, organization=self.org).update(can_view_conversations=False)
        self.assertEqual(review.water_quality_queue(self.owner, self.connection)['rows'], [])
        self.provider.assert_not_called()

    def test_assignment_is_visible_readonly_and_hidden_crm_or_task_remains_hidden(self):
        other = type(self.owner).objects.create_user('other-synthetic-manager')
        self.dialog.assignee = other
        self.dialog.save(update_fields=['assignee'])
        CommunicationAccess.objects.filter(user=self.owner, organization=self.org).update(can_assign_conversation=False)
        queue = review.water_quality_queue(self.owner, self.connection)
        self.assertEqual(len(queue['rows']), 1)
        self.assertFalse(queue['rows'][0]['can_edit'])
        item = CrmItem.objects.create(organization=self.org, direction='service', title='Synthetic private request', stage='srv_new')
        link = CrmSourceLink.objects.create(organization=self.org, avito_connection=self.connection,
            conversation=self.dialog, crm_item=item, ad_source='provider', avito_item_id=review.WATER_ID,
            evidence={'connection_id': 4, 'account_id': review.projects.ACCOUNT,
                'organization_id': self.org.pk, 'source_kind': 'conversation', 'source_id': self.dialog.pk,
                'ad_method': 'stored_provider_context', 'ad_confirmed_by_id': self.owner.pk,
                'ad_confirmed_at': self.now.isoformat()})
        with patch.object(links, 'item_for', side_effect=PermissionDenied):
            self.assertEqual(review.water_quality_queue(self.owner, self.connection)['rows'], [])
        task = ServiceTask.objects.create(organization=self.org, crm_item=item, title='Synthetic follow-up',
            start_date=self.now.date(), created_by=self.owner)
        link.next_task = task; link.save(update_fields=['next_task'])
        with patch.object(links, 'task_allowed', return_value=False):
            self.assertIsNone(review.water_quality_queue(self.owner, self.connection)['rows'][0]['next_task_id'])

    def test_calls_require_both_explicit_origin_and_specific_ad_confirmation(self):
        telephone = TelephonyConnection.objects.create(organization=self.org, name='Synthetic telephony')
        call = PhoneCall.objects.create(organization=self.org, connection=telephone,
            external_id='synthetic-call', employee=self.owner, direction='in',
            phone_number='+79990000001', started_at=self.now, source_kind='telephony')
        with patch.object(links, 'is_private_call', return_value=False):
            self.assertEqual(len(review.water_quality_queue(self.owner, self.connection)['rows']), 1)
            links.save_link(self.owner, 4, 'call', call.pk, quality='unknown', call_origin='manual',
                ad_mode='manual', manual_ad_id=review.WATER_ID, operation=uuid.uuid4(), expected_revision=0)
            rows = review.water_quality_queue(self.owner, self.connection)['rows']
        self.assertEqual({row['kind'] for row in rows}, {'call', 'conversation'})
        self.assertNotIn('+79990000001', json.dumps(rows))

    def test_queue_cap_is_explicit_and_no_unknown_source_is_assigned_to_water(self):
        for index in range(4):
            self.make_dialog('extra-fixture-' + str(index), review.WATER_ID)
        with patch.object(review, 'DISPLAY_LIMIT', 2):
            result = review.water_quality_queue(self.owner, self.connection)
        self.assertEqual(len(result['rows']), 2)
        self.assertFalse(result['display_complete'])
        self.assertEqual(result['awaiting_quality_inspected'], 5)

    def test_existing_manager_page_shows_queue_links_and_does_not_write(self):
        before = self.readonly_baseline()
        response = self.client.get(reverse('avito_crm_attribution', args=[4]))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Водоочистка · 8346503274')
        self.assertContains(response, str(self.dialog.uuid))
        self.assertContains(response, 'Неизвестный источник')
        self.assertEqual(self.readonly_baseline(), before)
        self.provider.assert_not_called()

    def test_operator_receipt_exports_counts_but_no_working_source_or_crm_identifiers(self):
        item = CrmItem.objects.create(pk=981234567, organization=self.org, direction='service',
            title='Synthetic private request', stage='srv_new')
        task = ServiceTask.objects.create(pk=982345678, organization=self.org, crm_item=item,
            title='Synthetic private follow-up', start_date=self.now.date(), created_by=self.owner)
        links.save_link(self.owner, 4, 'conversation', self.dialog.uuid,
            item_id=item.pk, task_id=task.pk, quality='unknown', ad_mode='auto',
            operation=uuid.uuid4(), expected_revision=0)
        private = review.water_quality_queue(self.owner, self.connection)
        self.assertEqual(private['rows'][0]['id'], str(self.dialog.uuid))
        self.assertEqual(private['rows'][0]['crm_item_id'], item.pk)
        self.assertEqual(private['rows'][0]['next_task_id'], task.pk)
        with patch('pool_service.avito_services_workplace_setup._connection', return_value=self.connection), \
                patch('pool_service.avito_services_workplace_setup._actor', return_value=self.owner):
            before = self.readonly_baseline()
            result = review.safe_owner_review_receipt()
        encoded = json.dumps(result, ensure_ascii=False)
        self.assertEqual(result['state'], 'available')
        self.assertEqual(result['water_quality']['awaiting_quality_inspected'], 1)
        self.assertNotIn('sources', result['water_quality'])
        for value in ('Тестовая', 'PRIVATE', 'manager_url', '/attribution/', 'amount', 'reason',
                str(self.dialog.uuid), str(item.pk), str(task.pk), 'crm_item_id', 'next_task_id'):
            self.assertNotIn(value, encoded)
        self.assertEqual(self.readonly_baseline(), before)
        self.provider.assert_not_called()
