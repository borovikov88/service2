import hashlib
from datetime import date, timedelta
from decimal import Decimal

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from pool_service.finance_imports.cost_control import (
    get_onec_cost_anomalies,
    get_onec_cost_control_dataset,
    monthly_cost_anomaly_summary,
    summarize_active_dataset,
    summarize_cost_anomalies,
)
from pool_service.models import (
    OneCImportBatch,
    OneCMonthlyProfit,
    OneCReportPeriodState,
    Organization,
    OrganizationAccess,
)


class OneCCostControlTests(TestCase):
    def setUp(self):
        self.organization = Organization.objects.create(
            name="Контроль себестоимости",
            paid_until=timezone.now() + timedelta(days=30),
        )
        self.owner = User.objects.create_user("cost-owner", password="test")
        OrganizationAccess.objects.create(
            user=self.owner,
            organization=self.organization,
            role="owner",
        )
        self.client.force_login(self.owner)
        self.next_row_number = 1

    def batch(self, suffix, *, organization=None, user=None):
        organization = organization or self.organization
        user = user or self.owner
        return OneCImportBatch.objects.create(
            organization=organization,
            import_type=OneCImportBatch.TYPE_MONTHLY_PROFIT,
            original_filename=f"synthetic-{suffix}.xlsx",
            stored_file=f"onec_imports/synthetic-{suffix}.xlsx",
            file_sha256=hashlib.sha256(suffix.encode()).hexdigest(),
            file_size=1,
            status=OneCImportBatch.STATUS_CONFIRMED,
            uploaded_by=user,
            confirmed_by=user,
            confirmed_at=timezone.now(),
        )

    def row(
        self,
        batch,
        *,
        month=date(2026, 1, 1),
        name="Тестовая номенклатура",
        article="ART-1",
        nomenclature_type="Запас",
        revenue=Decimal("10000.00"),
        cost=Decimal("0.00"),
    ):
        row = OneCMonthlyProfit.objects.create(
            import_batch=batch,
            organization=batch.organization,
            period_month=month,
            source_row_number=self.next_row_number,
            nomenclature=name,
            article=article,
            nomenclature_type=nomenclature_type,
            quantity=Decimal("1.000000"),
            revenue=revenue,
            cost=cost,
            gross_profit=revenue - (cost or Decimal("0.00")),
            profitability_percent=Decimal("100.0000") if revenue else None,
        )
        self.next_row_number += 1
        return row

    def activate(self, batch, *months):
        for month in months:
            OneCReportPeriodState.objects.update_or_create(
                organization=batch.organization,
                report_type=OneCImportBatch.TYPE_MONTHLY_PROFIT,
                period_month=month,
                defaults={"active_batch": batch, "updated_by": batch.uploaded_by},
            )

    def test_v1_rule_includes_only_active_positive_revenue_goods_without_cost(self):
        batch = self.batch("v1-rule")
        zero = self.row(batch, name="Нулевая себестоимость")
        null = self.row(batch, name="Отсутствующая себестоимость", cost=None)
        self.row(batch, name="Есть себестоимость", cost=Decimal("6000.00"))
        self.row(batch, name="Без продажи", revenue=Decimal("0.00"))
        self.row(batch, name="Возврат", revenue=Decimal("-100.00"))
        self.row(batch, name="Услуга", nomenclature_type="Услуга")
        self.row(batch, name="Работа", nomenclature_type="Работа")
        self.row(batch, name="Неизвестно", nomenclature_type="")
        normalized = self.row(
            batch,
            name="Нормализованный тип",
            nomenclature_type="  зАпАс  ",
        )
        self.activate(batch, date(2026, 1, 1))

        anomalies = get_onec_cost_anomalies(self.organization)
        self.assertEqual(set(anomalies), {zero, null, normalized})

    def test_historical_confirmed_batch_is_excluded(self):
        month = date(2026, 1, 1)
        old = self.batch("historical-old")
        historical_anomaly = self.row(old, month=month, name="Старая версия")
        new = self.batch("historical-new")
        active_row = self.row(
            new,
            month=month,
            name="Новая версия",
            cost=Decimal("600.00"),
            revenue=Decimal("1000.00"),
        )
        self.activate(new, month)

        self.assertTrue(
            OneCMonthlyProfit.objects.filter(pk=historical_anomaly.pk).exists()
        )
        self.assertEqual(list(get_onec_cost_anomalies(self.organization)), [])
        self.assertEqual(
            list(get_onec_cost_control_dataset(self.organization)),
            [active_row],
        )
        response = self.client.get(reverse("finance_onec_cost_control"))
        self.assertNotContains(response, "Старая версия")
        self.assertContains(response, "Строк, требующих проверки, не найдено")

    def test_active_anomaly_replaces_clean_historical_row(self):
        month = date(2025, 12, 1)
        old = self.batch("reverse-old-clean")
        old_row = self.row(
            old,
            month=month,
            name="Историческая строка с себестоимостью",
            revenue=Decimal("10000.00"),
            cost=Decimal("6000.00"),
        )
        new = self.batch("reverse-new-anomaly")
        new_row = self.row(
            new,
            month=month,
            name="Активная строка без себестоимости",
            revenue=Decimal("10000.00"),
            cost=Decimal("0.00"),
        )
        self.activate(new, month)

        anomalies = get_onec_cost_anomalies(self.organization)
        self.assertEqual(list(anomalies), [new_row])
        self.assertEqual(
            summarize_cost_anomalies(anomalies)["revenue"],
            Decimal("10000.00"),
        )
        self.assertTrue(OneCMonthlyProfit.objects.filter(pk=old_row.pk).exists())
        self.assertEqual(
            list(get_onec_cost_control_dataset(self.organization)),
            [new_row],
        )

        response = self.client.get(reverse("finance_onec_cost_control"))
        self.assertEqual(response.context["anomaly_summary"]["row_count"], 1)
        self.assertEqual(
            response.context["anomaly_summary"]["revenue"],
            Decimal("10000.00"),
        )
        self.assertContains(response, "Активная строка без себестоимости")
        self.assertNotContains(response, "Историческая строка с себестоимостью")

    def test_summary_monthly_breakdown_filters_and_ordering(self):
        january = date(2026, 1, 1)
        february = date(2026, 2, 1)
        batch = self.batch("summary")
        self.row(
            batch,
            month=january,
            name="Небольшая выручка",
            article="LOW",
            revenue=Decimal("100.00"),
        )
        highest = self.row(
            batch,
            month=february,
            name="Большая выручка",
            article="HIGH",
            revenue=Decimal("900.00"),
        )
        self.row(
            batch,
            month=february,
            name="Диагностическая услуга",
            nomenclature_type="Услуга",
            revenue=Decimal("500.00"),
        )
        self.row(
            batch,
            month=february,
            name="Неизвестный тип",
            nomenclature_type="",
            revenue=Decimal("300.00"),
        )
        self.activate(batch, january, february)

        dataset = get_onec_cost_control_dataset(self.organization)
        anomalies = get_onec_cost_anomalies(self.organization)
        dataset_summary = summarize_active_dataset(dataset)
        anomaly_summary = summarize_cost_anomalies(anomalies)
        monthly = list(monthly_cost_anomaly_summary(anomalies))

        self.assertEqual(dataset_summary["month_count"], 2)
        self.assertEqual(dataset_summary["row_count"], 4)
        self.assertEqual(dataset_summary["goods_count"], 2)
        self.assertEqual(dataset_summary["service_count"], 1)
        self.assertEqual(dataset_summary["work_count"], 0)
        self.assertEqual(dataset_summary["unknown_count"], 1)
        self.assertEqual(anomaly_summary["row_count"], 2)
        self.assertEqual(anomaly_summary["nomenclature_count"], 2)
        self.assertEqual(anomaly_summary["revenue"], Decimal("1000.00"))
        self.assertEqual([item["row_count"] for item in monthly], [1, 1])
        self.assertEqual(anomalies.first(), highest)
        self.assertEqual(
            get_onec_cost_anomalies(
                self.organization,
                period_month=january,
                search="LOW",
            ).count(),
            1,
        )
        self.assertFalse(
            get_onec_cost_anomalies(
                self.organization,
                period_month=january,
                search="HIGH",
            ).exists()
        )

    def test_page_filters_paginates_and_does_not_mutate_source_rows(self):
        batch = self.batch("pagination")
        for index in range(51):
            self.row(
                batch,
                name=f"Позиция {index:02d}",
                article=f"PAGE-{index:02d}",
                revenue=Decimal(1000 + index),
            )
        self.activate(batch, date(2026, 1, 1))
        before = list(
            OneCMonthlyProfit.objects.order_by("id").values_list(
                "id", "revenue", "cost", "gross_profit"
            )
        )

        response = self.client.get(reverse("finance_onec_cost_control"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.context["page_obj"]), 50)
        self.assertTrue(response.context["page_obj"].has_next())
        filtered = self.client.get(
            reverse("finance_onec_cost_control"),
            {"period": "2026-01", "search": "PAGE-50", "problem": "goods_zero_cost"},
        )
        self.assertEqual(filtered.context["anomaly_summary"]["row_count"], 1)
        self.assertContains(filtered, "PAGE-50")
        self.assertEqual(
            before,
            list(
                OneCMonthlyProfit.objects.order_by("id").values_list(
                    "id", "revenue", "cost", "gross_profit"
                )
            ),
        )

    def test_invalid_period_is_rejected_without_broadening_results(self):
        batch = self.batch("invalid-period")
        self.row(batch)
        self.activate(batch, date(2026, 1, 1))

        response = self.client.get(
            reverse("finance_onec_cost_control"),
            {"period": "not-a-month"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context["form"].errors)
        self.assertEqual(response.context["anomaly_summary"]["row_count"], 0)

    def test_owner_admin_accountant_and_superuser_have_access(self):
        self.client.force_login(self.owner)
        self.assertEqual(
            self.client.get(reverse("finance_onec_cost_control")).status_code,
            200,
        )
        cases = [
            ("admin-role", "admin", False),
            ("accountant-role", "accountant", False),
            ("superuser-role", "service", True),
        ]
        for username, role, is_superuser in cases:
            user = User.objects.create_user(
                username,
                password="test",
                is_superuser=is_superuser,
                is_staff=is_superuser,
            )
            OrganizationAccess.objects.create(
                user=user,
                organization=self.organization,
                role=role,
            )
            self.client.force_login(user)
            with self.subTest(role=role, superuser=is_superuser):
                self.assertEqual(
                    self.client.get(reverse("finance_onec_cost_control")).status_code,
                    200,
                )

    def test_non_finance_management_roles_are_denied(self):
        for role in ("manager", "service", "installer"):
            user = User.objects.create_user(f"cost-{role}", password="test")
            OrganizationAccess.objects.create(
                user=user,
                organization=self.organization,
                role=role,
            )
            self.client.force_login(user)
            with self.subTest(role=role):
                self.assertEqual(
                    self.client.get(reverse("finance_onec_cost_control")).status_code,
                    403,
                )

    def test_tenant_scope_prevents_foreign_anomaly_leakage(self):
        own_batch = self.batch("tenant-own")
        self.row(own_batch, name="Собственная позиция")
        self.activate(own_batch, date(2026, 1, 1))

        other = Organization.objects.create(
            name="Другая организация",
            paid_until=timezone.now() + timedelta(days=30),
        )
        other_user = User.objects.create_user("other-cost-owner")
        OrganizationAccess.objects.create(
            user=other_user,
            organization=other,
            role="owner",
        )
        foreign_batch = self.batch(
            "tenant-foreign",
            organization=other,
            user=other_user,
        )
        self.row(foreign_batch, name="Чужая секретная позиция")
        self.activate(foreign_batch, date(2026, 1, 1))

        response = self.client.get(
            reverse("finance_onec_cost_control"),
            {"search": "секретная"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, "Чужая секретная позиция")
        self.assertEqual(response.context["anomaly_summary"]["row_count"], 0)

    def test_anomaly_queryset_uses_select_related_for_batch_link(self):
        batch = self.batch("queries")
        self.row(batch)
        self.activate(batch, date(2026, 1, 1))

        with self.assertNumQueries(1):
            rows = list(get_onec_cost_anomalies(self.organization))
            self.assertEqual(rows[0].import_batch.id, batch.id)
            self.assertEqual(rows[0].organization.id, self.organization.id)
