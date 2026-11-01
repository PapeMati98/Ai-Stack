"""RSI HD-b1b028 regression tests for benign dot-prefixed path dispatch.

``normalize_relative`` used to compare every component against
``part.strip(" .")``, which refused ordinary dot-prefixed workspace paths such
as ``.rsi/run_checks.py``. The live reproduction bound inputs/checks under
``.rsi`` and both implement and review lanes died with
``MistralRuntimeError: Native tool dispatch policy refused``.

These tests pin the narrowed alias guard: leading dots are ordinary names,
while trailing dots/spaces, leading spaces, ``.``/``..`` traversal, reserved
device names, short-name aliases, protected names and secret/credential
patterns stay refused. Fixtures are synthetic, use PATH STRINGS ONLY for
credential patterns, are retained (never deleted) and live under the harness
temp root. No provider, auth or native inference is touched.
"""
from __future__ import annotations

from pathlib import Path
import tempfile
import unittest
import uuid

import fleet
import mistral_runtime as runtime

tools = runtime.tool_module()

# Path strings only; no such file is ever created or read.
CREDENTIAL_STRINGS = ("secrets.json", "credentials.md", "my_password.txt",
                      "app.cookies.txt", "id_rsa_fixture", "token.json",
                      "oauth_creds.json", "private.key", "server.pem")
PROTECTED_STRINGS = (".git", ".agents", ".vibe", ".env", ".env.production",
                     ".ssh", ".claude", ".codex", "auth.json", "auth-tokens.json")


class Fixture(unittest.TestCase):
    def setUp(self):
        artifacts = Path(tempfile.gettempdir()).resolve()
        if artifacts.is_relative_to(Path(__file__).resolve().parents[2]):
            raise RuntimeError("Synthetic test temp root must be outside the release payload")
        self.root = artifacts / ("fleet-mistral-dot-paths-" + uuid.uuid4().hex)
        self.root.mkdir(exist_ok=False)  # retained: no TemporaryDirectory cleanup
        self.workspace = self.root / "workspace"
        self.run = self.root / "run"
        self.workspace.mkdir()
        self.run.mkdir()
        # Benign dot-prefixed directory plus a nested benign dot file name.
        (self.workspace / ".rsi").mkdir()
        (self.workspace / ".rsi/nested").mkdir()
        (self.workspace / ".rsi/run_checks.py").write_text("SYNTHETIC-RSI\n", encoding="utf-8")
        (self.workspace / ".rsi/nested/.baseline-fleet.py").write_text("SYNTHETIC-BASE\n", encoding="utf-8")
        (self.workspace / ".rsi/src").mkdir()
        (self.workspace / ".rsi/src/notes.txt").write_text("SYNTHETIC-NOTES\n", encoding="utf-8")
        self.dispatch = {"scope": [".rsi"], "inputs": [".rsi/run_checks.py",
                                                       ".rsi/nested/.baseline-fleet.py"],
                         "checks": [["python", "-B", ".rsi/run_checks.py"]]}

    def refused(self, fn, *args, **kwargs):
        with self.assertRaises(tools.ToolPolicyError):
            fn(*args, **kwargs)

    def implement(self, **override):
        payload = dict(self.dispatch)
        payload.update(override)
        return tools.build_policy(workspace=self.workspace, run_dir=self.run,
                                  mode="implement", **payload)


class BenignDotPathDispatchTests(Fixture):
    def test_dot_inputs_and_checks_are_admitted_verbatim_in_implement(self):
        policy = self.implement()
        self.assertEqual(policy["scope"], [".rsi"])
        self.assertEqual(policy["inputs"], [".rsi/run_checks.py", ".rsi/nested/.baseline-fleet.py"])
        self.assertEqual(policy["checks"], [["python", "-B", ".rsi/run_checks.py"]])
        self.assertEqual(policy["tools"], list(tools.MAKER_TOOLS))

    def test_dot_paths_are_read_written_and_listed(self):
        policy = self.implement()
        self.assertIn("SYNTHETIC-RSI", tools.read_text(policy, ".rsi/run_checks.py")["content"])
        self.assertIn("SYNTHETIC-BASE", tools.read_text(policy, ".rsi/nested/.baseline-fleet.py")["content"])
        written = tools.write_text(policy, ".rsi/src/notes.txt", "SYNTHETIC-EDITED\n", True)
        self.assertFalse(written["created"])
        self.assertEqual((self.workspace / ".rsi/src/notes.txt").read_text(encoding="utf-8"),
                         "SYNTHETIC-EDITED\n")
        created = tools.write_text(policy, ".rsi/src/.created.py", "# SYNTHETIC\n")
        self.assertTrue(created["created"])
        entries = tools.list_files(policy)["entries"]
        self.assertIn(".rsi/run_checks.py", entries)
        self.assertIn(".rsi/nested/.baseline-fleet.py", entries)
        self.assertIn(".rsi/src/.created.py", entries)

    def test_dot_paths_bind_and_read_in_review_and_advisory(self):
        for mode in ("review", "advisory"):
            with self.subTest(mode=mode):
                policy = tools.build_policy(workspace=self.workspace, run_dir=self.run,
                                            mode=mode, scope=[".rsi"],
                                            inputs=[".rsi/run_checks.py"], checks=[])
                self.assertEqual(policy["mode"], mode)
                self.assertEqual(policy["tools"], list(tools.READ_ONLY_TOOLS))
                self.assertIn("SYNTHETIC-RSI", tools.read_text(policy, ".rsi/run_checks.py")["content"])
                self.refused(tools.write_text, policy, ".rsi/src/notes.txt", "nope", True)

    def test_other_ordinary_leading_dot_names_are_admitted(self):
        policy = self.implement()
        for name in (".gitignore", ".editorconfig", ".hidden.py"):
            with self.subTest(name=name):
                self.assertEqual(tools.normalize_relative(name), name)
                result = tools.write_text(policy, ".rsi/src/" + name, "SYNTHETIC\n")
                self.assertTrue(result["created"])


class AliasAndTraversalStillRefusedTests(Fixture):
    def test_trailing_dot_and_space_and_leading_space_aliases_stay_refused(self):
        policy = self.implement()
        for name in (".rsi/x.py.", ".rsi/x.py ", ".rsi/ x.py", " x.py",
                     ".rsi/.x.py.", ".rsi/sub /x.py", ".rsi/nested/.y.py."):
            with self.subTest(name=name):
                self.refused(tools.resolve_admitted, policy, name)

    def test_empty_current_and_parent_components_stay_refused(self):
        policy = self.implement()
        for name in (".", "..", ".rsi/./x.py", ".rsi/../x.py", ".rsi//x.py", ""):
            with self.subTest(name=name):
                self.refused(tools.normalize_relative, name)

    def test_reserved_device_and_short_name_aliases_stay_refused(self):
        policy = self.implement()
        for name in (".rsi/NUL", ".rsi/CON.py", ".rsi/src~1.py"):
            with self.subTest(name=name):
                self.refused(tools.resolve_admitted, policy, name)

    def test_protected_names_embedded_under_benign_dot_paths_stay_refused(self):
        policy = self.implement()
        for name in PROTECTED_STRINGS:
            with self.subTest(name=name):
                self.assertTrue(tools.is_protected_name(name))
                self.refused(tools.resolve_admitted, policy, ".rsi/" + name)
                self.refused(tools.normalize_relative, ".rsi/nested/" + name)

    def test_protected_directories_cannot_be_listed_or_read(self):
        for name in (".git", ".agents", ".vibe", ".ssh"):
            with self.subTest(name=name):
                (self.workspace / ".rsi" / name).mkdir()
                (self.workspace / ".rsi" / name / "state.json").write_text("SYNTHETIC\n", encoding="utf-8")
        policy = self.implement()
        entries = tools.list_files(policy)["entries"]
        for name in (".git", ".agents", ".vibe", ".ssh"):
            self.assertNotIn(f".rsi/{name}/state.json", entries)
        self.refused(tools.read_text, policy, ".rsi/.git/state.json")
        self.refused(tools.write_text, policy, ".rsi/.vibe/state.json", "nope", True)

    def test_credential_path_strings_stay_refused_without_creating_them(self):
        policy = self.implement()
        for name in CREDENTIAL_STRINGS:
            with self.subTest(name=name):
                self.assertTrue(tools.is_protected_name(name))
                self.refused(tools.resolve_admitted, policy, ".rsi/" + name)
                self.assertFalse((self.workspace / ".rsi" / name).exists())

    def test_input_only_scope_entries_stay_read_only(self):
        policy = self.implement(scope=[".rsi/src"])
        self.refused(tools.write_text, policy, ".rsi/run_checks.py", "changed", True)
        self.refused(tools.write_text, policy, ".rsi/nested/.baseline-fleet.py", "changed", True)
        tools.write_text(policy, ".rsi/src/notes.txt", "ok\n", True)


class DotPathWriteAdmissionTests(Fixture):
    def test_oversized_dot_path_file_is_still_refused_at_bind(self):
        limit = tools.DEFAULT_LIMITS["max_write_bytes"]
        target = self.workspace / ".rsi/src/large.py"
        target.write_bytes(b"x" * (limit + 1))
        self.refused(self.implement)

    def test_oversized_dot_path_file_is_admitted_for_review_and_inputs(self):
        limit = tools.DEFAULT_LIMITS["max_write_bytes"]
        target = self.workspace / ".rsi/src/large.py"
        target.write_bytes(b"x" * (limit + 1))
        review = tools.build_policy(workspace=self.workspace, run_dir=self.run, mode="review",
                                    scope=[".rsi"], inputs=[".rsi/run_checks.py"], checks=[])
        self.assertEqual(review["mode"], "review")
        advisory = tools.build_policy(workspace=self.workspace, run_dir=self.run, mode="advisory",
                                      scope=[], inputs=[".rsi/run_checks.py"], checks=[])
        self.assertEqual(advisory["mode"], "advisory")

    def test_actual_writer_limit_is_unchanged_for_dot_paths(self):
        policy = self.implement(scope=[".rsi/src"])
        limit = policy["limits"]["max_write_bytes"]
        self.assertEqual(limit, 524_288)
        tools.write_text(policy, ".rsi/src/big.py", "x" * limit, True)
        self.refused(tools.write_text, policy, ".rsi/src/big.py", "x" * (limit + 1), True)
        self.assertEqual((self.workspace / ".rsi/src/big.py").stat().st_size, limit)


class RuntimeDispatchRegressionTests(Fixture):
    def test_bind_tool_policy_accepts_dot_paths_in_every_mode(self):
        for mode in ("implement", "review", "advisory"):
            with self.subTest(mode=mode):
                scope = [".rsi"] if mode == "implement" else [".rsi"]
                policy = runtime.bind_tool_policy(
                    "synthetic", workspace=self.workspace, run_dir=self.run, mode=mode,
                    dispatch={"scope": scope,
                              "inputs": [".rsi/run_checks.py", ".rsi/nested/.baseline-fleet.py"],
                              "checks": [["python", "-B", ".rsi/run_checks.py"]]})
                self.assertEqual(policy["mode"], mode)
                self.assertIn(".rsi/run_checks.py", policy["inputs"])

    def test_bind_tool_policy_still_refuses_protected_dot_paths(self):
        with self.assertRaises(runtime.MistralRuntimeError):
            runtime.bind_tool_policy("synthetic", workspace=self.workspace, run_dir=self.run,
                                     mode="implement",
                                     dispatch={"scope": [".rsi"], "inputs": [".rsi/.git/config"],
                                               "checks": []})


if __name__ == "__main__":
    unittest.main()
