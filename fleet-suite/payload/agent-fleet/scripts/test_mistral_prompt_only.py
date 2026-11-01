"""Tests for Mistral prompt-only zero-tool contract (HD-474d9b).

Ensures native Mistral (Vibe Pro) prompt-only runs receive an unambiguous
inline-only, zero-tool contract without workspace/snapshot/tool prerequisites,
while scoped Mistral policies and other adapters retain their existing behavior.
"""
from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parent))

import fleet
import mistral_runtime as runtime
import routing_v2


class MistralPromptOnlyTestBase(unittest.TestCase):
    def setUp(self):
        artifacts = Path(tempfile.gettempdir()).resolve()
        if artifacts.is_relative_to(Path(__file__).resolve().parents[2]):
            raise RuntimeError("Synthetic test temp root must be outside the release payload")
        self.root = artifacts / ("fleet-mistral-promptonly-" + uuid.uuid4().hex)
        self.root.mkdir(exist_ok=False)
        self.workspace = self.root / "workspace"
        self.output_root = self.root / "output"
        self.workspace.mkdir()
        self.output_root.mkdir()
        (self.workspace / "src").mkdir()
        (self.workspace / "src" / "app.py").write_text("print('test')\n", encoding="utf-8", newline="\n")
        (self.workspace / "input.txt").write_text("sample data\n", encoding="utf-8", newline="\n")
        self.dispatch = {
            "scope": ["src"],
            "inputs": ["input.txt"],
            "checks": [["python", "-B", "-m", "unittest", "discover", "-s", "src"]],
        }
        self.profiles = fleet.load_profiles(fleet.DEFAULT_PROFILES)
        self.profile = self.profiles["mistral"]
        self.schema = fleet.load_json(fleet.HANDOFF_SCHEMA_PATH, "handoff schema")

    def offline_context(self):
        return _OfflineContext()


class _OfflineContext:
    def __init__(self):
        self.patches = [
            patch.object(fleet, "prepare_shared_knowledge_snapshot",
                         lambda run_dir, **kwargs: Path(run_dir) / "knowledge-snapshot"),
            patch.object(fleet, "resolve_executable", lambda profile: (
                runtime.MISTRAL_NATIVE_EXECUTABLE
                if profile.get("adapter") == "mistral"
                else "mock_executable"
            )),
            patch.object(fleet, "probe_command", lambda *args, **kwargs: {
                "launchable": True, "status": "ok", "version": "2.26.1", "output": "vibe 2.26.1\n",
            }),
            patch.object(fleet, "current_quota_views", lambda root, profiles, **kwargs: (
                {
                    "mistral": {"status": "ok", "headroom": 100, "confidence": 1.0},
                    "antigravity": {"status": "ok", "headroom": 100, "confidence": 1.0},
                },
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


class TestMistralPromptOnlyDirect(MistralPromptOnlyTestBase):
    """Direct semantic tests for build_worker_prompt."""

    def test_direct_prompt_generation_mistral_prompt_only(self):
        """Prompt-only Mistral sessions receive an inline-only contract without tool prerequisites."""
        prompt = fleet.build_worker_prompt(
            "Advise on architectural pattern",
            agent_id="mistral",
            adapter_id="mistral",
            mode="advisory",
            workspace=self.workspace,
            kind="advisory",
            complexity="medium",
            native_subagent_limit=0,
            enable_web=False,
            knowledge_snapshot=None,
            schema=self.schema,
            prompt_only=True,
        )

        # Contradictory instructions must NOT be present
        self.assertNotIn("Use tools first", prompt)
        self.assertNotIn("Maker checks", prompt)
        self.assertNotIn("rely on repository evidence", prompt)
        self.assertNotIn("Use approved local skills", prompt)
        self.assertNotIn("Report actual commands/tests", prompt)

        # Inline contract and honest limitations MUST be present
        self.assertIn("zero tools", prompt.lower())
        self.assertIn("honest limitations", prompt)
        self.assertIn("inline task and context", prompt)
        self.assertIn("Do not imply verification or make native check claims", prompt)

        # Core safety constraints must be preserved
        self.assertIn("Do not spawn subagents or delegate to another model or CLI.", prompt)
        self.assertIn("Do not use web search, web fetch, connectors, or remote data sources.", prompt)
        self.assertIn("Never read, print, or expose credentials", prompt)
        self.assertIn("Return only one JSON object matching the schema below. No Markdown fence.", prompt)
        self.assertIn("Handoff schema:", prompt)

    def test_prompt_only_profiles_do_not_offer_unavailable_capabilities(self):
        for profile in ("standard", "l2-thin"):
            with self.subTest(profile=profile):
                prompt = fleet.build_worker_prompt(
                    "Evaluate the inline packet", agent_id="mistral", adapter_id="mistral",
                    mode="review", workspace=self.workspace, kind="review", complexity="low",
                    native_subagent_limit=0, enable_web=True, managed_chrome=True,
                    knowledge_snapshot=self.root / "snapshot", schema=self.schema,
                    runtime_profile=profile, prompt_only=True,
                )
                for unavailable in ("Use tools first", "Read the stable shared-knowledge snapshot",
                                    "Use approved local skills", "Use at most", "Bundle initial file reads",
                                    "Standard native tool/turn limits", "Web access is allowed",
                                    "Use only the assigned managed Chrome", "Follow applicable repository instructions"):
                    self.assertNotIn(unavailable, prompt)
                self.assertIn("Return only one JSON object", prompt)
                self.assertIn("zero tools", prompt)
                self.assertIn("do not commit, push, deploy, or alter remotes", prompt)

    def test_direct_prompt_generation_mistral_scoped_preserves_tools(self):
        """Scoped Mistral sessions (prompt_only=False) retain maker checks and tool instructions."""
        prompt = fleet.build_worker_prompt(
            "Implement bugfix in workspace",
            agent_id="mistral",
            adapter_id="mistral",
            mode="implement",
            workspace=self.workspace,
            kind="implementation",
            complexity="medium",
            native_subagent_limit=0,
            enable_web=False,
            knowledge_snapshot=None,
            schema=self.schema,
            prompt_only=False,
        )

        self.assertIn("Use tools first", prompt)
        self.assertIn("Maker checks run by declared integer id in pinned offline Docker; there is no host shell.", prompt)
        self.assertIn("rely on repository evidence", prompt)
        self.assertIn("Use approved local skills and shared knowledge when relevant.", prompt)
        self.assertIn("Verify important claims. Report actual commands/tests, not intended ones.", prompt)

    def test_direct_prompt_generation_default_parameter_is_backwards_compatible(self):
        """Default prompt_only parameter is False, preserving existing behavior for unspecified callers."""
        prompt = fleet.build_worker_prompt(
            "Review code changes",
            agent_id="mistral",
            adapter_id="mistral",
            mode="review",
            workspace=self.workspace,
            kind="review",
            complexity="medium",
            native_subagent_limit=0,
            enable_web=False,
            knowledge_snapshot=None,
            schema=self.schema,
        )
        self.assertIn("Use tools first", prompt)
        self.assertIn("Maker checks", prompt)

    def test_direct_prompt_generation_other_adapters_unaffected(self):
        """Non-Mistral adapters are identical regardless of prompt_only flag."""
        for adapter in ("antigravity", "claude", "qwen", "opencode"):
            with self.subTest(adapter=adapter):
                p_false = fleet.build_worker_prompt(
                    "Review code",
                    agent_id=adapter,
                    adapter_id=adapter,
                    mode="review",
                    workspace=self.workspace,
                    kind="review",
                    complexity="medium",
                    native_subagent_limit=0,
                    enable_web=False,
                    knowledge_snapshot=None,
                    schema=self.schema,
                    prompt_only=False,
                )
                p_true = fleet.build_worker_prompt(
                    "Review code",
                    agent_id=adapter,
                    adapter_id=adapter,
                    mode="review",
                    workspace=self.workspace,
                    kind="review",
                    complexity="medium",
                    native_subagent_limit=0,
                    enable_web=False,
                    knowledge_snapshot=None,
                    schema=self.schema,
                    prompt_only=True,
                )
                self.assertEqual(p_false, p_true)


class TestMistralPromptOnlyDispatchSelection(MistralPromptOnlyTestBase):
    """End-to-end selection tests via execute_single / execute_task with synthetic mocks."""

    def test_advisory_without_dispatch_yields_prompt_only_contract(self):
        """Advisory Mistral run without tool dispatch derives prompt_only contract and disables tools."""
        captured_build_command: list[dict] = []
        original_build_command = fleet.build_command

        def spy_build_command(*args, **kwargs):
            res = original_build_command(*args, **kwargs)
            captured_build_command.append({"args": args, "kwargs": kwargs, "res": res})
            return res

        task_text = "Advisory architecture question without tool dispatch payload"
        with self.offline_context(), patch.object(fleet, "build_command", side_effect=spy_build_command):
            result = fleet.execute_single(
                task_text,
                profiles=self.profiles,
                agent_id="mistral",
                mode="advisory",
                kind="advisory",
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

        self.assertEqual(result.get("status"), "DRY_RUN")
        self.assertEqual(len(captured_build_command), 1)
        call = captured_build_command[0]

        # Actual bound tool policy must be None
        self.assertIsNone(call["kwargs"]["mistral_tool_policy"])

        # Command must be generated with --agent plan and all tools disabled
        cmd = call["res"][0]
        self.assertIn("--agent", cmd)
        self.assertEqual(cmd[cmd.index("--agent") + 1], "plan")
        self.assertIn("--disabled-tools", cmd)
        self.assertEqual(cmd[cmd.index("--disabled-tools") + 1], "*")

        # Worker prompt must have prompt-only contract
        prompt = call["kwargs"]["prompt"]
        self.assertNotIn("Use tools first", prompt)
        self.assertNotIn("Maker checks", prompt)
        self.assertNotIn("rely on repository evidence", prompt)
        self.assertNotIn("Use approved local skills", prompt)
        self.assertNotIn("Report actual commands/tests", prompt)
        self.assertIn("zero tools", prompt.lower())
        self.assertIn("honest limitations", prompt)
        self.assertIn("inline task and context", prompt)

    def test_advisory_with_dispatch_yields_scoped_tool_policy(self):
        """Advisory Mistral run with trusted dispatch retains scoped tool policy and maker checks."""
        captured_build_command: list[dict] = []
        original_build_command = fleet.build_command

        def spy_build_command(*args, **kwargs):
            res = original_build_command(*args, **kwargs)
            captured_build_command.append({"args": args, "kwargs": kwargs, "res": res})
            return res

        task_text = f"Advisory evaluation with dispatch: {json.dumps(self.dispatch)}"
        with self.offline_context(), patch.object(fleet, "build_command", side_effect=spy_build_command):
            result = fleet.execute_single(
                task_text,
                profiles=self.profiles,
                agent_id="mistral",
                mode="advisory",
                kind="advisory",
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

        self.assertEqual(result.get("status"), "DRY_RUN")
        self.assertEqual(len(captured_build_command), 1)
        call = captured_build_command[0]

        # Bound tool policy must exist
        policy = call["kwargs"]["mistral_tool_policy"]
        self.assertIsNotNone(policy)
        self.assertEqual(policy["mode"], "advisory")

        # Command must be generated with --agent accept-edits (scoped tool admission)
        cmd = call["res"][0]
        self.assertIn("--agent", cmd)
        self.assertEqual(cmd[cmd.index("--agent") + 1], "accept-edits")
        self.assertNotIn("--disabled-tools", cmd)

        # Worker prompt retains tool guidance
        prompt = call["kwargs"]["prompt"]
        self.assertIn("Use tools first", prompt)
        self.assertIn("Maker checks", prompt)
        self.assertIn("Read the stable shared-knowledge snapshot", prompt)

    def test_review_without_dispatch_yields_prompt_only_contract(self):
        """Review Mistral run without tool dispatch derives prompt_only contract."""
        captured_build_command: list[dict] = []
        original_build_command = fleet.build_command

        def spy_build_command(*args, **kwargs):
            res = original_build_command(*args, **kwargs)
            captured_build_command.append({"args": args, "kwargs": kwargs, "res": res})
            return res

        task_text = "Review architectural proposal without tools"
        with self.offline_context(), patch.object(fleet, "build_command", side_effect=spy_build_command):
            result = fleet.execute_single(
                task_text,
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
                mistral_tool_dispatch=None,
            )

        self.assertEqual(result.get("status"), "DRY_RUN")
        call = captured_build_command[0]
        self.assertIsNone(call["kwargs"]["mistral_tool_policy"])
        prompt = call["kwargs"]["prompt"]
        self.assertNotIn("Use tools first", prompt)
        self.assertNotIn("Maker checks", prompt)
        self.assertNotIn("rely on repository evidence", prompt)
        self.assertIn("zero tools", prompt.lower())

    def test_review_with_dispatch_yields_scoped_tool_policy(self):
        """Review Mistral run with trusted dispatch retains scoped tool policy."""
        captured_build_command: list[dict] = []
        original_build_command = fleet.build_command

        def spy_build_command(*args, **kwargs):
            res = original_build_command(*args, **kwargs)
            captured_build_command.append({"args": args, "kwargs": kwargs, "res": res})
            return res

        task_text = f"Review code with dispatch: {json.dumps(self.dispatch)}"
        with self.offline_context(), patch.object(fleet, "build_command", side_effect=spy_build_command):
            result = fleet.execute_single(
                task_text,
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

        self.assertEqual(result.get("status"), "DRY_RUN")
        call = captured_build_command[0]
        self.assertIsNotNone(call["kwargs"]["mistral_tool_policy"])
        prompt = call["kwargs"]["prompt"]
        self.assertIn("Use tools first", prompt)
        self.assertIn("Maker checks", prompt)

    def test_implement_mode_preserves_required_dispatch(self):
        """Implementation mode requires dispatch and cannot fall back to prompt-only."""
        with self.offline_context():
            with self.assertRaises(fleet.FleetError) as cm:
                fleet.execute_single(
                    "Implement without dispatch",
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

    def test_public_execute_single_dispatch_selection(self):
        """The public execute_single entry derives the prompt from its bound policy."""
        captured_build_command: list[dict] = []
        original_build_command = fleet.build_command

        def spy_build_command(*args, **kwargs):
            res = original_build_command(*args, **kwargs)
            captured_build_command.append({"args": args, "kwargs": kwargs, "res": res})
            return res

        task_text = "Prompt-only run via execute_single"
        with self.offline_context(), patch.object(fleet, "build_command", side_effect=spy_build_command):
            result = fleet.execute_single(
                task_text,
                profiles=self.profiles,
                agent_id="mistral",
                mode="advisory",
                kind="advisory",
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

        self.assertEqual(result.get("status"), "DRY_RUN")
        call = captured_build_command[0]
        self.assertIsNone(call["kwargs"]["mistral_tool_policy"])
        prompt = call["kwargs"]["prompt"]
        self.assertNotIn("Use tools first", prompt)
        self.assertNotIn("Maker checks", prompt)

    def test_unknown_tools_must_not_be_requested_in_prompt_only(self):
        """Prompt-only Mistral sessions explicitly state zero tools and disable all tool execution."""
        captured_build_command: list[dict] = []
        original_build_command = fleet.build_command

        def spy_build_command(*args, **kwargs):
            res = original_build_command(*args, **kwargs)
            captured_build_command.append({"args": args, "kwargs": kwargs, "res": res})
            return res

        with self.offline_context(), patch.object(fleet, "build_command", side_effect=spy_build_command):
            fleet.execute_single(
                "Task text",
                profiles=self.profiles,
                agent_id="mistral",
                mode="advisory",
                kind="advisory",
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

        call = captured_build_command[0]
        cmd = call["res"][0]
        # Command specifies --disabled-tools *
        self.assertIn("--disabled-tools", cmd)
        self.assertEqual(cmd[cmd.index("--disabled-tools") + 1], "*")
        # Prompt instructs zero tools and no tool calls
        prompt = call["kwargs"]["prompt"]
        self.assertIn("zero tools are admitted or available", prompt)
        self.assertIn("do not attempt tool calls", prompt)


if __name__ == "__main__":
    unittest.main()
