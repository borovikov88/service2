"""Internal read-only evidence preview, NOT a realization/settlement ledger.

Current profit imports contain register movements, not stable document-table
lines with contract and service-period evidence. Nothing here can authorize
binding, count performance or prove payment. No HTTP route or writer exists.
"""
from collections import Counter
from dataclasses import dataclass
from datetime import date
import hashlib
import json
from uuid import UUID

from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied, ValidationError
from django.db.models.functions import Lower
from django.utils import timezone
from zoneinfo import ZoneInfo

from pool_service.models import OneCImportBatch, OneCMonthlyProfit, Organization, onec_monthly_profit_source_identity
from pool_service.reward_models import OneCCustomerIdentity
from .crm_commercial_access import deals_for_user
from .finance import can_view_gross_profit
from .permissions import company_has_access


@dataclass(frozen=True)
class SourceEvidence:
    organization_id: int
    deal_id: int
    row_id: int
    batch_id: str
    source_identity: str
    snapshot_hash: str
    content_hash: str  # position-independent comparison hint, NEVER an identity


@dataclass(frozen=True)
class Candidate:
    evidence: SourceEvidence
    report_month: date
    document_date: date | None
    warnings: tuple[str, ...]


@dataclass(frozen=True)
class ServicePreview:
    candidates: tuple[Candidate, ...]
    proposed_months: tuple[date, ...]
    requested_contract: str
    binding_allowed: bool = False
    realized_amount: None = None
    paid_amount: None = None
    confirmed_months: tuple = ()


def _guid(value):
    try:
        parsed = UUID(str(value))
    except (TypeError, ValueError, AttributeError) as exc:
        raise ValidationError('Explicit nonzero source GUID required.') from exc
    if not parsed.int:
        raise ValidationError('Explicit nonzero source GUID required.')
    return str(parsed)


def _hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
        separators=(',', ':'), default=str).encode()).hexdigest()


def _date(value):
    try:
        return date.fromisoformat(value) if type(value) is str else None
    except ValueError:
        return None


def preview_service_rows(*, actor, organization_id, deal_id, row_ids,
                         external_organization_guid, contract_guid,
                         proposed_months=(), previous=()):
    """Explicit bounded selection; callers cannot turn this DTO into approval.

    Previous evidence is only a comparison hint, never trusted authorization or
    durable consumption history. The current rights/import version are reread.
    """
    if type(organization_id) is not int or organization_id <= 0 or type(deal_id) is not int or deal_id <= 0:
        raise ValidationError('Explicit organization and deal IDs required.')
    if (type(row_ids) not in (list, tuple) or not 1 <= len(row_ids) <= 50
            or any(type(pk) is not int or pk <= 0 for pk in row_ids)
            or len(set(row_ids)) != len(row_ids)):
        raise ValidationError('Select 1–50 distinct persisted rows.')
    if (type(proposed_months) not in (list, tuple) or len(proposed_months) > 120
            or any(type(month) is not date or month.day != 1 for month in proposed_months)
            or len(set(proposed_months)) != len(proposed_months)):
        raise ValidationError('Service periods must be distinct explicit months.')
    if (type(previous) not in (list, tuple) or len(previous) > 50
            or any(type(item) is not SourceEvidence for item in previous)):
        raise ValidationError('Invalid prior preview evidence.')
    external_org, contract = _guid(external_organization_guid), _guid(contract_guid)
    current_actor = get_user_model().objects.filter(pk=getattr(actor, 'pk', None), is_active=True).first()
    org = Organization.objects.filter(pk=organization_id).first()
    if not current_actor or not org or not company_has_access(org) or not can_view_gross_profit(current_actor, org):
        raise PermissionDenied
    deal = deals_for_user(current_actor, org).select_related('pipeline').filter(pk=deal_id,
        pipeline__organization=org, pipeline__code='service', is_archived=False).first()
    if not deal or not deal.client_id:
        raise PermissionDenied
    if any(item.organization_id != org.pk or item.deal_id != deal.pk for item in previous):
        raise PermissionDenied
    rows = list(OneCMonthlyProfit.objects.active_for(org).filter(pk__in=row_ids,
        import_batch__organization=org, import_batch__status=OneCImportBatch.STATUS_CONFIRMED,
        import_batch__source_type=OneCImportBatch.SOURCE_ODATA).select_related('import_batch').order_by('pk'))
    if len(rows) != len(row_ids):
        raise PermissionDenied  # includes stale, unconfirmed and foreign rows
    customer_guids = {_guid(row.source_data.get('customer_guid')) for row in rows if isinstance(row.source_data, dict)}
    def mapped_parties():
        return set(OneCCustomerIdentity.objects.filter(organization=org, client_id=deal.client_id,
            client__organization=org, confirmed_at__isnull=False, confirmed_by__isnull=False)
            .annotate(normalized_guid=Lower('onec_customer_id')).filter(normalized_guid__in=customer_guids)
            .order_by().values_list('normalized_guid', flat=True).distinct())
    if not customer_guids or mapped_parties() != customer_guids:
        raise PermissionDenied
    candidates = []
    today = timezone.localdate(timezone.now(), ZoneInfo('Asia/Barnaul'))
    for row in rows:
        data = row.source_data
        if not isinstance(data, dict) or len(json.dumps(data, ensure_ascii=False).encode()) > 65536:
            raise ValidationError('Source evidence unavailable.')
        if _guid(data.get('organization_guid')) != external_org or _guid(data.get('customer_guid')) not in customer_guids:
            raise PermissionDenied
        if not row.source_recorder or _guid(data.get('recorder')) != str(row.source_recorder):
            raise ValidationError('Source recorder evidence inconsistent.')
        if (type(data.get('line_number')) is not int or data['line_number'] != row.source_row_number
                or row.source_identity != onec_monthly_profit_source_identity(period_month=row.period_month,
                    source_row_number=row.source_row_number, source_recorder=row.source_recorder)):
            raise ValidationError('Register position evidence inconsistent.')
        # Existing identities and batch IDs remain authoritative import locators.
        # Exclude line number/batch/period from the comparison hint so simple
        # renumbering is not silently treated as a newly available document.
        content = {key: data.get(key) for key in ('organization_guid', 'customer_guid',
            'recorder', 'recorder_type', 'nomenclature_guid', 'document_guid',
            'document_type', 'document_date', 'vat')}
        for key in ('organization_guid', 'customer_guid', 'recorder', 'nomenclature_guid', 'document_guid'):
            if content[key] is not None:
                content[key] = _guid(content[key])
        content.update(quantity=row.quantity, revenue=row.revenue)
        evidence = SourceEvidence(org.pk, deal.pk, row.pk, str(row.import_batch_id),
            row.source_identity, _hash({'source': data, 'identity': row.source_identity,
            'period': row.period_month, 'quantity': row.quantity, 'revenue': row.revenue,
            'batch_sha': row.import_batch.file_sha256}), _hash(content))
        document_date = _date(data.get('document_date'))
        warnings = ['register_not_document_line', 'contract_unverified',
                    'service_period_unconfirmed', 'payment_unverified', 'consumption_registry_unavailable']
        if data.get('row_kind') == 'direct_order_expense':
            warnings.append('cost_only_not_realization')
        if document_date is None:
            warnings.append('document_date_unavailable')
        elif document_date > today:
            warnings.append('future_document')
        for old in previous:
            if old.source_identity == evidence.source_identity:
                warnings.append('unchanged_preview' if old == evidence else 'source_version_requires_review')
            elif old.content_hash == evidence.content_hash:
                warnings.append('possible_renumbering_requires_review')
        candidates.append(Candidate(evidence, row.period_month, document_date, tuple(dict.fromkeys(warnings))))
    counts = Counter(candidate.evidence.content_hash for candidate in candidates)
    candidates = [Candidate(c.evidence, c.report_month, c.document_date,
        c.warnings + (('ambiguous_equal_content',) if counts[c.evidence.content_hash] > 1 else ())) for c in candidates]
    # Detect version switches during the read; this is still not a transactionally
    # authorized write or durable snapshot. Any future command must revalidate.
    active_ids = set(OneCMonthlyProfit.objects.active_for(org).filter(pk__in=row_ids,
        import_batch__organization=org, import_batch__status=OneCImportBatch.STATUS_CONFIRMED).values_list('pk', flat=True))
    if active_ids != set(row_ids) or mapped_parties() != customer_guids:
        raise PermissionDenied
    return ServicePreview(tuple(candidates), tuple(sorted(proposed_months)), contract)
