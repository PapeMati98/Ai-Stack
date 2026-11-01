"""Synthetic completion-sidecar tests; no production state or sinks are used."""
import hashlib
import json
import os
from pathlib import Path
import sys
import unittest
import uuid

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import completion
import producer


TEST_ROOT = Path(os.environ["TEAM_TEST_ROOT"]).resolve() / ("audit-completion-" + uuid.uuid4().hex)
CALLER = "codex"
SESSION = "session-completion-001"
PROJECT = "completion-activation"
ENV = {"CODEX_SESSION_ID": SESSION, "COORDFS_HARNESS": "codex"}


def _write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, sort_keys=True), encoding="utf-8")


def _receipt(root, *, status="COMPLETE", agent=CALLER, session=SESSION,
             project=PROJECT):
    root.mkdir(parents=True, exist_ok=True)
    value = {
        "schema_version": 2, "action": "begin-task", "status": status,
        "agent": agent,
        "session_sha256": hashlib.sha256(session.encode("utf-8")).hexdigest(),
        "project_slug": project,
        "vault_binding": {
            "version": 1, "kind": "local-execution-evidence",
            "producer_authentication": "none", "vault_path_sha256": "b" * 64,
        },
        "health": {"status": "PASS", "lint": "PASS", "lint_error_count": 0},
        "coherent_read": True,
        "execution": {"attempts": [{"status": "COMPLETE", "coherent_read": True}]},
    }
    path = root / "context.json"
    _write_json(path, value)
    return path


def _plan(context_path, *, classified=True, marker="synthetic completion"):
    value = {
        "version": 1, "goal": marker,
        "planner": {"model": "gpt-6-astra", "source": "synthetic"},
        "workspace": str(TEST_ROOT / "workspace"),
        "requirements": ["private requirement marker"],
        "tasks": [{
            "id": "node-impl", "role": "maker", "covers": ["private requirement marker"],
            "checks": [["python", "-B", "-c", "synthetic command marker"]],
        }],
        "max_parallel": 1,
        "context_receipt": str(context_path),
        "context_receipt_agent": CALLER,
        "context_receipt_session": SESSION,
    }
    if classified:
        value["completion"] = {
            "task_kind": "delivery", "task_family": "implementation",
            "episode_role": "top-level", "eligible": None, "exception": "none",
        }
    return value


def _node(run_id="run-20261014T020000Z-b2b2"):
    return {
        "status": "ACCEPTED", "round": 0, "tests": [{"exit_code": 0}],
        "history": [
            {"role": "maker", "result": {"run_id": run_id}},
            {"role": "reviewer", "result": {"run_id": run_id + "c"}},
        ],
    }


def _graph(status="ACCEPTED_CANDIDATE"):
    return {"status": status, "nodes": {"node-impl": _node()}}


class CompletionSidecar(unittest.TestCase):
    def _emit(self, case, *, status="ACCEPTED_CANDIDATE", classified=True,
              uat_required=None, marker="synthetic completion"):
        root = TEST_ROOT / case
        context_root = root / "knowledge-context-receipts"
        context_path = _receipt(context_root)
        outbox = root / "telemetry" / "todo"
        outbox.mkdir(parents=True, exist_ok=True)
        plan = _plan(context_path, classified=classified, marker=marker)
        if classified and uat_required is not None:
            plan["completion"]["uat_required"] = uat_required
        result = completion.emit_completion(
            plan, _graph(status), root / "graph-run", "graph",
            outbox_root=outbox, environ=ENV, context_receipt_root=context_root,
        )
        return root, context_root, outbox, plan, result

    def _emit_two_candidates(self, case):
        root, context_root, outbox, old_plan, old_result = self._emit(
            case, marker="old closeout candidate")
        old_path = outbox / "task-closeouts" / completion._binding_key(
            CALLER, completion._session_hash(SESSION), PROJECT
        ) / (old_result["candidate_id"] + ".json")
        old_candidate = json.loads(old_path.read_text(encoding="utf-8"))
        context_path = context_root / "context.json"
        new_plan = _plan(context_path, marker="new closeout candidate")
        new_result = completion.emit_completion(
            new_plan, _graph(), root / "graph-run", "graph",
            outbox_root=outbox, environ=ENV, context_receipt_root=context_root,
        )
        self.assertIn(new_result["status"], {"EMITTED", "DUPLICATE"})
        new_path = old_path.parent / (new_result["candidate_id"] + ".json")
        new_candidate = json.loads(new_path.read_text(encoding="utf-8"))
        return root, outbox, old_candidate, new_candidate

    def _write_frozen_event(self, outbox, candidate, checkpoint_key, *, forged_packet=False):
        ledger = TEST_ROOT / "empty-ledger-for-frozen-events"
        ledger.mkdir(parents=True, exist_ok=True)
        packet = producer._packet(
            candidate, "COMPLETED", session_id=SESSION, ledger_root=ledger)
        if forged_packet:
            packet["project_slug"] = "forged-project"
            packet = completion.audit_core.validate_packet(packet)
        binding = {"caller_id": CALLER,
                   "session_sha256": completion._session_hash(SESSION),
                   "project_slug": PROJECT}
        event = {
            "schema_version": 1, "event_type": "delivery-audit.completion.v1",
            "caller_id": CALLER, "session_sha256": binding["session_sha256"],
            "project_slug": PROJECT, "plan_sha256": candidate["plan_sha256"],
            "candidate_sha256": candidate["sidecar_sha256"],
            "checkpoint_key": checkpoint_key, "packet": packet,
            "audit_id": completion.audit_core.audit_id(packet),
        }
        event["event_sha256"] = completion._sha(completion._json(event))
        directory = completion._event_dir(candidate, binding, outbox, create=True)
        self.assertIsNotNone(directory)
        _write_json(directory / (checkpoint_key + ".json"), event)
        return event

    def test_accepted_candidate_emits_only_bound_redacted_metadata(self):
        root, _, outbox, _, result = self._emit("accepted-redacted")
        self.assertIn(result["status"], {"EMITTED", "DUPLICATE"})
        files = list((outbox / "task-closeouts").rglob("*.json"))
        self.assertEqual(len(files), 1)
        raw = files[0].read_text(encoding="utf-8")
        sidecar = json.loads(raw)
        self.assertTrue(completion._verify_sidecar(sidecar))
        self.assertEqual(sidecar["caller_id"], CALLER)
        self.assertEqual(sidecar["session_sha256"], hashlib.sha256(SESSION.encode()).hexdigest())
        self.assertEqual(sidecar["project_slug"], PROJECT)
        self.assertIsNone(sidecar["metrics"]["uat_required"])
        self.assertEqual(sidecar["metrics"]["uat_status"], "unknown")
        self.assertIsNone(sidecar["metrics"]["evidence_complete"])
        self.assertEqual(sidecar["requirements"][0]["status"], "pass")
        for private_text in ("private requirement marker", "synthetic command marker",
                             str(root), str(root / "knowledge-context-receipts" / "context.json")):
            self.assertNotIn(private_text, raw)

    def test_duplicate_emission_is_idempotent_and_blocked_graph_does_not_emit(self):
        _, _, outbox, plan, first = self._emit("idempotent")
        context_root = TEST_ROOT / "idempotent" / "knowledge-context-receipts"
        second = completion.emit_completion(
            plan, _graph(), TEST_ROOT / "idempotent" / "graph-run", "graph",
            outbox_root=outbox, environ=ENV, context_receipt_root=context_root,
        )
        self.assertEqual(first["candidate_id"], second["candidate_id"])
        self.assertEqual(second["status"], "DUPLICATE")
        blocked = completion.emit_completion(
            plan, _graph("BLOCKED"), TEST_ROOT / "idempotent" / "graph-run", "graph",
            outbox_root=outbox, environ=ENV, context_receipt_root=context_root,
        )
        self.assertEqual(blocked["code"], "NOT_ACCEPTED")

    def test_explicit_no_uat_is_not_applicable_and_never_a_fake_pass(self):
        _, _, outbox, _, result = self._emit("explicit-no-uat", uat_required=False)
        self.assertIn(result["status"], {"EMITTED", "DUPLICATE"})
        sidecar = json.loads(next((outbox / "task-closeouts").rglob("*.json")).read_text(
            encoding="utf-8"))
        self.assertIs(sidecar["metrics"]["uat_required"], False)
        self.assertEqual(sidecar["metrics"]["uat_status"], "not-applicable")
        self.assertIsNot(sidecar["metrics"]["uat_status"], "pass")

    def test_missing_or_untrusted_classification_skips_without_sidecar(self):
        _, context_root, outbox, plan, result = self._emit("unclassified", classified=False)
        self.assertEqual(result["status"], "SKIPPED_UNCLASSIFIED")
        plan["completion"] = {
            "task_kind": "delivery", "task_family": "implementation",
            "episode_role": "top-level", "eligible": True,
            "exception": "none", "free_text": "sk_fixture_should_not_persist",
        }
        rejected = completion.emit_completion(
            plan, _graph(), TEST_ROOT / "unclassified" / "graph-run", "graph",
            outbox_root=outbox, environ=ENV, context_receipt_root=context_root,
        )
        self.assertEqual(rejected["status"], "SKIPPED_UNCLASSIFIED")
        self.assertEqual(list((outbox / "task-closeouts").rglob("*.json")), [])

    def test_context_status_agent_project_hash_and_native_sid_are_required(self):
        cases = [
            ("bad-status", {"status": "FAILED"}, CALLER, SESSION, PROJECT),
            ("bad-agent", {"agent": "other-agent"}, CALLER, SESSION, PROJECT),
            ("bad-project", {"project": ""}, CALLER, SESSION, PROJECT),
            ("bad-session-hash", {"session": "session-other"}, CALLER, SESSION, PROJECT),
        ]
        for name, overrides, caller, session, project in cases:
            case_root = TEST_ROOT / (name + "-v2")
            context_root = case_root / "knowledge-context-receipts"
            path = _receipt(context_root, status=overrides.get("status", "COMPLETE"),
                            agent=overrides.get("agent", CALLER),
                            project=overrides.get("project", PROJECT),
                            session=overrides.get("session", SESSION))
            plan = _plan(path)
            plan["context_receipt_agent"] = caller
            plan["context_receipt_session"] = session
            outbox = case_root / "telemetry" / "todo"
            outbox.mkdir(parents=True, exist_ok=True)
            result = completion.emit_completion(
                plan, _graph(), case_root, "graph", outbox_root=outbox,
                environ={"CODEX_SESSION_ID": SESSION}, context_receipt_root=context_root,
            )
            self.assertEqual(result["code"], "CONTEXT_BINDING_UNAVAILABLE")
            self.assertEqual(list((outbox / "task-closeouts").rglob("*.json")), [])

        root, context_root, outbox, plan, _ = self._emit("child-sid")
        rejected = completion.emit_completion(
            plan, _graph(), root / "graph-run", "graph", outbox_root=outbox,
            environ={"CODEX_SESSION_ID": "run-20261014T020000Z-b2b2"},
            context_receipt_root=context_root,
        )
        self.assertEqual(rejected["code"], "CONTEXT_BINDING_UNAVAILABLE")

    def test_pair_requires_real_accepted_terminal_and_preserves_pair_source(self):
        root = TEST_ROOT / "pair-path"
        context_root = root / "knowledge-context-receipts"
        context_path = _receipt(context_root)
        outbox = root / "telemetry" / "todo"
        outbox.mkdir(parents=True, exist_ok=True)
        plan = _plan(context_path)
        result = completion.emit_completion(
            plan, {"status": "ACCEPTED", **_node("run-20261014T030000Z-c3c3")},
            root / "pair-run", "pair", outbox_root=outbox,
            environ=ENV, context_receipt_root=context_root,
        )
        self.assertIn(result["status"], {"EMITTED", "DUPLICATE"})
        candidate = next((outbox / "task-closeouts").rglob("*.json"))
        value = json.loads(candidate.read_text(encoding="utf-8"))
        self.assertEqual(value["source"], "todo-pair")
        failed = completion.emit_completion(
            plan, {**_node(), "status": "BLOCKED"}, root / "pair-run", "pair",
            outbox_root=outbox, environ=ENV,
            context_receipt_root=context_root,
        )
        self.assertEqual(failed["code"], "NOT_ACCEPTED")

    def test_valid_old_closeout_is_consumed_before_selecting_one_new_candidate(self):
        _, outbox, old_candidate, new_candidate = self._emit_two_candidates(
            "consumed-then-new")
        self._write_frozen_event(outbox, old_candidate, "a" * 64)
        selected, _, error = completion.resolve_candidate(
            CALLER, SESSION, PROJECT, "b" * 64,
            outbox_root=outbox, environ=ENV,
        )
        self.assertIsNone(error)
        self.assertEqual(selected["plan_sha256"], new_candidate["plan_sha256"])

    def test_matching_old_checkpoint_replays_even_with_pending_new_candidate(self):
        _, outbox, old_candidate, new_candidate = self._emit_two_candidates(
            "replay-with-pending-new")
        self._write_frozen_event(outbox, old_candidate, "c" * 64)
        selected, _, error = completion.resolve_candidate(
            CALLER, SESSION, PROJECT, "c" * 64,
            outbox_root=outbox, environ=ENV,
        )
        self.assertIsNone(error)
        self.assertEqual(selected["plan_sha256"], old_candidate["plan_sha256"])
        self.assertNotEqual(selected["plan_sha256"], new_candidate["plan_sha256"])

    def test_two_unconsumed_candidates_remain_ambiguous(self):
        _, outbox, _, _ = self._emit_two_candidates("two-unconsumed")
        selected, _, error = completion.resolve_candidate(
            CALLER, SESSION, PROJECT, "d" * 64,
            outbox_root=outbox, environ=ENV,
        )
        self.assertIsNone(selected)
        self.assertEqual(error, "SKIPPED_AMBIGUOUS")

    def test_forged_consumed_event_cannot_hide_ambiguity_or_yield_to_new(self):
        _, outbox, old_candidate, _ = self._emit_two_candidates("forged-consumed")
        forged = self._write_frozen_event(
            outbox, old_candidate, "e" * 64, forged_packet=True)
        self.assertTrue(completion._verify_sidecar(old_candidate))
        self.assertFalse(completion._verify_event(forged, old_candidate, "e" * 64))
        selected, _, error = completion.resolve_candidate(
            CALLER, SESSION, PROJECT, "f" * 64,
            outbox_root=outbox, environ=ENV,
        )
        self.assertIsNone(selected)
        self.assertEqual(error, "EVENT_INVALID")


if __name__ == "__main__":
    unittest.main()
