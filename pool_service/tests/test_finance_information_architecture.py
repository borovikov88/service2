from contextlib import ExitStack, contextmanager
from datetime import timedelta
from unittest.mock import patch

from django.contrib.auth.models import User
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from pool_service.models import Organization, OrganizationAccess
from pool_service.services import finance as finance_service
from pool_service.services.finance import finance_navigation


@override_settings(ALLOWED_HOSTS=["testserver"])
class FinanceInformationArchitectureTests(TestCase):
    def setUp(self):
        self.organization = Organization.objects.create(
            name="Finance IA",
            paid_until=timezone.now() + timedelta(days=30),
        )
        self.management = User.objects.create_user(username="ia-owner", password="pass")
        self.operational = User.objects.create_user(username="ia-service", password="pass")
        self.conceptual = User.objects.create_user(username="ia-conceptual", password="pass")
        self.no_access = User.objects.create_user(username="ia-none", password="pass")
        OrganizationAccess.objects.create(
            user=self.management, organization=self.organization, role="owner"
        )
        OrganizationAccess.objects.create(
            user=self.operational, organization=self.organization, role="service"
        )
        OrganizationAccess.objects.create(
            user=self.conceptual, organization=self.organization, role="service"
        )

    @contextmanager
    def _data_capabilities(
        self, *, gross_profit=False, payroll=False, mapping=False, cashflow=False
    ):
        capabilities = {
            "can_import_gross_profit": gross_profit,
            "can_import_payroll": payroll,
            "can_manage_employee_mapping": mapping,
            "can_import_cashflow": cashflow,
        }
        data_access = any((gross_profit, payroll, mapping))
        with ExitStack() as stack:
            for name, value in capabilities.items():
                stack.enter_context(
                    patch(f"pool_service.services.finance.{name}", return_value=value)
                )
                if name != "can_import_cashflow":
                    stack.enter_context(patch(f"pool_service.finance_views.{name}", return_value=value))
            stack.enter_context(
                patch("pool_service.finance_views.can_access_finance_overview", return_value=False)
            )
            stack.enter_context(
                patch("pool_service.finance_views.can_access_my_finances", return_value=False)
            )
            stack.enter_context(
                patch("pool_service.finance_views.can_access_finance_data", return_value=data_access)
            )
            stack.enter_context(
                patch("pool_service.finance_views.can_access_finance_section", return_value=data_access)
            )
            yield

    def test_management_root_routes_to_overview_shell(self):
        self.client.force_login(self.management)

        response = self.client.get(reverse("finance_dashboard"))
        overview = self.client.get(reverse("finance_overview"))

        self.assertRedirects(response, reverse("finance_overview"), fetch_redirect_response=False)
        self.assertEqual(overview.status_code, 200)
        self.assertContains(overview, "Основные финансовые показатели компании")
        self.assertNotContains(overview, "Чистая прибыль")
        self.assertNotContains(overview, "Денежный поток")

    def test_operational_root_routes_to_existing_my_finances(self):
        self.client.force_login(self.operational)

        response = self.client.get(reverse("finance_dashboard"))
        dashboard = self.client.get(reverse("finance_my"))

        self.assertRedirects(response, reverse("finance_my"), fetch_redirect_response=False)
        self.assertEqual(dashboard.status_code, 200)
        self.assertContains(dashboard, "Мои финансы")
        self.assertNotContains(dashboard, "Валовая прибыль")

    @patch("pool_service.finance_views.can_access_finance_data", return_value=True)
    @patch("pool_service.finance_views.can_access_my_finances", return_value=False)
    @patch("pool_service.finance_views.can_access_finance_overview", return_value=False)
    @patch("pool_service.finance_views.can_access_finance_section", return_value=True)
    def test_data_only_root_routes_to_data_not_overview_or_my(self, *_mocks):
        self.client.force_login(self.conceptual)

        response = self.client.get(reverse("finance_dashboard"))

        self.assertRedirects(response, reverse("finance_data"), fetch_redirect_response=False)

    def test_every_finance_data_capability_renders_a_usable_destination(self):
        cases = (
            ({"gross_profit": True}, "finance_onec_import_list"),
            ({"payroll": True}, "finance_payroll_import_list"),
            ({"mapping": True}, "finance_payroll_employee_mapping"),
        )
        self.client.force_login(self.conceptual)

        for enabled, destination in cases:
            with self.subTest(capability=next(iter(enabled))):
                with self._data_capabilities(**enabled):
                    self.assertTrue(
                        finance_service.can_access_finance_data(self.conceptual, self.organization)
                    )
                    root = self.client.get(reverse("finance_dashboard"))
                    data = self.client.get(reverse("finance_data"))
                    self.assertRedirects(
                        root, reverse("finance_data"), fetch_redirect_response=False
                    )
                    self.assertEqual(data.status_code, 200)
                    self.assertContains(data, f'href="{reverse(destination)}"')

    def test_cashflow_only_does_not_enable_finance_data_without_a_ui(self):
        self.client.force_login(self.conceptual)

        with self._data_capabilities(cashflow=True):
            self.assertFalse(
                finance_service.can_access_finance_data(self.conceptual, self.organization)
            )
            self.assertEqual(self.client.get(reverse("finance_dashboard")).status_code, 403)
            self.assertEqual(self.client.get(reverse("finance_data")).status_code, 403)

    @patch("pool_service.finance_views.can_access_finance_data", return_value=False)
    @patch("pool_service.finance_views.can_access_my_finances", return_value=False)
    @patch("pool_service.finance_views.can_access_finance_overview", return_value=False)
    @patch("pool_service.finance_views.can_access_finance_section", return_value=False)
    def test_no_capability_is_denied(self, *_mocks):
        self.client.force_login(self.conceptual)
        self.assertEqual(self.client.get(reverse("finance_dashboard")).status_code, 403)

    def test_user_without_organization_has_no_finance_navigation_or_direct_access(self):
        self.client.force_login(self.no_access)

        profile = self.client.get(reverse("profile"))

        self.assertNotContains(profile, f'href="{reverse("finance_dashboard")}"')
        for route_name in ("finance_dashboard", "finance_overview", "finance_my", "finance_data"):
            with self.subTest(route_name=route_name):
                self.assertEqual(self.client.get(reverse(route_name)).status_code, 403)

    def test_management_navigation_has_semantic_groups_and_direct_analytics(self):
        navigation = finance_navigation(self.management, self.organization)
        groups = {group["key"]: group for group in navigation}
        analytics = {item["label"]: item["url"] for item in groups["analytics"]["items"]}

        self.assertEqual(set(groups), {"analytics", "operations", "data"})
        self.assertEqual(analytics["Валовая прибыль"], reverse("finance_onec_profit_dashboard"))
        self.assertEqual(analytics["Фонд оплаты труда"], reverse("finance_payroll_dashboard"))
        self.assertEqual(analytics["Контроль себестоимости"], reverse("finance_onec_cost_control"))

    @patch("pool_service.services.finance.can_manage_cash", return_value=False)
    @patch("pool_service.services.finance.can_access_cash", return_value=True)
    @patch("pool_service.services.finance.can_access_finance", return_value=False)
    @patch("pool_service.services.finance.can_access_finance_data", return_value=False)
    @patch("pool_service.services.finance.can_access_finance_overview", return_value=False)
    def test_cash_only_navigation_has_operations_without_analytics(self, *_mocks):
        navigation = finance_navigation(self.conceptual, self.organization)
        groups = {group["key"]: group for group in navigation}
        labels = {item["label"] for group in navigation for item in group["items"]}

        self.assertFalse(groups["analytics"]["items"])
        self.assertIn("Мои финансы", labels)
        self.assertIn("Касса ККМ", labels)
        self.assertIn("Перечисления", labels)
        self.assertNotIn("Касса организации", labels)

    @patch("pool_service.services.finance.can_import_cashflow", return_value=False)
    @patch("pool_service.services.finance.can_manage_employee_mapping", return_value=False)
    @patch("pool_service.services.finance.can_import_payroll", return_value=True)
    @patch("pool_service.services.finance.can_view_payroll_summary", return_value=False)
    @patch("pool_service.services.finance.can_import_gross_profit", return_value=False)
    @patch("pool_service.services.finance.can_view_gross_profit", return_value=False)
    @patch("pool_service.services.finance.can_view_cost_control", return_value=False)
    @patch("pool_service.services.finance.can_access_my_finances", return_value=False)
    @patch("pool_service.services.finance.can_access_finance_overview", return_value=False)
    def test_import_only_navigation_has_data_without_analytics(self, *_mocks):
        navigation = finance_navigation(self.conceptual, self.organization)
        groups = {group["key"]: group for group in navigation}

        self.assertFalse(groups["analytics"]["items"])
        self.assertFalse(groups["operations"]["items"])
        self.assertEqual(
            [item["label"] for item in groups["data"]["items"]], ["Данные 1С"]
        )

    @patch("pool_service.services.finance.can_import_cashflow", return_value=False)
    @patch("pool_service.services.finance.can_manage_employee_mapping", return_value=False)
    @patch("pool_service.services.finance.can_import_payroll", return_value=False)
    @patch("pool_service.services.finance.can_view_payroll_summary", return_value=True)
    @patch("pool_service.services.finance.can_import_gross_profit", return_value=False)
    @patch("pool_service.services.finance.can_view_gross_profit", return_value=False)
    @patch("pool_service.services.finance.can_view_cost_control", return_value=False)
    @patch("pool_service.services.finance.can_access_my_finances", return_value=False)
    @patch("pool_service.services.finance.can_access_finance_overview", return_value=False)
    def test_payroll_view_only_has_analytics_without_data(self, *_mocks):
        navigation = finance_navigation(self.conceptual, self.organization)
        groups = {group["key"]: group for group in navigation}

        self.assertEqual(
            [item["label"] for item in groups["analytics"]["items"]],
            ["Фонд оплаты труда"],
        )
        self.assertFalse(groups["data"]["items"])

    def test_desktop_and_mobile_render_the_same_navigation_items(self):
        self.client.force_login(self.management)

        response = self.client.get(reverse("finance_overview"))

        for label in (
            "Валовая прибыль", "Фонд оплаты труда", "Контроль себестоимости",
            "Мои финансы", "Касса ККМ", "Перечисления", "Данные 1С",
        ):
            self.assertContains(response, label)

    @patch("pool_service.finance_views.can_import_gross_profit", return_value=False)
    @patch("pool_service.finance_views.can_view_gross_profit", return_value=True)
    def test_gross_profit_view_uses_view_capability_and_hides_import_action(self, *_mocks):
        self.client.force_login(self.management)

        response = self.client.get(reverse("finance_onec_profit_dashboard"))

        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, "Обновить данные")
