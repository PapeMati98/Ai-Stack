from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import agent_loop  # noqa: E402
import security_contract as security  # noqa: E402


class AgentLoopTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="agent-loop-test-")
        self.project = Path(self.temp.name) / "project"
        self.project.mkdir()
        (self.project / "src").mkdir()
        (self.project / "src" / "app.py").write_text("print('ok')\n", encoding="utf-8")
        self.loop_id = "health"
        self.paths = agent_loop.build_paths(self.project, self.loop_id)
        self.paths.root.mkdir(parents=True)
        self._write_contract()
        self._write_gate()
        (self.paths.root / "task.md").write_text(
            "Review project health.\n", encoding="utf-8"
        )
        (self.paths.root / "assessment.json").write_text("{}\n", encoding="utf-8")
        self.fake_result = self.paths.root / "fake-result.json"
        self._write_fake_result()
        self.old_test_mode = os.environ.get("AGENT_LOOP_TEST_MODE")
        self.old_kill = os.environ.get("AGENT_LOOP_TEST_KILL")
        os.environ["AGENT_LOOP_TEST_MODE"] = "1"
        os.environ.pop("AGENT_LOOP_TEST_KILL", None)

    def tearDown(self) -> None:
        if self.old_test_mode is None:
            os.environ.pop("AGENT_LOOP_TEST_MODE", None)
        else:
            os.environ["AGENT_LOOP_TEST_MODE"] = self.old_test_mode
        if self.old_kill is None:
            os.environ.pop("AGENT_LOOP_TEST_KILL", None)
        else:
            os.environ["AGENT_LOOP_TEST_KILL"] = self.old_kill
        self.temp.cleanup()

    def _contract(self) -> dict:
        return {
            "schema_version": 1,
            "loop_id": self.loop_id,
            "goal": "Report project health.",
            "non_goals": ["No implementation."],
            "watched_scope": ["src/**"],
            "cadence": {"kind": "manual"},
            "timezone": "UTC",
            "maturity": "L1",
            "delegation": {
                "via": "agent-fleet",
                "mode": "review",
                "agent": "auto",
                "kind": "review",
                "complexity": "low",
                "task_file": "task.md",
                "assessment_file": "assessment.json",
                "enable_web": False,
            },
            "limits": {
                "max_attempts_per_run": 1,
                "run_timeout_seconds": 30,
                "snapshot_max_files": 100,
                "snapshot_max_bytes": 1_000_000,
                "budget": {
                    "runs_per_day": 10,
                    "tokens_per_run": 1000,
                    "tokens_per_day": 5000,
                    "unknown_token_policy": "stop",
                },
            },
            "stop_conditions": {
                "no_progress_runs": 3,
                "repeated_failure_runs": 3,
                "budget_stop": True,
            },
            "human_gates": ["L2_promotion"],
            "kill_switch": {"file": "STOP", "env": "AGENT_LOOP_TEST_KILL"},
            "retention": {"runs_keep_days": 30, "reports_keep_days": 30},
            "write_policy": {
                "allowed_write_areas": [".agents/loops/health/**"],
                "path_claims": ["src/**"],
                "forbid_watched_writes": True,
            },
        }

    def _write_contract(self, mutate=None) -> None:
        contract = self._contract()
        if mutate:
            mutate(contract)
        self.paths.contract.write_text(
            json.dumps(contract, indent=2) + "\n",
            encoding="utf-8",
        )

    def _write_gate(self) -> None:
        gate = {
            "schema_version": 1,
            "loop_id": self.loop_id,
            "verifier_default": "REJECT",
            "criteria": [
                {
                    "id": "parent-review",
                    "description": "Parent independently reviews the result.",
                    "check_kind": "parent-review",
                    "required": True,
                }
            ],
            "gates": [
                {
                    "id": "L2_promotion",
                    "kind": "human",
                    "status": "pending",
                    "approver": None,
                    "decided_at": None,
                    "note": None,
                }
            ],
        }
        self.paths.gate.write_text(json.dumps(gate, indent=2) + "\n", encoding="utf-8")

    def _fake_payload(
        self,
        *,
        status: str = "COMPLETED",
        tokens: int | None = 100,
        fleet_run_id: str = "run-fleet-test",
        summary: str = "No blockers.",
    ) -> dict:
        return {
            "action": "run",
            "status": status,
            "run_id": fleet_run_id,
            "agent": "codex",
            "model": "test-model",
            "duration_seconds": 1.25,
            "timed_out": status == "TIMEOUT",
            "token_usage": {
                "status": "known" if tokens is not None else "unknown",
                "total_tokens": tokens,
            },
            "quota_before": {"status": "unknown"},
            "quota_after": {"status": "not-refreshed"},
            "handoff": {"status": status.lower(), "summary": summary},
        }

    def _write_fake_result(self, **kwargs) -> None:
        self.fake_result.write_text(
            json.dumps(self._fake_payload(**kwargs), indent=2) + "\n",
            encoding="utf-8",
        )

    def _args(self, **overrides) -> argparse.Namespace:
        values = {
            "project": str(self.project),
            "loop": self.loop_id,
            "fleet_script": None,
            "report_only": True,
            "fake_fleet_result": "fake-result.json",
            "reason": "test",
            "operator_confirmed": True,
            "result": "acceptance.json",
        }
        values.update(overrides)
        return argparse.Namespace(**values)

    def _all_file_state(self) -> dict[str, tuple[int, int]]:
        return {
            path.relative_to(self.project).as_posix(): (
                path.stat().st_size,
                path.stat().st_mtime_ns,
            )
            for path in self.project.rglob("*")
            if path.is_file()
        }

    def test_validate_plan_doctor_are_zero_write(self) -> None:
        before = self._all_file_state()
        validate = agent_loop.command_validate(self._args())
        plan = agent_loop.command_plan(self._args())
        doctor = agent_loop.command_doctor(self._args())
        after = self._all_file_state()
        self.assertEqual(before, after)
        self.assertEqual(validate["writes_performed"], 0)
        self.assertEqual(plan["writes_performed"], 0)
        self.assertEqual(doctor["writes_performed"], 0)

    def test_fake_run_records_pending_parent_acceptance(self) -> None:
        result = agent_loop.command_run(self._args())
        self.assertEqual(result["status"], "COMPLETED")
        self.assertEqual(result["verifier"], "REJECT")
        self.assertTrue(result["parent_acceptance_required"])
        state = agent_loop.load_json(self.paths.state, "state")
        self.assertEqual(state["phase"], "IDLE")
        events = agent_loop.read_ledger(self.paths)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["verification"]["verdict"], "REJECT")
        self.assertEqual(events[0]["budget_charged"]["tokens_counted"], 100)
        self.assertTrue(Path(result["report_path"]).is_file())

    def test_record_result_appends_and_is_idempotent(self) -> None:
        run = agent_loop.command_run(self._args())
        acceptance = {
            "run_id": run["run_id"],
            "fleet_run_id": run["fleet"]["fleet_run_id"],
            "verified_by": "codex",
            "quality": 0,
            "criteria_pass": False,
            "note": "Rejected in test.",
        }
        (self.paths.root / "acceptance.json").write_text(
            json.dumps(acceptance) + "\n",
            encoding="utf-8",
        )
        first = agent_loop.command_record_result(self._args())
        second = agent_loop.command_record_result(self._args())
        self.assertEqual(first["status"], "RECORDED")
        self.assertEqual(second["status"], "ALREADY_RECORDED")
        events = agent_loop.read_ledger(self.paths)
        self.assertEqual(
            [event["event_type"] for event in events], ["run", "acceptance"]
        )
        self.assertEqual(events[0]["verification"]["verdict"], "REJECT")

    def test_unknown_tokens_fail_closed_with_hard_budget(self) -> None:
        self._write_fake_result(tokens=None)
        result = agent_loop.command_run(self._args())
        self.assertEqual(result["breaker"], "TELEMETRY_UNKNOWN")
        state = agent_loop.load_json(self.paths.state, "state")
        self.assertTrue(state["breaker"]["open"])

    def test_estimate_overrun_records_warning_and_continues(self) -> None:
        self._write_contract(
            lambda doc: doc["limits"]["budget"].update(
                {"token_policy": "estimate", "tokens_per_day": 100}
            )
        )
        self._write_fake_result(tokens=1001)
        result = agent_loop.command_run(self._args())
        self.assertIsNone(result["breaker"])
        self._write_fake_result(tokens=1)
        next_result = agent_loop.command_run(self._args())
        self.assertIsNone(next_result["breaker"])
        event = agent_loop.read_ledger(self.paths)[0]
        self.assertIn("tokens_per_run budget exceeded: 1001>1000", event["budget_charged"]["warnings"])
        self.assertEqual(event["fleet"]["tokens"], 1001)

    def test_estimate_unknown_records_warning_and_continues(self) -> None:
        self._write_contract(
            lambda doc: doc["limits"]["budget"].update(
                {"token_policy": "estimate", "tokens_per_day": 100}
            )
        )
        self._write_fake_result(tokens=None)
        result = agent_loop.command_run(self._args())
        self.assertIsNone(result["breaker"])
        event = agent_loop.read_ledger(self.paths)[0]
        self.assertTrue(event["budget_charged"]["tokens_unknown"])
        self.assertEqual(
            event["budget_charged"]["warnings"],
            ["token telemetry is unknown; numeric totals remain separate"],
        )

    def test_invalid_token_policy_is_rejected(self) -> None:
        self._write_contract(
            lambda doc: doc["limits"]["budget"].update({"token_policy": "maybe"})
        )
        with self.assertRaisesRegex(agent_loop.AgentLoopError, "token_policy"):
            agent_loop.load_validated(self.project, self.loop_id)

    def test_legacy_unknown_allow_remains_nonblocking(self) -> None:
        self._write_contract(
            lambda doc: doc["limits"]["budget"].update(
                {"token_policy": "hard", "unknown_token_policy": "allow"}
            )
        )
        self._write_fake_result(tokens=None)
        result = agent_loop.command_run(self._args())
        self.assertIsNone(result["breaker"])

    def test_same_result_opens_no_progress_breaker(self) -> None:
        self._write_contract(
            lambda doc: doc["stop_conditions"].update({"no_progress_runs": 1})
        )
        first = agent_loop.command_run(self._args())
        self.assertIsNone(first["breaker"])
        second = agent_loop.command_run(self._args())
        self.assertEqual(second["breaker"], "NO_PROGRESS")

    def test_repeated_failure_breaker(self) -> None:
        self._write_contract(
            lambda doc: doc["stop_conditions"].update({"repeated_failure_runs": 1})
        )
        self._write_fake_result(status="FAILED")
        result = agent_loop.command_run(self._args())
        self.assertEqual(result["breaker"], "REPEATED_FAILURE")

    def test_watched_write_is_detected_and_rejected(self) -> None:
        original_loader = agent_loop._load_fake_result

        def mutate_then_load(paths, value):
            (self.project / "src" / "app.py").write_text(
                "print('mutated')\n",
                encoding="utf-8",
            )
            return original_loader(paths, value)

        with mock.patch.object(
            agent_loop, "_load_fake_result", side_effect=mutate_then_load
        ):
            result = agent_loop.command_run(self._args())
        self.assertEqual(result["status"], "REJECTED_WRITE")
        self.assertEqual(result["breaker"], "L1_WRITE_DETECTED")
        self.assertEqual(result["writes_performed"], 1)

    def test_control_plane_write_is_detected_without_ledger_acceptance(self) -> None:
        original_loader = agent_loop._load_fake_result

        def mutate_then_load(paths, value):
            (paths.root / "task.md").write_text(
                "Changed during contractor execution.\n",
                encoding="utf-8",
            )
            return original_loader(paths, value)

        with mock.patch.object(
            agent_loop, "_load_fake_result", side_effect=mutate_then_load
        ):
            result = agent_loop.command_run(self._args())

        self.assertEqual(result["status"], "REJECTED_CONTROL_PLANE")
        self.assertEqual(result["breaker"], "CONTROL_PLANE_WRITE_DETECTED")
        self.assertFalse(result["parent_acceptance_required"])
        self.assertFalse(result["ledger_event_recorded"])
        self.assertEqual(agent_loop.read_ledger(self.paths), [])
        state = agent_loop.load_json(self.paths.state, "state")
        self.assertEqual(
            state["breaker"]["reason"],
            "CONTROL_PLANE_WRITE_DETECTED",
        )

    def test_interrupted_run_requires_operator_recovery(self) -> None:
        paths, config = agent_loop.load_validated(self.project, self.loop_id)
        state = agent_loop.default_state(
            self.loop_id, config["timezone"], paths.loop_lock
        )
        state["phase"] = "DELEGATING"
        state["current_run_id"] = "run-interrupted"
        state["current_started_at"] = agent_loop.utc_now()
        agent_loop.save_state(paths, state, config)
        with self.assertRaises(agent_loop.AgentLoopError):
            agent_loop.command_run(self._args())
        recovered = agent_loop.command_recover(self._args())
        self.assertEqual(recovered["status"], "STOPPED")
        events = agent_loop.read_ledger(paths)
        self.assertEqual(events[0]["run_id"], "run-interrupted")
        self.assertEqual(events[0]["breaker_event"]["reason"], "INTERRUPTED")

    def test_kill_switch_refuses_without_new_run_event(self) -> None:
        os.environ["AGENT_LOOP_TEST_KILL"] = "1"
        with self.assertRaises(agent_loop.AgentLoopError):
            agent_loop.command_run(self._args())
        self.assertEqual(agent_loop.read_ledger(self.paths), [])

    def test_stop_sets_file_and_breaker(self) -> None:
        result = agent_loop.command_stop(self._args())
        self.assertEqual(result["status"], "STOPPED")
        self.assertTrue((self.paths.root / "STOP").is_file())
        state = agent_loop.load_json(self.paths.state, "state")
        self.assertEqual(state["breaker"]["reason"], "KILL_SWITCH")

    def test_path_claim_collision_is_process_shared(self) -> None:
        other = agent_loop.build_paths(self.project, "other")
        other.root.mkdir(parents=True)
        first = agent_loop.PathClaimsLease(self.paths, ["src/**"])
        second = agent_loop.PathClaimsLease(other, ["src/app.py"])
        first.acquire()
        try:
            with self.assertRaises(agent_loop.AgentLoopError):
                second.acquire()
        finally:
            first.release()

    def test_cli_run_refuses_when_loop_lock_is_held(self) -> None:
        command = [
            sys.executable,
            str(SCRIPT_DIR / "agent_loop.py"),
            "run",
            "--project",
            str(self.project),
            "--loop",
            self.loop_id,
            "--report-only",
            "--fake-fleet-result",
            "fake-result.json",
        ]
        environment = dict(os.environ)
        environment["AGENT_LOOP_TEST_MODE"] = "1"

        with security.LoopLock(self.paths.loop_lock):
            completed = subprocess.run(
                command,
                cwd=str(self.project),
                env=environment,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                check=False,
                timeout=10,
            )

        self.assertEqual(completed.returncode, 2)
        refusal = json.loads(completed.stderr)
        self.assertEqual(refusal["status"], "REFUSED")
        self.assertIn("loop_lock_busy", refusal["error"])
        self.assertFalse(self.paths.ledger.exists())
        self.assertFalse(self.paths.state.exists())

    def test_snapshot_excludes_operational_state(self) -> None:
        before = agent_loop.snapshot_watched(
            self.project,
            ["."],
            max_files=100,
            max_bytes=1_000_000,
        )
        (self.paths.root / "transient.txt").write_text("state\n", encoding="utf-8")
        after = agent_loop.snapshot_watched(
            self.project,
            ["."],
            max_files=100,
            max_bytes=1_000_000,
        )
        self.assertEqual(before, after)

    def test_invalid_l2_is_refused(self) -> None:
        self._write_contract(lambda doc: doc.update({"maturity": "L2"}))
        with self.assertRaises(agent_loop.AgentLoopError):
            agent_loop.load_validated(self.project, self.loop_id)

    def test_scrub_rejects_allowlist_prefix_bypass(self) -> None:
        cleaned = security.scrub_environment(
            {
                "PATH": "C:\\Windows",
                "HOME_PASSWORD": "secret",
                "PATH_SECRET": "secret",
                "AGENT_LOOP_API_KEY": "secret",
                "AGENT_LOOP_FLEET_SCRIPT": "C:\\untrusted\\fleet.py",
                "AGENT_FLEET_DEPTH": "0",
                "AGENT_FLEET_ROUTING_MODE": "baseline",
                "AGENT_FLEET_MODEL_RADAR_SCRIPT": "C:\\untrusted\\radar.py",
                "PYTHONPATH": "C:\\untrusted\\modules",
                "PYTHONINSPECT": "1",
                "PYTHONUTF8": "1",
            }
        )
        self.assertIn("PATH", cleaned)
        self.assertIn("AGENT_FLEET_DEPTH", cleaned)
        self.assertIn("AGENT_FLEET_ROUTING_MODE", cleaned)
        self.assertIn("PYTHONUTF8", cleaned)
        self.assertNotIn("HOME_PASSWORD", cleaned)
        self.assertNotIn("PATH_SECRET", cleaned)
        self.assertNotIn("AGENT_LOOP_API_KEY", cleaned)
        self.assertNotIn("AGENT_LOOP_FLEET_SCRIPT", cleaned)
        self.assertNotIn("AGENT_FLEET_MODEL_RADAR_SCRIPT", cleaned)
        self.assertNotIn("PYTHONPATH", cleaned)
        self.assertNotIn("PYTHONINSPECT", cleaned)

    def test_fleet_summary_sanitizes_untrusted_text(self) -> None:
        synthetic_token = "sk-" + "abcdefghijklmnopqrstuvwxyz" + "012345"
        summary = agent_loop.safe_fleet_summary(
            {
                "run_id": "fleet-run\ninjected",
                "agent": "codex`spoof",
                "model": synthetic_token,
                "status": "COMPLETED\r\nextra",
                "duration_seconds": True,
                "token_usage": {"total_tokens": 42},
                "quota_before": {"source": synthetic_token},
            }
        )
        self.assertEqual(summary["fleet_run_id"], "fleet-run injected")
        self.assertEqual(summary["agent"], "codex spoof")
        self.assertEqual(summary["model"], "[redacted]")
        self.assertEqual(summary["run_status"], "COMPLETED  extra")
        self.assertIsNone(summary["duration_seconds"])
        self.assertEqual(summary["quota_before"]["source"], "[redacted]")

    def test_fleet_result_rejects_duplicate_json_keys(self) -> None:
        with self.assertRaises(agent_loop.AgentLoopError):
            agent_loop._parse_process_json('{"status":"ok","status":"bad"}', "")

    def test_corrupt_nested_state_is_refused(self) -> None:
        state = agent_loop.default_state(
            self.loop_id,
            agent_loop.ZoneInfo("UTC"),
            self.paths.loop_lock,
        )
        state["failure"] = {}
        security.atomic_write_json(self.paths.state, state)

        with self.assertRaises(agent_loop.AgentLoopError):
            agent_loop.load_state(
                self.paths,
                agent_loop.load_validated(self.project, self.loop_id)[1],
            )

    def test_ledger_acceptance_for_unknown_run_is_refused(self) -> None:
        security.append_jsonl(
            self.paths.ledger,
            {
                "schema_version": 1,
                "event_id": "evt-unknown-acceptance",
                "event_type": "acceptance",
                "run_id": "run-unknown",
                "fleet_run_id": "fleet-unknown",
                "recorded_at": agent_loop.utc_now(),
                "verified_by": "codex-parent",
                "quality": 0,
                "criteria_pass": False,
                "verdict": "REJECT",
                "note": "Unknown run must fail closed.",
            },
        )

        with self.assertRaisesRegex(agent_loop.AgentLoopError, "unknown run"):
            agent_loop.read_ledger(self.paths)

    def test_schema_documents_are_valid_json(self) -> None:
        references = SCRIPT_DIR.parent / "references"
        for schema in references.glob("*.schema.json"):
            with self.subTest(schema=schema.name):
                parsed = json.loads(schema.read_text(encoding="utf-8"))
                self.assertEqual(
                    parsed["$schema"], "https://json-schema.org/draft/2020-12/schema"
                )

    def test_shipped_examples_validate(self) -> None:
        references = SCRIPT_DIR.parent / "references"
        loop_id = "weekly-project-health"
        paths = agent_loop.build_paths(self.project, loop_id)
        paths.root.mkdir(parents=True)
        paths.contract.write_text(
            (references / "example-loop.json").read_text(encoding="utf-8"),
            encoding="utf-8",
        )
        paths.gate.write_text(
            (references / "example-gate.json").read_text(encoding="utf-8"),
            encoding="utf-8",
        )
        (paths.root / "task.md").write_text(
            "Review project health.\n", encoding="utf-8"
        )
        (paths.root / "assessment.json").write_text("{}\n", encoding="utf-8")

        loaded_paths, config = agent_loop.load_validated(self.project, loop_id)

        self.assertEqual(loaded_paths.root, paths.root)
        self.assertEqual(config["contract"]["maturity"], "L1")
        self.assertEqual(config["contract"]["delegation"]["via"], "agent-fleet")

    def test_cli_version(self) -> None:
        completed = subprocess.run(
            [sys.executable, str(SCRIPT_DIR / "agent_loop.py"), "--version"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn(agent_loop.VERSION, completed.stdout)

    def test_skill_routes_scientific_campaigns_through_fleet_gate_zero(self) -> None:
        skill = (SCRIPT_DIR.parent / "SKILL.md").read_text(encoding="utf-8")
        for phrase in (
            "Scientific campaign",
            "campaign-validate",
            "--campaign-contract",
            "--experiment-card",
            "parent records Gate 0",
            "operator-confirmed `recover`",
        ):
            self.assertIn(phrase, skill)


if __name__ == "__main__":
    unittest.main(verbosity=2)
