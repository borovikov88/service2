"""Baseline contracts for the planned Contacts screen, not a supplier feature."""
from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from pool_service.client_crm_models import ClientCompanyLink, ClientCRMProfile
from pool_service.models import Client, Organization, OrganizationAccess, Pool


class ContactsPreservationContractTests(TestCase):
    def setUp(self):
        self.org = Organization.objects.create(name="Synthetic contacts org")
        self.other_org = Organization.objects.create(name="Synthetic other org")
        self.owner = get_user_model().objects.create_user(username="contacts-owner")
        OrganizationAccess.objects.create(user=self.owner, organization=self.org, role="owner")
        self.client.force_login(self.owner)
        self.company = Client.objects.create(organization=self.org, client_type="legal", name="Synthetic company")
        self.person = Client.objects.create(organization=self.org, client_type="private", name="Synthetic person")

    def add_link(self, company, person):
        return self.client.post(reverse("client_detail", args=[company.pk]), {
            "action": "add_company_link", "related_client": person.pk,
            "position": "Synthetic contact position",
        })

    def test_person_can_link_to_multiple_companies_without_identity_replacement(self):
        second = Client.objects.create(organization=self.org, client_type="legal", name="Synthetic second company")
        profile = ClientCRMProfile.objects.create(client=self.person, onec_ref="00000000-0000-0000-0000-000000000123")
        pool = Pool.objects.create(client=self.person, organization=self.org, address="Synthetic object")
        before = set(Client.objects.values_list("pk", flat=True))
        self.assertEqual(self.add_link(self.company, self.person).status_code, 302)
        self.assertEqual(self.add_link(second, self.person).status_code, 302)
        self.assertSetEqual(set(ClientCompanyLink.objects.filter(person=self.person).values_list("company_id", flat=True)), {self.company.pk, second.pk})
        self.assertSetEqual(set(Client.objects.values_list("pk", flat=True)), before)
        profile.refresh_from_db()
        pool.refresh_from_db()
        self.assertEqual(profile.client_id, self.person.pk)
        self.assertEqual(profile.onec_ref, "00000000-0000-0000-0000-000000000123")
        self.assertEqual(pool.client_id, self.person.pk)

    def test_repeated_link_post_does_not_duplicate_relation(self):
        self.add_link(self.company, self.person)
        self.add_link(self.company, self.person)
        self.assertEqual(ClientCompanyLink.objects.filter(company=self.company, person=self.person).count(), 1)

    def test_cross_organization_link_is_rejected(self):
        outsider = Client.objects.create(organization=self.other_org, client_type="private", name="Synthetic outsider")
        self.assertEqual(self.add_link(self.company, outsider).status_code, 302)
        self.assertFalse(ClientCompanyLink.objects.exists())
        self.assertEqual(self.client.get(reverse("client_detail", args=[outsider.pk])).status_code, 403)

    def test_merged_identity_is_not_selectable_for_new_relation(self):
        ClientCRMProfile.objects.create(client=self.person, merged_into=self.company)
        self.assertEqual(self.add_link(self.company, self.person).status_code, 302)
        self.assertFalse(ClientCompanyLink.objects.exists())
