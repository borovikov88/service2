"""Regression coverage for formatted legacy phones in the relationship picker."""
from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from pool_service.client_crm_models import ClientCompanyLink, ClientContact, ClientCRMProfile
from pool_service.models import Client, Organization, OrganizationAccess


class RelationshipPhoneLookupTests(TestCase):
    def setUp(self):
        self.organization = Organization.objects.create(name="Relationship lookup tenant")
        self.foreign = Organization.objects.create(name="Other lookup tenant")
        self.owner = get_user_model().objects.create_user(username="relationship-lookup-owner")
        OrganizationAccess.objects.create(user=self.owner, organization=self.organization, role="owner")
        self.client.force_login(self.owner)
        self.company = self.customer("Company", client_type="legal")

    def customer(self, name, phone="", **kwargs):
        return Client.objects.create(
            name=name, phone=phone,
            organization=kwargs.pop("organization", self.organization),
            client_type=kwargs.pop("client_type", "private"), **kwargs,
        )

    def lookup(self, card, query):
        response = self.client.get(reverse("client_detail", args=[card.pk]), {
            "lookup": "relationships", "q": query,
        })
        self.assertEqual(response.status_code, 200)
        return response.json()

    def test_digits_and_country_prefixes_find_formatted_legacy_phone_without_writes(self):
        person = self.customer("Legacy person", phone="+7 900 123 4567")
        self.assertFalse(ClientContact.objects.filter(client=person).exists())
        for query in ("9001234567", "79001234567", "89001234567", "+7 (900) 123-45-67"):
            with self.subTest(query=query):
                data = self.lookup(self.company, query)
                self.assertEqual([item["id"] for item in data["results"]], [person.pk])
        person.refresh_from_db()
        self.assertEqual(person.phone, "+7 900 123 4567")
        self.assertFalse(ClientContact.objects.filter(client=person).exists())
        self.assertFalse(ClientCompanyLink.objects.exists())

    def test_person_card_can_find_company_by_formatted_legacy_phone(self):
        person = self.customer("Person card")
        company = self.customer("Legacy company", phone="+7 901 234 5678", client_type="legal")
        self.assertEqual([item["id"] for item in self.lookup(person, "89012345678")["results"]], [company.pk])

    def test_scope_and_existing_links_are_applied_before_phone_limit(self):
        phone = "+7 900 123 4567"
        for index in range(25):
            self.customer(f"A wrong type {index:02d}", phone=phone, client_type="legal")
            linked = self.customer(f"B already linked {index:02d}", phone=phone)
            ClientCompanyLink.objects.create(company=self.company, person=linked)
        self.customer("C other tenant", phone=phone, organization=self.foreign)
        merged = self.customer("D merged card", phone=phone)
        target = self.customer("Z eligible person", phone=phone)
        ClientCRMProfile.objects.create(client=merged, merged_into=target)
        data = self.lookup(self.company, "89001234567")
        self.assertEqual([item["id"] for item in data["results"]], [target.pk])
        self.assertFalse(data["has_more"])

    def test_additional_normalized_contacts_are_found(self):
        person = self.customer("Additional phone person")
        ClientContact.objects.create(
            client=person, kind="phone", value="+7 (900) 123-45-67", match_value="9001234567",
        )
        self.assertEqual([item["id"] for item in self.lookup(self.company, "89001234567")["results"]], [person.pk])

    def test_shared_eligible_numbers_are_choices_not_an_automatic_link(self):
        for index in range(25):
            self.customer(f"Shared {index:02d}", phone="+7 900 123 4567")
        data = self.lookup(self.company, "89001234567")
        self.assertEqual(len(data["results"]), 20)
        self.assertTrue(data["has_more"])
        self.assertFalse(ClientCompanyLink.objects.exists())

    def test_international_formatted_phone_keeps_country_identity(self):
        international = self.customer("International person", phone="+358 (40) 123 4567")
        for query in ("+358401234567", "358401234567"):
            with self.subTest(query=query):
                data = self.lookup(self.company, query)
                self.assertEqual([item["id"] for item in data["results"]], [international.pk])
        international.refresh_from_db()
        self.assertEqual(international.phone, "+358 (40) 123 4567")

    def test_short_query_stays_empty_and_cannot_expose_other_organization(self):
        self.customer("Should not preload", phone="+7 900 123 4567")
        for query in ("", "90", " 9 - 0 "):
            self.assertEqual(self.lookup(self.company, query), {"results": [], "has_more": False})
        foreign_company = self.customer("Other company", client_type="legal", organization=self.foreign)
        response = self.client.get(reverse("client_detail", args=[foreign_company.pk]), {
            "lookup": "relationships", "q": "89001234567",
        })
        self.assertEqual(response.status_code, 403)
