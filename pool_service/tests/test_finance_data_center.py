import uuid
from datetime import date, timedelta
from types import SimpleNamespace
from unittest.mock import patch

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from pool_service.models import (
    OneCImportBatch,
    OneCODataSyncRun,
    OneCReportPeriodState,
    Organization,
    OrganizationAccess,
    PayrollPlanSnapshot,
)


class FinanceDataCenterTests(TestCase):
    def setUp(self):
        self.organization = Organization.objects.create(
            name="Finance Data Center",
            paid_until=timezone.now() + timedelta(days=30),
        )
        self.owner = User.objects.create_user(
            username="finance-data-owner",
            password="password",
            first_name="Александр",
        )
        self.service = User.objects.create_user(
            username="finance-data-service",
            password="password",
        )
        OrganizationAccess.objects.create(
            organization=self.organization,
            user=self.owner,
            role="owner",
        )
        OrganizationAccess.objects.create(
            organization=self.organization,
            user=self.service,
            role="service",
        )

    def batch(self, import_type, month, *, status=OneCImportBatch.STATUS_CONFIRMED):
        return OneCImportBatch.objects.create(
            organization=self.organization,
            import_type=import_type,
            source_type=OneCImportBatch.SOURCE_ODATA,
            original_filename=f"{import_type}-{month:%Y-%m}.json",
            file_sha256=uuid.uuid4().hex + uuid.uuid4().hex,
            file_size=1,
            status=status,
            uploaded_by=self.owner,
            confirmed_by=self.owner if status == OneCImportBatch.STATUS_CONFIRMED else None,
            confirmed_at=timezone.now() if status == OneCImportBatch.STATUS_CONFIRMED else None,
            period_first=month,
            period_last=month,
        )

    def activate(self, import_type, month):
        batch = self.batch(import_type, month)
        OneCReportPeriodState.objects.create(
            organization=self.organization,
            report_type=import_type,
            period_month=month,
            active_batch=batch,
            updated_by=self.owner,
        )
        return batch

    @patch("pool_service.finance_views.is_odata_target_organization", return_value=True)
    def test_data_page_is_single_user_facing_center(self, _target):
        month = date(2026, 9, 1)
        self.activate(OneCImportBatch.TYPE_MONTHLY_PROFIT, month)
        self.activate(OneCImportBatch.TYPE_CASHFLOW, month)
        self.activate(OneCImportBatch.TYPE_PAYROLL_ACCRUAL, month)
        PayrollPlanSnapshot.objects.create(
            organization=self.organization,
            period_month=month,
            source_hash="a" * 64,
            source_rows=17,
            source_organization_guids=[],
            currency_guid=uuid.uuid4(),
            fetched_by=self.owner,
        )
        OneCODataSyncRun.objects.create(
            organization=self.organization,
            requested_by=self.owner,
            mode=OneCODataSyncRun.MODE_AUTO_APPLY,
            status=OneCODataSyncRun.STATUS_COMPLETED,
            requested_report_types=["monthly_profit", "cashflow", "payroll_accrual"],
            sync_scope={
                "monthly_profit": {"start": "2026-07-01", "end": "2026-09-01"},
                "cashflow": {"start": "2026-07-01", "end": "2026-09-01"},
                "payroll_accrual": {"start": "2026-07-01", "end": "2026-09-01"},
                "_schedule_day": "2026-09-20",
            },
            cursor={},
            progress={"outcome": "no_change"},
            result_summary={},
        )

        self.client.force_login(self.owner)
        response = self.client.get(reverse("finance_data"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Данные 1С")
        self.assertContains(response, "Валовая прибыль")
        self.assertContains(response, "ДДС")
        self.assertContains(response, "ФОТ")
        self.assertContains(response, "Оклады сотрудников")
        self.assertContains(response, "Обновить данные из 1С")
        self.assertContains(response, "История обновлений")
        self.assertContains(response, "Автоматически")
        self.assertContains(response, "Технические инструменты")
        self.assertNotContains(response, "История проверок")
        self.assertNotContains(response, "Управленческие данные 1С")

    def test_operational_employee_cannot_open_management_data_center(self):
        self.client.force_login(self.service)

        response = self.client.get(reverse("finance_data"))

        self.assertEqual(response.status_code, 403)

    @patch("pool_service.finance_views.refresh_payroll_plan_snapshot")
    def test_payroll_plan_refresh_supports_unified_ajax_flow(self, refresh):
        snapshot = SimpleNamespace(
            period_month=date(2026, 9, 1),
            fetched_at=timezone.now(),
        )
        refresh.return_value = (snapshot, True)
        self.client.force_login(self.owner)

        response = self.client.post(
            reverse("finance_payroll_plan_refresh"),
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["ok"], True)
        self.assertEqual(response.json()["period_month"], "2026-09-01")
        self.assertIn("Оклады из 1С обновлены", response.json()["message"])
