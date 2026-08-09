import importlib
from datetime import date, timedelta
from types import SimpleNamespace
from unittest.mock import patch

from django.apps import apps as django_apps
from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.db import IntegrityError, connection, transaction
from django.db.models import Sum
from django.db.models.deletion import ProtectedError
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from pool_service.finance_imports import services as import_services
from pool_service.finance_imports.services import (
    cancel_monthly_profit,
    confirm_monthly_profit,
    create_monthly_profit_preview,
    validate_period_assignment,
)
from pool_service.models import (
    OneCImportBatch,
    OneCMonthlyProfit,
    OneCReportPeriodActivation,
    OneCReportPeriodState,
    Organization,
    OrganizationAccess,
)
from pool_service.tests.test_onec_monthly_profit_import import (
    upload,
    vertical_xlsx_bytes,
)


MONTH_LABELS = {
    date(2025, 1, 1): "янв. 2025",
    date(2025, 2, 1): "фев. 2025",
    date(2025, 3, 1): "мар. 2025",
    date(2025, 4, 1): "апр. 2025",
}


class OneCActivePeriodVersionTests(TestCase):
    def setUp(self):
        self.private_dir = self.enterContext(TemporaryDirectoryContext())
        self.enterContext(override_settings(PRIVATE_MEDIA_ROOT=self.private_dir))
        self.organization = Organization.objects.create(
            name="Активные версии",
            paid_until=timezone.now() + timedelta(days=30),
        )
        self.user = User.objects.create_user("active-owner", password="test")
        OrganizationAccess.objects.create(
            user=self.user, organization=self.organization, role="owner"
        )
        self.client.force_login(self.user)

    def payload(self, prefix, months):
        blocks = []
        for index, month in enumerate(months, start=1):
            blocks.append(
                (
                    MONTH_LABELS[month],
                    [[f"{prefix}-{index}", 100 + index, 50, 50 + index, 50]],
                )
            )
        return vertical_xlsx_bytes(blocks=blocks, include_total=False)

    def preview(self, prefix, months, organization=None, user=None):
        organization = organization or self.organization
        user = user or self.user
        return create_monthly_profit_preview(
            upload(name=f"{prefix}.xlsx", data=self.payload(prefix, months)),
            organization,
            user,
        )

    def manual_confirmed_batch(self, suffix, months, organization=None, user=None):
        organization = organization or self.organization
        user = user or self.user
        batch = OneCImportBatch.objects.create(
            organization=organization,
            import_type=OneCImportBatch.TYPE_MONTHLY_PROFIT,
            original_filename=f"manual-{suffix}.xlsx",
            stored_file=f"onec_imports/manual-{suffix}.xlsx",
            file_sha256=(suffix * 64)[:64],
            file_size=1,
            status=OneCImportBatch.STATUS_CONFIRMED,
            uploaded_by=user,
            confirmed_by=user,
            confirmed_at=timezone.now(),
            rows_detected=len(months),
            rows_imported=len(months),
        )
        for index, month in enumerate(months, start=1):
            OneCMonthlyProfit.objects.create(
                import_batch=batch,
                organization=organization,
                period_month=month,
                source_row_number=index,
                nomenclature=f"Историческая строка {suffix}-{index}",
                revenue="100.00",
                cost="50.00",
                gross_profit="50.00",
            )
        return batch

    def run_backfill(self):
        migration = importlib.import_module(
            "pool_service.migrations.0085_backfill_onec_report_period_states"
        )
        migration.backfill_onec_report_period_states(
            django_apps, SimpleNamespace(connection=connection)
        )

    def replacement_pair(self, months=(date(2025, 1, 1), date(2025, 2, 1))):
        old = self.preview("old", months)
        confirm_monthly_profit(old.id, self.organization, self.user)
        new = self.preview("new", months)
        return old, new

    def assert_active_batch(self, batch, months):
        self.assertEqual(
            set(
                OneCReportPeriodState.objects.filter(
                    organization=self.organization,
                    report_type=OneCImportBatch.TYPE_MONTHLY_PROFIT,
                    period_month__in=months,
                ).values_list("active_batch_id", flat=True)
            ),
            {batch.id},
        )

    def test_unique_state_per_organization_type_and_month(self):
        batch = self.manual_confirmed_batch("a", [date(2025, 1, 1)])
        kwargs = {
            "organization": self.organization,
            "report_type": batch.import_type,
            "period_month": date(2025, 1, 1),
            "active_batch": batch,
        }
        OneCReportPeriodState.objects.create(**kwargs)
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                OneCReportPeriodState.objects.create(**kwargs)

    def test_active_batch_is_protected_from_delete(self):
        batch = self.manual_confirmed_batch("b", [date(2025, 1, 1)])
        OneCReportPeriodState.objects.create(
            organization=self.organization,
            report_type=batch.import_type,
            period_month=date(2025, 1, 1),
            active_batch=batch,
        )
        with self.assertRaises(ProtectedError):
            batch.delete()

    def test_backfill_creates_states_and_initial_history(self):
        months = [date(2025, 1, 1), date(2025, 2, 1)]
        batch = self.manual_confirmed_batch("c", months)
        self.run_backfill()
        self.assert_active_batch(batch, months)
        history = OneCReportPeriodActivation.objects.filter(batch=batch)
        self.assertEqual(history.count(), 2)
        self.assertFalse(history.exclude(replaced_batch=None).exists())

    def test_backfill_fails_loudly_for_ambiguous_confirmed_batches(self):
        month = date(2025, 1, 1)
        self.manual_confirmed_batch("d", [month])
        self.manual_confirmed_batch("e", [month])
        with self.assertRaisesRegex(RuntimeError, "multiple confirmed 1C batches"):
            self.run_backfill()
        self.assertFalse(OneCReportPeriodState.objects.exists())

    def test_backfill_keeps_tenants_separate(self):
        month = date(2025, 1, 1)
        first = self.manual_confirmed_batch("f", [month])
        other_org = Organization.objects.create(name="Другой tenant")
        other_user = User.objects.create_user("other-backfill")
        second = self.manual_confirmed_batch(
            "g", [month], organization=other_org, user=other_user
        )
        self.run_backfill()
        self.assertEqual(
            OneCReportPeriodState.objects.get(organization=self.organization).active_batch,
            first,
        )
        self.assertEqual(
            OneCReportPeriodState.objects.get(organization=other_org).active_batch,
            second,
        )

    def test_first_confirm_creates_rows_states_history_and_active_queryset(self):
        months = [date(2025, 1, 1), date(2025, 2, 1)]
        batch = self.preview("first", months)
        self.assertEqual(batch.metadata["overlap_count"], 0)
        confirm_monthly_profit(batch.id, self.organization, self.user)
        self.assert_active_batch(batch, months)
        self.assertEqual(OneCReportPeriodActivation.objects.filter(batch=batch).count(), 2)
        self.assertEqual(
            OneCMonthlyProfit.objects.active_for(self.organization).count(), 2
        )

    def test_full_replacement_retains_history_and_returns_only_new_rows(self):
        months = [date(2025, 1, 1), date(2025, 2, 1)]
        old, new = self.replacement_pair(months)
        self.assertEqual(new.metadata["overlap_count"], 2)
        confirm_monthly_profit(new.id, self.organization, self.user)
        self.assert_active_batch(new, months)
        self.assertEqual(
            OneCMonthlyProfit.objects.filter(import_batch=old).count(), 2
        )
        active = OneCMonthlyProfit.objects.active_for(self.organization)
        self.assertEqual(active.count(), 2)
        self.assertEqual(set(active.values_list("import_batch_id", flat=True)), {new.id})
        replacements = OneCReportPeriodActivation.objects.filter(
            batch=new, replaced_batch=old
        )
        self.assertEqual(replacements.count(), 2)

    def test_partial_replacement_switches_only_overlapping_months(self):
        january = date(2025, 1, 1)
        february = date(2025, 2, 1)
        march = date(2025, 3, 1)
        april = date(2025, 4, 1)
        old = self.preview("partial-old", [january, february, march])
        confirm_monthly_profit(old.id, self.organization, self.user)
        new = self.preview("partial-new", [march, april])
        self.assertEqual(new.metadata["overlap_months"], ["2025-03"])
        confirm_monthly_profit(new.id, self.organization, self.user)
        states = {
            state.period_month: state.active_batch_id
            for state in OneCReportPeriodState.objects.filter(
                organization=self.organization
            )
        }
        self.assertEqual(states[january], old.id)
        self.assertEqual(states[february], old.id)
        self.assertEqual(states[march], new.id)
        self.assertEqual(states[april], new.id)
        active = OneCMonthlyProfit.objects.active_for(self.organization)
        self.assertEqual(active.count(), 4)
        self.assertEqual(
            active.filter(import_batch=old).count(), 2
        )
        self.assertEqual(active.filter(import_batch=new).count(), 2)
        self.assertEqual(active.aggregate(total=Sum("revenue"))["total"], 406)

    def test_preview_overlap_is_read_only(self):
        months = [date(2025, 1, 1), date(2025, 2, 1)]
        old = self.preview("preview-old", months)
        confirm_monthly_profit(old.id, self.organization, self.user)
        before = list(
            OneCReportPeriodState.objects.values_list("id", "active_batch_id")
        )
        new = self.preview("preview-new", months)
        self.assertEqual(new.metadata["overlap_count"], 2)
        self.assertEqual(new.metadata["overlap_months"], ["2025-01", "2025-02"])
        self.assertEqual(
            list(OneCReportPeriodState.objects.values_list("id", "active_batch_id")),
            before,
        )

    def test_confirm_recomputes_overlap_after_stale_preview(self):
        month = date(2025, 1, 1)
        newest = self.preview("stale-new", [month])
        self.assertEqual(newest.metadata["overlap_count"], 0)
        old = self.preview("stale-old", [month])
        confirm_monthly_profit(old.id, self.organization, self.user)
        confirm_monthly_profit(newest.id, self.organization, self.user)
        self.assert_active_batch(newest, [month])
        activation = OneCReportPeriodActivation.objects.get(batch=newest)
        self.assertEqual(activation.replaced_batch, old)

    def test_cancel_preview_does_not_change_active_state(self):
        month = date(2025, 1, 1)
        old, new = self.replacement_pair([month])
        cancel_monthly_profit(new, self.user)
        self.assert_active_batch(old, [month])
        self.assertFalse(OneCReportPeriodActivation.objects.filter(batch=new).exists())

    def test_tenant_validation_and_active_queryset_are_scoped(self):
        month = date(2025, 1, 1)
        own = self.preview("tenant-own", [month])
        confirm_monthly_profit(own.id, self.organization, self.user)
        other_org = Organization.objects.create(name="Изолированный tenant")
        other_user = User.objects.create_user("isolated-owner")
        OrganizationAccess.objects.create(
            user=other_user, organization=other_org, role="owner"
        )
        other = self.preview("tenant-other", [month], other_org, other_user)
        confirm_monthly_profit(other.id, other_org, other_user)
        with self.assertRaises(ValidationError):
            validate_period_assignment(other, self.organization, other.import_type, month)
        self.assertEqual(
            set(
                OneCMonthlyProfit.objects.active_for(self.organization).values_list(
                    "import_batch_id", flat=True
                )
            ),
            {own.id},
        )

    def test_period_assignment_validation_rejects_invalid_candidates(self):
        month = date(2025, 1, 1)
        preview = self.preview("validation-preview", [month])
        with self.assertRaisesRegex(ValidationError, "подтверждённая"):
            validate_period_assignment(
                preview, self.organization, preview.import_type, month
            )
        confirmed = self.manual_confirmed_batch("validation", [month])
        with self.assertRaisesRegex(ValidationError, "Тип"):
            validate_period_assignment(
                confirmed, self.organization, "another_report", month
            )
        with self.assertRaisesRegex(ValidationError, "первого числа"):
            validate_period_assignment(
                confirmed, self.organization, confirmed.import_type, date(2025, 1, 2)
            )
        with self.assertRaisesRegex(ValidationError, "отсутствуют строки"):
            validate_period_assignment(
                confirmed,
                self.organization,
                confirmed.import_type,
                date(2025, 2, 1),
            )

    def test_preview_ui_distinguishes_new_data_and_replacement(self):
        month = date(2025, 1, 1)
        first = self.preview("ui-first", [month])
        first_response = self.client.get(
            reverse("finance_onec_import_preview", args=[first.id])
        )
        self.assertContains(first_response, "Новые данные")
        self.assertContains(first_response, "Подтвердить импорт")
        confirm_monthly_profit(first.id, self.organization, self.user)
        replacement = self.preview("ui-replacement", [month])
        replacement_response = self.client.get(
            reverse("finance_onec_import_preview", args=[replacement.id])
        )
        self.assertContains(replacement_response, "заменит ранее подтверждённые данные")
        self.assertContains(replacement_response, "Подтвердить и заменить данные")

    def test_confirm_uses_organization_namespace_lock(self):
        batch = self.preview("lock", [date(2025, 1, 1)])
        with patch.object(
            Organization.objects,
            "select_for_update",
            wraps=Organization.objects.select_for_update,
        ) as organization_lock:
            confirm_monthly_profit(batch.id, self.organization, self.user)
        organization_lock.assert_called_once_with()

    def test_monthly_row_failure_keeps_old_active_state(self):
        old, new = self.replacement_pair([date(2025, 1, 1)])
        with patch.object(
            import_services,
            "_bulk_create_monthly_rows",
            side_effect=RuntimeError("row failure"),
        ):
            with self.assertRaises(RuntimeError):
                confirm_monthly_profit(new.id, self.organization, self.user)
        self.assert_active_batch(old, [date(2025, 1, 1)])

    def test_failure_after_state_switch_rolls_back_replacement(self):
        month = date(2025, 1, 1)
        old, new = self.replacement_pair([month])
        original = import_services._activate_period_states

        def activate_then_fail(*args, **kwargs):
            original(*args, **kwargs)
            raise RuntimeError("after states")

        with patch.object(
            import_services, "_activate_period_states", side_effect=activate_then_fail
        ):
            with self.assertRaises(RuntimeError):
                confirm_monthly_profit(new.id, self.organization, self.user)
        self.assert_active_batch(old, [month])
        self.assertFalse(OneCReportPeriodActivation.objects.filter(batch=new).exists())

    def test_activation_history_failure_rolls_back_replacement(self):
        month = date(2025, 1, 1)
        old, new = self.replacement_pair([month])
        with patch.object(
            OneCReportPeriodActivation.objects,
            "create",
            side_effect=RuntimeError("history failure"),
        ):
            with self.assertRaises(RuntimeError):
                confirm_monthly_profit(new.id, self.organization, self.user)
        self.assert_active_batch(old, [month])

    def test_failure_before_confirmed_status_rolls_back_replacement(self):
        month = date(2025, 1, 1)
        old, new = self.replacement_pair([month])
        with patch.object(
            import_services,
            "_save_confirmed_batch",
            side_effect=RuntimeError("status failure"),
        ):
            with self.assertRaises(RuntimeError):
                confirm_monthly_profit(new.id, self.organization, self.user)
        self.assert_active_batch(old, [month])
        new.refresh_from_db()
        self.assertEqual(new.status, OneCImportBatch.STATUS_FAILED)

    def test_batch_detail_reports_active_and_replaced_months(self):
        months = [date(2025, 1, 1), date(2025, 2, 1)]
        old, new = self.replacement_pair(months)
        confirm_monthly_profit(new.id, self.organization, self.user)
        old_response = self.client.get(reverse("finance_onec_import_detail", args=[old.id]))
        new_response = self.client.get(reverse("finance_onec_import_detail", args=[new.id]))
        self.assertEqual(old_response.context["active_month_count"], 0)
        self.assertEqual(old_response.context["replaced_month_count"], 2)
        self.assertEqual(new_response.context["active_month_count"], 2)
        self.assertEqual(new_response.context["replaced_month_count"], 0)


class TemporaryDirectoryContext:
    def __enter__(self):
        from tempfile import TemporaryDirectory

        self.directory = TemporaryDirectory()
        return self.directory.name

    def __exit__(self, exc_type, exc_value, traceback):
        self.directory.cleanup()
