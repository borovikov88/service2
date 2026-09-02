from datetime import date, timedelta
from tempfile import TemporaryDirectory
from unittest.mock import patch

from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from pool_service.finance_imports.odata_unified_sync import (
    REPORT_CASHFLOW,
    REPORT_PROFIT,
    SyncConflictError,
    apply_auto_sync,
    start_unified_sync,
    step_unified_sync,
)
from pool_service.finance_imports.odata_profit_drafts import _read_snapshot as read_profit_snapshot
from pool_service.finance_imports.services import cancel_onec_import_batch, confirm_monthly_profit
from pool_service.finance_imports.odata_profit_drafts import confirm_odata_profit
from pool_service.models import (
    CashFlowRow,
    OneCImportBatch,
    OneCMonthlyProfit,
    OneCODataSyncRun,
    OneCReportPeriodActivation,
    OneCReportPeriodState,
    Organization,
    OrganizationAccess,
)
from pool_service.tests.test_onec_odata_unified_sync import cashflow_row, config, profit_row


@override_settings(ONEC_ODATA_TARGET_ORGANIZATION_ID=1)
class RefreshAndApplyTests(TestCase):
    def setUp(self):
        self.private = TemporaryDirectory()
        self.override = override_settings(PRIVATE_MEDIA_ROOT=self.private.name)
        self.override.enable()
        self.addCleanup(self.override.disable)
        self.addCleanup(self.private.cleanup)
        self.organization = Organization.objects.create(
            id=1, name="Аквалайн", paid_until=timezone.now() + timedelta(days=30)
        )
        self.other = Organization.objects.create(name="Другая")
        self.user = User.objects.create_user("owner", password="secret")
        OrganizationAccess.objects.create(
            organization=self.organization, user=self.user, role="owner"
        )
        self.client.force_login(self.user)

    def start(self, report_types=(REPORT_PROFIT,), start=date(2025, 5, 1), end=date(2025, 5, 1)):
        return start_unified_sync(
            self.organization, self.user, report_types,
            mode=OneCODataSyncRun.MODE_AUTO_APPLY,
            period_start=start, period_end=end,
        )[0]

    def auto_effects(self, run):
        return {
            "profit_rows": OneCMonthlyProfit.objects.count(),
            "cashflow_rows": CashFlowRow.objects.count(),
            "candidate_statuses": list(
                OneCImportBatch.objects.filter(sync_run=run)
                .order_by("id").values_list("id", "status")
            ),
            "confirmed_candidates": OneCImportBatch.objects.filter(
                sync_run=run, status=OneCImportBatch.STATUS_CONFIRMED,
            ).count(),
            "active_states": list(
                OneCReportPeriodState.objects.order_by(
                    "organization_id", "report_type", "period_month",
                ).values_list(
                    "organization_id", "report_type", "period_month", "active_batch_id",
                )
            ),
            "activations": OneCReportPeriodActivation.objects.count(),
        }

    def finish_profit(self, rows=None):
        run = self.start()
        with patch(
            "pool_service.finance_imports.odata_unified_sync._collect_profit_chunk",
            return_value=(rows if rows is not None else [profit_row()], 1),
        ):
            return step_unified_sync(
                run.id, self.user, [REPORT_PROFIT], 0, config=config(),
                mode=OneCODataSyncRun.MODE_AUTO_APPLY,
            )

    def test_profit_only_success(self):
        run = self.finish_profit()
        self.assertEqual(run.progress["outcome"], "applied")
        self.assertEqual(OneCMonthlyProfit.objects.count(), 1)
        self.assertEqual(OneCReportPeriodState.objects.get().active_batch.sync_run, run)

    def test_cashflow_only_success(self):
        run = self.start((REPORT_CASHFLOW,))
        with patch(
            "pool_service.finance_imports.odata_unified_sync._collect_cashflow_chunk",
            return_value=([cashflow_row()], 1, []),
        ):
            run = step_unified_sync(
                run.id, self.user, [REPORT_CASHFLOW], 0, config=config(),
                mode=OneCODataSyncRun.MODE_AUTO_APPLY,
            )
        self.assertEqual(run.progress["outcome"], "applied")
        self.assertEqual(OneCReportPeriodState.objects.get().report_type, REPORT_CASHFLOW)

    def test_combined_success_waits_for_cashflow(self):
        run = self.start((REPORT_PROFIT, REPORT_CASHFLOW))
        with patch("pool_service.finance_imports.odata_unified_sync._collect_profit_chunk", return_value=([profit_row()], 1)):
            first = step_unified_sync(
                run.id, self.user, [REPORT_PROFIT, REPORT_CASHFLOW], 0, config=config(),
                mode=OneCODataSyncRun.MODE_AUTO_APPLY,
            )
        self.assertFalse(OneCMonthlyProfit.objects.exists())
        with patch("pool_service.finance_imports.odata_unified_sync._collect_cashflow_chunk", return_value=([cashflow_row()], 1, [])):
            final = step_unified_sync(
                run.id, self.user, [REPORT_PROFIT, REPORT_CASHFLOW], first.cursor["version"],
                config=config(), mode=OneCODataSyncRun.MODE_AUTO_APPLY,
            )
        self.assertEqual(final.progress["outcome"], "applied")
        self.assertEqual(OneCReportPeriodState.objects.count(), 2)

    def test_combined_collection_failure_applies_nothing(self):
        run = self.start((REPORT_PROFIT, REPORT_CASHFLOW))
        with patch("pool_service.finance_imports.odata_unified_sync._collect_profit_chunk", return_value=([profit_row()], 1)):
            first = step_unified_sync(run.id, self.user, [REPORT_PROFIT, REPORT_CASHFLOW], 0, config=config(), mode=OneCODataSyncRun.MODE_AUTO_APPLY)
        with patch("pool_service.finance_imports.odata_unified_sync._collect_cashflow_chunk", side_effect=RuntimeError("network forbidden")):
            second = step_unified_sync(run.id, self.user, [REPORT_PROFIT, REPORT_CASHFLOW], first.cursor["version"], config=config(), mode=OneCODataSyncRun.MODE_AUTO_APPLY)
        self.assertEqual(second.progress["step_state"], "retryable_error")
        self.assertFalse(OneCMonthlyProfit.objects.exists())

    def test_initial_authoritative_empty_is_applied_and_activated(self):
        run = self.finish_profit(rows=[])
        candidate = OneCImportBatch.objects.get(sync_run=run)
        state = OneCReportPeriodState.objects.get(
            organization=self.organization,
            report_type=REPORT_PROFIT,
            period_month=date(2025, 5, 1),
        )
        self.assertEqual(run.progress["outcome"], "applied")
        self.assertEqual(candidate.status, OneCImportBatch.STATUS_CONFIRMED)
        self.assertEqual(candidate.rows_imported, 0)
        self.assertFalse(OneCMonthlyProfit.objects.filter(import_batch=candidate).exists())
        self.assertEqual(state.active_batch, candidate)
        self.assertTrue(
            OneCReportPeriodActivation.objects.filter(
                period_state=state, batch=candidate,
            ).exists()
        )

    def test_repeated_authoritative_empty_is_no_change(self):
        self.finish_profit(rows=[])
        before = {
            "batches": OneCImportBatch.objects.count(),
            "profit_rows": OneCMonthlyProfit.objects.count(),
            "states": list(OneCReportPeriodState.objects.values_list("id", "active_batch_id")),
            "activations": OneCReportPeriodActivation.objects.count(),
        }
        second = self.finish_profit(rows=[])
        self.assertEqual(second.progress["outcome"], "no_change")
        self.assertFalse(OneCImportBatch.objects.filter(sync_run=second).exists())
        self.assertEqual(
            {
                "batches": OneCImportBatch.objects.count(),
                "profit_rows": OneCMonthlyProfit.objects.count(),
                "states": list(OneCReportPeriodState.objects.values_list("id", "active_batch_id")),
                "activations": OneCReportPeriodActivation.objects.count(),
            },
            before,
        )

    def test_authoritative_empty_replaces_nonempty_month(self):
        first = self.finish_profit()
        old_batch = OneCReportPeriodState.objects.get().active_batch
        second = self.finish_profit(rows=[])
        state = OneCReportPeriodState.objects.get()
        self.assertEqual(second.progress["outcome"], "applied")
        self.assertNotEqual(state.active_batch, old_batch)
        self.assertEqual(state.active_batch.rows_imported, 0)
        self.assertEqual(OneCReportPeriodActivation.objects.count(), 2)

    def test_permission_revoked_before_apply_rolls_back(self):
        run = self.start()
        with patch("pool_service.finance_imports.odata_unified_sync._collect_profit_chunk", return_value=([profit_row()], 1)), patch(
            "pool_service.finance_imports.odata_unified_sync._has_report_permission",
            side_effect=[True, True, False],
        ):
            run = step_unified_sync(run.id, self.user, [REPORT_PROFIT], 0, config=config(), mode=OneCODataSyncRun.MODE_AUTO_APPLY)
        self.assertEqual(run.progress["step_state"], "permission_revoked")
        self.assertFalse(OneCMonthlyProfit.objects.exists())

    def test_duplicate_identity_validation_is_safe_and_applies_nothing(self):
        run = self.start()
        duplicate = profit_row()
        with patch("pool_service.finance_imports.odata_unified_sync._collect_profit_chunk", return_value=([profit_row(), duplicate], 1)):
            run = step_unified_sync(run.id, self.user, [REPORT_PROFIT], 0, config=config(), mode=OneCODataSyncRun.MODE_AUTO_APPLY)
        self.assertEqual(run.status, OneCODataSyncRun.STATUS_FAILED)
        self.assertEqual(run.progress["step_state"], "candidate_invalid")
        self.assertFalse(OneCMonthlyProfit.objects.exists())

    def test_terminal_explicit_start_gets_new_key(self):
        first = self.finish_profit(rows=[])
        second = self.start()
        self.assertNotEqual(first.pk, second.pk)
        self.assertNotEqual(first.idempotency_key, second.idempotency_key)

    def test_nonterminal_same_scope_reuses_run_and_key(self):
        first = self.start()
        second = self.start()
        self.assertEqual(first.pk, second.pk)
        self.assertEqual(first.idempotency_key, second.idempotency_key)

    def test_auto_different_scope_conflicts(self):
        self.start()
        with self.assertRaises(SyncConflictError):
            self.start(start=date(2025, 4, 1))

    def test_cross_mode_conflicts(self):
        self.start()
        with self.assertRaises(SyncConflictError):
            start_unified_sync(self.organization, self.user, [REPORT_PROFIT], today=date(2025, 5, 1))

    def test_period_over_24_months_creates_no_run(self):
        with self.assertRaises(ValidationError):
            self.start(start=date(2023, 5, 1), end=date(2025, 5, 1))
        self.assertFalse(OneCODataSyncRun.objects.exists())

    def test_invalid_period_route_is_safe_400(self):
        response = self.client.post(reverse("finance_onec_refresh_apply_start"), {
            "period_start": "2023-05", "period_end": "2025-05",
        })
        self.assertEqual(response.status_code, 400)
        self.assertNotIn("Traceback", response.content.decode())
        self.assertFalse(OneCODataSyncRun.objects.exists())

    def test_conflict_route_is_safe_409_without_run_id(self):
        self.start()
        response = self.client.post(reverse("finance_onec_refresh_apply_start"), {
            "period_start": "2025-04", "period_end": "2025-05",
        })
        self.assertEqual(response.status_code, 409)
        self.assertNotIn(str(OneCODataSyncRun.objects.get().id), response.content.decode())

    def test_legacy_routes_hide_auto_run(self):
        run = self.start()
        self.assertEqual(self.client.get(reverse("finance_onec_odata_sync_status", args=[run.id])).status_code, 404)
        self.assertEqual(self.client.post(reverse("finance_onec_odata_sync_step", args=[run.id]), {"cursor": 0}).status_code, 404)
        self.assertEqual(self.client.post(reverse("finance_onec_odata_sync_reactivate", args=[run.id]), {}).status_code, 404)

    def test_auto_routes_hide_preview_run(self):
        run, _ = start_unified_sync(self.organization, self.user, [REPORT_PROFIT], today=date(2025, 5, 1))
        self.assertEqual(self.client.get(reverse("finance_onec_refresh_apply_status", args=[run.id])).status_code, 404)
        self.assertEqual(self.client.post(reverse("finance_onec_refresh_apply_step", args=[run.id]), {"cursor": 0}).status_code, 404)

    def test_status_payload_is_sanitized(self):
        run = self.start()
        response = self.client.get(reverse("finance_onec_refresh_apply_status", args=[run.id]))
        body = response.content.decode()
        for forbidden in ("apply_plan", "baseline", "scope_fingerprint", "batch_id", "snapshot", "raw payload", "traceback"):
            self.assertNotIn(forbidden, body.lower())

    def test_internal_candidate_is_not_in_manual_list(self):
        run = self.start()
        candidate = OneCImportBatch.objects.create(
            organization=self.organization, import_type=REPORT_PROFIT,
            source_type=OneCImportBatch.SOURCE_ODATA, original_filename="internal.json",
            stored_file="internal.json", file_sha256="1" * 64,
            status=OneCImportBatch.STATUS_PREVIEWED, uploaded_by=self.user, sync_run=run,
        )
        response = self.client.get(reverse("finance_onec_import_list"))
        self.assertNotContains(response, candidate.original_filename)

    def test_manual_views_reject_internal_candidate(self):
        run = self.start()
        batch = OneCImportBatch.objects.create(
            organization=self.organization, import_type=REPORT_PROFIT,
            source_type=OneCImportBatch.SOURCE_ODATA, original_filename="internal.json",
            stored_file="internal.json", file_sha256="2" * 64,
            status=OneCImportBatch.STATUS_PREVIEWED, uploaded_by=self.user, sync_run=run,
        )
        self.assertEqual(self.client.get(reverse("finance_onec_import_preview", args=[batch.id])).status_code, 404)
        self.assertEqual(self.client.post(reverse("finance_onec_import_confirm", args=[batch.id])).status_code, 404)
        self.assertEqual(self.client.post(reverse("finance_onec_import_cancel", args=[batch.id])).status_code, 404)

    def test_service_mutations_reject_internal_candidate(self):
        run = self.start()
        batch = OneCImportBatch.objects.create(
            organization=self.organization, import_type=REPORT_PROFIT,
            source_type=OneCImportBatch.SOURCE_ODATA, original_filename="internal.json",
            stored_file="internal.json", file_sha256="3" * 64,
            status=OneCImportBatch.STATUS_PREVIEWED, uploaded_by=self.user, sync_run=run,
        )
        with self.assertRaises(OneCImportBatch.DoesNotExist):
            confirm_monthly_profit(batch.id, self.organization, self.user)
        with self.assertRaises(OneCImportBatch.DoesNotExist):
            confirm_odata_profit(batch.id, self.organization, self.user, config=config())
        with self.assertRaises(OneCImportBatch.DoesNotExist):
            cancel_onec_import_batch(batch, self.user)

    def test_manual_candidate_cancel_still_works(self):
        batch = OneCImportBatch.objects.create(
            organization=self.organization, import_type=REPORT_PROFIT,
            original_filename="manual.xlsx", stored_file="manual.xlsx",
            file_sha256="4" * 64, status=OneCImportBatch.STATUS_PREVIEWED,
            uploaded_by=self.user,
        )
        cancel_onec_import_batch(batch, self.user)
        batch.refresh_from_db()
        self.assertEqual(batch.status, OneCImportBatch.STATUS_CANCELLED)

    def test_fault_before_success_rolls_back_every_finance_mutation(self):
        run = self.start()
        with patch("pool_service.finance_imports.odata_unified_sync._collect_profit_chunk", return_value=([profit_row()], 1)), patch(
            "pool_service.finance_imports.odata_unified_sync._auto_apply_fault",
            side_effect=lambda stage: (_ for _ in ()).throw(RuntimeError("fault")) if stage == "before_success" else None,
        ):
            run = step_unified_sync(run.id, self.user, [REPORT_PROFIT], 0, config=config(), mode=OneCODataSyncRun.MODE_AUTO_APPLY)
        self.assertEqual(run.status, OneCODataSyncRun.STATUS_FAILED)
        self.assertFalse(OneCMonthlyProfit.objects.exists())
        self.assertFalse(OneCReportPeriodState.objects.exists())
        self.assertFalse(OneCReportPeriodActivation.objects.exists())

    def test_cas_detects_concurrent_state_create(self):
        run = self.start((REPORT_PROFIT, REPORT_CASHFLOW))
        with patch("pool_service.finance_imports.odata_unified_sync._collect_profit_chunk", return_value=([profit_row()], 1)):
            first = step_unified_sync(run.id, self.user, [REPORT_PROFIT, REPORT_CASHFLOW], 0, config=config(), mode=OneCODataSyncRun.MODE_AUTO_APPLY)
        external = OneCImportBatch.objects.create(
            organization=self.organization, import_type=REPORT_PROFIT,
            original_filename="external.xlsx", stored_file="external.xlsx", file_sha256="5" * 64,
            status=OneCImportBatch.STATUS_CONFIRMED, uploaded_by=self.user,
            period_first=date(2025, 5, 1), period_last=date(2025, 5, 1),
            metadata={"scope_months": ["2025-05-01"]},
        )
        OneCReportPeriodState.objects.create(
            organization=self.organization, report_type=REPORT_PROFIT,
            period_month=date(2025, 5, 1), active_batch=external, updated_by=self.user,
        )
        with patch("pool_service.finance_imports.odata_unified_sync._collect_cashflow_chunk", return_value=([], 1, [])):
            final = step_unified_sync(run.id, self.user, [REPORT_PROFIT, REPORT_CASHFLOW], first.cursor["version"], config=config(), mode=OneCODataSyncRun.MODE_AUTO_APPLY)
        self.assertEqual(final.progress["step_state"], "stale")
        self.assertFalse(OneCMonthlyProfit.objects.exists())

    def test_active_lease_and_stale_worker_are_mode_scoped(self):
        run = self.start()
        run.lease_token = "11111111-1111-1111-1111-111111111111"
        run.lease_started_at = timezone.now()
        run.save(update_fields=["lease_token", "lease_started_at"])
        with patch("pool_service.finance_imports.odata_unified_sync._collect_profit_chunk") as collector:
            busy = step_unified_sync(run.id, self.user, [REPORT_PROFIT], 0, config=config(), mode=OneCODataSyncRun.MODE_AUTO_APPLY)
        collector.assert_not_called()
        self.assertEqual(busy.progress["step_state"], "busy")

    def test_tenant_isolation(self):
        run = self.start()
        other_user = User.objects.create_user("other")
        OrganizationAccess.objects.create(organization=self.other, user=other_user, role="owner")
        self.client.force_login(other_user)
        response = self.client.get(reverse("finance_onec_refresh_apply_status", args=[run.id]))
        self.assertNotEqual(response.status_code, 200)

    def test_ui_has_primary_secondary_and_payroll_exclusion(self):
        response = self.client.get(reverse("finance_onec_import_list"))
        self.assertContains(response, "Обновить и применить данные из 1С")
        self.assertContains(response, "Проверить изменения без применения")
        self.assertContains(response, "ФОТ в это обновление не входит")

    def test_real_collectors_are_never_needed_by_start_or_status(self):
        with patch("pool_service.finance_imports.odata_unified_sync.read_profit_rows", side_effect=AssertionError("network")), patch(
            "pool_service.finance_imports.odata_unified_sync.read_cashflow_rows", side_effect=AssertionError("network")
        ):
            response = self.client.post(reverse("finance_onec_refresh_apply_start"), {
                "period_start": "2025-05", "period_end": "2025-05",
            })
            self.assertEqual(response.status_code, 200)
            run_id = response.json()["run_id"]
            self.assertEqual(self.client.get(reverse("finance_onec_refresh_apply_status", args=[run_id])).status_code, 200)

    def test_direct_finalize_rejects_unfinished_run(self):
        run = self.start()
        before = self.auto_effects(run)
        finalized = apply_auto_sync(run.id, self.user, [REPORT_PROFIT], config=config())
        self.assertEqual(finalized.status, OneCODataSyncRun.STATUS_FAILED)
        self.assertEqual(finalized.progress["step_state"], "candidate_invalid")
        self.assertIsNone(finalized.applied_at)
        self.assertEqual(self.auto_effects(run), before)

    def test_finalize_rejects_missing_unchanged_month_baseline(self):
        run = self.start(start=date(2025, 4, 1), end=date(2025, 5, 1))
        with patch("pool_service.finance_imports.odata_unified_sync._collect_profit_chunk", return_value=([profit_row()], 1)), patch(
            "pool_service.finance_imports.odata_unified_sync.apply_auto_sync",
            side_effect=lambda run_id, *_args, **_kwargs: OneCODataSyncRun.objects.get(pk=run_id),
        ):
            collected = step_unified_sync(
                run.id, self.user, [REPORT_PROFIT], 0, config=config(),
                mode=OneCODataSyncRun.MODE_AUTO_APPLY,
            )
        scope = dict(collected.sync_scope)
        scope["_baseline"] = [
            item for item in scope["_baseline"] if item["month"] != "2025-04-01"
        ]
        collected.sync_scope = scope
        collected.save(update_fields=["sync_scope"])
        before = self.auto_effects(collected)
        finalized = apply_auto_sync(collected.id, self.user, [REPORT_PROFIT], config=config())
        self.assertEqual(finalized.status, OneCODataSyncRun.STATUS_FAILED)
        self.assertEqual(self.auto_effects(collected), before)

    def test_candidate_snapshot_change_between_precheck_and_locked_apply_is_rejected(self):
        run = self.start()
        with patch("pool_service.finance_imports.odata_unified_sync._collect_profit_chunk", return_value=([profit_row()], 1)), patch(
            "pool_service.finance_imports.odata_unified_sync.apply_auto_sync",
            side_effect=lambda run_id, *_args, **_kwargs: OneCODataSyncRun.objects.get(pk=run_id),
        ):
            collected = step_unified_sync(
                run.id, self.user, [REPORT_PROFIT], 0, config=config(),
                mode=OneCODataSyncRun.MODE_AUTO_APPLY,
            )
        candidate = OneCImportBatch.objects.get(sync_run=collected)
        valid_payload = read_profit_snapshot(candidate)
        changed_payload = {**valid_payload, "rows": [{**valid_payload["rows"][0], "revenue": "999.00"}]}
        before = self.auto_effects(collected)
        with patch(
            "pool_service.finance_imports.odata_unified_sync.read_profit_snapshot",
            side_effect=[valid_payload, changed_payload],
        ):
            finalized = apply_auto_sync(collected.id, self.user, [REPORT_PROFIT], config=config())
        self.assertEqual(finalized.status, OneCODataSyncRun.STATUS_FAILED)
        self.assertEqual(finalized.progress["step_state"], "apply_failed")
        candidate.refresh_from_db()
        self.assertEqual(candidate.status, OneCImportBatch.STATUS_PREVIEWED)
        self.assertEqual(self.auto_effects(collected), before)

    def test_repeated_finalize_of_applied_run_is_idempotent(self):
        applied = self.finish_profit()
        before = self.auto_effects(applied)
        repeated = apply_auto_sync(applied.id, self.user, [REPORT_PROFIT], config=config())
        repeated.refresh_from_db()
        self.assertEqual(repeated.status, OneCODataSyncRun.STATUS_COMPLETED)
        self.assertEqual(repeated.progress["outcome"], "applied")
        self.assertIsNotNone(repeated.applied_at)
        self.assertEqual(self.auto_effects(repeated), before)

    def test_combined_finalize_rejects_apply_plan_missing_cashflow_change(self):
        run = self.start((REPORT_PROFIT, REPORT_CASHFLOW))
        with patch(
            "pool_service.finance_imports.odata_unified_sync._collect_profit_chunk",
            return_value=([profit_row()], 1),
        ):
            first = step_unified_sync(
                run.id, self.user, [REPORT_PROFIT, REPORT_CASHFLOW], 0,
                config=config(), mode=OneCODataSyncRun.MODE_AUTO_APPLY,
            )
        with patch(
            "pool_service.finance_imports.odata_unified_sync._collect_cashflow_chunk",
            return_value=([cashflow_row()], 1, []),
        ), patch(
            "pool_service.finance_imports.odata_unified_sync.apply_auto_sync",
            side_effect=lambda run_id, *_args, **_kwargs: OneCODataSyncRun.objects.get(pk=run_id),
        ):
            collected = step_unified_sync(
                run.id, self.user, [REPORT_PROFIT, REPORT_CASHFLOW],
                first.cursor["version"], config=config(),
                mode=OneCODataSyncRun.MODE_AUTO_APPLY,
            )
        self.assertEqual(
            {item["source_status"] for item in collected.sync_scope["_baseline"]},
            {"changed"},
        )
        scope = dict(collected.sync_scope)
        scope["_apply_plan"] = [
            item for item in scope["_apply_plan"] if item["report_type"] != REPORT_CASHFLOW
        ]
        collected.sync_scope = scope
        collected.save(update_fields=["sync_scope"])
        before = self.auto_effects(collected)
        finalized = apply_auto_sync(
            collected.id, self.user, [REPORT_PROFIT, REPORT_CASHFLOW], config=config()
        )
        self.assertEqual(finalized.status, OneCODataSyncRun.STATUS_FAILED)
        self.assertEqual(finalized.progress["step_state"], "candidate_invalid")
        self.assertEqual(self.auto_effects(collected), before)

    def test_repeated_finalize_of_no_change_run_is_idempotent(self):
        self.finish_profit(rows=[])
        no_change = self.finish_profit(rows=[])
        self.assertEqual(no_change.status, OneCODataSyncRun.STATUS_COMPLETED)
        self.assertEqual(no_change.progress["outcome"], "no_change")
        before = self.auto_effects(no_change)
        repeated = apply_auto_sync(no_change.id, self.user, [REPORT_PROFIT], config=config())
        repeated.refresh_from_db()
        self.assertEqual(repeated.status, OneCODataSyncRun.STATUS_COMPLETED)
        self.assertEqual(repeated.progress["outcome"], "no_change")
        self.assertEqual(self.auto_effects(repeated), before)
