from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, timedelta
import logging
import os
import re
import shutil
import subprocess
import sys
import threading

from django.conf import settings
from django.db import models, transaction
from django.utils import timezone

from .models import Client, Organization
from .client_crm_models import (
    ClientCRMProfile,
    ClientCompanyLink,
    ClientContact,
    ClientImportCandidate,
    ClientImportRun,
)
from .onec_diagnostic import config_from_settings, fetch_metadata
from .onec_diagnostic_universal import query_1c_rows


BUYER_ENTITY = "Catalog_Контрагенты"
CONTACT_ENTITY = "Catalog_Контрагенты_КонтактнаяИнформация"
PAGE_SIZE = 500
CONTACT_BATCH_SIZE = 40
SYSTEM_NAMES = {"розничный покупатель"}
IMPORT_RUN_STALE_MINUTES = 120

logger = logging.getLogger(__name__)


def normalize_phone(value: str | None) -> str:
    digits = "".join(ch for ch in str(value or "") if ch.isdigit())
    if len(digits) == 11 and digits[0] in {"7", "8"}:
        digits = "7" + digits[1:]
    elif len(digits) == 10:
        digits = "7" + digits
    return digits if 10 <= len(digits) <= 15 else ""


def normalize_email(value: str | None) -> str:
    return str(value or "").strip().lower()


def _parse_date(value):
    if not value or str(value).startswith("0001-01-01"):
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).date()
    except (TypeError, ValueError):
        try:
            return date.fromisoformat(str(value)[:10])
        except (TypeError, ValueError):
            return None


def _target_organization() -> Organization:
    raw = getattr(settings, "ONEC_ODATA_TARGET_ORGANIZATION_ID", "")
    try:
        organization_id = int(raw)
    except (TypeError, ValueError) as exc:
        raise RuntimeError("ONEC_ODATA_TARGET_ORGANIZATION_ID is not configured") from exc
    return Organization.objects.get(pk=organization_id)


def _buyer_rows(config, metadata_raw):
    cursor = ""
    while True:
        filters = [
            {"field": "DeletionMark", "op": "eq", "value": False},
            {"field": "Покупатель", "op": "eq", "value": True},
        ]
        if cursor:
            filters.append({"field": "Code", "op": "gt", "value": cursor})
        result = query_1c_rows(
            config,
            BUYER_ENTITY,
            fields=[
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
                "НомерТелефонаДляПоиска",
                "АдресЭПДляПоиска",
                "Покупатель",
                "Недействителен",
            ],
            filters=filters,
            limit=PAGE_SIZE,
            include_deleted=False,
            include_inactive=False,
            order_by=[{"field": "Code", "direction": "asc"}],
            metadata_raw=metadata_raw,
        )
        rows = result.get("rows", [])
        if not rows:
            break
        for row in rows:
            yield row
        if result.get("complete") or len(rows) < PAGE_SIZE:
            break
        next_cursor = str(rows[-1].get("Code") or "")
        if not next_cursor or next_cursor == cursor:
            raise RuntimeError("1C buyer pagination did not advance")
        cursor = next_cursor


def _contact_rows(config, refs, metadata_raw):
    contact_map = defaultdict(list)
    refs = [ref for ref in refs if ref]
    for start in range(0, len(refs), CONTACT_BATCH_SIZE):
        batch = refs[start : start + CONTACT_BATCH_SIZE]
        result = query_1c_rows(
            config,
            CONTACT_ENTITY,
            fields=[
                "Ref_Key",
                "LineNumber",
                "Тип",
                "Представление",
                "НомерТелефона",
                "НомерТелефонаБезКодов",
                "АдресЭП",
            ],
            filters=[{"field": "Ref_Key", "op": "in", "value": batch}],
            limit=500,
            include_deleted=False,
            include_inactive=False,
            order_by=[
                {"field": "Ref_Key", "direction": "asc"},
                {"field": "LineNumber", "direction": "asc"},
            ],
            metadata_raw=metadata_raw,
        )
        for row in result.get("rows", []):
            contact_map[str(row.get("Ref_Key") or "")].append(row)
    return contact_map


def _source_kind(row):
    kind = str(row.get("ВидКонтрагента") or "")
    if kind == "ИндивидуальныйПредприниматель":
        return ClientImportCandidate.KIND_IP
    if kind == "ЮридическоеЛицо":
        return ClientImportCandidate.KIND_LEGAL
    return ClientImportCandidate.KIND_PRIVATE


def _effective_kind(source_kind, resolution):
    if resolution == ClientImportCandidate.RESOLUTION_LEGAL:
        return ClientImportCandidate.KIND_LEGAL
    if resolution == ClientImportCandidate.RESOLUTION_PRIVATE:
        return ClientImportCandidate.KIND_PRIVATE
    if resolution == ClientImportCandidate.RESOLUTION_IP:
        return ClientImportCandidate.KIND_IP
    return source_kind


def _refresh_latest_run_counts(organization):
    counts = {
        item["status"]: item["count"]
        for item in ClientImportCandidate.objects.filter(
            organization=organization
        ).values("status").annotate(count=models.Count("id"))
    }
    latest = (
        ClientImportRun.objects.filter(
            organization=organization,
            status=ClientImportRun.STATUS_SUCCESS,
        )
        .order_by("-requested_at", "-id")
        .first()
    )
    if latest:
        latest.ready_count = counts.get(ClientImportCandidate.STATUS_READY, 0)
        latest.review_count = counts.get(ClientImportCandidate.STATUS_REVIEW, 0)
        latest.duplicate_count = counts.get(ClientImportCandidate.STATUS_DUPLICATE, 0)
        latest.invalid_count = (
            counts.get(ClientImportCandidate.STATUS_INVALID, 0)
            + counts.get(ClientImportCandidate.STATUS_SKIPPED, 0)
        )
        latest.imported_count = counts.get(ClientImportCandidate.STATUS_IMPORTED, 0)
        latest.save(
            update_fields=[
                "ready_count",
                "review_count",
                "duplicate_count",
                "invalid_count",
                "imported_count",
                "updated_at",
            ]
        )


def _candidate_contacts(row, extra_rows):
    phones = []
    emails = []

    def add_phone(raw, label=""):
        value = str(raw or "").strip()
        normalized = normalize_phone(value)
        if value and normalized and all(item["match"] != normalized for item in phones):
            phones.append({"value": value, "match": normalized, "label": label})

    def add_email(raw, label=""):
        value = str(raw or "").strip()
        normalized = normalize_email(value)
        if value and normalized and all(item["match"] != normalized for item in emails):
            emails.append({"value": value, "match": normalized, "label": label})

    add_phone(row.get("НомерТелефонаДляПоиска"), "Основной")
    add_email(row.get("АдресЭПДляПоиска"), "Основной")
    for contact in extra_rows:
        label = str(contact.get("Представление") or "")
        kind = str(contact.get("Тип") or "")
        if "Телефон" in kind:
            add_phone(contact.get("НомерТелефона") or contact.get("Представление"), label)
        elif "Почт" in kind:
            add_email(contact.get("АдресЭП") or contact.get("Представление"), label)
    return phones, emails


def _match_clients(organization, source_kind, inn, phones, source_ref=""):
    matches = Client.objects.filter(organization=organization).filter(
        models.Q(crm_profile__isnull=True)
        | models.Q(crm_profile__merged_into__isnull=True)
    )
    if source_ref:
        profile = ClientCRMProfile.objects.filter(onec_ref=source_ref).select_related("client").first()
        if profile and profile.client.organization_id == organization.id:
            return [profile.client], "Ref_Key 1С"

    expected_client_type = (
        "private"
        if source_kind == ClientImportCandidate.KIND_PRIVATE
        else "legal"
    )
    typed_matches = matches.filter(client_type=expected_client_type)

    if source_kind in {ClientImportCandidate.KIND_LEGAL, ClientImportCandidate.KIND_IP} and inn:
        by_inn = list(typed_matches.filter(inn=inn)[:3])
        if by_inn:
            return by_inn, "ИНН"
    phone_values = {item["match"] for item in phones if item.get("match")}
    if phone_values:
        contact_ids = ClientContact.objects.filter(
            kind=ClientContact.KIND_PHONE,
            match_value__in=phone_values,
            client__organization=organization,
            client__client_type=expected_client_type,
        ).values_list("client_id", flat=True)
        legacy_ids = []
        for client in typed_matches.exclude(phone__isnull=True).exclude(phone="").only("id", "phone"):
            if normalize_phone(client.phone) in phone_values:
                legacy_ids.append(client.id)
        ids = set(contact_ids) | set(legacy_ids)
        if ids:
            return list(typed_matches.filter(id__in=ids)[:4]), "телефон"
    return [], ""


def _update_run_progress(run, *, processed_rows, totals):
    if run is None:
        return
    ClientImportRun.objects.filter(pk=run.pk).update(
        processed_rows=processed_rows,
        ready_count=totals.get(ClientImportCandidate.STATUS_READY, 0),
        review_count=totals.get(ClientImportCandidate.STATUS_REVIEW, 0),
        duplicate_count=totals.get(ClientImportCandidate.STATUS_DUPLICATE, 0),
        invalid_count=(
            totals.get(ClientImportCandidate.STATUS_INVALID, 0)
            + totals.get(ClientImportCandidate.STATUS_SKIPPED, 0)
        ),
        imported_count=totals.get(ClientImportCandidate.STATUS_IMPORTED, 0),
        updated_at=timezone.now(),
    )


def scan_onec_clients(run=None):
    organization = _target_organization()
    config = config_from_settings()

    # Metadata is large and relatively expensive. Reuse one snapshot for the
    # entire scan instead of downloading it again for every OData batch.
    metadata_raw = fetch_metadata(config)
    rows = list(_buyer_rows(config, metadata_raw))
    if run is not None:
        ClientImportRun.objects.filter(pk=run.pk).update(
            total_rows=len(rows),
            updated_at=timezone.now(),
        )

    contacts_by_ref = _contact_rows(
        config,
        [row.get("Ref_Key") for row in rows],
        metadata_raw,
    )

    totals = defaultdict(int)
    processed_rows = 0
    for row in rows:
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
        phones, emails = _candidate_contacts(row, contacts_by_ref.get(ref, []))
        if (
            resolution != ClientImportCandidate.RESOLUTION_AUTO
            and not (existing and existing.applied_at)
        ):
            # Manual classification means "create/import this canonical 1C
            # client as classified". Legacy Service2 cards are merged later
            # through the explicit merge workspace, never guessed here.
            matches, match_reason = [], ""
        else:
            matches, match_reason = _match_clients(
                organization, effective_kind, inn, phones, ref
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

        totals[candidate.status] += 1
        totals[source_kind] += 1
        processed_rows += 1
        if processed_rows % 25 == 0 or processed_rows == len(rows):
            _update_run_progress(
                run,
                processed_rows=processed_rows,
                totals=totals,
            )

    totals["total"] = len(rows)
    return dict(totals)


def _worker_python_executable(base_dir):
    production_python = os.path.join(
        os.path.dirname(base_dir),
        "venv",
        "bin",
        "python",
    )
    if os.path.isfile(production_python) and os.access(production_python, os.X_OK):
        return production_python
    return sys.executable


def _reap_client_import_worker(process, run_id):
    try:
        return_code = process.wait()
        if return_code:
            logger.error(
                "1C client import worker exited with code %s for run_id=%s",
                return_code,
                run_id,
            )
            ClientImportRun.objects.filter(
                pk=run_id,
                status__in=[
                    ClientImportRun.STATUS_PENDING,
                    ClientImportRun.STATUS_RUNNING,
                ],
            ).update(
                status=ClientImportRun.STATUS_FAILED,
                error="Фоновый процесс импорта завершился с ошибкой",
                finished_at=timezone.now(),
                updated_at=timezone.now(),
            )
    except Exception:
        logger.exception("Failed while reaping 1C client import worker")


def start_client_import_worker(run_id):
    base_dir = str(settings.BASE_DIR)
    worker_script = os.path.join(base_dir, "scripts", "run_client_import_worker.sh")
    bash = shutil.which("bash")
    if not bash or not os.path.isfile(worker_script):
        logger.error("1C client import worker launcher is unavailable")
        return False

    env = os.environ.copy()
    env["SERVICE2_PYTHON"] = _worker_python_executable(base_dir)
    try:
        process = subprocess.Popen(
            [bash, worker_script, str(run_id)],
            cwd=base_dir,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
            close_fds=True,
        )
    except OSError:
        logger.exception("Failed to start 1C client import worker")
        return False

    threading.Thread(
        target=_reap_client_import_worker,
        args=(process, run_id),
        daemon=True,
        name="service2-client-import-reaper",
    ).start()
    return True


def request_client_import_scan(requested_by=None):
    organization = _target_organization()
    stale_before = timezone.now() - timedelta(minutes=IMPORT_RUN_STALE_MINUTES)

    with transaction.atomic():
        # Lock the organization row so simultaneous clicks from different
        # browsers cannot create two active imports.
        organization = Organization.objects.select_for_update().get(pk=organization.pk)
        ClientImportRun.objects.filter(
            organization=organization,
            status__in=[
                ClientImportRun.STATUS_PENDING,
                ClientImportRun.STATUS_RUNNING,
                ClientImportRun.STATUS_APPLYING,
            ],
            updated_at__lt=stale_before,
        ).update(
            status=ClientImportRun.STATUS_FAILED,
            error="Предыдущий импорт не завершился и был помечен как зависший",
            finished_at=timezone.now(),
            updated_at=timezone.now(),
        )

        active = (
            ClientImportRun.objects.select_for_update()
            .filter(
                organization=organization,
                status__in=[
                    ClientImportRun.STATUS_PENDING,
                    ClientImportRun.STATUS_RUNNING,
                    ClientImportRun.STATUS_APPLYING,
                ],
            )
            .order_by("-requested_at", "-id")
            .first()
        )
        if active is not None:
            return active, False

        run = ClientImportRun.objects.create(
            organization=organization,
            requested_by=requested_by,
            status=ClientImportRun.STATUS_PENDING,
        )

    if not start_client_import_worker(run.pk):
        ClientImportRun.objects.filter(pk=run.pk).update(
            status=ClientImportRun.STATUS_FAILED,
            error="Не удалось запустить фоновый процесс импорта",
            finished_at=timezone.now(),
            updated_at=timezone.now(),
        )
        run.refresh_from_db()
        return run, False
    return run, True


def _reap_client_apply_worker(process, run_id):
    try:
        return_code = process.wait()
        if return_code:
            logger.error(
                "1C client apply worker exited with code %s for run_id=%s",
                return_code,
                run_id,
            )
            ClientImportRun.objects.filter(
                pk=run_id,
                status=ClientImportRun.STATUS_APPLYING,
            ).update(
                status=ClientImportRun.STATUS_FAILED,
                error="Фоновый импорт клиентов завершился с ошибкой",
                finished_at=timezone.now(),
                updated_at=timezone.now(),
            )
    except Exception:
        logger.exception("Failed while reaping 1C client apply worker")


def start_client_apply_worker(run_id):
    base_dir = str(settings.BASE_DIR)
    worker_script = os.path.join(base_dir, "scripts", "run_client_apply_worker.sh")
    bash = shutil.which("bash")
    if not bash or not os.path.isfile(worker_script):
        logger.error("1C client apply worker launcher is unavailable")
        return False

    env = os.environ.copy()
    env["SERVICE2_PYTHON"] = _worker_python_executable(base_dir)
    try:
        process = subprocess.Popen(
            [bash, worker_script, str(run_id)],
            cwd=base_dir,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
            close_fds=True,
        )
    except OSError:
        logger.exception("Failed to start 1C client apply worker")
        return False

    threading.Thread(
        target=_reap_client_apply_worker,
        args=(process, run_id),
        daemon=True,
        name="service2-client-apply-reaper",
    ).start()
    return True


def request_client_apply():
    organization = _target_organization()
    with transaction.atomic():
        organization = Organization.objects.select_for_update().get(pk=organization.pk)
        run = (
            ClientImportRun.objects.select_for_update()
            .filter(organization=organization)
            .order_by("-requested_at", "-id")
            .first()
        )
        if run is None:
            raise ValueError("Импорт можно запустить только после успешного обновления из 1С")
        if run.status == ClientImportRun.STATUS_APPLYING:
            return run, False
        if run.status != ClientImportRun.STATUS_SUCCESS:
            raise ValueError("Импорт можно запустить только после успешного обновления из 1С")

        ready_count = ClientImportCandidate.objects.filter(
            organization=organization,
            status=ClientImportCandidate.STATUS_READY,
        ).count()
        if not ready_count:
            return run, False

        run.status = ClientImportRun.STATUS_APPLYING
        run.error = ""
        run.total_rows = ready_count
        run.processed_rows = 0
        run.started_at = timezone.now()
        run.finished_at = None
        run.save(
            update_fields=[
                "status",
                "error",
                "total_rows",
                "processed_rows",
                "started_at",
                "finished_at",
                "updated_at",
            ]
        )

    if not start_client_apply_worker(run.pk):
        ClientImportRun.objects.filter(pk=run.pk).update(
            status=ClientImportRun.STATUS_FAILED,
            error="Не удалось запустить фоновый импорт клиентов",
            finished_at=timezone.now(),
            updated_at=timezone.now(),
        )
        run.refresh_from_db()
        return run, False
    return run, True


def process_client_import_run(run_id):
    with transaction.atomic():
        run = (
            ClientImportRun.objects.select_for_update()
            .select_related("organization")
            .get(pk=run_id)
        )
        if run.status != ClientImportRun.STATUS_PENDING:
            return run
        run.status = ClientImportRun.STATUS_RUNNING
        run.started_at = timezone.now()
        run.error = ""
        run.save(update_fields=["status", "started_at", "error", "updated_at"])

    try:
        result = scan_onec_clients(run=run)
    except Exception as exc:
        logger.exception("1C client import failed for run_id=%s", run_id)
        code = getattr(exc, "code", exc.__class__.__name__)
        ClientImportRun.objects.filter(pk=run_id).update(
            status=ClientImportRun.STATUS_FAILED,
            error=f"Ошибка импорта: {str(code)[:400]}",
            finished_at=timezone.now(),
            updated_at=timezone.now(),
        )
    else:
        ClientImportRun.objects.filter(pk=run_id).update(
            status=ClientImportRun.STATUS_SUCCESS,
            total_rows=result.get("total", 0),
            processed_rows=result.get("total", 0),
            ready_count=result.get(ClientImportCandidate.STATUS_READY, 0),
            review_count=result.get(ClientImportCandidate.STATUS_REVIEW, 0),
            duplicate_count=result.get(ClientImportCandidate.STATUS_DUPLICATE, 0),
            invalid_count=(
                result.get(ClientImportCandidate.STATUS_INVALID, 0)
                + result.get(ClientImportCandidate.STATUS_SKIPPED, 0)
            ),
            imported_count=result.get(ClientImportCandidate.STATUS_IMPORTED, 0),
            error="",
            finished_at=timezone.now(),
            updated_at=timezone.now(),
        )
    return ClientImportRun.objects.get(pk=run_id)

def _split_person_name(value):
    cleaned = re.sub(r"(?i)\bИП\b", " ", str(value or "")).replace('"', " ")
    parts = [part for part in re.split(r"\s+", cleaned.strip()) if part]
    last_name = parts[0] if parts else ""
    first_name = parts[1] if len(parts) > 1 else ""
    middle_name = " ".join(parts[2:]) if len(parts) > 2 else ""
    return last_name, first_name, middle_name


def _sync_contacts(client, candidate):
    payload = candidate.payload or {}
    sources = ["onec"]
    for kind, key in ((ClientContact.KIND_PHONE, "phones"), (ClientContact.KIND_EMAIL, "emails")):
        for item in payload.get(key, []):
            value = str(item.get("value") or "").strip()
            if not value:
                continue
            match_value = normalize_phone(value) if kind == ClientContact.KIND_PHONE else normalize_email(value)
            contact, created = ClientContact.objects.get_or_create(
                client=client,
                kind=kind,
                value=value,
                defaults={
                    "match_value": match_value,
                    "label": str(item.get("label") or "")[:120],
                    "is_primary": False,
                    "sources": sources,
                    "source_reference": candidate.source_ref,
                },
            )
            if not created:
                changed = False
                if contact.match_value != match_value:
                    contact.match_value = match_value
                    changed = True
                if "onec" not in (contact.sources or []):
                    contact.sources = list(contact.sources or []) + ["onec"]
                    changed = True
                if changed:
                    contact.save(update_fields=["match_value", "sources", "updated_at"])


def _find_or_create_ip_person(company, candidate):
    existing_link = (
        ClientCompanyLink.objects.filter(
            company=company,
            source=ClientCompanyLink.SOURCE_ONEC_IP,
            source_reference=candidate.source_ref,
        )
        .select_related("person")
        .first()
    )
    person = existing_link.person if existing_link else None

    phones = (candidate.payload or {}).get("phones", [])
    phone_values = {item.get("match") for item in phones if item.get("match")}
    person_matches = Client.objects.filter(
        organization=candidate.organization,
        client_type="private",
    )
    ids = set()
    if phone_values:
        ids.update(
            ClientContact.objects.filter(
                client__in=person_matches,
                kind=ClientContact.KIND_PHONE,
                match_value__in=phone_values,
            ).values_list("client_id", flat=True)
        )
        for person in person_matches.exclude(phone__isnull=True).exclude(phone="").only("id", "phone"):
            if normalize_phone(person.phone) in phone_values:
                ids.add(person.id)
    if person is None:
        if len(ids) > 1:
            raise ValueError("Для ИП найдено несколько физлиц с тем же телефоном")
        person = person_matches.filter(id=next(iter(ids))).first() if ids else None

    last_name, first_name, middle_name = _split_person_name(
        (candidate.payload or {}).get("fio") or candidate.name
    )
    if person is None:
        exact_name = " ".join(part for part in [last_name, first_name, middle_name] if part).strip()
        named = list(person_matches.filter(name__iexact=exact_name)[:2]) if exact_name else []
        if named:
            raise ValueError(
                "Для ИП найдено физлицо с тем же ФИО без подтверждающего телефона"
            )
    if person is None:
        person = Client.objects.create(
            organization=candidate.organization,
            client_type="private",
            name=" ".join(part for part in [last_name, first_name, middle_name] if part).strip() or candidate.name,
            first_name=first_name or None,
            last_name=last_name or None,
            phone=candidate.phone or None,
            email=candidate.email or None,
        )
    profile, _ = ClientCRMProfile.objects.get_or_create(client=person)
    if middle_name and not profile.middle_name:
        profile.middle_name = middle_name
    if candidate.birth_date and not profile.birth_date:
        profile.birth_date = candidate.birth_date
    if profile.source == ClientCRMProfile.SOURCE_MANUAL:
        profile.source = ClientCRMProfile.SOURCE_ONEC_IP
    profile.save()
    _sync_contacts(person, candidate)
    ClientCompanyLink.objects.update_or_create(
        company=company,
        person=person,
        defaults={
            "is_primary": True,
            "source": ClientCompanyLink.SOURCE_ONEC_IP,
            "source_reference": candidate.source_ref,
            "automatic": True,
        },
    )
    return person


@transaction.atomic
def apply_candidate(candidate):
    candidate = ClientImportCandidate.objects.select_for_update().select_related(
        "organization", "matched_client"
    ).get(pk=candidate.pk)
    if candidate.status not in {
        ClientImportCandidate.STATUS_READY,
        ClientImportCandidate.STATUS_IMPORTED,
    }:
        raise ValueError("Карточка требует ручной проверки")

    kind = candidate.effective_kind
    client = candidate.matched_client
    if client is None:
        client_type = "private" if kind == ClientImportCandidate.KIND_PRIVATE else "legal"
        last_name = first_name = middle_name = ""
        if client_type == "private":
            last_name, first_name, middle_name = _split_person_name(candidate.name)
        client = Client.objects.create(
            organization=candidate.organization,
            client_type=client_type,
            name=candidate.name,
            company_name=candidate.name if client_type == "legal" else None,
            first_name=first_name or None,
            last_name=last_name or None,
            phone=candidate.phone or None,
            email=candidate.email or None,
            inn=candidate.inn or None,
        )
    else:
        already_linked_to_same_onec = ClientCRMProfile.objects.filter(
            client=client,
            onec_ref=candidate.source_ref,
        ).exists()
        if already_linked_to_same_onec:
            client.name = candidate.name or client.name
            if kind == ClientImportCandidate.KIND_PRIVATE:
                last_name, first_name, _middle_name = _split_person_name(candidate.name)
                client.first_name = first_name or client.first_name
                client.last_name = last_name or client.last_name
                client.company_name = None
            else:
                client.company_name = candidate.legal_name or candidate.name or client.company_name
            if candidate.inn:
                client.inn = candidate.inn
            if candidate.phone:
                client.phone = candidate.phone
            if candidate.email:
                client.email = candidate.email
        else:
            if candidate.inn and not client.inn:
                client.inn = candidate.inn
            if not client.phone and candidate.phone:
                client.phone = candidate.phone
            if not client.email and candidate.email:
                client.email = candidate.email
        client.save()

    profile, _ = ClientCRMProfile.objects.get_or_create(client=client)
    profile.legal_form = (
        ClientCRMProfile.LEGAL_FORM_IP
        if kind == ClientImportCandidate.KIND_IP
        else ClientCRMProfile.LEGAL_FORM_ENTITY
        if kind == ClientImportCandidate.KIND_LEGAL
        else ClientCRMProfile.LEGAL_FORM_NONE
    )
    profile.middle_name = profile.middle_name or (_split_person_name(candidate.name)[2] if client.client_type == "private" else "")
    profile.birth_date = candidate.birth_date or profile.birth_date
    profile.legal_name = candidate.legal_name or profile.legal_name
    profile.kpp = candidate.kpp or profile.kpp
    profile.onec_ref = candidate.source_ref
    profile.onec_code = candidate.source_code
    profile.onec_name = candidate.name
    profile.source = ClientCRMProfile.SOURCE_ONEC
    profile.last_synced_at = timezone.now()
    profile.save()
    _sync_contacts(client, candidate)

    if kind == ClientImportCandidate.KIND_IP:
        _find_or_create_ip_person(client, candidate)

    candidate.matched_client = client
    candidate.status = ClientImportCandidate.STATUS_IMPORTED
    candidate.reason = ""
    candidate.applied_at = timezone.now()
    candidate.save(update_fields=["matched_client", "status", "reason", "applied_at", "updated_at"])
    return client


@transaction.atomic
def resolve_import_candidate(candidate_id, resolution, resolved_by=None):
    allowed = {
        ClientImportCandidate.RESOLUTION_AUTO,
        ClientImportCandidate.RESOLUTION_LEGAL,
        ClientImportCandidate.RESOLUTION_PRIVATE,
        ClientImportCandidate.RESOLUTION_IP,
        ClientImportCandidate.RESOLUTION_SKIP,
    }
    if resolution not in allowed:
        raise ValueError("Недопустимое решение")

    organization_id = ClientImportCandidate.objects.values_list(
        "organization_id", flat=True
    ).get(pk=candidate_id)
    organization = Organization.objects.select_for_update().get(pk=organization_id)
    if ClientImportRun.objects.filter(
        organization=organization,
        status__in=[
            ClientImportRun.STATUS_PENDING,
            ClientImportRun.STATUS_RUNNING,
            ClientImportRun.STATUS_APPLYING,
        ],
    ).exists():
        raise ValueError(
            "Дождитесь завершения текущего обновления или импорта клиентов."
        )

    candidate = (
        ClientImportCandidate.objects.select_for_update()
        .select_related("organization")
        .get(pk=candidate_id, organization=organization)
    )
    if candidate.applied_at:
        raise ValueError("Импортированную карточку нельзя изменить")

    manual_type_resolutions = {
        ClientImportCandidate.RESOLUTION_LEGAL,
        ClientImportCandidate.RESOLUTION_PRIVATE,
        ClientImportCandidate.RESOLUTION_IP,
    }
    if candidate.status == ClientImportCandidate.STATUS_DUPLICATE and resolution in manual_type_resolutions:
        raise ValueError(
            "Для возможного дубля сначала нужно выбрать существующую карточку клиента."
        )
    if candidate.status == ClientImportCandidate.STATUS_INVALID and resolution in manual_type_resolutions:
        raise ValueError(
            "Некорректную системную карточку нельзя импортировать как обычного клиента."
        )

    candidate.resolution = resolution
    if resolution in manual_type_resolutions:
        # A manual type decision invalidates any earlier automatic match,
        # especially matches created before phone matching was type-scoped.
        candidate.matched_client = None
    candidate.resolved_by = resolved_by
    candidate.resolved_at = timezone.now() if resolution != ClientImportCandidate.RESOLUTION_AUTO else None
    candidate.resolution_note = (
        "Ручное решение"
        if resolution != ClientImportCandidate.RESOLUTION_AUTO
        else ""
    )

    if resolution == ClientImportCandidate.RESOLUTION_SKIP:
        candidate.status = ClientImportCandidate.STATUS_SKIPPED
        candidate.reason = "Не импортировать — решение пользователя"
    elif resolution == ClientImportCandidate.RESOLUTION_AUTO:
        # A fresh scan will recalculate automatic diagnostics. Until then keep
        # the row visible for review rather than silently marking it ready.
        candidate.status = ClientImportCandidate.STATUS_REVIEW
        candidate.reason = "Ручное решение сброшено; обновите данные из 1С"
    else:
        candidate.status = ClientImportCandidate.STATUS_READY
        candidate.reason = "Тип подтверждён пользователем"

    candidate.save(
        update_fields=[
            "resolution",
            "matched_client",
            "resolution_note",
            "resolved_by",
            "resolved_at",
            "status",
            "reason",
            "updated_at",
        ]
    )
    _refresh_latest_run_counts(candidate.organization)
    return candidate


def apply_ready_candidates(organization=None, run=None):
    organization = organization or _target_organization()
    candidate_ids = list(
        ClientImportCandidate.objects.filter(
            organization=organization,
            status=ClientImportCandidate.STATUS_READY,
        )
        .order_by("id")
        .values_list("id", flat=True)
    )
    total = len(candidate_ids)
    result = {"imported": 0, "failed": 0}
    processed = 0
    for candidate_id in candidate_ids:
        candidate = ClientImportCandidate.objects.get(pk=candidate_id)
        try:
            apply_candidate(candidate)
        except (ValueError, RuntimeError):
            candidate.status = ClientImportCandidate.STATUS_REVIEW
            candidate.reason = "Не удалось безопасно сопоставить карточку; требуется проверка"
            candidate.save(update_fields=["status", "reason", "updated_at"])
            result["failed"] += 1
        else:
            result["imported"] += 1
        processed += 1
        if run is not None and (processed % 25 == 0 or processed == total):
            ClientImportRun.objects.filter(
                pk=run.pk,
                status=ClientImportRun.STATUS_APPLYING,
            ).update(
                total_rows=total,
                processed_rows=processed,
                imported_count=result["imported"],
                review_count=result["failed"],
                updated_at=timezone.now(),
            )
    return result


def process_client_apply_run(run_id):
    run = (
        ClientImportRun.objects.select_related("organization")
        .filter(pk=run_id, status=ClientImportRun.STATUS_APPLYING)
        .first()
    )
    if run is None:
        return ClientImportRun.objects.get(pk=run_id)

    try:
        result = apply_ready_candidates(run.organization, run=run)
    except Exception as exc:
        logger.exception("1C client apply failed for run_id=%s", run_id)
        code = getattr(exc, "code", exc.__class__.__name__)
        ClientImportRun.objects.filter(pk=run_id).update(
            status=ClientImportRun.STATUS_FAILED,
            error=f"Ошибка импорта клиентов: {str(code)[:400]}",
            finished_at=timezone.now(),
            updated_at=timezone.now(),
        )
    else:
        counts = {
            item["status"]: item["count"]
            for item in ClientImportCandidate.objects.filter(
                organization=run.organization
            ).values("status").annotate(count=models.Count("id"))
        }
        ClientImportRun.objects.filter(pk=run_id).update(
            status=ClientImportRun.STATUS_SUCCESS,
            ready_count=counts.get(ClientImportCandidate.STATUS_READY, 0),
            review_count=counts.get(ClientImportCandidate.STATUS_REVIEW, 0),
            duplicate_count=counts.get(ClientImportCandidate.STATUS_DUPLICATE, 0),
            invalid_count=(
                counts.get(ClientImportCandidate.STATUS_INVALID, 0)
                + counts.get(ClientImportCandidate.STATUS_SKIPPED, 0)
            ),
            imported_count=counts.get(ClientImportCandidate.STATUS_IMPORTED, 0),
            error=(
                f"Требуют проверки после импорта: {result['failed']}"
                if result["failed"]
                else ""
            ),
            finished_at=timezone.now(),
            updated_at=timezone.now(),
        )
    return ClientImportRun.objects.get(pk=run_id)
