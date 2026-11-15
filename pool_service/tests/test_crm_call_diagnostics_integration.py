"""Only integration additions; the supplied diagnostics package is unchanged."""
from django.db import connection
from django.test import TestCase

from pool_service.client_crm_import import apply_candidate
from pool_service.client_crm_models import ClientCRMProfile, ClientImportCandidate, ClientImportProposal
from pool_service.communication_models import PhoneCall
from pool_service.models import Client, Organization
from . import test_crm_call_diagnostics as diagnostics_tests


class DiagnosticsIntegrationTests(TestCase):
    setUp = diagnostics_tests.CallDiagnosticsTests.setUp
    event = diagnostics_tests.CallDiagnosticsTests.event
    link = diagnostics_tests.CallDiagnosticsTests.link
    read = diagnostics_tests.CallDiagnosticsTests.read
    unavailable = diagnostics_tests.CallDiagnosticsTests.unavailable

    def test_crm_selected_contact_from_foreign_org_hides_link(self):
        event = self.event(state='done', processed_at=self.now)
        lead = self.link(event)
        foreign = Organization.objects.create(name='Synthetic foreign CRM contact tenant')
        contact = Client.objects.create(organization=foreign, name='PRIVATE-FOREIGN-CONTACT')
        with connection.cursor() as cursor:
            cursor.execute('UPDATE pool_service_crmlead SET contact_id=%s WHERE id=%s', [contact.pk, lead.pk])
        self.unavailable()

    def test_saved_call_diagnostic_does_not_apply_pending_onec_changes(self):
        customer = Client.objects.create(organization=self.org, name='PRIVATE-SAVED-CLIENT',
            phone='+12025550101', email='saved@example.test')
        profile = ClientCRMProfile.objects.create(client=customer,
            onec_ref='12121212-1111-2222-3333-444444444444', source='onec')
        candidate = ClientImportCandidate.objects.create(organization=self.org, source_ref=profile.onec_ref,
            name='PRIVATE-PROPOSED-CLIENT', phone='+12025550109', email='proposed@example.test',
            source_kind='private', status='imported', matched_client=customer, applied_at=self.now)
        apply_candidate(candidate)
        PhoneCall.objects.filter(pk=self.call.pk).update(client=customer)
        event = self.event(state='done', processed_at=self.now)
        lead = self.link(event)
        lead.client = customer; lead.contact = customer; lead.save()
        before = list(ClientImportProposal.objects.order_by('pk').values())
        self.assertTrue(self.read()['revisions'][0]['lead_linked'])
        self.assertEqual(list(ClientImportProposal.objects.order_by('pk').values()), before)
        customer.refresh_from_db()
        self.assertEqual((customer.name, customer.phone, customer.email),
            ('PRIVATE-SAVED-CLIENT', '+12025550101', 'saved@example.test'))
