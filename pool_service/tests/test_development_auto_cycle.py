import json
from io import StringIO
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import patch

from django.contrib.auth.models import User
from django.core.management import call_command, CommandError
from django.db import OperationalError
from django.test import TestCase, override_settings
from django.utils import timezone

from pool_service.development_forms import (
    DevelopmentTaskCreateForm,
    DevelopmentTaskUpdateForm,
)
from pool_service.models import DevelopmentIteration, DevelopmentTask, Notification, Organization, OrganizationAccess
from pool_service.services.development_review import ReviewResult, run_review
from pool_service.services.development_codex import dispatch_corrective_codex


SETTINGS = override_settings(
    OPENAI_API_KEY="test-key", OPENAI_DEVELOPMENT_MODEL="gpt-5.6-luna",
    DEVELOPMENT_MAX_CORRECTIVE_ITERATIONS=3,
    GITHUB_DEVELOPMENT_TOKEN="token", GITHUB_DEVELOPMENT_REPOSITORY="owner/repo",
    GITHUB_DEVELOPMENT_WORKFLOW="development-codex.yml",
    GITHUB_DEVELOPMENT_PROMPT_MAX_BYTES=40000,
)


class AutoCycleFixtureMixin:
    def setUp(self):
        self.org = Organization.objects.create(name="Auto", paid_until=timezone.now() + timedelta(days=3))
        self.user = User.objects.create_user("auto-owner")
        OrganizationAccess.objects.create(organization=self.org, user=self.user, role="owner")
        self.task = DevelopmentTask.objects.create(
            organization=self.org, initiator=self.user, title="Cycle", description="Implement",
            business_goal="Automate", definition_of_done="Tests pass",
            status=DevelopmentTask.STATUS_REVIEW, current_stage=DevelopmentTask.STAGE_REVIEW,
            automation_metadata={
                "effective_model": "gpt-5.6-luna",
                "auto_cycle_enabled": True,
            },
        )
        DevelopmentIteration.objects.create(
            task=self.task, iteration_number=1, executor_type="system", status="accepted",
            response="Primary analysis", automation_metadata={"purpose": "primary_analysis"},
        )
        self.codex = DevelopmentIteration.objects.create(
            task=self.task, iteration_number=2, executor_type="codex", status="accepted",
            result_summary="Done", response="PR body", test_result="passed",
            automation_metadata={"purpose": "codex_execution", "state": "completed", "applied": True, "validation_state": "passed"},
        )
        data = dict(self.task.automation_metadata)
        data["active_codex_iteration_id"] = self.codex.pk
        self.task.automation_metadata = data
        self.task.save(update_fields=["automation_metadata"])

    def response(self, decision, *, instructions=None, human_reason=None, response_id="resp-review"):
        body = {"decision": decision, "summary": f"Decision {decision}", "findings": ["finding"] if decision != "accepted" else [], "corrective_instructions": instructions or [], "human_reason": human_reason}
        usage = SimpleNamespace(input_tokens=100, output_tokens=20, input_tokens_details=SimpleNamespace(cached_tokens=0))
        return SimpleNamespace(id=response_id, model="gpt-5.6-luna", output_text=json.dumps(body), usage=usage)


class DevelopmentAutoCycleTests(AutoCycleFixtureMixin, TestCase):
    def test_auto_cycle_marker_is_not_user_editable(self):
        self.assertNotIn("automation_metadata", DevelopmentTaskCreateForm().fields)
        self.assertNotIn(
            "automation_metadata",
            DevelopmentTaskUpdateForm(instance=self.task).fields,
        )
        self.assertNotIn("auto_cycle_enabled", DevelopmentTaskCreateForm().fields)
        self.assertNotIn(
            "auto_cycle_enabled",
            DevelopmentTaskUpdateForm(instance=self.task).fields,
        )

    @SETTINGS
    @patch("pool_service.services.development_db.close_old_connections")
    @patch("pool_service.services.development_review._create_response")
    def test_review_recycles_database_connection_around_external_io(
        self, create, close_connections
    ):
        create.return_value = self.response("accepted")

        result = run_review(self.task.pk)

        self.assertEqual(result.state, "accepted")
        self.assertEqual(close_connections.call_count, 2)

    @SETTINGS
    @patch("pool_service.services.development_db.close_old_connections")
    @patch("pool_service.services.development_review._create_response")
    def test_review_external_failure_recycles_connection_before_recovery_orm(
        self, create, close_connections
    ):
        create.side_effect = TimeoutError("uncertain external result")

        result = run_review(self.task.pk)

        self.task.refresh_from_db()
        self.assertEqual(result.state, "launch_unknown")
        self.assertEqual(self.task.status, DevelopmentTask.STATUS_BLOCKED)
        self.assertEqual(close_connections.call_count, 2)

    @SETTINGS
    @patch("pool_service.services.development_db.close_old_connections")
    @patch(
        "pool_service.services.development_review._store_review_response",
        side_effect=OperationalError(2006, "password=must-not-be-logged"),
    )
    @patch("pool_service.services.development_review._create_response")
    def test_database_failure_after_review_response_is_safe_and_observable(
        self, create, _store, close_connections
    ):
        create.return_value = self.response("accepted")

        with self.assertLogs(
            "pool_service.services.development_review", level="WARNING"
        ) as captured, self.assertRaises(OperationalError):
            run_review(self.task.pk)

        review = self.task.iterations.get(
            executor_type=DevelopmentIteration.EXECUTOR_CHATGPT
        )
        logs = "\n".join(captured.output)
        self.assertEqual(review.automation_metadata["state"], "launching")
        self.assertIn(f"review={review.pk}", logs)
        self.assertIn("error_type=OperationalError", logs)
        self.assertIn("db_error_code=2006", logs)
        self.assertNotIn("must-not-be-logged", logs)
        self.assertEqual(close_connections.call_count, 2)

    @SETTINGS
    @patch("pool_service.services.development_review._create_response")
    def test_accepted_is_idempotent_and_notifies_once(self, create):
        create.return_value = self.response("accepted")
        first = run_review(self.task.pk)
        second = run_review(self.task.pk)
        self.task.refresh_from_db()
        self.assertTrue(first.changed)
        self.assertFalse(second.changed)
        self.assertEqual(self.task.status, DevelopmentTask.STATUS_READY_FOR_DEPLOY)
        self.assertEqual(create.call_count, 1)
        self.assertEqual(Notification.objects.filter(dedupe_key=f"development-task:{self.task.pk}:ready-for-deploy").count(), 1)

    @SETTINGS
    @patch("pool_service.services.development_review._create_response")
    def test_corrective_decision_prepares_revision(self, create):
        create.return_value = self.response("corrective_required", instructions=["Fix failing test"])
        result = run_review(self.task.pk)
        self.task.refresh_from_db()
        self.assertEqual(result.state, "corrective_required")
        self.assertEqual(self.task.status, DevelopmentTask.STATUS_REVISION)
        review = self.task.iterations.get(executor_type="chatgpt")
        self.assertEqual(review.next_prompt, "Fix failing test")

    @SETTINGS
    @patch("pool_service.services.development_review._create_response")
    def test_human_required_stops_cycle(self, create):
        create.return_value = self.response("human_required", human_reason="Ambiguous scope")
        run_review(self.task.pk)
        self.task.refresh_from_db()
        self.assertEqual(self.task.status, DevelopmentTask.STATUS_BLOCKED)
        self.assertEqual(self.task.blockers, "Ambiguous scope")

    @SETTINGS
    @patch("pool_service.services.development_codex._dispatch_workflow")
    @patch("pool_service.services.development_review._create_response")
    def test_corrective_launch_is_deduplicated(self, create, dispatch):
        create.return_value = self.response("corrective_required", instructions=["Fix test"])
        review_id = run_review(self.task.pk).review_id
        metadata = dict(self.task.automation_metadata)
        metadata.pop("auto_cycle_enabled")
        self.task.automation_metadata = metadata
        self.task.save(update_fields=["automation_metadata"])
        first = dispatch_corrective_codex(self.task.pk, review_id)
        second = dispatch_corrective_codex(self.task.pk, review_id)
        self.task.refresh_from_db()
        self.assertTrue(first.changed)
        self.assertFalse(second.changed)
        self.assertEqual(dispatch.call_count, 1)
        self.assertIs(self.task.automation_metadata["auto_cycle_enabled"], True)
        self.assertEqual(self.task.iterations.filter(automation_metadata__corrective_review_id=review_id).count(), 1)

    @SETTINGS
    @override_settings(DEVELOPMENT_MAX_CORRECTIVE_ITERATIONS=0)
    @patch("pool_service.services.development_review._create_response")
    def test_corrective_limit_stops_without_dispatch(self, create):
        create.return_value = self.response("corrective_required", instructions=["Fix test"])
        review_id = run_review(self.task.pk).review_id
        result = dispatch_corrective_codex(self.task.pk, review_id)
        self.task.refresh_from_db()
        self.assertEqual(result.state, "limit_reached")
        self.assertEqual(self.task.status, DevelopmentTask.STATUS_BLOCKED)


class PollDevelopmentCodexCommandTests(AutoCycleFixtureMixin, TestCase):
    def run_command(self, **options):
        output = StringIO()
        command_options = {"batch_size": 25, **options}
        call_command("poll_development_codex", stdout=output, **command_options)
        return output.getvalue().strip()

    @SETTINGS
    @patch("pool_service.management.commands.poll_development_codex.run_review")
    def test_regular_poll_processes_explicitly_enabled_task(self, review):
        review.return_value = ReviewResult("accepted", changed=True, review_id=3)

        output = self.run_command()

        review.assert_called_once_with(self.task.pk)
        self.assertIn("reviewed=1", output)

    @SETTINGS
    @patch("pool_service.management.commands.poll_development_codex.run_review")
    def test_regular_poll_skips_historical_task_without_marker(self, review):
        metadata = dict(self.task.automation_metadata)
        metadata.pop("auto_cycle_enabled")
        self.task.automation_metadata = metadata
        self.task.save(update_fields=["automation_metadata"])

        output = self.run_command()

        review.assert_not_called()
        self.assertEqual(output, "checked=0 reviewed=0 corrective=0 errors=0")

    @SETTINGS
    @patch("pool_service.management.commands.poll_development_codex.run_review")
    def test_regular_poll_skips_explicitly_disabled_task(self, review):
        metadata = dict(self.task.automation_metadata)
        metadata["auto_cycle_enabled"] = False
        self.task.automation_metadata = metadata
        self.task.save(update_fields=["automation_metadata"])

        output = self.run_command()

        review.assert_not_called()
        self.assertEqual(output, "checked=0 reviewed=0 corrective=0 errors=0")

    @SETTINGS
    @patch("pool_service.management.commands.poll_development_codex.run_review")
    def test_task_id_processes_only_requested_legacy_task(self, review):
        metadata = dict(self.task.automation_metadata)
        metadata.pop("auto_cycle_enabled")
        self.task.automation_metadata = metadata
        self.task.save(update_fields=["automation_metadata"])
        other = DevelopmentTask.objects.create(
            organization=self.org,
            initiator=self.user,
            title="Other eligible cycle",
            description="Must not be processed by targeted polling",
            status=DevelopmentTask.STATUS_REVIEW,
            current_stage=DevelopmentTask.STAGE_REVIEW,
            automation_metadata={"auto_cycle_enabled": True},
        )
        review.return_value = ReviewResult("accepted", changed=True, review_id=3)

        output = self.run_command(task_id=self.task.pk)

        review.assert_called_once_with(self.task.pk)
        self.assertNotEqual(other.pk, self.task.pk)
        self.assertIn(f"target_task_id={self.task.pk}", output)
        self.assertIn("reviewed=1", output)

    @SETTINGS
    @patch("pool_service.management.commands.poll_development_codex.run_review")
    def test_missing_task_id_fails_without_batch_processing(self, review):
        missing_id = self.task.pk + 1000

        with self.assertRaisesMessage(
            CommandError,
            f"DevelopmentTask id={missing_id} was not found; no tasks were processed.",
        ):
            self.run_command(task_id=missing_id)

        review.assert_not_called()

    @SETTINGS
    @patch(
        "pool_service.management.commands.poll_development_codex."
        "close_old_connections"
    )
    @patch("pool_service.management.commands.poll_development_codex.run_review")
    def test_database_failure_is_isolated_from_the_next_task(
        self, review, close_connections
    ):
        second_task = DevelopmentTask.objects.create(
            organization=self.org,
            initiator=self.user,
            title="Second cycle",
            description="Process after a broken connection",
            status=DevelopmentTask.STATUS_REVIEW,
            current_stage=DevelopmentTask.STAGE_REVIEW,
            automation_metadata={"auto_cycle_enabled": True},
        )
        review.side_effect = [
            OperationalError(2006, "password=must-not-be-logged"),
            ReviewResult("accepted", changed=True, review_id=999),
        ]

        with self.assertLogs(
            "pool_service.management.commands.poll_development_codex",
            level="WARNING",
        ) as captured:
            output = self.run_command()

        logs = "\n".join(captured.output)
        self.assertEqual(review.call_count, 2)
        self.assertEqual(review.call_args_list[1].args, (second_task.pk,))
        self.assertIn("reviewed=1", output)
        self.assertIn("errors=1", output)
        self.assertIn("stage=run_review", logs)
        self.assertIn("error_type=OperationalError", logs)
        self.assertIn("db_error_code=2006", logs)
        self.assertNotIn("must-not-be-logged", logs)
        # One initial boundary plus before/after boundaries for both tasks.
        self.assertGreaterEqual(close_connections.call_count, 5)

    @SETTINGS
    @patch("pool_service.services.development_review._create_response")
    def test_completed_codex_is_reviewed_and_accepted(self, create):
        create.return_value = self.response("accepted")

        first = self.run_command()
        second = self.run_command()

        self.task.refresh_from_db()
        self.assertEqual(self.task.status, DevelopmentTask.STATUS_READY_FOR_DEPLOY)
        self.assertIn("reviewed=1", first)
        self.assertIn("errors=0", first)
        self.assertIn("reviewed=0", second)
        self.assertEqual(create.call_count, 1)
        self.assertEqual(
            Notification.objects.filter(
                dedupe_key=f"development-task:{self.task.pk}:ready-for-deploy"
            ).count(),
            1,
        )

    @SETTINGS
    @patch("pool_service.services.development_codex._find_matching_run")
    @patch("pool_service.services.development_codex._dispatch_workflow")
    @patch("pool_service.services.development_review._create_response")
    def test_corrective_review_dispatches_exactly_one_iteration(self, create, dispatch, runs):
        create.return_value = self.response(
            "corrective_required", instructions=["Fix trusted regression"]
        )
        runs.return_value = {"id": 12345, "status": "in_progress"}

        first = self.run_command()
        event_count = self.task.events.count()
        second = self.run_command()

        self.task.refresh_from_db()
        corrective = self.task.iterations.filter(
            executor_type=DevelopmentIteration.EXECUTOR_CODEX,
            automation_metadata__corrective_number=1,
        )
        self.assertEqual(corrective.count(), 1)
        self.assertEqual(self.task.status, DevelopmentTask.STATUS_CODEX_WORKING)
        self.assertIn("reviewed=1", first)
        self.assertIn("corrective=1", first)
        self.assertIn("corrective=0", second)
        self.assertEqual(dispatch.call_count, 1)
        self.assertEqual(self.task.events.count(), event_count)

    @SETTINGS
    @patch("pool_service.services.development_codex._dispatch_workflow")
    @patch("pool_service.services.development_review._create_response")
    def test_human_review_blocks_without_corrective_dispatch(self, create, dispatch):
        create.return_value = self.response(
            "human_required", human_reason="Ambiguous security requirement"
        )

        self.run_command()
        self.run_command()

        self.task.refresh_from_db()
        self.assertEqual(self.task.status, DevelopmentTask.STATUS_BLOCKED)
        self.assertFalse(
            self.task.iterations.filter(
                executor_type=DevelopmentIteration.EXECUTOR_CODEX,
                automation_metadata__corrective_number__gt=0,
            ).exists()
        )
        self.assertEqual(dispatch.call_count, 0)
        self.assertEqual(
            Notification.objects.filter(
                dedupe_key=f"development-task:{self.task.pk}:review-human:3"
            ).count(),
            1,
        )

    @SETTINGS
    def test_security_and_infrastructure_failures_stop_the_cycle(self):
        for state in ("security_blocked", "infrastructure_failed"):
            with self.subTest(state=state):
                metadata = dict(self.codex.automation_metadata)
                metadata.update({"state": state, "applied": True})
                self.codex.automation_metadata = metadata
                self.codex.save(update_fields=["automation_metadata"])
                self.task.status = DevelopmentTask.STATUS_BLOCKED
                self.task.save(update_fields=["status"])
                with patch(
                    "pool_service.management.commands.poll_development_codex.run_review"
                ) as review, patch(
                    "pool_service.management.commands.poll_development_codex.dispatch_corrective_codex"
                ) as dispatch:
                    output = self.run_command()
                self.assertIn("errors=0", output)
                review.assert_not_called()
                dispatch.assert_not_called()

    @SETTINGS
    @patch("pool_service.services.development_review._create_response")
    def test_pending_review_is_resumed_without_creating_a_duplicate(self, create):
        create.return_value = self.response("accepted", response_id="resp-recovered")
        operation_key = f"task:{self.task.pk}:codex:{self.codex.pk}:review"
        review = DevelopmentIteration.objects.create(
            task=self.task,
            iteration_number=3,
            executor_type=DevelopmentIteration.EXECUTOR_CHATGPT,
            status=DevelopmentIteration.STATUS_WORKING,
            prompt="stored review prompt",
            started_at=timezone.now(),
            automation_metadata={
                "purpose": "ai_review",
                "operation_key": operation_key,
                "state": "pending",
                "codex_iteration_id": self.codex.pk,
            },
        )

        self.run_command()
        self.run_command()

        self.task.refresh_from_db()
        review.refresh_from_db()
        self.assertEqual(self.task.status, DevelopmentTask.STATUS_READY_FOR_DEPLOY)
        self.assertEqual(review.automation_metadata["state"], "completed")
        self.assertEqual(
            self.task.iterations.filter(
                executor_type=DevelopmentIteration.EXECUTOR_CHATGPT,
                automation_metadata__operation_key=operation_key,
            ).count(),
            1,
        )
        self.assertEqual(create.call_count, 1)
        self.assertEqual(len(review.automation_metadata["ai_usage"]["calls"]), 1)
        self.assertEqual(
            Notification.objects.filter(
                dedupe_key=f"development-task:{self.task.pk}:ready-for-deploy"
            ).count(),
            1,
        )

    @SETTINGS
    @patch("pool_service.services.development_review._create_response")
    def test_response_ready_review_is_applied_after_crash(self, create):
        create.return_value = self.response("accepted", response_id="resp-stored")

        with patch(
            "pool_service.services.development_review._apply_stored_review",
            side_effect=RuntimeError("simulated process crash"),
        ):
            first = self.run_command()
        review = self.task.iterations.get(
            executor_type=DevelopmentIteration.EXECUTOR_CHATGPT
        )
        self.assertEqual(review.automation_metadata["state"], "response_ready")
        self.assertIn("errors=1", first)

        second = self.run_command()
        third = self.run_command()

        self.task.refresh_from_db()
        review.refresh_from_db()
        self.assertEqual(self.task.status, DevelopmentTask.STATUS_READY_FOR_DEPLOY)
        self.assertEqual(review.automation_metadata["state"], "completed")
        self.assertIn("reviewed=1", second)
        self.assertIn("reviewed=0", third)
        self.assertEqual(create.call_count, 1)
        self.assertEqual(len(review.automation_metadata["ai_usage"]["calls"]), 1)
        self.assertEqual(
            Notification.objects.filter(
                dedupe_key=f"development-task:{self.task.pk}:ready-for-deploy"
            ).count(),
            1,
        )

    @SETTINGS
    @patch("pool_service.services.development_review._create_response")
    def test_stale_launching_review_fails_closed_without_second_create(self, create):
        operation_key = f"task:{self.task.pk}:codex:{self.codex.pk}:review"
        review = DevelopmentIteration.objects.create(
            task=self.task,
            iteration_number=3,
            executor_type=DevelopmentIteration.EXECUTOR_CHATGPT,
            status=DevelopmentIteration.STATUS_WORKING,
            prompt="stored review prompt",
            started_at=timezone.now() - timedelta(minutes=5),
            automation_metadata={
                "purpose": "ai_review",
                "operation_key": operation_key,
                "state": "launching",
                "launch_token": "review-launch-token",
                "launch_started_at": (timezone.now() - timedelta(minutes=5)).isoformat(),
                "codex_iteration_id": self.codex.pk,
            },
        )

        self.run_command()
        event_count = self.task.events.filter(
            metadata__action="ai_review_launch_unknown"
        ).count()
        self.run_command()

        self.task.refresh_from_db()
        review.refresh_from_db()
        self.assertEqual(self.task.status, DevelopmentTask.STATUS_BLOCKED)
        self.assertEqual(review.automation_metadata["state"], "launch_unknown")
        self.assertEqual(create.call_count, 0)
        self.assertEqual(
            self.task.events.filter(
                metadata__action="ai_review_launch_unknown"
            ).count(),
            event_count,
        )
        self.assertEqual(event_count, 1)
        self.assertEqual(
            Notification.objects.filter(
                dedupe_key=(
                    f"development-task:{self.task.pk}:"
                    f"review-launch-unknown:{review.pk}"
                )
            ).count(),
            1,
        )

    def make_stale_corrective_dispatch(self):
        review = DevelopmentIteration.objects.create(
            task=self.task,
            iteration_number=3,
            executor_type=DevelopmentIteration.EXECUTOR_CHATGPT,
            status=DevelopmentIteration.STATUS_REVISION,
            automation_metadata={
                "purpose": "ai_review",
                "decision": "corrective_required",
                "corrective_instructions": ["Fix trusted regression"],
                "fingerprint": "unique-corrective-review",
                "codex_iteration_id": self.codex.pk,
                "applied": True,
                "state": "completed",
            },
        )
        corrective = DevelopmentIteration.objects.create(
            task=self.task,
            iteration_number=4,
            executor_type=DevelopmentIteration.EXECUTOR_CODEX,
            status=DevelopmentIteration.STATUS_WORKING,
            prompt="corrective prompt",
            started_at=timezone.now() - timedelta(minutes=5),
            automation_metadata={
                "purpose": "codex_execution",
                "provider": "github_actions",
                "state": "dispatching",
                "launch_token": "a" * 32,
                "branch_name": "codex/dev-1-aaaaaaaaaaaa",
                "launch_started_at": (timezone.now() - timedelta(minutes=5)).isoformat(),
                "effective_model": "gpt-5.6-luna",
                "corrective_number": 1,
                "corrective_review_id": review.pk,
                "previous_codex_iteration_id": self.codex.pk,
            },
        )
        metadata = dict(self.task.automation_metadata)
        metadata["active_codex_iteration_id"] = corrective.pk
        self.task.automation_metadata = metadata
        self.task.status = DevelopmentTask.STATUS_REVISION
        self.task.save(update_fields=["automation_metadata", "status"])
        return review, corrective

    @SETTINGS
    @patch("pool_service.services.development_codex._dispatch_workflow")
    @patch("pool_service.services.development_codex._find_matching_run")
    def test_stale_corrective_dispatch_reconciles_without_second_post(self, runs, dispatch):
        review, corrective = self.make_stale_corrective_dispatch()
        runs.return_value = {"id": 12345, "status": "in_progress"}

        first = self.run_command()
        event_count = self.task.events.filter(
            metadata__action="corrective_codex_dispatch_reconciled"
        ).count()
        second = self.run_command()

        self.task.refresh_from_db()
        corrective.refresh_from_db()
        self.assertIn("corrective=1", first)
        self.assertIn("corrective=0", second)
        self.assertEqual(self.task.status, DevelopmentTask.STATUS_CODEX_WORKING)
        self.assertEqual(corrective.automation_metadata["state"], "in_progress")
        self.assertEqual(corrective.automation_metadata["workflow_run_id"], 12345)
        self.assertTrue(corrective.automation_metadata["dispatch_reconciled"])
        self.assertEqual(dispatch.call_count, 0)
        self.assertEqual(
            self.task.events.filter(
                metadata__action="corrective_codex_dispatch_reconciled"
            ).count(),
            event_count,
        )
        self.assertEqual(event_count, 1)
        self.assertEqual(
            self.task.iterations.filter(
                automation_metadata__corrective_review_id=review.pk
            ).count(),
            1,
        )

    @SETTINGS
    @patch("pool_service.services.development_codex._dispatch_workflow")
    @patch("pool_service.services.development_codex._find_matching_run", return_value=None)
    def test_ambiguous_corrective_dispatch_blocks_without_retry(self, runs, dispatch):
        _review, corrective = self.make_stale_corrective_dispatch()

        self.run_command()
        event_count = self.task.events.filter(
            metadata__action="corrective_codex_dispatch_unknown"
        ).count()
        self.run_command()

        self.task.refresh_from_db()
        corrective.refresh_from_db()
        self.assertEqual(self.task.status, DevelopmentTask.STATUS_BLOCKED)
        self.assertEqual(corrective.automation_metadata["state"], "dispatch_unknown")
        self.assertEqual(dispatch.call_count, 0)
        self.assertEqual(
            self.task.events.filter(
                metadata__action="corrective_codex_dispatch_unknown"
            ).count(),
            event_count,
        )
        self.assertEqual(event_count, 1)
        self.assertEqual(
            Notification.objects.filter(
                dedupe_key=(
                    f"development-task:{self.task.pk}:"
                    f"corrective-dispatch-unknown:{corrective.pk}"
                )
            ).count(),
            1,
        )
