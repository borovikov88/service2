"""Small calendar-slot invariants, independent of a database or AI service."""
import unittest
from datetime import date, datetime, time, timezone, timedelta
from types import SimpleNamespace

from pool_service.services.task_waiting_schedule import CONTROL_LABEL, schedule_waiting_check, finish_waiting_check


class WaitingSlotUnitTests(unittest.TestCase):
    def task(self, title="Meeting"):
        return SimpleNamespace(
            title=title, start_date=date(2026, 10, 7), end_date=date(2026, 10, 7),
            start_time=time(11), end_time=time(11), due_at=datetime(2026, 10, 7, 11, tzinfo=timezone.utc),
            _meta=SimpleNamespace(get_field=lambda name: SimpleNamespace(max_length=255)),
        )

    def test_slot_uses_internal_date_not_old_appointment(self):
        task, state = self.task(), {}
        schedule_waiting_check(task, state, datetime(2026, 11, 1, 9, tzinfo=timezone(timedelta(hours=7))))
        self.assertEqual(task.start_date, date(2026, 11, 1))
        self.assertIsNone(task.end_date)
        self.assertIsNone(task.start_time)
        self.assertIsNone(task.end_time)
        self.assertIsNone(task.due_at)
        self.assertTrue(task.title.startswith(CONTROL_LABEL))
        self.assertIn("09:00", task.title)
        self.assertEqual(state["agreement_title"], "Meeting")

    def test_repeat_wait_does_not_stack_titles(self):
        task, state = self.task(), {}
        for day in (1, 2):
            schedule_waiting_check(task, state, datetime(2026, 11, day, 9, tzinfo=timezone.utc))
        self.assertEqual(task.title.count(CONTROL_LABEL), 1)
        self.assertEqual(state["agreement_title"], "Meeting")

    def test_restores_original(self):
        task, state = self.task(), {}
        schedule_waiting_check(task, state, datetime(2026, 11, 1, tzinfo=timezone.utc))
        self.assertEqual(finish_waiting_check(task, state), ["title"])
        self.assertEqual(task.title, "Meeting")
        self.assertEqual(state, {})

    def test_preserves_new_human_title(self):
        task, state = self.task(), {}
        schedule_waiting_check(task, state, datetime(2026, 11, 1, tzinfo=timezone.utc))
        task.title = "New human title"
        self.assertEqual(finish_waiting_check(task, state), [])
        self.assertEqual(task.title, "New human title")

    def test_long_title_is_not_lost(self):
        task, state = self.task("x" * 255), {}
        schedule_waiting_check(task, state, datetime(2026, 11, 1, tzinfo=timezone.utc))
        self.assertEqual(len(task.title), 255)
        finish_waiting_check(task, state)
        self.assertEqual(task.title, "x" * 255)

    def test_naive_time_fails_before_mutation(self):
        task, state = self.task(), {}
        with self.assertRaises(ValueError):
            schedule_waiting_check(task, state, datetime(2026, 11, 1))
        self.assertEqual(task.title, "Meeting")
        self.assertEqual(state, {})

    def test_unrelated_state_is_preserved(self):
        task, state = self.task(), {"revision": 3, "reason": "Message"}
        schedule_waiting_check(task, state, datetime(2026, 11, 1, tzinfo=timezone.utc))
        finish_waiting_check(task, state)
        self.assertEqual(state, {"revision": 3, "reason": "Message"})
