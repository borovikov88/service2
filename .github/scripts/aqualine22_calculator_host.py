"""Use the existing pinned SSH identity without exposing its credentials."""
import io
import json
import os
from pathlib import Path
import shlex
import subprocess
import tarfile
import tempfile

from check_hosting_connection import validate_config
from aqualine22_remote import FILES, digest, validate

BASE = Path(__file__).resolve().parents[2]


def main():
    config = validate_config(os.environ)
    target = json.loads((BASE / "tools/aqualine22-calculator/deploy.json").read_text())
    target["revision"] = os.environ["GITHUB_SHA"]
    assets = BASE / "tools/aqualine22-calculator/public"
    target["files"] = {name: digest((assets / name).read_bytes()) for name in sorted(FILES)}
    validate(target)
    archive = io.BytesIO()
    with tarfile.open(fileobj=archive, mode="w") as tar:
        for name in sorted(FILES):
            tar.add(assets / name, arcname=name, recursive=False)
    remote = (Path(__file__).parent / "aqualine22_remote.py").read_text()
    command = "python3 -c " + shlex.quote(remote) + " " + shlex.quote(json.dumps(target, sort_keys=True))
    with tempfile.TemporaryDirectory(prefix="aqualine22-ssh-") as temporary:
        key, hosts = Path(temporary) / "key", Path(temporary) / "known_hosts"
        for path, value in ((key, config["DEPLOY_SSH_KEY"]), (hosts, config["DEPLOY_KNOWN_HOSTS"])):
            path.touch(mode=0o600)
            path.write_text(value.rstrip("\n") + "\n", encoding="utf-8")
        subprocess.run(["ssh-keygen", "-y", "-P", "", "-f", str(key)], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10)
        host, port = config["DEPLOY_HOST"], config["DEPLOY_PORT"]
        lookup = host if port == "22" else f"[{host}]:{port}"
        subprocess.run(["ssh-keygen", "-F", lookup, "-f", str(hosts)], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10)
        result = subprocess.run(
            ["ssh", "-F", "/dev/null", "-T", "-p", port, "-i", str(key),
             "-o", "BatchMode=yes", "-o", "IdentitiesOnly=yes", "-o", "IdentityAgent=none",
             "-o", "StrictHostKeyChecking=yes", "-o", f"UserKnownHostsFile={hosts}",
             "-o", "GlobalKnownHostsFile=/dev/null", "-o", "ConnectTimeout=10", "-o", "ConnectionAttempts=1",
             f"{config['DEPLOY_USER']}@{host}", command],
            input=archive.getvalue(), capture_output=True, check=False, timeout=90,
        )
        data = json.loads(result.stdout)
        if result.returncode != 0:
            print(json.dumps(data, sort_keys=True), flush=True)
            raise ValueError("Hosting refused publication; no further steps performed")
        if data.get("mode") != target["mode"]:
            raise ValueError("Unexpected publication response")
        print(json.dumps(data, sort_keys=True))
        if target["mode"] == "publish":
            if data.get("revision") != target["revision"] or data.get("url") != "https://aqualine22.ru/kalkulyator-vody/":
                raise ValueError("Publication does not match the requested website and source")
            print("AQUALINE22_FILES_PUBLISHED; verify the public page in a normal browser")


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        # SSH errors can contain command/configuration values. Never print them.
        message = str(error) if isinstance(error, ValueError) else type(error).__name__
        raise SystemExit("Aqualine hosting operation stopped: " + message)
