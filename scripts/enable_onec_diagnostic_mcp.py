#!/usr/bin/env python3
"""Enable the reviewed 1C Diagnostic MCP flag in an existing production .env.

This helper intentionally changes exactly one non-secret key and never prints the
file contents. It refuses missing/symlinked files and duplicate definitions.
An optional marker makes the activation one-time across later deployments.
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import re
import stat
import tempfile


KEY = "ADVISOR_ONEC_DIAGNOSTIC_MCP_ENABLED"
ENABLED_LINE = f"{KEY}=true"
KEY_RE = re.compile(rf"^\s*(?:export\s+)?{re.escape(KEY)}\s*=")


def enable_flag(path: Path) -> bool:
    if path.is_symlink() or not path.is_file():
        raise RuntimeError("Production .env must be an existing regular non-symlink file.")

    original_stat = path.stat()
    text = path.read_text(encoding="utf-8")
    lines = text.splitlines()
    matches = [index for index, line in enumerate(lines) if KEY_RE.match(line)]
    if len(matches) > 1:
        raise RuntimeError(f"{KEY} is defined more than once; refusing ambiguous update.")

    changed = False
    if matches:
        index = matches[0]
        if lines[index].strip() != ENABLED_LINE:
            lines[index] = ENABLED_LINE
            changed = True
    else:
        lines.append(ENABLED_LINE)
        changed = True

    if not changed:
        return False

    rendered = "\n".join(lines) + "\n"
    fd, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(rendered)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary_path, stat.S_IMODE(original_stat.st_mode))
        os.replace(temporary_path, path)
    finally:
        if temporary_path.exists():
            temporary_path.unlink()
    return True


def activate_once(env_file: Path, marker_file: Path | None = None) -> str:
    if marker_file is not None:
        if marker_file.is_symlink():
            raise RuntimeError("Activation marker must not be a symlink.")
        if marker_file.exists():
            if not marker_file.is_file():
                raise RuntimeError("Activation marker must be a regular file.")
            return "already_marked"

    changed = enable_flag(env_file)

    if marker_file is not None:
        marker_file.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary_name = tempfile.mkstemp(prefix=f".{marker_file.name}.", dir=str(marker_file.parent))
        temporary_path = Path(temporary_name)
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
                handle.write("activated\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(temporary_path, 0o600)
            os.replace(temporary_path, marker_file)
        finally:
            if temporary_path.exists():
                temporary_path.unlink()

    return "enabled" if changed else "already_enabled"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--env-file", required=True)
    parser.add_argument("--marker-file")
    args = parser.parse_args()
    result = activate_once(
        Path(args.env_file),
        Path(args.marker_file) if args.marker_file else None,
    )
    messages = {
        "enabled": "Diagnostic MCP activation flag enabled.",
        "already_enabled": "Diagnostic MCP activation flag already enabled.",
        "already_marked": "Diagnostic MCP activation already completed earlier; no changes made.",
    }
    print(messages[result])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
