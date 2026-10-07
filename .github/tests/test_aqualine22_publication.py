import importlib.util
from contextlib import contextmanager
from html.parser import HTMLParser
import io
import json
from pathlib import Path
import tarfile
import tempfile
import unittest
from types import SimpleNamespace
from urllib.parse import urljoin, urlsplit
from unittest.mock import patch

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/aqualine22_remote.py"
spec = importlib.util.spec_from_file_location("aqualine22_remote", SCRIPT)
remote = importlib.util.module_from_spec(spec)
spec.loader.exec_module(remote)
scope_spec = importlib.util.spec_from_file_location("aqualine22_scope", SCRIPT.with_name("aqualine22_scope.py"))
scope = importlib.util.module_from_spec(scope_spec)
scope_spec.loader.exec_module(scope)


class PublicationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.home = Path(self.temporary.name)
        live = patch.object(remote, "verify_live_root")
        self.verify_live = live.start()
        self.addCleanup(live.stop)
        self.logo = b"known public website logo"
        self.assets = {name: ("calculator: " + name).encode() for name in remote.FILES}
        self.config = {"mode": "publish", "origin": remote.ORIGIN,
                       "proof_asset": remote.PROOF_ASSET,
                       "proof_sha256": remote.digest(self.logo), "revision": "a" * 40,
                       "files": {name: remote.digest(data) for name, data in self.assets.items()}}
        self.root = self.website("informational")

    def website(self, name):
        root = self.home / name / "public_html"
        (root / "wp-includes").mkdir(parents=True)
        (root / "index.php").write_text("existing CMS")
        logo = root / remote.PROOF_ASSET
        logo.parent.mkdir(parents=True)
        logo.write_bytes(self.logo)
        return root

    def archive(self, assets=None):
        out = io.BytesIO()
        with tarfile.open(fileobj=out, mode="w") as tar:
            for name, data in (assets or self.assets).items():
                member = tarfile.TarInfo(name)
                member.size = len(data)
                tar.addfile(member, io.BytesIO(data))
        return out.getvalue()

    def publish(self):
        return remote.run_remote(self.config, self.archive(), self.home)

    def test_unique_verified_root_and_site_files_preserved(self):
        store = self.website("shop.aqualine22.ru")
        service = self.website("service2.aqualine22.ru")
        before = (self.root / "index.php").read_bytes()
        result = self.publish()
        self.assertEqual(result["url"], "https://aqualine22.ru/kalkulyator-vody/")
        self.assertEqual(result["root"], str(self.root))
        self.assertEqual(before, (self.root / "index.php").read_bytes())
        self.assertFalse((store / remote.DESTINATION).exists())
        self.assertFalse((service / remote.DESTINATION).exists())
        self.assertEqual((self.root / remote.DESTINATION / "engine.js").read_bytes(), self.assets["engine.js"])

    def test_inspection_never_creates_files(self):
        self.config["mode"] = "inspect"
        result = remote.run_remote(self.config, b"", self.home)
        self.assertEqual(result["matches"], 1)
        self.assertFalse((self.root / remote.DESTINATION).exists())

    def test_duplicate_identity_stops_before_writes(self):
        self.website("second-site")
        with self.assertRaises(remote.WebsiteBindingError):
            self.publish()
        self.assertFalse((self.root / remote.DESTINATION).exists())

    def test_no_identity_match_stops_before_writes(self):
        (self.root / remote.PROOF_ASSET).write_bytes(b"another site")
        with self.assertRaises(remote.WebsiteBindingError):
            self.publish()
        self.assertFalse((self.root / remote.DESTINATION).exists())

    def test_symlink_website_identity_is_rejected(self):
        proof = self.root / remote.PROOF_ASSET
        proof.unlink()
        outside = self.home / "elsewhere.png"
        outside.write_bytes(self.logo)
        proof.symlink_to(outside)
        with self.assertRaises(remote.WebsiteBindingError):
            self.publish()

    def test_existing_unmanaged_directory_is_preserved(self):
        target = self.root / remote.DESTINATION
        target.mkdir()
        (target / "index.html").write_text("existing page")
        with self.assertRaises(ValueError):
            self.publish()
        self.assertEqual((target / "index.html").read_text(), "existing page")

    def test_locally_modified_managed_page_is_preserved(self):
        self.publish()
        target = self.root / remote.DESTINATION
        (target / "app.js").write_text("local edit")
        with self.assertRaises(ValueError):
            self.publish()
        self.assertEqual((target / "app.js").read_text(), "local edit")

    def test_repeated_commit_is_idempotent(self):
        self.publish()
        result = self.publish()
        self.assertFalse(result["changed"])
        self.assertIsNone(result["backup"])

    def test_new_commit_keeps_private_backup(self):
        self.publish()
        self.config["revision"] = "b" * 40
        self.assets["index.html"] = b"new version"
        self.config["files"]["index.html"] = remote.digest(self.assets["index.html"])
        result = self.publish()
        backup = Path(result["backup"])
        self.assertEqual((backup / "index.html").read_bytes(), b"calculator: index.html")
        self.assertEqual(backup.parent.stat().st_mode & 0o077, 0)
        self.assertEqual((self.root / remote.DESTINATION / "index.html").read_bytes(), b"new version")

    def test_failed_directory_switch_restores_previous_version(self):
        self.publish()
        self.config["revision"] = "b" * 40
        rename = remote.os.rename
        def fail_stage(source, destination):
            if Path(source).name.startswith(".aqualine-water-stage-"):
                raise OSError("simulated failure")
            return rename(source, destination)
        with patch.object(remote.os, "rename", side_effect=fail_stage):
            with self.assertRaises(OSError):
                self.publish()
        self.assertEqual((self.root / remote.DESTINATION / "index.html").read_bytes(), self.assets["index.html"])

    def test_unsafe_archive_and_checksum_fail_before_writes(self):
        for assets in ({"../index.html": b"escape"}, {**self.assets, "index.html": b"wrong checksum"}):
            with self.subTest(assets=list(assets)):
                with self.assertRaises(ValueError):
                    remote.run_remote(self.config, self.archive(assets), self.home)
        self.assertFalse((self.root / remote.DESTINATION).exists())

    def test_corrupt_marker_revision_cannot_escape_backup_directory(self):
        self.publish()
        marker = self.root / remote.DESTINATION / remote.MARKER
        data = json.loads(marker.read_text())
        data["revision"] = "../../escape"
        marker.write_text(json.dumps(data))
        with self.assertRaises(ValueError):
            self.publish()
        self.assertFalse((self.home / "escape").exists())

    def test_other_domain_configuration_is_rejected(self):
        self.config["origin"] = "https://shop.aqualine22.ru"
        with self.assertRaises(ValueError):
            self.publish()
        self.assertFalse((self.root / remote.DESTINATION).exists())

    def test_staging_copy_cannot_be_selected(self):
        (self.root / remote.PROOF_ASSET).unlink()
        for name in ("staging.aqualine22.ru", "stage.aqualine22.ru"):
            self.website(name)
        with self.assertRaises(remote.WebsiteBindingError):
            self.publish()
        self.verify_live.assert_not_called()

    def test_unverified_live_mapping_stops_calculator_writes(self):
        self.verify_live.side_effect = ValueError("Wrong live vhost")
        with self.assertRaises(ValueError):
            self.publish()
        self.assertFalse((self.root / remote.DESTINATION).exists())


class LiveBindingTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def opener(self, wrong=False):
        @contextmanager
        def opened(url, timeout):
            self.assertEqual(urlsplit(url).netloc, "aqualine22.ru")
            self.assertEqual(urlsplit(url).scheme, "https")
            self.assertEqual(timeout, 15)
            path = self.root / urlsplit(url).path.lstrip("/")
            self.assertTrue(path.is_file())
            data = b"different vhost" if wrong else path.read_bytes()
            yield SimpleNamespace(status=200, geturl=lambda: url, read=lambda limit: data[:limit])
        return SimpleNamespace(open=opened)

    def test_live_nonce_is_verified_and_removed(self):
        with patch.object(remote.urllib.request, "build_opener", return_value=self.opener()):
            remote.verify_live_root(self.root)
        self.assertEqual(list(self.root.iterdir()), [])

    def test_wrong_live_root_removes_probe_and_fails(self):
        with patch.object(remote.urllib.request, "build_opener", return_value=self.opener(wrong=True)):
            with self.assertRaises(ValueError):
                remote.verify_live_root(self.root)
        self.assertEqual(list(self.root.iterdir()), [])

    def test_network_failure_removes_probe(self):
        opener = SimpleNamespace(open=lambda *args, **kwargs: (_ for _ in ()).throw(TimeoutError()))
        with patch.object(remote.urllib.request, "build_opener", return_value=opener):
            with self.assertRaises(TimeoutError):
                remote.verify_live_root(self.root)
        self.assertEqual(list(self.root.iterdir()), [])

    def test_redirect_is_rejected(self):
        with self.assertRaises(ValueError):
            remote.NoRedirect().redirect_request(None, None, 302, "", {}, "https://shop.aqualine22.ru/")


class ScopeTests(unittest.TestCase):
    def test_isolated_calculator_and_pipeline_skip_service_deploy(self):
        self.assertEqual(scope.classify(["tools/aqualine22-calculator/public/app.js", scope.CI_FILE]),
                         {"service_deploy": False, "calculator_deploy": True})

    def test_mixed_changes_preserve_service_deploy(self):
        self.assertEqual(scope.classify(["tools/aqualine22-calculator/public/app.js", "manage.py"]),
                         {"service_deploy": True, "calculator_deploy": True})

    def test_only_ci_or_unrelated_changes_preserve_service_deploy(self):
        for paths in ([scope.CI_FILE], ["README.md"], ["tools/aqualine22-calculator-lookalike/app.js"]):
            self.assertEqual(scope.classify(paths), {"service_deploy": True, "calculator_deploy": False})

    def test_manual_recovery_is_unchanged(self):
        self.assertEqual(scope.event_scope("workflow_dispatch", "refs/heads/main", None, "a" * 40, None),
                         {"service_deploy": True, "calculator_deploy": False})

    def test_pull_request_never_deploys(self):
        self.assertEqual(scope.event_scope("pull_request", "refs/pull/1/merge", None, "a" * 40, None),
                         {"service_deploy": False, "calculator_deploy": False})

    def test_empty_missing_or_failed_diff_stops_scope(self):
        with self.assertRaises(ValueError):
            scope.classify([])
        for before in (None, "0" * 40, "short"):
            with self.assertRaises(ValueError):
                scope.event_scope("push", "refs/heads/main", before, "a" * 40, lambda *args: [])
        def failure(*args):
            raise RuntimeError("Unavailable history")
        with self.assertRaises(RuntimeError):
            scope.event_scope("push", "refs/heads/main", "b" * 40, "a" * 40, failure)


class AssetPathsTests(unittest.TestCase):
    def test_assets_resolve_inside_calculator_directory(self):
        class Parser(HTMLParser):
            def __init__(self):
                super().__init__()
                self.assets = []
            def handle_starttag(self, tag, attributes):
                attributes = dict(attributes)
                if tag == "script" and "src" in attributes:
                    self.assets.append(attributes["src"])
                if tag == "link" and attributes.get("rel") in {"stylesheet", "icon"}:
                    self.assets.append(attributes["href"])
        page = SCRIPT.parents[2] / "tools/aqualine22-calculator/public/index.html"
        parser = Parser()
        parser.feed(page.read_text())
        base = "https://aqualine22.ru/kalkulyator-vody/"
        self.assertEqual({urljoin(base, asset) for asset in parser.assets},
                         {base + name for name in ("favicon.svg", "styles.css", "app.js")})


if __name__ == "__main__":
    unittest.main()
