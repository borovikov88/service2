"""CRM-aware communication screens, reusing recording/export services.

Phone history is resolved on read. This module never reassigns historical calls
or changes the external-system, recording, transcription or financial data.
"""
from copy import copy
import re
from urllib.parse import urlencode

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core import signing
from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.db.models import Q
from django.http import Http404, HttpResponseBadRequest, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_GET, require_http_methods

from . import communication_views as legacy
from .client_crm_models import ClientContact, ClientCRMProfile
from .client_crm_ui import (
    ClientEditorForm, CRMProfileEditorForm, LOOKUP_LIMIT, search_tokens,
    sync_primary_contact,
)
from .client_crm_views import _can_manage_import
from .client_phone_matching import clients_by_phone, clients_by_phones
from .client_queries import active_clients
from .communication_models import PhoneCall
from .communication_services import conversation_capability, organization_access
from .models import Client, Organization, OrganizationAccess
from .phone_utils import canonical_phone_value, format_phone, normalize_phone
from .services.megafon_internal_calls import (
    internal_call_sync_due,
    start_call_recording_sync_worker,
)
from .services.permissions import company_has_access


RETURN_SALT = "service2.call-client-return.v1"
RETURN_FIELDS = ("date_from", "date_to", "client", "employee", "direction", "missed", "q")


def _scope(user, source_kind, organization=None, *, export=False):
    if not user.is_authenticated or not user.is_active:
        raise PermissionDenied
    access = organization_access(user, organization)
    if not access:
        raise PermissionDenied
    organization = access.organization
    if source_kind == PhoneCall.SOURCE_UPLOADED:
        if not legacy._can_access_manual_recordings(user, organization):
            raise PermissionDenied
        return organization, True
    if source_kind != PhoneCall.SOURCE_TELEPHONY:
        raise Http404
    view_all = conversation_capability(user, "can_view_all_calls", organization)
    if not (view_all or conversation_capability(user, "can_view_own_calls", organization)):
        raise PermissionDenied
    if export and not conversation_capability(user, "can_listen_calls", organization):
        raise PermissionDenied
    return organization, view_all


def _can_create(user, organization):
    return bool(
        not user.is_superuser
        and _can_manage_import(user, organization.pk)
        and company_has_access(organization)
    )


def resolved_calls_for_client(queryset, client):
    """Apply exact shared phone identity before slicing, never suffix matching.

    Explicit assignments take priority. Unassigned calls enter a client's
    history only when its current phone has exactly one active CRM match.
    Call rows are not updated by a GET request. Internal calls remain employee
    conversations, not CRM history, even if their number resembles a client.
    """
    queryset = queryset.filter(organization_id=client.organization_id).exclude(
        direction=PhoneCall.DIRECTION_INTERNAL,
    )
    keys = {normalize_phone(client.phone)}
    keys.update(
        normalize_phone(value)
        for value in ClientContact.objects.filter(
            client=client, kind=ClientContact.KIND_PHONE,
        ).values_list("value", flat=True)
    )
    keys.discard("")
    matches = clients_by_phones(client.organization, keys, limit=2)
    unique_keys = {
        key for key, items in matches.items()
        if len(items) == 1 and items[0].pk == client.pk
    }
    condition = Q(client_id=client.pk)
    if unique_keys:
        raw_numbers = queryset.filter(client__isnull=True).order_by().values_list(
            "phone_number", flat=True,
        ).distinct()
        matched_numbers = [
            value for value in raw_numbers.iterator(chunk_size=1000)
            if normalize_phone(value) in unique_keys
        ]
        if matched_numbers:
            condition |= Q(client__isnull=True, phone_number__in=matched_numbers)
    return queryset.filter(condition)


def _selected_client(request, organization):
    value = (request.GET.get("client") or "").strip()
    if not value:
        return None
    if not value.isascii() or not value.isdigit() or len(value) > 18:
        raise ValueError("Некорректный клиент.")
    client = active_clients(Client.objects.filter(
        organization=organization, pk=int(value),
    )).select_related("organization").first()
    if client is None:
        raise ValueError("Некорректный клиент.")
    return client


def _filtered_calls(request, source_kind, organization, view_all):
    queryset = PhoneCall.objects.filter(organization=organization, source_kind=source_kind)
    if source_kind == PhoneCall.SOURCE_TELEPHONY and not view_all:
        queryset = queryset.filter(Q(employee=request.user) | Q(peer_employee=request.user))
    selected = _selected_client(request, organization)
    # Keep existing date, employee, direction, missed and text semantics, but
    # replace the former client suffix filter with the exact resolver above.
    filtered_request = copy(request)
    filtered_request.GET = request.GET.copy()
    filtered_request.GET.pop("client", None)
    if source_kind == PhoneCall.SOURCE_UPLOADED:
        queryset = legacy._filter_uploaded_audio(filtered_request, queryset, organization)
    else:
        queryset = legacy._filter_telephony_calls(filtered_request, queryset, organization, view_all)
    if selected:
        queryset = resolved_calls_for_client(queryset, selected)
    return queryset.order_by("-started_at", "-pk"), selected


def _return_token(request, organization, source_kind):
    values = {
        key: request.GET.get(key, "")[:256]
        for key in RETURN_FIELDS if request.GET.get(key)
    }
    return signing.dumps({
        "user": request.user.pk, "organization": organization.pk,
        "source": source_kind, "filters": values,
    }, salt=RETURN_SALT, compress=True)


def _return_url(request, call, token):
    values = {}
    if token:
        data = signing.loads(token, salt=RETURN_SALT, max_age=7200)
        if (
            not isinstance(data, dict) or data.get("user") != request.user.pk
            or data.get("organization") != call.organization_id
            or data.get("source") != call.source_kind
            or not isinstance(data.get("filters"), dict)
        ):
            raise signing.BadSignature("Wrong return context")
        values = {
            key: str(data["filters"][key])[:256]
            for key in RETURN_FIELDS if key in data["filters"]
        }
    name = "communication_manual_recordings" if call.source_kind == PhoneCall.SOURCE_UPLOADED else "communications_calls"
    url = reverse(name)
    return url + ("?" + urlencode(values) if values else "")


def _decorate_calls(rows, request, organization, source_kind):
    phone_matches = clients_by_phones(organization, [
        call.phone_number for call in rows
        if not call.client_id and call.direction != PhoneCall.DIRECTION_INTERNAL
    ])
    create_allowed = _can_create(request.user, organization)
    token = _return_token(request, organization, source_kind) if create_allowed else ""
    for call in rows:
        call.phone_display = format_phone(call.phone_number)
        call.create_client_url = ""
        if call.direction == PhoneCall.DIRECTION_INTERNAL:
            call.resolved_client = None
            call.ambiguous_clients = []
            continue
        if call.client_id:
            # Do not expose a corrupt cross-organization assignment or replace it.
            call.resolved_client = call.client if call.client.organization_id == organization.pk else None
            call.ambiguous_clients = []
            continue
        matches = phone_matches.get(normalize_phone(call.phone_number), [])
        call.resolved_client = matches[0] if len(matches) == 1 else None
        call.ambiguous_clients = matches if len(matches) > 1 else []
        if create_allowed and not matches and normalize_phone(call.phone_number):
            call.create_client_url = reverse("communication_call_client_create", args=[call.pk]) + "?" + urlencode({"back": token})


def _call_screen(request, source_kind):
    organization, view_all = _scope(request.user, source_kind)
    if source_kind == PhoneCall.SOURCE_UPLOADED:
        legacy._wake_uploaded_audio_worker_if_needed(organization, include_fresh_pending=True)
    elif internal_call_sync_due(organization):
        start_call_recording_sync_worker(limit=100)
    try:
        queryset, selected = _filtered_calls(request, source_kind, organization, view_all)
    except ValueError as exc:
        return HttpResponseBadRequest(str(exc))
    rows = list(queryset.select_related(
        "employee", "employee_profile", "peer_employee", "peer_employee_profile", "client", "analysis",
    ).defer("analysis__transcript")[:500])
    _decorate_calls(rows, request, organization, source_kind)
    manual = source_kind == PhoneCall.SOURCE_UPLOADED
    return render(request, "pool_service/communications/calls.html", {
        "active_tab": "communications", "calls": rows,
        "employees": OrganizationAccess.objects.filter(organization=organization).select_related("user"),
        "clients": [selected] if selected else [], "selected_client": selected,
        "client_lookup_url": reverse("communication_client_lookup", args=[source_kind]),
        "can_listen": manual or conversation_capability(request.user, "can_listen_calls", organization),
        "can_view_all": view_all,
        "can_access_manual_recordings": legacy._can_access_manual_recordings(request.user, organization),
        "manual_archive": manual,
    })


@login_required
@require_GET
def calls(request):
    return _call_screen(request, PhoneCall.SOURCE_TELEPHONY)


@login_required
@require_GET
def manual_recordings(request):
    return _call_screen(request, PhoneCall.SOURCE_UPLOADED)


@login_required
@require_GET
def call_transcripts_export(request, source_kind):
    export_format = (request.GET.get("format") or "txt").lower()
    if export_format not in {"txt", "csv", "docx"}:
        return HttpResponseBadRequest("Неизвестный формат выгрузки.")
    organization, view_all = _scope(request.user, source_kind, export=True)
    try:
        queryset, _selected = _filtered_calls(request, source_kind, organization, view_all)
    except ValueError as exc:
        return HttpResponseBadRequest(str(exc))
    return legacy._transcript_export_response(
        legacy._transcript_export_records(queryset, organization), export_format, source_kind,
    )


@login_required
@require_GET
def client_lookup(request, source_kind):
    organization, _view_all = _scope(request.user, source_kind)
    query = request.GET.get("q", "")
    tokens = search_tokens(query)
    rows = []
    if len(query) <= 160 and len(tokens) <= 12 and sum(map(len, tokens)) >= 3:
        condition = Q()
        fields = (
            "name", "company_name", "first_name", "last_name", "inn", "phone", "email",
            "crm_profile__legal_name", "crm_profile__middle_name",
            "crm_contacts__value", "crm_contacts__match_value",
        )
        for token in tokens:
            pattern = re.escape(token).replace("е", "[её]")
            clause = Q()
            for field in fields:
                clause |= Q(**{field + "__iregex": pattern})
            condition &= clause
        if normalize_phone(query):
            exact = clients_by_phone(organization, query, limit=LOOKUP_LIMIT + 1)
            condition |= Q(pk__in=[item.pk for item in exact])
        rows = list(active_clients(Client.objects.filter(organization=organization)).filter(
            condition,
        ).distinct().order_by("name", "id")[:LOOKUP_LIMIT + 1])
    response = JsonResponse({
        "results": [{"id": item.pk, "name": item.name, "phone": format_phone(item.phone),
                     "inn": item.inn or "", "client_type": item.client_type}
                    for item in rows[:LOOKUP_LIMIT]],
        "has_more": len(rows) > LOOKUP_LIMIT,
    })
    response["Cache-Control"] = "private, no-store"
    return response


def _visible_call(request, call_id, *, lock=False):
    queryset = PhoneCall.objects.all()
    if lock:
        queryset = queryset.select_for_update()
    call = get_object_or_404(queryset, pk=call_id)
    organization, view_all = _scope(request.user, call.source_kind, call.organization)
    if call.direction == PhoneCall.DIRECTION_INTERNAL:
        raise PermissionDenied
    if (
        call.source_kind == PhoneCall.SOURCE_TELEPHONY and not view_all
        and request.user.pk not in (call.employee_id, call.peer_employee_id)
    ):
        raise PermissionDenied
    if not _can_create(request.user, organization):
        raise PermissionDenied
    return call


@login_required
@require_http_methods(["GET", "POST"])
def call_client_create(request, call_id):
    call = _visible_call(request, call_id)
    if not normalize_phone(call.phone_number):
        return HttpResponseBadRequest("В звонке нет корректного номера телефона.")
    values = request.POST if request.method == "POST" else request.GET
    kind = values.get("client_type", "private")
    if kind not in {"private", "legal"}:
        return HttpResponseBadRequest("Некорректный тип клиента.")
    token = values.get("back", "")
    try:
        return_url = _return_url(request, call, token)
    except signing.BadSignature:
        return HttpResponseBadRequest("Ссылка возврата устарела. Откройте создание из списка звонков ещё раз.")
    if call.client_id and call.client.organization_id != call.organization_id:
        raise PermissionDenied
    # PhoneCall allows 40 characters, but Client.phone only allows 20. The
    # normalized identity keeps international country codes without separators;
    # Russian identities retain the agreed display format. Do not change the call.
    phone = canonical_phone_value(normalize_phone(call.phone_number))
    customer = Client(organization=call.organization, client_type=kind, phone=phone)
    profile = ClientCRMProfile(
        client=customer, source=ClientCRMProfile.SOURCE_MANUAL,
        legal_form=ClientCRMProfile.LEGAL_FORM_ENTITY if kind == "legal" else ClientCRMProfile.LEGAL_FORM_NONE,
    )
    posted = request.POST if request.method == "POST" else None
    client_form = ClientEditorForm(posted, instance=customer)
    # A caller cannot create an unrelated phone/client by modifying this POST.
    client_form.fields["phone"].disabled = True
    profile_form = CRMProfileEditorForm(posted, instance=profile, client=customer, prefix="profile")
    existing = [call.client] if call.client_id else clients_by_phone(call.organization, call.phone_number)
    if request.method == "POST":
        client_valid = client_form.is_valid()
        profile_valid = profile_form.is_valid()
        if client_valid and profile_valid:
            with transaction.atomic():
                # Serialize quick-create requests within this tenant, including
                # two different historical calls with the same unknown phone.
                Organization.objects.select_for_update().get(pk=call.organization_id)
                call = _visible_call(request, call_id, lock=True)
                if call.client_id and call.client.organization_id != call.organization_id:
                    raise PermissionDenied
                existing = [call.client] if call.client_id else clients_by_phone(call.organization, call.phone_number)
                if not existing:
                    # The form's initial phone came from the earlier read. Refuse
                    # if the call changed while the form was being validated.
                    if normalize_phone(client_form.cleaned_data["phone"]) != normalize_phone(call.phone_number):
                        return HttpResponseBadRequest("Номер звонка изменился. Откройте форму повторно.")
                    saved = client_form.save(commit=False)
                    if kind == "legal":
                        saved.company_name = saved.name
                    saved.save()
                    profile_form.save()
                    sync_primary_contact(saved, ClientContact.KIND_PHONE, "", saved.phone)
                    sync_primary_contact(saved, ClientContact.KIND_EMAIL, "", saved.email)
                    messages.success(request, "Клиент создан. Ранее поступившие звонки с этим номером теперь подписываются автоматически.")
                    return redirect(return_url)
        if existing:
            messages.info(request, "Номер уже есть в CRM. Новая карточка не создана.")
    base_url = reverse("communication_call_client_create", args=[call.pk])
    return render(request, "pool_service/communications/client_from_call.html", {
        "call": call, "phone_display": format_phone(call.phone_number), "client_type": kind,
        "client_form": client_form, "profile_form": profile_form, "existing_matches": existing,
        "return_url": return_url, "back": token,
        "person_url": base_url + "?" + urlencode({"back": token, "client_type": "private"}),
        "company_url": base_url + "?" + urlencode({"back": token, "client_type": "legal"}),
        "active_tab": "communications", "page_title": "Создать клиента из звонка",
        "show_search": False, "show_add_button": False,
    })
