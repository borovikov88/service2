"""Internal commands for explicit document associations; no transport or public API.

Only the existing server-side document adapter supplies snapshots. Do not expose
snapshot input through HTTP. Manual allocations are evidence links, not payment,
completion or a replacement financial source. Versions are append-only.
"""
from dataclasses import asdict
from datetime import date, timedelta
from decimal import Decimal
import hashlib
import json
from django.conf import settings
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from zoneinfo import ZoneInfo
from pool_service.crm_commercial_models import CANONICAL_KEY
from pool_service.crm_service_binding_models import ServiceContractBinding, ServiceBoundDocument, ServiceAllocationVersion, ServiceBindingEvent
from pool_service.onec_service_document_preview import DocumentSnapshot
from pool_service.reward_models import OneCCustomerIdentity
from .crm_commercial_commands import _locked_organization, _authorize, _owned_deal, CommercialConflict
from .crm_document_allocations import preview_allocations
from .finance import can_view_gross_profit
from .permissions import company_has_access


def _json(value):
    return json.loads(json.dumps(value, default=str, sort_keys=True))


def _hash(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,separators=(',',':')).encode()).hexdigest()


def _scope(actor, organization_id, deal_id, snapshot):
    org = _locked_organization('default', organization_id)
    actor = _authorize('default', actor.pk, org, 'edit_own_deals')
    if not company_has_access(org) or not can_view_gross_profit(actor, org):
        raise PermissionDenied
    deal = _owned_deal('default', actor.pk, org.pk, deal_id)
    if (deal.is_archived or deal.outcome != 'open' or deal.pipeline.code != 'service'
            or deal.pipeline.organization_id != org.pk or not deal.client_id
            or deal.object_kind not in ('pool','client_object')):
        raise ValidationError('Выберите конкретный объект в открытой сервисной сделке.')
    if type(snapshot) is not DocumentSnapshot:
        raise ValidationError('Требуется серверный снимок документа реализации.')
    from pool_service.onec_diagnostic import _validated_config, config_from_settings
    config=_validated_config(config_from_settings())
    if (str(getattr(settings,'ONEC_ODATA_TARGET_ORGANIZATION_ID','')) != str(org.pk)
            or snapshot.source_id != _hash(config.base_url)):
        raise PermissionDenied('Источник не настроен для этой организации.')
    observed=parse_datetime(snapshot.observed_at)
    now=timezone.now()
    if not observed or timezone.is_naive(observed) or not now-timedelta(minutes=15) <= observed <= now:
        raise ValidationError('Нужен новый серверный снимок документа (не старше 15 минут).')
    return actor, org, deal


def _party_valid(org, deal, snapshot):
    return (snapshot.organization_ref in getattr(settings,'ONEC_ODATA_ORGANIZATION_GUIDS',())
        and OneCCustomerIdentity.objects.select_for_update().filter(organization=org,client_id=deal.client_id,
            client__organization=org,onec_customer_id=snapshot.client_ref,
            confirmed_at__isnull=False,confirmed_by__isnull=False).exists())


def _observe_existing(actor,org,deal,snapshot):
    doc=ServiceBoundDocument.objects.select_for_update().filter(contract__deal=deal,
        contract__source_id=snapshot.source_id,document_ref=snapshot.document_ref).select_related('contract').first()
    if not doc: return None
    observed=parse_datetime(snapshot.observed_at)
    old_observed=parse_datetime(doc.current_snapshot['observed_at'])
    if observed < old_observed or (observed == old_observed and snapshot.snapshot_hash != doc.current_snapshot_hash):
        raise CommercialConflict('Получен более старый снимок. Обновите источник; откат наблюдений запрещён.')
    binding=doc.contract
    scope_ok=(_party_valid(org,deal,snapshot) and snapshot.organization_ref==binding.external_organization
        and snapshot.client_ref==binding.external_client and snapshot.contract_ref==binding.contract_ref
        and deal.client_id==binding.client_id and deal.object_kind==binding.object_kind
        and deal.pool_id==binding.pool_id and deal.client_object_id==binding.client_object_id)
    if not scope_ok and snapshot.snapshot_hash == doc.current_snapshot_hash and doc.current_snapshot.get('invalid_scope'):
        if observed > old_observed:
            doc.current_snapshot['observed_at']=snapshot.observed_at
            doc.save(update_fields=['current_snapshot'])
        return {'status':doc.status,'revision':doc.revision,'document_id':doc.pk,'saved':False}
    if snapshot.snapshot_hash != doc.current_snapshot_hash or not scope_ok:
        old=doc.current_snapshot_hash
        doc.current_snapshot_hash=snapshot.snapshot_hash
        # Never retain another party's new document contents in this deal.
        doc.current_snapshot=_json(asdict(snapshot)) if scope_ok else {'invalid_scope':True,'observed_at':snapshot.observed_at}
        doc.revision+=1;doc.status='requires_reconciliation';doc.save()
        ServiceBindingEvent.objects.create(organization=org,document=doc,
            operation_key=f'source-{doc.pk}-{doc.revision}',payload_hash=_hash(doc.current_snapshot),kind='source_changed',
            evidence={'before':old,'after':snapshot.snapshot_hash,'snapshot':doc.current_snapshot},actor=actor)
        return {'status':doc.status,'revision':doc.revision,'document_id':doc.pk,'saved':False}
    # A later identical read advances the watermark without revising allocation.
    if observed > old_observed:
        doc.current_snapshot['observed_at']=snapshot.observed_at
        doc.save(update_fields=['current_snapshot'])
    return None


def _contract(actor, org, deal, snapshot, *, confirm_contract):
    values=dict(source_id=snapshot.source_id,external_organization=snapshot.organization_ref,
        external_client=snapshot.client_ref,contract_ref=snapshot.contract_ref,
        client_id=deal.client_id,object_kind=deal.object_kind,
        pool_id=deal.pool_id,client_object_id=deal.client_object_id)
    binding=ServiceContractBinding.objects.select_for_update().filter(deal=deal).first()
    if binding:
        if any(getattr(binding,key)!=value for key,value in values.items()):
            raise CommercialConflict('Договор, клиент или объект изменился. Требуется отдельная сверка связи.')
        return binding
    if confirm_contract is not True:
        raise ValidationError('Явно подтвердите договор документа и выбранный объект сделки.')
    if ServiceContractBinding.objects.filter(organization=org,source_id=snapshot.source_id,
            external_organization=snapshot.organization_ref,contract_ref=snapshot.contract_ref).exists():
        raise CommercialConflict('Договор уже связан с другой сделкой/объектом.')
    return ServiceContractBinding.objects.create(organization=org,deal=deal,confirmed_by=actor,**values)


@transaction.atomic
def save_allocations(*, actor, organization_id, deal_id, snapshot, allocations,
                     operation_key, expected_revision, expected_snapshot_hash,
                     confirm_contract=False, confirm_service=False, reconcile=False, reason=''):
    """Store explicit allocation versions. A changed source first invalidates links.

    Call again with the returned revision and explicit reconciliation to replace
    the active version. Old versions/events remain. Replay rechecks current rights
    and source; it cannot revive stale allocations. There is no automatic credit.
    """
    CANONICAL_KEY(operation_key)
    if operation_key.startswith("source-"):
        raise ValidationError("Reserved operation key.")
    if any(type(value) is not int or value <= 0 for value in (organization_id, deal_id)):
        raise ValidationError("Explicit organization/deal IDs required.")
    if type(expected_revision) is not int or expected_revision < 0:
        raise ValidationError('Некорректная версия документа.')
    if confirm_service is not True or type(reconcile) is not bool or type(reason) is not str or not 1 <= len(reason.strip()) <= 1000:
        raise ValidationError('Подтвердите обслуживание и укажите основание распределения.')
    actor,org,deal=_scope(actor,organization_id,deal_id,snapshot)
    invalidation=_observe_existing(actor,org,deal,snapshot)
    if invalidation: return invalidation
    if not _party_valid(org,deal,snapshot):
        raise PermissionDenied("Контрагент/организация документа не подтверждены для сделки.")
    binding=_contract(actor,org,deal,snapshot,confirm_contract=confirm_contract)
    if ServiceBoundDocument.objects.filter(contract__organization=org, contract__source_id=snapshot.source_id,
            contract__external_organization=snapshot.organization_ref, document_ref=snapshot.document_ref).exclude(contract=binding).exists():
        raise CommercialConflict("Документ уже закреплён за другим договором. Требуется сверка.")
    current=_json(asdict(snapshot))
    doc,created=ServiceBoundDocument.objects.select_for_update().get_or_create(contract=binding,
        document_ref=snapshot.document_ref,defaults=dict(current_snapshot_hash=snapshot.snapshot_hash,current_snapshot=current))
    proposal=preview_allocations(snapshot,expected_snapshot_hash=expected_snapshot_hash,allocations=allocations)
    digest=_hash(dict(actor=actor.pk,deal=deal.pk,document=doc.pk,proposal=proposal.proposal_hash,
        reconcile=reconcile,reason=reason.strip(),confirm_contract=confirm_contract,confirm_service=confirm_service))
    previous=ServiceBindingEvent.objects.filter(organization=org,operation_key=operation_key).first()
    if previous:
        if previous.payload_hash!=digest: raise CommercialConflict('Ключ операции уже использован с другими данными.')
        version_id=previous.evidence['version_id']
        return {'status':doc.status,'revision':previous.evidence['revision'],'current_revision':doc.revision,
            'version_id':version_id,'document_id':doc.pk,'saved':True,'replayed':True,
            'active':doc.status=='manually_allocated' and doc.active_version_id==version_id}
    if doc.revision!=expected_revision: raise CommercialConflict('Обновите документ перед сохранением.')
    if doc.active_version_id and not reconcile:
        raise CommercialConflict('Распределение уже существует. Используйте явную сверку, не повторный зачёт.')
    today=timezone.localdate(timezone.now(),ZoneInfo('Asia/Barnaul'))
    unsafe={'ambiguous_equal_lines','correction_requires_review','unposted_document','deleted_document','future_document'}
    if (unsafe.intersection(snapshot.warnings) or not snapshot.posted or snapshot.deleted
            or snapshot.document_date.date()>min(today,snapshot.as_of_date)
            or any(item.month>today.replace(day=1) for item in allocations)):
        raise ValidationError('Будущий, изменённый, неоднозначный или корректировочный документ требует сверки источника.')
    doc.revision+=1
    version=ServiceAllocationVersion.objects.create(document=doc,revision=doc.revision,
        snapshot_hash=snapshot.snapshot_hash,snapshot=current,allocations=_json([asdict(item) for item in proposal.allocations]),
        reason=reason.strip(),created_by=actor)
    doc.active_version=version;doc.status='manually_allocated';doc.current_snapshot=current;doc.save()
    ServiceBindingEvent.objects.create(organization=org,document=doc,operation_key=operation_key,payload_hash=digest,
        kind='reconciled' if reconcile else 'allocated',evidence={'version_id':version.pk,'revision':doc.revision},actor=actor)
    return {'status':doc.status,'revision':doc.revision,'document_id':doc.pk,'version_id':version.pk,'saved':True,'active':True}


@transaction.atomic
def observe_document(*, actor, organization_id, deal_id, snapshot):
    """Record source invalidation without changing allocations or issuing credit.

    Internal adapter hook only; no live scheduler/import is installed by this
    package. An unobserved upstream change cannot be claimed to be detected.
    """
    actor,org,deal=_scope(actor,organization_id,deal_id,snapshot)
    result=_observe_existing(actor,org,deal,snapshot)
    return result or {'changed':False}


@transaction.atomic
def invalidate_scope_mismatch(*,actor,organization_id,deal_id,source_id,document_ref,
                              organization_ref,client_ref,contract_ref,read_started_at):
    """Trusted adapter error only, never a browser flag or transport failure.

    Match the previously bound locator exactly. Do not retain mismatched foreign
    contents. A read begun before a newer observation cannot invalidate that read.
    """
    from pool_service.onec_diagnostic import can_access_diagnostic_mcp
    org=_locked_organization('default',organization_id)
    actor=_authorize('default',actor.pk,org,'edit_own_deals')
    deal=_owned_deal('default',actor.pk,org.pk,deal_id)
    if not company_has_access(org) or not can_view_gross_profit(actor,org) or not can_access_diagnostic_mcp(actor,org): raise PermissionDenied
    doc=ServiceBoundDocument.objects.select_for_update().filter(contract__deal=deal,
        contract__source_id=source_id,document_ref=document_ref,contract__external_organization=organization_ref,
        contract__external_client=client_ref,contract__contract_ref=contract_ref).first()
    if not doc or parse_datetime(doc.current_snapshot['observed_at'])>read_started_at:
        return False
    if doc.status=='requires_reconciliation' and doc.current_snapshot.get('invalid_scope'): return True
    doc.revision+=1;doc.status='requires_reconciliation'
    doc.current_snapshot={'invalid_scope':True,'observed_at':timezone.now().isoformat()};doc.save()
    ServiceBindingEvent.objects.create(organization=org,document=doc,operation_key=f'source-{doc.pk}-{doc.revision}',
        payload_hash=_hash(doc.current_snapshot),kind='source_scope_mismatch',evidence=doc.current_snapshot,actor=actor)
    return True
