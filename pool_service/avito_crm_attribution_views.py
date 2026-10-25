"""Manager UI for explicit links to existing CRM cards."""
import uuid
from urllib.parse import urlencode

from django import forms
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied, ValidationError
from django.db.models import Q
from django.http import HttpResponseBadRequest, JsonResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET, require_http_methods

from . import crm_source_links as links
from . import crm_source_orders as orders
from .communication_models import Conversation, PhoneCall
from .crm_source_models import CrmSourceLink
from .models import ServiceTask, DataAuditLog
from .services.call_privacy import visible_calls


class LinkForm(forms.Form):
    source_kind = forms.ChoiceField(choices=[("conversation", "Диалог"), ("call", "Звонок")], widget=forms.HiddenInput)
    source_id = forms.CharField(max_length=64, widget=forms.HiddenInput)
    item_id = forms.IntegerField(required=False, min_value=1, widget=forms.Select, label="Существующая заявка CRM")
    quality = forms.ChoiceField(choices=CrmSourceLink.QUALITY_CHOICES, label="Качество обращения")
    reason = forms.CharField(required=False, max_length=500, label="Причина / пояснение менеджера",
        widget=forms.Textarea(attrs={"rows": 2}))
    next_task_id = forms.IntegerField(required=False, min_value=1, widget=forms.Select, label="Следующий шаг: существующая задача")
    ad_mode = forms.ChoiceField(choices=[("preserve", "Сохранить прежнее подтверждение"),
        ("auto", "Проверить сохранённый контекст Авито"), ("manual", "Подтвердить выбранное объявление вручную"),
        ("unknown", "Источник объявления неизвестен")], label="Источник объявления")
    manual_ad_id = forms.CharField(required=False, max_length=64, widget=forms.Select, label="Объявление для ручного подтверждения")
    revision = forms.IntegerField(min_value=0, widget=forms.HiddenInput)
    operation = forms.UUIDField(widget=forms.HiddenInput)
    call_origin = forms.ChoiceField(choices=[("preserve", "Сохранить прежнее состояние"), ("unknown", "Источник звонка неизвестен"), ("manual", "Подтверждаю, что звонок пришёл с Авито")], label="Происхождение звонка")
    order_action = forms.ChoiceField(choices=[("preserve", "Сохранить прежнюю финансовую ссылку"), ("set", "Подтвердить наблюдаемый документ вручную"), ("clear", "Снять ссылку")], label="Наблюдаемый документ заказа 1С")
    order_guid = forms.CharField(required=False, max_length=36, widget=forms.Select, label="Документ текущего источника 1С")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for field in self.fields.values():
            if not isinstance(field.widget, forms.HiddenInput):
                field.widget.attrs["class"] = "form-select" if isinstance(field.widget, forms.Select) else "form-control"


def _selected(request):
    data = request.POST if request.method == "POST" else request.GET
    if request.method == "POST":
        return data.get("source_kind"), data.get("source_id")
    if data.get("conversation"):
        return "conversation", data.get("conversation")
    if data.get("call"):
        return "call", data.get("call")
    return None, None


def _url(connection, kind, source):
    return reverse("avito_crm_attribution", args=[connection.pk]) + "?" + urlencode({
        "conversation" if kind == "conversation" else "call": str(source.uuid) if kind == "conversation" else source.pk})


def _choice_rows(actor, connection, query="", selected=None):
    eligible = links.requests_for(actor, connection)
    queryset = eligible
    query = query.strip()[:100]
    if query:
        queryset = queryset.filter(title__icontains=query)
    rows = list(queryset.order_by("-updated_at", "-pk")[:20])
    if selected and eligible.filter(pk=selected.pk).exists() and all(item.pk != selected.pk for item in rows):
        rows.insert(0, selected)
    return [("", "Не связано / снять связь")] + [
        (str(item.pk), "#" + str(item.pk) + " · " + item.title[:120]) for item in rows]


def _history(link, organization, can_finance):
    if link is None:
        return []
    labels = {"crm_item_id": "заявка", "next_task_id": "следующий шаг",
        "quality": "качество обращения", "reason_sha256": "пояснение менеджера",
        "source_evidence_sha256": "подтверждение источника обращения",
        "ad_confirmed_by_id": "автор подтверждения объявления", "ad_confirmed_at": "время подтверждения объявления",
        "ad_source": "способ подтверждения объявления", "avito_item_id": "объявление",
        "call_origin": "происхождение звонка",
        "order_guid_sha256": "ссылка на документ заказа",
        "order_evidence_sha256": "подтверждение финансового источника",
        "order_snapshot_id": "снимок финансового источника",
        "order_confirmed_by_id": "автор финансового подтверждения",
        "order_confirmed_at": "время финансового подтверждения"}
    rows = []
    for event in DataAuditLog.objects.filter(entity_type="crm_source_link",
            entity_id=str(link.pk), organization=organization).order_by("-pk")[:20]:
        fields = [labels[key] for key in event.changed_fields if key in labels
            and (can_finance or not key.startswith("order_"))]
        if fields:
            rows.append({"created_at": event.created_at, "actor_id": event.actor_id,
                "changed_fields": fields})
    return rows


@login_required
@never_cache
@require_GET
def request_lookup(request, connection_id):
    connection = links.scope(request.user, connection_id)
    kind, identifier = _selected(request)
    if kind is None:
        raise PermissionDenied
    links.source_for(request.user, connection, kind, identifier)
    choices = _choice_rows(request.user, connection, request.GET.get("q", ""))
    return JsonResponse({"results": [{"id": value, "text": label} for value, label in choices[1:]],
        "limited": len(choices) >= 21})


@login_required
@never_cache
@require_http_methods(["GET", "POST"])
def attribution(request, connection_id):
    connection = links.scope(request.user, connection_id)
    kind, identifier = _selected(request)
    source = links.source_for(request.user, connection, kind, identifier) if kind else None
    link = links.visible_link(request.user, connection, kind, source) if source else None
    can_finance = orders.allowed(request.user, connection.channel.organization)
    if request.method == "POST" and not can_finance and (request.POST.get("order_action", "preserve") != "preserve" or request.POST.get("order_guid")):
        raise PermissionDenied
    existing_hidden = bool(source and links.link_for(connection, kind, source) and link is None)
    can_edit = False
    if source:
        try:
            links.source_for(request.user, connection, kind, identifier, edit=True)
            can_edit = not existing_hidden
        except PermissionDenied:
            pass
    initial = {"source_kind": kind, "source_id": identifier,
        "item_id": link.crm_item_id if link else None, "quality": link.quality if link else "unknown",
        "reason": link.reason if link else "", "next_task_id": link.next_task_id if link else None,
        "ad_mode": "preserve" if link else "auto", "manual_ad_id": "",
        "revision": link.revision if link else 0, "operation": uuid.uuid4(),
        "call_origin": "preserve" if link else "unknown", "order_action": "preserve", "order_guid": ""}
    form = LinkForm(request.POST if request.method == "POST" else None, initial=initial)
    item = links.item_for(request.user, connection, link.crm_item_id) if link and link.crm_item_id else None
    posted_item = request.POST.get("item_id", "")
    if request.method == "POST" and posted_item.isascii() and posted_item.isdigit() and len(posted_item) <= 18:
        item = links.item_for(request.user, connection, int(posted_item))
    form.fields["item_id"].widget.choices = _choice_rows(
        request.user, connection, request.GET.get("request_q", ""), item)
    tasks = list(ServiceTask.objects.filter(organization=connection.channel.organization,
        crm_item=item, is_archived=False).select_related("parent_task").order_by("due_at", "pk")[:100]) if item else []
    tasks = [task for task in tasks if links.task_allowed(request.user, task, item, connection.channel.organization_id)]
    if link and link.next_task_id and all(task.pk != link.next_task_id for task in tasks):
        if links.task_allowed(request.user, link.next_task, item, connection.channel.organization_id):
            tasks.insert(0, link.next_task)
        else:
            form.fields["next_task_id"].help_text = "Прежний шаг сейчас недоступен. Его содержимое скрыто, ссылка сохранится."
    form.fields["next_task_id"].widget.choices = [("", "Не выбран")] + [
        (str(task.pk), "#" + str(task.pk) + " · " + task.title[:100]) for task in tasks]
    ads = links.owned_ads(connection)
    form.fields["manual_ad_id"].widget.choices = [("", "Не выбрано")] + [(value, value) for value in sorted(ads)]
    if kind != "call":
        form.fields.pop("call_origin")
    if can_finance:
        observed = orders.candidates(request.user, connection.channel.organization, item) or []
        form.fields["order_guid"].help_text = "До 50 наблюдаемых документов клиента; это не полный реестр заказов или оплат."
        form.fields["order_guid"].widget.choices = [("", "Не выбрано")] + [(str(row["guid"]), (row["number"] or str(row["guid"])) + " · " + (row["date"] or "дата неизвестна")) for row in observed]
    else:
        form.fields.pop("order_action")
        form.fields.pop("order_guid")
    status = 200
    if request.method == "POST":
        if source is None or not can_edit:
            raise PermissionDenied
        if form.is_valid():
            data = form.cleaned_data
            try:
                links.save_link(request.user, connection_id, data["source_kind"], data["source_id"],
                    item_id=data["item_id"], task_id=data["next_task_id"],
                    quality=data["quality"], reason=data["reason"], ad_mode=data["ad_mode"],
                    manual_ad_id=data["manual_ad_id"], call_origin=data.get("call_origin", "preserve"),
                    order_action=data.get("order_action", "preserve"), order_guid=data.get("order_guid", ""),
                    expected_revision=data["revision"],
                    operation=data["operation"])
            except links.LinkConflict as exc:
                form.add_error(None, str(exc))
                status = 409
            except (ValueError, ValidationError):
                form.add_error(None, "Проверьте выбранные ссылки, источник и версию. Связь не сохранена.")
                status = 400
            else:
                return redirect(_url(connection, kind, source))
        else:
            status = 400
    dialogs = []
    from .communication_services import conversation_capability
    if conversation_capability(request.user, "can_view_conversations", connection.channel.organization):
        for row in Conversation.objects.filter(connection=connection,
                organization=connection.channel.organization).order_by("-last_message_at", "-pk")[:50]:
            try:
                links.source_for(request.user, connection, "conversation", row.uuid)
            except PermissionDenied:
                continue
            dialogs.append({"label": row.display_name, "url": _url(connection, "conversation", row),
                "last_message_at": row.last_message_at})
    call_query = PhoneCall.objects.filter(organization=connection.channel.organization,
        source_kind=PhoneCall.SOURCE_TELEPHONY).exclude(direction=PhoneCall.DIRECTION_INTERNAL)
    if not conversation_capability(request.user, "can_view_all_calls", connection.channel.organization):
        if conversation_capability(request.user, "can_view_own_calls", connection.channel.organization):
            call_query = call_query.filter(Q(employee=request.user) | Q(peer_employee=request.user))
        else:
            call_query = call_query.none()
    calls = []
    for call in visible_calls(list(call_query.filter(connection__organization=connection.channel.organization).order_by("-started_at", "-pk")[:50])):
        try:
            links.source_for(request.user, connection, "call", call.pk)
        except PermissionDenied:
            continue
        calls.append({"id": call.pk, "started_at": call.started_at, "url": _url(connection, "call", call)})
    from .avito_owner_review import water_quality_queue
    try:
        quality_queue = water_quality_queue(request.user, connection)
    except PermissionDenied:
        quality_queue = {'state': 'unavailable'}
    from .views import CRM_STAGE_LABELS
    order_state = orders.state(request.user, connection.channel.organization, link, item)
    order_fetched_at = parse_datetime(order_state.get("fetched_at", "")) if order_state.get("fetched_at") else None
    return render(request, "pool_service/avito/attribution.html", {
        "active_tab": "communications", "connection": connection, "form": form if source else None,
        "source": source, "source_kind": kind, "can_edit": can_edit, "link": link, "crm_item": item,
        "can_finance": can_finance, "existing_hidden": existing_hidden,
        "order_state": order_state, "order_fetched_at": order_fetched_at,
        "crm_stage_label": CRM_STAGE_LABELS.get(item.stage, item.stage) if item else "",
        "history": _history(link, connection.channel.organization, can_finance),
        "dialogs": dialogs, "calls": calls, "coverage": links.coverage(request.user, connection),
        "water_quality_queue": quality_queue,
        "lookup_url": reverse("avito_crm_request_lookup", args=[connection.pk]) + "?" + urlencode({
            "conversation" if kind == "conversation" else "call": identifier}) if source else "",
        "quality_note": "Оценка менеджера не является результатом оплаты или уникальным клиентом.",
    }, status=status)
