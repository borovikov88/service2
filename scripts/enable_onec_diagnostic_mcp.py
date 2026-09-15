#!/usr/bin/env python3
"""Enable the reviewed 1C Diagnostic MCP flag in an existing production .env.

This helper intentionally changes exactly one non-secret key and never prints the
file contents. It refuses missing/symlinked files and duplicate definitions.
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


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--env-file", required=True)
    args = parser.parse_args()
    changed = enable_flag(Path(args.env_file))
    print("Diagnostic MCP activation flag enabled." if changed else "Diagnostic MCP activation flag already enabled.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
