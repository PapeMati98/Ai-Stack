from __future__ import annotations

import argparse
import copy
import hashlib
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import test_agent_graph as fixture_module


agent_graph = fixture_module.agent_graph
plan_contract = fixture_module.plan_contract
evidence_for = fixture_module.evidence_for
write_json = fixture_module.write_json

# Full discovery imports command_adapter first; bind the runner to this fixture's
# exact module instance so GraphError identity remains deterministic.
sys.modules["agent_graph"] = agent_graph
RUNNER_SPEC = importlib.util.spec_from_file_location(
    "graph_runner_under_test", Path(__file__).with_name("graph_runner.py")
)
assert RUNNER_SPEC and RUNNER_SPEC.loader
graph_runner = importlib.util.module_from_spec(RUNNER_SPEC)
RUNNER_SPEC.loader.exec_module(graph_runner)


ADAPTER_SOURCE = r'''
import argparse
import json
import os
import time
from pathlib import Path


def log(path, value):
    with Path(path).open("a", encoding="utf-8") as handle:
        handle.write(value + "\n")


parser = argparse.ArgumentParser()
parser.add_argument("node_id")
parser.add_argument("mode")
parser.add_argument("evidence_path")
parser.add_argument("event_log")
parser.add_argument("marker")
parser.add_argument("rejected_evidence_path")
args = parser.parse_args()
attempt_id = os.environ["AGENT_GRAPH_ATTEMPT_ID"]
log(args.event_log, f"start {args.node_id} {time.monotonic_ns()}")

if args.mode == "sleep":
    time.sleep(0.35)
elif args.mode == "slow":
    time.sleep(1.0)
elif args.mode == "reject-always":
    time.sleep(0.05)
elif args.mode == "reject-once":
    marker = Path(args.marker)
    if not marker.exists():
        marker.write_text("first\n", encoding="utf-8")
        args.evidence_path = args.rejected_evidence_path

if args.mode == "no-evidence":
    log(args.event_log, f"end {args.node_id} {time.monotonic_ns()}")
    raise SystemExit(0)

payload = json.loads(Path(args.evidence_path).read_text(encoding="utf-8"))
if args.mode == "reject-always" or (args.mode == "reject-once" and args.evidence_path == args.rejected_evidence_path):
    payload["verdict"] = "rejected"
    for criterion in payload["criteria_results"]:
        criterion["status"] = "FAIL"
payload["evidence_id"] = payload["evidence_id"] + "-" + attempt_id[:8]
Path(os.environ["AGENT_GRAPH_EVIDENCE_PATH"]).write_text(
    json.dumps({"attempt_id": attempt_id, "evidence": payload}),
    encoding="utf-8",
)
log(args.event_log, f"end {args.node_id} {time.monotonic_ns()}")
'''


class GraphRunnerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = fixture_module.AgentGraphTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self._known_processes: list[subprocess.Popen] = []
        self._scheduler_interrupted = False
        self._planner_pin = agent_graph.PLANNER_VALIDATOR_SHA256
        agent_graph.PLANNER_VALIDATOR_SHA256 = agent_graph.sha256_file(
            fixture_module.PLANNER_PATH
        )
        self.addCleanup(self._restore_planner_pin)
        # The runner publishes Office events asynchronously.  Let known
        # scheduler children and their bounded bridge finish before TEMP is
        # removed; teardown still reports a real lingering handle.
        self.addCleanup(self._wait_for_known_subprocesses)
        self.run_id, self.run_dir = self.fixture.init_run()
        self.event_log = self.fixture.root / "events.log"
        self.marker = self.fixture.root / "retry.marker"
        self.adapter = self.fixture.root / "adapter.py"
        self.adapter.write_text(ADAPTER_SOURCE, encoding="utf-8")

    def _restore_planner_pin(self) -> None:
        agent_graph.PLANNER_VALIDATOR_SHA256 = self._planner_pin

    def _wait_for_known_subprocesses(self) -> None:
        deadline = time.monotonic() + 1.8
        for process in self._known_processes:
            if process.poll() is not None:
                continue
            remaining = max(0.0, deadline - time.monotonic())
            process.wait(timeout=remaining)

        if not self._scheduler_interrupted:
            return
        office_root = self.fixture.state_root / ".office"
        while office_root.exists() and time.monotonic() < deadline:
            time.sleep(min(0.05, max(0.0, deadline - time.monotonic())))

    def _evidence_file(self, node_id: str, workspace: Path, sequence: str, verdict: str = "accepted") -> Path:
        path = self.fixture.root / f"{node_id}-{sequence}.json"
        graph_node = next(item for item in self.fixture.graph["nodes"] if item["node_id"] == node_id)
        changed_path = None
        if graph_node["write_scope"]:
            changed_path = (
                "frontend/runner-artifact.txt"
                if node_id == "node-integrate"
                else node_id.removeprefix("node-") + "/runner-artifact-" + sequence + ".txt"
            )
            if node_id == "node-integrate":
                changed_path = "frontend/runner-artifact-" + sequence + ".txt"
        write_json(
            path,
            evidence_for(
                self.fixture.graph,
                node_id,
                workspace,
                verdict=verdict,
                changed_path=changed_path,
                sequence=sequence,
            ),
        )
        return path

    def _dispatch(
        self,
        modes: dict[str, str],
        max_parallel: int = 2,
        *,
        parallelism: dict | None = None,
    ) -> Path:
        state = agent_graph.load_state(self.run_dir)
        plan = agent_graph.load_current_plan(self.run_dir, state)
        entries = {}
        for node_id, mode in modes.items():
            workspace = self.fixture.project
            if node_id in {"node-frontend", "node-backend"}:
                workspace = self.fixture.root / (node_id + "-worktree")
                workspace.mkdir(exist_ok=True)
            accepted = self._evidence_file(node_id, workspace, mode)
            rejected = self._evidence_file(node_id, workspace, mode + "-rejected", "rejected")
            argv = [
                sys.executable,
                str(self.adapter),
                node_id,
                mode,
                str(accepted),
                str(self.event_log),
                str(self.marker),
                str(rejected),
            ]
            pins = {
                str(Path(sys.executable).resolve()): agent_graph.sha256_file(Path(sys.executable)),
                str(self.adapter.resolve()): agent_graph.sha256_file(self.adapter),
                str(accepted.resolve()): agent_graph.sha256_file(accepted),
                str(rejected.resolve()): agent_graph.sha256_file(rejected),
            }
            entries[node_id] = {
                "argv": argv,
                "workspace_root": str(workspace.resolve()),
                "pinned_files": pins,
            }
        dispatch = {
            "schema_version": 2 if parallelism is not None else 1,
            "run_id": self.run_id,
            "graph_sha256": agent_graph.sha256_value(plan),
            "max_parallel": max_parallel,
            "nodes": entries,
        }
        if parallelism is not None:
            dispatch["parallelism"] = parallelism
        path = self.fixture.root / "dispatch.json"
        write_json(path, dispatch)
        return path

    def _run(self, dispatch: Path) -> dict:
        return graph_runner.command_run(
            argparse.Namespace(
                run_id=self.run_id,
                dispatch=str(dispatch),
                state_root=str(self.fixture.state_root),
            )
        )

    def _state(self) -> dict:
        return agent_graph.load_state(self.run_dir)

    def test_real_subprocesses_overlap_and_dependencies_start_after_acceptance(self) -> None:
        dispatch = self._dispatch(
            {
                "node-frontend": "sleep",
                "node-backend": "sleep",
                "node-integrate": "sleep",
            }
        )
        result = self._run(dispatch)
        self.assertEqual(result["status"], "WAITING")
        rows = {}
        for line in self.event_log.read_text(encoding="utf-8").splitlines():
            action, node_id, stamp = line.split()
            rows.setdefault(node_id, {})[action] = int(stamp)
        self.assertLess(rows["node-frontend"]["start"], rows["node-backend"]["end"])
        self.assertLess(rows["node-backend"]["start"], rows["node-frontend"]["end"])
        self.assertGreater(rows["node-integrate"]["start"], rows["node-frontend"]["end"])
        self.assertGreater(rows["node-integrate"]["start"], rows["node-backend"]["end"])
        self.assertGreaterEqual(result["scheduler"]["observed_execution_overlap"], 2)
        self.assertEqual(result["scheduler"]["parallelism"]["policy"], "legacy-hard")
        for attempt in result["scheduler"]["attempts"]:
            self.assertGreaterEqual(attempt["queue_wait_seconds"], 0.0)
            self.assertGreater(attempt["execution_seconds"], 0.0)
        state = self._state()
        for node_id in ("node-frontend", "node-backend", "node-integrate"):
            self.assertEqual(state["nodes"][node_id]["status"], "accepted")

    def test_rejected_retry_is_bounded_and_does_not_pass(self) -> None:
        dispatch = self._dispatch({"node-frontend": "reject-always"})
        result = self._run(dispatch)
        self.assertEqual(result["status"], "WAITING")
        state = self._state()
        node_state = state["nodes"]["node-frontend"]
        self.assertEqual(node_state["status"], "rejected")
        self.assertEqual(node_state["attempts"], 3)
        self.assertEqual(len([line for line in self.event_log.read_text(encoding="utf-8").splitlines() if line.startswith("start node-frontend")]), 3)

    def test_exit_zero_without_evidence_is_not_pass(self) -> None:
        result = self._run(self._dispatch({"node-frontend": "no-evidence"}))
        self.assertEqual(result["status"], "NEEDS_ATTENTION")
        self.assertEqual(result["errors"]["node-frontend"]["status"], "MISSING_OR_CHANGED_EVIDENCE")
        self.assertNotEqual(self._state()["nodes"]["node-frontend"]["status"], "accepted")

    def test_stale_dispatch_pin_and_operation_nonce_are_refused(self) -> None:
        dispatch_path = self._dispatch({"node-frontend": "sleep"})
        dispatch = graph_runner.read(dispatch_path)
        plan = agent_graph.load_current_plan(self.run_dir, self._state())
        changed_plan = copy.deepcopy(plan)
        changed_plan["intent"] += " stale"
        with self.assertRaisesRegex(agent_graph.GraphError, "stale: graph hash mismatch"):
            graph_runner.validate_dispatch(dispatch, changed_plan, self.run_id)
        self.adapter.write_text(ADAPTER_SOURCE + "\n# changed\n", encoding="utf-8")
        with self.assertRaisesRegex(agent_graph.GraphError, "Dispatch input changed"):
            graph_runner.validate_dispatch(dispatch, plan, self.run_id)
        self.fixture.start(self.run_id, "node-frontend", "op-nonce-reused")
        with self.assertRaisesRegex(agent_graph.GraphError, "operation_id reused with another payload"):
            agent_graph.command_start(
                argparse.Namespace(
                    run_id=self.run_id,
                    node_id="node-backend",
                    operation_id="op-nonce-reused",
                    workspace_root=str(self.fixture.project),
                    external_ref=None,
                    state_root=str(self.fixture.state_root),
                )
            )

    def test_same_resource_is_serialized(self) -> None:
        for node in self.fixture.graph["nodes"]:
            if node["node_id"] in {"node-quality", "node-security"}:
                node["resource_locks"] = ["repo:shared"]
        # Build a new internally consistent synthetic approval, never mutate
        # an initialized Graph snapshot behind its planning contract.
        write_json(self.fixture.graph_path, self.fixture.graph)
        self.fixture.contract_path = self.fixture.make_approved_contract(stem='shared-lock-contract')
        self.fixture.acceptance_path = self.fixture.make_acceptance_contract(
            self.fixture.graph, self.fixture.contract_path, 'shared-lock-acceptance')
        self.run_id, self.run_dir = self.fixture.init_run()
        dispatch_path = self._dispatch({
            'node-frontend':'sleep', 'node-backend':'sleep', 'node-integrate':'sleep',
            'node-quality':'sleep', 'node-security':'sleep'})
        dispatch = graph_runner.read(dispatch_path)
        source_refs = [
            {"kind": "fleet_run", "id": "external-node-frontend-sleep"},
            {"kind": "fleet_run", "id": "external-node-backend-sleep"},
        ]
        for node_id in ("node-quality", "node-security"):
            evidence_path = self.fixture.root / f"{node_id}-sleep.json"
            evidence = graph_runner.read(evidence_path)
            evidence["external_refs"].extend(source_refs)
            write_json(evidence_path, evidence)
            dispatch["nodes"][node_id]["pinned_files"][str(evidence_path.resolve())] = agent_graph.sha256_file(evidence_path)
        write_json(dispatch_path, dispatch)
        result = self._run(dispatch_path)
        self.assertEqual(result["status"], "WAITING")
        rows = {}
        for line in self.event_log.read_text(encoding="utf-8").splitlines():
            action, node_id, stamp = line.split()
            rows.setdefault(node_id, {})[action] = int(stamp)
        self.assertTrue(
            rows["node-quality"]["end"] <= rows["node-security"]["start"]
            or rows["node-security"]["end"] <= rows["node-quality"]["start"]
        )
        waits = {
            (row["node_id"], row["code"])
            for row in result["scheduler"]["wait_reasons"]
        }
        self.assertTrue(
            ("node-quality", "resource-lock") in waits
            or ("node-security", "resource-lock") in waits
        )

    def test_legacy_cap_one_remains_a_hard_serialization_boundary(self) -> None:
        result = self._run(
            self._dispatch(
                {"node-frontend": "sleep", "node-backend": "sleep"},
                max_parallel=1,
            )
        )
        self.assertEqual(result["scheduler"]["parallelism"]["policy"], "legacy-hard")
        self.assertEqual(result["scheduler"]["parallelism"]["effective"], 1)
        rows = {}
        for line in self.event_log.read_text(encoding="utf-8").splitlines():
            action, node_id, stamp = line.split()
            rows.setdefault(node_id, {})[action] = int(stamp)
        self.assertTrue(
            rows["node-frontend"]["end"] <= rows["node-backend"]["start"]
            or rows["node-backend"]["end"] <= rows["node-frontend"]["start"]
        )

    def test_estimate_adapts_to_host_but_explicit_hard_cap_does_not(self) -> None:
        host = {
            "capacity": 6,
            "logical_cpu_count": 24,
            "available_memory_bytes": 48 * 1024**3,
            "missing_evidence": [],
        }
        estimate = {
            "schema_version": 2,
            "max_parallel": 2,
            "parallelism": {
                "policy": "estimate",
                "reason": "fresh-host-capacity",
                "evidence": {"source": "bounded-preflight"},
            },
        }
        hard = copy.deepcopy(estimate)
        hard["parallelism"]["policy"] = "hard"
        self.assertEqual(graph_runner.resolve_parallelism(estimate, host)["effective"], 6)
        self.assertEqual(graph_runner.resolve_parallelism(hard, host)["effective"], 2)

    def test_v2_parallelism_requires_reason_and_evidence(self) -> None:
        dispatch_path = self._dispatch(
            {"node-frontend": "sleep"},
            parallelism={
                "policy": "estimate",
                "reason": "fresh-host-capacity",
                "evidence": {"source": "bounded-preflight"},
            },
        )
        dispatch = graph_runner.read(dispatch_path)
        plan = agent_graph.load_current_plan(self.run_dir, self._state())
        graph_runner.validate_dispatch(dispatch, plan, self.run_id)
        dispatch["parallelism"]["evidence"] = {}
        with self.assertRaisesRegex(agent_graph.GraphError, "parallelism evidence"):
            graph_runner.validate_dispatch(dispatch, plan, self.run_id)

    def test_missing_adapter_and_dependency_waits_are_explicit(self) -> None:
        result = self._run(self._dispatch({"node-frontend": "sleep"}))
        waits = {
            (row["node_id"], row["code"]): row
            for row in result["scheduler"]["wait_reasons"]
        }
        self.assertIn(("node-backend", "missing-adapter"), waits)
        self.assertIn(("node-integrate", "dependency"), waits)
        self.assertIn("quota", result["scheduler"]["visibility_gaps"])

    def test_six_shared_capacity_leases_overlap_and_cap_one_serializes(self) -> None:
        helper_source = r'''
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, sys.argv[1])
import graph_runner

root = Path(sys.argv[2])
capacity = int(sys.argv[3])
go_at = float(sys.argv[4])
output = Path(sys.argv[5])
while time.time() < go_at:
    time.sleep(0.005)
with graph_runner.shared_capacity_lease(
    root,
    capacity,
    attempt_id=output.stem,
    node_id="node-probe",
    timeout=10,
):
    started = time.perf_counter_ns()
    time.sleep(0.3)
    ended = time.perf_counter_ns()
output.write_text(json.dumps({"start": started, "end": ended}), encoding="utf-8")
'''

        def measured(capacity: int) -> tuple[int, float]:
            with tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                helper = root / "lease_probe.py"
                helper.write_text(helper_source, encoding="utf-8")
                go_at = time.time() + 0.8
                outputs = [root / f"probe-{index}.json" for index in range(6)]
                began = time.monotonic()
                processes = [
                    subprocess.Popen(
                        [
                            sys.executable,
                            "-B",
                            str(helper),
                            str(Path(graph_runner.__file__).parent),
                            str(root / "capacity"),
                            str(capacity),
                            str(go_at),
                            str(output),
                        ],
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.PIPE,
                        text=True,
                    )
                    for output in outputs
                ]
                for process in processes:
                    _, error = process.communicate(timeout=15)
                    self.assertEqual(process.returncode, 0, error)
                elapsed = time.monotonic() - began
                intervals = [json.loads(path.read_text(encoding="utf-8")) for path in outputs]
                return graph_runner.maximum_overlap(intervals), elapsed

        overlap, _ = measured(6)
        serialized, elapsed = measured(1)
        self.assertEqual(overlap, 6)
        self.assertEqual(serialized, 1)
        self.assertGreater(elapsed, 2.2)

    def test_resume_receipt_does_not_redispatch(self) -> None:
        dispatch = self._dispatch({"node-frontend": "slow"})
        command = [
            sys.executable,
            "-B",
            str(Path(graph_runner.__file__).resolve()),
            "--run-id",
            self.run_id,
            "--dispatch",
            str(dispatch),
            "--state-root",
            str(self.fixture.state_root),
        ]
        process = subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self._known_processes.append(process)
        journal_path = self.run_dir / "runner" / "journal.json"
        deadline = time.time() + 10
        while time.time() < deadline:
            if journal_path.exists() and "STARTED" in journal_path.read_text(encoding="utf-8"):
                break
            time.sleep(0.05)
        process.kill()
        self._scheduler_interrupted = True
        process.wait(timeout=10)
        deadline = time.time() + 10
        while time.time() < deadline:
            if any((self.run_dir / "runner").glob("*/finished.json")):
                break
            time.sleep(0.05)
        resumed = self._run(dispatch)
        self.assertEqual(resumed["status"], "WAITING")
        self.assertEqual(self._state()["nodes"]["node-frontend"]["attempts"], 1)
        self.assertEqual(len([line for line in self.event_log.read_text(encoding="utf-8").splitlines() if line.startswith("start node-frontend")]), 1)

    def test_human_and_final_nodes_cannot_be_auto_dispatched(self) -> None:
        dispatch = self._dispatch({"node-frontend": "sleep"})
        value = graph_runner.read(dispatch)
        plan = agent_graph.load_current_plan(self.run_dir, self._state())
        final_entry = copy.deepcopy(next(iter(value["nodes"].values())))
        value["nodes"]["node-final"] = final_entry
        with self.assertRaisesRegex(agent_graph.GraphError, "Authority/closeout nodes"):
            graph_runner.validate_dispatch(value, plan, self.run_id)
        human_plan = copy.deepcopy(plan)
        human = next(node for node in human_plan["nodes"] if node["node_id"] == "node-integrate")
        human["executor"] = "human"
        human["capability"] = "human-approval"
        human_value = copy.deepcopy(value)
        human_value["graph_sha256"] = agent_graph.sha256_value(human_plan)
        human_value["nodes"].pop("node-final")
        human_value["nodes"]["node-integrate"] = final_entry
        with self.assertRaisesRegex(agent_graph.GraphError, "Authority/closeout nodes"):
            graph_runner.validate_dispatch(human_value, human_plan, self.run_id)

    def test_runner_lock_refuses_second_scheduler(self) -> None:
        dispatch = self._dispatch({"node-frontend": "slow"})
        command = [
            sys.executable,
            "-B",
            str(Path(graph_runner.__file__).resolve()),
            "--run-id",
            self.run_id,
            "--dispatch",
            str(dispatch),
            "--state-root",
            str(self.fixture.state_root),
        ]
        first = subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self._known_processes.append(first)
        lock_path = self.run_dir / "runner" / "runner.lock"
        deadline = time.time() + 10
        while time.time() < deadline and not lock_path.exists():
            time.sleep(0.05)
        try:
            second = subprocess.run(command, capture_output=True, text=True, timeout=10)
            self.assertEqual(second.returncode, 2)
            self.assertIn("RESOURCE_BUSY", second.stdout + second.stderr)
        finally:
            if first.poll() is None:
                try:
                    first.wait(timeout=1.8)
                except subprocess.TimeoutExpired:
                    first.kill()
                    self._scheduler_interrupted = True
            first.wait(timeout=1.8)

    def _seed_dispatch(self, dispatch_path: Path) -> str:
        dispatch = graph_runner.read(dispatch_path)
        folder = self.run_dir / "runner"
        folder.mkdir(parents=True, exist_ok=True)
        fingerprint = agent_graph.sha256_value(dispatch)
        journal = {"dispatch_sha256": fingerprint, "attempts": {}, "waits": {}}
        graph_runner.write(folder / "journal.json", journal)
        graph_runner.write(folder / "dispatch.json", dispatch)
        return fingerprint

    def _amend_backend(self) -> None:
        self.fixture.complete(
            self.run_id, "node-backend", "contract-amend-trigger", "backend/api.txt"
        )
        base = plan_contract.load_json(self.fixture.contract_path, "approved base contract")
        candidate = copy.deepcopy(base)
        candidate["revision"] = 2
        candidate["previous_contract_hash"] = base["approval"]["contract_hash"]
        candidate["status"] = "draft"
        candidate["approval"] = {
            "approved_by": None,
            "approved_at": None,
            "contract_hash": None,
        }
        candidate["graph"]["previous_projection_sha256"] = base["graph"]["projection_sha256"]
        candidate["graph"]["projection_sha256"] = None
        candidate["graph"]["integration_task_id"] = "TASK-008"
        candidate["planning"]["planner_session_id"] = "planner-session-amendment"
        candidate["gates"] = {
            gate: {
                "round": 0,
                "status": "pending",
                "auditor": None,
                "reviewed_prompt_sha256": None,
                "reviewed_plan_sha256": None,
                "reviewed_clause_ids": [],
                "reviewed_requirement_ids": [],
                "reviewed_task_ids": [],
                "attested_hard_risk_flags": [],
                "evidence": None,
            }
            for gate in ("fidelity", "sufficiency")
        }
        candidate["amendment"] = {
            "reason": "Replace the backend-dependent subgraph after terminal evidence.",
            "affected_task_ids": [
                "TASK-002",
                "TASK-003",
                "TASK-004",
                "TASK-005",
                "TASK-006",
            ],
            "authority": "parent",
            "approval_ref": "parent-test-contract-amendment",
        }
        candidate["closeout"] = {"status": "pending", "receipt_sha256": None}
        tasks = {task["task_id"]: task for task in candidate["tasks"]}
        tasks["TASK-001"]["acceptance_criteria"].append(
            {
                "criterion_id": "crit-preflight-amendment",
                "description": "Approved amendment keeps the preflight evidence bound",
                "evidence_kind": "deterministic-check",
            }
        )
        for task_id in candidate["amendment"]["affected_task_ids"]:
            tasks[task_id]["status"] = "blocked"
        replacements = []
        replacement_spec = [
            ("TASK-002", "TASK-007", "node-backend-repair", [], []),
            ("TASK-003", "TASK-008", "node-integrate-repair", ["TASK-001", "TASK-007"], []),
            (
                "TASK-004",
                "TASK-009",
                "node-quality-recheck",
                ["TASK-008"],
                ["TASK-001", "TASK-007", "TASK-008"],
            ),
            (
                "TASK-005",
                "TASK-010",
                "node-security-recheck",
                ["TASK-008"],
                ["TASK-001", "TASK-007", "TASK-008"],
            ),
            ("TASK-006", "TASK-011", "node-final-recheck", ["TASK-009", "TASK-010"], []),
        ]
        for source_id, task_id, node_id, dependencies, evidence_from in replacement_spec:
            replacement = copy.deepcopy(tasks[source_id])
            replacement["task_id"] = task_id
            replacement["graph_node_id"] = node_id
            replacement["title"] = f"Replacement for {tasks[source_id]['title']}"
            replacement["depends_on"] = dependencies
            replacement["evidence_from"] = evidence_from
            replacement["status"] = "pending"
            for criterion in replacement["acceptance_criteria"]:
                criterion["criterion_id"] = (
                    criterion["criterion_id"] + f"-r{task_id.removeprefix('TASK-')}"
                )
            replacements.append(replacement)
            requirement_id = replacement["requirement_refs"][0]
            requirement = next(
                item
                for item in candidate["requirements"]
                if item["requirement_id"] == requirement_id
            )
            requirement["task_refs"].append(task_id)
        candidate["tasks"].extend(replacements)
        amendment_path = self.fixture.approve_contract_value(
            candidate, "contract-amendment", self.fixture.contract_path
        )
        amendment_contract = plan_contract.load_json(
            amendment_path, "approved amendment"
        )
        amendment_graph = plan_contract.compile_graph_value(amendment_contract)
        amendment_acceptance_path = self.fixture.make_acceptance_contract(
            amendment_graph, amendment_path, "acceptance-contract-amendment"
        )
        agent_graph.command_amend(
            argparse.Namespace(
                run_id=self.run_id,
                operation_id="op-contract-amend",
                amendment=None,
                planning_contract_amendment=str(amendment_path),
                acceptance_contract_amendment=str(amendment_acceptance_path),
                triggered_by_node="node-backend",
                approval_ref=None,
                state_root=str(self.fixture.state_root),
            )
        )

    def _adopt_args(self, dispatch_path: Path, old_hash: str) -> argparse.Namespace:
        return argparse.Namespace(
            run_id=self.run_id,
            dispatch=str(dispatch_path),
            expected_old_dispatch_hash=old_hash,
            state_root=str(self.fixture.state_root),
        )

    def test_ordinary_drive_still_refuses_dispatch_mismatch(self) -> None:
        old_path = self._dispatch({"node-frontend": "sleep"})
        old_hash = self._seed_dispatch(old_path)
        self._amend_backend()
        new_dispatch = copy.deepcopy(graph_runner.read(old_path))
        plan = agent_graph.load_current_plan(self.run_dir, self._state())
        new_dispatch["graph_sha256"] = agent_graph.sha256_value(plan)
        new_path = self.fixture.root / "dispatch-new.json"
        write_json(new_path, new_dispatch)
        with self.assertRaisesRegex(
            agent_graph.GraphError, "Dispatch changed after execution began"
        ):
            self._run(new_path)
        journal = graph_runner.read(self.run_dir / "runner" / "journal.json")
        self.assertEqual(journal["dispatch_sha256"], old_hash)

    def test_adopt_dispatch_appends_approved_revision_and_keeps_old_bytes(self) -> None:
        old_path = self._dispatch({"node-frontend": "sleep"})
        old_dispatch = graph_runner.read(old_path)
        old_hash = self._seed_dispatch(old_path)
        attempt_dir = self.run_dir / "runner" / ("c" * 32)
        attempt_dir.mkdir()
        job = {
            "attempt_id": "c" * 32,
            "node_id": "node-frontend",
            "attempt": 1,
            "dispatch": old_dispatch["nodes"]["node-frontend"],
        }
        context = {"run_id": self.run_id, "node": {"node_id": "node-frontend"}}
        graph_runner.write(attempt_dir / "job.json", job)
        graph_runner.write(attempt_dir / "context.json", context)
        before_job = (attempt_dir / "job.json").read_bytes()
        before_context = (attempt_dir / "context.json").read_bytes()
        self._amend_backend()
        new_dispatch = copy.deepcopy(old_dispatch)
        plan = agent_graph.load_current_plan(self.run_dir, self._state())
        new_dispatch["graph_sha256"] = agent_graph.sha256_value(plan)
        new_path = self.fixture.root / "dispatch-adopted.json"
        write_json(new_path, new_dispatch)
        result = graph_runner.adopt_dispatch(self._adopt_args(new_path, old_hash))
        self.assertEqual(result["status"], "DISPATCH_ADOPTED")
        journal = graph_runner.read(self.run_dir / "runner" / "journal.json")
        self.assertEqual(journal["dispatch_sha256"], agent_graph.sha256_value(new_dispatch))
        snapshot = self.run_dir / "runner" / "dispatch-snapshots" / f"{old_hash}.json"
        self.assertEqual(agent_graph.sha256_value(graph_runner.read(snapshot)), old_hash)
        self.assertEqual((attempt_dir / "job.json").read_bytes(), before_job)
        self.assertEqual((attempt_dir / "context.json").read_bytes(), before_context)
        again = graph_runner.adopt_dispatch(self._adopt_args(new_path, old_hash))
        self.assertTrue(again.get("idempotent"))

    def test_adopt_dispatch_refuses_missing_foreign_same_revision_and_adapter_change(self) -> None:
        old_path = self._dispatch({"node-frontend": "sleep"})
        old_dispatch = graph_runner.read(old_path)
        old_hash = self._seed_dispatch(old_path)
        new_same = copy.deepcopy(old_dispatch)
        new_same["max_parallel"] = 1
        same_path = self.fixture.root / "dispatch-same-rev.json"
        write_json(same_path, new_same)
        with self.assertRaisesRegex(agent_graph.GraphError, "Same-revision"):
            graph_runner.adopt_dispatch(self._adopt_args(same_path, old_hash))
        self._amend_backend()
        with agent_graph.run_lock(self.run_dir):
            state = agent_graph.load_state(self.run_dir)
            saved_amendments = list(state["amendments"])
            state["amendments"] = []
            agent_graph.atomic_write_json(self.run_dir / "state.json", state)
        foreign = copy.deepcopy(old_dispatch)
        foreign["graph_sha256"] = self._state()["plan_hash"]
        foreign_path = self.fixture.root / "dispatch-foreign.json"
        write_json(foreign_path, foreign)
        with self.assertRaisesRegex(agent_graph.GraphError, "missing planning amendment|Foreign"):
            graph_runner.adopt_dispatch(self._adopt_args(foreign_path, old_hash))
        with agent_graph.run_lock(self.run_dir):
            state = agent_graph.load_state(self.run_dir)
            state["amendments"] = saved_amendments
            agent_graph.atomic_write_json(self.run_dir / "state.json", state)
        new_dispatch = copy.deepcopy(old_dispatch)
        plan = agent_graph.load_current_plan(self.run_dir, self._state())
        new_dispatch["graph_sha256"] = agent_graph.sha256_value(plan)
        new_dispatch["nodes"]["node-frontend"]["argv"] = list(
            new_dispatch["nodes"]["node-frontend"]["argv"]
        ) + ["--changed"]
        changed_path = self.fixture.root / "dispatch-changed-adapter.json"
        write_json(changed_path, new_dispatch)
        with self.assertRaisesRegex(
            agent_graph.GraphError, "Retained node adapter cannot change silently"
        ):
            graph_runner.adopt_dispatch(self._adopt_args(changed_path, old_hash))
        with self.assertRaisesRegex(
            agent_graph.GraphError, "does not match the expected old dispatch hash"
        ):
            graph_runner.adopt_dispatch(self._adopt_args(changed_path, "d" * 64))

    def test_adopt_dispatch_refuses_active_attempt_and_recovers_same_request(self) -> None:
        old_path = self._dispatch({"node-frontend": "sleep"})
        old_dispatch = graph_runner.read(old_path)
        old_hash = self._seed_dispatch(old_path)
        journal_path = self.run_dir / "runner" / "journal.json"
        journal = graph_runner.read(journal_path)
        journal["attempts"]["e" * 32] = {
            "attempt_id": "e" * 32,
            "node_id": "node-frontend",
            "phase": "STARTED",
        }
        graph_runner.write(journal_path, journal)
        self._amend_backend()
        new_dispatch = copy.deepcopy(old_dispatch)
        plan = agent_graph.load_current_plan(self.run_dir, self._state())
        new_dispatch["graph_sha256"] = agent_graph.sha256_value(plan)
        new_path = self.fixture.root / "dispatch-active.json"
        write_json(new_path, new_dispatch)
        with self.assertRaisesRegex(
            agent_graph.GraphError, "running or unresolved"
        ):
            graph_runner.adopt_dispatch(self._adopt_args(new_path, old_hash))
        journal["attempts"] = {}
        graph_runner.write(journal_path, journal)
        request = {
            "schema_version": 1,
            "expected_old_dispatch_hash": old_hash,
            "new_dispatch_sha256": agent_graph.sha256_value(new_dispatch),
            "new_graph_sha256": new_dispatch["graph_sha256"],
            "old_graph_sha256": old_dispatch["graph_sha256"],
            "revision": self._state()["revision"],
            "plan_hash": self._state()["plan_hash"],
        }
        graph_runner.write(self.run_dir / "runner" / "dispatch-adoption-intent.json", request)
        recovered = graph_runner.adopt_dispatch(self._adopt_args(new_path, old_hash))
        self.assertEqual(recovered["status"], "DISPATCH_ADOPTED")
        conflict = copy.deepcopy(request)
        conflict["new_dispatch_sha256"] = "f" * 64
        graph_runner.write(self.run_dir / "runner" / "dispatch-adoption-intent.json", conflict)
        other = copy.deepcopy(new_dispatch)
        other["max_parallel"] = 1
        other_path = self.fixture.root / "dispatch-conflict.json"
        write_json(other_path, other)
        with self.assertRaisesRegex(
            agent_graph.GraphError, "Conflicting dispatch adoption request"
        ):
            graph_runner.adopt_dispatch(
                self._adopt_args(other_path, old_hash)
            )

    def test_public_cli_documents_adopt_and_reconcile(self) -> None:
        parser = graph_runner.build_parser(
            [
                "adopt-dispatch",
                "--run-id",
                "run-graph-20260913T010000Z-aaaaaaaaaa",
                "--expected-old-dispatch-hash",
                "a" * 64,
                "--dispatch",
                "dispatch.json",
            ]
        )
        self.assertEqual(parser.action, "adopt-dispatch")
        recon = graph_runner.build_parser(
            [
                "reconcile-failure",
                "--run-id",
                "run-graph-20260913T010000Z-aaaaaaaaaa",
                "--node-id",
                "node-frontend",
                "--attempt-id",
                "abcd",
            ]
        )
        self.assertEqual(recon.action, "reconcile-failure")
        drive = graph_runner.build_parser(
            ["--run-id", self.run_id, "--dispatch", "dispatch.json"]
        )
        self.assertEqual(drive.action, "run")

    def _failure_fixture(
        self,
        *,
        verdict: str = "blocked",
        record_result: bool = True,
        write_adapter_result: bool = True,
    ):
        dispatch_path = self._dispatch({"node-frontend": "sleep"})
        self._seed_dispatch(dispatch_path)
        started = self.fixture.start(self.run_id, "node-frontend", "op-start-fail")
        attempt_id = "b" * 32
        folder = self.run_dir / "runner" / attempt_id
        folder.mkdir()
        evidence = evidence_for(
            self.fixture.graph,
            "node-frontend",
            self.fixture.project,
            verdict=verdict,
            changed_path="frontend/runner-artifact-fail.txt",
            sequence="fail",
        )
        if record_result:
            self.fixture.result(
                self.run_id, "node-frontend", "op-result-fail", evidence
            )
        graph_runner.write(
            folder / "job.json",
            {
                "attempt_id": attempt_id,
                "node_id": "node-frontend",
                "attempt": started["attempt"],
                "dispatch": graph_runner.read(dispatch_path)["nodes"]["node-frontend"],
            },
        )
        graph_runner.write(
            folder / "context.json",
            {
                "run_id": self.run_id,
                "node": {"node_id": "node-frontend"},
                "graph": self.fixture.graph,
            },
        )
        if write_adapter_result:
            graph_runner.write(
                folder / "adapter-result.json",
                {"attempt_id": attempt_id, "evidence": evidence},
            )
        graph_runner.write(
            folder / "finished.json",
            {"attempt_id": attempt_id, "status": "EXITED", "returncode": 1},
        )
        journal_path = self.run_dir / "runner" / "journal.json"
        journal = graph_runner.read(journal_path)
        journal["attempts"][attempt_id] = {
            "attempt_id": attempt_id,
            "node_id": "node-frontend",
            "phase": "STARTED",
        }
        graph_runner.write(journal_path, journal)
        return attempt_id, evidence, dispatch_path

    def test_settled_failure_reconciles_exact_attempt_and_is_idempotent(self) -> None:
        attempt_id, evidence, _dispatch = self._failure_fixture()
        args = argparse.Namespace(
            run_id=self.run_id,
            node_id="node-frontend",
            attempt_id=attempt_id,
            state_root=str(self.fixture.state_root),
        )
        result = graph_runner.reconcile_failure(args)
        self.assertEqual(result["status"], "FAILURE_RECONCILED")
        self.assertEqual(result["verdict"], "blocked")
        self.assertEqual(result["returncode"], 1)
        self.assertNotEqual(result["verdict"], "accepted")
        journal = graph_runner.read(self.run_dir / "runner" / "journal.json")
        row = journal["attempts"][attempt_id]
        self.assertEqual(row["phase"], "FAILURE_RECONCILED")
        self.assertEqual(row["disposition"], "failure-reconciled")
        finished = graph_runner.read(
            self.run_dir / "runner" / attempt_id / "finished.json"
        )
        self.assertEqual(finished["returncode"], 1)
        self.assertEqual(self._state()["nodes"]["node-frontend"]["status"], "blocked")
        again = graph_runner.reconcile_failure(args)
        self.assertTrue(again.get("idempotent"))

    def test_settled_failure_refuses_wrong_node_attempt_hash_running_and_blocked_only(
        self,
    ) -> None:
        attempt_id, evidence, _dispatch = self._failure_fixture()
        base = argparse.Namespace(
            run_id=self.run_id,
            node_id="node-frontend",
            attempt_id=attempt_id,
            state_root=str(self.fixture.state_root),
        )
        wrong_node = argparse.Namespace(**{**vars(base), "node_id": "node-backend"})
        with self.assertRaisesRegex(agent_graph.GraphError, "exact node and attempt"):
            graph_runner.reconcile_failure(wrong_node)
        wrong_attempt = argparse.Namespace(**{**vars(base), "attempt_id": "d" * 32})
        with self.assertRaisesRegex(agent_graph.GraphError, "exact node and attempt"):
            graph_runner.reconcile_failure(wrong_attempt)
        tampered = copy.deepcopy(evidence)
        tampered["summary"] = "changed summary for hash"
        graph_runner.write(
            self.run_dir / "runner" / attempt_id / "adapter-result.json",
            {"attempt_id": attempt_id, "evidence": tampered},
        )
        with self.assertRaisesRegex(
            agent_graph.GraphError, "Canonical terminal result does not bind"
        ):
            graph_runner.reconcile_failure(base)
        graph_runner.write(
            self.run_dir / "runner" / attempt_id / "adapter-result.json",
            {"attempt_id": attempt_id, "evidence": evidence},
        )
        run_id2, run_dir2 = self.fixture.init_run()
        self.run_id, self.run_dir = run_id2, run_dir2
        dispatch_path = self._dispatch({"node-frontend": "sleep"})
        self._seed_dispatch(dispatch_path)
        self.fixture.start(run_id2, "node-frontend", "op-start-running")
        running_id = "1" * 32
        folder = run_dir2 / "runner" / running_id
        folder.mkdir()
        graph_runner.write(
            folder / "job.json",
            {"attempt_id": running_id, "node_id": "node-frontend", "attempt": 1},
        )
        graph_runner.write(
            folder / "context.json",
            {"run_id": run_id2, "node": {"node_id": "node-frontend"}},
        )
        graph_runner.write(
            folder / "finished.json",
            {"attempt_id": running_id, "status": "EXITED", "returncode": 1},
        )
        journal = graph_runner.read(run_dir2 / "runner" / "journal.json")
        journal["attempts"][running_id] = {
            "attempt_id": running_id,
            "node_id": "node-frontend",
            "phase": "STARTED",
        }
        graph_runner.write(run_dir2 / "runner" / "journal.json", journal)
        running_args = argparse.Namespace(
            run_id=run_id2,
            node_id="node-frontend",
            attempt_id=running_id,
            state_root=str(self.fixture.state_root),
        )
        with self.assertRaisesRegex(agent_graph.GraphError, "Running node"):
            graph_runner.reconcile_failure(running_args)
        run_id3, run_dir3 = self.fixture.init_run()
        self.run_id, self.run_dir = run_id3, run_dir3
        dispatch_path = self._dispatch({"node-frontend": "sleep"})
        self._seed_dispatch(dispatch_path)
        blocked_id = "2" * 32
        folder = run_dir3 / "runner" / blocked_id
        folder.mkdir()
        graph_runner.write(
            folder / "job.json",
            {"attempt_id": blocked_id, "node_id": "node-frontend", "attempt": 1},
        )
        graph_runner.write(
            folder / "context.json",
            {"run_id": run_id3, "node": {"node_id": "node-frontend"}},
        )
        graph_runner.write(
            folder / "finished.json",
            {"attempt_id": blocked_id, "status": "EXITED", "returncode": 1},
        )
        journal = graph_runner.read(run_dir3 / "runner" / "journal.json")
        journal["attempts"][blocked_id] = {
            "attempt_id": blocked_id,
            "node_id": "node-frontend",
            "phase": "STARTED",
        }
        graph_runner.write(run_dir3 / "runner" / "journal.json", journal)
        with agent_graph.run_lock(run_dir3):
            state = agent_graph.load_state(run_dir3)
            state["nodes"]["node-frontend"]["status"] = "blocked"
            agent_graph.atomic_write_json(run_dir3 / "state.json", state)
        blocked_args = argparse.Namespace(
            run_id=run_id3,
            node_id="node-frontend",
            attempt_id=blocked_id,
            state_root=str(self.fixture.state_root),
        )
        with self.assertRaisesRegex(
            agent_graph.GraphError,
            "Canonical terminal result does not bind|Blocked status without canonical attempt evidence",
        ):
            graph_runner.reconcile_failure(blocked_args)

    def test_later_terminal_attempt_cannot_reconcile_earlier_attempt(self) -> None:
        first_id, first_evidence, _dispatch = self._failure_fixture(verdict="rejected")
        started = self.fixture.start(self.run_id, "node-frontend", "op-start-later")
        later_id = "3" * 32
        later_evidence = evidence_for(
            self.fixture.graph,
            "node-frontend",
            self.fixture.project,
            verdict="blocked",
            changed_path="frontend/runner-artifact-later.txt",
            sequence="later",
        )
        self.fixture.result(self.run_id, "node-frontend", "op-result-later", later_evidence)
        folder = self.run_dir / "runner" / later_id
        folder.mkdir()
        graph_runner.write(
            folder / "job.json",
            {
                "attempt_id": later_id,
                "node_id": "node-frontend",
                "attempt": started["attempt"],
            },
        )
        graph_runner.write(
            folder / "context.json",
            {"run_id": self.run_id, "node": {"node_id": "node-frontend"}},
        )
        graph_runner.write(
            folder / "adapter-result.json",
            {"attempt_id": later_id, "evidence": later_evidence},
        )
        graph_runner.write(
            folder / "finished.json",
            {"attempt_id": later_id, "status": "EXITED", "returncode": 1},
        )
        journal = graph_runner.read(self.run_dir / "runner" / "journal.json")
        journal["attempts"][later_id] = {
            "attempt_id": later_id,
            "node_id": "node-frontend",
            "phase": "STARTED",
        }
        graph_runner.write(self.run_dir / "runner" / "journal.json", journal)
        graph_runner.write(
            self.run_dir / "runner" / first_id / "adapter-result.json",
            {"attempt_id": first_id, "evidence": later_evidence},
        )
        earlier = argparse.Namespace(
            run_id=self.run_id,
            node_id="node-frontend",
            attempt_id=first_id,
            state_root=str(self.fixture.state_root),
        )
        with self.assertRaisesRegex(
            agent_graph.GraphError,
            "Canonical terminal result does not bind|Later unrelated",
        ):
            graph_runner.reconcile_failure(earlier)

    def test_drive_reconciles_settled_failure_without_acceptance(self) -> None:
        attempt_id, evidence, dispatch_path = self._failure_fixture()
        journal = graph_runner.read(self.run_dir / "runner" / "journal.json")
        errors: dict = {}
        graph_runner.reconcile(
            self.run_dir,
            argparse.Namespace(
                state_root=str(self.fixture.state_root),
                run_id=self.run_id,
                dispatch=str(dispatch_path),
            ),
            journal,
            errors,
        )
        journal = graph_runner.read(self.run_dir / "runner" / "journal.json")
        self.assertEqual(journal["attempts"][attempt_id]["phase"], "FAILURE_RECONCILED")
        self.assertEqual(self._state()["nodes"]["node-frontend"]["status"], "blocked")
        self.assertEqual(
            graph_runner.read(self.run_dir / "runner" / attempt_id / "finished.json")[
                "returncode"
            ],
            1,
        )
        self.assertNotIn(attempt_id, errors)
        self.assertNotEqual(self._state()["nodes"]["node-frontend"]["status"], "accepted")

    def _history_fingerprint(self, run_dir: Path) -> dict[str, str]:
        payload = {}
        for relative in ("state.json", "runner/journal.json"):
            path = run_dir / relative
            payload[relative] = hashlib.sha256(path.read_bytes()).hexdigest()
        return payload

    def test_settled_failure_reconciles_absent_adapter_result_and_is_idempotent(self) -> None:
        attempt_id, evidence, _dispatch = self._failure_fixture(write_adapter_result=False)
        adapter = self.run_dir / "runner" / attempt_id / "adapter-result.json"
        self.assertFalse(adapter.exists())
        args = argparse.Namespace(
            run_id=self.run_id,
            node_id="node-frontend",
            attempt_id=attempt_id,
            state_root=str(self.fixture.state_root),
        )
        result = graph_runner.reconcile_failure(args)
        self.assertEqual(result["status"], "FAILURE_RECONCILED")
        self.assertEqual(result["verdict"], "blocked")
        self.assertEqual(result["returncode"], 1)
        self.assertNotEqual(result["verdict"], "accepted")
        self.assertFalse(adapter.exists())
        journal = graph_runner.read(self.run_dir / "runner" / "journal.json")
        row = journal["attempts"][attempt_id]
        self.assertEqual(row["phase"], "FAILURE_RECONCILED")
        self.assertEqual(self._state()["nodes"]["node-frontend"]["status"], "blocked")
        self.assertEqual(self._state()["nodes"]["node-frontend"]["attempts"], 1)
        again = graph_runner.reconcile_failure(args)
        self.assertTrue(again.get("idempotent"))
        self.assertFalse(adapter.exists())

    def test_absent_adapter_result_refuses_foreign_later_hash_missing_result_zero_exit_and_malformed(
        self,
    ) -> None:
        attempt_id, evidence, _dispatch = self._failure_fixture(write_adapter_result=False)
        base = argparse.Namespace(
            run_id=self.run_id,
            node_id="node-frontend",
            attempt_id=attempt_id,
            state_root=str(self.fixture.state_root),
        )

        def refuse(args, pattern: str) -> None:
            before = self._history_fingerprint(self.run_dir)
            with self.assertRaisesRegex(agent_graph.GraphError, pattern):
                graph_runner.reconcile_failure(args)
            self.assertEqual(self._history_fingerprint(self.run_dir), before)
            self.assertEqual(self._state()["nodes"]["node-frontend"]["status"], "blocked")
            self.assertNotEqual(
                graph_runner.read(self.run_dir / "runner" / "journal.json")["attempts"][attempt_id].get("phase"),
                "FAILURE_RECONCILED",
            )

        refuse(
            argparse.Namespace(**{**vars(base), "run_id": "run-graph-19990101T000000Z-ffffffffff"}),
            "Unknown run_id|exact node and attempt",
        )
        refuse(argparse.Namespace(**{**vars(base), "node_id": "node-backend"}), "exact node and attempt")
        refuse(argparse.Namespace(**{**vars(base), "attempt_id": "d" * 32}), "exact node and attempt")

        context_path = self.run_dir / "runner" / attempt_id / "context.json"
        context = graph_runner.read(context_path)
        context["run_id"] = "run-graph-19990101T000000Z-eeeeeeeeee"
        graph_runner.write(context_path, context)
        refuse(base, "Canonical terminal result does not bind")
        context["run_id"] = self.run_id
        graph_runner.write(context_path, context)

        state = self._state()
        original_hash = state["nodes"]["node-frontend"]["evidence"][0]["evidence_hash"]
        state["nodes"]["node-frontend"]["evidence"][0]["evidence_hash"] = "f" * 64
        with agent_graph.run_lock(self.run_dir):
            agent_graph.atomic_write_json(self.run_dir / "state.json", state)
        refuse(base, "Canonical terminal result does not bind")
        state["nodes"]["node-frontend"]["evidence"][0]["evidence_hash"] = original_hash
        with agent_graph.run_lock(self.run_dir):
            agent_graph.atomic_write_json(self.run_dir / "state.json", state)

        result_id = "op-result-fail"
        current = self._state()
        saved_result = current["operations"][result_id]
        del current["operations"][result_id]
        with agent_graph.run_lock(self.run_dir):
            agent_graph.atomic_write_json(self.run_dir / "state.json", current)
        refuse(base, "Canonical terminal result does not bind")
        current["operations"][result_id] = saved_result
        with agent_graph.run_lock(self.run_dir):
            agent_graph.atomic_write_json(self.run_dir / "state.json", current)

        extra_state = self._state()
        extra_state["operations"]["op-start-fail-ambiguous"] = copy.deepcopy(
            extra_state["operations"]["op-start-fail"]
        )
        with agent_graph.run_lock(self.run_dir):
            agent_graph.atomic_write_json(self.run_dir / "state.json", extra_state)
        refuse(base, "Canonical terminal result does not bind|Original start operation")
        extra_state["operations"].pop("op-start-fail-ambiguous")
        with agent_graph.run_lock(self.run_dir):
            agent_graph.atomic_write_json(self.run_dir / "state.json", extra_state)

        later_state = self._state()
        later_start = copy.deepcopy(later_state["operations"]["op-start-fail"])
        later_start["recorded_at"] = "2099-01-01T00:00:00.000000Z"
        later_start["result"] = dict(later_start["result"])
        later_start["result"]["attempt"] = int(later_start["result"]["attempt"]) + 1
        later_state["operations"]["op-start-later-absent"] = later_start
        later_result = copy.deepcopy(later_state["operations"]["op-result-fail"])
        later_result["recorded_at"] = "2099-01-01T00:00:00.000000Z"
        later_state["operations"]["op-result-later-absent"] = later_result
        with agent_graph.run_lock(self.run_dir):
            agent_graph.atomic_write_json(self.run_dir / "state.json", later_state)
        refuse(base, "Canonical terminal result does not bind|Later unrelated")
        later_state["operations"].pop("op-start-later-absent")
        later_state["operations"].pop("op-result-later-absent")
        with agent_graph.run_lock(self.run_dir):
            agent_graph.atomic_write_json(self.run_dir / "state.json", later_state)

        running_state = self._state()
        running_state["nodes"]["node-frontend"]["status"] = "running"
        with agent_graph.run_lock(self.run_dir):
            agent_graph.atomic_write_json(self.run_dir / "state.json", running_state)
        running_before = self._history_fingerprint(self.run_dir)
        with self.assertRaisesRegex(agent_graph.GraphError, "Running node"):
            graph_runner.reconcile_failure(base)
        self.assertEqual(self._history_fingerprint(self.run_dir), running_before)
        running_state["nodes"]["node-frontend"]["status"] = "blocked"
        with agent_graph.run_lock(self.run_dir):
            agent_graph.atomic_write_json(self.run_dir / "state.json", running_state)

        finished = graph_runner.read(self.run_dir / "runner" / attempt_id / "finished.json")
        finished["returncode"] = 0
        graph_runner.write(self.run_dir / "runner" / attempt_id / "finished.json", finished)
        refuse(base, "Canonical terminal result does not bind")
        finished["returncode"] = 1
        graph_runner.write(self.run_dir / "runner" / attempt_id / "finished.json", finished)

        graph_runner.write(
            self.run_dir / "runner" / attempt_id / "adapter-result.json",
            {"attempt_id": attempt_id, "evidence": evidence, "extra": True},
        )
        refuse(base, "Canonical terminal result does not bind")
        (self.run_dir / "runner" / attempt_id / "adapter-result.json").unlink()

    def test_drive_reconciles_absent_adapter_result_without_acceptance(self) -> None:
        attempt_id, evidence, dispatch_path = self._failure_fixture(write_adapter_result=False)
        journal = graph_runner.read(self.run_dir / "runner" / "journal.json")
        errors: dict = {}
        graph_runner.reconcile(
            self.run_dir,
            argparse.Namespace(
                state_root=str(self.fixture.state_root),
                run_id=self.run_id,
                dispatch=str(dispatch_path),
            ),
            journal,
            errors,
        )
        journal = graph_runner.read(self.run_dir / "runner" / "journal.json")
        self.assertEqual(journal["attempts"][attempt_id]["phase"], "FAILURE_RECONCILED")
        self.assertEqual(self._state()["nodes"]["node-frontend"]["status"], "blocked")
        self.assertFalse(
            (self.run_dir / "runner" / attempt_id / "adapter-result.json").exists()
        )
        self.assertNotIn(attempt_id, errors)
        self.assertNotEqual(self._state()["nodes"]["node-frontend"]["status"], "accepted")

    def test_real_minimized_fixture_reconciles_without_adapter_result(self) -> None:
        original = (
            Path(__file__).resolve().parents[5]
            / "docs"
            / "fleet-shared-compatibility-20260913"
            / "two-gap-continuation"
            / "failure-fixture-original-copy"
        )
        pins = json.loads(
            (original.parent / "fixture-original-pins.json").read_text(encoding="utf-8")
        )
        original_hashes = {
            relative: hashlib.sha256((original / relative).read_bytes()).hexdigest()
            for relative in pins["pins"]
        }
        self.assertEqual(original_hashes, pins["pins"])
        with tempfile.TemporaryDirectory() as temporary:
            replica = Path(temporary) / "fixture"
            shutil.copytree(original, replica)
            run_id = "run-graph-20260912T161931Z-eb5106dd3e"
            run_dir = replica / run_id
            evidence_path = (
                run_dir / "evidence" / "evidence-task002-launch-blocked-20260912.json"
            )
            with agent_graph.run_lock(run_dir):
                state = agent_graph.load_state(run_dir)
                state["nodes"]["node-t-03"]["evidence"][0]["path"] = str(evidence_path)
                agent_graph.atomic_write_json(run_dir / "state.json", state)
            job_path = run_dir / "runner" / "e8890f8d3d254dda8aa93307bdba544d" / "job.json"
            job = graph_runner.read(job_path)
            job["lock_root"] = str(replica / ".resource-locks")
            graph_runner.write(job_path, job)
            adapter = (
                run_dir
                / "runner"
                / "e8890f8d3d254dda8aa93307bdba544d"
                / "adapter-result.json"
            )
            self.assertFalse(adapter.exists())
            args = argparse.Namespace(
                run_id=run_id,
                node_id="node-t-03",
                attempt_id="e8890f8d3d254dda8aa93307bdba544d",
                state_root=str(replica),
            )
            first = graph_runner.reconcile_failure(args)
            second = graph_runner.reconcile_failure(args)
            self.assertEqual(first["status"], "FAILURE_RECONCILED")
            self.assertEqual(first["verdict"], "blocked")
            self.assertEqual(first["returncode"], 1)
            self.assertEqual(
                first["evidence_hash"],
                "3855f072e2595799ccc419b437ad6d310c66b70f36b08df811676ee9b32755c8",
            )
            self.assertTrue(second.get("idempotent"))
            self.assertFalse(adapter.exists())
            journal = graph_runner.read(run_dir / "runner" / "journal.json")
            self.assertEqual(
                journal["attempts"]["e8890f8d3d254dda8aa93307bdba544d"]["phase"],
                "FAILURE_RECONCILED",
            )
            self.assertEqual(agent_graph.load_state(run_dir)["nodes"]["node-t-03"]["status"], "blocked")
            self.assertEqual(agent_graph.load_state(run_dir)["nodes"]["node-t-03"]["attempts"], 1)
        restored = {
            relative: hashlib.sha256((original / relative).read_bytes()).hexdigest()
            for relative in pins["pins"]
        }
        self.assertEqual(restored, pins["pins"])


if __name__ == "__main__":
    unittest.main()
