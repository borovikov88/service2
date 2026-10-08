"""Owner-scoped personal-call privacy enforcement.

This module is deliberately independent of UI permissions: once a telephony
call explicitly matches a participating owner's private-number list, every
work surface must treat it as unavailable. Existing rows are not deleted.
"""
from __future__ import annotations

from collections import defaultdict
from contextlib import contextmanager

from django.contrib.auth.models import User
from django.db import transaction

from pool_service.call_processing_models import CallPrivateNumber
from pool_service.communication_models import PhoneCall
from pool_service.models import Organization
from pool_service.services.call_processing_policy import phone_key


def _key(value):
    return phone_key(value) if value else ""


def _participant_ids(call):
    return tuple(
        dict.fromkeys(
            uid
            for uid in (call.employee_id, call.peer_employee_id)
            if uid
        )
    )


def _counterpart_keys(call, usernames):
    participants = _participant_ids(call)
    if not participants:
        return {}

    if call.direction != PhoneCall.DIRECTION_INTERNAL:
        key = _key(call.phone_number)
        values = frozenset({key}) if key else frozenset()
        return {uid: values for uid in participants}

    if len(participants) == 1:
        owner_id = participants[0]
        if call.employee_id == owner_id:
            values = {
                _key(call.peer_provider_user),
                _key(call.peer_provider_extension),
            }
        else:
            values = {
                _key(call.provider_user),
                _key(call.provider_extension),
            }
        return {
            owner_id: frozenset(value for value in values if value),
        }

    if len(participants) != 2:
        return {uid: frozenset() for uid in participants}

    first_id, second_id = participants

    first_counterparts = {
        _key(call.peer_provider_user),
        _key(call.peer_provider_extension),
        _key(usernames.get(second_id)),
    }
    second_counterparts = {
        _key(call.provider_user),
        _key(call.provider_extension),
        _key(usernames.get(first_id)),
    }
    return {
        first_id: frozenset(value for value in first_counterparts if value),
        second_id: frozenset(value for value in second_counterparts if value),
    }


def private_call_ids(calls):
    """Return explicit private matches without exposing labels or phone values."""
    calls = list(calls)
    if not calls:
        return set()

    participant_ids = {
        uid for call in calls for uid in _participant_ids(call)
    }
    if not participant_ids:
        return set()

    organization_ids = {call.organization_id for call in calls if call.organization_id}
    rows = list(
        CallPrivateNumber.objects.filter(
            organization_id__in=organization_ids,
            owner_id__in=participant_ids,
        ).values_list("organization_id", "owner_id", "phone_key")
    )
    if not rows:
        return set()

    private = defaultdict(lambda: defaultdict(set))
    relevant_users = set()
    for organization_id, owner_id, key in rows:
        private[organization_id][owner_id].add(key)
        relevant_users.add(owner_id)

    # Usernames are a useful phone identity for internal calls in Service2.
    # Fetch them in one query; never return them from this privacy service.
    usernames = dict(
        User.objects.filter(pk__in=participant_ids)
        .values_list("id", "username")
    )

    hidden = set()
    for call in calls:
        owner_lists = private.get(call.organization_id)
        if not owner_lists:
            continue
        counterparts = _counterpart_keys(call, usernames)
        for owner_id, numbers in owner_lists.items():
            if owner_id not in counterparts:
                continue
            if numbers & counterparts[owner_id]:
                hidden.add(call.pk)
                break
    return hidden


def is_private_call(call):
    return bool(call and call.pk and call.pk in private_call_ids([call]))


@contextmanager
def locked_call_for_privacy(call_id):
    """Serialize a final privacy decision with organization privacy updates."""
    with transaction.atomic():
        organization_id = PhoneCall.objects.filter(pk=call_id).values_list(
            "organization_id", flat=True
        ).first()
        if not organization_id:
            yield None
            return
        # Privacy settings use the same organization -> call lock order.
        Organization.objects.select_for_update().get(pk=organization_id)
        call = PhoneCall.objects.select_for_update().filter(
            pk=call_id, organization_id=organization_id
        ).first()
        yield call


def visible_calls(calls):
    calls = list(calls)
    hidden = private_call_ids(calls)
    return [call for call in calls if call.pk not in hidden]


def task_source_is_private(task):
    """Return True when a task derives from an explicitly private call."""
    payload = task.payload_json if isinstance(task.payload_json, dict) else {}
    raw_call_id = payload.get("source_call_id")
    try:
        call_id = int(raw_call_id)
    except (TypeError, ValueError):
        return False
    if call_id <= 0:
        return False
    call = PhoneCall.objects.filter(
        pk=call_id,
        organization_id=task.organization_id,
    ).first()
    return is_private_call(call) if call else False


def private_source_task_ids(tasks):
    """Batch-classify existing tasks whose source call is now explicitly private."""
    tasks = list(tasks)
    task_calls = {}
    call_ids = set()
    for task in tasks:
        payload = task.payload_json if isinstance(task.payload_json, dict) else {}
        raw_call_id = payload.get("source_call_id")
        try:
            call_id = int(raw_call_id)
        except (TypeError, ValueError):
            continue
        if call_id <= 0:
            continue
        task_calls[task.pk] = (task.organization_id, call_id)
        call_ids.add(call_id)
    if not call_ids:
        return set()

    calls = list(PhoneCall.objects.filter(pk__in=call_ids))
    hidden_calls = private_call_ids(calls)
    call_orgs = {call.pk: call.organization_id for call in calls}
    return {
        task_id
        for task_id, (organization_id, call_id) in task_calls.items()
        if call_id in hidden_calls and call_orgs.get(call_id) == organization_id
    }


def visible_calls_page(queryset, *, page_size=50, chunk_size=200, count_all=True):
    """Return a bounded visible page and optional exact visible count.

    The queryset is streamed in fixed chunks. Exact counters may scan the
    matching history, but never materialize the whole archive in Python.
    """
    page_size = max(0, int(page_size))
    chunk_size = max(1, min(int(chunk_size), 500))
    page = []
    visible_count = 0
    buffer = []

    def consume(items):
        nonlocal visible_count
        hidden = private_call_ids(items)
        for call in items:
            if call.pk in hidden:
                continue
            visible_count += 1
            if len(page) < page_size:
                page.append(call)

    for call in queryset.iterator(chunk_size=chunk_size):
        buffer.append(call)
        if len(buffer) >= chunk_size:
            consume(buffer)
            buffer = []
            if not count_all and len(page) >= page_size:
                return page, None
    if buffer:
        consume(buffer)
    return page, visible_count if count_all else None
