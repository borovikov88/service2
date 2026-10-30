"""Synthetic conversion contact and preserved next-action regressions."""
from django.core.exceptions import PermissionDenied, ValidationError
from django.test import TestCase
from pool_service.tests.test_crm_lead_commands import LeadFixture
from pool_service.models import Client, ServiceTask
from pool_service.client_crm_models import ClientCompanyLink
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
