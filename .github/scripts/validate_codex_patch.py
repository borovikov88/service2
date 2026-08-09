#!/usr/bin/env python3
import argparse
import hashlib
import json
from pathlib import Path, PurePosixPath
import subprocess
import sys


MAX_PATCH_BYTES = 5_000_000
MAX_SUMMARY_BYTES = 100_000
EXPECTED_FILES = {
    "codex.patch",
    "codex-final.txt",
    "manifest.json",
    "pr-title.txt",
}
SECURITY_BLOCKED_EXIT = 3


def fail(message, *, security=False):
    prefix = "SECURITY_BLOCKED" if security else "ARTIFACT_INVALID"
    print(f"{prefix}: {message}", file=sys.stderr)
    raise SystemExit(SECURITY_BLOCKED_EXIT if security else 2)


def forbidden_reason(path):
    pure = PurePosixPath(path)
    parts = pure.parts
    lower_parts = [part.lower() for part in parts]
    basename = pure.name.lower()
    lower = path.lower()
    if lower.startswith(".github/workflows/") or lower.startswith(".github/actions/"):
        return "GitHub automation path"
    if lower.startswith(".github/"):
        return "protected GitHub configuration path"
    if ".gitmodules" in lower_parts:
        return "Git submodule configuration"
    if any(part.startswith(".git") and part != ".gitignore" for part in lower_parts):
        return "Git control file"
    if any(part == ".env" or part.startswith(".env.") for part in lower_parts):
        return "environment file"
    if any(part == "deploy" or part.startswith("deploy.") for part in lower_parts):
        return "deployment script"
    if basename in {"update.sh", "passenger_wsgi.py"}:
        return "production runtime script"
    if lower in {"service_site/wsgi.py", "service_site/asgi.py"}:
        return "production application entry point"
    return ""


def validate_relative_path(raw_path):
    if not raw_path or "\\" in raw_path or "\x00" in raw_path:
        fail("unsafe patch path", security=True)
    path = PurePosixPath(raw_path)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        fail("path escapes repository", security=True)
    reason = forbidden_reason(raw_path)
    if reason:
        fail(f"Codex attempted to modify protected file ({reason}): {raw_path}", security=True)


def patch_paths(patch_file):
    completed = subprocess.run(
        ["git", "apply", "--numstat", "-z", "--binary", str(patch_file)],
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if completed.returncode != 0:
        fail("git could not inspect patch")
    fields = completed.stdout.split(b"\0")
    paths = []
    index = 0
    while index < len(fields):
        entry = fields[index]
        index += 1
        if not entry:
            continue
        pieces = entry.split(b"\t", 2)
        if len(pieces) != 3:
            fail("unexpected patch numstat format")
        encoded_path = pieces[2]
        if encoded_path:
            candidates = [encoded_path]
        else:
            if index + 1 >= len(fields):
                fail("incomplete rename path data")
            candidates = [fields[index], fields[index + 1]]
            index += 2
        for candidate in candidates:
            try:
                path = candidate.decode("utf-8", errors="strict")
            except UnicodeDecodeError:
                fail("patch path is not UTF-8", security=True)
            validate_relative_path(path)
            paths.append(path)
    if not paths:
        fail("patch contains no changed paths")
    return sorted(set(paths))


def validate_manifest(artifact_dir, expected):
    actual_files = {item.name for item in artifact_dir.iterdir() if item.is_file()}
    if actual_files != EXPECTED_FILES:
        fail("artifact contains missing or unexpected files")
    if any(item.is_dir() for item in artifact_dir.iterdir()):
        fail("artifact contains unexpected directories")
    if any(item.is_symlink() for item in artifact_dir.iterdir()):
        fail("artifact contains symbolic links", security=True)
    try:
        manifest = json.loads((artifact_dir / "manifest.json").read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        fail("manifest is unreadable")
    expected_keys = {
        "task_reference",
        "launch_token",
        "branch_name",
        "patch_sha256",
        "patch_size",
    }
    if set(manifest) != expected_keys:
        fail("manifest shape is invalid")
    for key in ("task_reference", "launch_token", "branch_name"):
        if manifest.get(key) != expected[key]:
            fail(f"artifact correlation mismatch: {key}", security=True)
    return manifest


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--artifact-dir", required=True)
    parser.add_argument("--task-reference", required=True)
    parser.add_argument("--launch-token", required=True)
    parser.add_argument("--branch-name", required=True)
    args = parser.parse_args()
    artifact_dir = Path(args.artifact_dir).resolve()
    if not artifact_dir.is_dir():
        fail("artifact directory is missing")
    manifest = validate_manifest(
        artifact_dir,
        {
            "task_reference": args.task_reference,
            "launch_token": args.launch_token,
            "branch_name": args.branch_name,
        },
    )
    patch_file = artifact_dir / "codex.patch"
    patch_size = patch_file.stat().st_size
    if patch_size <= 0 or patch_size > MAX_PATCH_BYTES:
        fail("patch size is outside allowed bounds")
    patch_sha = hashlib.sha256(patch_file.read_bytes()).hexdigest()
    if manifest.get("patch_size") != patch_size or manifest.get("patch_sha256") != patch_sha:
        fail("patch digest does not match manifest", security=True)
    if (artifact_dir / "codex-final.txt").stat().st_size > MAX_SUMMARY_BYTES:
        fail("Codex summary is too large")
    title = (artifact_dir / "pr-title.txt").read_bytes()
    try:
        title_text = title.decode("utf-8")
    except UnicodeDecodeError:
        fail("PR title is not UTF-8")
    if not title_text.strip() or len(title) > 255 or "\x00" in title_text:
        fail("PR title is invalid")
    paths = patch_paths(patch_file)
    check = subprocess.run(
        ["git", "apply", "--check", "--binary", str(patch_file)],
        check=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    if check.returncode != 0:
        fail("patch does not apply cleanly to trusted base")
    print(json.dumps({"state": "allowed", "paths": paths}, ensure_ascii=True))


if __name__ == "__main__":
    main()
