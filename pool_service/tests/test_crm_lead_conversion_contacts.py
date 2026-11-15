"""Synthetic conversion contact and preserved next-action regressions."""
from datetime import timedelta
from django.core.exceptions import PermissionDenied, ValidationError
from django.test import TestCase, Client as HttpClient
from django.db import connection
from django.urls import reverse
from django.utils import timezone
from pool_service.tests.test_crm_lead_commands import LeadFixture
from pool_service.models import Client, Organization, OrganizationAccess, ServiceTask
from pool_service.client_crm_models import ClientBusinessRole, ClientBusinessRoleEvent, ClientCompanyLink, ClientContact, ClientCRMProfile, ClientImportCandidate
from pool_service.crm_commercial_models import CrmDeal, CrmDealTaskLink, CrmLeadTaskLink
from pool_service.services.task_feedback import version_for


class ConversionContactsTests(LeadFixture, TestCase):
    def contact(self, **changes):
        values=dict(type='private',person_name='Synthetic new person',company_name='',phone='+79990001122',inn='')
        values.update(changes)
        return values

    def test_person_company_atomic_replay_preserves_open_task(self):
        lead=self.create();task=lead.next_task;revision=lead.revision;before_tasks=ServiceTask.objects.count()
        changes={'expected_task_version':version_for(task),'deals':[self.deal(new_contact=self.contact(type='both',company_name='Synthetic new company'),reuse_next_action=True,next_action=self.action())]}
        result=self.command('lead.qualify',changes,lead,key='conversion-preserve')
        lead.refresh_from_db();task.refresh_from_db();deal=CrmDeal.objects.get(pk=result['deal_ids'][0])
        self.assertEqual((lead.status,lead.result,lead.next_task_id),('closed','qualified',None))
        self.assertEqual(deal.next_action.task_link.task_id,task.pk)
        self.assertEqual(ServiceTask.objects.count(),before_tasks)
        self.assertEqual(task.title,'Clarify synthetic need')
        self.assertEqual(task.client_id,deal.client_id)
        self.assertTrue(ClientCompanyLink.objects.filter(company_id=deal.client_id).exists())
        CrmLeadTaskLink.objects.get(lead=lead,task=task).full_clean()
        CrmDealTaskLink.objects.get(deal=deal,task=task).full_clean()
        from pool_service.crm_commercial_models import _confirmed_task_transfer
        self.assertTrue(_confirmed_task_transfer('default',task.pk,deal.pk,self.org.pk))
        self.assertFalse(_confirmed_task_transfer('default',task.pk+10000,deal.pk,self.org.pk))
        self.assertFalse(_confirmed_task_transfer('default',task.pk,deal.pk,self.org.pk+10000))
        self.assertFalse(_confirmed_task_transfer('default',task.pk,deal.pk+10000,self.org.pk))
        before=Client.objects.count()
        self.assertEqual(self.command('lead.qualify',changes,lead,key='conversion-preserve',expected_revision=revision),result)
        self.assertEqual(Client.objects.count(),before)

    def test_duplicate_name_rejected_without_partial_deal_or_contact(self):
        lead=self.create();before=Client.objects.count()
        with self.assertRaises(ValidationError):
            self.command('lead.qualify',{'deals':[self.deal(new_contact=self.contact(person_name=self.client.name))]},lead)
        self.assertEqual(Client.objects.count(),before);self.assertFalse(CrmDeal.objects.exists())
        lead.refresh_from_db();self.assertEqual(lead.status,'working')

    def test_invalid_second_row_rolls_back_new_contact(self):
        lead=self.create();before=Client.objects.count()
        with self.assertRaises(PermissionDenied):
            self.command('lead.qualify',{'deals':[self.deal(new_contact=self.contact()),self.deal(client_id=999999)]},lead)
        self.assertEqual(Client.objects.count(),before);self.assertFalse(CrmDeal.objects.exists())

    def test_transfer_requires_current_task_version(self):
        lead=self.create();before=self.state()
        with self.assertRaises(ValidationError):
            self.command('lead.qualify',{'deals':[self.deal(client_id=self.client.pk,reuse_next_action=True,next_action=self.action())]},lead)
        self.assertEqual(before,self.state())

    def test_formatted_legacy_phone_blocks_new_identity(self):
        self.client.phone='+7 (999) 000-11-22';self.client.save(update_fields=['phone'])
        lead=self.create();before=Client.objects.count()
        with self.assertRaises(ValidationError):
            self.command('lead.qualify',{'deals':[self.deal(new_contact=self.contact(phone='89990001122'))]},lead)
        self.assertEqual(Client.objects.count(),before)

    def test_revoked_right_and_waiting_task_prevent_transfer(self):
        from pool_service.services.crm_commercial_commands import CommercialConflict
        lead=self.create();task=lead.next_task
        changes={'expected_task_version':version_for(task),'deals':[self.deal(new_contact=self.contact(),reuse_next_action=True,next_action=self.action())]}
        self.grant.qualify_own_leads=False;self.grant.save()
        with self.assertRaises(PermissionDenied):self.command('lead.qualify',changes,lead)
        self.grant.qualify_own_leads=True;self.grant.save()
        task.status=ServiceTask.STATUS_WAITING;task.save(update_fields=['status'])
        changes['expected_task_version']=version_for(task)
        with self.assertRaises((ValidationError,CommercialConflict)):self.command('lead.qualify',changes,lead)
        self.assertFalse(CrmDeal.objects.exists());self.assertEqual(Client.objects.count(),1)

    def test_existing_person_and_new_company_remain_linked_on_lead_deal_and_replay(self):
        lead = self.create(client_id=self.client.pk)
        revision = lead.revision
        changes = {'deals': [self.deal(customer={'contact': {'id': self.client.pk},
            'company': {'name': 'Synthetic selected company', 'inn': '1234567890'}})]}
        result = self.command('lead.qualify', changes, lead, key='independent-customer')
        lead.refresh_from_db()
        deal = CrmDeal.objects.get(pk=result['deal_ids'][0])
        self.assertEqual((lead.client_id, lead.contact_id), (deal.client_id, self.client.pk))
        self.assertEqual(deal.contact_id, self.client.pk)
        self.assertEqual(deal.client.client_type, 'legal')
        self.assertTrue(ClientCompanyLink.objects.filter(company=deal.client, person=self.client).exists())
        before = (Client.objects.count(), ClientCompanyLink.objects.count(), CrmDeal.objects.count())
        self.assertEqual(self.command('lead.qualify', changes, lead, key='independent-customer', expected_revision=revision), result)
        self.assertEqual(before, (Client.objects.count(), ClientCompanyLink.objects.count(), CrmDeal.objects.count()))
        http = HttpClient(); http.force_login(self.user)
        for route, record in [('crm_live_lead_detail', lead), ('crm_live_detail', deal)]:
            response = http.get(reverse(route, args=[record.pk]), {'org_id': self.org.pk})
            self.assertEqual(response.status_code, 200, response.content)
            data = response.json()['lead' if route == 'crm_live_lead_detail' else 'deal']
            self.assertEqual(data['contact']['id'], self.client.pk)
            self.assertEqual(data['company']['id'], deal.client_id)

    def test_new_categorized_person_with_existing_company_preserves_person_phone_email(self):
        OrganizationAccess.objects.filter(organization=self.org, user=self.user).update(role='owner')
        self.grant.view_team_deals = True; self.grant.save(update_fields=['view_team_deals'])
        company = Client.objects.create(organization=self.org, client_type='legal', name='Synthetic existing company')
        lead = self.create()
        result = self.command('lead.qualify', {'deals': [self.deal(customer={
            'contact': {'name': 'Synthetic landlord', 'phone': '+79990001122', 'email': 'landlord@example.test', 'category': 'landlord'},
            'company': {'id': company.pk}})]}, lead)
        deal = CrmDeal.objects.get(pk=result['deal_ids'][0])
        self.assertEqual(deal.client_id, company.pk)
        self.assertNotEqual(deal.contact_id, company.pk)
        self.assertEqual(deal.contact.email, 'landlord@example.test')
        self.assertTrue(ClientContact.objects.filter(client_id=deal.contact_id, kind='phone', match_value='9990001122').exists())
        self.assertTrue(ClientBusinessRole.objects.filter(client_id=deal.contact_id, role='landlord', active=True).exists())
        self.assertEqual(ClientBusinessRoleEvent.objects.get(client_id=deal.contact_id).actor_id, self.user.pk)

    def test_company_is_optional_and_legacy_both_keeps_explicit_person(self):
        lead = self.create()
        result = self.command('lead.qualify', {'deals': [self.deal(customer={'contact': {'id': self.client.pk}, 'company': None})]}, lead)
        deal = CrmDeal.objects.get(pk=result['deal_ids'][0])
        self.assertEqual((deal.client_id, deal.contact_id), (self.client.pk, self.client.pk))
        another = self.create()
        result = self.command('lead.qualify', {'deals': [self.deal(new_contact=self.contact(type='both', company_name='Synthetic legacy company'))]}, another)
        deal = CrmDeal.objects.get(pk=result['deal_ids'][0])
        self.assertEqual(deal.client.client_type, 'legal')
        self.assertEqual(deal.contact.client_type, 'private')
        self.assertTrue(ClientCompanyLink.objects.filter(company=deal.client, person=deal.contact).exists())

    def test_customer_selection_rejects_foreign_or_wrong_type_without_writes(self):
        foreign = Organization.objects.create(name='Synthetic foreign contact organization')
        hidden = Client.objects.create(organization=foreign, client_type='private', name='Synthetic foreign person')
        company = Client.objects.create(organization=self.org, client_type='legal', name='Synthetic company')
        lead = self.create()
        before = self.state()
        for customer in ({'contact': {'id': hidden.pk}}, {'contact': {'id': company.pk}},
                {'contact': {'id': self.client.pk}, 'company': {'id': hidden.pk}},
                {'company': {'id': self.client.pk}}):
            with self.subTest(customer=customer), self.assertRaises(PermissionDenied):
                self.command('lead.qualify', {'deals': [self.deal(customer=customer)]}, lead)
            self.assertEqual(before, self.state())

    def test_failed_company_selection_rolls_back_new_contact_category_and_all_links(self):
        OrganizationAccess.objects.filter(organization=self.org, user=self.user).update(role='owner')
        self.grant.view_team_deals = True; self.grant.save(update_fields=['view_team_deals'])
        lead = self.create()
        before = (Client.objects.count(), ClientBusinessRole.objects.count(), ClientBusinessRoleEvent.objects.count(), self.state())
        with self.assertRaises(PermissionDenied):
            self.command('lead.qualify', {'deals': [self.deal(customer={
                'contact': {'name': 'Synthetic rollback person', 'category': 'personal'},
                'company': {'id': 999999}})]}, lead)
        self.assertEqual(before, (Client.objects.count(), ClientBusinessRole.objects.count(), ClientBusinessRoleEvent.objects.count(), self.state()))

    def test_manager_can_create_plain_contact_but_cannot_classify_it(self):
        lead = self.create()
        before = (Client.objects.count(), self.state())
        with self.assertRaises(PermissionDenied):
            self.command('lead.qualify', {'deals': [self.deal(customer={
                'contact': {'name': 'Synthetic restricted category', 'category': 'supplier'}})]}, lead)
        self.assertEqual(before, (Client.objects.count(), self.state()))
        self.assertFalse(ClientBusinessRole.objects.exists())
        self.assertFalse(ClientBusinessRoleEvent.objects.exists())
        result = self.command('lead.qualify', {'deals': [self.deal(customer={
            'contact': {'name': 'Synthetic plain contact'}})]}, lead)
        self.assertEqual(CrmDeal.objects.get(pk=result['deal_ids'][0]).contact.name, 'Synthetic plain contact')

    def test_customer_payload_cannot_change_existing_identity_or_mix_legacy_selection(self):
        lead = self.create()
        for customer in ({}, {'contact': {'id': True}}, {'contact': {'id': self.client.pk, 'name': 'Changed name'}},
                {'contact': {'name': 'Synthetic invalid email', 'email': 'not-email'}},
                {'contact': {'name': 'Synthetic unknown category', 'category': 'unknown'}}):
            with self.subTest(customer=customer), self.assertRaises(ValidationError):
                self.command('lead.qualify', {'deals': [self.deal(customer=customer)]}, lead)
        with self.assertRaises(ValidationError):
            self.command('lead.qualify', {'deals': [self.deal(client_id=self.client.pk, customer={'contact': {'id': self.client.pk}})]}, lead)
        self.assertFalse(CrmDeal.objects.exists())

    def test_lead_edit_keeps_selected_person_and_rejects_cross_tenant_contacts(self):
        company = Client.objects.create(organization=self.org, client_type='legal', name='Synthetic lead company')
        lead = self.create(client_id=company.pk, contact_id=self.client.pk)
        self.assertEqual((lead.client_id, lead.contact_id), (company.pk, self.client.pk))
        self.command('lead.edit', {'title': 'Synthetic edited lead'}, lead)
        lead.refresh_from_db()
        self.assertEqual(lead.contact_id, self.client.pk)
        foreign = Organization.objects.create(name='Synthetic foreign contact tenant')
        hidden = Client.objects.create(organization=foreign, name='Synthetic unavailable contact')
        with self.assertRaises(PermissionDenied):
            self.command('lead.edit', {'contact_id': hidden.pk}, lead)
        with self.assertRaises(ValidationError):
            self.command('lead.edit', {'contact_id': company.pk}, lead)
        self.command('lead.edit', {'client_id': self.client.pk}, lead)
        lead.refresh_from_db()
        self.assertEqual((lead.client_id, lead.contact_id), (self.client.pk, self.client.pk))

    def test_corrupt_contact_scope_is_hidden_before_list_and_detail_serialization(self):
        lead = self.create(client_id=self.client.pk)
        result = self.command('lead.qualify', {'deals': [self.deal(customer={'contact': {'id': self.client.pk}})]}, lead)
        foreign = Organization.objects.create(name='Synthetic foreign hidden tenant')
        hidden = Client.objects.create(organization=foreign, name='Synthetic secret contact')
        # Raw SQL models an inconsistent historical row, outside validated ORM writes.
        with connection.cursor() as cursor:
            cursor.execute('UPDATE pool_service_crmlead SET contact_id = %s WHERE id = %s', [hidden.pk, lead.pk])
            cursor.execute('UPDATE pool_service_crmdeal SET contact_id = %s WHERE id = %s', [hidden.pk, result['deal_ids'][0]])
        http = HttpClient(); http.force_login(self.user)
        for route, record_id in [('crm_live_lead_detail', lead.pk), ('crm_live_detail', result['deal_ids'][0])]:
            response = http.get(reverse(route, args=[record_id]), {'org_id': self.org.pk})
            self.assertEqual(response.status_code, 404)
            self.assertNotContains(response, hidden.name, status_code=404)
        for route in ('crm_live_lead_data', 'crm_live_data'):
            response = http.get(reverse(route), {'org_id': self.org.pk})
            self.assertEqual(response.status_code, 200)
            self.assertNotContains(response, hidden.name)

    def test_contact_lookup_filters_type_and_matches_formatted_phone(self):
        self.org.plan_type = Organization.PLAN_COMPANY_PAID
        self.org.paid_until = timezone.now() + timedelta(days=30)
        self.org.save(update_fields=['plan_type', 'paid_until'])
        self.client.phone = '+7 (999) 000-11-22'; self.client.save(update_fields=['phone'])
        ClientBusinessRole.objects.create(client=self.client, role='personal', active=True)
        Client.objects.create(organization=self.org, client_type='legal', name='Synthetic search company')
        http = HttpClient(); http.force_login(self.user)
        response = http.get(reverse('crm_live_lead_clients'), {'org_id': self.org.pk, 'kind': 'private', 'q': '89990001122'})
        self.assertEqual(response.status_code, 200)
        self.assertEqual([row['id'] for row in response.json()['clients']], [self.client.pk])
        self.assertEqual(response.json()['clients'][0]['categories'], ['personal'])
        response = http.get(reverse('crm_live_lead_clients'), {'org_id': self.org.pk, 'kind': 'legal', 'q': 'Synthetic'})
        self.assertTrue(all(row['client_type'] == 'legal' for row in response.json()['clients']))

    def test_model_form_preserves_type_and_organization_of_used_contact(self):
        from django.forms import modelform_factory
        lead = self.create(client_id=self.client.pk, contact_id=self.client.pk)
        form_type = modelform_factory(Client, fields=['client_type', 'name', 'organization'])
        data = {'client_type': 'legal', 'name': self.client.name, 'organization': self.org.pk}
        form = form_type(data, instance=self.client)
        self.assertFalse(form.is_valid())
        self.assertIn('client_type', form.errors)
        self.client.refresh_from_db()
        self.assertEqual(self.client.client_type, 'private')
        foreign = Organization.objects.create(name='Synthetic reparent destination')
        form = form_type({**data, 'client_type': 'private', 'organization': foreign.pk}, instance=self.client)
        self.assertFalse(form.is_valid())
        self.assertIn('organization', form.errors)
        lead.refresh_from_db()
        self.assertEqual(lead.contact_id, self.client.pk)

    def test_long_formatted_international_phone_is_rejected_before_writes(self):
        lead = self.create()
        before = (Client.objects.count(), self.state())
        with self.assertRaises(ValidationError):
            self.command('lead.qualify', {'deals': [self.deal(customer={'contact': {
                'name': 'Synthetic long phone', 'phone': '+44 (20) 1234 5678 90 123'}})]}, lead)
        self.assertEqual(before, (Client.objects.count(), self.state()))

    def test_onec_type_refresh_requires_review_without_changing_crm_contact(self):
        from pool_service.client_crm_import import apply_ready_candidates
        source_ref = '66666666-1111-2222-3333-444444444444'
        ClientCRMProfile.objects.create(client=self.client, onec_ref=source_ref, onec_name=self.client.name)
        lead = self.create(client_id=self.client.pk, contact_id=self.client.pk)
        candidate = ClientImportCandidate.objects.create(organization=self.org, source_ref=source_ref,
            source_kind='legal', name='Synthetic refreshed company', status='ready', matched_client=self.client)
        result = apply_ready_candidates(self.org, candidate_ids=[candidate.pk])
        self.assertEqual(result, {'imported': 0, 'failed': 1})
        candidate.refresh_from_db(); self.client.refresh_from_db(); lead.refresh_from_db()
        self.assertEqual(candidate.status, ClientImportCandidate.STATUS_REVIEW)
        self.assertEqual(self.client.client_type, 'private')
        self.assertEqual(lead.contact_id, self.client.pk)
        self.assertEqual(Client.objects.count(), 1)
