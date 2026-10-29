"""Explicit contact editing; no account creation, access grants or phone matching."""
import hashlib
import json
from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.db.models import Q
from pool_service.client_crm_models import ClientContact, OrganizationPersonLink
from pool_service.client_queries import active_clients
from pool_service.models import Client, Employee, Organization, OrganizationAccess
from pool_service.phone_utils import normalize_phone, canonical_phone_value
from .permissions import company_has_access


def phone_revision(client):
    rows = list(ClientContact.objects.filter(client=client, kind="phone").order_by("pk").values(
        "pk", "value", "phone_type", "is_primary", "label", "sources", "updated_at"))
    return hashlib.sha256(json.dumps([client.phone, rows], default=str, sort_keys=True).encode()).hexdigest()


def _locked_person(actor, person_id):
    org_id = Client.objects.filter(pk=person_id).values_list("organization_id", flat=True).first()
    org = Organization.objects.select_for_update().filter(pk=org_id).first()
    person = Client.objects.select_for_update().filter(pk=person_id, organization_id=org_id, client_type="private").first()
    actor = get_user_model().objects.select_for_update().filter(pk=getattr(actor, "pk", None), is_active=True).first()
    if not org or not person or not actor or not active_clients().filter(pk=person.pk).exists():
        raise PermissionDenied
    if not actor.is_superuser and (not company_has_access(org) or not OrganizationAccess.objects.select_for_update().filter(
            organization=org, user=actor, role__in=("owner", "admin")).exists()):
        raise PermissionDenied
    return person, actor


@transaction.atomic
def save_phone(actor, person_id, *, value, phone_type, primary, revision):
    person, actor = _locked_person(actor, person_id)
    if revision != phone_revision(person):
        raise ValidationError("Карточка изменилась. Обновите её.")
    key = normalize_phone(value)
    if not key or len(str(value)) > 40 or phone_type not in dict(ClientContact.PHONE_TYPES) or primary not in ("0", "1"):
        raise ValidationError("Проверьте номер и его тип.")
    rows = list(ClientContact.objects.select_for_update().filter(client=person, kind="phone"))
    matches = [row for row in rows if normalize_phone(row.value) == key]
    if len(matches) > 1:
        raise ValidationError("Есть несколько записей этого номера. Требуется сверка.")
    for previous in rows:
        if "contact_directory" not in (previous.sources or []):
            previous.sources = list(dict.fromkeys([*(previous.sources or []), "contact_directory"]))
            previous.save(update_fields=["sources", "updated_at"])
    old_key = normalize_phone(person.phone)
    if old_key and old_key != key and not any(normalize_phone(item.value) == old_key for item in rows):
        ClientContact.objects.create(client=person, kind="phone", value=person.phone, match_value=old_key,
            is_primary=True, sources=["legacy_primary"])
    row = matches[0] if matches else ClientContact(client=person, kind="phone", value=canonical_phone_value(value), match_value=key)
    row.phone_type = phone_type
    row.sources = list(dict.fromkeys([*(row.sources or []), "contact_directory"]))
    # Additional numbers survive the legacy primary-phone editor.
    make_primary = primary == "1" or not person.phone
    row.is_primary = make_primary or normalize_phone(person.phone) == key
    row.save()
    if make_primary:
        ClientContact.objects.filter(client=person, kind="phone").exclude(pk=row.pk).update(is_primary=False)
        person.phone = row.value
        person.save(update_fields=["phone"])
    return row


@transaction.atomic
def link_person(actor, person_id, user_id):
    person, actor = _locked_person(actor, person_id)
    try:
        user_id = int(user_id)
    except (TypeError, ValueError):
        raise ValidationError("Выберите пользователя.")
    user = get_user_model().objects.select_for_update().filter(pk=user_id, is_active=True).first()
    if not user or not OrganizationAccess.objects.select_for_update().filter(
            organization_id=person.organization_id, user=user,
            role__in=("owner", "admin", "manager", "service", "installer")).exists():
        raise PermissionDenied
    existing = OrganizationPersonLink.objects.filter(organization_id=person.organization_id).filter(
        Q(user=user) | Q(person=person)).first()
    if existing:
        if existing.user_id == user.pk and existing.person_id == person.pk:
            return existing
        raise ValidationError("У пользователя или физлица уже есть связь. Требуется отдельная сверка.")
    employees = list(Employee.objects.select_for_update().filter(organization_id=person.organization_id, user=user))
    if len(employees) > 1 or any(not e.is_active or e.employment_status == Employee.STATUS_DISMISSED for e in employees):
        raise ValidationError("Проверьте действующую карточку сотрудника.")
    return OrganizationPersonLink.objects.create(organization_id=person.organization_id, person=person, user=user,
        employee=employees[0] if employees else None, created_by=actor)
