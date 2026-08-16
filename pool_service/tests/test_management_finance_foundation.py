from datetime import timedelta
from decimal import Decimal
from io import BytesIO
from tempfile import TemporaryDirectory

from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import IntegrityError, transaction
from django.test import TestCase, override_settings
from django.utils import timezone
from openpyxl import load_workbook

from pool_service.finance_imports.cashflow_parser import CashFlowParseError, parse_cashflow
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
from pool_service.finance_imports.payroll_parser import PayrollParseError
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
from pool_service.tests.fixtures.management_finance import (
    cashflow_xlsx,
    payroll_160_rows_xlsx,
    payroll_unmatched_months_xlsx,
    payroll_xlsx,
)


def upload(name, data):
    return SimpleUploadedFile(
        name, data,
        content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )


def replace_cell(data, coordinate, value):
    source = BytesIO(data)
    workbook = load_workbook(source)
    workbook.active[coordinate] = value
    output = BytesIO()
    workbook.save(output)
    return output.getvalue()


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

    def test_database_uniqueness_uses_null_semantics(self):
        Employee.objects.create(
            organization=self.organization, display_name="Без пользователя 1", user=None
        )
        Employee.objects.create(
            organization=self.organization, display_name="Без пользователя 2", user=None
        )
        Employee.objects.create(
            organization=self.organization, display_name="С пользователем", user=self.user
        )
        Employee.objects.create(
            organization=self.other, display_name="Другой tenant", user=self.user
        )
        with self.assertRaises(IntegrityError), transaction.atomic():
            Employee.objects.create(
                organization=self.organization, display_name="Дубликат", user=self.user
            )

        for suffix in ("one", "two"):
            EmployeeOneCIdentity.objects.create(
                organization=self.organization,
                raw_name=f"Нет идентификаторов {suffix}",
                normalized_name=f"нет идентификаторов {suffix}",
                onec_employee_id=None,
                personnel_number=None,
                source_identity_key=None,
                status=EmployeeOneCIdentity.STATUS_NOT_FOUND,
            )

        for field, value in (
            ("onec_employee_id", "onec-unique"),
            ("personnel_number", "personnel-unique"),
            ("source_identity_key", "a" * 64),
        ):
            values = {
                "organization": self.organization,
                "raw_name": field,
                "normalized_name": field,
                "status": EmployeeOneCIdentity.STATUS_NOT_FOUND,
                field: value,
            }
            EmployeeOneCIdentity.objects.create(**values)
            EmployeeOneCIdentity.objects.create(
                **{**values, "organization": self.other, "raw_name": f"other-{field}"}
            )
            with self.assertRaises(IntegrityError), transaction.atomic():
                EmployeeOneCIdentity.objects.create(
                    **{**values, "raw_name": f"duplicate-{field}"}
                )

    def test_empty_optional_identifiers_are_persisted_as_null(self):
        identity = EmployeeOneCIdentity.objects.create(
            organization=self.organization,
            raw_name="Пустые идентификаторы",
            normalized_name="пустые идентификаторы",
            onec_employee_id="  ",
            personnel_number="",
            source_identity_key="",
            status=EmployeeOneCIdentity.STATUS_NOT_FOUND,
        )
        identity.refresh_from_db()
        self.assertIsNone(identity.onec_employee_id)
        self.assertIsNone(identity.personnel_number)
        self.assertIsNone(identity.source_identity_key)

    def test_model_uses_canonical_stable_id_but_preserves_source_key_interior(self):
        identity = EmployeeOneCIdentity.objects.create(
            organization=self.organization,
            raw_name="Нормализация",
            normalized_name="нормализация",
            onec_employee_id=" AB\t  123 ",
            personnel_number=" 123\n 45 ",
            source_identity_key=" source  key ",
            status=EmployeeOneCIdentity.STATUS_NOT_FOUND,
        )
        identity.refresh_from_db()
        self.assertEqual(identity.onec_employee_id, "AB 123")
        self.assertEqual(identity.personnel_number, "123 45")
        self.assertEqual(identity.source_identity_key, "source  key")

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

    def test_conflicting_stable_ids_fail_closed(self):
        first = self.employee("Первый Сотрудник")
        second = self.employee("Второй Сотрудник")
        EmployeeOneCIdentity.objects.create(
            organization=self.organization,
            employee=first,
            raw_name=first.display_name,
            normalized_name=normalize_onec_name(first.display_name),
            onec_employee_id="onec-conflict",
            status=EmployeeOneCIdentity.STATUS_MANUALLY_MATCHED,
        )
        EmployeeOneCIdentity.objects.create(
            organization=self.organization,
            employee=second,
            raw_name=second.display_name,
            normalized_name=normalize_onec_name(second.display_name),
            personnel_number="personnel-conflict",
            status=EmployeeOneCIdentity.STATUS_MANUALLY_MATCHED,
        )
        with self.assertRaises(ValidationError):
            resolve_employee_identity(
                self.organization,
                "Любое имя",
                onec_employee_id="onec-conflict",
                personnel_number="personnel-conflict",
            )

    def test_fallback_identity_is_isolated_by_organization(self):
        first = resolve_employee_identity(self.organization, "Шукшин Илья Сергеевич")
        second = resolve_employee_identity(self.other, " ШУКШИН  ИЛЬЯ СЕРГЕЕВИЧ ")
        self.assertNotEqual(first.pk, second.pk)
        self.assertEqual(first.source_identity_key, second.source_identity_key)
        self.assertNotEqual(first.organization_id, second.organization_id)

    def test_fallback_identity_is_enriched_when_stable_identifier_appears(self):
        first = resolve_employee_identity(
            self.organization,
            "Шукшин Илья Сергеевич",
            department_name="Сервис",
        )
        source_identity_key = first.source_identity_key

        enriched = resolve_employee_identity(
            self.organization,
            " ШУКШИН  ИЛЬЯ СЕРГЕЕВИЧ ",
            department_name=" сервис ",
            onec_employee_id="  onec-17  ",
        )

        self.assertEqual(enriched.pk, first.pk)
        self.assertEqual(enriched.onec_employee_id, "onec-17")
        self.assertEqual(enriched.source_identity_key, source_identity_key)
        self.assertEqual(EmployeeOneCIdentity.objects.count(), 1)

    def test_new_stable_identity_does_not_store_fallback_key(self):
        identity = resolve_employee_identity(
            self.organization,
            "Стабильный Сотрудник",
            onec_employee_id="onec-stable",
        )
        self.assertIsNone(identity.source_identity_key)

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
        self.assertEqual(parsed.records[0]["source_row_number"], 4)
        self.assertNotIn(
            "подразделение",
            {row["employee_normalized_name"] for row in parsed.records},
        )
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

    def test_malformed_payroll_header_fails_closed(self):
        malformed = replace_cell(payroll_xlsx(), "A1", "Неизвестная структура")
        with self.assertRaises(PayrollParseError):
            create_payroll_preview(
                upload("bad-payroll.xlsx", malformed), self.organization, self.user
            )

    def test_unmatched_source_identity_is_reused_for_all_months_and_replacement(self):
        source = payroll_unmatched_months_xlsx()
        first = create_payroll_preview(
            upload("unmatched.xlsx", source), self.organization, self.user
        )
        confirm_payroll(first.pk, self.organization, self.user)
        rows = PayrollRow.objects.filter(import_batch=first)
        identity_ids = set(rows.values_list("employee_identity_id", flat=True))
        self.assertEqual(rows.count(), 12)
        self.assertEqual(len(identity_ids), 1)
        identity = EmployeeOneCIdentity.objects.get(pk=identity_ids.pop())
        self.assertEqual(identity.status, EmployeeOneCIdentity.STATUS_NOT_FOUND)

        replacement = create_payroll_preview(
            upload("unmatched-new.xlsx", payroll_unmatched_months_xlsx(accrued_delta=1)),
            self.organization,
            self.user,
        )
        confirm_payroll(replacement.pk, self.organization, self.user)
        self.assertEqual(EmployeeOneCIdentity.objects.count(), 1)
        self.assertEqual(
            set(PayrollRow.objects.values_list("employee_identity_id", flat=True)),
            {identity.pk},
        )

    def test_ambiguous_identity_is_reused_and_manual_remap_resolves_all_rows(self):
        for suffix in ("A", "B"):
            Employee.objects.create(
                organization=self.organization,
                display_name="Шукшин Илья Сергеевич",
                first_name="Илья",
                last_name="Шукшин",
                middle_name="Сергеевич",
                position_name=suffix,
            )
        batch = create_payroll_preview(
            upload("ambiguous.xlsx", payroll_unmatched_months_xlsx()),
            self.organization,
            self.user,
        )
        confirm_payroll(batch.pk, self.organization, self.user)
        rows = PayrollRow.objects.filter(import_batch=batch)
        identity_ids = set(rows.values_list("employee_identity_id", flat=True))
        self.assertEqual(len(identity_ids), 1)
        identity = EmployeeOneCIdentity.objects.get(pk=identity_ids.pop())
        self.assertEqual(identity.status, EmployeeOneCIdentity.STATUS_AMBIGUOUS)
        self.assertIsNone(identity.employee)

        selected = Employee.objects.order_by("id").first()
        confirm_employee_identity(identity, selected, self.user)
        self.assertEqual(
            PayrollRow.objects.filter(
                import_batch=batch, employee_identity__employee=selected
            ).count(),
            12,
        )

    def test_real_shape_fixture_creates_one_identity_per_distinct_source_employee(self):
        batch = create_payroll_preview(
            upload("payroll-160.xlsx", payroll_160_rows_xlsx()),
            self.organization,
            self.user,
        )
        confirm_payroll(batch.pk, self.organization, self.user)
        self.assertEqual(PayrollRow.objects.filter(import_batch=batch).count(), 160)
        self.assertEqual(EmployeeOneCIdentity.objects.count(), 17)
        self.assertEqual(
            PayrollRow.objects.filter(import_batch=batch)
            .values("employee_identity_id").distinct().count(),
            17,
        )

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
        self.assertEqual(parsed.records[0]["source_row_number"], 6)
        labels = {row["normalized_article_name"] for row in parsed.records}
        documents = {row["document_raw"].casefold() for row in parsed.records}
        self.assertNotIn("статья", labels)
        self.assertNotIn("документ движения", documents)
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

    def test_malformed_cashflow_header_fails_closed(self):
        malformed = replace_cell(cashflow_xlsx(), "A2", "Неверный уровень")
        with self.assertRaises(CashFlowParseError):
            create_cashflow_preview(
                upload("bad-cashflow.xlsx", malformed), self.organization, self.user
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
