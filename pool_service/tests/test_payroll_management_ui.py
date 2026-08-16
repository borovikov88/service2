from datetime import date, timedelta
from decimal import Decimal
from tempfile import TemporaryDirectory
from unittest.mock import patch

from django.contrib.auth.models import User
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from pool_service.finance_imports.payroll_dashboard import payroll_dashboard_data
from pool_service.models import (
    DataAuditLog, Employee, EmployeeOneCIdentity, OneCImportBatch,
    OneCReportPeriodState, Organization, OrganizationAccess, PayrollRow,
)
from pool_service.tests.fixtures.management_finance import payroll_xlsx


class PayrollManagementUITests(TestCase):
    def setUp(self):
        self.private_dir = TemporaryDirectory()
        self.override = override_settings(PRIVATE_MEDIA_ROOT=self.private_dir.name)
        self.override.enable()
        self.addCleanup(self.override.disable)
        self.addCleanup(self.private_dir.cleanup)
        self.organization = Organization.objects.create(
            name="Организация", paid_until=timezone.now() + timedelta(days=30)
        )
        self.other = Organization.objects.create(name="Другая")
        self.user = User.objects.create_user("owner", password="password")
        OrganizationAccess.objects.create(
            organization=self.organization, user=self.user, role="owner"
        )
        self.client.force_login(self.user)

    def _identity(self, name="Секретный Сотрудник"):
        return EmployeeOneCIdentity.objects.create(
            organization=self.organization, raw_name=name,
            normalized_name=name.casefold(), normalized_department_name="основное",
            source_identity_key=("a" * 63 + str(EmployeeOneCIdentity.objects.count() % 10)),
            department_name="Основное", status=EmployeeOneCIdentity.STATUS_NOT_FOUND,
        )

    def _batch(self, suffix):
        return OneCImportBatch.objects.create(
            organization=self.organization, import_type=OneCImportBatch.TYPE_PAYROLL,
            original_filename=f"{suffix}.xlsx", stored_file=f"test/{suffix}.xlsx",
            file_sha256=(suffix * 64)[:64], file_size=1,
            status=OneCImportBatch.STATUS_CONFIRMED, uploaded_by=self.user,
            period_first=date(2026, 1, 1), period_last=date(2026, 2, 1),
        )

    def _row(self, batch, identity, month, opening, accrued, paid, closing, row_number=1):
        return PayrollRow.objects.create(
            import_batch=batch, organization=self.organization, employee_identity=identity,
            period_month=month, source_row_number=row_number, department_name="Основное",
            employee_raw_name=identity.raw_name, employee_normalized_name=identity.normalized_name,
            opening_balance=opening, accrued=accrued, paid=paid, closing_balance=closing,
        )

    def _activate(self, batch, *months):
        for month in months:
            OneCReportPeriodState.objects.update_or_create(
                organization=self.organization, report_type=OneCImportBatch.TYPE_PAYROLL,
                period_month=month, defaults={"active_batch": batch, "updated_by": self.user},
            )

    def test_stock_kpis_use_boundary_months_not_sum(self):
        identity = self._identity()
        batch = self._batch("a")
        self._row(batch, identity, date(2026, 1, 1), 100, 50, 20, 130)
        self._row(batch, identity, date(2026, 2, 1), 130, 40, 60, 110, 2)
        self._activate(batch, date(2026, 1, 1), date(2026, 2, 1))
        data = payroll_dashboard_data(
            self.organization, date(2026, 1, 1), date(2026, 2, 1)
        )
        self.assertEqual(data["accrued"], Decimal("90"))
        self.assertEqual(data["paid"], Decimal("80"))
        self.assertEqual(data["opening"], Decimal("100"))
        self.assertEqual(data["closing"], Decimal("110"))
        self.assertEqual(data["debt_change"], Decimal("10"))

    def test_stock_kpis_aggregate_multiple_employees_at_boundaries(self):
        batch = self._batch("m")
        for index, values in enumerate(((10, 20), (30, 40)), 1):
            identity = self._identity(f"Сотрудник {index}")
            self._row(batch, identity, date(2026, 1, 1), values[0], 1, 1, values[0], index)
            self._row(batch, identity, date(2026, 2, 1), values[0], 1, 1, values[1], index + 10)
        self._activate(batch, date(2026, 1, 1), date(2026, 2, 1))
        data = payroll_dashboard_data(self.organization, date(2026, 1, 1), date(2026, 2, 1))
        self.assertEqual(data["opening"], Decimal("40"))
        self.assertEqual(data["closing"], Decimal("60"))

    def test_active_month_replacement_does_not_double_count(self):
        identity = self._identity()
        first = self._batch("a")
        replacement = self._batch("b")
        self._row(first, identity, date(2026, 1, 1), 1, 10, 5, 6)
        self._row(first, identity, date(2026, 2, 1), 6, 20, 10, 16, 2)
        self._row(replacement, identity, date(2026, 2, 1), 6, 200, 100, 106)
        self._row(replacement, identity, date(2026, 3, 1), 106, 300, 200, 206, 2)
        self._activate(first, date(2026, 1, 1))
        self._activate(replacement, date(2026, 2, 1), date(2026, 3, 1))
        data = payroll_dashboard_data(self.organization, date(2026, 1, 1), date(2026, 3, 1))
        self.assertEqual(data["accrued"], Decimal("510"))
        self.assertEqual(data["closing"], Decimal("206"))

    def test_import_history_shows_status_period_rows_and_active_coverage(self):
        batch = self._batch("history")
        self._activate(batch, date(2026, 1, 1), date(2026, 2, 1))
        response = self.client.get(reverse("finance_payroll_import_list"))
        self.assertContains(response, "history.xlsx")
        self.assertContains(response, "Подтверждён")
        self.assertContains(response, ">2<", html=False)

    @patch("pool_service.finance_views.can_view_payroll_personal", return_value=False)
    @patch("pool_service.finance_views.can_view_payroll_summary", return_value=True)
    def test_summary_only_response_has_no_personal_data(self, _summary, _personal):
        identity = self._identity()
        batch = self._batch("s")
        self._row(batch, identity, date.today().replace(month=1, day=1), 1, 999, 1, 999)
        self._activate(batch, date.today().replace(month=1, day=1))
        response = self.client.get(reverse("finance_payroll_dashboard"))
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, identity.raw_name)
        self.assertNotIn("employees", response.context["data"])

    @patch("pool_service.finance_views.can_view_payroll_personal", return_value=True)
    @patch("pool_service.finance_views.can_view_payroll_summary", return_value=True)
    def test_personal_permission_renders_employee_rows(self, _summary, _personal):
        identity = self._identity()
        batch = self._batch("p")
        month = date.today().replace(month=1, day=1)
        self._row(batch, identity, month, 1, 10, 2, 9)
        self._activate(batch, month)
        self.assertContains(self.client.get(reverse("finance_payroll_dashboard")), identity.raw_name)

    @patch("pool_service.finance_views.can_import_payroll", return_value=False)
    def test_import_requires_its_own_permission(self, _permission):
        self.assertEqual(self.client.get(reverse("finance_payroll_import_upload")).status_code, 403)

    @patch("pool_service.finance_views.can_manage_employee_mapping", return_value=False)
    def test_mapping_requires_its_own_permission(self, _permission):
        self.assertEqual(self.client.get(reverse("finance_payroll_employee_mapping")).status_code, 403)

    def test_preview_confirm_flow_and_duplicate_post(self):
        upload = SimpleUploadedFile(
            "payroll.xlsx", payroll_xlsx(),
            content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )
        response = self.client.post(reverse("finance_payroll_import_upload"), {"report": upload})
        self.assertEqual(response.status_code, 302)
        batch = OneCImportBatch.objects.get(import_type=OneCImportBatch.TYPE_PAYROLL)
        self.assertFalse(PayrollRow.objects.exists())
        preview = self.client.get(reverse("finance_payroll_import_preview", args=[batch.pk]))
        self.assertContains(preview, "Сотрудников источника")
        self.assertEqual(batch.metadata["payroll_summary"], {
            "distinct_employees": 2,
            "departments": ["Основное подразделение"],
            "opening_balance": "30.00",
            "accrued": "300.00",
            "paid": "240.00",
            "closing_balance": "90.00",
        })
        first = self.client.post(reverse("finance_payroll_import_confirm", args=[batch.pk]))
        self.assertEqual(first.status_code, 302)
        self.assertEqual(PayrollRow.objects.count(), 4)
        second = self.client.post(reverse("finance_payroll_import_confirm", args=[batch.pk]))
        self.assertEqual(second.status_code, 302)
        self.assertEqual(PayrollRow.objects.count(), 4)

    @patch("pool_service.finance_views.can_view_payroll_personal", return_value=False)
    @patch("pool_service.finance_views.can_import_payroll", return_value=True)
    def test_importer_preview_does_not_leak_personal_rows(self, _import, _personal):
        upload = SimpleUploadedFile(
            "payroll-private.xlsx", payroll_xlsx(),
            content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )
        self.client.post(reverse("finance_payroll_import_upload"), {"report": upload})
        batch = OneCImportBatch.objects.get(original_filename="payroll-private.xlsx")
        response = self.client.get(reverse("finance_payroll_import_preview", args=[batch.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, "Иванов Иван Иванович")
        self.assertContains(response, "Индивидуальные строки скрыты")

    @patch("pool_service.finance_views.can_manage_employee_mapping", return_value=True)
    @patch("pool_service.finance_views.can_view_payroll_personal", return_value=False)
    def test_mapping_manager_sees_identity_but_not_payroll_amounts(self, _personal, _mapping):
        identity = self._identity()
        batch = self._batch("mappings")
        self._row(batch, identity, date(2026, 1, 1), 123456, 987654, 234567, 876543)
        response = self.client.get(reverse("finance_payroll_employee_mapping"))
        self.assertContains(response, identity.raw_name)
        self.assertNotContains(response, "987654")

    def test_cross_org_batch_and_mapping_are_rejected(self):
        other_user = User.objects.create_user("other")
        other_batch = OneCImportBatch.objects.create(
            organization=self.other, import_type=OneCImportBatch.TYPE_PAYROLL,
            original_filename="other.xlsx", stored_file="test/other.xlsx",
            file_sha256="e" * 64, file_size=1,
            status=OneCImportBatch.STATUS_PREVIEWED, uploaded_by=other_user,
        )
        other_identity = EmployeeOneCIdentity.objects.create(
            organization=self.other, raw_name="Чужой", normalized_name="чужой",
            source_identity_key="f" * 64, status=EmployeeOneCIdentity.STATUS_NOT_FOUND,
        )
        other_employee = Employee.objects.create(organization=self.other, display_name="Чужой")
        self.assertEqual(self.client.get(
            reverse("finance_payroll_import_preview", args=[other_batch.pk])
        ).status_code, 404)
        response = self.client.post(
            reverse("finance_payroll_employee_map", args=[other_identity.pk]),
            {"employee": other_employee.pk},
        )
        self.assertEqual(response.status_code, 404)

    def test_manual_mapping_updates_all_historical_rows_through_identity_fk(self):
        identity = self._identity()
        employee = Employee.objects.create(organization=self.organization, display_name="Иванов Иван")
        batch = self._batch("h")
        self._row(batch, identity, date(2026, 1, 1), 1, 1, 1, 1)
        self._row(batch, identity, date(2026, 2, 1), 1, 1, 1, 1, 2)
        response = self.client.post(
            reverse("finance_payroll_employee_map", args=[identity.pk]),
            {"employee": employee.pk, "comment": "Проверено"},
        )
        self.assertEqual(response.status_code, 302)
        identity.refresh_from_db()
        self.assertEqual(identity.employee, employee)
        self.assertEqual(identity.status, EmployeeOneCIdentity.STATUS_MANUALLY_MATCHED)
        self.assertEqual(PayrollRow.objects.filter(employee_identity__employee=employee).count(), 2)
        self.assertTrue(DataAuditLog.objects.filter(
            entity_type="EmployeeOneCIdentity", entity_id=str(identity.pk)
        ).exists())

    def test_invalid_period_fails_safe_with_message(self):
        response = self.client.get(reverse("finance_payroll_dashboard"), {
            "period_from": "2026-08", "period_to": "2026-01",
        })
        self.assertContains(response, "Начальный месяц не может быть позже конечного")
