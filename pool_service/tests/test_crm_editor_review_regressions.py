"""Regression coverage for the two editor findings on PR #287."""
from datetime import timedelta
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from pool_service.client_crm_models import ClientCRMProfile
from pool_service.models import Client, Organization, OrganizationAccess


class CRMEditorReviewRegressionTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        paid_until = timezone.now() + timedelta(days=30)
        cls.first_org = Organization.objects.create(name="First editor tenant", paid_until=paid_until)
        cls.second_org = Organization.objects.create(name="Second editor tenant", paid_until=paid_until)
        cls.outside_org = Organization.objects.create(name="Outside editor tenant", paid_until=paid_until)
        cls.owner = get_user_model().objects.create_user(username="editor-review-owner")
        OrganizationAccess.objects.create(user=cls.owner, organization=cls.first_org, role="owner")
        OrganizationAccess.objects.create(user=cls.owner, organization=cls.second_org, role="admin")
        cls.customer = Client.objects.create(
            organization=cls.second_org, client_type="legal", name="Example Company",
            company_name="Example Company", phone="+7 900 123 4567", email="example@example.test",
        )
        cls.profile = ClientCRMProfile.objects.create(
            client=cls.customer, notes="Keep these notes", legal_name="Example Company Full Name",
            onec_ref="00000000-0000-0000-0000-000000000287", source="onec",
        )

    def setUp(self):
        self.client.force_login(self.owner)
        self.url = reverse("client_edit", args=[self.customer.pk])

    def assert_customer_unchanged(self):
        self.customer.refresh_from_db()
        self.profile.refresh_from_db()
        self.assertEqual(self.customer.name, "Example Company")
        self.assertEqual(self.customer.company_name, "Example Company")
        self.assertEqual(self.profile.notes, "Keep these notes")

    def test_admin_can_edit_client_in_second_authorized_organization(self):
        self.assertEqual(self.client.get(self.url).status_code, 200)
        response = self.client.post(self.url, {"name": "Updated Company", "return_tab": "contacts"})
        self.assertRedirects(
            response, reverse("client_detail", args=[self.customer.pk]) + "?tab=contacts",
            fetch_redirect_response=False,
        )
        self.customer.refresh_from_db()
        self.assertEqual(self.customer.name, "Updated Company")
        self.assertEqual(self.customer.company_name, "Updated Company")

    def test_owner_of_first_tenant_cannot_use_manager_access_to_edit_second(self):
        OrganizationAccess.objects.filter(user=self.owner, organization=self.second_org).update(role="manager")
        self.assertEqual(self.client.get(self.url).status_code, 403)
        self.assertEqual(self.client.post(self.url, {"name": "Denied"}).status_code, 403)
        self.assert_customer_unchanged()

    def test_owner_cannot_edit_unrelated_tenant(self):
        Client.objects.filter(pk=self.customer.pk).update(organization=self.outside_org)
        self.assertEqual(self.client.get(self.url).status_code, 403)
        self.assertEqual(self.client.post(self.url, {"name": "Denied"}).status_code, 403)
        self.assert_customer_unchanged()

    def test_legacy_company_name_submission_updates_both_names(self):
        response = self.client.post(self.url, {"company_name": "Legacy New Name"})
        self.assertEqual(response.status_code, 302)
        self.customer.refresh_from_db()
        self.profile.refresh_from_db()
        self.assertEqual(self.customer.name, "Legacy New Name")
        self.assertEqual(self.customer.company_name, "Legacy New Name")
        self.assertEqual(self.customer.email, "example@example.test")
        self.assertEqual(self.customer.phone, "+7 900 123 4567")
        self.assertEqual(self.profile.notes, "Keep these notes")
        self.assertEqual(self.profile.legal_name, "Example Company Full Name")
        self.assertEqual(self.profile.onec_ref, "00000000-0000-0000-0000-000000000287")
        self.assertEqual(self.profile.source, "onec")

    def test_explicit_new_name_takes_precedence_over_legacy_alias(self):
        response = self.client.post(self.url, {"name": "Modern Name", "company_name": "Old Alias"})
        self.assertEqual(response.status_code, 302)
        self.customer.refresh_from_db()
        self.assertEqual(self.customer.name, "Modern Name")
        self.assertEqual(self.customer.company_name, "Modern Name")

    def test_omitted_names_preserve_existing_company(self):
        response = self.client.post(self.url, {"return_tab": "contacts"})
        self.assertEqual(response.status_code, 302)
        self.assert_customer_unchanged()

    def test_blank_legacy_name_is_invalid_and_does_not_write_profile(self):
        response = self.client.post(self.url, {"company_name": "  ", "profile-notes": "Do not save"})
        self.assertEqual(response.status_code, 200)
        self.assertIn("name", response.context["client_form"].errors)
        self.assert_customer_unchanged()

    def test_subscription_write_guard_is_preserved(self):
        with patch("pool_service.client_crm_ui.is_org_access_blocked", return_value=True):
            response = self.client.post(self.url, {"name": "Denied", "profile-notes": "Do not save"})
        self.assertEqual(response.status_code, 403)
        self.assert_customer_unchanged()
