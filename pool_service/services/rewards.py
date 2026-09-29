from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from datetime import date, timedelta
from decimal import Decimal, ROUND_HALF_UP

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import models, transaction
from django.utils import timezone

from pool_service.finance_imports.monthly_profit_parser import classify_nomenclature_type
from pool_service.finance_imports.odata_profit import ODataPreviewError
from pool_service.finance_imports.odata_profit_drafts import read_odata_author_names
from pool_service.models import Employee, OneCMonthlyProfit, Organization
from pool_service.reward_models import (
    OneCAuthorIdentity,
    OneCCustomerIdentity,
    RewardAdjustment,
    RewardMonthClose,
    RewardOrderObjectLink,
    RewardParticipantTemplate,
    RewardParticipation,
    RewardParticipationChange,
    RewardSchemeVersion,
)
from pool_service.services.finance import can_access_management_finance

MONEY = Decimal("0.01")
ONE = Decimal("1.000000")
MAX_FIXED_REWARD = Decimal("1000000.00")
RETAIL_CHECK = "Document_ЧекККМ"
RETAIL_REPORT = "Document_ОтчетОРозничныхПродажах"
MONTH_CLOSE = "Document_ЗакрытиеМесяца"
REALIZATION = "Document_РасходнаяНакладная"


def money(value):
    return Decimal(value or 0).quantize(MONEY, rounding=ROUND_HALF_UP)


def _source_mapping(value):
    """Treat historical/irregular source_data as optional metadata, never as a page-fatal value."""
    return value if isinstance(value, dict) else {}


def _source_text(value):
    """Normalize optional persisted source metadata before string operations."""
    return "" if value is None else str(value).strip()


def month_start(value):
    if isinstance(value, date):
        return value.replace(day=1)
    try:
        parsed = date.fromisoformat(f"{value}-01")
    except (TypeError, ValueError) as exc:
        raise ValidationError("Период должен быть в формате YYYY-MM.") from exc
    return parsed


def _management_role(user, organization):
    return can_access_management_finance(user, organization)


def can_view_rewards(user, organization):
    return bool(
        user and user.is_authenticated and organization and (
            user.is_superuser
            or _management_role(user, organization)
            or user.has_perm("pool_service.view_employee_rewards")
        )
    )


def can_manage_participation(user, organization):
    return bool(
        user and user.is_authenticated and organization and (
            user.is_superuser
            or _management_role(user, organization)
            or user.has_perm("pool_service.manage_employee_reward_participation")
        )
    )


def can_manage_rules(user, organization):
    return bool(
        user and user.is_authenticated and organization and (
            user.is_superuser
            or _management_role(user, organization)
            or user.has_perm("pool_service.manage_employee_reward_rules")
        )
    )


def can_close_period(user, organization):
    return bool(
        user and user.is_authenticated and organization and (
            user.is_superuser
            or _management_role(user, organization)
            or user.has_perm("pool_service.close_employee_reward_period")
        )
    )


def scheme_for_month(organization, period_month):
    return (
        RewardSchemeVersion.objects.filter(
            organization=organization,
            effective_from__lte=period_month,
        )
        .filter(models_q_effective(period_month))
        .order_by("-effective_from", "-version")
        .first()
    )


def models_q_effective(period_month):
    from django.db.models import Q
    return Q(effective_to__isnull=True) | Q(effective_to__gte=period_month)


@transaction.atomic
def ensure_test_scheme(organization, user, period_month):
    if not can_manage_rules(user, organization):
        raise PermissionDenied
    period_month = month_start(period_month)
    _lock_reward_organization(organization)
    scheme = scheme_for_month(organization, period_month)
    if scheme:
        return scheme
    if RewardMonthClose.objects.filter(
        organization=organization,
        period_month__gte=period_month,
    ).exists():
        raise ValidationError("Нельзя создавать правила задним числом через уже закрытый месяц.")

    versions = RewardSchemeVersion.objects.filter(
        organization=organization,
        name="Тестовая схема №1",
    )
    latest_version = versions.order_by("-version").first()
    next_scheme = (
        versions.filter(effective_from__gt=period_month)
        .order_by("effective_from", "version")
        .first()
    )
    effective_to = None
    if next_scheme:
        effective_to = next_scheme.effective_from - timedelta(days=1)

    return RewardSchemeVersion.objects.create(
        organization=organization,
        name="Тестовая схема №1",
        version=(latest_version.version if latest_version else 0) + 1,
        effective_from=period_month,
        effective_to=effective_to,
        created_by=user,
        confirmed_by=user if status == RewardParticipation.STATUS_CONFIRMED else None,
        confirmed_at=timezone.now() if status == RewardParticipation.STATUS_CONFIRMED else None,
    )


@transaction.atomic
def create_scheme_version(organization, user, *, effective_from, values):
    if not can_manage_rules(user, organization):
        raise PermissionDenied
    effective_from = month_start(effective_from)
    _lock_reward_organization(organization)
    latest = (
        RewardSchemeVersion.objects.filter(organization=organization, name="Тестовая схема №1")
        .order_by("-version")
        .first()
    )
    version = (latest.version if latest else 0) + 1
    if latest and effective_from <= latest.effective_from:
        raise ValidationError("Новая версия правил должна начинаться позже предыдущей версии.")
    if RewardMonthClose.objects.filter(
        organization=organization,
        period_month__gte=effective_from,
    ).exists():
        raise ValidationError("Нельзя менять правила задним числом через уже закрытый месяц.")
    if latest and (latest.effective_to is None or latest.effective_to >= effective_from):
        previous_day = effective_from - timedelta(days=1)
        latest.effective_to = previous_day
        latest.save(update_fields=["effective_to"])
    allowed = {
        "documentation_retail_fixed", "documentation_document_fixed",
        "sale_rate", "project_rate", "work_rate", "client_manager_rate",
    }
    try:
        fields = {key: Decimal(str(value)) for key, value in values.items() if key in allowed}
    except (ValueError, ArithmeticError) as exc:
        raise ValidationError("Ставки и фиксированные суммы должны быть числовыми.") from exc
    fixed_fields = ("documentation_retail_fixed", "documentation_document_fixed")
    rate_fields = ("sale_rate", "project_rate", "work_rate", "client_manager_rate")
    for key, value in fields.items():
        if not value.is_finite():
            raise ValidationError(
                "Значения ставок и фиксированных сумм должны быть конечными числами."
            )
    for key in fixed_fields:
        if key in fields and (fields[key] < 0 or fields[key] > MAX_FIXED_REWARD):
            raise ValidationError("Фиксированная сумма должна быть от 0 до 1 000 000 ₽.")
    for key in rate_fields:
        if key in fields and (fields[key] < 0 or fields[key] > ONE):
            raise ValidationError("Процентная ставка должна быть от 0% до 100%.")
    return RewardSchemeVersion.objects.create(
        organization=organization,
        name="Тестовая схема №1",
        version=version,
        effective_from=effective_from,
        created_by=user,
        **fields,
    )


def _is_documentation_reward_source(row):
    data = _source_mapping(row.source_data)
    return (
        data.get("row_kind") != "direct_order_expense"
        and data.get("recorder_type") not in {RETAIL_REPORT, MONTH_CLOSE}
    )



def _row_document_key(row):
    data = _source_mapping(row.source_data)
    recorder_type = data.get("recorder_type") or ""
    recorder = str(data.get("recorder") or row.source_recorder or "")
    return f"odata-source:{row.organization_id}:{recorder_type}:{recorder}"


def _row_gp(row):
    if row.cost_source == OneCMonthlyProfit.COST_SOURCE_UNDEFINED:
        return None
    value = row.displayed_gross_profit
    return None if value is None else money(value)


def _lock_reward_organization(organization):
    """Serialize reward mutations/closing per organization on the DB connection."""
    Organization.objects.select_for_update().only("pk").get(pk=organization.pk)



def _reload_reward_participation(participation):
    return (
        RewardParticipation.objects.select_for_update()
        .select_related("employee", "author_identity", "organization")
        .get(pk=participation.pk)
    )


def _reload_author_identity(identity):
    return (
        OneCAuthorIdentity.objects.select_for_update()
        .select_related("organization", "employee")
        .get(pk=identity.pk)
    )


def active_profit_rows(organization, period_month):
    return list(
        OneCMonthlyProfit.objects.active_for(organization)
        .filter(period_month=period_month)
        .select_related("import_batch")
        .order_by("source_row_number", "id")
    )


def _resolve_stale_author_proposals(organization, user, period_month, key, current_author_guid):
    stale = (
        RewardParticipation.objects.select_related("author_identity")
        .filter(
            organization=organization,
            period_month=period_month,
            role=RewardParticipation.ROLE_DOCUMENTATION,
            scope_key=key,
            source_document_key=key,
            assignment_source=RewardParticipation.SOURCE_ONEC_AUTHOR,
            author_identity__isnull=False,
        )
        .exclude(status=RewardParticipation.STATUS_NOT_APPLICABLE)
    )
    if current_author_guid:
        stale = stale.exclude(author_identity__onec_user_id=current_author_guid)
    for item in stale:
        before = participation_snapshot(item)
        item.status = RewardParticipation.STATUS_NOT_APPLICABLE
        item.basis = "Автор_Key исходного документа изменился после повторной синхронизации."
        item.save(update_fields=["status", "basis", "updated_at"])
        RewardParticipationChange.objects.create(
            participation=item,
            actor=user,
            before=before,
            after=participation_snapshot(item),
            reason="Автор_Key документа изменён в источнике 1С",
        )


def _resolve_missing_author_placeholder(organization, user, period_month, key):
    placeholders = RewardParticipation.objects.filter(
        organization=organization,
        period_month=period_month,
        role=RewardParticipation.ROLE_DOCUMENTATION,
        scope_key=key,
        source_document_key=key,
        assignment_source=RewardParticipation.SOURCE_ONEC_AUTHOR,
        author_identity__isnull=True,
    )
    marker_reason = "Источник 1С после синхронизации предоставил Автор_Key"
    for item in placeholders:
        latest_reason = (
            item.changes.order_by("-id").values_list("reason", flat=True).first()
        )
        if (
            item.status == RewardParticipation.STATUS_NOT_APPLICABLE
            and latest_reason == marker_reason
        ):
            continue
        before = participation_snapshot(item)
        item.status = RewardParticipation.STATUS_NOT_APPLICABLE
        item.basis = "Ранее автор отсутствовал; после повторной синхронизации Автор_Key получен."
        item.save(update_fields=["status", "basis", "updated_at"])
        RewardParticipationChange.objects.create(
            participation=item,
            actor=user,
            before=before,
            after=participation_snapshot(item),
            reason=marker_reason,
        )



def _ensure_required_documentation(organization, user, period_month, key, row, *, basis, author_identity=None):
    data = _source_mapping(row.source_data)
    existing = RewardParticipation.objects.filter(
        organization=organization,
        period_month=period_month,
        role=RewardParticipation.ROLE_DOCUMENTATION,
        scope_key=key,
        source_document_key=key,
        assignment_source=RewardParticipation.SOURCE_ONEC_AUTHOR,
        author_identity=author_identity,
    ).order_by("id").first()
    if existing:
        if (
            author_identity is None
            and existing.status == RewardParticipation.STATUS_NOT_APPLICABLE
            and existing.changes.order_by("-id").values_list("reason", flat=True).first()
            == "Источник 1С после синхронизации предоставил Автор_Key"
        ):
            before = participation_snapshot(existing)
            existing.employee = None
            existing.status = RewardParticipation.STATUS_REQUIRED
            existing.share = ONE
            existing.confirmed_by = None
            existing.confirmed_at = None
            existing.basis = (
                "Автор_Key снова отсутствует после ранее синхронизированного автора — "
                "требуется новое решение руководителя."
            )
            existing.save(update_fields=[
                "employee", "status", "share", "confirmed_by", "confirmed_at",
                "basis", "updated_at",
            ])
            RewardParticipationChange.objects.create(
                participation=existing,
                actor=user,
                before=before,
                after=participation_snapshot(existing),
                reason="Автор_Key снова отсутствует после повторной синхронизации",
            )
        return existing, False
    item = RewardParticipation.objects.create(
        organization=organization,
        employee=None,
        author_identity=author_identity,
        role=RewardParticipation.ROLE_DOCUMENTATION,
        status=RewardParticipation.STATUS_REQUIRED,
        share=ONE,
        period_month=period_month,
        scope_key=key,
        source_document_key=key,
        source_document_type=data.get("recorder_type") or "",
        source_document_guid=str(data.get("recorder") or row.source_recorder or ""),
        source_document_number=data.get("document_number") or "",
        source_document_date=_safe_date(data.get("document_date") or data.get("source_date")),
        scope_line_identities=[],
        customer_name=row.customer_name,
        basis=basis,
        assignment_source=RewardParticipation.SOURCE_ONEC_AUTHOR,
        created_by=user,
    )
    RewardParticipationChange.objects.create(
        participation=item,
        actor=user,
        before={},
        after=participation_snapshot(item),
        reason=basis,
    )
    return item, True


def _retire_removed_author_proposals(
    organization, user, period_month, active_document_keys
):
    removed = (
        RewardParticipation.objects.filter(
            organization=organization,
            period_month=period_month,
            role=RewardParticipation.ROLE_DOCUMENTATION,
            assignment_source=RewardParticipation.SOURCE_ONEC_AUTHOR,
        )
        .exclude(status=RewardParticipation.STATUS_NOT_APPLICABLE)
        .exclude(source_document_key__in=active_document_keys)
    )
    for item in removed:
        before = participation_snapshot(item)
        item.status = RewardParticipation.STATUS_NOT_APPLICABLE
        item.basis = (
            "Исходный документ отсутствует в активной подтверждённой версии месяца."
        )
        item.save(update_fields=["status", "basis", "updated_at"])
        RewardParticipationChange.objects.create(
            participation=item,
            actor=user,
            before=before,
            after=participation_snapshot(item),
            reason="Документ удалён или перенесён при повторной синхронизации",
        )




@transaction.atomic
def sync_author_proposals(organization, user, period_month, *, enrich_names=True):
    if not can_manage_participation(user, organization):
        raise PermissionDenied
    _lock_reward_organization(organization)
    if RewardMonthClose.objects.filter(organization=organization, period_month=period_month).exists():
        raise ValidationError("Месяц закрыт. Новые назначения оформляются корректировкой.")
    rows = active_profit_rows(organization, period_month)
    by_doc = {}
    for row in rows:
        data = _source_mapping(row.source_data)
        if not _is_documentation_reward_source(row):
            continue
        recorder_type = data.get("recorder_type")
        key = _row_document_key(row)
        by_doc.setdefault(key, row)
    _retire_removed_author_proposals(
        organization, user, period_month, set(by_doc)
    )
    missing_name_guids = set()
    for row in by_doc.values():
        source_data = _source_mapping(row.source_data)
        author_guid = _source_text(source_data.get("author_guid"))
        author_name = _source_text(source_data.get("author_name"))
        if author_guid and not author_name:
            missing_name_guids.add(author_guid)
    live_author_names = {}
    if enrich_names and missing_name_guids:
        try:
            live_author_names = read_odata_author_names(missing_name_guids)
        except (ODataPreviewError, ValidationError, OSError):
            # Name enrichment is helpful for the manager but must not block
            # reconciliation of the stable 1C author GUID.
            live_author_names = {}
    created = 0
    issues = 0
    names_updated = 0
    for key, row in by_doc.items():
        data = _source_mapping(row.source_data)
        author_guid = _source_text(data.get("author_guid"))
        author_name = (
            _source_text(data.get("author_name"))
            or live_author_names.get(author_guid, "")
        )
        if not author_guid:
            _resolve_stale_author_proposals(
                organization, user, period_month, key, None
            )
            _, was_created = _ensure_required_documentation(
                organization, user, period_month, key, row,
                basis="Автор исходного документа 1С отсутствует — требуется сопоставление вручную.",
            )
            created += int(was_created)
            issues += 1
            continue
        _resolve_missing_author_placeholder(organization, user, period_month, key)
        _resolve_stale_author_proposals(
            organization, user, period_month, key, author_guid
        )
        identity, _ = OneCAuthorIdentity.objects.get_or_create(
            organization=organization,
            onec_user_id=author_guid,
            defaults={"raw_name": author_name, "status": OneCAuthorIdentity.STATUS_NEEDS_MAPPING},
        )
        if author_name and identity.raw_name != author_name:
            identity.raw_name = author_name
            identity.save(update_fields=["raw_name", "updated_at"])
            names_updated += 1
        if identity.status == OneCAuthorIdentity.STATUS_TECHNICAL:
            _, was_created = _ensure_required_documentation(
                organization, user, period_month, key, row,
                basis="Автор — техническая учётная запись 1С; сотрудник не назначен автоматически.",
                author_identity=identity,
            )
            created += int(was_created)
            issues += 1
            continue
        if identity.status == OneCAuthorIdentity.STATUS_EXCLUDED:
            continue
        employee = identity.employee if identity.status == OneCAuthorIdentity.STATUS_MAPPED else None
        status = RewardParticipation.STATUS_CONFIRMED if employee else RewardParticipation.STATUS_REQUIRED
        defaults = {
            "employee": employee,
            "author_identity": identity,
            "status": status,
            "share": ONE,
            "period_month": period_month,
            "source_document_type": data.get("recorder_type") or "",
            "source_document_guid": str(data.get("recorder") or row.source_recorder or ""),
            "source_document_number": data.get("document_number") or "",
            "source_document_date": _safe_date(data.get("document_date") or data.get("source_date")),
            "scope_line_identities": [],
            "customer_name": row.customer_name,
            "basis": "Автор исходного документа 1С",
            "assignment_source": RewardParticipation.SOURCE_ONEC_AUTHOR,
            "created_by": user,
            "confirmed_by": user if employee else None,
            "confirmed_at": timezone.now() if employee else None,
        }
        proposal, was_created = RewardParticipation.objects.get_or_create(
            organization=organization,
            period_month=period_month,
            role=RewardParticipation.ROLE_DOCUMENTATION,
            scope_key=key,
            source_document_key=key,
            author_identity=identity,
            defaults=defaults,
        )
        if was_created:
            RewardParticipationChange.objects.create(
                participation=proposal,
                actor=user,
                before={},
                after=participation_snapshot(proposal),
                reason=(
                    "Сопоставленный автор 1С автоматически подтверждён"
                    if employee
                    else "Автор 1С требует сопоставления"
                ),
            )
        if (
            not was_created
            and employee
            and proposal.assignment_source == RewardParticipation.SOURCE_ONEC_AUTHOR
            and proposal.status in {
                RewardParticipation.STATUS_REQUIRED,
                RewardParticipation.STATUS_PENDING,
            }
        ):
            before = participation_snapshot(proposal)
            proposal.employee = employee
            proposal.status = RewardParticipation.STATUS_CONFIRMED
            proposal.share = ONE
            proposal.basis = "Автор исходного документа 1С"
            proposal.confirmed_by = user
            proposal.confirmed_at = timezone.now()
            proposal.save(update_fields=[
                "employee", "status", "share", "basis",
                "confirmed_by", "confirmed_at", "updated_at",
            ])
            RewardParticipationChange.objects.create(
                participation=proposal,
                actor=user,
                before=before,
                after=participation_snapshot(proposal),
                reason="Сопоставленный автор 1С автоматически подтверждён",
            )
        if (
            not was_created
            and proposal.assignment_source == RewardParticipation.SOURCE_ONEC_AUTHOR
            and proposal.status == RewardParticipation.STATUS_NOT_APPLICABLE
        ):
            before = participation_snapshot(proposal)
            proposal.employee = employee
            proposal.status = status
            proposal.share = ONE
            proposal.basis = "Автор исходного документа 1С"
            proposal.confirmed_by = user if employee else None
            proposal.confirmed_at = timezone.now() if employee else None
            proposal.save(update_fields=[
                "employee", "status", "share", "basis",
                "confirmed_by", "confirmed_at", "updated_at",
            ])
            RewardParticipationChange.objects.create(
                participation=proposal,
                actor=user,
                before=before,
                after=participation_snapshot(proposal),
                reason="Автор_Key снова соответствует ранее созданному предложению",
            )
        created += int(was_created)
    return {
        "created": created,
        "issues": issues,
        "names_updated": names_updated,
    }


def _safe_date(value):
    try:
        return date.fromisoformat(value) if value else None
    except (TypeError, ValueError):
        return None


@transaction.atomic
def map_author(identity, employee, user):
    if not can_manage_participation(user, identity.organization):
        raise PermissionDenied
    _lock_reward_organization(identity.organization)
    identity = _reload_author_identity(identity)
    if employee.organization_id != identity.organization_id:
        raise ValidationError("Сотрудник относится к другой организации.")
    identity.employee = employee
    identity.status = OneCAuthorIdentity.STATUS_MAPPED
    identity.confirmed_by = user
    identity.confirmed_at = timezone.now()
    identity.save(update_fields=["employee", "status", "confirmed_by", "confirmed_at", "updated_at"])
    closed_periods = set(
        RewardMonthClose.objects.filter(
            organization=identity.organization
        ).values_list("period_month", flat=True)
    )
    pending_items = list(
        identity.reward_participations.select_for_update()
        .filter(status=RewardParticipation.STATUS_REQUIRED)
        .exclude(period_month__in=closed_periods)
    )
    for item in pending_items:
        before = participation_snapshot(item)
        item.employee = employee
        item.status = RewardParticipation.STATUS_CONFIRMED
        item.confirmed_by = user
        item.confirmed_at = timezone.now()
        item.save(update_fields=[
            "employee", "status", "confirmed_by", "confirmed_at", "updated_at",
        ])
        RewardParticipationChange.objects.create(
            participation=item,
            actor=user,
            before=before,
            after=participation_snapshot(item),
            reason="Сопоставленный автор 1С автоматически подтверждён",
        )


@transaction.atomic
def save_participation(participation, user, *, employee, role, share, status, line_identities=None):
    if not can_manage_participation(user, participation.organization):
        raise PermissionDenied
    _lock_reward_organization(participation.organization)
    participation = _reload_reward_participation(participation)
    if RewardMonthClose.objects.filter(
        organization=participation.organization, period_month=participation.period_month
    ).exists():
        raise ValidationError("Закрытый месяц нельзя переписывать.")
    if employee and employee.organization_id != participation.organization_id:
        raise ValidationError("Сотрудник относится к другой организации.")
    share = Decimal(str(share)).quantize(Decimal("0.000001"))
    if share < 0 or share > 1:
        raise ValidationError("Доля должна быть от 0 до 100%.")
    before = participation_snapshot(participation)
    participation.employee = employee
    participation.role = role
    participation.share = share
    participation.status = status
    if line_identities is not None:
        participation.scope_line_identities = list(dict.fromkeys(line_identities))
    if status == RewardParticipation.STATUS_CONFIRMED:
        participation.confirmed_by = user
        participation.confirmed_at = timezone.now()
    participation.full_clean()
    participation.save()
    RewardParticipationChange.objects.create(
        participation=participation,
        actor=user,
        before=before,
        after=participation_snapshot(participation),
    )
    return participation


@transaction.atomic
def cancel_pending_participation(participation, user):
    if not can_manage_participation(user, participation.organization):
        raise PermissionDenied
    _lock_reward_organization(participation.organization)
    participation = _reload_reward_participation(participation)
    if RewardMonthClose.objects.filter(
        organization=participation.organization,
        period_month=participation.period_month,
    ).exists():
        raise ValidationError("Закрытый месяц нельзя переписывать.")
    if participation.status != RewardParticipation.STATUS_PENDING:
        raise ValidationError("Отменить можно только назначение, ожидающее подтверждения.")
    if participation.assignment_source not in {
        RewardParticipation.SOURCE_MANUAL,
        RewardParticipation.SOURCE_TEMPLATE,
    }:
        raise ValidationError("Автоматическое назначение автора 1С отменяется через сопоставление автора.")
    before = participation_snapshot(participation)
    participation.status = RewardParticipation.STATUS_NOT_APPLICABLE
    participation.basis = (
        (participation.basis + " · ") if participation.basis else ""
    ) + "Ошибочное назначение отменено руководителем."
    participation.confirmed_by = user
    participation.confirmed_at = timezone.now()
    participation.save(update_fields=[
        "status", "basis", "confirmed_by", "confirmed_at", "updated_at",
    ])
    RewardParticipationChange.objects.create(
        participation=participation,
        actor=user,
        before=before,
        after=participation_snapshot(participation),
        reason="Отмена ошибочного pending-назначения",
    )
    return participation


@transaction.atomic
def update_participation_share(participation, user, share):
    if not can_manage_participation(user, participation.organization):
        raise PermissionDenied
    _lock_reward_organization(participation.organization)
    participation = _reload_reward_participation(participation)
    if RewardMonthClose.objects.filter(
        organization=participation.organization,
        period_month=participation.period_month,
    ).exists():
        raise ValidationError("Закрытый месяц нельзя переписывать.")
    if participation.status == RewardParticipation.STATUS_NOT_APPLICABLE:
        raise ValidationError("Для роли «не применяется» доля не задаётся.")
    share = Decimal(str(share)).quantize(Decimal("0.000001"))
    if share <= 0 or share > ONE:
        raise ValidationError("Доля должна быть больше 0 и не больше 100%.")
    if participation.status == RewardParticipation.STATUS_CONFIRMED:
        other_share = RewardParticipation.objects.filter(
            organization=participation.organization,
            period_month=participation.period_month,
            scope_key=participation.scope_key,
            role=participation.role,
            status=RewardParticipation.STATUS_CONFIRMED,
        ).exclude(pk=participation.pk).aggregate(total=models.Sum("share"))["total"] or Decimal("0")
        if other_share + share > ONE:
            raise ValidationError("Подтверждённые доли по роли превышают 100%.")
    before = participation_snapshot(participation)
    participation.share = share
    participation.save(update_fields=["share", "updated_at"])
    RewardParticipationChange.objects.create(
        participation=participation,
        actor=user,
        before=before,
        after=participation_snapshot(participation),
        reason="Изменение доли участия",
    )
    return participation


@transaction.atomic
def confirm_participation(participation, user):
    if not can_manage_participation(user, participation.organization):
        raise PermissionDenied
    _lock_reward_organization(participation.organization)
    participation = _reload_reward_participation(participation)
    if RewardMonthClose.objects.filter(
        organization=participation.organization,
        period_month=participation.period_month,
    ).exists():
        raise ValidationError("Закрытый месяц нельзя переписывать.")
    if not participation.employee_id:
        raise ValidationError("Нельзя подтвердить участие без сотрудника.")
    confirmed_share = RewardParticipation.objects.filter(
        organization=participation.organization,
        period_month=participation.period_month,
        scope_key=participation.scope_key,
        role=participation.role,
        status=RewardParticipation.STATUS_CONFIRMED,
    ).exclude(pk=participation.pk).aggregate(total=models.Sum("share"))["total"] or Decimal("0")
    if confirmed_share + participation.share > ONE:
        raise ValidationError("Подтверждённые доли по роли превышают 100%.")
    before = participation_snapshot(participation)
    participation.status = RewardParticipation.STATUS_CONFIRMED
    participation.confirmed_by = user
    participation.confirmed_at = timezone.now()
    participation.save(update_fields=[
        "status", "confirmed_by", "confirmed_at", "updated_at"
    ])
    RewardParticipationChange.objects.create(
        participation=participation,
        actor=user,
        before=before,
        after=participation_snapshot(participation),
        reason="Подтверждение участия",
    )
    return participation


def participation_snapshot(item):
    return {
        "employee_id": item.employee_id,
        "role": item.role,
        "share": str(item.share),
        "status": item.status,
        "scope_line_identities": item.scope_line_identities,
    }


def _scope_rows(participation, rows_by_identity, rows_by_document):
    if participation.scope_line_identities:
        return [rows_by_identity[key] for key in participation.scope_line_identities if key in rows_by_identity]
    return rows_by_document.get(participation.source_document_key, [])


def _rate_for(role, scheme):
    return {
        RewardParticipation.ROLE_SALE: scheme.sale_rate,
        RewardParticipation.ROLE_PROJECT: scheme.project_rate,
        RewardParticipation.ROLE_WORK: scheme.work_rate,
        RewardParticipation.ROLE_CLIENT_MANAGER: scheme.client_manager_rate,
    }.get(role)


def _fixed_for(document_type, scheme):
    return scheme.documentation_retail_fixed if document_type == RETAIL_CHECK else scheme.documentation_document_fixed


def _author_sync_issue_count(organization, period_month, rows):
    current_documents = {}
    for row in rows:
        data = _source_mapping(row.source_data)
        if not _is_documentation_reward_source(row):
            continue
        current_documents.setdefault(_row_document_key(row), row)

    issue_count = 0
    active_generated_keys = set(
        RewardParticipation.objects.filter(
            organization=organization,
            period_month=period_month,
            role=RewardParticipation.ROLE_DOCUMENTATION,
            assignment_source=RewardParticipation.SOURCE_ONEC_AUTHOR,
        )
        .exclude(status=RewardParticipation.STATUS_NOT_APPLICABLE)
        .values_list("source_document_key", flat=True)
    )
    issue_count += len(active_generated_keys.difference(current_documents))
    for key, row in current_documents.items():
        expected_guid = _source_text(_source_mapping(row.source_data).get("author_guid"))
        all_proposals = list(
            RewardParticipation.objects.filter(
                organization=organization,
                period_month=period_month,
                role=RewardParticipation.ROLE_DOCUMENTATION,
                source_document_key=key,
                assignment_source=RewardParticipation.SOURCE_ONEC_AUTHOR,
            ).select_related("author_identity")
        )
        if not all_proposals:
            issue_count += 1
            continue
        active = [
            item for item in all_proposals
            if item.status != RewardParticipation.STATUS_NOT_APPLICABLE
        ]
        if expected_guid:
            matching = [
                item for item in active
                if item.author_identity_id
                and item.author_identity.onec_user_id == expected_guid
            ]
            stale_active = [
                item for item in active
                if not item.author_identity_id
                or item.author_identity.onec_user_id != expected_guid
            ]
            if not matching or stale_active:
                issue_count += 1
        else:
            if any(item.author_identity_id for item in active):
                issue_count += 1
    return issue_count



def calculate_month(organization, period_month, *, employee_id=None, use_closed=True):
    period_month = month_start(period_month)
    closed = RewardMonthClose.objects.filter(organization=organization, period_month=period_month).first()
    if closed and use_closed:
        snapshot = closed.snapshot
        if employee_id:
            snapshot = dict(snapshot)
            snapshot["employees"] = [x for x in snapshot.get("employees", []) if x.get("employee_id") == employee_id]
            snapshot["details"] = [x for x in snapshot.get("details", []) if x.get("employee_id") == employee_id]
        return snapshot

    scheme = scheme_for_month(organization, period_month)
    rows = active_profit_rows(organization, period_month)
    rows_by_identity = {row.source_identity: row for row in rows}
    rows_by_document = defaultdict(list)
    for row in rows:
        original_key = _row_document_key(row)
        business_key = _row_business_scope_key(row)
        rows_by_document[original_key].append(row)
        if business_key != original_key:
            rows_by_document[business_key].append(row)

    participations = list(
        RewardParticipation.objects.filter(organization=organization, period_month=period_month)
        .select_related("employee", "author_identity")
        .order_by("scope_key", "role", "employee_id", "id")
    )
    groups = defaultdict(list)
    for item in participations:
        groups[(item.scope_key, item.role)].append(item)

    details = []
    issues = []
    participation_keys = {
        (item.source_document_key, item.role)
        for item in participations
    }
    business_scopes = defaultdict(list)
    source_documents = {}
    for row in rows:
        row_data = _source_mapping(row.source_data)
        business_scopes[_row_business_scope_key(row)].append(row)
        if _is_documentation_reward_source(row):
            source_documents.setdefault(_row_document_key(row), row)
    for business_key, scope_rows in business_scopes.items():
        sale_rows = [
            row for row in scope_rows
            if _source_mapping(row.source_data).get("row_kind") != "direct_order_expense"
            and _source_mapping(row.source_data).get("recorder_type") != MONTH_CLOSE
        ]
        if sale_rows and (business_key, RewardParticipation.ROLE_SALE) not in participation_keys:
            issues.append({
                "kind": "missing_sale_role",
                "label": (
                    _source_mapping(sale_rows[0].source_data).get("resolved_order_display")
                    or sale_rows[0].document_name
                    or business_key
                ),
                "count": 1,
            })
        has_service = any(
            classify_nomenclature_type(row.nomenclature_type) == "service"
            for row in sale_rows
        )
        if has_service and (business_key, RewardParticipation.ROLE_WORK) not in participation_keys:
            issues.append({
                "kind": "missing_work_role",
                "label": (
                    _source_mapping(sale_rows[0].source_data).get("resolved_order_display")
                    or sale_rows[0].document_name
                    or business_key
                ),
                "count": 1,
            })
    for source_key, row in source_documents.items():
        if (source_key, RewardParticipation.ROLE_DOCUMENTATION) not in participation_keys:
            issues.append({
                "kind": "missing_documentation_role",
                "label": row.document_name or source_key,
                "count": 1,
            })
    missing_cost_rows = [
        row for row in rows
        if row.cost_source == OneCMonthlyProfit.COST_SOURCE_UNDEFINED
    ]
    if missing_cost_rows:
        issues.append({
            "kind": "month_missing_cost",
            "label": "Есть строки с неопределённой себестоимостью.",
            "count": len(missing_cost_rows),
        })

    employee_totals = defaultdict(lambda: {
        "documentation_count": 0, "seller_revenue": Decimal("0"), "seller_gp": Decimal("0"),
        "documentation_reward": Decimal("0"), "sale_reward": Decimal("0"),
        "project_reward": Decimal("0"), "work_reward": Decimal("0"),
        "adjustments": Decimal("0"), "review_count": 0,
    })

    if scheme is None:
        issues.append({"kind": "missing_scheme", "label": "Нет версии тестовой схемы для месяца."})
    author_sync_issues = _author_sync_issue_count(
        organization, period_month, rows
    )
    if author_sync_issues:
        issues.append({
            "kind": "author_sync_stale",
            "label": "Предложения по авторам не соответствуют активным данным 1С. Обновите предложения по авторам.",
            "count": author_sync_issues,
        })

    for (scope_key, role), items in groups.items():
        confirmed = [x for x in items if x.status == RewardParticipation.STATUS_CONFIRMED and x.employee_id]
        pending = [x for x in items if x.status not in {RewardParticipation.STATUS_CONFIRMED, RewardParticipation.STATUS_NOT_APPLICABLE}]
        if pending:
            issues.append({"kind": "unconfirmed", "label": scope_key, "count": len(pending)})
            for x in pending:
                if x.employee_id:
                    employee_totals[x.employee_id]["review_count"] += 1
        if not confirmed or scheme is None:
            continue
        selected_identities = set(confirmed[0].scope_line_identities or [])
        missing_selected = selected_identities.difference(rows_by_identity)
        if missing_selected:
            issues.append({
                "kind": "missing_selected_lines",
                "label": scope_key,
                "count": len(missing_selected),
            })
            for item in confirmed:
                employee_totals[item.employee_id]["review_count"] += 1
            continue
        moved_selected = [
            identity
            for identity in selected_identities
            if _row_business_scope_key(rows_by_identity[identity])
            != confirmed[0].source_document_key
        ]
        if moved_selected:
            issues.append({
                "kind": "moved_selected_lines",
                "label": scope_key,
                "count": len(moved_selected),
            })
            for item in confirmed:
                employee_totals[item.employee_id]["review_count"] += 1
            continue
        if (
            role != RewardParticipation.ROLE_DOCUMENTATION
            and not selected_identities
        ):
            aliased_rows = rows_by_document.get(
                confirmed[0].source_document_key, []
            )
            moved_all_lines = [
                row for row in aliased_rows
                if _source_mapping(row.source_data).get("row_kind") != "direct_order_expense"
                and _row_business_scope_key(row)
                != confirmed[0].source_document_key
            ]
            if moved_all_lines:
                issues.append({
                    "kind": "moved_all_lines_scope",
                    "label": scope_key,
                    "count": len(moved_all_lines),
                })
                for item in confirmed:
                    employee_totals[item.employee_id]["review_count"] += 1
                continue
        scope_rows = _scope_rows(confirmed[0], rows_by_identity, rows_by_document)
        if not scope_rows:
            issues.append({"kind": "missing_base", "label": scope_key, "count": 1})
            continue
        if role != RewardParticipation.ROLE_DOCUMENTATION and confirmed[0].scope_line_identities:
            full_scope = rows_by_document.get(confirmed[0].source_document_key, [])
            direct_cost_rows = [
                row for row in full_scope
                if _source_mapping(row.source_data).get("row_kind") == "direct_order_expense"
            ]
            business_line_ids = {
                row.source_identity for row in full_scope
                if _source_mapping(row.source_data).get("row_kind") != "direct_order_expense"
                and _source_mapping(row.source_data).get("recorder_type") != MONTH_CLOSE
            }
            selected_ids = set(confirmed[0].scope_line_identities)
            if direct_cost_rows and selected_ids != business_line_ids:
                issues.append({
                    "kind": "partial_direct_cost_allocation",
                    "label": scope_key,
                    "count": len(direct_cost_rows),
                })
                for x in confirmed:
                    employee_totals[x.employee_id]["review_count"] += 1
                continue
            if direct_cost_rows:
                present = {row.source_identity for row in scope_rows}
                scope_rows = scope_rows + [
                    row for row in direct_cost_rows if row.source_identity not in present
                ]
        revenue = money(sum((Decimal(row.revenue or 0) for row in scope_rows), Decimal("0")))
        if role == RewardParticipation.ROLE_DOCUMENTATION:
            base = money(sum((_row_gp(row) or Decimal("0") for row in scope_rows), Decimal("0")))
            base_quality = "Фиксированная оплата оформления"
        else:
            gp_values = [_row_gp(row) for row in scope_rows]
            if any(value is None for value in gp_values):
                issues.append({"kind": "missing_cost", "label": scope_key, "count": 1})
                for x in confirmed:
                    employee_totals[x.employee_id]["review_count"] += 1
                continue
            base = money(sum(gp_values, Decimal("0")))
            base_quality = (
                "Расчётная ВП"
                if any(row.cost_source == OneCMonthlyProfit.COST_SOURCE_CALCULATED for row in scope_rows)
                else "Фактическая ВП"
            )
        share_total = sum((x.share for x in confirmed), Decimal("0"))
        if share_total > ONE:
            issues.append({"kind": "share_overflow", "label": scope_key, "count": 1})
            continue
        if share_total < ONE:
            issues.append({"kind": "unallocated", "label": scope_key, "share": str(ONE - share_total)})

        document_type = confirmed[0].source_document_type
        if role == RewardParticipation.ROLE_DOCUMENTATION:
            fund = money(_fixed_for(document_type, scheme))
        else:
            rate = _rate_for(role, scheme)
            fund = money(max(base, Decimal("0")) * Decimal(rate or 0))
        allocations = _allocate(fund, confirmed, full=(share_total == ONE))
        for item, amount in allocations:
            totals = employee_totals[item.employee_id]
            if role == RewardParticipation.ROLE_DOCUMENTATION:
                totals["documentation_count"] += 1
                totals["documentation_reward"] += amount
                rate_label = f"{_fixed_for(document_type, scheme):.2f} ₽"
            else:
                rate = _rate_for(role, scheme) or Decimal("0")
                rate_label = f"{(rate * 100):.2f}%"
                if role == RewardParticipation.ROLE_SALE:
                    totals["seller_revenue"] += money(revenue * item.share)
                    totals["seller_gp"] += money(base * item.share)
                    totals["sale_reward"] += amount
                elif role == RewardParticipation.ROLE_PROJECT:
                    totals["project_reward"] += amount
                elif role == RewardParticipation.ROLE_WORK:
                    totals["work_reward"] += amount
            details.append({
                "employee_id": item.employee_id,
                "employee": item.employee.display_name,
                "customer": item.customer_name,
                "document": _document_label(item),
                "role": item.get_role_display(),
                "base": str(base),
                "base_quality": base_quality,
                "rate": rate_label,
                "role_fund": str(fund),
                "share": str(item.share),
                "amount": str(amount),
                "status": item.get_status_display(),
                "scope_key": scope_key,
            })

    adjustments = RewardAdjustment.objects.filter(
        organization=organization, period_month=period_month, status=RewardAdjustment.STATUS_CONFIRMED
    ).select_related("employee")
    for adjustment in adjustments:
        employee_totals[adjustment.employee_id]["adjustments"] += money(adjustment.amount)

    employees = []
    employee_map = {e.id: e for e in Employee.objects.filter(organization=organization)}
    for eid, totals in employee_totals.items():
        if employee_id and eid != employee_id:
            continue
        total = money(
            totals["documentation_reward"] + totals["sale_reward"] + totals["project_reward"]
            + totals["work_reward"] + totals["adjustments"]
        )
        employees.append({
            "employee_id": eid,
            "employee": employee_map[eid].display_name if eid in employee_map else f"#{eid}",
            **{k: (str(money(v)) if isinstance(v, Decimal) else v) for k, v in totals.items()},
            "total": str(total),
        })
    employees.sort(key=lambda x: x["employee"].casefold())

    author_names_by_guid = {}
    for row in rows:
        source_data = _source_mapping(row.source_data)
        author_guid = _source_text(source_data.get("author_guid"))
        author_name = _source_text(source_data.get("author_name"))
        if author_guid and author_name:
            author_names_by_guid.setdefault(author_guid, author_name)

    unmapped_authors = list(
        OneCAuthorIdentity.objects.filter(
            organization=organization,
            status=OneCAuthorIdentity.STATUS_NEEDS_MAPPING,
            reward_participations__period_month=period_month,
            reward_participations__status__in=[
                RewardParticipation.STATUS_REQUIRED,
                RewardParticipation.STATUS_PENDING,
                RewardParticipation.STATUS_CONFIRMED,
            ],
        ).distinct().values("id", "onec_user_id", "raw_name")
    )
    for author in unmapped_authors:
        onec_user_id = _source_text(author.get("onec_user_id"))
        raw_name = _source_text(author.get("raw_name"))
        if raw_name == onec_user_id:
            raw_name = ""
        author["display_name"] = (
            raw_name
            or author_names_by_guid.get(onec_user_id)
            or onec_user_id
        )
        author["has_human_name"] = author["display_name"] != onec_user_id
    if unmapped_authors:
        issues.append({"kind": "unmapped_author", "label": "Несопоставленные авторы 1С", "count": len(unmapped_authors)})

    month_gp_values = [_row_gp(row) for row in rows]
    month_gross_profit = (
        None
        if any(value is None for value in month_gp_values)
        else money(sum(month_gp_values, Decimal("0")))
    )
    total_test_reward = money(sum(
        (Decimal(item["total"]) for item in employees),
        Decimal("0"),
    ))
    reward_exceeds_gross_profit = (
        month_gross_profit is not None and total_test_reward > month_gross_profit
    )
    if reward_exceeds_gross_profit:
        issues.append({
            "kind": "reward_exceeds_gross_profit",
            "label": "Суммарный тестовый результат превышает ВП месяца.",
            "count": 1,
        })

    return {
        "period_month": period_month.isoformat(),
        "is_test": True,
        "closed": False,
        "scheme": _scheme_payload(scheme),
        "month_gross_profit": None if month_gross_profit is None else str(month_gross_profit),
        "total_test_reward": str(total_test_reward),
        "reward_exceeds_gross_profit": reward_exceeds_gross_profit,
        "employees": employees,
        "details": [x for x in details if not employee_id or x["employee_id"] == employee_id],
        "issues": issues,
        "unmapped_authors": unmapped_authors,
    }


def _allocate(fund, participations, *, full):
    result = []
    running = Decimal("0")
    ordered = sorted(participations, key=lambda x: (x.employee_id or 0, x.id or 0))
    for index, item in enumerate(ordered):
        if full and index == len(ordered) - 1:
            amount = money(fund - running)
        else:
            amount = money(fund * item.share)
        running += amount
        result.append((item, amount))
    return result


def _document_label(item):
    parts = []
    if item.source_document_number:
        parts.append(f"№{item.source_document_number}")
    if item.source_document_date:
        parts.append(f"от {item.source_document_date:%d.%m.%Y}")
    return " ".join(parts) or item.source_document_key


def _scheme_payload(scheme):
    if scheme is None:
        return None
    return {
        "id": scheme.id, "name": scheme.name, "version": scheme.version,
        "effective_from": scheme.effective_from.isoformat(),
        "documentation_retail_fixed": str(scheme.documentation_retail_fixed),
        "documentation_document_fixed": str(scheme.documentation_document_fixed),
        "sale_rate": str(scheme.sale_rate), "project_rate": str(scheme.project_rate),
        "work_rate": str(scheme.work_rate), "client_manager_rate": str(scheme.client_manager_rate),
    }


@transaction.atomic
def close_month(organization, user, period_month):
    if not can_close_period(user, organization):
        raise PermissionDenied
    period_month = month_start(period_month)
    _lock_reward_organization(organization)
    if RewardMonthClose.objects.filter(organization=organization, period_month=period_month).exists():
        raise ValidationError("Месяц уже закрыт.")
    scheme = scheme_for_month(organization, period_month)
    if scheme is None:
        raise ValidationError("Нельзя закрыть месяц без версии тестовой схемы.")
    snapshot = calculate_month(organization, period_month, use_closed=False)
    blocking = {
        "missing_scheme", "missing_base", "missing_cost", "month_missing_cost",
        "share_overflow", "unmapped_author", "unconfirmed", "unallocated",
        "partial_direct_cost_allocation", "missing_selected_lines", "moved_selected_lines", "moved_all_lines_scope", "author_sync_stale", "missing_sale_role",
        "missing_documentation_role", "missing_work_role",
    }
    if any(issue.get("kind") in blocking for issue in snapshot["issues"]):
        raise ValidationError("Есть неполные или неподтверждённые данные; месяц не закрыт.")
    raw = json.dumps(snapshot, sort_keys=True, ensure_ascii=False).encode("utf-8")
    obj = RewardMonthClose.objects.create(
        organization=organization,
        period_month=period_month,
        scheme_version=scheme,
        snapshot={**snapshot, "closed": True},
        source_hash=hashlib.sha256(raw).hexdigest(),
        closed_by=user,
    )
    return obj


def _row_business_scope_key(row):
    """Stable order scope when available; otherwise the original sale document."""
    data = _source_mapping(row.source_data)
    order_guid = data.get("resolved_order_guid") or data.get("direct_expense_order_guid")
    if order_guid:
        return f"odata-order:{row.organization_id}:{str(order_guid).lower()}"
    return _row_document_key(row)


def reward_document_options(organization, period_month):
    """Read-only order workspace built only from active confirmed Service2 profit rows."""
    period_month = month_start(period_month)
    rows = active_profit_rows(organization, period_month)
    groups = defaultdict(list)
    for row in rows:
        groups[_row_business_scope_key(row)].append(row)

    customer_links = {
        item.onec_customer_id: item
        for item in OneCCustomerIdentity.objects.filter(organization=organization).select_related("client")
    }
    object_links = {
        item.source_document_key: item
        for item in RewardOrderObjectLink.objects.filter(organization=organization).select_related("client", "pool")
    }

    result = []
    for scope_key, scope_rows in groups.items():
        assignment_rows = [
            row for row in scope_rows
            if _source_mapping(row.source_data).get("row_kind") != "direct_order_expense"
            and _source_mapping(row.source_data).get("recorder_type") != MONTH_CLOSE
        ]
        if not assignment_rows:
            # Month-close accounting rows are never standalone reward work items.
            continue
        primary = assignment_rows[0]
        data = _source_mapping(primary.source_data)
        label = _source_text(
            data.get("resolved_order_display")
            or data.get("document_display")
            or primary.document_name
            or "Заказ"
        ) or "Заказ"
        customer_guid = _source_text(
            data.get("resolved_order_customer_guid") or data.get("customer_guid")
        ).lower()
        customer_link = customer_links.get(customer_guid)
        object_link = object_links.get(scope_key)

        revenue = money(sum((Decimal(row.revenue or 0) for row in scope_rows), Decimal("0")))
        analytical_costs = [
            row.analytical_cost
            for row in scope_rows
            if row.analytical_cost is not None
        ]
        cost_missing = any(row.analytical_cost is None for row in scope_rows)
        cost = None if cost_missing else money(sum(analytical_costs, Decimal("0")))
        gp_values = [_row_gp(row) for row in scope_rows]
        gross_profit = (
            None
            if any(value is None for value in gp_values)
            else money(sum(gp_values, Decimal("0")))
        )

        lines = []
        direct_expenses = []
        source_document_keys = set()
        for row in scope_rows:
            row_data = _source_mapping(row.source_data)
            source_document_keys.add(_row_document_key(row))
            recorder_type = row_data.get("recorder_type")
            is_direct = row_data.get("row_kind") == "direct_order_expense"
            if recorder_type == MONTH_CLOSE and not is_direct:
                continue
            gp = _row_gp(row)
            line = {
                "identity": row.source_identity,
                "name": _source_text(
                    row_data.get("direct_expense_line_name")
                    or row_data.get("direct_expense_content")
                    or row.nomenclature
                ) or "Позиция",
                "type": "Прямые затраты" if is_direct else row.nomenclature_type,
                "kind": classify_nomenclature_type(row.nomenclature_type),
                "revenue": str(money(row.revenue or 0)),
                "cost": None if row.analytical_cost is None else str(money(row.analytical_cost)),
                "gross_profit": None if gp is None else str(gp),
                "cost_missing": gp is None,
                "is_direct_expense": is_direct,
            }
            if is_direct:
                direct_expenses.append(line)
            else:
                lines.append(line)

        result.append({
            "scope_key": scope_key,
            "label": label,
            "customer": _source_text(
                data.get("resolved_order_customer_name") or primary.customer_name
            ) or "Покупатель не указан",
            "customer_guid": customer_guid,
            "client_id": customer_link.client_id if customer_link else None,
            "client_name": customer_link.client.name if customer_link else "",
            "pool_id": object_link.pool_id if object_link else None,
            "pool_label": object_link.pool.address if object_link else "",
            "source_document_type": data.get("resolved_order_type") or data.get("recorder_type") or "",
            "source_document_guid": str(data.get("resolved_order_guid") or data.get("recorder") or primary.source_recorder or ""),
            "source_document_number": data.get("resolved_order_number") or data.get("document_number") or "",
            "source_document_date": _safe_date(data.get("resolved_order_date") or data.get("document_date") or data.get("source_date")),
            "source_document_keys": sorted(source_document_keys),
            "revenue": str(revenue),
            "cost": None if cost is None else str(cost),
            "gross_profit": None if gross_profit is None else str(gross_profit),
            "cost_missing": cost_missing,
            "has_work": any(item["kind"] == "service" for item in lines),
            "lines": lines,
            "direct_expenses": direct_expenses,
            "direct_expense_total": str(money(sum(
                (Decimal(item["cost"] or 0) for item in direct_expenses),
                Decimal("0"),
            ))),
        })
    result.sort(key=lambda item: (item["customer"].casefold(), item["label"].casefold()))
    return result


def _assignment_scope_key(document_key, role, line_identities):
    normalized = sorted(set(line_identities or []))
    suffix = "all"
    if normalized:
        digest = hashlib.sha256("\n".join(normalized).encode("utf-8")).hexdigest()[:20]
        suffix = f"lines:{digest}"
    return f"{document_key}:{role}:{suffix}"


@transaction.atomic
def create_manual_participation(
    organization,
    user,
    period_month,
    *,
    document_key,
    employee,
    role,
    share,
    line_identities=None,
    not_applicable=False,
    assignment_source=RewardParticipation.SOURCE_MANUAL,
    basis="Ручное распределение по подтверждённым строкам ВП",
):
    if not can_manage_participation(user, organization):
        raise PermissionDenied
    period_month = month_start(period_month)
    _lock_reward_organization(organization)
    if RewardMonthClose.objects.filter(organization=organization, period_month=period_month).exists():
        raise ValidationError("Закрытый месяц нельзя переписывать.")
    roles = {
        RewardParticipation.ROLE_CLIENT_MANAGER,
        RewardParticipation.ROLE_SALE,
        RewardParticipation.ROLE_PROJECT,
        RewardParticipation.ROLE_WORK,
    }
    if role not in roles:
        raise ValidationError("Эта роль недоступна для ручного создания участия.")
    options = {item["scope_key"]: item for item in reward_document_options(organization, period_month)}
    document = options.get(document_key)
    if document is None:
        raise ValidationError("Документ не относится к активным подтверждённым данным месяца.")
    allowed_lines = {item["identity"] for item in document["lines"] if not item["is_direct_expense"]}
    selected = list(dict.fromkeys(line_identities or []))
    if any(identity not in allowed_lines for identity in selected):
        raise ValidationError("Выбранная строка не относится к документу или является прямой затратой.")
    if role == RewardParticipation.ROLE_WORK and selected:
        line_kinds = {
            item["identity"]: item["kind"]
            for item in document["lines"]
            if not item["is_direct_expense"]
        }
        if any(line_kinds.get(identity) != "service" for identity in selected):
            raise ValidationError("Для роли «Выполнение работ» можно выбирать только работы и услуги.")
    if role in {RewardParticipation.ROLE_PROJECT, RewardParticipation.ROLE_WORK} and not selected and not not_applicable:
        raise ValidationError("Для проекта или выполнения работ нужно выбрать конкретные позиции/работы.")
    if not_applicable:
        employee = None
        share_decimal = Decimal("0")
        status = RewardParticipation.STATUS_NOT_APPLICABLE
    else:
        if employee is None or employee.organization_id != organization.id:
            raise ValidationError("Нужно выбрать сотрудника этой организации.")
        share_decimal = Decimal(str(share)).quantize(Decimal("0.000001"))
        if share_decimal <= 0 or share_decimal > ONE:
            raise ValidationError("Доля должна быть больше 0 и не больше 100%.")
        status = (
            RewardParticipation.STATUS_CONFIRMED
            if assignment_source == RewardParticipation.SOURCE_MANUAL
            else RewardParticipation.STATUS_PENDING
        )
    scope_key = _assignment_scope_key(document_key, role, selected)
    active_role_scopes = RewardParticipation.objects.filter(
        organization=organization,
        period_month=period_month,
        source_document_key=document_key,
        role=role,
    ).exclude(status=RewardParticipation.STATUS_NOT_APPLICABLE)
    selected_set = set(selected)
    for current in active_role_scopes:
        if current.scope_key == scope_key:
            continue
        current_set = set(current.scope_line_identities or [])
        overlaps = (
            not selected_set
            or not current_set
            or bool(selected_set.intersection(current_set))
        )
        if overlaps:
            raise ValidationError(
                "Пересекающиеся наборы позиций в одной роли недопустимы; "
                "добавьте сотрудника в существующий объём либо выберите непересекающиеся строки."
            )
    active_share = (
        RewardParticipation.objects.filter(
            organization=organization,
            period_month=period_month,
            scope_key=scope_key,
            role=role,
        )
        .exclude(status=RewardParticipation.STATUS_NOT_APPLICABLE)
        .aggregate(total=models.Sum("share"))["total"]
        or Decimal("0")
    )
    if not not_applicable and active_share + share_decimal > ONE:
        raise ValidationError(
            "Суммарная доля по этой роли и выбранным позициям не может превышать 100%."
        )
    existing = RewardParticipation.objects.filter(
        organization=organization,
        period_month=period_month,
        scope_key=scope_key,
        role=role,
        employee=employee,
        status__in=[RewardParticipation.STATUS_PENDING, RewardParticipation.STATUS_CONFIRMED, RewardParticipation.STATUS_NOT_APPLICABLE],
    ).first()
    if existing:
        raise ValidationError("Такое участие уже добавлено.")
    item = RewardParticipation(
        organization=organization,
        employee=employee,
        role=role,
        status=status,
        share=share_decimal,
        period_month=period_month,
        scope_key=scope_key,
        source_document_key=document_key,
        source_document_type=document["source_document_type"],
        source_document_guid=document["source_document_guid"],
        source_document_number=document["source_document_number"],
        source_document_date=document["source_document_date"],
        scope_line_identities=selected,
        customer_name=document["customer"],
        basis=basis,
        assignment_source=assignment_source,
        created_by=user,
        confirmed_by=user if status == RewardParticipation.STATUS_CONFIRMED else None,
        confirmed_at=timezone.now() if status == RewardParticipation.STATUS_CONFIRMED else None,
    )
    item.full_clean()
    item.save()
    RewardParticipationChange.objects.create(
        participation=item,
        actor=user,
        before={},
        after=participation_snapshot(item),
        reason="Создание назначения",
    )
    return item


@transaction.atomic
def resolve_documentation_placeholder(participation, user, *, employee=None, not_applicable=False):
    if not can_manage_participation(user, participation.organization):
        raise PermissionDenied
    _lock_reward_organization(participation.organization)
    participation = _reload_reward_participation(participation)
    if RewardMonthClose.objects.filter(
        organization=participation.organization,
        period_month=participation.period_month,
    ).exists():
        raise ValidationError("Закрытый месяц нельзя переписывать.")
    if (
        participation.role != RewardParticipation.ROLE_DOCUMENTATION
        or participation.author_identity_id is not None
        or participation.status != RewardParticipation.STATUS_REQUIRED
        or participation.employee_id is not None
    ):
        raise ValidationError("Эта строка не является неразрешённым оформлением без Автор_Key.")
    before = participation_snapshot(participation)
    if not_applicable:
        participation.status = RewardParticipation.STATUS_NOT_APPLICABLE
        participation.share = Decimal("0")
        participation.basis = "Оформление вручную отмечено как не применимое."
        participation.confirmed_by = user
        participation.confirmed_at = timezone.now()
    else:
        if employee is None or employee.organization_id != participation.organization_id:
            raise ValidationError("Нужно выбрать сотрудника этой организации.")
        participation.employee = employee
        participation.status = RewardParticipation.STATUS_CONFIRMED
        participation.share = ONE
        participation.basis = "Автор_Key отсутствовал; оформитель назначен руководителем вручную."
        participation.confirmed_by = user
        participation.confirmed_at = timezone.now()
    participation.save()
    RewardParticipationChange.objects.create(
        participation=participation,
        actor=user,
        before=before,
        after=participation_snapshot(participation),
        reason="Разрешение оформления без Автор_Key",
    )
    return participation


@transaction.atomic
def add_documentation_participant(participation, employee, share, user):
    """Add a co-documenter to the same fixed-fee unit; never creates another fund."""
    if participation.role != RewardParticipation.ROLE_DOCUMENTATION:
        raise ValidationError("Совместный оформитель добавляется только к роли оформления.")
    if not can_manage_participation(user, participation.organization):
        raise PermissionDenied
    _lock_reward_organization(participation.organization)
    participation = _reload_reward_participation(participation)
    if RewardMonthClose.objects.filter(
        organization=participation.organization,
        period_month=participation.period_month,
    ).exists():
        raise ValidationError("Совместного оформителя нельзя добавлять после закрытия месяца.")
    if participation.status not in {
        RewardParticipation.STATUS_PENDING,
        RewardParticipation.STATUS_CONFIRMED,
    }:
        raise ValidationError(
            "Совместного оформителя можно добавлять только к активному назначению оформления."
        )
    source_is_active = any(
        _is_documentation_reward_source(row)
        and _row_document_key(row) == participation.source_document_key
        for row in active_profit_rows(
            participation.organization,
            participation.period_month,
        )
    )
    if not source_is_active:
        raise ValidationError(
            "Исходный документ оформления отсутствует в активной версии месяца."
        )
    if employee.organization_id != participation.organization_id:
        raise ValidationError("Сотрудник относится к другой организации.")
    share = Decimal(str(share)).quantize(Decimal("0.000001"))
    if share <= 0 or share > ONE:
        raise ValidationError("Доля должна быть больше 0 и не больше 100%.")
    current_share = (
        RewardParticipation.objects.filter(
            organization=participation.organization,
            period_month=participation.period_month,
            scope_key=participation.scope_key,
            role=participation.role,
        )
        .exclude(status=RewardParticipation.STATUS_NOT_APPLICABLE)
        .aggregate(total=models.Sum("share"))["total"]
        or Decimal("0")
    )
    if current_share + share > ONE:
        raise ValidationError("Суммарная доля оформителей не может превышать 100%.")
    item = RewardParticipation.objects.create(
        organization=participation.organization,
        employee=employee,
        role=participation.role,
        status=RewardParticipation.STATUS_CONFIRMED,
        share=share,
        period_month=participation.period_month,
        scope_key=participation.scope_key,
        source_document_key=participation.source_document_key,
        source_document_type=participation.source_document_type,
        source_document_guid=participation.source_document_guid,
        source_document_number=participation.source_document_number,
        source_document_date=participation.source_document_date,
        scope_line_identities=list(participation.scope_line_identities),
        customer_name=participation.customer_name,
        object_label=participation.object_label,
        basis="Совместное оформление комплекта документов",
        assignment_source=RewardParticipation.SOURCE_MANUAL,
        created_by=user,
        confirmed_by=user,
        confirmed_at=timezone.now(),
    )
    RewardParticipationChange.objects.create(
        participation=item,
        actor=user,
        before={},
        after=participation_snapshot(item),
        reason="Добавлен совместный оформитель",
    )
    return item


@transaction.atomic
def confirm_participation_batch(organization, user, period_month, participation_ids):
    if not can_manage_participation(user, organization):
        raise PermissionDenied
    period_month = month_start(period_month)
    _lock_reward_organization(organization)
    ids = list(dict.fromkeys(int(value) for value in participation_ids))
    items = list(
        RewardParticipation.objects.select_for_update()
        .filter(
            organization=organization,
            period_month=period_month,
            pk__in=ids,
        )
        .select_related("employee")
        .order_by("scope_key", "role", "id")
    )
    if len(items) != len(ids):
        raise ValidationError("Часть выбранных назначений недоступна.")
    if any(item.status == RewardParticipation.STATUS_NOT_APPLICABLE for item in items):
        raise ValidationError("Строка «не применяется» не требует подтверждения.")
    for item in items:
        if item.status != RewardParticipation.STATUS_CONFIRMED:
            confirm_participation(item, user)
    return items


def map_customer_identity(organization, user, period_month, *, document_key, client):
    """Create/update an explicit 1C counterparty -> Service2 client mapping."""
    if not can_manage_participation(user, organization):
        raise PermissionDenied
    if client.organization_id != organization.id:
        raise ValidationError("Клиент относится к другой организации.")
    options = {item["scope_key"]: item for item in reward_document_options(organization, period_month)}
    document = options.get(document_key)
    if not document or not document.get("customer_guid"):
        raise ValidationError("У заказа нет надёжного GUID контрагента 1С для сопоставления.")
    identity, _ = OneCCustomerIdentity.objects.update_or_create(
        organization=organization,
        onec_customer_id=document["customer_guid"],
        defaults={
            "raw_name": document["customer"],
            "client": client,
            "confirmed_by": user,
            "confirmed_at": timezone.now(),
        },
    )
    return identity


def map_order_object(organization, user, period_month, *, document_key, pool):
    """Link one stable order scope to a Service2 object without guessing by text."""
    if not can_manage_participation(user, organization):
        raise PermissionDenied
    if pool.organization_id != organization.id:
        raise ValidationError("Объект относится к другой организации.")
    options = {item["scope_key"]: item for item in reward_document_options(organization, period_month)}
    document = options.get(document_key)
    if not document:
        raise ValidationError("Заказ не относится к активным подтверждённым данным месяца.")
    if not document.get("client_id"):
        raise ValidationError("Сначала сопоставьте контрагента 1С с клиентом Service2.")
    if pool.client_id != document["client_id"]:
        raise ValidationError("Объект относится к другому клиенту.")
    link, _ = RewardOrderObjectLink.objects.update_or_create(
        organization=organization,
        source_document_key=document_key,
        defaults={
            "client_id": document["client_id"],
            "pool": pool,
            "linked_by": user,
        },
    )
    link.full_clean()
    return link


@transaction.atomic
def sync_reward_rules(organization, user, period_month):
    """Apply only deterministic client-manager rules to reliably linked open orders."""
    if not can_manage_participation(user, organization):
        raise PermissionDenied
    period_month = month_start(period_month)
    _lock_reward_organization(organization)
    if RewardMonthClose.objects.filter(
        organization=organization, period_month=period_month
    ).exists():
        return 0

    documents = reward_document_options(organization, period_month)
    templates = list(
        RewardParticipantTemplate.objects.filter(
            organization=organization,
            role=RewardParticipantTemplate.ROLE_CLIENT_MANAGER,
            effective_from__lte=period_month,
        )
        .filter(models_q_effective(period_month))
        .select_related("client", "pool", "employee")
    )
    created = 0
    for document in documents:
        client_id = document.get("client_id")
        if not client_id:
            continue
        pool_id = document.get("pool_id")
        object_rules = [
            item for item in templates
            if item.client_id == client_id and item.pool_id == pool_id and pool_id
        ]
        client_rules = [
            item for item in templates
            if item.client_id == client_id and item.pool_id is None
        ]
        applicable = object_rules or client_rules
        if not applicable:
            continue

        manual_exists = RewardParticipation.objects.filter(
            organization=organization,
            period_month=period_month,
            source_document_key=document["scope_key"],
            role=RewardParticipation.ROLE_CLIENT_MANAGER,
        ).exclude(
            status=RewardParticipation.STATUS_NOT_APPLICABLE
        ).exclude(
            assignment_source=RewardParticipation.SOURCE_TEMPLATE
        ).exists()
        if manual_exists:
            continue

        desired_ids = {item.employee_id for item in applicable if item.employee_id}
        stale = RewardParticipation.objects.filter(
            organization=organization,
            period_month=period_month,
            source_document_key=document["scope_key"],
            role=RewardParticipation.ROLE_CLIENT_MANAGER,
            assignment_source=RewardParticipation.SOURCE_TEMPLATE,
        ).exclude(status=RewardParticipation.STATUS_NOT_APPLICABLE)
        for item in stale:
            if item.employee_id not in desired_ids:
                before = participation_snapshot(item)
                item.status = RewardParticipation.STATUS_NOT_APPLICABLE
                item.basis = "Закрепление клиента/объекта изменено."
                item.save(update_fields=["status", "basis", "updated_at"])
                RewardParticipationChange.objects.create(
                    participation=item, actor=user, before=before,
                    after=participation_snapshot(item),
                    reason="Автоматическое закрепление изменено",
                )

        scope_key = _assignment_scope_key(
            document["scope_key"], RewardParticipation.ROLE_CLIENT_MANAGER, []
        )
        existing_share = (
            RewardParticipation.objects.filter(
                organization=organization,
                period_month=period_month,
                scope_key=scope_key,
                role=RewardParticipation.ROLE_CLIENT_MANAGER,
                assignment_source=RewardParticipation.SOURCE_TEMPLATE,
                status=RewardParticipation.STATUS_CONFIRMED,
            ).aggregate(total=models.Sum("share"))["total"] or Decimal("0")
        )
        for template in applicable:
            if not template.employee_id:
                continue
            if RewardParticipation.objects.filter(
                organization=organization,
                period_month=period_month,
                scope_key=scope_key,
                role=RewardParticipation.ROLE_CLIENT_MANAGER,
                employee_id=template.employee_id,
                assignment_source=RewardParticipation.SOURCE_TEMPLATE,
                status=RewardParticipation.STATUS_CONFIRMED,
            ).exists():
                continue
            if existing_share + template.share > ONE:
                raise ValidationError(
                    f"Закрепления менеджера клиента для «{document['customer']}» превышают 100%."
                )
            item = RewardParticipation.objects.create(
                organization=organization,
                employee_id=template.employee_id,
                role=RewardParticipation.ROLE_CLIENT_MANAGER,
                status=RewardParticipation.STATUS_CONFIRMED,
                share=template.share,
                period_month=period_month,
                scope_key=scope_key,
                source_document_key=document["scope_key"],
                source_document_type=document["source_document_type"],
                source_document_guid=document["source_document_guid"],
                source_document_number=document["source_document_number"],
                source_document_date=document["source_document_date"],
                scope_line_identities=[],
                customer_name=document["customer"],
                object_label=document.get("pool_label") or "",
                basis=(
                    "Автоматическое закрепление объекта"
                    if template.pool_id else "Автоматическое закрепление клиента"
                ),
                assignment_source=RewardParticipation.SOURCE_TEMPLATE,
                created_by=user,
                confirmed_by=user,
                confirmed_at=timezone.now(),
            )
            RewardParticipationChange.objects.create(
                participation=item, actor=user, before={},
                after=participation_snapshot(item),
                reason=item.basis,
            )
            existing_share += template.share
            created += 1
    return created


def reward_order_workspace(organization, period_month, *, employee_id=None):
    """Presentation-only order workspace; does not reimplement GP/reward formulas."""
    data = calculate_month(organization, period_month, employee_id=employee_id)
    documents = reward_document_options(organization, period_month)
    participations = list(
        RewardParticipation.objects.filter(
            organization=organization, period_month=month_start(period_month)
        ).select_related("employee", "author_identity")
    )
    details_by_scope = defaultdict(list)
    for detail in data.get("details", []):
        details_by_scope[detail.get("scope_key")].append(detail)

    technical_by_scope = defaultdict(list)
    human_issue = {
        "missing_sale_role": "Не указано, кто продал",
        "missing_work_role": "Есть работы, но не указаны исполнители",
        "missing_documentation_role": "Не определён оформитель",
        "unallocated": "Доли распределены не полностью",
        "share_overflow": "Доли превышают 100%",
        "missing_cost": "Отсутствует себестоимость",
        "month_missing_cost": "Есть позиции без себестоимости",
        "missing_selected_lines": "Изменились строки заказа после назначения",
        "moved_selected_lines": "Выбранные позиции перенесены в другой заказ",
        "moved_all_lines_scope": "Состав заказа изменился после назначения",
        "partial_direct_cost_allocation": "Прямые затраты нельзя точно отнести к выбранной части заказа",
        "unconfirmed": "Автоматическое предложение требует решения",
        "author_sync_stale": "Данные об оформителе требуют обновления",
        "unmapped_author": "Не определён оформитель",
    }
    for issue in data.get("issues", []):
        technical_by_scope[issue.get("label", "")].append(
            human_issue.get(issue.get("kind"), "")
        )

    role_order = [
        RewardParticipation.ROLE_CLIENT_MANAGER,
        RewardParticipation.ROLE_SALE,
        RewardParticipation.ROLE_DOCUMENTATION,
        RewardParticipation.ROLE_PROJECT,
        RewardParticipation.ROLE_WORK,
    ]
    role_labels = dict(RewardParticipation.ROLE_CHOICES)
    scheme = data.get("scheme") or {}
    rate_labels = {
        RewardParticipation.ROLE_CLIENT_MANAGER: f"{Decimal(scheme.get('client_manager_rate') or 0) * 100:.2f}%",
        RewardParticipation.ROLE_SALE: f"{Decimal(scheme.get('sale_rate') or 0) * 100:.2f}%",
        RewardParticipation.ROLE_PROJECT: f"{Decimal(scheme.get('project_rate') or 0) * 100:.2f}%",
        RewardParticipation.ROLE_WORK: f"{Decimal(scheme.get('work_rate') or 0) * 100:.2f}%",
        RewardParticipation.ROLE_DOCUMENTATION: "фиксированная сумма",
    }

    orders = []
    attention = []
    for document in documents:
        keys = {document["scope_key"], *document.get("source_document_keys", [])}
        items = [item for item in participations if item.source_document_key in keys]
        role_rows = []
        problems = []

        active_sale = [x for x in items if x.role == RewardParticipation.ROLE_SALE and x.status != RewardParticipation.STATUS_NOT_APPLICABLE]
        if not active_sale:
            problems.append("Не указано, кто продал")
        active_work = [x for x in items if x.role == RewardParticipation.ROLE_WORK and x.status != RewardParticipation.STATUS_NOT_APPLICABLE]
        if document["has_work"] and not active_work:
            problems.append("Есть работы, но не указаны исполнители")
        doc_items = [x for x in items if x.role == RewardParticipation.ROLE_DOCUMENTATION and x.status != RewardParticipation.STATUS_NOT_APPLICABLE]
        if any(x.status == RewardParticipation.STATUS_REQUIRED or not x.employee_id for x in doc_items):
            problems.append("Не определён оформитель")
        if document["cost_missing"]:
            problems.append("Отсутствует себестоимость")

        for role in role_order:
            role_items = [
                item for item in items
                if item.role == role
                and item.status != RewardParticipation.STATUS_NOT_APPLICABLE
            ]
            role_scope_groups = defaultdict(list)
            for item in role_items:
                role_scope_groups[item.scope_key].append(item)
            for scope_key, scoped_items in sorted(role_scope_groups.items()):
                share_total = sum((item.share for item in scoped_items), Decimal("0"))
                if share_total < ONE and role in {
                    RewardParticipation.ROLE_SALE,
                    RewardParticipation.ROLE_DOCUMENTATION,
                    RewardParticipation.ROLE_WORK,
                }:
                    problems.append(
                        f"{role_labels[role]}: распределено {(share_total * 100):.0f} из 100%"
                    )
                if share_total > ONE:
                    problems.append(f"{role_labels[role]}: доли превышают 100%")
                amount_by_employee = defaultdict(Decimal)
                base = None
                fund = None
                calculated_rate = None
                for detail in details_by_scope.get(scope_key, []):
                    amount_by_employee[detail["employee_id"]] += Decimal(detail["amount"])
                    if base is None:
                        base = Decimal(detail["base"])
                    if fund is None and detail.get("role_fund") is not None:
                        fund = Decimal(detail["role_fund"])
                    if calculated_rate is None:
                        calculated_rate = detail.get("rate")
                role_rows.append({
                    "role": role,
                    "label": role_labels[role],
                    "scope_key": scope_key,
                    "rate": calculated_rate or rate_labels[role],
                    "fund": None if fund is None else str(money(fund)),
                    "share_total": str(share_total),
                    "remaining_share": str(max(Decimal("0"), ONE - share_total)),
                    "base": None if base is None else str(money(base)),
                    "participants": [{
                        "id": item.id,
                        "employee": item.employee.display_name if item.employee_id else "Требует сопоставления",
                        "share": str(item.share),
                        "status": item.status,
                        "assignment_source": item.assignment_source,
                        "author_identity_id": item.author_identity_id,
                        "amount": str(money(amount_by_employee.get(item.employee_id, Decimal("0")))),
                        "scope_lines": list(item.scope_line_identities or []),
                    } for item in scoped_items],
                })

        for item in items:
            for message in technical_by_scope.get(item.scope_key, []):
                if message:
                    problems.append(message)
        problems = list(dict.fromkeys(problems))
        order = {
            **document,
            "roles": role_rows,
            "problems": problems,
            "status_ok": not problems,
        }
        orders.append(order)
        if problems:
            attention.append(order)

    return {
        "data": data,
        "orders": orders,
        "attention": attention,
    }
