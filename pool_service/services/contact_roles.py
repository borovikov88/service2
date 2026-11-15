"""Explicit role editing. No identity matching, accounting or intake effects."""
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from pool_service.client_crm_models import ClientBusinessRole, ClientBusinessRoleEvent
from pool_service.client_queries import active_clients
from pool_service.models import Client, Organization, OrganizationAccess


def _apply(client, actor, enabled, source_client=None, *, role_code="supplier", using=None):
    if not isinstance(role_code, str) or role_code not in ClientBusinessRole.ROLE_LABELS:
        raise ValidationError("Unknown contact category")
    using = using or client._state.db or "default"
    roles = ClientBusinessRole.objects.using(using)
    role = roles.filter(client=client, role=role_code).first()
    if role is None and not enabled:
        return None
    if role is None:
        role = roles.create(client=client, role=role_code)
    if role.active != enabled:
        role.active = enabled
        role.revision += 1
        role.save(using=using, update_fields=["active", "revision"])
        ClientBusinessRoleEvent.objects.using(using).create(client=client, actor=actor, role=role_code,
            active=enabled, revision=role.revision, source_client=source_client)
    return role


@transaction.atomic
def set_role(actor, client_id, role_code, enabled, revision):
    from django.contrib.auth import get_user_model
    from .permissions import company_has_access
    if not actor.is_authenticated:
        raise PermissionDenied
    if not isinstance(role_code, str) or role_code not in ClientBusinessRole.ROLE_LABELS:
        raise ValidationError("Unknown contact category")
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
    role = ClientBusinessRole.objects.filter(client=client, role=role_code).first()
    current = role.revision if role else 0
    if expected != current:
        raise ValidationError("Stale revision")
    return _apply(client, actor, enabled == "1", role_code=role_code)


def set_supplier(actor, client_id, enabled, revision):
    """Keep existing callers and supplier forms compatible."""
    return set_role(actor, client_id, "supplier", enabled, revision)


def merge_supplier_role(source, target, actor):
    """Compatibility entry point: preserve every known contact category."""
    # Caller holds both Client locks and an atomic transaction. Preserve source
    # history; never let the generic relation mover rewrite audit ownership.
    role_codes = list(ClientBusinessRole.objects.filter(client=source, active=True,
        role__in=ClientBusinessRole.ROLE_LABELS).values_list("role", flat=True))
    if role_codes:
        if actor is None:
            raise ValidationError("An actor is required to preserve role audit")
        for role_code in role_codes:
            _apply(target, actor, True, source_client=source, role_code=role_code)
