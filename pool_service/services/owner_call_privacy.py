"""Owner/peer exclusion before persistence, without a global contact blacklist.

Unknown or historical identity is not guessed. Existing conservative guards for
saved calls remain in call_privacy; this service never deletes their history.
"""
import hashlib
import json
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.db.models import Q
from pool_service.call_processing_models import CallPrivateNumber
from pool_service.client_crm_models import ClientContact
from pool_service.client_queries import active_clients
from pool_service.communication_models import TelephonyEmployeeIdentity
from pool_service.models import Client, OrganizationAccess
from .call_processing_policy import phone_key
from .call_processing_settings import _lock_owner, add_private_numbers, remove_private_number


def _verified_identity(connection, provider_user, extension, occurred_at):
    """No caller/lead assignment, names, username or bare phone inference."""
    if not connection.is_active or (not provider_user and not extension) or occurred_at is None:
        return None
    query = Q()
    if provider_user:
        query |= Q(external_user=provider_user)
    if extension:
        query |= Q(extension=extension)
    rows = list(TelephonyEmployeeIdentity.objects.filter(query,
        organization_id=connection.organization_id, connection=connection).select_related("employee__user")[:3])
    if len(rows) != 1:
        return None
    row = rows[0]
    if (not row.is_active or row.requires_manual_confirmation or row.reassignment_detected_at
            or row.status != TelephonyEmployeeIdentity.STATUS_MANUALLY_MATCHED
            or row.match_method != TelephonyEmployeeIdentity.MATCH_MANUAL
            or not row.confirmed_by_id or not row.confirmed_at or row.confirmed_at > occurred_at
            or provider_user and row.external_user != provider_user
            or extension and row.extension != extension):
        return None
    employee = row.employee
    if (not employee or employee.organization_id != connection.organization_id or not employee.is_active
            or employee.employment_status == employee.STATUS_DISMISSED
            or not employee.user_id or not employee.user.is_active):
        return None
    if not OrganizationAccess.objects.filter(organization_id=connection.organization_id, user_id=employee.user_id, role="owner").exists():
        return None
    return row


def verified_owner(connection, provider_user, extension, occurred_at):
    identity = _verified_identity(connection, provider_user, extension, occurred_at)
    return identity.employee.user_id if identity else None


def exclude_before_save(*, connection, provider_user, extension, peer_phone, occurred_at, existing=None):
    """Called under the capture organization lock; stores no raw payload or tombstone."""
    if not transaction.get_connection().in_atomic_block:
        raise RuntimeError("Owner privacy requires the capture transaction")
    identity = _verified_identity(connection, provider_user, extension, occurred_at)
    if not identity:
        return False
    owner_id = identity.employee.user_id
    # A row already attributed to another participant must not be reclassified
    # from the final leg of a transferred call.
    if existing:
        if (existing.employee_id not in (None, owner_id) or existing.peer_employee_id not in (None, owner_id)
                or existing.employee_profile_id not in (None, identity.employee_id)
                or existing.peer_employee_profile_id not in (None, identity.employee_id)
                or existing.direction not in ("in", "out")
                or phone_key(existing.phone_number) != phone_key(peer_phone)):
            return False
        for field, expected in (("provider_user", identity.external_user), ("provider_extension", identity.extension),
                ("peer_provider_user", identity.external_user), ("peer_provider_extension", identity.extension)):
            saved = getattr(existing, field)
            if saved and saved != expected:
                return False
    key = phone_key(peer_phone)
    return bool(key and CallPrivateNumber.objects.filter(organization_id=connection.organization_id,
        owner_id=owner_id, phone_key=key).exists())


def contact_state(actor, person):
    keys = sorted({key for key in [phone_key(person.phone), *(phone_key(value) for value in
        ClientContact.objects.filter(client=person, kind="phone").values_list("value", flat=True))] if key})
    private = sorted(CallPrivateNumber.objects.filter(organization_id=person.organization_id,
        owner_id=actor.pk, phone_key__in=keys).values_list("phone_key", flat=True))
    revision = hashlib.sha256(json.dumps([person.pk, keys, private]).encode()).hexdigest()
    return {"keys": keys, "revision": revision, "marked": bool(keys) and keys == private,
        "partial": bool(private) and keys != private}


@transaction.atomic
def set_personal_peer(actor, person_id, enabled, revision):
    org_id = Client.objects.filter(pk=person_id).values_list("organization_id", flat=True).first()
    from pool_service.models import Organization
    organization = Organization.objects.filter(pk=org_id).first()
    owner = _lock_owner(actor, organization)
    person = active_clients(Client.objects.select_for_update()).filter(pk=person_id,
        organization=organization, client_type="private").first()
    if not person:
        raise PermissionDenied
    state = contact_state(owner, person)
    if enabled not in ("0", "1") or state["revision"] != revision or not state["keys"]:
        raise ValidationError("Обновите карточку и проверьте номера.")
    if enabled == "1":
        # Numbers are a confirmed snapshot, never an automatically expanding
        # policy when an imported contact gains a new phone.
        add_private_numbers(user=owner, organization=organization, label=person.name[:120],
            numbers_text="\n".join(state["keys"]))
    else:
        for pk in CallPrivateNumber.objects.filter(organization=organization, owner=owner,
                phone_key__in=state["keys"]).values_list("pk", flat=True):
            remove_private_number(user=owner, organization=organization, number_id=pk)
