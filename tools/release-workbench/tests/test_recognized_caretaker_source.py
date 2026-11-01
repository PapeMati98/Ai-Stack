"""Synthetic fixtures only: the real caretaker detector is never opened."""

from contextlib import contextmanager, ExitStack
import builtins
import hashlib
import io
import os
from pathlib import Path
import stat
import subprocess
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import uuid

from release_workbench import exporter, integration, privacy


SOURCE = "shared/openclaw-hub-caretaker/releases/0.1.78/caretaker/secrets.py"
CONTENT = b"def detect_sensitive_content(value):\n    return bool(value)\n"
ROOT = Path(os.environ.get("TEAM_TEST_ROOT", str(Path(__file__).resolve().parents[1] / "test-temp")))


class CaretakerSourceTests(unittest.TestCase):
    def fixture(self):
        root = ROOT / "recognized-caretaker-source" / uuid.uuid4().hex
        root.mkdir(parents=True)
        return root

    def source(self, root, relative=SOURCE, content=CONTENT):
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
        return target

    @contextmanager
    def no_content_reads(self):
        # A refused fixture must not reach any of the scanner's open routes.
        with ExitStack() as stack:
            for owner, name in ((builtins, "open"), (io, "open"), (os, "open")):
                stack.enter_context(patch.object(owner, name, side_effect=AssertionError("refused fixture opened")))
            yield

    def assert_refused(self, root, rule="scan.refused_sensitive_path"):
        with self.no_content_reads():
            result = privacy.scan_tree(root)
        self.assertEqual(result["status"], "FAIL")
        self.assertEqual(result["files_scanned"], 0)
        self.assertTrue(any(item["rule"] == rule for item in result["findings"]))

    def test_exact_regular_source_is_read_and_scanned(self):
        root = self.fixture()
        target = self.source(root)
        original = os.open
        with patch.object(privacy.os, "open", wraps=original) as opened:
            result = privacy.scan_tree(root)
        self.assertEqual(result["status"], "PASS")
        self.assertEqual(result["files_scanned"], 1)
        self.assertTrue(any(Path(call.args[0]) == target for call in opened.call_args_list))

    def test_fake_secret_in_exact_source_is_detected(self):
        root = self.fixture()
        fake = "gh" + "p_" + "A" * 36
        self.source(root, content=("api_key = " + repr(fake) + "\n").encode())
        result = privacy.scan_tree(root)
        self.assertEqual(result["status"], "FAIL")
        self.assertEqual(result["files_scanned"], 1)
        rules = {item["rule"] for item in result["findings"] if item["path"] == SOURCE}
        self.assertTrue({"privacy.github_token", "privacy.credential_assignment"} <= rules)

    def test_other_names_cases_versions_and_suffixes_refused_before_read(self):
        aliases = ["secrets.py", "elsewhere/secrets.py", SOURCE + ".json",
                   SOURCE.replace("shared/", "Shared/", 1),
                   SOURCE.replace("0.1.78", "0.1.79"),
                   SOURCE.replace("secrets.py", "SECRETS.py"),
                   SOURCE + ".", SOURCE + " "]
        # Windows disallows distinct trailing-dot/space files; the classifier
        # assertions below cover those spellings without aliasing a real file.
        for relative in aliases:
            with self.subTest(relative=relative):
                self.assertTrue(privacy._is_sensitive_path(relative, is_directory=False))
                if relative.endswith((".", " ")) and os.name == "nt":
                    continue
                root = self.fixture()
                self.source(root, relative)
                self.assert_refused(root)

    def test_normalization_aliases_never_gain_classification(self):
        for relative in ("./" + SOURCE, SOURCE.replace("shared/", "shared//", 1),
                         SOURCE.replace("releases/", "releases/./", 1),
                         SOURCE.replace("releases/", "releases/x/../", 1),
                         SOURCE.replace("/", "\\"), "/" + SOURCE):
            with self.subTest(relative=relative):
                self.assertTrue(privacy._is_sensitive_path(relative, is_directory=False))
                with self.assertRaises(ValueError):
                    parts = exporter._check_literal_path(relative, "source path")
                    exporter._check_dangerous(parts, "source path")

    def test_directory_and_sensitive_ancestors_refused_before_read(self):
        root = self.fixture()
        directory = root / SOURCE
        directory.mkdir(parents=True)
        (directory / "child.py").write_bytes(CONTENT)
        self.assertTrue(privacy._is_sensitive_path(SOURCE, is_directory=True))
        self.assert_refused(root)
        for ancestor in (".env", "secrets", ".ssh", "secret_run.py"):
            with self.subTest(ancestor=ancestor):
                root = self.fixture()
                self.source(root, ancestor + "/" + SOURCE)
                self.assert_refused(root)

    def test_size_check_still_precedes_read(self):
        root = self.fixture()
        self.source(root)
        with patch.object(privacy, "_MAX_FILE_BYTES", 1):
            self.assert_refused(root, "scan.file_too_large")

    @contextmanager
    def entry_metadata(self, target, *, mode, attributes):
        original = os.scandir

        @contextmanager
        def inspect(directory):
            with original(directory) as entries:
                wrapped = []
                for entry in entries:
                    if Path(entry.path) == target:
                        info = SimpleNamespace(st_mode=mode, st_file_attributes=attributes)
                        entry = SimpleNamespace(name=entry.name, path=entry.path,
                                                stat=lambda follow_symlinks=False: info)
                    wrapped.append(entry)
                yield iter(wrapped)

        with patch.object(privacy.os, "scandir", inspect):
            yield

    def test_link_and_reparse_metadata_refused_before_read(self):
        for mode, attributes in ((stat.S_IFLNK, 0), (stat.S_IFREG, 0x400)):
            with self.subTest(mode=mode, attributes=attributes):
                root = self.fixture()
                target = self.source(root)
                with self.entry_metadata(target, mode=mode, attributes=attributes):
                    self.assert_refused(root, "scan.refused_link")

    def test_exporter_exact_path_and_store_refusals(self):
        exporter._check_dangerous(exporter._check_literal_path(SOURCE, "source path"), "source path")
        for relative in ("secrets.py", "elsewhere/secrets.py", SOURCE + ".json", SOURCE + "/child.py",
                         SOURCE.replace("shared/", "Shared/", 1),
                         SOURCE.replace("secrets.py", "SECRETS.py"),
                         ".env/" + SOURCE, "secrets/" + SOURCE, ".ssh/" + SOURCE,
                         "credentials.json", "auth-store.json"):
            with self.subTest(relative=relative), self.no_content_reads(), self.assertRaises(ValueError):
                exporter._check_dangerous(exporter._check_literal_path(relative, "source path"), "source path")

    def git(self, repo, *args):
        return subprocess.run(
            ["git", "-c", "core.hooksPath=", "-c", "init.templateDir=", "-c", "core.autocrlf=false",
             "-C", str(repo), *args], check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            stdin=subprocess.DEVNULL, timeout=30, shell=False,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        ).stdout.decode().strip()

    def test_exporter_pinned_manifest_hash_and_blob_checks_remain(self):
        repo = self.fixture()
        self.source(repo)
        # Real Git, isolated configuration and task-owned synthetic objects.
        with patch.dict(os.environ, {"GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1",
                                     "GIT_TERMINAL_PROMPT": "0"}):
            self.git(repo, "init", "--quiet")
            self.git(repo, "add", "--", SOURCE)
            self.git(repo, "-c", "user.name=Synthetic Fixture", "-c", "user.email=fixture@example.invalid",
                     "-c", "commit.gpgsign=false", "commit", "--quiet", "-m", "Synthetic source fixture")
            commit = self.git(repo, "rev-parse", "HEAD")
            entry = {"path": SOURCE, "target": SOURCE, "sha256": hashlib.sha256(CONTENT).hexdigest()}
            manifest = {"version": 1, "max_total_bytes": 4096, "sources": [
                {"id": "synthetic", "repo": str(repo), "commit": commit, "files": [entry]}]}
            self.assertEqual(exporter.validate_manifest(manifest)["status"], "valid")
            # Working-tree changes cannot replace the pinned blob.
            (repo / SOURCE).write_bytes(b"working tree differs\n")
            self.assertEqual(exporter.validate_manifest(manifest)["status"], "valid")
            entry["sha256"] = "0" * 64
            with self.assertRaisesRegex(ValueError, "sha256 mismatch"):
                exporter.validate_manifest(manifest)
            entry["sha256"] = hashlib.sha256(CONTENT).hexdigest()
            entry["path"] = SOURCE.rsplit("/", 1)[0]
            with self.assertRaises(ValueError):
                exporter.validate_manifest(manifest)
            entry["path"] = SOURCE
            # A synthetic symlink blob at the exact path must still be refused.
            blob = self.git(repo, "rev-parse", commit + ":" + SOURCE)
            self.git(repo, "update-index", "--cacheinfo", "120000," + blob + "," + SOURCE)
            self.git(repo, "-c", "user.name=Synthetic Fixture", "-c", "user.email=fixture@example.invalid",
                     "-c", "commit.gpgsign=false", "commit", "--quiet", "-m", "Synthetic link fixture")
            manifest["sources"][0]["commit"] = self.git(repo, "rev-parse", "HEAD")
            with self.assertRaisesRegex(ValueError, "symlink"):
                exporter.validate_manifest(manifest)

    def test_integration_accepts_only_existing_regular_final_source(self):
        root = self.fixture()
        target = self.source(root)
        with self.no_content_reads():
            self.assertEqual(integration._safe_path(target), target)
        missing = self.fixture() / SOURCE
        with self.no_content_reads(), self.assertRaises(ValueError):
            integration._safe_path(missing)
        directory = self.fixture() / SOURCE
        directory.mkdir(parents=True)
        with self.no_content_reads(), self.assertRaises(ValueError):
            integration._safe_path(directory)

    def test_integration_lookalikes_and_unsafe_ancestors_refused_before_lstat(self):
        root = self.fixture()
        aliases = ["secrets.py", "elsewhere/secrets.py", SOURCE + ".json", SOURCE + "/child.py",
                   SOURCE.replace("shared/", "Shared/", 1), SOURCE.replace("secrets.py", "SECRETS.py"),
                   ".env/" + SOURCE, "secrets/" + SOURCE, ".ssh/" + SOURCE,
                   SOURCE.replace("releases/", "releases/./", 1),
                   SOURCE.replace("releases/", "releases//", 1),
                   SOURCE.replace("releases/", "releases/x/../", 1),
                   ".ssh/../" + SOURCE, ".ssh./" + SOURCE, ".ssh /" + SOURCE,
                   "safe./" + SOURCE, SOURCE + ".", SOURCE + " "]
        for relative in aliases:
            with self.subTest(relative=relative), self.no_content_reads(), patch.object(
                    Path, "lstat", side_effect=AssertionError("unsafe path inspected")), self.assertRaises(ValueError):
                # Keep raw dot/empty components: Path construction erases them.
                integration._safe_path(str(root) + os.sep + relative.replace("/", os.sep))

    def test_integration_links_and_reparse_at_final_or_ancestor_refused(self):
        root = self.fixture()
        target = self.source(root)
        original = Path.lstat
        for linked in (target, target.parent):
            for mode, attributes in ((stat.S_IFLNK, 0), (stat.S_IFREG, 0x400)):
                def metadata(path, *args, **kwargs):
                    if path == linked:
                        return SimpleNamespace(st_mode=mode, st_file_attributes=attributes)
                    return original(path, *args, **kwargs)
                with self.subTest(final=linked == target, attributes=attributes), self.no_content_reads(), patch.object(
                        Path, "lstat", metadata), self.assertRaisesRegex(ValueError, "linked"):
                    integration._safe_path(target)


if __name__ == "__main__":
    unittest.main()
