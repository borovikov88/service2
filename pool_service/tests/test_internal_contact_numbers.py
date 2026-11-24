from datetime import timedelta

from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied, ValidationError
from django.test import TestCase, override_settings
from django.urls import reverse

from pool_service.client_crm_models import ClientBusinessRole
from pool_service.communication_models import PhoneCall
from pool_service.crm_commercial_models import CrmLead
from pool_service.models import Client, OrganizationAccess
from pool_service.services.call_access import can_read_call
from pool_service.services.call_processing_settings import preview_rules, runtime_decision_for_call
from pool_service.services.internal_contact_numbers import confirm_internal_numbers, internal_state
from pool_service.staff_call_models import InternalContactNumber
from pool_service.tests.test_crm_lead_intake import LeadIntakeFixture


@override_settings(COMMUNICATION_RECORDING_DOWNLOAD_INLINE=False, COMMUNICATION_CLIENT_ENRICHMENT_ENABLED=False)
class InternalContactNumbersTests(LeadIntakeFixture, TestCase):
    def setUp(self):
        super().setUp()
        self.owner = get_user_model().objects.create_user('synthetic-confirming-owner')
        OrganizationAccess.objects.create(organization=self.org, user=self.owner, role='owner')
        self.person = Client.objects.create(organization=self.org, client_type='private',
            name='Test internal', phone='+79001234567')
        ClientBusinessRole.objects.create(client=self.person, role='internal', active=True)

    def confirm(self, enabled='1', actor=None, revision=None):
        confirm_internal_numbers(actor or self.owner, self.person.pk, enabled=enabled,
            revision=revision or internal_state(self.person)['revision'])

    def test_only_director_can_confirm_unique_reviewed_number(self):
        with self.assertRaises(PermissionDenied):
            self.confirm(actor=self.user)
        revision = internal_state(self.person)['revision']
        self.person.phone = '+79001234568'
        self.person.save()
        with self.assertRaises(ValidationError):
            self.confirm(revision=revision)
        Client.objects.create(organization=self.org, client_type='legal', name='Other identity',
            phone=self.person.phone)
        with self.assertRaises(ValidationError):
            self.confirm()
        self.assertFalse(InternalContactNumber.objects.exists())

    def test_snapshot_is_separate_from_contact_edits_and_can_be_ended(self):
        self.confirm()
        self.person.phone = '+79001234568'
        self.person.save()
        state = internal_state(self.person)
        self.assertEqual(state['confirmed'], ['9001234567'])
        self.assertEqual(state['missing'], ['9001234568'])
        self.confirm(enabled='0')
        row = InternalContactNumber.objects.get()
        self.assertEqual(row.ended_by, self.owner)
        self.assertIsNotNone(row.valid_to)

    def test_existing_tag_alone_does_not_reclassify_history(self):
        call = self.mapped_call()
        self.confirm()
        call.save()
        self.assertIsNone(call.internal_contact_number_id)

    def test_confirmed_number_excludes_autolead_and_analysis_without_rewriting_direction(self):
        self.confirm()
        InternalContactNumber.objects.update(valid_from=self.now - timedelta(days=1))
        call = self.mapped_call()
        self.assertIsNotNone(call.internal_contact_number_id)
        self.assertEqual(call.direction, PhoneCall.DIRECTION_IN)
        self.assertEqual(self.deliver('call').error_code, 'internal_ignored')
        self.assertFalse(CrmLead.objects.exists())
        self.assertFalse(can_read_call(self.user, call, media=True))
        self.assertTrue(can_read_call(self.owner, call, media=True))
        self.assertEqual(runtime_decision_for_call(call).reason, 'internal_contact')
        row = next(row for row in preview_rules(user=self.owner, organization=self.org)['rows'] if row['id'] == call.pk)
        self.assertFalse(row['selected'])
        self.assertIn('внутренний номер', row['reason'])
        self.confirm(enabled='0')
        call.save()
        self.assertIsNotNone(call.internal_contact_number_id)

    def test_director_can_review_and_confirm_from_contact(self):
        self.client.force_login(self.owner)
        url = reverse('client_detail', args=[self.person.pk])
        self.assertContains(self.client.get(url), 'Подтвердить номера')
        response = self.client.post(url, {'action': 'set_internal_call_numbers', 'enabled': '1',
            'internal_revision': internal_state(self.person)['revision']})
        self.assertEqual(response.status_code, 302)
        self.assertTrue(InternalContactNumber.objects.filter(contact=self.person).exists())

    def test_pbx_participant_resolution_preserves_confirmed_exclusion(self):
        from pool_service.models import Employee
        from pool_service.staff_call_models import EmployeeTelephoneAssignment
        from pool_service.services.call_ai import request_call_analysis
        self.confirm()
        since = self.now - timedelta(days=1)
        InternalContactNumber.objects.update(valid_from=since)
        self.megafon()
        peer = Employee.objects.create(organization=self.org, user=self.owner, display_name='Test peer')
        common = dict(organization=self.org, connection=self.telephony, confirmed_by=self.owner, valid_from=since)
        EmployeeTelephoneAssignment.objects.create(**common, employee=self.employee, extension='101')
        EmployeeTelephoneAssignment.objects.create(**common, employee=peer, phone_key='9001234567')
        self.assertEqual(self.history(ext='101', type='out').status_code, 200)
        call = PhoneCall.objects.get(connection=self.telephony)
        PhoneCall.objects.filter(pk=call.pk).update(recording_file='example.mp3')
        self.assertEqual(call.direction, 'internal')
        self.assertIsNotNone(call.internal_contact_number_id)
        self.assertEqual(runtime_decision_for_call(call).reason, 'internal_contact')
        self.assertFalse(request_call_analysis(call.pk))

    def test_undated_first_receipt_is_held_and_a_later_provider_date_can_clear_it(self):
        self.confirm()
        self.megafon()
        self.assertEqual(self.history(start='').status_code, 200)
        call = PhoneCall.objects.get(connection=self.telephony)
        self.assertTrue(call.internal_number_pending)
        self.assertEqual(self.deliver('call').error_code, 'classification_pending')
        before = self.now - timedelta(minutes=2)
        corrected_time = before.strftime('%Y%m%dT%H%M%SZ')
        for _ in range(2):
            response = self.history(start=corrected_time, phone='+79001234578')
            self.assertTrue(response.json()['ignored'])
        call.refresh_from_db()
        self.assertTrue(call.internal_number_pending)
        self.assertIsNone(call.intake_occurred_at)
        self.assertEqual(self.history(start=corrected_time).status_code, 200)
        call.refresh_from_db()
        self.assertIsNone(call.internal_contact_number_id)
        self.assertFalse(call.internal_number_pending)
        # The PBX timing hold has cleared. The contact's active directory
        # category independently keeps this call out of sales.
        self.assertEqual(self.deliver('call').error_code, 'contact_category_ignored')
        self.assertFalse(CrmLead.objects.exists())

    def test_replay_of_a_legacy_provider_call_without_intake_receipt(self):
        self.megafon()
        call = PhoneCall.objects.create(organization=self.org, connection=self.telephony,
            external_id=self.call_data['callid'], direction='in', phone_number=self.call_data['phone'],
            employee=self.user, employee_profile=self.employee, started_at=self.now)
        self.assertEqual(self.history(user='synthetic-provider', ext='101').status_code, 200)
        call.refresh_from_db()
        self.assertIsNone(call.intake_received_at)
        self.assertIsNone(call.undated_sales_head)
