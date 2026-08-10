import json
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import patch

from django.contrib.auth.models import User
from django.test import TestCase, override_settings
from django.utils import timezone

from pool_service.models import DevelopmentIteration, DevelopmentTask, Notification, Organization, OrganizationAccess
from pool_service.services.development_review import run_review
from pool_service.services.development_codex import dispatch_corrective_codex


SETTINGS = override_settings(
    OPENAI_API_KEY="test-key", OPENAI_DEVELOPMENT_MODEL="gpt-5.6-luna",
    DEVELOPMENT_MAX_CORRECTIVE_ITERATIONS=3,
    GITHUB_DEVELOPMENT_TOKEN="token", GITHUB_DEVELOPMENT_REPOSITORY="owner/repo",
    GITHUB_DEVELOPMENT_WORKFLOW="development-codex.yml",
    GITHUB_DEVELOPMENT_PROMPT_MAX_BYTES=40000,
)


class DevelopmentAutoCycleTests(TestCase):
    def setUp(self):
        self.org = Organization.objects.create(name="Auto", paid_until=timezone.now() + timedelta(days=3))
        self.user = User.objects.create_user("auto-owner")
        OrganizationAccess.objects.create(organization=self.org, user=self.user, role="owner")
        self.task = DevelopmentTask.objects.create(
            organization=self.org, initiator=self.user, title="Cycle", description="Implement",
            business_goal="Automate", definition_of_done="Tests pass",
            status=DevelopmentTask.STATUS_REVIEW, current_stage=DevelopmentTask.STAGE_REVIEW,
            automation_metadata={"effective_model": "gpt-5.6-luna"},
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
        first = dispatch_corrective_codex(self.task.pk, review_id)
        second = dispatch_corrective_codex(self.task.pk, review_id)
        self.assertTrue(first.changed)
        self.assertFalse(second.changed)
        self.assertEqual(dispatch.call_count, 1)
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
