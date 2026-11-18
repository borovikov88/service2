"""Refresh CRM evidence through the normal authenticated finance import path."""
from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied, ValidationError
from django.utils import timezone

from pool_service.services.finance import can_access_finance_overview
from .odata_profit import validate_config, ODataPreviewError
from .odata_profit_drafts import config_from_settings, is_odata_target_organization


class CrmOrderSyncStageError(ValidationError):
    """Bounded diagnosis only; never retains source text in the public message."""
    def __init__(self, stage):
        self.stage = stage if stage in {'config', 'read', 'apply'} else 'unknown'
        super().__init__('CRM order refresh did not complete.', code='crm_order_sync_stage')


def _stage(stage, operation):
    try:
        return operation()
    except (ValidationError, ODataPreviewError):
        raise CrmOrderSyncStageError(stage) from None


def sync_crm_orders(organization, user, *, now=None, config=None, opener=None):
    if not is_odata_target_organization(organization):
        raise PermissionDenied("CRM orders are not configured for this organization.")
    actor = get_user_model().objects.filter(pk=getattr(user, "pk", None), is_active=True).first()
    if actor is None or not can_access_finance_overview(actor, organization):
        raise PermissionDenied("Current financial access is required.")
    from .odata_crm_orders import read_crm_orders, source_identity
    from pool_service.crm_order_models import CrmDealOrderLink
    from pool_service.services.crm_order_finance import apply_crm_order_snapshot
    config = _stage('config', lambda: validate_config(config or config_from_settings()))
    linked_orders = list(CrmDealOrderLink.objects.filter(organization=organization,
        order__source_id=source_identity(config)).values_list('order__order_guid', flat=True))
    source = _stage('read', lambda: read_crm_orders(config=config, now=now or timezone.now(), opener=opener,
        required_order_guids=linked_orders))
    return _stage('apply', lambda: apply_crm_order_snapshot(actor=actor, organization=organization,
        snapshot=source, config=config, now=timezone.now()))
