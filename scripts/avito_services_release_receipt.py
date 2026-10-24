"""Strict public projection of one approved services draft-photo operation."""
import json
import sys

try:
    from scripts.avito_readonly_receipt import (
        require, integer, publication_release, approved_photo, release_timestamp, MAX_BYTES,
    )
except ModuleNotFoundError as exc:
    if exc.name not in {"scripts", "scripts.avito_readonly_receipt"}:
        raise
    from avito_readonly_receipt import (
        require, integer, publication_release, approved_photo, release_timestamp, MAX_BYTES,
    )

FRAME = "AVITO_SERVICES_RELEASE_OPERATION_RECEIPT "
GENERATION = "services-v15-20261024-r1"
SOURCE_PIN_FRAME = "AVITO_SERVICES_SOURCE_PIN_RECEIPT "
SOURCE_PIN_GENERATION = "services-source-pin-r2-20261024"
CODES = {
    "release_scope_invalid", "release_generation_invalid", "release_actor_invalid",
    "release_account_mismatch", "release_preflight_failed", "release_photo_source_unavailable",
    "release_busy", "release_photo_pending", "release_completed", "release_photo_failed",
    "release_photo_unknown", "release_binding_changed", "release_media_receipt_invalid",
    "release_internal_error",
}


def photo_proof(value):
    require(isinstance(value, dict) and type(value.get("connection_id")) is int
            and value["connection_id"] == 4 and value.get("account_id") == "446312402")
    result = {"connection_id": 4, "account_id": "446312402"}
    result["checked_at"] = release_timestamp(value.get("checked_at"))
    require(result["checked_at"] is not None)
    result["source"] = approved_photo(value.get("source"))
    require("error" not in result["source"])
    for key in ("upload_saved", "private_preview_verified", "cover_order_saved",
                "original_draft_restored", "published_unchanged"):
        require(value.get(key) is True)
        result[key] = True
    for key in ("image_publicly_available", "browser_clicks_verified", "provider_content_verified"):
        require(value.get(key) is False)
        result[key] = False
    for key in ("revision_before", "revision_after", "edit_version_id", "restore_version_id"):
        result[key] = integer(value.get(key), 1000000000)
        require(result[key] > 0)
    require(result["revision_after"] == result["revision_before"] + 2
            and result["edit_version_id"] != result["restore_version_id"])
    return result


def normalize_r1(value):
    require(isinstance(value, dict) and value.get("schema_version") == 1
            and value.get("kind") == "avito_services_release_operation"
            and value.get("generation") == GENERATION
            and type(value.get("connection_id")) is int and value["connection_id"] == 4
            and value.get("account_id") == "446312402"
            and type(value.get("expected_published_revision")) is int
            and value["expected_published_revision"] == 15)
    require(value.get("state") in {"blocked", "busy", "completed", "unknown", "failed"}
            and value.get("code") in CODES)
    result = {key: value[key] for key in (
        "schema_version", "kind", "generation", "connection_id", "account_id",
        "expected_published_revision", "state", "code")}
    result["prepared_owner_page"] = "/avito/4/services-feed/"
    result["checked_at"] = release_timestamp(value.get("checked_at"))
    require(result["checked_at"] is not None)
    for key, project in (("publication_preflight", publication_release),
                         ("photo_source", approved_photo), ("photo_proof", photo_proof)):
        raw = value.get(key)
        result[key] = project(raw) if raw is not None else None
    if result["state"] == "completed":
        require(result["code"] == "release_completed" and result["photo_proof"] is not None
                and result["publication_preflight"] is not None and result["photo_source"] is not None)
    else:
        require(result["code"] != "release_completed")
    if result["publication_preflight"] is not None and "error" not in result["publication_preflight"]:
        require(result["publication_preflight"]["revision"] == 15)
    require(len(json.dumps(result, ensure_ascii=False).encode()) <= MAX_BYTES)
    return result



SOURCE_PIN_CODES = {
    "source_pin_generation_invalid", "source_pin_scope_invalid", "source_pin_actor_invalid",
    "source_pin_source_changed", "source_pin_pending_draft", "source_pin_photo_required",
    "source_pin_busy", "source_pin_inventory_changed", "source_pin_binding_changed",
    "source_pin_already_bound", "source_pin_published", "source_pin_already_published",
    "source_pin_http_unconfirmed", "source_pin_provider_unavailable", "source_pin_internal_error",
    "source_pin_ledger_invalid",
}


def normalize_source_pin(value):
    require(isinstance(value, dict) and value.get("schema_version") == 1
            and value.get("kind") == "avito_services_source_pin"
            and value.get("generation") == SOURCE_PIN_GENERATION
            and type(value.get("connection_id")) is int and value["connection_id"] == 4
            and value.get("account_id") == "446312402"
            and type(value.get("source_revision")) is int and value["source_revision"] == 15)
    require(value.get("state") in {
        "blocked", "busy", "published", "already_published", "already_bound", "unknown", "failed"})
    require(value.get("code") in SOURCE_PIN_CODES)
    result = {key: value[key] for key in (
        "schema_version", "kind", "generation", "connection_id", "account_id",
        "source_revision", "state", "code")}
    result["prepared_owner_page"] = "/avito/4/services-feed/"
    result["checked_at"] = release_timestamp(value.get("checked_at"))
    require(result["checked_at"] is not None)
    raw_photo, raw_publication = value.get("photo_before_publication"), value.get("publication")
    result["photo_before_publication"] = photo_proof(raw_photo) if raw_photo is not None else None
    result["publication"] = publication_release(raw_publication) if raw_publication is not None else None
    for key in ("same_content", "all_ids_active"):
        item = value.get(key)
        require(item is None or type(item) is bool)
        result[key] = item
    excluded = value.get("excluded_count")
    result["excluded_count"] = integer(excluded, 200) if excluded is not None else None
    if result["state"] in {"published", "already_published"}:
        require(result["same_content"] is True and result["all_ids_active"] is True
                and result["excluded_count"] == 0 and result["photo_before_publication"] is not None)
        publication = result["publication"]
        require(publication is not None and "error" not in publication
                and publication["binding_present"] is True and publication["current_publication"] is True
                and publication["snapshot_valid"] is True and publication["facts"]["file_published"] is True)
    if result["state"] == "already_bound":
        publication = result["publication"]
        require(publication is not None and "error" not in publication
                and publication["binding_present"] is True and publication["current_publication"] is True)
    require(len(json.dumps(result, ensure_ascii=False).encode()) <= MAX_BYTES)
    return result


def normalize(value):
    if isinstance(value, dict) and value.get("kind") == "avito_services_source_pin":
        return normalize_source_pin(value)
    return normalize_r1(value)


def main():
    try:
        with open(sys.argv[1], "rb") as handle:
            raw = handle.read(MAX_BYTES + 1)
        require(len(raw) <= MAX_BYTES)
        lines = raw.decode("utf-8").splitlines()
        frames = [(prefix, line[len(prefix):]) for line in lines
                  for prefix in (FRAME, SOURCE_PIN_FRAME) if line.startswith(prefix)]
        require(len(frames) == 1)
        prefix, payload = frames[0]
        value = json.loads(payload)
        expected_kind = ("avito_services_source_pin" if prefix == SOURCE_PIN_FRAME
                         else "avito_services_release_operation")
        require(isinstance(value, dict) and value.get("kind") == expected_kind)
        result = normalize(value)
    except (OSError, ValueError, TypeError, KeyError, IndexError, UnicodeError):
        print("AVITO_SERVICES_RELEASE_UNAVAILABLE receipt_invalid")
        return 0
    output = ("AVITO_SERVICES_SOURCE_PIN_RESULT " if result["kind"] == "avito_services_source_pin"
              else "AVITO_SERVICES_RELEASE_OPERATION_RESULT ")
    print(output + json.dumps(result, ensure_ascii=False, allow_nan=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
