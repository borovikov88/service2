"""Read-only message projection and explicit protected analysis requests."""
from unittest.mock import patch
from django.test import TestCase
from django.urls import reverse
from pool_service.tests.test_crm_lead_intake import LeadIntakeTests
from pool_service.tests.test_crm_inbox import Fixture
from pool_service.communication_models import CommunicationAccess, PhoneCall
from pool_service.crm_commercial_models import CrmLead

class TimelineTests(LeadIntakeTests):
    mapped_call=LeadIntakeTests.mapped_call
    deliver=LeadIntakeTests.deliver

    def test_continuation_private_contributor_cannot_leak_or_queue_analysis(self):
        from pool_service.services.crm_lead_commands import execute_lead_command
        from pool_service.call_processing_models import CallPrivateNumber
        from pool_service.phone_utils import normalize_phone
        self.mapped_call(); self.deliver('call')
        incoming = CrmLead.objects.get(); call = PhoneCall.objects.get()
        original = CrmLead.objects.create(organization=self.org, assignee=self.user, title='Synthetic independent target')
        self.grant.transition_own_leads = True; self.grant.save()
        execute_lead_command(actor=self.user, organization=self.org, operation_key='synthetic-private-continuation',
            command_type='lead.transition', subject_id=incoming.pk, expected_revision=incoming.revision,
            changes={'status':'closed', 'result':'duplicate', 'target_lead_id':original.pk})
        self.client.force_login(self.user)
        detail = reverse('crm_live_lead_detail',args=[original.pk])+'?org_id='+str(self.org.pk)
        analysis = reverse('crm_live_lead_analysis',args=[original.pk,call.pk])+'?org_id='+str(self.org.pk)
        self.assertEqual(len(self.client.get(detail).json()['sources']), 1)
        CommunicationAccess.objects.filter(user=self.user,organization=self.org).update(can_listen_calls=True)
        with patch('pool_service.communication_views.request_call_analysis') as queue:
            CallPrivateNumber.objects.create(organization=self.org,owner=self.user,phone_key=normalize_phone(call.phone_number))
            response = self.client.get(detail)
            self.assertEqual(response.status_code, 200, 'Independent target remains readable')
            self.assertEqual(response.json()['sources'], [])
            self.assertEqual(self.client.post(analysis).status_code, 404)
            queue.assert_not_called()

    def test_contributor_keyset_crosses_empty_batch_and_preserves_cursor_ties(self):
        from pool_service.services.crm_lead_access import source_metadata_many, source_metadata
        from pool_service.crm_commercial_models import CrmLeadSource
        from pool_service.call_processing_models import CallPrivateNumber
        from pool_service.phone_utils import normalize_phone
        from django.utils.dateparse import parse_datetime
        target=CrmLead.objects.create(organization=self.org,title='Synthetic batch target')
        for index in range(200):
            CrmLead.objects.create(organization=self.org,title='Synthetic empty contributor '+str(index),
                status='closed',result='duplicate',duplicate_of=target)
        self.mapped_call();self.deliver('call')
        hidden=CrmLead.objects.filter(sources__inbox__source_type='call').get()
        hidden.status='closed';hidden.result='duplicate';hidden.duplicate_of=target;hidden.save()
        call=PhoneCall.objects.get()
        CallPrivateNumber.objects.create(organization=self.org,owner=self.user,phone_key=normalize_phone(call.phone_number))
        contributors=[]
        for index in range(2):
            with patch('django.utils.timezone.now',return_value=self.now):
                self.incoming(external_conversation_id='synthetic-batch-'+str(index),external_message_id='synthetic-batch-'+str(index))
            # Claim against the real clock: model next_attempt_at defaults are
            # callable references, independent of the synthetic capture clock.
            event=self.deliver();lead=CrmLeadSource.objects.get(inbox=event).lead
            lead.status='closed';lead.result='duplicate';lead.duplicate_of=target;lead.save();contributors.append(lead)
        rows=source_metadata_many(user=self.user,organization=self.org,leads=[target,*contributors],limit=1)
        self.assertEqual(len(rows[target.pk]),1)
        self.assertEqual([len(rows[lead.pk]) for lead in contributors],[1,1])
        first=rows[target.pk][0]
        second=source_metadata(user=self.user,organization=self.org,lead=target,limit=1,
            before=(parse_datetime(first['received_at']),first['id']))
        self.assertEqual(len(second),1);self.assertNotEqual(first['id'],second[0]['id'])
        self.assertEqual(first['received_at'],second[0]['received_at'])
        self.assertGreater(first['id'],second[0]['id'])
        self.assertEqual({first['type'],second[0]['type']},{'message'})

    def test_contributor_revoked_between_split_reads_hides_other_public_source(self):
        from pool_service.services import crm_lead_access as access
        from pool_service.crm_commercial_models import CrmLeadSource
        from pool_service.crm_inbox_models import CrmInboxEvent
        from pool_service.call_processing_models import CallPrivateNumber
        from pool_service.phone_utils import normalize_phone
        self.mapped_call();self.deliver('call');contributor=CrmLead.objects.get();call=PhoneCall.objects.get()
        self.incoming(external_message_id='synthetic-additional-public-source')
        event=CrmInboxEvent.objects.get(source_type='message')
        CrmLeadSource.objects.create(organization=self.org,lead=contributor,inbox=event)
        target=CrmLead.objects.create(organization=self.org,title='Synthetic revocation target')
        contributor.status='closed';contributor.result='duplicate';contributor.duplicate_of=target;contributor.save()
        original=access._identity_batches;revoked=[]
        def batches(queryset,field='pk'):
            for ids in original(queryset,field):
                yield ids
                if contributor.pk in ids and not revoked:
                    CallPrivateNumber.objects.create(organization=self.org,owner=self.user,phone_key=normalize_phone(call.phone_number))
                    revoked.append(True)
        with patch.object(access,'_identity_batches',batches):
            rows=access.source_metadata_many(user=self.user,organization=self.org,leads=[target])
        self.assertTrue(revoked)
        self.assertEqual(rows[target.pk],[],'A newly private call hides the whole contributor including its message')

    def test_message_body_only_in_authorized_detail(self):
        self.incoming(participant_name='Synthetic sender');self.deliver();lead=CrmLead.objects.get()
        self.client.force_login(self.user)
        url=reverse('crm_live_lead_detail',args=[lead.pk])+'?org_id='+str(self.org.pk)
        data=self.client.get(url).json()
        self.assertEqual(data['sources'][0]['message']['sender'],'Synthetic sender')
        self.assertIsNone(data['source_cursor'])
        self.assertEqual(self.client.get(url+'&source_cursor=invalid').status_code,404)
        CommunicationAccess.objects.filter(user=self.user,organization=self.org).update(can_view_conversations=False)
        self.assertEqual(self.client.get(url).status_code,404)

    def test_analysis_explicit_current_permissions_missing_file_and_repeat(self):
        self.mapped_call();self.deliver('call');lead=CrmLead.objects.get();call=PhoneCall.objects.get()
        self.client.force_login(self.user)
        url=reverse('crm_live_lead_analysis',args=[lead.pk,call.pk])+'?org_id='+str(self.org.pk)
        self.assertEqual(self.client.get(url).status_code,405)
        CommunicationAccess.objects.filter(user=self.user,organization=self.org).update(can_listen_calls=True)
        with patch('pool_service.communication_views.request_call_analysis',side_effect=[True,False]) as queue, patch('pool_service.communication_views.start_requested_call_analysis_worker',return_value=False) as worker:
            self.assertEqual(self.client.post(url).status_code,409);queue.assert_not_called()
            call.recording_file='synthetic.wav';call.save(update_fields=['recording_file'])
            self.assertEqual(self.client.post(url).json()['status'],'queued')
            self.assertEqual(self.client.post(url).json()['status'],'already_processing')
            self.assertEqual(queue.call_count,2);worker.assert_called_once()
            CommunicationAccess.objects.filter(user=self.user,organization=self.org).update(can_listen_calls=False)
            self.assertEqual(self.client.post(url).status_code,403);self.assertEqual(queue.call_count,2)

    def test_sources_cursor_bounded_disjoint_and_scoped(self):
        for index in range(22):
            self.incoming(external_message_id='synthetic-page-'+str(index));self.deliver()
        lead=CrmLead.objects.get();self.client.force_login(self.user)
        url=reverse('crm_live_lead_detail',args=[lead.pk])+'?org_id='+str(self.org.pk)
        first=self.client.get(url).json();self.assertEqual(len(first['sources']),20)
        from urllib.parse import quote
        from django.core import signing
        token=signing.loads(first['source_cursor'],salt='crm-lead-sources')
        for index in (0,2):
            foreign=list(token);foreign[index]+=10000
            cursor=signing.dumps(foreign,salt='crm-lead-sources')
            self.assertEqual(self.client.get(url+'&source_cursor='+quote(cursor)).status_code,404)
        second=self.client.get(url+'&source_cursor='+quote(first['source_cursor'])).json()
        self.assertEqual(len(second['sources']),2)
        self.assertFalse({x['id'] for x in first['sources']} & {x['id'] for x in second['sources']})
        self.assertIsNone(second['source_cursor'])

    def test_analysis_rejects_unlinked_private_and_csrf(self):
        from django.test import Client as HttpClient
        from pool_service.call_processing_models import CallPrivateNumber
        from pool_service.phone_utils import normalize_phone
        self.mapped_call();self.deliver('call');lead=CrmLead.objects.get();call=PhoneCall.objects.get()
        call.recording_file='synthetic.wav';call.save(update_fields=['recording_file'])
        CommunicationAccess.objects.filter(user=self.user,organization=self.org).update(can_listen_calls=True)
        self.client.force_login(self.user)
        url=reverse('crm_live_lead_analysis',args=[lead.pk,call.pk])+'?org_id='+str(self.org.pk)
        protected=HttpClient(enforce_csrf_checks=True);protected.force_login(self.user)
        with patch('pool_service.communication_views.request_call_analysis') as queue:
            self.assertEqual(protected.post(url).status_code,403)
            unlinked=PhoneCall.objects.create(organization=self.org,connection=call.connection,external_id='synthetic-unlinked',phone_number='+79990005566',direction='in',started_at=self.now,result='answered',employee=self.user)
            unknown=reverse('crm_live_lead_analysis',args=[lead.pk,unlinked.pk])+'?org_id='+str(self.org.pk)
            self.assertEqual(self.client.post(unknown).status_code,404)
            CallPrivateNumber.objects.create(organization=self.org,owner=self.user,phone_key=normalize_phone(call.phone_number))
            self.assertEqual(self.client.post(url).status_code,404);queue.assert_not_called()
