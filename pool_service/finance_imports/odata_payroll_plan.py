#!/usr/bin/env python3
"""Read current active payroll plan rows from 1C OData without database writes."""
import argparse
from collections import defaultdict
from datetime import date
from decimal import Decimal, localcontext
import json
import os
from pathlib import Path
import re
import signal
import sys

try:
    from .odata_payroll import (
        MAX_OUTPUT_BYTES,
        PayrollError,
        Reader,
        amount,
        calendar_date,
        diagnostic,
        guid,
        identifier,
        no_duplicate_keys,
    )
except ImportError:
    from odata_payroll import (
        MAX_OUTPUT_BYTES,
        PayrollError,
        Reader,
        amount,
        calendar_date,
        diagnostic,
        guid,
        identifier,
        no_duplicate_keys,
    )

PLAN_REGISTER = "InformationRegister_ПлановыеНачисленияИУдержания_RecordType"
EMPLOYEE_CATALOG = "Catalog_Сотрудники"
TYPE_CATALOG = "Catalog_ВидыНачисленийИУдержаний"
PLAN_FIELDS = (
    "Active",
    "Period",
    "Актуальность",
    "Организация_Key",
    "Сотрудник_Key",
    "Валюта_Key",
    "ВидНачисленияУдержания_Key",
    "Сумма",
)
MAX_PLAN_ROWS = 10000


def _normalize_name(value):
    return re.sub(r"\s+", " ", (value or "").strip()).casefold().replace("ё", "е")


def _catalog_rows(reader, entity, ids, fields, *, kind):
    result = {}
    ordered = sorted(ids)
    for start in range(0, len(ordered), 40):
        requested = set(ordered[start:start + 40])
        if not requested:
            continue
        filter_value = " or ".join(
            "Ref_Key eq guid'%s'" % key for key in sorted(requested)
        )
        for rows in reader.pages_for(
            entity,
            {
                "$format": "json",
                "$select": ",".join(fields),
                "$filter": "(" + filter_value + ")",
            },
        ):
            for row in rows:
                key = guid(row.get("Ref_Key"))
                if key not in requested or key in result:
                    raise PayrollError("CATALOG_INVALID", "catalog")
                if row.get("DeletionMark") is not False:
                    raise PayrollError("CATALOG_INVALID", "catalog")
                if kind == "type" and row.get("IsFolder") is not False:
                    raise PayrollError("CATALOG_INVALID", "catalog")
                description = row.get("Description")
                if (
                    not isinstance(description, str)
                    or not description.strip()
                    or len(description) > 500
                ):
                    raise PayrollError("CATALOG_INVALID", "catalog")
                item = {"description": description.strip()}
                if kind == "type":
                    item["type_value"] = identifier(row.get("Тип"))
                result[key] = item
        if not requested.issubset(result):
            raise PayrollError("CATALOG_INCOMPLETE", "catalog")
    return result


def read_current_plan(config, as_of, *, organization_guids=(), opener=None):
    if not isinstance(as_of, date):
        raise PayrollError("INVALID_DATE", "config")
    raw_orgs = config.get("ONEC_ODATA_ORGANIZATION_GUIDS") or ""
    if not isinstance(raw_orgs, str):
        raise PayrollError("INVALID_CONFIG", "config")
    allowed = {
        guid(value)
        for value in (part.strip() for part in raw_orgs.split(","))
        if value
    }
    selected = (
        {guid(value) for value in organization_guids}
        if organization_guids
        else allowed
    )
    if (
        not allowed
        or not selected
        or not selected.issubset(allowed)
        or len(allowed) > 40
    ):
        raise PayrollError("INVALID_CONFIG", "config")
    currency = guid(config.get("ONEC_ODATA_PAYROLL_CURRENCY_GUID"))
    reader = Reader(config, opener)

    next_day = date.fromordinal(as_of.toordinal() + 1)
    org_filter = " or ".join(
        "Организация_Key eq guid'%s'" % key for key in sorted(selected)
    )
    filters = (
        "Active eq true and Актуальность eq true and "
        "Period lt datetime'%sT00:00:00' and (%s)"
    ) % (next_day.isoformat(), org_filter)

    records = []
    employees = set()
    types = set()
    for rows in reader.pages_for(
        PLAN_REGISTER,
        {
            "$format": "json",
            "$select": ",".join(PLAN_FIELDS),
            "$filter": filters,
        },
    ):
        for row in rows:
            if len(records) >= MAX_PLAN_ROWS:
                raise PayrollError("RESPONSE_LIMIT", "register")
            if type(row.get("Active")) is not bool or type(row.get("Актуальность")) is not bool:
                raise PayrollError("INVALID_ROW", "register")
            if not row["Active"] or not row["Актуальность"]:
                continue
            org = guid(row.get("Организация_Key"))
            if org not in selected:
                raise PayrollError("ORGANIZATION_MISMATCH", "register")
            employee = guid(row.get("Сотрудник_Key"))
            kind = guid(row.get("ВидНачисленияУдержания_Key"))
            row_currency = guid(row.get("Валюта_Key"))
            if row_currency != currency:
                raise PayrollError("INVALID_CONFIG", "register")
            period = calendar_date(row.get("Period"))
            if period > as_of:
                raise PayrollError("INVALID_DATE", "register")
            value = amount(row.get("Сумма"))
            if value != value.quantize(Decimal(".01")):
                raise PayrollError("INVALID_AMOUNT", "register")
            records.append((org, employee, kind, period, value))
            employees.add(employee)
            types.add(kind)

    employee_catalog = _catalog_rows(
        reader,
        EMPLOYEE_CATALOG,
        employees,
        ("Ref_Key", "Description", "DeletionMark"),
        kind="employee",
    )
    type_catalog = _catalog_rows(
        reader,
        TYPE_CATALOG,
        types,
        ("Ref_Key", "Description", "Тип", "IsFolder", "DeletionMark"),
        kind="type",
    )

    latest_period = {}
    for org, employee, kind, period, value in records:
        key = (org, employee, kind)
        latest_period[key] = max(period, latest_period.get(key, period))

    groups = defaultdict(lambda: {"amount": Decimal("0"), "source_rows": 0})
    with localcontext() as context:
        context.prec = 64
        for org, employee, kind, period, value in records:
            key = (org, employee, kind)
            if period != latest_period[key]:
                continue
            type_info = type_catalog[kind]
            if type_info["type_value"] != "Начисление":
                continue
            group = groups[key]
            group["amount"] += value
            group["source_rows"] += 1

    items = []
    for (org, employee, kind), values in sorted(groups.items()):
        employee_name = employee_catalog[employee]["description"]
        type_name = type_catalog[kind]["description"]
        items.append(
            {
                "organization_guid": org,
                "employee_guid": employee,
                "employee_name": employee_name,
                "accrual_type_guid": kind,
                "accrual_type_name": type_name,
                "amount": format(values["amount"], "f"),
                "source_period": latest_period[(org, employee, kind)].isoformat(),
                "source_rows": values["source_rows"],
                "is_base_salary": "оклад" in _normalize_name(type_name),
            }
        )

    result = {
        "kind": "payroll_plan_snapshot_v1",
        "period_month": as_of.replace(day=1).isoformat(),
        "as_of": as_of.isoformat(),
        "selected_organizations": sorted(selected),
        "currency_guid": currency,
        "source_rows": len(records),
        "items": items,
    }
    reader.check_time()
    raw = (json.dumps(result, ensure_ascii=False, sort_keys=True) + "\n").encode()
    if len(raw) > MAX_OUTPUT_BYTES:
        raise PayrollError("OUTPUT_LIMIT", "output")
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--app-dir", required=True)
    parser.add_argument("--as-of", required=True)
    args = parser.parse_args(argv)

    stage = "runtime"

    def timeout(signum, frame):
        raise PayrollError("TIMEOUT", "runtime", "TIMEOUT")

    try:
        signal.signal(signal.SIGALRM, timeout)
        signal.alarm(60)
        stage = "dependencies"
        from dotenv import dotenv_values

        stage = "config"
        try:
            as_of = date.fromisoformat(args.as_of)
        except ValueError:
            raise PayrollError("INVALID_DATE", "config") from None
        config = {**dotenv_values(Path(args.app_dir) / ".env"), **os.environ}
        result = read_current_plan(config, as_of)
        stage = "output"
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 0
    except Exception as exc:
        print(json.dumps(diagnostic(exc, stage)))
        return 1
    finally:
        signal.alarm(0)


if __name__ == "__main__":
    sys.exit(main())
