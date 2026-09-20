import uuid
from datetime import date, timedelta
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from pool_service.finance_imports.payroll_plan import payroll_compensation_dashboard_data
from pool_service.models import (
    DataAuditLog,
    Employee,
    EmployeeCompensationMonth,
    EmployeeOneCIdentity,
    Organization,
    OrganizationAccess,
    PayrollPlanItem,
    PayrollPlanSnapshot,
)


class EmployeeCompensationMonthTests(TestCase):
    def setUp(self):
        self.organization = Organization.objects.create(
            name="Compensation Org",
            paid_until=timezone.now() + timedelta(days=30),
        )
        self.owner = User.objects.create_superuser(
            username="comp-owner",
            email="owner@example.com",
            password="password",
        )
        OrganizationAccess.objects.create(
            organization=self.organization,
            user=self.owner,
            role="owner",
        )
        self.employee = Employee.objects.create(
            organization=self.organization,
            display_name="Иванов Иван",
            department_name="Сервис",
        )
        self.identity = EmployeeOneCIdentity.objects.create(
            organization=self.organization,
            employee=self.employee,
            raw_name="Иванов Иван",
            normalized_name="иванов иван",
            department_name="Сервис",
            status=EmployeeOneCIdentity.STATUS_AUTO_MATCHED,
            match_method=EmployeeOneCIdentity.MATCH_EXTERNAL_ID,
            onec_employee_id=str(uuid.uuid4()),
        )
        self.month = date(2026, 9, 1)
        self.snapshot = PayrollPlanSnapshot.objects.create(
            organization=self.organization,
            period_month=self.month,
            source_hash="a" * 64,
            source_rows=1,
            source_organization_guids=[],
            currency_guid=uuid.uuid4(),
            fetched_by=self.owner,
        )
        PayrollPlanItem.objects.create(
            snapshot=self.snapshot,
            employee_identity=self.identity,
            onec_employee_id=self.identity.onec_employee_id,
            employee_raw_name=self.employee.display_name,
            accrual_type_id=str(uuid.uuid4()),
            accrual_type_name="Оклад",
            amount=Decimal("60000.00"),
            is_base_salary=True,
            source_period=self.month,
            source_organization_guid=uuid.uuid4(),
        )

    def test_payroll_summary_includes_all_requested_salary_components(self):
        EmployeeCompensationMonth.objects.create(
            organization=self.organization,
            employee=self.employee,
            period_month=self.month,
            percent_amount=Decimal("10000"),
            bonus_amount=Decimal("5000"),
            extra_days_count=Decimal("2"),
            extra_days_amount=Decimal("4000"),
            transport_compensation_amount=Decimal("3000"),
            deduction_amount=Decimal("1500"),
            updated_by=self.owner,
        )

        data = payroll_compensation_dashboard_data(self.organization, self.month)

        self.assertTrue(data["has_data"])
        row = data["employees"][0]
        self.assertEqual(row["base_salary"], Decimal("60000"))
        self.assertEqual(row["percent_amount"], Decimal("10000"))
        self.assertEqual(row["bonus_amount"], Decimal("5000"))
        self.assertEqual(row["extra_days_count"], Decimal("2"))
        self.assertEqual(row["extra_days_amount"], Decimal("4000"))
        self.assertEqual(row["transport_compensation_amount"], Decimal("3000"))
        self.assertEqual(row["deduction_amount"], Decimal("1500"))
        self.assertEqual(row["total"], Decimal("80500"))
        self.assertEqual(data["total"], Decimal("80500"))

    @patch("pool_service.finance_views.current_payroll_plan_date", return_value=date(2026, 9, 20))
    def test_employee_profile_shows_monthly_salary_history(self, _date):
        EmployeeCompensationMonth.objects.create(
            organization=self.organization,
            employee=self.employee,
            period_month=self.month,
            percent_amount=Decimal("10000"),
            bonus_amount=Decimal("5000"),
            extra_days_count=Decimal("2"),
            extra_days_amount=Decimal("4000"),
            transport_compensation_amount=Decimal("3000"),
            deduction_amount=Decimal("1500"),
            updated_by=self.owner,
        )
        self.client.force_login(self.owner)

        response = self.client.get(
            reverse("finance_payroll_employee_profile", args=[self.employee.pk])
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "История зарплаты по месяцам")
        self.assertContains(response, "Компенсация транспорта")
        self.assertContains(response, "Удержания")
        self.assertEqual(response.context["salary"]["total"], Decimal("80500"))
        self.assertEqual(response.context["salary"]["extra_days_count"], Decimal("2"))

    def test_monthly_components_can_be_saved_and_are_audited(self):
        self.client.force_login(self.owner)

        response = self.client.post(
            reverse(
                "finance_payroll_employee_compensation_update",
                args=[self.employee.pk],
            ),
            {
                "period_month": "2026-09",
                "percent_amount": "10000",
                "bonus_amount": "5000",
                "extra_days_count": "2",
                "extra_days_amount": "4000",
                "transport_compensation_amount": "3000",
                "deduction_amount": "1500",
                "note": "Согласовано",
            },
        )

        self.assertEqual(response.status_code, 302)
        compensation = EmployeeCompensationMonth.objects.get(
            organization=self.organization,
            employee=self.employee,
            period_month=self.month,
        )
        self.assertEqual(compensation.transport_compensation_amount, Decimal("3000"))
        self.assertEqual(compensation.deduction_amount, Decimal("1500"))
        self.assertEqual(compensation.updated_by, self.owner)
        self.assertTrue(
            DataAuditLog.objects.filter(
                entity_type="EmployeeCompensationMonth",
                entity_id=str(compensation.pk),
            ).exists()
        )

    def test_negative_components_are_rejected(self):
        self.client.force_login(self.owner)

        response = self.client.post(
            reverse(
                "finance_payroll_employee_compensation_update",
                args=[self.employee.pk],
            ),
            {
                "period_month": "2026-09",
                "percent_amount": "-1",
                "bonus_amount": "0",
                "extra_days_count": "0",
                "extra_days_amount": "0",
                "transport_compensation_amount": "0",
                "deduction_amount": "0",
                "note": "",
            },
        )

        self.assertEqual(response.status_code, 302)
        self.assertFalse(
            EmployeeCompensationMonth.objects.filter(
                organization=self.organization,
                employee=self.employee,
                period_month=self.month,
            ).exists()
        )
