"""Customer-only completion is atomic, scoped, replayable and creates no sale."""
from datetime import timedelta
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied, ValidationError
from django.test import Client as HttpClient, TestCase
from django.urls import reverse
from django.utils import timezone

from pool_service.client_crm_models import ClientBusinessRole, ClientBusinessRoleEvent, ClientCompanyLink, ClientContact
from pool_service.crm_commercial_models import CrmDeal, CrmEvent, CrmLead, CrmLeadDealLink, CrmLeadTaskLink, CrmLeadThread
from pool_service.models import Client, Organization, OrganizationAccess, ServiceTask, ServiceTaskChange
from pool_service.services.crm_commercial_commands import CommercialConflict
from pool_service.services.task_feedback import version_for
from pool_service.tests.test_crm_lead_commands import LeadFixture


class LeadCustomerCompletionTests(LeadFixture, TestCase):
    def setUp(self):
        super().setUp()
        self.org.plan_type = Organization.PLAN_COMPANY_PAID
        self.org.paid_until = timezone.now() + timedelta(days=30)
        self.org.save(update_fields=["plan_type", "paid_until"])

    def completion(self, lead, customer=None):
        values = {"customer": customer if customer is not None else {"contact": {"id": self.client.pk}}}
        if lead.next_task_id:
            values["expected_task_version"] = version_for(lead.next_task)
        return values

    def snapshot(self):
        return self.state() + [list(model.objects.order_by("pk").values()) for model in
            (Client, ClientContact, ClientCompanyLink, ClientBusinessRole, ClientBusinessRoleEvent, ServiceTaskChange)]

    def test_both_new_identities_complete_without_deal_and_preserve_task_history(self):
        lead = self.create(client_id=self.client.pk, need="")
        task = lead.next_task
        CrmLeadThread.objects.create(organization=self.org, lead=lead, context_key="b" * 64)
        before_tasks = ServiceTask.objects.count()
        changes = self.completion(lead, {
            "contact": {"name": "Synthetic saved person", "phone": "+79990001122", "email": "saved@example.test"},
            "company": {"name": "Synthetic saved company", "inn": "1234567890"},
        })
        result = self.command("lead.complete_customer", changes, lead)
        lead.refresh_from_db(); task.refresh_from_db()
        self.assertEqual((lead.status, lead.result, lead.need, lead.next_task_id), ("closed", "customer_saved", "", None))
        self.assertEqual((lead.client.client_type, lead.contact.client_type), ("legal", "private"))
        self.assertTrue(ClientCompanyLink.objects.filter(company=lead.client, person=lead.contact, automatic=False).exists())
        self.assertEqual(lead.contact.email, "saved@example.test")
        self.assertTrue(ClientContact.objects.filter(client=lead.contact, kind="phone", sources=["manual"]).exists())
        self.assertEqual(result["task_id"], task.pk)
        self.assertEqual(task.client_id, self.client.pk)
        self.assertEqual(task.status, ServiceTask.STATUS_CANCELLED)
        self.assertTrue(task.is_archived)
        self.assertIsNone(task.completed_at)
        CrmLeadTaskLink.objects.get(lead=lead, task=task).full_clean()
        self.assertTrue(ServiceTaskChange.objects.filter(task=task, field_name="crm_lead_resolved").exists())
        self.assertTrue(CrmLeadThread.objects.filter(lead=lead).exists())
        self.assertEqual(ServiceTask.objects.count(), before_tasks)
        self.assertFalse(CrmDeal.objects.exists())
        self.assertFalse(CrmLeadDealLink.objects.exists())
        event = CrmEvent.objects.get(pk=result["event_id"])
        self.assertEqual(event.safe_diff["result"], {"before": "", "after": "customer_saved"})
        self.assertEqual(event.reason, lead.reason)

    def test_existing_contact_only_or_company_only(self):
        company = Client.objects.create(organization=self.org, client_type="legal", name="Synthetic company only")
        for customer, primary, person in (
            ({"contact": {"id": self.client.pk}, "company": None}, self.client.pk, self.client.pk),
            ({"contact": None, "company": {"id": company.pk}}, company.pk, None),
        ):
            with self.subTest(customer=customer):
                lead = self.create()
                self.command("lead.complete_customer", self.completion(lead, customer), lead)
                lead.refresh_from_db()
                self.assertEqual((lead.client_id, lead.contact_id), (primary, person))
        self.assertEqual(Client.objects.count(), 2)
        self.assertFalse(CrmDeal.objects.exists())

    def test_existing_company_and_new_categorized_contact(self):
        OrganizationAccess.objects.filter(user=self.user, organization=self.org).update(role="owner")
        company = Client.objects.create(organization=self.org, client_type="legal", name="Synthetic landlord company")
        lead = self.create()
        self.command("lead.complete_customer", self.completion(lead, {
            "contact": {"name": "Synthetic landlord", "category": "landlord"},
            "company": {"id": company.pk},
        }), lead)
        lead.refresh_from_db()
        self.assertEqual(lead.client_id, company.pk)
        self.assertTrue(ClientBusinessRole.objects.filter(client_id=lead.contact_id, role="landlord", active=True).exists())
        self.assertEqual(ClientBusinessRoleEvent.objects.get(client_id=lead.contact_id).actor_id, self.user.pk)

    def test_no_identity_or_unexpected_fields_are_rejected_without_writes(self):
        lead = self.create(); before = self.snapshot()
        for customer in (None, {}, {"contact": None, "company": None}, {"contact": {"id": self.client.pk, "name": "Spoof"}}):
            with self.subTest(customer=customer), self.assertRaises(ValidationError):
                self.command("lead.complete_customer", {"customer": customer}, lead)
            self.assertEqual(before, self.snapshot())
        for extra in ({"deals": []}, {"next_action": self.action()}, {"need": "Unexpected change"}):
            with self.subTest(extra=extra), self.assertRaises(ValidationError):
                self.command("lead.complete_customer", {**self.completion(lead), **extra}, lead)
            self.assertEqual(before, self.snapshot())

    def test_company_failure_rolls_back_new_person_category_and_task(self):
        OrganizationAccess.objects.filter(user=self.user, organization=self.org).update(role="owner")
        lead = self.create(); before = self.snapshot()
        for company in ({"id": 999999}, {"name": "Synthetic duplicate company", "phone": "+79990001122"}):
            with self.subTest(company=company), self.assertRaises((PermissionDenied, ValidationError)):
                self.command("lead.complete_customer", self.completion(lead, {
                    "contact": {"name": "Synthetic rollback person", "phone": "+79990001122", "category": "personal"},
                    "company": company,
                }), lead)
            self.assertEqual(before, self.snapshot())

    def test_cross_organization_and_wrong_identity_kind_are_rejected(self):
        foreign = Organization.objects.create(name="Synthetic foreign completion organization")
        hidden = Client.objects.create(organization=foreign, name="Synthetic foreign identity")
        company = Client.objects.create(organization=self.org, client_type="legal", name="Synthetic wrong type")
        lead = self.create(); before = self.snapshot()
        for customer in ({"contact": {"id": hidden.pk}}, {"company": {"id": hidden.pk}},
                         {"contact": {"id": company.pk}}, {"company": {"id": self.client.pk}}):
            with self.subTest(customer=customer), self.assertRaises(PermissionDenied):
                self.command("lead.complete_customer", self.completion(lead, customer), lead)
            self.assertEqual(before, self.snapshot())

    def test_exact_replay_is_idempotent_and_changed_payload_conflicts(self):
        lead = self.create(); revision = lead.revision
        changes = self.completion(lead, {"company": {"name": "Synthetic replay company"}})
        result = self.command("lead.complete_customer", changes, lead, key="customer-completion-replay")
        before = self.snapshot()
        self.assertEqual(self.command("lead.complete_customer", changes, lead,
            key="customer-completion-replay", expected_revision=revision), result)
        self.assertEqual(before, self.snapshot())
        with self.assertRaises(CommercialConflict):
            self.command("lead.complete_customer", {**changes, "reason": "Changed reason"}, lead,
                key="customer-completion-replay", expected_revision=revision)
        self.assertEqual(before, self.snapshot())

    def test_closed_archived_and_stale_leads_cannot_complete_again(self):
        lead = self.create()
        with self.assertRaises(CommercialConflict):
            self.command("lead.complete_customer", self.completion(lead), lead, expected_revision=0)
        lead.is_archived = True; lead.save()
        with self.assertRaises(ValidationError):
            self.command("lead.complete_customer", self.completion(lead), lead)
        lead.is_archived = False; lead.save()
        self.command("lead.complete_customer", self.completion(lead), lead)
        lead.refresh_from_db(); before = self.snapshot()
        with self.assertRaises(ValidationError):
            self.command("lead.complete_customer", self.completion(lead), lead)
        with self.assertRaises(ValidationError):
            self.command("lead.add_deal", {"deals": [self.deal()]}, lead)
        self.assertEqual(before, self.snapshot())

    def test_cannot_fabricate_customer_result_through_plain_transition(self):
        lead = self.create(); before = self.snapshot()
        with self.assertRaises(ValidationError):
            self.command("lead.transition", {"status": "closed", "result": "customer_saved"}, lead)
        self.assertEqual(before, self.snapshot())

    def test_open_task_requires_current_version_and_cancel_does_not_complete_it(self):
        lead = self.create(); changes = self.completion(lead)
        task = lead.next_task
        task.title = "Synthetic updated step"; task.save(update_fields=["title", "updated_at"])
        before = self.snapshot()
        for values in (changes, {"customer": changes["customer"]}):
            with self.subTest(values=values), self.assertRaises(CommercialConflict):
                self.command("lead.complete_customer", values, lead)
            self.assertEqual(before, self.snapshot())

    def test_finished_task_history_is_not_rewritten(self):
        lead = self.create(); task = lead.next_task
        task.status = ServiceTask.STATUS_DONE; task.completed_at = timezone.now()
        task.save(update_fields=["status", "completed_at", "updated_at"])
        before_task = ServiceTask.objects.filter(pk=task.pk).values().get()
        before_history = list(ServiceTaskChange.objects.filter(task=task).values())
        self.command("lead.complete_customer", {"customer": {"contact": {"id": self.client.pk}}}, lead)
        self.assertEqual(before_task, ServiceTask.objects.filter(pk=task.pk).values().get())
        self.assertEqual(before_history, list(ServiceTaskChange.objects.filter(task=task).values()))

    def test_transition_access_is_required_but_deal_capabilities_are_not(self):
        lead = self.create()
        self.grant.qualify_own_leads = self.grant.create_own_deals = self.grant.manage_own_next_action = False
        self.grant.edit_own_leads = False
        self.grant.save()
        http = HttpClient(); http.force_login(self.user)
        response = http.get(reverse("crm_live_lead_detail", args=[lead.pk]), {"org_id": self.org.pk})
        self.assertEqual(response.status_code, 200, response.content)
        self.assertTrue(response.json()["lead"]["can_complete_customer"])
        self.assertFalse(response.json()["lead"]["can_qualify"])
        self.command("lead.complete_customer", self.completion(lead), lead)
        response = http.get(reverse("crm_live_lead_detail", args=[lead.pk]), {"org_id": self.org.pk})
        self.assertFalse(response.json()["lead"]["can_complete_customer"])
        self.assertFalse(response.json()["lead"]["can_add_deal"])
        self.assertEqual(response.json()["lead"]["result_label"], "Контакты сохранены без сделки")
        self.assertFalse(CrmDeal.objects.exists())

    def test_revoked_transition_and_category_authority_prevent_writes(self):
        lead = self.create(); before = self.snapshot()
        with self.assertRaises(PermissionDenied):
            self.command("lead.complete_customer", self.completion(lead, {
                "contact": {"name": "Synthetic unauthorized internal", "category": "internal"},
            }), lead)
        self.assertEqual(before, self.snapshot())
        self.grant.transition_own_leads = False; self.grant.save()
        with self.assertRaises(PermissionDenied):
            self.command("lead.complete_customer", self.completion(lead), lead)
        self.assertEqual(before, self.snapshot())

    def test_contact_role_is_required_even_with_explicit_crm_capabilities(self):
        from pool_service.crm_lead_views import _capabilities, _serialize
        lead = self.create(); before = self.snapshot()
        OrganizationAccess.objects.filter(user=self.user, organization=self.org).update(role="accountant")
        self.assertFalse(_serialize(lead, self.user, _capabilities(self.user, self.org))["can_complete_customer"])
        with self.assertRaises(PermissionDenied):
            self.command("lead.complete_customer", self.completion(lead), lead)
        self.assertEqual(before, self.snapshot())

    def test_free_new_lead_can_be_claimed_without_creating_task(self):
        lead = CrmLead.objects.create(organization=self.org, title="Synthetic unassigned address", status="new")
        before_tasks = ServiceTask.objects.count()
        self.command("lead.complete_customer", self.completion(lead), lead)
        lead.refresh_from_db()
        self.assertEqual((lead.assignee_id, lead.result, lead.next_task_id), (self.user.pk, "customer_saved", None))
        self.assertEqual(ServiceTask.objects.count(), before_tasks)

    def test_free_lead_requires_take_and_colleague_ownership_is_never_stolen(self):
        lead = CrmLead.objects.create(organization=self.org, title="Synthetic free address", status="new")
        self.grant.take_leads = False; self.grant.save()
        before = self.snapshot()
        with self.assertRaises(PermissionDenied):
            self.command("lead.complete_customer", self.completion(lead), lead)
        self.assertEqual(before, self.snapshot())
        colleague = get_user_model().objects.create_user("synthetic-other-completion-manager")
        OrganizationAccess.objects.create(organization=self.org, user=colleague, role="manager")
        lead.assignee = colleague; lead.save()
        self.grant.take_leads = True; self.grant.save()
        before = self.snapshot()
        with self.assertRaises(CommercialConflict):
            self.command("lead.complete_customer", self.completion(lead), lead)
        self.assertEqual(before, self.snapshot())

    def test_task_authority_is_rechecked_on_completion_and_detached_task_replay(self):
        lead = self.create(); changes = self.completion(lead); revision = lead.revision
        colleague = get_user_model().objects.create_user("synthetic-task-owner")
        OrganizationAccess.objects.create(organization=self.org, user=colleague, role="manager")
        task = lead.next_task
        task.created_by = colleague; task.save(update_fields=["created_by", "updated_at"])
        task.responsibles.clear()
        before = self.snapshot()
        with self.assertRaises(PermissionDenied):
            self.command("lead.complete_customer", changes, lead)
        self.assertEqual(before, self.snapshot())
        task.responsibles.add(self.user)
        changes["expected_task_version"] = version_for(task)
        self.command("lead.complete_customer", changes, lead, key="customer-task-replay")
        task.responsibles.clear()
        before = self.snapshot()
        with self.assertRaises(PermissionDenied):
            self.command("lead.complete_customer", changes, lead, key="customer-task-replay", expected_revision=revision)
        self.assertEqual(before, self.snapshot())

    def test_replay_rechecks_source_privacy_and_contact_role(self):
        lead = self.create(); changes = self.completion(lead); revision = lead.revision
        self.command("lead.complete_customer", changes, lead, key="customer-current-access")
        before = self.snapshot()
        with patch("pool_service.services.crm_lead_access.can_read_lead", return_value=False), self.assertRaises(PermissionDenied):
            self.command("lead.complete_customer", changes, lead, key="customer-current-access", expected_revision=revision)
        OrganizationAccess.objects.filter(user=self.user, organization=self.org).update(role="accountant")
        with self.assertRaises(PermissionDenied):
            self.command("lead.complete_customer", changes, lead, key="customer-current-access", expected_revision=revision)
        self.assertEqual(before, self.snapshot())
