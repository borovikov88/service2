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
            unknown=reverse('crm_live_lead_analysis',args=[lead.pk,call.pk+10000])+'?org_id='+str(self.org.pk)
            self.assertEqual(self.client.post(unknown).status_code,404)
            CallPrivateNumber.objects.create(organization=self.org,owner=self.user,phone_key=normalize_phone(call.phone_number))
            self.assertEqual(self.client.post(url).status_code,404);queue.assert_not_called()
