"""Offline HD-a9a428: bound the native Vibe session token budget to the run's step count.

Synthetic fixtures only; no inference, auth or cleanup. Verifies that the
historical fixed 262144 ceiling is now a floor while bounded reviews/implementations
(48-96 tool calls) get a derived ceiling that survives Vibe's cumulative
prompt+completion accounting without cutting runs short after ~13-27 steps.
"""
from __future__ import annotations

from pathlib import Path
import tempfile
import unittest
import uuid

import fleet
import mistral_runtime as runtime

tools = runtime.tool_module()


class SyntheticFixture(unittest.TestCase):
    def setUp(self):
        artifacts = Path(tempfile.gettempdir()).resolve()
        if artifacts.is_relative_to(Path(__file__).resolve().parents[2]):
            raise RuntimeError("Synthetic test temp root must be outside the release payload")
        self.root = artifacts / ("fleet-mistral-session-budget-" + uuid.uuid4().hex)
        self.root.mkdir(exist_ok=False)
        self.workspace = self.root / "workspace"
        self.run = self.root / "run"
        self.workspace.mkdir()
        self.run.mkdir()
        (self.workspace / "src").mkdir()
        (self.workspace / "src/auth.py").write_text("answer = 42\n", encoding="utf-8", newline="\n")
        (self.workspace / "input.txt").write_text("synthetic input\n", encoding="utf-8", newline="\n")
        self.dispatch = {"scope": ["src"], "inputs": ["input.txt"],
                         "checks": [["python", "-B", "-m", "unittest", "discover", "-s", "src"]]}

    def build_policy(self, *, mode: str = "implement",
                     max_tool_calls: int | None = None) -> dict:
        return tools.build_policy(workspace=self.workspace, run_dir=self.run,
                                  mode=mode, max_tool_calls=max_tool_calls, **self.dispatch)


class HelperValueTableTests(unittest.TestCase):
    def test_constants_match_derived_ceiling(self):
        self.assertEqual(runtime.MISTRAL_MAX_TURNS * runtime.MISTRAL_SESSION_TOKENS_PER_STEP, 1048576)
        self.assertEqual(runtime.MISTRAL_MIN_SESSION_TOKENS, 262144)
        self.assertEqual(runtime.MISTRAL_MAX_TOKENS, 1048576)
        self.assertEqual(runtime.MISTRAL_SESSION_TOKENS_PER_STEP, 16384)

    def test_helper_table_for_every_representative_bound(self):
        cases = {
            None: 262144, 1: 262144, 13: 262144, 15: 262144,
            16: 278528, 48: 802816, 63: 1048576, 64: 1048576,
            96: 1048576, 200: 1048576, 1000: 1048576,
        }
        for bound, expected in cases.items():
            with self.subTest(bound=bound):
                self.assertEqual(runtime.session_token_budget(bound), expected)


class HelperValidationTests(unittest.TestCase):
    def test_invalid_bound_types_raise_runtime_error(self):
        for bad in (True, False, 0, -1, -100, "32", 1.0, 2.5, [48], (48,)):
            with self.subTest(bad=bad):
                with self.assertRaises(runtime.MistralRuntimeError):
                    runtime.session_token_budget(bad)

    def test_invalid_keeps_floor_for_none_only(self):
        # Only ``None`` keeps the historical floor; everything else is the input or a refusal.
        self.assertEqual(runtime.session_token_budget(None), runtime.MISTRAL_MIN_SESSION_TOKENS)

    def test_int_subclass_is_not_an_int_bound(self):
        class IntSubclass(int):
            pass
        with self.assertRaises(runtime.MistralRuntimeError):
            runtime.session_token_budget(IntSubclass(48))


class BuildCommandBudgetTests(SyntheticFixture):
    def _defaults(self, *, mode: str = "review",
                  tool_policy: dict | None = None) -> dict:
        return {"executable": runtime.MISTRAL_NATIVE_EXECUTABLE,
                "model": "mistral-large-4", "mode": mode,
                "run_dir": self.run, "timeout": 120,
                "tool_policy": tool_policy}

    def test_review_with_bound_96_emits_ceiling(self):
        policy = self.build_policy(mode="review", max_tool_calls=96)
        command = runtime.build_command(**self._defaults(mode="review", tool_policy=policy))
        self.assertEqual(command[command.index("--max-tokens") + 1], "1048576")

    def test_review_with_bound_48_emits_802816(self):
        policy = self.build_policy(mode="review", max_tool_calls=48)
        command = runtime.build_command(**self._defaults(mode="review", tool_policy=policy))
        self.assertEqual(command[command.index("--max-tokens") + 1], "802816")

    def test_review_with_bound_13_emits_floor(self):
        policy = self.build_policy(mode="review", max_tool_calls=13)
        command = runtime.build_command(**self._defaults(mode="review", tool_policy=policy))
        self.assertEqual(command[command.index("--max-tokens") + 1], "262144")

    def test_advisory_without_tool_policy_uses_floor(self):
        command = runtime.build_command(**self._defaults(mode="review", tool_policy=None))
        self.assertEqual(command[command.index("--agent") + 1], "plan")
        self.assertEqual(command[command.index("--max-tokens") + 1], str(runtime.MISTRAL_MIN_SESSION_TOKENS))

    def test_implement_without_tool_policy_still_refused_first(self):
        with self.assertRaises(runtime.MistralRuntimeError):
            runtime.build_command(**self._defaults(mode="implement", tool_policy=None))

    def test_explicit_max_tokens_within_ceiling_is_honoured(self):
        command = runtime.build_command(**self._defaults(mode="review"), max_tokens=32768)
        self.assertEqual(command[command.index("--max-tokens") + 1], "32768")
        command = runtime.build_command(**self._defaults(mode="review"), max_tokens=runtime.MISTRAL_MAX_TOKENS)
        self.assertEqual(command[command.index("--max-tokens") + 1], str(runtime.MISTRAL_MAX_TOKENS))

    def test_explicit_max_tokens_above_ceiling_is_refused(self):
        with self.assertRaises(runtime.MistralRuntimeError):
            runtime.build_command(**self._defaults(mode="review"), max_tokens=runtime.MISTRAL_MAX_TOKENS + 1)

    def test_malformed_tool_policy_limits_raise_runtime_error_not_keyerror(self):
        # Parent integration regression: a policy without usable limits must fail
        # closed with the adapter's own error type, never a bare KeyError/TypeError.
        for malformed in ({}, {"limits": {}}, {"limits": None}, {"limits": "96"}):
            with self.subTest(policy=malformed):
                with self.assertRaises(runtime.MistralRuntimeError):
                    runtime.build_command(**self._defaults(mode="review", tool_policy=malformed))

    def test_build_policy_refuses_nonpositive_max_tool_calls(self):
        # build_policy -> validate_policy already pins max_tool_calls to its bound.
        with self.assertRaises(tools.ToolPolicyError):
            self.build_policy(mode="review", max_tool_calls=0)

    def test_implement_with_max_tool_calls_64_emits_ceiling(self):
        policy = self.build_policy(mode="implement", max_tool_calls=64)
        command = runtime.build_command(**self._defaults(mode="implement", tool_policy=policy))
        self.assertEqual(command[command.index("--max-tokens") + 1], "1048576")


class FleetLevelBudgetTests(SyntheticFixture):
    def test_fleet_command_picks_up_derived_max_tokens_from_bound_policy(self):
        policy = self.build_policy(mode="review", max_tool_calls=96)
        profile = fleet.load_profiles(fleet.DEFAULT_PROFILES)["mistral"]
        command, _, _ = fleet.build_command(
            "mistral", runtime.MISTRAL_NATIVE_EXECUTABLE,
            prompt="synthetic", workspace=self.workspace, mode="review",
            model="mistral-large-4", complexity="high", timeout=120,
            run_dir=self.run, native_subagent_limit=0, enable_web=False,
            materialize=False, profile=profile, mistral_tool_policy=policy,
        )
        self.assertEqual(command[command.index("--max-tokens") + 1], "1048576")


if __name__ == "__main__":
    unittest.main()
