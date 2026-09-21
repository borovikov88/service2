from datetime import date, timedelta
from decimal import Decimal
import uuid

from django.contrib.auth.models import Permission, User
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from pool_service.finance_imports.employee_matching import resolve_employee_identity
from pool_service.models import (
    Employee,
    EmployeeOneCIdentity,
    OneCImportBatch,
    Organization,
    OrganizationAccess,
    PayrollPlanItem,
    PayrollPlanSnapshot,
    PayrollRow,
)
from pool_service.services.employee_hr import (
    bootstrap_employee_profiles,
    consolidate_duplicate_employee_identities,
    employee_current_plan,
)


ONEC_ID = "5fd3a2ac-c2ab-11ef-9fc3-fa163e1420a5"
ORG_GUID = uuid.UUID("afded18c-c07f-11ef-9fc3-fa163e1420a5")
CURRENCY_GUID = uuid.UUID("c26a4d87-c6e2-4aca-ab05-1b02be6ecaec")


class EmployeeIdentityRepairTests(TestCase):
    def setUp(self):
        self.organization = Organization.objects.create(
            name="Employee HR Test",
            paid_until=timezone.now() + timedelta(days=30),
        )
        self.actor = User.objects.create_user(
            "owner",
            password="password",
            first_name="Александр",
            last_name="Боровиков",
        )
        OrganizationAccess.objects.create(
            organization=self.organization,
            user=self.actor,
            role="admin",
        )

    def legacy_identity(self, name="Боровиков Александр Юрьевич"):
        return EmployeeOneCIdentity.objects.create(
            organization=self.organization,
            raw_name=name,
            normalized_name=name.casefold(),
            normalized_department_name="основное подразделение",
            source_identity_key="a" * 64,
            department_name="Основное подразделение",
            status=EmployeeOneCIdentity.STATUS_NOT_FOUND,
            match_method=EmployeeOneCIdentity.MATCH_NONE,
        )

    def stable_identity(self, name="Боровиков Александр Юрьевич"):
        return EmployeeOneCIdentity.objects.create(
            organization=self.organization,
            raw_name=name,
            normalized_name=name.casefold(),
            normalized_department_name="",
            onec_employee_id=ONEC_ID,
            department_name="",
            status=EmployeeOneCIdentity.STATUS_NOT_FOUND,
            match_method=EmployeeOneCIdentity.MATCH_NONE,
        )

    def test_stable_1c_id_enriches_single_legacy_identity_instead_of_duplicate(self):
        legacy = self.legacy_identity()

        resolved = resolve_employee_identity(
            self.organization,
            legacy.raw_name,
            onec_employee_id=ONEC_ID,
        )

        self.assertEqual(resolved.pk, legacy.pk)
        resolved.refresh_from_db()
        self.assertEqual(resolved.onec_employee_id, ONEC_ID)
        self.assertEqual(
            EmployeeOneCIdentity.objects.filter(
                organization=self.organization,
                normalized_name=legacy.normalized_name,
            ).count(),
            1,
        )
        self.assertEqual(resolved.department_name, "Основное подразделение")

    def test_duplicate_merge_preserves_payroll_and_plan_references(self):
        legacy = self.legacy_identity()
        stable = self.stable_identity()
        batch = OneCImportBatch.objects.create(
            organization=self.organization,
            import_type=OneCImportBatch.TYPE_PAYROLL,
            original_filename="payroll.xlsx",
            stored_file="test/payroll.xlsx",
            file_sha256="b" * 64,
            file_size=1,
            status=OneCImportBatch.STATUS_CONFIRMED,
            uploaded_by=self.actor,
        )
        payroll = PayrollRow.objects.create(
            import_batch=batch,
            organization=self.organization,
            employee_identity=stable,
            period_month=date(2026, 9, 1),
            source_row_number=1,
            department_name="",
            employee_raw_name=stable.raw_name,
            employee_normalized_name=stable.normalized_name,
            opening_balance=Decimal("0"),
            accrued=Decimal("100"),
            paid=Decimal("90"),
            closing_balance=Decimal("10"),
        )
        snapshot = PayrollPlanSnapshot.objects.create(
            organization=self.organization,
            period_month=date(2026, 9, 1),
            source_hash="c" * 64,
            source_rows=1,
            source_organization_guids=[str(ORG_GUID)],
            currency_guid=CURRENCY_GUID,
            fetched_by=self.actor,
        )
        plan_item = PayrollPlanItem.objects.create(
            snapshot=snapshot,
            employee_identity=stable,
            onec_employee_id=ONEC_ID,
            employee_raw_name=stable.raw_name,
            accrual_type_id="salary",
            accrual_type_name="Оклад",
            amount=Decimal("60000"),
            is_base_salary=True,
            source_period=date(2026, 6, 4),
            source_organization_guid=ORG_GUID,
        )

        result = consolidate_duplicate_employee_identities(
            self.organization,
            actor=self.actor,
        )

        self.assertEqual(result["merged"], [(stable.pk, legacy.pk)])
        self.assertFalse(EmployeeOneCIdentity.objects.filter(pk=stable.pk).exists())
        legacy.refresh_from_db()
        payroll.refresh_from_db()
        plan_item.refresh_from_db()
        self.assertEqual(legacy.onec_employee_id, ONEC_ID)
        self.assertEqual(legacy.department_name, "Основное подразделение")
        self.assertEqual(payroll.employee_identity_id, legacy.pk)
        self.assertEqual(plan_item.employee_identity_id, legacy.pk)

    def test_bootstrap_creates_one_employee_and_links_unique_service2_account(self):
        self.legacy_identity()
        self.stable_identity()

        result = bootstrap_employee_profiles(
            self.organization,
            actor=self.actor,
        )

        self.assertEqual(len(result["created_employee_ids"]), 1)
        employee = Employee.objects.get(organization=self.organization)
        self.assertEqual(employee.display_name, "Боровиков Александр Юрьевич")
        self.assertEqual(employee.department_name, "Основное подразделение")
        self.assertEqual(employee.user_id, self.actor.pk)
        identities = list(
            EmployeeOneCIdentity.objects.filter(organization=self.organization)
        )
        self.assertEqual(len(identities), 1)
        self.assertEqual(identities[0].employee_id, employee.pk)
        self.assertEqual(
            identities[0].status,
            EmployeeOneCIdentity.STATUS_AUTO_MATCHED,
        )


class EmployeeHRCardTests(TestCase):
    def setUp(self):
        self.organization = Organization.objects.create(
            name="Employee HR UI",
            paid_until=timezone.now() + timedelta(days=30),
        )
        self.user = User.objects.create_user(
            "hr-owner",
            password="password",
            first_name="Александр",
            last_name="Боровиков",
        )
        OrganizationAccess.objects.create(
            organization=self.organization,
            user=self.user,
            role="admin",
        )
        self.client.force_login(self.user)
        self.employee = Employee.objects.create(
            organization=self.organization,
            user=self.user,
            display_name="Боровиков Александр Юрьевич",
            first_name="Александр",
            last_name="Боровиков",
            middle_name="Юрьевич",
            department_name="Основное подразделение",
            hired_at=date(2020, 1, 15),
        )
        self.identity = EmployeeOneCIdentity.objects.create(
            organization=self.organization,
            employee=self.employee,
            raw_name=self.employee.display_name,
            normalized_name=self.employee.display_name.casefold(),
            normalized_department_name="основное подразделение",
            source_identity_key="d" * 64,
            onec_employee_id=ONEC_ID,
            department_name="Основное подразделение",
            status=EmployeeOneCIdentity.STATUS_AUTO_MATCHED,
            match_method=EmployeeOneCIdentity.MATCH_EXTERNAL_ID,
        )

    def grant_hr(self):
        permission = Permission.objects.get(
            codename="view_employee_hr",
            content_type__app_label="pool_service",
            content_type__model="employee",
        )
        self.user.user_permissions.add(permission)

    def test_hr_pages_require_explicit_permission_not_admin_role(self):
        self.assertEqual(
            self.client.get(reverse("finance_payroll_employee_list")).status_code,
            403,
        )
        self.assertEqual(
            self.client.get(
                reverse("finance_payroll_employee_profile", args=[self.employee.pk])
            ).status_code,
            403,
        )

    def test_hr_card_shows_current_salary_and_employee_history(self):
        self.grant_hr()
        month = timezone.localdate().replace(day=1)
        snapshot = PayrollPlanSnapshot.objects.create(
            organization=self.organization,
            period_month=month,
            source_hash="e" * 64,
            source_rows=1,
            source_organization_guids=[str(ORG_GUID)],
            currency_guid=CURRENCY_GUID,
            fetched_by=self.user,
        )
        PayrollPlanItem.objects.create(
            snapshot=snapshot,
            employee_identity=self.identity,
            onec_employee_id=ONEC_ID,
            employee_raw_name=self.employee.display_name,
            accrual_type_id="salary",
            accrual_type_name="Оклад",
            amount=Decimal("60000"),
            is_base_salary=True,
            source_period=month,
            source_organization_guid=ORG_GUID,
        )

        listing = self.client.get(reverse("finance_payroll_employee_list"))
        profile = self.client.get(
            reverse("finance_payroll_employee_profile", args=[self.employee.pk])
        )

        self.assertEqual(listing.status_code, 200)
        self.assertContains(listing, self.employee.display_name)
        self.assertContains(listing, "60")
        self.assertNotContains(listing, "Сопоставление 1С")
        self.assertNotContains(listing, "identity")
        self.assertNotContains(listing, "без стабильного ID")
        self.assertEqual(profile.status_code, 200)
        self.assertContains(profile, "Карточка сотрудника: зарплата, ФОТ и кадровые данные")
        self.assertContains(profile, "15.01.2020")
        self.assertContains(profile, "Оклад")
        self.assertContains(profile, "60")
        self.assertContains(profile, "Отпуска и отгулы")
        self.assertContains(profile, "Проценты")
        self.assertContains(profile, "Бонусы")
        self.assertContains(profile, "История зарплаты по месяцам")

    def test_latest_snapshot_without_employee_does_not_resurrect_old_salary(self):
        month = date(2026, 9, 1)
        older = PayrollPlanSnapshot.objects.create(
            organization=self.organization,
            period_month=month,
            source_hash="f" * 64,
            source_rows=1,
            source_organization_guids=[str(ORG_GUID)],
            currency_guid=CURRENCY_GUID,
            fetched_by=self.user,
        )
        PayrollPlanItem.objects.create(
            snapshot=older,
            employee_identity=self.identity,
            onec_employee_id=ONEC_ID,
            employee_raw_name=self.employee.display_name,
            accrual_type_id="salary",
            accrual_type_name="Оклад",
            amount=Decimal("60000"),
            is_base_salary=True,
            source_period=month,
            source_organization_guid=ORG_GUID,
        )
        PayrollPlanSnapshot.objects.create(
            organization=self.organization,
            period_month=month,
            source_hash="0" * 64,
            source_rows=0,
            source_organization_guids=[str(ORG_GUID)],
            currency_guid=CURRENCY_GUID,
            fetched_by=self.user,
        )

        plan = employee_current_plan(self.employee, month)

        self.assertEqual(plan["base_salary"], Decimal("0.00"))
        self.assertEqual(plan["items"], [])
