from datetime import timedelta
from decimal import Decimal
from tempfile import TemporaryDirectory

from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.utils import timezone

from pool_service.finance_imports.cashflow_parser import parse_cashflow
from pool_service.finance_imports.cashflow_services import (
    confirm_cashflow,
    create_cashflow_preview,
)
from pool_service.finance_imports.employee_matching import (
    confirm_employee_identity,
    normalize_onec_name,
    resolve_employee_identity,
)
from pool_service.finance_imports.payroll_parser import PARSER_VERSION as PAYROLL_VERSION
from pool_service.finance_imports.payroll_parser import parse_payroll
from pool_service.finance_imports.payroll_services import (
    confirm_payroll,
    create_payroll_preview,
)
from pool_service.models import (
    CashFlowArticleMapping,
    CashFlowRow,
    Employee,
    EmployeeOneCIdentity,
    OneCImportBatch,
    OneCReportPeriodActivation,
    OneCReportPeriodState,
    Organization,
    OrganizationAccess,
    PayrollRow,
)
from pool_service.services.finance import (
    can_import_cashflow,
    can_import_payroll,
    can_manage_cashflow_classification,
    can_manage_employee_mapping,
    can_view_cashflow,
    can_view_payroll_personal,
    can_view_payroll_summary,
)
from pool_service.tests.fixtures.management_finance import cashflow_xlsx, payroll_xlsx


def upload(name, data):
    return SimpleUploadedFile(
        name, data,
        content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )


class EmployeeFoundationTests(TestCase):
    def setUp(self):
        self.organization = Organization.objects.create(name="Организация")
        self.other = Organization.objects.create(name="Другая")
        self.user = User.objects.create_user("employee")

    def employee(self, name, *, organization=None, active=True):
        parts = name.split()
        return Employee.objects.create(
            organization=organization or self.organization,
            display_name=name,
            last_name=parts[0],
            first_name=parts[1] if len(parts) > 1 else "",
            middle_name=parts[2] if len(parts) > 2 else "",
            is_active=active,
        )

    def test_employee_survives_user_removal_and_is_tenant_scoped(self):
        employee = self.employee("Иванов Иван Иванович")
        employee.user = self.user
        employee.save()
        self.user.delete()
        employee.refresh_from_db()
        self.assertIsNone(employee.user)
        self.assertEqual(Employee.objects.filter(organization=self.organization).count(), 1)
        self.assertFalse(Employee.objects.filter(organization=self.other).exists())

    def test_duplicate_normalized_identities_are_allowed(self):
        for suffix in ("а", "б"):
            EmployeeOneCIdentity.objects.create(
                organization=self.organization,
                raw_name=f"Иванов Иван {suffix}",
                normalized_name="иванов иван",
                status=EmployeeOneCIdentity.STATUS_NOT_FOUND,
            )
        self.assertEqual(
            EmployeeOneCIdentity.objects.filter(normalized_name="иванов иван").count(), 2
        )

    def test_normalization_ambiguity_no_partial_match_and_no_employee_creation(self):
        self.employee("Семёнов Иван Иванович")
        self.employee("Семенов Иван Иванович")
        before = Employee.objects.count()
        ambiguous = resolve_employee_identity(
            self.organization, "  СЕМЕНОВ   ИВАН ИВАНОВИЧ  "
        )
        partial = resolve_employee_identity(self.organization, "Семенов Иван")
        self.assertEqual(normalize_onec_name(" СЕМЁНОВ  Иван "), "семенов иван")
        self.assertEqual(ambiguous.status, EmployeeOneCIdentity.STATUS_AMBIGUOUS)
        self.assertIsNone(ambiguous.employee)
        self.assertEqual(partial.status, EmployeeOneCIdentity.STATUS_NOT_FOUND)
        self.assertIsNone(partial.employee)
        self.assertEqual(Employee.objects.count(), before)

    def test_stable_ids_override_name_ambiguity_and_confirmed_mapping_is_reused(self):
        selected = self.employee("Иванов Иван Иванович")
        self.employee("Иванов Иван Иванович")
        identity = EmployeeOneCIdentity.objects.create(
            organization=self.organization,
            employee=selected,
            raw_name="Иванов Иван Иванович",
            normalized_name="иванов иван иванович",
            onec_employee_id="onec-1",
            personnel_number="007",
            status=EmployeeOneCIdentity.STATUS_MANUALLY_MATCHED,
            match_method=EmployeeOneCIdentity.MATCH_MANUAL,
        )
        self.assertEqual(
            resolve_employee_identity(
                self.organization, "Другое имя", onec_employee_id="onec-1"
            ).pk,
            identity.pk,
        )
        self.assertEqual(
            resolve_employee_identity(
                self.organization, "Другое имя", personnel_number="007"
            ).pk,
            identity.pk,
        )
        self.assertEqual(
            resolve_employee_identity(self.organization, "Иванов Иван Иванович").pk,
            identity.pk,
        )

    def test_inactive_employee_matches_historically_and_cross_org_mapping_is_rejected(self):
        employee = self.employee("Петров Пётр Петрович", active=False)
        identity = resolve_employee_identity(self.organization, "Петров Петр Петрович")
        self.assertEqual(identity.employee, employee)
        foreign = self.employee("Чужой Сотрудник", organization=self.other)
        identity.employee = foreign
        with self.assertRaises(ValidationError):
            identity.full_clean()
        with self.assertRaises(ValidationError):
            confirm_employee_identity(identity, foreign, self.user)

    def test_manual_remap_has_audit_trail(self):
        employee = self.employee("Новый Сотрудник")
        identity = resolve_employee_identity(self.organization, "Неизвестный")
        remapped = confirm_employee_identity(identity, employee, self.user, comment="Проверено")
        self.assertEqual(remapped.employee, employee)
        self.assertEqual(remapped.status, EmployeeOneCIdentity.STATUS_MANUALLY_MATCHED)
        self.assertTrue(remapped.confirmed_at)
        self.assertTrue(remapped.organization.dataauditlog_set.filter(
            entity_type="EmployeeOneCIdentity", entity_id=str(identity.pk)
        ).exists())


class FoundationImportTests(TestCase):
    def setUp(self):
        self.private_dir = TemporaryDirectory()
        self.override = override_settings(PRIVATE_MEDIA_ROOT=self.private_dir.name)
        self.override.enable()
        self.addCleanup(self.override.disable)
        self.addCleanup(self.private_dir.cleanup)
        self.organization = Organization.objects.create(
            name="Организация", paid_until=timezone.now() + timedelta(days=30)
        )
        self.user = User.objects.create_user("owner")
        OrganizationAccess.objects.create(
            organization=self.organization, user=self.user, role="owner"
        )

    def test_anonymized_payroll_fixture_distinguishes_subtotals_and_preserves_unmatched(self):
        parsed = parse_payroll(
            upload("payroll.xlsx", payroll_xlsx()), filename="payroll.xlsx"
        )
        self.assertEqual(len(parsed.records), 4)
        self.assertFalse(parsed.critical_errors)
        self.assertEqual(parsed.metadata["control_totals"]["accrued"], "300.00")
        batch = create_payroll_preview(
            upload("payroll.xlsx", payroll_xlsx()), self.organization, self.user
        )
        self.assertFalse(PayrollRow.objects.exists())
        confirm_payroll(batch.pk, self.organization, self.user)
        self.assertEqual(PayrollRow.objects.count(), 4)
        self.assertEqual(
            sum(PayrollRow.objects.values_list("accrued", flat=True), Decimal("0")),
            Decimal("300.00"),
        )
        self.assertTrue(PayrollRow.objects.filter(employee_identity__employee__isnull=True).exists())

    def test_payroll_duplicate_versioning_and_parser_lock(self):
        source = payroll_xlsx()
        batch = create_payroll_preview(
            upload("payroll.xlsx", source), self.organization, self.user
        )
        with self.assertRaises(ValidationError):
            create_payroll_preview(
                upload("same.xlsx", source), self.organization, self.user
            )
        batch.parser_version = "obsolete"
        batch.save(update_fields=["parser_version"])
        with self.assertRaises(ValidationError):
            confirm_payroll(batch.pk, self.organization, self.user)
        self.assertFalse(PayrollRow.objects.exists())
        refreshed = create_payroll_preview(
            upload("same.xlsx", source), self.organization, self.user
        )
        self.assertEqual(refreshed.parser_version, PAYROLL_VERSION)
        confirm_payroll(refreshed.pk, self.organization, self.user)
        replacement = create_payroll_preview(
            upload("replacement.xlsx", payroll_xlsx(accrued_delta=1)),
            self.organization,
            self.user,
        )
        confirm_payroll(replacement.pk, self.organization, self.user)
        states = OneCReportPeriodState.objects.filter(
            organization=self.organization, report_type=OneCImportBatch.TYPE_PAYROLL
        )
        self.assertTrue(states.filter(active_batch=replacement).exists())
        self.assertTrue(OneCReportPeriodActivation.objects.filter(
            batch=replacement, replaced_batch=refreshed
        ).exists())
        self.assertEqual(
            PayrollRow.objects.active_for(
                self.organization, OneCImportBatch.TYPE_PAYROLL
            ).filter(import_batch=replacement).count(),
            4,
        )

    def test_cashflow_fixture_persists_only_documents_and_exact_totals(self):
        parsed = parse_cashflow(
            upload("cashflow.xlsx", cashflow_xlsx()), filename="cashflow.xlsx"
        )
        self.assertEqual(len(parsed.records), 4)
        self.assertFalse(parsed.critical_errors)
        self.assertEqual(parsed.metadata["control_totals"], {
            "receipts": "60.00", "payments": "150.00", "net_cash_flow": "-90.00",
        })
        batch = create_cashflow_preview(
            upload("cashflow.xlsx", cashflow_xlsx()), self.organization, self.user
        )
        confirm_cashflow(batch.pk, self.organization, self.user)
        self.assertEqual(CashFlowRow.objects.count(), 4)
        self.assertFalse(CashFlowRow.objects.filter(document_raw__in=[
            "Заработная плата", "01.2025", "Внутреннее перемещение", "02.2025",
        ]).exists())
        source_row = CashFlowRow.objects.order_by("id").first()
        source_values = (
            source_row.article_raw, source_row.receipts,
            source_row.payments, source_row.net_cash_flow,
        )
        mapping = CashFlowArticleMapping.objects.create(
            organization=self.organization,
            article_name=source_row.article_raw,
            normalized_article_name=source_row.normalized_article_name,
        )
        mapping.flow_type = CashFlowArticleMapping.FLOW_OPERATING
        mapping.classification_status = CashFlowArticleMapping.CLASS_CONFIRMED
        mapping.save()
        source_row.refresh_from_db()
        self.assertEqual(
            (
                source_row.article_raw, source_row.receipts,
                source_row.payments, source_row.net_cash_flow,
            ),
            source_values,
        )

    def test_cashflow_parser_lock_and_version_replacement(self):
        source = cashflow_xlsx()
        first = create_cashflow_preview(
            upload("cashflow.xlsx", source), self.organization, self.user
        )
        first.parser_version = "obsolete"
        first.save(update_fields=["parser_version"])
        with self.assertRaises(ValidationError):
            confirm_cashflow(first.pk, self.organization, self.user)
        self.assertFalse(CashFlowRow.objects.exists())
        first = create_cashflow_preview(
            upload("cashflow.xlsx", source), self.organization, self.user
        )
        confirm_cashflow(first.pk, self.organization, self.user)
        with self.assertRaises(ValidationError):
            create_cashflow_preview(
                upload("confirmed-duplicate.xlsx", source), self.organization, self.user
            )
        second = create_cashflow_preview(
            upload("cashflow-new.xlsx", cashflow_xlsx(payment_delta=1)),
            self.organization,
            self.user,
        )
        confirm_cashflow(second.pk, self.organization, self.user)
        self.assertTrue(OneCReportPeriodActivation.objects.filter(
            batch=second, replaced_batch=first
        ).exists())


class ClassificationAndPermissionTests(TestCase):
    def setUp(self):
        self.organization = Organization.objects.create(name="Организация")
        self.other = Organization.objects.create(name="Другая")

    def test_classification_dimensions_are_independent_and_do_not_change_fact(self):
        mapping = CashFlowArticleMapping.objects.create(
            organization=self.organization,
            article_name="Александр",
            normalized_article_name="александр",
            management_category="Дивиденды",
            flow_type=CashFlowArticleMapping.FLOW_FINANCING,
            pnl_treatment=CashFlowArticleMapping.PNL_EXCLUDE,
            classification_status=CashFlowArticleMapping.CLASS_CONFIRMED,
            is_internal_turnover=False,
            include_in_external_cashflow=True,
            is_dividend=True,
        )
        original = (mapping.flow_type, mapping.pnl_treatment, mapping.is_dividend)
        mapping.is_internal_turnover = True
        mapping.include_in_external_cashflow = False
        mapping.save()
        self.assertEqual(
            (mapping.flow_type, mapping.pnl_treatment, mapping.is_dividend), original
        )
        self.assertFalse(CashFlowArticleMapping.objects.filter(
            organization=self.other, normalized_article_name="александр"
        ).exists())
        unclassified = CashFlowArticleMapping.objects.create(
            organization=self.organization,
            article_name="Новая статья",
            normalized_article_name="новая статья",
        )
        self.assertEqual(unclassified.flow_type, CashFlowArticleMapping.FLOW_UNCLASSIFIED)

    def test_management_roles_allowed_but_manager_has_no_payroll_access(self):
        guards = (
            can_view_cashflow, can_import_cashflow, can_manage_cashflow_classification,
            can_view_payroll_summary, can_view_payroll_personal, can_import_payroll,
            can_manage_employee_mapping,
        )
        for role in ("owner", "admin", "accountant", "manager"):
            user = User.objects.create_user(role)
            OrganizationAccess.objects.create(
                organization=self.organization, user=user, role=role
            )
            expected = role != "manager"
            for guard in guards:
                with self.subTest(role=role, guard=guard.__name__):
                    self.assertEqual(guard(user, self.organization), expected)
                    self.assertFalse(guard(user, self.other))
