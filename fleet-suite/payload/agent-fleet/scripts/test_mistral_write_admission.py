"""HD-997f36 admission regression tests for native Mistral writable scope sizes.

The native Mistral full-file writer truncates and rewrites each in-scope file
with content bounded by ``max_write_bytes`` (524288). If an existing file in
the writable scope is larger than that bound, the writer cannot edit it
end-to-end without losing content it cannot read. The admission check added to
``mistral_tools.validate_policy`` refuses such scopes before any native
inference; the tests below cover the exact cases the helpdesk ticket names.

Synthetic fixtures only; no auth, inference or cleanup. Inherited harness temp
roots (``TEMP``/``TEAM_TEST_ROOT``) remain the only allowed artifact locations.
"""
from __future__ import annotations

import os
from pathlib import Path
import tempfile
import unittest
import uuid

import fleet
import mistral_runtime as runtime

tools = runtime.tool_module()


class Fixture(unittest.TestCase):
    def setUp(self):
        # Synthetic evidence lives in OS temp; the immutable payload is excluded.
        artifacts = Path(tempfile.gettempdir()).resolve()
        if artifacts.is_relative_to(Path(__file__).resolve().parents[2]):
            raise RuntimeError("Synthetic test temp root must be outside the release payload")
        self.root = artifacts / ("fleet-mistral-write-admission-" + uuid.uuid4().hex)
        self.root.mkdir(exist_ok=False)
        self.workspace = self.root / "workspace"
        self.run = self.root / "run"
        self.workspace.mkdir()
        self.run.mkdir()
        (self.workspace / "src").mkdir()
        self.dispatch = {"scope": ["src"], "inputs": [], "checks": []}

    def refused(self, fn, *args, **kwargs):
        with self.assertRaises(tools.ToolPolicyError):
            fn(*args, **kwargs)


class WriteAdmissionBoundaryTests(Fixture):
    def test_exact_max_write_bytes_file_is_admitted(self):
        limit = tools.DEFAULT_LIMITS["max_write_bytes"]
        target = self.workspace / "src/exact.py"
        target.write_bytes(b"x" * limit)
        self.assertEqual(target.stat().st_size, limit)
        policy = tools.build_policy(workspace=self.workspace, run_dir=self.run,
                                    mode="implement", **self.dispatch)
        # Exact-bound file is accepted; the writer may fully replace it.
        result = tools.write_text(policy, "src/exact.py", "ok\n", True)
        self.assertFalse(result["created"])
        self.assertEqual(target.read_text(encoding="utf-8"), "ok\n")

    def test_one_byte_over_max_write_bytes_is_refused_at_bind(self):
        limit = tools.DEFAULT_LIMITS["max_write_bytes"]
        target = self.workspace / "src/over.py"
        target.write_bytes(b"x" * (limit + 1))
        self.refused(tools.build_policy, workspace=self.workspace, run_dir=self.run,
                     mode="implement", **self.dispatch)

    def test_recorded_931549_byte_file_is_refused_at_bind(self):
        target = self.workspace / "src/large.py"
        target.write_bytes(b"x" * 931_549)
        self.refused(tools.build_policy, workspace=self.workspace, run_dir=self.run,
                     mode="implement", **self.dispatch)

    def test_existing_oversized_file_is_not_altered_by_refusal(self):
        # Refusal at bind must be observation-only; the offending file is unchanged.
        limit = tools.DEFAULT_LIMITS["max_write_bytes"]
        target = self.workspace / "src/large.py"
        body = b"x" * (limit + 9)
        target.write_bytes(body)
        original = target.read_bytes()
        self.refused(tools.build_policy, workspace=self.workspace, run_dir=self.run,
                     mode="implement", **self.dispatch)
        self.assertEqual(target.read_bytes(), original)
        self.assertEqual(target.stat().st_size, len(body))


class WriteAdmissionScopeShapeTests(Fixture):
    def test_directory_nested_oversized_file_is_refused(self):
        limit = tools.DEFAULT_LIMITS["max_write_bytes"]
        nested = self.workspace / "src" / "nested"
        nested.mkdir()
        # A small admitted sibling must not shadow the larger offender.
        (nested / "ok.py").write_text("ok\n", encoding="utf-8")
        (nested / "big.py").write_bytes(b"x" * (limit + 1))
        self.refused(tools.build_policy, workspace=self.workspace, run_dir=self.run,
                     mode="implement", **self.dispatch)

    def test_missing_scope_path_is_preserved_at_bind(self):
        # A scope entry that does not yet exist must remain admitted; the writer creates it.
        dispatch = {"scope": ["src/never.py"], "inputs": [], "checks": []}
        policy = tools.build_policy(workspace=self.workspace, run_dir=self.run,
                                    mode="implement", **dispatch)
        result = tools.write_text(policy, "src/never.py", "# created\n")
        self.assertTrue(result["created"])
        self.assertTrue((self.workspace / "src/never.py").is_file())

    def test_existing_small_file_can_still_be_replaced(self):
        # The admission check must not regress the existing replace workflow.
        target = self.workspace / "src/small.py"
        target.write_text("answer = 42\n", encoding="utf-8", newline="\n")
        policy = tools.build_policy(workspace=self.workspace, run_dir=self.run,
                                    mode="implement", **self.dispatch)
        tools.write_text(policy, "src/small.py", "answer = 43\n", True)
        self.assertEqual(target.read_text(encoding="utf-8"), "answer = 43\n")


class WriteAdmissionInputsAndModesTests(Fixture):
    def test_read_only_inputs_oversized_still_admitted(self):
        # Inputs are read-only; an oversized input must NOT block admission or reads.
        limit = tools.DEFAULT_LIMITS["max_write_bytes"]
        (self.workspace / "big_input.txt").write_bytes(b"x" * (limit + 7))
        (self.workspace / "src/small.py").write_text("ok\n", encoding="utf-8")
        dispatch = {"scope": ["src"], "inputs": ["big_input.txt"], "checks": []}
        policy = tools.build_policy(workspace=self.workspace, run_dir=self.run,
                                    mode="implement", **dispatch)
        # Writers still touch the in-scope small file.
        tools.write_text(policy, "src/small.py", "ok\n", True)
        # Reading the oversized input remains allowed; the read bound is max_file_bytes.
        result = tools.read_text(policy, "big_input.txt")
        self.assertEqual(result["path"], "big_input.txt")
        self.assertTrue(result["content"])

    def test_review_mode_admits_oversized_scope_files(self):
        # Advisory/review modes never write; size admission is skipped.
        limit = tools.DEFAULT_LIMITS["max_write_bytes"]
        target = self.workspace / "src/large.py"
        target.write_bytes(b"x" * (limit + 1))
        policy = tools.build_policy(workspace=self.workspace, run_dir=self.run,
                                    mode="review", **self.dispatch)
        self.assertEqual(policy["mode"], "review")
        # The read-only reviewer can read the oversized file.
        tools.read_text(policy, "src/large.py")


class WriteAdmissionTraversalTests(Fixture):
    def test_read_only_input_count_does_not_consume_writable_scan_budget(self):
        (self.workspace / "src/small.py").write_text("ok\n", encoding="utf-8")
        inputs = self.workspace / "inputs"
        inputs.mkdir()
        for index in range(3):
            (inputs / f"{index}.txt").write_text("read only\n", encoding="utf-8")
        policy = tools.build_policy(workspace=self.workspace, run_dir=self.run,
                                    mode="implement", scope=["src/small.py"],
                                    inputs=["inputs"], checks=[])
        policy["limits"]["snapshot_max_files"] = 1
        self.assertEqual(tools.validate_policy(policy)["scope"], ["src/small.py"])

    def test_truncated_writable_walk_is_refused(self):
        for index in range(2):
            (self.workspace / "src" / f"{index}.py").write_text("ok\n", encoding="utf-8")
        policy = tools.build_policy(workspace=self.workspace, run_dir=self.run,
                                    mode="implement", **self.dispatch)
        policy["limits"]["snapshot_max_files"] = 1
        self.refused(tools.validate_policy, policy)

    def test_exact_existing_file_scope_is_refused_before_native_dispatch(self):
        target = self.workspace / "src/large.py"
        target.write_bytes(b"x" * 931_549)
        with self.assertRaises(runtime.MistralRuntimeError):
            runtime.bind_tool_policy("synthetic", workspace=self.workspace, run_dir=self.run,
                                     mode="implement", dispatch={"scope": ["src/large.py"],
                                                                 "inputs": [], "checks": []})


if __name__ == "__main__":
    unittest.main()
