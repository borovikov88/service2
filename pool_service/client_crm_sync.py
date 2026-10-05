from __future__ import annotations

from collections import defaultdict
from datetime import timedelta
import logging

from django.db import transaction
from django.utils import timezone

from .client_crm_import import (
    BUYER_ENTITY,
    PAGE_SIZE,
    SYSTEM_NAMES,
    _candidate_contacts,
    _contact_rows,
    _effective_kind,
    _match_clients,
    _parse_date,
    _source_kind,
    _target_organization,
    apply_candidate,
    apply_ready_candidates,
    scan_onec_clients,
)
from .client_crm_models import ClientImportCandidate
from .onec_diagnostic import config_from_settings, fetch_metadata
from .onec_diagnostic_universal import query_1c_rows


logger = logging.getLogger(__name__)

RECENT_LOOKBACK_HOURS = 48


def _buyer_fields():
    return [
        "Ref_Key",
        "Code",
        "Description",
        "НаименованиеПолное",
        "ЮридическоеФизическоеЛицо",
        "ВидКонтрагента",
        "ИНН",
        "КПП",
        "ФИО",
        "ДатаРождения",
        "ДатаСоздания",
        "НомерТелефонаДляПоиска",
        "АдресЭПДляПоиска",
        "Покупатель",
        "Недействителен",
    ]


def _recent_buyer_rows(config, metadata_raw, *, lookback_hours):
    # 1C stores this field as Edm.DateTime without an offset.  A 48-hour
    # overlap deliberately makes the comparison tolerant to host/1C timezone
    # differences while keeping the query small.
    cutoff = (timezone.now() - timedelta(hours=lookback_hours)).replace(tzinfo=None)
    result = query_1c_rows(
        config,
        BUYER_ENTITY,
        fields=_buyer_fields(),
        filters=[
            {"field": "DeletionMark", "op": "eq", "value": False},
            {"field": "Покупатель", "op": "eq", "value": True},
            {"field": "ДатаСоздания", "op": "ge", "value": cutoff},
        ],
        limit=PAGE_SIZE,
        include_deleted=False,
        include_inactive=False,
        order_by=[
            {"field": "ДатаСоздания", "direction": "asc"},
            {"field": "Code", "direction": "asc"},
        ],
        metadata_raw=metadata_raw,
    )
    rows = list(result.get("rows", []))
    if not result.get("complete") and len(rows) >= PAGE_SIZE:
        raise RuntimeError(
            "Recent 1C client window returned more than the safe incremental limit"
        )
    return rows


def _stage_recent_candidate(organization, row, extra_contacts):
    ref = str(row.get("Ref_Key") or "")
    name = str(row.get("Description") or row.get("НаименованиеПолное") or "").strip()
    source_kind = _source_kind(row)

    existing = ClientImportCandidate.objects.filter(
        organization=organization,
        source_ref=ref,
    ).only("resolution", "applied_at").first()
    resolution = (
        existing.resolution
        if existing is not None
        else ClientImportCandidate.RESOLUTION_AUTO
    )
    effective_kind = _effective_kind(source_kind, resolution)

    inn = str(row.get("ИНН") or "").strip()
    phones, emails = _candidate_contacts(row, extra_contacts)

    if (
        resolution != ClientImportCandidate.RESOLUTION_AUTO
        and not (existing and existing.applied_at)
    ):
        matches, match_reason = [], ""
    else:
        matches, match_reason = _match_clients(
            organization,
            effective_kind,
            inn,
            phones,
            ref,
        )

    status = ClientImportCandidate.STATUS_READY
    reason = ""
    matched_client = None
    if not ref or not name:
        status = ClientImportCandidate.STATUS_INVALID
        reason = "Нет идентификатора или имени в 1С"
    elif name.casefold().strip() in SYSTEM_NAMES:
        status = ClientImportCandidate.STATUS_INVALID
        reason = "Системная карточка 1С"
    elif len(matches) > 1:
        status = ClientImportCandidate.STATUS_DUPLICATE
        reason = f"Несколько карточек Service2 совпали по: {match_reason}"
    elif len(matches) == 1:
        matched_client = matches[0]
    elif (
        effective_kind == ClientImportCandidate.KIND_LEGAL
        and not inn
        and resolution == ClientImportCandidate.RESOLUTION_AUTO
    ):
        status = ClientImportCandidate.STATUS_REVIEW
        reason = "У юридического лица не заполнен ИНН"

    if resolution == ClientImportCandidate.RESOLUTION_SKIP:
        status = ClientImportCandidate.STATUS_SKIPPED
        reason = "Не импортировать — решение пользователя"
    elif resolution != ClientImportCandidate.RESOLUTION_AUTO and status not in {
        ClientImportCandidate.STATUS_INVALID,
        ClientImportCandidate.STATUS_DUPLICATE,
    }:
        status = ClientImportCandidate.STATUS_READY
        reason = "Тип подтверждён пользователем"

    payload = {
        "phones": phones,
        "emails": emails,
        "onec_type": str(row.get("ВидКонтрагента") or ""),
        "onec_person_kind": str(row.get("ЮридическоеФизическоеЛицо") or ""),
        "fio": str(row.get("ФИО") or "").strip(),
        "onec_created_at": str(row.get("ДатаСоздания") or ""),
    }

    candidate, _ = ClientImportCandidate.objects.update_or_create(
        organization=organization,
        source_ref=ref,
        defaults={
            "source_code": str(row.get("Code") or ""),
            "source_kind": source_kind,
            "name": name,
            "legal_name": str(row.get("НаименованиеПолное") or "").strip(),
            "inn": inn,
            "kpp": str(row.get("КПП") or "").strip(),
            "phone": phones[0]["value"] if phones else "",
            "email": emails[0]["value"] if emails else "",
            "birth_date": _parse_date(row.get("ДатаРождения")),
            "payload": payload,
            "status": status,
            "reason": reason,
            "matched_client": matched_client,
        },
    )

    if candidate.applied_at:
        candidate.status = ClientImportCandidate.STATUS_IMPORTED
        candidate.save(update_fields=["status", "updated_at"])

    return candidate


def sync_recent_onec_clients(*, lookback_hours=RECENT_LOOKBACK_HOURS):
    organization = _target_organization()
    config = config_from_settings()
    metadata_raw = fetch_metadata(config)

    rows = _recent_buyer_rows(
        config,
        metadata_raw,
        lookback_hours=lookback_hours,
    )
    contacts_by_ref = _contact_rows(
        config,
        [row.get("Ref_Key") for row in rows],
        metadata_raw,
    )

    result = defaultdict(int)
    for row in rows:
        ref = str(row.get("Ref_Key") or "")
        candidate = _stage_recent_candidate(
            organization,
            row,
            contacts_by_ref.get(ref, []),
        )
        result["seen"] += 1

        if candidate.status == ClientImportCandidate.STATUS_READY:
            try:
                apply_candidate(candidate)
            except (ValueError, RuntimeError):
                logger.exception(
                    "Automatic 1C client import requires review for candidate %s",
                    candidate.pk,
                )
                candidate.status = ClientImportCandidate.STATUS_REVIEW
                candidate.reason = (
                    "Автоматический импорт не смог безопасно сопоставить карточку"
                )
                candidate.save(
                    update_fields=["status", "reason", "updated_at"]
                )
                result["review"] += 1
            else:
                result["imported"] += 1
        elif (
            candidate.status == ClientImportCandidate.STATUS_IMPORTED
            and candidate.matched_client_id
        ):
            try:
                apply_candidate(candidate)
            except (ValueError, RuntimeError):
                logger.exception(
                    "Automatic refresh failed for imported 1C client candidate %s",
                    candidate.pk,
                )
                result["refresh_failed"] += 1
            else:
                result["refreshed"] += 1
        elif candidate.status in {
            ClientImportCandidate.STATUS_REVIEW,
            ClientImportCandidate.STATUS_DUPLICATE,
        }:
            result["review"] += 1
        elif candidate.status in {
            ClientImportCandidate.STATUS_INVALID,
            ClientImportCandidate.STATUS_SKIPPED,
        }:
            result["skipped"] += 1

    return dict(result)


def sync_all_onec_clients():
    organization = _target_organization()
    scan_result = scan_onec_clients()
    apply_result = apply_ready_candidates(organization)

    refreshed = 0
    refresh_failed = 0
    imported_ids = list(
        ClientImportCandidate.objects.filter(
            organization=organization,
            status=ClientImportCandidate.STATUS_IMPORTED,
            matched_client__isnull=False,
        )
        .order_by("id")
        .values_list("id", flat=True)
    )
    for candidate_id in imported_ids:
        candidate = ClientImportCandidate.objects.get(pk=candidate_id)
        try:
            apply_candidate(candidate)
        except (ValueError, RuntimeError):
            logger.exception(
                "Nightly refresh failed for imported 1C client candidate %s",
                candidate_id,
            )
            refresh_failed += 1
        else:
            refreshed += 1

    return {
        "scan": scan_result,
        "apply": apply_result,
        "refreshed": refreshed,
        "refresh_failed": refresh_failed,
    }
