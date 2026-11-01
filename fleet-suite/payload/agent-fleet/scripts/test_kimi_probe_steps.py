"""Regression tests for the Kimi native availability probe step budget.

The Kimi k3 reasoning model uses one step for chain-of-thought before it can
emit the trivial "OK" reply. A probe configured with
``loop_control.max_steps_per_turn = 1`` aborted with
``max_steps_exceeded: Turn exceeded maxSteps=1`` for every attempt since
2026-09-16, leaving the model locked out even when the account balance was
healthy. The native ``kimi_usage.query`` cannot lift that lock because it only
covers the weekly window, so the probe is the sole path that proves native
execution and must actually be able to finish.

Each test in this module fails on the pre-fix implementation (because the
written config still pins ``max_steps_per_turn`` to 1) and passes after the
budget is widened to the smallest value that lets the model complete the
trivial reply. No real Kimi CLI is launched; the probes substitute a fake for
``fleet.run_bounded_child`` so we read the produced ``config.toml`` directly.
"""
from __future__ import annotations

import json
import sys
import tempfile
import tomllib
import unittest
from pathlib import Path
from unittest.mock import patch


SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

import fleet  # noqa: E402
import kimi_usage  # noqa: E402


MONTHLY_USAGE_LIMIT_ERROR = (
    "error: failed to run prompt: provider.api_error: 403 "
    "You've reached your monthly usage limit"
)


def _fake_prepare(folder: Path, **kwargs) -> Path:
    """Mirror the real ``prepare_guarded_kimi_home`` shape without OAuth I/O."""
    home = folder / "kimi-home"
    home.mkdir()
    (home / "config.toml").write_text(
        'default_model="k3"\n[models.k3]\nmodel="test"\n[background]\n',
        encoding="utf-8",
    )
    return home


class _KimiProbeHarness(unittest.TestCase):
    """Shared fixtures only; holds no tests, so nothing runs twice."""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _run_probe(self, run_result: dict) -> tuple[dict | None, str]:
        """Drive ``kimi_usage.native_probe`` and capture the config it wrote.

        The config has to be read INSIDE the fake child. ``native_probe`` owns
        its own ``tempfile.TemporaryDirectory`` and removes it before returning,
        so the file no longer exists once the call is back in the test - reading
        it afterwards fails with FileNotFoundError on every run, which is what
        the first two maker attempts of this test did on 2026-09-24.
        """
        captured: dict[str, str] = {}

        def fake_child(*args, **kwargs):
            home_path = Path(kwargs["env"]["KIMI_CODE_HOME"])
            captured["config"] = (home_path / "config.toml").read_text(encoding="utf-8")
            return run_result

        row = None
        with patch.object(
            fleet, "prepare_guarded_kimi_home", _fake_prepare
        ), patch.object(
            fleet, "run_bounded_child", side_effect=fake_child
        ), patch.object(
            fleet, "resolve_executable", return_value="kimi.exe"
        ):
            try:
                row = kimi_usage.native_probe(
                    fleet, {"models": {"low": "k3"}}, self.root
                )
            except ValueError:
                row = None
        self.assertIn("config", captured, "the probe never reached the child process")
        return row, captured["config"]

    def _probe_config(self, run_result: dict) -> tuple[dict, str]:
        """Drive ``kimi_usage.native_probe`` and return the written config."""
        _, config_text = self._run_probe(run_result)
        return tomllib.loads(config_text), config_text

class KimiProbeStepBudgetTests(_KimiProbeHarness):
    """The probe must let the reasoning Kimi k3 model finish a trivial reply."""

    def test_probe_config_allows_enough_steps_for_trivial_response(self) -> None:
        """The written probe config must reserve at least two steps per turn.

        The reasoning Kimi k3 spends one step on chain-of-thought and another
        on the trivial reply. Anything tighter than ``max_steps_per_turn=2``
        reproduces the 2026-09-16 ``max_steps_exceeded`` failure.
        """
        config, _ = self._probe_config(
            {
                "exit_code": 1,
                "stderr": MONTHLY_USAGE_LIMIT_ERROR,
            }
        )
        self.assertGreaterEqual(
            config["loop_control"]["max_steps_per_turn"], 2,
            "Probe step budget must accommodate Kimi k3 reasoning plus reply",
        )
        # The retry and printable-turn budgets stay clamped: more steps must not
        # give the model any new action surface.
        # Retries stay at zero - through the key the current CLI actually reads.
        # `max_retries_per_step` is deprecated and ignored by Kimi CLI 0.43.1.
        self.assertNotIn("max_retries_per_step", config["loop_control"])
        self.assertEqual(config["loop_control"]["max_attempts_per_step"], 1)
        self.assertEqual(config["background"]["print_max_turns"], 1)

    def test_probe_keeps_deny_star_rule(self) -> None:
        """The ``pattern = "*"`` deny rule must survive the step-budget change.

        A wider turn budget is only acceptable if every tool pattern remains
        denied, so the model gains no new capability. The probe must still
        carry the blanket deny rule for the trivial-reply probe to stay safe.
        """
        _, config_text = self._probe_config(
            {
                "exit_code": 1,
                "stderr": MONTHLY_USAGE_LIMIT_ERROR,
            }
        )
        # The deny rule is written as a single TOML array-of-tables entry.
        self.assertIn('pattern = "*"', config_text)
        self.assertIn('decision = "deny"', config_text)
        # Belt-and-braces: the loop_control is still inside the same config.
        config = tomllib.loads(config_text)
        self.assertGreaterEqual(
            config["loop_control"]["max_steps_per_turn"], 2,
            "Step budget must widen even when the deny rule is present",
        )

    def test_probe_returns_native_cli_check_with_monthly_on_success(self) -> None:
        """A successful probe must surface ``source: native-cli-check`` with monthly coverage.

        When the bounded child returns exit code 0 with non-zero tokens, the
        probe must report availability through the only path
        ``quota_availability.check`` accepts for clearing a monthly lockout,
        and the underlying config must keep the widened step budget that makes
        the success path reachable at all.
        """
        result = {
            "exit_code": 0,
            "timed_out": False,
            "stdout": json.dumps(
                {
                    "type": "usage.record",
                    "usage": {"input_tokens": 100, "output_tokens": 1},
                }
            ),
        }
        row, config_text = self._run_probe(result)
        config = tomllib.loads(config_text)
        self.assertIsNotNone(row, "a successful native child must not raise")
        self.assertEqual(row["status"], "available")
        self.assertEqual(row["source"], "native-cli-check")
        self.assertIn("monthly", row["covered_scopes"])
        # The success path is only reachable when the probe can spend a step on
        # reasoning before the trivial reply.
        self.assertGreaterEqual(
            config["loop_control"]["max_steps_per_turn"], 2,
            "Successful probe path still requires the widened step budget",
        )


class KimiProbeReplyEvidenceTests(_KimiProbeHarness):
    """Kimi CLI 0.43.1 emits no usage event, so a text reply must be accepted.

    Measured 2026-09-24: the stream carried only ``system.version``, the assistant
    message and ``session.resume_hint``. A token-only success test could never
    pass while the model was in fact answering "OK".
    """

    ASSISTANT_OK = json.dumps({"role": "assistant", "content": "OK"})
    ASSISTANT_THINKING_ONLY = json.dumps(
        {"role": "assistant", "content": [{"type": "think", "think": "Reasoning..."}]}
    )

    def test_text_reply_without_usage_event_proves_availability(self) -> None:
        stdout = "\n".join([
            json.dumps({"role": "meta", "type": "system.version", "version": "0.43.1"}),
            self.ASSISTANT_OK,
            json.dumps({"role": "meta", "type": "session.resume_hint"}),
        ])
        row, _ = self._run_probe({"exit_code": 0, "timed_out": False, "stdout": stdout})
        self.assertIsNotNone(row)
        self.assertEqual(row["source"], "native-cli-check")
        self.assertIn("monthly", row["covered_scopes"])
        self.assertEqual(row["probe_evidence"], "assistant-text")
        self.assertNotIn("probe_tokens", row)

    def test_thinking_only_reply_never_clears_the_lockout(self) -> None:
        """The exact failure seen live must stay a failure.

        With a 16-token budget Kimi returned reasoning and no reply. Accepting that
        as availability would lift a monthly lockout on no evidence at all.
        """
        row, _ = self._run_probe(
            {"exit_code": 0, "timed_out": False, "stdout": self.ASSISTANT_THINKING_ONLY}
        )
        self.assertIsNone(row, "reasoning without a reply must raise, not report available")

    def test_text_reply_with_nonzero_exit_is_not_availability(self) -> None:
        row, _ = self._run_probe(
            {"exit_code": 1, "timed_out": False, "stdout": self.ASSISTANT_OK, "stderr": ""}
        )
        self.assertIsNone(row)

    def test_text_reply_after_timeout_is_not_availability(self) -> None:
        row, _ = self._run_probe(
            {"exit_code": 0, "timed_out": True, "stdout": self.ASSISTANT_OK}
        )
        self.assertIsNone(row)

    def test_assistant_text_ignores_non_assistant_and_thinking_parts(self) -> None:
        stdout = "\n".join([
            json.dumps({"role": "user", "content": "Reply OK only."}),
            json.dumps({"role": "assistant", "content": [
                {"type": "thinking", "text": "hidden"},
                {"type": "text", "text": "OK"},
            ]}),
            "not json",
        ])
        self.assertEqual(kimi_usage.assistant_text(stdout), "OK")
        self.assertEqual(kimi_usage.assistant_text(self.ASSISTANT_THINKING_ONLY), "")
        self.assertEqual(kimi_usage.assistant_text(""), "")


if __name__ == "__main__":
    unittest.main(verbosity=2)