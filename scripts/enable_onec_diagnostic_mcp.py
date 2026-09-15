#!/usr/bin/env python3
"""Enable the reviewed 1C Diagnostic MCP flag in an existing production .env.

This helper intentionally changes exactly one non-secret key and never prints the
file contents. It refuses missing/symlinked files and duplicate definitions.
The .env inode is edited in place so owner/group/mode/ACL metadata are preserved.
An optional marker makes the activation one-time across later deployments.
"""

from __future__ import annotations

import argparse
import fcntl
import os
from pathlib import Path
import re
import tempfile


KEY = "ADVISOR_ONEC_DIAGNOSTIC_MCP_ENABLED"
ENABLED_LINE = f"{KEY}=true"
KEY_RE = re.compile(rf"^\s*(?:export\s+)?{re.escape(KEY)}\s*=")
MAX_ENV_BYTES = 1024 * 1024


def _write_all(fd: int, payload: bytes) -> None:
    view = memoryview(payload)
    while view:
        written = os.write(fd, view)
        if written <= 0:
            raise OSError("Short write while updating production .env")
        view = view[written:]


def enable_flag(path: Path) -> bool:
    flags = os.O_RDWR
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        raise RuntimeError("Production .env must be an existing regular non-symlink file.") from exc

    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        before = os.fstat(fd)
        if not path.is_file() or path.is_symlink():
            raise RuntimeError("Production .env must be an existing regular non-symlink file.")
        if before.st_size > MAX_ENV_BYTES:
            raise RuntimeError("Production .env is unexpectedly large; refusing update.")

        os.lseek(fd, 0, os.SEEK_SET)
        original = bytearray()
        while True:
            chunk = os.read(fd, min(65536, MAX_ENV_BYTES + 1 - len(original)))
            if not chunk:
                break
            original.extend(chunk)
            if len(original) > MAX_ENV_BYTES:
                raise RuntimeError("Production .env is unexpectedly large; refusing update.")
        try:
            text = bytes(original).decode("utf-8")
        except UnicodeDecodeError as exc:
            raise RuntimeError("Production .env must be valid UTF-8.") from exc

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

        rendered = ("\n".join(lines) + "\n").encode("utf-8")
        if len(rendered) > MAX_ENV_BYTES:
            raise RuntimeError("Updated production .env would exceed the safety limit.")

        # Keep the original inode instead of os.replace(): ownership, group and
        # POSIX ACLs therefore remain attached to the same file. If a normal
        # write error occurs, best-effort restoration happens before failing.
        try:
            os.lseek(fd, 0, os.SEEK_SET)
            _write_all(fd, rendered)
            os.ftruncate(fd, len(rendered))
            os.fsync(fd)
        except Exception:
            try:
                os.lseek(fd, 0, os.SEEK_SET)
                _write_all(fd, bytes(original))
                os.ftruncate(fd, len(original))
                os.fsync(fd)
            finally:
                raise

        after = os.fstat(fd)
        if (
            after.st_ino != before.st_ino
            or after.st_uid != before.st_uid
            or after.st_gid != before.st_gid
            or (after.st_mode & 0o7777) != (before.st_mode & 0o7777)
        ):
            raise RuntimeError("Production .env ownership/access metadata changed unexpectedly.")
        return True
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


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
