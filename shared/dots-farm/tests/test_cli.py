"""Actual CLI journeys in separate processes, retained private SQLite fixtures."""

from contextlib import closing
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import time
import unittest
import uuid

FARM = Path(__file__).resolve().parents[1]
WORKSPACE = FARM.parents[1]
sys.path.insert(0, str(FARM))
from dots_farm.adapters import SyntheticOffice, SyntheticTarget, SyntheticSource, SyntheticModel
from dots_farm.engine import Engine
from dots_farm.store import Store


class CLITests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        root = Path(os.environ.get("DOTS_TEST_ROOT", str(FARM / ".test-runs")))
        if not root.is_absolute():
            root = WORKSPACE / root
        root = root.resolve()
        if not root.is_relative_to(WORKSPACE):
            raise ValueError("DOTS_TEST_ROOT must be within the task workspace")
        cls.fixture_root = root / ("cli-" + uuid.uuid4().hex)
        cls.fixture_root.mkdir(parents=True, exist_ok=False)

    def setUp(self):
        self.fixture = self.fixture_root / self._testMethodName
        self.fixture.mkdir()
        self.state = self.fixture / "state"
        self.targets = self.fixture / "targets"
        self.evidence = []

    def run_command(self, args, expected=0, source=None, entry=None, cwd=None):
        argv = [sys.executable, "-B", str(entry or FARM / "dots.py"), "--state", str(self.state)]
        if entry is None:
            argv += ["--target-state", str(self.targets)]
        if source is not None:
            argv += ["--source", str(source)]
        argv += args
        result = subprocess.run(argv, cwd=cwd or WORKSPACE, capture_output=True, text=True,
                                encoding="utf-8", timeout=15,
                                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        self.evidence.append({"argv": argv, "cwd": str(cwd or WORKSPACE), "exit_code": result.returncode,
                              "stdout": result.stdout.strip(), "stderr": result.stderr.strip()})
        (self.fixture / "command-evidence.json").write_text(json.dumps(self.evidence, indent=2), encoding="utf-8")
        self.assertEqual(result.returncode, expected, result.stderr)
        self.assertNotIn("Traceback", result.stderr)
        return json.loads(result.stdout) if expected == 0 else result

    def source(self, kind):
        path = self.fixture / (kind + "-source.json")
        entry = {"id": "source-one", "org": "org-a", "content": "PRIVATE-CLI delivery slipped",
                 "model_decision": {"action": "alert" if kind == "observer" else "collect",
                                    "reason_code": "relevant" if kind == "observer" else "invoice"}}
        if kind == "collector":
            entry.update({"invoice": {"org": "org-a", "number": "SYN-CLI-1", "date": "2026-10-08", "amount": "10.00", "currency": "PLN"},
                          "simulate_timeout_after_write": True})
        path.write_text(json.dumps([entry]), encoding="utf-8")
        return path

    def create(self, kind, name=None, source=None):
        return self.run_command(["create", name or kind, "--kind", kind, "--org", "org-a", "--project", "pilot", "--interval", "1", "--budget", "100"], source=source)

    def test_complete_owner_journey_across_process_restarts(self):
        self.run_command(["init", "--global-cap", "200", "--emergency-reserve", "20", "--now", "1000"])
        observer_source, collector_source = self.source("observer"), self.source("collector")
        self.create("observer", source=observer_source)
        self.create("collector", source=collector_source)
        text = self.fixture / "private-memory.txt"
        text.write_text("Delivery original contract PRIVATE-MEMORY", encoding="utf-8")
        self.run_command(["memory", "observer", "--memory-id", "contract", "--text-file", str(text), "--now", "1000"])
        text.write_text("Collector separate contract", encoding="utf-8")
        self.run_command(["memory", "collector", "--memory-id", "contract", "--text-file", str(text), "--now", "1000"])
        first = self.run_command(["tick", "--now", "1000"])
        self.assertEqual([row["outcome"] for row in first["ticks"]], ["ok", "ok"])
        status = self.run_command(["status"])
        self.assertEqual(status["global_budget"]["spent"], 2)
        self.assertEqual(status["global_budget"]["held"], 0)
        self.assertEqual(status["live_qualification"], "BLOCKED")
        self.assertEqual(SyntheticTarget(self.targets / "drafts.sqlite3").count(), 1)
        self.assertEqual(SyntheticOffice(self.targets / "office.sqlite3").count(), 1)
        text.write_text("Delivery corrected milestone", encoding="utf-8")
        self.assertEqual(self.run_command(["correct", "observer", "--memory-id", "contract", "--text-file", str(text)])["revision"], 2)
        private = Store(self.state / "controller.sqlite3")
        self.assertEqual(private.retrieve("observer", "original"), [])
        self.assertEqual(private.retrieve("collector", "corrected"), [])
        self.assertEqual(private.retrieve("observer", "milestone")[0]["revision"], 2)
        self.run_command(["pause", "observer"])
        self.assertEqual(self.run_command(["tick", "--dot", "observer", "--now", "1001"])["ticks"][0]["outcome"], "paused")
        self.assertTrue(self.run_command(["status", "--dot", "observer"])["dots"][0]["paused"])
        self.run_command(["resume", "observer"])
        self.assertEqual(self.run_command(["tick", "--now", "10000"])["ticks"][0]["model_calls"], 0)
        self.run_command(["reconcile", "--now", "10000"])
        self.assertEqual(SyntheticTarget(self.targets / "drafts.sqlite3").count(), 1)
        metadata = self.run_command(["memory", "observer", "--query", "milestone"])
        self.assertEqual(metadata["records"], [{"id": "contract", "revision": 2}])
        for result in self.evidence:
            self.assertNotIn("PRIVATE-CLI", result["stdout"] + result["stderr"])
            self.assertNotIn("PRIVATE-MEMORY", result["stdout"] + result["stderr"])

    def test_cli_unknown_cost_and_lookup_reconcile_read_actual_adapters(self):
        self.run_command(["init", "--global-cap", "200", "--now", "1000"])
        source = self.source("collector")
        self.create("collector", source=source)
        self.run_command(["tick", "--now", "1000"])
        store = Store(self.state / "controller.sqlite3")
        effect = store.effects("collector")[0]
        # Additional genuine uncertain intent with a target commit, as after a
        # process loss. CLI reconciles by querying the persistent target.
        store.intent("collector", "orphan-draft", "collect", "synthetic-drafts",
                     {"org": "org-a", "project": "pilot", "draft": True}, 1)
        store.effect_status("collector", "orphan-draft", "unknown")
        target = SyntheticTarget(self.targets / "drafts.sqlite3")
        target.put("orphan-draft", {"org": "org-a", "project": "pilot", "draft": True})
        result = self.run_command(["reconcile", "--dot", "collector", "--now", "1001"])
        self.assertEqual(result["results"][0]["confirmed"], 1)
        self.assertEqual(target.count(), 2)
        self.assertEqual(store.effects("collector")[0]["status"], "confirmed")
        self.assertEqual(store.effects("collector")[1]["status"], "confirmed")
        self.assertEqual(effect["status"], "confirmed")
        self.run_command(["reconcile", "--dot", "collector", "--reservation", "not-owned", "--actual-cost", "1"], expected=2)

    def test_cli_explicit_unknown_cost_reconciliation_across_restart(self):
        self.run_command(["init", "--global-cap", "200", "--now", "1000"])
        source = self.source("observer")
        self.create("observer", source=source)
        class UnknownCostModel:
            def decide(this, config, entry, memory):
                return {"decision": {"action": "alert", "reason_code": "relevant"}, "cost_micro_usd": None,
                        "usage": {"input_tokens": 1, "output_tokens": 1}}
        office = SyntheticOffice(self.targets / "office.sqlite3")
        engine = Engine(Store(self.state / "controller.sqlite3"), SyntheticSource(source), UnknownCostModel(),
                        SyntheticTarget(self.targets / "drafts.sqlite3"), office, clock=lambda: 1000)
        self.assertEqual(engine.tick("observer")[0]["outcome"], "pending")
        status = self.run_command(["status", "--dot", "observer"])
        self.assertEqual(status["global_budget"]["held"], 10)
        self.assertEqual(office.count(), 0)
        reservation = status["dots"][0]["unknown_reservations"][0]
        self.run_command(["reconcile", "--dot", "observer", "--reservation", reservation, "--actual-cost", "2"])
        self.assertEqual(self.run_command(["tick", "--dot", "observer", "--now", "1002"])["ticks"][0]["model_calls"], 0)
        self.assertEqual(office.count(), 1)
        final = self.run_command(["status", "--dot", "observer"])
        self.assertEqual(final["global_budget"]["spent"], 2)
        self.assertEqual(final["global_budget"]["held"], 0)
        self.assertEqual(final["dots"][0]["pending"], 0)

    def test_cli_hard_exit_before_reserve_recovers_without_phantom_hold(self):
        self.run_command(["init", "--global-cap", "200", "--now", "1000"])
        source = self.source("observer")
        self.create("observer", source=source)
        crash_script = self.fixture / "crash-before-reserve.py"
        crash_script.write_text('''import os
from pathlib import Path
import sys
sys.path.insert(0, sys.argv[1])
from dots_farm.adapters import SyntheticSource, SyntheticModel, SyntheticTarget, SyntheticOffice
from dots_farm.engine import Engine
from dots_farm.store import Store
state, targets, source = map(Path, sys.argv[2:])
engine = Engine(Store(state / "controller.sqlite3"), SyntheticSource(source),
                SyntheticModel(), SyntheticTarget(targets / "drafts.sqlite3"),
                SyntheticOffice(targets / "office.sqlite3"), clock=lambda: 1000)
def lose_process(*args, **kwargs):
    os._exit(73)
engine.store.reserve = lose_process
engine.tick("observer")
''', encoding="utf-8")
        argv = [sys.executable, "-B", str(crash_script), str(FARM), str(self.state),
                str(self.targets), str(source)]
        result = subprocess.run(argv, cwd=WORKSPACE, capture_output=True, text=True,
                                encoding="utf-8", timeout=15,
                                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        self.evidence.append({"argv": argv, "cwd": str(WORKSPACE), "exit_code": result.returncode,
                              "stdout": result.stdout.strip(), "stderr": result.stderr.strip()})
        (self.fixture / "command-evidence.json").write_text(json.dumps(self.evidence, indent=2), encoding="utf-8")
        self.assertEqual(result.returncode, 73)
        self.assertEqual(result.stdout + result.stderr, "")
        store = Store(self.state / "controller.sqlite3")
        office = SyntheticOffice(self.targets / "office.sqlite3")
        target = SyntheticTarget(self.targets / "drafts.sqlite3")
        reopened = Engine(store, SyntheticSource(source), SyntheticModel(), target, office)
        interrupted = reopened.journal.jobs("observer")[0]
        self.assertEqual((interrupted["state"], interrupted["attempts"]), ("calling", 0))
        with self.assertRaisesRegex(ValueError, "^unknown_reservation$"):
            store.settle(interrupted["reservation"], None)
        self.assertEqual(office.count(), 0)
        # A hard exit retains its real lease; recovery must wait for expiry.
        self.assertEqual(self.run_command(["tick", "--dot", "observer", "--now", "1001"])["ticks"][0]["outcome"], "leased")
        recovered = self.run_command(["tick", "--dot", "observer", "--now", "1061"])["ticks"][0]
        self.assertEqual((recovered["outcome"], recovered["model_calls"]), ("ok", 1))
        status = self.run_command(["status", "--dot", "observer"])
        self.assertEqual((status["global_budget"]["spent"], status["global_budget"]["held"]), (1, 0))
        self.assertEqual(status["dots"][0]["pending"], 0)
        self.assertEqual(status["dots"][0]["unknown_reservations"], [])
        completed = reopened.journal.jobs("observer", pending=False)[0]
        self.assertEqual((completed["state"], completed["attempts"]), ("done", 1))
        self.assertNotEqual(completed["reservation"], interrupted["reservation"])
        self.assertEqual(self.run_command(["tick", "--dot", "observer", "--now", "1062"])["ticks"][0]["model_calls"], 0)
        self.assertEqual(office.count(), 1)
        self.assertEqual(target.count(), 0)

    def test_cli_hard_exit_after_zero_settlement_recovers_with_fresh_hold(self):
        self.run_command(["init", "--global-cap", "200", "--now", "1000"])
        source = self.source("observer")
        self.create("observer", source=source)
        crash_script = self.fixture / "crash-after-zero-settlement.py"
        crash_script.write_text('''import os
from pathlib import Path
import sys
sys.path.insert(0, sys.argv[1])
from dots_farm.adapters import SyntheticSource, SyntheticTarget, SyntheticOffice
from dots_farm.engine import Engine
from dots_farm.store import Store
state, targets, source = map(Path, sys.argv[2:])
class NoCallModel:
    def decide(self, config, item, memory):
        (state / "unexpected-inference.txt").write_text("called", encoding="utf-8")
        raise AssertionError("inference_must_not_start")
engine = Engine(Store(state / "controller.sqlite3"), SyntheticSource(source),
                NoCallModel(), SyntheticTarget(targets / "drafts.sqlite3"),
                SyntheticOffice(targets / "office.sqlite3"), clock=lambda: 1000)
reserve, settle = engine.store.reserve, engine.store.settle
def cancel(*args, **kwargs):
    held = reserve(*args, **kwargs)
    engine.store.pause("observer")
    return held
def lose_process(reservation, actual=None):
    result = settle(reservation, actual)
    if actual == 0:
        os._exit(74)
    return result
engine.store.reserve = cancel
engine.store.settle = lose_process
engine.tick("observer")
''', encoding="utf-8")
        argv = [sys.executable, "-B", str(crash_script), str(FARM), str(self.state),
                str(self.targets), str(source)]
        result = subprocess.run(argv, cwd=WORKSPACE, capture_output=True, text=True,
                                encoding="utf-8", timeout=15,
                                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        self.evidence.append({"argv": argv, "cwd": str(WORKSPACE), "exit_code": result.returncode,
                              "stdout": result.stdout.strip(), "stderr": result.stderr.strip()})
        (self.fixture / "command-evidence.json").write_text(json.dumps(self.evidence, indent=2), encoding="utf-8")
        self.assertEqual(result.returncode, 74)
        self.assertEqual(result.stdout + result.stderr, "")
        self.assertFalse((self.state / "unexpected-inference.txt").exists())
        store = Store(self.state / "controller.sqlite3")
        office = SyntheticOffice(self.targets / "office.sqlite3")
        target = SyntheticTarget(self.targets / "drafts.sqlite3")
        reopened = Engine(store, SyntheticSource(source), SyntheticModel(), target, office)
        interrupted = reopened.journal.jobs("observer")[0]
        self.assertEqual((interrupted["state"], interrupted["attempts"]), ("calling", 0))
        with closing(sqlite3.connect(store.path)) as conn:
            cancelled = conn.execute("SELECT status, actual FROM reservations WHERE id=?",
                                     (interrupted["reservation"],)).fetchone()
        self.assertEqual(cancelled, ("settled", 0))
        self.assertEqual((store.budget_status()["spent"], store.budget_status()["held"]), (0, 0))
        self.assertEqual(office.count(), 0)
        self.assertEqual(target.count(), 0)
        self.assertTrue(self.run_command(["status", "--dot", "observer"])["dots"][0]["paused"])
        self.assertEqual(self.run_command(["tick", "--dot", "observer", "--now", "1001"])["ticks"][0]["outcome"], "paused")
        self.run_command(["resume", "observer"])
        recovered = self.run_command(["tick", "--dot", "observer", "--now", "1061"])["ticks"][0]
        self.assertEqual((recovered["outcome"], recovered["model_calls"]), ("ok", 1))
        status = self.run_command(["status", "--dot", "observer"])
        self.assertEqual((status["global_budget"]["spent"], status["global_budget"]["held"]), (1, 0))
        self.assertEqual(status["dots"][0]["pending"], 0)
        self.assertEqual(status["dots"][0]["unknown_reservations"], [])
        completed = reopened.journal.jobs("observer", pending=False)[0]
        self.assertEqual((completed["state"], completed["attempts"]), ("done", 1))
        self.assertNotEqual(completed["reservation"], interrupted["reservation"])
        self.assertEqual(self.run_command(["tick", "--dot", "observer", "--now", "1062"])["ticks"][0]["model_calls"], 0)
        self.assertEqual(office.count(), 1)
        self.assertEqual(target.count(), 0)

    def test_cli_paused_lookup_recovery_runbook_preserves_stop(self):
        self.run_command(["init", "--global-cap", "200", "--now", "1000"])
        self.create("collector", source=self.source("collector"))
        store = Store(self.state / "controller.sqlite3")
        payload = {"org": "org-a", "project": "pilot", "draft": True}
        store.intent("collector", "orphan-draft", "collect", "synthetic-drafts", payload, 1)
        store.effect_status("collector", "orphan-draft", "unknown")
        target = SyntheticTarget(self.targets / "drafts.sqlite3")
        target.put("orphan-draft", payload)
        self.run_command(["pause", "collector"])
        paused = self.run_command(["reconcile", "--dot", "collector", "--now", "1000"])["results"][0]
        self.assertEqual((paused["outcome"], paused["confirmed"]), ("paused_or_leased", 0))
        self.assertEqual(store.effects("collector")[0]["status"], "unknown")
        self.assertTrue(self.run_command(["status", "--dot", "collector"])["dots"][0]["paused"])
        # The documented recovery window runs lookup only, with no tick/timer.
        self.run_command(["resume", "collector"])
        reconciled = self.run_command(["reconcile", "--dot", "collector", "--now", "1001"])["results"][0]
        self.assertEqual((reconciled["outcome"], reconciled["confirmed"]), ("lookup_only", 1))
        self.run_command(["pause", "collector"])
        final = self.run_command(["status", "--dot", "collector"])
        self.assertTrue(final["dots"][0]["paused"])
        self.assertEqual(final["dots"][0]["unconfirmed_effects"], 0)
        self.assertEqual((final["global_budget"]["spent"], final["global_budget"]["held"]), (0, 0))
        self.assertEqual(target.count(), 1)
        self.assertEqual(SyntheticOffice(self.targets / "office.sqlite3").count(), 0)

    def test_cli_legacy_phantom_reservation_can_be_explicitly_recovered(self):
        self.run_command(["init", "--global-cap", "0", "--now", "1000"])
        source = self.source("observer")
        self.create("observer", source=source)
        self.assertEqual(self.run_command(["tick", "--dot", "observer", "--now", "1000"])["ticks"][0]["model_calls"], 0)
        store = Store(self.state / "controller.sqlite3")
        office = SyntheticOffice(self.targets / "office.sqlite3")
        engine = Engine(store, SyntheticSource(source), SyntheticModel(),
                        SyntheticTarget(self.targets / "drafts.sqlite3"), office)
        job = engine.journal.jobs("observer")[0]
        missing = job["reservation"]
        with self.assertRaisesRegex(ValueError, "^unknown_reservation$"):
            store.settle(missing, None)
        # Persist the old recovery result: blocked, but no call or Store hold.
        engine.journal.update(job, state="blocked", reason="cost_unknown")
        self.assertEqual(self.run_command(["status", "--dot", "observer"])["dots"][0]["unknown_reservations"], [missing])
        self.run_command(["reconcile", "--dot", "observer", "--reservation", missing,
                          "--actual-cost", "1"], expected=2)
        engine.journal.update(job, decision_json=json.dumps({"action": "alert", "reason_code": "relevant"}))
        self.run_command(["reconcile", "--dot", "observer", "--reservation", missing,
                          "--actual-cost", "0"], expected=2)
        self.assertEqual(office.count(), 0)
        engine.journal.update(job, decision_json=None)
        reconciled = self.run_command(["reconcile", "--dot", "observer", "--reservation", missing,
                                       "--actual-cost", "0"])
        self.assertEqual(reconciled["outcome"], "pre_inference_recovered")
        self.assertEqual(office.count(), 0)
        ready = self.run_command(["status", "--dot", "observer"])
        self.assertEqual(ready["dots"][0]["unknown_reservations"], [])
        self.assertEqual((ready["global_budget"]["spent"], ready["global_budget"]["held"]), (0, 0))
        self.run_command(["init", "--global-cap", "200", "--now", "1000"])
        completed = self.run_command(["tick", "--dot", "observer", "--now", "1002"])["ticks"][0]
        self.assertEqual((completed["outcome"], completed["model_calls"]), ("ok", 1))
        self.assertEqual(office.count(), 1)
        final = self.run_command(["status", "--dot", "observer"])
        self.assertEqual(final["dots"][0]["pending"], 0)
        self.assertEqual((final["global_budget"]["spent"], final["global_budget"]["held"]), (1, 0))
        reopened = engine.journal.jobs("observer", pending=False)[0]
        self.assertEqual(reopened["attempts"], 1)
        self.assertNotEqual(reopened["reservation"], missing)

    def test_cli_interrupted_decision_cost_policy_survives_restart(self):
        self.run_command(["init", "--global-cap", "200", "--now", "1000"])
        source = self.source("observer")
        office = SyntheticOffice(self.targets / "office.sqlite3")
        target = SyntheticTarget(self.targets / "drafts.sqlite3")
        class Interrupted(BaseException):
            pass
        class ReplyModel:
            def __init__(this, actual):
                this.actual = actual
            def decide(this, config, entry, memory):
                return {"decision": {"action": "alert", "reason_code": "relevant"},
                        "cost_micro_usd": this.actual,
                        "usage": {"input_tokens": 1, "output_tokens": 1}}
        def interrupt(*args, **kwargs):
            raise Interrupted
        for dot_id, actual, outcome, reason in (("unknown", None, "pending", "cost_unknown"),
                                                ("overrun", 11, "stopped_or_fenced", "cost_overrun")):
            with self.subTest(dot_id=dot_id):
                self.create("observer", name=dot_id, source=source)
                engine = Engine(Store(self.state / "controller.sqlite3"), SyntheticSource(source),
                                ReplyModel(actual), target, office, clock=lambda: 1000)
                engine.store.settle = interrupt
                with self.assertRaises(Interrupted):
                    engine.tick(dot_id)
                self.assertEqual(engine.journal.jobs(dot_id)[0]["state"], "decided")
                result = self.run_command(["tick", "--dot", dot_id, "--now", "1002"])["ticks"][0]
                self.assertEqual(result["outcome"], outcome)
                self.assertEqual(result["model_calls"], 0)
                status = self.run_command(["status", "--dot", dot_id])["dots"][0]
                self.assertIn(reason, status["pending_reasons"])
                self.assertEqual(status["budget"]["held"], 10 if actual is None else 0)
                self.assertEqual(status["budget"]["spent"], actual or 0)
                self.assertEqual(status["paused"], actual is not None)
                self.assertEqual(office.count(), 0)
                self.assertEqual(target.count(), 0)

    def test_pause_cli_independent_of_hung_model_process(self):
        self.run_command(["init", "--global-cap", "100", "--now", "1000"])
        source = self.source("observer")
        self.create("observer", source=source)
        started = self.fixture / "model-started.txt"
        code = """import json,sys,time
from pathlib import Path
from dots_farm.engine import Engine
from dots_farm.store import Store
from dots_farm.adapters import SyntheticSource,SyntheticModel,SyntheticTarget,SyntheticOffice
state,targets,source,started=map(Path,sys.argv[1:])
class HangingModel:
    def decide(self,config,item,memory):
        started.write_text('started')
        time.sleep(2)
        return SyntheticModel().decide(config,item,memory)
engine=Engine(Store(state/'controller.sqlite3'),SyntheticSource(source),HangingModel(),
              SyntheticTarget(targets/'drafts.sqlite3'),SyntheticOffice(targets/'office.sqlite3'),clock=lambda:1000)
print(json.dumps(engine.tick('observer')))
"""
        env = {**os.environ, "PYTHONPATH": str(FARM), "PYTHONDONTWRITEBYTECODE": "1"}
        argv = [sys.executable, "-B", "-c", code, str(self.state), str(self.targets), str(source), str(started)]
        child = subprocess.Popen(argv, cwd=WORKSPACE, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                 text=True, creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        supervisor_started = self.fixture / "supervisor-started.txt"
        # Passive synthetic supervisor process, not installed Hermes. It stays
        # hung concurrently and cannot own controller/STOP connections.
        supervisor = subprocess.Popen([sys.executable, "-B", "-c",
                                       "import pathlib,sys,time; pathlib.Path(sys.argv[1]).write_text('started'); time.sleep(20)",
                                       str(supervisor_started)], cwd=WORKSPACE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                      creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        try:
            deadline = time.monotonic() + 5
            while (not started.exists() or not supervisor_started.exists()) and time.monotonic() < deadline:
                time.sleep(0.02)
            self.assertTrue(started.exists())
            self.assertTrue(supervisor_started.exists())
            self.assertIsNone(supervisor.poll())
            begin = time.monotonic()
            self.run_command(["pause", "observer"])
            self.assertLess(time.monotonic() - begin, 1.5)
            out, err = child.communicate(timeout=10)
            self.assertEqual(child.returncode, 0, err)
            self.assertEqual(json.loads(out)[0]["outcome"], "stopped_or_fenced")
            self.assertEqual(SyntheticOffice(self.targets / "office.sqlite3").count(), 0)
            self.assertTrue(Store(self.state / "controller.sqlite3").get_dot("observer")["paused"])
        finally:
            if supervisor.poll() is None:
                supervisor.terminate()
            supervisor.communicate(timeout=5)
            if child.poll() is None:
                child.terminate()
                child.communicate(timeout=5)

    def test_demo_clean_hermes_plan_and_validation(self):
        demo = self.run_command(["demo"])
        self.assertEqual(len(demo["ticks"]), 2)
        self.assertEqual(demo["status"]["global_budget"]["cap"], 5000000)
        plan = self.run_command(["hermes-plan"])
        self.assertEqual(plan["live_qualification"], "BLOCKED")
        self.assertTrue(plan["wake"]["no_agent"])
        self.assertEqual(plan["existing_profiles_touched"], [])
        self.assertFalse(plan["supervisor_powers"]["bypass_stop"])
        self.run_command(["demo"], expected=2)
        self.run_command(["create", "dst", "--kind", "observer", "--org", "org-a", "--project", "pilot", "--timezone", "Europe/Warsaw"], expected=2)
        self.run_command(["init", "--global-cap", "-1"], expected=2)
        self.run_command(["create", "../bad", "--kind", "observer", "--org", "org-a", "--project", "pilot"], expected=2)
        argv = [sys.executable, "-B", str(FARM / "dots.py"), "--state", "relative", "status"]
        result = subprocess.run(argv, capture_output=True, text=True, timeout=10,
                                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        self.assertEqual(result.returncode, 2)

    def test_creator_helper_runs_real_create_and_preserves_existing_dot(self):
        self.run_command(["init", "--global-cap", "100"])
        helper = WORKSPACE / "global/codex/skills/create-dots/scripts/create.py"
        self.run_command(["--runtime", str(FARM / "dots.py"), "observer", "--kind", "observer", "--org", "org-a", "--project", "pilot", "--budget", "100"], entry=helper)
        self.assertEqual(self.run_command(["status"])["dots"][0]["id"], "observer")
        self.run_command(["--runtime", str(FARM / "dots.py"), "observer", "--kind", "observer", "--org", "org-a", "--project", "pilot"], expected=2, entry=helper)
        self.assertEqual(len(Store(self.state / "controller.sqlite3").list_dots()), 1)

    def test_creator_absolute_paths_from_other_cwd_and_installed_skill(self):
        self.run_command(["init", "--global-cap", "100"])
        other_cwd = self.fixture / "another-project"
        other_cwd.mkdir()
        origin = WORKSPACE / "global/codex/skills/create-dots/scripts/create.py"
        installed = self.fixture / "installed-skills/create-dots/scripts/create.py"
        installed.parent.mkdir(parents=True)
        with installed.open("xb") as stream:
            stream.write(origin.read_bytes())
        for helper, name in ((origin, "origin-observer"), (installed, "installed-observer")):
            self.run_command(["--runtime", str(FARM / "dots.py"), name, "--kind", "observer",
                              "--org", "org-a", "--project", "pilot", "--budget", "50"],
                             entry=helper, cwd=other_cwd)
        dots = self.run_command(["status"], cwd=other_cwd)["dots"]
        self.assertEqual({dot["id"] for dot in dots}, {"origin-observer", "installed-observer"})


if __name__ == "__main__":
    unittest.main()
