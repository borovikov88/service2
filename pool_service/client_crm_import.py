from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime
import re

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from .models import Client, Organization
from .client_crm_models import (
    ClientCRMProfile,
    ClientCompanyLink,
    ClientContact,
    ClientImportCandidate,
)
from .onec_diagnostic import config_from_settings
from .onec_diagnostic_universal import query_1c_rows


BUYER_ENTITY = "Catalog_Контрагенты"
CONTACT_ENTITY = "Catalog_Контрагенты_КонтактнаяИнформация"
PAGE_SIZE = 500
CONTACT_BATCH_SIZE = 40
SYSTEM_NAMES = {"розничный покупатель"}


def normalize_phone(value: str | None) -> str:
    digits = "".join(ch for ch in str(value or "") if ch.isdigit())
    if len(digits) == 11 and digits[0] in {"7", "8"}:
        digits = "7" + digits[1:]
    elif len(digits) == 10:
        digits = "7" + digits
    return digits if 10 <= len(digits) <= 15 else ""


def normalize_email(value: str | None) -> str:
    return str(value or "").strip().lower()


def _parse_date(value):
    if not value or str(value).startswith("0001-01-01"):
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).date()
    except (TypeError, ValueError):
        try:
            return date.fromisoformat(str(value)[:10])
        except (TypeError, ValueError):
            return None


def _target_organization() -> Organization:
    raw = getattr(settings, "ONEC_ODATA_TARGET_ORGANIZATION_ID", "")
    try:
        organization_id = int(raw)
    except (TypeError, ValueError) as exc:
        raise RuntimeError("ONEC_ODATA_TARGET_ORGANIZATION_ID is not configured") from exc
    return Organization.objects.get(pk=organization_id)


def _buyer_rows(config):
    cursor = ""
    while True:
        filters = [
            {"field": "DeletionMark", "op": "eq", "value": False},
            {"field": "Покупатель", "op": "eq", "value": True},
        ]
        if cursor:
            filters.append({"field": "Code", "op": "gt", "value": cursor})
        result = query_1c_rows(
            config,
            BUYER_ENTITY,
            fields=[
                "Ref_Key",
                "Code",
                "Description",
                "НаименованиеПолное",
                "ЮридическоеФизическоеЛицо",
                "ВидКонтрагента",
                "ИНН",
                "КПП",
                "ФИО",
                "ДатаРождения",
                "НомерТелефонаДляПоиска",
                "АдресЭПДляПоиска",
                "Покупатель",
                "Недействителен",
            ],
            filters=filters,
            limit=PAGE_SIZE,
            include_deleted=False,
            include_inactive=False,
            order_by=[{"field": "Code", "direction": "asc"}],
        )
        rows = result.get("rows", [])
        if not rows:
            break
        for row in rows:
            yield row
        if result.get("complete") or len(rows) < PAGE_SIZE:
            break
        next_cursor = str(rows[-1].get("Code") or "")
        if not next_cursor or next_cursor == cursor:
            raise RuntimeError("1C buyer pagination did not advance")
        cursor = next_cursor


def _contact_rows(config, refs):
    contact_map = defaultdict(list)
    refs = [ref for ref in refs if ref]
    for start in range(0, len(refs), CONTACT_BATCH_SIZE):
        batch = refs[start : start + CONTACT_BATCH_SIZE]
        result = query_1c_rows(
            config,
            CONTACT_ENTITY,
            fields=[
                "Ref_Key",
                "LineNumber",
                "Тип",
                "Представление",
                "НомерТелефона",
                "НомерТелефонаБезКодов",
                "АдресЭП",
            ],
            filters=[{"field": "Ref_Key", "op": "in", "value": batch}],
            limit=500,
            include_deleted=False,
            include_inactive=False,
            order_by=[
                {"field": "Ref_Key", "direction": "asc"},
                {"field": "LineNumber", "direction": "asc"},
            ],
        )
        for row in result.get("rows", []):
            contact_map[str(row.get("Ref_Key") or "")].append(row)
    return contact_map


def _source_kind(row):
    kind = str(row.get("ВидКонтрагента") or "")
    if kind == "ИндивидуальныйПредприниматель":
        return ClientImportCandidate.KIND_IP
    if kind == "ЮридическоеЛицо":
        return ClientImportCandidate.KIND_LEGAL
    return ClientImportCandidate.KIND_PRIVATE


def _candidate_contacts(row, extra_rows):
    phones = []
    emails = []

    def add_phone(raw, label=""):
        value = str(raw or "").strip()
        normalized = normalize_phone(value)
        if value and normalized and all(item["match"] != normalized for item in phones):
            phones.append({"value": value, "match": normalized, "label": label})

    def add_email(raw, label=""):
        value = str(raw or "").strip()
        normalized = normalize_email(value)
        if value and normalized and all(item["match"] != normalized for item in emails):
            emails.append({"value": value, "match": normalized, "label": label})

    add_phone(row.get("НомерТелефонаДляПоиска"), "Основной")
    add_email(row.get("АдресЭПДляПоиска"), "Основной")
    for contact in extra_rows:
        label = str(contact.get("Представление") or "")
        kind = str(contact.get("Тип") or "")
        if "Телефон" in kind:
            add_phone(contact.get("НомерТелефона") or contact.get("Представление"), label)
        elif "Почт" in kind:
            add_email(contact.get("АдресЭП") or contact.get("Представление"), label)
    return phones, emails


def _match_clients(organization, source_kind, inn, phones):
    matches = Client.objects.filter(organization=organization)
    if source_kind in {ClientImportCandidate.KIND_LEGAL, ClientImportCandidate.KIND_IP} and inn:
        by_inn = list(matches.filter(client_type="legal", inn=inn)[:3])
        if by_inn:
            return by_inn, "ИНН"
    phone_values = {item["match"] for item in phones if item.get("match")}
    if phone_values:
        contact_ids = ClientContact.objects.filter(
            kind=ClientContact.KIND_PHONE,
            match_value__in=phone_values,
            client__organization=organization,
        ).values_list("client_id", flat=True)
        legacy_ids = []
        for client in matches.exclude(phone__isnull=True).exclude(phone="").only("id", "phone"):
            if normalize_phone(client.phone) in phone_values:
                legacy_ids.append(client.id)
        ids = set(contact_ids) | set(legacy_ids)
        if ids:
            return list(matches.filter(id__in=ids)[:4]), "телефон"
    return [], ""


def scan_onec_clients():
    organization = _target_organization()
    config = config_from_settings()
    rows = list(_buyer_rows(config))
    contacts_by_ref = _contact_rows(config, [row.get("Ref_Key") for row in rows])

    totals = defaultdict(int)
    with transaction.atomic():
        for row in rows:
            ref = str(row.get("Ref_Key") or "")
            name = str(row.get("Description") or row.get("НаименованиеПолное") or "").strip()
            source_kind = _source_kind(row)
            inn = str(row.get("ИНН") or "").strip()
            phones, emails = _candidate_contacts(row, contacts_by_ref.get(ref, []))
            matches, match_reason = _match_clients(organization, source_kind, inn, phones)

            status = ClientImportCandidate.STATUS_READY
            reason = ""
            matched_client = None
            if not ref or not name:
                status = ClientImportCandidate.STATUS_INVALID
                reason = "Нет идентификатора или имени в 1С"
            elif name.casefold().strip() in SYSTEM_NAMES:
                status = ClientImportCandidate.STATUS_INVALID
                reason = "Системная карточка 1С"
            elif len(matches) > 1:
                status = ClientImportCandidate.STATUS_DUPLICATE
                reason = f"Несколько карточек Service2 совпали по: {match_reason}"
            elif len(matches) == 1:
                matched_client = matches[0]
            elif source_kind == ClientImportCandidate.KIND_LEGAL and not inn:
                status = ClientImportCandidate.STATUS_REVIEW
                reason = "У юридического лица не заполнен ИНН"

            payload = {
                "phones": phones,
                "emails": emails,
                "onec_type": str(row.get("ВидКонтрагента") or ""),
                "onec_person_kind": str(row.get("ЮридическоеФизическоеЛицо") or ""),
                "fio": str(row.get("ФИО") or "").strip(),
            }
            candidate, _ = ClientImportCandidate.objects.update_or_create(
                organization=organization,
                source_ref=ref,
                defaults={
                    "source_code": str(row.get("Code") or ""),
                    "source_kind": source_kind,
                    "name": name,
                    "legal_name": str(row.get("НаименованиеПолное") or "").strip(),
                    "inn": inn,
                    "kpp": str(row.get("КПП") or "").strip(),
                    "phone": phones[0]["value"] if phones else "",
                    "email": emails[0]["value"] if emails else "",
                    "birth_date": _parse_date(row.get("ДатаРождения")),
                    "payload": payload,
                    "status": status if status != ClientImportCandidate.STATUS_READY or not matched_client else ClientImportCandidate.STATUS_READY,
                    "reason": reason,
                    "matched_client": matched_client,
                },
            )
            if candidate.applied_at and matched_client and candidate.status == ClientImportCandidate.STATUS_READY:
                candidate.status = ClientImportCandidate.STATUS_IMPORTED
                candidate.save(update_fields=["status", "updated_at"])
            totals[candidate.status] += 1
            totals[source_kind] += 1
    totals["total"] = len(rows)
    return dict(totals)


def _split_person_name(value):
    cleaned = re.sub(r"(?i)\bИП\b", " ", str(value or "")).replace('"', " ")
    parts = [part for part in re.split(r"\s+", cleaned.strip()) if part]
    last_name = parts[0] if parts else ""
    first_name = parts[1] if len(parts) > 1 else ""
    middle_name = " ".join(parts[2:]) if len(parts) > 2 else ""
    return last_name, first_name, middle_name


def _sync_contacts(client, candidate):
    payload = candidate.payload or {}
    sources = ["onec"]
    for kind, key in ((ClientContact.KIND_PHONE, "phones"), (ClientContact.KIND_EMAIL, "emails")):
        for item in payload.get(key, []):
            value = str(item.get("value") or "").strip()
            if not value:
                continue
            match_value = normalize_phone(value) if kind == ClientContact.KIND_PHONE else normalize_email(value)
            contact, created = ClientContact.objects.get_or_create(
                client=client,
                kind=kind,
                value=value,
                defaults={
                    "match_value": match_value,
                    "label": str(item.get("label") or "")[:120],
                    "is_primary": False,
                    "sources": sources,
                    "source_reference": candidate.source_ref,
                },
            )
            if not created:
                changed = False
                if contact.match_value != match_value:
                    contact.match_value = match_value
                    changed = True
                if "onec" not in (contact.sources or []):
                    contact.sources = list(contact.sources or []) + ["onec"]
                    changed = True
                if changed:
                    contact.save(update_fields=["match_value", "sources", "updated_at"])


def _find_or_create_ip_person(company, candidate):
    phones = (candidate.payload or {}).get("phones", [])
    phone_values = {item.get("match") for item in phones if item.get("match")}
    person_matches = Client.objects.filter(
        organization=candidate.organization,
        client_type="private",
    )
    ids = set()
    if phone_values:
        ids.update(
            ClientContact.objects.filter(
                client__in=person_matches,
                kind=ClientContact.KIND_PHONE,
                match_value__in=phone_values,
            ).values_list("client_id", flat=True)
        )
        for person in person_matches.exclude(phone__isnull=True).exclude(phone="").only("id", "phone"):
            if normalize_phone(person.phone) in phone_values:
                ids.add(person.id)
    if len(ids) > 1:
        raise ValueError("Для ИП найдено несколько физлиц с тем же телефоном")
    person = person_matches.filter(id=next(iter(ids))).first() if ids else None

    last_name, first_name, middle_name = _split_person_name(
        (candidate.payload or {}).get("fio") or candidate.name
    )
    if person is None:
        exact_name = " ".join(part for part in [last_name, first_name, middle_name] if part).strip()
        named = list(person_matches.filter(name__iexact=exact_name)[:2]) if exact_name else []
        if len(named) == 1:
            person = named[0]
        elif len(named) > 1:
            raise ValueError("Для ИП найдено несколько физлиц с тем же ФИО")
    if person is None:
        person = Client.objects.create(
            organization=candidate.organization,
            client_type="private",
            name=" ".join(part for part in [last_name, first_name, middle_name] if part).strip() or candidate.name,
            first_name=first_name or None,
            last_name=last_name or None,
            phone=candidate.phone or None,
            email=candidate.email or None,
        )
    profile, _ = ClientCRMProfile.objects.get_or_create(client=person)
    if middle_name and not profile.middle_name:
        profile.middle_name = middle_name
    if candidate.birth_date and not profile.birth_date:
        profile.birth_date = candidate.birth_date
    if profile.source == ClientCRMProfile.SOURCE_MANUAL:
        profile.source = ClientCRMProfile.SOURCE_ONEC_IP
    profile.save()
    _sync_contacts(person, candidate)
    ClientCompanyLink.objects.update_or_create(
        company=company,
        person=person,
        defaults={
            "is_primary": True,
            "source": ClientCompanyLink.SOURCE_ONEC_IP,
            "source_reference": candidate.source_ref,
            "automatic": True,
        },
    )
    return person


@transaction.atomic
def apply_candidate(candidate):
    candidate = ClientImportCandidate.objects.select_for_update().select_related(
        "organization", "matched_client"
    ).get(pk=candidate.pk)
    if candidate.status not in {
        ClientImportCandidate.STATUS_READY,
        ClientImportCandidate.STATUS_IMPORTED,
    }:
        raise ValueError("Карточка требует ручной проверки")

    client = candidate.matched_client
    if client is None:
        client_type = "private" if candidate.source_kind == ClientImportCandidate.KIND_PRIVATE else "legal"
        last_name = first_name = middle_name = ""
        if client_type == "private":
            last_name, first_name, middle_name = _split_person_name(candidate.name)
        client = Client.objects.create(
            organization=candidate.organization,
            client_type=client_type,
            name=candidate.name,
            company_name=candidate.name if client_type == "legal" else None,
            first_name=first_name or None,
            last_name=last_name or None,
            phone=candidate.phone or None,
            email=candidate.email or None,
            inn=candidate.inn or None,
        )
    else:
        if candidate.inn and not client.inn:
            client.inn = candidate.inn
        if not client.phone and candidate.phone:
            client.phone = candidate.phone
        if not client.email and candidate.email:
            client.email = candidate.email
        client.save()

    profile, _ = ClientCRMProfile.objects.get_or_create(client=client)
    profile.legal_form = (
        ClientCRMProfile.LEGAL_FORM_IP
        if candidate.source_kind == ClientImportCandidate.KIND_IP
        else ClientCRMProfile.LEGAL_FORM_ENTITY
        if candidate.source_kind == ClientImportCandidate.KIND_LEGAL
        else ClientCRMProfile.LEGAL_FORM_NONE
    )
    profile.middle_name = profile.middle_name or (_split_person_name(candidate.name)[2] if client.client_type == "private" else "")
    profile.birth_date = candidate.birth_date or profile.birth_date
    profile.legal_name = candidate.legal_name or profile.legal_name
    profile.kpp = candidate.kpp or profile.kpp
    profile.onec_ref = candidate.source_ref
    profile.onec_code = candidate.source_code
    profile.onec_name = candidate.name
    profile.source = ClientCRMProfile.SOURCE_ONEC
    profile.last_synced_at = timezone.now()
    profile.save()
    _sync_contacts(client, candidate)

    if candidate.source_kind == ClientImportCandidate.KIND_IP:
        _find_or_create_ip_person(client, candidate)

    candidate.matched_client = client
    candidate.status = ClientImportCandidate.STATUS_IMPORTED
    candidate.reason = ""
    candidate.applied_at = timezone.now()
    candidate.save(update_fields=["matched_client", "status", "reason", "applied_at", "updated_at"])
    return client


def apply_ready_candidates(organization=None):
    organization = organization or _target_organization()
    candidates = ClientImportCandidate.objects.filter(
        organization=organization,
        status=ClientImportCandidate.STATUS_READY,
    ).order_by("id")
    result = {"imported": 0, "failed": 0}
    for candidate in candidates.iterator():
        try:
            apply_candidate(candidate)
        except (ValueError, RuntimeError):
            candidate.status = ClientImportCandidate.STATUS_REVIEW
            candidate.reason = "Не удалось безопасно сопоставить карточку; требуется проверка"
            candidate.save(update_fields=["status", "reason", "updated_at"])
            result["failed"] += 1
        else:
            result["imported"] += 1
    return result
