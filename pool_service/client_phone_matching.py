from __future__ import annotations

from django.db.models import Q

from .client_crm_models import ClientContact
from .client_queries import active_clients
from .models import Client
from .phone_utils import normalize_phone


def clients_by_phone(organization, value, *, limit=10):
    """Resolve every active CRM client sharing a normalized phone.

    ClientContact is authoritative for the CRM layer; legacy Client.phone is
    included for backwards compatibility until every old card has contacts.
    No arbitrary winner is selected when more than one client shares a number.
    """
    match_value = normalize_phone(value)
    if not match_value or organization is None:
        return []

    base = active_clients(
        Client.objects.filter(organization=organization)
    )

    ids = set(
        ClientContact.objects.filter(
            client__organization=organization,
            kind=ClientContact.KIND_PHONE,
            match_value=match_value,
        )
        .filter(
            Q(client__crm_profile__isnull=True)
            | Q(client__crm_profile__merged_into__isnull=True)
        )
        .values_list("client_id", flat=True)
    )

    # Legacy fallback. Keep this until old code paths no longer write Client.phone.
    for client in base.exclude(phone__isnull=True).exclude(phone="").only(
        "id", "phone"
    ):
        if normalize_phone(client.phone) == match_value:
            ids.add(client.id)
            if len(ids) >= limit:
                break

    if not ids:
        return []
    return list(base.filter(id__in=ids).order_by("name", "id")[:limit])
