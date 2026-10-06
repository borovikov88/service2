from collections import defaultdict
from contextlib import nullcontext

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
        # The apply snapshot must be acquired inside the same transaction that
        # writes it. The separate deployment dry-run is never reused for writes.
        with transaction.atomic() if apply_changes else nullcontext():
            self._run(apply_changes=apply_changes)

    def _run(self, *, apply_changes):
        def read_rows(queryset):
            if apply_changes:
                queryset = queryset.select_for_update()
            return list(queryset)

        # Lock parents first, including clients without a legacy primary phone.
        # Do not join nullable relations in a FOR UPDATE query. Organization
        # lookup uses this locked snapshot, not lazy per-contact queries.
        all_clients = read_rows(Client.objects.order_by("id"))
        client_org = {item.pk: item.organization_id for item in all_clients}
        clients = [item for item in all_clients if item.phone]
        contacts = read_rows(
            ClientContact.objects.filter(kind=ClientContact.KIND_PHONE)
            .order_by("client_id", "id")
        )
        calls = read_rows(
            PhoneCall.objects.exclude(phone_number="").order_by("id")
        )

        all_values = [item.phone for item in clients]
        all_values += [item.value for item in contacts]
        all_values += [item.phone_number for item in calls]
        valid = sum(bool(normalize_phone(value)) for value in all_values)
        invalid = len(all_values) - valid
        ownership = defaultdict(set)
        for client in clients:
            normalized = normalize_phone(client.phone)
            if normalized:
                ownership[(client.organization_id, normalized)].add(client.id)
        for contact in contacts:
            normalized = normalize_phone(contact.value)
            if normalized:
                ownership[(client_org[contact.client_id], normalized)].add(
                    contact.client_id
                )
        conflicts = {
            key: ids for key, ids in ownership.items() if len(ids) > 1
        }
        self.stdout.write(
            "Phone audit: "
            f"client={len(clients)}, contacts={len(contacts)}, calls={len(calls)}, "
            f"total={len(all_values)}, normalizable={valid}, invalid={invalid}, "
            f"cross_client_conflicts={len(conflicts)}"
        )
        conflict_client_count = len(
            {client_id for ids in conflicts.values() for client_id in ids}
        )
        self.stdout.write(
            f"Phone conflict summary: groups={len(conflicts)}, "
            f"affected_clients={conflict_client_count}"
        )
        if not apply_changes:
            self.stdout.write("Dry-run only. Re-run with --apply to persist changes.")
            return

        stats = defaultdict(int)
        for client in clients:
            if not normalize_phone(client.phone):
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
            elif contact.match_value:
                ClientContact.objects.filter(pk=contact.pk).update(match_value="")
                stats["invalid_match_cleared"] += 1

        for (_client_id, normalized), items in grouped.items():
            keeper = min(items, key=lambda item: (not item.is_primary, item.id))
            sources = []
            for item in items:
                for source in item.sources or []:
                    if source not in sources:
                        sources.append(source)
            desired = {
                "sources": sources,
                "is_primary": any(item.is_primary for item in items),
                "match_value": normalized,
                "value": canonical_phone_value(keeper.value),
            }
            changed_fields = []
            for field, value in desired.items():
                if getattr(keeper, field) != value:
                    setattr(keeper, field, value)
                    changed_fields.append(field)
            # Remove duplicates before saving a canonical value which one of
            # them may already own. All candidate rows are locked above.
            for duplicate in items:
                if duplicate.pk != keeper.pk:
                    duplicate.delete()
                    stats["duplicate_contacts_merged"] += 1
            if changed_fields:
                keeper.save(update_fields=changed_fields + ["updated_at"])
                stats["contacts_normalized"] += 1

        for client in clients:
            normalized = normalize_phone(client.phone)
            if not normalized:
                continue
            if not ClientContact.objects.filter(
                client=client,
                kind=ClientContact.KIND_PHONE,
                match_value=normalized,
            ).exists():
                ClientContact.objects.create(
                    client=client,
                    kind=ClientContact.KIND_PHONE,
                    value=canonical_phone_value(client.phone),
                    match_value=normalized,
                    label="\u041e\u0441\u043d\u043e\u0432\u043d\u043e\u0439",
                    is_primary=True,
                    sources=["legacy"],
                )
                stats["legacy_contacts_created"] += 1

        # Resolve all historical calls in one pass over the locked clients.
        active_ids = set(
            active_clients(Client.objects.all()).values_list("id", flat=True)
        )
        client_by_id = {
            item.id: item for item in all_clients if item.id in active_ids
        }
        resolution = defaultdict(set)
        for client in client_by_id.values():
            normalized = normalize_phone(client.phone)
            if normalized:
                resolution[(client.organization_id, normalized)].add(client.id)
        if client_by_id:
            contact_rows = ClientContact.objects.filter(
                client_id__in=client_by_id,
                kind=ClientContact.KIND_PHONE,
            ).values_list("client_id", "match_value")
            for client_id, match_value in contact_rows:
                if match_value:
                    resolution[(client_org[client_id], match_value)].add(client_id)

        for call in calls:
            updates = {}
            display = canonical_phone_value(call.phone_number)
            if display != call.phone_number:
                updates["phone_number"] = display
            # Manual/trusted assignments acquired under lock take precedence.
            if call.client_id is not None:
                stats["calls_existing_assignment_preserved"] += 1
            else:
                match_ids = resolution.get(
                    (call.organization_id, normalize_phone(call.phone_number)),
                    set(),
                )
                if len(match_ids) == 1:
                    client_id = next(iter(match_ids))
                    updates["client_id"] = client_id
                    updates["contact_name"] = client_by_id[client_id].name
                    stats["calls_unambiguous"] += 1
                elif len(match_ids) > 1:
                    ambiguous = "\u041d\u0435\u0441\u043a\u043e\u043b\u044c\u043a\u043e \u043a\u043b\u0438\u0435\u043d\u0442\u043e\u0432"
                    if call.contact_name != ambiguous:
                        updates["contact_name"] = ambiguous
                    stats["calls_ambiguous"] += 1
                else:
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
