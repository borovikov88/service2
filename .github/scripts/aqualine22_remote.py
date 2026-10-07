"""Publish only the Aqualine water calculator in a verified WordPress web root."""
import hashlib
import io
import json
import os
from pathlib import Path
import re
import shutil
import sys
import tarfile
import tempfile
import uuid
import urllib.request

ORIGIN = "https://aqualine22.ru"
PROOF_ASSET = "wp-content/uploads/2013/05/logo5.png"
DESTINATION = "kalkulyator-vody"
FILES = {"index.html", "styles.css", "engine.js", "app.js", "favicon.svg", ".htaccess"}
MARKER = ".aqualine-managed.json"
OWNER = "aqualine22-water-calculator-v1"
MAX_ARCHIVE = 256 * 1024


class WebsiteBindingError(ValueError):
    def __init__(self, report):
        super().__init__("Website directory identity is missing or ambiguous; no files changed")
        self.report = report


def digest(data):
    return hashlib.sha256(data).hexdigest()


def no_symlinks(path, home):
    relative = path.relative_to(home)
    current = home
    for part in relative.parts:
        current /= part
        if current.is_symlink():
            raise ValueError("Symlink in website path")


def validate(config):
    if set(config) != {"mode", "origin", "proof_asset", "proof_sha256", "revision", "files"}:
        raise ValueError("Unexpected publication configuration")
    if config["mode"] not in {"inspect", "publish"} or config["origin"] != ORIGIN:
        raise ValueError("Publication is restricted to aqualine22.ru")
    if config["proof_asset"] != PROOF_ASSET:
        raise ValueError("Unexpected website identity asset")
    if not re.fullmatch(r"[0-9a-f]{64}", config["proof_sha256"]):
        raise ValueError("Invalid website identity hash")
    if not re.fullmatch(r"[0-9a-f]{40}", config["revision"]):
        raise ValueError("Invalid source commit")
    files = config["files"]
    if set(files) != FILES or any(not re.fullmatch(r"[0-9a-f]{64}", value) for value in files.values()):
        raise ValueError("Unexpected calculator manifest")


def inspect_roots(home, proof_sha256):
    roots = list(home.glob("*/public_html"))
    if (home / "public_html").is_dir():
        roots.append(home / "public_html")
    if len(roots) > 64:
        raise ValueError("Too many website roots; explicit mapping is required")
    report, matches = [], []
    for root in sorted(set(roots)):
        try:
            no_symlinks(root, home)
            root.relative_to(home)
            if root.stat().st_uid != os.getuid():
                continue
        except (ValueError, OSError):
            continue
        # Store, Service2, staging and backup roots cannot receive this publication.
        excluded = bool(re.search(r"shop|service2|stag(?:e|ing)|backup|hotfix|test", root.parent.name, re.I))
        wordpress = all((root / name).exists() for name in ("wp-content", "wp-includes", "index.php"))
        proof = root / PROOF_ASSET
        match = False
        if wordpress and not excluded:
            try:
                no_symlinks(proof, home)
                if proof.is_file() and proof.stat().st_size <= 2 * 1024 * 1024:
                    match = digest(proof.read_bytes()) == proof_sha256
            except (ValueError, OSError):
                pass
        report.append({"root": str(root), "wordpress": wordpress, "excluded": excluded, "identity_match": match})
        if match:
            matches.append(root)
    return report, matches


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ValueError("Website identity request redirected; no calculator files changed")


def verify_live_root(root):
    """Confirm the selected directory is served by the requested HTTPS domain."""
    name = "aqualine-verify-" + uuid.uuid4().hex + ".txt"
    nonce = uuid.uuid4().hex.encode("ascii")
    path = root / name
    created = False
    try:
        with path.open("xb") as stream:
            created = True
            stream.write(nonce)
            stream.flush()
            os.fsync(stream.fileno())
        path.chmod(0o644)
        url = ORIGIN + "/" + name
        opener = urllib.request.build_opener(NoRedirect())
        with opener.open(url, timeout=15) as response:
            if response.status != 200 or response.geturl() != url or response.read(257) != nonce:
                raise ValueError("Selected directory is not served by aqualine22.ru; no calculator files changed")
    finally:
        if created and path.is_file() and not path.is_symlink() and path.stat().st_size == len(nonce) and path.read_bytes() == nonce:
            path.unlink()


def read_assets(archive, expected):
    if len(archive) > MAX_ARCHIVE:
        raise ValueError("Calculator archive is too large")
    result = {}
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:") as tar:
        for member in tar:
            if member.name not in FILES or member.name in result or not member.isfile():
                raise ValueError("Unexpected archive entry")
            if member.size > 128 * 1024:
                raise ValueError("Calculator asset is too large")
            content = tar.extractfile(member).read()
            if len(content) != member.size or digest(content) != expected[member.name]:
                raise ValueError("Calculator asset checksum mismatch")
            result[member.name] = content
    if set(result) != FILES:
        raise ValueError("Incomplete calculator archive")
    return result


def check_existing(target):
    if target.is_symlink() or not target.is_dir():
        raise ValueError("Existing destination is not a managed calculator")
    if {p.name for p in target.iterdir()} != FILES | {MARKER}:
        raise ValueError("Existing calculator contains unknown files")
    marker = target / MARKER
    if marker.is_symlink() or marker.stat().st_size > 16 * 1024:
        raise ValueError("Invalid existing calculator marker")
    metadata = json.loads(marker.read_text())
    if set(metadata) != {"owner", "revision", "files"} or metadata.get("owner") != OWNER:
        raise ValueError("Destination belongs to another project")
    if not re.fullmatch(r"[0-9a-f]{40}", metadata.get("revision", "")):
        raise ValueError("Invalid existing calculator revision")
    if set(metadata.get("files", {})) != FILES:
        raise ValueError("Destination belongs to another project")
    for name, sha in metadata["files"].items():
        if not isinstance(sha, str) or not re.fullmatch(r"[0-9a-f]{64}", sha):
            raise ValueError("Invalid existing calculator checksum")
        path = target / name
        if path.is_symlink() or not path.is_file() or path.stat().st_size > 128 * 1024:
            raise ValueError("Unexpected existing calculator asset")
        if digest(path.read_bytes()) != sha:
            raise ValueError("Existing calculator has local changes")
    return metadata


def publish(root, assets, config):
    target = root / DESTINATION
    metadata = {"owner": OWNER, "revision": config["revision"], "files": config["files"]}
    previous = None
    if target.exists() or target.is_symlink():
        previous = check_existing(target)
        if previous == metadata:
            return {"changed": False, "backup": None}
    stage = Path(tempfile.mkdtemp(prefix=".aqualine-water-stage-", dir=root))
    backup = None
    try:
        for name, content in assets.items():
            path = stage / name
            with path.open("xb") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            path.chmod(0o644)
        (stage / MARKER).write_text(json.dumps(metadata, sort_keys=True), encoding="utf-8")
        (stage / MARKER).chmod(0o644)
        stage.chmod(0o755)
        if previous is not None:
            # Recheck ownership immediately before moving the existing directory.
            if check_existing(target) != previous:
                raise ValueError("Calculator changed while publication was prepared")
            backups = root.parent / ".aqualine-water-backups"
            if backups.is_symlink():
                raise ValueError("Invalid calculator backup directory")
            backups.mkdir(mode=0o700, exist_ok=True)
            if backups.stat().st_uid != os.getuid() or backups.stat().st_mode & 0o077:
                raise ValueError("Calculator backup directory is not private")
            backup = backups / (previous["revision"][:12] + "-" + uuid.uuid4().hex)
            os.rename(target, backup)
        elif target.exists() or target.is_symlink():
            raise ValueError("Destination appeared while publication was prepared")
        try:
            os.rename(stage, target)
        except Exception:
            if backup is not None and not target.exists():
                os.rename(backup, target)
            raise
        return {"changed": True, "backup": str(backup) if backup else None}
    finally:
        if stage.exists():
            shutil.rmtree(stage)


def run_remote(config, archive, home=None):
    validate(config)
    home = (home or Path.home()).resolve()
    report, matches = inspect_roots(home, config["proof_sha256"])
    if config["mode"] == "inspect":
        return {"mode": "inspect", "roots": report, "matches": len(matches)}
    if len(matches) != 1:
        raise WebsiteBindingError(report)
    assets = read_assets(archive, config["files"])
    root = matches[0]
    verify_live_root(root)
    result = publish(root, assets, config)
    return {"mode": "publish", "root": str(root), "revision": config["revision"], "url": ORIGIN + "/" + DESTINATION + "/", **result}


if __name__ == "__main__":
    try:
        config = json.loads(sys.argv[1])
        archive = sys.stdin.buffer.read(MAX_ARCHIVE + 1)
        print(json.dumps(run_remote(config, archive), sort_keys=True))
    except Exception as error:
        message = str(error) if isinstance(error, ValueError) else type(error).__name__
        print(json.dumps({"error": message, "roots": getattr(error, "report", [])}, sort_keys=True))
        raise SystemExit(2)
