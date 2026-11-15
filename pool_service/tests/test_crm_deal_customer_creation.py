"""Shared contact/company creation inside the existing deal command boundary."""
import hashlib
import json
from datetime import timedelta
from unittest.mock import patch

from django.core.exceptions import PermissionDenied, ValidationError
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from pool_service.client_crm_models import ClientBusinessRole, ClientBusinessRoleEvent, ClientCompanyLink, ClientContact
from pool_service.crm_commercial_models import CrmCommand, CrmDeal
from pool_service.models import Client, Organization, ServiceTask
from pool_service.services.crm_commercial_commands import CommercialConflict, create_commercial_deal
from pool_service.tests.test_crm_live_pilot_commands import PilotFixture


class DealCustomerCreationTests(PilotFixture, TestCase):
    def customer_create(self, customer, **changes):
        values = dict(actor=self.user, organization=self.org, operation_key="customer-create",
            pipeline_id=self.pipeline.pk, title="Synthetic shared customer",
            next_action={"title": "Discuss request", "due_at": self.due}, customer=customer)
        values.update(changes)
        return create_commercial_deal(**values)

    def snapshot(self):
        return (Client.objects.count(), ClientContact.objects.count(), ClientCompanyLink.objects.count(),
            ClientBusinessRole.objects.count(), ClientBusinessRoleEvent.objects.count(), self.state())

    def test_new_person_company_and_task_commit_together_and_replay_without_duplicates(self):
        customer = {"contact": {"name": "Synthetic new person", "phone": "+79990002244", "email": "person@example.test"},
            "company": {"name": "Synthetic new company", "inn": "1234567890", "phone": "+79990002255", "email": "company@example.test"}}
        result = self.customer_create(customer)
        deal = CrmDeal.objects.get(pk=result["deal_id"])
        task = ServiceTask.objects.get(pk=result["task_id"])
        self.assertEqual((deal.client.client_type, deal.contact.client_type), ("legal", "private"))
        self.assertEqual((task.client_id, task.organization_id), (deal.client_id, self.org.pk))
        self.assertTrue(ClientCompanyLink.objects.filter(company=deal.client, person=deal.contact, automatic=False).exists())
        self.assertEqual(ClientContact.objects.filter(client__in=[deal.client, deal.contact]).count(), 4)
        before = self.snapshot()
        self.assertEqual(self.customer_create(customer), result)
        self.assertEqual(self.snapshot(), before)
        customer["contact"]["name"] = "Changed request"
        with self.assertRaises(CommercialConflict):
            self.customer_create(customer)
        self.assertEqual(self.snapshot(), before)

    def test_existing_person_and_company_are_selected_without_rewriting_them(self):
        company = Client.objects.create(organization=self.org, client_type="legal", name="Synthetic existing company")
        result = self.customer_create({"contact": {"id": self.client_record.pk}, "company": {"id": company.pk}})
        deal = CrmDeal.objects.get(pk=result["deal_id"])
        self.assertEqual((deal.client_id, deal.contact_id), (company.pk, self.client_record.pk))
        self.assertEqual(Client.objects.count(), 2)

    def test_contact_and_company_can_each_be_the_only_customer(self):
        first = self.customer_create({"contact": {"name": "Synthetic person only"}})
        person_deal = CrmDeal.objects.get(pk=first["deal_id"])
        self.assertEqual(person_deal.client_id, person_deal.contact_id)
        second = self.customer_create({"company": {"name": "Synthetic company only"}}, operation_key="company-only")
        company_deal = CrmDeal.objects.get(pk=second["deal_id"])
        self.assertEqual(company_deal.client.client_type, "legal")
        self.assertIsNone(company_deal.contact_id)

    def test_wrong_kind_foreign_and_missing_selection_are_rejected_atomically(self):
        foreign = Client.objects.create(organization=Organization.objects.create(name="Synthetic foreign"), name="Synthetic hidden")
        company = Client.objects.create(organization=self.org, client_type="legal", name="Synthetic company")
        before = self.snapshot()
        for customer in ({"contact": {"id": foreign.pk}}, {"contact": {"id": company.pk}},
                {"company": {"id": self.client_record.pk}},
                {"contact": {"name": "Synthetic rollback person"}, "company": {"id": 999999}}):
            with self.subTest(customer=customer), self.assertRaises(PermissionDenied):
                self.customer_create(customer)
            self.assertEqual(self.snapshot(), before)

    def test_task_failure_rolls_back_every_identity_link_and_command(self):
        before = self.snapshot()
        with patch("pool_service.services.crm_commercial_commands._new_next_task", side_effect=RuntimeError("Synthetic write failure")):
            with self.assertRaises(RuntimeError):
                self.customer_create({"contact": {"name": "Synthetic rollback person"}, "company": {"name": "Synthetic rollback company"}})
        self.assertEqual(self.snapshot(), before)

    def test_category_permission_and_revoked_crm_capability_remain_enforced(self):
        before = self.snapshot()
        with self.assertRaises(PermissionDenied):
            self.customer_create({"contact": {"name": "Synthetic category denial", "category": "personal"}})
        self.assertEqual(self.snapshot(), before)
        self.member.role = "owner"; self.member.save(update_fields=["role"])
        self.grant.view_team_deals = True; self.grant.save(update_fields=["view_team_deals"])
        result = self.customer_create({"contact": {"name": "Synthetic landlord", "category": "landlord"}})
        deal = CrmDeal.objects.get(pk=result["deal_id"])
        self.assertTrue(ClientBusinessRole.objects.filter(client=deal.contact, role="landlord", active=True).exists())
        self.assertEqual(ClientBusinessRoleEvent.objects.get(client=deal.contact).actor_id, self.user.pk)
        before = self.snapshot()
        self.grant.create_own_deals = False; self.grant.save(update_fields=["create_own_deals"])
        with self.assertRaises(PermissionDenied):
            self.customer_create({"contact": {"name": "Synthetic revoked"}}, operation_key="revoked")
        self.assertEqual(self.snapshot(), before)

    def test_duplicate_detection_uses_same_contact_service_as_conversion(self):
        self.client_record.phone = "+7 (999) 000-22-44"; self.client_record.save(update_fields=["phone"])
        before = self.snapshot()
        with self.assertRaises(ValidationError):
            self.customer_create({"contact": {"name": "Synthetic duplicated phone", "phone": "89990002244"}})
        self.assertEqual(self.snapshot(), before)

    def test_invalid_or_mixed_customer_payload_cannot_write(self):
        before = self.snapshot()
        for customer in ({}, {"contact": {"id": True}}, {"contact": {"id": self.client_record.pk, "name": "Mutated"}},
                {"contact": {"name": "Synthetic email", "email": "not-email"}}):
            with self.subTest(customer=customer), self.assertRaises(ValidationError):
                self.customer_create(customer)
        with self.assertRaises(ValidationError):
            self.customer_create({"contact": {"id": self.client_record.pk}}, client_id=self.client_record.pk)
        self.assertEqual(self.snapshot(), before)

    def test_legacy_client_id_keeps_its_exact_payload_hash(self):
        result = self.create()
        legacy = dict(actor_id=self.user.pk, organization_id=self.org.pk, command_type="deal.create",
            client_id=self.client_record.pk, pipeline_id=self.pipeline.pk, title="Synthetic project",
            next_action={"title": "Call back", "due_at": self.due.astimezone(timezone.get_fixed_timezone(0)).isoformat()})
        digest = hashlib.sha256(json.dumps(legacy, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()
        self.assertEqual(CrmCommand.objects.get(operation_key="pilot-create").payload_hash, digest)
        self.assertEqual(self.create(), result)


class DealCustomerHttpTests(PilotFixture, TestCase):
    def setUp(self):
        super().setUp()
        self.org.plan_type = Organization.PLAN_COMPANY_PAID
        self.org.paid_until = timezone.now() + timedelta(days=30)
        self.org.save(update_fields=["plan_type", "paid_until"])
        self.grant.view_team_deals = True; self.grant.save(update_fields=["view_team_deals"])
        self.client.force_login(self.user)

    def url(self, name):
        return reverse(name) + "?org_id=" + str(self.org.pk)

    def test_customer_request_is_serialized_and_mixed_or_null_shapes_fail(self):
        changes = {"title": "Synthetic HTTP customer", "pipeline_id": self.pipeline.pk,
            "next_action": {"title": "Call", "due_at": self.due.isoformat()},
            "customer": {"contact": {"name": "Synthetic HTTP person"}, "company": {"name": "Synthetic HTTP company"}}}
        payload = {"command_type": "deal.create", "operation_key": "http-customer", "changes": changes}
        response = self.client.post(self.url("crm_live_command"), json.dumps(payload), content_type="application/json")
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()["deal"]["contact"]["name"], "Synthetic HTTP person")
        self.assertEqual(response.json()["deal"]["company"]["name"], "Synthetic HTTP company")
        before = Client.objects.count()
        for extra in ({"client_id": self.client_record.pk}, {"customer": None}, {"customer": {"contact": {"id": True}}}):
            with self.subTest(extra=extra):
                payload["changes"] = {**changes, **extra}
                self.assertEqual(self.client.post(self.url("crm_live_command"), json.dumps(payload), content_type="application/json").status_code, 400)
        self.assertEqual(Client.objects.count(), before)

    def test_lookup_filters_kind_and_searches_canonical_phone_email_and_inn(self):
        person = Client.objects.create(organization=self.org, client_type="private", name="Synthetic finder person", phone="+7 (999) 000-77-55", email="finder@example.test")
        company = Client.objects.create(organization=self.org, client_type="legal", name="Synthetic finder company", inn="1234567890")
        Client.objects.create(organization=Organization.objects.create(name="Synthetic elsewhere"), name="Synthetic finder hidden")
        for query, kind, expected in (("89990007755", "private", person), ("finder@example.test", "private", person),
                ("1234567890", "legal", company), ("finder", "legal", company)):
            with self.subTest(query=query, kind=kind):
                response = self.client.get(self.url("crm_live_clients"), {"org_id": self.org.pk, "q": query, "kind": kind})
                self.assertEqual(response.status_code, 200, response.content)
                self.assertEqual([row["id"] for row in response.json()["clients"]], [expected.pk])
                self.assertEqual(response.json()["clients"][0]["client_type"], kind)
        self.assertEqual(self.client.get(self.url("crm_live_clients"), {"org_id": self.org.pk, "q": "finder", "kind": "all-types"}).status_code, 400)

    def test_category_capability_is_scoped_to_current_organization(self):
        def capability():
            response = self.client.get(self.url("crm_live_data"))
            self.assertEqual(response.status_code, 200, response.content)
            return response.json()["can_categorize_contacts"]
        self.assertFalse(capability())
        self.member.role = "owner"; self.member.save(update_fields=["role"])
        self.assertTrue(capability())
