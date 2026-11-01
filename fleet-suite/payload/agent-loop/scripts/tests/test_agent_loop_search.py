from __future__ import annotations

import contextlib
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


SCRIPTS = Path(__file__).resolve().parents[1]
MODULE_PATH = SCRIPTS / "agent_loop_search.py"
VERIFIER = SCRIPTS / "trusted_verifiers" / "python_compile_v1.py"
sys.path.insert(0, str(SCRIPTS))
SPEC = importlib.util.spec_from_file_location("agent_loop_search", MODULE_PATH)
assert SPEC and SPEC.loader
search = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(search)


def run(command: list[str], cwd: Path) -> str:
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


class AgentLoopSearchTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        (self.repo / "code.py").write_text("VALUE = 1\n", encoding="utf-8")
        (self.repo / "protected.txt").write_text("stable\n", encoding="utf-8")
        run(["git", "init", "-q"], self.repo)
        run(["git", "config", "user.name", "Agent Loop Search Test"], self.repo)
        run(["git", "config", "user.email", "agent-loop-search@example.invalid"], self.repo)
        run(["git", "add", "."], self.repo)
        run(["git", "commit", "-q", "-m", "baseline"], self.repo)

        self.inputs = self.root / "inputs"
        self.inputs.mkdir()
        self.maker_one = self.inputs / "maker-one.json"
        self.maker_two = self.inputs / "maker-two.json"
        self.supervisor = self.inputs / "supervisor.json"
        for path in (self.maker_one, self.maker_two, self.supervisor):
            path.write_text("{}\n", encoding="utf-8")
        self.quality_contract = self.inputs / "quality.json"
        self.quality_contract.write_text(
            json.dumps({"schema_version": 1, "group_id": "search-test"}) + "\n",
            encoding="utf-8",
        )

        current = self.root / "current"
        self.fleet_script = current / "agent-fleet" / "scripts" / "fleet.py"
        self.fleet_script.parent.mkdir(parents=True)
        self.fleet_script.write_text("print('{}')\n", encoding="utf-8")
        (current / ".agent-fleet-suite-runtime.json").write_text(
            json.dumps({"distribution_package_version": "test"}), encoding="utf-8"
        )
        self.contract_path = self.root / "contract.json"
        self.state_root = self.root / "state"
        self.fleet_state = self.root / "fleet-state"
        self.fleet_state.mkdir()
        self.worktrees: list[Path] = []
        self.write_contract()

    def tearDown(self) -> None:
        for worktree in reversed(self.worktrees):
            subprocess.run(
                ["git", "worktree", "remove", "--force", str(worktree)],
                cwd=self.repo,
                capture_output=True,
                text=True,
                check=False,
            )
        self.temporary.cleanup()

    @staticmethod
    def actor(agent: str, assessment: Path) -> dict[str, object]:
        return {
            "agent": agent,
            "model": None,
            "execution_profile": None,
            "assessment_file": str(assessment),
            "timeout_seconds": 60,
        }

    def contract(self) -> dict[str, object]:
        return {
            "schema_version": 3,
            "loop_id": "search-test",
            "goal": "Compare bounded candidate hypotheses without mutating the baseline.",
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
                "max_repository_files": 20,
                "max_repository_bytes": 100000,
            },
            "implementers": [
                self.actor("codex", self.maker_one),
                self.actor("qwen", self.maker_two),
            ],
            "auditor": {"agent": "antigravity", "model": None, "timeout_seconds": 60},
            "supervisor": self.actor("claude", self.supervisor),
            "verification": {
                "trusted_runner": str(VERIFIER),
                "trusted_runner_sha256": search.l2.sha256_file(VERIFIER),
                "timeout_seconds": 30,
            },
            "quality": {
                "contract": str(self.quality_contract),
                "contract_sha256": search.l2.sha256_file(self.quality_contract),
            },
            "search": {
                "hypotheses": ["Use direct lookup.", "Use a normalized index."],
                "complexity": "medium",
                "baseline_quality_score": 70,
                "minimum_improvement": 3,
                "tie_budget": 1,
                "stagnation_window": 2,
                "max_supervisor_interventions": 1,
            },
            "limits": {
                "max_wall_seconds": 600,
                "max_total_tokens": 100000,
                "max_fresh_input_tokens": None,
                "max_cache_read_tokens": None,
                "max_cache_write_tokens": None,
                "max_output_tokens": None,
                "max_reasoning_tokens": None,
                "unknown_token_policy": "allow",
                "token_policy": "estimate",
                "max_changed_files": 2,
                "max_diff_bytes": 100000,
            },
            "network_allowed": False,
            "kill_switch": {"file": "STOP", "env": "AGENT_LOOP_SEARCH_TEST_STOP"},
        }

    def write_contract(self, value: dict[str, object] | None = None) -> None:
        self.contract_path.write_text(
            json.dumps(value or self.contract(), indent=2) + "\n", encoding="utf-8"
        )

    def prepare(self) -> tuple[Path, dict[str, object]]:
        with mock.patch.dict(
            os.environ, {"AGENT_FLEET_STATE_DIR": str(self.fleet_state)}, clear=False
        ):
            result = search.prepare_loop(self.contract_path, self.state_root, self.fleet_script)
        return Path(str(result["loop_dir"])), result

    def add_candidate_worktree(self, loop_dir: Path, run_id: str, value: int) -> tuple[Path, Path]:
        output_dir = loop_dir / "fleet-runs" / run_id
        output_dir.mkdir()
        worktree = output_dir / "worktree"
        run(["git", "worktree", "add", "--detach", str(worktree), "HEAD"], self.repo)
        self.worktrees.append(worktree)
        (worktree / "code.py").write_text(f"VALUE = {value}\n", encoding="utf-8")
        return output_dir, worktree

    def test_prepare_freezes_inputs_and_requires_exact_hash(self) -> None:
        loop_dir, prepared = self.prepare()
        self.assertEqual(prepared["status"], "AWAITING_GO")
        frozen = search.l2.load_json(loop_dir / "contract.json", "contract")
        state = search.l2.load_json(loop_dir / "state.json", "state")
        actor_paths = [
            Path(item["assessment_file"])
            for item in frozen["contract"]["implementers"]
        ]
        actor_paths.append(Path(frozen["contract"]["supervisor"]["assessment_file"]))
        self.assertTrue(all(str(path).startswith(str(loop_dir / "inputs")) for path in actor_paths))
        self.assertEqual(
            Path(frozen["contract"]["quality"]["contract"]),
            (loop_dir / "inputs" / "quality-contract.json").resolve(),
        )
        with self.assertRaisesRegex(search.SearchError, "approved hash"):
            search.approve_loop(loop_dir, "0" * 64, "unit-test")
        approved = search.approve_loop(loop_dir, state["contract_hash"], "unit-test")
        self.assertEqual(approved["status"], "READY")

    def test_prepare_rejects_dirty_baseline(self) -> None:
        (self.repo / "code.py").write_text("VALUE = 2\n", encoding="utf-8")
        with self.assertRaisesRegex(search.SearchError, "clean"):
            self.prepare()

    def test_prepare_rejects_duplicate_hypotheses(self) -> None:
        value = self.contract()
        value["search"]["hypotheses"] = ["Same", "same"]
        self.write_contract(value)
        with self.assertRaisesRegex(search.SearchError, "unique"):
            self.prepare()

    def test_prepare_rejects_non_independent_supervisor(self) -> None:
        value = self.contract()
        value["supervisor"]["agent"] = "CODEX"
        self.write_contract(value)
        with self.assertRaisesRegex(search.SearchError, "supervisor agent"):
            self.prepare()

    def test_prepare_rejects_invalid_token_policy(self) -> None:
        value = self.contract()
        value["limits"]["token_policy"] = "maybe"
        self.write_contract(value)
        with self.assertRaisesRegex(search.SearchError, "token_policy"):
            self.prepare()

    def test_estimate_projection_keeps_unknown_auditor_telemetry_advisory(self) -> None:
        loop_dir, prepared = self.prepare()
        frozen = search.l2.load_json(loop_dir / "contract.json", "contract")
        state = search.l2.load_json(loop_dir / "state.json", "state")
        warnings = search.add_auditor_budget(
            state,
            frozen["contract"],
            {"auditor": {"run_id": "missing-auditor"}, "telemetry": {"runs": []}},
        )
        self.assertEqual(warnings, [])
        self.assertTrue(state["token_warnings"])
        self.assertEqual(state["tokens_total"], 0)

    def test_prepare_rejects_auditor_on_maker_cli_even_with_another_model(self) -> None:
        value = self.contract()
        value["auditor"] = {
            "agent": "codex",
            "model": "another-model",
            "timeout_seconds": 60,
        }
        self.write_contract(value)
        with self.assertRaisesRegex(search.SearchError, "different CLI"):
            self.prepare()

    def test_load_rejects_frozen_quality_contract_drift(self) -> None:
        loop_dir, _ = self.prepare()
        frozen_quality = loop_dir / "inputs" / "quality-contract.json"
        frozen_quality.write_text('{"schema_version": 1, "group_id": "changed"}\n', encoding="utf-8")
        with self.assertRaisesRegex(search.SearchError, "quality contract drift"):
            search.load_loop(loop_dir)

    def test_candidate_outcome_uses_baseline_improvement_and_tie_budget(self) -> None:
        contract = self.contract()
        state = {"champion": None, "alternates": []}
        self.assertEqual(
            search.candidate_outcome(contract, state, {"accepted": True, "quality_score": 72})[0],
            "BELOW_BASELINE",
        )
        self.assertEqual(
            search.candidate_outcome(contract, state, {"accepted": True, "quality_score": 73}),
            ("NEW_CHAMPION", True, False),
        )
        state["champion"] = {"quality_score": 80}
        self.assertEqual(
            search.candidate_outcome(contract, state, {"accepted": True, "quality_score": 80}),
            ("TIED_ALTERNATE", False, True),
        )
        state["alternates"] = [{"quality_score": 80}]
        self.assertEqual(
            search.candidate_outcome(contract, state, {"accepted": True, "quality_score": 80})[0],
            "NON_CHAMPION",
        )

    def test_supervisor_note_requires_completed_valid_handoff(self) -> None:
        valid = {
            "status": "COMPLETED",
            "handoff_valid": True,
            "handoff": {"summary": "Try the bounded alternate."},
        }
        self.assertIn("bounded alternate", search.supervisor_note_from_result(valid))
        self.assertIsNone(search.supervisor_note_from_result({**valid, "handoff_valid": False}))

    def test_run_preserves_two_isolated_candidates_and_selects_measured_champion(self) -> None:
        loop_dir, prepared = self.prepare()
        search.approve_loop(loop_dir, prepared["contract_hash"], "unit-test")
        frozen = search.l2.load_json(loop_dir / "contract.json", "contract")
        maker_runs = [
            "run-20260825T120000Z-aaaaaaaaaa",
            "run-20260825T120001Z-bbbbbbbbbb",
        ]
        gate_ids = [
            "qgate-20260825T120100Z-aaaaaaaaaa",
            "qgate-20260825T120101Z-bbbbbbbbbb",
        ]
        worktrees = [
            self.add_candidate_worktree(loop_dir, maker_runs[0], 2),
            self.add_candidate_worktree(loop_dir, maker_runs[1], 3),
        ]
        maker_results = []
        for index, ((output_dir, worktree), run_id, agent) in enumerate(
            zip(worktrees, maker_runs, ("codex", "qwen"), strict=True)
        ):
            maker_results.append(
                (
                    {
                        "status": "COMPLETED",
                        "handoff_valid": True,
                        "handoff": {"summary": f"candidate {index + 1}"},
                        "run_id": run_id,
                        "agent": agent,
                        "model": None,
                        "output_dir": str(output_dir),
                        "git_evidence": {"worktree": str(worktree)},
                        "token_usage": {"total_tokens": 100 + index},
                    },
                    "",
                    "",
                )
            )

        def quality_side_effect(**kwargs):
            run_id = kwargs["maker_run_id"]
            index = maker_runs.index(run_id)
            auditor_run = f"run-20260825T12020{index}Z-{str(index + 1) * 10}"
            score = 75 if index == 0 else 80
            gate = {
                "gate_id": gate_ids[index],
                "gate_kind": "quality",
                "contract_sha256": frozen["contract"]["quality"]["contract_sha256"],
                "group_id": frozen["contract"]["quality"]["group_id"],
                "parent_acceptance": None,
                "workers": [{"run_id": run_id}],
                "auditor": {
                    "run_id": auditor_run,
                    "agent": "antigravity",
                    "model": None,
                },
                "independence": {"passed": True},
                "result": {
                    "verdict": "ACCEPT",
                    "items": [
                        {
                            "run_id": run_id,
                            "verdict": "ACCEPT",
                            "quality_score": score,
                            "criteria_passed": ["bounded"],
                            "criteria_failed": [],
                            "findings": [],
                        }
                    ],
                    "integration": {"verdict": "ACCEPT"},
                    "unresolved_risks": [],
                },
                "telemetry": {
                    "runs": [{"run_id": auditor_run, "total_tokens": 20 + index}]
                },
            }
            return {"status": "CANDIDATE_READY", "gate": gate}, "", ""

        with (
            mock.patch.object(search.l2, "invoke_fleet", side_effect=maker_results) as makers,
            mock.patch.object(search, "invoke_quality_gate", side_effect=quality_side_effect) as gates,
        ):
            result = search.run_loop(loop_dir)

        self.assertEqual(result["phase"], "CHAMPION_READY")
        self.assertEqual(result["champion"]["maker_run_id"], maker_runs[1])
        self.assertEqual(result["champion"]["quality_score"], 80)
        self.assertEqual(makers.call_count, 2)
        self.assertEqual(gates.call_count, 2)
        self.assertTrue(
            all(call.kwargs["state_dir"] == self.fleet_state for call in makers.call_args_list)
        )
        self.assertTrue(
            all(call.kwargs["state_root"] == self.fleet_state for call in gates.call_args_list)
        )
        state = search.l2.load_json(loop_dir / "state.json", "state")
        self.assertEqual([item["outcome"] for item in state["candidates"]], [
            "NEW_CHAMPION",
            "NEW_CHAMPION",
        ])
        self.assertEqual(state["tokens_total"], 242)
        self.assertTrue(Path(state["manifest_path"]).is_file())

    def test_baseline_drift_stops_search_and_cannot_return_stale_champion_status(self) -> None:
        loop_dir, prepared = self.prepare()
        search.approve_loop(loop_dir, prepared["contract_hash"], "unit-test")

        def dirty_baseline(**_kwargs):
            (self.repo / "code.py").write_text("VALUE = 99\n", encoding="utf-8")
            raise search.l2.PilotError("simulated provider failure")

        try:
            with mock.patch.object(search.l2, "invoke_fleet", side_effect=dirty_baseline):
                with self.assertRaisesRegex(search.SearchError, "baseline drift"):
                    search.run_loop(loop_dir)
            state = search.l2.load_json(loop_dir / "state.json", "state")
            self.assertEqual(state["phase"], "STOPPED")
            self.assertIn("baseline drift", state["reason"])
        finally:
            run(["git", "restore", "code.py"], self.repo)

    def test_maker_result_must_match_frozen_agent_model_and_unique_run(self) -> None:
        actor = {"agent": "grok", "model": "grok-4.6"}
        valid = {
            "run_id": "run-20260825T120000Z-aaaaaaaaaa",
            "agent": "GROK",
            "model": "grok-4.6",
        }
        self.assertEqual(
            search.validate_maker_result(valid, actor, {"candidates": []}),
            valid["run_id"],
        )
        with self.assertRaisesRegex(search.SearchError, "model differs"):
            search.validate_maker_result({**valid, "model": "grok-4.5"}, actor, {"candidates": []})
        with self.assertRaisesRegex(search.SearchError, "duplicate"):
            search.validate_maker_result(
                valid,
                actor,
                {"candidates": [{"maker_run_id": valid["run_id"]}]},
            )

    @staticmethod
    def acceptance_fixture(root: Path) -> tuple[dict[str, object], dict[str, object]]:
        fleet_state = root / "acceptance-fleet-state"
        fleet_state.mkdir(exist_ok=True)
        frozen = {
            "fleet_runtime": {"fleet_script": str(root / "fleet.py")},
            "fleet_state_root": str(fleet_state),
            "contract": {"project": str(root)},
        }
        state = {
            "phase": "CHAMPION_READY",
            "champion": {"gate_id": "qgate-champion"},
            "candidates": [
                {"gate_id": "qgate-champion", "outcome": "NEW_CHAMPION"},
                {"gate_id": "qgate-below", "outcome": "BELOW_BASELINE"},
            ],
        }
        return frozen, state

    def test_accept_rejects_unclosed_non_champion_gate_including_below_baseline(self) -> None:
        frozen, state = self.acceptance_fixture(self.root)
        statuses = {"qgate-champion": "PARENT_ACCEPTED", "qgate-below": "CANDIDATE_READY"}
        with (
            mock.patch.object(search, "load_loop", return_value=(self.root, frozen, state)),
            mock.patch.object(
                search,
                "quality_gate_status",
                side_effect=lambda _script, _state, gate, _project: {"status": statuses[gate]},
            ),
        ):
            with self.assertRaisesRegex(search.SearchError, "qgate-below"):
                search.accept_loop(self.root, "qgate-champion", "parent", "checked")

    def test_accept_allows_rejected_or_failed_non_champion_gate(self) -> None:
        for closed_status in ("PARENT_REJECTED", "NEEDS_REPAIR", "BLOCKED"):
            with self.subTest(closed_status=closed_status):
                frozen, state = self.acceptance_fixture(self.root)
                statuses = {
                    "qgate-champion": "PARENT_ACCEPTED",
                    "qgate-below": closed_status,
                }
                with (
                    mock.patch.object(search, "load_loop", return_value=(self.root, frozen, state)),
                    mock.patch.object(
                        search,
                        "quality_gate_status",
                        side_effect=lambda _script, _state, gate, _project: {"status": statuses[gate]},
                    ),
                    mock.patch.object(search.l2, "loop_lock", return_value=contextlib.nullcontext()),
                    mock.patch.object(search, "save_state"),
                    mock.patch.object(search.l2, "append_event"),
                ):
                    result = search.accept_loop(
                        self.root, "qgate-champion", "parent", "evidence checked"
                    )
                self.assertEqual(result["phase"], "ACCEPTED")


if __name__ == "__main__":
    unittest.main()
