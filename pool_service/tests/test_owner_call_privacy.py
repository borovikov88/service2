from datetime import timedelta
from unittest.mock import patch
from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.test import TestCase, override_settings
from django.urls import reverse
from pool_service.tests.test_crm_inbox import Fixture
from pool_service.models import Client, Employee, OrganizationAccess
from pool_service.communication_models import PhoneCall, TelephonyEmployeeIdentity, ConversationMessage
from pool_service.call_processing_models import CallPrivateNumber
from pool_service.crm_inbox_models import CrmInboxEvent
from pool_service.services.owner_call_privacy import contact_state, set_personal_peer, verified_owner


@override_settings(COMMUNICATION_RECORDING_DOWNLOAD_INLINE=False, COMMUNICATION_CLIENT_ENRICHMENT_ENABLED=False)
class OwnerCallPrivacyTests(Fixture, TestCase):
    def setUp(self):
        super().setUp(); self.megafon()
        self.owner=get_user_model().objects.create_user(username="synthetic-owner")
        self.staff=get_user_model().objects.create_user(username="synthetic-staff")
        OrganizationAccess.objects.create(organization=self.org,user=self.owner,role="owner")
        OrganizationAccess.objects.create(organization=self.org,user=self.staff,role="manager")
        self.employee=Employee.objects.create(organization=self.org,user=self.owner,display_name="Synthetic owner")
        self.identity=TelephonyEmployeeIdentity.objects.create(organization=self.org,connection=self.telephony,
            extension="101",external_user="synthetic-owner-provider",employee=self.employee,
            status="manually_matched",match_method="manual",confirmed_by=self.owner,confirmed_at=self.now-timedelta(days=1))
        self.person=Client.objects.create(organization=self.org,name="Synthetic private peer",client_type="private",phone="+79001234567")
        self.mark()

    def mark(self):
        set_personal_peer(self.owner,self.person.pk,"1",contact_state(self.owner,self.person)["revision"])

    def call(self, **changes):
        return self.history(user="synthetic-owner-provider",ext="101",**changes)

    def test_owner_peer_both_directions_and_replay_do_not_persist_or_download(self):
        with patch('pool_service.communication_api.download_call_recording') as download:
            for direction in ('in','out','in'):
                response=self.call(type=direction,link='https://synthetic.invalid/recording')
                self.assertEqual(response.status_code,200)
                self.assertEqual(response.json(),{'accepted':True,'ignored':True,'reason':'private_call'})
            download.assert_not_called()
        self.assertFalse(PhoneCall.objects.exists());self.assertFalse(CrmInboxEvent.objects.exists())

    def test_other_owner_calls_and_employee_conversations_are_work(self):
        self.assertEqual(self.call(phone='+79009998877').status_code,200)
        self.assertTrue(PhoneCall.objects.exists())
        employee=Employee.objects.create(organization=self.org,user=self.staff,display_name='Synthetic staff')
        TelephonyEmployeeIdentity.objects.create(organization=self.org,connection=self.telephony,extension='102',
            external_user='synthetic-staff-provider',employee=employee,status='manually_matched',match_method='manual',
            confirmed_by=self.owner,confirmed_at=self.now-timedelta(days=1))
        self.assertEqual(self.history(callid='staff-call',user='synthetic-staff-provider',ext='102').status_code,200)
        self.assertEqual(PhoneCall.objects.count(),2)
        self.incoming(participant_phone=self.person.phone)
        self.assertEqual(ConversationMessage.objects.count(),1)

    def test_stale_identity_reassignment_unknown_time_are_not_guessed(self):
        for changes in ({'requires_manual_confirmation':True},{'reassignment_detected_at':self.now},
                {'status':'auto_matched'},{'confirmed_at':self.now+timedelta(days=1)}):
            original={key:getattr(self.identity,key) for key in changes}
            TelephonyEmployeeIdentity.objects.filter(pk=self.identity.pk).update(**changes)
            self.assertIsNone(verified_owner(self.telephony,'synthetic-owner-provider','101',self.now))
            TelephonyEmployeeIdentity.objects.filter(pk=self.identity.pk).update(**original)
        self.assertIsNone(verified_owner(self.telephony,'synthetic-owner-provider','999',self.now))
        self.assertIsNone(verified_owner(self.telephony,'synthetic-owner-provider','101',None))

    def test_existing_history_is_preserved_on_private_replay(self):
        CallPrivateNumber.objects.all().delete()
        self.call(); call=PhoneCall.objects.get(); before=PhoneCall.objects.values().get(pk=call.pk)
        self.mark()
        self.assertEqual(self.call(duration='900',link='https://synthetic.invalid/new').json()['reason'],'private_call')
        self.assertEqual(PhoneCall.objects.values().get(pk=call.pk),before)

    def test_only_owner_own_list_and_current_numbers_snapshot(self):
        with self.assertRaises(PermissionDenied):
            set_personal_peer(self.staff,self.person.pk,'1',contact_state(self.staff,self.person)['revision'])
        revision=contact_state(self.owner,self.person)['revision']
        self.person.phone='+79009998877';self.person.save()
        with self.assertRaises(ValidationError):
            set_personal_peer(self.owner,self.person.pk,'0',revision)
        self.assertEqual(CallPrivateNumber.objects.count(),1)
        self.client.force_login(self.staff)
        response=self.client.get(reverse('client_detail',args=[self.person.pk]))
        self.assertNotContains(response,'data-owner-personal-peer')

    def test_existing_other_participant_is_not_reclassified_from_transfer(self):
        from pool_service.services.owner_call_privacy import exclude_before_save
        existing=PhoneCall(employee_id=self.staff.pk,peer_employee_id=None)
        with transaction.atomic():
            self.assertFalse(exclude_before_save(connection=self.telephony,provider_user='synthetic-owner-provider',
                extension='101',peer_phone=self.person.phone,occurred_at=self.now,existing=existing))

    def test_profile_only_and_provider_only_transfer_are_not_private_replays(self):
        from pool_service.services.owner_call_privacy import exclude_before_save
        peer = Employee.objects.create(organization=self.org, display_name="Synthetic staff without user")
        for fields in ({"employee_profile_id":peer.pk}, {"provider_user":"another-provider"},
                {"provider_extension":"102"}, {"peer_provider_user":"another-provider"}):
            existing=PhoneCall(direction="in", phone_number=self.person.phone, **fields)
            with transaction.atomic():
                self.assertFalse(exclude_before_save(connection=self.telephony,provider_user="synthetic-owner-provider",
                    extension="101",peer_phone=self.person.phone,occurred_at=self.now,existing=existing))

    def test_readonly_diagnostic_contains_no_numbers_or_labels(self):
        from io import StringIO
        from django.core.management import call_command
        out=StringIO()
        before=CallPrivateNumber.objects.count()
        call_command("preview_owner_call_privacy",organization=self.org.pk,owner=self.owner.pk,stdout=out)
        self.assertIn('"early_gate_ready": true',out.getvalue())
        self.assertNotIn(self.person.name,out.getvalue()); self.assertNotIn(self.person.phone,out.getvalue())
        self.assertEqual(CallPrivateNumber.objects.count(),before)
