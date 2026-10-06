from django.db import migrations


def _normalize_phone(value):
    digits = "".join(ch for ch in str(value or "") if ch.isdigit())
    if len(digits) == 10:
        digits = "7" + digits
    elif len(digits) == 11 and digits[0] in {"7", "8"}:
        digits = "7" + digits[1:]
    return digits if 10 <= len(digits) <= 15 else ""


def _format_phone(value):
    raw = str(value or "").strip()
    normalized = _normalize_phone(raw)
    if len(normalized) == 11 and normalized.startswith("7"):
        return f"+7 {normalized[1:4]} {normalized[4:7]} {normalized[7:11]}"
    return raw


def normalize_existing_phone_data(apps, schema_editor):
    Client = apps.get_model("pool_service", "Client")
    ClientCRMProfile = apps.get_model("pool_service", "ClientCRMProfile")
    ClientCompanyLink = apps.get_model("pool_service", "ClientCompanyLink")
    ClientContact = apps.get_model("pool_service", "ClientContact")
    ClientImportCandidate = apps.get_model("pool_service", "ClientImportCandidate")
    PhoneCall = apps.get_model("pool_service", "PhoneCall")

    # Canonicalize primary client phones.
    for client in Client.objects.exclude(phone__isnull=True).exclude(phone="").iterator():
        formatted = _format_phone(client.phone)
        if formatted and formatted != client.phone:
            Client.objects.filter(pk=client.pk).update(phone=formatted)

    # Canonicalize CRM phone contacts and merge equivalent formatting variants.
    grouped = {}
    for contact in ClientContact.objects.filter(kind="phone").order_by("client_id", "id").iterator():
        normalized = _normalize_phone(contact.value) or _normalize_phone(contact.match_value)
        if not normalized:
            continue
        grouped.setdefault((contact.client_id, normalized), []).append(contact)

    for (_client_id, normalized), contacts in grouped.items():
        keeper = contacts[0]
        formatted = _format_phone(normalized)

        merged_sources = []
        merged_label = ""
        merged_reference = ""
        is_primary = False
        for item in contacts:
            for source in (item.sources or []):
                if source not in merged_sources:
                    merged_sources.append(source)
            merged_label = merged_label or (item.label or "")
            merged_reference = merged_reference or (item.source_reference or "")
            is_primary = is_primary or bool(item.is_primary)

        duplicate_ids = [item.pk for item in contacts[1:]]
        if duplicate_ids:
            ClientContact.objects.filter(pk__in=duplicate_ids).delete()

        ClientContact.objects.filter(pk=keeper.pk).update(
            value=formatted,
            match_value=normalized,
            label=merged_label,
            source_reference=merged_reference,
            sources=merged_sources,
            is_primary=is_primary,
        )

    # Keep staged/imported 1C data canonical too, including JSON payload contacts.
    for candidate in ClientImportCandidate.objects.all().iterator():
        changed = False
        phone = _format_phone(candidate.phone)
        payload = dict(candidate.payload or {})
        raw_phones = list(payload.get("phones") or [])
        normalized_phones = []
        seen = set()
        for item in raw_phones:
            item = dict(item or {})
            normalized = _normalize_phone(item.get("value") or item.get("match"))
            if not normalized:
                normalized_phones.append(item)
                continue
            if normalized in seen:
                continue
            seen.add(normalized)
            item["value"] = _format_phone(normalized)
            item["match"] = normalized
            normalized_phones.append(item)
        if raw_phones != normalized_phones:
            payload["phones"] = normalized_phones
            changed = True
        if phone != (candidate.phone or ""):
            candidate.phone = phone
            changed = True
        if changed:
            candidate.payload = payload
            candidate.save(update_fields=["phone", "payload"])

    # Normalize the visible phone in historical calls.
    for call in PhoneCall.objects.exclude(phone_number="").iterator():
        formatted = _format_phone(call.phone_number)
        if formatted and formatted != call.phone_number:
            PhoneCall.objects.filter(pk=call.pk).update(phone_number=formatted)

    # Build a phone -> active client map using both main and additional phones.
    merged_ids = set(
        ClientCRMProfile.objects.exclude(merged_into_id__isnull=True)
        .values_list("client_id", flat=True)
    )
    active_clients = list(
        Client.objects.exclude(pk__in=merged_ids)
        .only("id", "organization_id", "phone")
    )
    active_by_id = {client.id: client for client in active_clients}
    phone_map = {}
    for client in active_clients:
        normalized = _normalize_phone(client.phone)
        if normalized:
            phone_map.setdefault((client.organization_id, normalized), set()).add(client.id)

    for contact in ClientContact.objects.filter(kind="phone").only(
        "client_id", "match_value", "value"
    ).iterator():
        if contact.client_id in merged_ids:
            continue
        normalized = _normalize_phone(contact.match_value or contact.value)
        if not normalized:
            continue
        client = active_by_id.get(contact.client_id)
        if client is not None:
            phone_map.setdefault((client.organization_id, normalized), set()).add(client.id)

    profile_legal_form = dict(
        ClientCRMProfile.objects.filter(client_id__in=[item.id for item in active_clients])
        .values_list("client_id", "legal_form")
    )
    link_pairs = set(
        ClientCompanyLink.objects.values_list("company_id", "person_id")
    )

    def resolve_client_id(candidate_ids):
        if len(candidate_ids) == 1:
            return next(iter(candidate_ids))
        ip_companies = [
            client_id for client_id in candidate_ids
            if profile_legal_form.get(client_id) == "ip"
        ]
        if len(ip_companies) != 1:
            return None
        company_id = ip_companies[0]
        if all(
            client_id == company_id or (company_id, client_id) in link_pairs
            for client_id in candidate_ids
        ):
            return company_id
        return None

    names = dict(Client.objects.values_list("id", "name"))
    for call in PhoneCall.objects.filter(client_id__isnull=True).only(
        "id", "organization_id", "phone_number"
    ).iterator():
        normalized = _normalize_phone(call.phone_number)
        if not normalized:
            continue
        client_id = resolve_client_id(
            phone_map.get((call.organization_id, normalized), set())
        )
        if not client_id:
            continue
        PhoneCall.objects.filter(pk=call.pk, client_id__isnull=True).update(
            client_id=client_id,
            contact_name=names.get(client_id, ""),
        )


class Migration(migrations.Migration):
    dependencies = [
        ("pool_service", "0128_client_import_resolution_merge"),
    ]

    operations = [
        migrations.RunPython(
            normalize_existing_phone_data,
            migrations.RunPython.noop,
        ),
    ]
