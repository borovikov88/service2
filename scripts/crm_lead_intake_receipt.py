#!/usr/bin/env python3
"""Print only the validated aggregate receipt, never captured SSH output."""
import json
from pathlib import Path
import re
import sys

PREFIX = "CRM_LEAD_INTAKE_ACCESS "
APPROVAL_ID = "crm-lead-intake-aqualine-20261026"
FIELDS = {"status", "expected_sha", "run_id", "run_attempt", "approval_id",
          "crm_v2_read", "crm_v2_write", "requester_access", "managers_access_verified",
          "eligible_manager_count", "granted_manager_count", "excluded_manager_count",
          "pipelines_created", "stages_created", "crm_intake_process", "starts_at"}
REMOTE_CODES = {"invalid_arguments", "invalid_sha", "invalid_path", "invalid_execution",
                "invalid_checkout", "runtime_layout", "invalid_lock", "deployment_busy",
                "head_mismatch", "dirty_checkout", "untracked_code", "approval_marker_missing",
                "origin_mismatch", "main_unavailable", "main_mismatch", "command_failed"}
COMMAND_CODES = {"crm_access_protected_deploy_required", "crm_access_approval_required",
                 "crm_access_execution_context_required", "crm_access_commit_unavailable",
                 "crm_access_commit_mismatch", "crm_access_organization_unavailable",
                 "crm_access_membership_changed", "crm_access_requester_or_organization_ineligible",
                 "crm_access_readback_failed", "crm_access_operation_failed", "crm_access_previous_cohort_unavailable", "crm_access_intake_state_requires_review"}


def unique_object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError()
        value[key] = item
    return value


def normalize(text, expected, run_id, run_attempt):
    if (len(text) > 16384 or not re.fullmatch(r"[0-9a-f]{40}", expected)
            or not re.fullmatch(r"[1-9][0-9]{0,19}", run_id)
            or not re.fullmatch(r"[1-9][0-9]{0,5}", run_attempt)):
        raise ValueError()
    lines = [line for line in text.splitlines() if line.startswith(PREFIX)]
    if len(lines) != 1:
        raise ValueError()
    value = json.loads(lines[0][len(PREFIX):], object_pairs_hook=unique_object)
    if not isinstance(value, dict) or set(value) != FIELDS:
        raise ValueError()
    if (value["status"] not in {"applied", "already_applied"}
            or value["expected_sha"] != expected or value["approval_id"] != APPROVAL_ID):
        raise ValueError()
    for field, number in (("run_id", run_id), ("run_attempt", run_attempt)):
        if type(value[field]) is not int or value[field] != int(number):
            raise ValueError()
    flags = ("crm_v2_read", "crm_v2_write", "crm_intake_process", "requester_access", "managers_access_verified")
    for field in flags:
        if type(value[field]) is not bool:
            raise ValueError()
    if not isinstance(value["starts_at"], str) or not re.fullmatch(r"[0-9T:.+Z-]{20,40}", value["starts_at"]):
        raise ValueError()
    counters = ("eligible_manager_count", "granted_manager_count", "excluded_manager_count",
                "pipelines_created", "stages_created")
    for field in counters:
        if type(value[field]) is not int or not 0 <= value[field] <= 10000:
            raise ValueError()
    if value["granted_manager_count"] > value["eligible_manager_count"]:
        raise ValueError()
    if value["status"] == "applied" and not all(value[field] for field in flags):
        raise ValueError()
    if value["status"] == "already_applied" and any(value[field] for field in (
            "granted_manager_count", "pipelines_created", "stages_created")):
        raise ValueError()
    return value


def failure(text):
    if len(text) <= 16384:
        commands = [match[1] for line in text.splitlines()
                    if (match := re.fullmatch(r"CommandError: (crm_access_[a-z_]+)", line))]
        if len(commands) == 1 and commands[0] in COMMAND_CODES:
            return "CRM_LEAD_INTAKE_ACCESS_BLOCKED code=" + commands[0]
        lines = [line for line in text.splitlines() if line.startswith("CRM_LEAD_INTAKE_ACCESS_REMOTE_BLOCKED ")]
        if len(lines) == 1:
            match = re.fullmatch(r"CRM_LEAD_INTAKE_ACCESS_REMOTE_BLOCKED code=([a-z_]+)", lines[0])
            if match and match[1] in REMOTE_CODES:
                return "CRM_LEAD_INTAKE_ACCESS_BLOCKED code=" + match[1]
    return "CRM_LEAD_INTAKE_ACCESS_BLOCKED code=command_or_transport_failed"


def main(args):
    try:
        if len(args) == 2 and args[0] == "--failure":
            with Path(args[1]).open() as stream:
                print(failure(stream.read(16385)))
            return 2
        if len(args) != 4:
            raise ValueError()
        with Path(args[0]).open() as stream:
            value = normalize(stream.read(16385), *args[1:])
        print(PREFIX + json.dumps(value, sort_keys=True))
        return 0
    except (OSError, ValueError, TypeError, IndexError, KeyError):
        print("CRM_LEAD_INTAKE_ACCESS_BLOCKED code=invalid_receipt")
        return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
