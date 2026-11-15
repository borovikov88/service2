"""Synthetic integration contract for 1C review and explicit CRM contacts."""
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Event
from unittest import skipUnless

from django.core.exceptions import ValidationError
from django.db import connection, connections, transaction
from django.test import TestCase, TransactionTestCase
from django.utils import timezone

from pool_service.client_crm_import import apply_candidate
from pool_service.client_crm_models import ClientCRMProfile, ClientCompanyLink, ClientImportCandidate, ClientImportProposal
from pool_service.client_import_review import decide, token
from pool_service.crm_commercial_models import CrmDeal, CrmLead
from pool_service.models import Client, Organization, OrganizationAccess
from .test_crm_lead_commands import LeadFixture


class IntegrationFixture(LeadFixture):
    def setUp(self):
        super().setUp()
        OrganizationAccess.objects.filter(user=self.user, organization=self.org).update(role='owner')
        self.grant.view_team_deals = True
        self.grant.save(update_fields=['view_team_deals'])
        self.org.paid_until = timezone.now() + timedelta(days=30)
        self.org.save(update_fields=['paid_until'])
        self.profile = ClientCRMProfile.objects.create(client=self.client,
            onec_ref='66666666-1111-2222-3333-444444444444', source='onec')

    def stage(self, **changes):
        values = dict(organization=self.org, source_ref=self.profile.onec_ref,
            source_kind='private', name=self.client.name, status='imported',
            matched_client=self.client, applied_at=timezone.now())
        values.update(changes)
        candidate = ClientImportCandidate.objects.create(**values)
        apply_candidate(candidate)
        return candidate

    def accept(self, proposal):
        return decide(actor=self.user, organization_id=self.org.pk, proposal_id=proposal.pk,
            decision='accepted', expected_token=token(proposal))


class OnecCrmIntegrationTests(IntegrationFixture, TestCase):
    def test_company_pair_without_link_blocks_type_accept_and_reparent(self):
        from django.forms import modelform_factory
        company = Client.objects.create(organization=self.org, client_type='legal',
            name='Synthetic standalone company')
        profile = ClientCRMProfile.objects.create(client=company, source='onec',
            onec_ref='77777777-1111-2222-3333-444444444444')
        lead = self.create(client_id=company.pk, contact_id=self.client.pk)
        self.assertFalse(ClientCompanyLink.objects.exists())
        candidate = ClientImportCandidate.objects.create(organization=self.org,
            source_ref=profile.onec_ref, source_kind='private', name='Synthetic proposed person',
            status='imported', matched_client=company, applied_at=timezone.now())
        apply_candidate(candidate)
        proposal = ClientImportProposal.objects.get(candidate=candidate, field='classification')
        with self.assertRaises(ValidationError):
            self.accept(proposal)
        company.refresh_from_db(); lead.refresh_from_db(); proposal.refresh_from_db()
        self.assertEqual(company.client_type, 'legal')
        self.assertEqual((lead.client_id, lead.contact_id), (company.pk, self.client.pk))
        self.assertEqual(proposal.state, 'pending')
        lead.full_clean()
        foreign = Organization.objects.create(name='Synthetic other scope')
        form_class = modelform_factory(Client, fields=['client_type', 'name', 'organization'])
        for kind, org_id, error in [('private', self.org.pk, 'client_type'),
                ('legal', foreign.pk, 'organization')]:
            company.refresh_from_db()
            form = form_class({'client_type': kind, 'name': company.name, 'organization': org_id}, instance=company)
            self.assertFalse(form.is_valid()); self.assertIn(error, form.errors)

    def test_deal_company_pair_remains_protected_after_manual_link_removed(self):
        lead = self.create()
        result = self.command('lead.qualify', {'deals': [self.deal(customer={
            'contact': {'id': self.client.pk}, 'company': {'name': 'Synthetic standalone deal company'}})]}, lead)
        deal = CrmDeal.objects.get(pk=result['deal_ids'][0])
        company = deal.client
        ClientCompanyLink.objects.filter(company=company).delete()
        lead.refresh_from_db(); lead.contact = None; lead.save(update_fields=['contact'])
        profile = ClientCRMProfile.objects.create(client=company, source='onec',
            onec_ref='88888888-1111-2222-3333-444444444444')
        candidate = ClientImportCandidate.objects.create(organization=self.org,
            source_ref=profile.onec_ref, source_kind='private', name='Synthetic proposed person',
            status='imported', matched_client=company, applied_at=timezone.now())
        apply_candidate(candidate)
        with self.assertRaises(ValidationError):
            self.accept(ClientImportProposal.objects.get(candidate=candidate, field='classification'))
        company.refresh_from_db(); deal.refresh_from_db()
        self.assertEqual(company.client_type, 'legal')
        self.assertEqual((deal.client_id, deal.contact_id), (company.pk, self.client.pk))
        deal.full_clean()

    def test_selected_pair_survives_import_blank_source_and_field_accept(self):
        self.client.phone = '+12025550101'
        self.client.save(update_fields=['phone'])
        lead = self.create(client_id=self.client.pk)
        result = self.command('lead.qualify', {'deals': [self.deal(customer={
            'contact': {'id': self.client.pk}, 'company': {'name': 'Synthetic explicit company'}})]}, lead)
        deal = CrmDeal.objects.get(pk=result['deal_ids'][0])
        links = list(ClientCompanyLink.objects.order_by('pk').values())
        candidate = self.stage(phone='+12025550102')
        self.client.refresh_from_db()
        self.assertEqual(self.client.phone, '+12025550101')
        proposal = ClientImportProposal.objects.get(candidate=candidate, field='phone')
        self.accept(proposal)
        candidate.phone = ''; candidate.name = ''; candidate.payload = {}
        candidate.save(update_fields=['phone', 'name', 'payload'])
        apply_candidate(candidate)
        self.client.refresh_from_db(); lead.refresh_from_db(); deal.refresh_from_db()
        self.assertEqual(self.client.phone, '+12025550102')
        self.assertEqual((lead.contact_id, deal.contact_id), (self.client.pk, self.client.pk))
        self.assertEqual(lead.client_id, deal.client_id)
        self.assertEqual(list(ClientCompanyLink.objects.order_by('pk').values()), links)

    def test_selected_deal_contact_blocks_type_accept_even_without_lead_contact(self):
        candidate = self.stage(source_kind='legal', name='Synthetic proposed company')
        proposal = ClientImportProposal.objects.get(candidate=candidate, field='classification')
        lead = self.create()
        result = self.command('lead.qualify', {'deals': [self.deal(customer={
            'contact': {'id': self.client.pk}})]}, lead)
        # A deal remains a consumer even when a historical lead has no contact.
        lead.refresh_from_db()
        lead.contact = None
        lead.save(update_fields=['contact'])
        with self.assertRaises(ValidationError):
            self.accept(proposal)
        self.client.refresh_from_db(); proposal.refresh_from_db()
        self.assertEqual(self.client.client_type, 'private')
        self.assertEqual(proposal.state, 'pending')
        self.assertEqual(CrmDeal.objects.get(pk=result['deal_ids'][0]).contact_id, self.client.pk)


@skipUnless(connection.vendor == 'mysql', 'Requires MySQL/InnoDB row locks')
class OnecCrmIntegrationConcurrencyTests(IntegrationFixture, TransactionTestCase):
    def worker(self, function):
        connections.close_all()
        try:
            return function()
        finally:
            connections.close_all()

    def test_selected_contact_commits_before_waiting_type_accept(self):
        candidate = self.stage(source_kind='legal', name='Synthetic proposed company')
        proposal = ClientImportProposal.objects.get(candidate=candidate, field='classification')
        started = Event()
        def accept():
            started.set()
            try:
                self.accept(proposal)
            except ValidationError:
                return 'blocked'
            return 'accepted'
        with ThreadPoolExecutor(max_workers=1) as pool:
            with transaction.atomic():
                Organization.objects.select_for_update().get(pk=self.org.pk)
                future = pool.submit(self.worker, accept)
                self.assertTrue(started.wait(3)); self.assertFalse(future.done())
                lead = self.create(client_id=self.client.pk, contact_id=self.client.pk)
            self.assertEqual(future.result(timeout=15), 'blocked')
        self.client.refresh_from_db(); lead.refresh_from_db()
        self.assertEqual(self.client.client_type, 'private')
        self.assertEqual(lead.contact_id, self.client.pk)

    def test_type_accept_commits_before_waiting_contact_selection(self):
        candidate = self.stage(source_kind='legal', name='Synthetic proposed company')
        proposal = ClientImportProposal.objects.get(candidate=candidate, field='classification')
        started = Event()
        def select():
            started.set()
            try:
                self.create(client_id=self.client.pk, contact_id=self.client.pk)
            except ValidationError:
                return 'blocked'
            return 'selected'
        with ThreadPoolExecutor(max_workers=1) as pool:
            with transaction.atomic():
                Organization.objects.select_for_update().get(pk=self.org.pk)
                self.accept(proposal)
                future = pool.submit(self.worker, select)
                self.assertTrue(started.wait(3)); self.assertFalse(future.done())
            self.assertEqual(future.result(timeout=15), 'blocked')
        self.client.refresh_from_db()
        self.assertEqual(self.client.client_type, 'legal')
        self.assertFalse(CrmLead.objects.exists())
