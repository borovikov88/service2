"""Director-confirmed snapshots. Contact edits never silently change the scope."""
import hashlib
import json

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from pool_service.client_crm_models import ClientBusinessRole
from pool_service.client_phone_matching import clients_by_phones
from pool_service.client_queries import active_clients
from pool_service.models import Client, Organization
from pool_service.staff_call_models import InternalContactNumber

from .call_processing_policy import phone_key
from .call_processing_settings import _lock_owner
from .contact_people import phone_rows


def internal_state(person):
    keys = sorted({key for row in phone_rows(person) if (key := phone_key(row['value']))})
    active = list(InternalContactNumber.objects.filter(organization_id=person.organization_id,
        contact=person, valid_to__isnull=True).order_by('phone_key').values_list('phone_key', 'pk'))
    tagged = ClientBusinessRole.objects.filter(client=person, role='internal', active=True).exists()
    revision = hashlib.sha256(json.dumps([person.pk, keys, active, tagged]).encode()).hexdigest()
    return {'keys': keys, 'confirmed': [key for key, _ in active], 'tagged': tagged,
            'revision': revision, 'missing': sorted(set(keys) - {key for key, _ in active})}


@transaction.atomic
def confirm_internal_numbers(actor, person_id, *, enabled, revision):
    org_id = Client.objects.values_list('organization_id', flat=True).get(pk=person_id)
    organization = Organization.objects.get(pk=org_id)
    owner = _lock_owner(actor, organization)
    person = active_clients(Client.objects.select_for_update()).filter(pk=person_id, organization=organization).first()
    if not person:
        raise PermissionDenied
    state = internal_state(person)
    if state['revision'] != revision or enabled not in ('0', '1'):
        raise ValidationError('Обновите карточку и проверьте подтверждённые номера.')
    if enabled == '0':
        InternalContactNumber.objects.filter(organization=organization, contact=person, valid_to__isnull=True).update(
            valid_to=timezone.now(), ended_by=owner)
        return
    if not state['tagged'] or not state['missing']:
        raise ValidationError('Для подтверждения нужен контакт категории «Внутренний» с новым номером.')
    # Full legacy directory resolution is needed only on this explicit owner action.
    matches = clients_by_phones(organization, state['missing'], limit=2)
    if any([client.pk for client in matches.get(key, ())] != [person.pk] for key in state['missing']):
        raise ValidationError('Номер встречается у нескольких контактов. Уточните владельца номера.')
    if InternalContactNumber.objects.filter(organization=organization, phone_key__in=state['missing'], valid_to__isnull=True).exists():
        raise ValidationError('Номер уже подтверждён для другого контакта.')
    InternalContactNumber.objects.bulk_create([InternalContactNumber(organization=organization, contact=person,
        phone_key=key, confirmed_by=owner) for key in state['missing']])


def conflicts_with_confirmed_peer(organization, peer, existing):
    if existing is None:
        return False
    key = phone_key(peer)
    previous = phone_key(existing.phone_number)
    if existing.internal_contact_number_id:
        confirmed = existing.internal_contact_number
        return confirmed.organization_id != organization.pk or confirmed.phone_key != key or previous != key
    if previous == key:
        return False
    return InternalContactNumber.objects.filter(organization=organization, phone_key__in=[key, previous]).exists()


def classify_external_receipt(organization, peer, occurred_at, existing):
    """Run in the capture transaction. Only an undated first receipt is reconsidered."""
    if not transaction.get_connection().in_atomic_block:
        raise RuntimeError('Capture transaction required')
    key = phone_key(peer)
    if existing and (not existing.internal_number_pending or not occurred_at
            or key != existing.internal_contact_number.phone_key):
        return existing.internal_contact_number_id, existing.internal_number_pending
    if not key:
        return None, False
    received = (existing.intake_received_at or existing.created_at) if existing else timezone.now()
    rules = InternalContactNumber.objects.filter(organization=organization, phone_key=key,
        valid_from__lte=received)
    if occurred_at is not None:
        rules = rules.filter(valid_from__lte=occurred_at).filter(Q(valid_to__isnull=True) | Q(valid_to__gt=occurred_at))
    rule_id = rules.order_by('-valid_from', '-pk').values_list('pk', flat=True).first()
    return rule_id, rule_id is not None and occurred_at is None


@transaction.atomic
def end_internal_rule(actor, organization, rule_id):
    owner = _lock_owner(actor, organization)
    updated = InternalContactNumber.objects.filter(pk=rule_id, organization=organization, valid_to__isnull=True).update(
        valid_to=timezone.now(), ended_by=owner)
    if not updated:
        raise ValidationError('Обновите список действующих номеров.')
