"""Automatic call titles; explicitly edited lead names are preserved."""

LEGACY_CALL_TITLE = "Входящий звонок — требуется уточнение"


def has_manual_title(lead, edited_ids=None):
    if lead.title_is_manual or lead.identity_origin == "legacy" and lead.acquisition_source == "manual":
        return True
    if lead.identity_origin == "legacy":
        if edited_ids is not None:
            return lead.pk in edited_ids
        from pool_service.crm_commercial_models import CrmEvent
        return CrmEvent.objects.filter(organization=lead.organization, lead=lead,
            event_type="lead.edit", safe_diff__title_changed__after=True).exists()
    return False


def call_title(phone, missed=False, direction="in"):
    label = "Исходящий звонок" if direction == "out" else "Пропущенный звонок" if missed else "Входящий звонок"
    return (label + (" · " + phone.strip() if phone else " · номер не определён"))[:255]


def automatic_call_title(title, phone, missed=False, direction="in"):
    if title in {LEGACY_CALL_TITLE, call_title(phone), call_title(phone, True), call_title(phone, direction="out")}:
        return call_title(phone, missed, direction)
    return title


def display_call_title(lead, row, calls=None):
    """Keep stored generated text stable; render current contact names on reads."""
    from pool_service.communication_models import PhoneCall
    from pool_service.crm_commercial_models import CrmLeadSource
    name = " · ".join(item["name"] for item in (row["contact"], row["company"]) if item)
    if lead.title == "Входящее обращение — требуется уточнение" and row["source"]:
        return ("Входящее обращение · " + name)[:255] if name else lead.title
    if calls is None:
        calls = list(PhoneCall.objects.filter(organization=lead.organization,
            pk__in=CrmLeadSource.objects.filter(organization=lead.organization, lead=lead,
                inbox__organization=lead.organization, inbox__source_type="call").values("inbox__source_id")
        ).order_by("-started_at", "-pk").values("phone_number", "result", "direction"))
    generated = {LEGACY_CALL_TITLE}
    for call in calls:
        generated.update((call_title(call["phone_number"]), call_title(call["phone_number"], True), call_title(call["phone_number"], direction="out")))
    if lead.title not in generated or not calls:
        return lead.title
    return call_title(name or calls[0]["phone_number"], calls[0]["result"] == "missed", calls[0]["direction"])
