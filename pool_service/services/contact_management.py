"""Reviewed, atomic directory merges. No account grants or automatic matching."""
import hashlib
import json

from django.contrib.auth import get_user_model
from django.core import signing
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.db.models import Q

from pool_service.client_crm_models import (
    ClientCRMProfile, ClientCompanyLink, ClientContact, ClientContactExclusion,
    ClientImportRun, ClientMergeRecord, OrganizationPersonLink,
)
from pool_service.client_import_review import editor_revision
from pool_service.client_merge import merge_clients
from pool_service.client_queries import active_clients
from pool_service.crm_commercial_models import CrmLead, CrmDeal
from pool_service.models import Client, ClientAccess, Organization, OrganizationAccess
from pool_service.phone_utils import normalize_phone, format_phone
from .permissions import company_has_access

MAX_MERGE = 20
SALT = "contacts-directory-merge-v1"


def _selection(actor, ids, target_id):
    if (type(ids) is not list or not 2 <= len(ids) <= MAX_MERGE
            or any(type(pk) is not int or pk < 1 for pk in ids) or len(set(ids)) != len(ids)):
        raise ValidationError("Выберите от 2 до 20 карточек.")
    rows = list(active_clients().filter(pk__in=ids).order_by("pk"))
    if len(rows) != len(ids) or len({row.organization_id for row in rows}) != 1:
        raise PermissionDenied
    org = Organization.objects.select_for_update().filter(pk=rows[0].organization_id).first()
    rows = list(active_clients(Client.objects.select_for_update()).filter(pk__in=ids,
        organization=org).order_by("pk"))
    current_actor = get_user_model().objects.select_for_update().filter(pk=actor.pk, is_active=True).first()
    if (not org or not current_actor or current_actor.is_superuser or len(rows) != len(ids)
            or not company_has_access(org)
            or not OrganizationAccess.objects.select_for_update().filter(organization=org,
                user=current_actor, role__in=("owner", "admin")).exists()):
        raise PermissionDenied
    if len({row.client_type for row in rows}) != 1:
        raise ValidationError("Компании и физические лица объединяются отдельно. Для связи человека с компанией используйте раздел «Контакты» в карточке.")
    if ClientImportRun.objects.filter(organization=org, status__in=("pending", "running", "applying")).exists():
        raise ValidationError("Дождитесь завершения импорта клиентов.")
    profiles = {p.client_id: p for p in ClientCRMProfile.objects.filter(client_id__in=ids)}
    imported = [row.pk for row in rows if profiles.get(row.pk) and profiles[row.pk].onec_ref]
    if len(imported) > 1:
        raise ValidationError("Выбраны разные карточки 1С. Сначала проверьте их в 1С — объединение разных контрагентов здесь недоступно.")
    target_id = target_id or (imported[0] if imported else rows[0].pk)
    if type(target_id) is not int or target_id not in ids:
        raise ValidationError("Выберите основную карточку.")
    if imported and target_id != imported[0]:
        raise ValidationError("Основной должна остаться карточка, связанная с 1С.")
    if (any(row.user_id for row in rows) or OrganizationPersonLink.objects.filter(person_id__in=ids).exists()
            or ClientAccess.objects.filter(client_id__in=ids).exists()):
        raise ValidationError("У выбранных карточек есть учётные записи или доступы. Их объединение требует отдельной сверки прав.")
    from pool_service.crm_material_models import CrmMaterialIssue, CrmMaterialMovement
    sources = [pk for pk in ids if pk != target_id]
    # Refuse protected accounting graphs instead of rewriting immutable scopes,
    # confirmations or the meaning of a verified 1C customer identity.
    from pool_service.crm_order_models import CrmDealOrderLink
    if CrmDealOrderLink.objects.filter(Q(client_id__in=sources) | Q(deal__client_id__in=sources)).exists():
        raise ValidationError("У переносимой карточки есть подтверждённая связь сделки с заказом 1С. Сначала требуется сверка этой связи.")
    from pool_service.crm_recurring_models import CrmRecurringConfirmation, CrmRecurringDraft
    drafts = CrmRecurringDraft.objects.filter(Q(client_id__in=ids) | Q(payer_id__in=ids) | Q(deal__client_id__in=ids))
    if drafts.exclude(organization=org).exists() or drafts.filter(
            Q(client_id__in=sources) | Q(payer_id__in=sources) | Q(deal__client_id__in=sources)).exists():
        raise ValidationError("У переносимой карточки есть условия постоянного обслуживания или роль плательщика. Сначала требуется сверка договора.")
    if CrmRecurringConfirmation.objects.filter(pool__client_id__in=sources).exists():
        raise ValidationError("У карточки есть подтверждённое обслуживание. Сначала требуется сверка объектов.")
    for rel in Client._meta.related_objects:
        if rel.many_to_many:
            continue
        model = rel.related_model
        related = model.objects.filter(**{rel.field.name + "_id__in": ids})
        if any(field.name == "organization" for field in model._meta.fields) and related.exclude(organization_id=org.pk).exists():
            raise ValidationError("Обнаружена связь с другой организацией. Объединение не выполнено.")
        if model.__name__ in {"OneCCustomerIdentity", "ServiceContractBinding", "ClientInvite",
                "RewardParticipantTemplate", "RewardOrderObjectLink"} and related.filter(**{rel.field.name + "_id__in": sources}).exists():
            raise ValidationError("У переносимой карточки есть подтверждённые договоры, связи учёта или приглашения. Сначала требуется их сверка.")
    if ClientCompanyLink.objects.filter(Q(company_id__in=ids) | Q(person_id__in=ids)).exclude(
            company__organization_id=org.pk, person__organization_id=org.pk).exists():
        raise ValidationError("Связь контакта с компанией выходит за пределы организации.")
    if (CrmMaterialIssue.objects.filter(client_id__in=sources).exists()
            or CrmMaterialMovement.objects.filter(client_id__in=sources).exists()):
        raise ValidationError("У выбранных карточек есть история выдачи материалов. Сначала требуется её сверка.")
    return rows, next(row for row in rows if row.pk == target_id), current_actor


def _snapshot(rows):
    ids = [row.pk for row in rows]
    cards = []
    for row in rows:
        profile = ClientCRMProfile.objects.filter(client=row).first() or ClientCRMProfile(client=row)
        cards.append(dict(id=row.pk, name=row.name, type=row.client_type, phone=row.phone or "", email=row.email or "",
            inn=row.inn or "", onec=bool(profile.onec_ref), revision=editor_revision(row, profile), source_revision=profile.onec_ref))
    # Include linked identities/revisions so an intervening merge, phone edit,
    # new CRM card or newly attached history invalidates the preview.
    relations = {}
    for rel in Client._meta.related_objects:
        if rel.many_to_many:
            continue
        model, field = rel.related_model, rel.field.name
        fields = ["pk", field + "_id"]
        if any(f.name == "revision" for f in model._meta.fields):
            fields.append("revision")
        digest = hashlib.sha256()
        for item in model.objects.filter(**{field + "_id__in": ids}).order_by("pk").values(*fields).iterator(chunk_size=500):
            digest.update(json.dumps(item, sort_keys=True, default=str).encode())
        relations[model._meta.label + ":" + field] = digest.hexdigest()
    return {"cards": cards, "relations": relations}


def _fingerprint(snapshot):
    return hashlib.sha256(json.dumps(snapshot, sort_keys=True, default=str).encode()).hexdigest()


@transaction.atomic
def preview_merge(actor, ids, target_id=None):
    rows, target, actor = _selection(actor, ids, target_id)
    snapshot = _snapshot(rows)
    contacts = {"phone": {}, "email": {}}
    for row in rows:
        for kind in contacts:
            values = [getattr(row, kind)] + list(ClientContact.objects.filter(client=row, kind=kind).values_list("value", flat=True))
            for value in values:
                if value:
                    key = (normalize_phone(value) or value) if kind == "phone" else value.casefold()
                    contacts[kind][key] = format_phone(value) if kind == "phone" else value
    return {"cards": [{k: v for k, v in card.items() if k not in {"revision", "source_revision"}} for card in snapshot["cards"]],
        "target_id": target.pk, "phones": list(contacts["phone"].values()), "emails": list(contacts["email"].values()),
        "leads": CrmLead.objects.filter(Q(client_id__in=ids) | Q(contact_id__in=ids)).count(),
        "deals": CrmDeal.objects.filter(Q(client_id__in=ids) | Q(contact_id__in=ids)).count(),
        "token": signing.dumps({"ids": sorted(ids), "target": target.pk, "actor": actor.pk, "org": target.organization_id,
            "revision": _fingerprint(snapshot)}, salt=SALT, compress=True)}


def _receipt_target(actor, receipt):
    org = Organization.objects.select_for_update().get(pk=receipt.organization_id)
    current = get_user_model().objects.select_for_update().filter(pk=actor.pk, is_active=True).first()
    if (not current or current.is_superuser or receipt.actor_id != current.pk or not company_has_access(org)
            or not OrganizationAccess.objects.select_for_update().filter(organization=org, user=current,
                role__in=("owner", "admin")).exists()):
        raise PermissionDenied
    return Client.objects.get(pk=receipt.target_id, organization=org)


@transaction.atomic
def execute_merge(actor, token):
    operation_key = hashlib.sha256(str(token).encode()).hexdigest()
    receipt = ClientMergeRecord.objects.filter(operation_key=operation_key).first()
    if receipt:
        return _receipt_target(actor, receipt)
    try:
        payload = signing.loads(token, salt=SALT, max_age=600)
    except (signing.BadSignature, TypeError):
        raise ValidationError("Предпросмотр устарел. Откройте объединение заново.")
    if payload["actor"] != actor.pk:
        raise PermissionDenied
    Organization.objects.select_for_update().get(pk=payload["org"])
    # A competing request may have committed while this request waited on org.
    receipt = ClientMergeRecord.objects.filter(operation_key=operation_key).first()
    if receipt:
        return _receipt_target(actor, receipt)
    rows, target, actor = _selection(actor, payload["ids"], payload["target"])
    snapshot = _snapshot(rows)
    if payload["revision"] != _fingerprint(snapshot):
        raise ValidationError("Одна из карточек изменилась. Проверьте обновлённые данные перед объединением.")
    # Preserve full original editable data in the operation history, not in Git.
    snapshot["originals"] = [dict(client=Client.objects.filter(pk=row.pk).values().get(),
        profile=ClientCRMProfile.objects.filter(client=row).values().first(),
        contacts=list(ClientContact.objects.filter(client=row).values())) for row in rows]
    snapshot = json.loads(json.dumps(snapshot, default=str))
    notes = []
    for row in [target, *(row for row in rows if row.pk != target.pk)]:
        profile = ClientCRMProfile.objects.filter(client=row).first()
        if profile and profile.notes and profile.notes not in notes:
            notes.append(profile.notes)
    for source in rows:
        if source.pk == target.pk:
            continue
        for contact in ClientContact.objects.filter(client_id__in=[source.pk, target.pk]):
            contact.match_value = normalize_phone(contact.value) if contact.kind == "phone" else contact.value.casefold()
            contact.save(update_fields=["match_value", "updated_at"])
        merge_clients(source.pk, target.pk, organization_id=target.organization_id, actor=actor, _directory=True)
        for exclusion in ClientContactExclusion.objects.filter(client=source):
            ClientContactExclusion.objects.get_or_create(client=target, kind=exclusion.kind,
                match_value=exclusion.match_value, defaults={"removed_data": exclusion.removed_data, "actor": actor})
        target.refresh_from_db()
    profile = ClientCRMProfile.objects.get(client=target)
    profile.notes = "\n\n".join(notes)
    profile.save(update_fields=["notes", "updated_at"])
    for row in ClientContact.objects.filter(client=target):
        key = normalize_phone(row.value) if row.kind == "phone" else row.value.casefold()
        current = normalize_phone(target.phone) if row.kind == "phone" else (target.email or "").casefold()
        row.is_primary = bool(key and key == current)
        if row.label == "Основной":
            row.label = ""
        row.save(update_fields=["is_primary", "label", "updated_at"])
        ClientContactExclusion.objects.filter(client=target, kind=row.kind, match_value=key).delete()
    ClientMergeRecord.objects.create(organization_id=target.organization_id, target=target, actor=actor,
        snapshot=snapshot, operation_key=operation_key)
    return target
