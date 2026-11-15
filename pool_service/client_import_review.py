"""Non-destructive 1C reconciliation. Callers lock Organization, Clients in PK order, then source/profile/review rows.

Source versions are per target/field, including absence. A rejected value stays
rejected until that source field changes. No call privacy settings are written.
"""
import hashlib
import json
from datetime import date

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from .client_crm_models import ClientCRMProfile, ClientContact, ClientCompanyLink, ClientImportCandidate, ClientImportProposal
from .models import Client, Organization, OrganizationAccess
from .phone_utils import normalize_phone, canonical_phone_value

CLIENT_FIELDS = ("name", "first_name", "last_name", "company_name", "phone", "email", "inn")
PROFILE_FIELDS = ("middle_name", "birth_date", "legal_name", "kpp")
LABELS = dict(name="Имя / название", first_name="Имя", last_name="Фамилия", company_name="Название компании",
    phone="Основной телефон", email="Основной email", inn="ИНН", middle_name="Отчество",
    birth_date="Дата рождения", legal_name="Полное название", kpp="КПП", classification="Тип клиента",
    ip_person="Создать и связать физлицо ИП")


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, default=str).encode()).hexdigest()


def scalar(value):
    return value.isoformat() if isinstance(value, date) else value


def equivalent(field, value):
    if field.startswith("contact:"):
        if not value:
            return ""
        return {"kind": value["kind"], "match": equivalent(value["kind"], value["value"])}
    if field == "phone":
        return normalize_phone(value) or str(value or "").strip()
    if field == "email":
        return str(value or "").strip().lower()
    return scalar(value) or ""


def incoming(candidate, *, person=False):
    from .client_crm_import import _split_person_name
    private = person or candidate.effective_kind == "private"
    name = ((candidate.payload or {}).get("fio") or candidate.name) if person else candidate.name
    last, first, middle = _split_person_name(name) if private else ("", "", "")
    if person:
        name = " ".join(filter(None, (last, first, middle)))
    return dict(name=name, first_name=first, last_name=last,
        company_name=(candidate.legal_name or candidate.name) if not private else "",
        phone=canonical_phone_value(candidate.phone) if candidate.phone else "",
        email=candidate.email, inn="" if person else candidate.inn, middle_name=middle,
        birth_date=scalar(candidate.birth_date), legal_name="" if person else candidate.legal_name,
        kpp="" if person else candidate.kpp,
        classification={"client_type": "private" if private else "legal",
            "legal_form": "" if private else "ip" if candidate.effective_kind == "ip" else "entity"})


def contacts(candidate):
    result = {}
    for kind, key in (("phone", "phones"), ("email", "emails")):
        for item in (candidate.payload or {}).get(key, []):
            value = str(item.get("value") or "").strip()
            match = normalize_phone(value) if kind == "phone" else equivalent(kind, value)
            if value and match:
                field = "contact:" + kind + ":" + digest(match)
                result[field] = dict(kind=kind, value=canonical_phone_value(value) if kind == "phone" else value,
                    match_value=match, label=str(item.get("label") or "")[:120])
    return result


def source_values(candidate, *, person=False, client=None):
    values = {**incoming(candidate, person=person), **contacts(candidate)}
    if client:
        imported = contacts(candidate)
        for contact in ClientContact.objects.filter(client=client):
            item = next((item for item in imported.values() if item["kind"] == contact.kind
                and item["match_value"] == equivalent(contact.kind, contact.value)), None)
            if item:
                values["contact_label:" + str(contact.pk)] = item["label"]
    if not person:
        values["ip_person"] = ({"name": incoming(candidate, person=True)["name"],
            "source_ref": candidate.source_ref, "values": source_values(candidate, person=True)}
            if candidate.effective_kind == "ip" else None)
    return values


def context(candidate, client, profile, field=None):
    if client.organization_id != candidate.organization_id or profile.merged_into_id:
        raise ValueError("Карточка вне организации или объединена")
    main = client.pk == candidate.matched_client_id
    if main:
        if profile.onec_ref != candidate.source_ref:
            raise ValueError("Связь с источником 1С изменилась")
        link = None
    else:
        link = ClientCompanyLink.objects.filter(company_id=candidate.matched_client_id, person=client,
            source="onec_ip", source_reference=candidate.source_ref).values(
                "pk", "company_id", "person_id", "source", "source_reference", "automatic").first()
        company = ClientCRMProfile.objects.filter(client_id=candidate.matched_client_id,
            client__organization_id=candidate.organization_id, onec_ref=candidate.source_ref,
            merged_into__isnull=True, legal_form="ip", client__client_type="legal").exists()
        if candidate.effective_kind != "ip" or not link or not company or profile.onec_ref or client.client_type != "private":
            raise ValueError("Источник физлица ИП не подтверждён")
    return dict(source_ref=candidate.source_ref, organization_id=candidate.organization_id,
        main_client_id=candidate.matched_client_id, client_type=client.client_type,
        onec_ref=profile.onec_ref, link=link,
        contact_revision=digest(list(ClientContact.objects.filter(client=client, kind=field).order_by("pk").values()))
            if field in ("phone", "email") else None)


def lock_clients(candidate, target_id=None):
    """Organization is held by the caller; never lock a foreign tenant's graph."""
    ids = {pk for pk in (candidate.matched_client_id, target_id) if pk}
    related = ClientCompanyLink.objects.filter(Q(company_id__in=ids) | Q(person_id__in=ids)).filter(
        company__organization_id=candidate.organization_id, person__organization_id=candidate.organization_id)
    for company_id, person_id in related.values_list("company_id", "person_id"):
        ids.update((company_id, person_id))
    locked = {client.pk: client for client in Client.objects.select_for_update().filter(
        pk__in=ids, organization_id=candidate.organization_id).order_by("pk")}
    if set(locked) != ids:
        raise ValueError("Карточка вне организации источника")
    return locked


def current(client, profile, field):
    if field in ("classification", "ip_person"):
        links = ClientCompanyLink.objects.filter(Q(company=client) | Q(person=client))
        if links.exclude(company__organization_id=client.organization_id,
                person__organization_id=client.organization_id).exists():
            raise ValueError("Связь выходит за пределы организации; требуется отдельная проверка")
    if field == "classification":
        return dict(client_type=client.client_type, legal_form=profile.legal_form,
            links=list(ClientCompanyLink.objects.filter(Q(company=client) | Q(person=client)).order_by("pk").values(
                "pk", "company_id", "person_id", "person__name", "company__name", "source", "source_reference",
                "automatic", "is_primary", "position", "roles", "updated_at")))
    if field == "ip_person":
        return list(ClientCompanyLink.objects.filter(company=client).order_by("pk").values(
            "pk", "person_id", "source", "source_reference", "automatic", "is_primary"))
    if field.startswith("contact_label:"):
        return ClientContact.objects.filter(client=client, pk=int(field.split(":")[1])).values(
            "pk", "kind", "value", "label", "source_reference", "sources", "updated_at").first()
    if field.startswith("contact:"):
        # Whole contact set and scalar protect an addition against concurrent edits.
        kind = field.split(":")[1]
        return dict(primary=getattr(client, kind), rows=list(ClientContact.objects.filter(client=client, kind=kind)
            .order_by("pk").values("pk", "value", "label", "sources", "source_reference", "is_primary", "phone_type", "updated_at")))
    return scalar(getattr(client if field in CLIENT_FIELDS else profile, field))


def json_value(value):
    return json.loads(json.dumps(value, default=str))


def observe(candidate, client, values):
    states = candidate.review_state or {}
    prefix = str(client.pk) + ":"
    keys = set(values) | {key[len(prefix):] for key in states if key.startswith(prefix)}
    for field in keys:
        value = values.get(field)
        fingerprint = digest(equivalent(field, value))
        key = prefix + field
        old = states.get(key)
        if not old or old["digest"] != fingerprint:
            states[key] = {"digest": fingerprint, "version": old["version"] + 1 if old else 1}
            ClientImportProposal.objects.filter(candidate=candidate, client=client, field=field,
                state="pending").update(state="stale")
    candidate.review_state = states
    candidate.save(update_fields=["review_state"])


def propose(candidate, client, profile, field, after):
    if field.startswith("contact:"):
        # Approval concerns this contact identity. Labels have a separate field
        # lifecycle; a changed label must not revive a rejected phone/email.
        after = {**after, "label": ""}
    version = candidate.review_state[str(client.pk) + ":" + field]["version"]
    proposal, created = ClientImportProposal.objects.get_or_create(candidate=candidate, client=client,
        field=field, source_version=version, defaults=dict(before=json_value(current(client, profile, field)),
            after=json_value(after), context=context(candidate, client, profile, field)))
    proposal.created_now = created
    return proposal


def write_scalar(client, profile, field, value):
    obj = client if field in CLIENT_FIELDS else profile
    if field == "birth_date" and value:
        value = date.fromisoformat(value)
    setattr(obj, field, value)
    obj.save(update_fields=[field])


def add_contact(client, candidate, data):
    # Preserve representation, label, primary flag and provenance of existing contacts.
    for contact in ClientContact.objects.filter(client=client, kind=data["kind"]):
        if equivalent(data["kind"], contact.value) == data["match_value"]:
            return contact
    return ClientContact.objects.create(client=client, **data, sources=["onec"],
        source_reference=candidate.source_ref, is_primary=False)


def reconcile(candidate, client, profile, *, created=False, person=False):
    values = source_values(candidate, person=person, client=client)
    for field in (*CLIENT_FIELDS, *PROFILE_FIELDS):
        value = values[field]
        model = Client if field in CLIENT_FIELDS else ClientCRMProfile
        limit = model._meta.get_field(field).max_length
        if limit and value and len(value) > limit:
            raise ValueError("Значение 1С превышает допустимую длину поля")
    if any(len(item["value"]) > 255 for item in contacts(candidate).values()):
        raise ValueError("Контакт 1С превышает допустимую длину")
    observe(candidate, client, values)
    new_proposals = []

    def queue(field, value):
        row = propose(candidate, client, profile, field, value)
        if row.created_now:
            new_proposals.append(row)

    for field, value in values.items():
        if field == "ip_person" or not value:
            continue
        version = candidate.review_state[str(client.pk) + ":" + field]["version"]
        if ClientImportProposal.objects.filter(candidate=candidate, client=client, field=field,
                source_version=version, state__in=("accepted", "rejected")).exists():
            continue
        old = current(client, profile, field)
        if field.startswith("contact_label:"):
            if old and not old["label"]:
                ClientContact.objects.filter(pk=old["pk"], client=client).update(label=value, updated_at=timezone.now())
            elif old and old["label"] != value:
                queue(field, value)
        elif field.startswith("contact:"):
            if any(equivalent(value["kind"], row["value"]) == value["match_value"] for row in old["rows"]):
                continue
            if (created or not old["rows"] and not old["primary"]
                    or equivalent(value["kind"], old["primary"]) == value["match_value"]):
                add_contact(client, candidate, value)
            else:
                queue(field, value)
        elif field == "classification":
            if {k: old[k] for k in ("client_type", "legal_form")} == value:
                continue
            if created:
                profile.legal_form = value["legal_form"]
                profile.save(update_fields=["legal_form"])
            else:
                queue(field, value)
        elif ((field in ("first_name", "last_name", "middle_name") and client.client_type != "private")
                or (field == "company_name" and client.client_type != "legal")):
            queue(field, value)
        elif not equivalent(field, old):
            write_scalar(client, profile, field, value)
        elif equivalent(field, old) != equivalent(field, value):
            queue(field, value)
    # The review starts after this import's own safe fills. Do not rebase an
    # older pending comparison: only an explicit review POST may do that.
    for row in new_proposals:
        row.before = json_value(current(client, profile, row.field))
        row.context = context(candidate, client, profile, row.field)
        row.save(update_fields=["before", "context"])


def editor_revision(client, profile):
    """Optimistic token for the full manual editor; sync metadata is excluded."""
    return digest([dict((f, scalar(getattr(client, f))) for f in (*CLIENT_FIELDS, "client_type", "contact_position")),
        dict((f, scalar(getattr(profile, f))) for f in (*PROFILE_FIELDS, "legal_form", "ogrn", "notes", "manager_id", "responsible_id")),
        list(ClientContact.objects.filter(client=client).order_by("pk").values())])


def token(proposal):
    return digest([proposal.pk, proposal.source_version, proposal.before, proposal.after, proposal.context])


@transaction.atomic
def decide(*, actor, organization_id, proposal_id, decision, expected_token):
    from django.contrib.auth import get_user_model
    from .services.permissions import company_has_access
    org = Organization.objects.select_for_update().get(pk=organization_id)
    actor = get_user_model().objects.select_for_update().filter(pk=actor.pk, is_active=True).first()
    if (not actor or not company_has_access(org) or not (actor.is_superuser or
        OrganizationAccess.objects.select_for_update().filter(user=actor, organization=org, role__in=("owner", "admin")).exists())):
        raise PermissionDenied
    if decision not in ("accepted", "rejected", "refresh"):
        raise ValidationError("Недопустимое решение")
    row = ClientImportProposal.objects.filter(pk=proposal_id, candidate__organization=org,
        client__organization=org).first()
    if not row:
        raise PermissionDenied
    candidate = ClientImportCandidate.objects.get(pk=row.candidate_id, organization=org)
    locked = lock_clients(candidate, row.client_id)
    candidate = ClientImportCandidate.objects.select_for_update().get(pk=candidate.pk, organization=org)
    client = locked[row.client_id]
    profile = ClientCRMProfile.objects.select_for_update().get(client=client)
    row = ClientImportProposal.objects.select_for_update().get(pk=row.pk)
    if expected_token != token(row):
        raise ValidationError("Предложение изменилось. Обновите страницу.")
    if row.state == decision:
        return row  # exact repeat never writes again, even after later manual edits
    if row.state != "pending" and not (decision == "refresh" and row.state == "stale"):
        raise ValidationError("По этому предложению уже принято решение.")
    try:
        ctx = context(candidate, client, profile, row.field)
    except ValueError:
        ctx = None
    values = source_values(candidate, person=client.pk != candidate.matched_client_id, client=client)
    state = (candidate.review_state or {}).get(str(client.pk) + ":" + row.field, {})
    if decision == "refresh":
        if (not ctx or not candidate.source_available or candidate.status not in ("ready", "imported")
                or state.get("version") != row.source_version
                or equivalent(row.field, json_value(values.get(row.field))) != equivalent(row.field, row.after)):
            raise ValidationError("Источник изменился. Используйте новое предложение.")
        row.before = json_value(current(client, profile, row.field))
        row.context = ctx
        row.state = "pending"
        row.save(update_fields=["before", "context", "state"])
        return row
    if (not candidate.source_available or candidate.status not in ("ready", "imported")
            or ctx != row.context or state.get("version") != row.source_version
            or equivalent(row.field, json_value(values.get(row.field))) != equivalent(row.field, row.after)
            or json_value(current(client, profile, row.field)) != row.before):
        row.state = "stale"
        row.save(update_fields=["state"])
        return row
    if decision == "accepted":
        if row.field == "classification":
            # The comparison explicitly includes these exact source-owned links.
            # Manual or unrelated links must be resolved in the card first.
            links = list(ClientCompanyLink.objects.select_for_update().filter(Q(company=client) | Q(person=client))
                .select_related("company", "person"))
            removable = [link for link in links if link.company_id == client.pk and link.automatic
                and link.source == "onec_ip" and link.source_reference == candidate.source_ref
                and link.company.organization_id == org.pk and link.person.organization_id == org.pk
                and row.after["legal_form"] != "ip"]
            if row.after["client_type"] != client.client_type and len(links) != len(removable):
                raise ValidationError("Сначала проверьте ручные связи в карточке клиента.")
            ClientCompanyLink.objects.filter(pk__in=[link.pk for link in removable]).delete()
            client.client_type = row.after["client_type"]
            client.save(update_fields=["client_type"])
            profile.legal_form = row.after["legal_form"]
            profile.save(update_fields=["legal_form"])
        elif row.field == "ip_person":
            from .client_crm_import import _find_or_create_ip_person
            _find_or_create_ip_person(client, candidate, allow_create=True)
        elif row.field.startswith("contact_label:"):
            ClientContact.objects.filter(client=client, pk=int(row.field.split(":")[1])).update(label=row.after, updated_at=timezone.now())
        elif row.field.startswith("contact:"):
            add_contact(client, candidate, row.after)
        elif row.field in (*CLIENT_FIELDS, *PROFILE_FIELDS):
            if ((row.field in ("first_name", "last_name", "middle_name") and client.client_type != "private")
                    or (row.field == "company_name" and client.client_type != "legal")):
                raise ValidationError("Сначала подтвердите изменение типа клиента.")
            if row.field in ("phone", "email"):
                previous = getattr(client, row.field)
                if previous:
                    # Preserve a legacy scalar as an additional contact before replacing it.
                    old_data = dict(kind=row.field, value=previous, match_value=equivalent(row.field, previous), label="")
                    if not any(equivalent(row.field, c.value) == old_data["match_value"]
                            for c in ClientContact.objects.filter(client=client, kind=row.field)):
                        ClientContact.objects.create(client=client, **old_data, sources=["legacy_primary"])
                contact = add_contact(client, candidate, dict(kind=row.field, value=row.after,
                    match_value=equivalent(row.field, row.after), label=""))
                ClientContact.objects.filter(client=client, kind=row.field).update(is_primary=False)
                contact.is_primary = True
                contact.save(update_fields=["is_primary"])
            write_scalar(client, profile, row.field, row.after)
        else:
            raise ValidationError("Неизвестное поле")
    row.state, row.decided_by, row.decided_at = decision, actor, timezone.now()
    row.save(update_fields=["state", "decided_by", "decided_at"])
    return row
