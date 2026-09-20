from datetime import date, datetime, timedelta, timezone as dt_timezone
from decimal import Decimal
import uuid
from unittest.mock import patch

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from pool_service.finance_imports.payroll_plan import (
    PayrollPlanSyncError,
    current_payroll_plan_date,
    payroll_compensation_dashboard_data,
    refresh_payroll_plan_snapshot,
)
from pool_service.finance_imports.odata_payroll_plan import read_current_plan
from pool_service.models import (
    Employee,
    EmployeeOneCIdentity,
    Organization,
    OrganizationAccess,
    PayrollPlanItem,
    PayrollPlanSnapshot,
)


ORG_GUID = "11111111-1111-1111-1111-111111111111"
CURRENCY_GUID = "22222222-2222-2222-2222-222222222222"
EMPLOYEE_GUID = "33333333-3333-3333-3333-333333333333"
TYPE_GUID = "44444444-4444-4444-4444-444444444444"
OTHER_TYPE_GUID = "55555555-5555-5555-5555-555555555555"


class PayrollPlanCalendarTests(TestCase):
    @patch("pool_service.finance_imports.payroll_plan.calendar_timezone")
    def test_current_plan_date_uses_1c_business_timezone(self, calendar_timezone_mock):
        from zoneinfo import ZoneInfo

        calendar_timezone_mock.return_value = ZoneInfo("Asia/Barnaul")
        utc_time = datetime(2026, 8, 31, 19, 30, tzinfo=dt_timezone.utc)

        self.assertEqual(
            current_payroll_plan_date(utc_time),
            date(2026, 9, 1),
        )


class PayrollPlanReaderTests(TestCase):
    @patch("pool_service.finance_imports.odata_payroll_plan.Reader")
    def test_reader_uses_latest_effective_plan_and_marks_only_salary(self, reader_cls):
        reader = reader_cls.return_value

        def pages(entity, options):
            if entity == "InformationRegister_ПлановыеНачисленияИУдержания_RecordType":
                return [[
                    {
                        "Active": True,
                        "Period": "2026-01-01T00:00:00",
                        "Актуальность": True,
                        "Организация_Key": ORG_GUID,
                        "Сотрудник_Key": EMPLOYEE_GUID,
                        "Валюта_Key": CURRENCY_GUID,
                        "ВидНачисленияУдержания_Key": TYPE_GUID,
                        "Сумма": "50000.00",
                    },
                    {
                        "Active": True,
                        "Period": "2026-06-04T00:00:00",
                        "Актуальность": True,
                        "Организация_Key": ORG_GUID,
                        "Сотрудник_Key": EMPLOYEE_GUID,
                        "Валюта_Key": CURRENCY_GUID,
                        "ВидНачисленияУдержания_Key": TYPE_GUID,
                        "Сумма": "60000.00",
                    },
                    {
                        "Active": True,
                        "Period": "2026-06-04T00:00:00",
                        "Актуальность": True,
                        "Организация_Key": ORG_GUID,
                        "Сотрудник_Key": EMPLOYEE_GUID,
                        "Валюта_Key": CURRENCY_GUID,
                        "ВидНачисленияУдержания_Key": OTHER_TYPE_GUID,
                        "Сумма": "5000.00",
                    },
                ]]
            if entity == "Catalog_Сотрудники":
                return [[{
                    "Ref_Key": EMPLOYEE_GUID,
                    "Description": "Иванов Иван Иванович",
                    "DeletionMark": False,
                }]]
            if entity == "Catalog_ВидыНачисленийИУдержаний":
                return [[
                    {
                        "Ref_Key": TYPE_GUID,
                        "Description": "Оклад",
                        "Тип": "Начисление",
                        "IsFolder": False,
                        "DeletionMark": False,
                    },
                    {
                        "Ref_Key": OTHER_TYPE_GUID,
                        "Description": "Доплата",
                        "Тип": "Начисление",
                        "IsFolder": False,
                        "DeletionMark": False,
                    },
                ]]
            raise AssertionError(entity)

        reader.pages_for.side_effect = pages
        result = read_current_plan(
            {
                "ONEC_ODATA_ORGANIZATION_GUIDS": ORG_GUID,
                "ONEC_ODATA_PAYROLL_CURRENCY_GUID": CURRENCY_GUID,
            },
            date(2026, 9, 20),
        )

        self.assertEqual(result["source_rows"], 3)
        by_type = {item["accrual_type_name"]: item for item in result["items"]}
        self.assertEqual(by_type["Оклад"]["amount"], "60000.00")
        self.assertTrue(by_type["Оклад"]["is_base_salary"])
        self.assertEqual(by_type["Доплата"]["amount"], "5000.00")
        self.assertFalse(by_type["Доплата"]["is_base_salary"])
        reader.check_time.assert_called_once()


class PayrollCurrentCompensationTests(TestCase):
    def setUp(self):
        self.organization = Organization.objects.create(
            name="Synthetic Payroll Plan",
            paid_until=timezone.now() + timedelta(days=30),
        )
        self.user = User.objects.create_user("owner", password="password")
        OrganizationAccess.objects.create(
            organization=self.organization,
            user=self.user,
            role="owner",
        )
        self.client.force_login(self.user)
        self.month = timezone.localdate().replace(day=1)

    def identity(self, name="Иванов Иван Иванович"):
        employee = Employee.objects.create(
            organization=self.organization,
            display_name=name,
        )
        return EmployeeOneCIdentity.objects.create(
            organization=self.organization,
            employee=employee,
            raw_name=name,
            normalized_name=name.casefold(),
            onec_employee_id=EMPLOYEE_GUID,
            status=EmployeeOneCIdentity.STATUS_AUTO_MATCHED,
            match_method=EmployeeOneCIdentity.MATCH_EXTERNAL_ID,
        )

    def snapshot(self, identity=None):
        identity = identity or self.identity()
        snapshot = PayrollPlanSnapshot.objects.create(
            organization=self.organization,
            period_month=self.month,
            source_hash="a" * 64,
            source_rows=2,
            source_organization_guids=[ORG_GUID],
            currency_guid=uuid.UUID(CURRENCY_GUID),
            fetched_by=self.user,
        )
        PayrollPlanItem.objects.create(
            snapshot=snapshot,
            employee_identity=identity,
            onec_employee_id=EMPLOYEE_GUID,
            employee_raw_name=identity.raw_name,
            accrual_type_id=TYPE_GUID,
            accrual_type_name="Оклад",
            amount=Decimal("60000.00"),
            is_base_salary=True,
            source_period=self.month,
            source_organization_guid=uuid.UUID(ORG_GUID),
        )
        PayrollPlanItem.objects.create(
            snapshot=snapshot,
            employee_identity=identity,
            onec_employee_id=EMPLOYEE_GUID,
            employee_raw_name=identity.raw_name,
            accrual_type_id=OTHER_TYPE_GUID,
            accrual_type_name="Доплата",
            amount=Decimal("5000.00"),
            is_base_salary=False,
            source_period=self.month,
            source_organization_guid=uuid.UUID(ORG_GUID),
        )
        return snapshot

    def test_dashboard_data_keeps_base_salary_separate_from_future_variable_pay(self):
        self.snapshot()
        data = payroll_compensation_dashboard_data(
            self.organization,
            self.month,
        )

        self.assertTrue(data["has_data"])
        self.assertEqual(data["base_salary_total"], Decimal("60000.00"))
        self.assertEqual(data["percent_total"], Decimal("0.00"))
        self.assertEqual(data["bonus_total"], Decimal("0.00"))
        self.assertEqual(data["total"], Decimal("60000.00"))
        self.assertEqual(data["other_plan_total"], Decimal("5000.00"))
        self.assertEqual(data["other_plan_items"], 1)
        self.assertEqual(data["employees"][0]["total"], Decimal("60000.00"))

    @patch("pool_service.finance_views.can_view_payroll_summary", return_value=True)
    @patch("pool_service.finance_views.can_view_payroll_personal", return_value=True)
    @patch("pool_service.finance_views.can_import_payroll", return_value=False)
    @patch("pool_service.finance_views.can_manage_employee_mapping", return_value=False)
    def test_personal_payroll_permission_renders_current_compensation(
        self, _mapping, _import, _personal, _summary
    ):
        identity = self.identity()
        self.snapshot(identity)

        response = self.client.get(reverse("finance_payroll_dashboard"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Зарплата текущего месяца")
        self.assertContains(response, identity.raw_name)
        self.assertContains(response, "60")
        self.assertNotContains(response, "Обновить оклады из 1С")

    @patch("pool_service.finance_views.can_view_payroll_summary", return_value=True)
    @patch("pool_service.finance_views.can_view_payroll_personal", return_value=False)
    @patch("pool_service.finance_views.can_import_payroll", return_value=True)
    @patch("pool_service.finance_views.can_manage_employee_mapping", return_value=False)
    def test_summary_only_permission_never_exposes_individual_salary(
        self, _mapping, _import, _personal, _summary
    ):
        identity = self.identity()
        self.snapshot(identity)

        response = self.client.get(reverse("finance_payroll_dashboard"))

        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, "Зарплата текущего месяца")
        self.assertNotContains(response, identity.raw_name)
        self.assertIsNone(response.context["compensation"])

    @patch("pool_service.finance_views.can_import_payroll", return_value=False)
    def test_refresh_requires_import_permission(self, _permission):
        response = self.client.post(reverse("finance_payroll_plan_refresh"))
        self.assertEqual(response.status_code, 403)

    def test_refresh_is_post_only(self):
        response = self.client.get(reverse("finance_payroll_plan_refresh"))
        self.assertEqual(response.status_code, 405)

    @patch("pool_service.finance_imports.payroll_plan._require_access")
    @patch("pool_service.finance_imports.payroll_plan.auto_coverage_config")
    @patch("pool_service.finance_imports.payroll_plan.config_from_settings")
    @patch("pool_service.finance_imports.payroll_plan._read_plan_payload")
    def test_refresh_persists_versioned_snapshot_and_reuses_same_fingerprint(
        self, reader, config, _coverage, _access
    ):
        config.return_value = {
            "ONEC_ODATA_ORGANIZATION_GUIDS": ORG_GUID,
            "ONEC_ODATA_PAYROLL_CURRENCY_GUID": CURRENCY_GUID,
        }
        as_of = date(2026, 9, 20)
        reader.return_value = {
            "kind": "payroll_plan_snapshot_v1",
            "period_month": "2026-09-01",
            "as_of": "2026-09-20",
            "selected_organizations": [ORG_GUID],
            "currency_guid": CURRENCY_GUID,
            "source_rows": 1,
            "items": [
                {
                    "organization_guid": ORG_GUID,
                    "employee_guid": EMPLOYEE_GUID,
                    "employee_name": "Иванов Иван Иванович",
                    "accrual_type_guid": TYPE_GUID,
                    "accrual_type_name": "Оклад",
                    "amount": "60000.00",
                    "source_period": "2026-06-04",
                    "source_rows": 1,
                    "is_base_salary": True,
                }
            ],
        }

        first, created = refresh_payroll_plan_snapshot(
            self.organization,
            self.user,
            as_of=as_of,
        )
        second, created_again = refresh_payroll_plan_snapshot(
            self.organization,
            self.user,
            as_of=as_of,
        )

        self.assertTrue(created)
        self.assertFalse(created_again)
        self.assertEqual(first.pk, second.pk)
        self.assertEqual(PayrollPlanSnapshot.objects.count(), 1)
        self.assertEqual(PayrollPlanItem.objects.count(), 1)
        identity = EmployeeOneCIdentity.objects.get(onec_employee_id=EMPLOYEE_GUID)
        self.assertEqual(identity.raw_name, "Иванов Иван Иванович")

    @patch("pool_service.finance_imports.payroll_plan._require_access")
    @patch("pool_service.finance_imports.payroll_plan.auto_coverage_config")
    @patch("pool_service.finance_imports.payroll_plan.config_from_settings")
    @patch("pool_service.finance_imports.payroll_plan._read_plan_payload")
    def test_failed_refresh_preserves_last_successful_snapshot(
        self, reader, config, _coverage, _access
    ):
        existing = self.snapshot()
        config.return_value = {
            "ONEC_ODATA_ORGANIZATION_GUIDS": ORG_GUID,
            "ONEC_ODATA_PAYROLL_CURRENCY_GUID": CURRENCY_GUID,
        }
        reader.side_effect = PayrollPlanSyncError("1С недоступна")

        with self.assertRaises(PayrollPlanSyncError):
            refresh_payroll_plan_snapshot(
                self.organization,
                self.user,
                as_of=self.month,
            )

        self.assertTrue(PayrollPlanSnapshot.objects.filter(pk=existing.pk).exists())
        self.assertEqual(PayrollPlanSnapshot.objects.count(), 1)
