"""CRM lookup/editor regressions; synthetic clients only."""
import json
from datetime import date, timedelta
from pathlib import Path
import shutil
import subprocess
import unittest

from django.contrib.auth import get_user_model
from django.test import SimpleTestCase, TestCase
from django.urls import reverse
from django.utils import timezone

from pool_service.client_crm_models import ClientCompanyLink, ClientContact, ClientCRMProfile
from pool_service.client_crm_ui import card_url, relationship_results
from pool_service.models import Client, Organization, OrganizationAccess


class CRMUIWorkflowTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.org = Organization.objects.create(
            name="CRM UI synthetic tenant", paid_until=timezone.now() + timedelta(days=30),
        )
        cls.other_org = Organization.objects.create(
            name="Other synthetic tenant", paid_until=timezone.now() + timedelta(days=30),
        )
        cls.owner = get_user_model().objects.create_user(username="crm-ui-owner")
        cls.manager = get_user_model().objects.create_user(username="crm-ui-manager")
        cls.other = get_user_model().objects.create_user(username="crm-ui-other")
        for user, org, role in (
            (cls.owner, cls.org, "owner"), (cls.manager, cls.org, "manager"),
            (cls.other, cls.other_org, "owner"),
        ):
            OrganizationAccess.objects.create(user=user, organization=org, role=role)
        cls.person = Client.objects.create(
            organization=cls.org, client_type="private", name="Example Person",
            first_name="Example", last_name="Person", phone="+358 40 123 4567",
        )
        cls.company = Client.objects.create(
            organization=cls.org, client_type="legal", name="Тест Строй-инвест",
            company_name="Тест Строй-инвест", inn="1234567890",
        )

    def setUp(self):
        self.client.force_login(self.owner)

    def detail(self, client=None):
        return reverse("client_detail", args=[(client or self.person).pk])

    def edit(self, client=None):
        return reverse("client_edit", args=[(client or self.person).pk])

    def lookup(self, query, client=None):
        return self.client.get(self.detail(client), {"lookup": "relationships", "q": query})

    def test_short_lookup_does_not_query_candidates(self):
        for query in ("", "Ст", " Ст - ", "---", "!" * 200):
            with self.subTest(query=query), self.assertNumQueries(0):
                response = relationship_results(self.person, query)
                self.assertEqual(json.loads(response.content)["results"], [])

    def test_lookup_finds_hyphen_space_and_cyrillic_case(self):
        for query in ("Строй инвест", "СТРОЙ-ИНВЕСТ", "строй\u00a0инвест"):
            response = self.lookup(query)
            self.assertEqual(response.status_code, 200)
            self.assertEqual([r["id"] for r in response.json()["results"]], [self.company.pk])
            self.assertIn("no-store", response["Cache-Control"])

    def test_lookup_normalizes_yo_and_searches_contacts(self):
        self.company.name = "Компания Семён"
        self.company.save(update_fields=["name"])
        self.assertEqual(self.lookup("семен").json()["results"][0]["id"], self.company.pk)
        ClientContact.objects.create(
            client=self.company, kind="phone", value="+7 900 123 4567",
            match_value="9001234567", sources=["onec"],
        )
        self.assertEqual(self.lookup("9001234567").json()["results"][0]["id"], self.company.pk)

    def test_lookup_excludes_other_tenant_type_merged_and_already_linked(self):
        Client.objects.create(organization=self.other_org, client_type="legal", name="Строй инвест foreign")
        Client.objects.create(organization=self.org, client_type="private", name="Строй инвест person")
        merged = Client.objects.create(organization=self.org, client_type="legal", name="Строй инвест merged")
        ClientCRMProfile.objects.create(client=merged, merged_into=self.company)
        ClientCompanyLink.objects.create(company=self.company, person=self.person)
        self.assertEqual(self.lookup("Строй инвест").json()["results"], [])

    def test_company_picker_returns_people_only(self):
        response = self.lookup("Example", self.company)
        self.assertEqual([r["id"] for r in response.json()["results"]], [self.person.pk])

    def test_lookup_limits_results(self):
        for index in range(25):
            Client.objects.create(
                organization=self.org, client_type="legal", name=f"Lookup Company {index:02}",
            )
        data = self.lookup("Lookup").json()
        self.assertEqual(len(data["results"]), 20)
        self.assertTrue(data["has_more"])

    def test_lookup_and_editor_permissions_are_not_card_read_permissions(self):
        for user in (self.manager, self.other):
            self.client.force_login(user)
            self.assertEqual(self.lookup("Строй").status_code, 403)
            self.assertEqual(self.client.get(self.edit()).status_code, 403)
            self.assertEqual(self.client.post(self.edit(), {"name": "Forbidden"}).status_code, 403)
        self.person.refresh_from_db()
        self.assertEqual(self.person.name, "Example Person")

    def test_unauthenticated_lookup_requires_login(self):
        self.client.logout()
        self.assertEqual(self.lookup("Строй").status_code, 302)

    def test_detail_has_empty_picker_and_contacts_tab_is_server_rendered(self):
        response = self.client.get(self.detail(), {"tab": "contacts"})
        self.assertContains(response, "data-crm-lookup")
        self.assertNotContains(response, '<select class="form-select" name="related_client"')
        self.assertNotContains(response, self.company.name)
        self.assertContains(response, 'class="tab-pane fade show active" id="client-contacts"')
        self.assertEqual(response.context["card_tab"], "contacts")
        self.assertEqual(self.client.get(self.detail(), {"tab": "invalid"}).context["card_tab"], "overview")

    def test_relationship_create_update_delete_stay_on_contacts(self):
        response = self.client.post(self.detail(), {
            "action": "add_company_link", "related_client": self.company.pk,
            "position": "Test contact",
        })
        self.assertRedirects(response, card_url(self.person.pk, "contacts"), fetch_redirect_response=False)
        link = ClientCompanyLink.objects.get(company=self.company, person=self.person)
        for action in ("update_company_link", "delete_company_link"):
            response = self.client.post(self.detail(), {
                "action": action, "link_id": link.pk, "position": "Updated",
            })
            self.assertRedirects(response, card_url(self.person.pk, "contacts"), fetch_redirect_response=False)

    def test_invalid_relationship_stays_contacts_and_does_not_link_foreign_client(self):
        foreign = Client.objects.create(organization=self.other_org, client_type="legal", name="Foreign")
        response = self.client.post(self.detail(), {
            "action": "add_company_link", "related_client": foreign.pk,
        })
        self.assertEqual(response.url, card_url(self.person.pk, "contacts"))
        self.assertFalse(ClientCompanyLink.objects.exists())

    def test_automatic_ip_link_cannot_be_deleted(self):
        link = ClientCompanyLink.objects.create(
            company=self.company, person=self.person, automatic=True, source="onec_ip",
        )
        response = self.client.post(self.detail(), {"action": "delete_company_link", "link_id": link.pk})
        self.assertEqual(response.url, card_url(self.person.pk, "contacts"))
        self.assertTrue(ClientCompanyLink.objects.filter(pk=link.pk).exists())

    def test_editor_populates_profile_and_saves_person_fields(self):
        profile = ClientCRMProfile.objects.create(
            client=self.person, middle_name="Middle", birth_date=date(1990, 2, 3), notes="Existing note",
        )
        page = self.client.get(self.edit(), {"tab": "contacts"})
        self.assertContains(page, 'value="1990-02-03"')
        self.assertContains(page, 'data-crm-phone="true"')
        self.assertContains(page, "Existing note")
        response = self.client.post(self.edit(), {
            "name": "Chosen alias", "profile-middle_name": "Updated",
            "profile-birth_date": "1991-04-05", "profile-manager": self.manager.pk,
            "profile-notes": "New note", "return_tab": "contacts",
        })
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, card_url(self.person.pk, "contacts"))
        profile.refresh_from_db(); self.person.refresh_from_db()
        self.assertEqual(self.person.name, "Chosen alias")
        self.assertEqual(profile.middle_name, "Updated")
        self.assertEqual(profile.birth_date, date(1991, 4, 5))
        self.assertEqual(profile.manager_id, self.manager.pk)
        self.assertEqual(self.person.phone, "+358 40 123 4567")

    def test_company_editor_saves_requisites_without_fake_contact_person(self):
        response = self.client.post(self.edit(self.company), {
            "name": "Updated company", "profile-legal_name": "Full company",
            "profile-kpp": "123456789", "profile-ogrn": "1234567890123",
        })
        self.assertEqual(response.status_code, 302)
        self.company.refresh_from_db()
        self.assertEqual(self.company.company_name, "Updated company")
        self.assertEqual(self.company.crm_profile.legal_name, "Full company")
        self.assertEqual(self.company.crm_profile.kpp, "123456789")

    def test_invalid_profile_does_not_partially_save_client(self):
        profile = ClientCRMProfile.objects.create(client=self.person, notes="Keep")
        response = self.client.post(self.edit(), {
            "name": "Should not save", "phone": "89001234567",
            "profile-birth_date": "bad-date", "profile-manager": self.other.pk,
        })
        self.assertEqual(response.status_code, 200)
        self.person.refresh_from_db(); profile.refresh_from_db()
        self.assertEqual(self.person.name, "Example Person")
        self.assertEqual(self.person.phone, "+358 40 123 4567")
        self.assertEqual(profile.notes, "Keep")
        self.assertFalse(ClientContact.objects.filter(client=self.person).exists())

    def test_editor_preserves_1c_ids_type_and_automatic_relationships(self):
        profile = ClientCRMProfile.objects.create(
            client=self.person, onec_ref="aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
            onec_code="source-code", source="onec",
        )
        link = ClientCompanyLink.objects.create(
            company=self.company, person=self.person, automatic=True, source="onec_ip",
        )
        response = self.client.post(self.edit(), {
            "name": "Changed", "client_type": "legal", "organization": self.other_org.pk,
            "profile-onec_ref": "forged", "profile-source": "manual", "profile-legal_form": "ip",
        })
        self.assertEqual(response.status_code, 302)
        self.person.refresh_from_db(); profile.refresh_from_db(); link.refresh_from_db()
        self.assertEqual(self.person.client_type, "private")
        self.assertEqual(self.person.organization_id, self.org.pk)
        self.assertEqual(profile.onec_ref, "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee")
        self.assertEqual(profile.onec_code, "source-code")
        self.assertEqual(profile.source, "onec")
        self.assertTrue(link.automatic)

    def test_changing_primary_keeps_unrelated_manual_and_imported_contacts(self):
        old = ClientContact.objects.create(
            client=self.person, kind="phone", value=self.person.phone,
            match_value="+358401234567", is_primary=True, sources=["manual", "onec"],
        )
        extra = ClientContact.objects.create(
            client=self.person, kind="phone", value="+7 900 111 2233",
            match_value="9001112233", sources=["manual"],
        )
        self.assertEqual(self.client.post(self.edit(), {"phone": "89001234567"}).status_code, 302)
        old.refresh_from_db(); extra.refresh_from_db(); self.person.refresh_from_db()
        self.assertEqual(old.sources, ["onec"])
        self.assertFalse(old.is_primary)
        self.assertEqual(extra.sources, ["manual"])
        self.assertEqual(self.person.phone, "+7 900 123 4567")
        self.assertEqual(ClientContact.objects.filter(client=self.person, kind="phone", is_primary=True).count(), 1)

    def test_legacy_partial_post_keeps_foreign_phone_and_profile_fields(self):
        profile = ClientCRMProfile.objects.create(client=self.person, notes="Keep", birth_date=date(1980, 1, 2))
        response = self.client.post(self.edit(), {
            "client_type": "private", "first_name": "Updated", "last_name": "Person",
            "phone": self.person.phone, "email": "", "company_name": "", "inn": "", "contact_position": "",
        })
        self.assertEqual(response.status_code, 302)
        self.person.refresh_from_db(); profile.refresh_from_db()
        self.assertEqual(self.person.phone, "+358 40 123 4567")
        self.assertEqual(profile.notes, "Keep")
        self.assertEqual(profile.birth_date, date(1980, 1, 2))
        self.assertEqual(ClientContact.objects.get(client=self.person, kind="phone").value, self.person.phone)

    def test_changing_person_name_parts_does_not_replace_explicit_alias(self):
        response = self.client.post(self.edit(), {"name": "Example Person", "first_name": "Updated"})
        self.assertEqual(response.status_code, 302)
        self.person.refresh_from_db()
        self.assertEqual(self.person.name, "Example Person")
        self.assertEqual(self.person.first_name, "Updated")

    def test_editor_email_is_synchronized_and_return_tab_is_allowlisted(self):
        response = self.client.post(self.edit(), {
            "email": "new@example.test", "return_tab": "https://other.example.test/",
        })
        self.assertEqual(response.url, card_url(self.person.pk, "overview"))
        self.assertEqual(ClientContact.objects.get(client=self.person, kind="email").value, "new@example.test")

    def test_blocked_subscription_cannot_save(self):
        self.org.paid_until = timezone.now() - timedelta(days=1)
        self.org.trial_started_at = None
        self.org.save(update_fields=["paid_until", "trial_started_at"])
        response = self.client.post(self.edit(), {"name": "Forbidden"})
        self.person.refresh_from_db()
        self.assertNotEqual(response.status_code, 200)
        self.assertEqual(self.person.name, "Example Person")


@unittest.skipUnless(shutil.which("node"), "Node.js is required for real-script DOM unit tests")
class CRMClientJavascriptTests(SimpleTestCase):
    def test_actual_picker_and_list_scripts(self):
        result = subprocess.run(
            [shutil.which("node"), "--test", str(Path(__file__).with_name("crm_client_picker.test.cjs"))],
            capture_output=True, text=True, timeout=20, check=False,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
