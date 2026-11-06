"""Check configured hosting access without deploying or loading Django settings."""

import argparse
import importlib.util
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import tempfile
from urllib.parse import urlsplit


REMOTE_CHECK = r"""set -eu
test "$(id -un)" = "$1"
cd -- "$2"
test -d .git
test -f manage.py
test -x ../venv/bin/python
test -d ../tmp && test -w ../tmp
printf 'SSH_KEY_OK\n'
git rev-parse --verify HEAD
../venv/bin/python -B --version
../venv/bin/python -B -I -c 'import django; print("Django", django.get_version())'
printf 'HOSTING_LAYOUT_OK\n'
"""


def validate_config(environ):
    names = (
        "DEPLOY_HOST", "DEPLOY_PORT", "DEPLOY_USER", "DEPLOY_APP_PATH",
        "DEPLOY_SSH_KEY", "DEPLOY_KNOWN_HOSTS", "DEPLOY_HEALTH_URL",
    )
    config = {name: environ.get(name, "") for name in names}
    for name, value in config.items():
        if not value.strip() or "\x00" in value:
            raise ValueError(f"Missing or invalid {name}")
    host = config["DEPLOY_HOST"]
    if len(host) > 253 or any(
        not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?", label)
        for label in host.split(".")
    ):
        raise ValueError("Invalid DEPLOY_HOST")
    port = config["DEPLOY_PORT"]
    if not re.fullmatch(r"[0-9]{1,5}", port) or not 1 <= int(port) <= 65535:
        raise ValueError("Invalid DEPLOY_PORT")
    config["DEPLOY_PORT"] = str(int(port))
    if not re.fullmatch(r"[a-zA-Z_][a-zA-Z0-9_-]{0,63}", config["DEPLOY_USER"]):
        raise ValueError("Invalid DEPLOY_USER")
    path = config["DEPLOY_APP_PATH"]
    if not path.startswith("/") or path == "/" or any(ord(c) < 32 for c in path):
        raise ValueError("Invalid DEPLOY_APP_PATH")
    health = config["DEPLOY_HEALTH_URL"]
    try:
        url = urlsplit(health)
        valid = (
            url.scheme == "https" and url.hostname and url.port != 0
            and url.username is None and url.password is None
            and "?" not in health and "#" not in health
            and not any(c.isspace() or ord(c) < 32 for c in health)
        )
    except ValueError:
        valid = False
    if not valid:
        raise ValueError("Invalid DEPLOY_HEALTH_URL")
    return config


def incident_helper():
    path = Path(__file__).with_name("avito_refresh_incident.py")
    spec = importlib.util.spec_from_file_location("avito_refresh_incident", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return path, module


def incident_output(stdout, helper):
    # Never echo raw remote output in incident mode, even if it is unexpected.
    if len(stdout) > 32768:
        raise ValueError("Invalid incident output")
    lines = [line for line in stdout.splitlines() if line.startswith(helper.MARKER)]
    if len(lines) != 1:
        raise ValueError("Missing incident output")
    try:
        value = json.loads(lines[0][len(helper.MARKER):])
        clean = helper.public_summary(value)
    except (ValueError, TypeError, KeyError):
        raise ValueError("Invalid incident output") from None
    return helper.MARKER + json.dumps(clean, ensure_ascii=True, separators=(",", ":"))


def run_check(environ, *, avito_incident=False):
    config = validate_config(environ)  # Reject all bad configuration before networking.
    host, port = config["DEPLOY_HOST"], config["DEPLOY_PORT"]
    with tempfile.TemporaryDirectory(prefix="hosting-check-") as temporary:
        key = Path(temporary) / "key"
        hosts = Path(temporary) / "known_hosts"
        for path, value in (
            (key, config["DEPLOY_SSH_KEY"]), (hosts, config["DEPLOY_KNOWN_HOSTS"]),
        ):
            path.touch(mode=0o600)
            path.write_text(value.rstrip("\n") + "\n", encoding="utf-8")
        print("Checking private key format", flush=True)
        subprocess.run(
            ["ssh-keygen", "-y", "-P", "", "-f", str(key)], check=True,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10,
        )
        print("Checking pinned host key entry", flush=True)
        matched = subprocess.run(
            ["ssh-keygen", "-F", host if port == "22" else f"[{host}]:{port}",
             "-f", str(hosts)], check=True, stdout=subprocess.PIPE, text=True,
            stderr=subprocess.DEVNULL, timeout=10,
        )
        # -F verifies only the hostname. Parse matched key material separately;
        # an unrelated valid host must not mask a malformed matching entry.
        entries = [line for line in matched.stdout.splitlines()
                   if line.strip() and not line.startswith("#")]
        if not entries:
            raise ValueError("No matching pinned host key")
        matched_key = Path(temporary) / "matched_host_key"
        matched_key.touch(mode=0o600)
        for entry in entries:
            matched_key.write_text(entry + "\n", encoding="utf-8")
            subprocess.run(
                ["ssh-keygen", "-l", "-f", str(matched_key)], check=True,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10,
            )
        remote_command = "bash --noprofile --norc -s -- " + " ".join(shlex.quote(config[name]) for name in (
            "DEPLOY_USER", "DEPLOY_APP_PATH",
        ))
        print("Checking SSH authentication and hosting layout", flush=True)
        remote_check = REMOTE_CHECK
        if avito_incident:
            path, helper = incident_helper()
            remote_check += "\n../venv/bin/python -B -I - <<'AVITO_FIXED_INCIDENT'\n" + path.read_text(encoding="utf-8") + "\nAVITO_FIXED_INCIDENT\n"
        result = subprocess.run(
            ["ssh", "-F", "/dev/null", "-T", "-p", port, "-i", str(key),
             "-o", "BatchMode=yes", "-o", "IdentitiesOnly=yes",
             "-o", "IdentityAgent=none", "-o", "StrictHostKeyChecking=yes",
             "-o", f"UserKnownHostsFile={hosts}", "-o", "GlobalKnownHostsFile=/dev/null",
             "-o", "ConnectTimeout=10", "-o", "ConnectionAttempts=1",
             f"{config['DEPLOY_USER']}@{host}", remote_command],
            input=remote_check, text=True, capture_output=True, check=True, timeout=45,
        )
        if avito_incident:
            print(incident_output(result.stdout, helper))
        else:
            print(result.stdout, end="")
        print("Checking HTTPS response", flush=True)
        response = subprocess.run(
            ["curl", "-q", "--silent", "--show-error", "--proto", "=https",
             "--connect-timeout", "10", "--max-time", "20", "--retry", "2",
             "--retry-max-time", "45", "--output", "/dev/null",
             "--write-out", "%{http_code}", "--url", config["DEPLOY_HEALTH_URL"]],
            text=True, capture_output=True, check=True, timeout=70,
        )
        if not re.fullmatch(r"2[0-9]{2}", response.stdout):
            raise ValueError("Health check did not return HTTP 2xx")
        print(f"HEALTH_HTTP={response.stdout}")
        print("HOSTING_CONNECTION_CHECK_OK (no deployment performed)")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--avito-refresh-incident", action="store_true",
                        help="Read only the fixed 2026-10-08 Avito error summary")
    args = parser.parse_args()
    try:
        run_check(os.environ, avito_incident=args.avito_refresh_incident)
    except (ValueError, OSError, subprocess.SubprocessError) as error:
        # CalledProcessError contains command arguments, so never print it.
        message = str(error) if isinstance(error, ValueError) else type(error).__name__
        raise SystemExit(f"Hosting connection check failed: {message}")
