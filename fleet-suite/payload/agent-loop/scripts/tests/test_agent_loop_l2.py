import importlib.util
import json
import os
import socket
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock


RUNTIME = Path(__file__).resolve().parents[1]
MODULE_PATH = RUNTIME / "agent_loop_l2.py"
VERIFIER = RUNTIME / "trusted_verifiers" / "python_compile_v1.py"
SPEC = importlib.util.spec_from_file_location("agent_loop_l2", MODULE_PATH)
assert SPEC and SPEC.loader
loop = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(loop)


def run(command, cwd):
    completed = subprocess.run(
        command,
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        raise AssertionError(completed.stderr or completed.stdout)
    return completed.stdout.strip()


class AgentLoopL2Test(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        (self.repo / "code.py").write_text("VALUE = 1\n", encoding="utf-8")
        (self.repo / "protected.txt").write_text("do not change\n", encoding="utf-8")
        run(["git", "init", "-q"], self.repo)
        run(["git", "config", "user.name", "Agent Loop Test"], self.repo)
        run(["git", "config", "user.email", "agent-loop@example.invalid"], self.repo)
        run(["git", "add", "."], self.repo)
        run(["git", "commit", "-q", "-m", "baseline"], self.repo)

        self.assessments = self.root / "assessments"
        self.assessments.mkdir()
        self.implementer_assessment = self.assessments / "implementer.json"
        self.validator_assessment = self.assessments / "validator.json"
        self.implementer_assessment.write_text("{}\n", encoding="utf-8")
        self.validator_assessment.write_text("{}\n", encoding="utf-8")

        current = self.root / "current"
        self.fleet_script = current / "agent-fleet" / "scripts" / "fleet.py"
        self.fleet_script.parent.mkdir(parents=True)
        self.fleet_script.write_text("print('{}')\n", encoding="utf-8")
        (current / ".agent-fleet-suite-runtime.json").write_text(
            json.dumps({"distribution_package_version": "test"}), encoding="utf-8"
        )
        self.state_root = self.root / "state"
        self.contract_path = self.root / "contract.json"
        self.write_contract()

    def tearDown(self):
        if self.repo.exists():
            listed = run(["git", "worktree", "list", "--porcelain"], self.repo)
            worktrees = [line[9:] for line in listed.splitlines() if line.startswith("worktree ")]
            for worktree in worktrees[1:]:
                subprocess.run(
                    ["git", "worktree", "remove", "--force", worktree],
                    cwd=self.repo,
                    capture_output=True,
                    check=False,
                )
        self.temporary.cleanup()

    def contract(self):
        return {
            "schema_version": 2,
            "loop_id": "test-loop",
            "goal": "Change only code.py and prove the bounded loop controls.",
            "non_goals": ["No merge", "No deploy"],
            "project": str(self.repo),
            "base_ref": "HEAD",
            "scope": {
                "allowed_paths": ["code.py"],
                "protected_paths": ["protected.txt"],
            },
            "executor": {
                "runtime_profile": "l2-thin",
                "knowledge_mode": "none",
                "max_prompt_bytes": 20000,
                "max_model_tool_calls": 4,
                "max_task_bytes": 20000,
                "max_repository_files": 10,
                "max_repository_bytes": 100000,
            },
            "implementer": {
                "agent": "codex",
                "model": None,
                "execution_profile": None,
                "assessment_file": str(self.implementer_assessment),
                "timeout_seconds": 30,
            },
            "validator": {
                "agent": "claude",
                "model": None,
                "execution_profile": None,
                "assessment_file": str(self.validator_assessment),
                "timeout_seconds": 30,
            },
            "verification": {
                "trusted_runner": str(VERIFIER),
                "trusted_runner_sha256": loop.sha256_file(VERIFIER),
                "timeout_seconds": 30,
            },
            "limits": {
                "max_repairs": 1,
                "max_wall_seconds": 300,
                "max_total_tokens": 100000,
                "max_fresh_input_tokens": None,
                "max_cache_read_tokens": None,
                "max_cache_write_tokens": None,
                "max_output_tokens": None,
                "max_reasoning_tokens": None,
                "unknown_token_policy": "allow",
                "token_policy": "hard",
                "max_changed_files": 2,
                "max_diff_bytes": 100000,
            },
            "network_allowed": False,
            "kill_switch": {"file": "STOP", "env": "AGENT_LOOP_TEST_STOP"},
        }

    def write_contract(self, value=None):
        self.contract_path.write_text(
            json.dumps(value or self.contract(), indent=2) + "\n", encoding="utf-8"
        )

    def prepare(self):
        result = loop.prepare_loop(self.contract_path, self.state_root, self.fleet_script)
        return Path(result["loop_dir"]), result

    def approve(self, loop_dir, contract_hash):
        return loop.approve_loop(loop_dir, contract_hash, "unit-test")

    def add_worktree(self, loop_dir, run_name, outside=False):
        output_dir = (self.root / "escaped" / run_name) if outside else (loop_dir / "fleet-runs" / run_name)
        output_dir.mkdir(parents=True)
        worktree = output_dir / "worktree"
        run(["git", "worktree", "add", "--detach", str(worktree), "HEAD"], self.repo)
        return output_dir, worktree

    @staticmethod
    def maker_result(output_dir, worktree, run_id="maker-1"):
        return {
            "status": "COMPLETED",
            "handoff_valid": True,
            "run_id": run_id,
            "agent": "codex",
            "output_dir": str(output_dir),
            "git_evidence": {"worktree": str(worktree)},
            "token_usage": {"total_tokens": 10},
        }

    def test_prepare_is_atomic_and_approval_is_hash_bound(self):
        loop_dir, prepared = self.prepare()
        self.assertEqual(prepared["status"], "AWAITING_GO")
        self.assertFalse(any(self.state_root.glob(".test-loop.prepare-*")))
        frozen, state = loop.load_json(loop_dir / "contract.json", "contract"), loop.load_json(
            loop_dir / "state.json", "state"
        )
        for actor in ("implementer", "validator"):
            assessment = Path(frozen["contract"][actor]["assessment_file"])
            self.assertTrue(assessment.is_file())
            self.assertTrue(str(assessment).startswith(str(loop_dir / "inputs")))
        self.assertEqual(
            frozen["controller_runtime"]["script_sha256"], loop.sha256_file(MODULE_PATH)
        )
        with self.assertRaises(loop.PilotError):
            self.approve(loop_dir, "0" * 64)
        approved = self.approve(loop_dir, state["contract_hash"])
        self.assertEqual(approved["status"], "READY")

    def test_frozen_runtime_manifest_detects_helper_drift(self):
        loop_dir, _ = self.prepare()
        helper = self.fleet_script.parent / "unexpected-helper.py"
        helper.write_text("VALUE = 1\n", encoding="utf-8")
        with self.assertRaisesRegex(loop.PilotError, "runtime manifest drift"):
            loop.load_loop(loop_dir)

    def test_prepare_rejects_state_inside_project(self):
        with self.assertRaisesRegex(loop.PilotError, "outside the project"):
            loop.prepare_loop(self.contract_path, self.repo / "state", self.fleet_script)

    def test_contract_rejects_same_implementer_and_validator(self):
        value = self.contract()
        value["validator"]["agent"] = "CODEX"
        self.write_contract(value)
        with self.assertRaisesRegex(loop.PilotError, "different agents"):
            self.prepare()

    def test_contract_rejects_repository_above_context_gate(self):
        value = self.contract()
        value["executor"]["max_repository_files"] = 1
        self.write_contract(value)
        with self.assertRaisesRegex(loop.PilotError, "file count"):
            self.prepare()

    def test_invoke_fleet_passes_and_verifies_thin_runtime_controls(self):
        task = self.root / "task.txt"
        task.write_text("bounded task\n", encoding="utf-8")
        actor = self.contract()["implementer"]
        executor = self.contract()["executor"]
        payload = {
            "status": "COMPLETED",
            "runtime_profile": "l2-thin",
            "knowledge_mode": "none",
            "max_prompt_bytes": 20000,
            "max_model_tool_calls": 4,
            "prompt_bytes": 1234,
        }
        completed = subprocess.CompletedProcess(
            ["fleet"], 0, json.dumps(payload), ""
        )
        fleet_state = self.root / "fleet-state"
        fleet_state.mkdir()
        with mock.patch.object(loop, "run_process", return_value=completed) as process:
            parsed, _, _ = loop.invoke_fleet(
                fleet_script=self.fleet_script,
                actor=actor,
                executor=executor,
                task_path=task,
                kind="implementation",
                complexity="low",
                mode="implement",
                workspace=self.repo,
                output_root=self.root / "runs",
                state_dir=fleet_state,
            )
        command = process.call_args.args[0]
        self.assertEqual(parsed["runtime_profile"], "l2-thin")
        self.assertEqual(command[command.index("--state-dir") + 1], str(fleet_state.resolve()))
        self.assertEqual(command[command.index("--knowledge-mode") + 1], "none")
        self.assertEqual(command[command.index("--max-model-tool-calls") + 1], "4")

    def test_candidate_rejects_protected_change(self):
        loop_dir, _ = self.prepare()
        frozen = loop.load_json(loop_dir / "contract.json", "contract")
        output_dir, worktree = self.add_worktree(loop_dir, "protected")
        (worktree / "protected.txt").write_text("changed\n", encoding="utf-8")
        attempt = loop_dir / "evidence" / "attempt-1"
        attempt.mkdir()
        with self.assertRaisesRegex(loop.PilotError, "protected path"):
            loop.inspect_candidate(
                self.maker_result(output_dir, worktree), frozen["contract"], attempt
            )

    def test_candidate_rejects_fleet_output_escape(self):
        loop_dir, _ = self.prepare()
        frozen = loop.load_json(loop_dir / "contract.json", "contract")
        output_dir, worktree = self.add_worktree(loop_dir, "escaped", outside=True)
        (worktree / "code.py").write_text("VALUE = 2\n", encoding="utf-8")
        attempt = loop_dir / "evidence" / "attempt-1"
        attempt.mkdir()
        with self.assertRaisesRegex(loop.PilotError, "escaped the loop state root"):
            loop.inspect_candidate(
                self.maker_result(output_dir, worktree), frozen["contract"], attempt
            )

    def test_recover_refuses_live_lock(self):
        loop_dir, _ = self.prepare()
        state = loop.load_json(loop_dir / "state.json", "state")
        state["phase"] = "IMPLEMENTING"
        loop.atomic_json(loop_dir / "state.json", state)
        loop.atomic_json(
            loop_dir / ".lock",
            {"pid": os.getpid(), "host": socket.gethostname(), "created_at": loop.utc_now()},
        )
        with self.assertRaisesRegex(loop.PilotError, "still active"):
            loop.recover_loop(loop_dir, "test")
        (loop_dir / ".lock").unlink()

    def test_recover_removes_only_confirmed_stale_local_lock(self):
        loop_dir, _ = self.prepare()
        state = loop.load_json(loop_dir / "state.json", "state")
        state["phase"] = "VERIFYING"
        loop.atomic_json(loop_dir / "state.json", state)
        loop.atomic_json(
            loop_dir / ".lock",
            {"pid": 99999999, "host": socket.gethostname(), "created_at": loop.utc_now()},
        )
        with mock.patch.object(loop, "process_is_alive", return_value=False):
            result = loop.recover_loop(loop_dir, "simulated crash")
        self.assertEqual(result["status"], "ESCALATED")
        self.assertFalse((loop_dir / ".lock").exists())

    def test_strict_verifier_output_rejects_extra_lines(self):
        with self.assertRaisesRegex(loop.PilotError, "exactly one"):
            loop.parse_single_json_object('{"status":"PASS"}\nnoise\n', "verifier")

    def test_grok_invocation_disables_foreign_harness_context(self):
        with mock.patch.dict(
            loop.os.environ,
            {"HOME": str(self.root), "USERPROFILE": str(self.root), "SENTINEL": "1"},
            clear=True,
        ):
            env = loop.fleet_invocation_environment({"agent": "grok"})
        self.assertEqual(env["GROK_HOME"], str(self.root / ".grok"))
        self.assertEqual(env["GROK_MEMORY"], "0")
        self.assertEqual(env["GROK_SUBAGENTS"], "0")
        self.assertEqual(env["GROK_CLAUDE_SKILLS_ENABLED"], "false")
        self.assertEqual(env["GROK_CLAUDE_HOOKS_ENABLED"], "false")
        self.assertEqual(env["GROK_CURSOR_MCPS_ENABLED"], "false")
        self.assertEqual(env["SENTINEL"], "1")

    def test_unknown_token_telemetry_can_fail_closed(self):
        state = {
            "tokens_total": 0,
            "fresh_input_tokens_total": 0,
            "cache_read_tokens_total": 0,
            "cache_write_tokens_total": 0,
            "output_tokens_total": 0,
            "reasoning_tokens_total": 0,
            "token_telemetry_complete": True,
            "cost_telemetry_complete": True,
        }
        contract = self.contract()
        contract["limits"]["unknown_token_policy"] = "stop"
        violations = loop.update_budget(state, contract, {})
        self.assertIn("total_tokens telemetry is unknown", violations[0])
        self.assertFalse(state["token_telemetry_complete"])

    def test_estimate_records_overrun_and_unknown_without_stopping(self):
        state = {
            "tokens_total": 0,
            "fresh_input_tokens_total": 0,
            "cache_read_tokens_total": 0,
            "cache_write_tokens_total": 0,
            "output_tokens_total": 0,
            "reasoning_tokens_total": 0,
            "known_cost_usd_total": 0.0,
            "token_telemetry_complete": True,
            "cost_telemetry_complete": True,
            "token_warnings": [],
        }
        contract = self.contract()
        contract["limits"]["token_policy"] = "estimate"
        contract["limits"]["max_total_tokens"] = 10
        self.assertEqual(
            loop.update_budget(state, contract, {"token_usage": {"total_tokens": 11}}),
            [],
        )
        self.assertEqual(
            loop.update_budget(state, contract, {"token_usage": {}}),
            [],
        )
        self.assertEqual(state["tokens_total"], 11)
        self.assertFalse(state["token_telemetry_complete"])
        self.assertEqual(len(state["token_warnings"]), 2)
        self.assertIn("total_tokens budget exceeded: 11>10", state["token_warnings"][0])
        self.assertIn("total_tokens telemetry is unknown", state["token_warnings"][1])

    def test_missing_token_policy_keeps_legacy_hard_behavior(self):
        state = {
            "tokens_total": 0,
            "fresh_input_tokens_total": 0,
            "cache_read_tokens_total": 0,
            "cache_write_tokens_total": 0,
            "output_tokens_total": 0,
            "reasoning_tokens_total": 0,
            "token_telemetry_complete": True,
            "cost_telemetry_complete": True,
            "token_warnings": [],
        }
        contract = self.contract()
        contract["limits"].pop("token_policy")
        contract["limits"]["max_total_tokens"] = 10
        contract["limits"]["unknown_token_policy"] = "stop"
        violations = loop.update_budget(state, contract, {})
        self.assertIn("total_tokens telemetry is unknown", violations[0])
        self.assertEqual(state["token_warnings"], [])

    def test_invalid_token_policy_is_rejected(self):
        value = self.contract()
        value["limits"]["token_policy"] = "maybe"
        self.write_contract(value)
        with self.assertRaisesRegex(loop.PilotError, "token_policy"):
            self.prepare()

    def test_legacy_unknown_allow_remains_nonblocking(self):
        state = {"token_telemetry_complete": True, "cost_telemetry_complete": True}
        contract = self.contract()
        contract["limits"].pop("token_policy", None)
        contract["limits"]["unknown_token_policy"] = "allow"
        contract["limits"]["max_total_tokens"] = 10
        self.assertEqual(loop.update_budget(state, contract, {}), [])
        self.assertFalse(state["token_telemetry_complete"])

    def test_token_budget_separates_fresh_and_cached_input(self):
        state = {
            "tokens_total": 0,
            "fresh_input_tokens_total": 0,
            "cache_read_tokens_total": 0,
            "cache_write_tokens_total": 0,
            "output_tokens_total": 0,
            "reasoning_tokens_total": 0,
            "known_cost_usd_total": 0.0,
            "token_telemetry_complete": True,
            "cost_telemetry_complete": True,
        }
        contract = self.contract()
        contract["limits"]["max_fresh_input_tokens"] = 25
        result = {
            "adapter": "codex",
            "token_usage": {
                "input_tokens": 100,
                "cache_read_tokens": 80,
                "output_tokens": 5,
                "reasoning_tokens": 2,
                "total_tokens": 105,
                "cost_usd": 0.01,
            },
        }
        self.assertEqual(loop.update_budget(state, contract, result), [])
        self.assertEqual(state["fresh_input_tokens_total"], 20)
        self.assertEqual(state["cache_read_tokens_total"], 80)
        self.assertEqual(state["known_cost_usd_total"], 0.01)

    def test_manifest_rejects_unlisted_evidence(self):
        loop_dir, prepared = self.prepare()
        extra = loop_dir / "evidence" / "extra.txt"
        extra.write_text("unexpected\n", encoding="utf-8")
        manifest = loop_dir / "evidence" / "manifest.json"
        loop.atomic_json(
            manifest,
            {"contract_hash": prepared["contract_hash"], "files": []},
        )
        state = loop.load_json(loop_dir / "state.json", "state")
        state["manifest_path"] = str(manifest)
        with self.assertRaisesRegex(loop.PilotError, "exactly cover"):
            loop.verify_manifest(loop_dir, state)

    def test_run_uses_at_most_one_repair(self):
        loop_dir, prepared = self.prepare()
        self.approve(loop_dir, prepared["contract_hash"])
        first_output, first_worktree = self.add_worktree(loop_dir, "run-1")
        second_output, second_worktree = self.add_worktree(loop_dir, "run-2")
        results = [
            (self.maker_result(first_output, first_worktree, "maker-1"), "", ""),
            (self.maker_result(second_output, second_worktree, "maker-2"), "", ""),
        ]
        with mock.patch.object(loop, "invoke_fleet", side_effect=results) as invoke:
            status = loop.run_loop(loop_dir)
        self.assertEqual(status["status"], "ESCALATED")
        self.assertEqual(status["attempt_count"], 2)
        self.assertEqual(invoke.call_count, 2)

    def test_budget_stop_still_records_the_consuming_run(self):
        value = self.contract()
        value["limits"]["max_total_tokens"] = 5
        self.write_contract(value)
        loop_dir, prepared = self.prepare()
        self.approve(loop_dir, prepared["contract_hash"])
        maker = self.maker_result(self.root / "unused-output", self.root / "unused-worktree")
        maker["token_usage"]["total_tokens"] = 10
        with mock.patch.object(loop, "invoke_fleet", return_value=(maker, "", "")):
            status = loop.run_loop(loop_dir)
        self.assertEqual(status["status"], "STOPPED")
        state = loop.load_json(loop_dir / "state.json", "state")
        self.assertEqual(state["phase"], "STOPPED")
        self.assertEqual(state["tokens_total"], 10)
        self.assertEqual(len(state["attempts"]), 1)
        self.assertEqual(state["attempts"][0]["maker_run_id"], "maker-1")
        self.assertEqual(state["attempts"][0]["outcome"], "BUDGET_STOP")
        self.assertEqual(state["attempts"][0]["diagnostic_after_stop"]["verifier"], "NOT_RUN")

    def test_budget_stop_runs_deterministic_diagnostic_without_reviewer(self):
        value = self.contract()
        value["limits"]["max_total_tokens"] = 5
        self.write_contract(value)
        loop_dir, prepared = self.prepare()
        self.approve(loop_dir, prepared["contract_hash"])
        output_dir, worktree = self.add_worktree(loop_dir, "budget-diagnostic")
        (worktree / "code.py").write_text("VALUE = 2\n", encoding="utf-8")
        maker = self.maker_result(output_dir, worktree)
        maker["token_usage"]["total_tokens"] = 10
        verifier = {"status": "PASS", "result": {"status": "PASS"}}
        with mock.patch.object(loop, "invoke_fleet", return_value=(maker, "", "")) as invoke:
            with mock.patch.object(loop, "run_trusted_verifier", return_value=verifier) as check:
                status = loop.run_loop(loop_dir)
        self.assertEqual(status["status"], "STOPPED")
        self.assertEqual(invoke.call_count, 1)
        check.assert_called_once()
        state = loop.load_json(loop_dir / "state.json", "state")
        diagnostic = state["attempts"][0]["diagnostic_after_stop"]
        self.assertEqual(diagnostic["candidate"], "PRESENT")
        self.assertEqual(diagnostic["verifier"], "PASS")

    def test_baseline_integrity_failure_is_not_suppressed(self):
        loop_dir, prepared = self.prepare()
        self.approve(loop_dir, prepared["contract_hash"])

        def dirty_base(**_kwargs):
            (self.repo / "code.py").write_text("VALUE = 99\n", encoding="utf-8")
            raise loop.PilotError("simulated Fleet failure")

        with mock.patch.object(loop, "invoke_fleet", side_effect=dirty_base):
            with self.assertRaisesRegex(loop.PilotError, "became dirty"):
                loop.run_loop(loop_dir)
        state = loop.load_json(loop_dir / "state.json", "state")
        self.assertEqual(state["phase"], "STOPPED")
        self.assertIn("Baseline integrity failure", state["reason"])
        run(["git", "restore", "code.py"], self.repo)


if __name__ == "__main__":
    unittest.main()
