from django.db import migrations


def _normalize_phone(value):
    digits = "".join(ch for ch in str(value or "") if ch.isdigit())
    if len(digits) == 11 and digits[0] in {"7", "8"}:
        digits = "7" + digits[1:]
    elif len(digits) == 10:
        digits = "7" + digits
    return digits if 10 <= len(digits) <= 15 else ""


def _format_phone(value):
    normalized = _normalize_phone(value)
    if len(normalized) == 11 and normalized.startswith("7"):
        return f"+7 {normalized[1:4]} {normalized[4:7]} {normalized[7:11]}"
    return str(value or "").strip()


def normalize_existing_client_phones(apps, schema_editor):
    Client = apps.get_model("pool_service", "Client")
    ClientContact = apps.get_model("pool_service", "ClientContact")
    PhoneCall = apps.get_model("pool_service", "PhoneCall")

    for client in Client.objects.exclude(phone__isnull=True).exclude(phone="").iterator():
        formatted = _format_phone(client.phone)
        if formatted and formatted != client.phone:
            Client.objects.filter(pk=client.pk).update(phone=formatted)

    for contact in ClientContact.objects.filter(kind="phone").order_by("id").iterator():
        formatted = _format_phone(contact.value)
        normalized = _normalize_phone(contact.value)
        if not normalized:
            continue

        duplicate = None
        if formatted:
            duplicate = (
                ClientContact.objects.filter(
                    client_id=contact.client_id,
                    kind="phone",
                    value=formatted,
                )
                .exclude(pk=contact.pk)
                .order_by("id")
                .first()
            )
        if duplicate:
            duplicate_sources = list(duplicate.sources or [])
            for source in list(contact.sources or []):
                if source not in duplicate_sources:
                    duplicate_sources.append(source)
            updates = {
                "match_value": normalized,
                "sources": duplicate_sources,
                "is_primary": bool(duplicate.is_primary or contact.is_primary),
            }
            if not duplicate.label and contact.label:
                updates["label"] = contact.label
            if not duplicate.source_reference and contact.source_reference:
                updates["source_reference"] = contact.source_reference
            ClientContact.objects.filter(pk=duplicate.pk).update(**updates)
            ClientContact.objects.filter(pk=contact.pk).delete()
            continue

        updates = {}
        if formatted and formatted != contact.value:
            updates["value"] = formatted
        if normalized != contact.match_value:
            updates["match_value"] = normalized
        if updates:
            ClientContact.objects.filter(pk=contact.pk).update(**updates)

    for call in PhoneCall.objects.exclude(phone_number="").iterator():
        formatted = _format_phone(call.phone_number)
        if formatted and formatted != call.phone_number:
            PhoneCall.objects.filter(pk=call.pk).update(phone_number=formatted)


class Migration(migrations.Migration):
    dependencies = [
        ("pool_service", "0128_client_import_resolution_merge"),
    ]

    operations = [
        migrations.RunPython(normalize_existing_client_phones, migrations.RunPython.noop),
    ]
