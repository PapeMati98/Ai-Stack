"""Beam context-window repair regression: peak across distinct step inputs.

The native saved run13steps observation (max=29022, cumulative=285183,
limit=262144) used to falsely stop the synthetic private child because the
cumulative callback summed distinct step inputs. The run_bounded_child
context callback now measures the largest distinct step input, not the sum;
the cumulative telemetry summary stays as a sum for genuine accounting.

All fixtures below are synthetic, owned by the test, and use numeric counters
only: no real prompts, keys, settings or network. Temporary directories come
from mkdtemp and are never deleted.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import fleet
from process_supervision import ContextEventUnreadable
from test_fleet_context_budget import step_finish


NATIVE_LIMIT = 262_144
NATIVE_STEP_SIZES = [
    4_890, 10_725, 13_589, 13_782, 23_212, 25_795, 26_383,
    26_677, 26_934, 27_490, 28_066, 28_618, 29_022,
]
NATIVE_STEP_COUNT = len(NATIVE_STEP_SIZES)
NATIVE_MAX_STEP_INPUT = max(NATIVE_STEP_SIZES)
NATIVE_CUMULATIVE_SUM = sum(NATIVE_STEP_SIZES)


def _native_saved_steps() -> list[dict]:
    """Replay the saved native run13steps metadata with distinct identities.

    Each individual size stays below the 262144 limit; the cumulative sum
    (285183) is above. The identities are synthetic ids derived from the
    saved run; no prompt content is present.
    """
    assert len(NATIVE_STEP_SIZES) == NATIVE_STEP_COUNT
    assert max(NATIVE_STEP_SIZES) == NATIVE_MAX_STEP_INPUT
    assert sum(NATIVE_STEP_SIZES) == NATIVE_CUMULATIVE_SUM
    return [step_finish(f"native-redacted-{index}", fresh=size, read=0)
            for index, size in enumerate(NATIVE_STEP_SIZES)]


class BeamContextWindowRepairTests(unittest.TestCase):
    def test_callback_tracks_largest_distinct_step_not_sum(self) -> None:
        """Replay 13 metadata-only steps; cumulative sum exceeds the limit,
        every individual size stays below it. The callback returns the peak
        across distinct steps seen so far and never reports a sum."""
        callback = fleet.opencode_max_distinct_step_context_tokens()
        peak_after_first = callback(json.dumps(_native_saved_steps()[0]))
        self.assertEqual(peak_after_first, 4_890)
        observations = [callback(json.dumps(event)) for event in _native_saved_steps()[1:]]
        # The final observation equals the largest distinct size, never the sum.
        self.assertEqual(observations[-1], NATIVE_MAX_STEP_INPUT)
        self.assertEqual(max(observations), NATIVE_MAX_STEP_INPUT)
        for value in observations:
            self.assertIsNotNone(value)
            self.assertLess(value, NATIVE_LIMIT)

    def test_duplicate_same_identity_does_not_inflate(self) -> None:
        callback = fleet.opencode_max_distinct_step_context_tokens()
        first = callback(json.dumps(step_finish("dup", fresh=12_000, read=0)))
        same = callback(json.dumps(step_finish("dup", fresh=12_000, read=0)))
        peak = callback(json.dumps(step_finish("peak", fresh=19_000, read=0)))
        again = callback(json.dumps(step_finish("dup", fresh=12_000, read=0)))
        self.assertEqual(first, 12_000)
        self.assertEqual(same, 12_000)
        self.assertEqual(peak, 19_000)
        self.assertEqual(again, 19_000)

    def test_conflicting_duplicate_fails_closed(self) -> None:
        callback = fleet.opencode_max_distinct_step_context_tokens()
        callback(json.dumps(step_finish("x", fresh=12_000, read=0)))
        with self.assertRaises(ContextEventUnreadable) as err:
            callback(json.dumps(step_finish("x", fresh=12_001, read=0)))
        self.assertIn("Conflicting OpenCode step input counters", str(err.exception))

    def test_unknown_malformed_and_invalid_counter_types_remain_unknown(self) -> None:
        callback = fleet.opencode_max_distinct_step_context_tokens()
        invalid_step_lines = [
            # Counter type is unusable: string, bool, negative.
            json.dumps({"type": "step_finish", "part": {
                "type": "step-finish", "id": "u",
                "tokens": {"input": "12000", "output": 5, "reasoning": 0,
                           "cache": {"read": 0, "write": 0}}}}),
            json.dumps(step_finish("u2", fresh=True, read=0)),
            json.dumps(step_finish("u3", fresh=10, read=True)),
            json.dumps(step_finish("u4", fresh=-1, read=0)),
            json.dumps(step_finish("u5", fresh=10, read=-1)),
            # Identity is required.
            json.dumps({"type": "step_finish", "part": {
                "type": "step-finish",
                "tokens": {"input": 10, "output": 1, "reasoning": 0,
                           "cache": {"read": 0, "write": 0}}}}),
            # Counter is missing entirely.
            json.dumps({"type": "step_finish", "part": {
                "type": "step-finish", "id": "u6",
                "tokens": {"output": 1, "reasoning": 0,
                           "cache": {"read": 0, "write": 0}}}}),
            # part is not a dict.
            json.dumps({"type": "step_finish", "part": "not-a-dict"}),
            # Two step_finish records glued together: not valid JSONL.
            json.dumps(step_finish("a", fresh=10, read=0))
            + json.dumps(step_finish("b", fresh=20, read=0)),
            # A bare JSON fragment.
            "{not json",
        ]
        for line in invalid_step_lines:
            with self.subTest(line=line):
                with self.assertRaises(ContextEventUnreadable) as err:
                    callback(line)
                self.assertIn("OpenCode", str(err.exception))

        # Non-step_finish events are ignored (not unreadable) and never
        # influence the peak.
        self.assertIsNone(callback(json.dumps({"type": "tool_use",
                                               "part": {"type": "tool", "id": "t",
                                               "state": {"output": "x"}}})))
        self.assertIsNone(callback(json.dumps({"type": "result",
                                               "usage": {"input_tokens": 999999}})))
        # A step with usable counters summing to 0 is a zero peak: the
        # callback never invents a non-zero number for a missing counter.
        zero_event = step_finish("z", fresh=0, read=0, output=0, reasoning=0)
        zero_event["part"]["tokens"]["cache"]["read"] = 0
        zero_event["part"]["tokens"]["cache"]["write"] = 0
        self.assertEqual(callback(json.dumps(zero_event)), 0)

    def test_output_reasoning_total_excluded_from_step_input(self) -> None:
        callback = fleet.opencode_max_distinct_step_context_tokens()
        event = step_finish("v", fresh=10_000, read=5_000, output=9_999_999,
                            reasoning=8_888_888)
        event["part"]["tokens"]["total"] = 7_777_777
        self.assertEqual(callback(json.dumps(event)), 15_000)
        # Reasoning/output/total never enter the measured peak.
        follow_up = step_finish("w", fresh=15_500, read=0)
        self.assertEqual(callback(json.dumps(follow_up)), 15_500)

    def test_cumulative_helper_kept_for_backcompat_still_sums(self) -> None:
        """The cumulative helper is kept for genuine accounting, even though
        the stop callback no longer uses it."""
        cumulative = fleet.opencode_cumulative_context_tokens()
        observations = [cumulative(json.dumps(event))
                        for event in _native_saved_steps()]
        self.assertEqual(observations[-1], NATIVE_CUMULATIVE_SUM)

    def test_cumulative_telemetry_remains_sum_of_distinct_steps(self) -> None:
        """context_telemetry.cumulative_step_input stays as a sum of the
        distinct step inputs; the repair does not touch that accounting."""
        events = _native_saved_steps()
        telemetry = fleet.context_telemetry(
            [json.dumps(event) + "\n" for event in events], adapter="opencode"
        )
        self.assertEqual(telemetry["max_step_input"], NATIVE_MAX_STEP_INPUT)
        self.assertEqual(telemetry["cumulative_step_input"], NATIVE_CUMULATIVE_SUM)
        self.assertEqual(telemetry["steps"], NATIVE_STEP_COUNT)

    def test_run_bounded_child_dispatches_peak_callback(self) -> None:
        """run_bounded_child wires the new peak callback for opencode."""
        return_value = {"exit_code": 0, "stdout": "", "stderr": "",
                        "timed_out": False, "tool_budget_exceeded": False,
                        "tool_events": [], "context_peak_tokens": None,
                        "context_limit_tokens": None,
                        "context_budget_exceeded": False,
                        "context_scan_incomplete": False,
                        "context_scan_stopped": False,
                        "supervision_failed": False}
        with mock.patch.object(fleet.process_supervision, "run",
                               return_value=return_value) as run:
            fleet.run_bounded_child(
                [sys.executable, "-c", "pass"], cwd=Path.cwd(), env={},
                stdin_text=None, timeout=1, adapter_id="opencode",
                max_model_tool_calls=None,
                max_context_tokens=NATIVE_LIMIT)
        run.assert_called_once()
        cb = run.call_args.kwargs["context_tokens"]
        self.assertTrue(callable(cb))
        # The observer returned to process_supervision is the per-run callback
        # produced by opencode_max_distinct_step_context_tokens(): calling it
        # with a saved native step reports the peak, not the cumulative sum.
        self.assertEqual(
            cb(json.dumps(_native_saved_steps()[0])), NATIVE_STEP_SIZES[0])
        observations = [
            cb(json.dumps(event))
            for event in _native_saved_steps()[1:]]
        self.assertEqual(observations[-1], NATIVE_MAX_STEP_INPUT)

    def _run_synthetic_private_child(self, *, events, input_root,
                                     max_context_tokens=NATIVE_LIMIT):
        """Exercise the real bounded-child supervisor with owned fake events."""
        root = Path(input_root)
        root.mkdir(parents=True, exist_ok=True)
        code = (
            "from pathlib import Path\nimport json,time\n"
            "Path('retained-work.txt').write_text('owned partial work')\n"
            f"events={events!r}\n"
            "for event in events: print(json.dumps(event),flush=True); "
            "time.sleep(0.03)\n"
            "time.sleep(0.8)\n"
            "Path('late.txt').write_text('child reached completion')\n"
        )
        result = fleet.run_bounded_child(
            [sys.executable, "-u", "-c", code], cwd=root,
            env=dict(os.environ), stdin_text=None, timeout=10,
            adapter_id="opencode", max_model_tool_calls=None,
            max_context_tokens=max_context_tokens)
        return result, root

    def test_replay_saved_native_thirteen_steps_completes_synthetic_child(self) -> None:
        """13 metadata-only steps, all below limit and sum above: the child
        runs to completion because the peak across distinct steps never
        exceeds the limit."""
        root = Path(tempfile.mkdtemp(prefix="beam-context-repair-owned-"))
        result, child_root = self._run_synthetic_private_child(
            events=_native_saved_steps(), input_root=root)
        self.assertEqual(result["exit_code"], 0)
        # No false stop: the child wrote its completion sentinel.
        self.assertTrue((child_root / "late.txt").exists(),
                        "synthetic child must reach completion when peak stays below limit")
        self.assertTrue((child_root / "retained-work.txt").exists())
        self.assertEqual(result["context_peak_tokens"], NATIVE_MAX_STEP_INPUT)
        self.assertFalse(result["context_budget_exceeded"])

    def test_genuine_single_step_overflow_stops_with_work_retained(self) -> None:
        """One step above the limit, even with only one identity, still stops."""
        root = Path(tempfile.mkdtemp(prefix="beam-context-overflow-owned-"))
        events = [step_finish("over", fresh=NATIVE_LIMIT + 1, read=0)]
        events[0]["part"]["tokens"]["cache"]["read"] = 0
        result, child_root = self._run_synthetic_private_child(
            events=events, input_root=root)
        self.assertTrue(result["context_budget_exceeded"])
        self.assertEqual(result["context_peak_tokens"], NATIVE_LIMIT + 1)
        self.assertTrue((child_root / "retained-work.txt").exists())
        self.assertFalse((child_root / "late.txt").exists())

    def test_equal_boundary_runs_to_completion_without_auto_accept(self) -> None:
        """A step equal to the limit is not an overshoot."""
        root = Path(tempfile.mkdtemp(prefix="beam-context-boundary-owned-"))
        events = [step_finish("ok", fresh=12_000, read=0),
                  step_finish("equal", fresh=NATIVE_LIMIT, read=0)]
        result, child_root = self._run_synthetic_private_child(
            events=events, input_root=root)
        self.assertEqual(result["exit_code"], 0)
        self.assertFalse(result["context_budget_exceeded"])
        self.assertEqual(result["context_peak_tokens"], NATIVE_LIMIT)
        self.assertTrue((child_root / "late.txt").exists())
        self.assertNotIn("status", result)

    def test_conflicting_duplicate_stops_fail_closed_with_work_retained(self) -> None:
        """A same-identity conflict triggers scan_stopped (fail-closed)."""
        root = Path(tempfile.mkdtemp(prefix="beam-context-conflict-owned-"))
        events = [step_finish("c", fresh=12_000, read=0),
                  step_finish("c", fresh=12_001, read=0),
                  step_finish("late", fresh=10_000, read=0)]
        result, child_root = self._run_synthetic_private_child(
            events=events, input_root=root)
        self.assertTrue(result["context_scan_incomplete"])
        self.assertTrue(result["context_scan_stopped"])
        self.assertTrue((child_root / "retained-work.txt").exists())
        self.assertFalse((child_root / "late.txt").exists())

    def test_malformed_input_stops_private_child_fail_closed(self) -> None:
        """Unreadable counters stay unknown and stop the private child."""
        root = Path(tempfile.mkdtemp(prefix="beam-context-malformed-owned-"))
        malformed = step_finish("bad", fresh=12_000, read=0)
        malformed["part"]["tokens"]["input"] = "12000"
        result, child_root = self._run_synthetic_private_child(
            events=[malformed], input_root=root)
        self.assertTrue(result["context_scan_incomplete"])
        self.assertTrue(result["context_scan_stopped"])
        self.assertTrue((child_root / "retained-work.txt").exists())
        self.assertFalse((child_root / "late.txt").exists())

    def test_absent_limit_leaves_old_behavior_and_unknown_stays_unknown(self) -> None:
        """Without context_stop_tokens the stop callback never fires."""
        root = Path(tempfile.mkdtemp(prefix="beam-context-noop-owned-"))
        result, child_root = self._run_synthetic_private_child(
            events=_native_saved_steps(), input_root=root,
            max_context_tokens=None)
        self.assertEqual(result["exit_code"], 0)
        self.assertTrue((child_root / "late.txt").exists())

    def test_other_adapter_refused_before_any_private_child_is_launched(self) -> None:
        """The peak callback is opencode-only; other adapters still fail early."""
        with mock.patch.object(fleet.subprocess, "Popen") as spawn:
            with self.assertRaises(fleet.FleetError):
                fleet.run_bounded_child(
                    [sys.executable], cwd=Path.cwd(), env={},
                    stdin_text=None, timeout=1, adapter_id="grok",
                    max_model_tool_calls=None,
                    max_context_tokens=NATIVE_LIMIT)
            spawn.assert_not_called()


if __name__ == "__main__":
    unittest.main()
