#!/usr/bin/env python3
"""Read only 1C metadata; report candidate payroll schema, never payroll rows."""

import argparse
import base64
import json
import os
from pathlib import Path
import re
import signal
import sys
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener
import xml.etree.ElementTree as ET


MAX_BYTES = 16 * 1024 * 1024
MAX_OUTPUT_BYTES = 256 * 1024
TIMEOUT_SECONDS = 20
TOTAL_TIMEOUT_SECONDS = 45
EDM_NAMESPACES = {
    "http://schemas.microsoft.com/ado/2006/04/edm",
    "http://schemas.microsoft.com/ado/2007/05/edm",
    "http://schemas.microsoft.com/ado/2008/09/edm",
    "http://schemas.microsoft.com/ado/2009/11/edm",
    "http://docs.oasis-open.org/odata/ns/edm",
}
CANDIDATE = re.compile(
    r"payroll|salary|personnel|employee|начисл|зарплат|заработ|персонал|"
    r"сотрудник|физическ.*лиц|оплат.*труд|удержан", re.IGNORECASE
)
IDENTIFIER = re.compile(r"[^\W\d]\w*(?:\.[^\W\d]\w*)*\Z", re.UNICODE)


class SchemaError(Exception):
    """Only fixed, non-sensitive diagnostic codes are exposed to the user."""


class NoRedirectHandler(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def metadata_url(base_url):
    parts = urlsplit(base_url)
    if (
        parts.scheme != "https" or not parts.hostname
        or parts.username is not None or parts.password is not None
        or parts.query or parts.fragment
        or not parts.path.endswith("/odata/standard.odata/")
        or any(char in base_url for char in "\r\n\t\\")
    ):
        raise SchemaError("INVALID_HTTPS_ODATA_CONFIG")
    try:
        parts.port
    except ValueError:
        raise SchemaError("INVALID_HTTPS_ODATA_CONFIG") from None
    return base_url + "$metadata"


def fetch_metadata(config, *, opener=None):
    url = metadata_url(config.get("ONEC_ODATA_BASE_URL") or "")
    username = config.get("ONEC_ODATA_USERNAME") or ""
    password = config.get("ONEC_ODATA_PASSWORD") or ""
    if bool(username) != bool(password) or ":" in username:
        raise SchemaError("INVALID_ODATA_CREDENTIAL_CONFIG")
    request = Request(url, headers={"Accept": "application/xml"}, method="GET")
    if username:
        token = base64.b64encode(f"{username}:{password}".encode()).decode("ascii")
        request.add_header("Authorization", f"Basic {token}")
    client = opener or build_opener(NoRedirectHandler())
    try:
        with client.open(request, timeout=TIMEOUT_SECONDS) as response:
            if response.status != 200:
                raise SchemaError("METADATA_HTTP_NOT_200")
            raw = response.read(MAX_BYTES + 1)
    except HTTPError as exc:
        raise SchemaError(f"METADATA_HTTP_{exc.code}") from None
    if len(raw) > MAX_BYTES:
        raise SchemaError("METADATA_SIZE_LIMIT")
    return raw


def local_tag(element):
    if element.tag.startswith("{"):
        namespace, tag = element.tag[1:].split("}", 1)
        if namespace in EDM_NAMESPACES:
            return tag
    return ""


def identifier(value):
    if not isinstance(value, str) or not IDENTIFIER.fullmatch(value):
        raise SchemaError("INVALID_SCHEMA_IDENTIFIER")
    return value


def type_name(value):
    if isinstance(value, str) and value.startswith("Collection(") and value.endswith(")"):
        return identifier(value[11:-1])
    return identifier(value)


def describe_metadata(raw, *, entity_names=()):
    if len(raw) > MAX_BYTES:
        raise SchemaError("METADATA_SIZE_LIMIT")
    try:
        xml = raw.decode("utf-8-sig")
    except UnicodeError:
        raise SchemaError("METADATA_REQUIRES_UTF8") from None
    if re.search(r"<!\s*(?:DOCTYPE|ENTITY)\b", xml, re.IGNORECASE):
        raise SchemaError("METADATA_DTD_FORBIDDEN")
    try:
        root = ET.fromstring(xml)
    except (ET.ParseError, ValueError):
        raise SchemaError("METADATA_INVALID_XML") from None
    schemas = [node for node in root.iter() if local_tag(node) == "Schema"]
    if not schemas:
        raise SchemaError("METADATA_EDM_SCHEMA_MISSING")
    types, aliases, entity_sets = {}, {}, []
    for schema in schemas:
        namespace = identifier(schema.get("Namespace"))
        if schema.get("Alias"):
            aliases[identifier(schema.get("Alias"))] = namespace
        for node in schema:
            kind = local_tag(node)
            if kind in ("EntityType", "ComplexType"):
                qualified = namespace + "." + identifier(node.get("Name"))
                if qualified in types:
                    raise SchemaError("METADATA_DUPLICATE_TYPE")
                types[qualified] = node
            elif kind == "EntityContainer":
                for item in node:
                    if local_tag(item) == "EntitySet":
                        entity_sets.append({
                            "name": identifier(item.get("Name")),
                            "type": type_name(item.get("EntityType")),
                        })

    def canonical(value):
        name = type_name(value)
        prefix, dot, suffix = name.partition(".")
        return aliases.get(prefix, prefix) + dot + suffix

    candidates = []
    for item in entity_sets:
        qualified = canonical(item["type"])
        chain, seen = [qualified], set()
        names = [item["name"], qualified]
        while chain:
            current = chain.pop()
            if current in seen:
                continue
            seen.add(current)
            node = types.get(current)
            if node is None:
                raise SchemaError("METADATA_TYPE_REFERENCE_MISSING")
            names.extend(child.get("Name", "") for child in node if local_tag(child) == "Property")
            if node.get("BaseType"):
                chain.append(canonical(node.get("BaseType")))
        if any(CANDIDATE.search(name) for name in names):
            candidates.append({"name": item["name"], "type": qualified})

    requested = set(entity_names)
    known_names = {item["name"] for item in candidates}
    if not requested.issubset(known_names):
        raise SchemaError("REQUESTED_ENTITY_NOT_A_CANDIDATE")
    chosen = [item for item in candidates if item["name"] in requested] if requested else candidates
    include_properties = bool(requested)
    described, pending = {}, [item["type"] for item in chosen] if include_properties else []
    while pending:
        qualified = pending.pop()
        if qualified in described or qualified.startswith("Edm."):
            continue
        node = types.get(qualified)
        if node is None:
            raise SchemaError("METADATA_TYPE_REFERENCE_MISSING")
        description = {"kind": local_tag(node), "properties": []}
        described[qualified] = description
        if node.get("BaseType"):
            base = canonical(node.get("BaseType"))
            description["base_type"] = base
            pending.append(base)
        for child in node:
            if local_tag(child) == "Property":
                name, declared = identifier(child.get("Name")), child.get("Type")
                reference = canonical(declared)
                description["properties"].append({"name": name, "type": declared})
                pending.append(reference)
    result = {
        "kind": "schema_only",
        "notice": "Candidates by schema names only; payroll source and semantics are NOT verified.",
        "candidate_count": len(candidates),
        "entity_sets": sorted(chosen, key=lambda item: item["name"])[:100],
        "truncated": len(chosen) > 100,
        "properties_included": include_properties,
        "next_step": "Use --entity NAME for selected candidate details." if not include_properties else "Verify source semantics before implementing import.",
        "types": dict(sorted(described.items())),
    }
    if len(serialize_result(result).encode()) > MAX_OUTPUT_BYTES:
        raise SchemaError("SCHEMA_OUTPUT_LIMIT")
    return result


def serialize_result(result):
    return json.dumps(result, ensure_ascii=False, indent=2) + "\n"


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--app-dir", required=True)
    parser.add_argument("--entity", action="append", default=[])
    args = parser.parse_args(argv)
    def timed_out(signum, frame):
        raise SchemaError("METADATA_TOTAL_TIMEOUT")
    try:
        signal.signal(signal.SIGALRM, timed_out)
        signal.alarm(TOTAL_TIMEOUT_SECONDS)
        from dotenv import dotenv_values
        config = {**dotenv_values(Path(args.app_dir) / ".env"), **os.environ}
        result = describe_metadata(fetch_metadata(config), entity_names=args.entity)
        sys.stdout.write(serialize_result(result))
        return 0
    except Exception as exc:
        code = str(exc) if isinstance(exc, SchemaError) else "METADATA_CHECK_FAILED"
        print(json.dumps({"error": code}))
        return 1
    finally:
        signal.alarm(0)


if __name__ == "__main__":
    sys.exit(main())
