import copy
import hashlib
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from caretaker import committed_inputs as gate


TEMP_ROOT = Path(os.environ.get("TEAM_TEST_ROOT", tempfile.gettempdir())) / 'committed-inputs-tests'


class CommittedInputsTests(unittest.TestCase):
    def setUp(self):
        TEMP_ROOT.mkdir(parents=True, exist_ok=True)
        self.tmp = tempfile.TemporaryDirectory(prefix="committed-inputs-", dir=TEMP_ROOT)
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.vault = self.root / "custom-vault"
        self.vault.mkdir()
        self.git("init", "--quiet")
        self.git("config", "user.email", "fixture@example.invalid")
        self.git("config", "user.name", "Fixture")
        self.git("config", "core.autocrlf", "false")
        self.frozen = {"vault_path": str(self.vault), "inputs": []}
        for name, body in (("proposals/one.md", b"proposal\n"),
                           ("raw/source.json", b'{"fixture":true}\n'),
                           ("raw/original.md", b"synthetic body\n")):
            path = self.vault / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(body)
            self.frozen["inputs"].append({"path": name,
                "sha256": hashlib.sha256(body).hexdigest(), "bytes": len(body)})
        self.git("add", ".")
        self.git("commit", "--quiet", "-m", "synthetic fixture")

    def git(self, *args):
        return subprocess.run(["git", "-C", str(self.root), *args], check=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=10,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout

    def test_three_committed_inputs(self):
        self.assertTrue(gate.inputs_match_head(self.frozen))

    def test_uncommitted_input(self):
        self.frozen["inputs"][0] = {"path": "new.md", "sha256": hashlib.sha256(b"new").hexdigest(), "bytes": 3}
        (self.vault / "new.md").write_bytes(b"new")
        self.assertFalse(gate.inputs_match_head(self.frozen))

    def test_worktree_alone_does_not_prove_frozen_content(self):
        path = self.vault / "proposals/one.md"
        path.write_bytes(b"new")
        self.frozen["inputs"][0].update(sha256=hashlib.sha256(b"new").hexdigest(), bytes=3)
        self.assertFalse(gate.inputs_match_head(self.frozen))

    def test_changed_head_blob(self):
        (self.vault / "proposals/one.md").write_bytes(b"changed committed blob")
        self.git("add", ".")
        self.git("commit", "--quiet", "-m", "changed synthetic blob")
        self.assertFalse(gate.inputs_match_head(self.frozen))

    def test_missing_head_blob_despite_existing_worktree_file(self):
        self.git("rm", "--cached", "custom-vault/proposals/one.md")
        self.git("commit", "--quiet", "-m", "remove synthetic tracked blob")
        self.assertFalse(gate.inputs_match_head(self.frozen))

    def test_malformed_entries(self):
        cases = [None, [], {}, [None], [dict(self.frozen["inputs"][0], bytes=True)],
                 [dict(self.frozen["inputs"][0], bytes=-1)],
                 [dict(self.frozen["inputs"][0], sha256="x" * 64)],
                 [dict(self.frozen["inputs"][0], sha256="0" * 64)],
                 [dict(self.frozen["inputs"][0], bytes=1)],
                 self.frozen["inputs"] * 2]
        for entries in cases:
            with self.subTest(entries=entries):
                candidate = dict(self.frozen, inputs=entries)
                self.assertFalse(gate.inputs_match_head(candidate))

    def test_unsafe_paths(self):
        for name in ("../escape", "/absolute", "a/../b", "a//b", "a/./b",
                     "C:/file", "a\\file", "one\nHEAD:other", "a\x00b", ":(top)file"):
            with self.subTest(path=name):
                candidate = copy.deepcopy(self.frozen)
                candidate["inputs"][0]["path"] = name
                self.assertFalse(gate.inputs_match_head(candidate))

    def test_vault_outside_repo(self):
        self.assertFalse(gate.inputs_match_head(dict(self.frozen, vault_path=str(TEMP_ROOT))))

    def test_vault_must_be_proper_subdirectory(self):
        self.assertFalse(gate.inputs_match_head(dict(self.frozen, vault_path=str(self.root))))

    def test_nonregular_vault(self):
        self.assertFalse(gate.inputs_match_head(dict(self.frozen, vault_path=str(self.vault / "raw/original.md"))))

    def test_nonregular_input(self):
        candidate = copy.deepcopy(self.frozen)
        candidate["inputs"][0]["path"] = "raw"
        self.assertFalse(gate.inputs_match_head(candidate))

    def test_linked_vault(self):
        link = self.root / "linked-vault"
        try:
            link.symlink_to(self.vault, target_is_directory=True)
        except OSError as exc:
            self.skipTest(f"host does not permit symlink fixture: {exc.winerror if hasattr(exc, 'winerror') else exc.errno}")
        self.assertFalse(gate.inputs_match_head(dict(self.frozen, vault_path=str(link))))

    def test_committed_symlink_blob(self):
        oid = self.git("rev-parse", "HEAD:custom-vault/raw/original.md").strip().decode()
        self.git("update-index", "--add", "--cacheinfo", f"120000,{oid},custom-vault/raw/original.md")
        self.git("commit", "--quiet", "-m", "synthetic nonregular blob")
        self.assertFalse(gate.inputs_match_head(self.frozen))

    def test_timeout_missing_git_and_no_head(self):
        for error in (subprocess.TimeoutExpired(["git"], 10), FileNotFoundError(),
                      subprocess.CalledProcessError(128, ["git"])):
            with self.subTest(error=type(error).__name__), patch.object(gate, "_git", side_effect=error):
                self.assertFalse(gate.inputs_match_head(self.frozen))

    def test_head_changed_during_read(self):
        first = gate._head(self.root)
        with patch.object(gate, "_head", side_effect=[first, b"0" * len(first)]):
            self.assertFalse(gate.inputs_match_head(self.frozen))

    def test_root_mismatch(self):
        real_git = gate._git
        def altered(cwd, *args, **kwargs):
            if args == ("rev-parse", "--show-prefix"):
                return b"wrong-vault/\n"
            return real_git(cwd, *args, **kwargs)
        with patch.object(gate, "_git", side_effect=altered):
            self.assertFalse(gate.inputs_match_head(self.frozen))

    def test_count_and_byte_bounds(self):
        candidate = dict(self.frozen, inputs=[self.frozen["inputs"][0]] * (gate.MAX_INPUTS + 1))
        self.assertFalse(gate.inputs_match_head(candidate))
        candidate = copy.deepcopy(self.frozen)
        candidate["inputs"][0]["bytes"] = gate.MAX_INPUT_BYTES + 1
        self.assertFalse(gate.inputs_match_head(candidate))
        with patch.object(gate, "MAX_TOTAL_BYTES", 1):
            self.assertFalse(gate.inputs_match_head(self.frozen))

    def test_git_environment_and_hidden_argv(self):
        with patch.dict(os.environ, {"GIT_DIR": "untrusted", "GIT_CONFIG_COUNT": "1"}), patch.object(gate.subprocess, "run") as run:
            run.return_value.stdout = b"result"
            self.assertEqual(gate._git(self.root, "rev-parse", "HEAD"), b"result")
            args, kwargs = run.call_args
            self.assertIsInstance(args[0], list)
            self.assertFalse(kwargs["shell"])
            self.assertNotIn("GIT_DIR", kwargs["env"])
            self.assertNotIn("GIT_CONFIG_COUNT", kwargs["env"])
            self.assertEqual(kwargs["creationflags"], getattr(subprocess, "CREATE_NO_WINDOW", 0))


if __name__ == "__main__":
    unittest.main()
