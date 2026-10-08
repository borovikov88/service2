"""Version hashing must distinguish changes, not equivalent timezone offsets."""

from copy import copy
from datetime import date, datetime, time, timedelta, timezone
from types import SimpleNamespace

from django.test import SimpleTestCase

from pool_service.services.task_feedback import _digest, _iso, version_for


class TaskFeedbackCanonicalTimeTests(SimpleTestCase):
    def setUp(self):
        self.local = datetime(
            2026, 10, 7, 11, 0, 0, 123456,
            tzinfo=timezone(timedelta(hours=7)),
        )
        self.utc = self.local.astimezone(timezone.utc)
        self.task = SimpleNamespace(
            title="Meeting", description="Original source", status="new",
            start_date=date(2026, 10, 7), end_date=date(2026, 10, 7),
            start_time=time(11, 0), end_time=time(11, 0),
            due_at=self.local, completed_at=None, is_archived=False,
            primary_responsible_id=7, payload_json={},
            responsibles=SimpleNamespace(values_list=lambda *args, **kwargs: [7, 3]),
        )

    def test_equal_aware_instants_have_equal_iso_values(self):
        self.assertEqual(_iso(self.local), _iso(self.utc))
        self.assertEqual(_iso(self.local), "2026-10-07T04:00:00.123456+00:00")

    def test_date_time_naive_datetime_and_none_keep_their_meaning(self):
        values = [date(2026, 10, 7), time(11, 0), datetime(2026, 10, 7, 11, 0)]
        for value in values:
            with self.subTest(value=value):
                self.assertEqual(_iso(value), value.isoformat())
        self.assertIsNone(_iso(None))

    def test_due_at_offset_does_not_change_task_version(self):
        reloaded = copy(self.task)
        reloaded.due_at = self.utc
        self.assertEqual(version_for(self.task), version_for(reloaded))

    def test_completed_at_offset_does_not_change_task_version(self):
        self.task.completed_at = self.local
        reloaded = copy(self.task)
        reloaded.completed_at = self.utc
        self.assertEqual(version_for(self.task), version_for(reloaded))

    def test_a_real_deadline_change_still_changes_the_version(self):
        changed = copy(self.task)
        changed.due_at = self.utc + timedelta(minutes=1)
        self.assertNotEqual(version_for(self.task), version_for(changed))

    def test_microsecond_changes_are_not_lost(self):
        changed = copy(self.task)
        changed.due_at = self.utc + timedelta(microseconds=1)
        self.assertNotEqual(version_for(self.task), version_for(changed))

    def test_date_and_status_changes_still_change_the_version(self):
        for field, value in (("end_date", date(2026, 10, 8)), ("status", "waiting")):
            with self.subTest(field=field):
                changed = copy(self.task)
                setattr(changed, field, value)
                self.assertNotEqual(version_for(self.task), version_for(changed))

    def test_wait_command_digest_is_independent_of_timezone_representation(self):
        local_command = {"action": "wait", "next_check_at": _iso(self.local)}
        utc_command = {"action": "wait", "next_check_at": _iso(self.utc)}
        self.assertEqual(_digest(local_command), _digest(utc_command))
