import json
from io import StringIO
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import patch

from django.contrib.auth.models import User
from django.core.management import call_command
from django.test import TestCase, override_settings
from django.utils import timezone

from pool_service.models import DevelopmentIteration, DevelopmentTask, Organization
from pool_service.services.development_codex import (
    GitHubRequestError,
    PullRequestEvidence,
    load_pull_request_evidence,
)
from pool_service.services.development_review import (
    review_updated_accepted_pull_request,
    run_review,
)


SETTINGS = override_settings(
    OPENAI_API_KEY="test-key",
    OPENAI_DEVELOPMENT_MODEL="gpt-5.6-luna",
    DEVELOPMENT_MAX_CORRECTIVE_ITERATIONS=1,
    GITHUB_DEVELOPMENT_TOKEN="token",
    GITHUB_DEVELOPMENT_REPOSITORY="owner/repo",
    GITHUB_DEVELOPMENT_WORKFLOW="development-codex.yml",
)
BRANCH = "codex/dev-16-abcdef123456"
BASE_SHA = "a" * 40
HEAD_A = "b" * 40
HEAD_B = "c" * 40


def evidence(head_sha=HEAD_A, *, patch_text="@@ -1 +1 @@\n-old\n+new", sufficient=True):
    reasons = [] if sufficient else ["missing_patch"]
    body = {
        "repository": "owner/repo",
        "pr_number": 16,
        "state": "open",
        "base_ref": "main",
        "base_sha": BASE_SHA,
        "head_ref": BRANCH,
        "head_sha": head_sha,
        "head_repository": "owner/repo",
        "changed_files": [{"filename": "pool_service/example.py", "patch": patch_text}],
        "truncated": not sufficient,
        "truncation_reason": reasons,
        "included_file_count": 1,
        "total_file_count": 1,
        "included_bytes": len(patch_text.encode()),
        "sufficient_for_automatic_acceptance": sufficient,
        "evidence_sha256": "d" * 64,
        "fetched_at": "2026-08-15T00:00:00+00:00",
    }
    return PullRequestEvidence(body)


class GitHubEvidenceLoaderTests(TestCase):
    def pull(self, **changes):
        value = {
            "number": 16,
            "state": "open",
            "changed_files": 1,
            "base": {"ref": "main", "sha": BASE_SHA, "repo": {"full_name": "owner/repo"}},
            "head": {"ref": BRANCH, "sha": HEAD_A, "repo": {"full_name": "owner/repo"}},
        }
        value.update(changes)
        return value

    def files(self):
        return [{
            "filename": "pool_service/example.py", "status": "modified",
            "additions": 1, "deletions": 1, "changes": 2,
            "patch": "@@ -1 +1 @@\n-old\n+new",
        }]

    @SETTINGS
    @patch("pool_service.services.development_codex._github_request")
    def test_loads_actual_patch_and_validated_identity(self, request):
        request.side_effect = [self.pull(), self.files()]
        result = load_pull_request_evidence(16, BRANCH)
        self.assertEqual(result.head_sha, HEAD_A)
        self.assertIn("+new", result.snapshot["changed_files"][0]["patch"])
        self.assertTrue(result.sufficient)
        self.assertEqual(len(result.snapshot["evidence_sha256"]), 64)

    @SETTINGS
    @patch("pool_service.services.development_codex._github_request")
    def test_rejects_foreign_repository_wrong_base_and_wrong_head(self, request):
        cases = [
            (self.pull(head={"ref": BRANCH, "sha": HEAD_A, "repo": {"full_name": "other/repo"}}), "HeadRepositoryMismatch"),
            (self.pull(base={"ref": "develop", "sha": BASE_SHA, "repo": {"full_name": "owner/repo"}}), "BaseRefMismatch"),
            (self.pull(head={"ref": "codex/dev-99-abcdef123456", "sha": HEAD_A, "repo": {"full_name": "owner/repo"}}), "HeadRefMismatch"),
        ]
        for pull, cause in cases:
            with self.subTest(cause=cause):
                request.reset_mock(side_effect=True)
                request.return_value = pull
                with self.assertRaisesRegex(GitHubRequestError, cause):
                    load_pull_request_evidence(16, BRANCH)

    @SETTINGS
    @patch("pool_service.services.development_codex.MAX_REVIEW_EVIDENCE_BYTES", 8)
    @patch("pool_service.services.development_codex._github_request")
    def test_large_patch_is_bounded_and_marked_truncated(self, request):
        request.side_effect = [self.pull(), self.files()]
        result = load_pull_request_evidence(16, BRANCH)
        self.assertLessEqual(result.snapshot["included_bytes"], 8)
        self.assertTrue(result.snapshot["truncated"])
        self.assertIn("byte_limit", result.snapshot["truncation_reason"])
        self.assertFalse(result.sufficient)

    @SETTINGS
    @patch("pool_service.services.development_codex._github_request")
    def test_changed_files_are_loaded_with_bounded_pagination(self, request):
        pull = self.pull(changed_files=101)
        first_page = [
            {
                "filename": f"file-{number}.py", "status": "modified",
                "additions": 1, "deletions": 0, "changes": 1, "patch": "+x",
            }
            for number in range(100)
        ]
        second_page = [{
            "filename": "file-100.py", "status": "modified",
            "additions": 1, "deletions": 0, "changes": 1, "patch": "+x",
        }]
        request.side_effect = [pull, first_page, second_page]
        result = load_pull_request_evidence(16, BRANCH)
        self.assertEqual(result.snapshot["included_file_count"], 101)
        self.assertFalse(result.snapshot["truncated"])
        self.assertEqual(request.call_count, 3)


@SETTINGS
class GitHubEvidenceReviewTests(TestCase):
    def setUp(self):
        org = Organization.objects.create(
            name="Evidence", paid_until=timezone.now() + timedelta(days=3)
        )
        user = User.objects.create_user("evidence-owner")
        self.task = DevelopmentTask.objects.create(
            organization=org,
            initiator=user,
            title="Review evidence",
            description="Use GitHub PR",
            definition_of_done="Review actual patch",
            status=DevelopmentTask.STATUS_REVIEW,
            current_stage=DevelopmentTask.STAGE_REVIEW,
            automation_metadata={"auto_cycle_enabled": True, "effective_model": "gpt-5.6-luna"},
        )
        DevelopmentIteration.objects.create(
            task=self.task, iteration_number=1, executor_type="system",
            status="accepted", response="Analysis",
            automation_metadata={"purpose": "primary_analysis"},
        )
        self.codex = DevelopmentIteration.objects.create(
            task=self.task, iteration_number=2, executor_type="codex",
            status="accepted", result_summary="Summary only", response="PR body",
            automation_metadata={
                "purpose": "codex_execution", "state": "completed", "applied": True,
                "pr_number": 16, "branch_name": BRANCH, "corrective_number": 0,
            },
        )
        metadata = dict(self.task.automation_metadata)
        metadata["active_codex_iteration_id"] = self.codex.pk
        self.task.automation_metadata = metadata
        self.task.save(update_fields=["automation_metadata"])

    def response(self, decision="accepted"):
        body = {
            "decision": decision,
            "summary": "reviewed",
            "findings": [] if decision == "accepted" else ["defect"],
            "corrective_instructions": ["fix defect"] if decision == "corrective_required" else [],
            "human_reason": None,
        }
        return SimpleNamespace(
            id="resp", model="gpt-5.6-luna", output_text=json.dumps(body), usage=None
        )

    @patch("pool_service.services.development_review._create_response")
    @patch("pool_service.services.development_review.load_pull_request_evidence")
    def test_actual_patch_is_in_payload_without_local_workspace(self, loader, create):
        loader.return_value = evidence(patch_text="@@ patch from github +actual")
        create.return_value = self.response()
        result = run_review(self.task.pk)
        self.assertEqual(result.state, "accepted")
        payload = json.loads(create.call_args.args[0])
        self.assertIn("+actual", payload["github_pr_evidence"]["changed_files"][0]["patch"])
        review = self.task.iterations.get(automation_metadata__purpose="ai_review")
        self.assertEqual(
            review.automation_metadata["operation_key"],
            f"task:{self.task.pk}:pr:16:head:{HEAD_A}:review",
        )

    @patch("pool_service.services.development_review._create_response")
    @patch("pool_service.services.development_review.load_pull_request_evidence")
    def test_same_head_is_idempotent_and_new_head_is_reviewed(self, loader, create):
        loader.return_value = evidence(HEAD_A)
        create.return_value = self.response()
        run_review(self.task.pk)
        self.task.refresh_from_db()
        self.assertEqual(self.task.status, DevelopmentTask.STATUS_READY_FOR_DEPLOY)

        unchanged = review_updated_accepted_pull_request(self.task.pk)
        self.assertFalse(unchanged.changed)
        self.assertEqual(create.call_count, 1)

        loader.return_value = evidence(HEAD_B, patch_text="@@ updated head B")
        updated = review_updated_accepted_pull_request(self.task.pk)
        self.assertTrue(updated.changed)
        self.assertEqual(create.call_count, 2)
        heads = set(
            self.task.iterations.filter(automation_metadata__purpose="ai_review")
            .values_list("automation_metadata__head_sha", flat=True)
        )
        self.assertEqual(heads, {HEAD_A, HEAD_B})

    @patch("pool_service.services.development_review._create_response")
    @patch("pool_service.services.development_review.load_pull_request_evidence")
    def test_evidence_failures_are_retryable_and_do_not_consume_corrective_limit(self, loader, create):
        for error in (
            GitHubRequestError(category="transport", cause_type="TimeoutError"),
            GitHubRequestError(category="http", status_code=403, cause_type="HTTPError"),
            GitHubRequestError(category="http", status_code=404, cause_type="HTTPError"),
            GitHubRequestError(category="evidence", cause_type="MalformedPullRequest"),
        ):
            with self.subTest(error=str(error)):
                loader.side_effect = error
                result = run_review(self.task.pk)
                self.assertEqual(result.state, "evidence_failed")
                self.assertEqual(create.call_count, 0)
                self.assertFalse(
                    self.task.iterations.filter(automation_metadata__corrective_number__gt=0).exists()
                )
        loader.side_effect = None
        loader.return_value = evidence()
        create.return_value = self.response()
        self.assertEqual(run_review(self.task.pk).state, "accepted")

    @patch("pool_service.services.development_review._create_response")
    @patch("pool_service.services.development_review.load_pull_request_evidence")
    def test_insufficient_evidence_cannot_be_automatically_accepted(self, loader, create):
        loader.return_value = evidence(sufficient=False)
        create.return_value = self.response("accepted")
        result = run_review(self.task.pk)
        self.assertEqual(result.state, "human_required")
        self.task.refresh_from_db()
        self.assertEqual(self.task.status, DevelopmentTask.STATUS_BLOCKED)
        prompt = json.loads(create.call_args.args[0])
        self.assertIn("Automatic acceptance is forbidden", prompt["evidence_notice"])

    @patch("pool_service.services.development_review._create_response")
    @patch("pool_service.services.development_review.load_pull_request_evidence")
    def test_changed_accepted_pr_requires_auto_cycle_opt_in(self, loader, create):
        loader.return_value = evidence()
        create.return_value = self.response()
        run_review(self.task.pk)
        metadata = dict(self.task.automation_metadata)
        metadata["auto_cycle_enabled"] = False
        self.task.automation_metadata = metadata
        self.task.save(update_fields=["automation_metadata"])
        loader.return_value = evidence(HEAD_B)
        result = review_updated_accepted_pull_request(self.task.pk)
        self.assertEqual(result.state, "not_available")
        self.assertEqual(create.call_count, 1)

    @patch(
        "pool_service.management.commands.poll_development_codex."
        "review_updated_accepted_pull_request"
    )
    def test_poller_uses_narrow_helper_for_auto_cycle_ready_task(self, review_updated):
        from pool_service.services.development_review import ReviewResult

        self.task.status = DevelopmentTask.STATUS_READY_FOR_DEPLOY
        self.task.save(update_fields=["status"])
        review_updated.return_value = ReviewResult("accepted")
        call_command("poll_development_codex", stdout=StringIO())
        review_updated.assert_called_once_with(self.task.pk)

    @patch(
        "pool_service.management.commands.poll_development_codex."
        "review_updated_accepted_pull_request"
    )
    def test_poller_does_not_select_ready_task_without_auto_cycle(self, review_updated):
        self.task.status = DevelopmentTask.STATUS_READY_FOR_DEPLOY
        metadata = dict(self.task.automation_metadata)
        metadata["auto_cycle_enabled"] = False
        self.task.automation_metadata = metadata
        self.task.save(update_fields=["status", "automation_metadata"])
        call_command("poll_development_codex", stdout=StringIO())
        review_updated.assert_not_called()
