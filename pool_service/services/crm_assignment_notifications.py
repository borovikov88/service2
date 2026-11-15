"""Transactional inbox notices and deduplicated, reauthorized post-commit push.

Text deliberately contains only the CRM kind and number. Names, needs, clients,
and communication contents remain behind the card's current access policy.
"""
import logging
from urllib.parse import urlencode

from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.urls import reverse
from django.utils import timezone

from pool_service.crm_assignment_models import CrmAssignmentDelivery
from pool_service.crm_commercial_models import CrmAccess, CrmEvent, CrmLead, CrmDeal
from pool_service.models import Employee, Notification, Organization, OrganizationAccess, Profile
from .crm_commercial_access import deals_for_user
from .crm_deal_objects import validate_deal_object
from .crm_lead_access import can_read_lead
from .notifications import _create_notification
from .push_notifications import send_push_to_users


logger = logging.getLogger(__name__)


def _current(event_id, recipient_id, organization_id, *, using):
    organization = Organization.objects.using(using).select_for_update().get(pk=organization_id)
    recipient = get_user_model().objects.using(using).select_for_update().filter(pk=recipient_id, is_active=True).first()
    if recipient is None or not organization.crm_v2_read:
        return None
    memberships = list(OrganizationAccess.objects.using(using).select_for_update().filter(organization=organization, user=recipient))
    employees = list(Employee.objects.using(using).select_for_update().filter(organization=organization, user=recipient))
    grant = CrmAccess.objects.using(using).select_for_update().filter(organization=organization, user=recipient).first()
    if not memberships or not grant or any(not row.is_active or row.employment_status == Employee.STATUS_DISMISSED for row in employees):
        return None
    event = CrmEvent.objects.using(using).select_for_update().filter(pk=event_id, organization=organization).first()
    if event is None:
        return None
    field = 'assignee_id' if event.lead_id else 'manager_id'
    change = (event.safe_diff or {}).get(field) or {}
    if change.get('after') != recipient.pk or change.get('before') == recipient.pk:
        return None
    model, card_id = (CrmLead, event.lead_id) if event.lead_id else (CrmDeal, event.deal_id)
    card = model.objects.using(using).select_for_update().filter(pk=card_id, organization=organization).first()
    if card is None:
        return None
    if card.organization_id != organization.pk or getattr(card, field) != recipient.pk or card.is_archived:
        return None
    subject = {'lead_id': card.pk} if event.lead_id else {'deal_id': card.pk}
    latest = CrmEvent.objects.using(using).select_for_update().filter(organization=organization,
        safe_diff__has_key=field, **subject).order_by('-pk').values_list('pk', flat=True).first()
    if latest != event.pk:
        return None
    from .crm_assignments import assignment_recipient
    try:
        recipient = assignment_recipient(using, organization, recipient.pk,
            'lead' if event.lead_id else 'deal', subject=card)
    except PermissionDenied:
        return None
    if event.lead_id:
        if not can_read_lead(recipient, organization, card):
            return None
        title, noun = 'Вам назначен лид', 'лид'
        url = reverse('crm_live_leads') + '?' + urlencode({'org_id': organization.pk, 'lead': card.pk})
    else:
        if not deals_for_user(recipient, organization).filter(pk=card.pk).exists():
            return None
        try:
            validate_deal_object(using, recipient, card)
        except PermissionDenied:
            return None
        title, noun = 'Вам назначена сделка', 'сделку'
        url = reverse('crm_live_workspace') + '?' + urlencode({'org_id': organization.pk, 'pipeline': card.pipeline_id, 'deal': card.pk})
    return event, recipient, organization, title, f'Вы назначены ответственным за {noun} №{card.pk}. Откройте карточку, чтобы продолжить работу.', url


def notify_assignment(event, recipient):
    """Called in the assignment transaction; replaying an event sends nothing."""
    using = event._state.db or 'default'
    with transaction.atomic(using=using):
        current = _current(event.pk, recipient.pk, event.organization_id, using=using)
        if current is None:
            return None
        event, recipient, organization, title, message, url = current
        profile = Profile.objects.using(using).select_for_update().filter(user=recipient).first()
        push_enabled = profile is None or profile.push_notifications_enabled
        delivery, created = CrmAssignmentDelivery.objects.using(using).get_or_create(event=event,
            defaults={'recipient': recipient, 'push_requested': push_enabled})
        if not created:
            return delivery
        if profile is None or profile.in_app_notifications_enabled:
            note, _ = _create_notification(recipient, title=title, message=message, kind='crm_assignment',
                organization=organization, action_url=url, dedupe_key=f'crm-assignment:{event.pk}')
            delivery.notification = note
            delivery.save(using=using, update_fields=['notification'])
        if push_enabled:
            transaction.on_commit(lambda pk=delivery.pk, alias=using: deliver_assignment_push(pk, using=alias), using=using)
        return delivery


def deliver_assignment_push(delivery_id, *, using='default'):
    try:
        seed = CrmAssignmentDelivery.objects.using(using).select_related('event').filter(pk=delivery_id).first()
        if seed is None:
            return
        with transaction.atomic(using=using):
            current = _current(seed.event_id, seed.recipient_id, seed.event.organization_id, using=using)
            if current is None:
                return
            delivery = CrmAssignmentDelivery.objects.using(using).select_for_update().get(pk=delivery_id)
            if not delivery.push_requested or delivery.push_attempted_at is not None:
                return
            event, recipient, organization, title, message, url = current
            profile = Profile.objects.using(using).select_for_update().filter(user=recipient).first()
            if profile is not None and not profile.push_notifications_enabled:
                return
            note = Notification.objects.using(using).filter(pk=delivery.notification_id,
                user=recipient, organization=organization).first()
            # Persist the attempt before crossing the transport boundary; an
            # uncertain response must not produce duplicate pushes on a retry.
            delivery.push_attempted_at = timezone.now()
            delivery.save(using=using, update_fields=['push_attempted_at'])
        send_push_to_users([recipient], title=title, message=message, action_url=url, notification=note)
    except Exception:
        # Delivery cannot turn an already committed reassignment into an error.
        logger.warning('CRM assignment push could not be delivered.')
