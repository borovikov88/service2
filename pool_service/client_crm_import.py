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
from .phone_utils import canonical_phone_value, normalize_phone


BUYER_ENTITY = "Catalog_Контрагенты"
CONTACT_ENTITY = "Catalog_Контрагенты_КонтактнаяИнформация"
PAGE_SIZE = 500
CONTACT_BATCH_SIZE = 40
SYSTEM_NAMES = {"розничный покупатель"}
IMPORT_RUN_STALE_MINUTES = 120

logger = logging.getLogger(__name__)



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


def _buyer_rows(config, metadata_raw, *, selector="Покупатель"):
    cursor = ""
    while True:
        filters = [
            {"field": "DeletionMark", "op": "eq", "value": False},
            {"field": selector, "op": "eq", "value": True},
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
                "Поставщик",
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
        if result.get("complete") is False and len(rows) < PAGE_SIZE:
            raise RuntimeError("Incomplete 1C buyer page; refusing reconciliation")
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


def _union_counterparties(buyers, suppliers):
    """A dual-role reference is processed once. Conflicting observations abort the scan."""
    from .services.contact_roles import source_roles
    result = {}
    for rows, role in ((buyers, "buyer"), (suppliers, "supplier")):
        for row in rows:
            ref = str(row.get("Ref_Key") or "")
            flags = source_roles(row)
            if role == "supplier" and flags.get(role) is not True:
                continue
            old = result.get(ref)
            if old is not None:
                previous = source_roles(old)
                if any(previous[key] != flags[key] for key in previous.keys() & flags.keys()):
                    raise RuntimeError("Counterparty roles changed during the scan; retry the complete read")
                continue
            result[ref] = row
    return list(result.values())


def _inactive_role_rows(config, metadata_raw, organization, observed):
    """Verify loss of both roles by exact reference. Never infer it from absence."""
    known = ClientImportCandidate.objects.filter(organization=organization, matched_client__organization=organization,
        applied_at__isnull=False, matched_client__business_roles__onec_active=True).exclude(source_ref__in=observed)
    refs = list(known.order_by("source_ref").values_list("source_ref", flat=True).distinct())
    for start in range(0, len(refs), CONTACT_BATCH_SIZE):
        batch = refs[start:start + CONTACT_BATCH_SIZE]
        result = query_1c_rows(config, BUYER_ENTITY, fields=["Ref_Key", "Покупатель", "Поставщик"],
            filters=[{"field": "Ref_Key", "op": "in", "value": batch}], limit=len(batch),
            include_deleted=False, include_inactive=False, metadata_raw=metadata_raw)
        rows = result.get("rows", [])
        returned = [str(row.get("Ref_Key") or "") for row in rows]
        if result.get("complete") is False or len(set(returned)) != len(returned) or not set(returned) <= set(batch):
            raise RuntimeError("Incomplete exact counterparty role lookup")
        yield from (row for row in rows if row.get("Покупатель") is False and row.get("Поставщик") is False)


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
        if result.get("complete") is False:
            raise RuntimeError(
                "1C contact batch is incomplete; refusing contact reconciliation"
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
            phones.append({"value": canonical_phone_value(value), "match": normalized, "label": label})

    def add_email(raw, label=""):
        value = str(raw or "").strip()
        normalized = normalize_email(value)
        if value and normalized and all(item["match"] != normalized for item in emails):
            emails.append({"value": value, "match": normalized, "label": label})

    add_phone(row.get("НомерТелефонаДляПоиска"), "Основной")
    add_email(row.get("АдресЭПДляПоиска"), "Основной")
    for contact in extra_rows:
        if contact.get("Ref_Key") and str(contact["Ref_Key"]) != str(row.get("Ref_Key") or ""):
            raise ValueError("Контактная информация относится к другому источнику")
        label = str(contact.get("Представление") or "")
        kind = str(contact.get("Тип") or "")
        if "Телефон" in kind:
            add_phone(contact.get("НомерТелефона") or contact.get("Представление"), label)
        elif "Почт" in kind:
            add_email(contact.get("АдресЭП") or contact.get("Представление"), label)
    return phones, emails


def _phone_match_values(items):
    """Normalize phone keys from current or persisted import payloads."""
    values = set()
    for item in items or []:
        if not isinstance(item, dict):
            continue
        # The visible value is authoritative. Older staged candidates may keep
        # an 11-digit Russian key in "match", so normalize either representation
        # with the current shared contract before comparing it to CRM contacts.
        for raw_value in (item.get("value"), item.get("match")):
            normalized = normalize_phone(raw_value)
            if normalized:
                values.add(normalized)
                break
    return values


def _has_conflicting_onec_identity(matches, source_ref):
    if not source_ref or not matches:
        return False
    client_ids = [client.pk for client in matches]
    return ClientCRMProfile.objects.filter(
        client_id__in=client_ids,
        onec_ref__isnull=False,
    ).exclude(
        onec_ref=""
    ).exclude(
        onec_ref=source_ref
    ).exists()


def _has_private_name_conflict(organization, name):
    normalized_name = str(name or "").strip()
    if not normalized_name:
        return False
    return Client.objects.filter(
        organization=organization,
        client_type="private",
        name__iexact=normalized_name,
    ).filter(
        models.Q(crm_profile__isnull=True)
        | models.Q(crm_profile__merged_into__isnull=True)
    ).exists()


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
    phone_values = _phone_match_values(phones)
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


def scan_onec_clients(run=None, *, return_seen_refs=False):
    organization = _target_organization()
    config = config_from_settings()

    # Metadata is large and relatively expensive. Reuse one snapshot for the
    # entire scan instead of downloading it again for every OData batch.
    metadata_raw = fetch_metadata(config)
    rows = _union_counterparties(_buyer_rows(config, metadata_raw),
        _buyer_rows(config, metadata_raw, selector="Поставщик"))
    observed = {str(row.get("Ref_Key") or "") for row in rows}
    retired = list(_inactive_role_rows(config, metadata_raw, organization, observed))
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

    from .client_crm_sync import _stage_recent_candidate
    from .client_crm_models import ClientImportProposal
    totals = defaultdict(int)
    seen_refs = []
    # Network reads finish before locking. Full and incremental share the same
    # staging rules and organization lock as manual decisions and merges.
    with transaction.atomic():
        Organization.objects.select_for_update().get(pk=organization.pk)
        from .services.contact_roles import reconcile_onec_roles, source_roles
        for row in retired:
            candidate = ClientImportCandidate.objects.select_for_update().filter(organization=organization,
                source_ref=str(row["Ref_Key"]), matched_client__organization=organization,
                matched_client__crm_profile__onec_ref=str(row["Ref_Key"]), matched_client__crm_profile__merged_into__isnull=True,
                applied_at__isnull=False).first()
            if candidate:
                candidate.payload = {**candidate.payload, "business_roles": source_roles(row)}
                candidate.save(update_fields=["payload", "updated_at"])
                reconcile_onec_roles(candidate.matched_client, candidate)
        for row in rows:
            ref = str(row.get("Ref_Key") or "")
            if ref:
                seen_refs.append(ref)
            candidate = _stage_recent_candidate(organization, row, contacts_by_ref.get(ref, []))
            totals[candidate.status] += 1
            totals[candidate.source_kind] += 1
        absent = ClientImportCandidate.objects.filter(organization=organization).exclude(source_ref__in=seen_refs)
        ClientImportProposal.objects.filter(candidate__in=absent, state="pending").update(state="stale")
        absent.update(source_available=False)
        _update_run_progress(run, processed_rows=len(rows), totals=totals)

    totals["total"] = len(rows)
    result = dict(totals)
    if return_seen_refs:
        result["_seen_refs"] = seen_refs
    return result


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
    # Kept for callers: contact reconciliation uses the same non-destructive review.
    from .client_import_review import reconcile
    profile = ClientCRMProfile.objects.get(client=client)
    reconcile(candidate, client, profile, person=client.pk != candidate.matched_client_id)


def _find_or_create_ip_person(company, candidate, *, allow_create=False):
    from .client_import_review import reconcile, propose, source_values
    links = list(ClientCompanyLink.objects.select_for_update().filter(company=company,
        source=ClientCompanyLink.SOURCE_ONEC_IP, source_reference=candidate.source_ref))
    if len(links) > 1:
        raise ValueError("Для ИП найдено несколько связанных физлиц")
    created = False
    if links:
        if company.client_type != "legal" or company.crm_profile.legal_form != "ip":
            # Classification is an explicit proposal; it cannot implicitly
            # authorize writes to a linked person while awaiting a decision.
            return None
        person = Client.objects.select_for_update().get(pk=links[0].person_id)
        if person.organization_id != company.organization_id or person.client_type != "private":
            raise ValueError("Некорректная связь ИП")
        profile, _ = ClientCRMProfile.objects.get_or_create(client=person)
        if profile.merged_into_id or profile.onec_ref:
            raise ValueError("Источник физлица ИП требует проверки")
    else:
        if not allow_create:
            propose(candidate, company, company.crm_profile, "ip_person",
                source_values(candidate)["ip_person"])
            return None
        if (company.client_type != "legal" or company.crm_profile.legal_form != "ip"
                or ClientCompanyLink.objects.filter(company=company).exists()):
            raise ValueError("Сначала проверьте существующие связи ИП")
        last, first, middle = _split_person_name((candidate.payload or {}).get("fio") or candidate.name)
        name = " ".join(filter(None, (last, first, middle)))
        phones = _phone_match_values((candidate.payload or {}).get("phones", []))
        if candidate.phone:
            phones.add(normalize_phone(candidate.phone))
        people = Client.objects.filter(organization=candidate.organization, client_type="private")
        contact_match = ClientContact.objects.filter(client__in=people, kind="phone", match_value__in=phones).exists()
        scalar_match = any(normalize_phone(value) in phones for value in people.exclude(phone__isnull=True).exclude(phone="").values_list("phone", flat=True))
        if contact_match or scalar_match or people.filter(name__iexact=name).exists():
            raise ValueError("Совпадение ФИО или телефона не подтверждает личность ИП; требуется ручная связь")
        person = Client.objects.create(organization=candidate.organization, client_type="private", name=name or candidate.name)
        profile = ClientCRMProfile.objects.create(client=person, source=ClientCRMProfile.SOURCE_ONEC_IP)
        ClientCompanyLink.objects.create(company=company, person=person, is_primary=True,
            source=ClientCompanyLink.SOURCE_ONEC_IP, source_reference=candidate.source_ref, automatic=True)
        created = True
    reconcile(candidate, person, profile, created=created, person=True)
    return person


@transaction.atomic
def apply_candidate(candidate):
    from .client_import_review import reconcile, lock_clients
    # All paths use the same order; stale caller instances never supply values.
    org_id = ClientImportCandidate.objects.values_list("organization_id", flat=True).get(pk=candidate.pk)
    Organization.objects.select_for_update().get(pk=org_id)
    candidate = ClientImportCandidate.objects.get(pk=candidate.pk, organization_id=org_id)
    locked = lock_clients(candidate)
    candidate = ClientImportCandidate.objects.select_for_update().get(pk=candidate.pk, organization_id=org_id)
    if (candidate.status not in {ClientImportCandidate.STATUS_READY, ClientImportCandidate.STATUS_IMPORTED}
            or not candidate.source_available or not candidate.source_ref
            or candidate.name.casefold().strip() in SYSTEM_NAMES):
        raise ValueError("Карточка требует ручной проверки")
    linked = ClientCRMProfile.objects.filter(onec_ref=candidate.source_ref).first()
    if linked and (linked.client_id != candidate.matched_client_id or linked.merged_into_id):
        raise ValueError("Идентификатор 1С уже связан с другой карточкой")
    kind = candidate.effective_kind
    created = candidate.matched_client_id is None
    if created:
        # Recheck identity after staging: another import/manual edit may have won.
        matches, _reason = _match_clients(candidate.organization, kind, candidate.inn,
            (candidate.payload or {}).get("phones", []), candidate.source_ref)
        if matches and candidate.resolution == ClientImportCandidate.RESOLUTION_AUTO:
            raise ValueError("Сопоставление изменилось; требуется повторная проверка")
        if candidate.resolution == ClientImportCandidate.RESOLUTION_AUTO:
            if kind == "legal" and not candidate.inn:
                raise ValueError("У юридического лица не заполнен ИНН")
            if (kind == "private" and not candidate.phone and not _phone_match_values((candidate.payload or {}).get("phones", []))
                    and _has_private_name_conflict(candidate.organization, candidate.name)):
                raise ValueError("Совпадение имени требует проверки")
        if not candidate.name or len(candidate.name) > Client._meta.get_field("name").max_length:
            raise ValueError("Некорректное имя для новой карточки")
        client = Client.objects.create(organization_id=org_id,
            client_type="private" if kind == "private" else "legal", name=candidate.name)
    else:
        client = locked[candidate.matched_client_id]
        if client.organization_id != org_id:
            raise ValueError("Карточка вне организации источника")
        previous = ClientCRMProfile.objects.select_for_update().filter(client=client).first()
        if previous and (previous.merged_into_id or previous.onec_ref and previous.onec_ref != candidate.source_ref):
            raise ValueError("Идентификатор 1С или карточка изменились")
        if not linked:
            # An unlinked phone/name match is insufficient evidence of identity.
            if not (kind in ("legal", "ip") and client.client_type == "legal"
                    and candidate.inn and client.inn == candidate.inn):
                raise ValueError("Личность клиента не подтверждена источником")
            matches, reason = _match_clients(candidate.organization, kind, candidate.inn, [], candidate.source_ref)
            if reason != "ИНН" or [item.pk for item in matches] != [client.pk]:
                raise ValueError("Сопоставление по ИНН неоднозначно")
    profile, _ = ClientCRMProfile.objects.get_or_create(client=client)
    profile.onec_ref = candidate.source_ref
    profile.onec_code = candidate.source_code
    profile.onec_name = candidate.name
    profile.source = ClientCRMProfile.SOURCE_ONEC
    profile.last_synced_at = timezone.now()
    profile.save(update_fields=["onec_ref", "onec_code", "onec_name", "source", "last_synced_at", "updated_at"])
    candidate.matched_client = client
    candidate.save(update_fields=["matched_client"])
    reconcile(candidate, client, profile, created=created)
    from .services.contact_roles import reconcile_onec_roles
    reconcile_onec_roles(client, candidate)
    if kind == ClientImportCandidate.KIND_IP:
        _find_or_create_ip_person(client, candidate, allow_create=created)
    else:
        from .client_crm_models import ClientImportProposal
        ClientImportProposal.objects.filter(candidate=candidate, state="pending").exclude(client=client).update(state="stale")
    # Changed source type never removes a relationship implicitly.
    candidate.status = ClientImportCandidate.STATUS_IMPORTED
    candidate.reason = ""
    candidate.applied_at = timezone.now()
    candidate.save(update_fields=["status", "reason", "applied_at", "updated_at"])
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


def apply_ready_candidates(organization=None, run=None, candidate_ids=None):
    organization = organization or _target_organization()
    if candidate_ids is None:
        candidate_ids = list(
            ClientImportCandidate.objects.filter(
                organization=organization,
                status=ClientImportCandidate.STATUS_READY,
            )
            .order_by("id")
            .values_list("id", flat=True)
        )
    else:
        candidate_ids = list(
            ClientImportCandidate.objects.filter(
                organization=organization,
                status=ClientImportCandidate.STATUS_READY,
                id__in=list(candidate_ids),
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
