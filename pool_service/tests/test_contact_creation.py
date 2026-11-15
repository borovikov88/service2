"""Directory entry uses the same contact validation and atomic resolver as CRM."""
import json
from datetime import timedelta
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from pool_service.client_crm_models import ClientBusinessRole, ClientCompanyLink, ClientContact, ClientCRMProfile
from pool_service.models import Client, Employee, Organization, OrganizationAccess


class ContactCreationTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user("synthetic-contact-creator")
        self.org = Organization.objects.create(name="Synthetic contact organization", paid_until=timezone.now() + timedelta(days=7))
        OrganizationAccess.objects.create(user=self.user, organization=self.org, role="admin")
        self.client.force_login(self.user)
        self.url = reverse("client_create")
        self.lookup = reverse("contact_create_lookup")

    def submit(self, customer, **extra):
        return self.client.post(self.url, {"org_id": self.org.pk, "customer": json.dumps(customer), **extra}, HTTP_ACCEPT="application/json")

    def test_bootstrap_single_and_multiple_organization_selection(self):
        response = self.client.get(self.url, {"initial_kind": "company"}, HTTP_ACCEPT="application/json")
        data = response.json()["bootstrap"]
        self.assertEqual(data["organization_id"], self.org.pk)
        self.assertEqual(data["initial_kind"], "company")
        self.assertTrue(data["can_categorize_contacts"])
        self.assertEqual(data["urls"]["clients"], self.lookup)
        other = Organization.objects.create(name="Second synthetic", paid_until=timezone.now() + timedelta(days=7))
        OrganizationAccess.objects.create(user=self.user, organization=other, role="manager")
        self.assertIsNone(self.client.get(self.url, HTTP_ACCEPT="application/json").json()["bootstrap"]["organization_id"])
        response = self.client.post(self.url, {"customer": json.dumps({"contact": {"name": "Ambiguous synthetic"}})}, HTTP_ACCEPT="application/json")
        self.assertEqual(response.status_code, 400)
        self.assertFalse(Client.objects.exists())
        selected = self.client.get(self.url, {"org_id": other.pk}, HTTP_ACCEPT="application/json").json()["bootstrap"]
        self.assertFalse(selected["can_categorize_contacts"])

    def test_contact_and_company_created_separately_linked_and_classified(self):
        response = self.submit({"contact": {"name": "Synthetic landlord", "phone": "+7 900 555 11 22", "email": "landlord@example.invalid", "category": "landlord"},
                                "company": {"name": "Synthetic company", "inn": "1234567890"}}, return_to="clients_list")
        self.assertEqual(response.status_code, 200, response.content)
        data = response.json()
        person, company = Client.objects.get(pk=data["contact"]["id"]), Client.objects.get(pk=data["company"]["id"])
        self.assertEqual(data["client"]["id"], company.pk)
        self.assertEqual(person.organization_id, self.org.pk)
        self.assertEqual(company.organization_id, self.org.pk)
        self.assertTrue(ClientCompanyLink.objects.filter(person=person, company=company).exists())
        self.assertTrue(ClientBusinessRole.objects.filter(client=person, role="landlord", active=True).exists())
        self.assertEqual(set(ClientContact.objects.filter(client=person).values_list("kind", flat=True)), {"phone", "email"})
        self.assertEqual(data["redirect_url"], reverse("clients_list"))

    def test_company_does_not_require_a_person_or_phone(self):
        response = self.submit({"company": {"name": "Standalone synthetic company"}})
        self.assertEqual(response.status_code, 200)
        self.assertIsNone(response.json()["contact"])
        self.assertEqual(Client.objects.get().client_type, "legal")

    def test_validation_and_second_identity_failure_roll_back_everything(self):
        Client.objects.create(organization=self.org, client_type="legal", name="Existing synthetic company", inn="1234567890")
        response = self.submit({"contact": {"name": "Must roll back", "category": "personal"},
                                "company": {"name": "New synthetic company", "inn": "1234567890"}})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(Client.objects.count(), 1)
        self.assertFalse(ClientBusinessRole.objects.exists())
        for fresh in ({"name": "", "phone": ""}, {"name": "Bad email", "email": "invalid"}, {"name": "Bad phone", "phone": "abc"}):
            self.assertEqual(self.submit({"contact": fresh}).status_code, 400)
        self.assertEqual(Client.objects.count(), 1)

    def test_existing_records_are_explicitly_selected_without_merging(self):
        person = Client.objects.create(organization=self.org, client_type="private", name="Existing person")
        company = Client.objects.create(organization=self.org, client_type="legal", name="Existing company")
        response = self.submit({"contact": {"id": person.pk}, "company": {"id": company.pk}})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(Client.objects.count(), 2)
        self.assertTrue(ClientCompanyLink.objects.filter(person=person, company=company).exists())

    def test_duplicate_phone_email_name_and_inn_are_blocked(self):
        existing = Client.objects.create(organization=self.org, client_type="private", name="Existing synthetic", phone="8 (900) 123-45-67", email="EXISTING@example.invalid")
        ClientContact.objects.create(client=existing, kind="phone", value="+7 900 123 00 00", match_value="9001230000")
        cases = [{"name": "Another one", "phone": "+7 900 123 45 67"}, {"name": "Another two", "phone": "+7 900 123 00 00"},
                 {"name": "Another three", "email": "existing@example.invalid"}, {"name": "Existing synthetic"}]
        for contact in cases:
            self.assertEqual(self.submit({"contact": contact}).status_code, 400)
        self.assertEqual(Client.objects.count(), 1)

    def test_foreign_organization_and_foreign_selection_are_denied(self):
        foreign = Organization.objects.create(name="Foreign synthetic", paid_until=timezone.now() + timedelta(days=7))
        record = Client.objects.create(organization=foreign, name="Hidden synthetic", client_type="private")
        for response in (self.client.get(self.url, {"org_id": foreign.pk}, HTTP_ACCEPT="application/json"),
                         self.submit({"contact": {"id": record.pk}}),
                         self.client.get(self.lookup, {"org_id": foreign.pk, "q": "Hidden"})):
            self.assertEqual(response.status_code, 403)
            self.assertNotContains(response, "Hidden synthetic", status_code=403)
        self.assertEqual(Client.objects.count(), 1)

    def test_manager_cannot_forge_category(self):
        OrganizationAccess.objects.filter(user=self.user).update(role="manager")
        self.assertEqual(self.submit({"contact": {"name": "Forbidden category", "category": "internal"}}).status_code, 403)
        self.assertFalse(Client.objects.exists())
        self.assertEqual(self.submit({"contact": {"name": "Allowed contact"}}).status_code, 200)

    def test_current_membership_and_employment_are_rechecked(self):
        from pool_service.contact_creation import _authorize_locked
        original = _authorize_locked
        def revoked(using, actor, organization):
            OrganizationAccess.objects.filter(user=actor, organization=organization).delete()
            return original(using, actor, organization)
        with patch("pool_service.contact_creation._authorize_locked", side_effect=revoked):
            self.assertEqual(self.submit({"contact": {"name": "Revoked synthetic"}}).status_code, 403)
        self.assertFalse(Client.objects.exists())
        Employee.objects.create(organization=self.org, user=self.user, is_active=False)
        self.assertEqual(self.client.get(self.url).status_code, 403)

    def test_superuser_and_expired_organization_cannot_create(self):
        self.user.is_superuser = True
        self.user.save(update_fields=["is_superuser"])
        self.assertEqual(self.submit({"contact": {"name": "Super synthetic"}}).status_code, 403)
        self.user.is_superuser = False
        self.user.save(update_fields=["is_superuser"])
        self.org.paid_until = timezone.now() - timedelta(days=1)
        self.org.trial_started_at = None
        self.org.save()
        self.assertEqual(self.client.get(self.url).status_code, 403)
        self.assertFalse(Client.objects.exists())

    def test_redirect_allowlist_and_legacy_names(self):
        response = self.client.post(self.url, {"client_type": "private", "first_name": "Synthetic", "last_name": "Legacy", "phone": "+7 900 555 00 11", "return_to": "pool_create"})
        record = Client.objects.get()
        self.assertEqual((record.first_name, record.last_name), ("Synthetic", "Legacy"))
        self.assertRedirects(response, reverse("pool_create") + "?client_id=" + str(record.pk), fetch_redirect_response=False)
        response = self.submit({"contact": {"name": "Second synthetic"}}, return_to="https://example.invalid/")
        self.assertEqual(response.json()["redirect_url"], reverse("clients_list"))
        self.assertEqual(self.client.get(self.url, {"return_to": "https://example.invalid/"}, HTTP_ACCEPT="application/json").json()["bootstrap"]["return_url"], reverse("clients_list"))

    def test_lookup_is_bounded_scoped_typed_and_excludes_merged(self):
        for n in range(35):
            Client.objects.create(organization=self.org, client_type="private", name=f"Search synthetic {n:02d}")
        company = Client.objects.create(organization=self.org, client_type="legal", name="Search synthetic company", email="directory@example.invalid")
        merged = Client.objects.create(organization=self.org, client_type="private", name="Search synthetic merged")
        ClientCRMProfile.objects.create(client=merged, merged_into=company)
        response = self.client.get(self.lookup, {"org_id": self.org.pk, "q": "Search", "kind": "private"})
        rows = response.json()["clients"]
        self.assertEqual(len(rows), 30)
        self.assertNotIn(merged.pk, [row["id"] for row in rows])
        self.assertEqual(self.client.get(self.lookup, {"q": "Se"}).json()["clients"], [])
        self.assertEqual(self.client.get(self.lookup, {"q": "directory@example.invalid", "kind": "legal"}).json()["clients"][0]["id"], company.pk)

    def test_malformed_payload_returns_structured_errors(self):
        for body in ("{", "[]", json.dumps({"customer": "{"}), json.dumps({"customer": {"contact": None, "company": None}}),
                     json.dumps({"client_type": []}), json.dumps({"client_type": "private", "first_name": {"forged": "value"}})):
            response = self.client.post(self.url, body, content_type="application/json")
            self.assertEqual(response.status_code, 400)
            self.assertTrue(response.json()["errors"])
        self.assertFalse(Client.objects.exists())
