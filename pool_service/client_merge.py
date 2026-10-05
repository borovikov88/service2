from __future__ import annotations

from difflib import SequenceMatcher
import re

from django.db import transaction
from django.utils import timezone

from .models import Client, ClientAccess
from .client_crm_import import normalize_phone
from .client_crm_models import (
    ClientCRMProfile,
    ClientCompanyLink,
    ClientContact,
    ClientImportCandidate,
)


def _normalized_name(value):
    text = re.sub(r"[^0-9a-zа-яё]+", " ", str(value or "").casefold())
    stop = {"ооо", "оао", "ао", "ип", "мбдоу", "мбоу", "краевое", "муниципальное"}
    return " ".join(part for part in text.split() if part and part not in stop)


def _merge_profile(source, target, actor):
    source_profile, _ = ClientCRMProfile.objects.get_or_create(client=source)
    target_profile, _ = ClientCRMProfile.objects.get_or_create(client=target)

    if source_profile.onec_ref and target_profile.onec_ref and source_profile.onec_ref != target_profile.onec_ref:
        raise ValueError("Нельзя объединить две разные карточки, уже связанные с 1С")
    if source_profile.merged_into_id:
        raise ValueError("Исходная карточка уже объединена")

    for field in ("middle_name", "birth_date", "legal_name", "kpp", "ogrn", "responsible", "manager", "notes"):
        source_value = getattr(source_profile, field)
        target_value = getattr(target_profile, field)
        if source_value and not target_value:
            setattr(target_profile, field, source_value)
    target_profile.save()

    source_profile.merged_into = target
    source_profile.merged_at = timezone.now()
    source_profile.merged_by = actor
    source_profile.save(update_fields=["merged_into", "merged_at", "merged_by", "updated_at"])


def _merge_contacts(source, target):
    for contact in ClientContact.objects.filter(client=source).order_by("id"):
        existing = ClientContact.objects.filter(
            client=target,
            kind=contact.kind,
            value=contact.value,
        ).first()
        if existing:
            sources = list(existing.sources or [])
            for value in contact.sources or []:
                if value not in sources:
                    sources.append(value)
            changed = False
            if sources != (existing.sources or []):
                existing.sources = sources
                changed = True
            if contact.is_primary and not existing.is_primary:
                existing.is_primary = True
                changed = True
            if not existing.match_value and contact.match_value:
                existing.match_value = contact.match_value
                changed = True
            if not existing.label and contact.label:
                existing.label = contact.label
                changed = True
            if changed:
                existing.save()
            contact.delete()
        else:
            contact.client = target
            contact.save(update_fields=["client", "updated_at"])


def _merge_company_links(source, target):
    for link in list(ClientCompanyLink.objects.filter(company=source).select_related("person")):
        if link.person_id == target.id:
            link.delete()
            continue
        merged, _ = ClientCompanyLink.objects.get_or_create(
            company=target,
            person=link.person,
            defaults={
                "position": link.position,
                "roles": link.roles,
                "is_primary": link.is_primary,
                "source": link.source,
                "source_reference": link.source_reference,
                "automatic": link.automatic,
            },
        )
        if merged.pk != link.pk:
            link.delete()

    for link in list(ClientCompanyLink.objects.filter(person=source).select_related("company")):
        if link.company_id == target.id:
            link.delete()
            continue
        merged, _ = ClientCompanyLink.objects.get_or_create(
            company=link.company,
            person=target,
            defaults={
                "position": link.position,
                "roles": link.roles,
                "is_primary": link.is_primary,
                "source": link.source,
                "source_reference": link.source_reference,
                "automatic": link.automatic,
            },
        )
        if merged.pk != link.pk:
            link.delete()


def _merge_staff_access(source, target):
    for access in ClientAccess.objects.filter(client=source).select_related("user"):
        existing = ClientAccess.objects.filter(client=target, user=access.user).first()
        if existing:
            if existing.role == "viewer" and access.role == "editor":
                existing.role = "editor"
            if not existing.phone and access.phone:
                existing.phone = access.phone
            existing.save(update_fields=["role", "phone"])
            access.delete()
        else:
            access.client = target
            access.save(update_fields=["client"])


def _generic_relations(source, target):
    special_models = {
        ClientCRMProfile,
        ClientContact,
        ClientCompanyLink,
        ClientImportCandidate,
        ClientAccess,
    }
    relations = list(Client._meta.related_objects)
    # Move objects first because some reward links validate that pool.client
    # and link.client are the same client.
    relations.sort(key=lambda rel: 0 if rel.related_model.__name__ == "Pool" else 1)

    moved = {}
    for rel in relations:
        model = rel.related_model
        if model in special_models:
            continue
        if rel.many_to_many:
            continue
        field = rel.field
        if rel.one_to_one:
            source_obj = model.objects.filter(**{field.name: source}).first()
            if not source_obj:
                continue
            if model.objects.filter(**{field.name: target}).exists():
                raise ValueError(
                    f"Нельзя автоматически объединить связь {model._meta.verbose_name}"
                )
            setattr(source_obj, field.name, target)
            source_obj.save(update_fields=[field.name])
            moved[model.__name__] = moved.get(model.__name__, 0) + 1
            continue

        count = model.objects.filter(**{field.name: source}).update(**{field.name: target})
        if count:
            moved[model.__name__] = moved.get(model.__name__, 0) + count
    return moved


@transaction.atomic
def merge_clients(source_id, target_id, actor=None):
    if source_id == target_id:
        raise ValueError("Нельзя объединить карточку саму с собой")

    locked = {
        item.pk: item
        for item in Client.objects.select_for_update()
        .filter(pk__in=[source_id, target_id])
        .select_related("organization")
    }
    source = locked.get(source_id)
    target = locked.get(target_id)
    if not source or not target:
        raise ValueError("Карточка клиента не найдена")
    if source.organization_id != target.organization_id:
        raise ValueError("Клиенты относятся к разным организациям")

    source_profile = ClientCRMProfile.objects.filter(client=source).first()
    target_profile = ClientCRMProfile.objects.filter(client=target).first()
    if source_profile and source_profile.merged_into_id:
        raise ValueError("Исходная карточка уже объединена")
    if target_profile is None or not target_profile.onec_ref:
        raise ValueError("Целевая карточка должна быть импортирована из 1С")
    if source_profile and source_profile.onec_ref:
        raise ValueError("Исходная карточка уже связана с 1С")
    if source.user_id and target.user_id and source.user_id != target.user_id:
        raise ValueError(
            "У обеих карточек есть разные пользовательские аккаунты. Такое объединение нужно разобрать отдельно."
        )

    moved = _generic_relations(source, target)
    _merge_staff_access(source, target)
    _merge_contacts(source, target)
    _merge_company_links(source, target)
    ClientImportCandidate.objects.filter(matched_client=source).update(matched_client=target)

    changed = []
    if not target.phone and source.phone:
        target.phone = source.phone
        changed.append("phone")
    if not target.email and source.email:
        target.email = source.email
        changed.append("email")
    if not target.inn and source.inn:
        target.inn = source.inn
        changed.append("inn")
    if not target.user_id and source.user_id:
        user_id = source.user_id
        source.user_id = None
        source.save(update_fields=["user"])
        target.user_id = user_id
        changed.append("user")
    if changed:
        target.save(update_fields=changed)

    _merge_profile(source, target, actor)
    return {
        "source_id": source.id,
        "target_id": target.id,
        "moved": moved,
    }


def merge_suggestions(source, targets, limit=5):
    source_name = _normalized_name(source.name or source.company_name)
    source_phone = normalize_phone(source.phone)
    scored = []
    for target in targets:
        target_name = _normalized_name(target.name or target.company_name)
        score = SequenceMatcher(None, source_name, target_name).ratio() if source_name and target_name else 0.0
        target_phone = normalize_phone(target.phone)
        if source_phone and target_phone and source_phone == target_phone:
            score = max(score, 0.95)
        if source.client_type == target.client_type:
            score += 0.03
        score = min(score, 1.0)
        scored.append((score, target))
    scored.sort(key=lambda item: (-item[0], item[1].name.casefold(), item[1].id))
    return [
        {"client": target, "score": round(score * 100)}
        for score, target in scored[:limit]
        if score >= 0.35
    ]
