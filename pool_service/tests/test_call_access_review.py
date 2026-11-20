from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone
from pool_service.call_access_review_views import ReviewForm
from pool_service.client_crm_models import ClientBusinessRole
from pool_service.communication_models import CommunicationAccess, PhoneCall, TelephonyConnection
from pool_service.crm_commercial_models import CrmAccess, CrmLead
from pool_service.models import Client, Employee, Organization, OrganizationAccess
from pool_service.services.call_processing_settings import add_private_numbers
from pool_service.staff_call_models import CallWorkContext


class CallAccessReviewTests(TestCase):
    def setUp(self):
        self.org = Organization.objects.create(name='Synthetic access review', crm_v2_read=True)
        self.owner = self.staff('owner')
        self.manager = self.staff('manager')
        self.external = TelephonyConnection.objects.create(organization=self.org, external_id='synthetic-tenant')
        self.number = '+79001234567'
        self.call = PhoneCall.objects.create(organization=self.org, connection=self.external, employee=self.owner,
            phone_number=self.number, direction='out', external_id='synthetic-call', started_at=timezone.now(),
            recording_file='test-private-path.mp3')
        self.url = reverse('communication_call_access_review', args=[self.org.pk])
        self.client.force_login(self.owner)

    def staff(self, role):
        user = get_user_model().objects.create_user('synthetic-report-' + role)
        OrganizationAccess.objects.create(organization=self.org, user=user, role=role)
        Employee.objects.create(organization=self.org, user=user, display_name=role)
        CrmAccess.objects.create(organization=self.org, user=user, view_leads=True, view_team_deals=True)
        CommunicationAccess.objects.filter(organization=self.org, user=user).update(can_view_all_calls=True, can_listen_calls=True)
        return user

    def report(self):
        return self.client.post(self.url, {'phone':self.number,'employee':self.manager.pk})

    def test_delegation_changes_report_without_serving_content(self):
        lead = CrmLead.objects.create(organization=self.org, assignee=self.owner, title='Unrelated sensitive title')
        CallWorkContext.objects.create(organization=self.org, call=self.call, lead=lead, changed_by=self.owner)
        report = self.report()
        self.assertFalse(report.context['results'][0]['audio_allowed'])
        self.assertNotContains(report, 'test-private-path')
        self.assertNotContains(report, lead.title)
        self.assertIn('no-store', report.headers['Cache-Control'])
        lead.assignee = self.manager
        lead.save()
        row = self.report().context['results'][0]
        self.assertTrue(row['audio_allowed'])
        self.assertTrue(row['file_exists'])
        self.assertTrue(row['cards'][0]['assigned'])
        add_private_numbers(user=self.owner, organization=self.org, label='Synthetic personal peer', numbers_text=self.number)
        self.assertEqual(self.report().context['results'], [])

    def test_lists_ambiguous_contacts_without_choosing_a_customer(self):
        for name in ('First synthetic person', 'Second synthetic person'):
            person = Client.objects.create(organization=self.org, name=name, phone=self.number, client_type='private')
            ClientBusinessRole.objects.create(client=person, role='internal', active=True)
        self.assertEqual(len(self.report().context['contacts']), 2)
        self.call.refresh_from_db()
        self.assertIsNone(self.call.client_id)

    def test_owner_and_organization_gates(self):
        foreign = get_user_model().objects.create_user('foreign-report-user')
        other = Organization.objects.create(name='Synthetic other')
        OrganizationAccess.objects.create(organization=other, user=foreign, role='manager')
        self.assertFalse(ReviewForm(self.org, {'phone':self.number,'employee':foreign.pk}).is_valid())
        self.assertEqual(self.client.get(reverse('communication_call_access_review', args=[other.pk])).status_code, 403)
        self.client.force_login(self.manager)
        self.assertEqual(self.report().status_code, 403)
