"""Offline Mistral dry-run qualification tests. Synthetic fixtures only; no inference or cleanup."""
from __future__ import annotations

import copy
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import uuid

import fleet
import mistral_runtime as runtime
import routing_v2

tools = runtime.tool_module()


class MistralDryRunBase(unittest.TestCase):
    def setUp(self):
        artifacts = Path(tempfile.gettempdir()).resolve()
        if artifacts.is_relative_to(Path(__file__).resolve().parents[2]):
            raise RuntimeError("Synthetic test temp root must be outside the release payload")
        self.root = artifacts / ("fleet-mistral-dryrun-" + uuid.uuid4().hex)
        self.root.mkdir(exist_ok=False)
        self.workspace = self.root / "workspace"
        self.output_root = self.root / "output"
        self.workspace.mkdir()
        self.output_root.mkdir()
        (self.workspace / "src").mkdir()
        (self.workspace / "src" / "app.py").write_text("print('hello')\n", encoding="utf-8", newline="\n")
        (self.workspace / "input.txt").write_text("sample input\n", encoding="utf-8", newline="\n")
        self.dispatch = {
            "scope": ["src"],
            "inputs": ["input.txt"],
            "checks": [["python", "-B", "-m", "unittest", "discover", "-s", "src"]],
        }
        self.profiles = fleet.load_profiles(fleet.DEFAULT_PROFILES)
        self.profile = self.profiles["mistral"]

    def offline_context(self):
        """Context manager patching only external readiness/quota/probe effects."""
        return _OfflineContext()


class _OfflineContext:
    def __init__(self):
        self.patches = [
            patch.object(fleet, "resolve_executable", lambda profile: runtime.MISTRAL_NATIVE_EXECUTABLE),
            patch.object(fleet, "probe_command", lambda *args, **kwargs: {
                "launchable": True, "status": "ok", "version": "2.26.1", "output": "vibe 2.26.1\n",
            }),
            patch.object(fleet, "current_quota_views", lambda root, profiles, **kwargs: (
                {"mistral": {"status": "ok", "headroom": 100, "confidence": 1.0}},
                {"records": []}
            )),
            patch.object(routing_v2, "quota_hard_gates", lambda profile, quota_before, **kwargs: []),
            patch.object(routing_v2, "runtime_model_visible", lambda selected_execution, **kwargs: (True, "visible")),
        ]

    def __enter__(self):
        for p in self.patches:
            p.start()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        for p in reversed(self.patches):
            p.stop()


class TestMistralDryRun(MistralDryRunBase):
    def test_regression_planned_run_dir_and_worktree_dry_run_success(self):
        """RSI e0a43b baseline regression: planned run_dir and worktree do not exist on disk,
        but dry_run must succeed without materializing directories/files, reporting intended future paths.
        """
        run_id = "run-20261025T000000Z-e0a43b0001"
        planned_run = self.output_root / run_id
        planned_wt = planned_run / "worktree"
        self.assertFalse(planned_run.exists())
        self.assertFalse(planned_wt.exists())

        task_text = f"Implement task payload: {json.dumps(self.dispatch)}"
        with self.offline_context():
            result = fleet.execute_single(
                task_text,
                profiles=self.profiles,
                agent_id="mistral",
                mode="implement",
                kind="implementation",
                complexity="medium",
                model_override=None,
                workspace=self.workspace,
                timeout=300,
                allow_write=True,
                native_subagent_limit=0,
                enable_web=False,
                inherit_api_keys=False,
                dry_run=True,
                output_root=self.output_root,
                root=self.root,
                run_id_override=run_id,
                mistral_tool_dispatch=self.dispatch,
            )

        self.assertEqual(result.get("action"), "run")
        self.assertEqual(result.get("status"), "DRY_RUN")
        self.assertEqual(result.get("execution_workspace"), str(planned_wt))
        self.assertEqual(result.get("output_dir"), str(planned_run))
        self.assertTrue(result.get("worktree_planned"))
        self.assertFalse(result.get("worktree_created"))
        # Verify no directories or files were materialized on disk
        self.assertFalse(planned_run.exists())
        self.assertFalse(planned_wt.exists())

    def test_oversized_existing_file_refusal_via_size_admission(self):
        """A file in scope whose existing size exceeds max_write_bytes must be refused via size admission."""
        oversized_file = self.workspace / "src" / "big.py"
        limit = tools.DEFAULT_LIMITS["max_write_bytes"]
        oversized_file.write_bytes(b"x" * (limit + 1))

        task_text = f"Implement task payload: {json.dumps(self.dispatch)}"
        with self.offline_context():
            with self.assertRaises(fleet.FleetError) as cm:
                fleet.execute_single(
                    task_text,
                    profiles=self.profiles,
                    agent_id="mistral",
                    mode="implement",
                    kind="implementation",
                    complexity="medium",
                    model_override=None,
                    workspace=self.workspace,
                    timeout=300,
                    allow_write=True,
                    native_subagent_limit=0,
                    enable_web=False,
                    inherit_api_keys=False,
                    dry_run=True,
                    output_root=self.output_root,
                    root=self.root,
                    mistral_tool_dispatch=self.dispatch,
                )
            self.assertIn("per-write bound", str(cm.exception))

    def test_scoped_small_writable_file_success(self):
        """Scoped small writable files validate cleanly and dry-run succeeds."""
        (self.workspace / "src" / "module.py").write_text("a = 1\n", encoding="utf-8", newline="\n")
        task_text = f"Implement task payload: {json.dumps(self.dispatch)}"
        with self.offline_context():
            result = fleet.execute_single(
                task_text,
                profiles=self.profiles,
                agent_id="mistral",
                mode="implement",
                kind="implementation",
                complexity="medium",
                model_override=None,
                workspace=self.workspace,
                timeout=300,
                allow_write=True,
                native_subagent_limit=0,
                enable_web=False,
                inherit_api_keys=False,
                dry_run=True,
                output_root=self.output_root,
                root=self.root,
                mistral_tool_dispatch=self.dispatch,
            )
        self.assertEqual(result.get("status"), "DRY_RUN")

    def test_malformed_traversal_and_protected_dispatch_refusal(self):
        """Malformed, traversal, and protected dispatch entries must fail closed."""
        cases = [
            ("traversal", {"scope": ["../escape"], "inputs": [], "checks": [["python", "-V"]]}),
            ("protected_git", {"scope": [".git"], "inputs": [], "checks": [["python", "-V"]]}),
            ("protected_env", {"scope": [".env"], "inputs": [], "checks": [["python", "-V"]]}),
            ("reserved_device", {"scope": ["CON"], "inputs": [], "checks": [["python", "-V"]]}),
            ("malformed_check", {"scope": ["src"], "inputs": [], "checks": [["python", "\0invalid"]]}),
            ("empty_scope_implement", {"scope": [], "inputs": ["input.txt"], "checks": [["python", "-V"]]}),
        ]
        with self.offline_context():
            for name, bad_dispatch in cases:
                with self.subTest(case=name):
                    task_text = f"Implement task payload: {json.dumps(bad_dispatch)}"
                    with self.assertRaises(fleet.FleetError):
                        fleet.execute_single(
                            task_text,
                            profiles=self.profiles,
                            agent_id="mistral",
                            mode="implement",
                            kind="implementation",
                            complexity="medium",
                            model_override=None,
                            workspace=self.workspace,
                            timeout=300,
                            allow_write=True,
                            native_subagent_limit=0,
                            enable_web=False,
                            inherit_api_keys=False,
                            dry_run=True,
                            output_root=self.output_root,
                            root=self.root,
                            mistral_tool_dispatch=bad_dispatch,
                        )

    def test_invalid_model_refusal(self):
        """Model other than mistral-large-4 must be refused immediately."""
        task_text = f"Implement task payload: {json.dumps(self.dispatch)}"
        with self.offline_context():
            with self.assertRaises(fleet.FleetError) as cm:
                fleet.execute_single(
                    task_text,
                    profiles=self.profiles,
                    agent_id="mistral",
                    mode="implement",
                    kind="implementation",
                    complexity="medium",
                    model_override="mistral-medium",
                    workspace=self.workspace,
                    timeout=300,
                    allow_write=True,
                    native_subagent_limit=0,
                    enable_web=False,
                    inherit_api_keys=False,
                    dry_run=True,
                    output_root=self.output_root,
                    root=self.root,
                    mistral_tool_dispatch=self.dispatch,
                )
            self.assertIn("mistral-large-4", str(cm.exception))

    def test_missing_implement_dispatch_refusal(self):
        """Implement mode without trusted dispatch must be refused."""
        with self.offline_context():
            with self.assertRaises(fleet.FleetError) as cm:
                fleet.execute_single(
                    "Plain text task without dispatch payload",
                    profiles=self.profiles,
                    agent_id="mistral",
                    mode="implement",
                    kind="implementation",
                    complexity="medium",
                    model_override=None,
                    workspace=self.workspace,
                    timeout=300,
                    allow_write=True,
                    native_subagent_limit=0,
                    enable_web=False,
                    inherit_api_keys=False,
                    dry_run=True,
                    output_root=self.output_root,
                    root=self.root,
                    mistral_tool_dispatch=None,
                )
            self.assertIn("requires trusted tool dispatch", str(cm.exception))

    def test_strict_non_dry_run_validation_enforced(self):
        """Non-dry-run validation must remain strict: nonexistent directories and preview policies on disk are refused."""
        # Workspace must exist and be an existing directory
        nonexistent = self.root / "does_not_exist_workspace"
        with self.assertRaises(tools.ToolPolicyError):
            tools.validate_workspace(str(nonexistent))

        # validate_policy in non-preview mode requires existing run_dir
        dummy_policy = {
            "schema": tools.POLICY_SCHEMA,
            "mode": "review",
            "workspace": str(self.workspace),
            "run_dir": str(self.output_root / "nonexistent_run"),
            "scope": ["src"],
            "inputs": ["input.txt"],
            "checks": [["python", "-V"]],
            "limits": dict(tools.DEFAULT_LIMITS),
            "runtime": dict(tools.PINNED_RUNTIME),
            "tools": tools.tools_for("review", [["python", "-V"]]),
        }
        with self.assertRaises(tools.ToolPolicyError):
            tools.validate_policy(dummy_policy, preview=False)

        # Non-preview validate_policy refuses preview key
        preview_policy = dict(dummy_policy, preview=True)
        with self.assertRaises(tools.ToolPolicyError):
            tools.validate_policy(preview_policy, preview=False)

        # write_policy refuses preview policy
        with self.assertRaises(tools.ToolPolicyError):
            tools.write_policy(preview_policy)

        # load_policy refuses preview policy on disk
        fake_run = self.root / "fake_run"
        fake_run.mkdir()
        policy_path = fake_run / tools.POLICY_FILENAME
        policy_path.write_text(json.dumps(dict(dummy_policy, run_dir=str(fake_run), preview=True)), encoding="utf-8")
        with self.assertRaises(tools.ToolPolicyError):
            tools.load_policy(policy_path)

    def test_review_and_advisory_modes_dry_run(self):
        """Review and advisory modes dry-run cleanly with planned run_dir."""
        with self.offline_context():
            # Review with dispatch
            res_review = fleet.execute_single(
                f"Review: {json.dumps(self.dispatch)}",
                profiles=self.profiles,
                agent_id="mistral",
                mode="review",
                kind="review",
                complexity="medium",
                model_override=None,
                workspace=self.workspace,
                timeout=300,
                allow_write=False,
                native_subagent_limit=0,
                enable_web=False,
                inherit_api_keys=False,
                dry_run=True,
                output_root=self.output_root,
                root=self.root,
                mistral_tool_dispatch=self.dispatch,
            )
            self.assertEqual(res_review.get("status"), "DRY_RUN")

            # Advisory without dispatch (prompt-only)
            res_advisory = fleet.execute_single(
                "Advisory task without dispatch",
                profiles=self.profiles,
                agent_id="mistral",
                mode="advisory",
                kind="architecture",
                complexity="medium",
                model_override=None,
                workspace=self.workspace,
                timeout=300,
                allow_write=False,
                native_subagent_limit=0,
                enable_web=False,
                inherit_api_keys=False,
                dry_run=True,
                output_root=self.output_root,
                root=self.root,
                mistral_tool_dispatch=None,
            )
            self.assertEqual(res_advisory.get("status"), "DRY_RUN")

    def test_reused_existing_implementation_worktree(self):
        """When existing_execution_workspace is supplied, dry-run validates against it."""
        # Create an existing execution worktree
        reused_worktree = self.root / "reused_worktree"
        reused_worktree.mkdir()
        (reused_worktree / "src").mkdir()
        (reused_worktree / "src" / "app.py").write_text("print('reused')\n", encoding="utf-8", newline="\n")
        (reused_worktree / "input.txt").write_text("input\n", encoding="utf-8", newline="\n")

        # Mock validate_reused_execution_workspace to avoid requiring git repo on synthetic temp dir
        task_text = f"Implement task: {json.dumps(self.dispatch)}"
        with self.offline_context():
            with patch.object(fleet, "validate_reused_execution_workspace", return_value=(reused_worktree, reused_worktree)):
                with patch.object(fleet, "validated_base_provenance", return_value=("a" * 40, "recorded")):
                    res = fleet.execute_single(
                        task_text,
                        profiles=self.profiles,
                        agent_id="mistral",
                        mode="implement",
                        kind="implementation",
                        complexity="medium",
                        model_override=None,
                        workspace=self.workspace,
                        timeout=300,
                        allow_write=True,
                        native_subagent_limit=0,
                        enable_web=False,
                        inherit_api_keys=False,
                        dry_run=True,
                        output_root=self.output_root,
                        root=self.root,
                        existing_execution_workspace=reused_worktree,
                        preserved_base_commit_sha="a" * 40,
                        preserved_base_provenance="recorded",
                        mistral_tool_dispatch=self.dispatch,
                    )
                    self.assertEqual(res.get("status"), "DRY_RUN")
                    self.assertEqual(res.get("execution_workspace"), str(reused_worktree))

                    # Oversized file inside reused worktree must be refused via size admission
                    oversized = reused_worktree / "src" / "huge.py"
                    oversized.write_bytes(b"z" * (tools.DEFAULT_LIMITS["max_write_bytes"] + 5))
                    with self.assertRaises(fleet.FleetError) as cm:
                        fleet.execute_single(
                            task_text,
                            profiles=self.profiles,
                            agent_id="mistral",
                            mode="implement",
                            kind="implementation",
                            complexity="medium",
                            model_override=None,
                            workspace=self.workspace,
                            timeout=300,
                            allow_write=True,
                            native_subagent_limit=0,
                            enable_web=False,
                            inherit_api_keys=False,
                            dry_run=True,
                            output_root=self.output_root,
                            root=self.root,
                            existing_execution_workspace=reused_worktree,
                            preserved_base_commit_sha="a" * 40,
                            preserved_base_provenance="recorded",
                            mistral_tool_dispatch=self.dispatch,
                        )
                    self.assertIn("per-write bound", str(cm.exception))


if __name__ == "__main__":
    unittest.main()
