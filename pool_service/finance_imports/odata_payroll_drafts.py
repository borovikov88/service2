"""Single-month accrual drafts. Financial activation only after confirmation."""
from decimal import Decimal, localcontext
import copy
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.files.base import ContentFile
from django.db import IntegrityError, transaction

from pool_service.models import OneCImportBatch, OneCReportPeriodState, Organization, PayrollAccrualMonth
from pool_service.services.finance import can_import_payroll
from pool_service.services.permissions import company_has_access
from .odata_payroll import (MAX_OUTPUT_BYTES, PayrollError, amount, diagnostic, guid,
                            month_bounds, no_duplicate_keys)
from .odata_profit_drafts import is_odata_target_organization
from .services import DuplicateImportError, _activate_period_states, _audit, _save_confirmed_batch
from .validators import delete_private_batch_file

PARSER_VERSION = "odata-payroll-1"
SNAPSHOT_SCHEMA = "onec_payroll_accrual_v1"
REPORT_TYPE = OneCImportBatch.TYPE_PAYROLL_ACCRUAL


def _require_access(organization, user):
    active_user = get_user_model().objects.filter(pk=getattr(user, "pk", None), is_active=True).first()
    if not active_user or not can_import_payroll(active_user, organization) or not company_has_access(organization):
        raise PermissionDenied("Недостаточно прав для импорта ФОТ.")
    if not is_odata_target_organization(organization):
        raise ValidationError("Получение данных 1С не настроено для этой организации.")


def config_from_settings():
    config = {key: getattr(settings, key, "") for key in (
        "ONEC_ODATA_BASE_URL", "ONEC_ODATA_USERNAME", "ONEC_ODATA_PASSWORD",
        "ONEC_ODATA_PAYROLL_CURRENCY_GUID", "ONEC_ODATA_PAYROLL_CURRENCY_CODE",
        "ONEC_ODATA_PAYROLL_WITHHOLDING_GUIDS",
    )}
    config["ONEC_ODATA_ORGANIZATION_GUIDS"] = ",".join(settings.ONEC_ODATA_ORGANIZATION_GUIDS)
    config.update({key: os.environ.get(key, "") for key in ("SSL_CERT_FILE", "SSL_CERT_DIR")})
    return config


def _scope(config):
    try:
        if config.get("ONEC_ODATA_PAYROLL_CURRENCY_CODE") != "RUB":
            raise ValueError
        currency = guid(config.get("ONEC_ODATA_PAYROLL_CURRENCY_GUID"))
        orgs = sorted({guid(value.strip()) for value in config["ONEC_ODATA_ORGANIZATION_GUIDS"].split(",") if value.strip()})
        if not orgs or len(orgs) > 40:
            raise ValueError
    except (PayrollError, KeyError, TypeError, ValueError):
        raise ValidationError("Настройте организации 1С и подтвердите GUID валюты ФОТ как RUB.") from None
    # Credentials may rotate without invalidating a verified dataset.
    binding = {"base_url": config.get("ONEC_ODATA_BASE_URL"), "organizations": orgs, "currency": currency, "currency_code": "RUB"}
    withholding = _withholding_guids(config.get("ONEC_ODATA_PAYROLL_WITHHOLDING_GUIDS", ""))
    if withholding:
        binding["withholding_guids"] = sorted(withholding)
    fingerprint = hashlib.sha256(json.dumps(binding, sort_keys=True).encode()).hexdigest()
    return orgs, currency, fingerprint


def _integer(value, *, positive=False):
    if type(value) is not int or value < int(positive) or value > 50000:
        raise ValidationError("Некорректные счётчики исходных данных ФОТ.")
    return value


def _money(value):
    try:
        number = amount(value)
        if number != number.quantize(Decimal(".01")) or abs(number) >= Decimal("1e18"):
            raise ValueError
        return number
    except (PayrollError, ValueError):
        raise ValidationError("Сумма ФОТ не соответствует допустимой точности или размеру.") from None


def _withholding_guids(value):
    try:
        result = {guid(item.strip()) for item in value.split(",") if item.strip()}
        if len(result) > 100:
            raise ValueError
        return result
    except (AttributeError, PayrollError, ValueError):
        raise ValidationError("Некорректные GUID подтверждённых удержаний ФОТ.") from None


def _withholding_preview(preview, orgs, currency):
    """Reconcile individually acknowledged positive deductions with signed receipts.

    The original snapshot stays untouched. Legacy accrual-only snapshots retain
    their existing checks; deductions require richer reader evidence.
    """
    if not isinstance(preview, dict) or not isinstance(preview.get("groups"), list):
        return preview
    allowed = _withholding_guids(getattr(settings, "ONEC_ODATA_PAYROLL_WITHHOLDING_GUIDS", ""))
    if not allowed and not any(isinstance(g, dict) and g.get("type_value") != "Начисление" for g in preview["groups"]):
        return preview
    details = preview.get("kind_groups")
    if not allowed or not isinstance(details, list) or not details:
        raise ValidationError("Обнаружен неподтверждённый вид начисления или удержания. Требуется сверка с отчётом 1С.")
    fields = ("rows", "negative_amount_rows", "negative_currency_amount_rows", "non_cent_rows", "period_month_differs_rows")
    aggregates, seen, deductions, gross = {}, set(), {}, {}
    for item in details:
        if not isinstance(item, dict):
            raise ValidationError("Некорректная расшифровка видов ФОТ.")
        org, category, kind = item.get("organization_guid"), item.get("type_value"), item.get("kind_guid")
        key = (org, item.get("currency_guid"), category)
        identity = key + (kind,)
        if org not in orgs or key[1] != currency or identity in seen:
            raise ValidationError("Изменились организация, валюта или виды ФОТ.")
        try:
            guid(kind)
        except PayrollError:
            raise ValidationError("Некорректный GUID вида ФОТ.") from None
        seen.add(identity)
        if category != "Начисление" and (category not in ("Налог", "Удержание") or kind not in allowed):
            raise ValidationError("Вид удержания ФОТ не подтверждён.")
        if category == "Начисление" and kind in allowed:
            raise ValidationError("Подтверждённое удержание изменило тип в 1С.")
        value = _money(item.get("amount"))
        if value < 0 or _money(item.get("amount_currency")) != value:
            raise ValidationError("Корректировки или валютные суммы ФОТ требуют сверки.")
        counters = {field: _integer(item.get(field), positive=field == "rows") for field in fields}
        if counters["negative_amount_rows"] or counters["negative_currency_amount_rows"] or counters["non_cent_rows"] or counters["period_month_differs_rows"] > counters["rows"]:
            raise ValidationError("Корректировки ФОТ требуют отдельной сверки.")
        aggregate = aggregates.setdefault(key, {**{f: 0 for f in fields}, "amount": Decimal(0), "amount_currency": Decimal(0)})
        for field in fields:
            aggregate[field] += counters[field]
        aggregate["amount"] += value
        aggregate["amount_currency"] += value
        target = gross if category == "Начисление" else deductions
        target[org] = target.get(org, Decimal(0)) + value
    supplied = set()
    for group in preview["groups"]:
        if not isinstance(group, dict):
            raise ValidationError("Некорректная группа ФОТ.")
        key = (group.get("organization_guid"), group.get("currency_guid"), group.get("type_value"))
        if key in supplied or key not in aggregates:
            raise ValidationError("Расшифровка видов не совпала с итогами ФОТ.")
        supplied.add(key)
        if any(_integer(group.get(f)) != aggregates[key][f] for f in fields) or any(_money(group.get(f)) != aggregates[key][f] for f in ("amount", "amount_currency")):
            raise ValidationError("Расшифровка видов не совпала с итогами ФОТ.")
    if supplied != set(aggregates) or not set(deductions).issubset(gross):
        raise ValidationError("Нет полного покрытия начислений и удержаний.")
    result = copy.deepcopy(preview)
    result["groups"] = [g for g in result["groups"] if g["type_value"] == "Начисление"]
    if _integer(preview.get("rows"), positive=True) != sum(a["rows"] for a in aggregates.values()):
        raise ValidationError("Количество строк ФОТ не совпало.")
    result["rows"] = sum(g["rows"] for g in result["groups"])
    settlements = result.get("settlements")
    if not isinstance(settlements, dict) or not isinstance(settlements.get("groups"), list):
        raise ValidationError("Нет контрольного регистра ФОТ.")
    for control in settlements["groups"]:
        if not isinstance(control, dict):
            raise ValidationError("Некорректный контрольный регистр ФОТ.")
        if control.get("record_type") != "Receipt":
            continue
        org = control.get("organization_guid")
        for suffix, total_field in (("", "amount"), ("_currency", "amount_currency")):
            positive = _money(control.get("positive_amount" + suffix))
            negative = _money(control.get("negative_amount" + suffix))
            if positive != gross.get(org) or negative != -deductions.get(org, Decimal(0)) or _money(control.get(total_field)) != positive + negative:
                raise ValidationError("Начисления и удержания не совпали в двух регистрах 1С.")
            control[total_field] = str(positive)
    return result


def classify_preview(preview, month, orgs, currency):
    preview = _withholding_preview(preview, orgs, currency)
    return _classify_gross_preview(preview, month, orgs, currency)


def _classify_gross_preview(preview, month, orgs, currency):
    """Accept only reconciled accrual semantics; do not infer net/payments/debt."""
    if not isinstance(preview, dict) or preview.get("kind") != "unclassified_monthly_payroll_preview" or preview.get("month") != month or preview.get("period_basis") != "ПериодРегистрации":
        raise ValidationError("Структура или период исходных данных ФОТ изменились.")
    if preview.get("selected_organizations") != orgs:
        raise ValidationError("Набор организаций исходных данных изменился.")
    groups = preview.get("groups")
    if preview.get("status") != "data_present" or not isinstance(groups, list) or not groups:
        raise ValidationError("Начислений за месяц нет. Действующие данные сохранены; нулевой ФОТ не создан.")
    totals, counts, differs = {}, 0, 0
    with localcontext() as context:
        context.prec = 64
        for group in groups:
            if not isinstance(group, dict):
                raise ValidationError("Некорректная группа начислений.")
            org = group.get("organization_guid")
            if org not in orgs or org in totals or group.get("currency_guid") != currency:
                raise ValidationError("Начисления содержат другую организацию, валюту или повторную группу.")
            if group.get("type_value") != "Начисление":
                raise ValidationError("Обнаружен неподтверждённый вид начисления или удержания. Требуется сверка с отчётом 1С.")
            value = _money(group.get("amount"))
            if _money(group.get("amount_currency")) != value or _integer(group.get("non_cent_rows")):
                raise ValidationError("Валютные суммы начислений требуют отдельной сверки.")
            rows = _integer(group.get("rows"), positive=True)
            mismatch = _integer(group.get("period_month_differs_rows"))
            if mismatch > rows:
                raise ValidationError("Некорректный счётчик периодов начислений.")
            totals[org] = value
            counts += rows
            differs += mismatch
        if counts != _integer(preview.get("rows"), positive=True):
            raise ValidationError("Количество начислений не совпадает с контрольным итогом.")
        total = _money(str(sum(totals.values(), Decimal(0))))
    missing = sorted(set(orgs) - set(totals))
    if preview.get("organizations_without_rows") != missing:
        raise ValidationError("Покрытие организаций не совпадает с исходными данными.")
    settlements = preview.get("settlements")
    if not isinstance(settlements, dict) or settlements.get("status") != "data_present" or not isinstance(settlements.get("groups"), list):
        raise ValidationError("Нет данных для сверки начислений с расчётами с персоналом.")
    controls = {}
    for group in settlements["groups"]:
        if not isinstance(group, dict):
            raise ValidationError("Некорректная группа расчётов с персоналом.")
        if group.get("organization_guid") not in orgs or group.get("currency_guid") != currency:
            raise ValidationError("Расчёты с персоналом содержат другую организацию или валюту.")
        if group.get("record_type") == "Expense":
            continue  # Payments are not imported or labelled as payroll paid.
        if group.get("record_type") != "Receipt" or group.get("recorder_type") != "StandardODATA.Document_НачислениеЗарплатыУНФ":
            raise ValidationError("Начисления в расчётах с персоналом требуют отдельной сверки по виду документа.")
        org = group["organization_guid"]
        if org in controls:
            raise ValidationError("Повторная контрольная группа начислений.")
        controls[org] = _money(group.get("amount"))
        if controls[org] != _money(group.get("amount_currency")) or _integer(group.get("non_cent_rows")):
            raise ValidationError("Валютные суммы контрольного регистра не совпадают.")
    if controls != totals:
        raise ValidationError("Начисления не совпали в двух регистрах 1С. Действующие данные сохранены.")
    return {"accrued": str(total), "source_rows": counts, "organizations_with_rows": sorted(totals),
            "organizations_without_rows": missing, "period_month_differs_rows": differs}


def _state_token(organization, month):
    # An OData activation hides Excel accruals too; both sources participate in CAS.
    token = {kind: None for kind in (REPORT_TYPE, OneCImportBatch.TYPE_PAYROLL)}
    for state in OneCReportPeriodState.objects.filter(
        organization=organization, report_type__in=token, period_month=month
    ):
        token[state.report_type] = {"batch": str(state.active_batch_id), "updated_at": state.updated_at.isoformat()}
    return token


def _encode(payload):
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    if len(raw) > MAX_OUTPUT_BYTES:
        raise ValidationError("Предпросмотр ФОТ превышает допустимый размер.")
    return raw


def read_for_draft(config, month):
    """Kill the isolated reader on timeout, including a slow-drip TLS response.

    Secrets travel in the child environment, never command arguments or logs.
    The script has no database imports/writes and emits only bounded aggregates.
    """
    environment = os.environ.copy()
    environment.update(config)
    command = [sys.executable, "-B", str(Path(__file__).with_name("odata_payroll.py")),
               "--app-dir", str(settings.BASE_DIR), "--month", month]
    try:
        result = subprocess.run(command, env=environment, stdin=subprocess.DEVNULL,
                                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                timeout=65, check=False)
    except subprocess.TimeoutExpired:
        raise PayrollError("TIMEOUT", "read", "TIMEOUT") from None
    if result.returncode or len(result.stdout) > MAX_OUTPUT_BYTES:
        raise PayrollError("CHECK_FAILED", "read")
    try:
        return json.loads(result.stdout, object_pairs_hook=no_duplicate_keys)
    except (ValueError, UnicodeError):
        raise PayrollError("INVALID_RESPONSE", "parse") from None


def auto_coverage_config():
    """Explicit acknowledgement of the exact configured source organization set."""
    config = config_from_settings()
    orgs, _, fingerprint = _scope(config)
    value = getattr(settings, "ONEC_ODATA_PAYROLL_AUTO_COVERAGE_GUIDS", "")
    try:
        acknowledged = sorted({guid(item.strip()) for item in value.split(",") if item.strip()})
    except (AttributeError, PayrollError, ValueError):
        acknowledged = []
    if acknowledged != orgs:
        raise ValidationError("Подтвердите охват организаций для автоматического обновления ФОТ.")
    return fingerprint


def empty_preview(preview, month, orgs, currency):
    """Only a successful explicitly empty reader response may preserve a month."""
    empty = bool(isinstance(preview, dict)
        and preview.get("kind") == "unclassified_monthly_payroll_preview"
        and preview.get("month") == month
        and preview.get("period_basis") == "ПериодРегистрации"
        and preview.get("selected_organizations") == orgs
        and preview.get("organizations_without_rows") == orgs
        and preview.get("status") == "missing"
        and type(preview.get("rows")) is int and preview["rows"] == 0
        and preview.get("groups") == []
        and isinstance(preview.get("settlements"), dict))
    if not empty:
        return False
    controls = preview["settlements"]
    if controls.get("status") == "missing":
        return type(controls.get("rows")) is int and controls["rows"] == 0 and controls.get("groups") == []
    if controls.get("status") != "data_present" or not isinstance(controls.get("groups"), list) or not controls["groups"]:
        return False
    # Advances/payments can exist before this month's accrual. They are not FOT.
    count = 0
    for group in controls["groups"]:
        if (not isinstance(group, dict) or group.get("record_type") != "Expense"
                or group.get("organization_guid") not in orgs or group.get("currency_guid") != currency):
            return False
        if _money(group.get("amount")) != _money(group.get("amount_currency")) or _integer(group.get("non_cent_rows")):
            return False
        count += _integer(group.get("rows"), positive=True)
    return count == _integer(controls.get("rows"), positive=True)


def create_odata_payroll_draft(month, organization, user, *, preview=None):
    _require_access(organization, user)
    try:
        period, _ = month_bounds(month)
    except PayrollError:
        raise ValidationError("Укажите месяц в формате ГГГГ-ММ.") from None
    config = config_from_settings()
    orgs, currency, fingerprint = _scope(config)
    baseline = _state_token(organization, period)
    try:
        if preview is None:
            preview = read_for_draft(config, month)
    except Exception as exc:
        safe = diagnostic(exc)
        raise ValidationError("Не удалось прочитать ФОТ из 1С: %s / %s." % (safe["error"], safe["reason"])) from None
    summary = classify_preview(preview, month, orgs, currency)
    # Unchanged scheduled runs reuse the active snapshot rather than creating
    # another version merely because the CAS baseline changed after activation.
    active = baseline[REPORT_TYPE]
    if active:
        previous = OneCImportBatch.objects.get(pk=active["batch"], organization=organization, import_type=REPORT_TYPE)
        try:
            previous_payload, _, _ = _read_snapshot(previous, organization)
        except ValidationError:
            # Old snapshots are merely an optional no-op optimisation. A changed
            # scope or damaged old private file must not prevent fresh recovery.
            previous_payload = None
        if previous.status == OneCImportBatch.STATUS_CONFIRMED and previous_payload is not None and previous_payload["preview"] == preview:
            _require_access(organization, user)
            raise DuplicateImportError(previous)
    payload = {"schema": SNAPSHOT_SCHEMA, "organization_id": organization.pk, "month": month,
               "scope": fingerprint, "baseline": baseline, "preview": preview}
    raw = _encode(payload)
    digest = hashlib.sha256(raw).hexdigest()
    duplicate = OneCImportBatch.objects.filter(organization=organization, import_type=REPORT_TYPE, file_sha256=digest).first()
    if duplicate:
        raise DuplicateImportError(duplicate)
    batch = OneCImportBatch(organization=organization, import_type=REPORT_TYPE,
        source_type=OneCImportBatch.SOURCE_ODATA, original_filename=f"payroll-accrual-{month}.json",
        file_sha256=digest, file_size=len(raw), status=OneCImportBatch.STATUS_PREVIEWED,
        uploaded_by=user, parser_version=PARSER_VERSION, period_first=period, period_last=period,
        rows_detected=summary["source_rows"], metadata={"summary": summary, "currency_guid": currency})
    name = batch.stored_file.field.generate_filename(batch, "source.json")
    batch.stored_file.name = batch.stored_file.storage.save(name, ContentFile(raw))
    try:
        with transaction.atomic():
            locked = Organization.objects.select_for_update().get(pk=organization.pk)
            _require_access(locked, user)
            if _scope(config_from_settings())[2] != fingerprint:
                raise ValidationError("Настройки источника изменились во время получения. Повторите предпросмотр.")
            if _state_token(organization, period) != baseline:
                raise ValidationError("Данные месяца обновились во время получения. Получите новый предпросмотр.")
            batch.save()
            _audit(batch, user, {"status": "uploaded"}, {"status": "previewed", "source_type": "odata", "scope": fingerprint})
    except IntegrityError:
        delete_private_batch_file(batch)
        duplicate = OneCImportBatch.objects.filter(organization=organization, import_type=REPORT_TYPE, file_sha256=digest).first()
        if duplicate:
            raise DuplicateImportError(duplicate) from None
        raise
    except Exception:
        delete_private_batch_file(batch)
        raise
    return batch


def _read_snapshot(batch, organization):
    if batch.import_type != REPORT_TYPE or batch.organization_id != organization.pk or batch.source_type != OneCImportBatch.SOURCE_ODATA or batch.parser_version != PARSER_VERSION:
        raise ValidationError("Предпросмотр устарел или относится к другому источнику.")
    try:
        with batch.stored_file.open("rb") as source:
            raw = source.read(MAX_OUTPUT_BYTES + 1)
        if len(raw) > MAX_OUTPUT_BYTES or len(raw) != batch.file_size or hashlib.sha256(raw).hexdigest() != batch.file_sha256:
            raise ValueError
        payload = json.loads(raw, object_pairs_hook=no_duplicate_keys)
    except (OSError, ValueError, TypeError, PayrollError):
        raise ValidationError("Сохранённый предпросмотр недоступен или изменён. Получите данные заново.") from None
    if not isinstance(payload, dict) or payload.get("schema") != SNAPSHOT_SCHEMA or payload.get("organization_id") != organization.pk:
        raise ValidationError("Неверная структура сохранённого предпросмотра.")
    orgs, currency, fingerprint = _scope(config_from_settings())
    if payload.get("scope") != fingerprint:
        raise ValidationError("Настройки источника изменились. Получите данные заново.")
    try:
        period, _ = month_bounds(payload.get("month"))
    except PayrollError:
        raise ValidationError("Неверный месяц сохранённого предпросмотра.") from None
    if period != batch.period_first or period != batch.period_last:
        raise ValidationError("Период предпросмотра изменён.")
    summary = classify_preview(payload.get("preview"), payload["month"], orgs, currency)
    return payload, summary, currency


def payroll_accrual_confirmation_state(batch, organization):
    if batch.status != OneCImportBatch.STATUS_PREVIEWED:
        return {"can_confirm": False, "message": "Этот предпросмотр уже обработан."}
    try:
        payload, summary, currency = _read_snapshot(batch, organization)
        if _state_token(organization, batch.period_first) != payload["baseline"]:
            raise ValidationError("Активные данные месяца изменились. Получите новый предпросмотр.")
    except (ValidationError, KeyError) as exc:
        message = "; ".join(exc.messages) if isinstance(exc, ValidationError) else "Неверная структура предпросмотра."
        return {"can_confirm": False, "message": message}
    return {"can_confirm": True, "message": "", "summary": summary, "currency_guid": currency,
            "replaces_active": any(payload["baseline"].values())}


def confirm_odata_payroll(batch_id, organization, user, *, confirm_coverage=False):
    if confirm_coverage is not True:
        raise ValidationError("Подтвердите сумму и охват организаций предпросмотра.")
    _require_access(organization, user)
    with transaction.atomic():
        organization = Organization.objects.select_for_update().get(pk=organization.pk)
        _require_access(organization, user)
        batch = OneCImportBatch.objects.select_for_update().get(pk=batch_id, organization=organization, import_type=REPORT_TYPE)
        if batch.status == OneCImportBatch.STATUS_CONFIRMED:
            return batch  # Retry does not reactivate a superseded version.
        if batch.status != OneCImportBatch.STATUS_PREVIEWED:
            raise ValidationError("Подтвердить можно только действующий предпросмотр.")
        payload, summary, currency = _read_snapshot(batch, organization)
        if "baseline" not in payload or _state_token(organization, batch.period_first) != payload["baseline"]:
            raise ValidationError("Активные данные месяца изменились. Получите новый предпросмотр.")
        states = list(OneCReportPeriodState.objects.select_for_update().filter(organization=organization, report_type=REPORT_TYPE, period_month=batch.period_first).select_related("active_batch"))
        row = PayrollAccrualMonth(import_batch=batch, organization=organization, period_month=batch.period_first,
            accrued=Decimal(summary["accrued"]), currency_guid=currency,
            source_organization_guids=summary["organizations_with_rows"], source_rows=summary["source_rows"])
        row.full_clean()
        row.save()
        batch.status = OneCImportBatch.STATUS_CONFIRMED
        _save_confirmed_batch(batch, user, 1)
        _activate_period_states(batch, organization, user, [batch.period_first], states)
        _audit(batch, user, {"status": "previewed"}, {"status": "confirmed", "batch_sha256": batch.file_sha256,
            "parser_version": PARSER_VERSION, "scope": payload["scope"], "coverage_confirmed": True})
        return batch
