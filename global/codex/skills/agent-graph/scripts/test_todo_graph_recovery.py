"""Tests for inspected interruption recovery in todo_graph.

Synthetic fixture directories are retained under this test's artifact root.
Real-user run IDs are never used.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
from pathlib import Path
import sys
import threading
import unittest
from unittest.mock import patch

sys.dont_write_bytecode = True
os.environ["AGENT_GRAPH_WATCH_EVENTS"] = "0"
sys.path.insert(0, str(Path(__file__).resolve().parent))

MODULE_PATH = Path(__file__).with_name("todo_graph.py")
SPEC = importlib.util.spec_from_file_location("todo_graph_under_test", MODULE_PATH)
todo_graph = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = todo_graph
SPEC.loader.exec_module(todo_graph)

# Synthetic recovery runs must not publish TODO journals into the real user
# home. Scoped to this module; the caller's explicit telemetry setting is
# restored on teardown. Nothing is changed at import time.
_test_telemetry = patch.dict(os.environ, {"AGENT_TODO_TELEMETRY": "off"})


def setUpModule():
    _test_telemetry.start()


def tearDownModule():
    _test_telemetry.stop()

FIXTURES_ROOT = Path(__file__).resolve().parent / '.test-artifacts' / 'recovery'


class FakeAdapter:
    def __init__(self, behavior=None):
        self.behavior = behavior or self.default_behavior
        self.calls: list = []
        self.lock = threading.Lock()

    @staticmethod
    def default_behavior(plan, task, role, workspace, folder, feedback, maker=None):
        if role == "maker":
            return {"status": "CANDIDATE", "lineage": "maker-lineage"}
        return {
            "status": "REVIEWED",
            "verdict": "ACCEPT",
            "checked": list(task["done_when"]),
            "findings": [],
            "lineage": "reviewer-lineage",
        }

    def invoke(self, plan, task, role, workspace, folder, feedback, maker=None):
        with self.lock:
            self.calls.append((task["id"], role, feedback, maker))
        return self.behavior(plan, task, role, workspace, folder, feedback, maker)


class RecoveryTests(unittest.TestCase):
    fixtures_root: Path = FIXTURES_ROOT

    def setUp(self):
        # Unit fixtures are not Git repositories. Supply an independent live
        # HEAD result rather than deriving expected HEAD from the usage record.
        base_patch = patch.object(todo_graph, "_workspace_base", return_value="0" * 40)
        base_patch.start()
        self.addCleanup(base_patch.stop)
        tag = f"{self._testMethodName}-pid{os.getpid()}-id{id(self)}"
        root = getattr(self, "fixtures_root", FIXTURES_ROOT)
        self.fixture_dir = root / tag
        self.fixture_dir.mkdir(parents=True, exist_ok=True)
        self.workspace = self.fixture_dir / "workspace"
        self.workspace.mkdir(parents=True, exist_ok=True)
        self.run_dir = self.fixture_dir / "run"
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.ledger_dir = self.fixture_dir / "ledger"
        self.ledger_dir.mkdir(parents=True, exist_ok=True)
        (self.workspace / "a.txt").write_text("initial content", encoding="utf-8")

    # ------------------------------------------------------------------ plan

    def make_plan(self, **overrides):
        plan = {
            "version": 1,
            "goal": "recovery test",
            "planner": {"model": "test-model", "source": "test"},
            "requirements": ["REQ-1"],
            "max_parallel": 1,
            "workspace": str(self.workspace),
            "execution_context": "legacy-test",
            "tasks": [
                {
                    "id": "task-a",
                    "text": "do task a",
                    "covers": ["REQ-1"],
                    "done_when": ["evidence for task-a"],
                    "scope": ["a.txt"],
                    "inputs": [],
                    "depends_on": [],
                    "maker": [{"agent": "maker", "model": "m-model", "reason": "fit", "score": 80}],
                    "reviewer": [{"agent": "reviewer", "model": "r-model", "reason": "fit", "score": 80}],
                    "checks": [[sys.executable, "-c", "pass"]],
                    "requires_approval": [],
                }
            ],
        }
        plan.update(overrides)
        return plan

    def make_running_state(self, plan):
        state = {
            "plan_hash": todo_graph.digest(plan),
            "nodes": {"task-a": {"status": "RUNNING", "round": 1, "history": []}},
            "version": todo_graph.VERSION,
        }
        todo_graph.write(self.run_dir / "state.json", state)
        return state

    def make_progress(
        self,
        with_receipt=True,
        with_usage=True,
        usage_overrides=None,
        **receipt_overrides,
    ):
        """Create progress and authoritative on-disk artifacts (Suite055 protocol)."""
        task_folder = self.run_dir / "task-a"
        task_folder.mkdir(parents=True, exist_ok=True)
        history = []
        skip_disk = receipt_overrides.pop("_skip_disk_result", False)
        receipt = None
        if with_receipt:
            fleet_run_id = receipt_overrides.pop("fleet_run_id", "fleet-run-test-001")
            maker_folder = task_folder / "1" / "maker"
            maker_folder.mkdir(parents=True, exist_ok=True)
            usage_file = self.ledger_dir / f"usage-{id(self)}.json"
            if with_usage:
                usage_data = {
                    "run_id": fleet_run_id,
                    "agent": "maker",
                    "model": "m-model",
                    "model_provider_id": "maker-lineage",
                    "runtime_attestation": {
                        "status": "VERIFIED", "package_version": "0.9.55",
                        "package_fingerprint": "1" * 64,
                        "payload_fingerprint": "2" * 64,
                        "payload_file_count": 185,
                        "runtime_root": str(self.fixture_dir / "synthetic-pinned-runtime"),
                    },
                    "worktree_base_commit_sha": "0" * 40,
                    "worktree_base_provenance": "preserved",
                    "exit_code": 0,
                    "provider_terminal_reason": "completed",
                    "execution_workspace": str(self.workspace),
                }
                if usage_overrides:
                    usage_data.update(usage_overrides)
                todo_graph.write(usage_file, usage_data)
            receipt = {
                "status": "CANDIDATE",
                "lineage": "maker-lineage",
                "model": "m-model",
                "run_id": fleet_run_id,
                "usage_record": str(usage_file) if with_usage else str(self.ledger_dir / "missing-usage.json"),
            }
            receipt.update(receipt_overrides)
            history.append({"role": "maker", "result": receipt})
        progress = {"status": "RUNNING", "round": 1, "history": history}
        todo_graph.write(task_folder / "progress.json", progress)
        if with_receipt and receipt and not skip_disk:
            maker_folder = task_folder / "1" / "maker"
            maker_folder.mkdir(parents=True, exist_ok=True)
            todo_graph.write(maker_folder / "result.json", receipt)
        return progress

    def make_process_evidence(self, pid=99999, started=1_234_567_890.0):
        task_folder = self.run_dir / "task-a"
        maker_folder = task_folder / "1" / "maker"
        maker_folder.mkdir(parents=True, exist_ok=True)
        proc = {"pid": pid, "started": started}
        todo_graph.write(maker_folder / "process.json", proc)
        return proc

    def make_native_checkpoint(self, pid=99999, started_at=1_234_567_890.0, filename="attempt-1.process-checkpoint.json"):
        task_folder = self.run_dir / "task-a"
        maker_folder = task_folder / "1" / "maker"
        maker_folder.mkdir(parents=True, exist_ok=True)
        proc = {"pid": pid, "started_at": started_at}
        todo_graph.write(maker_folder / filename, proc)
        return proc

    def full_fixture(self, with_receipt=True, with_usage=True, usage_overrides=None, **receipt_overrides):
        """Create plan + RUNNING state + progress + process evidence in one call."""
        plan = self.make_plan()
        self.make_running_state(plan)
        self.make_progress(with_receipt=with_receipt, with_usage=with_usage, usage_overrides=usage_overrides, **receipt_overrides)
        self.make_process_evidence()
        return plan

    def _inspect(self, plan):
        return todo_graph.inspect_run(self.run_dir, plan)

    # ------------------------------------------------------- running guard

    def test_running_guard_blocks_blind_replay(self):
        plan = self.full_fixture()
        with self.assertRaisesRegex(ValueError, "needs inspection"):
            todo_graph.run(plan, self.run_dir, adapter=FakeAdapter())

    # ---------------------------------------------------------------- inspect

    def test_inspect_yields_exact_hashes_and_process_evidence(self):
        plan = self.full_fixture()
        record = todo_graph.inspect_run(self.run_dir, plan)

        self.assertEqual(record["schema_version"], 1)
        self.assertEqual(record["plan_hash"], todo_graph.digest(plan))
        self.assertEqual(record["run_id"], self.run_dir.name)
        self.assertIn("inspected_at", record)
        self.assertEqual(record["workspace"], str(self.workspace.resolve()))

        node = record["nodes"]["task-a"]
        self.assertEqual(node["status"], "RUNNING")
        self.assertEqual(node["task_id"], "task-a")
        self.assertEqual(node["scope"], ["a.txt"])
        self.assertEqual(node["task_hash"], todo_graph.digest(plan["tasks"][0]))
        self.assertEqual(node["checks_hash"], todo_graph.digest(plan["tasks"][0]["checks"]))
        self.assertIn("scope_hash", node)
        self.assertIn("artifact_hashes", node)
        self.assertIn("a.txt", node["artifact_hashes"])
        self.assertEqual(node["model"], "m-model")
        self.assertEqual(node["model_hash"], todo_graph.digest("m-model"))
        self.assertEqual(node["lineage"], "maker-lineage")
        self.assertIn("runtime_hash", node)
        self.assertIn("base_hash", node)
        self.assertIn("checkout_base", node)
        self.assertIn("process_evidence", node)
        self.assertIn("lock_evidence", node)
        self.assertIsNotNone(node["maker_receipt"])
        self.assertEqual(node["maker_receipt"]["status"], "CANDIDATE")

        self.assertIn("inspection_hash", record)
        self.assertEqual(record["inspection_hash"], todo_graph._inspection_hash(record))

    def test_inspect_without_maker_receipt_records_none(self):
        plan = self.full_fixture(with_receipt=False)
        record = todo_graph.inspect_run(self.run_dir, plan)
        self.assertIsNone(record["nodes"]["task-a"]["maker_receipt"])

    # --------------------------------------------------- resolve fail-closed

    def test_resolve_fails_closed_on_alive_process(self):
        plan = self.full_fixture()
        with patch.object(todo_graph, "_process_alive", return_value=True), patch.object(
            todo_graph, "_pid_reused", return_value=False
        ):
            record = self._inspect(plan)
            result = todo_graph.resolve_interrupted(self.run_dir, plan, record, adapter=FakeAdapter())
        self.assertEqual(result["status"], "BLOCKED")
        self.assertIn("alive", result["nodes"]["task-a"]["reason"].lower())

    def test_resolve_fails_closed_on_unknown_process(self):
        plan = self.full_fixture()
        with patch.object(todo_graph, "_process_alive", return_value=None):
            record = self._inspect(plan)
            result = todo_graph.resolve_interrupted(self.run_dir, plan, record, adapter=FakeAdapter())
        self.assertEqual(result["status"], "BLOCKED")
        self.assertIn("unknown", result["nodes"]["task-a"]["reason"].lower())

    def test_resolve_fails_closed_on_pid_reuse(self):
        plan = self.full_fixture()
        record = self._inspect(plan)
        record["nodes"]["task-a"]["process_evidence"]["alive"] = False
        record["nodes"]["task-a"]["process_evidence"]["pid_reused"] = True
        record["inspection_hash"] = todo_graph._inspection_hash(record)
        with patch.object(todo_graph, "_node_process_evidence") as mock_proc:
            mock_proc.return_value = {
                "pid": 99999,
                "started": 1_234_567_890.0,
                "alive": False,
                "pid_reused": True,
                "source": "synthetic",
            }
            result = todo_graph.resolve_interrupted(self.run_dir, plan, record, adapter=FakeAdapter())
        self.assertEqual(result["status"], "BLOCKED")
        self.assertIn("reus", result["nodes"]["task-a"]["reason"].lower())

    def test_resolve_fails_closed_on_unknown_pid_reuse(self):
        plan = self.full_fixture()
        record = self._inspect(plan)
        record["inspection_hash"] = todo_graph._inspection_hash(record)
        with patch.object(todo_graph, "_node_process_evidence") as mock_proc:
            mock_proc.return_value = {
                "pid": 99999,
                "started": 1_234_567_890.0,
                "alive": False,
                "pid_reused": None,
                "source": "synthetic",
            }
            result = todo_graph.resolve_interrupted(self.run_dir, plan, record, adapter=FakeAdapter())
        self.assertEqual(result["status"], "BLOCKED")
        self.assertIn("unknown", result["nodes"]["task-a"]["reason"].lower())

    def test_resolve_fails_closed_on_changed_plan_hash(self):
        plan = self.full_fixture()
        record = self._inspect(plan)
        changed_plan = self.make_plan(goal="different goal")
        result = todo_graph.resolve_interrupted(
            self.run_dir, changed_plan, record, adapter=FakeAdapter()
        )
        self.assertEqual(result["status"], "BLOCKED")
        self.assertIn("plan", result["reason"].lower())

    def test_resolve_fails_closed_on_tampered_inspection_hash(self):
        plan = self.full_fixture()
        record = self._inspect(plan)
        record["plan_hash"] = "0" * 64
        result = todo_graph.resolve_interrupted(self.run_dir, plan, record, adapter=FakeAdapter())
        self.assertEqual(result["status"], "BLOCKED")
        self.assertIn("hash", result["reason"].lower())

    def test_resolve_fails_closed_on_changed_artifact(self):
        plan = self.full_fixture()
        record = self._inspect(plan)
        (self.workspace / "a.txt").write_text("mutated after inspection", encoding="utf-8")
        with patch.object(todo_graph, "_process_alive", return_value=False), patch.object(
            todo_graph, "_pid_reused", return_value=False
        ):
            result = todo_graph.resolve_interrupted(self.run_dir, plan, record, adapter=FakeAdapter())
        self.assertEqual(result["status"], "BLOCKED")
        reason = result["nodes"]["task-a"]["reason"].lower()
        self.assertTrue("artifact" in reason or "scope" in reason)

    def test_resolve_fails_closed_on_missing_maker_receipt(self):
        plan = self.full_fixture(with_receipt=False)
        record = self._inspect(plan)
        with patch.object(todo_graph, "_process_alive", return_value=False), patch.object(
            todo_graph, "_pid_reused", return_value=False
        ):
            result = todo_graph.resolve_interrupted(self.run_dir, plan, record, adapter=FakeAdapter())
        self.assertEqual(result["status"], "BLOCKED")
        self.assertIn("maker receipt", result["nodes"]["task-a"]["reason"].lower())
        state = todo_graph.read(self.run_dir / "state.json")
        self.assertEqual(state["nodes"]["task-a"]["status"], "RUNNING")

    def test_resolve_fails_closed_on_changed_model(self):
        plan = self.full_fixture()
        record = self._inspect(plan)
        task_folder = self.run_dir / "task-a"
        maker_folder = task_folder / "1" / "maker"
        result_json = todo_graph.read(maker_folder / "result.json")
        result_json["model"] = "other-model"
        todo_graph.write(maker_folder / "result.json", result_json)
        with patch.object(todo_graph, "_process_alive", return_value=False), patch.object(
            todo_graph, "_pid_reused", return_value=False
        ):
            result = todo_graph.resolve_interrupted(self.run_dir, plan, record, adapter=FakeAdapter())
        self.assertEqual(result["status"], "BLOCKED")
        self.assertIn("model", result["nodes"]["task-a"]["reason"].lower())
        state = todo_graph.read(self.run_dir / "state.json")
        self.assertEqual(state["nodes"]["task-a"]["status"], "RUNNING")

    def test_resolve_fails_closed_on_changed_runtime(self):
        plan = self.full_fixture()
        record = self._inspect(plan)
        usage_file = Path(record["nodes"]["task-a"]["maker_receipt"]["usage_record"])
        usage_data = todo_graph.read(usage_file)
        usage_data["runtime_attestation"] = {"status": "UNVERIFIED"}
        todo_graph.write(usage_file, usage_data)
        with patch.object(todo_graph, "_process_alive", return_value=False), patch.object(
            todo_graph, "_pid_reused", return_value=False
        ):
            result = todo_graph.resolve_interrupted(self.run_dir, plan, record, adapter=FakeAdapter())
        self.assertEqual(result["status"], "BLOCKED")
        self.assertIn("runtime", result["nodes"]["task-a"]["reason"].lower())
        state = todo_graph.read(self.run_dir / "state.json")
        self.assertEqual(state["nodes"]["task-a"]["status"], "RUNNING")

    def test_fail_closed_preserves_running_state_and_artifacts(self):
        plan = self.full_fixture(with_receipt=False)
        record = self._inspect(plan)
        before_artifact = (self.workspace / "a.txt").read_bytes()
        with patch.object(todo_graph, "_process_alive", return_value=True), patch.object(
            todo_graph, "_pid_reused", return_value=False
        ):
            result = todo_graph.resolve_interrupted(self.run_dir, plan, record, adapter=FakeAdapter())
        self.assertEqual(result["status"], "BLOCKED")
        self.assertTrue(result["nodes"]["task-a"].get("state_preserved"))
        state = todo_graph.read(self.run_dir / "state.json")
        self.assertEqual(state["nodes"]["task-a"]["status"], "RUNNING")
        progress = todo_graph.read(self.run_dir / "task-a" / "progress.json")
        self.assertEqual(progress["status"], "RUNNING")
        self.assertEqual((self.workspace / "a.txt").read_bytes(), before_artifact)

    def test_resolve_fails_closed_on_untrusted_maker_status(self):
        plan = self.full_fixture(status="BLOCKED")
        record = self._inspect(plan)
        with patch.object(todo_graph, "_process_alive", return_value=False), patch.object(
            todo_graph, "_pid_reused", return_value=False
        ):
            result = todo_graph.resolve_interrupted(self.run_dir, plan, record, adapter=FakeAdapter())
        self.assertEqual(result["status"], "BLOCKED")
        self.assertIn("maker status", result["nodes"]["task-a"]["reason"].lower())

    def test_resolve_fails_closed_when_node_not_running(self):
        plan = self.full_fixture()
        record = self._inspect(plan)
        state = todo_graph.read(self.run_dir / "state.json")
        state["nodes"]["task-a"]["status"] = "ACCEPTED"
        todo_graph.write(self.run_dir / "state.json", state)
        with patch.object(todo_graph, "_process_alive", return_value=False), patch.object(
            todo_graph, "_pid_reused", return_value=False
        ):
            result = todo_graph.resolve_interrupted(self.run_dir, plan, record, adapter=FakeAdapter())
        self.assertEqual(result["status"], "BLOCKED")
        self.assertIn("no longer running", result["nodes"]["task-a"]["reason"].lower())

    # ------------------------------------------------------ safe recovery

    def test_safe_accepted_recovery_through_tests_and_reviewer(self):
        plan = self.full_fixture()
        record = self._inspect(plan)
        adapter = FakeAdapter()
        with patch.object(todo_graph, "_process_alive", return_value=False), patch.object(
            todo_graph, "_pid_reused", return_value=False
        ):
            result = todo_graph.resolve_interrupted(self.run_dir, plan, record, adapter=adapter)

        self.assertEqual(result["status"], "ACCEPTED_CANDIDATE")
        self.assertEqual(result["recovered"], ["task-a"])
        self.assertEqual(result["blocked"], [])
        self.assertEqual(result["nodes"]["task-a"]["status"], "ACCEPTED")

        roles = [call[1] for call in adapter.calls]
        self.assertIn("reviewer", roles)
        self.assertNotIn("maker", roles)  # retained maker is not re-invoked

        state = todo_graph.read(self.run_dir / "state.json")
        self.assertEqual(state["nodes"]["task-a"]["status"], "ACCEPTED")

    def test_recovery_preserves_original_running_history(self):
        plan = self.full_fixture()
        record = self._inspect(plan)
        with patch.object(todo_graph, "_process_alive", return_value=False), patch.object(
            todo_graph, "_pid_reused", return_value=False
        ):
            todo_graph.resolve_interrupted(self.run_dir, plan, record, adapter=FakeAdapter())
        progress = todo_graph.read(self.run_dir / "task-a" / "progress.json")
        recovery_events = [h for h in progress["history"] if h.get("role") == "recovery"]
        self.assertTrue(recovery_events)
        self.assertEqual(recovery_events[0]["original_status"], "RUNNING")
        maker_events = [h for h in progress["history"] if h.get("role") == "maker"]
        self.assertTrue(maker_events)

    def test_recovery_does_not_fabricate_candidate_or_accept(self):
        plan = self.full_fixture(with_receipt=False)
        record = self._inspect(plan)
        with patch.object(todo_graph, "_process_alive", return_value=False), patch.object(
            todo_graph, "_pid_reused", return_value=False
        ):
            result = todo_graph.resolve_interrupted(self.run_dir, plan, record, adapter=FakeAdapter())
        self.assertEqual(result["status"], "BLOCKED")
        for node_result in result["nodes"].values():
            self.assertNotEqual(node_result.get("status"), "ACCEPTED")
            self.assertNotEqual(node_result.get("status"), "CANDIDATE")

    def test_running_guard_still_blocks_after_fail_closed(self):
        plan = self.full_fixture(with_receipt=False)
        record = self._inspect(plan)
        with patch.object(todo_graph, "_process_alive", return_value=False), patch.object(
            todo_graph, "_pid_reused", return_value=False
        ):
            todo_graph.resolve_interrupted(self.run_dir, plan, record, adapter=FakeAdapter())
        with self.assertRaisesRegex(ValueError, "needs inspection"):
            todo_graph.run(plan, self.run_dir, adapter=FakeAdapter())

    # ---------------------------------------------- adversarial recovery tests

    def test_resolve_fails_closed_on_omitted_model(self):
        plan = self.full_fixture(model="", usage_overrides={"model": ""})
        record = self._inspect(plan)
        with patch.object(todo_graph, "_process_alive", return_value=False), patch.object(
            todo_graph, "_pid_reused", return_value=False
        ):
            result = todo_graph.resolve_interrupted(self.run_dir, plan, record, adapter=FakeAdapter())
        self.assertEqual(result["status"], "BLOCKED")
        self.assertIn("model", result["nodes"]["task-a"]["reason"].lower())
        state = todo_graph.read(self.run_dir / "state.json")
        self.assertEqual(state["nodes"]["task-a"]["status"], "RUNNING")

    def test_resolve_fails_closed_on_omitted_runtime(self):
        plan = self.full_fixture(usage_overrides={"runtime_attestation": None})
        record = self._inspect(plan)
        with patch.object(todo_graph, "_process_alive", return_value=False), patch.object(
            todo_graph, "_pid_reused", return_value=False
        ):
            result = todo_graph.resolve_interrupted(self.run_dir, plan, record, adapter=FakeAdapter())
        self.assertEqual(result["status"], "BLOCKED")
        self.assertIn("runtime", result["nodes"]["task-a"]["reason"].lower())
        state = todo_graph.read(self.run_dir / "state.json")
        self.assertEqual(state["nodes"]["task-a"]["status"], "RUNNING")

    def test_resolve_fails_closed_on_omitted_base(self):
        plan = self.full_fixture(usage_overrides={"worktree_base_commit_sha": ""})
        record = self._inspect(plan)
        with patch.object(todo_graph, "_process_alive", return_value=False), patch.object(
            todo_graph, "_pid_reused", return_value=False
        ):
            result = todo_graph.resolve_interrupted(self.run_dir, plan, record, adapter=FakeAdapter())
        self.assertEqual(result["status"], "BLOCKED")
        self.assertIn("base", result["nodes"]["task-a"]["reason"].lower())
        state = todo_graph.read(self.run_dir / "state.json")
        self.assertEqual(state["nodes"]["task-a"]["status"], "RUNNING")

    def test_resolve_fails_closed_on_omitted_lineage(self):
        plan = self.full_fixture(lineage="", usage_overrides={"model_provider_id": ""})
        record = self._inspect(plan)
        with patch.object(todo_graph, "_process_alive", return_value=False), patch.object(
            todo_graph, "_pid_reused", return_value=False
        ):
            result = todo_graph.resolve_interrupted(self.run_dir, plan, record, adapter=FakeAdapter())
        self.assertEqual(result["status"], "BLOCKED")
        self.assertIn("lineage", result["nodes"]["task-a"]["reason"].lower())
        state = todo_graph.read(self.run_dir / "state.json")
        self.assertEqual(state["nodes"]["task-a"]["status"], "RUNNING")

    def test_resolve_fails_closed_on_omitted_run_id(self):
        plan = self.full_fixture(run_id="", fleet_run_id="", usage_overrides={"run_id": ""})
        record = self._inspect(plan)
        with patch.object(todo_graph, "_process_alive", return_value=False), patch.object(
            todo_graph, "_pid_reused", return_value=False
        ):
            result = todo_graph.resolve_interrupted(self.run_dir, plan, record, adapter=FakeAdapter())
        self.assertEqual(result["status"], "BLOCKED")
        self.assertIn("run_id", result["nodes"]["task-a"]["reason"].lower())
        state = todo_graph.read(self.run_dir / "state.json")
        self.assertEqual(state["nodes"]["task-a"]["status"], "RUNNING")

    def test_resolve_fails_closed_on_forged_progress_only_receipt(self):
        plan = self.full_fixture(_skip_disk_result=True)
        record = self._inspect(plan)
        with patch.object(todo_graph, "_process_alive", return_value=False), patch.object(
            todo_graph, "_pid_reused", return_value=False
        ):
            result = todo_graph.resolve_interrupted(self.run_dir, plan, record, adapter=FakeAdapter())
        self.assertEqual(result["status"], "BLOCKED")
        reason = result["nodes"]["task-a"]["reason"].lower()
        self.assertTrue("forged" in reason or "authoritative" in reason or "result.json" in reason)
        state = todo_graph.read(self.run_dir / "state.json")
        self.assertEqual(state["nodes"]["task-a"]["status"], "RUNNING")

    def test_resolve_fails_closed_on_alive_reviewer_child(self):
        plan = self.full_fixture()
        rev_folder = self.run_dir / "task-a" / "1" / "reviewer"
        rev_folder.mkdir(parents=True, exist_ok=True)
        todo_graph.write(rev_folder / "process.json", {"pid": 88888, "started": 1_234_567_890.0})
        record = self._inspect(plan)

        def mock_alive(pid, **_sample):
            return pid == 88888

        with patch.object(todo_graph, "_process_alive", side_effect=mock_alive), patch.object(
            todo_graph, "_pid_reused", return_value=False
        ):
            result = todo_graph.resolve_interrupted(self.run_dir, plan, record, adapter=FakeAdapter())
        self.assertEqual(result["status"], "BLOCKED")
        self.assertIn("alive", result["nodes"]["task-a"]["reason"].lower())
        state = todo_graph.read(self.run_dir / "state.json")
        self.assertEqual(state["nodes"]["task-a"]["status"], "RUNNING")

    def test_resolve_fails_closed_on_alive_test_child(self):
        plan = self.full_fixture()
        check_folder = self.run_dir / "task-a" / "1" / "check-0"
        check_folder.mkdir(parents=True, exist_ok=True)
        todo_graph.write(check_folder / "process.json", {"pid": 77777, "started": 1_234_567_890.0})
        record = self._inspect(plan)

        def mock_alive(pid, **_sample):
            return pid == 77777

        with patch.object(todo_graph, "_process_alive", side_effect=mock_alive), patch.object(
            todo_graph, "_pid_reused", return_value=False
        ):
            result = todo_graph.resolve_interrupted(self.run_dir, plan, record, adapter=FakeAdapter())
        self.assertEqual(result["status"], "BLOCKED")
        self.assertIn("alive", result["nodes"]["task-a"]["reason"].lower())
        state = todo_graph.read(self.run_dir / "state.json")
        self.assertEqual(state["nodes"]["task-a"]["status"], "RUNNING")

    def test_resolve_fails_closed_on_native_checkpoint_orphan_alive(self):
        plan = self.full_fixture()
        self.make_native_checkpoint(pid=66666, started_at=1_234_567_890.0)
        record = self._inspect(plan)

        def mock_alive(pid, **_sample):
            return pid == 66666

        with patch.object(todo_graph, "_process_alive", side_effect=mock_alive), patch.object(
            todo_graph, "_pid_reused", return_value=False
        ):
            result = todo_graph.resolve_interrupted(self.run_dir, plan, record, adapter=FakeAdapter())
        self.assertEqual(result["status"], "BLOCKED")
        self.assertIn("alive", result["nodes"]["task-a"]["reason"].lower())
        state = todo_graph.read(self.run_dir / "state.json")
        self.assertEqual(state["nodes"]["task-a"]["status"], "RUNNING")

    def test_resolve_fails_closed_on_native_checkpoint_unknown(self):
        plan = self.full_fixture()
        self.make_native_checkpoint(pid=0, started_at=1_234_567_890.0)
        record = self._inspect(plan)
        with patch.object(todo_graph, "_process_alive", return_value=False), patch.object(
            todo_graph, "_pid_reused", return_value=False
        ):
            result = todo_graph.resolve_interrupted(self.run_dir, plan, record, adapter=FakeAdapter())
        self.assertEqual(result["status"], "BLOCKED")
        self.assertIn("unknown", result["nodes"]["task-a"]["reason"].lower())
        state = todo_graph.read(self.run_dir / "state.json")
        self.assertEqual(state["nodes"]["task-a"]["status"], "RUNNING")

    def test_resolve_fails_closed_on_unknown_start_provenance(self):
        plan = self.make_plan()
        self.make_running_state(plan)
        self.make_progress(with_receipt=True)
        self.make_process_evidence(pid=99999, started=None)
        record = self._inspect(plan)
        with patch.object(todo_graph, "_process_alive", return_value=False):
            result = todo_graph.resolve_interrupted(self.run_dir, plan, record, adapter=FakeAdapter())
        self.assertEqual(result["status"], "BLOCKED")
        self.assertIn("unknown", result["nodes"]["task-a"]["reason"].lower())
        state = todo_graph.read(self.run_dir / "state.json")
        self.assertEqual(state["nodes"]["task-a"]["status"], "RUNNING")

    def test_resolve_fails_closed_on_concurrent_recovery_race(self):
        plan = self.full_fixture()
        record = self._inspect(plan)
        lock_file = self.run_dir / "runner.lock"
        lock_file.write_text(str(os.getpid()), encoding="utf-8")
        with patch.object(todo_graph, "_process_alive", return_value=True):
            result = todo_graph.resolve_interrupted(self.run_dir, plan, record, adapter=FakeAdapter())
        self.assertEqual(result["status"], "BLOCKED")
        self.assertIn("lock", result["reason"].lower())
        state = todo_graph.read(self.run_dir / "state.json")
        self.assertEqual(state["nodes"]["task-a"]["status"], "RUNNING")

    def test_resolve_fails_closed_on_two_concurrent_resolves(self):
        plan = self.full_fixture()
        record = self._inspect(plan)
        lock_file = self.run_dir / "runner.lock"
        lock_file.write_text(str(os.getpid()), encoding="utf-8")
        with patch.object(todo_graph, "_process_alive", return_value=False), patch.object(
            todo_graph, "_pid_reused", return_value=False
        ):
            # When runner.lock is pre-created by another claimant, exclusive() raises FileExistsError
            result = todo_graph.resolve_interrupted(self.run_dir, plan, record, adapter=FakeAdapter())
        self.assertEqual(result["status"], "BLOCKED")
        self.assertIn("lock", result["reason"].lower())
        state = todo_graph.read(self.run_dir / "state.json")
        self.assertEqual(state["nodes"]["task-a"]["status"], "RUNNING")

    def test_resolve_fails_closed_on_recomputed_tampered_inspection(self):
        plan = self.full_fixture()
        record = self._inspect(plan)
        record["plan_hash"] = "0" * 64
        record["inspection_hash"] = todo_graph._inspection_hash(record)
        with patch.object(todo_graph, "_process_alive", return_value=False), patch.object(
            todo_graph, "_pid_reused", return_value=False
        ):
            result = todo_graph.resolve_interrupted(self.run_dir, plan, record, adapter=FakeAdapter())
        self.assertEqual(result["status"], "BLOCKED")
        self.assertIn("plan", result["reason"].lower())
        state = todo_graph.read(self.run_dir / "state.json")
        self.assertEqual(state["nodes"]["task-a"]["status"], "RUNNING")

    def test_resolve_fails_closed_when_evidence_inside_workspace(self):
        plan = self.full_fixture()
        inside_run = self.workspace / "nested_run"
        inside_run.mkdir(parents=True, exist_ok=True)
        record = self._inspect(plan)
        with self.assertRaisesRegex(ValueError, "outside the execution workspace"):
            todo_graph.resolve_interrupted(inside_run, plan, record, adapter=FakeAdapter())

    def test_resolve_validates_plan(self):
        plan = self.full_fixture()
        record = self._inspect(plan)
        bad_plan = dict(plan)
        bad_plan["version"] = 99
        with self.assertRaisesRegex(ValueError, "Expected TODO version 1"):
            todo_graph.resolve_interrupted(self.run_dir, bad_plan, record, adapter=FakeAdapter())

    # ------------------------------------------------ mandatory usage tests

    def test_resolve_fails_closed_on_mandatory_missing_usage(self):
        plan = self.full_fixture(with_usage=False)
        record = self._inspect(plan)
        with patch.object(todo_graph, "_process_alive", return_value=False), patch.object(
            todo_graph, "_pid_reused", return_value=False
        ):
            result = todo_graph.resolve_interrupted(self.run_dir, plan, record, adapter=FakeAdapter())
        self.assertEqual(result["status"], "BLOCKED")
        self.assertIn("usage", result["nodes"]["task-a"]["reason"].lower())

    def test_resolve_fails_closed_on_incomplete_terminal_reason(self):
        plan = self.full_fixture(usage_overrides={"provider_terminal_reason": ""})
        record = self._inspect(plan)
        with patch.object(todo_graph, "_process_alive", return_value=False), patch.object(
            todo_graph, "_pid_reused", return_value=False
        ):
            result = todo_graph.resolve_interrupted(self.run_dir, plan, record, adapter=FakeAdapter())
        self.assertEqual(result["status"], "BLOCKED")
        self.assertIn("terminal", result["nodes"]["task-a"]["reason"].lower())

    def test_resolve_fails_closed_on_failed_terminal_reason(self):
        plan = self.full_fixture(usage_overrides={"provider_terminal_reason": "error"})
        record = self._inspect(plan)
        with patch.object(todo_graph, "_process_alive", return_value=False), patch.object(
            todo_graph, "_pid_reused", return_value=False
        ):
            result = todo_graph.resolve_interrupted(self.run_dir, plan, record, adapter=FakeAdapter())
        self.assertEqual(result["status"], "BLOCKED")
        self.assertIn("terminal", result["nodes"]["task-a"]["reason"].lower())

    def test_resolve_fails_closed_on_nonzero_exit_code(self):
        plan = self.full_fixture(usage_overrides={"exit_code": 1})
        record = self._inspect(plan)
        with patch.object(todo_graph, "_process_alive", return_value=False), patch.object(
            todo_graph, "_pid_reused", return_value=False
        ):
            result = todo_graph.resolve_interrupted(self.run_dir, plan, record, adapter=FakeAdapter())
        self.assertEqual(result["status"], "BLOCKED")
        self.assertIn("exit", result["nodes"]["task-a"]["reason"].lower())

    def test_resolve_fails_closed_on_wrong_execution_workspace(self):
        plan = self.full_fixture(usage_overrides={"execution_workspace": str(self.fixture_dir / "other_ws")})
        record = self._inspect(plan)
        with patch.object(todo_graph, "_process_alive", return_value=False), patch.object(
            todo_graph, "_pid_reused", return_value=False
        ):
            result = todo_graph.resolve_interrupted(self.run_dir, plan, record, adapter=FakeAdapter())
        self.assertEqual(result["status"], "BLOCKED")
        self.assertIn("workspace", result["nodes"]["task-a"]["reason"].lower())

    def test_resolve_fails_closed_on_wrong_model_in_usage(self):
        plan = self.full_fixture(usage_overrides={"model": "mismatched-model"})
        record = self._inspect(plan)
        with patch.object(todo_graph, "_process_alive", return_value=False), patch.object(
            todo_graph, "_pid_reused", return_value=False
        ):
            result = todo_graph.resolve_interrupted(self.run_dir, plan, record, adapter=FakeAdapter())
        self.assertEqual(result["status"], "BLOCKED")
        self.assertIn("model", result["nodes"]["task-a"]["reason"].lower())

    def test_resolve_fails_closed_on_wrong_base_in_usage(self):
        plan = self.full_fixture(usage_overrides={"worktree_base_commit_sha": "1" * 40})
        record = self._inspect(plan)
        with patch.object(todo_graph, "_process_alive", return_value=False), patch.object(
            todo_graph, "_pid_reused", return_value=False
        ):
            result = todo_graph.resolve_interrupted(self.run_dir, plan, record, adapter=FakeAdapter())
        self.assertEqual(result["status"], "BLOCKED")
        self.assertIn("base", result["nodes"]["task-a"]["reason"].lower())

    def test_resolve_fails_closed_when_live_checkout_base_is_unavailable(self):
        plan = self.full_fixture()
        with patch.object(todo_graph, "_workspace_base", return_value=None):
            record = self._inspect(plan)
            with patch.object(todo_graph, "_process_alive", return_value=False), patch.object(
                todo_graph, "_pid_reused", return_value=False
            ):
                result = todo_graph.resolve_interrupted(self.run_dir, plan, record, adapter=FakeAdapter())
        self.assertEqual(result["status"], "BLOCKED")
        self.assertIn("base", result["nodes"]["task-a"]["reason"].lower())

    def test_resolve_fails_closed_on_wrong_run_id_in_usage(self):
        plan = self.full_fixture(usage_overrides={"run_id": "other-fleet-run-id"})
        record = self._inspect(plan)
        with patch.object(todo_graph, "_process_alive", return_value=False), patch.object(
            todo_graph, "_pid_reused", return_value=False
        ):
            result = todo_graph.resolve_interrupted(self.run_dir, plan, record, adapter=FakeAdapter())
        self.assertEqual(result["status"], "BLOCKED")
        self.assertIn("run_id", result["nodes"]["task-a"]["reason"].lower())

    def test_resolve_fails_closed_on_unverified_runtime_in_usage(self):
        plan = self.full_fixture(usage_overrides={"runtime_attestation": {"status": "FAILED"}})
        record = self._inspect(plan)
        with patch.object(todo_graph, "_process_alive", return_value=False), patch.object(
            todo_graph, "_pid_reused", return_value=False
        ):
            result = todo_graph.resolve_interrupted(self.run_dir, plan, record, adapter=FakeAdapter())
        self.assertEqual(result["status"], "BLOCKED")
        self.assertIn("runtime", result["nodes"]["task-a"]["reason"].lower())

    def test_runtime_status_alone_is_not_exact_provenance(self):
        for incomplete in ("VERIFIED", {"status": "VERIFIED"},
                           {"status": "VERIFIED", "package_fingerprint": "not-a-sha"}):
            plan = self.full_fixture(usage_overrides={"runtime_attestation": incomplete})
            record = self._inspect(plan)
            with patch.object(todo_graph, "_process_alive", return_value=False), patch.object(
                todo_graph, "_pid_reused", return_value=False
            ):
                result = todo_graph.resolve_interrupted(self.run_dir, plan, record, adapter=FakeAdapter())
            self.assertEqual(result["status"], "BLOCKED")
            self.assertIn("runtime", result["nodes"]["task-a"]["reason"].lower())

    def test_resolve_fails_closed_on_selected_maker_mismatch(self):
        plan = self.full_fixture(usage_overrides={"agent": "unauthorized-agent"})
        record = self._inspect(plan)
        with patch.object(todo_graph, "_process_alive", return_value=False), patch.object(
            todo_graph, "_pid_reused", return_value=False
        ):
            result = todo_graph.resolve_interrupted(self.run_dir, plan, record, adapter=FakeAdapter())
        self.assertEqual(result["status"], "BLOCKED")
        self.assertIn("maker", result["nodes"]["task-a"]["reason"].lower())

    def test_resolve_fails_closed_on_usage_json_fallback_only(self):
        """usage_record is mandatory; a loose usage.json is never an authoritative fallback."""
        plan = self.full_fixture(with_usage=False)
        maker_folder = self.run_dir / "task-a" / "1" / "maker"
        fallback_usage = {
            "run_id": "fleet-run-test-001",
            "agent": "maker",
            "model": "m-model",
            "model_provider_id": "maker-lineage",
            "runtime_attestation": {
                "status": "VERIFIED", "package_version": "0.9.55",
                "package_fingerprint": "1" * 64,
                "payload_fingerprint": "2" * 64,
                "payload_file_count": 185,
                "runtime_root": str(self.fixture_dir / "synthetic-pinned-runtime"),
            },
            "worktree_base_commit_sha": "0" * 40,
            "worktree_base_provenance": "preserved",
            "exit_code": 0,
            "provider_terminal_reason": "completed",
            "execution_workspace": str(self.workspace),
        }
        todo_graph.write(maker_folder / "usage.json", fallback_usage)
        record = self._inspect(plan)
        with patch.object(todo_graph, "_process_alive", return_value=False), patch.object(
            todo_graph, "_pid_reused", return_value=False
        ):
            result = todo_graph.resolve_interrupted(self.run_dir, plan, record, adapter=FakeAdapter())
        self.assertEqual(result["status"], "BLOCKED")
        self.assertIn("usage", result["nodes"]["task-a"]["reason"].lower())
        state = todo_graph.read(self.run_dir / "state.json")
        self.assertEqual(state["nodes"]["task-a"]["status"], "RUNNING")

    def test_resolve_fails_closed_on_result_usage_lineage_mismatch(self):
        plan = self.full_fixture(
            lineage="maker-lineage",
            usage_overrides={"model_provider_id": "other-lineage"},
        )
        record = self._inspect(plan)
        with patch.object(todo_graph, "_process_alive", return_value=False), patch.object(
            todo_graph, "_pid_reused", return_value=False
        ):
            result = todo_graph.resolve_interrupted(self.run_dir, plan, record, adapter=FakeAdapter())
        self.assertEqual(result["status"], "BLOCKED")
        self.assertIn("lineage", result["nodes"]["task-a"]["reason"].lower())
        state = todo_graph.read(self.run_dir / "state.json")
        self.assertEqual(state["nodes"]["task-a"]["status"], "RUNNING")

    def test_resolve_fails_closed_on_authoritative_disk_status_not_candidate(self):
        """Progress history cannot promote a non-candidate on-disk result."""
        plan = self.full_fixture()
        maker_folder = self.run_dir / "task-a" / "1" / "maker"
        result_json = todo_graph.read(maker_folder / "result.json")
        result_json["status"] = "BLOCKED"
        todo_graph.write(maker_folder / "result.json", result_json)
        record = self._inspect(plan)
        with patch.object(todo_graph, "_process_alive", return_value=False), patch.object(
            todo_graph, "_pid_reused", return_value=False
        ):
            result = todo_graph.resolve_interrupted(self.run_dir, plan, record, adapter=FakeAdapter())
        self.assertEqual(result["status"], "BLOCKED")
        self.assertIn("maker status", result["nodes"]["task-a"]["reason"].lower())
        state = todo_graph.read(self.run_dir / "state.json")
        self.assertEqual(state["nodes"]["task-a"]["status"], "RUNNING")

    # ----------------------------------------------- lease no-delete tests

    def test_exclusive_owner_release_renames_to_released_proof(self):
        lock_file = self.fixture_dir / "test-lease.lock"
        with todo_graph.exclusive(lock_file):
            self.assertTrue(lock_file.is_file())
            content = lock_file.read_text(encoding="utf-8")
            self.assertTrue(content.startswith(str(os.getpid())))
        # After exit, original lock_file should no longer exist
        self.assertFalse(lock_file.exists())
        # A retained .released file must exist in the directory
        released_files = list(self.fixture_dir.glob("test-lease.lock.*.released"))
        self.assertEqual(len(released_files), 1)
        released_content = released_files[0].read_text(encoding="utf-8")
        self.assertTrue(released_content.startswith(str(os.getpid())))
        # Original name can be re-acquired
        with todo_graph.exclusive(lock_file):
            self.assertTrue(lock_file.is_file())

    def test_exclusive_retains_foreign_replacement(self):
        lock_file = self.fixture_dir / "foreign-test.lock"
        with self.assertRaises(RuntimeError):
            with todo_graph.exclusive(lock_file):
                # Overwrite lock file with foreign bytes
                lock_file.write_text("9999999:foreign-token\n", encoding="utf-8")
                raise RuntimeError("forced exit")
        # Foreign replacement must be retained and NOT unlinked or renamed
        self.assertTrue(lock_file.is_file())
        self.assertEqual(lock_file.read_text(encoding="utf-8"), "9999999:foreign-token\n")

    def test_exclusive_refuses_concurrent_claimant(self):
        lock_file = self.fixture_dir / "claimant-test.lock"
        with todo_graph.exclusive(lock_file):
            with self.assertRaises(FileExistsError):
                with todo_graph.exclusive(lock_file):
                    pass

    # ---------------------------------------------------- Suite055 test

    def test_suite055_faithful_sanitized_positive_recovery(self):
        """Sanitized Suite055 CANDIDATE shape under retained synthetic ledger root."""
        plan = self.make_plan()
        self.make_running_state(plan)
        fleet_id = "fleet-run-suite055-001"
        self.make_progress(with_receipt=True, with_usage=True, fleet_run_id=fleet_id)
        self.make_process_evidence()

        record = self._inspect(plan)
        # Fleet run_id is independent from Graph run directory name
        self.assertEqual(record["run_id"], self.run_dir.name)
        self.assertEqual(record["nodes"]["task-a"]["run_id"], fleet_id)
        self.assertNotEqual(record["run_id"], fleet_id)

        adapter = FakeAdapter()
        with patch.object(todo_graph, "_process_alive", return_value=False), patch.object(
            todo_graph, "_pid_reused", return_value=False
        ):
            result = todo_graph.resolve_interrupted(self.run_dir, plan, record, adapter=adapter)

        self.assertEqual(result["status"], "ACCEPTED_CANDIDATE")
        self.assertEqual(result["nodes"]["task-a"]["status"], "ACCEPTED")

    # ---------------------------------------------------- CLI surface tests

    def test_cli_inspect(self):
        plan = self.full_fixture()
        todo_file = self.fixture_dir / "todo.json"
        todo_graph.write(todo_file, plan)
        code = todo_graph.main(["inspect", "--todo", str(todo_file), "--run-dir", str(self.run_dir)])
        self.assertEqual(code, 0)

    def test_cli_resolve_interrupted_missing_inspection_refusal(self):
        plan = self.full_fixture()
        todo_file = self.fixture_dir / "todo.json"
        todo_graph.write(todo_file, plan)
        code = todo_graph.main(["resolve-interrupted", "--todo", str(todo_file), "--run-dir", str(self.run_dir)])
        self.assertEqual(code, 3)

    def test_cli_resolve_interrupted_blocked_exit_code_3(self):
        plan = self.full_fixture()
        todo_file = self.fixture_dir / "todo.json"
        todo_graph.write(todo_file, plan)
        insp_record = self._inspect(plan)
        insp_file = self.fixture_dir / "inspection.json"
        todo_graph.write(insp_file, insp_record)
        with patch.object(todo_graph, "_process_alive", return_value=True):
            code = todo_graph.main([
                "resolve-interrupted",
                "--todo", str(todo_file),
                "--run-dir", str(self.run_dir),
                "--inspection", str(insp_file),
            ])
        self.assertEqual(code, 3)

    def test_cli_resolve_interrupted_accepted_exit_code_0(self):
        plan = self.full_fixture()
        todo_file = self.fixture_dir / "todo.json"
        todo_graph.write(todo_file, plan)
        insp_record = self._inspect(plan)
        insp_file = self.fixture_dir / "inspection.json"
        todo_graph.write(insp_file, insp_record)
        with patch.object(todo_graph, "_process_alive", return_value=False), patch.object(
            todo_graph, "_pid_reused", return_value=False
        ), patch.object(todo_graph, "FleetAdapter", return_value=FakeAdapter()):
            code = todo_graph.main([
                "resolve-interrupted",
                "--todo", str(todo_file),
                "--run-dir", str(self.run_dir),
                "--inspection", str(insp_file),
            ])
        self.assertEqual(code, 0)
        state = todo_graph.read(self.run_dir / "state.json")
        self.assertEqual(state["nodes"]["task-a"]["status"], "ACCEPTED")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument(
        "--artifact-root",
        dest="artifact_root",
        type=str,
        default=None,
        help="Root directory for test fixtures",
    )
    args, remaining = parser.parse_known_args()
    if args.artifact_root:
        FIXTURES_ROOT = Path(args.artifact_root).resolve()
        RecoveryTests.fixtures_root = FIXTURES_ROOT
    unittest.main(argv=[sys.argv[0]] + remaining)
