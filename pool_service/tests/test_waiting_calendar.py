"""Waiting removes an obsolete appointment from all active read surfaces."""
import json
from datetime import datetime, time, timedelta, timezone as datetime_timezone
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4
from zoneinfo import ZoneInfo

from django.contrib.auth.models import User
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from pool_service.models import Organization, OrganizationAccess, ServiceTask, ServiceTaskChange
from pool_service.operations_mcp_views import _task_data, _reschedule_task, _complete_task
from pool_service.services.call_commitment_control import _effective_deadline, process_call_commitment_controls
from pool_service.services.task_feedback import apply_feedback, state_for, version_for, waiting_control
from pool_service.services.task_waiting_schedule import CONTROL_LABEL, waiting_schedule_metadata


@override_settings(COMMUNICATION_TIME_ZONE="Asia/Barnaul", TIME_ZONE="Asia/Barnaul")
class WaitingCalendarTests(TestCase):
    def setUp(self):
        self.org = Organization.objects.create(name="Waiting calendar test organization")
        self.owner = User.objects.create_user("waiting-calendar-owner")
        OrganizationAccess.objects.create(user=self.owner, organization=self.org, role="owner")
        self.old_date = timezone.localdate() - timedelta(days=45)
        self.check = timezone.now() + timedelta(days=45)
        self.check_date = self.check.astimezone(ZoneInfo("Asia/Barnaul")).date()
        self.task = ServiceTask.objects.create(
            organization=self.org, title="Meeting at the site", description="Original agreement",
            start_date=self.old_date, end_date=self.old_date,
            start_time=time(11), end_time=time(11),
            due_at=datetime.combine(self.old_date, time(11), tzinfo=ZoneInfo("Asia/Barnaul")),
            task_type=ServiceTask.TYPE_CRM_FOLLOWUP, source_type=ServiceTask.SOURCE_SYSTEM,
            status=ServiceTask.STATUS_NEW, created_by=self.owner, primary_responsible=self.owner,
            visibility=ServiceTask.VISIBILITY_PRIVATE, auto_created=True, is_editable=True,
            payload_json={"source": "call_analysis", "source_call_id": 901, "actor": "employee"},
        )
        self.task.responsibles.add(self.owner)
        self.client.force_login(self.owner)
        self.authenticated = SimpleNamespace(grant=SimpleNamespace(authorized_by=self.owner, id=901))

    def apply(self, action="wait", **kwargs):
        self.task.refresh_from_db()
        arguments = {
            "task_id": self.task.pk, "user": self.owner, "action": action,
            "comment": "Client postponed; no new date agreed", "request_id": uuid4(),
            "expected_version": version_for(self.task),
        }
        if action == "wait":
            arguments["next_check_at"] = self.check
        arguments.update(kwargs)
        result = apply_feedback(**arguments)
        self.task.refresh_from_db()
        return result

    def test_wait_uses_control_slot_without_fabricating_appointment(self):
        self.apply()
        self.assertEqual(self.task.start_date, self.check_date)
        self.assertIsNone(self.task.end_date)
        self.assertIsNone(self.task.start_time)
        self.assertIsNone(self.task.end_time)
        self.assertIsNone(self.task.due_at)
        self.assertTrue(self.task.title.startswith(CONTROL_LABEL))
        self.assertEqual(waiting_control(self.task), (True, self.check))
        self.assertEqual(self.task.description, "Original agreement")

    @patch("pool_service.views._redirect_if_access_blocked", return_value=None)
    def test_grid_and_list_remove_old_slot_and_show_only_internal_check(self, _blocked):
        self.apply()
        for mode in ("grid", "list"):
            with self.subTest(mode=mode):
                old = self.client.get(reverse("readings_all"), {"month": self.old_date.strftime("%Y-%m"), "view": mode})
                self.assertEqual(old.status_code, 200)
                self.assertNotIn(self.task.pk, [entry["id"] for entry in old.context["task_search_index"]])
                self.assertEqual(old.context["overdue_count"], 0)
                new = self.client.get(reverse("readings_all"), {"month": self.check_date.strftime("%Y-%m"), "view": mode})
                self.assertEqual(new.status_code, 200)
                entries = [entry for entry in new.context["task_search_index"] if entry["id"] == self.task.pk]
                self.assertEqual(len(entries), 1)
                self.assertEqual(entries[0]["date"], self.check_date.isoformat())
                self.assertTrue(entries[0]["title"].startswith(CONTROL_LABEL))
                self.assertEqual(entries[0]["status"], "planned")
                self.assertEqual(new.context["overdue_count"], 0)

    @patch("pool_service.views._redirect_if_access_blocked", return_value=None)
    def test_both_card_templates_distinguish_check_and_unknown_deadline(self, _blocked):
        self.apply()
        for params in ({}, {"modal": "1"}):
            with self.subTest(params=params):
                response = self.client.get(reverse("task_edit", args=[self.task.pk]), params)
                self.assertEqual(response.status_code, 200)
                self.assertContains(response, "data-waiting-check")
                self.assertContains(response, "Дата встречи пока не согласована")
                self.assertContains(response, "Следующая внутренняя проверка")
                self.assertContains(response, self.check.astimezone(ZoneInfo("Asia/Barnaul")).strftime("%d.%m.%Y %H:%M"))

    def test_mcp_data_does_not_present_control_date_as_appointment(self):
        self.apply()
        data = _task_data(self.task)
        self.assertEqual(data["schedule_kind"], "internal_check")
        self.assertIsNone(data["start_date"])
        self.assertIsNone(data["end_date"])
        self.assertIsNone(data["start_time"])
        self.assertIsNone(data["end_time"])
        self.assertEqual(datetime.fromisoformat(data["next_check_at"]), self.check)
        self.assertEqual(data["calendar_check_date"], self.check_date.isoformat())
        self.assertEqual(data["agreement_title"], "Meeting at the site")

    def test_mcp_reschedule_on_check_day_is_not_a_noop(self):
        self.apply()
        result = _reschedule_task(self.authenticated, self.org, {
            "task_id": self.task.pk, "due_date": self.check_date.isoformat(), "reason": "Now confirmed by client",
        })
        self.assertTrue(result["changed"])
        self.task.refresh_from_db()
        self.assertEqual(self.task.status, ServiceTask.STATUS_NEW)
        self.assertEqual(self.task.title, "Meeting at the site")
        self.assertEqual(self.task.end_date, self.check_date)
        self.assertIsNone(self.task.start_time)
        self.assertIsNone(state_for(self.task)["next_check_at"])
        self.assertEqual(result["task"]["schedule_kind"], "appointment")

    def test_mcp_completion_releases_waiting_label_and_check(self):
        self.apply()
        _complete_task(self.authenticated, self.org, {"task_id": self.task.pk, "comment": "Done"})
        self.task.refresh_from_db()
        self.assertEqual(self.task.title, "Meeting at the site")
        self.assertEqual(self.task.status, ServiceTask.STATUS_DONE)
        self.assertIsNone(state_for(self.task)["next_check_at"])
        self.assertFalse(waiting_control(self.task)[0])

    @patch("pool_service.views._redirect_if_access_blocked", return_value=None)
    def test_cancelled_waiting_task_is_removed_from_calendar_and_cannot_move(self, _blocked):
        self.apply()
        self.apply("cancel", comment="Client cancelled the meeting")
        self.assertEqual(self.task.status, ServiceTask.STATUS_CANCELLED)
        self.assertFalse(waiting_control(self.task)[0])
        self.assertEqual(self.task.title, "Meeting at the site")

        response = self.client.get(
            reverse("readings_all"),
            {"month": self.check_date.strftime("%Y-%m")},
        )
        self.assertEqual(response.status_code, 200)
        self.assertNotIn(
            self.task.pk,
            [entry["id"] for entry in response.context["task_search_index"]],
        )
        move = self.client.post(
            reverse("task_move"),
            data=json.dumps({
                "task_id": self.task.pk,
                "target_date": (self.check_date + timedelta(days=1)).isoformat(),
            }),
            content_type="application/json",
        )
        self.assertEqual(move.status_code, 400)
        self.assertEqual(move.json()["error"], "cancelled_task")

    @patch("pool_service.views._redirect_if_access_blocked", return_value=None)
    def test_restored_completed_waiting_task_stays_without_appointment_until_rescheduled(self, _blocked):
        self.apply()
        _complete_task(
            self.authenticated,
            self.org,
            {"task_id": self.task.pk, "comment": "Done for now"},
        )
        self.task.refresh_from_db()
        self.assertTrue(self.task.is_archived)
        self.assertTrue(state_for(self.task)["appointment_unknown"])

        response = self.client.post(
            reverse("archive_restore_task", args=[self.task.pk])
        )
        self.assertEqual(response.status_code, 302)
        self.task.refresh_from_db()
        self.assertFalse(self.task.is_archived)
        self.assertIsNone(self.task.completed_at)
        self.assertEqual(self.task.status, ServiceTask.STATUS_WAITING)
        restored_state = state_for(self.task)
        self.assertEqual(restored_state["mode"], "waiting")
        self.assertTrue(restored_state["appointment_unknown"])
        self.assertIsNone(restored_state["next_check_at"])

        metadata = waiting_schedule_metadata(self.task)
        self.assertEqual(metadata["schedule_kind"], "no_appointment")
        self.assertIsNone(metadata["start_date"])
        self.assertIsNone(metadata["end_date"])

        calendar_response = self.client.get(
            reverse("readings_all"),
            {"month": self.check_date.strftime("%Y-%m")},
        )
        self.assertEqual(calendar_response.status_code, 200)
        self.assertNotIn(
            self.task.pk,
            [entry["id"] for entry in calendar_response.context["task_search_index"]],
        )

        move = self.client.post(
            reverse("task_move"),
            data=json.dumps({
                "task_id": self.task.pk,
                "target_date": (self.check_date + timedelta(days=1)).isoformat(),
            }),
            content_type="application/json",
        )
        self.assertEqual(move.status_code, 409)
        self.assertEqual(move.json()["error"], "waiting_task_use_feedback")

        new_date = self.check_date + timedelta(days=3)
        self.apply("reschedule", due_date=new_date)
        self.assertEqual(self.task.status, ServiceTask.STATUS_NEW)
        self.assertEqual(self.task.end_date, new_date)
        self.assertNotIn("appointment_unknown", state_for(self.task))
        self.assertEqual(
            waiting_schedule_metadata(self.task)["schedule_kind"],
            "appointment",
        )

    def test_human_reschedule_restores_agreement_and_actual_date(self):
        self.apply()
        new_date = self.check_date + timedelta(days=3)
        self.apply("reschedule", due_date=new_date, due_time=time(14, 30))
        self.assertEqual(self.task.title, "Meeting at the site")
        self.assertEqual(self.task.start_date, new_date)
        self.assertEqual(self.task.end_date, new_date)
        self.assertEqual(self.task.start_time, time(14, 30))
        self.assertFalse(waiting_control(self.task)[0])
        self.assertNotIn("waiting_calendar_title", state_for(self.task))

    def test_repeat_wait_does_not_stack_label_or_create_another_task(self):
        self.apply()
        self.apply(next_check_at=self.check + timedelta(days=1))
        self.assertEqual(self.task.title.count(CONTROL_LABEL), 1)
        self.assertEqual(state_for(self.task)["agreement_title"], "Meeting at the site")
        self.assertEqual(ServiceTask.objects.filter(organization=self.org).count(), 1)
        self.assertEqual(self.task.payload_json["source_call_id"], 901)

    def test_original_appointment_remains_in_history(self):
        self.apply()
        event = json.loads(self.task.changes.get(field_name__startswith="feedback:").new_value)
        self.assertEqual(event["before"]["start_date"], self.old_date.isoformat())
        self.assertEqual(event["before"]["title"], "Meeting at the site")
        self.assertEqual(event["before"]["start_time"], "11:00:00")

    def test_new_human_title_is_not_overwritten_when_waiting_ends(self):
        self.apply()
        ServiceTask.objects.filter(pk=self.task.pk).update(title="Human corrected task title")
        self.apply("reschedule", due_date=self.check_date)
        self.assertEqual(self.task.title, "Human corrected task title")

    def test_long_title_survives_wait_and_reschedule(self):
        original = "x" * 255
        ServiceTask.objects.filter(pk=self.task.pk).update(title=original)
        self.apply()
        self.assertLessEqual(len(self.task.title), 255)
        self.assertEqual(state_for(self.task)["agreement_title"], original)
        self.apply("reschedule", due_date=self.check_date)
        self.assertEqual(self.task.title, original)

    def test_slot_date_uses_communication_timezone_at_midnight(self):
        utc_date = (timezone.now() + timedelta(days=60)).date()
        check = datetime.combine(utc_date, time(18, 15), tzinfo=datetime_timezone.utc)
        self.apply(next_check_at=check)
        self.assertEqual(self.task.start_date, utc_date + timedelta(days=1))
        self.assertIn("01:15", self.task.title)

    def test_comment_does_not_move_internal_check(self):
        self.apply()
        before = (self.task.title, self.task.start_date, state_for(self.task)["next_check_at"])
        self.apply("comment", comment="Waiting for an answer")
        self.assertEqual((self.task.title, self.task.start_date, state_for(self.task)["next_check_at"]), before)

    def test_waiting_read_and_serialization_do_not_write_task(self):
        self.apply()
        before = ServiceTask.objects.filter(pk=self.task.pk).values().get()
        history_count = ServiceTaskChange.objects.count()
        for _ in range(2):
            _task_data(self.task)
            waiting_schedule_metadata(self.task)
        self.assertEqual(ServiceTask.objects.filter(pk=self.task.pk).values().get(), before)
        self.assertEqual(ServiceTaskChange.objects.count(), history_count)

    def test_no_old_due_reminder_before_internal_check(self):
        self.apply()
        self.assertEqual(_effective_deadline(self.task, self.task.payload_json), self.check)
        result = process_call_commitment_controls(now=self.check - timedelta(minutes=1))
        self.assertEqual(result["due_reminders"], 0)
        self.assertEqual(result["escalations"], 0)

    def test_malformed_check_does_not_expose_a_fabricated_deadline(self):
        self.apply()
        self.task.payload_json["task_feedback"]["next_check_at"] = "invalid"
        data = waiting_schedule_metadata(self.task)
        self.assertIsNone(data["next_check_at"])
        self.assertIsNone(data["start_date"])
        self.assertIsNone(data["end_date"])
