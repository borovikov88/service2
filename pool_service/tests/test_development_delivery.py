from unittest.mock import patch

from django.contrib.auth.models import User
from django.test import TestCase, override_settings

from pool_service.models import DevelopmentIteration, DevelopmentTask, Organization
from pool_service.services.development_delivery import (
    DevelopmentDeliveryError,
    publish_approval_and_enable_auto_merge,
)


HEAD = "b" * 40
DELIVERY_SETTINGS = override_settings(
    GITHUB_DEVELOPMENT_REPOSITORY="owner/repo",
    GITHUB_DEVELOPMENT_REVIEW_TOKEN="review-token",
    GITHUB_DEVELOPMENT_REVIEW_LOGIN="service2-reviewer",
    GITHUB_DEVELOPMENT_AUTO_MERGE_ENABLED=True,
)


class DevelopmentDeliveryTests(TestCase):
    def setUp(self):
        owner = User.objects.create_user("owner")
        organization = Organization.objects.create(name="Org")
        self.task = DevelopmentTask.objects.create(
            organization=organization,
            initiator=owner,
            title="Automated task",
            description="Implement",
            status=DevelopmentTask.STATUS_READY_FOR_DEPLOY,
            automation_metadata={"auto_cycle_enabled": True},
        )
        self.review = DevelopmentIteration.objects.create(
            task=self.task,
            iteration_number=1,
            executor_type=DevelopmentIteration.EXECUTOR_CHATGPT,
            status=DevelopmentIteration.STATUS_ACCEPTED,
            automation_metadata={
                "purpose": "ai_review",
                "decision": "accepted",
                "applied": True,
                "pr_number": 86,
                "head_sha": HEAD,
                "evidence_snapshot": {"head_ref": "codex/dev-86-abcdef123456"},
            },
        )

    @DELIVERY_SETTINGS
    @patch("pool_service.services.development_delivery._request")
    def test_publishes_exact_head_approval_then_enables_auto_merge(self, request):
        request.side_effect = [
            {"login": "service2-reviewer"},
            {"number": 86, "state": "open", "node_id": "PR_node", "user": {"login": "github-actions[bot]"}, "base": {"ref": "main", "repo": {"full_name": "owner/repo"}}, "head": {"sha": HEAD, "ref": "codex/dev-86-abcdef123456", "repo": {"full_name": "owner/repo"}}},
            [],
            {"id": 1},
            {"number": 86, "state": "open", "node_id": "PR_node", "user": {"login": "github-actions[bot]"}, "base": {"ref": "main", "repo": {"full_name": "owner/repo"}}, "head": {"sha": HEAD, "ref": "codex/dev-86-abcdef123456", "repo": {"full_name": "owner/repo"}}},
            {"data": {"enablePullRequestAutoMerge": {"pullRequest": {"id": "PR_node", "autoMergeRequest": {"enabledAt": "now"}}}}},
        ]
        result = publish_approval_and_enable_auto_merge(self.task.pk)
        self.assertEqual(result.state, "auto_merge_enabled")
        self.assertTrue(result.changed)
        approval = request.call_args_list[3]
        self.assertEqual(approval.args[0], "POST")
        self.assertEqual(approval.kwargs["payload"]["commit_id"], HEAD)
        self.task.refresh_from_db()
        self.assertEqual(self.task.automation_metadata["github_delivery"]["head_sha"], HEAD)

    @DELIVERY_SETTINGS
    @patch("pool_service.services.development_delivery._request")
    def test_refuses_reviewer_that_is_pull_request_author(self, request):
        request.side_effect = [
            {"login": "service2-reviewer"},
            {"number": 86, "state": "open", "node_id": "PR_node", "user": {"login": "service2-reviewer"}, "base": {"ref": "main", "repo": {"full_name": "owner/repo"}}, "head": {"sha": HEAD, "ref": "codex/dev-86-abcdef123456", "repo": {"full_name": "owner/repo"}}},
        ]
        with self.assertRaisesRegex(DevelopmentDeliveryError, "must differ"):
            publish_approval_and_enable_auto_merge(self.task.pk)
        self.assertEqual(request.call_count, 2)

    @override_settings(
        GITHUB_DEVELOPMENT_AUTO_MERGE_ENABLED=False,
        GITHUB_DEVELOPMENT_REVIEW_TOKEN="",
        GITHUB_DEVELOPMENT_REVIEW_LOGIN="",
    )
    @patch("pool_service.services.development_delivery._request")
    def test_disabled_is_fail_closed_without_github_call(self, request):
        result = publish_approval_and_enable_auto_merge(self.task.pk)
        self.assertEqual(result.state, "disabled")
        request.assert_not_called()

    @DELIVERY_SETTINGS
    @patch("pool_service.services.development_delivery._request")
    def test_refuses_changed_head_before_posting_review(self, request):
        request.side_effect = [
            {"login": "service2-reviewer"},
            {"number": 86, "state": "open", "node_id": "PR_node", "user": {"login": "github-actions[bot]"}, "base": {"ref": "main", "repo": {"full_name": "owner/repo"}}, "head": {"sha": "c" * 40, "ref": "codex/dev-86-abcdef123456", "repo": {"full_name": "owner/repo"}}},
        ]
        with self.assertRaisesRegex(DevelopmentDeliveryError, "head changed"):
            publish_approval_and_enable_auto_merge(self.task.pk)
        self.assertEqual(request.call_count, 2)

    @DELIVERY_SETTINGS
    @patch("pool_service.services.development_delivery._request")
    def test_refuses_retarget_after_approval_before_auto_merge(self, request):
        valid = {"number": 86, "state": "open", "node_id": "PR_node", "user": {"login": "github-actions[bot]"}, "base": {"ref": "main", "repo": {"full_name": "owner/repo"}}, "head": {"sha": HEAD, "ref": "codex/dev-86-abcdef123456", "repo": {"full_name": "owner/repo"}}}
        retargeted = {**valid, "base": {"ref": "release", "repo": {"full_name": "owner/repo"}}}
        request.side_effect = [
            {"login": "service2-reviewer"}, valid, [], {"id": 1}, retargeted,
        ]
        with self.assertRaisesRegex(DevelopmentDeliveryError, "changed before auto-merge"):
            publish_approval_and_enable_auto_merge(self.task.pk)
        self.assertEqual(request.call_count, 5)

    @DELIVERY_SETTINGS
    @patch("pool_service.services.development_delivery._request")
    def test_rejects_incomplete_auto_merge_response(self, request):
        valid = {"number": 86, "state": "open", "node_id": "PR_node", "user": {"login": "github-actions[bot]"}, "base": {"ref": "main", "repo": {"full_name": "owner/repo"}}, "head": {"sha": HEAD, "ref": "codex/dev-86-abcdef123456", "repo": {"full_name": "owner/repo"}}}
        request.side_effect = [
            {"login": "service2-reviewer"}, valid, [], {"id": 1}, valid, {"data": {}},
        ]
        with self.assertRaisesRegex(DevelopmentDeliveryError, "rejected auto-merge"):
            publish_approval_and_enable_auto_merge(self.task.pk)
        self.task.refresh_from_db()
        self.assertNotIn("github_delivery", self.task.automation_metadata)
