from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from hashlib import sha256
from urllib.parse import urlencode

from django.contrib.auth import get_user_model
from django.db import transaction
from django.db.models import Exists, F, OuterRef, Q
from django.db.models.functions import Coalesce
from django.urls import reverse

from pool_service.communication_models import CommunicationChannel, Conversation, ConversationMessage, PhoneCall
from pool_service.communication_services import conversation_capability
from pool_service.crm_commercial_models import CrmLead, CrmLeadSource
from pool_service.crm_followup_models import CustomerReminderPolicy, CustomerReminderReceipt
from pool_service.models import Employee, Organization, OrganizationAccess
from pool_service.services.call_access import can_read_call
from pool_service.services.call_privacy import is_private_call
from pool_service.services.communication_reply_state import manager_reply_filter, with_reply_state
from pool_service.services.crm_lead_access import can_read_lead
from pool_service.services.notifications import notify_users
from pool_service.staff_call_models import StaffAccount
from pool_service.staff_structure_models import DepartmentAssignment, StaffDepartment
from .customer_reminder_calendar import BARNAUL, previous_workday_open, reminder_step, summary_ready, working_start


@dataclass(frozen=True)
class Work:
    kind: str
    source: object
    started_at: datetime
    marker: int
    lead: CrmLead | None = None
    retry: bool = False

    @property
    def group_key(self):
        return ("lead", self.lead.pk) if self.lead else (self.kind, self.source.pk)

    @property
    def receipt_key(self):
        # An original event anchors the SLA. A scheduled retry has its own identity.
        retry_key = f":{self.started_at.isoformat()}" if self.retry else ""
        return f"{self.kind}:{self.source.pk}:{self.marker}{retry_key}"

    @property
    def responsible_id(self):
        if self.kind == "message" and self.source.assignee_id:
            return self.source.assignee_id
        if self.lead:
            return self.lead.assignee_id
        if self.kind != "call":
            return None
        context = getattr(self.source, "work_context", None)
        if context and context.organization_id == self.source.organization_id and context.deal_id:
            if context.deal.organization_id == self.source.organization_id and not context.deal.is_archived:
                return context.deal.manager_id
        return self.source.employee_id

    @property
    def label(self):
        return {"message": "Ответить в чате", "call": "Перезвонить", "lead": "Разобрать лид"}[self.kind]

    @property
    def title(self):
        if self.kind == "lead":
            return f"Лид №{self.source.pk}"
        if self.kind == "message":
            return self.source.display_name
        return self.source.contact_name or self.source.phone_number

    @property
    def url(self):
        if self.kind == "lead":
            return reverse("crm_live_lead_detail", args=[self.source.pk]) + "?" + urlencode({"org_id": self.source.organization_id})
        if self.kind == "message":
            return reverse("communications_conversations") + "?" + urlencode({
                "org_id": self.source.organization_id, "conversation": self.source.uuid,
            })
        return ""


def eligible_users(organization):
    dismissed = Employee.objects.filter(organization=organization, user_id__isnull=False).filter(
        Q(is_active=False) | Q(employment_status=Employee.STATUS_DISMISSED),
    ).values("user_id")
    technical = StaffAccount.objects.filter(
        organization=organization, kind__in=("service", "system"), user_id__isnull=False,
    ).values("user_id")
    return get_user_model().objects.filter(
        is_active=True,
        organizationaccess__organization=organization,
        organizationaccess__role__in=("owner", "admin", "manager", "service", "installer"),
    ).exclude(pk__in=dismissed).exclude(pk__in=technical).distinct()


def _related_lead(organization, kind, source_ids):
    ids = list(CrmLeadSource.objects.filter(
        organization=organization, lead__organization=organization,
        inbox__organization=organization, inbox__scope_verified=True,
        inbox__source_type=kind, inbox__source_id__in=source_ids,
        lead__is_archived=False,
    ).order_by().values_list("lead_id", flat=True).distinct()[:2])
    return CrmLead.objects.filter(pk=ids[0], organization=organization).first() if len(ids) == 1 else None


def lead_for_call(organization, call):
    context = getattr(call, "work_context", None)
    if context:
        if context.organization_id == organization.pk and context.lead_id:
            return CrmLead.objects.filter(pk=context.lead_id, organization=organization, is_archived=False).first()
        return None
    return _related_lead(organization, "call", [call.pk])


def _chat_work(organization, source_id=None, *, since=None):
    dialogs = with_reply_state(Conversation.objects.filter(
        organization=organization, connection__channel__organization=organization,
        connection__is_active=True, connection__channel__is_active=True,
        connection__channel__kind__in=CommunicationChannel.MESSAGE_KINDS,
    ).exclude(status__in=(Conversation.STATUS_DONE, Conversation.STATUS_SPAM))).filter(
        last_exchange_direction=ConversationMessage.DIRECTION_IN,
    ).select_related("client", "connection__channel")
    if source_id is not None:
        dialogs = dialogs.filter(pk=source_id)
    if since is not None:
        dialogs = dialogs.filter(Exists(ConversationMessage.objects.filter(
            conversation_id=OuterRef("pk"), created_at__gte=since,
            direction=ConversationMessage.DIRECTION_IN, intake_historical=False,
        )))
    for dialog in dialogs.iterator(chunk_size=128):
        messages = dialog.messages.annotate(event_at=Coalesce("intake_occurred_at", "created_at"))
        last_reply = messages.filter(manager_reply_filter()).order_by("-event_at", "-pk").values("event_at", "pk").first()
        incoming = messages.filter(
            direction=ConversationMessage.DIRECTION_IN, intake_historical=False,
        ).exclude(max_details__shared_phone__isnull=False)
        if last_reply:
            incoming = incoming.filter(
                Q(event_at__gt=last_reply["event_at"]) |
                Q(event_at=last_reply["event_at"], pk__gt=last_reply["pk"]),
            )
        first = incoming.order_by("created_at", "pk").values("created_at", "pk").first()
        if first:
            yield Work("message", dialog, first["created_at"], first["pk"],
                       _related_lead(organization, "message", incoming.order_by().values("pk")))


def callback_candidates(organization):
    return PhoneCall.objects.filter(
        organization=organization, connection__organization=organization,
        source_kind=PhoneCall.SOURCE_TELEPHONY, direction=PhoneCall.DIRECTION_IN,
        result=PhoneCall.RESULT_MISSED, intake_historical=False, internal_number_pending=False,
        internal_contact_number__isnull=True, missedcallfollowup__resolved_at__isnull=True,
    ).exclude(privacy_phone_key="").select_related("organization", "missedcallfollowup", "work_context__deal")


def returned_call(call):
    attempts = PhoneCall.objects.filter(
        organization_id=call.organization_id, connection__organization_id=call.organization_id,
        source_kind=PhoneCall.SOURCE_TELEPHONY, result=PhoneCall.RESULT_ANSWERED,
        direction__in=(PhoneCall.DIRECTION_IN, PhoneCall.DIRECTION_OUT),
        privacy_phone_key=call.privacy_phone_key, started_at__gt=call.started_at, duration_seconds__gt=0,
        internal_contact_number__isnull=True, internal_number_pending=False,
    ).select_related("organization")
    if call.client_id:
        attempts = attempts.filter(Q(client_id=call.client_id) | Q(client_id__isnull=True))
    return any(not is_private_call(attempt) for attempt in attempts.iterator(chunk_size=32))


def _call_work(organization, source_id=None, *, since=None):
    calls = callback_candidates(organization).order_by("started_at", "pk")
    if source_id is not None:
        calls = calls.filter(pk=source_id)
    if since is not None:
        calls = calls.filter(Q(created_at__gte=since) | Q(missedcallfollowup__next_attempt_at__gte=since))
    for call in calls.iterator(chunk_size=128):
        if is_private_call(call) or returned_call(call):
            continue
        followup = getattr(call, "missedcallfollowup", None)
        retry_at = followup.next_attempt_at if followup else None
        yield Work("call", call, retry_at or max(call.started_at, call.created_at), call.pk,
                   lead_for_call(organization, call), retry=bool(retry_at))


def _lead_work(organization, source_id=None, *, since=None):
    leads = CrmLead.objects.filter(
        Q(status="new") | Q(needs_review=True),
        organization=organization, kind="client", is_archived=False,
    ).exclude(status="closed")
    if source_id is not None:
        leads = leads.filter(pk=source_id)
    if since is not None:
        leads = leads.filter(received_at__gte=since)
    for lead in leads.order_by("received_at", "pk").iterator(chunk_size=128):
        yield Work("lead", lead, lead.received_at, lead.pk, lead)


def work_items(organization, *, since=None):
    yield from _chat_work(organization, since=since)
    yield from _call_work(organization, since=since)
    yield from _lead_work(organization, since=since)


def may_see(work, user, organization):
    if work.kind == "message":
        return conversation_capability(user, "can_view_conversations", organization)
    if work.kind == "call":
        return can_read_call(user, work.source)
    return can_read_lead(user, organization, work.source)


class RecipientDirectory:
    def __init__(self, policy, now):
        self.organization = policy.organization
        self.users = {user.pk: user for user in eligible_users(self.organization) if not user.is_superuser}
        memberships = list(OrganizationAccess.objects.filter(
            organization=self.organization, user_id__in=self.users,
            role__in=("manager", "owner"),
        ).order_by("user_id").values_list("user_id", "role"))
        self.manager_ids = [user_id for user_id, role in memberships if role == "manager"]
        owners = [user_id for user_id, role in memberships if role == "owner"]
        self.owner_ids = [policy.fallback_owner_id] if policy.fallback_owner_id in owners else owners if len(owners) == 1 else []
        self.departments = dict(StaffDepartment.objects.filter(
            organization=self.organization,
        ).values_list("pk", "parent_id"))
        self.user_departments = {}
        self.heads_by_department = defaultdict(list)
        self.head_ids = set()
        assignments = DepartmentAssignment.objects.filter(
            department__organization=self.organization, employee__organization=self.organization,
            employee__user_id=F("user_at_assignment_id"), employee__is_active=True,
            user_at_assignment_id__in=self.users, valid_from__lte=now,
        ).filter(Q(valid_to__isnull=True) | Q(valid_to__gt=now)).order_by("-valid_from", "-pk")
        for user_id, department, position, includes_children in assignments.values_list(
            "user_at_assignment_id", "department_id", "position", "includes_children",
        ):
            if user_id in self.user_departments:
                continue
            self.user_departments[user_id] = department
            if position == "sales_head":
                self.heads_by_department[department].append((user_id, includes_children))
                self.head_ids.add(user_id)

    def _visible(self, work, candidate_ids):
        return tuple(
            self.users[user_id] for user_id in dict.fromkeys(candidate_ids)
            if user_id in self.users and may_see(work, self.users[user_id], self.organization)
        )

    def managers(self, work):
        assigned = self._visible(work, (work.responsible_id,))
        return assigned or self._visible(work, self.manager_ids)

    def escalation(self, work):
        department = self.user_departments.get(work.responsible_id)
        if department is None:
            heads = self._visible(work, self.head_ids) if len(self.head_ids) == 1 else ()
            return heads or self._visible(work, self.owner_ids)
        direct = True
        visited = set()
        while department in self.departments and department not in visited:
            visited.add(department)
            heads = self._visible(work, (
                user_id for user_id, covers_children in self.heads_by_department[department]
                if user_id != work.responsible_id and (direct or covers_children)
            ))
            if heads:
                return heads
            direct = False
            department = self.departments[department]
        return self._visible(work, self.owner_ids)

    def recipients(self, work, step):
        if step == "30":
            return self.escalation(work)
        managers = self.managers(work)
        if step == "morning":
            return tuple(dict.fromkeys((*managers, *self.escalation(work))))
        return managers

    def can_handle(self, work, user):
        if user.pk in self.owner_ids:
            return may_see(work, user, self.organization)
        return user in self.managers(work)


def queue_url(organization):
    return reverse("customer_followups") + "?" + urlencode({"org_id": organization.pk})


def _work_step(work, policy, now):
    if work.retry:
        if now < working_start(work.started_at) or now != working_start(now):
            return None
        return "morning" if reminder_step(work.started_at, now) == "morning" else "retry"
    if work.started_at < policy.started_at:
        # Existing open work enters the next shift summary rather than replaying
        # its two historical notifications.
        stage = reminder_step(policy.started_at, now)
        return stage if stage == "morning" else None
    return reminder_step(work.started_at, now)


def _current_work(work, organization):
    source = {"message": _chat_work, "call": _call_work, "lead": _lead_work}[work.kind]
    return next(source(organization, work.source.pk), None)


def _recipient_batches(policy, now, *, morning):
    org = policy.organization
    directory = RecipientDirectory(policy, now)
    grouping = defaultdict(list)
    # Minute ticks only scan recent work. A completed digest avoids rescanning
    # older open work on every tick.
    since = None if morning else previous_workday_open(now)
    for work in work_items(org, since=since):
        step = _work_step(work, policy, now)
        if not step or (step == "morning") != morning:
            continue
        for user in directory.recipients(work, step):
            if not CustomerReminderReceipt.objects.filter(
                organization=org, user=user, work_key=work.receipt_key, step=step,
            ).exists():
                group_key = None if step == "morning" else work.group_key
                grouping[(user.pk, step, group_key)].append(work)
    return directory.users, grouping


def _write_notice(policy, user, step, items, now):
    org = policy.organization
    with transaction.atomic():
        Organization.objects.select_for_update().get(pk=org.pk)
        if not CustomerReminderPolicy.objects.select_for_update().filter(
            pk=policy.pk, fallback_owner_id=policy.fallback_owner_id, started_at=policy.started_at,
        ).exists():
            return False
        directory = RecipientDirectory(policy, now)
        if user.pk not in directory.users:
            return False
        if step == "morning":
            batch_key = f"morning:{now.astimezone(BARNAUL).date().isoformat()}"
            if CustomerReminderReceipt.objects.filter(
                organization=org, user=user, work_key=batch_key, step=step,
            ).exists():
                return False
        else:
            batch_key = ""

        keys = []
        labels = set()
        for item in items:
            work = _current_work(item, org)
            if (not work or work.receipt_key != item.receipt_key or work.group_key != item.group_key
                    or _work_step(work, policy, now) != step
                    or user not in directory.recipients(work, step)):
                continue
            _, created = CustomerReminderReceipt.objects.get_or_create(
                organization=org, user=user, work_key=work.receipt_key, step=step,
            )
            if created:
                keys.append(work.receipt_key)
                labels.add(work.label)
        if not keys:
            return False
        if batch_key:
            CustomerReminderReceipt.objects.create(organization=org, user=user, work_key=batch_key, step=step)
        title = "Утренняя сводка по обращениям" if step == "morning" else "Напоминание по обращению"
        detail = "Открытые действия: " + ", ".join(sorted(labels)) + "."
        dedupe = sha256("|".join(sorted(keys)).encode()).hexdigest()
        payload = dict(
            title=title, message=detail, level="warning", kind="crm_assignment",
            action_url=queue_url(org), organization=org, dedupe_key=f"followup:{org.pk}:{step}:{dedupe}",
        )
        notify_users([user], send_push=False, **payload)
        # A receipt prevents another push if this tick or an uncertain delivery is retried.
        transaction.on_commit(lambda: notify_users([user], send_in_app=False, **payload), robust=True)
    return True


def _deliver_batches(policy, now, *, morning):
    users, groups = _recipient_batches(policy, now, morning=morning)
    return sum(
        _write_notice(policy, users[user_id], step, items, now)
        for (user_id, step, _), items in groups.items()
    )


def notify_due(policy, now):
    delivered = _deliver_batches(policy, now, morning=False)
    if not summary_ready(now, policy.last_digest_on):
        return delivered
    delivered += _deliver_batches(policy, now, morning=True)
    digest_date = now.astimezone(BARNAUL).date()
    CustomerReminderPolicy.objects.filter(pk=policy.pk, started_at=policy.started_at).update(
        last_digest_on=digest_date,
    )
    policy.last_digest_on = digest_date
    return delivered

