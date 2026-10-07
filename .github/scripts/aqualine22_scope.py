"""Isolate calculator-only pushes while preserving the Service2 deployment path."""
import os
from pathlib import Path
import re
import subprocess

CALCULATOR_FILES = {
    ".github/workflows/aqualine22-calculator.yml",
    ".github/scripts/aqualine22_remote.py",
    ".github/scripts/aqualine22_calculator_host.py",
    ".github/scripts/aqualine22_scope.py",
    ".github/tests/test_aqualine22_publication.py",
}
CI_FILE = ".github/workflows/ci-deploy.yml"


def is_calculator(path):
    return path in CALCULATOR_FILES or path.startswith("tools/aqualine22-calculator/")


def classify(paths):
    paths = set(paths)
    if not paths:
        raise ValueError("Empty publication diff; deployment scope is unknown")
    calculator = any(is_calculator(path) for path in paths)
    isolated = calculator and all(is_calculator(path) or path == CI_FILE for path in paths)
    return {"service_deploy": not isolated, "calculator_deploy": calculator}


def event_scope(event, ref, before, head, diff_reader):
    if event == "pull_request":
        return {"service_deploy": False, "calculator_deploy": False}
    if ref != "refs/heads/main":
        raise ValueError("Hosting scope requires main")
    if event == "workflow_dispatch":
        return {"service_deploy": True, "calculator_deploy": False}
    if event != "push" or not all(re.fullmatch(r"[0-9a-f]{40}", sha or "") and sha != "0" * 40 for sha in (before, head)):
        raise ValueError("Missing exact push boundaries; deployment scope is unknown")
    return classify(diff_reader(before, head))


def read_diff(before, head):
    result = subprocess.run(["git", "diff", "--no-renames", "--name-only", "-z", before, head],
                            check=True, capture_output=True)
    return [path for path in result.stdout.decode("utf-8").split("\0") if path]


if __name__ == "__main__":
    scope = event_scope(os.environ["EVENT_NAME"], os.environ["WORKFLOW_REF"],
                        os.environ.get("BEFORE_SHA"), os.environ["GITHUB_SHA"], read_diff)
    with Path(os.environ["GITHUB_OUTPUT"]).open("a") as output:
        for name, value in scope.items():
            output.write(f"{name}={str(value).lower()}\n")
