"""Explicit role editing. No identity matching, accounting or intake effects."""
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from pool_service.client_crm_models import ClientBusinessRole, ClientBusinessRoleEvent
from pool_service.client_queries import active_clients
from pool_service.models import Client, Organization, OrganizationAccess


def _apply(client, actor, enabled, source_client=None):
    role = ClientBusinessRole.objects.filter(client=client, role="supplier").first()
    if role is None and not enabled:
        return None
    if role is None:
        role = ClientBusinessRole.objects.create(client=client, role="supplier")
    if role.active != enabled:
        role.active = enabled
        role.revision += 1
        role.save(update_fields=["active", "revision"])
        ClientBusinessRoleEvent.objects.create(client=client, actor=actor, role="supplier",
            active=enabled, revision=role.revision, source_client=source_client)
    return role


@transaction.atomic
def set_supplier(actor, client_id, enabled, revision):
    from django.contrib.auth import get_user_model
    from .permissions import company_has_access
    if not actor.is_authenticated:
        raise PermissionDenied
    if enabled not in {"0", "1"}:
        raise ValidationError("Invalid state")
    try:
        expected = int(revision)
    except (TypeError, ValueError):
        raise ValidationError("Invalid revision")
    # Match merge/import ordering: organization -> client -> current actor.
    org_id = Client.objects.filter(pk=client_id).values_list("organization_id", flat=True).first()
    organization = Organization.objects.select_for_update().filter(pk=org_id).first()
    client = Client.objects.select_for_update().filter(pk=client_id).first()
    if (client is None or client.organization_id != org_id
            or client.client_type not in {"legal", "private"}
            or not active_clients().filter(pk=client.pk).exists()):
        raise PermissionDenied
    actor = get_user_model().objects.select_for_update().filter(pk=actor.pk).first()
    if actor is None or not actor.is_active:
        raise PermissionDenied
    if not actor.is_superuser and not company_has_access(organization):
        raise PermissionDenied
    if not actor.is_superuser and not OrganizationAccess.objects.select_for_update().filter(
        user=actor, organization_id=client.organization_id, role__in={"owner", "admin"},
    ).exists():
        raise PermissionDenied
    role = ClientBusinessRole.objects.filter(client=client, role="supplier").first()
    current = role.revision if role else 0
    if expected != current:
        raise ValidationError("Stale revision")
    return _apply(client, actor, enabled == "1")


def merge_supplier_role(source, target, actor):
    # Caller holds both Client locks and an atomic transaction. Preserve source
    # history; never let the generic relation mover rewrite audit ownership.
    if ClientBusinessRole.objects.filter(client=source, role="supplier", active=True).exists():
        if actor is None:
            raise ValidationError("An actor is required to preserve role audit")
        _apply(target, actor, True, source_client=source)

