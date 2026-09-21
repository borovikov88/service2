from datetime import timedelta

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from pool_service.models import Organization, OrganizationAccess
from pool_service.services.finance import (
    can_access_finance_operations,
    can_access_management_finance,
    finance_operations_navigation,
    management_finance_navigation,
)


class SplitFinanceNavigationTests(TestCase):
    def setUp(self):
        self.organization = Organization.objects.create(
            name="Split Finance",
            paid_until=timezone.now() + timedelta(days=30),
        )

    def user_with_role(self, role):
        user = User.objects.create_user(
            username=f"split-{role}",
            password="password",
        )
        OrganizationAccess.objects.create(
            organization=self.organization,
            user=user,
            role=role,
        )
        return user

    def test_management_workspace_is_limited_to_owner_admin_accountant(self):
        for role in ("owner", "admin", "accountant", "manager", "service", "installer"):
            with self.subTest(role=role):
                user = self.user_with_role(role)
                self.assertEqual(
                    can_access_management_finance(user, self.organization),
                    role in {"owner", "admin", "accountant"},
                )

    def test_operations_workspace_preserves_operational_roles(self):
        for role in ("owner", "admin", "accountant", "manager", "service", "installer"):
            with self.subTest(role=role):
                user = self.user_with_role(role)
                self.assertTrue(
                    can_access_finance_operations(user, self.organization)
                )

    def test_owner_navigation_is_split_without_cross_contamination(self):
        owner = self.user_with_role("owner")
        management = management_finance_navigation(owner, self.organization)
        operations = finance_operations_navigation(owner, self.organization)

        management_labels = {
            item["label"] for group in management for item in group["items"]
        }
        operations_labels = {
            item["label"] for group in operations for item in group["items"]
        }

        self.assertIn("Обзор", management_labels)
        self.assertIn("Валовая прибыль", management_labels)
        self.assertIn("ДДС", management_labels)
        self.assertIn("ФОТ", management_labels)
        self.assertIn("Данные 1С", management_labels)
        self.assertNotIn("Касса ККМ", management_labels)
        self.assertNotIn("Перечисления", management_labels)

        self.assertIn("Расходы и подотчёт", operations_labels)
        self.assertIn("Касса ККМ", operations_labels)
        self.assertIn("Перечисления", operations_labels)
        self.assertIn("Касса организации", operations_labels)
        self.assertNotIn("Валовая прибыль", operations_labels)
        self.assertNotIn("ДДС", operations_labels)

    def test_service_role_renders_only_operations_top_level(self):
        service = self.user_with_role("service")
        self.client.force_login(service)

        response = self.client.get(reverse("finance_my"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, ">Операции<", html=False)
        self.assertNotContains(response, "Управленческие финансы")
        self.assertContains(response, "Расходы и подотчёт")

    def test_owner_renders_both_top_level_finance_workspaces(self):
        owner = self.user_with_role("owner")
        self.client.force_login(owner)

        response = self.client.get(reverse("finance_overview"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Управленческие финансы")
        self.assertContains(response, ">Операции<", html=False)

    def test_operations_entry_uses_expenses_and_accountable_for_cash_roles(self):
        manager = self.user_with_role("manager")
        self.client.force_login(manager)

        response = self.client.get(reverse("finance_operations"))

        self.assertRedirects(
            response,
            reverse("finance_my"),
            fetch_redirect_response=False,
        )

    def test_operations_entry_uses_my_finance_without_cash_access(self):
        service = self.user_with_role("service")
        self.client.force_login(service)

        response = self.client.get(reverse("finance_operations"))

        self.assertRedirects(
            response,
            reverse("finance_my"),
            fetch_redirect_response=False,
        )

    def test_legacy_finance_root_still_routes_service_to_my_finance(self):
        service = self.user_with_role("service")
        self.client.force_login(service)

        response = self.client.get(reverse("finance_dashboard"))

        self.assertRedirects(
            response,
            reverse("finance_my"),
            fetch_redirect_response=False,
        )
