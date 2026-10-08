import json
from datetime import date, datetime, time, timedelta
from unittest.mock import patch
from uuid import uuid4
from zoneinfo import ZoneInfo

from django.contrib.auth.models import User
from django.core.exceptions import PermissionDenied, ValidationError
from django.test import Client as TestClient, TestCase
from django.urls import reverse
from django.utils import timezone

from pool_service.models import Organization, OrganizationAccess, ServiceTask, ServiceTaskChange
from pool_service.services.task_feedback import (
    FeedbackConflict, apply_feedback, history_for, state_for, version_for, waiting_control,
)
from pool_service.services.call_commitment_control import (
    _control_key, _deliver_control_push, _due_notification, _effective_deadline,
    process_call_commitment_controls,
)


class TaskFeedbackTests(TestCase):
    def setUp(self):
        self.org = Organization.objects.create(name="Feedback test organization")
        self.user = User.objects.create_user("feedback-manager", password="test")
        OrganizationAccess.objects.create(organization=self.org, user=self.user, role="manager")
        self.outsider = User.objects.create_user("feedback-outsider", password="test")
        other_org = Organization.objects.create(name="Other feedback organization")
        OrganizationAccess.objects.create(organization=other_org, user=self.outsider, role="owner")
        self.task = ServiceTask.objects.create(
            organization=self.org, title="Appointment", description="Original call agreement",
            start_date=date(2026, 10, 7), end_date=date(2026, 10, 7),
            start_time=time(11), end_time=time(11),
            due_at=datetime(2026, 10, 7, 11, tzinfo=ZoneInfo("Asia/Barnaul")),
            task_type=ServiceTask.TYPE_CRM_FOLLOWUP, source_type=ServiceTask.SOURCE_SYSTEM,
            status=ServiceTask.STATUS_NEW, primary_responsible=self.user,
            created_by=self.user, visibility=ServiceTask.VISIBILITY_PRIVATE,
            auto_created=True, is_editable=True,
            payload_json={"source": "call_analysis", "source_call_id": 700,
                          "actor": "employee", "operations_mcp_idempotency_key": "preserve-me"},
        )
        self.task.responsibles.add(self.user)

    def apply(self, action="comment", **overrides):
        self.task.refresh_from_db()
        args = {"task_id": self.task.id, "user": self.user, "action": action,
                "comment": "Client sent a message", "expected_version": version_for(self.task),
                "request_id": uuid4()}
        args.update(overrides)
        return apply_feedback(**args)

    def test_comment_does_not_change_status_deadline_or_original_description(self):
        before_due = self.task.due_at
        self.apply(comment="<script>alert(1)</script>")
        self.task.refresh_from_db()
        self.assertEqual(self.task.status, ServiceTask.STATUS_NEW)
        self.assertEqual(self.task.due_at, before_due)
        self.assertEqual(self.task.description, "Original call agreement")
        self.assertEqual(self.task.payload_json["operations_mcp_idempotency_key"], "preserve-me")
        self.assertEqual(history_for(self.task)[0]["comment"], "<script>alert(1)</script>")
        self.assertNotIn("control_revision", state_for(self.task))

    def test_wait_clears_appointment_and_keeps_separate_control(self):
        next_check = timezone.now() + timedelta(days=2)
        self.apply("wait", next_check_at=next_check)
        self.task.refresh_from_db()
        self.assertEqual(self.task.status, ServiceTask.STATUS_WAITING)
        self.assertIsNone(self.task.end_date)
        self.assertIsNone(self.task.due_at)
        self.assertIsNone(self.task.end_time)
        self.assertEqual(waiting_control(self.task), (True, next_check))
        self.assertEqual(_effective_deadline(self.task, self.task.payload_json), next_check)
        title, message = _due_notification(self.task, "employee")
        self.assertNotEqual(title, "Срок задачи наступил")
        self.assertIn("внутренней проверки", message)

    def test_wait_requires_future_check_and_preserves_original_on_error(self):
        for value in (None, timezone.now() - timedelta(days=1)):
            with self.subTest(value=value), self.assertRaises(ValidationError):
                self.apply("wait", next_check_at=value)
        self.task.refresh_from_db()
        self.assertEqual(self.task.status, ServiceTask.STATUS_NEW)
        self.assertFalse(self.task.changes.exists())

    def test_reschedule_updates_all_deadline_fields_and_clears_waiting(self):
        self.apply("wait", next_check_at=timezone.now() + timedelta(days=1))
        new_date = timezone.localdate() + timedelta(days=3)
        self.apply("reschedule", due_date=new_date, due_time=time(14, 30))
        self.task.refresh_from_db()
        self.assertEqual(self.task.start_date, new_date)
        self.assertEqual(self.task.end_date, new_date)
        self.assertEqual(self.task.end_time, time(14, 30))
        self.assertEqual(self.task.status, ServiceTask.STATUS_NEW)
        self.assertFalse(waiting_control(self.task)[0])
        self.assertEqual(self.task.due_at.astimezone(ZoneInfo("Asia/Barnaul")).hour, 14)
        self.assertIsNone(state_for(self.task)["next_check_at"])

    def test_date_only_reschedule_does_not_retain_old_time(self):
        new_date = timezone.localdate() + timedelta(days=3)
        self.apply("reschedule", due_date=new_date)
        self.task.refresh_from_db()
        self.assertIsNone(self.task.end_time)
        self.assertIsNone(self.task.due_at)
        self.assertEqual(_effective_deadline(self.task, self.task.payload_json).date(), new_date)

    def test_cancellation_is_not_completion_and_stops_control(self):
        self.apply("cancel", comment="Client cancelled the appointment")
        self.task.refresh_from_db()
        self.assertEqual(self.task.status, ServiceTask.STATUS_CANCELLED)
        self.assertIsNone(self.task.completed_at)
        self.assertEqual(process_call_commitment_controls()["checked"], 0)
        self.assertEqual(history_for(self.task)[0]["action"], "Отменена")

    def test_complete_uses_existing_completed_archive(self):
        self.apply("complete", comment="Appointment finished")
        self.task.refresh_from_db()
        self.assertTrue(self.task.completed_at)
        self.assertTrue(self.task.is_archived)
        self.assertEqual(self.task.archived_reason, ServiceTask.ARCHIVE_REASON_COMPLETED)
        self.assertEqual(process_call_commitment_controls()["checked"], 0)

    def test_cross_organization_write_is_denied(self):
        with self.assertRaises(PermissionDenied):
            self.apply(user=self.outsider)
        self.assertFalse(self.task.changes.exists())

    def test_unrelated_same_organization_user_is_denied(self):
        OrganizationAccess.objects.create(organization=self.org, user=self.outsider, role="manager")
        with self.assertRaises(PermissionDenied):
            self.apply(user=self.outsider)

    def test_revoked_membership_is_denied(self):
        OrganizationAccess.objects.filter(organization=self.org, user=self.user).delete()
        with self.assertRaises(PermissionDenied):
            self.apply()

    def test_stale_edit_is_rejected_without_losing_newer_state(self):
        old_version = version_for(self.task)
        self.apply("reschedule", due_date=timezone.localdate() + timedelta(days=3))
        with self.assertRaises(FeedbackConflict):
            self.apply("cancel", expected_version=old_version)
        self.task.refresh_from_db()
        self.assertEqual(self.task.status, ServiceTask.STATUS_NEW)
        self.assertEqual(self.task.changes.filter(field_name__startswith="feedback:").count(), 1)

    def test_exact_replay_has_no_duplicate_history(self):
        key, old_version = uuid4(), version_for(self.task)
        first = self.apply(request_id=key, expected_version=old_version)
        second = self.apply(request_id=key, expected_version=old_version)
        self.assertTrue(first[2])
        self.assertFalse(second[2])
        self.assertEqual(first[1].id, second[1].id)
        self.assertEqual(self.task.changes.filter(field_name__startswith="feedback:").count(), 1)

    def test_reusing_request_for_different_action_is_rejected(self):
        key = uuid4()
        self.apply(request_id=key)
        with self.assertRaises(FeedbackConflict):
            self.apply("cancel", request_id=key)

    def test_history_failure_rolls_back_entire_change(self):
        with patch.object(ServiceTaskChange.objects, "create", side_effect=RuntimeError("test audit failure")):
            with self.assertRaises(RuntimeError):
                self.apply("cancel")
        self.task.refresh_from_db()
        self.assertEqual(self.task.status, ServiceTask.STATUS_NEW)
        self.assertEqual(state_for(self.task), {})

    @patch("pool_service.services.call_commitment_control.send_push_to_users")
    def test_old_pending_control_push_does_not_send_after_wait(self, push):
        old_key = _control_key(self.task, _effective_deadline(self.task, self.task.payload_json))
        self.apply("wait", next_check_at=timezone.now() + timedelta(days=2))
        _deliver_control_push(self.task.id, old_key)
        push.assert_not_called()

    def test_control_resumes_only_at_internal_check(self):
        check = timezone.now() + timedelta(days=2)
        self.apply("wait", next_check_at=check)
        self.assertEqual(process_call_commitment_controls(now=check - timedelta(minutes=1))["due_reminders"], 0)
        self.assertEqual(process_call_commitment_controls(now=check)["due_reminders"], 1)
        self.assertEqual(process_call_commitment_controls(now=check + timedelta(minutes=1))["due_reminders"], 0)

    def test_a_comment_does_not_reset_existing_reminder_cycle(self):
        due = _effective_deadline(self.task, self.task.payload_json)
        process_call_commitment_controls(now=due)
        self.apply(comment="I have seen the reminder")
        self.assertEqual(process_call_commitment_controls(now=due + timedelta(minutes=1))["due_reminders"], 0)

    def test_operations_mcp_task_is_in_control_scope(self):
        self.task.source_type = ServiceTask.SOURCE_MANAGER
        self.task.payload_json = {"source": "operations_mcp"}
        self.task.save(update_fields=["source_type", "payload_json"])
        due = _effective_deadline(self.task, self.task.payload_json)
        self.assertEqual(process_call_commitment_controls(now=due)["due_reminders"], 1)

    def test_invalid_wait_marker_never_revives_original_deadline(self):
        self.apply("wait", next_check_at=timezone.now() + timedelta(days=1))
        self.task.refresh_from_db()
        self.task.payload_json["task_feedback"]["next_check_at"] = "broken"
        self.assertEqual(waiting_control(self.task), (True, None))
        self.assertIsNone(_effective_deadline(self.task, self.task.payload_json))

    @patch("pool_service.views._redirect_if_access_blocked", return_value=None)
    def test_feedback_page_escapes_comment_and_has_real_action_form(self, _blocked):
        self.apply(comment="<script>alert(1)</script>")
        self.client.force_login(self.user)
        response = self.client.get(reverse("task_feedback", args=[self.task.id]))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "&lt;script&gt;")
        self.assertNotContains(response, "<script>alert(1)</script>")
        self.assertContains(response, 'name="expected_version"')
        self.assertContains(response, 'name="request_id"')

    @patch("pool_service.views._redirect_if_access_blocked", return_value=None)
    def test_feedback_page_rejects_cross_organization_read(self, _blocked):
        self.client.force_login(self.outsider)
        response = self.client.get(reverse("task_feedback", args=[self.task.id]))
        self.assertEqual(response.status_code, 403)

    def test_unauthenticated_access_requires_login(self):
        self.assertEqual(self.client.get(reverse("task_feedback", args=[self.task.id])).status_code, 302)

    def test_post_requires_csrf(self):
        client = TestClient(enforce_csrf_checks=True)
        client.force_login(self.user)
        response = client.post(reverse("task_feedback", args=[self.task.id]), {"action": "cancel", "comment": "No token"})
        self.assertEqual(response.status_code, 403)
