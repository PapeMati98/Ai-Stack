"""Queue/dispatch regressions for ``delivery-audit`` worker.

Scope: only the two findings addressed by this slice.

1. Native launched ``run-one --drain`` must enable the existing safe
   ``inspect/resolve`` recovery path; never blind-replay the Graph or alter any
   foreign process/lease.
2. One blocked oldest audit must never starve subsequent queued delivery
   audits. Use existing bounded record semantics; no busy loops, sleeps, new
   daemons, foreground manual steps, registry changes or deletes.

All tests inject ``native_executor`` and never launch a real model, Office,
Helpdesk or native CLI. Two bounded owned Python test children use the actual
Windows locks; their child code forbids all model/transport subprocesses.
"""
from __future__ import annotations

import copy
from contextlib import contextmanager, redirect_stdout
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock
import uuid
import xml.etree.ElementTree as ET

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
import audit
import audit_graph as graph
import audit_core as core

def state_test_root():
    if "AUDIT_TEST_STATE_ROOT" in os.environ:
        return Path(os.environ["AUDIT_TEST_STATE_ROOT"])
    if "TEAM_TEST_ROOT" in os.environ:
        return Path(os.environ["TEAM_TEST_ROOT"]) / "state"
    return Path(tempfile.gettempdir()) / ("audit-queue-" + uuid.uuid4().hex) / "state"


STATE_TEST_ROOT = state_test_root()
PINS = {"fleet": "present", "graph": "present", "hidden_launch": "present"}


def packet(task="task-1", workspace="a", provider="alpha"):
    return {"schema_version": 1, "task_id": task, "session_id": "owner-session", "project_slug": "test-audit",
            "harness": "codex", "task_kind": "delivery", "task_family": "implementation", "outcome": "COMPLETED",
            "workspace_id": hashlib.sha256(workspace.encode()).hexdigest(),
            "requirements": [{"id": "delivery-check", "status": "pass", "evidence_refs": ["ev-1"]}],
            "runs": [{"run_id": "delivery-" + task, "actor_id": "child-" + task, "model_id": "delivery-model",
                      "provider": provider, "tier": "W", "role": "maker", "status": "completed", "verified": True,
                      "input_tokens": None, "cached_input_tokens": None, "output_tokens": None, "tool_calls": None}],
            "metrics": {"wall_seconds": None, "repair_rounds": None, "duplicate_work": False,
                        "uat_required": False, "uat_status": "not-applicable", "evidence_complete": True},
            "delegation": {"parent_tier": "M", "eligible": True, "exception": "none", "planned_tasks": 1},
            "observations": [{"code": "schema-mismatch", "component": "fleet", "evidence_refs": ["ev-1"]}],
            "evidence_refs": ["ev-1"]}


@contextmanager
def retained(name):
    root = STATE_TEST_ROOT / ("queue-" + name + "-" + uuid.uuid4().hex)
    root.mkdir(parents=True)
    yield root  # Retain everything; no TemporaryDirectory or unlink/rmtree.


def config(root, **overrides):
    value = {"enabled": True, "state": "RUNNING", "test_only": True,
             "office_enabled": False, "rsi_enabled": False}
    value.update(overrides)
    graph.write_json(root / audit.CONFIG_FILENAME, value)
    return audit.RunnerConfig.load(root)


def call(argv, runtime=None):
    output = io.StringIO()
    with redirect_stdout(output):
        code = audit.main(argv, runtime=runtime)
    values = [json.loads(line) for line in output.getvalue().splitlines() if line.strip()]
    return code, values


def owned_scenario(name, root):
    config(root)
    store = core.AuditStore(root / "state")
    store.enqueue(packet("task-1"))
    if name == "generic-drain":
        store.enqueue(packet("task-2"))
    processed = []

    def executor(p, prior, state, **kwargs):
        processed.append(p["task_id"])
        if name == "unknown-finish":
            raise graph.AuditGraphLaunchUnknown()
        if p["task_id"] == "task-1":
            if processed.count("task-1") > 1:
                raise AssertionError("Oldest generic failure replayed inside the same public drain")
            raise ValueError("Synthetic generic native evidence failure")
        return graph.run(p, prior), {"verification_class": "TEST_ONLY"}

    with mock.patch.object(audit, "verify_runtime_pins", return_value=PINS), \
            mock.patch.object(subprocess, "run", side_effect=AssertionError("Live effects forbidden")), \
            mock.patch.object(subprocess, "Popen", side_effect=AssertionError("Live effects forbidden")):
        argv = ["run-one", "--state-dir", str(root), "--recover-interrupted"]
        if name == "generic-drain":
            argv.append("--drain")
        code, values = call(argv, audit.Runtime(native_executor=executor))
    records = {r["packet"]["task_id"]: r for r in audit._read_records(root, finished_only=False)}
    if name == "generic-drain":
        assert code == 0 and processed == ["task-1", "task-2"], (code, processed)
        assert records["task-1"]["status"] == "RUNNING"
        assert records["task-1"]["claim"]["expires_at"] > time.time()
        assert records["task-2"]["status"] == "BLOCKED"
        assert store.claim("immediate-worker") is None
    else:
        assert code == 3 and processed == ["task-1"], (code, processed)
        assert records["task-1"]["status"] == "BLOCKED"
        assert store.claim("future-worker") is None
        assert any(value.get("retryable") is False for value in values)
    print(json.dumps({"scenario": name, "status": "PASS", "processed": processed,
                      "record_statuses": {task: record["status"] for task, record in records.items()},
                      "native_completed": False}))


class OwnedWindowsQueue(unittest.TestCase):
    def bounded_scenario(self, scenario):
        with retained("owned-" + scenario) as root:
            environment = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
            process = subprocess.Popen([sys.executable, "-B", str(Path(__file__).resolve()),
                                        "--owned-scenario", scenario, str(root)],
                                       cwd=str(root), env=environment, stdin=subprocess.DEVNULL,
                                       stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                       creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
            timed_out = False
            try:
                stdout, stderr = process.communicate(timeout=6)
            except subprocess.TimeoutExpired:
                timed_out = True
                process.terminate()  # Only this retained Popen handle; no PID/tree lookup.
                stdout, stderr = process.communicate(timeout=6)
            (root / "owned-stdout.txt").write_bytes(stdout)
            (root / "owned-stderr.txt").write_bytes(stderr)
            self.assertFalse(timed_out, "Owned public worker deadlocked; retained evidence: " + str(root))
            self.assertEqual(process.returncode, 0, stderr.decode("utf-8", errors="replace")[-1500:])
            self.assertEqual(json.loads(stdout)["status"], "PASS")

    def test_public_drain_generic_oldest_failure_does_not_starve_newer_job(self):
        self.bounded_scenario("generic-drain")

    def test_public_unknown_finish_uses_real_locks_without_deadlock(self):
        self.bounded_scenario("unknown-finish")


class NoLiveEffects(unittest.TestCase):
    """No live subprocess may be spawned. Injected executors stay in-process."""

    def setUp(self):
        self.live_run = mock.patch.object(subprocess, "run",
            side_effect=AssertionError("Live subprocess forbidden in candidate test"))
        self.live_popen = mock.patch.object(subprocess, "Popen",
            side_effect=AssertionError("Live model/transport forbidden in candidate test"))
        self.live_run.start()
        self.live_popen.start()
        self.addCleanup(self.live_run.stop)
        self.addCleanup(self.live_popen.stop)


class QueueRepair(NoLiveEffects):
    def test_no_env_default_root_passes_actual_state_guard(self):
        environment = {key: value for key, value in os.environ.items()
                       if key not in {"AUDIT_TEST_STATE_ROOT", "TEAM_TEST_ROOT"}}
        with mock.patch.dict(os.environ, environment, clear=True):
            root = state_test_root()
        root.mkdir(parents=True)
        self.assertTrue(root.is_relative_to(Path(tempfile.gettempdir())))
        self.assertEqual(audit._state_path(str(root)), root.resolve())
        inside_checkout = Path(__file__).resolve().parent
        with mock.patch.dict(os.environ, {"TEAM_TEST_ROOT": str(inside_checkout)}, clear=True):
            self.assertEqual(state_test_root(), inside_checkout / "state")
        with mock.patch.dict(os.environ, {"TEAM_TEST_ROOT": str(root),
                                          "AUDIT_TEST_STATE_ROOT": str(inside_checkout)}, clear=True):
            explicit_root = state_test_root()
        self.assertEqual(explicit_root, inside_checkout)
        with self.assertRaisesRegex(ValueError, "AUDIT_STATE_INSIDE_SOURCE_CHECKOUT"):
            audit._state_path(str(explicit_root))

    def test_drain_runtime_block_without_a_claim_returns_without_spinning(self):
        with retained("no-progress") as root:
            config(root)
            core.AuditStore(root / "state").enqueue(packet("task-1"))
            executor = mock.Mock(side_effect=AssertionError("Pins block dispatch"))
            with mock.patch.object(audit, "verify_runtime_pins", return_value={**PINS, "graph": "fingerprint-mismatch"}) as pins:
                code, _ = call(["run-one", "--state-dir", str(root), "--drain"], audit.Runtime(native_executor=executor))
            self.assertEqual(code, 3)
            pins.assert_called_once()
            executor.assert_not_called()

    def test_backoff_survives_restart_stop_and_later_safe_retry(self):
        with retained("backoff-stop-retry") as root:
            config(root)
            store = core.AuditStore(root / "state")
            store.enqueue(packet("task-1"))
            failure = audit.Runtime(native_executor=mock.Mock(side_effect=ValueError("Synthetic failure")))
            with mock.patch.object(audit, "verify_runtime_pins", return_value=PINS):
                code, values = call(["run-one", "--state-dir", str(root)], failure)
            self.assertEqual(code, 3)
            self.assertGreaterEqual(values[-1]["retry_after_seconds"], 30)
            self.assertLessEqual(values[-1]["retry_after_seconds"], 900)
            record = audit._read_records(root, finished_only=False)[0]
            deadline = record["claim"]["expires_at"]
            self.assertIsNone(core.AuditStore(root / "state").claim("restarted-worker"))
            seen = []
            def success(p, prior, state, **kwargs):
                seen.append(kwargs.get("recover_interrupted"))
                return graph.run(p, prior), {"verification_class": "TEST_ONLY"}
            runtime = audit.Runtime(native_executor=success)
            config(root, state="STOP")
            with mock.patch.object(core.time, "time", return_value=deadline + 1), \
                    mock.patch.object(audit, "verify_runtime_pins", return_value=PINS):
                self.assertEqual(call(["run-one", "--state-dir", str(root), "--recover-interrupted"], runtime)[0], 3)
                self.assertEqual(seen, [])
                config(root)
                self.assertEqual(call(["run-one", "--state-dir", str(root), "--recover-interrupted"], runtime)[0], 0)
            self.assertEqual(seen, [True])
            self.assertEqual(store.records()[0]["status"], "BLOCKED")
            self.assertIsNone(store.claim("finished-cannot-replay"))

    def test_worker_launch_argv_enables_recover_interrupted(self):
        with retained("argv-recover") as root:
            config(root, test_only=False)
            argv = audit.build_launch_argv(root, "a" * 64)
            tree = ET.parse(argv[1])
            action = tree.getroot().find("action")
            arguments = action.attrib["arguments"]
            # The detached worker is the only production worker (cmd_run_one with
            # --drain). It must opt in to the existing safe inspect/resolve recovery
            # so an interrupted Graph state.json recovers through the installed
            # public Graph rather than a blind replay.
            self.assertIn("run-one", arguments)
            self.assertIn("--drain", arguments)
            self.assertIn("--recover-interrupted", arguments)
            # No foreign process/lease change: only the pinned python and our worker.
            self.assertEqual(action.attrib["executable"], sys.executable)
            self.assertIn(str(Path(audit.__file__).resolve()), arguments)
            self.assertEqual(action.attrib["directory"], str(root))

    def test_is_launch_outcome_unknown_matches_canonical_exception(self):
        canonical = ValueError("AUDIT_GRAPH_LAUNCH_OUTCOME_UNKNOWN")
        self.assertTrue(audit._is_launch_outcome_unknown(canonical))
        # Future form exposed as ``AuditGraphLaunchUnknown(ValueError)`` with
        # ``code`` attribute must match without depending on the class symbol.
        typed = type("AuditGraphLaunchUnknown", (ValueError,),
                     {"__init__": lambda self, *a, **kw:
                      ValueError.__init__(self, "AUDIT_GRAPH_LAUNCH_OUTCOME_UNKNOWN")})()
        typed.code = "AUDIT_GRAPH_LAUNCH_OUTCOME_UNKNOWN"
        self.assertTrue(audit._is_launch_outcome_unknown(typed))
        # Retryable failures must NOT be flagged as non-retryable launch-unknown.
        retryable = ValueError("AUDIT_NATIVE_EVIDENCE_UNAVAILABLE")
        self.assertFalse(audit._is_launch_outcome_unknown(retryable))
        unrelated = ValueError("private unrelated failure")
        self.assertFalse(audit._is_launch_outcome_unknown(unrelated))
        self.assertFalse(audit._is_launch_outcome_unknown(KeyError("x")))

    def test_nonretryable_launch_unknown_retains_blocked_record(self):
        with retained("retain-unknown") as root:
            config(root)
            store = core.AuditStore(root / "state")
            store.enqueue(packet("task-1"))
            job = store.claim("fixture-owner")
            self.assertIsNotNone(job)
            returned = audit._retained_launch_unknown(store, job)
            self.assertIsNotNone(returned)
            record = store.records()[0]
            self.assertEqual(record["status"], "BLOCKED")
            # The finished BLOCKED record is no longer claim-eligible: claim() must
            # return None so a later worker cannot blindly re-dispatch the same
            # non-retryable launch-unknown audit.
            self.assertIsNone(store.claim("next-owner"))
            # The journal snapshot chain retains every revision; nothing was dropped.
            snapshots = sorted((store.root / "journal").glob(record["audit_id"] + ".*.json"))
            self.assertGreaterEqual(len(snapshots), 2)

    def test_oldest_failure_then_newer_job_progresses_in_same_drain(self):
        with retained("drain-nonretryable") as root:
            config(root)
            store = core.AuditStore(root / "state")
            store.enqueue(packet("task-1"))  # oldest, fails non-retryably
            store.enqueue(packet("task-2"))  # newer, must progress
            processed = []

            def executor(p, prior, state, **kwargs):
                processed.append(p["task_id"])
                if p["task_id"] == "task-1":
                    raise ValueError("AUDIT_GRAPH_LAUNCH_OUTCOME_UNKNOWN")
                return graph.run(p, prior), {"verification_class": "TEST_ONLY"}

            with mock.patch.object(audit, "verify_runtime_pins", return_value=PINS):
                code, values = call(["run-one", "--state-dir", str(root), "--drain"],
                                    audit.Runtime(native_executor=executor))
            # Both tasks were claimed by the same drain; no starvation, no replay.
            self.assertEqual(processed, ["task-1", "task-2"])
            self.assertEqual(code, 0)
            # task-1 is retained BLOCKED with the explicit non-retryable code;
            # task-2 was processed successfully under the test_only path.
            by_task = {r["packet"]["task_id"]: r for r in audit._read_records(root, finished_only=False)}
            self.assertEqual(by_task["task-1"]["status"], "BLOCKED")
            self.assertEqual(by_task["task-2"]["status"], "BLOCKED")  # test_only path settles BLOCKED
            blocked_payload = next(value for value in values
                                   if value.get("code") == "AUDIT_GRAPH_LAUNCH_OUTCOME_UNKNOWN"
                                   and value.get("audit_id") == by_task["task-1"]["audit_id"])
            self.assertEqual(blocked_payload["retryable"], False)
            self.assertEqual(blocked_payload["recovery"], "inspect-owned-attempt")

    def test_unknown_launch_failure_cannot_blindly_replay(self):
        with retained("unknown-no-replay") as root:
            config(root)
            store = core.AuditStore(root / "state")
            store.enqueue(packet("task-1"))
            store.enqueue(packet("task-2"))
            counter = {"task-1": 0, "task-2": 0}

            def executor(p, prior, state, **kwargs):
                counter[p["task_id"]] += 1
                if p["task_id"] == "task-1":
                    raise ValueError("AUDIT_GRAPH_LAUNCH_OUTCOME_UNKNOWN")
                return graph.run(p, prior), {"verification_class": "TEST_ONLY"}

            with mock.patch.object(audit, "verify_runtime_pins", return_value=PINS):
                call(["run-one", "--state-dir", str(root), "--drain"],
                     audit.Runtime(native_executor=executor))
            self.assertEqual(counter["task-1"], 1, "non-retryable launch-unknown must not be replayed")
            self.assertEqual(counter["task-2"], 1, "newer delivery audit must run exactly once")
            # A future worker cannot re-dispatch the retained launch-unknown.
            self.assertIsNone(store.claim("next-worker"))

    def test_stop_during_drain_still_prevents_all_dispatch(self):
        with retained("drain-stop-gate") as root:
            config(root)
            store = core.AuditStore(root / "state")
            store.enqueue(packet("task-1"))
            store.enqueue(packet("task-2"))
            processed = []

            def executor(p, prior, state, **kwargs):
                if not processed:
                    # Switch to STOP between iterations; the controller must respect
                    # the gate before any further dispatch.
                    config(root, state="STOP")
                processed.append(p["task_id"])
                return graph.run(p, prior), {"verification_class": "TEST_ONLY"}

            with mock.patch.object(audit, "verify_runtime_pins", return_value=PINS):
                code, _ = call(["run-one", "--state-dir", str(root), "--drain"],
                               audit.Runtime(native_executor=executor))
            # Only the first task was claimed before STOP landed; task-2 stays QUEUED.
            self.assertEqual(processed, ["task-1"])
            self.assertEqual(code, 3)
            queued = [r["packet"]["task_id"] for r in audit._read_records(root, finished_only=False)
                      if r["status"] == "QUEUED"]
            self.assertEqual(queued, ["task-2"])
            self.assertFalse(audit.RunnerConfig.load(root).is_running())

    def test_retryable_transient_failure_keeps_claim_eligibility(self):
        """A retryable failure must NOT block newer QUEUED jobs and must remain
        re-claimable by a future worker via existing record semantics."""
        with retained("retry-eligibility") as root:
            config(root)
            store = core.AuditStore(root / "state")
            store.enqueue(packet("task-1"))
            failure = audit.Runtime(
                native_executor=mock.Mock(side_effect=ValueError("private unrelated failure")))
            with mock.patch.object(audit, "verify_runtime_pins", return_value=PINS):
                code, values = call(["run-one", "--state-dir", str(root)], failure)
            self.assertEqual(code, 3)
            retryable_payload = next(value for value in values if value.get("retryable") is True)
            self.assertEqual(retryable_payload["code"], "AUDIT_NATIVE_EVIDENCE_UNAVAILABLE")
            self.assertFalse(any(value.get("retryable") is False for value in values))
            self.assertIsNone(store.claim("before-backoff-worker"))
            deferred = audit._read_records(root, finished_only=False)[0]
            deadline = deferred["claim"]["expires_at"]
            self.assertGreater(deadline, time.time())
            with mock.patch.object(core.time, "time", return_value=deadline + 1):
                job = store.claim("next-worker")
            self.assertIsNotNone(job)
            self.assertEqual(job["status"], "RUNNING")
            self.assertEqual(job["packet"]["task_id"], "task-1")


if __name__ == "__main__":
    if len(sys.argv) == 4 and sys.argv[1] == "--owned-scenario":
        owned_scenario(sys.argv[2], core._safe_path(sys.argv[3]))
    else:
        unittest.main()
