from __future__ import annotations

from collections import defaultdict

from django.db.models import Q

from .client_crm_models import ClientContact
from .client_queries import active_clients
from .models import Client
from .phone_utils import normalize_phone


def clients_by_phones(organization, values, *, limit=10):
    """Resolve multiple phone values with one CRM lookup pass.

    ClientContact is authoritative; legacy Client.phone is scanned once for
    backwards compatibility. Results are keyed by the shared normalized phone
    value and never choose an arbitrary client when a number is ambiguous.
    """
    if organization is None:
        return {}

    match_values = {
        normalized
        for value in values
        if (normalized := normalize_phone(value))
    }
    if not match_values:
        return {}

    base = active_clients(Client.objects.filter(organization=organization))
    ids_by_match = {value: set() for value in match_values}

    contact_rows = (
        ClientContact.objects.filter(
            client__organization=organization,
            kind=ClientContact.KIND_PHONE,
            match_value__in=match_values,
        )
        .filter(
            Q(client__crm_profile__isnull=True)
            | Q(client__crm_profile__merged_into__isnull=True)
        )
        .values_list("match_value", "client_id")
    )
    for match_value, client_id in contact_rows:
        ids_by_match[match_value].add(client_id)

    # Legacy fallback. Scan the organization's old Client.phone fields once,
    # regardless of how many call numbers are being resolved.
    for client in base.exclude(phone__isnull=True).exclude(phone="").only(
        "id", "phone"
    ):
        match_value = normalize_phone(client.phone)
        if match_value in ids_by_match:
            ids_by_match[match_value].add(client.id)

    all_client_ids = {
        client_id
        for client_ids in ids_by_match.values()
        for client_id in client_ids
    }
    results = {value: [] for value in match_values}
    if not all_client_ids:
        return results

    matches_by_client = defaultdict(set)
    for match_value, client_ids in ids_by_match.items():
        for client_id in client_ids:
            matches_by_client[client_id].add(match_value)

    clients = base.filter(id__in=all_client_ids).order_by("name", "id")
    for client in clients:
        for match_value in matches_by_client.get(client.id, ()):
            if len(results[match_value]) < limit:
                results[match_value].append(client)
    return results


def clients_by_phone(organization, value, *, limit=10):
    """Resolve every active CRM client sharing one normalized phone."""
    match_value = normalize_phone(value)
    if not match_value:
        return []
    return clients_by_phones(
        organization,
        [value],
        limit=limit,
    ).get(match_value, [])
