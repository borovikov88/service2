"""Pure draft validation for explicit service obligations; NOT active contract terms.

No ORM, source fetch, authorization, scheduler, payment or completion inference.
A future writer must recheck organization/client/object, current rights and revision.
"""
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from fractions import Fraction
import hashlib
import json
from django.core.exceptions import ValidationError


@dataclass(frozen=True)
class ServicePeriodDraft:
    month: date
    quantity: Decimal
    unit: str
    agreed_amount: Decimal | None


@dataclass(frozen=True)
class ServiceTermsPreview:
    periods: tuple[ServicePeriodDraft, ...]
    proposal_hash: str
    listed_amount: Decimal | None
    contract_total: Decimal | None
    complete_period_list: bool
    blockers: tuple[str, ...]
    can_activate: bool = False
    realized_amount: None = None
    paid_amount: None = None


def _decimal(value, *, money=False):
    if (type(value) is not Decimal or not value.is_finite() or value < 0
            or (not money and value == 0) or len(value.as_tuple().digits) > 24
            or not -12 <= value.as_tuple().exponent <= 12):
        raise ValidationError('Укажите конечное неотрицательное Decimal-значение; объём должен быть больше нуля.')
    if money and (Fraction(value) * 100).denominator != 1:
        raise ValidationError('Договорную сумму укажите точно до копеек, без автоматического округления.')


def preview_terms(*, start, end, periods, complete_period_list, renewal_review_on):
    """Validate only explicitly entered months/quantities/prices, never derive them.

    Seasonal gaps are valid; absence of a month is not proof of a free service.
    An indefinite or incomplete term has no computable whole-contract total.
    Zero amount is explicit; None stays unknown. Decimal arithmetic is context-free.
    """
    if type(start) is not date or (end is not None and (type(end) is not date or end < start)):
        raise ValidationError('Укажите корректный срок договора.')
    if type(complete_period_list) is not bool:
        raise ValidationError('Явно укажите полноту перечня договорных периодов.')
    if end is None and complete_period_list:
        raise ValidationError('Для бессрочного договора нельзя подтвердить полный перечень будущих месяцев.')
    if type(renewal_review_on) is not date:
        raise ValidationError('Явно выберите дату проверки продления; автоматического срока нет.')
    if type(periods) not in (list, tuple) or not 1 <= len(periods) <= 1200:
        raise ValidationError('Нужны 1–1200 явно выбранных периодов.')
    seen = set()
    for row in periods:
        if type(row) is not ServicePeriodDraft or type(row.month) is not date or row.month.day != 1:
            raise ValidationError('Период должен быть явно выбранным календарным месяцем.')
        if row.month < start.replace(day=1) or (end is not None and row.month > end.replace(day=1)):
            raise ValidationError('Период вне срока договора.')
        if row.month in seen:
            raise ValidationError('Один месяц нельзя повторить в перечне обязательств.')
        seen.add(row.month)
        _decimal(row.quantity)
        if type(row.unit) is not str or not row.unit.strip() or len(row.unit) > 64:
            raise ValidationError('Явно укажите единицу объёма; количество не определяет календарный месяц.')
        if row.agreed_amount is not None:
            _decimal(row.agreed_amount, money=True)
    ordered = tuple(ServicePeriodDraft(r.month, r.quantity, r.unit.strip(), r.agreed_amount)
                    for r in sorted(periods, key=lambda r: r.month))
    payload = [start.isoformat(), end.isoformat() if end else None, complete_period_list,
               renewal_review_on.isoformat(), [[r.month.isoformat(), str(Fraction(r.quantity)), r.unit,
                   None if r.agreed_amount is None else str(Fraction(r.agreed_amount))] for r in ordered]]
    proposal_hash = hashlib.sha256(json.dumps(payload, ensure_ascii=False, separators=(',', ':')).encode()).hexdigest()
    listed = None
    if all(r.agreed_amount is not None for r in ordered):
        # Sum integer kopecks so a caller's low Decimal precision cannot round money.
        cents = sum(int(Fraction(r.agreed_amount) * 100) for r in ordered)
        listed = Decimal(f'{cents // 100}.{cents % 100:02d}')
    blockers = ['draft_not_authorized', 'contract_scope_unverified', 'terms_activation_policy_unconfirmed',
                'payment_not_verified', 'realization_coverage_not_verified']
    if end is None: blockers.append('indefinite_term')
    if not complete_period_list: blockers.append('period_list_incomplete')
    if listed is None: blockers.append('agreed_amount_unknown')
    return ServiceTermsPreview(ordered, proposal_hash, listed,
        listed if complete_period_list and end is not None else None,
        complete_period_list, tuple(blockers))
