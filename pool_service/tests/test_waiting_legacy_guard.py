import json
from datetime import timedelta
from unittest.mock import patch
from uuid import uuid4

from django.contrib.auth.models import User
from django.http import HttpResponse
from django.test import Client, TestCase
from django.urls import resolve, reverse
from django.utils import timezone

from pool_service.models import Organization, OrganizationAccess, ServiceTask
from pool_service.services.task_feedback import apply_feedback, state_for, version_for
from pool_service.task_waiting_guards import guarded_task_edit, guarded_task_move
from pool_service.services.task_waiting_schedule import CONTROL_LABEL


class WaitingLegacyGuardTests(TestCase):
    def setUp(self):
        self.org = Organization.objects.create(name="Legacy waiting guard test")
        self.user = User.objects.create_user("waiting-guard-owner")
        OrganizationAccess.objects.create(user=self.user, organization=self.org, role="owner")
        self.task = ServiceTask.objects.create(
            organization=self.org, title="Meeting", start_date=timezone.localdate(),
            end_date=timezone.localdate(), task_type=ServiceTask.TYPE_CRM_FOLLOWUP,
            source_type=ServiceTask.SOURCE_MANAGER, status=ServiceTask.STATUS_NEW,
            created_by=self.user, primary_responsible=self.user,
        )
        self.task.responsibles.add(self.user)
        self.client.force_login(self.user)

    def wait(self):
        apply_feedback(task_id=self.task.pk, user=self.user, action="wait", comment="No confirmed date",
                       expected_version=version_for(self.task), request_id=uuid4(),
                       next_check_at=timezone.now() + timedelta(days=2))
        self.task.refresh_from_db()

    def snapshot(self):
        return ServiceTask.objects.filter(pk=self.task.pk).values().get(), self.task.changes.count()

    def test_existing_named_paths_resolve_to_guards(self):
        self.assertIs(resolve(reverse("task_edit", args=[self.task.pk])).func, guarded_task_edit)
        self.assertIs(resolve(reverse("task_move")).func, guarded_task_move)

    def test_stale_generic_form_cannot_restore_a_waiting_appointment(self):
        self.wait()
        before = self.snapshot()
        response = self.client.post(reverse("task_edit", args=[self.task.pk]), {
            "title": "Old form", "start_date": timezone.localdate().isoformat(),
            "end_date": timezone.localdate().isoformat(),
        })
        self.assertEqual(response.status_code, 409)
        self.assertContains(response, reverse("task_feedback", args=[self.task.pk]), status_code=409)
        self.assertEqual(self.snapshot(), before)

    def test_drag_cannot_reinterpret_internal_check_as_appointment(self):
        self.wait()
        before = self.snapshot()
        response = self.client.post(reverse("task_move"), data=json.dumps({
            "task_id": self.task.pk, "target_date": (timezone.localdate() + timedelta(days=5)).isoformat(),
        }), content_type="application/json")
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["error"], "waiting_task_use_feedback")
        self.assertEqual(self.snapshot(), before)

    def test_other_organization_cannot_learn_waiting_details(self):
        self.wait()
        outsider = User.objects.create_user("waiting-guard-outsider")
        org = Organization.objects.create(name="Other guard organization")
        OrganizationAccess.objects.create(user=outsider, organization=org, role="owner")
        self.client.force_login(outsider)
        response = self.client.post(reverse("task_edit", args=[self.task.pk]), {"title": "Other"})
        self.assertEqual(response.status_code, 403)
        self.assertNotContains(response, "feedback_url", status_code=403)

    def test_nonwaiting_edit_still_delegates_to_existing_view(self):
        with patch("pool_service.views.task_edit", return_value=HttpResponse("legacy edit")) as original:
            response = self.client.post(reverse("task_edit", args=[self.task.pk]), {"title": "Normal"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, b"legacy edit")
        original.assert_called_once()

    def test_nonwaiting_move_still_delegates_to_existing_view(self):
        with patch("pool_service.views.task_move", return_value=HttpResponse("legacy move")) as original:
            response = self.client.post(reverse("task_move"), data=json.dumps({
                "task_id": self.task.pk, "target_date": timezone.localdate().isoformat(),
            }), content_type="application/json")
        self.assertEqual(response.status_code, 200)
        original.assert_called_once()

    def test_guard_preserves_csrf_requirement(self):
        self.wait()
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.user)
        response = client.post(reverse("task_edit", args=[self.task.pk]), {"title": "No csrf token"})
        self.assertEqual(response.status_code, 403)

    @patch("pool_service.views._redirect_if_access_blocked", return_value=None)
    def test_bulk_active_status_cannot_turn_internal_check_into_appointment(self, _blocked):
        self.wait()
        response = self.client.post(reverse("crm_tasks_bulk_update"), {
            "task_ids": [self.task.pk],
            "bulk_action": "set_status",
            "bulk_status": ServiceTask.STATUS_IN_PROGRESS,
        })
        self.assertEqual(response.status_code, 302)
        self.task.refresh_from_db()
        self.assertEqual(self.task.status, ServiceTask.STATUS_WAITING)
        self.assertTrue(self.task.title.startswith(CONTROL_LABEL))
        self.assertEqual(state_for(self.task)["mode"], "waiting")

    @patch("pool_service.views._redirect_if_access_blocked", return_value=None)
    def test_bulk_cancel_releases_waiting_metadata(self, _blocked):
        self.wait()
        response = self.client.post(reverse("crm_tasks_bulk_update"), {
            "task_ids": [self.task.pk],
            "bulk_action": "set_status",
            "bulk_status": ServiceTask.STATUS_CANCELLED,
        })
        self.assertEqual(response.status_code, 302)
        self.task.refresh_from_db()
        self.assertEqual(self.task.status, ServiceTask.STATUS_CANCELLED)
        self.assertEqual(self.task.title, "Meeting")
        self.assertEqual(state_for(self.task)["mode"], "cancel")
        self.assertIsNone(state_for(self.task)["next_check_at"])

    @patch("pool_service.views._redirect_if_access_blocked", return_value=None)
    def test_bulk_archive_waiting_restores_only_as_cancelled_not_appointment(self, _blocked):
        self.wait()
        response = self.client.post(reverse("crm_tasks_bulk_update"), {
            "task_ids": [self.task.pk],
            "bulk_action": "archive",
        })
        self.assertEqual(response.status_code, 302)
        self.task.refresh_from_db()
        self.assertTrue(self.task.is_archived)
        self.assertEqual(self.task.status, ServiceTask.STATUS_CANCELLED)
        self.assertEqual(self.task.title, "Meeting")
        self.assertEqual(state_for(self.task)["mode"], "cancel")

        response = self.client.post(reverse("archive_restore_task", args=[self.task.pk]))
        self.assertEqual(response.status_code, 302)
        self.task.refresh_from_db()
        self.assertFalse(self.task.is_archived)
        self.assertEqual(self.task.status, ServiceTask.STATUS_CANCELLED)
