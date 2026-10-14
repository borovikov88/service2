#!/usr/bin/env python3
"""Require current main and its latest successful production CI before SSH reads."""
import json
import os
import re
import sys
import urllib.request

REPOSITORY = "borovikov88/service2"
MAX_RESPONSE = 1024 * 1024
BASE = "https://api.github.com/repos/" + REPOSITORY


class GateError(Exception):
    pass


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def validate(event, ref, expected, manual, current, runs):
    if event not in {"schedule", "workflow_dispatch", "workflow_run"} or ref != "refs/heads/main":
        raise GateError("audit_untrusted_event")
    if not re.fullmatch(r"[0-9a-f]{40}", expected or ""):
        raise GateError("audit_sha_invalid")
    if event == "workflow_dispatch" and manual != expected:
        raise GateError("audit_manual_sha_mismatch")
    if not isinstance(current, dict) or current.get("commit", {}).get("sha") != expected:
        raise GateError("audit_main_changed")
    if not isinstance(runs, dict) or not isinstance(runs.get("workflow_runs"), list):
        raise GateError("audit_ci_unavailable")
    # API results are newest first. An in-flight/failed newer run must not be
    # hidden by choosing an older green run for the same commit.
    latest = next((run for run in runs["workflow_runs"] if isinstance(run, dict)
                   and run.get("head_sha") == expected and run.get("head_branch") == "main"
                   and run.get("event") in {"push", "workflow_dispatch"}), None)
    if (latest is None or latest.get("status") != "completed" or latest.get("conclusion") != "success"
            or latest.get("path") != ".github/workflows/ci-deploy.yml"
            or latest.get("repository", {}).get("full_name") != REPOSITORY):
        raise GateError("audit_ci_not_successful")
    return expected


def fetch(path, token):
    request = urllib.request.Request(BASE + path, headers={
        "Authorization": "Bearer " + token, "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28", "Accept-Encoding": "identity",
    })
    try:
        with urllib.request.build_opener(NoRedirect()).open(request, timeout=20) as response:
            payload = response.read(MAX_RESPONSE + 1)
        if len(payload) > MAX_RESPONSE:
            raise GateError("audit_ci_response_limit")
        return json.loads(payload)
    except GateError:
        raise
    except Exception:
        raise GateError("audit_ci_unavailable") from None


def main():
    if os.environ.get("GITHUB_REPOSITORY") != REPOSITORY:
        raise GateError("audit_repository_invalid")
    token = os.environ.get("GH_TOKEN", "")
    if not token:
        raise GateError("audit_ci_token_missing")
    sha = validate(os.environ.get("GITHUB_EVENT_NAME"), os.environ.get("GITHUB_REF"),
                   os.environ.get("AVITO_EXPECTED_SHA"), os.environ.get("AVITO_MANUAL_SHA", ""),
                   fetch("/branches/main", token),
                   fetch("/actions/workflows/ci-deploy.yml/runs?branch=main&per_page=25", token))
    print("AVITO_READONLY_MAIN_CI_OK " + sha)


if __name__ == "__main__":
    try:
        main()
    except (GateError, TypeError, KeyError, AttributeError):
        raise SystemExit("AVITO_READONLY_GATE_BLOCKED") from None
