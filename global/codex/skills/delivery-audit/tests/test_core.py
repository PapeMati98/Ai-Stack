"""Observable owner-protocol tests; synthetic state is retained, never deleted."""
import copy
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import uuid
import threading
import unittest
from unittest import mock

SOURCE = Path(__file__).resolve().parents[1] / "scripts" / "audit_core.py"
SPEC = importlib.util.spec_from_file_location("audit_core_under_test", SOURCE)
core = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(core)


def packet(task="task-1", workspace="a", actor=None, provider="codex"):
    result = {
        "schema_version": 1, "task_id": task, "session_id": "parent-session",
        "project_slug": "agent-stack", "harness": "antigravity",
        "task_kind": "delivery", "task_family": "implementation",
        "outcome": "COMPLETED", "workspace_id": workspace * 64,
        "requirements": [{"id": "r1", "status": "pass", "evidence_refs": ["e1"]}],
        "runs": [], "metrics": {
            "wall_seconds": 1.0, "repair_rounds": 0, "duplicate_work": False,
            "uat_required": False, "uat_status": "not-applicable", "evidence_complete": True,
        },
        "delegation": {
            "parent_tier": "D", "eligible": True, "exception": "none", "planned_tasks": 1,
        },
        "observations": [], "evidence_refs": ["e1"],
    }
    if actor:
        result["runs"] = [run(actor=actor, provider=provider)]
    return result


def run(actor="child-1", provider="codex", **changes):
    result = dict(
        run_id="run-" + actor, actor_id=actor, model_id="verified-model",
        provider=provider, tier="W", role="maker", status="completed", verified=True,
        input_tokens=None, cached_input_tokens=None, output_tokens=None, tool_calls=None,
    )
    result.update(changes)
    return result


def observation_packet(task="task-1", workspace="a", actor="actor-1", provider="codex",
                       code="schema-mismatch", component="fleet-adapter:zcode"):
    result = packet(task, workspace, actor, provider)
    result["observations"] = [dict(code=code, component=component, evidence_refs=["e1"])]
    return result


def finding_for(findings, code="schema-mismatch"):
    return next(f for f in findings if f["code"] == code)


class StateCase(unittest.TestCase):
    def setUp(self):
        # Tests only write beneath the explicit synthetic test root.
        parent = Path(os.environ["TEAM_TEST_ROOT"])
        parent.mkdir(parents=True, exist_ok=True)
        self.root = parent / ("core-protocol-" + uuid.uuid4().hex)
        # Python 3.13 mkdtemp mode700 drops inherited restricted-token access.
        self.root.mkdir()
        self.store = core.AuditStore(self.root / "state")

    def complete(self, p, status="COMPLETED", report=None):
        queued = self.store.enqueue(p)
        job = self.store.claim("worker")
        self.assertEqual(job["audit_id"], queued["audit_id"])
        if report is None:
            prior = [record["packet"] for record in self.store.records()]
            report = dict(
                execution_findings=core.execution_findings(p),
                recurrence_findings=core.recurrence_findings(p, prior),
            )
        return self.store.finish(job["audit_id"], job["claim_token"], status, report)


class ValidationTests(StateCase):
    def test_canonical_mapping_is_independent_and_hash_bound(self):
        source = packet()
        before = copy.deepcopy(source)
        result = core.validate_packet(source)
        result["metrics"]["wall_seconds"] = 100
        self.assertEqual(source, before)
        self.assertIsInstance(result, dict)
        reverse = dict(reversed(list(source.items())))
        self.assertEqual(core.audit_id(source), core.audit_id(reverse))
        source["metrics"]["wall_seconds"] = 2
        self.assertNotEqual(core.audit_id(source), core.audit_id(before))

    def test_unknown_private_payload_never_creates_store(self):
        cases = []
        for field in ("raw_prompt", "source_code", "command", "stderr", "mail", "token"):
            p = packet()
            p[field] = "synthetic-private-value"
            cases.append(p)
        p = packet()
        p["runs"] = [dict(run(), stdout="synthetic-private-value")]
        cases.append(p)
        p = packet()
        p["metrics"]["summary"] = "synthetic-private-value"
        cases.append(p)
        for p in cases:
            with self.subTest(fields=list(p)):
                with self.assertRaises(ValueError) as failure:
                    self.store.enqueue(p)
                self.assertNotIn("synthetic-private-value", str(failure.exception))
                self.assertFalse(self.store.root.exists())

    def test_ids_are_opaque_not_paths_contacts_commands_or_secrets(self):
        for value in ("../outside", r"C:\source", "C:source", "//host/share",
                      "https://example.invalid", "a@example.invalid", "192.0.2.1",
                      "run --execute", "x\ninjection", "sk-synthetic-key", "a" * 121):
            p = packet()
            p["task_id"] = value
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    core.validate_packet(p)
        p = packet()
        p["workspace_id"] = r"C:\source"
        with self.assertRaises(ValueError):
            core.validate_packet(p)

    def test_strict_nested_types_enums_and_evidence_namespace(self):
        mutations = [
            lambda p: p.update(schema_version=True),
            lambda p: p["metrics"].update(repair_rounds=True),
            lambda p: p["metrics"].update(duplicate_work="false"),
            lambda p: p["metrics"].update(wall_seconds=-1),
            lambda p: p["metrics"].update(wall_seconds=float("nan")),
            lambda p: p["metrics"].update(wall_seconds=float("inf")),
            lambda p: p["delegation"].update(eligible="unknown"),
            lambda p: p["requirements"][0].update(status="accepted"),
            lambda p: p["requirements"][0].update(evidence_refs=["invented"]),
            lambda p: p.update(observations=[dict(code="schema-mismatch", component="customer-app",
                                                  evidence_refs=["e1"])]),
            lambda p: p.update(evidence_refs=["e1", "e1"]),
            lambda p: p.update(runs=[run(), run()]),
            lambda p: p.update(runs=[run(input_tokens=10, cached_input_tokens=11)]),
        ]
        for mutation in mutations:
            p = packet()
            mutation(p)
            with self.assertRaises(ValueError):
                core.validate_packet(p)

    def test_null_telemetry_stays_unknown(self):
        p = packet(actor="child")
        p["metrics"].update(wall_seconds=None, repair_rounds=None,
                            duplicate_work=None, evidence_complete=None)
        p["delegation"].update(eligible=None, planned_tasks=None)
        clean = core.validate_packet(p)
        self.assertIsNone(clean["metrics"]["evidence_complete"])
        self.assertNotIn("missing-delegation", [f["code"] for f in core.execution_findings(p)])

    def test_depth_cycles_size_and_record_limits_fail_closed(self):
        p = packet()
        cyclic = []
        cyclic.append(cyclic)
        p["raw"] = cyclic
        with self.assertRaises(ValueError):
            core.validate_packet(p)
        p = packet()
        deep = "e"
        for _ in range(13):
            deep = [deep]
        p["raw"] = deep
        with self.assertRaises(ValueError):
            core.validate_packet(p)
        p = packet()
        p["requirements"] = [dict(id="r" + str(i), status="pass", evidence_refs=["e1"])
                             for i in range(257)]
        with self.assertRaises(ValueError):
            core.validate_packet(p)
        p = packet()
        refs = ["x" * 119 + str(i) for i in range(3)]
        p["evidence_refs"] = refs
        p["requirements"] = [
            dict(id="r" + str(i).zfill(3) + "x" * 116, status="pass", evidence_refs=refs)
            for i in range(200)
        ]
        self.assertGreater(len(json.dumps(p)), 65536)
        with self.assertRaises(ValueError):
            core.validate_packet(p)

    def test_skipped_internal_episodes_have_no_store_effect(self):
        for kind in ("audit", "audit-review", "subtask", "status", "metadata-only"):
            p = packet()
            p["task_kind"] = kind
            self.assertFalse(core.should_audit(p))
            self.assertEqual(self.store.enqueue(p)["status"], "SKIPPED")
        p = packet()
        p["delegation"]["exception"] = "worker-node"
        self.assertFalse(core.should_audit(p))
        self.assertFalse(self.store.root.exists())
        for kind in ("delivery", "repair"):
            p["task_kind"] = kind
            p["delegation"]["exception"] = "none"
            self.assertTrue(core.should_audit(p))

    def test_state_paths_refuse_relative_traversal_unc_and_reparse(self):
        for value in ("relative-state", str(self.root) + "/../outside", r"\\host\share"):
            with self.assertRaises(ValueError):
                core.AuditStore(value)
        metadata = mock.Mock(st_mode=0, st_file_attributes=0x400)
        with mock.patch.object(Path, "lstat", return_value=metadata):
            with self.assertRaises(ValueError):
                core.AuditStore(self.root / "junction")

    def test_actual_link_input_target_is_refused(self):
        target = self.root / "link-target"
        target.mkdir()
        link = self.root / "linked-state"
        try:
            link.symlink_to(target, target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest("Host cannot create a synthetic directory symlink")
        with self.assertRaises(ValueError):
            core.AuditStore(link)
        self.assertEqual(list(target.iterdir()), [])


class ExecutionTests(unittest.TestCase):
    def test_declaration_without_receipt_does_not_prove_delegation(self):
        p = packet()
        p["delegation"]["planned_tasks"] = 12
        self.assertIn("missing-delegation", [f["code"] for f in core.execution_findings(p)])
        p["runs"] = [run(actor="actual-worker", provider="z-ai")]
        self.assertNotIn("missing-delegation", [f["code"] for f in core.execution_findings(p)])

    def test_parent_receipt_and_failed_or_ambiguous_child_are_not_child_execution(self):
        p = packet(actor="parent-session")
        self.assertIn("missing-delegation", [f["code"] for f in core.execution_findings(p)])
        p["runs"] = [run(actor="same-actor", role="planner", tier="D"),
                     run(actor="same-actor", run_id="other-run")]
        self.assertIn("missing-delegation", [f["code"] for f in core.execution_findings(p)])
        for changes in (dict(verified=False), dict(status="failed"), dict(provider="unknown"),
                        dict(model_id="unknown"), dict(tier="unknown")):
            p = packet()
            p["runs"] = [run(**changes)]
            self.assertNotIn("missing-delegation", [f["code"] for f in core.execution_findings(p)])

    def test_unknown_ineligible_exceptions_and_worker_tiers_are_not_punished(self):
        for value in (False, None):
            p = packet()
            p["delegation"]["eligible"] = value
            self.assertNotIn("missing-delegation", [f["code"] for f in core.execution_findings(p)])
        for value in (False, None):
            p["metrics"]["evidence_complete"] = value
            self.assertNotIn("missing-delegation", [f["code"] for f in core.execution_findings(p)])
        for exception in ("trivial", "credentials", "irreversible", "capability-unavailable",
                          "quota-exhausted", "explicit-owner-model", "worker-node"):
            p = packet()
            p["delegation"]["exception"] = exception
            self.assertNotIn("missing-delegation", [f["code"] for f in core.execution_findings(p)])
        for tier in ("K", "W", "unknown"):
            p = packet()
            p["delegation"]["parent_tier"] = tier
            self.assertNotIn("missing-delegation", [f["code"] for f in core.execution_findings(p)])

    def test_explicit_behavior_metrics_and_uat_are_advisory(self):
        p = packet(actor="child")
        p["metrics"].update(duplicate_work=True, repair_rounds=2,
                            uat_required=True, uat_status="fail")
        p["requirements"][0]["status"] = "fail"
        codes = {f["code"] for f in core.execution_findings(p)}
        self.assertEqual(codes, {"duplicate-work", "repeat-repair", "missing-checks", "missing-uat"})
        p["metrics"]["uat_status"] = "unknown"
        result = finding_for(core.execution_findings(p), "missing-uat")
        self.assertEqual(result["classification"], "needs-evidence")
        self.assertTrue(all(set(f) == {"classification", "code", "component", "evidence_refs"}
                            for f in core.execution_findings(p)))


class RecurrenceTests(unittest.TestCase):
    def trio(self):
        return [
            observation_packet("task-" + str(i), "a" if i == 1 else "b",
                               "actor-" + str(i), "codex" if i == 1 else "anthropic")
            for i in range(1, 4)
        ]

    def test_three_unique_tasks_two_workspaces_two_verified_provider_actors(self):
        first, second, current = self.trio()
        result = finding_for(core.recurrence_findings(current, [first, second]))
        self.assertEqual(result["classification"], "systemic")
        self.assertEqual(result["unique_tasks"], 3)
        self.assertEqual(result["unique_workspaces"], 2)
        self.assertEqual(result["unique_actors"], 3)
        self.assertEqual(result["unique_providers"], 2)
        self.assertEqual(result["unknown_tasks"], 0)

    def test_replays_same_workspace_actor_or_provider_do_not_inflate_independence(self):
        first, second, current = self.trio()
        result = finding_for(core.recurrence_findings(current, [first] * 10))
        self.assertEqual(result["unique_tasks"], 2)
        self.assertEqual(result["classification"], "task-specific")
        for field in ("workspace_id",):
            trio = self.trio()
            for p in trio:
                p[field] = "a" * 64
            self.assertNotEqual(finding_for(core.recurrence_findings(trio[2], trio[:2]))[
                "classification"], "systemic")
        for field in ("actor_id", "provider"):
            trio = self.trio()
            for p in trio:
                p["runs"][0][field] = "one-identity"
                p["harness"] = "zcode" if p is trio[0] else "antigravity"
            self.assertNotEqual(finding_for(core.recurrence_findings(trio[2], trio[:2]))[
                "classification"], "systemic")

    def test_unknowns_and_failed_models_remain_needs_evidence_with_denominator(self):
        for changes in (dict(status="failed"), dict(verified=False), dict(model_id="unknown")):
            first, second, current = self.trio()
            current["runs"][0].update(changes)
            result = finding_for(core.recurrence_findings(current, [first, second]))
            self.assertEqual(result["classification"], "needs-evidence")
            self.assertEqual(result["total_tasks"], 3)
            self.assertEqual(result["unknown_tasks"], 1)
            self.assertEqual(result["unique_tasks"], 2)
        first, second, current = self.trio()
        first["metrics"]["evidence_complete"] = None
        result = finding_for(core.recurrence_findings(current, [first, second]))
        self.assertEqual(result["classification"], "task-specific")
        self.assertEqual(result["unknown_tasks"], 1)

    def test_conflicting_snapshots_of_same_task_require_evidence(self):
        first, second, current = self.trio()
        replay = copy.deepcopy(first)
        replay["metrics"]["wall_seconds"] = 7
        result = finding_for(core.recurrence_findings(current, [first, replay, second]))
        self.assertEqual(result["unique_tasks"], 2)
        self.assertEqual(result["total_tasks"], 3)
        self.assertEqual(result["unknown_tasks"], 1)
        self.assertNotEqual(result["classification"], "systemic")

    def test_transients_unknown_code_ungrounded_refs_and_component_changes_never_systemic(self):
        for code in ("quota-reset", "provider-transient", "other"):
            trio = self.trio()
            for p in trio:
                p["observations"][0]["code"] = code
            result = finding_for(core.recurrence_findings(trio[2], trio[:2]), code)
            self.assertEqual(result["classification"],
                             "needs-evidence" if code == "other" else "transient")
        first, second, current = self.trio()
        current["observations"][0]["evidence_refs"] = []
        self.assertEqual(finding_for(core.recurrence_findings(current, [first, second]))[
            "classification"], "needs-evidence")
        current["observations"][0].update(component="hub", evidence_refs=["e1"])
        self.assertEqual(finding_for(core.recurrence_findings(current, [first, second]))[
            "unique_tasks"], 1)

    def test_historical_patterns_are_not_reopened_without_current_observation(self):
        first, second, current = self.trio()
        current["observations"] = []
        self.assertEqual(core.recurrence_findings(current, [first, second]), [])
        with self.assertRaises(ValueError):
            core.recurrence_findings(current, [first] * 257)


class StoreTests(StateCase):
    def test_immutable_enqueue_replay_finish_replay_and_pending_transport_survive_restart(self):
        p = packet()
        first = self.store.enqueue(p)
        snapshot = self.store.root / "inputs" / (first["audit_id"] + ".json")
        initial = snapshot.read_bytes()
        for _ in range(3):
            self.assertEqual(core.AuditStore(self.store.root).enqueue(p)["status"], "DUPLICATE")
        self.assertEqual(len(list((self.store.root / "journal").glob("*.json"))), 1)
        job = core.AuditStore(self.store.root).claim("owner")
        self.assertEqual(job["packet"], p)
        self.assertIsNone(self.store.claim("second"))
        report = {"execution_findings": core.execution_findings(p)}
        finished = self.store.finish(first["audit_id"], job["claim_token"], "COMPLETED", report)
        restarted = core.AuditStore(self.store.root)
        self.assertEqual(restarted.finish(first["audit_id"], job["claim_token"],
                                          "COMPLETED", report), finished)
        self.assertEqual(len(restarted.records()), 1)
        self.assertIsNone(restarted.claim("owner"))
        self.assertEqual(snapshot.read_bytes(), initial)
        self.assertEqual(finished["report"]["input_sha256"], first["audit_id"])
        self.assertEqual([sink["status"] for sink in finished["outbox"]], ["PENDING"])
        self.assertEqual(len(list((self.store.root / "journal").glob("*.json"))), 3)

    def test_token_authority_expiry_recovery_and_old_claim_retention(self):
        self.store.enqueue(packet())
        with mock.patch.object(core.time, "time", return_value=100):
            old = self.store.claim("old-owner", 10)
        with mock.patch.object(core.time, "time", return_value=105):
            self.assertIsNone(self.store.claim("new-owner", 10))
        with mock.patch.object(core.time, "time", return_value=111):
            with self.assertRaises(ValueError):
                self.store.finish(old["audit_id"], old["claim_token"], "COMPLETED", {})
            new = self.store.claim("new-owner", 10)
            self.assertNotEqual(new["claim_token"], old["claim_token"])
            with self.assertRaises(ValueError):
                self.store.finish(old["audit_id"], old["claim_token"], "COMPLETED", {})
            self.store.finish(new["audit_id"], new["claim_token"], "COMPLETED", {})
        history = [json.loads(path.read_bytes())
                   for path in (self.store.root / "journal").glob("*.json")]
        owners = {r["claim"]["owner"] for r in history if r["claim"]}
        self.assertEqual(owners, {"old-owner", "new-owner"})

    def test_private_report_wrong_input_and_forged_claim_cannot_commit(self):
        self.store.enqueue(packet())
        job = self.store.claim("owner")
        journals = len(list((self.store.root / "journal").glob("*.json")))
        for report in ({"summary": "synthetic-private-text"}, {"input_sha256": "0" * 64},
                       {"findings": [{"code": "invented", "component": "hub",
                                      "classification": "systemic", "evidence_refs": ["e1"]}]}):
            with self.assertRaises(ValueError):
                self.store.finish(job["audit_id"], job["claim_token"], "COMPLETED", report)
        with self.assertRaises(ValueError):
            self.store.finish(job["audit_id"], "0" * 64, "COMPLETED", {})
        self.assertEqual(len(list((self.store.root / "journal").glob("*.json"))), journals)
        self.assertEqual(self.store.records(), [])
        self.assertIsNone(self.store.claim("other"))

    def test_atomic_publish_crash_replays_input_and_result_without_duplicate_job(self):
        original = core.os.replace
        def crash(source, destination):
            if Path(destination).parent.name == "records":
                raise OSError("synthetic crash before record publish")
            return original(source, destination)
        with mock.patch.object(core.os, "replace", side_effect=crash):
            with self.assertRaises(OSError):
                self.store.enqueue(packet())
        self.assertEqual(self.store.enqueue(packet())["status"], "QUEUED")
        job = self.store.claim("owner")
        with mock.patch.object(core.os, "replace", side_effect=crash):
            with self.assertRaises(OSError):
                self.store.finish(job["audit_id"], job["claim_token"], "COMPLETED", {})
        self.assertEqual(self.store.records(), [])
        self.store.finish(job["audit_id"], job["claim_token"], "COMPLETED", {})
        self.assertEqual(len(self.store.records()), 1)
        self.assertTrue(list((self.store.root / "records").glob("*.tmp")))
        self.assertEqual(self.store.enqueue(packet())["status"], "DUPLICATE")

    def test_corrupt_state_is_retained_and_refused_not_silently_skipped(self):
        queued = self.store.enqueue(packet())
        record = self.store.root / "records" / (queued["audit_id"] + ".json")
        record.write_text("{broken", encoding="ascii")
        for call in (lambda: self.store.enqueue(packet()), lambda: self.store.claim("owner"),
                     lambda: self.store.records()):
            with self.assertRaises(ValueError):
                call()
        self.assertEqual(record.read_text(encoding="ascii"), "{broken")

    def test_failed_results_retained_and_record_limit_is_bounded(self):
        self.complete(packet("failed-task"), status="FAILED", report={})
        self.complete(packet("second-task"), report={})
        self.assertEqual(len(self.store.records(1)), 1)
        self.assertEqual(len(self.store.records(256)), 2)
        self.assertEqual(self.store.records(0), [])
        for limit in (-1, 257, True):
            with self.assertRaises(ValueError):
                self.store.records(limit)

    def test_systemic_outbox_requires_grounded_new_task_and_dedupes_replay(self):
        first = observation_packet("task-a", "a", "actor-a", "codex")
        second = observation_packet("task-b", "b", "actor-b", "anthropic")
        current = observation_packet("task-c", "a", "actor-c", "codex")
        self.complete(first)
        self.complete(second)
        finished = self.complete(current)
        self.assertEqual([s["sink"] for s in finished["outbox"]], ["office", "rsi"])
        prior = self.store.records()
        replay = copy.deepcopy(current)
        replay["metrics"]["wall_seconds"] = 2
        # Even a forged report cannot reopen the same task/pattern.
        forged = finished["report"]
        forged = {key: value for key, value in forged.items() if key != "input_sha256"}
        repeated = self.complete(replay, report=forged)
        self.assertEqual([s["sink"] for s in repeated["outbox"]], ["office"])
        self.assertEqual(len(self.store.records()), len(prior) + 1)

    def test_model_systemic_assertion_alone_never_creates_rsi_sink(self):
        p = observation_packet()
        forged = dict(recurrence_findings=[dict(
            code="schema-mismatch", component="fleet-adapter:zcode", classification="systemic",
            evidence_refs=["e1"], unique_tasks=100, unique_workspaces=100,
            unique_actors=100, unique_providers=100,
        )])
        self.assertEqual([s["sink"] for s in self.complete(p, report=forged)["outbox"]], ["office"])

    def test_threaded_duplicate_enqueues_and_claims_have_one_winner(self):
        barrier = threading.Barrier(8)
        results = []
        failures = []
        def enqueue():
            try:
                barrier.wait(timeout=10)
                results.append(core.AuditStore(self.store.root).enqueue(packet())["status"])
            except Exception as failure:
                failures.append(type(failure).__name__)
        threads = [threading.Thread(target=enqueue) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=20)
            self.assertFalse(thread.is_alive())
        self.assertEqual(failures, [])
        self.assertEqual(results.count("QUEUED"), 1)
        self.assertEqual(results.count("DUPLICATE"), 7)
        results.clear()
        barrier = threading.Barrier(8)
        def claim(index):
            try:
                barrier.wait(timeout=10)
                job = core.AuditStore(self.store.root).claim("owner-" + str(index))
                results.append(job is not None)
            except Exception as failure:
                failures.append(type(failure).__name__)
        threads = [threading.Thread(target=claim, args=(i,)) for i in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=20)
            self.assertFalse(thread.is_alive())
        self.assertEqual(failures, [])
        self.assertEqual(results.count(True), 1)
        self.assertEqual(len(list((self.store.root / "journal").glob("*.json"))), 2)

    def test_exclusive_os_lock_across_hidden_python_processes(self):
        def compete(mode):
            children = []
            for _ in range(4):
                children.append(subprocess.Popen(
                    [sys.executable, "-B", str(Path(__file__).resolve()),
                     "--store-worker", mode, str(self.store.root)],
                    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                ))
            for child in children:
                child.stdin.write(b"go\n")
                child.stdin.flush()
            outputs = []
            codes = []
            for child in children:
                try:
                    output, _ = child.communicate(timeout=30)
                except subprocess.TimeoutExpired:
                    child.kill()
                    child.communicate()
                    raise
                codes.append(child.returncode)
                outputs.append(output.decode("ascii").strip())
            self.assertEqual(codes, [0] * len(children))
            return outputs
        enqueues = compete("enqueue")
        self.assertEqual(enqueues.count("QUEUED"), 1)
        self.assertEqual(enqueues.count("DUPLICATE"), 3)
        claims = compete("claim")
        self.assertEqual(claims.count("claimed"), 1)
        self.assertEqual(claims.count("busy"), 3)


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--store-worker":
        sys.stdin.buffer.readline()
        store = core.AuditStore(Path(sys.argv[3]))
        if sys.argv[2] == "enqueue":
            print(store.enqueue(packet())["status"])
        else:
            print("claimed" if store.claim("process-" + str(os.getpid())) else "busy")
    else:
        unittest.main()
