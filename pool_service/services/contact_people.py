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


def _locked_person(actor, person_id, *, companies=False):
    org_id = Client.objects.filter(pk=person_id).values_list("organization_id", flat=True).first()
    org = Organization.objects.select_for_update().filter(pk=org_id).first()
    person = Client.objects.select_for_update().filter(pk=person_id, organization_id=org_id,
        client_type__in=("private", "legal") if companies else ("private",)).first()
    actor = get_user_model().objects.select_for_update().filter(pk=getattr(actor, "pk", None), is_active=True).first()
    if not org or not person or not actor or not active_clients().filter(pk=person.pk).exists():
        raise PermissionDenied
    if not actor.is_superuser and (not company_has_access(org) or not OrganizationAccess.objects.select_for_update().filter(
            organization=org, user=actor, role__in=("owner", "admin")).exists()):
        raise PermissionDenied
    return person, actor


@transaction.atomic
def save_phone(actor, person_id, *, value, phone_type, primary, revision, _companies=False):
    person, actor = _locked_person(actor, person_id, companies=_companies)
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
    from pool_service.client_crm_models import ClientContactExclusion
    ClientContactExclusion.objects.filter(client=person, kind="phone", match_value=key).delete()
    if make_primary:
        ClientContact.objects.filter(client=person, kind="phone").exclude(pk=row.pk).update(is_primary=False)
        person.phone = row.value
        person.save(update_fields=["phone"])
    return row


def _phone_key(value):
    return normalize_phone(value) or "raw:" + str(value)


def phone_rows(client):
    """Include legacy scalar phones without creating records on a read."""
    from pool_service.phone_utils import format_phone
    rows, seen = [], set()
    primary_key = _phone_key(client.phone) if client.phone else ""
    for item in ClientContact.objects.filter(client=client, kind="phone").order_by("-is_primary", "pk"):
        key = _phone_key(item.value)
        if key in seen:
            continue
        seen.add(key)
        rows.append(dict(key=key, value=item.value, display=format_phone(item.value),
            phone_type=item.phone_type, type_label=item.get_phone_type_display(),
            label="" if item.label == "Основной" else item.label, primary=bool(key and key == primary_key)))
    if client.phone and primary_key not in seen:
        rows.insert(0, dict(key=primary_key, value=client.phone, display=format_phone(client.phone),
            phone_type="other", type_label="Другой", label="", primary=True))
    return sorted(rows, key=lambda row: not row["primary"])


@transaction.atomic
def manage_phone(actor, client_id, *, action, key, value="", phone_type="other", label="", primary="0", revision):
    """Edit/delete an identity-shaped phone, including imported and legacy rows."""
    import re
    from pool_service.client_crm_models import ClientContactExclusion
    client, actor = _locked_person(actor, client_id, companies=True)
    if actor.is_superuser:
        raise PermissionDenied
    if revision != phone_revision(client):
        raise ValidationError("Карточка изменилась. Обновите страницу перед сохранением.")
    if action not in {"add", "edit", "delete"}:
        raise ValidationError("Выберите действие с номером.")
    rows = list(ClientContact.objects.select_for_update().filter(client=client, kind="phone"))
    old_key = normalize_phone(client.phone)
    matches = [row for row in rows if _phone_key(row.value) == key]
    scalar_match = bool(client.phone and _phone_key(client.phone) == key)
    if action != "add" and not (matches or scalar_match):
        raise ValidationError("Этот номер уже изменён или удалён. Обновите карточку.")
    new_key = normalize_phone(value)
    if action != "delete":
        if (not new_key or not re.fullmatch(r"[+0-9 ().\-\s]+", value)
                or len(value) > 40 or len(canonical_phone_value(value)) > 20
                or phone_type not in dict(ClientContact.PHONE_TYPES) or len(label) > 120
                or primary not in {"0", "1"}):
            raise ValidationError("Проверьте номер, его тип и подпись.")
        if new_key != key and any(normalize_phone(row.value) == new_key for row in rows):
            raise ValidationError("Такой номер уже есть. Отредактируйте существующую строку.")
    if action == "delete" or action == "edit" and new_key != key:
        data = [dict(value=row.value, label=row.label, sources=row.sources, source_reference=row.source_reference) for row in matches]
        if scalar_match and not matches:
            data.append(dict(value=client.phone, sources=["legacy_primary"]))
        ClientContactExclusion.objects.update_or_create(client=client, kind="phone", match_value=key,
            defaults={"removed_data": data, "actor": actor})
        ClientContact.objects.filter(pk__in=[row.pk for row in matches]).delete()
    if action == "delete":
        if scalar_match:
            remaining = ClientContact.objects.filter(client=client, kind="phone").order_by("-is_primary", "pk").first()
            client.phone = remaining.value if remaining else ""
            client.save(update_fields=["phone"])
    else:
        if action == "edit" and new_key == key and matches:
            row = matches[0]
            row.value, row.match_value = canonical_phone_value(value), new_key
            row.phone_type, row.label = phone_type, label.strip()
            row.sources = list(dict.fromkeys([*(row.sources or []), "contact_directory"]))
            # A legacy duplicate is folded into this one explicit phone row.
            for duplicate in matches[1:]:
                row.sources = list(dict.fromkeys([*row.sources, *(duplicate.sources or [])]))
                duplicate.delete()
            row.save()
            if scalar_match or primary == "1":
                client.phone = row.value
                client.save(update_fields=["phone"])
        else:
            if scalar_match:
                client.phone = ""
                client.save(update_fields=["phone"])
            row = save_phone(actor, client.pk, value=value, phone_type=phone_type,
                primary="1" if scalar_match else primary, revision=phone_revision(client), _companies=True)
            row.label = label.strip()
            row.save(update_fields=["label", "updated_at"])
        ClientContactExclusion.objects.filter(client=client, kind="phone", match_value=new_key).delete()
    client.refresh_from_db(fields=["phone"])
    current_key = normalize_phone(client.phone)
    for row in ClientContact.objects.filter(client=client, kind="phone"):
        row.is_primary = bool(current_key and normalize_phone(row.value) == current_key)
        if row.label == "Основной":
            row.label = ""
        row.save(update_fields=["is_primary", "label", "updated_at"])


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
