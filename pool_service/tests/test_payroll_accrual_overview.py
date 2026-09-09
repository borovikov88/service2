"""Synthetic financial values only; no production records or source identifiers."""
from datetime import date, timedelta
from decimal import Decimal
import uuid

from django.contrib.auth.models import User
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from pool_service.finance_imports.payroll_accrual_dashboard import accrual_dashboard_data
from pool_service.finance_imports.overview import finance_overview_data
from pool_service.models import (
    OneCImportBatch, OneCReportPeriodState, Organization, PayrollAccrualMonth, PayrollRow,
)


class PayrollAccrualOverviewTests(TestCase):
    def setUp(self):
        self.org = Organization.objects.create(name="Synthetic FOT", paid_until=timezone.now() + timedelta(days=30))
        self.user = User.objects.create_user("synthetic-fot-owner")
        self.currency = uuid.UUID(int=101)
        self.first, self.second = date(2025, 1, 1), date(2025, 2, 1)

    def batch(self, kind, month, *, organization=None, confirmed=True, active=True):
        organization = organization or self.org
        batch = OneCImportBatch.objects.create(
            organization=organization, import_type=kind, uploaded_by=self.user,
            source_type=(OneCImportBatch.SOURCE_ODATA if kind == OneCImportBatch.TYPE_PAYROLL_ACCRUAL else OneCImportBatch.SOURCE_XLSX),
            status=OneCImportBatch.STATUS_CONFIRMED if confirmed else OneCImportBatch.STATUS_PREVIEWED,
            original_filename="synthetic", stored_file="synthetic/not-read", file_sha256=uuid.uuid4().hex * 2,
        )
        if active:
            OneCReportPeriodState.objects.update_or_create(
                organization=organization, report_type=kind, period_month=month,
                defaults={"active_batch": batch, "updated_by": self.user},
            )
        return batch

    def excel(self, month, amount):
        batch = self.batch(OneCImportBatch.TYPE_PAYROLL, month)
        PayrollRow.objects.create(
            organization=self.org, import_batch=batch, period_month=month, source_row_number=1,
            employee_raw_name="Synthetic", employee_normalized_name="synthetic",
            accrued=Decimal(amount), paid=Decimal("2"), opening_balance=Decimal("1"), closing_balance=Decimal("3"),
        )
        return batch

    def odata(self, month, amount, *, currency=None, **kwargs):
        batch = self.batch(OneCImportBatch.TYPE_PAYROLL_ACCRUAL, month, **kwargs)
        PayrollAccrualMonth.objects.create(
            organization=batch.organization, import_batch=batch, period_month=month,
            accrued=Decimal(amount), currency_guid=currency or self.currency,
            source_rows=2, source_organization_guids=[str(uuid.UUID(int=201))],
        )
        return batch

    def test_one_source_per_month_odata_replaces_excel_without_mutating_ledger(self):
        self.excel(self.first, "8")
        self.excel(self.second, "12")
        self.odata(self.second, "15")
        with CaptureQueriesContext(connection) as queries:
            data = accrual_dashboard_data(self.org, self.first, self.second)
        self.assertEqual(data["accrued"], Decimal("23"))
        self.assertEqual([row["source"] for row in data["months"]], ["excel", "odata"])
        self.assertEqual(PayrollRow.objects.get(period_month=self.second).accrued, Decimal("12"))
        self.assertFalse(any(item["sql"].lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE")) for item in queries))
        self.assertNotIn("paid", data)
        self.assertNotIn("closing", data)

    def test_only_confirmed_active_projection_counts_and_reimport_not_additive(self):
        self.odata(self.first, "10")
        self.odata(self.first, "14")
        self.odata(self.first, "80", active=False)
        self.odata(self.second, "90", confirmed=False)
        data = accrual_dashboard_data(self.org, self.first, self.second)
        self.assertEqual(data["accrued"], Decimal("14"))
        self.assertFalse(data["months"][1]["has_data"])
        self.assertEqual(data["data_through"], self.first)

    def test_empty_state_is_missing_but_valid_zero_is_data(self):
        self.batch(OneCImportBatch.TYPE_PAYROLL_ACCRUAL, self.first)
        empty = accrual_dashboard_data(self.org, self.first, self.second)
        self.assertIsNone(empty["accrued"])
        self.assertFalse(empty["has_data"])
        self.odata(self.first, "0")
        data = accrual_dashboard_data(self.org, self.first, self.second)
        self.assertEqual(data["accrued"], Decimal("0"))
        self.assertTrue(data["has_data"])
        self.assertIsNone(data["months"][1]["accrued"])

    def test_other_organization_and_corrupt_cross_org_active_link_are_excluded(self):
        other = Organization.objects.create(name="Other synthetic company")
        batch = self.odata(self.first, "999", organization=other)
        OneCReportPeriodState.objects.create(
            organization=self.org, report_type=OneCImportBatch.TYPE_PAYROLL_ACCRUAL,
            period_month=self.first, active_batch=batch,
        )
        data = accrual_dashboard_data(self.org, self.first, self.second)
        self.assertFalse(data["has_data"])
        self.assertIsNone(data["data_through"])

    def test_mixed_projection_currencies_never_become_one_total(self):
        self.odata(self.first, "7")
        self.odata(self.second, "11", currency=uuid.UUID(int=102))
        data = accrual_dashboard_data(self.org, self.first, self.second)
        self.assertTrue(data["currency_conflict"])
        self.assertIsNone(data["accrued"])
        self.assertFalse(data["has_data"])

    def test_finance_overview_uses_new_source_and_keeps_missing_month_unknown(self):
        self.excel(self.first, "8")
        self.excel(self.second, "12")
        self.odata(self.second, "15")
        data = finance_overview_data(self.org, {"period": "custom", "start": "2025-01", "end": "2025-03"}, today=date(2025, 3, 20))
        self.assertEqual(data["payroll_accrued"], Decimal("23"))
        self.assertEqual(data["freshness"]["payroll"]["data_through"], self.second)
        self.assertEqual(data["freshness"]["payroll"]["missing_months"], [date(2025, 3, 1)])
        self.assertIsNone(next(card for card in data["economy_cards"] if card["key"] == "payroll_to_revenue")["value"])
