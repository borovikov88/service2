#!/usr/bin/env python3
"""Read only 1C metadata; report candidate payroll schema, never payroll rows."""

import argparse
import base64
import http.client
import json
import os
from pathlib import Path
import re
import signal
import socket
import ssl
import sys
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, HTTPSHandler, Request, build_opener
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
ERROR_CODES = {
    "INVALID_HTTPS_ODATA_CONFIG", "INVALID_ODATA_CREDENTIAL_CONFIG",
    "METADATA_HTTP_NOT_200", "METADATA_SIZE_LIMIT", "METADATA_REQUIRES_UTF8",
    "METADATA_DTD_FORBIDDEN", "METADATA_INVALID_XML", "METADATA_EDM_SCHEMA_MISSING",
    "METADATA_DUPLICATE_TYPE", "METADATA_TYPE_REFERENCE_MISSING",
    "INVALID_SCHEMA_IDENTIFIER", "REQUESTED_ENTITY_NOT_A_CANDIDATE",
    "SCHEMA_OUTPUT_LIMIT", "METADATA_TOTAL_TIMEOUT", "METADATA_CHECK_FAILED",
}
STAGES = {
    "runtime_setup", "dependencies", "config_read", "metadata_config",
    "metadata_request", "metadata_open", "metadata_read", "metadata_get",
    "schema_parse", "output", "tls_config",
}
REASONS = {
    "DNS", "TLS_CERTIFICATE", "TLS", "TIMEOUT", "CONNECTION_REFUSED",
    "CONNECTION", "NETWORK", "NETWORK_IO", "CONFIG_IO", "IO",
    "MISSING_DOTENV", "DEPENDENCY_UNAVAILABLE", "ENCODING", "INVALID_REQUEST",
    "MALFORMED_XML", "UNEXPECTED_ERROR", "VALIDATION",
}


class SchemaError(Exception):
    """Only fixed, non-sensitive diagnostic codes are exposed to the user."""

    def __init__(self, code, *, stage=None, reason="VALIDATION", verify_code=None):
        super().__init__(code)
        self.code, self.stage, self.reason = code, stage, reason
        self.verify_code = verify_code


def certificate_verify_code(exc):
    for _ in range(3):
        if not isinstance(exc, URLError) or not isinstance(exc.reason, BaseException):
            break
        exc = exc.reason
    if isinstance(exc, (ssl.SSLCertVerificationError, SchemaError)):
        code = getattr(exc, "verify_code", None)
        if type(code) is int and 0 <= code <= 2147483647:
            return code
    return None


def safe_reason(exc, stage):
    network_wrapper = isinstance(exc, URLError)
    for _ in range(3):
        if not isinstance(exc, URLError) or not isinstance(exc.reason, BaseException):
            break
        exc = exc.reason
    if isinstance(exc, socket.gaierror):
        return "DNS"
    if isinstance(exc, ssl.SSLCertVerificationError):
        return "TLS_CERTIFICATE"
    if isinstance(exc, ssl.SSLError):
        return "TLS"
    if isinstance(exc, TimeoutError):
        return "TIMEOUT"
    if isinstance(exc, ConnectionRefusedError):
        return "CONNECTION_REFUSED"
    if isinstance(exc, ConnectionError):
        return "CONNECTION"
    if isinstance(exc, ModuleNotFoundError):
        return "MISSING_DOTENV" if exc.name == "dotenv" else "DEPENDENCY_UNAVAILABLE"
    if isinstance(exc, ImportError):
        return "DEPENDENCY_UNAVAILABLE"
    if isinstance(exc, UnicodeError):
        return "ENCODING"
    if isinstance(exc, ET.ParseError):
        return "MALFORMED_XML"
    if isinstance(exc, (http.client.InvalidURL, ValueError)) and stage in {
        "metadata_config", "metadata_request", "metadata_open", "metadata_get",
    }:
        return "INVALID_REQUEST"
    if network_wrapper:
        return "NETWORK"
    if isinstance(exc, OSError):
        if stage == "config_read":
            return "CONFIG_IO"
        return "NETWORK_IO" if stage.startswith("metadata_") else "IO"
    return "UNEXPECTED_ERROR"


def safe_diagnostic(exc, stage):
    code, reason = "METADATA_CHECK_FAILED", safe_reason(exc, stage)
    if isinstance(exc, SchemaError):
        candidate = exc.code
        if isinstance(candidate, str) and (
            candidate in ERROR_CODES or re.fullmatch(r"METADATA_HTTP_[1-5][0-9]{2}", candidate)
        ):
            code = candidate
        stage = exc.stage if isinstance(exc.stage, str) and exc.stage in STAGES else stage
        reason = exc.reason if isinstance(exc.reason, str) and exc.reason in REASONS else "UNEXPECTED_ERROR"
    result = {"error": code, "stage": stage if isinstance(stage, str) and stage in STAGES else "runtime_setup", "reason": reason}
    verify_code = certificate_verify_code(exc)
    if reason == "TLS_CERTIFICATE" and verify_code is not None:
        result["verify_code"] = verify_code
    return result


def at_stage(stage, operation):
    try:
        return operation()
    except SchemaError as exc:
        if exc.stage is None:
            exc.stage = stage
        raise
    except HTTPError as exc:
        code = f"METADATA_HTTP_{exc.code}" if type(exc.code) is int and 100 <= exc.code <= 599 else "METADATA_HTTP_NOT_200"
        raise SchemaError(code, stage=stage) from None
    except Exception as exc:
        raise SchemaError("METADATA_CHECK_FAILED", stage=stage, reason=safe_reason(exc, stage), verify_code=certificate_verify_code(exc)) from None


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
    url = at_stage("metadata_config", lambda: metadata_url(config.get("ONEC_ODATA_BASE_URL") or ""))
    username = config.get("ONEC_ODATA_USERNAME") or ""
    password = config.get("ONEC_ODATA_PASSWORD") or ""
    if bool(username) != bool(password) or ":" in username:
        raise SchemaError("INVALID_ODATA_CREDENTIAL_CONFIG", stage="metadata_config")
    request = at_stage("metadata_request", lambda: Request(url, headers={"Accept": "application/xml"}, method="GET"))
    if username:
        token = at_stage("metadata_request", lambda: base64.b64encode(f"{username}:{password}".encode()).decode("ascii"))
        request.add_header("Authorization", f"Basic {token}")
    if opener is None:
        # Django settings load .env into os.environ. This standalone probe merges
        # it without mutating process state, so pass its configured CA paths
        # explicitly. No custom CA keeps Python's default trust store.
        context = at_stage("tls_config", lambda: ssl.create_default_context(
            cafile=config.get("SSL_CERT_FILE") or None,
            capath=config.get("SSL_CERT_DIR") or None,
        ))
        client = at_stage("metadata_request", lambda: build_opener(
            NoRedirectHandler(), HTTPSHandler(context=context),
        ))
    else:
        client = opener
    with at_stage("metadata_open", lambda: client.open(request, timeout=TIMEOUT_SECONDS)) as response:
        if response.status != 200:
            raise SchemaError("METADATA_HTTP_NOT_200", stage="metadata_open")
        raw = at_stage("metadata_read", lambda: response.read(MAX_BYTES + 1))
    if len(raw) > MAX_BYTES:
        raise SchemaError("METADATA_SIZE_LIMIT", stage="metadata_read")
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
        raise SchemaError("METADATA_TOTAL_TIMEOUT", reason="TIMEOUT")
    stage = "runtime_setup"
    try:
        signal.signal(signal.SIGALRM, timed_out)
        signal.alarm(TOTAL_TIMEOUT_SECONDS)
        stage = "dependencies"
        from dotenv import dotenv_values
        stage = "config_read"
        config = {**dotenv_values(Path(args.app_dir) / ".env"), **os.environ}
        stage = "metadata_get"
        raw = fetch_metadata(config)
        stage = "schema_parse"
        result = describe_metadata(raw, entity_names=args.entity)
        stage = "output"
        sys.stdout.write(serialize_result(result))
        return 0
    except Exception as exc:
        try:
            print(json.dumps(safe_diagnostic(exc, stage)))
        except OSError:
            pass
        return 1
    finally:
        signal.alarm(0)


if __name__ == "__main__":
    sys.exit(main())
