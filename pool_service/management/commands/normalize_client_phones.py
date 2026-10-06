from collections import defaultdict

from django.core.management.base import BaseCommand
from django.db import transaction

from pool_service.client_crm_models import ClientContact
from pool_service.client_queries import active_clients
from pool_service.communication_models import PhoneCall
from pool_service.models import Client
from pool_service.phone_utils import canonical_phone_value, normalize_phone


class Command(BaseCommand):
    help = "Audit and normalize CRM/client/call phone data. Dry-run by default."

    def add_arguments(self, parser):
        parser.add_argument(
            "--apply",
            action="store_true",
            help="Apply normalization after printing the audit summary.",
        )

    def handle(self, *args, **options):
        apply_changes = options["apply"]
        clients = list(
            Client.objects.select_related("organization")
            .exclude(phone__isnull=True)
            .exclude(phone="")
            .order_by("organization_id", "id")
        )
        contacts = list(
            ClientContact.objects.filter(kind=ClientContact.KIND_PHONE)
            .select_related("client__organization")
            .order_by("client_id", "-is_primary", "id")
        )
        calls = list(
            PhoneCall.objects.select_related("organization", "client")
            .exclude(phone_number="")
            .order_by("organization_id", "id")
        )

        all_values = [(item.organization_id, item.phone) for item in clients]
        all_values += [
            (item.client.organization_id, item.value) for item in contacts
        ]
        all_values += [(item.organization_id, item.phone_number) for item in calls]

        valid = 0
        invalid = 0
        for _organization_id, value in all_values:
            if normalize_phone(value):
                valid += 1
            else:
                invalid += 1

        ownership = defaultdict(set)
        for client in clients:
            normalized = normalize_phone(client.phone)
            if normalized:
                ownership[(client.organization_id, normalized)].add(client.id)
        for contact in contacts:
            normalized = normalize_phone(contact.value)
            if normalized:
                ownership[(contact.client.organization_id, normalized)].add(
                    contact.client_id
                )
        conflicts = {
            key: client_ids
            for key, client_ids in ownership.items()
            if len(client_ids) > 1
        }

        self.stdout.write(
            "Phone audit: "
            f"client={len(clients)}, contacts={len(contacts)}, calls={len(calls)}, "
            f"total={len(all_values)}, normalizable={valid}, invalid={invalid}, "
            f"cross_client_conflicts={len(conflicts)}"
        )
        conflict_client_count = len(
            {client_id for client_ids in conflicts.values() for client_id in client_ids}
        )
        self.stdout.write(
            f"Phone conflict summary: groups={len(conflicts)}, "
            f"affected_clients={conflict_client_count}"
        )

        if not apply_changes:
            self.stdout.write("Dry-run only. Re-run with --apply to persist changes.")
            return

        stats = defaultdict(int)
        with transaction.atomic():
            for client in clients:
                normalized = normalize_phone(client.phone)
                if not normalized:
                    continue
                display = canonical_phone_value(client.phone)
                if display != client.phone:
                    Client.objects.filter(pk=client.pk).update(phone=display)
                    client.phone = display
                    stats["client_phone_updated"] += 1

            grouped = defaultdict(list)
            for contact in contacts:
                normalized = normalize_phone(contact.value)
                if normalized:
                    grouped[(contact.client_id, normalized)].append(contact)
                else:
                    if contact.match_value:
                        ClientContact.objects.filter(pk=contact.pk).update(
                            match_value=""
                        )
                        stats["invalid_match_cleared"] += 1

            for (_client_id, normalized), items in grouped.items():
                keeper = sorted(
                    items,
                    key=lambda item: (not item.is_primary, item.id),
                )[0]
                sources = []
                for item in items:
                    for source in item.sources or []:
                        if source not in sources:
                            sources.append(source)
                keeper.sources = sources
                keeper.is_primary = any(item.is_primary for item in items)
                keeper.match_value = normalized
                keeper.value = canonical_phone_value(keeper.value)
                # Delete same-client duplicates before canonicalizing the keeper:
                # one duplicate may already own the canonical display value and
                # the model has a unique (client, kind, value) constraint.
                for duplicate in items:
                    if duplicate.pk == keeper.pk:
                        continue
                    duplicate.delete()
                    stats["duplicate_contacts_merged"] += 1
                keeper.save(
                    update_fields=[
                        "sources",
                        "is_primary",
                        "match_value",
                        "value",
                        "updated_at",
                    ]
                )
                stats["contacts_normalized"] += 1

            # Every legacy primary phone gets a normalized ClientContact so all
            # matching paths can use the same indexed value.
            for client in clients:
                normalized = normalize_phone(client.phone)
                if not normalized:
                    continue
                contact = (
                    ClientContact.objects.filter(
                        client=client,
                        kind=ClientContact.KIND_PHONE,
                        match_value=normalized,
                    )
                    .order_by("-is_primary", "id")
                    .first()
                )
                if contact is None:
                    ClientContact.objects.create(
                        client=client,
                        kind=ClientContact.KIND_PHONE,
                        value=canonical_phone_value(client.phone),
                        match_value=normalized,
                        label="Основной",
                        is_primary=True,
                        sources=["legacy"],
                    )
                    stats["legacy_contacts_created"] += 1

            # Build one in-memory resolver for the historical call pass.
            # This avoids rescanning all clients for every PhoneCall.
            active_client_list = list(
                active_clients(Client.objects.all()).only(
                    "id",
                    "organization_id",
                    "name",
                    "phone",
                )
            )
            client_by_id = {client.id: client for client in active_client_list}
            resolution = defaultdict(set)
            for active_client in active_client_list:
                normalized = normalize_phone(active_client.phone)
                if normalized:
                    resolution[
                        (active_client.organization_id, normalized)
                    ].add(active_client.id)
            active_ids = list(client_by_id)
            if active_ids:
                contact_rows = ClientContact.objects.filter(
                    client_id__in=active_ids,
                    kind=ClientContact.KIND_PHONE,
                ).values_list(
                    "client_id",
                    "client__organization_id",
                    "match_value",
                )
                for client_id, organization_id, match_value in contact_rows:
                    if match_value:
                        resolution[(organization_id, match_value)].add(client_id)

            for call in calls:
                normalized = normalize_phone(call.phone_number)
                match_ids = resolution.get(
                    (call.organization_id, normalized),
                    set(),
                )
                display = canonical_phone_value(call.phone_number)
                updates = {}
                if display != call.phone_number:
                    updates["phone_number"] = display

                # Existing non-null client links may have been selected manually
                # or created by another trusted workflow. Never overwrite them
                # during a phone-format backfill.
                if call.client_id is not None:
                    stats["calls_existing_assignment_preserved"] += 1
                    if updates:
                        PhoneCall.objects.filter(pk=call.pk).update(**updates)
                        stats["calls_updated"] += 1
                    continue

                if len(match_ids) == 1:
                    client_id = next(iter(match_ids))
                    matched_client = client_by_id[client_id]
                    if call.client_id != client_id:
                        updates["client_id"] = client_id
                    if call.contact_name != matched_client.name:
                        updates["contact_name"] = matched_client.name
                    stats["calls_unambiguous"] += 1
                elif len(match_ids) > 1:
                    if call.client_id is not None:
                        updates["client_id"] = None
                    if call.contact_name != "Несколько клиентов":
                        updates["contact_name"] = "Несколько клиентов"
                    stats["calls_ambiguous"] += 1
                else:
                    if call.client_id is not None:
                        updates["client_id"] = None
                    stats["calls_unmatched"] += 1
                if updates:
                    PhoneCall.objects.filter(pk=call.pk).update(**updates)
                    stats["calls_updated"] += 1

        self.stdout.write(
            self.style.SUCCESS(
                "Applied: " + ", ".join(
                    f"{key}={value}" for key, value in sorted(stats.items())
                )
            )
        )
