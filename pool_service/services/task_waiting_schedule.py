"""Calendar slots for a waiting task are internal checks, not appointments.

ServiceTask.start_date is a required legacy calendar key. In waiting mode it
must point at the next internal check, never the withdrawn appointment date.
A labelled title makes the distinction explicit to every existing calendar,
list and agent reader. The unmodified agreement title stays in durable state.
"""
from __future__ import annotations

from datetime import datetime


CONTROL_LABEL = "\u041a\u043e\u043d\u0442\u0440\u043e\u043b\u044c \u043e\u0436\u0438\u0434\u0430\u043d\u0438\u044f"


def schedule_waiting_check(task, state: dict, local_check: datetime) -> list[str]:
    """Set only a labelled internal calendar slot; keep appointment fields empty.

The caller owns the task lock/transaction and records the original snapshot.
local_check must already use the configured communication timezone.
"""
    if local_check.tzinfo is None or local_check.utcoffset() is None:
        raise ValueError("An aware internal check time is required.")
    original_title = task.title
    if (
        task.title == state.get("waiting_calendar_title")
        and isinstance(state.get("agreement_title"), str)
    ):
        original_title = state["agreement_title"]
    max_length = task._meta.get_field("title").max_length
    prefix = f"{CONTROL_LABEL} {local_check:%H:%M}: "
    calendar_title = (prefix + original_title)[:max_length]
    state["agreement_title"] = original_title
    state["waiting_calendar_title"] = calendar_title
    state["calendar_kind"] = "internal_check"
    task.title = calendar_title
    task.start_date = local_check.date()
    # Do not put the internal check time into appointment-time fields: a later
    # date-only reschedule must not accidentally promise that time to a client.
    task.end_date = task.start_time = task.end_time = task.due_at = None
    return ["title", "start_date", "end_date", "start_time", "end_time", "due_at"]


def finish_waiting_check(task, state: dict) -> list[str]:
    """Restore the agreement label only if nobody has replaced it meanwhile."""
    fields = []
    generated = state.pop("waiting_calendar_title", None)
    original = state.pop("agreement_title", None)
    state.pop("calendar_kind", None)
    if isinstance(generated, str) and isinstance(original, str) and task.title == generated:
        task.title = original
        fields.append("title")
    return fields


def waiting_schedule_metadata(task) -> dict:
    """Do not expose the legacy calendar key as an agreed deadline to agents."""
    payload = task.payload_json if isinstance(task.payload_json, dict) else {}
    state = payload.get("task_feedback")
    state = state if isinstance(state, dict) else {}
    waiting = (
        task.status == "waiting" and task.end_date is None
        and state.get("mode") == "waiting"
    )
    if not waiting:
        return {"schedule_kind": "appointment", "next_check_at": None, "agreement_title": task.title}
    check = state.get("next_check_at")
    try:
        parsed = datetime.fromisoformat(check) if isinstance(check, str) else None
        if parsed is None or parsed.tzinfo is None or parsed.utcoffset() is None:
            check = None
    except ValueError:
        check = None
    original = state.get("agreement_title")
    return {
        "schedule_kind": "internal_check",
        "agreement_title": original if isinstance(original, str) else task.title,
        "next_check_at": check,
        "calendar_check_date": task.start_date.isoformat() if task.start_date else None,
        "start_date": None, "end_date": None, "start_time": None, "end_time": None,
    }


def release_waiting_schedule(task, *, mode="active", status="new") -> list[str]:
    """Release waiting state when an existing non-feedback writer changes it.

The caller holds the same business lock and must save the returned fields
atomically with its explicit deadline/completion update and history entry.
"""
    payload = dict(task.payload_json) if isinstance(task.payload_json, dict) else {}
    state = payload.get("task_feedback")
    if not isinstance(state, dict) or state.get("mode") != "waiting":
        return []
    state = dict(state)
    fields = finish_waiting_check(task, state)
    state["mode"] = mode
    state["next_check_at"] = None
    state["requires_review"] = True
    state["revision"] = int(state.get("revision", 0)) + 1
    state["control_revision"] = int(state.get("control_revision", 0)) + 1
    payload["task_feedback"] = state
    payload["needs_due_date"] = False
    payload.pop("control_state", None)
    task.payload_json = payload
    task.status = status
    return fields + ["payload_json", "status"]
