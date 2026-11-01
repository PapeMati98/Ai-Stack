"""Offline synthetic tests for release_workbench.exporter (task-owned temp dirs)."""

import hashlib
import json
import os
import subprocess
import tempfile
import unittest
import unittest.mock

from release_workbench import exporter
from release_workbench.exporter import (
    export_candidate,
    validate_manifest,
    verify_export,
)

GIT_ENV = {
    "GIT_AUTHOR_NAME": "t",
    "GIT_AUTHOR_EMAIL": "t@example.invalid",
    "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@example.invalid",
}


def _git(repo, *args, **env):
    merged = dict(os.environ)
    merged.update(GIT_ENV)
    merged.update(env)
    proc = subprocess.run(
        ["git", *args],
        cwd=repo,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        shell=False,
        timeout=120,
        env=merged,
    )
    if proc.returncode != 0:
        raise AssertionError("git %r failed: %s" % (args, proc.stderr.decode()))
    return proc.stdout.decode().strip()


def _make_repo(files):
    """Create a tiny git repo; files maps relative posix path -> bytes."""
    repo = tempfile.mkdtemp(prefix="src-repo-")
    _git(repo, "-c", "init.defaultBranch=main", "-c", "commit.gpgsign=false",
         "init", "-q")
    _git(repo, "config", "commit.gpgsign", "false")
    for rel, data in files.items():
        full = os.path.join(repo, *rel.split("/"))
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "wb") as handle:
            handle.write(data)
        _git(repo, "add", "--", rel)
    _git(repo, "-c", "core.autocrlf=false", "commit", "-qm", "seed")
    commit = _git(repo, "rev-parse", "HEAD")
    return repo, commit


def _manifest(repo, commit, pairs, **over):
    files = [
        {
            "path": src,
            "target": tgt,
            "sha256": hashlib.sha256(content).hexdigest(),
        }
        for src, tgt, content in pairs
    ]
    manifest = {
        "version": 1,
        "max_total_bytes": 209715200,
        "sources": [
            {"id": "fleet-snap", "repo": repo, "commit": commit, "files": files}
        ],
    }
    manifest.update(over)
    return manifest


class ExporterTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="exporter-test-")
        self.out_parent = os.path.join(self.tmp.name, "out")
        os.makedirs(self.out_parent)

    def tearDown(self):
        self.tmp.cleanup()

    def _dest(self, name="candidate"):
        return os.path.join(self.out_parent, name)

    def test_valid_export_verify_and_receipt_hygiene(self):
        repo, commit = _make_repo(
            {
                "pkg/auth.py": b"TOKEN = 'placeholder'\n",
                "pkg/LICENSE": b"keep me\n",
            }
        )
        manifest = _manifest(
            repo,
            commit,
            [
                ("pkg/auth.py", "runtime/package/auth.py", b"TOKEN = 'placeholder'\n"),
                ("pkg/LICENSE", "runtime/package/LICENSE", b"keep me\n"),
            ],
        )
        self.assertEqual(
            validate_manifest(manifest)["status"], "valid"
        )
        receipt = export_candidate(
            manifest, self._dest(), allowed_output_parent=self.out_parent
        )
        self.assertEqual(receipt["status"], "exported")
        blob = json.dumps(receipt)
        self.assertNotIn(repo, blob)  # no absolute source root disclosure
        self.assertNotIn(self.out_parent, blob)
        self.assertNotIn("repo", blob)
        checked = verify_export(self._dest(), receipt)
        self.assertEqual(checked["status"], "verified")
        self.assertEqual(checked["files"], 2)

    def test_reproducible_receipt(self):
        repo, commit = _make_repo({"a.py": b"x = 1\n"})
        manifest = _manifest(repo, commit, [("a.py", "pkg/a.py", b"x = 1\n")])
        first = export_candidate(
            manifest, self._dest("one"), allowed_output_parent=self.out_parent
        )
        second = export_candidate(
            manifest, self._dest("two"), allowed_output_parent=self.out_parent
        )
        self.assertEqual(first, second)

    def test_poisoned_later_entry_writes_nothing(self):
        repo, commit = _make_repo({"good.py": b"ok\n", "evil.py": b"no\n"})
        before = _git(repo, "rev-parse", "HEAD")
        manifest = _manifest(
            repo,
            commit,
            [
                ("good.py", "pkg/good.py", b"ok\n"),
                ("../escape.py", "pkg/evil.py", b"no\n"),  # poisoned 2nd entry
            ],
        )
        with self.assertRaises(ValueError):
            export_candidate(
                manifest, self._dest(), allowed_output_parent=self.out_parent
            )
        self.assertFalse(os.path.lexists(self._dest()))
        self.assertEqual(_git(repo, "rev-parse", "HEAD"), before)
        self.assertEqual(_git(repo, "status", "--porcelain"), "")

    def test_poisoned_hash_writes_nothing(self):
        repo, commit = _make_repo({"good.py": b"ok\n"})
        manifest = _manifest(
            repo, commit, [("good.py", "pkg/good.py", b"ok\n")]
        )
        manifest["sources"][0]["files"][0]["sha256"] = "0" * 64
        with self.assertRaises(ValueError):
            export_candidate(
                manifest, self._dest(), allowed_output_parent=self.out_parent
            )
        self.assertFalse(os.path.lexists(self._dest()))

    def test_case_collision_targets(self):
        repo, commit = _make_repo({"a.py": b"a\n", "b.py": b"b\n"})
        manifest = _manifest(
            repo,
            commit,
            [("a.py", "Pkg/Mod.py", b"a\n"), ("b.py", "pkg/mod.py", b"b\n")],
        )
        with self.assertRaises(ValueError):
            validate_manifest(manifest)

    def test_traversal_absolute_windows_rejected(self):
        repo, commit = _make_repo({"a.py": b"a\n"})
        bad_targets = [
            "../escape.py",
            "/abs/path.py",
            "pkg\\win.py",
            "C:/drive.py",
            "//unc/share.py",
            "pkg/./dot.py",
            "pkg/CON.txt",
            "pkg/stream.txt:secret",
            ".git/hooks/x.py",
        ]
        for bad in bad_targets:
            with self.subTest(target=bad):
                manifest = _manifest(
                    repo, commit, [("a.py", bad, b"a\n")]
                )
                with self.assertRaises(ValueError):
                    validate_manifest(manifest)
        with self.assertRaises(ValueError):
            validate_manifest(
                _manifest(repo, commit, [("../a.py", "pkg/a.py", b"a\n")])
            )

    def test_dangerous_data_paths_rejected(self):
        repo, commit = _make_repo({"a.py": b"a\n"})
        bad = [
            ".env",
            "cfg/.env.local",
            "cfg/auth-tokens.json",
            "cfg/credentials.json",
            "cfg/secrets.yaml",
            "vault/keys.txt",
            "data/raw/sample.csv",
            "notes/proposals/plan.md",
            "logs/app.log",
            "node_modules/dep/index.js",
            ".aws/config",
        ]
        for target in bad:
            with self.subTest(target=target):
                with self.assertRaises(ValueError):
                    validate_manifest(
                        _manifest(repo, commit, [("a.py", target, b"a\n")])
                    )

    def test_symlink_blob_refused(self):
        repo = tempfile.mkdtemp(prefix="src-link-")
        _git(repo, "-c", "init.defaultBranch=main",
             "-c", "commit.gpgsign=false", "init", "-q")
        _git(repo, "config", "commit.gpgsign", "false")
        with open(os.path.join(repo, "real.txt"), "wb") as handle:
            handle.write(b"data\n")
        _git(repo, "add", "--", "real.txt")
        blob = _git(repo, "hash-object", "-w", "--", "real.txt")
        _git(repo, "update-index", "--add", "--cacheinfo",
             "120000," + blob + ",link.txt")
        _git(repo, "commit", "-qm", "with link")
        commit = _git(repo, "rev-parse", "HEAD")
        manifest = {
            "version": 1,
            "max_total_bytes": 209715200,
            "sources": [
                {
                    "id": "fleet-snap",
                    "repo": repo,
                    "commit": commit,
                    "files": [
                        {
                            "path": "link.txt",
                            "target": "pkg/link.txt",
                            "sha256": hashlib.sha256(b"data\n").hexdigest(),
                        }
                    ],
                }
            ],
        }
        with self.assertRaises(ValueError):
            validate_manifest(manifest)

    def test_executable_blob_refused(self):
        repo = tempfile.mkdtemp(prefix="src-exec-")
        _git(repo, "-c", "init.defaultBranch=main",
             "-c", "commit.gpgsign=false", "init", "-q")
        _git(repo, "config", "commit.gpgsign", "false")
        with open(os.path.join(repo, "run.sh"), "wb") as handle:
            handle.write(b"#!/bin/sh\necho hi\n")
        _git(repo, "add", "--", "run.sh")
        _git(repo, "update-index", "--chmod=+x", "--", "run.sh")
        _git(repo, "commit", "-qm", "exec bit")
        commit = _git(repo, "rev-parse", "HEAD")
        data = b"#!/bin/sh\necho hi\n"
        manifest = _manifest(
            repo, commit, [("run.sh", "pkg/run.sh", data)]
        )
        with self.assertRaises(ValueError):
            export_candidate(
                manifest, self._dest(), allowed_output_parent=self.out_parent
            )
        self.assertFalse(os.path.lexists(self._dest()))

    def test_size_budget_enforced(self):
        repo, commit = _make_repo({"big.py": b"0123456789"})
        manifest = _manifest(
            repo, commit, [("big.py", "pkg/big.py", b"0123456789")],
            max_total_bytes=4,
        )
        with self.assertRaises(ValueError):
            validate_manifest(manifest)

    def test_no_overwrite_existing_destination(self):
        repo, commit = _make_repo({"a.py": b"a\n"})
        manifest = _manifest(repo, commit, [("a.py", "pkg/a.py", b"a\n")])
        os.makedirs(self._dest())
        with open(os.path.join(self._dest(), "keep.txt"), "w") as handle:
            handle.write("pre-existing")
        with self.assertRaises(ValueError):
            export_candidate(
                manifest, self._dest(), allowed_output_parent=self.out_parent
            )
        with open(os.path.join(self._dest(), "keep.txt")) as handle:
            self.assertEqual(handle.read(), "pre-existing")

    def test_destination_outside_parent_refused(self):
        repo, commit = _make_repo({"a.py": b"a\n"})
        manifest = _manifest(repo, commit, [("a.py", "pkg/a.py", b"a\n")])
        elsewhere = os.path.join(self.tmp.name, "elsewhere")
        with self.assertRaises(ValueError):
            export_candidate(
                manifest, elsewhere, allowed_output_parent=self.out_parent
            )
        self.assertFalse(os.path.lexists(elsewhere))

    def test_verify_refuses_extra_missing_changed(self):
        repo, commit = _make_repo({"a.py": b"a\n", "b.py": b"b\n"})
        manifest = _manifest(
            repo,
            commit,
            [("a.py", "pkg/a.py", b"a\n"), ("b.py", "pkg/b.py", b"b\n")],
        )
        receipt = export_candidate(
            manifest, self._dest(), allowed_output_parent=self.out_parent
        )
        with open(os.path.join(self._dest(), "pkg", "extra.py"), "w") as handle:
            handle.write("extra")
        with self.assertRaises(ValueError):
            verify_export(self._dest(), receipt)
        os.remove(os.path.join(self._dest(), "pkg", "extra.py"))
        os.remove(os.path.join(self._dest(), "pkg", "b.py"))
        with self.assertRaises(ValueError):
            verify_export(self._dest(), receipt)
        with open(os.path.join(self._dest(), "pkg", "b.py"), "w") as handle:
            handle.write("changed")
        with self.assertRaises(ValueError):
            verify_export(self._dest(), receipt)

    def test_verify_refuses_candidate_symlink(self):
        repo, commit = _make_repo({"a.py": b"a\n"})
        manifest = _manifest(repo, commit, [("a.py", "pkg/a.py", b"a\n")])
        receipt = export_candidate(
            manifest, self._dest(), allowed_output_parent=self.out_parent
        )
        link = os.path.join(self._dest(), "pkg", "link.py")
        try:
            os.symlink(os.path.join(self._dest(), "pkg", "a.py"), link)
        except OSError:
            self.skipTest("cannot create symlinks here")
        with self.assertRaises(ValueError):
            verify_export(self._dest(), receipt)

    def test_unknown_fields_and_shape_rejected(self):
        repo, commit = _make_repo({"a.py": b"a\n"})
        manifest = _manifest(repo, commit, [("a.py", "pkg/a.py", b"a\n")])
        manifest["extra"] = 1
        with self.assertRaises(ValueError):
            validate_manifest(manifest)
        manifest = _manifest(repo, commit, [("a.py", "pkg/a.py", b"a\n")])
        manifest["sources"][0]["files"][0]["mode"] = "100644"
        with self.assertRaises(ValueError):
            validate_manifest(manifest)
        manifest = _manifest(repo, commit, [("a.py", "pkg/a.py", b"a\n")])
        manifest["sources"].append(dict(manifest["sources"][0]))
        with self.assertRaises(ValueError):
            validate_manifest(manifest)
        manifest = _manifest(repo, commit, [("a.py", "pkg/a.py", b"a\n")])
        manifest["sources"][0]["commit"] = "HEAD"
        with self.assertRaises(ValueError):
            validate_manifest(manifest)


    def test_env_prefix_variants_rejected(self):
        # Regression: '.env*' means any leading '.env' segment, final or not.
        repo, commit = _make_repo({"a.py": b"a\n", ".envrc": b"a\n"})
        bad = [
            ".envrc",
            ".env.production",
            "cfg/.env/keys.txt",
            "cfg/credentials-backup",
            "cfg/secrets2.yaml",
            "cfg/auth-old.json",
        ]
        for target in bad:
            with self.subTest(target=target):
                with self.assertRaises(ValueError):
                    validate_manifest(
                        _manifest(repo, commit, [("a.py", target, b"a\n")])
                    )
        # Dangerous names are denied on source paths too.
        with self.assertRaises(ValueError):
            validate_manifest(
                _manifest(
                    repo, commit, [(".envrc", "pkg/ok.py", b"a\n")]
                )
            )

    def test_target_prefix_conflict_writes_nothing(self):
        # Regression: 'pkg' vs 'pkg/x.py' must fail before materialization.
        repo, commit = _make_repo({"f.py": b"f\n", "x.py": b"x\n"})
        manifest = _manifest(
            repo,
            commit,
            [("f.py", "pkg", b"f\n"), ("x.py", "pkg/x.py", b"x\n")],
        )
        with self.assertRaises(ValueError):
            validate_manifest(manifest)
        with self.assertRaises(ValueError):
            export_candidate(
                manifest, self._dest(), allowed_output_parent=self.out_parent
            )
        self.assertFalse(os.path.lexists(self._dest()))

    def test_casefold_prefix_conflict_writes_nothing(self):
        repo, commit = _make_repo({"f.py": b"f\n", "x.py": b"x\n"})
        manifest = _manifest(
            repo,
            commit,
            [("f.py", "PKG", b"f\n"), ("x.py", "pkg/x.py", b"x\n")],
        )
        with self.assertRaises(ValueError):
            validate_manifest(manifest)
        with self.assertRaises(ValueError):
            export_candidate(
                manifest, self._dest(), allowed_output_parent=self.out_parent
            )
        self.assertFalse(os.path.lexists(self._dest()))

    def test_export_reread_failure_raises_valueerror(self):
        # Fault injection: OS-level read failure must surface as ValueError.
        import builtins

        repo, commit = _make_repo({"a.py": b"a\n"})
        manifest = _manifest(repo, commit, [("a.py", "pkg/a.py", b"a\n")])
        real_open = builtins.open

        def _deny_reread(path, mode="r", *args, **kwargs):
            if "r" in mode and os.path.basename(str(path)) == "a.py":
                raise PermissionError("denied")
            return real_open(path, mode, *args, **kwargs)

        with unittest.mock.patch.object(builtins, "open", _deny_reread):
            with self.assertRaises(ValueError):
                export_candidate(
                    manifest,
                    self._dest(),
                    allowed_output_parent=self.out_parent,
                )

    def test_verify_unreadable_candidate_raises_valueerror(self):
        # Fault injection: OS-level read failure must surface as ValueError.
        import builtins

        repo, commit = _make_repo({"a.py": b"a\n"})
        manifest = _manifest(repo, commit, [("a.py", "pkg/a.py", b"a\n")])
        receipt = export_candidate(
            manifest, self._dest(), allowed_output_parent=self.out_parent
        )
        real_open = builtins.open

        def _deny_read(path, mode="r", *args, **kwargs):
            if "r" in mode and os.path.basename(str(path)) == "a.py":
                raise PermissionError("denied")
            return real_open(path, mode, *args, **kwargs)

        with unittest.mock.patch.object(builtins, "open", _deny_read):
            with self.assertRaises(ValueError):
                verify_export(self._dest(), receipt)


if __name__ == "__main__":
    unittest.main()
