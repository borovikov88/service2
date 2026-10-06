from django.db.models import Q

from .models import Client
from .phone_utils import format_phone, normalize_phone, phone_variants


def active_clients(queryset=None):
    """Return clients that are still active CRM identities."""
    queryset = queryset if queryset is not None else Client.objects.all()
    return queryset.filter(
        Q(crm_profile__isnull=True)
        | Q(crm_profile__merged_into__isnull=True)
    )


def find_active_client_by_phone(organization, phone):
    normalized = normalize_phone(phone)
    if not normalized:
        return None

    matches = list(
        active_clients(
            Client.objects.filter(
                organization=organization,
                crm_contacts__kind="phone",
                crm_contacts__match_value=normalized,
            )
        )
        .select_related("crm_profile")
        .distinct()
        .order_by("id")[:6]
    )

    if not matches:
        matches = list(
            active_clients(
                Client.objects.filter(
                    organization=organization,
                    phone__in=phone_variants(phone),
                )
            )
            .select_related("crm_profile")
            .distinct()
            .order_by("id")[:6]
        )

    if len(matches) == 1:
        return matches[0]
    if len(matches) < 2:
        return None

    match_ids = {item.id for item in matches}
    ip_companies = [
        item for item in matches
        if item.client_type == "legal"
        and getattr(getattr(item, "crm_profile", None), "legal_form", "") == "ip"
    ]
    if len(ip_companies) == 1:
        company = ip_companies[0]
        linked_person_ids = set(
            company.person_links.filter(person_id__in=match_ids)
            .values_list("person_id", flat=True)
        )
        if match_ids <= ({company.id} | linked_person_ids):
            return company
    return None


def relink_unassigned_calls_for_client(client):
    if not client or not client.organization_id:
        return 0

    phone_keys = set()
    main_phone = normalize_phone(client.phone)
    if main_phone:
        phone_keys.add(main_phone)
    phone_keys.update(
        value for value in client.crm_contacts.filter(kind="phone")
        .values_list("match_value", flat=True) if value
    )
    if not phone_keys:
        return 0

    from .communication_models import PhoneCall

    updated = 0
    calls = PhoneCall.objects.filter(
        organization_id=client.organization_id,
        client__isnull=True,
    ).only("id", "phone_number")
    for call in calls.iterator():
        if normalize_phone(call.phone_number) not in phone_keys:
            continue
        resolved = find_active_client_by_phone(client.organization, call.phone_number)
        if not resolved or resolved.id != client.id:
            continue
        PhoneCall.objects.filter(pk=call.pk, client__isnull=True).update(
            client=client,
            contact_name=client.name,
            phone_number=format_phone(call.phone_number),
        )
        updated += 1
    return updated
