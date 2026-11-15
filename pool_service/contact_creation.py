"""Shared contact/company creation for the directory, without CRM side effects."""
import json
from functools import wraps
from urllib.parse import urlencode

from django.contrib import messages
from django.contrib.auth import get_user_model
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import router, transaction
from django.db.models import Q
from django.http import JsonResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET, require_http_methods

from .client_crm_models import ClientBusinessRole
from .client_queries import CONTACT_STAFF_ROLES, readable_clients
from .contact_directory import search_contacts
from .models import Client, Employee, Organization, OrganizationAccess
from .services.crm_conversion_contacts import normalize_customer, resolve_conversion_customer
from .services.permissions import company_has_access


RETURN_TOKENS = {"clients_list", "pool_create"}


def _json(request):
    return "application/json" in request.headers.get("Accept", "") or request.content_type == "application/json"


def _response(data, *, status=200):
    response = JsonResponse(data, status=status)
    response["Cache-Control"] = "private, no-store"
    return response


def _permission_errors(view):
    @wraps(view)
    def wrapped(request, *args, **kwargs):
        try:
            return view(request, *args, **kwargs)
        except PermissionDenied:
            if _json(request):
                message = "Создание контакта недоступно. Проверьте организацию и права доступа."
                return _response({"error": message, "errors": [message]}, status=403)
            raise
    return wrapped


def _organizations(user):
    if not user.is_authenticated or user.is_superuser or not get_user_model().objects.filter(pk=user.pk, is_active=True).exists():
        raise PermissionDenied
    blocked = Employee.objects.filter(user=user).filter(Q(is_active=False) | Q(employment_status=Employee.STATUS_DISMISSED)).values("organization_id")
    rows = Organization.objects.filter(accesses__user=user, accesses__role__in=CONTACT_STAFF_ROLES).exclude(pk__in=blocked).distinct().order_by("name", "pk")
    organizations = [row for row in rows if company_has_access(row)]
    if not organizations:
        raise PermissionDenied
    return organizations


def _selected(values, organizations, *, required=False):
    raw = values.get("org_id")
    if raw not in (None, ""):
        if isinstance(raw, bool) or not str(raw).isascii() or not str(raw).isdigit() or int(raw) < 1:
            raise ValidationError("Выберите организацию.")
        result = next((org for org in organizations if org.pk == int(raw)), None)
        if result is None:
            raise PermissionDenied
        return result
    if len(organizations) == 1:
        return organizations[0]
    if required:
        raise ValidationError("Выберите организацию, в которой нужно сохранить контакт.")
    return None


def _navigation(values):
    token = values.get("return_to")
    token = token if isinstance(token, str) and token in RETURN_TOKENS else "clients_list"
    return token, reverse(token)


def _summary(client):
    if client is None:
        return None
    return {"id": client.pk, "name": client.name, "client_type": client.client_type,
            "phone": client.phone or "", "email": client.email or "", "inn": client.inn or "",
            "url": reverse("client_detail", args=[client.pk]),
            "categories": [role.role for role in client.business_roles.all()
                           if role.active and role.role in ClientBusinessRole.ROLE_LABELS]}


def _bootstrap(request, values, organizations, selected, *, customer=None, errors=None):
    token, return_url = _navigation(values)
    kind = "company" if values.get("initial_kind") == "company" or values.get("kind") == "company" or values.get("client_type") == "legal" else "contact"
    return {"organization_id": selected.pk if selected else None,
            "organizations": [{"id": org.pk, "name": org.name} for org in organizations],
            "can_categorize_contacts": bool(selected and OrganizationAccess.objects.filter(
                user=request.user, organization=selected, role__in=("owner", "admin")).exists()),
            "urls": {"create": reverse("client_create"), "clients": reverse("contact_create_lookup")},
            "customer": customer, "errors": errors or [], "return_url": return_url,
            "return_token": token, "initial_kind": kind}


def _legacy_customer(values):
    """Accept old form submissions while saving person and company separately."""
    kind = values.get("client_type", "private")
    if not isinstance(kind, str) or kind not in {"private", "legal"}:
        raise ValidationError("Выберите контакт или компанию.")
    for field in ("name", "first_name", "last_name", "company_name", "phone", "email", "inn", "contact_position", "category"):
        if field in values and not isinstance(values[field], str):
            raise ValidationError("Реквизиты должны быть текстом.")
    first, last = str(values.get("first_name", "")).strip(), str(values.get("last_name", "")).strip()
    if any(len(value) > Client._meta.get_field(field).max_length for field, value in (("first_name", first), ("last_name", last))):
        raise ValidationError("Сократите имя или фамилию.")
    person_name = str(values.get("name", "")).strip() or " ".join(filter(None, (first, last)))
    contact = {"name": person_name, "phone": values.get("phone", ""), "email": values.get("email", ""), "category": values.get("category", "")} if kind == "private" or person_name else None
    company = {"name": values.get("company_name", ""), "inn": values.get("inn", ""),
               "phone": "" if contact else values.get("phone", ""), "email": "" if contact else values.get("email", "")} if kind == "legal" else None
    return {"contact": contact, "company": company}


def _authorize_locked(using, user, organization):
    # Recheck current membership, employment and subscription under the same
    # tenant mutex used by CRM conversion. No caller can choose another tenant.
    actor = get_user_model().objects.using(using).select_for_update().get(pk=user.pk)
    if not actor.is_active or actor.is_superuser or not company_has_access(organization):
        raise PermissionDenied
    roles = OrganizationAccess.objects.using(using).select_for_update().filter(user=actor, organization=organization)
    if not any(role in CONTACT_STAFF_ROLES for role in roles.values_list("role", flat=True)):
        raise PermissionDenied
    employees = list(Employee.objects.using(using).select_for_update().filter(user=actor, organization=organization))
    if any(not employee.is_active or employee.employment_status == Employee.STATUS_DISMISSED for employee in employees):
        raise PermissionDenied
    return actor


@login_required
@never_cache
@require_http_methods(["GET", "POST"])
@_permission_errors
def create(request):
    organizations = _organizations(request.user)
    customer = None
    selected = None
    values = request.GET
    try:
        if request.method == "POST":
            if request.content_type == "application/json":
                try:
                    values = json.loads(request.body)
                except (ValueError, UnicodeDecodeError) as exc:
                    raise ValidationError("Не удалось прочитать данные формы.") from exc
                if type(values) is not dict:
                    raise ValidationError("Не удалось прочитать данные формы.")
            else:
                values = request.POST
        selected = _selected(values, organizations, required=request.method == "POST")
        if request.method == "POST":
            legacy = "customer" not in values
            customer = _legacy_customer(values) if legacy else values.get("customer")
            if isinstance(customer, str):
                try:
                    customer = json.loads(customer)
                except ValueError as exc:
                    raise ValidationError("Проверьте данные контакта и компании.") from exc
            customer = normalize_customer(customer)
            using = router.db_for_write(Client)
            with transaction.atomic(using=using):
                organization = Organization.objects.using(using).select_for_update().get(pk=selected.pk)
                actor = _authorize_locked(using, request.user, organization)
                client, contact = resolve_conversion_customer(using, actor, organization, customer)
                if legacy and contact:
                    contact.first_name = str(values.get("first_name", "")).strip()
                    contact.last_name = str(values.get("last_name", "")).strip()
                    contact.contact_position = str(values.get("contact_position", "")).strip()
                    if len(contact.contact_position) > Client._meta.get_field("contact_position").max_length:
                        raise ValidationError("Сократите должность контактного лица.")
                    contact.save(using=using, update_fields=["first_name", "last_name", "contact_position"])
                company = client if client.client_type == "legal" else None
                token, target = _navigation(values)
                if token == "pool_create":
                    target += "?" + urlencode({"client_id": client.pk})
                result = {"client": _summary(client), "contact": _summary(contact), "company": _summary(company), "redirect_url": target}
            if _json(request):
                return _response(result)
            messages.success(request, "Контакт и компания сохранены." if company and contact else "Компания сохранена." if company else "Контакт сохранён.")
            return redirect(target)
        bootstrap = _bootstrap(request, values, organizations, selected)
    except ValidationError as exc:
        errors = exc.messages
        if _json(request):
            return _response({"error": errors[0], "errors": errors}, status=400)
        selected = selected or _selected({}, organizations)
        bootstrap = _bootstrap(request, values, organizations, selected, customer=customer, errors=errors)
    if _json(request):
        return _response({"bootstrap": bootstrap})
    return render(request, "pool_service/client_create.html", {"page_title": "Контакты и компании",
        "active_tab": "clients", "contact_create_bootstrap": bootstrap,
        "client_return_token": bootstrap["return_token"], "return_url": bootstrap["return_url"],
        "show_search": False, "show_add_button": False, "is_edit": False})


def can_create(user):
    """Advisory directory button; the endpoint reauthorizes every request."""
    try:
        return bool(_organizations(user))
    except PermissionDenied:
        return False


@login_required
@require_GET
@_permission_errors
def lookup(request):
    organizations = _organizations(request.user)
    try:
        organization = _selected(request.GET, organizations, required=True)
        kind = request.GET.get("kind", "")
        if kind not in {"", "private", "legal"}:
            raise ValidationError("Выберите контакт или компанию.")
        query = request.GET.get("q", "").strip()
        if len(query) < 3 or len(query) > 160:
            return _response({"clients": []})
        queryset = readable_clients(request.user).filter(organization=organization)
        if kind:
            queryset = queryset.filter(client_type=kind)
        queryset = search_contacts(queryset, request.user, query).prefetch_related("business_roles")
        return _response({"clients": [_summary(row) for row in queryset.order_by("name", "pk")[:30]]})
    except ValidationError as exc:
        return _response({"error": exc.messages[0], "errors": exc.messages}, status=400)
