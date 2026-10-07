"""CRM card UI helpers. No import/merge or external-system writes."""
import re
import unicodedata

from django import forms
from django.contrib import messages
from django.contrib.auth import get_user_model
from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.db.models import Q
from django.http import HttpResponseForbidden, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_http_methods

from .client_crm_models import ClientCompanyLink, ClientContact, ClientCRMProfile
from .client_queries import active_clients
from .models import Client
from .phone_utils import canonical_phone_value, format_phone, normalize_phone
from .services.permissions import is_org_access_blocked


CARD_TABS = ("overview", "contacts", "objects", "tasks", "calls")
LOOKUP_LIMIT = 20


def card_tab(value):
    return value if value in CARD_TABS else "overview"


def card_url(client_id, tab="overview"):
    return reverse("client_detail", args=[client_id]) + "?tab=" + card_tab(tab)


def search_tokens(value):
    text = unicodedata.normalize("NFKC", str(value or "")).casefold().replace("ё", "е")
    return re.findall(r"[^\W_]+", text, flags=re.UNICODE)


def relationship_results(client, query):
    """Called only after the card view's existing manage-permission check."""
    tokens = search_tokens(query)
    if not client.organization_id or len(str(query or "")) > 160 or len(tokens) > 12 or sum(map(len, tokens)) < 3:
        return JsonResponse({"results": [], "has_more": False})
    expected_type = "private" if client.client_type == "legal" else "legal"
    queryset = active_clients(Client.objects.filter(
        organization_id=client.organization_id, client_type=expected_type,
    ))
    if client.client_type == "legal":
        linked = ClientCompanyLink.objects.filter(company=client).values("person_id")
    else:
        linked = ClientCompanyLink.objects.filter(person=client).values("company_id")
    queryset = queryset.exclude(pk__in=linked)
    fields = (
        "name", "company_name", "first_name", "last_name", "inn", "phone", "email",
        "crm_profile__legal_name", "crm_profile__middle_name",
        "crm_contacts__value", "crm_contacts__match_value",
    )
    query_filter = Q()
    for token in tokens:
        # Escaped literal tokens only: no user-controlled regular expression.
        # iregex also handles Cyrillic case on SQLite (unlike its LIKE/LOWER).
        pattern = re.escape(token).replace("е", "[её]")
        clause = Q()
        for field in fields:
            clause |= Q(**{field + "__iregex": pattern})
        query_filter &= clause
    phone_key = normalize_phone(query)
    if phone_key:
        # Scope first, then normalize, then cap the final result. Capping a
        # general phone lookup first could hide eligible people behind many
        # company matches (or behind contacts already linked to this card).
        legacy_ids = [
            pk for pk, phone in queryset.exclude(phone__isnull=True).exclude(phone="")
            .values_list("pk", "phone").iterator(chunk_size=500)
            if normalize_phone(phone) == phone_key
        ]
        contact_ids = ClientContact.objects.filter(
            client_id__in=queryset.values("pk"), kind=ClientContact.KIND_PHONE,
            match_value=phone_key,
        ).values("client_id")
        query_filter |= Q(pk__in=legacy_ids) | Q(pk__in=contact_ids)
    rows = list(queryset.filter(query_filter).distinct().order_by("name", "id")[:LOOKUP_LIMIT + 1])
    response = JsonResponse({
        "results": [
            {"id": item.pk, "name": item.name, "phone": format_phone(item.phone),
             "inn": item.inn or "", "client_type": item.client_type}
            for item in rows[:LOOKUP_LIMIT]
        ],
        "has_more": len(rows) > LOOKUP_LIMIT,
    })
    response["Cache-Control"] = "private, no-store"
    return response


class ClientEditorForm(forms.ModelForm):
    class Meta:
        model = Client
        fields = ("name", "last_name", "first_name", "phone", "email", "inn", "contact_position")
        labels = {
            "name": "Имя в CRM", "last_name": "Фамилия", "first_name": "Имя",
            "phone": "Основной телефон", "email": "Основной email",
            "inn": "ИНН", "contact_position": "Должность",
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["name"].required = True
        self.fields["phone"].required = False
        self.fields["last_name"].required = False
        self.fields["first_name"].required = False
        if self.instance.client_type == "legal":
            self.fields["name"].label = "Краткое название компании"
            for name in ("last_name", "first_name", "contact_position"):
                self.fields.pop(name)
        else:
            self.fields["name"].help_text = "Название карточки, в том числе привычное имя или примечание."
        for field in self.fields.values():
            field.widget.attrs["class"] = "form-control"
        self.fields["phone"].widget.attrs.update({"inputmode": "tel", "autocomplete": "tel"})

    def clean_phone(self):
        value = (self.cleaned_data.get("phone") or "").strip()
        if value and not normalize_phone(value):
            raise forms.ValidationError("Укажите корректный номер телефона.")
        if value == (self.initial.get("phone") or ""):
            return value
        return canonical_phone_value(value) if value else ""


class CRMProfileEditorForm(forms.ModelForm):
    class Meta:
        model = ClientCRMProfile
        fields = (
            "middle_name", "birth_date", "legal_name", "kpp", "ogrn",
            "manager", "responsible", "notes",
        )
        labels = {
            "middle_name": "Отчество", "birth_date": "Дата рождения",
            "legal_name": "Полное наименование", "kpp": "КПП",
            "ogrn": "ОГРН / ОГРНИП", "manager": "Менеджер клиента",
            "responsible": "Ответственный", "notes": "Заметки и договорённости",
        }
        widgets = {
            "birth_date": forms.DateInput(format="%Y-%m-%d", attrs={"type": "date"}),
            "notes": forms.Textarea(attrs={"rows": 5}),
        }

    def __init__(self, *args, client, **kwargs):
        from .client_crm_views import CLIENT_CARD_ROLES
        super().__init__(*args, **kwargs)
        self.fields["birth_date"].input_formats = ["%Y-%m-%d"]
        if client.client_type == "legal":
            for name in ("middle_name", "birth_date"):
                self.fields.pop(name)
        else:
            for name in ("legal_name", "kpp", "ogrn"):
                self.fields.pop(name)
        staff = get_user_model().objects.filter(
            organizationaccess__organization_id=client.organization_id,
            organizationaccess__role__in=CLIENT_CARD_ROLES,
        ).distinct()
        existing = [self.instance.manager_id, self.instance.responsible_id]
        staff = staff.filter(Q(is_active=True) | Q(pk__in=[pk for pk in existing if pk]))
        for name in ("manager", "responsible"):
            self.fields[name].queryset = staff.order_by("last_name", "first_name", "username")
            self.fields[name].empty_label = "Не назначен"
        for field in self.fields.values():
            field.widget.attrs["class"] = "form-select" if isinstance(field.widget, forms.Select) else "form-control"


def complete_form_data(data, form):
    """Partial/older POSTs must not silently clear fields absent from the form."""
    result = data.copy()
    for name, field in form.fields.items():
        key = form.add_prefix(name)
        if key not in result:
            value = form.initial.get(name, field.initial)
            result[key] = "" if value is None else value
    return result


def _contact_key(kind, value):
    return normalize_phone(value) if kind == ClientContact.KIND_PHONE else (value or "").strip().casefold()


def sync_primary_contact(client, kind, previous, value):
    """Retire only a replaced manual primary; keep unrelated and 1C contacts."""
    contacts = list(ClientContact.objects.select_for_update().filter(
        client=client, kind=kind,
    ).order_by("-is_primary", "id"))
    key = _contact_key(kind, value)
    old_key = _contact_key(kind, previous)
    matches = [item for item in contacts if key and _contact_key(kind, item.value) == key]
    target = next((item for item in matches if item.value == value), None)
    target = target or (matches[0] if matches else None)
    for item in contacts:
        if target and item.pk == target.pk:
            continue
        sources = list(item.sources or [])
        replaced_primary = (
            old_key != key and item.is_primary
            and _contact_key(kind, item.value) == old_key and "manual" in sources
        )
        if replaced_primary:
            sources.remove("manual")
            if not sources:
                item.delete()
                continue
            item.sources = sources
        if item.is_primary or replaced_primary:
            item.is_primary = False
            item.save(update_fields=["is_primary", "sources", "updated_at"])
    if not key:
        return
    if target is None:
        ClientContact.objects.create(
            client=client, kind=kind, value=value, match_value=key,
            label="Основной", is_primary=True, sources=["manual"],
        )
    else:
        sources = list(target.sources or [])
        if previous != value and "manual" not in sources:
            sources.append("manual")
        target.value = value
        target.match_value = key
        target.is_primary = True
        target.sources = sources
        target.save(update_fields=["value", "match_value", "is_primary", "sources", "updated_at"])


@login_required
@require_http_methods(["GET", "POST"])
def client_edit(request, client_id):
    # Reuse the original superuser read-only rule; account/1C identities are not editable here.
    from .views import _deny_superuser_write, client_edit as legacy_client_edit
    from .client_crm_views import _can_manage_import
    readonly = _deny_superuser_write(request)
    if readonly:
        return readonly
    with transaction.atomic():
        queryset = Client.objects.select_related("organization")
        if request.method == "POST":
            queryset = queryset.select_for_update()
        client = get_object_or_404(queryset, pk=client_id)
        if client.organization_id is None:
            return legacy_client_edit(request, client_id)
        # Check the target tenant, not the user's first membership.
        if not _can_manage_import(request.user, client.organization_id):
            return HttpResponseForbidden()
        if request.method == "POST" and is_org_access_blocked(request.user):
            return HttpResponseForbidden()
        profiles = ClientCRMProfile.objects.filter(client=client)
        if request.method == "POST":
            profiles = profiles.select_for_update()
        profile = profiles.first() or ClientCRMProfile(client=client)
        tab = card_tab(request.POST.get("return_tab") if request.method == "POST" else request.GET.get("tab"))
        if profile.merged_into_id:
            return redirect(card_url(profile.merged_into_id, tab))
        previous_phone, previous_email = client.phone, client.email
        client_form = ClientEditorForm(instance=client)
        profile_form = CRMProfileEditorForm(instance=profile, client=client, prefix="profile")
        if request.method == "POST":
            posted_data = request.POST.copy()
            if client.client_type == "legal" and "name" not in posted_data and "company_name" in posted_data:
                # Forms opened before deployment submitted company_name, not name.
                # An explicitly blank legacy value must still fail validation.
                posted_data["name"] = posted_data["company_name"]
            client_form = ClientEditorForm(
                complete_form_data(posted_data, client_form), instance=client,
            )
            profile_form = CRMProfileEditorForm(
                complete_form_data(request.POST, profile_form),
                instance=profile, client=client, prefix="profile",
            )
            client_valid = client_form.is_valid()
            profile_valid = profile_form.is_valid()
            if client_valid and profile_valid:
                updated = client_form.save(commit=False)
                if updated.client_type == "legal":
                    updated.company_name = updated.name
                elif (
                    "name" not in request.POST
                    and ({"first_name", "last_name"} & set(client_form.changed_data)
                         or "middle_name" in profile_form.changed_data)
                ):
                    name = " ".join(filter(None, (
                        updated.last_name, updated.first_name,
                        profile_form.cleaned_data.get("middle_name", ""),
                    )))
                    if name:
                        if len(name) > Client._meta.get_field("name").max_length:
                            client_form.add_error("name", "Укажите более короткое имя карточки.")
                        else:
                            updated.name = name
                if not client_form.errors:
                    updated.save()
                    profile_form.save()
                    sync_primary_contact(updated, ClientContact.KIND_PHONE, previous_phone, updated.phone)
                    sync_primary_contact(updated, ClientContact.KIND_EMAIL, previous_email, updated.email)
                    messages.success(request, "Карточка клиента обновлена.")
                    return redirect(card_url(updated.pk, tab))
        contacts = list(ClientContact.objects.filter(client=client).order_by("kind", "-is_primary", "id"))
    return render(request, "pool_service/client_edit.html", {
        "client": client, "profile": profile, "client_form": client_form,
        "profile_form": profile_form, "contacts": contacts, "return_tab": tab,
        "return_url": card_url(client.pk, tab), "contacts_url": card_url(client.pk, "contacts"),
        "page_title": "Редактирование клиента", "page_subtitle": client.name,
        "active_tab": "clients", "show_search": False, "show_add_button": False, "add_url": None,
    })
