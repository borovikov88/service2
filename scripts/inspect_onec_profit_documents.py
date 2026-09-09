#!/usr/bin/env python3
"""GET-only 1C probe for sales document identity; never print GUIDs or amounts."""

from __future__ import annotations

import argparse
import base64
from collections import defaultdict
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
import json
import os
from pathlib import Path
import re
import signal
import ssl
import sys
from urllib.parse import quote, unquote, urljoin, urlsplit, urlunsplit
from urllib.request import HTTPSHandler, Request, build_opener
from uuid import UUID
import xml.etree.ElementTree as ET

try:
    from scripts.inspect_onec_payroll_schema import (
        NoRedirectHandler,
        SchemaError,
        fetch_metadata,
        identifier,
        local_tag,
        metadata_url,
        safe_diagnostic,
        type_name,
    )
except ModuleNotFoundError:  # Direct execution adds scripts/, not its parent, to sys.path.
    from inspect_onec_payroll_schema import (  # type: ignore
        NoRedirectHandler,
        SchemaError,
        fetch_metadata,
        identifier,
        local_tag,
        metadata_url,
        safe_diagnostic,
        type_name,
    )


ENTITY_SET = "AccumulationRegister_Продажи_RecordType"
BASE_PATH = "/odata/standard.odata/"
MAX_BYTES = 8 * 1024 * 1024
MAX_ROWS = 500
MAX_PAGES = 20
MAX_DOCUMENTS = 200
TIMEOUT_SECONDS = 20
TOTAL_TIMEOUT_SECONDS = 55
MONTH_RE = re.compile(r"^\d{4}-\d{2}$")
LINK_RE = re.compile(r"заказ|реализац|документ|основан|сделк", re.IGNORECASE)


class ProbeError(Exception):
    """A fixed diagnostic failure that does not expose source data or URLs."""

    def __init__(self, code):
        super().__init__(code)
        self.code = code


def _month_bounds(value):
    if not isinstance(value, str) or not MONTH_RE.fullmatch(value):
        raise ProbeError("INVALID_MONTH")
    try:
        start = date.fromisoformat(value + "-01")
    except ValueError:
        raise ProbeError("INVALID_MONTH") from None
    end = date(start.year + (start.month == 12), start.month % 12 + 1, 1)
    return start, end


def _guid(value):
    try:
        return str(UUID(str(value))).lower()
    except (TypeError, ValueError, AttributeError):
        raise ProbeError("INVALID_GUID_VALUE") from None


def _schema(raw):
    if len(raw) > MAX_BYTES * 2:
        raise ProbeError("METADATA_SIZE_LIMIT")
    try:
        xml = raw.decode("utf-8-sig")
    except UnicodeError:
        raise ProbeError("METADATA_REQUIRES_UTF8") from None
    if re.search(r"<!\s*(?:DOCTYPE|ENTITY)\b", xml, re.IGNORECASE):
        raise ProbeError("METADATA_DTD_FORBIDDEN")
    try:
        root = ET.fromstring(xml)
    except (ET.ParseError, ValueError):
        raise ProbeError("METADATA_INVALID_XML") from None

    types, aliases, sets = {}, {}, {}
    schemas = [item for item in root.iter() if local_tag(item) == "Schema"]
    if not schemas:
        raise ProbeError("METADATA_EDM_SCHEMA_MISSING")
    for schema in schemas:
        namespace = identifier(schema.get("Namespace"))
        if schema.get("Alias"):
            aliases[identifier(schema.get("Alias"))] = namespace
        for node in schema:
            kind = local_tag(node)
            if kind in {"EntityType", "ComplexType"}:
                qualified = namespace + "." + identifier(node.get("Name"))
                if qualified in types:
                    raise ProbeError("METADATA_DUPLICATE_TYPE")
                types[qualified] = node
            elif kind == "EntityContainer":
                for item in node:
                    if local_tag(item) == "EntitySet":
                        sets[identifier(item.get("Name"))] = type_name(item.get("EntityType"))

    def canonical(value):
        name = type_name(value)
        prefix, dot, suffix = name.partition(".")
        return aliases.get(prefix, prefix) + dot + suffix

    sets = {name: canonical(kind) for name, kind in sets.items()}

    def properties(entity_set):
        qualified = sets.get(entity_set)
        if not qualified:
            raise ProbeError("REQUIRED_ENTITY_MISSING")
        result, seen = {}, set()
        while qualified and qualified not in seen:
            seen.add(qualified)
            node = types.get(qualified)
            if node is None:
                raise ProbeError("METADATA_TYPE_REFERENCE_MISSING")
            for child in node:
                if local_tag(child) == "Property":
                    result.setdefault(identifier(child.get("Name")), child.get("Type"))
            qualified = canonical(node.get("BaseType")) if node.get("BaseType") else None
        return result

    return sets, properties


def _safe_url(base_url, current_url, candidate):
    target = urlsplit(urljoin(current_url, candidate))
    base = urlsplit(base_url)
    decoded_path = target.path
    for _ in range(5):
        updated = unquote(decoded_path)
        if updated == decoded_path:
            break
        decoded_path = updated
    path_segments = decoded_path.replace("\\", "/").split("/")
    if (
        target.scheme != "https"
        or target.hostname != base.hostname
        or target.port != base.port
        or target.username is not None
        or target.password is not None
        or target.fragment
        or not decoded_path.startswith(unquote(base.path))
        or "\\" in decoded_path
        or any(segment in {".", ".."} for segment in path_segments)
    ):
        raise ProbeError("UNSAFE_ODATA_URL")
    return urlunsplit((target.scheme, target.netloc, target.path, target.query, ""))


def _opener(config):
    context = ssl.create_default_context(
        cafile=config.get("SSL_CERT_FILE") or None,
        capath=config.get("SSL_CERT_DIR") or None,
    )
    return build_opener(NoRedirectHandler(), HTTPSHandler(context=context))


def _authorization(config):
    username = config.get("ONEC_ODATA_USERNAME") or ""
    password = config.get("ONEC_ODATA_PASSWORD") or ""
    if bool(username) != bool(password) or ":" in username:
        raise ProbeError("INVALID_ODATA_CREDENTIAL_CONFIG")
    if not username:
        return ""
    return "Basic " + base64.b64encode(f"{username}:{password}".encode()).decode("ascii")


def _pages(config, initial_url, *, opener):
    base_url = config.get("ONEC_ODATA_BASE_URL") or ""
    metadata_url(base_url)  # Enforce the stricter HTTPS config used by the schema probe.
    auth = _authorization(config)
    current = _safe_url(base_url, base_url, initial_url)
    seen = set()
    for _ in range(MAX_PAGES):
        if current in seen:
            raise ProbeError("PAGINATION_LOOP")
        seen.add(current)
        request = Request(current, headers={"Accept": "application/json"}, method="GET")
        if auth:
            request.add_header("Authorization", auth)
        try:
            with opener.open(request, timeout=TIMEOUT_SECONDS) as response:
                raw = response.read(MAX_BYTES + 1)
        except Exception:
            raise ProbeError("ODATA_GET_FAILED") from None
        if len(raw) > MAX_BYTES:
            raise ProbeError("ODATA_SIZE_LIMIT")
        try:
            payload = json.loads(raw.decode("utf-8-sig"), parse_float=Decimal)
        except (UnicodeError, json.JSONDecodeError):
            raise ProbeError("ODATA_INVALID_JSON") from None
        if not isinstance(payload, dict):
            raise ProbeError("ODATA_INVALID_PAYLOAD")
        if "d" in payload:
            legacy = payload.get("d")
            if not isinstance(legacy, dict) or not isinstance(legacy.get("results"), list):
                raise ProbeError("ODATA_INVALID_PAYLOAD")
            rows, next_link = legacy["results"], legacy.get("__next")
        else:
            rows = payload.get("value")
            next_link = payload.get("@odata.nextLink") or payload.get("odata.nextLink")
            if not isinstance(rows, list):
                raise ProbeError("ODATA_INVALID_PAYLOAD")
        yield rows
        if not next_link:
            return
        current = _safe_url(base_url, current, next_link)
    raise ProbeError("PAGINATION_LIMIT")


def _entity_from_type(value, allowed_sets):
    if not isinstance(value, str) or not value.strip():
        return None
    local = value.strip().rsplit(".", 1)[-1]
    return local if local.startswith("Document_") and local in allowed_sets else None


def _numeric_nonzero(value):
    if isinstance(value, (bool, float)) or value is None:
        raise ProbeError("INVALID_AMOUNT_SHAPE")
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise ProbeError("INVALID_AMOUNT_SHAPE") from None
    if not amount.is_finite():
        raise ProbeError("INVALID_AMOUNT_SHAPE")
    return amount != 0


def _source_date(value, start, end):
    if not isinstance(value, str):
        raise ProbeError("INVALID_PERIOD")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00")).date()
    except ValueError:
        raise ProbeError("INVALID_PERIOD") from None
    if not start <= parsed < end:
        raise ProbeError("PERIOD_FILTER_FAILED")
    return parsed.isoformat()


def _display_scalar(value, *, max_length=100):
    if value is None:
        return ""
    if isinstance(value, (dict, list, bool, float)):
        raise ProbeError("INVALID_DOCUMENT_DISPLAY")
    text = str(value).strip()
    if len(text) > max_length:
        raise ProbeError("INVALID_DOCUMENT_DISPLAY")
    try:
        UUID(text)
    except (TypeError, ValueError, AttributeError):
        return text
    return "[hidden-guid]"


def _document_fields(entity_set, properties):
    props = properties(entity_set)
    fields = [name for name in ("Ref_Key", "Number", "Date") if name in props]
    if "Ref_Key" not in fields or "Number" not in fields or "Date" not in fields:
        raise ProbeError("DOCUMENT_DISPLAY_FIELDS_MISSING")
    link_names = []
    for name, declared in props.items():
        if name in fields or name.endswith("_Type") or not LINK_RE.search(name):
            continue
        if declared not in {"Edm.Guid", "Edm.String"}:
            continue
        link_names.append(name)
        companion = name + "_Type"
        if companion in props and companion not in link_names:
            link_names.append(companion)
    if len(link_names) > 24:
        raise ProbeError("TOO_MANY_DOCUMENT_LINK_FIELDS")
    return fields + link_names


def _query_url(base_url, entity_set, fields, expression, *, top):
    return (
        f"{base_url}{quote(entity_set, safe='')}?"
        f"$select={quote(','.join(fields))}&$filter={quote(expression)}&$top={top}"
    )


def _read_sales(config, month, organizations, register_props, *, opener):
    required = {
        "Recorder", "Recorder_Type", "LineNumber", "Period", "Active",
        "Организация_Key", "Документ", "Сумма", "Себестоимость",
    }
    if not required.issubset(register_props):
        raise ProbeError("SALES_DIAGNOSTIC_FIELDS_MISSING")
    fields = list(required)
    if "Документ_Type" in register_props:
        fields.append("Документ_Type")
    start, end = _month_bounds(month)
    org_filter = " or ".join(f"Организация_Key eq guid'{value}'" for value in organizations)
    expression = (
        "Active eq true and "
        f"Period ge datetime'{start.isoformat()}T00:00:00' and "
        f"Period lt datetime'{end.isoformat()}T00:00:00' and ({org_filter})"
    )
    url = _query_url(config["ONEC_ODATA_BASE_URL"], ENTITY_SET, sorted(fields), expression, top=MAX_ROWS + 1)
    rows = []
    for page in _pages(config, url, opener=opener):
        rows.extend(page)
        if len(rows) > MAX_ROWS:
            break
    truncated = len(rows) > MAX_ROWS
    return rows[:MAX_ROWS], truncated, sorted(fields), start, end


def _read_documents(config, refs, sets, properties, *, opener):
    documents = {}
    by_type = defaultdict(set)
    for entity_set, guid in refs:
        if entity_set in sets and entity_set.startswith("Document_"):
            by_type[entity_set].add(guid)
    if sum(map(len, by_type.values())) > MAX_DOCUMENTS:
        raise ProbeError("DOCUMENT_LIMIT")
    for entity_set, guids in sorted(by_type.items()):
        fields = _document_fields(entity_set, properties)
        for offset in range(0, len(guids), 30):
            batch = sorted(guids)[offset:offset + 30]
            expression = " or ".join(f"Ref_Key eq guid'{value}'" for value in batch)
            url = _query_url(config["ONEC_ODATA_BASE_URL"], entity_set, fields, expression, top=len(batch) + 1)
            returned = []
            for page in _pages(config, url, opener=opener):
                returned.extend(page)
            if len(returned) > len(batch):
                raise ProbeError("DOCUMENT_QUERY_OVERFLOW")
            for raw in returned:
                if not isinstance(raw, dict):
                    raise ProbeError("INVALID_DOCUMENT_ROW")
                key = _guid(raw.get("Ref_Key"))
                if key not in batch or (entity_set, key) in documents:
                    raise ProbeError("INVALID_DOCUMENT_IDENTITY")
                documents[(entity_set, key)] = raw
    return documents, set(refs) - set(documents)


def _links(entity_set, raw, sets):
    result = []
    for name, value in raw.items():
        if name in {"Ref_Key", "Number", "Date"} or name.endswith("_Type") or not LINK_RE.search(name):
            continue
        try:
            guid = _guid(value)
        except ProbeError:
            continue
        linked_type = _entity_from_type(raw.get(name + "_Type"), sets)
        if linked_type is None and name.endswith("_Key"):
            inferred = "Document_" + name[:-4]
            linked_type = inferred if inferred in sets else None
        if linked_type:
            result.append((name, (linked_type, guid)))
    return result


def inspect(config, *, month, requested_organizations=(), opener=None, metadata_raw=None):
    configured = tuple(dict.fromkeys(
        _guid(item.strip())
        for item in (config.get("ONEC_ODATA_ORGANIZATION_GUIDS") or "").split(",")
        if item.strip()
    ))
    requested = tuple(dict.fromkeys(_guid(item) for item in requested_organizations)) or configured
    if not requested or not set(requested).issubset(configured):
        raise ProbeError("ORGANIZATION_NOT_ALLOWED")
    client = opener or _opener(config)
    raw_schema = metadata_raw if metadata_raw is not None else fetch_metadata(config, opener=client)
    sets, properties = _schema(raw_schema)
    register_props = properties(ENTITY_SET)
    sales, truncated, selected_fields, start, end = _read_sales(
        config, month, requested, register_props, opener=client
    )

    normalized, primary_refs = [], set()
    for raw in sales:
        if not isinstance(raw, dict) or raw.get("Active") is not True:
            raise ProbeError("INVALID_SALES_ROW")
        if _guid(raw.get("Организация_Key")) not in requested:
            raise ProbeError("ORGANIZATION_FILTER_FAILED")
        recorder_type = _entity_from_type(raw.get("Recorder_Type"), sets)
        recorder = _guid(raw.get("Recorder"))
        if recorder_type is None:
            raise ProbeError("UNSUPPORTED_RECORDER_TYPE")
        primary_refs.add((recorder_type, recorder))
        document, document_type = None, None
        if raw.get("Документ") not in (None, ""):
            document = _guid(raw.get("Документ"))
            document_type = _entity_from_type(raw.get("Документ_Type"), sets)
            if document_type:
                primary_refs.add((document_type, document))
        normalized.append({
            "recorder": (recorder_type, recorder),
            "document": (document_type, document) if document_type and document else None,
            "period": _source_date(raw.get("Period"), start, end),
            "revenue": _numeric_nonzero(raw.get("Сумма")),
            "cost": _numeric_nonzero(raw.get("Себестоимость")),
        })

    documents, missing_refs = _read_documents(
        config, primary_refs, sets, properties, opener=client
    )
    linked_refs = {
        ref for entity_set, raw in documents.items()
        for _, ref in _links(entity_set[0], raw, sets)
    }
    missing_links = linked_refs - set(documents) - missing_refs
    if missing_links:
        linked_documents, unavailable_links = _read_documents(
            config, missing_links, sets, properties, opener=client
        )
        documents.update(linked_documents)
        missing_refs.update(unavailable_links)

    all_refs = set(documents) | missing_refs
    ordered_refs = sorted(all_refs)
    labels = {ref: f"D{index:03d}" for index, ref in enumerate(ordered_refs, 1)}

    def display(ref):
        result = {
            "label": labels[ref],
            "type": ref[0],
            "resolved": ref in documents,
        }
        if ref not in documents:
            return result
        raw = documents[ref]
        return {
            **result,
            "number": _display_scalar(raw.get("Number")),
            "date": _display_scalar(raw.get("Date"), max_length=40)[:10],
        }

    candidates = defaultdict(lambda: {"rows": set(), "paths": set(), "revenue": 0, "cost": 0})
    patterns = defaultdict(lambda: {"rows": 0, "revenue_rows": 0, "cost_rows": 0})
    for index, row in enumerate(normalized):
        recorder = row["recorder"]
        document = row["document"]
        key = (
            labels[recorder], labels.get(document, ""), row["period"],
            row["revenue"], row["cost"],
        )
        patterns[key]["rows"] += 1
        patterns[key]["revenue_rows"] += int(row["revenue"])
        patterns[key]["cost_rows"] += int(row["cost"])
        row_keys = [(recorder, "register.Recorder")]
        if document:
            row_keys.append((document, "register.Документ"))
        for source in filter(None, (recorder, document)):
            if source not in documents:
                continue
            for field, target in _links(source[0], documents[source], sets):
                row_keys.append((target, f"{source[0]}.{field}"))
        row_targets = defaultdict(set)
        for target, path in row_keys:
            row_targets[target].add(path)
        for target, paths in row_targets.items():
            item = candidates[target]
            item["rows"].add(index)
            item["paths"].update(paths)
            item["revenue"] += int(row["revenue"])
            item["cost"] += int(row["cost"])

    joins = []
    for ref, item in candidates.items():
        if len(item["rows"]) < 2 or not item["revenue"] or not item["cost"]:
            continue
        joins.append({
            "key": display(ref),
            "paths": sorted(item["paths"]),
            "row_count": len(item["rows"]),
            "revenue_row_count": item["revenue"],
            "cost_row_count": item["cost"],
        })

    return {
        "kind": "profit_document_diagnostic",
        "month": month,
        "organization_count": len(requested),
        "rows_scanned": len(normalized),
        "truncated": truncated,
        "register_fields": {
            name: register_props.get(name) for name in selected_fields
            if name in {"Recorder", "Recorder_Type", "Документ", "Документ_Type"}
        },
        "documents": [display(ref) for ref in sorted(documents)],
        "missing_documents": [
            {"type": entity_set, "count": count}
            for entity_set, count in sorted({
                entity_set: sum(1 for item in missing_refs if item[0] == entity_set)
                for entity_set, _ in missing_refs
            }.items())
        ],
        "row_patterns": [
            {
                "recorder": recorder,
                "document": document or None,
                "period": period,
                "has_revenue": has_revenue,
                "has_cost": has_cost,
                **counts,
            }
            for (recorder, document, period, has_revenue, has_cost), counts
            in sorted(patterns.items())
        ],
        "join_candidates": sorted(joins, key=lambda item: item["key"]["label"]),
        "notice": "No GUIDs, amounts, customer names, employee names, or credentials are emitted.",
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--app-dir", required=True)
    parser.add_argument("--month", required=True)
    parser.add_argument("--organization", action="append", default=[])
    args = parser.parse_args(argv)

    def timed_out(signum, frame):
        raise ProbeError("TOTAL_TIMEOUT")

    try:
        signal.signal(signal.SIGALRM, timed_out)
        signal.alarm(TOTAL_TIMEOUT_SECONDS)
        from dotenv import dotenv_values
        config = {**dotenv_values(Path(args.app_dir) / ".env"), **os.environ}
        result = inspect(
            config, month=args.month, requested_organizations=args.organization
        )
        sys.stdout.write(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
        return 0
    except Exception as exc:
        if isinstance(exc, ProbeError):
            payload = {"error": exc.code}
        elif isinstance(exc, SchemaError):
            payload = safe_diagnostic(exc, "metadata_get")
        else:
            payload = {"error": "PROFIT_DIAGNOSTIC_FAILED"}
        try:
            print(json.dumps(payload, ensure_ascii=False))
        except OSError:
            pass
        return 1
    finally:
        signal.alarm(0)


if __name__ == "__main__":
    sys.exit(main())
