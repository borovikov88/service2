"""Explicit GET-only source read using existing diagnostic configuration/policy."""
from uuid import UUID
from urllib.request import build_opener
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.utils import timezone
from zoneinfo import ZoneInfo
from pool_service import onec_diagnostic as source
from pool_service.onec_service_document_preview import read_service_document_snapshot
from pool_service.reward_models import OneCCustomerIdentity
from .crm_commercial_commands import _locked_organization,_authorize,_owned_deal
from .finance import can_view_gross_profit
from .permissions import company_has_access


class DocumentScopeChanged(source.OneCDiagnosticError):
    pass


def access(actor,organization_id,deal_id):
    with transaction.atomic():
        org=_locked_organization('default',organization_id)
        actor=_authorize('default',actor.pk,org,'edit_own_deals')
        if not company_has_access(org) or not can_view_gross_profit(actor,org): raise PermissionDenied
        deal=_owned_deal('default',actor.pk,org.pk,deal_id)
        if deal.pipeline.code!='service' or deal.is_archived or deal.outcome!='open': raise PermissionDenied
        return actor,org,deal


def read_document(*,actor,organization_id,deal_id,document_ref,organization_ref,client_ref,contract_ref):
    actor,org,deal=access(actor,organization_id,deal_id)
    if not source.can_access_diagnostic_mcp(actor,org):
        raise PermissionDenied('Read-only доступ 1С не разрешён текущей политикой диагностики.')
    try:
        refs=[str(UUID(value)) for value in (document_ref,organization_ref,client_ref,contract_ref)]
        if any(UUID(value).int==0 for value in refs): raise ValueError
    except (ValueError,TypeError,AttributeError): raise ValidationError('Укажите точные идентификаторы документа, организации, контрагента и договора 1С.')
    document_ref,organization_ref,client_ref,contract_ref=refs
    config=source._validated_config(source.config_from_settings())
    if organization_ref not in config.organization_guids or not OneCCustomerIdentity.objects.filter(
        organization=org,client_id=deal.client_id,client__organization=org,onec_customer_id=client_ref,
        confirmed_at__isnull=False,confirmed_by__isnull=False).exists(): raise PermissionDenied
    if deal.object_kind not in ('pool','client_object'):
        raise ValidationError('Сначала выберите конкретный объект в карточке сделки.')
    opener=build_opener(source.NoRedirectHandler())
    metadata=source.fetch_metadata(config,opener=opener)
    started=timezone.now()
    try:
        snapshot=read_service_document_snapshot(config,document_ref=document_ref,organization_ref=organization_ref,
            client_ref=client_ref,contract_ref=contract_ref,as_of_date=timezone.localdate(timezone.now(),ZoneInfo('Asia/Barnaul')),
            metadata_raw=metadata,opener=opener)
    except source.OneCDiagnosticError as exc:
        if exc.code in {'DOCUMENT_SCOPE_MISMATCH','ORGANIZATION_SCOPE_VIOLATION','PARENT_ORGANIZATION_SCOPE_VIOLATION','DOCUMENT_CHANGED_DURING_READ'}:
            from .crm_service_bindings import invalidate_scope_mismatch, _hash
            changed=invalidate_scope_mismatch(actor=actor,organization_id=org.pk,deal_id=deal.pk,
                source_id=_hash(config.base_url),document_ref=document_ref,organization_ref=organization_ref,
                client_ref=client_ref,contract_ref=contract_ref,read_started_at=started)
            if changed: raise DocumentScopeChanged('DOCUMENT_SCOPE_CHANGED') from exc
        raise
    actor,org,deal=access(actor,organization_id,deal_id)
    if not source.can_access_diagnostic_mcp(actor,org) or not OneCCustomerIdentity.objects.filter(
        organization=org,client_id=deal.client_id,client__organization=org,onec_customer_id=client_ref,
        confirmed_at__isnull=False,confirmed_by__isnull=False).exists(): raise PermissionDenied
    return snapshot
