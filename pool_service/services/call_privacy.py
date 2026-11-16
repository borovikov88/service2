"""Owner-scoped personal-call privacy enforcement.

This module is deliberately independent of UI permissions: once a telephony
call explicitly matches a participating owner's private-number list, every
work surface must treat it as unavailable. Existing rows are not deleted.
"""
from __future__ import annotations

from collections import defaultdict
from contextlib import contextmanager
from itertools import islice

from django.contrib.auth.models import User
from django.db import transaction
from django.db.models import Q

from pool_service.call_processing_models import CallPrivateNumber
from pool_service.communication_models import PhoneCall
from pool_service.models import Organization
from pool_service.services.call_processing_policy import phone_key

_UNSPECIFIED = object()


def _key(value):
    return phone_key(value) if value else ""


def _participant_ids(call):
    return tuple(
        dict.fromkeys(
            uid
            for uid in (call.employee_id, call.peer_employee_id,
                        call.owner_user_snapshot if call.direction != PhoneCall.DIRECTION_INTERNAL else None)
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


def private_call_ids(calls, *, for_update=False):
    """Return explicit private matches without exposing labels or phone values."""
    calls = list(calls)
    if not calls:
        return set()
    from pool_service.staff_call_models import CallPrivacyHold
    held = set(CallPrivacyHold.objects.filter(call_id__in=[c.pk for c in calls],
        released_at__isnull=True).values_list('call_id', flat=True))

    participant_ids = {
        uid for call in calls for uid in _participant_ids(call)
    }
    if not participant_ids:
        return held

    organization_ids = {call.organization_id for call in calls if call.organization_id}
    privacy_query = CallPrivateNumber.objects.filter(
            organization_id__in=organization_ids,
            owner_id__in=participant_ids,
        )
    if for_update:
        privacy_query = privacy_query.select_for_update()
    rows = list(privacy_query.values_list("organization_id", "owner_id", "phone_key"))
    if not rows:
        return held

    private = defaultdict(lambda: defaultdict(set))
    relevant_users = set()
    for organization_id, owner_id, key in rows:
        private[organization_id][owner_id].add(key)
        relevant_users.add(owner_id)

    # Usernames are a useful phone identity for internal calls in Service2.
    # Fetch them in one query; never return them from this privacy service.
    users_query = User.objects.filter(pk__in=participant_ids)
    if for_update:
        users_query = users_query.select_for_update()
    usernames = dict(users_query.values_list("id", "username"))

    hidden = set(held)
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


def preserve_private_history(organization, owner, key):
    from pool_service.staff_call_models import CallPrivacyHold
    calls = PhoneCall.objects.filter(organization=organization).filter(
        Q(employee_id=owner.pk) | Q(peer_employee_id=owner.pk)
        | Q(owner_user_snapshot=owner.pk)).order_by('pk')
    def save_batch(batch):
        ids = {uid for call in batch for uid in _participant_ids(call)}
        names = dict(User.objects.filter(pk__in=ids).values_list('pk', 'username'))
        for call in batch:
            if key in _counterpart_keys(call, names).get(owner.pk, ()):
                CallPrivacyHold.objects.update_or_create(call=call,
                    defaults={'organization': organization, 'owner': owner, 'released_at': None})
    batch = []
    for call in calls.iterator(chunk_size=200):
        batch.append(call)
        if len(batch) == 200:
            save_batch(batch)
            batch = []
    save_batch(batch)


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


def iter_visible_calls(queryset, *, chunk_size=200):
    iterator = queryset.iterator(chunk_size=chunk_size)
    while True:
        batch = list(islice(iterator, chunk_size))
        if not batch:
            return
        yield from visible_calls(batch)


def task_source_is_private(task, user=_UNSPECIFIED):
    """Return True when a task derives from an explicitly private call."""
    if user is not _UNSPECIFIED:
        return task.pk in private_source_task_ids([task], user=user)
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
    if user is not _UNSPECIFIED:
        from .call_access import can_read_call
        return not call or not can_read_call(user, call, media=True)
    return is_private_call(call) if call else False


def private_source_task_ids(tasks, *, for_update=False, user=_UNSPECIFIED):
    """Batch-classify existing tasks whose source call is now explicitly private."""
    tasks = list(tasks)
    # Child tasks inherit source restrictions; ancestry never grants access.
    from pool_service.models import ServiceTask
    ancestors = {task.pk: task for task in tasks}
    frontier = {task.parent_task_id for task in tasks if task.parent_task_id} - ancestors.keys()
    for _ in range(20):
        if not frontier:
            break
        batch = list(ServiceTask.objects.filter(pk__in=frontier).only('pk', 'organization_id', 'payload_json', 'parent_task_id'))
        ancestors.update({task.pk: task for task in batch})
        frontier = {task.parent_task_id for task in batch if task.parent_task_id} - ancestors.keys()
    task_calls = {}
    call_ids = set()
    invalid = set()
    for task in tasks:
        current, seen, references = task, set(), set()
        while current is not None:
            if current.pk in seen or len(seen) >= 20 or current.organization_id != task.organization_id:
                invalid.add(task.pk)
                break
            seen.add(current.pk)
            payload = current.payload_json if isinstance(current.payload_json, dict) else {}
            if payload.get('source_call_id') is not None:
                value = payload['source_call_id']
                if type(value) not in (str, int) or not str(value).isascii() or not str(value).isdigit() or int(value) <= 0:
                    invalid.add(task.pk)
                else:
                    references.add(int(value))
            if current.parent_task_id and current.parent_task_id not in ancestors:
                invalid.add(task.pk)
            current = ancestors.get(current.parent_task_id)
        task_calls[task.pk] = (task.organization_id, references)
        call_ids.update(references)
    if not call_ids:
        return invalid if user is not _UNSPECIFIED else set()

    calls_query = PhoneCall.objects.filter(pk__in=call_ids)
    if for_update:
        calls_query = calls_query.select_for_update()
    calls = list(calls_query)
    hidden_calls = private_call_ids(calls, for_update=for_update)
    if user is not _UNSPECIFIED and call_ids - hidden_calls:
        from .call_access import scope_calls
        allowed = set()
        remaining = call_ids - hidden_calls
        for org in Organization.objects.filter(pk__in={c.organization_id for c in calls if c.pk in remaining}):
            allowed.update(scope_calls(user, org, PhoneCall.objects.filter(pk__in=remaining), media=True).values_list('pk', flat=True))
        hidden_calls |= call_ids - allowed
    call_orgs = {call.pk: call.organization_id for call in calls}
    if user is _UNSPECIFIED:
        invalid = set()
    else:
        invalid |= {task_id for task_id, (org_id, references) in task_calls.items()
                    if any(call_orgs.get(pk) != org_id for pk in references)}
    return invalid | {
        task_id
        for task_id, (organization_id, references) in task_calls.items()
        if any(pk in hidden_calls and call_orgs.get(pk) == organization_id for pk in references)
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
