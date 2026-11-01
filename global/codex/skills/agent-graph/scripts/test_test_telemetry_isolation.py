"""Scheduler component tests must not publish TODO journals into production home.

The RSI runner starts with AGENT_TODO_TELEMETRY=off, which hides this leak.
This module turns telemetry ON under a synthetic home and drives a real
TodoGraphTests scheduler execution. Journals may still be written only to an
explicit Journal root; the production-shaped home path stays empty.
"""
import importlib.util
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import run_telemetry as telemetry


def load_todo_graph_tests():
    spec = importlib.util.spec_from_file_location(
        "test_todo_graph_telemetry_probe", SCRIPTS / "test_todo_graph.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def journal_files(home):
    root = Path(home) / ".agents" / "telemetry" / "todo"
    if not root.exists():
        return []
    return sorted(path for path in root.rglob("*.jsonl") if path.is_file())


class SchedulerTelemetryIsolationTests(unittest.TestCase):
    def setUp(self):
        self._telemetry = os.environ.get("AGENT_TODO_TELEMETRY")
        self._profile = os.environ.get("USERPROFILE")
        self._home = os.environ.get("HOME")
        self.fake_home = Path(tempfile.mkdtemp(prefix="fake-prod-home-"))
        os.environ["AGENT_TODO_TELEMETRY"] = "on"
        os.environ["USERPROFILE"] = str(self.fake_home)
        os.environ["HOME"] = str(self.fake_home)

    def tearDown(self):
        self._restore("AGENT_TODO_TELEMETRY", self._telemetry)
        self._restore("USERPROFILE", self._profile)
        self._restore("HOME", self._home)

    @staticmethod
    def _restore(name, previous):
        if previous is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = previous

    def test_scheduler_stays_accepted_without_production_home_journals(self):
        self.assertEqual(Path.home().resolve(), self.fake_home.resolve())
        self.assertEqual(os.environ.get("AGENT_TODO_TELEMETRY"), "on")
        module = load_todo_graph_tests()
        case = module.TodoGraphTests("run_plan")
        case.setUp()
        try:
            plan = case.make_plan(case.temp, [case.task("probe", ["probe"])])
            state = case.run_plan(plan, module.FakeAdapter())
        finally:
            case.tearDown()
            case.doCleanups()
        self.assertEqual(state["nodes"]["probe"]["status"], "ACCEPTED")
        # The component fixture must put back the caller's explicit ON.
        # A process-global off left behind would make the next Journal test meaningless.
        self.assertEqual(os.environ.get("AGENT_TODO_TELEMETRY"), "on")
        self.assertEqual(journal_files(self.fake_home), [])

        explicit = Path(tempfile.mkdtemp(prefix="explicit-journal-"))
        journal = telemetry.Journal(explicit)
        journal.emit("node_terminal", node="probe", status="ACCEPTED")
        closed = journal.close()
        self.assertTrue(closed["enabled"])
        self.assertTrue(closed["complete"])
        self.assertTrue(journal.path.is_file())
        self.assertIn("node_terminal", journal.path.read_text(encoding="utf-8"))
        self.assertFalse(journal.path.resolve().is_relative_to(
            (self.fake_home / ".agents" / "telemetry").resolve()))
        self.assertEqual(journal_files(self.fake_home), [])

    def test_unset_telemetry_defaults_on_and_root_stays_home_todo(self):
        os.environ.pop("AGENT_TODO_TELEMETRY", None)
        explicit = Path(tempfile.mkdtemp(prefix="default-on-journal-"))
        evidence = explicit / "evidence"
        evidence.mkdir()
        located = telemetry.root_for(evidence)
        self.assertEqual(
            located,
            Path.home() / ".agents" / "telemetry" / "todo" / located.name,
        )
        self.assertEqual(journal_files(self.fake_home), [])
        journal = telemetry.Journal(explicit)
        self.assertTrue(journal.enabled)
        journal.emit("run_start")
        closed = journal.close()
        self.assertTrue(closed["enabled"])
        self.assertTrue(closed["complete"])
        self.assertTrue(journal.path.is_file())
        self.assertEqual(journal_files(self.fake_home), [])

    def test_module_scope_fixture_turns_telemetry_off_and_restores_the_caller(self):
        """The module-scoped fixture must be scoped, and must restore the caller.

        Importing the module must not touch the environment; only the explicit
        setUpModule/tearDownModule pair may change it, and it must hand the
        caller's value back.
        """
        module = load_todo_graph_tests()
        self.assertEqual(os.environ.get("AGENT_TODO_TELEMETRY"), "on")
        module.setUpModule()
        try:
            self.assertEqual(os.environ.get("AGENT_TODO_TELEMETRY"), "off")
        finally:
            module.tearDownModule()
        self.assertEqual(os.environ.get("AGENT_TODO_TELEMETRY"), "on")

    def test_child_process_inherits_the_scoped_telemetry_value(self):
        """A child process must inherit the scoped OFF value, not the caller's ON."""
        module = load_todo_graph_tests()
        module.setUpModule()
        try:
            child_env = dict(os.environ)
            self.assertEqual(child_env.get("AGENT_TODO_TELEMETRY"), "off")
            completed = subprocess.run(
                [sys.executable, "-c",
                 "import os; print(os.environ.get('AGENT_TODO_TELEMETRY'))"],
                env=child_env, capture_output=True, text=True, timeout=60,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        finally:
            module.tearDownModule()
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(completed.stdout.strip(), "off")
        self.assertEqual(os.environ.get("AGENT_TODO_TELEMETRY"), "on")


if __name__ == "__main__":
    unittest.main()
