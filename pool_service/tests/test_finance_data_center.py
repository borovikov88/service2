import uuid
from datetime import date, timedelta
from types import SimpleNamespace
from unittest.mock import patch

from django.contrib.auth.models import User
from django.contrib.sessions.backends.db import SessionStore
from django.test import RequestFactory, TestCase
from django.urls import reverse
from django.utils import timezone

from pool_service.finance_views import (
    _FINANCE_ONEC_COMPLETED_RUN_SESSION_KEY,
    _record_payroll_plan_run_result,
    finance_onec_refresh_apply_step,
    finance_payroll_plan_refresh,
)
from pool_service.models import (
    OneCImportBatch,
    OneCODataSyncRun,
    OneCReportPeriodState,
    Organization,
    OrganizationAccess,
    PayrollPlanSnapshot,
)


class FinanceDataCenterTests(TestCase):
    def setUp(self):
        self.organization = Organization.objects.create(
            name="Finance Data Center",
            paid_until=timezone.now() + timedelta(days=30),
        )
        self.owner = User.objects.create_user(
            username="finance-data-owner",
            password="password",
            first_name="Александр",
        )
        self.service = User.objects.create_user(
            username="finance-data-service",
            password="password",
        )
        OrganizationAccess.objects.create(
            organization=self.organization,
            user=self.owner,
            role="owner",
        )
        OrganizationAccess.objects.create(
            organization=self.organization,
            user=self.service,
            role="service",
        )

    def batch(self, import_type, month, *, status=OneCImportBatch.STATUS_CONFIRMED):
        return OneCImportBatch.objects.create(
            organization=self.organization,
            import_type=import_type,
            source_type=OneCImportBatch.SOURCE_ODATA,
            original_filename=f"{import_type}-{month:%Y-%m}.json",
            file_sha256=uuid.uuid4().hex + uuid.uuid4().hex,
            file_size=1,
            status=status,
            uploaded_by=self.owner,
            confirmed_by=self.owner if status == OneCImportBatch.STATUS_CONFIRMED else None,
            confirmed_at=timezone.now() if status == OneCImportBatch.STATUS_CONFIRMED else None,
            period_first=month,
            period_last=month,
        )

    def activate(self, import_type, month):
        batch = self.batch(import_type, month)
        OneCReportPeriodState.objects.create(
            organization=self.organization,
            report_type=import_type,
            period_month=month,
            active_batch=batch,
            updated_by=self.owner,
        )
        return batch

    @patch("pool_service.finance_views.is_odata_target_organization", return_value=True)
    def test_data_page_is_single_user_facing_center(self, _target):
        month = date(2026, 9, 1)
        self.activate(OneCImportBatch.TYPE_MONTHLY_PROFIT, month)
        self.activate(OneCImportBatch.TYPE_CASHFLOW, month)
        self.activate(OneCImportBatch.TYPE_PAYROLL_ACCRUAL, month)
        PayrollPlanSnapshot.objects.create(
            organization=self.organization,
            period_month=month,
            source_hash="a" * 64,
            source_rows=17,
            source_organization_guids=[],
            currency_guid=uuid.uuid4(),
            fetched_by=self.owner,
        )
        OneCODataSyncRun.objects.create(
            organization=self.organization,
            requested_by=self.owner,
            mode=OneCODataSyncRun.MODE_AUTO_APPLY,
            status=OneCODataSyncRun.STATUS_COMPLETED,
            requested_report_types=["monthly_profit", "cashflow", "payroll_accrual"],
            sync_scope={
                "monthly_profit": {"start": "2026-07-01", "end": "2026-09-01"},
                "cashflow": {"start": "2026-07-01", "end": "2026-09-01"},
                "payroll_accrual": {"start": "2026-07-01", "end": "2026-09-01"},
                "_schedule_day": "2026-09-20",
            },
            cursor={},
            progress={"outcome": "no_change"},
            result_summary={},
        )

        self.client.force_login(self.owner)
        response = self.client.get(reverse("finance_data"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Данные 1С")
        self.assertContains(response, "Валовая прибыль")
        self.assertContains(response, "ДДС")
        self.assertContains(response, "ФОТ")
        self.assertContains(response, "Оклады сотрудников")
        self.assertContains(response, "Обновить данные из 1С")
        self.assertContains(response, "История обновлений")
        self.assertContains(response, "Автоматически")
        self.assertContains(response, "Технические инструменты")
        self.assertNotContains(response, "История проверок")
        self.assertNotContains(response, "Управленческие данные 1С")

    @patch(
        "pool_service.finance_views._finance_data_default_period",
        return_value=(date(2026, 7, 1), date(2026, 9, 1)),
    )
    @patch("pool_service.finance_views.is_odata_target_organization", return_value=True)
    def test_data_page_marks_previous_month_as_requiring_update(self, _target, _period):
        month = date(2026, 8, 1)
        self.activate(OneCImportBatch.TYPE_MONTHLY_PROFIT, month)
        self.activate(OneCImportBatch.TYPE_CASHFLOW, month)
        self.activate(OneCImportBatch.TYPE_PAYROLL_ACCRUAL, month)
        PayrollPlanSnapshot.objects.create(
            organization=self.organization,
            period_month=month,
            source_hash="b" * 64,
            source_rows=17,
            source_organization_guids=[],
            currency_guid=uuid.uuid4(),
            fetched_by=self.owner,
        )
        self.client.force_login(self.owner)

        response = self.client.get(reverse("finance_data"))

        self.assertEqual(response.status_code, 200)
        statuses = {row["key"]: row for row in response.context["source_statuses"]}
        self.assertEqual(statuses["profit"]["freshness"], "stale")
        self.assertEqual(statuses["cashflow"]["freshness"], "stale")
        self.assertEqual(statuses["payroll"]["freshness"], "stale")
        self.assertEqual(statuses["payroll_plan"]["freshness"], "stale")
        self.assertContains(response, "Требует обновления", count=4)
        self.assertNotContains(response, '<span class="badge text-bg-success">Актуально</span>')

    @patch(
        "pool_service.finance_views._finance_data_default_period",
        return_value=(date(2026, 7, 1), date(2026, 9, 1)),
    )
    @patch("pool_service.finance_views.is_odata_target_organization", return_value=True)
    def test_data_page_offers_resume_for_unfinished_manual_refresh(self, _target, _period):
        run = OneCODataSyncRun.objects.create(
            organization=self.organization,
            requested_by=self.owner,
            mode=OneCODataSyncRun.MODE_AUTO_APPLY,
            status=OneCODataSyncRun.STATUS_RUNNING,
            requested_report_types=["monthly_profit", "cashflow", "payroll_accrual"],
            sync_scope={
                "monthly_profit": {"start": "2026-07-01", "end": "2026-09-01"},
                "cashflow": {"start": "2026-07-01", "end": "2026-09-01"},
                "payroll_accrual": {"start": "2026-07-01", "end": "2026-09-01"},
            },
            cursor={"version": 7},
            progress={"completed_chunks": 4, "total_chunks": 9},
            result_summary={},
        )
        self.client.force_login(self.owner)

        response = self.client.get(reverse("finance_data"))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["active_refresh_run"]["run_id"], str(run.id))
        self.assertEqual(response.context["active_refresh_run"]["cursor"], 7)
        self.assertContains(response, "Есть незавершённое обновление")
        self.assertContains(response, "Продолжить обновление")
        self.assertContains(
            response,
            reverse("finance_onec_refresh_apply_step", kwargs={"run_id": run.id}),
        )
        self.assertNotContains(
            response,
            f'action="{reverse("finance_onec_refresh_apply_start")}"',
        )

    @patch(
        "pool_service.finance_views._finance_data_default_period",
        return_value=(date(2026, 7, 1), date(2026, 9, 1)),
    )
    @patch("pool_service.finance_views.is_odata_target_organization", return_value=True)
    def test_scheduled_refresh_is_not_offered_as_manual_resume(self, _target, _period):
        OneCODataSyncRun.objects.create(
            organization=self.organization,
            requested_by=self.owner,
            mode=OneCODataSyncRun.MODE_AUTO_APPLY,
            status=OneCODataSyncRun.STATUS_RUNNING,
            requested_report_types=["monthly_profit", "cashflow", "payroll_accrual"],
            sync_scope={
                "monthly_profit": {"start": "2026-07-01", "end": "2026-09-01"},
                "cashflow": {"start": "2026-07-01", "end": "2026-09-01"},
                "payroll_accrual": {"start": "2026-07-01", "end": "2026-09-01"},
                "_schedule_day": "2026-09-21",
            },
            cursor={"version": 2},
            progress={"completed_chunks": 1, "total_chunks": 9},
            result_summary={},
        )
        self.client.force_login(self.owner)

        response = self.client.get(reverse("finance_data"))

        self.assertEqual(response.status_code, 200)
        self.assertIsNone(response.context["active_refresh_run"])
        self.assertNotContains(response, "Есть незавершённое обновление")
        self.assertContains(response, 'data-refresh-start')

    @patch(
        "pool_service.finance_views._finance_data_default_period",
        return_value=(date(2026, 7, 1), date(2026, 9, 1)),
    )
    @patch("pool_service.finance_views.is_odata_target_organization", return_value=True)
    def test_hourly_scheduled_refresh_is_not_offered_as_manual_resume(self, _target, _period):
        OneCODataSyncRun.objects.create(
            organization=self.organization,
            requested_by=self.owner,
            mode=OneCODataSyncRun.MODE_AUTO_APPLY,
            status=OneCODataSyncRun.STATUS_RUNNING,
            requested_report_types=["monthly_profit", "cashflow", "payroll_accrual"],
            sync_scope={
                "monthly_profit": {"start": "2026-07-01", "end": "2026-09-01"},
                "cashflow": {"start": "2026-07-01", "end": "2026-09-01"},
                "payroll_accrual": {"start": "2026-07-01", "end": "2026-09-01"},
                "_schedule_slot": "2026-09-21T07",
            },
            cursor={"version": 3},
            progress={"completed_chunks": 2, "total_chunks": 9},
            result_summary={},
        )
        self.client.force_login(self.owner)

        response = self.client.get(reverse("finance_data"))

        self.assertEqual(response.status_code, 200)
        self.assertIsNone(response.context["active_refresh_run"])
        self.assertNotContains(response, "Есть незавершённое обновление")
        self.assertContains(
            response,
            f'action="{reverse("finance_onec_refresh_apply_start")}"',
        )

    def _mark_run_completed_in_session(self, run):
        session = self.client.session
        session[_FINANCE_ONEC_COMPLETED_RUN_SESSION_KEY] = {
            "run_id": str(run.id),
            "completed_at": int(timezone.now().timestamp()),
        }
        session.save()

    def _completed_all_data_run(self, **overrides):
        values = {
            "organization": self.organization,
            "requested_by": self.owner,
            "mode": OneCODataSyncRun.MODE_AUTO_APPLY,
            "status": OneCODataSyncRun.STATUS_COMPLETED,
            "requested_report_types": [
                "monthly_profit", "cashflow", "payroll_accrual"
            ],
            "sync_scope": {
                "monthly_profit": {"start": "2026-07-01", "end": "2026-09-01"},
                "cashflow": {"start": "2026-07-01", "end": "2026-09-01"},
                "payroll_accrual": {"start": "2026-07-01", "end": "2026-09-01"},
            },
            "cursor": {},
            "progress": {},
            "result_summary": {},
        }
        values.update(overrides)
        return OneCODataSyncRun.objects.create(**values)

    @patch("pool_service.finance_views.refresh_payroll_plan_snapshot")
    def test_payroll_plan_refresh_records_parent_run_result(self, refresh):
        snapshot = PayrollPlanSnapshot.objects.create(
            organization=self.organization,
            period_month=date(2026, 9, 1),
            source_hash="c" * 64,
            source_rows=17,
            source_organization_guids=[],
            currency_guid=uuid.uuid4(),
            fetched_by=self.owner,
        )
        run = self._completed_all_data_run()
        refresh.return_value = (snapshot, True)
        self.client.force_login(self.owner)
        self._mark_run_completed_in_session(run)

        response = self.client.post(
            reverse("finance_payroll_plan_refresh"),
            {"sync_run_id": str(run.id)},
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )

        self.assertEqual(response.status_code, 200)
        run.refresh_from_db()
        plan_result = run.result_summary["payroll_plan_refresh"]
        self.assertEqual(plan_result["status"], "success")
        self.assertEqual(plan_result["snapshot_id"], snapshot.pk)
        self.assertEqual(plan_result["created"], True)
        self.assertEqual(plan_result["period_month"], "2026-09-01")

    @patch("pool_service.finance_views.refresh_payroll_plan_snapshot")
    def test_authorized_resumer_can_finish_another_users_run(self, refresh):
        resumer = User.objects.create_user(
            username="finance-data-resumer",
            password="password",
            first_name="Бухгалтер",
        )
        OrganizationAccess.objects.create(
            organization=self.organization,
            user=resumer,
            role="accountant",
        )
        snapshot = PayrollPlanSnapshot.objects.create(
            organization=self.organization,
            period_month=date(2026, 9, 1),
            source_hash="f" * 64,
            source_rows=17,
            source_organization_guids=[],
            currency_guid=uuid.uuid4(),
            fetched_by=resumer,
        )
        run = self._completed_all_data_run(requested_by=self.owner)
        refresh.return_value = (snapshot, True)
        self.client.force_login(resumer)
        self._mark_run_completed_in_session(run)

        response = self.client.post(
            reverse("finance_payroll_plan_refresh"),
            {"sync_run_id": str(run.id)},
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )

        self.assertEqual(response.status_code, 200)
        run.refresh_from_db()
        self.assertEqual(
            run.result_summary["payroll_plan_refresh"]["snapshot_id"],
            snapshot.pk,
        )

    @patch("pool_service.finance_views.refresh_payroll_plan_snapshot")
    def test_completed_run_cannot_be_relinked_to_later_snapshot(self, refresh):
        original = PayrollPlanSnapshot.objects.create(
            organization=self.organization,
            period_month=date(2026, 9, 1),
            source_hash="1" * 64,
            source_rows=17,
            source_organization_guids=[],
            currency_guid=uuid.uuid4(),
            fetched_by=self.owner,
        )
        run = self._completed_all_data_run(
            result_summary={
                "payroll_plan_refresh": {
                    "status": "success",
                    "snapshot_id": original.pk,
                    "created": True,
                    "period_month": "2026-09-01",
                    "fetched_at": original.fetched_at.isoformat(),
                    "error_message": "",
                }
            }
        )
        self.client.force_login(self.owner)

        response = self.client.post(
            reverse("finance_payroll_plan_refresh"),
            {"sync_run_id": str(run.id)},
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn("уже привязаны", response.json()["error"])
        refresh.assert_not_called()
        run.refresh_from_db()
        self.assertEqual(
            run.result_summary["payroll_plan_refresh"]["snapshot_id"],
            original.pk,
        )

    def test_payroll_run_result_is_immutable_under_duplicate_recording(self):
        first = PayrollPlanSnapshot.objects.create(
            organization=self.organization,
            period_month=date(2026, 9, 1),
            source_hash="2" * 64,
            source_rows=17,
            source_organization_guids=[],
            currency_guid=uuid.uuid4(),
            fetched_by=self.owner,
        )
        second = PayrollPlanSnapshot.objects.create(
            organization=self.organization,
            period_month=date(2026, 9, 1),
            source_hash="3" * 64,
            source_rows=17,
            source_organization_guids=[],
            currency_guid=uuid.uuid4(),
            fetched_by=self.owner,
        )
        run = self._completed_all_data_run()

        first_write = _record_payroll_plan_run_result(
            run,
            status="success",
            snapshot=first,
            created=True,
        )
        second_write = _record_payroll_plan_run_result(
            run,
            status="success",
            snapshot=second,
            created=True,
        )

        self.assertTrue(first_write)
        self.assertFalse(second_write)
        run.refresh_from_db()
        self.assertEqual(
            run.result_summary["payroll_plan_refresh"]["snapshot_id"],
            first.pk,
        )

    @patch("pool_service.finance_views.refresh_payroll_plan_snapshot")
    def test_old_completed_run_without_completion_marker_is_rejected(self, refresh):
        run = self._completed_all_data_run()
        self.client.force_login(self.owner)

        response = self.client.post(
            reverse("finance_payroll_plan_refresh"),
            {"sync_run_id": str(run.id)},
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn("только что завершён", response.json()["error"])
        refresh.assert_not_called()

    @patch(
        "pool_service.finance_views._onec_sync_report_types",
        return_value=["monthly_profit", "cashflow", "payroll_accrual"],
    )
    @patch("pool_service.finance_views.refresh_payroll_plan_snapshot")
    def test_reposting_step_for_old_completed_run_does_not_arm_payroll_link(
        self, refresh, _report_types
    ):
        run = self._completed_all_data_run(cursor={"version": 4})
        self.client.force_login(self.owner)

        session = SessionStore()
        step_request = RequestFactory().post(
            reverse("finance_onec_refresh_apply_step", kwargs={"run_id": run.id}),
            {"cursor": "4"},
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )
        step_request.user = self.owner
        step_request.session = session
        with patch(
            "pool_service.finance_views._onec_sync_guard",
            return_value=(self.organization, None),
        ):
            step_response = finance_onec_refresh_apply_step(step_request, run.id)
        self.assertEqual(
            step_response.status_code,
            200,
            step_response.content.decode("utf-8", errors="replace"),
        )
        self.assertNotIn(
            _FINANCE_ONEC_COMPLETED_RUN_SESSION_KEY,
            session,
        )

        payroll_request = RequestFactory().post(
            reverse("finance_payroll_plan_refresh"),
            {"sync_run_id": str(run.id)},
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )
        payroll_request.user = self.owner
        payroll_request.session = session
        response = finance_payroll_plan_refresh(payroll_request)

        self.assertEqual(response.status_code, 400)
        refresh.assert_not_called()

    @patch("pool_service.finance_views.refresh_payroll_plan_snapshot")
    def test_scheduled_run_cannot_be_linked_even_with_session_marker(self, refresh):
        run = self._completed_all_data_run(
            sync_scope={
                "monthly_profit": {"start": "2026-07-01", "end": "2026-09-01"},
                "cashflow": {"start": "2026-07-01", "end": "2026-09-01"},
                "payroll_accrual": {"start": "2026-07-01", "end": "2026-09-01"},
                "_schedule_slot": "2026-09-21T07",
            }
        )
        self.client.force_login(self.owner)
        self._mark_run_completed_in_session(run)

        response = self.client.post(
            reverse("finance_payroll_plan_refresh"),
            {"sync_run_id": str(run.id)},
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )

        self.assertEqual(response.status_code, 400)
        refresh.assert_not_called()

    @patch(
        "pool_service.finance_views._onec_sync_report_types",
        return_value=["monthly_profit", "cashflow", "payroll_accrual"],
    )
    @patch("pool_service.finance_views.refresh_payroll_plan_snapshot")
    @patch("pool_service.finance_views.step_unified_sync")
    def test_step_that_completes_run_arms_same_session_for_payroll_link(
        self, step_sync, refresh, _report_types
    ):
        run = self._completed_all_data_run(
            status=OneCODataSyncRun.STATUS_RUNNING,
            cursor={"version": 0},
        )
        snapshot = PayrollPlanSnapshot.objects.create(
            organization=self.organization,
            period_month=date(2026, 9, 1),
            source_hash="4" * 64,
            source_rows=17,
            source_organization_guids=[],
            currency_guid=uuid.uuid4(),
            fetched_by=self.owner,
        )

        def complete_run(run_id, user, report_types, expected, mode):
            current = OneCODataSyncRun.objects.get(pk=run_id)
            current.status = OneCODataSyncRun.STATUS_COMPLETED
            current.save(update_fields=["status"])
            return current

        step_sync.side_effect = complete_run
        refresh.return_value = (snapshot, True)
        self.client.force_login(self.owner)

        session = SessionStore()
        step_request = RequestFactory().post(
            reverse("finance_onec_refresh_apply_step", kwargs={"run_id": run.id}),
            {"cursor": "0"},
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )
        step_request.user = self.owner
        step_request.session = session
        with patch(
            "pool_service.finance_views._onec_sync_guard",
            return_value=(self.organization, None),
        ):
            step_response = finance_onec_refresh_apply_step(step_request, run.id)
        self.assertEqual(
            step_response.status_code,
            200,
            step_response.content.decode("utf-8", errors="replace"),
        )
        self.assertIn(
            _FINANCE_ONEC_COMPLETED_RUN_SESSION_KEY,
            session,
        )

        payroll_request = RequestFactory().post(
            reverse("finance_payroll_plan_refresh"),
            {"sync_run_id": str(run.id)},
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )
        payroll_request.user = self.owner
        payroll_request.session = session
        payroll_response = finance_payroll_plan_refresh(payroll_request)

        self.assertEqual(payroll_response.status_code, 200)
        self.assertNotIn(
            _FINANCE_ONEC_COMPLETED_RUN_SESSION_KEY,
            session,
        )
        run.refresh_from_db()
        self.assertEqual(
            run.result_summary["payroll_plan_refresh"]["snapshot_id"],
            snapshot.pk,
        )

    @patch("pool_service.finance_views.is_odata_target_organization", return_value=True)
    def test_history_combines_created_payroll_snapshot_with_parent_run(self, _target):
        snapshot = PayrollPlanSnapshot.objects.create(
            organization=self.organization,
            period_month=date(2026, 9, 1),
            source_hash="d" * 64,
            source_rows=17,
            source_organization_guids=[],
            currency_guid=uuid.uuid4(),
            fetched_by=self.owner,
        )
        self._completed_all_data_run(
            result_summary={
                "payroll_plan_refresh": {
                    "status": "success",
                    "snapshot_id": snapshot.pk,
                    "created": True,
                    "period_month": "2026-09-01",
                    "fetched_at": snapshot.fetched_at.isoformat(),
                    "error_message": "",
                }
            }
        )
        self.client.force_login(self.owner)

        response = self.client.get(reverse("finance_data"))

        rows = response.context["update_history"]
        self.assertEqual(
            sum(1 for row in rows if row["data_label"] == "Все данные"),
            1,
        )
        self.assertEqual(
            sum(1 for row in rows if row["data_label"] == "Оклады сотрудников"),
            0,
        )
        combined = next(row for row in rows if row["data_label"] == "Все данные")
        self.assertEqual(combined["method"], "Вручную")
        self.assertEqual(
            combined["technical_kind"],
            "Валовая прибыль, ДДС, ФОТ, оклады",
        )

    @patch("pool_service.finance_views.is_odata_target_organization", return_value=True)
    def test_unified_history_keeps_finance_range_and_shows_payroll_month(self, _target):
        snapshot = PayrollPlanSnapshot.objects.create(
            organization=self.organization,
            period_month=date(2026, 9, 1),
            source_hash="5" * 64,
            source_rows=17,
            source_organization_guids=[],
            currency_guid=uuid.uuid4(),
            fetched_by=self.owner,
        )
        self._completed_all_data_run(
            sync_scope={
                "monthly_profit": {"start": "2026-07-01", "end": "2026-08-01"},
                "cashflow": {"start": "2026-07-01", "end": "2026-08-01"},
                "payroll_accrual": {"start": "2026-07-01", "end": "2026-08-01"},
            },
            result_summary={
                "payroll_plan_refresh": {
                    "status": "success",
                    "snapshot_id": snapshot.pk,
                    "created": True,
                    "period_month": "2026-09-01",
                    "fetched_at": snapshot.fetched_at.isoformat(),
                    "error_message": "",
                }
            },
        )
        self.client.force_login(self.owner)

        response = self.client.get(reverse("finance_data"))

        row = next(
            item for item in response.context["update_history"]
            if item["data_label"] == "Все данные"
        )
        self.assertEqual(row["period_start"], "2026-07")
        self.assertEqual(row["period_end"], "2026-08")
        self.assertEqual(row["payroll_period"], "2026-09")
        self.assertContains(response, "Оклады: 2026-09")

    @patch("pool_service.finance_views.is_odata_target_organization", return_value=True)
    def test_linked_snapshot_stays_hidden_when_parent_run_is_outside_display_limit(self, _target):
        snapshot = PayrollPlanSnapshot.objects.create(
            organization=self.organization,
            period_month=date(2026, 9, 1),
            source_hash="6" * 64,
            source_rows=17,
            source_organization_guids=[],
            currency_guid=uuid.uuid4(),
            fetched_by=self.owner,
        )
        self._completed_all_data_run(
            result_summary={
                "payroll_plan_refresh": {
                    "status": "success",
                    "snapshot_id": snapshot.pk,
                    "created": True,
                    "period_month": "2026-09-01",
                    "fetched_at": snapshot.fetched_at.isoformat(),
                    "error_message": "",
                }
            }
        )
        for index in range(31):
            self._completed_all_data_run(
                sync_scope={
                    "monthly_profit": {"start": "2026-09-01", "end": "2026-09-01"},
                    "cashflow": {"start": "2026-09-01", "end": "2026-09-01"},
                    "payroll_accrual": {"start": "2026-09-01", "end": "2026-09-01"},
                    "_schedule_slot": f"2026-09-21T{index % 24:02d}-{index}",
                }
            )
        self.client.force_login(self.owner)

        response = self.client.get(reverse("finance_data"))

        rows = response.context["update_history"]
        self.assertFalse(
            any(row["data_label"] == "Оклады сотрудников" for row in rows)
        )

    @patch("pool_service.finance_views.is_odata_target_organization", return_value=True)
    def test_existing_payroll_snapshot_remains_separate_when_run_reuses_it(self, _target):
        snapshot = PayrollPlanSnapshot.objects.create(
            organization=self.organization,
            period_month=date(2026, 9, 1),
            source_hash="e" * 64,
            source_rows=17,
            source_organization_guids=[],
            currency_guid=uuid.uuid4(),
            fetched_by=self.owner,
        )
        self._completed_all_data_run(
            result_summary={
                "payroll_plan_refresh": {
                    "status": "success",
                    "snapshot_id": snapshot.pk,
                    "created": False,
                    "period_month": "2026-09-01",
                    "fetched_at": snapshot.fetched_at.isoformat(),
                    "error_message": "",
                }
            }
        )
        self.client.force_login(self.owner)

        response = self.client.get(reverse("finance_data"))

        rows = response.context["update_history"]
        self.assertTrue(any(row["data_label"] == "Все данные" for row in rows))
        self.assertTrue(
            any(row["data_label"] == "Оклады сотрудников" for row in rows)
        )

    @patch("pool_service.finance_views.is_odata_target_organization", return_value=True)
    def test_hourly_sync_history_is_marked_automatic(self, _target):
        self._completed_all_data_run(
            sync_scope={
                "monthly_profit": {"start": "2026-07-01", "end": "2026-09-01"},
                "cashflow": {"start": "2026-07-01", "end": "2026-09-01"},
                "payroll_accrual": {"start": "2026-07-01", "end": "2026-09-01"},
                "_schedule_slot": "2026-09-21T07",
            }
        )
        self.client.force_login(self.owner)

        response = self.client.get(reverse("finance_data"))

        run_row = next(
            row for row in response.context["update_history"]
            if row["technical_kind"] == "Обновление"
        )
        self.assertEqual(run_row["method"], "Автоматически")

    @patch("pool_service.finance_views.is_odata_target_organization", return_value=True)
    def test_failed_payroll_plan_step_marks_unified_history_partial(self, _target):
        self._completed_all_data_run(
            result_summary={
                "payroll_plan_refresh": {
                    "status": "failed",
                    "snapshot_id": None,
                    "created": False,
                    "period_month": None,
                    "fetched_at": None,
                    "error_message": "1С не ответила по окладам.",
                }
            }
        )
        self.client.force_login(self.owner)

        response = self.client.get(reverse("finance_data"))

        row = next(
            item for item in response.context["update_history"]
            if item["data_label"] == "Все данные"
        )
        self.assertEqual(row["result_label"], "Частично выполнено")
        self.assertEqual(row["result_tone"], "warning")
        self.assertIn("1С не ответила по окладам", row["error_message"])

    def test_operational_employee_cannot_open_management_data_center(self):
        self.client.force_login(self.service)

        response = self.client.get(reverse("finance_data"))

        self.assertEqual(response.status_code, 403)

    @patch("pool_service.finance_views.refresh_payroll_plan_snapshot")
    def test_payroll_plan_refresh_supports_unified_ajax_flow(self, refresh):
        snapshot = SimpleNamespace(
            period_month=date(2026, 9, 1),
            fetched_at=timezone.now(),
        )
        refresh.return_value = (snapshot, True)
        self.client.force_login(self.owner)

        response = self.client.post(
            reverse("finance_payroll_plan_refresh"),
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["ok"], True)
        self.assertEqual(response.json()["period_month"], "2026-09-01")
        self.assertIn("Оклады из 1С обновлены", response.json()["message"])
