"""Conservative CI selection and a fail-closed aggregate release gate.

Only documentation, the isolated calculator and known UI directories opt out
of the full server/MySQL suite. Python, SQL, shared UI, CI policy, dependencies,
renames to unknown paths and unavailable diff evidence all select full CI.
"""
import argparse
import json
import os
from pathlib import Path, PurePosixPath
import re
import subprocess

BROWSER_GROUPS = ("shared", "commercial", "release", "material")
DOMAINS = frozenset(("crm", "avito"))
JOBS = ("scope", "checks", "server", "browser", "mysql")


def plan(mode="full", domains=()):
    domains = sorted(set(domains))
    if mode not in ("full", "docs", "calculator", "ui"):
        raise ValueError("Unknown CI mode")
    if (mode == "ui" and (not domains or not set(domains) <= DOMAINS)) or (mode != "ui" and domains):
        raise ValueError("Invalid CI domains")
    # CRM integrations include Avito attribution and owner/call/contact privacy.
    # Run both clusters for UI changes rather than guessing narrower dependencies.
    if mode == "ui":
        domains = sorted(DOMAINS)
    browsers = list(BROWSER_GROUPS) if mode in ("full", "ui") else []
    return {
        "mode": mode, "domains": domains,
        "server": mode in ("full", "ui"), "mysql": mode == "full",
        "browser": bool(browsers),
        "browser_matrix": {"group": browsers or ["none"]},
    }


def path_kind(path):
    parts = PurePosixPath(path)
    if not path or parts.is_absolute() or ".." in parts.parts or "\\" in path:
        return "full"
    if path in ("AGENTS.md", "README.md", "CONTRIBUTING.md") or (
            path.startswith("docs/") and parts.suffix in (".md", ".rst", ".txt")):
        return "docs"
    if path.startswith("tools/aqualine22-calculator/") and parts.suffix in (".mjs", ".js", ".json", ".html", ".css", ".md"):
        return "calculator"
    if path.startswith(("pool_service/static/pool_service/crm_live/", "pool_service/templates/pool_service/crm_live/")) and parts.suffix in (".js", ".css", ".html"):
        return "crm"
    if path.startswith("pool_service/templates/pool_service/avito/") and parts.suffix == ".html":
        return "avito"
    return "full"


def classify(paths):
    kinds = {path_kind(path) for path in paths}
    if not kinds or "full" in kinds:
        return plan()
    kinds.discard("docs")
    if not kinds:
        return plan("docs")
    if kinds == {"calculator"}:
        return plan("calculator")
    if kinds <= DOMAINS:
        return plan("ui", kinds)
    return plan()


def git(*args):
    return subprocess.check_output(["git", *args], stderr=subprocess.DEVNULL)


def event_plan(env, payload, git_reader=git):
    """Use the complete local Git diff, not GitHub's truncated path filters."""
    try:
        head = env["GITHUB_SHA"]
        if not re.fullmatch(r"[0-9a-f]{40}", head):
            return plan()
        if git_reader("rev-parse", "HEAD").decode().strip() != head:
            raise ValueError("CI checkout mismatch")
        event = env.get("GITHUB_EVENT_NAME")
        if event == "pull_request":
            base = payload["pull_request"]["base"]["sha"]
        elif event == "push" and env.get("GITHUB_REF") == "refs/heads/main":
            base = payload["before"]
        else:
            return plan()
        if not re.fullmatch(r"[0-9a-f]{40}", base or "") or base == "0" * 40:
            return plan()
        git_reader("merge-base", "--is-ancestor", base, head)
        changed = git_reader("diff", "--no-renames", "--name-only", "-z", base, head)
        return classify([p for p in changed.decode("utf-8").split("\0") if p])
    except (KeyError, TypeError, ValueError, UnicodeError, subprocess.SubprocessError):
        return plan()


def validate_gate(needs):
    if set(needs) != set(JOBS):
        raise ValueError("Missing or unexpected CI jobs")
    if needs["scope"]["result"] != "success" or needs["checks"]["result"] != "success":
        raise ValueError("CI planning or policy/configuration checks did not pass")
    outputs = needs["scope"]["outputs"]
    expected = plan(outputs["ci_mode"], json.loads(outputs["ci_domains"]))
    if json.loads(outputs["ci_browser_matrix"]) != expected["browser_matrix"]:
        raise ValueError("Incomplete browser matrix")
    reuse = outputs["ci_reuse"]
    if reuse not in ("true", "false"):
        raise ValueError("Missing reuse decision")
    for job in ("server", "browser", "mysql"):
        if outputs["ci_" + job] != str(expected[job]).lower():
            raise ValueError("Inconsistent CI plan")
        required = expected[job] and reuse != "true"
        if needs[job]["result"] != ("success" if required else "skipped"):
            raise ValueError("Required CI job failed, was cancelled or unexpectedly skipped: " + job)
    return expected["mode"]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("plan", "gate"))
    args = parser.parse_args()
    if args.command == "gate":
        mode = validate_gate(json.loads(os.environ["CI_NEEDS_JSON"]))
        print("CI_GATE_VERIFIED mode=" + mode)
        return
    try:
        payload = json.loads(Path(os.environ["GITHUB_EVENT_PATH"]).read_text(encoding="utf-8"))
    except (OSError, KeyError, ValueError):
        payload = {}
    selected = event_plan(os.environ, payload)
    with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as output:
        for name, value in selected.items():
            if isinstance(value, bool):
                value = str(value).lower()
            elif not isinstance(value, str):
                value = json.dumps(value, separators=(",", ":"))
            output.write(f"ci_{name}={value}\n")
    print("CI_PLAN " + json.dumps(selected, sort_keys=True))


if __name__ == "__main__":
    main()
