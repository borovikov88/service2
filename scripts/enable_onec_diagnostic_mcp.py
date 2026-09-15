#!/usr/bin/env python3
"""Configure the reviewed 1C Diagnostic MCP in an existing production .env.

The helper changes only the three non-secret Diagnostic MCP settings required for
production, never prints .env contents, preserves the original .env inode and all
bytes outside the target setting lines, and supports a versioned one-time marker.
"""

from __future__ import annotations

import argparse
import fcntl
import os
from pathlib import Path
import re
import tempfile


ENABLED_KEY = "ADVISOR_ONEC_DIAGNOSTIC_MCP_ENABLED"
RESOURCE_KEY = "ADVISOR_ONEC_DIAGNOSTIC_MCP_RESOURCE_URL"
ISSUER_KEY = "ADVISOR_ONEC_DIAGNOSTIC_MCP_AUTH_ISSUER"
KEY = ENABLED_KEY  # Backward-compatible import for older tests/tools.

PRODUCTION_RESOURCE_URL = "https://service2.aqualine22.ru/mcp/1c"
PRODUCTION_ISSUER_URL = "https://service2.aqualine22.ru/onec-diagnostic"
PRODUCTION_CONFIGURATION = (
    (ENABLED_KEY, "true"),
    (RESOURCE_KEY, PRODUCTION_RESOURCE_URL),
    (ISSUER_KEY, PRODUCTION_ISSUER_URL),
)

LEGACY_MARKER = b"activated\n"
CURRENT_MARKER = b"activated-v2\n"
MAX_ENV_BYTES = 1024 * 1024
MAX_MARKER_BYTES = 64


def _write_all(fd: int, payload: bytes) -> None:
    view = memoryview(payload)
    while view:
        written = os.write(fd, view)
        if written <= 0:
            raise OSError("Short write while updating production .env")
        view = view[written:]


def _line_re(key: str):
    return re.compile(
        rb"(?m)^[ \t]*(?:export[ \t]+)?"
        + re.escape(key.encode("ascii"))
        + rb"[ \t]*=[^\r\n]*(?P<ending>\r\n|\n|\Z)"
    )


def _render_setting(original: bytes, key: str, value: str) -> tuple[bytes, bool]:
    pattern = _line_re(key)
    matches = list(pattern.finditer(original))
    if len(matches) > 1:
        raise RuntimeError(f"{key} is defined more than once; refusing ambiguous update.")

    desired = f"{key}={value}".encode("ascii")
    if matches:
        match = matches[0]
        replacement = desired + match.group("ending")
        if match.group(0) == replacement:
            return original, False
        return original[: match.start()] + replacement + original[match.end() :], True

    separators = re.findall(rb"\r\n|\n", original)
    newline = separators[-1] if separators else b"\n"
    if not original:
        return desired + newline, True
    separator = b"" if original.endswith(b"\n") else newline
    return original + separator + desired + newline, True


def configure_production(path: Path) -> bool:
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
        original_bytes = bytes(original)

        rendered = original_bytes
        changed = False
        for key, value in PRODUCTION_CONFIGURATION:
            rendered, setting_changed = _render_setting(rendered, key, value)
            changed = changed or setting_changed
        if not changed:
            return False
        if len(rendered) > MAX_ENV_BYTES:
            raise RuntimeError("Updated production .env would exceed the safety limit.")

        # Keep the original inode instead of os.replace(): ownership, group and
        # POSIX ACLs remain attached to the same file. On a write error, restore
        # the exact original bytes before failing.
        try:
            os.lseek(fd, 0, os.SEEK_SET)
            _write_all(fd, rendered)
            os.ftruncate(fd, len(rendered))
            os.fsync(fd)
        except Exception:
            try:
                os.lseek(fd, 0, os.SEEK_SET)
                _write_all(fd, original_bytes)
                os.ftruncate(fd, len(original_bytes))
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


def _read_marker(marker_file: Path) -> bytes | None:
    if marker_file.is_symlink():
        raise RuntimeError("Activation marker must not be a symlink.")
    if not marker_file.exists():
        return None
    if not marker_file.is_file():
        raise RuntimeError("Activation marker must be a regular file.")
    content = marker_file.read_bytes()
    if len(content) > MAX_MARKER_BYTES:
        raise RuntimeError("Activation marker is unexpectedly large.")
    if content not in {LEGACY_MARKER, CURRENT_MARKER}:
        raise RuntimeError("Activation marker has an unknown version; refusing update.")
    return content


def _write_current_marker(marker_file: Path) -> None:
    marker_file.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(prefix=f".{marker_file.name}.", dir=str(marker_file.parent))
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(CURRENT_MARKER)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary_path, 0o600)
        os.replace(temporary_path, marker_file)
    finally:
        if temporary_path.exists():
            temporary_path.unlink()


def activate_once(env_file: Path, marker_file: Path | None = None) -> str:
    marker = _read_marker(marker_file) if marker_file is not None else None
    if marker == CURRENT_MARKER:
        return "already_marked"

    changed = configure_production(env_file)

    if marker_file is not None:
        _write_current_marker(marker_file)

    if marker == LEGACY_MARKER:
        return "configured" if changed else "marker_upgraded"
    return "configured" if changed else "already_configured"


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
        "configured": "Diagnostic MCP production configuration applied.",
        "already_configured": "Diagnostic MCP production configuration already correct.",
        "marker_upgraded": "Diagnostic MCP production configuration verified; activation marker upgraded.",
        "already_marked": "Diagnostic MCP activation already completed earlier; no changes made.",
    }
    print(messages[result])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
