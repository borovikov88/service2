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


def normalize(value):
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


def main():
    try:
        with open(sys.argv[1], "rb") as handle:
            raw = handle.read(MAX_BYTES + 1)
        require(len(raw) <= MAX_BYTES)
        lines = raw.decode("utf-8").splitlines()
        frames = [line[len(FRAME):] for line in lines if line.startswith(FRAME)]
        require(len(frames) == 1)
        result = normalize(json.loads(frames[0]))
    except (OSError, ValueError, TypeError, KeyError, IndexError, UnicodeError):
        print("AVITO_SERVICES_RELEASE_UNAVAILABLE receipt_invalid")
        return 0
    print("AVITO_SERVICES_RELEASE_OPERATION_RESULT " +
          json.dumps(result, ensure_ascii=False, allow_nan=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
