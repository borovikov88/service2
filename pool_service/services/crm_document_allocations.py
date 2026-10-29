"""Pure proposal validation over document snapshots; never authorizes DB writes.

No ORM, HTTP route, import, payment inference or durable consumption ledger.
A supplied snapshot must originate from the bounded document adapter; this DTO
is not an authorization token and must never be accepted as one by a writer.
"""
from collections import defaultdict
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
import hashlib
import json

from django.core.exceptions import ValidationError
from pool_service.onec_service_document_preview import DocumentSnapshot


@dataclass(frozen=True)
class Allocation:
    line_number: int
    content_hash: str
    month: date
    quantity: Decimal
    amount: Decimal


@dataclass(frozen=True)
class AllocationPreview:
    proposal_hash: str
    allocations: tuple[Allocation, ...]
    blockers: tuple[str, ...]
    binding_allowed: bool = False
    realized_amount: None = None
    paid_amount: None = None
    confirmed_months: tuple = ()


def preview_allocations(snapshot, *, expected_snapshot_hash, allocations):
    """Explicit months, including partial quantities; quantity never implies coverage.

    Repeating a proposal yields the same hash, not permission to consume again.
    Even a valid proposal needs authenticated deal/contract/object scope, fresh
    source reread and durable reconcile/consumption checks before any future write.
    """
    if type(snapshot) is not DocumentSnapshot or expected_snapshot_hash != snapshot.snapshot_hash:
        raise ValidationError('Source changed; reload and reconcile the document.')
    if type(allocations) not in (list, tuple) or not 1 <= len(allocations) <= 200:
        raise ValidationError('Select 1–200 explicit allocations.')
    lines = {line.number: line for line in snapshot.lines}
    seen = set(); totals = defaultdict(lambda: [Decimal(0), Decimal(0)])
    for item in allocations:
        if type(item) is not Allocation or type(item.line_number) is not int:
            raise ValidationError('Invalid allocation.')
        line = lines.get(item.line_number)
        if line is None or item.content_hash != line.content_hash:
            raise ValidationError('Line changed or moved; reconcile explicitly.')
        if type(item.month) is not date or item.month.day != 1:
            raise ValidationError('Choose a calendar month explicitly.')
        key = (item.line_number, item.month)
        if key in seen:
            raise ValidationError('Duplicate line/month allocation.')
        seen.add(key)
        for value in (item.quantity, item.amount):
            if type(value) is not Decimal or not value.is_finite() or value <= 0:
                raise ValidationError('Positive finite decimal quantity and amount required.')
        totals[item.line_number][0] += item.quantity
        totals[item.line_number][1] += item.amount
        if totals[item.line_number][0] > line.quantity or totals[item.line_number][1] > line.total:
            raise ValidationError('Allocation exceeds the source line; corrections require review.')
    ordered = tuple(sorted(allocations, key=lambda item: (item.line_number, item.month)))
    # Normalize decimal scale so equivalent proposals have the same local digest.
    payload = [snapshot.source_id, snapshot.document_ref, snapshot.organization_ref,
        snapshot.client_ref, snapshot.contract_ref, snapshot.snapshot_hash,
        [[item.line_number, item.content_hash, item.month.isoformat(),
          str(item.quantity.normalize()), str(item.amount.normalize())] for item in ordered]]
    digest = hashlib.sha256(json.dumps(payload, separators=(',', ':')).encode()).hexdigest()
    blockers = set(snapshot.warnings) | {'deal_contract_object_scope_unverified',
        'service_line_scope_unverified', 'durable_consumption_unavailable',
        'period_coverage_unverified', 'correction_policy_unconfirmed'}
    if snapshot.document_date.date() > snapshot.as_of_date:
        blockers.add('future_document')
    if not snapshot.posted: blockers.add('unposted_document')
    if snapshot.deleted: blockers.add('deleted_document')
    if any(item.month > snapshot.as_of_date.replace(day=1) for item in ordered):
        blockers.add('future_period')
    return AllocationPreview(digest, ordered, tuple(sorted(blockers)))
