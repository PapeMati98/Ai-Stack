"""Synthetic Hub-closeout producer tests; all sinks are injected callbacks."""
import copy
import hashlib
import json
import os
from pathlib import Path
import sys
import unittest
import uuid
from unittest import mock

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import audit_core
import completion
import producer


TEST_ROOT = Path(os.environ["TEAM_TEST_ROOT"]).resolve() / ("audit-producer-" + uuid.uuid4().hex)
CALLER = "codex"
SESSION = "session-producer-001"
PROJECT = "producer-activation"
CHECKPOINT = "checkpoint-producer-001"
ENV = {"CODEX_SESSION_ID": SESSION, "COORDFS_HARNESS": "codex"}


def _write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, sort_keys=True), encoding="utf-8")


def _context(root, session=SESSION, project=PROJECT, agent=CALLER):
    root.mkdir(parents=True, exist_ok=True)
    value = {
        "schema_version": 2,
        "action": "begin-task",
        "status": "COMPLETE",
        "agent": agent,
        "session_sha256": hashlib.sha256(session.encode("utf-8")).hexdigest(),
        "project_slug": project,
        "vault_binding": {
            "version": 1, "kind": "local-execution-evidence",
            "producer_authentication": "none", "vault_path_sha256": "a" * 64,
        },
        "health": {"status": "PASS", "lint": "PASS", "lint_error_count": 0},
        "coherent_read": True,
        "execution": {"attempts": [{"status": "COMPLETE", "coherent_read": True}]},
    }
    path = root / "context.json"
    _write_json(path, value)
    return path


def _plan(receipt_path, *, session=SESSION, project=PROJECT, agent=CALLER,
          task_kind="delivery", episode_role="top-level", classified=True,
          marker="synthetic-plan", uat_required=None):
    task = {
        "id": "node-build", "role": "maker", "covers": ["synthetic requirement"],
        "checks": [["python", "-B", "-c", "pass"]],
    }
    plan = {
        "version": 1, "goal": marker,
        "planner": {"model": "gpt-6-astra", "source": "synthetic"},
        "workspace": str(TEST_ROOT / "synthetic-workspace"),
        "requirements": ["synthetic requirement"], "tasks": [task], "max_parallel": 1,
        "context_receipt": str(receipt_path),
        "context_receipt_agent": agent, "context_receipt_session": session,
    }
    if classified:
        plan["completion"] = {
            "task_kind": task_kind, "task_family": "implementation",
            "episode_role": episode_role, "eligible": None, "exception": "none",
        }
        if uat_required is not None:
            plan["completion"]["uat_required"] = uat_required
    return plan


def _accepted_result(run_id="run-20261014T010000Z-a1a1"):
    return {
        "status": "ACCEPTED_CANDIDATE",
        "nodes": {
            "node-build": {
                "status": "ACCEPTED", "round": 0,
                "tests": [{"exit_code": 0}],
                "history": [
                    {"role": "maker", "result": {"run_id": run_id}},
                    {"role": "reviewer", "result": {"run_id": run_id + "b"}},
                ],
            },
        },
    }


def _make_candidate(case="default", *, session=SESSION, project=PROJECT, agent=CALLER,
                    task_kind="delivery", episode_role="top-level", classified=True,
                    marker="synthetic-plan", uat_required=None):
    root = TEST_ROOT / case
    context_root = root / "knowledge-context-receipts"
    receipt_path = _context(context_root, session, project, agent)
    plan = _plan(receipt_path, session=session, project=project, agent=agent,
                 task_kind=task_kind, episode_role=episode_role, classified=classified,
                 marker=marker, uat_required=uat_required)
    outbox = root / "telemetry" / "todo"
    outbox.mkdir(parents=True, exist_ok=True)
    emitted = completion.emit_completion(
        plan, _accepted_result(), root / "graph-run", "graph",
        outbox_root=outbox, environ={"CODEX_SESSION_ID": session, "COORDFS_HARNESS": "codex"},
        context_receipt_root=context_root,
    )
    return root, context_root, receipt_path, outbox, plan, emitted


def _closeout(session=SESSION, project=PROJECT, agent=CALLER, checkpoint=CHECKPOINT,
              outcome="COMPLETED", receipt_status="COMPLETE"):
    checkpoint_key = producer._hub_checkpoint_key(agent, project, session, checkpoint)
    return {
        "action": "record-session", "receipt_status": receipt_status,
        "outcome": outcome, "agent": agent, "project_slug": project,
        "session_sha256": hashlib.sha256(session.encode("utf-8")).hexdigest(),
        "checkpoint_key": checkpoint_key, "work_success_attested": False,
    }


def _ledger(root, run_id, *, status="COMPLETED", forged_verified=None,
            output_dir=None, execution_workspace=None, workspace=None):
    record = {
        "run_id": run_id, "agent": "codex", "model": "gpt-6-astra",
        "model_provider_id": "openai", "run_status": status,
        "exit_code": 0 if status == "COMPLETED" else 2,
        "protocol_terminal_completed": status == "COMPLETED",
        "provider_terminal_reason": "succeeded" if status == "COMPLETED" else "error",
        "runtime_attestation": {"status": "VERIFIED"},
        "model_identity": {"status": "verified"},
        "input_tokens": 12, "cached_input_tokens": None,
        "output_tokens": 4, "tool_calls": None,
        "output_dir": str(output_dir) if output_dir is not None else None,
        "execution_workspace": str(execution_workspace) if execution_workspace is not None else None,
        "workspace": str(workspace) if workspace is not None else None,
    }
    if forged_verified is not None:
        record["verified"] = forged_verified
    _write_json(root / (run_id + ".json"), record)
    return record


def _candidate_from(outbox):
    files = [path for path in (outbox / "task-closeouts").rglob("*.json")
             if path.parent.suffix != ".closeouts"]
    if len(files) != 1:
        raise AssertionError(f"expected one synthetic sidecar, got {len(files)}")
    return json.loads(files[0].read_text(encoding="utf-8"))


def _bind_ledgers(root, ledger, candidate, plan, *, statuses=None):
    statuses = statuses or {}
    for run in candidate["runs"]:
        expected_dir = (root / "graph-run" / "node-build" / "1" /
                        run["scheduler_role"] / "fleet" / run["run_id"])
        _ledger(ledger, run["run_id"], status=statuses.get(run["run_id"], "COMPLETED"),
                output_dir=expected_dir, execution_workspace=plan["workspace"],
                workspace=plan["workspace"])


class ProducerActivation(unittest.TestCase):
    def test_closeout_joins_bound_native_metadata_and_ignores_forged_verified(self):
        case = "bound-native-proof"
        root, _, _, outbox, plan, emitted = _make_candidate(case)
        self.assertIn(emitted["status"], {"EMITTED", "DUPLICATE"})
        candidate = _candidate_from(outbox)
        run_ids = [run["run_id"] for run in candidate["runs"]]
        ledger = root / "synthetic-fleet-ledger"
        ledger.mkdir(parents=True, exist_ok=True)
        _bind_ledgers(root, ledger, candidate, plan,
                      statuses={run_ids[1]: "PARTIAL"})
        good_path = ledger / (run_ids[0] + ".json")
        good_record = json.loads(good_path.read_text(encoding="utf-8"))
        good_record["verified"] = False
        _write_json(good_path, good_record)
        partial_path = ledger / (run_ids[1] + ".json")
        partial_record = json.loads(partial_path.read_text(encoding="utf-8"))
        partial_record["verified"] = True
        _write_json(partial_path, partial_record)
        proved = producer._verify_native_runs(
            candidate["runs"], ledger_root=ledger,
            workspace_id=candidate["workspace_id"],
        )
        self.assertIs(proved[0]["verified"], True)
        self.assertEqual(proved[0]["tier"], "D")
        self.assertIs(proved[1]["verified"], False)

        sent = []
        result = producer.after_hub_closeout(
            _closeout(), CALLER, SESSION, PROJECT,
            {"packet": {"task_kind": "status", "secret": "sk_fixture"}},
            checkpoint_id=CHECKPOINT, outbox_root=outbox, ledger_root=ledger,
            environ=ENV,
            dispatch=lambda packet: (sent.append(packet) or {"status": "QUEUED"}),
        )
        self.assertEqual(result["status"], "QUEUED", result)
        self.assertEqual(sent[0]["task_kind"], "delivery")
        self.assertEqual(sent[0]["session_id"], SESSION)
        self.assertNotIn("sk_fixture", json.dumps(sent[0]))
        self.assertIsNone(sent[0]["metrics"]["uat_required"])
        self.assertEqual(sent[0]["metrics"]["uat_status"], "unknown")
        self.assertIsNone(sent[0]["metrics"]["evidence_complete"])
        self.assertEqual(sent[0]["runs"][0]["role"], "maker")
        self.assertEqual(sent[0]["runs"][0]["input_tokens"], 12)
        self.assertIsNone(sent[0]["runs"][0]["cached_input_tokens"])
        self.assertEqual(len(sent), 1)

    def test_workspace_and_role_or_output_folder_mismatch_never_verify(self):
        root, _, _, outbox, plan, _ = _make_candidate("workspace-role-binding")
        candidate = _candidate_from(outbox)
        run = candidate["runs"][0]
        ledger = root / "synthetic-fleet-ledger"
        ledger.mkdir(parents=True, exist_ok=True)
        expected_dir = (root / "graph-run" / "node-build" / "1" /
                        run["scheduler_role"] / "fleet" / run["run_id"])

        _ledger(ledger, run["run_id"], output_dir=expected_dir,
                execution_workspace=str(TEST_ROOT / "foreign-workspace"),
                workspace=str(TEST_ROOT / "foreign-workspace"))
        wrong_workspace = producer._verify_native_runs(
            [run], ledger_root=ledger, workspace_id=candidate["workspace_id"])
        self.assertIs(wrong_workspace[0]["verified"], False)

        _ledger(ledger, run["run_id"], output_dir=(
                    root / "graph-run" / "node-build" / "1" / "reviewer" / "fleet" / run["run_id"]),
                execution_workspace=plan["workspace"], workspace=plan["workspace"])
        wrong_folder = producer._verify_native_runs(
            [run], ledger_root=ledger, workspace_id=candidate["workspace_id"])
        self.assertIs(wrong_folder[0]["verified"], False)

        tampered_role = dict(run, scheduler_role="reviewer")
        role_tamper = producer._verify_native_runs(
            [tampered_role], ledger_root=ledger, workspace_id=candidate["workspace_id"])
        self.assertIs(role_tamper[0]["verified"], False)

    def test_explicit_non_uat_contract_can_complete_without_fabricated_uat_pass(self):
        root, _, _, outbox, plan, _ = _make_candidate(
            "explicit-no-uat", uat_required=False)
        candidate = _candidate_from(outbox)
        ledger = root / "synthetic-fleet-ledger"
        ledger.mkdir(parents=True, exist_ok=True)
        _bind_ledgers(root, ledger, candidate, plan)
        sent = []
        result = producer.after_hub_closeout(
            _closeout(), CALLER, SESSION, PROJECT, checkpoint_id=CHECKPOINT,
            outbox_root=outbox, ledger_root=ledger, environ=ENV,
            dispatch=lambda packet: sent.append(packet) or {"status": "QUEUED"},
        )
        self.assertEqual(result["status"], "QUEUED", result)
        self.assertIs(sent[0]["metrics"]["uat_required"], False)
        self.assertEqual(sent[0]["metrics"]["uat_status"], "not-applicable")
        self.assertIsNot(sent[0]["metrics"]["uat_status"], "pass")
        self.assertIs(sent[0]["metrics"]["evidence_complete"], True)

    def test_evidence_complete_requires_nonempty_fully_bound_completed_native_runs(self):
        root, _, _, outbox, plan, _ = _make_candidate(
            "evidence-complete-run-gate", uat_required=False)
        candidate = _candidate_from(outbox)
        valid_ledger = root / "synthetic-ledger-valid"
        valid_ledger.mkdir(parents=True, exist_ok=True)
        _bind_ledgers(root, valid_ledger, candidate, plan)

        valid_packet = producer._packet(
            candidate, "COMPLETED", session_id=SESSION, ledger_root=valid_ledger)
        self.assertIs(valid_packet["metrics"]["evidence_complete"], True)
        self.assertFalse(any(item["code"] == "missing-checks"
                             for item in audit_core.execution_findings(valid_packet)))

        cases = ("missing-ledger", "partial-terminal", "wrong-workspace",
                 "wrong-role-folder", "no-claimed-runs")
        for case in cases:
            ledger = root / ("synthetic-ledger-" + case)
            ledger.mkdir(parents=True, exist_ok=True)
            candidate_value = copy.deepcopy(candidate)
            first = candidate["runs"][0]
            expected_dir = (root / "graph-run" / "node-build" / "1" /
                            first["scheduler_role"] / "fleet" / first["run_id"])
            if case != "missing-ledger":
                statuses = {first["run_id"]: "PARTIAL"} if case == "partial-terminal" else None
                _bind_ledgers(root, ledger, candidate, plan, statuses=statuses)
            if case == "wrong-workspace":
                _ledger(ledger, first["run_id"], output_dir=expected_dir,
                        execution_workspace=str(TEST_ROOT / "foreign-workspace"),
                        workspace=str(TEST_ROOT / "foreign-workspace"))
            elif case == "wrong-role-folder":
                _ledger(ledger, first["run_id"], output_dir=(
                    root / "graph-run" / "node-build" / "1" / "reviewer" / "fleet" / first["run_id"]),
                    execution_workspace=plan["workspace"], workspace=plan["workspace"])
            elif case == "no-claimed-runs":
                candidate_value["runs"] = []

            packet = producer._packet(
                candidate_value, "COMPLETED", session_id=SESSION, ledger_root=ledger)
            self.assertIsNone(packet["metrics"]["evidence_complete"], case)
            self.assertTrue(any(item["code"] == "missing-checks"
                                for item in audit_core.execution_findings(packet)), case)

    def test_receipt_binding_and_terminal_closeout_gates_precede_dispatch(self):
        _, _, _, outbox, _, _ = _make_candidate("closeout-gates")
        calls = []
        cases = [
            (_closeout(agent="other-agent"), CALLER, SESSION, PROJECT, CHECKPOINT),
            (_closeout(outcome="BLOCKED"), CALLER, SESSION, PROJECT, CHECKPOINT),
            (_closeout(receipt_status="PENDING"), CALLER, SESSION, PROJECT, CHECKPOINT),
            (_closeout(), CALLER, "session-other", PROJECT, CHECKPOINT),
            (_closeout(project="other-project"), CALLER, SESSION, PROJECT, CHECKPOINT),
            (_closeout(), CALLER, SESSION, PROJECT, "checkpoint-other"),
        ]
        for receipt_value, caller, session, project, checkpoint in cases:
            result = producer.after_hub_closeout(
                receipt_value, caller, session, project,
                checkpoint_id=checkpoint, outbox_root=outbox, environ=ENV,
                dispatch=lambda packet: calls.append(packet) or {"status": "QUEUED"},
            )
            self.assertTrue(result["status"].startswith("SKIPPED"))
        self.assertEqual(calls, [])

    def test_native_session_environment_and_receipt_source_allowlist_reject_mismatch(self):
        root = TEST_ROOT / "receipt-allowlist"
        allowed = root / "knowledge-context-receipts"
        receipt_path = _context(allowed)
        plan = _plan(receipt_path)
        outbox = root / "telemetry" / "todo"
        outbox.mkdir(parents=True, exist_ok=True)
        mismatch = completion.emit_completion(
            plan, _accepted_result(), root, "graph", outbox_root=outbox,
            environ={"CODEX_SESSION_ID": "provider-child-run"},
            context_receipt_root=allowed,
        )
        self.assertEqual(mismatch["code"], "CONTEXT_BINDING_UNAVAILABLE")

        outside = root / "outside.json"
        _write_json(outside, json.loads(receipt_path.read_text(encoding="utf-8")))
        plan["context_receipt"] = str(outside)
        rejected = completion.emit_completion(
            plan, _accepted_result(), root, "graph", outbox_root=outbox,
            environ=ENV, context_receipt_root=allowed,
        )
        self.assertEqual(rejected["code"], "CONTEXT_BINDING_UNAVAILABLE")

    def test_role_kind_and_unclassified_candidates_never_dispatch(self):
        cases = [
            ("worker-role", "delivery", "worker", True, "ROLE_EXCLUDED"),
            ("audit-kind", "audit", "top-level", True, "ROLE_EXCLUDED"),
            ("status-kind", "status", "top-level", True, "ROLE_EXCLUDED"),
            ("unclassified", "delivery", "top-level", False, "SIDECAR_UNAVAILABLE"),
        ]
        calls = []
        for case, kind, role, classified, expected in cases:
            session = "session-" + case
            project = "project-" + case
            root, _, _, outbox, _, emitted = _make_candidate(
                case, session=session, project=project, task_kind=kind,
                episode_role=role, classified=classified,
            )
            if not classified:
                self.assertEqual(emitted["status"], "SKIPPED_UNCLASSIFIED")
            result = producer.after_hub_closeout(
                _closeout(session, project), CALLER, session, project,
                checkpoint_id=CHECKPOINT, outbox_root=outbox,
                environ={"CODEX_SESSION_ID": session},
                dispatch=lambda packet: calls.append(packet) or {"status": "QUEUED"},
            )
            self.assertEqual(result["code"], expected, result)
        self.assertEqual(calls, [])

    def test_multiple_unconsumed_candidates_for_same_session_are_ambiguous(self):
        root = TEST_ROOT / "ambiguous-candidates"
        context_root = root / "knowledge-context-receipts"
        receipt_path = _context(context_root)
        outbox = root / "telemetry" / "todo"
        outbox.mkdir(parents=True, exist_ok=True)
        for marker in ("plan-one", "plan-two"):
            plan = _plan(receipt_path, marker=marker)
            emitted = completion.emit_completion(
                plan, _accepted_result(), root / marker, "graph", outbox_root=outbox,
                environ=ENV, context_receipt_root=context_root,
            )
            self.assertIn(emitted["status"], {"EMITTED", "DUPLICATE"})
        calls = []
        result = producer.after_hub_closeout(
            _closeout(), CALLER, SESSION, PROJECT, checkpoint_id=CHECKPOINT,
            outbox_root=outbox, environ=ENV,
            dispatch=lambda packet: calls.append(packet) or {"status": "QUEUED"},
        )
        self.assertEqual(result["status"], "SKIPPED_AMBIGUOUS")
        self.assertEqual(calls, [])

    def test_replay_identity_and_dispatch_failure_keep_final_receipt_unchanged(self):
        root, _, _, outbox, plan, _ = _make_candidate("idempotent-closeout")
        candidate = _candidate_from(outbox)
        ledger = root / "synthetic-fleet-ledger"
        ledger.mkdir(parents=True, exist_ok=True)
        _bind_ledgers(root, ledger, candidate, plan)
        final_receipt = _closeout()
        before = copy.deepcopy(final_receipt)
        failed = producer.after_hub_closeout(
            final_receipt, CALLER, SESSION, PROJECT, checkpoint_id=CHECKPOINT,
            outbox_root=outbox, ledger_root=ledger, environ=ENV,
            dispatch=lambda packet: (_ for _ in ()).throw(RuntimeError("synthetic failure")),
        )
        self.assertEqual(failed["status"], "ENQUEUE_FAILED", failed)
        self.assertEqual(final_receipt, before)

        sent = []
        first = producer.after_hub_closeout(
            final_receipt, CALLER, SESSION, PROJECT, checkpoint_id=CHECKPOINT,
            outbox_root=outbox, ledger_root=ledger, environ=ENV,
            dispatch=lambda packet: (sent.append(packet) or {"status": "QUEUED"}),
        )
        second = producer.after_hub_closeout(
            final_receipt, CALLER, SESSION, PROJECT, checkpoint_id=CHECKPOINT,
            outbox_root=outbox, ledger_root=ledger, environ=ENV,
            dispatch=lambda packet: (sent.append(packet) or {"status": "DUPLICATE"}),
        )
        self.assertEqual(first["audit_id"], second["audit_id"])
        self.assertEqual(first["audit_id"], audit_core.audit_id(sent[0]))
        self.assertEqual(audit_core.audit_id(sent[0]), audit_core.audit_id(sent[1]))
        self.assertEqual(final_receipt["receipt_status"], "COMPLETE")
        self.assertEqual(final_receipt["outcome"], "COMPLETED")

    def test_privacy_rejection_happens_before_any_enqueue_process(self):
        invalid = {"schema_version": 1, "task_id": "sk_test-secret"}
        with mock.patch.object(producer.subprocess, "run") as native_run:
            with self.assertRaises(ValueError):
                producer._production_enqueue(invalid)
        native_run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
