#!/usr/bin/env python3
"""SPEC N1 contract tests. Fixtures are retained outside the skill tree.

Run with PYTHONDONTWRITEBYTECODE=1 (or python -B) and -p no:cacheprovider
so collection, imports and subprocesses cannot leave bytecode in sources.
"""
from __future__ import annotations
import sys
sys.dont_write_bytecode = True

import contextlib
import hashlib
import importlib.util
import io
import json
import os
import subprocess
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

SCRIPT = Path(__file__).resolve().with_name("loop_ledger.py")
SKILL_TREE = SCRIPT.parents[1]


def tree_snapshot():
    result = {}
    for path in SKILL_TREE.rglob("*"):
        relative = path.relative_to(SKILL_TREE).as_posix()
        if path.is_symlink():
            result[relative] = "link"
        elif path.is_file():
            result[relative] = hashlib.sha256(path.read_bytes()).hexdigest()
        elif path.is_dir():
            result[relative] = "directory"
    return result


# Capture before loading BOTH production modules, including all bytecode paths.
BEFORE_IMPORTS = tree_snapshot()


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


GUARD = load("loop_guard", SCRIPT.with_name("loop_guard.py"))
LEDGER = load("office_loop_ledger_test_target", SCRIPT)
REQS = ["Requirement Alpha", "Requirement Beta"]
NONLEASED = {"init", "acquire", "status", "reconcile", "observation", "stop"}


def tearDownModule():
    if tree_snapshot() != BEFORE_IMPORTS:
        raise AssertionError("skill tree changed during imports/collection/tests, including bytecode")


def evidence_root():
    override = os.environ.get("AGENT_TEST_ROOT")
    base = Path(override) / "office-loop" if override else Path.home() / ".agents" / "test-runs" / "office-loop"
    if not base.is_absolute() or LEDGER.inside(base.resolve(), LEDGER.SKILL_TREE):
        raise AssertionError("test fixtures must be outside skill sources")
    base.mkdir(parents=True, exist_ok=True)
    return base


def cli(argv):
    stdout, stderr = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
        code = LEDGER.main(argv)
    assert stderr.getvalue() == "", stderr.getvalue()
    assert len(stdout.getvalue().splitlines()) == 1, stdout.getvalue()
    result = json.loads(stdout.getvalue())
    assert isinstance(result, dict) and result["release_authority"] is False
    return code, result


class Case:
    def __init__(self, config=None, initialize=True):
        self.base = Path(tempfile.mkdtemp(prefix="ledger-", dir=evidence_root()))
        self.ws = self.base / "workspace with spaces"
        self.ws.mkdir()
        (self.ws / "app.py").write_text("alpha", encoding="utf-8")
        (self.ws / "src").mkdir()
        (self.ws / "src" / "app.py").write_text("bravo", encoding="utf-8")
        self.root, self.gid, self.token, self.serial = self.base / "state", "n1-goal", None, 0
        self.goal = {"version": 1, "goal_id": self.gid, "goal": "Deliver a verified Office loop",
                     "workspace": str(self.ws), "requirements": REQS[:],
                     "constraints": ["Synthetic inputs only"], "allowed_scope": ["app.py", "src/"]}
        self.goal_file = self.write("goal", self.goal)
        self.cfg_file = self.write("config", config) if config is not None else None
        if initialize:
            self.initialize()
            self.token = self.call("acquire")["token"]

    def write(self, name, value):
        self.serial += 1
        target = self.base / (name + "-" + str(self.serial) + ".json")
        target.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
        return str(target)

    def argv(self, command, *extra, token=True):
        args = [command, "--root", str(self.root), "--goal-id", self.gid]
        if token and command not in NONLEASED:
            args += ["--token", self.token or "no-token"]
        return args + list(extra)

    def call(self, command, *extra, error=None, token=True):
        code, result = cli(self.argv(command, *extra, token=token))
        if error is None:
            assert code == 0, result
            assert result["status"] == "OK", result
        else:
            assert code != 0 and result["status"] == "ERROR", result
            if error != "ANY":
                assert result["code"] == error, result
        return result

    def initialize(self):
        args = ["--goal", self.goal_file]
        if self.cfg_file:
            args += ["--config", self.cfg_file]
        return self.call("init", *args)

    def state(self):
        return json.loads((self.root / self.gid / "state.json").read_text(encoding="utf-8"))

    def report(self, **changes):
        status = self.call("status")
        old = self.state()["review"] or {}
        report = {"round": status["round"], "summary": "Independent synthetic review",
                  "reconciliation": "Reconciled against all original requirements",
                  "checks": [{"method": "synthetic-check", "result": "passed", "evidence": "retained-check-1"}],
                  "requirements": {r: {"status": "not_met", "evidence": "requirement-evidence-" + str(i)} for i, r in enumerate(REQS)},
                  "claims": old.get("claims", [{"id": "C1", "statement": "The claim is tracked", "status": "uncertain", "evidence": "claim-evidence"}]),
                  "issues": old.get("issues", [{"id": "I1", "statement": "Acceptance is pending", "status": "open", "blocking": True, "evidence": "issue-evidence"}]),
                  "previous_review_sha256": status["review_sha256"], "artifact_sha256": status["artifact_sha256"],
                  "progress": True, "final_acceptance": {"passed": False, "evidence": "acceptance-pending"}}
        report.update(changes)
        return report

    def review(self, report=None, **changes):
        return self.call("review", "--report", self.write("review", report if report is not None else self.report(**changes)))

    def batch(self):
        todo_path = self.write("todo", {"version": 1, "tasks": [{"id": "synthetic", "text": "FROZEN_TODO_CONTENT"}]})
        return {"todo_path": todo_path, "todo_sha256": hashlib.sha256(Path(todo_path).read_bytes()).hexdigest(),
                "recipient": "synthetic-recipient", "summary": "Synthetic bounded work"}

    def prepare(self):
        return self.call("prepare", "--batch", self.write("batch", self.batch()))

    def intent(self):
        return self.write("intent", {"observed_at": time.time() - 1, "detail": "Native session observed; persist intent"})

    def dispatch(self):
        return self.call("dispatching", "--evidence", self.intent())

    def delivered(self, **changes):
        value = {"marker_seen": True, "marker": self.state()["pending"]["marker"], "receipt": {"message_id": "synthetic-message-1"}}
        value.update(changes)
        return self.call("delivered", "--evidence", self.write("delivery", value))

    def complete_run(self):
        return self.call("completed", "--evidence", self.write("completion", {"terminal": True, "run_id": "synthetic-run-1"}))

    def observe(self, **changes):
        partial = {"session": "active", "wake_verified": False, "quota": "available", "stop": False,
                   "ready_work": True, "new_planning_gap": False, "director_packet_unchanged": False}
        partial.update(changes)
        result = self.call("observation", "--partial", self.write("partial", partial))
        clean = GUARD._validate_observation(result["observation"], self.gid)
        assert clean == result["observation"]
        assert result["next"]["release_authority"] is False
        expected = GUARD.next_action(self.goal, clean)
        assert {k: v for k, v in result["next"].items() if k != "release_authority"} == expected
        return result


class Lifecycle(unittest.TestCase):
    def test_full_cycle_with_real_artifact_repair(self):
        c = Case()
        self.assertEqual(c.state()["goal"], c.goal)
        self.assertEqual(c.call("status")["phase"], "observing")
        self.assertEqual(c.complete_run()["phase"], "reviewing")
        first = c.report()
        reviewed = c.review(first)
        self.assertEqual(reviewed["phase"], "reviewed")
        self.assertEqual(c.call("status")["required_action"], "prepare")
        self.assertEqual(c.state()["review"]["requirements"], first["requirements"])
        expected_hash = hashlib.sha256(LEDGER.canonical(first)).hexdigest()
        self.assertEqual(reviewed["review_sha256"], expected_hash)
        prepared = c.prepare()
        self.assertEqual(prepared["round"], 1)
        self.assertEqual(prepared["marker"], "[OL-n1-goal-0001]")
        self.assertNotIn("FROZEN_TODO_CONTENT", json.dumps(prepared))
        self.assertEqual(c.dispatch()["phase"], "dispatching")
        self.assertEqual(c.observe()["next"]["decision"], "reconcile_execution")
        self.assertEqual(c.delivered()["phase"], "running")
        self.assertEqual(c.observe()["next"]["decision"], "wait_on_existing_handle")
        (c.ws / "app.py").write_text("repair", encoding="utf-8")
        self.assertEqual(c.complete_run()["phase"], "reviewing")
        self.assertEqual(c.call("status")["round"], 1)
        final = c.report(requirements={r: {"status": "met", "evidence": "verified-output"} for r in REQS},
                         final_acceptance={"passed": True, "evidence": "integrated-acceptance"})
        final["claims"][0]["status"] = "accepted"
        final["issues"][0]["status"] = "resolved"
        self.assertNotEqual(final["artifact_sha256"], first["artifact_sha256"])
        self.assertEqual(c.review(final)["phase"], "complete")
        status = c.call("status")
        self.assertFalse(status["stale_review"])
        self.assertEqual(status["blocking_issues"], [])
        self.assertEqual(status["unmet"], [])
        self.assertEqual(status["counters"]["batches_completed"], 1)
        self.assertEqual(c.observe()["next"]["decision"], "complete")
        self.assertEqual(len(list((c.root / c.gid).glob("review-*.json"))), 2)

    def test_review_directly_in_round_zero_and_invalidate_rolls_back(self):
        c = Case()
        self.assertEqual(c.review()["phase"], "reviewed")
        self.assertEqual(c.prepare()["round"], 1)
        result = c.call("invalidate", "--reason", "new artifact evidence")
        self.assertEqual((result["phase"], result["round"]), ("reviewing", 0))
        self.assertIsNone(c.state()["pending"])
        self.assertIsNone(c.state()["intent"])
        c.review()
        c.call("invalidate", "--reason", "check again")
        self.assertEqual(c.call("status")["phase"], "reviewing")
        self.assertEqual(c.call("status")["round"], 0)

    def test_completed_needs_terminal_and_run_id_in_round_zero(self):
        c = Case()
        for evidence in ({}, {"terminal": False, "run_id": "r"}, {"terminal": True}, {"terminal": True, "run_id": ""}):
            with self.subTest(evidence=evidence):
                c.call("completed", "--evidence", c.write("bad", evidence), error="ANY")
                self.assertEqual(c.state()["phase"], "observing")
        self.assertEqual(c.complete_run()["phase"], "reviewing")
        self.assertEqual(c.state()["round"], 0)

    def test_missing_token_refused_for_every_leased_command(self):
        c = Case()
        fixtures = {"renew": [], "release": [], "review": ["--report", c.write("r", {})],
                    "invalidate": ["--reason", "test"], "prepare": ["--batch", c.write("b", {})],
                    "dispatching": ["--evidence", c.intent()], "delivered": ["--evidence", c.write("d", {})],
                    "completed": ["--evidence", c.write("c", {})]}
        before = c.state()
        for command, args in fixtures.items():
            with self.subTest(command=command):
                c.call(command, *args, token=False, error="LEASE_REQUIRED")
                self.assertEqual(c.state(), before)

    def test_renew_release_and_expired_lease(self):
        c = Case()
        old = c.token
        expiry = c.state()["lease"]["expires_at"]
        with patch.object(LEDGER.time, "time", return_value=time.time() + 10):
            self.assertGreater(c.call("renew")["expires_at"], expiry)
        c.call("release")
        c.call("renew", error="LEASE_REQUIRED")
        c.token = c.call("acquire")["token"]
        self.assertNotEqual(old, c.token)
        c.call("acquire", error="LEASE_HELD")
        with patch.object(LEDGER.time, "time", return_value=c.state()["lease"]["expires_at"] + 1):
            c.call("release", error="LEASE_REQUIRED")
            self.assertNotEqual(c.call("acquire")["token"], c.token)

    def test_stop_without_token_in_all_relevant_phases(self):
        for phase in ("observing", "reviewed", "prepared", "dispatching", "running", "complete", "stopped_limit"):
            with self.subTest(phase=phase):
                c = Case(config={"max_batches": 0} if phase == "stopped_limit" else None)
                if phase == "complete":
                    c.review(requirements={r: {"status": "met", "evidence": "ok"} for r in REQS},
                             claims=[], issues=[], final_acceptance={"passed": True, "evidence": "ok"})
                elif phase != "observing":
                    c.review()
                    if phase in {"prepared", "dispatching", "running"}:
                        c.prepare()
                    if phase in {"dispatching", "running"}:
                        c.dispatch()
                    if phase == "running":
                        c.delivered()
                self.assertEqual(c.state()["phase"], phase)
                self.assertEqual(c.call("stop", "--reason", "owner stop")["phase"], "stopped")
                self.assertEqual(c.call("stop", "--reason", "repeat stop")["phase"], "stopped")
                self.assertEqual(c.observe(stop=False)["next"]["decision"], "stop")
                c.token = c.call("acquire")["token"]
                c.call("prepare", "--batch", c.write("b", c.batch()), error="INVALID_PHASE")


class ReviewAndDelivery(unittest.TestCase):
    def test_report_refusals_and_unchanged_state(self):
        c = Case()
        valid = c.report()
        bad_reports = []
        def bad(code, **changes):
            report = json.loads(json.dumps(valid))
            report.update(changes)
            bad_reports.append((code, report))
        bad("ROUND_MISMATCH", round=1)
        bad("ROUND_MISMATCH", round=False)
        bad("REVIEW_INVALID", checks=[])
        bad("PREVIOUS_REVIEW_MISMATCH", previous_review_sha256="0" * 64)
        bad("STALE_ARTIFACTS", artifact_sha256="0" * 64)
        bad("REVIEW_INVALID", requirements={REQS[0]: {"status": "met", "evidence": "ok"}})
        bad("INVALID_INPUT", requirements={r: {"status": "met"} for r in REQS})
        bad("REVIEW_INVALID", claims=[{"id": "C", "statement": "bad state", "status": "open", "evidence": "ok"}])
        bad("INVALID_INPUT", issues=[{"id": "I", "statement": "missing bool", "status": "open", "evidence": "ok"}])
        bad("INVALID_INPUT", progress=1)
        bad("INVALID_INPUT", final_acceptance={"passed": True})
        bad("INVALID_INPUT", surprise="untrusted")
        before = c.state()
        for code, report in bad_reports:
            with self.subTest(code=code, report=report):
                c.call("review", "--report", c.write("bad-review", report), error=code)
                self.assertEqual(c.state(), before)

    def test_append_only_ids_statements_and_kinds(self):
        c = Case()
        c.review()
        c.call("invalidate", "--reason", "independent recheck")
        for change in ("omit_claim", "omit_issue", "claim_statement", "issue_statement", "kind", "duplicate", "clear_blocking"):
            with self.subTest(change=change):
                report = c.report()
                if change == "omit_claim":
                    report["claims"] = []
                elif change == "omit_issue":
                    report["issues"] = []
                elif change.endswith("statement"):
                    report["claims" if change.startswith("claim") else "issues"][0]["statement"] = "different text"
                elif change == "kind":
                    issue = report["issues"].pop()
                    issue.pop("blocking")
                    issue["status"] = "accepted"
                    report["claims"].append(issue)
                elif change == "duplicate":
                    report["issues"][0]["id"] = "C1"
                else:
                    report["issues"][0]["blocking"] = False
                c.call("review", "--report", c.write("bad", report), error="REVIEW_INVALID" if change == "duplicate" else "APPEND_ONLY")
        c.call("review", "--report", c.write("bad-prev", c.report(previous_review_sha256=None)), error="PREVIOUS_REVIEW_MISMATCH")
        report = c.report()
        report["claims"][0]["status"] = "refuted"
        report["issues"][0].update(status="resolved", blocking=False)
        report["claims"].append({"id": "C2", "statement": "A new claim", "evidence": "new-evidence", "status": "accepted"})
        c.review(report)
        self.assertEqual(len(c.state()["review"]["claims"]), 2)

    def test_stale_root_file_blocks_prepare_and_dispatch_but_not_repair_review(self):
        c = Case()
        c.review()
        before_hash = c.call("reconcile")["artifact_sha256"]
        (c.ws / "app.py").write_text("omega", encoding="utf-8")
        rec = c.call("reconcile")
        self.assertTrue(rec["changed_since_review"])
        self.assertTrue(rec["stale_review"])
        self.assertNotEqual(rec["artifact_sha256"], before_hash)
        c.call("prepare", "--batch", c.write("batch", c.batch()), error="STALE_ARTIFACTS")
        c.call("invalidate", "--reason", "artifact changed")
        c.review()
        c.prepare()
        (c.ws / "app.py").write_text("third", encoding="utf-8")
        c.call("dispatching", "--evidence", c.intent(), error="STALE_ARTIFACTS")
        self.assertEqual(c.state()["phase"], "prepared")
        c.call("invalidate", "--reason", "review repaired source")
        c.review()
        self.assertFalse(c.call("reconcile")["stale_review"])

    def test_todo_is_required_hashed_and_frozen(self):
        c = Case()
        c.review()
        good = c.batch()
        for variant in ("missing_path", "bad_digest", "missing_recipient"):
            candidate = good.copy()
            if variant == "missing_path":
                candidate.pop("todo_path")
            elif variant == "bad_digest":
                candidate["todo_sha256"] = "f" * 64
            else:
                candidate.pop("recipient")
            c.call("prepare", "--batch", c.write("bad-batch", candidate), error="TODO_HASH_MISMATCH" if variant == "bad_digest" else "INVALID_INPUT")
        prepared = c.call("prepare", "--batch", c.write("good-batch", good))
        pending = c.state()["pending"]
        frozen = Path(good["todo_path"]).read_text(encoding="utf-8")
        self.assertEqual(pending["todo_text"], frozen)
        self.assertEqual(pending["todo_sha256"], hashlib.sha256(frozen.encode()).hexdigest())
        self.assertEqual(prepared["batch_sha256"], hashlib.sha256(LEDGER.canonical({k: v for k, v in pending.items() if k != "batch_sha256"})).hexdigest())
        Path(good["todo_path"]).write_text('{"different":"later"}', encoding="utf-8")
        c.dispatch()
        self.assertEqual(c.state()["pending"]["todo_text"], frozen)

    def test_dispatch_requires_fresh_intent_and_delivery_requires_matching_marker_receipt(self):
        c = Case()
        c.review()
        c.prepare()
        c.call("delivered", "--evidence", c.write("d", {}), error="INVALID_PHASE")
        c.call("dispatching", error="ARGUMENT_ERROR")
        for evidence in ({}, {"observed_at": time.time(), "detail": ""},
                         {"observed_at": time.time() - 301, "detail": "old"},
                         {"observed_at": time.time() + 30, "detail": "future"},
                         {"observed_at": True, "detail": "bad number"}):
            c.call("dispatching", "--evidence", c.write("bad-intent", evidence), error="ANY")
            self.assertEqual(c.state()["phase"], "prepared")
        stamp = datetime.fromtimestamp(time.time() - 1, timezone.utc).isoformat()
        c.call("dispatching", "--evidence", c.write("intent", {"observed_at": stamp, "detail": "native state verified"}))
        self.assertEqual(c.state()["intent"]["observed_at"], stamp)
        marker = c.state()["pending"]["marker"]
        bad_receipts = ({}, {"source": "Office"}, {"id": ""}, {"id": "x", "payload": "untrusted"}, " ", "x" * 2001, True, None)
        for value in bad_receipts:
            c.call("delivered", "--evidence", c.write("bad-delivery", {"marker_seen": True, "marker": marker, "receipt": value}), error="ANY")
            self.assertEqual(c.state()["phase"], "dispatching")
        for values in ({"marker_seen": False, "marker": marker}, {"marker_seen": True, "marker": "wrong"}):
            c.call("delivered", "--evidence", c.write("bad-marker", {**values, "receipt": "native-id"}), error="MARKER_REQUIRED")
        c.call("dispatching", "--evidence", c.intent(), error="DELIVERY_UNCERTAIN")
        c.call("reconcile")
        self.assertEqual(c.state()["phase"], "dispatching")
        self.assertEqual(c.call("status")["required_action"], "reconcile_delivery")
        c.call("dispatching", "--evidence", c.intent(), error="DELIVERY_UNCERTAIN")
        c.call("invalidate", "--reason", "retry", error="INVALID_PHASE")
        c.delivered(receipt="native-identifiable-id", delivery="inflight")
        self.assertEqual(c.observe()["observation"]["delivery"], "inflight")
        c.call("invalidate", "--reason", "retry", error="INVALID_PHASE")
        c.call("review", "--report", c.write("bad-phase", {}), error="INVALID_PHASE")

    def test_open_blocker_prevents_guard_completion_and_partial_cannot_override_ledger(self):
        c = Case()
        c.review(requirements={r: {"status": "met", "evidence": "passed"} for r in REQS},
                 final_acceptance={"passed": True, "evidence": "claimed final check"})
        self.assertEqual(c.state()["phase"], "reviewed")
        result = c.observe(final_acceptance=True, execution="active", delivery="unknown", same_failed_result_count=999)
        self.assertFalse(result["observation"]["final_acceptance"])
        self.assertEqual(result["observation"]["execution"], "terminal")
        self.assertEqual(result["observation"]["same_failed_result_count"], 0)
        self.assertEqual(result["next"]["decision"], "final_acceptance_check")
        c.call("invalidate", "--reason", "resolve blocker")
        report = c.report(requirements={r: {"status": "met", "evidence": "passed"} for r in REQS}, final_acceptance={"passed": True, "evidence": "passed"})
        report["issues"][0]["status"] = "resolved"
        c.review(report)
        self.assertEqual(c.observe()["next"]["decision"], "complete")
        (c.ws / "app.py").write_text("later edit", encoding="utf-8")
        self.assertNotEqual(c.observe()["next"]["decision"], "complete")

    def test_diagnosis_after_two_failures_does_not_stop_loop_and_progress_resets(self):
        c = Case()
        c.review(progress=False)
        c.call("invalidate", "--reason", "independent recheck")
        c.review(progress=False)
        self.assertEqual(c.call("status")["required_action"], "diagnose")
        self.assertEqual(c.observe()["next"]["decision"], "diagnose")
        self.assertEqual(c.state()["phase"], "reviewed")
        c.call("invalidate", "--reason", "diagnosis applied")
        c.review(progress=True)
        self.assertEqual(c.call("status")["no_progress_reviews"], 0)
        self.assertEqual(c.prepare()["phase"], "prepared")


class ConfigurationAndSnapshots(unittest.TestCase):
    def test_defaults_no_hidden_limits_config_frozen_reinit_refused(self):
        c = Case()
        self.assertEqual(c.state()["config"], {"watch": ["**/*"], "lease_minutes": 30})
        before = c.state()
        c.call("init", "--goal", c.goal_file, error="ALREADY_INITIALIZED")
        self.assertEqual(c.state(), before)
        future = time.time() + 365 * 24 * 3600
        with patch.object(LEDGER.time, "time", return_value=future):
            c.token = c.call("acquire")["token"]
            self.assertEqual(c.review()["phase"], "reviewed")
            self.assertEqual(c.prepare()["phase"], "prepared")
        configured = Case(config={"watch": ["*.py"], "lease_minutes": 60, "max_batches": 3})
        Path(configured.cfg_file).write_text('{"max_batches":0}', encoding="utf-8")
        self.assertEqual(configured.state()["config"]["max_batches"], 3)

    def test_explicit_batch_limit_and_acceptance_at_last_batch(self):
        zero = Case(config={"max_batches": 0})
        self.assertEqual(zero.review()["phase"], "stopped_limit")
        self.assertEqual(zero.observe()["next"]["decision"], "stop")
        for passed in (False, True):
            c = Case(config={"max_batches": 1})
            c.review()
            c.prepare()
            c.dispatch()
            c.delivered()
            c.complete_run()
            changes = {}
            if passed:
                changes = {"requirements": {r: {"status": "met", "evidence": "ok"} for r in REQS},
                           "issues": [{"id": "I1", "statement": "Acceptance is pending", "evidence": "resolved", "status": "resolved", "blocking": True}],
                           "final_acceptance": {"passed": True, "evidence": "ok"}}
            self.assertEqual(c.review(**changes)["phase"], "complete" if passed else "stopped_limit")

    def test_elapsed_time_limit_fences_prepare_and_dispatch_and_review(self):
        for point in ("prepare", "dispatching", "review"):
            with self.subTest(point=point):
                c = Case(config={"max_hours": 0.001})
                if point != "review":
                    c.review()
                if point == "dispatching":
                    c.prepare()
                with patch.object(LEDGER.time, "time", return_value=c.state()["created_at"] + 3600):
                    c.token = c.call("acquire")["token"]
                    if point == "review":
                        self.assertEqual(c.review()["phase"], "stopped_limit")
                    elif point == "prepare":
                        c.call("prepare", "--batch", c.write("b", c.batch()), error="LIMIT_REACHED")
                    else:
                        c.call("dispatching", "--evidence", c.intent(), error="LIMIT_REACHED")
                    self.assertEqual(c.state()["phase"], "stopped_limit")

    def test_invalid_configs_and_goal_schema_leave_no_state(self):
        for config in ({"lease_minutes": 61}, {"max_batches": -1}, {"max_batches": True},
                       {"max_hours": -1}, {"watch": ["../*"]}, {"watch": []}, {"alien": 3}):
            c = Case(config=config, initialize=False)
            c.call("init", "--goal", c.goal_file, "--config", c.cfg_file, error="ANY")
            self.assertFalse(c.root.exists())
        c = Case(initialize=False)
        bad_goal = c.goal.copy()
        bad_goal.pop("constraints")
        c.call("init", "--goal", c.write("bad-goal", bad_goal), error="GUARD_REJECTED")
        self.assertFalse(c.root.exists())
        bad_goal = {**c.goal, "goal_id": "../escape"}
        c.call("init", "--goal", c.write("bad-id", bad_goal), error="INVALID_GOAL_ID")
        self.assertFalse(c.root.exists())

    def test_workspace_skill_and_symlink_destinations_refused_before_any_write(self):
        c = Case(initialize=False)
        for forbidden in (c.ws / "forbidden-state", SKILL_TREE / "forbidden-ledger-test"):
            original = tree_snapshot()
            code, data = cli(["init", "--root", str(forbidden), "--goal", c.goal_file])
            self.assertNotEqual(code, 0)
            self.assertEqual(data["code"], "STATE_LOCATION_INVALID")
            self.assertFalse(forbidden.exists())
            self.assertEqual(tree_snapshot(), original)
        link = c.base / "workspace-link"
        try:
            link.symlink_to(c.ws, target_is_directory=True)
        except OSError:
            return  # Windows may require symlink privilege; direct-path checks ran above.
        code, data = cli(["init", "--root", str(link / "state"), "--goal", c.goal_file])
        self.assertNotEqual(code, 0)
        self.assertEqual(data["code"], "STATE_LOCATION_INVALID")
        self.assertFalse((c.ws / "state").exists())

    def test_preexisting_misplaced_state_is_refused_before_lock_creation(self):
        c = Case()
        bad_root = c.ws / "misplaced"
        directory = bad_root / c.gid
        directory.mkdir(parents=True)
        (directory / "state.json").write_bytes((c.root / c.gid / "state.json").read_bytes())
        code, data = cli(["status", "--root", str(bad_root), "--goal-id", c.gid])
        self.assertNotEqual(code, 0)
        self.assertEqual(data["code"], "STATE_LOCATION_INVALID")
        self.assertFalse((bad_root / ".ledger.lock").exists())

    def test_globs_exclusions_and_custom_watch(self):
        c = Case()
        baseline = c.call("reconcile")["artifact_sha256"]
        for excluded in LEDGER.IGNORED_DIRS:
            path = c.ws / excluded
            path.mkdir()
            (path / "ignored.txt").write_text("ignored", encoding="utf-8")
        self.assertEqual(c.call("reconcile")["artifact_sha256"], baseline)
        for relative, pattern, matches in (("app.py", "**/*", True), ("app.py", "**/*.py", True),
                                           ("src/app.py", "*.py", False), ("src/app.py", "src/*.py", True),
                                           ("src/sub/app.py", "src/*.py", False), ("src/sub/app.py", "src/**/*.py", True)):
            self.assertEqual(LEDGER.glob_match(relative, pattern), matches)
        c = Case(config={"watch": ["src/*.py"]})
        baseline = c.call("reconcile")["artifact_sha256"]
        (c.ws / "app.py").write_text("root ignored by custom watch", encoding="utf-8")
        self.assertEqual(c.call("reconcile")["artifact_sha256"], baseline)
        (c.ws / "src" / "app.py").write_text("watched edit", encoding="utf-8")
        self.assertNotEqual(c.call("reconcile")["artifact_sha256"], baseline)

    def test_symlinked_file_and_directory_are_omitted(self):
        c = Case()
        external = c.base / "outside"
        external.mkdir()
        (external / "outside.py").write_text("outside", encoding="utf-8")
        baseline = c.call("reconcile")["artifact_sha256"]
        try:
            (c.ws / "linked-file.py").symlink_to(external / "outside.py")
            (c.ws / "linked-directory").symlink_to(external, target_is_directory=True)
        except OSError:
            self.skipTest("symlink privilege unavailable")
        self.assertEqual(c.call("reconcile")["artifact_sha256"], baseline)
        (external / "outside.py").write_text("external change", encoding="utf-8")
        self.assertEqual(c.call("reconcile")["artifact_sha256"], baseline)

    def test_snapshot_resource_limits(self):
        c = Case()
        self.assertEqual(LEDGER.MAX_SNAPSHOT_FILES, 10000)
        self.assertEqual(LEDGER.MAX_SNAPSHOT_FILE_BYTES, 50 * 1024 * 1024)
        with patch.object(LEDGER, "MAX_SNAPSHOT_FILES", 1):
            c.call("reconcile", error="SNAPSHOT_LIMIT")
        with patch.object(LEDGER, "MAX_SNAPSHOT_FILE_BYTES", 4):
            c.call("reconcile", error="SNAPSHOT_LIMIT")

    def test_same_size_change_during_hashing_is_detected(self):
        c = Case()
        target = c.ws / "app.py"
        old = target.stat()
        real_fstat = LEDGER.os.fstat
        calls = []
        def changing_fstat(fd):
            calls.append(fd)
            if len(calls) == 2:
                target.write_text("omega", encoding="utf-8")
                os.utime(target, ns=(old.st_atime_ns, old.st_mtime_ns + 2000000000))
            return real_fstat(fd)
        with patch.object(LEDGER.os, "fstat", side_effect=changing_fstat):
            with self.assertRaises(LEDGER.LedgerError) as error:
                LEDGER.file_hash(target)
        self.assertEqual(error.exception.code, "FILE_CHANGED")
        self.assertEqual(target.stat().st_size, old.st_size)


class CLIErrorsAndConcurrency(unittest.TestCase):
    def test_argparse_and_json_errors_are_one_json_stdout_document(self):
        for argv in ([], ["nonsense"], ["init"], ["status", "--bogus"], ["renew", "--token"], ["--root", "relative", "status"]):
            code, data = cli(argv)
            self.assertNotEqual(code, 0)
            self.assertEqual(data["code"], "ARGUMENT_ERROR")
        c = Case()
        for raw in (b'{"round":0,"round":1}', b'{"round":NaN}', b'not-json', b'\xff', b' ' * (LEDGER.MAX_INPUT_BYTES + 1)):
            path = Path(c.write("invalid-json", {}))
            path.write_bytes(raw)
            c.call("review", "--report", str(path), error="ANY")
        self.assertEqual(cli(["--help"])[0], 0)

    def test_default_home_and_single_goal_selection(self):
        c = Case(initialize=False)
        with patch.dict(os.environ, {"OFFICE_LOOP_HOME": str(c.root)}):
            code, result = cli(["init", "--goal", c.goal_file])
            self.assertEqual(code, 0, result)
            self.assertTrue((c.root / c.gid / "state.json").exists())
            self.assertEqual(cli(["status"])[1]["goal_id"], c.gid)
        other = Case(initialize=False)
        fake_home = other.base / "fake-home"
        with patch.dict(os.environ, {}, clear=False), patch.object(LEDGER.Path, "home", return_value=fake_home):
            # Remove only a key from the temporary environment mapping, never a file.
            os.environ.pop("OFFICE_LOOP_HOME", None)
            self.assertEqual(cli(["init", "--goal", other.goal_file])[0], 0)
        self.assertTrue((fake_home / ".agents" / "office-loop" / other.gid / "state.json").exists())

    def run_process(self, argv):
        return subprocess.Popen([sys.executable, "-B", str(SCRIPT), *argv],
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))

    def collect(self, process):
        stdout, stderr = process.communicate(timeout=30)
        self.assertEqual(stderr, "")
        self.assertEqual(len(stdout.splitlines()), 1)
        result = json.loads(stdout)
        self.assertIs(result["release_authority"], False)
        return process.returncode, result

    def test_real_cli_processes_compete_for_one_lease_and_init(self):
        c = Case(initialize=False)
        argv = c.argv("init", "--goal", c.goal_file)
        results = [self.collect(p) for p in [self.run_process(argv), self.run_process(argv)]]
        self.assertEqual(sorted(code for code, _ in results), [0, 2])
        self.assertEqual([r["code"] for code, r in results if code], ["ALREADY_INITIALIZED"])
        argv = c.argv("acquire")
        results = [self.collect(p) for p in [self.run_process(argv), self.run_process(argv)]]
        self.assertEqual(sorted(code for code, _ in results), [0, 2])
        self.assertEqual([r["code"] for code, r in results if code], ["LEASE_HELD"])
        self.assertEqual(c.state()["lease"]["token"], [r["token"] for code, r in results if code == 0][0])
        c.token = c.state()["lease"]["token"]
        self.assertEqual(self.collect(self.run_process(c.argv("renew")))[0], 0)
        self.assertEqual(self.collect(self.run_process(c.argv("release")))[0], 0)

    def test_stop_cannot_be_overwritten_by_concurrent_dispatch_transaction(self):
        c = Case()
        c.review()
        c.prepare()
        intent_path = c.intent()
        entered, allow_dispatch, stop_attempted = threading.Event(), threading.Event(), threading.Event()
        real_observed = LEDGER.observed
        def pause_inside_transaction(*args, **kwargs):
            entered.set()
            if not allow_dispatch.wait(10):
                raise AssertionError("test transaction did not resume")
            return real_observed(*args, **kwargs)
        def execute(argv):
            try:
                return LEDGER.execute(LEDGER.build_parser().parse_args(argv))
            except LEDGER.LedgerError as error:
                return {"status": "ERROR", "code": error.code}
        def stop():
            stop_attempted.set()
            return execute(c.argv("stop", "--reason", "concurrent owner stop"))
        with patch.object(LEDGER, "observed", side_effect=pause_inside_transaction):
            with ThreadPoolExecutor(max_workers=2) as pool:
                dispatch = pool.submit(execute, c.argv("dispatching", "--evidence", intent_path))
                self.assertTrue(entered.wait(10))
                stopped = pool.submit(stop)
                self.assertTrue(stop_attempted.wait(10))
                allow_dispatch.set()
                self.assertEqual(dispatch.result(timeout=10)["phase"], "dispatching")
                self.assertEqual(stopped.result(timeout=10)["phase"], "stopped")
        self.assertEqual(c.call("status")["phase"], "stopped")
        c.token = c.call("acquire")["token"]
        c.call("dispatching", "--evidence", intent_path, error="INVALID_PHASE")

    def test_skill_tree_remains_unchanged_including_imports_and_subprocesses(self):
        self.assertEqual(tree_snapshot(), BEFORE_IMPORTS)
        self.assertTrue(sys.dont_write_bytecode)


if __name__ == "__main__":
    unittest.main()
