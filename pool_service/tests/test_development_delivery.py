import io
from types import SimpleNamespace
from unittest.mock import patch

from django.contrib.auth.models import User
from django.core.management import call_command
from django.test import TestCase, override_settings

from pool_service.models import DevelopmentIteration, DevelopmentTask, DevelopmentTaskEvent, Organization
from pool_service.services.development_delivery import (
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
    def test_saved_accepted_review_never_publishes_approval_or_auto_merge(self, request):
        metadata = dict(self.task.automation_metadata)
        event_count = DevelopmentTaskEvent.objects.filter(task=self.task).count()

        result = publish_approval_and_enable_auto_merge(self.task.pk)

        self.assertEqual(result.state, "retired")
        self.assertFalse(result.changed)
        request.assert_not_called()
        self.task.refresh_from_db()
        self.assertEqual(self.task.automation_metadata, metadata)
        self.assertEqual(self.task.status, DevelopmentTask.STATUS_READY_FOR_DEPLOY)
        self.assertEqual(DevelopmentTaskEvent.objects.filter(task=self.task).count(), event_count)

    @DELIVERY_SETTINGS
    @patch("pool_service.services.development_delivery._request")
    def test_previously_delivered_metadata_does_not_return_success(self, request):
        self.task.automation_metadata["github_delivery"] = {
            "head_sha": HEAD, "state": "auto_merge_enabled", "pr_number": 86,
        }
        self.task.save(update_fields=["automation_metadata"])
        before = dict(self.task.automation_metadata)

        result = publish_approval_and_enable_auto_merge(self.task.pk)

        self.assertEqual(result.state, "retired")
        self.assertFalse(result.changed)
        request.assert_not_called()
        self.task.refresh_from_db()
        self.assertEqual(self.task.automation_metadata, before)

    @override_settings(
        GITHUB_DEVELOPMENT_AUTO_MERGE_ENABLED=False,
        GITHUB_DEVELOPMENT_REVIEW_TOKEN="",
        GITHUB_DEVELOPMENT_REVIEW_LOGIN="",
    )
    @patch("pool_service.services.development_delivery._request")
    def test_retirement_is_unconditional_even_with_old_feature_disabled(self, request):
        result = publish_approval_and_enable_auto_merge(self.task.pk)
        self.assertEqual(result.state, "retired")
        self.assertFalse(result.changed)
        request.assert_not_called()

    @DELIVERY_SETTINGS
    @patch("pool_service.services.development_delivery._request")
    @patch("pool_service.services.development_delivery.DevelopmentTask.objects.get")
    def test_retirement_happens_before_task_lookup_or_network(self, get, request):
        result = publish_approval_and_enable_auto_merge(999999)
        self.assertEqual(result.state, "retired")
        self.assertFalse(result.changed)
        get.assert_not_called()
        request.assert_not_called()

    @DELIVERY_SETTINGS
    @patch("pool_service.services.development_delivery._request")
    def test_repeated_delivery_calls_do_not_replay_old_acceptance(self, request):
        for _ in range(2):
            result = publish_approval_and_enable_auto_merge(self.task.pk)
            self.assertEqual(result.state, "retired")
            self.assertFalse(result.changed)
        request.assert_not_called()
        self.task.refresh_from_db()
        self.assertNotIn("github_delivery", self.task.automation_metadata)
        self.assertFalse(self.task.events.filter(metadata__action="github_auto_merge_enabled").exists())

    @DELIVERY_SETTINGS
    @patch("pool_service.services.development_delivery._request")
    @patch("pool_service.management.commands.poll_development_codex.review_updated_accepted_pull_request")
    def test_poller_replay_of_accepted_review_makes_no_github_write(self, recheck, request):
        recheck.return_value = SimpleNamespace(changed=False, review_id=self.review.pk)
        metadata = dict(self.task.automation_metadata)
        event_count = self.task.events.count()
        for _ in range(2):
            output = io.StringIO()
            call_command("poll_development_codex", task_id=self.task.pk, stdout=output)
            self.assertIn("delivered=0", output.getvalue())
            self.assertIn("errors=0", output.getvalue())
        request.assert_not_called()
        self.assertEqual(recheck.call_count, 2)
        self.task.refresh_from_db()
        self.assertEqual(self.task.automation_metadata, metadata)
        self.assertEqual(self.task.events.count(), event_count)

    @DELIVERY_SETTINGS
    @patch("pool_service.services.development_delivery._request")
    @patch("pool_service.management.commands.poll_development_codex.run_review")
    def test_poller_newly_accepted_path_also_cannot_publish(self, run_review, request):
        self.task.status = DevelopmentTask.STATUS_REVIEW
        self.task.save(update_fields=["status"])

        def accept_without_provider(_task_id):
            self.task.status = DevelopmentTask.STATUS_READY_FOR_DEPLOY
            self.task.save(update_fields=["status"])
            return SimpleNamespace(changed=True, review_id=self.review.pk)

        run_review.side_effect = accept_without_provider
        output = io.StringIO()
        call_command("poll_development_codex", task_id=self.task.pk, stdout=output)

        self.assertIn("reviewed=1", output.getvalue())
        self.assertIn("delivered=0", output.getvalue())
        self.assertIn("errors=0", output.getvalue())
        request.assert_not_called()
        self.task.refresh_from_db()
        self.assertNotIn("github_delivery", self.task.automation_metadata)
        self.assertFalse(self.task.events.filter(metadata__action="github_auto_merge_enabled").exists())
