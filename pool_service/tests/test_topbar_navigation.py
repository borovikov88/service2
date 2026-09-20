from datetime import timedelta

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from pool_service.context_processors import _finance_topbar_breadcrumbs
from pool_service.models import Organization, OrganizationAccess


class FinanceTopbarBreadcrumbBuilderTests(TestCase):
    def test_nested_finance_route_keeps_parent_clickable(self):
        navigation = [
            {
                "label": "ОПЕРАЦИИ",
                "items": [
                    {
                        "label": "Мои финансы",
                        "route_name": "finance_my",
                        "url": "/finance/my/",
                        "active": True,
                    }
                ],
            }
        ]

        breadcrumbs = _finance_topbar_breadcrumbs(
            [],
            navigation,
            "finance_employee_detail",
        )

        self.assertEqual(
            breadcrumbs,
            [
                {"label": "Операции", "url": reverse("finance_operations")},
                {"label": "Мои финансы", "url": "/finance/my/"},
                {"label": "Сотрудник", "url": ""},
            ],
        )

    def test_payroll_employee_card_has_intermediate_employee_list(self):
        navigation = [
            {
                "label": "АНАЛИТИКА",
                "items": [
                    {
                        "label": "Фонд оплаты труда",
                        "route_name": "finance_payroll_dashboard",
                        "url": "/finance/payroll/",
                        "active": True,
                    }
                ],
            }
        ]

        breadcrumbs = _finance_topbar_breadcrumbs(
            navigation,
            [],
            "finance_payroll_employee_profile",
        )

        self.assertEqual(
            breadcrumbs,
            [
                {"label": "Управленческие финансы", "url": reverse("finance_dashboard")},
                {"label": "Фонд оплаты труда", "url": "/finance/payroll/"},
                {"label": "Сотрудники", "url": reverse("finance_payroll_employee_list")},
                {"label": "Карточка сотрудника", "url": ""},
            ],
        )


class FinanceTopbarRenderedTests(TestCase):
    def setUp(self):
        self.organization = Organization.objects.create(
            name="Topbar Test",
            paid_until=timezone.now() + timedelta(days=30),
        )
        self.owner = User.objects.create_user(
            "topbar-owner",
            password="password",
            first_name="Владелец",
        )
        self.employee = User.objects.create_user(
            "topbar-employee",
            password="password",
            first_name="Сотрудник",
        )
        OrganizationAccess.objects.create(
            organization=self.organization,
            user=self.owner,
            role="admin",
        )
        OrganizationAccess.objects.create(
            organization=self.organization,
            user=self.employee,
            role="manager",
        )
        self.client.force_login(self.owner)

    def test_employee_finance_page_renders_clickable_hierarchy_and_back(self):
        response = self.client.get(
            reverse("finance_employee_detail", args=[self.employee.pk])
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'data-history-back')
        self.assertContains(response, ">Назад<", html=False)
        self.assertContains(
            response,
            f'href="{reverse("finance_operations")}" class="desktop-topbar__crumb">Операции</a>',
            html=False,
        )
        self.assertContains(
            response,
            f'href="{reverse("finance_my")}" class="desktop-topbar__crumb">Мои финансы</a>',
            html=False,
        )
        self.assertContains(
            response,
            'aria-current="page">Сотрудник</span>',
            html=False,
        )
