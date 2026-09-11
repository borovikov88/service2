"""Explicit write path for human cash-flow classifications.

The canonical management-finance module stays read-only.  This module is the
small, guarded write boundary used by the management screen.  It never changes
cash-flow facts, import batches or active versions.
"""

from django.core.exceptions import ValidationError
from django.db import transaction

from pool_service.finance_imports.employee_matching import normalize_onec_name
from pool_service.models import CashFlowArticleMapping, DataAuditLog


def canonical_article_key(article_name):
    """Return the same canonical key the 1C cash-flow importers use."""
    return normalize_onec_name(article_name)


def mapping_snapshot(mapping):
    """A compact audit snapshot without financial source amounts."""
    if mapping is None:
        return {}
    return {
        "article_name": mapping.article_name,
        "normalized_article_name": mapping.normalized_article_name,
        "management_category": mapping.management_category,
        "flow_type": mapping.flow_type,
        "classification_status": mapping.classification_status,
        "is_internal_turnover": mapping.is_internal_turnover,
        "include_in_external_cashflow": mapping.include_in_external_cashflow,
        "is_dividend": mapping.is_dividend,
        "comment": mapping.comment,
        "updated_by_id": mapping.updated_by_id,
    }


def save_explicit_cashflow_mapping(
    *,
    organization,
    article_name,
    expected_normalized_article_name,
    values,
    user,
):
    """Create or update one organization-bound mapping after an explicit POST.

    Callers must resolve ``article_name`` from the canonical read-only service
    first.  The expected active key closes the gap between a user-controlled
    POST field and the active confirmed 1C article it represents.
    """
    canonical_key = canonical_article_key(article_name)
    expected_key = canonical_article_key(expected_normalized_article_name)
    if not canonical_key or canonical_key != expected_key:
        raise ValidationError(
            "Статья активной версии имеет некорректный ключ; mapping не изменён."
        )

    flow_type = values["flow_type"]
    if flow_type not in {
        CashFlowArticleMapping.FLOW_OPERATING,
        CashFlowArticleMapping.FLOW_INVESTING,
        CashFlowArticleMapping.FLOW_FINANCING,
        CashFlowArticleMapping.FLOW_LIQUIDITY,
        CashFlowArticleMapping.FLOW_INTERNAL,
        CashFlowArticleMapping.FLOW_UNCLASSIFIED,
    }:
        raise ValidationError("Укажите поддерживаемый тип потока.")
    classification_status = values["classification_status"]
    if classification_status not in {
        CashFlowArticleMapping.CLASS_CONFIRMED,
        CashFlowArticleMapping.CLASS_NEEDS_REVIEW,
        CashFlowArticleMapping.CLASS_UNCLASSIFIED,
    }:
        raise ValidationError("Укажите поддерживаемый статус классификации.")
    is_dividend = values.get("is_dividend", False)
    if not isinstance(is_dividend, bool):
        raise ValidationError("Признак дивидендов должен иметь булево значение.")
    if is_dividend and (
        flow_type != CashFlowArticleMapping.FLOW_FINANCING
        or classification_status != CashFlowArticleMapping.CLASS_CONFIRMED
    ):
        raise ValidationError(
            "Признак дивидендов допустим только для подтверждённого финансового потока."
        )

    # The form does not accept arbitrary allocation flags.  Internal turnover
    # is always internal and never external; every other explicit type remains
    # external until a separately designed policy says otherwise.
    is_internal = flow_type == CashFlowArticleMapping.FLOW_INTERNAL
    include_external = not is_internal

    with transaction.atomic():
        mapping = CashFlowArticleMapping.objects.select_for_update().filter(
            organization=organization,
            normalized_article_name=canonical_key,
        ).first()
        before = mapping_snapshot(mapping)
        created = mapping is None
        if mapping is None:
            mapping = CashFlowArticleMapping(
                organization=organization,
                normalized_article_name=canonical_key,
            )

        mapping.article_name = article_name
        mapping.normalized_article_name = canonical_key
        mapping.management_category = values["management_category"]
        mapping.flow_type = flow_type
        mapping.classification_status = classification_status
        mapping.is_internal_turnover = is_internal
        mapping.include_in_external_cashflow = include_external
        mapping.is_dividend = is_dividend
        mapping.comment = values["comment"]
        mapping.updated_by = user
        # Do not call full_clean(): `liquidity` is intentionally supported by
        # the management read-model before it is added to legacy field choices.
        mapping.save()

        after = mapping_snapshot(mapping)
        DataAuditLog.objects.create(
            entity_type="CashFlowArticleMapping",
            entity_id=str(mapping.pk),
            action=(
                DataAuditLog.ACTION_CREATE if created else DataAuditLog.ACTION_UPDATE
            ),
            organization=organization,
            actor=user,
            before=before,
            after=after,
            changed_fields=sorted(
                key for key in set(before) | set(after)
                if before.get(key) != after.get(key)
            ),
        )
    return mapping, created
