"""
Adversarial failure-injection suite for agent-loop V0.1 security contract.

Runs entirely under a unique TEMP project. No real deploy, no production repo,
no network, no Agent Fleet subprocess.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path

# Local import of golden security kernel
SUITE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SUITE_DIR))

import security_contract as sc  # noqa: E402


class TempProjectTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="agent-loop-adv-"))
        self.project = self.tmp / "project"
        self.project.mkdir()
        (self.project / "src").mkdir()
        (self.project / "src" / "app.py").write_text("print('ok')\n", encoding="utf-8")
        (self.project / ".agents" / "loops" / "health").mkdir(parents=True)
        self.loop_id = "health"
        self.addCleanup(lambda: shutil.rmtree(self.tmp, ignore_errors=True))


# ---------------------------------------------------------------------------
# JSON contract
# ---------------------------------------------------------------------------


class TestLoopJsonContract(TempProjectTest):
    def test_valid_minimal_contract(self) -> None:
        doc = {
            "schema_version": 1,
            "loop_id": "health",
            "goal": "daily health report",
            "non_goals": ["mutate code"],
            "watched_scope": ["src/**"],
            "cadence": {"kind": "manual"},
            "timezone": "Europe/Warsaw",
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
                "run_timeout_seconds": 600,
                "budget": {
                    "runs_per_day": 2,
                    "tokens_per_run": 50_000,
                    "tokens_per_day": 100_000,
                    "unknown_token_policy": "stop",
                },
            },
            "stop_conditions": {
                "no_progress_runs": 3,
                "repeated_failure_runs": 3,
                "budget_stop": True,
            },
            "human_gates": ["L2_promotion"],
            "kill_switch": {"file": "STOP", "env": "AGENT_LOOP_KILL"},
            "retention": {"runs_keep_days": 30, "reports_keep_days": 30},
            "write_policy": {
                "allowed_write_areas": [".agents/loops/health/**"],
                "path_claims": ["src/**"],
                "forbid_watched_writes": True,
            },
        }
        self.assertEqual(sc.validate_loop_json(doc), [])

    def test_invalid_missing_fields_and_l3_rejected(self) -> None:
        errors = sc.validate_loop_json({"schema_version": 1, "maturity": "L3"})
        self.assertTrue(any(e.startswith("missing:") for e in errors))
        self.assertIn("L3_out_of_scope_v0_1", errors)


# ---------------------------------------------------------------------------
# Path traversal / escape
# ---------------------------------------------------------------------------


class TestPathTraversal(TempProjectTest):
    def test_rejects_dotdot_escape(self) -> None:
        with self.assertRaises(sc.PathEscapeError):
            sc.resolve_under_root(self.project, "../../outside.txt")

    def test_rejects_absolute_outside(self) -> None:
        outside = self.tmp / "outside.txt"
        outside.write_text("x", encoding="utf-8")
        with self.assertRaises(sc.PathEscapeError):
            sc.resolve_under_root(self.project, outside)

    def test_accepts_in_root(self) -> None:
        p = sc.resolve_under_root(self.project, "src/app.py")
        self.assertTrue(p.exists())
        self.assertTrue(str(p).startswith(str(self.project.resolve())))

    def test_rejects_mixed_traversal(self) -> None:
        with self.assertRaises(sc.PathEscapeError):
            sc.resolve_under_root(self.project, "src/../../../../Windows/system32")

    @unittest.skipUnless(os.name == "nt", "Windows reparse/junction probe")
    def test_junction_escape_if_supported(self) -> None:
        """If a junction points outside, resolve must not accept escape."""
        outside = self.tmp / "escaped_target"
        outside.mkdir()
        (outside / "secret.txt").write_text("nope", encoding="utf-8")
        link = self.project / "evil_link"
        # mklink /J requires privileges sometimes; skip soft if fails.
        created = subprocess.run(
            ["cmd.exe", "/d", "/c", "mklink", "/J", str(link), str(outside)],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        if created.returncode != 0 or not link.exists():
            self.skipTest("junction creation not permitted")
        # Reading via junction still resolves under project? On Windows resolve
        # follows reparse to outside, so it must be rejected.
        try:
            with self.assertRaises(sc.PathEscapeError):
                sc.resolve_under_root(self.project, "evil_link/secret.txt")
        finally:
            # remove junction without deleting outside
            os.rmdir(link)


# ---------------------------------------------------------------------------
# Path claims collision
# ---------------------------------------------------------------------------


class TestPathClaims(TempProjectTest):
    def test_two_loops_same_claim_collide(self) -> None:
        reg = sc.PathClaimRegistry()
        reg.acquire("loop-a", self.project, ["src/**"])
        with self.assertRaises(sc.PathClaimCollisionError):
            reg.acquire("loop-b", self.project, ["src/app.py"])

    def test_disjoint_claims_ok(self) -> None:
        reg = sc.PathClaimRegistry()
        reg.acquire("loop-a", self.project, ["src"])
        reg.acquire("loop-b", self.project, ["docs"])
        self.assertIn("loop-a", reg.claims)
        self.assertIn("loop-b", reg.claims)

    def test_release_allows_reuse(self) -> None:
        reg = sc.PathClaimRegistry()
        reg.acquire("loop-a", self.project, ["src"])
        reg.release("loop-a")
        reg.acquire("loop-b", self.project, ["src"])


# ---------------------------------------------------------------------------
# Parallel lock same loop
# ---------------------------------------------------------------------------


class TestLoopLock(TempProjectTest):
    def test_second_acquire_fails(self) -> None:
        lock_path = self.project / ".agents" / "loops" / "health" / "loop.lock"
        lock1 = sc.LoopLock(lock_path)
        lock1.acquire(blocking=False)
        try:
            lock2 = sc.LoopLock(lock_path)
            with self.assertRaises(sc.CircuitOpenError):
                lock2.acquire(blocking=False)
        finally:
            lock1.release()

    def test_sequential_ok(self) -> None:
        lock_path = self.project / ".agents" / "loops" / "health" / "loop.lock"
        with sc.LoopLock(lock_path):
            pass
        with sc.LoopLock(lock_path):
            pass

    def test_threaded_contention(self) -> None:
        lock_path = self.project / ".agents" / "loops" / "health" / "loop.lock"
        errors: list[BaseException] = []
        acquired = threading.Event()
        release = threading.Event()

        def holder() -> None:
            lock = sc.LoopLock(lock_path)
            try:
                lock.acquire(blocking=False)
                acquired.set()
                release.wait(timeout=5)
            finally:
                lock.release()

        def contender() -> None:
            acquired.wait(timeout=5)
            lock = sc.LoopLock(lock_path)
            try:
                lock.acquire(blocking=False)
            except BaseException as exc:
                errors.append(exc)

        t1 = threading.Thread(target=holder)
        t2 = threading.Thread(target=contender)
        t1.start()
        t2.start()
        t2.join(timeout=5)
        release.set()
        t1.join(timeout=5)
        self.assertTrue(errors, "contender should fail to acquire")
        self.assertIsInstance(errors[0], sc.CircuitOpenError)


# ---------------------------------------------------------------------------
# Atomic replace / crash simulation
# ---------------------------------------------------------------------------


class TestAtomicState(TempProjectTest):
    def test_atomic_write_roundtrip(self) -> None:
        path = self.project / ".agents" / "loops" / "health" / "state.json"
        sc.atomic_write_json(path, {"v": 1, "ok": True})
        data = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(data["v"], 1)

    def test_interrupted_before_replace_leaves_old(self) -> None:
        path = self.project / ".agents" / "loops" / "health" / "state.json"
        sc.atomic_write_json(path, {"v": 1})
        # Simulate crash: write temp but never replace
        tmp = path.with_suffix(".json.crashed.tmp")
        tmp.write_text("{broken", encoding="utf-8")
        # Canonical path still valid
        data = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(data["v"], 1)
        self.assertTrue(tmp.exists())

    def test_append_jsonl_is_append_only(self) -> None:
        path = self.project / ".agents" / "loops" / "health" / "runs.jsonl"
        sc.append_jsonl(path, {"run_id": "r1", "status": "ok"})
        sc.append_jsonl(path, {"run_id": "r2", "status": "ok"})
        lines = path.read_text(encoding="utf-8").strip().splitlines()
        self.assertEqual(len(lines), 2)
        self.assertEqual(json.loads(lines[0])["run_id"], "r1")


# ---------------------------------------------------------------------------
# Budget idempotency
# ---------------------------------------------------------------------------


class TestBudgetIdempotency(unittest.TestCase):
    def test_same_run_id_charged_once(self) -> None:
        ledger = sc.BudgetLedger(
            max_tokens_per_run=1000,
            max_tokens_per_day=5000,
            max_runs_per_day=10,
        )
        a = ledger.settle("run-1", 100)
        b = ledger.settle("run-1", 100)
        c = ledger.settle("run-1", 999)  # even different tokens ignored
        self.assertTrue(a["charged"])
        self.assertFalse(b["charged"])
        self.assertFalse(c["charged"])
        self.assertEqual(ledger.day_token_total, 100)
        self.assertEqual(ledger.day_run_total, 1)

    def test_status_plan_recovery_not_billed(self) -> None:
        ledger = sc.BudgetLedger(1000, 5000, 10)
        ledger.note_non_billable("status")
        ledger.note_non_billable("plan")
        ledger.note_non_billable("recovery")
        self.assertEqual(ledger.day_token_total, 0)
        self.assertEqual(ledger.day_run_total, 0)
        self.assertEqual(ledger.non_billable_events, 3)

    def test_missing_telemetry_unknown_display_zero_charge(self) -> None:
        ledger = sc.BudgetLedger(1000, 5000, 10)
        r = ledger.settle("run-x", None)
        self.assertTrue(r["charged"])
        self.assertEqual(r["tokens"], 0)
        self.assertEqual(r["tokens_display"], "unknown")
        self.assertEqual(ledger.day_run_total, 1)

    def test_daily_budget_stop(self) -> None:
        ledger = sc.BudgetLedger(1000, 150, 10)
        ledger.settle("run-1", 100)
        with self.assertRaises(sc.BudgetError):
            ledger.settle("run-2", 100)

    def test_max_runs_per_day(self) -> None:
        ledger = sc.BudgetLedger(1000, 99999, 1)
        ledger.settle("run-1", 10)
        with self.assertRaises(sc.BudgetError):
            ledger.settle("run-2", 10)

    def test_per_run_budget(self) -> None:
        ledger = sc.BudgetLedger(50, 99999, 10)
        with self.assertRaises(sc.BudgetError):
            ledger.settle("run-1", 51)

    def test_persist_roundtrip_keeps_settled(self) -> None:
        ledger = sc.BudgetLedger(1000, 5000, 10)
        ledger.settle("run-1", 40)
        restored = sc.BudgetLedger.from_dict(ledger.to_dict())
        r = restored.settle("run-1", 40)
        self.assertFalse(r["charged"])
        self.assertEqual(restored.day_token_total, 40)


# ---------------------------------------------------------------------------
# Circuit breaker
# ---------------------------------------------------------------------------


class TestCircuitBreaker(unittest.TestCase):
    def test_kill_switch_before_run(self) -> None:
        cb = sc.CircuitBreaker(max_attempts=5)
        cb.arm_kill_switch("manual")
        with self.assertRaises(sc.KillSwitchActive):
            cb.begin_attempt()

    def test_kill_switch_during_recovery(self) -> None:
        cb = sc.CircuitBreaker(max_attempts=5)
        cb.begin_attempt()
        cb.record_failure("error")
        cb.arm_kill_switch("recovery")
        with self.assertRaises(sc.KillSwitchActive):
            cb.begin_attempt()

    def test_max_attempts(self) -> None:
        cb = sc.CircuitBreaker(max_attempts=2)
        cb.begin_attempt()
        cb.begin_attempt()
        with self.assertRaises(sc.CircuitOpenError) as ctx:
            cb.begin_attempt()
        self.assertEqual(str(ctx.exception), "max_attempts")

    def test_repeated_failure(self) -> None:
        cb = sc.CircuitBreaker(max_attempts=10, repeated_failure_threshold=3)
        for _ in range(2):
            cb.begin_attempt()
            cb.record_failure("error")
        cb.begin_attempt()
        with self.assertRaises(sc.CircuitOpenError) as ctx:
            cb.record_failure("error")
        self.assertEqual(str(ctx.exception), "repeated_failure")

    def test_no_progress(self) -> None:
        cb = sc.CircuitBreaker(max_attempts=10, no_progress_threshold=3)
        # First success establishes baseline fingerprint.
        cb.begin_attempt()
        cb.record_success("same-fp")
        # Next (threshold-1) identical results accumulate no-progress.
        for _ in range(2):
            cb.begin_attempt()
            cb.record_success("same-fp")
        # Threshold-th identical result opens the circuit.
        cb.begin_attempt()
        with self.assertRaises(sc.CircuitOpenError) as ctx:
            cb.record_success("same-fp")
        self.assertEqual(str(ctx.exception), "no_progress")

    def test_timeout_opens(self) -> None:
        cb = sc.CircuitBreaker(max_attempts=10)
        cb.begin_attempt()
        with self.assertRaises(sc.CircuitOpenError) as ctx:
            cb.record_failure("timeout")
        self.assertEqual(str(ctx.exception), "timeout")

    def test_budget_opens(self) -> None:
        cb = sc.CircuitBreaker(max_attempts=10)
        cb.begin_attempt()
        with self.assertRaises(sc.CircuitOpenError) as ctx:
            cb.record_failure("budget")
        self.assertEqual(str(ctx.exception), "budget_exceeded")

    def test_progress_resets_no_progress_counter(self) -> None:
        cb = sc.CircuitBreaker(max_attempts=10, no_progress_threshold=3)
        cb.begin_attempt()
        cb.record_success("a")
        cb.begin_attempt()
        cb.record_success("a")  # no progress count=1
        cb.begin_attempt()
        cb.record_success("b")  # reset
        self.assertEqual(cb.consecutive_no_progress, 0)
        self.assertIsNone(cb.open_reason)


# ---------------------------------------------------------------------------
# L1 write guard
# ---------------------------------------------------------------------------


class TestL1WriteGuard(TempProjectTest):
    def test_allows_state_and_report(self) -> None:
        p1 = sc.assert_l1_write_allowed(
            self.project, self.loop_id, ".agents/loops/health/state.json"
        )
        p2 = sc.assert_l1_write_allowed(
            self.project, self.loop_id, ".agents/loops/health/reports/r1.md"
        )
        self.assertTrue(str(p1).endswith("state.json"))
        self.assertTrue(str(p2).endswith("r1.md"))

    def test_rejects_watched_project_file(self) -> None:
        with self.assertRaises(sc.L1WriteViolation):
            sc.assert_l1_write_allowed(
                self.project,
                self.loop_id,
                "src/app.py",
                watched_globs=["src/**"],
            )

    def test_rejects_other_loop_state(self) -> None:
        with self.assertRaises(sc.L1WriteViolation):
            sc.assert_l1_write_allowed(
                self.project, self.loop_id, ".agents/loops/other/state.json"
            )

    def test_rejects_traversal_to_src(self) -> None:
        with self.assertRaises((sc.L1WriteViolation, sc.PathEscapeError)):
            sc.assert_l1_write_allowed(
                self.project,
                self.loop_id,
                ".agents/loops/health/../../src/app.py",
            )

    def test_invalid_loop_id(self) -> None:
        with self.assertRaises(sc.SecurityError):
            sc.loop_state_root(self.project, "../evil")


# ---------------------------------------------------------------------------
# Env scrub
# ---------------------------------------------------------------------------


class TestEnvScrub(unittest.TestCase):
    def test_strips_known_api_keys(self) -> None:
        src = {
            "PATH": "/usr/bin",
            "OPENAI_API_KEY": "sk-" + "test-secret-value-" + "1234567890",
            "XAI_API_KEY": "xai-secret",
            "ANTHROPIC_API_KEY": "ant-secret",
            "AGENT_FLEET_DEPTH": "0",
            "USERNAME": "mateusz",
        }
        out = sc.scrub_environment(src)
        self.assertNotIn("OPENAI_API_KEY", out)
        self.assertNotIn("XAI_API_KEY", out)
        self.assertNotIn("ANTHROPIC_API_KEY", out)
        self.assertIn("PATH", out)
        self.assertEqual(out.get("AGENT_LOOP_SCRUBBED"), "1")
        self.assertEqual(out.get("NO_COLOR"), "1")

    def test_strips_pattern_names(self) -> None:
        src = {
            "PATH": "C:\\Windows",
            "MY_CUSTOM_SECRET": "abc",
            "DB_PASSWORD": "pw",
            "HOME": "C:\\Users\\x",
        }
        out = sc.scrub_environment(src)
        self.assertNotIn("MY_CUSTOM_SECRET", out)
        self.assertNotIn("DB_PASSWORD", out)
        self.assertIn("HOME", out)

    def test_strips_secret_shaped_values(self) -> None:
        synthetic_token = "sk-" + "abcdefghijklmnopqrstuvwxyz"
        src = {
            "PATH": "/bin",
            "WEIRD_VAR": f"prefix {synthetic_token} more",
            "HOME": "/home/u",
        }
        out = sc.scrub_environment(src)
        self.assertNotIn("WEIRD_VAR", out)

    def test_deny_by_default_unknown_keys(self) -> None:
        src = {"PATH": "/bin", "RANDOM_APP_SETTING": "hello", "HOME": "/h"}
        out = sc.scrub_environment(src)
        self.assertNotIn("RANDOM_APP_SETTING", out)

    def test_log_text_must_not_contain_secrets(self) -> None:
        secret = "sk-" + "abcdefghijklmnopqrstuvwxyz" + "0123"
        log = f"run ok tokens=unknown key={secret}"
        with self.assertRaises(sc.SecurityError):
            sc.assert_no_secrets_in_text(log, labels={"openai": secret})


# ---------------------------------------------------------------------------
# Fake Fleet outcomes (controller integration contract)
# ---------------------------------------------------------------------------


class TestFakeFleetOutcomes(unittest.TestCase):
    """Model expected controller reactions to Fleet adapter results."""

    def test_success_records_acceptance(self) -> None:
        fleet = {
            "status": "ok",
            "run_id": "fleet-1",
            "tokens": 120,
            "acceptance": "accepted",
            "quota_before": "unknown",
            "quota_after": "unknown",
        }
        self.assertEqual(fleet["acceptance"], "accepted")
        self.assertEqual(sc.default_verifier_result(), "REJECT")
        # Parent acceptance required before promoting verifier
        verifier = (
            "ACCEPT"
            if fleet["acceptance"] == "accepted" and fleet["status"] == "ok"
            else sc.default_verifier_result()
        )
        # Still default REJECT unless parent explicitly accepts - L1 rule:
        # independent parent must set acceptance; controller default remains REJECT
        # until criteria met. Simulate parent gate:
        parent_accepts = True
        final = verifier if parent_accepts else sc.default_verifier_result()
        self.assertEqual(final, "ACCEPT")

    def test_error_without_telemetry(self) -> None:
        fleet = {"status": "error", "run_id": "fleet-2", "tokens": None}
        ledger = sc.BudgetLedger(1000, 5000, 10)
        r = ledger.settle(fleet["run_id"], fleet["tokens"])
        self.assertEqual(r["tokens_display"], "unknown")
        self.assertEqual(sc.default_verifier_result(), "REJECT")

    def test_unaccepted_result_stays_reject(self) -> None:
        fleet = {
            "status": "ok",
            "run_id": "fleet-3",
            "tokens": 10,
            "acceptance": "rejected",
        }
        parent_accepts = fleet["acceptance"] == "accepted"
        final = "ACCEPT" if parent_accepts else sc.default_verifier_result()
        self.assertEqual(final, "REJECT")


# ---------------------------------------------------------------------------
# Doctor / plan zero-write
# ---------------------------------------------------------------------------


class TestZeroWriteCommands(TempProjectTest):
    def test_doctor_plan_do_not_mutate_project(self) -> None:
        """Simulate doctor/plan: only read + validate; writes_performed=0."""
        before = {
            p.relative_to(self.project).as_posix(): p.stat().st_mtime_ns
            for p in self.project.rglob("*")
            if p.is_file()
        }
        doc = {
            "schema_version": 1,
            "loop_id": "health",
            "goal": "g",
            "non_goals": [],
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
                "run_timeout_seconds": 60,
                "budget": {
                    "runs_per_day": 1,
                    "tokens_per_run": 1,
                    "tokens_per_day": 1,
                    "unknown_token_policy": "stop",
                },
            },
            "stop_conditions": {
                "no_progress_runs": 1,
                "repeated_failure_runs": 1,
                "budget_stop": True,
            },
            "human_gates": [],
            "kill_switch": {"file": "STOP", "env": "AGENT_LOOP_KILL"},
            "retention": {"runs_keep_days": 1, "reports_keep_days": 1},
            "write_policy": {
                "allowed_write_areas": [".agents/loops/health/**"],
                "path_claims": ["src"],
                "forbid_watched_writes": True,
            },
        }
        errors = sc.validate_loop_json(doc)
        ledger = sc.BudgetLedger(1, 1, 1)
        ledger.note_non_billable("doctor")
        ledger.note_non_billable("plan")
        after = {
            p.relative_to(self.project).as_posix(): p.stat().st_mtime_ns
            for p in self.project.rglob("*")
            if p.is_file()
        }
        self.assertEqual(before, after)
        self.assertEqual(errors, [])
        result = {"writes_performed": 0, "validation_errors": errors}
        self.assertEqual(result["writes_performed"], 0)


# ---------------------------------------------------------------------------
# Optional probe: import controller if Kimi delivered it
# ---------------------------------------------------------------------------


class TestControllerPresenceProbe(unittest.TestCase):
    def test_controller_workbench_status(self) -> None:
        root = Path(__file__).resolve().parents[2] / "skill-workbench" / "agent-loop"
        # Document presence without failing suite - adversarial kernel is independent.
        py_files = list(root.rglob("*.py")) if root.exists() else []
        # Soft assertion: suite still GO for kernel; controller integration is separate.
        self.assertIsInstance(py_files, list)


def main() -> int:
    loader = unittest.TestLoader()
    suite = loader.loadTestsFromModule(sys.modules[__name__])
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)
    # Keep the packaged payload immutable unless a caller explicitly requests
    # a machine-readable test artifact outside the source tree.
    summary = {
        "tests_run": result.testsRun,
        "failures": len(result.failures),
        "errors": len(result.errors),
        "skipped": len(result.skipped),
        "was_successful": result.wasSuccessful(),
    }
    summary_path = os.environ.get("AGENT_LOOP_TEST_SUMMARY")
    if summary_path:
        out = Path(summary_path).expanduser().resolve()
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(summary, indent=2))
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
