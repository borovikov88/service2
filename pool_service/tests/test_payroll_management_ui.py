from datetime import date, timedelta
from decimal import Decimal
from contextlib import ExitStack, contextmanager
from tempfile import TemporaryDirectory
from unittest.mock import patch

from django.contrib.auth.models import User
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from pool_service.finance_imports.payroll_dashboard import (
    payroll_dashboard_data, payroll_identity_rows,
)
from pool_service.finance_imports.payroll_parser import PARSER_VERSION as PAYROLL_PARSER_VERSION
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

    @contextmanager
    def _permissions(self, *, summary=False, personal=False, import_access=False, mapping=False):
        permissions = {
            "can_view_payroll_summary": summary,
            "can_view_payroll_personal": personal,
            "can_import_payroll": import_access,
            "can_manage_employee_mapping": mapping,
        }
        with ExitStack() as stack:
            for name, value in permissions.items():
                stack.enter_context(
                    patch(f"pool_service.finance_views.{name}", return_value=value)
                )
                stack.enter_context(
                    patch(f"pool_service.services.finance.{name}", return_value=value)
                )
            yield

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
        data = payroll_dashboard_data(
            self.organization, date(2026, 1, 1), date(2026, 3, 1), include_personal=True
        )
        self.assertEqual(data["accrued"], Decimal("510"))
        self.assertEqual(data["paid"], Decimal("305"))
        self.assertEqual(data["opening"], Decimal("1"))
        self.assertEqual(data["closing"], Decimal("206"))
        self.assertEqual(data["employees"][0]["accrued"], Decimal("510"))
        identity_row = payroll_identity_rows(self.organization).get(pk=identity.pk)
        self.assertEqual(identity_row.active_payroll_row_count, 3)
        self.assertEqual(identity_row.first_active_period, date(2026, 1, 1))
        self.assertEqual(identity_row.last_active_period, date(2026, 3, 1))

    def test_identity_statistics_exclude_replaced_and_other_organization_rows(self):
        identity = self._identity()
        first = self._batch("identity-a")
        replacement = self._batch("identity-b")
        for index, month in enumerate((date(2026, 1, 1), date(2026, 2, 1)), 1):
            self._row(first, identity, month, 1, 1, 1, 1, index)
        self._row(replacement, identity, date(2026, 2, 1), 1, 2, 2, 1)
        self._activate(first, date(2026, 1, 1))
        self._activate(replacement, date(2026, 2, 1))
        other_user = User.objects.create_user("identity-other")
        other_identity = EmployeeOneCIdentity.objects.create(
            organization=self.other, raw_name="Чужая версия", normalized_name="чужая версия",
            source_identity_key="d" * 64, status=EmployeeOneCIdentity.STATUS_NOT_FOUND,
        )
        other_batch = OneCImportBatch.objects.create(
            organization=self.other, import_type=OneCImportBatch.TYPE_PAYROLL,
            original_filename="other.xlsx", stored_file="test/other.xlsx",
            file_sha256="c" * 64, file_size=1,
            status=OneCImportBatch.STATUS_CONFIRMED, uploaded_by=other_user,
        )
        PayrollRow.objects.create(
            import_batch=other_batch, organization=self.other, employee_identity=other_identity,
            period_month=date(2024, 1, 1), source_row_number=1,
            employee_raw_name="Чужая версия", employee_normalized_name="чужая версия",
            opening_balance=1, accrued=999, paid=999, closing_balance=1,
        )
        OneCReportPeriodState.objects.create(
            organization=self.other, report_type=OneCImportBatch.TYPE_PAYROLL,
            period_month=date(2024, 1, 1), active_batch=other_batch, updated_by=other_user,
        )
        with self.assertNumQueries(1):
            row = payroll_identity_rows(self.organization).get(pk=identity.pk)
        self.assertEqual(row.active_payroll_row_count, 2)
        self.assertEqual(row.first_active_period, date(2026, 1, 1))
        self.assertEqual(row.last_active_period, date(2026, 2, 1))
        response = self.client.get(reverse("finance_payroll_employee_mapping"))
        self.assertContains(response, "2 ·")
        self.assertNotContains(response, "Чужая версия")

    def test_dashboard_unresolved_count_excludes_replacement_only_identity(self):
        identity_old = self._identity("Старая версия")
        identity_active = self._identity("Активная версия")
        old_batch = self._batch("unresolved-old")
        active_batch = self._batch("unresolved-active")
        month = date(2026, 1, 1)
        self._row(old_batch, identity_old, month, 1, 1, 1, 1)
        self._row(active_batch, identity_active, month, 1, 1, 1, 1)
        self._activate(active_batch, month)

        with self._permissions(summary=True, mapping=True):
            response = self.client.get(reverse("finance_payroll_dashboard"))

        self.assertEqual(response.context["unresolved_count"], 1)

    def test_dashboard_unresolved_count_excludes_foreign_active_identity(self):
        other_user = User.objects.create_user("unresolved-other")
        foreign_identity = EmployeeOneCIdentity.objects.create(
            organization=self.other, raw_name="Чужой сотрудник",
            normalized_name="чужой сотрудник", source_identity_key="e" * 64,
            status=EmployeeOneCIdentity.STATUS_NOT_FOUND,
        )
        foreign_batch = OneCImportBatch.objects.create(
            organization=self.other, import_type=OneCImportBatch.TYPE_PAYROLL,
            original_filename="foreign.xlsx", stored_file="test/foreign.xlsx",
            file_sha256="f" * 64, file_size=1,
            status=OneCImportBatch.STATUS_CONFIRMED, uploaded_by=other_user,
        )
        PayrollRow.objects.create(
            import_batch=foreign_batch, organization=self.other,
            employee_identity=foreign_identity, period_month=date(2026, 1, 1),
            source_row_number=1, employee_raw_name=foreign_identity.raw_name,
            employee_normalized_name=foreign_identity.normalized_name,
            opening_balance=1, accrued=1, paid=1, closing_balance=1,
        )
        OneCReportPeriodState.objects.create(
            organization=self.other, report_type=OneCImportBatch.TYPE_PAYROLL,
            period_month=date(2026, 1, 1), active_batch=foreign_batch,
            updated_by=other_user,
        )

        with self._permissions(summary=True, mapping=True):
            response = self.client.get(reverse("finance_payroll_dashboard"))

        self.assertEqual(response.context["unresolved_count"], 0)

    def test_dashboard_unresolved_count_excludes_manually_mapped_identity(self):
        identity = self._identity("Сопоставляемый сотрудник")
        employee = Employee.objects.create(
            organization=self.organization, display_name="Сопоставленный сотрудник"
        )
        batch = self._batch("unresolved-mapped")
        month = date(2026, 1, 1)
        self._row(batch, identity, month, 1, 1, 1, 1)
        self._activate(batch, month)

        with self._permissions(summary=True, mapping=True):
            before = self.client.get(reverse("finance_payroll_dashboard"))
            self.client.post(
                reverse("finance_payroll_employee_map", args=[identity.pk]),
                {"employee": employee.pk, "comment": "Подтверждено"},
            )
            after = self.client.get(reverse("finance_payroll_dashboard"))

        self.assertEqual(before.context["unresolved_count"], 1)
        self.assertEqual(after.context["unresolved_count"], 0)

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
        self.assertContains(preview, "Данные ещё не импортированы")
        self.assertContains(preview, "Перейти к подтверждению")
        self.assertNotContains(preview, "<form method=\"post\" action=", html=False)
        self.assertEqual(batch.parser_version, PAYROLL_PARSER_VERSION)
        self.assertEqual(batch.metadata["payroll_summary"], {
            "distinct_employees": 2,
            "departments": ["Основное подразделение"],
            "opening_balance": "30.00",
            "accrued": "300.00",
            "paid": "240.00",
            "closing_balance": "90.00",
        })
        confirm_url = reverse("finance_payroll_import_confirm", args=[batch.pk])
        confirmation = self.client.get(confirm_url)
        self.assertContains(confirmation, "Это финальный шаг")
        self.assertContains(confirmation, "Подтвердить импорт")
        self.assertNotContains(confirmation, "Иванов Иван Иванович")

        missing_acknowledgement = self.client.post(confirm_url)
        self.assertEqual(missing_acknowledgement.status_code, 200)
        self.assertContains(missing_acknowledgement, "Подтвердите, что проверили")
        self.assertFalse(PayrollRow.objects.exists())

        first = self.client.post(confirm_url, {"acknowledgement": "on"})
        self.assertEqual(first.status_code, 302)
        self.assertEqual(PayrollRow.objects.count(), 4)
        second = self.client.post(confirm_url, {"acknowledgement": "on"})
        self.assertEqual(second.status_code, 200)
        self.assertContains(second, "Импорт уже подтверждён")
        self.assertNotContains(second, "Подтвердить импорт")
        self.assertEqual(PayrollRow.objects.count(), 4)
        self.assertEqual(DataAuditLog.objects.filter(
            entity_type="OneCImportBatch", entity_id=str(batch.pk),
            after__status=OneCImportBatch.STATUS_CONFIRMED,
        ).count(), 1)

    def test_stale_parser_preview_blocks_button_and_direct_confirm(self):
        upload = SimpleUploadedFile(
            "stale-payroll.xlsx", payroll_xlsx(),
            content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )
        self.client.post(reverse("finance_payroll_import_upload"), {"report": upload})
        batch = OneCImportBatch.objects.get(original_filename="stale-payroll.xlsx")
        batch.parser_version = "obsolete"
        batch.save(update_fields=["parser_version"])
        preview = self.client.get(reverse("finance_payroll_import_preview", args=[batch.pk]))
        self.assertNotContains(preview, "Перейти к подтверждению")
        self.assertContains(preview, "Предпросмотр создан устаревшей версией парсера")
        confirm_url = reverse("finance_payroll_import_confirm", args=[batch.pk])
        confirmation = self.client.get(confirm_url)
        self.assertContains(confirmation, "устаревшей версией парсера")
        self.assertNotContains(confirmation, "Подтвердить импорт")
        response = self.client.post(confirm_url, {"acknowledgement": "on"})
        self.assertEqual(response.status_code, 200)
        batch.refresh_from_db()
        self.assertEqual(batch.status, OneCImportBatch.STATUS_PREVIEWED)
        self.assertFalse(PayrollRow.objects.exists())

    def test_confirmation_audit_contains_only_safe_request_context(self):
        upload = SimpleUploadedFile(
            "audit-payroll.xlsx", payroll_xlsx(),
            content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )
        self.client.post(reverse("finance_payroll_import_upload"), {"report": upload})
        batch = OneCImportBatch.objects.get(original_filename="audit-payroll.xlsx")
        self.client.cookies["sensitive_test_cookie"] = "must-not-be-audited"
        response = self.client.post(
            reverse("finance_payroll_import_confirm", args=[batch.pk]),
            {"acknowledgement": "on", "csrfmiddlewaretoken": "must-not-be-audited"},
            REMOTE_ADDR="192.0.2.25",
            HTTP_X_FORWARDED_FOR="198.51.100.99",
            HTTP_USER_AGENT="Payroll Test Browser/1.0",
        )
        self.assertEqual(response.status_code, 302)
        audit = DataAuditLog.objects.get(
            entity_type="OneCImportBatch", entity_id=str(batch.pk),
            after__status=OneCImportBatch.STATUS_CONFIRMED,
        )
        context = audit.after["confirmation_context"]
        self.assertEqual(audit.ip_address, "192.0.2.25")
        self.assertEqual(audit.user_agent, "Payroll Test Browser/1.0")
        self.assertEqual(context["remote_ip"], "192.0.2.25")
        self.assertEqual(context["actor_user_id"], self.user.pk)
        self.assertEqual(context["organization_id"], self.organization.pk)
        self.assertEqual(context["batch_id"], str(batch.pk))
        self.assertEqual(context["batch_sha256"], batch.file_sha256)
        self.assertEqual(context["parser_version"], PAYROLL_PARSER_VERSION)
        self.assertEqual(context["rows_detected"], 4)
        self.assertEqual(context["period_first"], "2024-12-01")
        self.assertEqual(context["period_last"], "2025-01-01")
        self.assertEqual(context["route"], "finance_payroll_import_confirm")
        serialized = str(audit.after).casefold()
        self.assertNotIn("cookie", serialized)
        self.assertNotIn("sessionid", serialized)
        self.assertNotIn("csrf", serialized)
        self.assertNotIn("198.51.100.99", serialized)

    def test_critical_error_blocks_confirmation_page_and_post(self):
        upload = SimpleUploadedFile(
            "critical-payroll.xlsx", payroll_xlsx(),
            content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )
        self.client.post(reverse("finance_payroll_import_upload"), {"report": upload})
        batch = OneCImportBatch.objects.get(original_filename="critical-payroll.xlsx")
        metadata = dict(batch.metadata)
        metadata["critical_errors"] = ["Контрольная ошибка"]
        batch.metadata = metadata
        batch.save(update_fields=["metadata"])
        preview = self.client.get(reverse("finance_payroll_import_preview", args=[batch.pk]))
        self.assertContains(preview, "Контрольная ошибка")
        self.assertNotContains(preview, "Перейти к подтверждению")
        confirm_url = reverse("finance_payroll_import_confirm", args=[batch.pk])
        confirmation = self.client.get(confirm_url)
        self.assertContains(confirmation, "Подтверждение заблокировано")
        self.assertNotContains(confirmation, "Подтвердить импорт")
        post = self.client.post(confirm_url, {"acknowledgement": "on"})
        self.assertEqual(post.status_code, 200)
        batch.refresh_from_db()
        self.assertEqual(batch.status, OneCImportBatch.STATUS_PREVIEWED)
        self.assertFalse(PayrollRow.objects.exists())

    @patch("pool_service.finance_views.can_view_payroll_personal", return_value=False)
    @patch("pool_service.finance_views.can_import_payroll", return_value=True)
    def test_import_only_confirmation_is_aggregate_only(self, _import, _personal):
        upload = SimpleUploadedFile(
            "aggregate-payroll.xlsx", payroll_xlsx(),
            content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )
        self.client.post(reverse("finance_payroll_import_upload"), {"report": upload})
        batch = OneCImportBatch.objects.get(original_filename="aggregate-payroll.xlsx")
        response = self.client.get(reverse("finance_payroll_import_confirm", args=[batch.pk]))
        self.assertContains(response, "Сотрудников источника")
        self.assertContains(response, "300.00")
        self.assertNotContains(response, "Иванов Иван Иванович")
        self.assertNotContains(response, "Петров Пётр Петрович")

    def test_overlap_months_are_prominent_on_confirmation_page(self):
        existing = self._batch("overlap-existing")
        self._activate(existing, date(2024, 12, 1), date(2025, 1, 1))
        upload = SimpleUploadedFile(
            "overlap-payroll.xlsx", payroll_xlsx(),
            content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )
        self.client.post(reverse("finance_payroll_import_upload"), {"report": upload})
        batch = OneCImportBatch.objects.get(original_filename="overlap-payroll.xlsx")
        response = self.client.get(reverse("finance_payroll_import_confirm", args=[batch.pk]))
        self.assertContains(response, "Будут заменены активные данные за")
        self.assertContains(response, "2024-12")
        self.assertContains(response, "2025-01")
        self.assertContains(response, "останутся в истории")

    def test_confirmation_requires_import_permission_and_is_aggregate_for_personal_user(self):
        upload = SimpleUploadedFile(
            "permission-confirm.xlsx", payroll_xlsx(),
            content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )
        self.client.post(reverse("finance_payroll_import_upload"), {"report": upload})
        batch = OneCImportBatch.objects.get(original_filename="permission-confirm.xlsx")
        confirm_url = reverse("finance_payroll_import_confirm", args=[batch.pk])
        with self._permissions():
            self.assertEqual(self.client.get(confirm_url).status_code, 403)
            self.assertEqual(self.client.post(
                confirm_url, {"acknowledgement": "on"}
            ).status_code, 403)
        with self._permissions(import_access=True, personal=True):
            response = self.client.get(confirm_url)
            self.assertEqual(response.status_code, 200)
            self.assertContains(response, "Сотрудников источника")
            self.assertNotContains(response, "Иванов Иван Иванович")

    def test_missing_stored_file_blocks_confirmation_get_and_post(self):
        upload = SimpleUploadedFile(
            "missing-source.xlsx", payroll_xlsx(),
            content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )
        self.client.post(reverse("finance_payroll_import_upload"), {"report": upload})
        batch = OneCImportBatch.objects.get(original_filename="missing-source.xlsx")
        batch.stored_file.storage.delete(batch.stored_file.name)
        confirm_url = reverse("finance_payroll_import_confirm", args=[batch.pk])
        response = self.client.get(confirm_url)
        self.assertContains(response, "Сохранённый исходный файл недоступен")
        self.assertNotContains(response, "Подтвердить импорт")
        post = self.client.post(confirm_url, {"acknowledgement": "on"})
        self.assertEqual(post.status_code, 200)
        batch.refresh_from_db()
        self.assertEqual(batch.status, OneCImportBatch.STATUS_PREVIEWED)
        self.assertFalse(PayrollRow.objects.exists())

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
        self.assertEqual(self.client.post(
            reverse("finance_payroll_import_confirm", args=[other_batch.pk])
        ).status_code, 404)
        response = self.client.post(
            reverse("finance_payroll_employee_map", args=[other_identity.pk]),
            {"employee": other_employee.pk},
        )
        self.assertEqual(response.status_code, 404)

    def test_foreign_employee_id_is_rejected_for_local_identity(self):
        identity = self._identity()
        foreign = Employee.objects.create(organization=self.other, display_name="Чужой")
        response = self.client.post(
            reverse("finance_payroll_employee_map", args=[identity.pk]),
            {"employee": foreign.pk},
        )
        self.assertEqual(response.status_code, 302)
        identity.refresh_from_db()
        self.assertIsNone(identity.employee)

    def test_dashboard_excludes_other_organization_active_rows(self):
        own_identity = self._identity("Свой")
        own_batch = self._batch("own")
        self._row(own_batch, own_identity, date(2026, 1, 1), 1, 10, 5, 6)
        self._activate(own_batch, date(2026, 1, 1))
        other_user = User.objects.create_user("dashboard-other")
        other_identity = EmployeeOneCIdentity.objects.create(
            organization=self.other, raw_name="Чужой", normalized_name="чужой",
            source_identity_key="b" * 64, status=EmployeeOneCIdentity.STATUS_NOT_FOUND,
        )
        other_batch = OneCImportBatch.objects.create(
            organization=self.other, import_type=OneCImportBatch.TYPE_PAYROLL,
            original_filename="foreign.xlsx", stored_file="test/foreign.xlsx",
            file_sha256="9" * 64, file_size=1,
            status=OneCImportBatch.STATUS_CONFIRMED, uploaded_by=other_user,
        )
        PayrollRow.objects.create(
            import_batch=other_batch, organization=self.other, employee_identity=other_identity,
            period_month=date(2026, 1, 1), source_row_number=1,
            employee_raw_name="Чужой", employee_normalized_name="чужой",
            opening_balance=999, accrued=999, paid=999, closing_balance=999,
        )
        OneCReportPeriodState.objects.create(
            organization=self.other, report_type=OneCImportBatch.TYPE_PAYROLL,
            period_month=date(2026, 1, 1), active_batch=other_batch, updated_by=other_user,
        )
        data = payroll_dashboard_data(self.organization, date(2026, 1, 1), date(2026, 1, 1))
        self.assertEqual(data["accrued"], Decimal("10"))

    def test_missing_boundary_months_remain_none_and_visible(self):
        identity = self._identity()
        batch = self._batch("boundary")
        self._row(batch, identity, date(2026, 2, 1), 10, 20, 5, 25)
        self._activate(batch, date(2026, 2, 1))
        data = payroll_dashboard_data(self.organization, date(2026, 1, 1), date(2026, 3, 1))
        self.assertIsNone(data["opening"])
        self.assertIsNone(data["closing"])
        self.assertIsNone(data["debt_change"])
        self.assertFalse(data["months"][0]["has_data"])
        self.assertFalse(data["months"][2]["has_data"])

    def test_permission_matrix_and_combinations(self):
        identity = self._identity()
        batch = self._batch("permissions")
        month = date.today().replace(month=1, day=1)
        self._row(batch, identity, month, 1, 777777, 2, 3)
        self._activate(batch, month)
        dashboard = reverse("finance_payroll_dashboard")
        imports = reverse("finance_payroll_import_list")
        mapping = reverse("finance_payroll_employee_mapping")

        with self._permissions(summary=True):
            response = self.client.get(dashboard)
            self.assertEqual(response.status_code, 200)
            self.assertNotContains(response, identity.raw_name)
            self.assertEqual(self.client.get(imports).status_code, 403)
            self.assertEqual(self.client.get(mapping).status_code, 403)
        with self._permissions(summary=True, personal=True):
            self.assertContains(self.client.get(dashboard), identity.raw_name)
            self.assertEqual(self.client.get(imports).status_code, 403)
            self.assertEqual(self.client.get(mapping).status_code, 403)
        with self._permissions(import_access=True):
            self.assertEqual(self.client.get(imports).status_code, 200)
            self.assertEqual(self.client.get(dashboard).status_code, 403)
        with self._permissions(mapping=True):
            response = self.client.get(mapping)
            self.assertEqual(response.status_code, 200)
            self.assertNotContains(response, "777777")
            self.assertEqual(self.client.get(dashboard).status_code, 403)
        with self._permissions(summary=True, import_access=True):
            self.assertEqual(self.client.get(dashboard).status_code, 200)
            self.assertEqual(self.client.get(imports).status_code, 200)
            self.assertNotContains(self.client.get(dashboard), identity.raw_name)
        with self._permissions(summary=True, mapping=True):
            self.assertEqual(self.client.get(dashboard).status_code, 200)
            self.assertEqual(self.client.get(mapping).status_code, 200)
            self.assertNotContains(self.client.get(dashboard), identity.raw_name)
        with self._permissions(summary=True, personal=True, import_access=True, mapping=True):
            self.assertContains(self.client.get(dashboard), identity.raw_name)
            self.assertEqual(self.client.get(imports).status_code, 200)
            self.assertEqual(self.client.get(mapping).status_code, 200)
        with self._permissions():
            self.assertEqual(self.client.get(dashboard).status_code, 403)
            self.assertEqual(self.client.get(imports).status_code, 403)
            self.assertEqual(self.client.get(mapping).status_code, 403)

    def test_payroll_navigation_uses_first_accessible_destination(self):
        dashboard = reverse("finance_payroll_dashboard")
        imports = reverse("finance_payroll_import_list")
        mapping = reverse("finance_payroll_employee_mapping")
        label = "ФОТ / Расчёты с персоналом"

        with self._permissions(summary=True):
            response = self.client.get(dashboard)
            self.assertContains(response, f'href="{dashboard}"')
            self.assertContains(response, label)
        with self._permissions(import_access=True):
            response = self.client.get(imports)
            self.assertContains(response, f'href="{imports}"')
            self.assertContains(response, label)
        with self._permissions(mapping=True):
            response = self.client.get(mapping)
            self.assertContains(response, f'href="{mapping}"')
            self.assertContains(response, label)
        with self._permissions():
            response = self.client.get(reverse("finance_dashboard"))
            self.assertNotContains(response, label)

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
