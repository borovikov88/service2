"""Read-only, permission-scoped card timeline with one chronological cursor."""
from datetime import timezone as utc
from decimal import Decimal
from urllib.parse import urlencode

from django.core import signing
from django.core.exceptions import PermissionDenied
from django.db.models import Q
from django.db.models.functions import Coalesce
from django.http import Http404
from django.urls import reverse
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from pool_service.client_queries import readable_clients
from pool_service.communication_models import ConversationMessage, PhoneCall, CallAnalysis
from pool_service.crm_commercial_models import CrmLead, CrmLeadSource, CrmEvent, CrmStage
from pool_service.models import OrganizationAccess, ServiceTask, ServiceTaskChange
from .crm_lead_access import (leads_for_user, can_read_lead, source_family,
    _communication_caps, _without_private_calls)
from .call_privacy import private_call_ids
from .task_read_permissions import readable_tasks

PAGE_SIZE = 30
KINDS = {"all", "comment", "call", "message", "task", "change"}
RANK = {"event": 4, "task": 3, "call": 2, "message": 1}
FIELD_LABELS = {
    "amount": "Сумма", "currency": "Валюта", "amount_basis": "Основание суммы",
    "budget_kind": "Бюджет", "budget_min": "Сумма / от", "budget_max": "До",
    "stage_id": "Стадия", "manager_id": "Ответственный", "assignee_id": "Ответственный",
    "status": "Состояние", "result": "Результат", "outcome": "Исход", "column": "Этап",
    "expected_decision_at": "Ожидаемое решение", "closed_at": "Завершение",
    "fulfillment_kind": "Исполнение", "object_kind": "Объект",
    "client_id": "Клиент", "contact_id": "Контакт",
}
VALUE_LABELS = {
    "unknown": "Не указано", "indicative": "Предварительная оценка", "agreed": "Согласовано",
    "exact": "Точная сумма", "range": "Диапазон", "RUB": "₽", "open": "Открыта",
    "won": "Успешно", "lost": "Провалена", "cancelled": "Отменена", "new": "Новый",
    "working": "В работе", "qualifying": "Уточнение", "waiting": "Ожидание",
    "deferred": "Отложен", "closed": "Завершён", "qualified": "Создана сделка",
    "customer_saved": "Клиент сохранён", "linked": "Связан со сделкой", "duplicate": "Повторное обращение",
    "non_target": "Нецелевой", "no_deal": "Без сделки", "spam": "Спам", "deleted": "Удалён",
    "unparsed": "Не разобран", "history": "Завершён", "failed": "Провален",
    "none": "Не выбран", "pool": "Бассейн", "client_object": "Объект клиента", "planned": "Планируемый объект",
    "construction": "Строительство и монтаж", "service": "Обслуживание", "retail": "Поставка", "mixed": "Смешанное",
}
MARKERS = {"title_changed": "Название изменено", "need_changed": "Потребность изменена",
    "work_subject_changed": "Предмет сделки изменён", "planned_object_changed": "Данные объекта изменены",
    "specialization_changed": "Специализация изменена", "next_action_changed": "Следующее действие изменено"}


def _name(user):
    return (user.get_full_name() or user.username) if user else "Система"


def _family(user, organization, subject):
    roots = [subject] if isinstance(subject, CrmLead) else list(leads_for_user(user, organization).filter(
        deal_links__deal=subject, deal_links__organization=organization).distinct())
    ids = set()
    for lead in roots:
        if can_read_lead(user, organization, lead):
            ids.update(source_family(user, organization, lead).values_list("pk", flat=True))
    return ids


def _cursor(request, organization, subject, kind):
    identity = [request.user.pk, organization.pk, subject._meta.model_name, subject.pk, kind]
    raw = request.GET.get("cursor")
    if not raw:
        return identity, None
    try:
        value = signing.loads(raw, salt="crm-card-activity-v1", max_age=3600)
        if not isinstance(value, list) or len(value) != 8 or value[:5] != identity:
            raise ValueError
        stamp = parse_datetime(value[5])
        if stamp is None or timezone.is_naive(stamp) or type(value[6]) is not int or value[6] not in RANK.values() or type(value[7]) is not int or value[7] <= 0:
            raise ValueError
        return identity, (stamp, value[6], value[7])
    except (signing.BadSignature, ValueError, TypeError, IndexError):
        raise Http404


def _portion(queryset, stamp_field, rank, before):
    if before:
        stamp, previous_rank, pk = before
        older = Q(**{stamp_field + "__lt": stamp})
        if rank < previous_rank:
            older |= Q(**{stamp_field: stamp})
        elif rank == previous_rank:
            older |= Q(**{stamp_field: stamp, "pk__lt": pk})
        queryset = queryset.filter(older)
    return list(queryset.order_by("-" + stamp_field, "-pk")[:PAGE_SIZE + 1])


def _entry(stream, obj, at, kind, title, **values):
    return {"id": stream + ":" + str(obj.pk), "at": at.astimezone(utc.utc).isoformat(),
        "kind": kind, "title": title, "_key": (at, RANK[stream], obj.pk), **values}


def _event_entries(request, organization, subject, family, kind, before):
    from pool_service.crm_live_views import EVENT_LABELS
    from pool_service.crm_lead_views import EVENTS
    labels = {**EVENT_LABELS, **EVENTS, "quote.prepare": "Подготовлено коммерческое предложение",
        "quote.send": "Коммерческое предложение отправлено", "quote.accept": "Коммерческое предложение принято",
        "quote.reject": "Коммерческое предложение отклонено", "quote.replace": "Коммерческое предложение заменено",
        "contact_merged": "Карточки контактов объединены"}
    scope = Q(lead_id__in=family)
    if not isinstance(subject, CrmLead):
        scope |= Q(deal=subject)
    queryset = CrmEvent.objects.filter(scope, organization=organization).select_related("actor", "card_comment")
    comments = Q(event_type__in=["lead.comment", "deal.comment"])
    if kind == "comment":
        queryset = queryset.filter(comments)
    elif kind == "change":
        queryset = queryset.exclude(comments)
    rows = _portion(queryset, "occurred_at", RANK["event"], before)
    stage_ids, user_ids, client_ids = set(), set(), set()
    for event in rows:
        for key, change in event.safe_diff.items():
            if key == "stage_id":
                stage_ids.update(value for value in change.values() if type(value) is int)
            if key in {"manager_id", "assignee_id"}:
                user_ids.update(value for value in change.values() if type(value) is int)
            if key in {"client_id", "contact_id"}:
                client_ids.update(value for value in change.values() if type(value) is int)
    client_names = dict(readable_clients(request.user).filter(organization=organization, pk__in=client_ids).values_list("pk", "name"))
    stages = dict(CrmStage.objects.filter(pipeline__organization=organization, pk__in=stage_ids).values_list("pk", "name"))
    staff = {access.user_id: _name(access.user) for access in OrganizationAccess.objects.filter(
        organization=organization, user_id__in=user_ids).select_related("user")}
    def display(key, value):
        if value is None or value == "":
            return "Не указано"
        if key == "stage_id":
            return stages.get(value, "Стадия недоступна")
        if key in {"manager_id", "assignee_id"}:
            return staff.get(value, "Сотрудник недоступен")
        if key in {"client_id", "contact_id"}:
            return client_names.get(value, "Карточка объединена или недоступна")
        if key in {"amount", "budget_min", "budget_max"}:
            return format(Decimal(value), ",.2f").replace(",", "\u00a0").replace(".", ",")
        return VALUE_LABELS.get(str(value), str(value))
    output = []
    for event in rows:
        comment = getattr(event, "card_comment", None)
        is_comment = event.event_type in {"lead.comment", "deal.comment"}
        details = []
        for key, change in event.safe_diff.items():
            if key in FIELD_LABELS and change.get("before") != change.get("after"):
                details.append(FIELD_LABELS[key] + ": " + display(key, change.get("before")) + " → " + display(key, change.get("after")))
            elif key in MARKERS and change.get("after"):
                details.append(MARKERS[key])
        if event.safe_diff.get("manual_completion_exception", {}).get("after"):
            details.append("Ручное завершение без подтверждения исполнения и оплаты")
        # Existing task reasons can contain protected task text; do not copy it
        # into the team-visible commercial history.
        allowed_reason = event.lead_id or event.event_type in {"deal.transition", "deal.close_negative", "deal.bind_order", "deal.unbind_order", "deal.onec_completed", "deal.onec_review", "deal.assign"}
        origin = "Из исходного лида" if event.lead_id and (not isinstance(subject, CrmLead) or event.lead_id != subject.pk) else ""
        output.append(_entry("event", event, event.occurred_at, "comment" if is_comment else "change",
            "Комментарий" if is_comment else labels.get(event.event_type, "Изменение карточки"),
            actor=_name(event.actor), body=comment.body if comment else (event.reason if allowed_reason else ""),
            details=details, origin=origin))
    return output


def activity_page(request, organization, subject):
    """No network calls, implicit links, read receipts or writes on this GET."""
    kind = request.GET.get("type", "all")
    if kind not in KINDS:
        raise Http404
    identity, before = _cursor(request, organization, subject, kind)
    family = _family(request.user, organization, subject)
    caps = _communication_caps(request.user, organization)
    rows = []
    returned_calls = []
    if kind in {"all", "change", "comment"}:
        rows += _event_entries(request, organization, subject, family, kind, before)
    sources = CrmLeadSource.objects.filter(organization=organization, lead_id__in=family,
        inbox__organization=organization, inbox__scope_verified=True)
    phone_key = ""
    if isinstance(subject, CrmLead):
        from .crm_lead_identity import lead_identity, history_phone_key
        contact_identity = lead_identity(subject, user=request.user)
        phone_key = history_phone_key(organization, contact_identity)
        # A representative's card must not absorb coworkers' company calls.
        linked_clients = {contact_identity.contact.pk} if contact_identity.contact else ({contact_identity.client.pk} if contact_identity.client else set())
    else:
        linked_clients = {subject.contact_id} if subject.contact_id else {subject.client_id} - {None}
    client_ids = readable_clients(request.user).filter(organization=organization,
        pk__in=linked_clients).values("pk")
    if kind in {"all", "message"} and caps["can_view_conversations"]:
        source_messages = sources.filter(inbox__source_type="message").values("inbox__source_id")
        conversations = ConversationMessage.objects.filter(pk__in=source_messages,
            conversation__organization=organization).values("conversation_id")
        messages = ConversationMessage.objects.filter(
            Q(conversation_id__in=conversations) | Q(conversation__client_id__in=client_ids),
            conversation__organization=organization, conversation__connection__is_active=True,
            conversation__connection__channel__organization=organization,
            conversation__connection__channel__is_active=True, direction__in=["in", "out"],
        ).filter(Q(conversation__client__isnull=True) | Q(conversation__client__organization=organization))
        messages = messages.select_related("conversation__connection__channel", "sent_by").annotate(
            _activity_at=Coalesce("intake_occurred_at", "created_at"))
        candidates = _portion(messages, "_activity_at", RANK["message"], before)
        linked_messages = set(sources.filter(inbox__source_type="message", inbox__source_id__in=[message.pk for message in candidates])
            .values_list("inbox__source_id", flat=True))
        for message in candidates:
            channel = message.conversation.connection.channel
            title = ("Входящее" if message.direction == "in" else "Исходящее") + " сообщение · " + channel.get_kind_display()
            rows.append(_entry("message", message, message._activity_at, "message", title,
                body=message.body[:10000], truncated=len(message.body) > 10000,
                actor=message.sender_name or (_name(message.sent_by) if message.sent_by_id else ""),
                status=message.get_delivery_status_display(),
                relation="lead_source" if message.pk in linked_messages else "contact_history",
                origin="Источник этого лида" if message.pk in linked_messages else "Общая история контакта / диалога",
                url=reverse("communications_conversations") + "?" + urlencode({"org_id": organization.pk, "conversation": str(message.conversation.uuid)})))
    if kind in {"all", "call"} and (caps["can_view_all_calls"] or caps["can_view_own_calls"]):
        from .call_access import scope_calls
        from .crm_lead_access import _with_phone_key
        call_scope = Q(pk__in=sources.filter(inbox__source_type="call").values("inbox__source_id")) | Q(client_id__in=client_ids)
        call_scope |= (Q(work_context__lead_id__in=family) if isinstance(subject, CrmLead)
            else Q(work_context__deal_id=subject.pk))
        if phone_key:
            call_scope |= Q(client__isnull=True, _crm_phone_key=phone_key)
        calls = PhoneCall.objects.filter(
            organization=organization, source_kind=PhoneCall.SOURCE_TELEPHONY,
            direction__in=[PhoneCall.DIRECTION_IN, PhoneCall.DIRECTION_OUT],
            connection__organization=organization, connection__is_active=True,
        ).filter(Q(client__isnull=True) | Q(client__organization=organization)).filter(
            Q(employee_profile__isnull=True) | Q(employee_profile__organization=organization))
        calls = _with_phone_key(calls).filter(call_scope)
        calls = scope_calls(request.user, organization, calls)
        calls = _without_private_calls(calls).select_related("analysis", "employee", "employee_profile")
        candidates = _portion(calls, "started_at", RANK["call"], before)
        contacts = {contact.pk: contact for contact in readable_clients(request.user).filter(
            organization=organization, pk__in={call.client_id for call in candidates})}
        employee_ids = set(OrganizationAccess.objects.filter(organization=organization,
            user_id__in={call.employee_id for call in candidates}).values_list("user_id", flat=True))
        linked_calls = set(sources.filter(inbox__source_type="call", inbox__source_id__in=[call.pk for call in candidates])
            .values_list("inbox__source_id", flat=True))
        private = private_call_ids(candidates)
        for call in candidates:
            if call.pk in private:
                continue
            title = ("Пропущенный звонок" if call.result == PhoneCall.RESULT_MISSED else "Входящий звонок") if call.direction == "in" else "Исходящий звонок"
            contact = contacts.get(call.client_id)
            peer = " · ".join(value for value in (contact.name if contact else "Контакт не определён", call.phone_number) if value)
            staff = (call.employee_profile.display_name if call.employee_profile_id and call.employee_profile.organization_id == organization.pk else
                _name(call.employee) if call.employee_id in employee_ids else "") or "Сотрудник не определён"
            participants = (peer, staff) if call.direction == "in" else (staff, peer)
            media = {}
            from pool_service.communication_views import _authorize_call_access
            try:
                _authorize_call_access(request, call)
            except Http404:
                continue
            except PermissionDenied:
                pass
            else:
                if call.recording_file:
                    media["recording_url"] = reverse("communication_call_recording", args=[call.pk]) + "?" + urlencode({"org_id": organization.pk})
                analysis = getattr(call, "analysis", None)
                if analysis and analysis.status == CallAnalysis.STATUS_READY:
                    media.update(summary=analysis.summary[:4000], transcript=analysis.transcript[:20000])
                    facts = analysis.facts if isinstance(analysis.facts, dict) else {}
                    if isinstance(facts.get("request"), str):
                        media["facts"] = {"request": facts["request"][:2000]}
                if isinstance(subject, CrmLead) and call.pk in linked_calls and call.recording_file:
                    media["analysis_url"] = reverse("crm_live_lead_analysis", args=[subject.pk, call.pk]) + "?" + urlencode({"org_id": organization.pk})
                    media["analysis_status"] = analysis.status if analysis else "none"
            returned_calls.append(call)
            rows.append(_entry("call", call, call.started_at, "call", title, body=call.phone_number,
                details=[" → ".join(participants)],
                status=call.get_result_display() + " · " + str(call.duration_seconds) + " сек", media=media,
                relation="lead_source" if call.pk in linked_calls else "contact_history",
                origin="Источник этого лида" if call.pk in linked_calls else "Общая история контакта",
                url=reverse("communications_calls") + "?" + urlencode({"org_id": organization.pk, "call_id": call.pk})))
    if kind in {"all", "task"}:
        task_scope = Q(commercial_lead_link__lead_id__in=family, commercial_lead_link__organization=organization)
        if not isinstance(subject, CrmLead):
            task_scope |= Q(commercial_deal_link__deal=subject, commercial_deal_link__organization=organization)
        tasks = readable_tasks(ServiceTask.objects.filter(task_scope, organization=organization), request.user)
        changes = ServiceTaskChange.objects.filter(task_id__in=[task.pk for task in tasks]).select_related("task", "changed_by")
        candidates = _portion(changes, "created_at", RANK["task"], before)
        current_ids = {task.pk for task in readable_tasks(ServiceTask.objects.filter(
            pk__in={change.task_id for change in candidates}, organization=organization), request.user)}
        for change in candidates:
            if change.task_id not in current_ids:
                continue
            rows.append(_entry("task", change, change.created_at, "task", "Дело · " + change.get_action_display(),
                body=change.task.title, actor=_name(change.changed_by), status=change.task.get_status_display(),
                url=reverse("task_edit", args=[change.task_id])))
    # Permission revocation between split reads must not reveal derived history.
    if family != _family(request.user, organization, subject) or caps != _communication_caps(request.user, organization):
        raise Http404
    # Client-linked calls need their own final check: they may have no lead
    # source whose authorization would detect a newly private phone number.
    hidden_calls = {"call:" + str(pk) for pk in private_call_ids(returned_calls)}
    rows = [row for row in rows if row["id"] not in hidden_calls]
    rows.sort(key=lambda row: row["_key"], reverse=True)
    selected = rows[:PAGE_SIZE]
    cursor = None
    if len(rows) > PAGE_SIZE:
        stamp, rank, pk = selected[-1]["_key"]
        cursor = signing.dumps(identity + [stamp.isoformat(), rank, pk], salt="crm-card-activity-v1")
    for row in selected:
        row.pop("_key")
    return {"items": selected, "cursor": cursor, "type": kind,
        "scope_note": "События карточки и исходного лида; общение — из связанных диалогов и контактов. Показаны только доступные вам записи."}
