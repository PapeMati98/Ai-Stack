"""Regression: a test run must not leave temp directories in the shared-skill origin tree.

Measured 2026-09-21, before the fix. `cd scripts && python -m unittest test_todo_graph`
(10 tests, OK) left 8 `tmp<random>/` directories in `scripts/`, each holding
`run-<hex>/{state.json,todo.json,<task>/{progress.json,result.json}}`. This directory is the
ORIGIN of the shared skills: bootstrap mirrors it into every agent tree and the private
backup ships it, so the litter propagated into all three agent trees. Measurement before
cleanup: 38 directories / 14 110 files / 3.35 MB in the skill root, 16 directories in
`scripts/`, 128 directories in each of the three agent trees. The backup shrank by exactly
3.35 MB after cleanup.

Cause (2026-09-21), and it is NOT a tempfile problem:
  * `test_todo_graph.TodoGraphTests.run_plan` built the run directory as
    `Path(self.temp.name)`. `self.temp` is ALREADY a `Path`, so `.name` is only its LAST
    COMPONENT (`tmp<random>`); `run_dir` became the RELATIVE path `tmp<random>/run-<hex>`.
    `todo_graph._run` does `folder = Path(folder).resolve()` (todo_graph.py:303), so the
    relative path was resolved against the current working directory - the shared-skill
    origin - and every call planted a `tmp<random>/` there.
  * `TemporaryDirectory()` always landed in %TEMP% and `cleanup()` always removed it, which
    is why the leak looked like a tempfile problem. The directory that survived was never
    the one teardown removed.
  * The count matches exactly: 8 of the 10 tests call `run_plan`
    (`test_resume_preserves_previous_accepted_result` uses an absolute `self.temp / "run"`,
    and `test_validate_rejects_cycle...` runs nothing). 8 directories, 8 callers.

Trope (a) checked 2026-09-21, ruled out: nothing in `scripts/` assigns `tempfile.tempdir`
or `os.environ["TEMP"/"TMP"]`, which is why a direct `TemporaryDirectory()` call from
`scripts/` lands in %TEMP% while the test suite did not.
Trope (b) checked 2026-09-21, ruled out: every child process inherits the environment - all
`env=` sites copy `os.environ` (graph_runner.py:528, test_command_adapter.py:58,
test_fleet_bound.py:46, test_fleet_scheduler_hooks.py:133) - so TEMP/TMP always reach the
child, and `tempfile.gettempdir()` never falls back to the working directory.
Trope (c) checked 2026-09-21, ruled out: `cleanup()` never failed. It removed the real
%TEMP% directory; the directory left behind was a different one.

Second source, checked 2026-09-21 and NOT found in this suite: every other module here that
uses `TemporaryDirectory`/`mkdtemp` joins onto the full temp path or onto
`TemporaryDirectory().name`, never onto `Path(...).name`:
test_agent_graph.py:280, test_fleet_bound.py:16, test_jit_harness.py:98,
test_todo_fleet.py:75 (all `Path(self.temp.name)` where `self.temp` is the
TemporaryDirectory OBJECT, i.e. the full path), while test_command_adapter,
test_fleet_scheduler_hooks, test_graph_runner, test_office_bridge and test_run_telemetry
use the context-manager value directly. No code path in `scripts/` can put 12 627 files
into ONE directory: a leaked run directory holds a few dozen files (state.json, todo.json,
progress.json, result.json per task). That historic directory in the skill root therefore
came from OUTSIDE this suite - a real run started with the skill root as its working
directory, or external tooling. It is not closed out here by assumption but by measurement:
both probes below run with the working directory moved to a throwaway sandbox, so any
relative litter from `scripts/` code lands there and is counted.

Why this file drives ONE real `run_plan` call instead of replaying the whole
`test_todo_graph` suite (decided 2026-09-21): that suite asserts on timing
(`threading.Barrier(2)` with a 3 s timeout, `Event.wait(timeout=0.15)`), so a detector that
replays it inherits that flakiness under a loaded machine and roughly doubles its runtime.
A detector that can fail for a reason unrelated to the leak is not evidence of the leak.
The probe below calls the real `TodoGraphTests.run_plan` on a single-task plan - the exact
production path that leaked - and asserts the task really reached ACCEPTED, so an
accidentally empty probe cannot read as "no leak".
"""
import importlib.util
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
SKILL_ROOT = SCRIPTS.parent
PROBE_MODULE_FILE = SCRIPTS / "test_todo_graph.py"


def temp_dirs(root):
    return {path.name for path in Path(root).glob("tmp*") if path.is_dir()}


def load_todo_graph_tests():
    """Import a private copy of test_todo_graph by FILE PATH.

    Loading by path instead of by module name keeps this check independent of sys.path and
    of the current working directory - the check itself moves the working directory, so a
    name-based import could resolve against the sandbox and fail for the wrong reason.
    """
    if str(SCRIPTS) not in sys.path:
        sys.path.insert(0, str(SCRIPTS))
    spec = importlib.util.spec_from_file_location("test_todo_graph_leak_probe", PROBE_MODULE_FILE)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def probe_run_plan(module, run_plan=None):
    """Drive one real `run_plan` call with the working directory moved to a sandbox.

    Returns (state, leaked). `leaked` is the set of `tmp*` directories the call planted in
    the sandbox, i.e. in the working directory - exactly the 2026-09-21 failure shape.
    """
    case = module.TodoGraphTests(methodName="run_plan")
    case.setUp()
    original = os.getcwd()
    sandbox = tempfile.mkdtemp()
    try:
        os.chdir(sandbox)
        plan = case.make_plan(case.temp, [case.task("probe", ["probe"])])
        call = run_plan or module.TodoGraphTests.run_plan
        state = call(case, plan, module.FakeAdapter())
    finally:
        os.chdir(original)
        leaked = temp_dirs(sandbox)
        case.tearDown()
        shutil.rmtree(sandbox, ignore_errors=True)
    return state, leaked


class SkillTreeTempLeakTests(unittest.TestCase):
    def test_run_plan_leaves_no_temp_dir_in_the_skill_tree(self):
        module = load_todo_graph_tests()
        before_root = temp_dirs(SKILL_ROOT)
        before_scripts = temp_dirs(SCRIPTS)
        state, leaked = probe_run_plan(module)
        # The probe must really have executed the scheduler, otherwise "no leak" is vacuous.
        self.assertEqual(state["nodes"]["probe"]["status"], "ACCEPTED")
        self.assertEqual(leaked, set(), "run_plan planted temp dirs in the working directory: %s" % sorted(leaked))
        self.assertEqual(temp_dirs(SCRIPTS) - before_scripts, set(), "new tmp* in scripts/")
        self.assertEqual(temp_dirs(SKILL_ROOT) - before_root, set(), "new tmp* in the skill root")

    def test_detector_catches_the_historical_relative_run_dir(self):
        """Guard the guard: prove the check above fails on the 2026-09-21 implementation."""
        module = load_todo_graph_tests()

        def historical_run_plan(self, plan, adapter=None, approved=()):
            # The exact historical line. `self.temp` is a Path, so `.name` is only
            # `tmp<random>`. After 2026-09-21 hardening, run() refuses that relative
            # path instead of resolving it against the working directory.
            run_dir = Path(self.temp.name) / ("run-" + module.todo_graph.digest(plan)[:12])
            return module.todo_graph.run(plan, run_dir, adapter=adapter, approved=approved)

        with self.assertRaises(ValueError) as ctx:
            probe_run_plan(module, historical_run_plan)
        self.assertIn("absolute", str(ctx.exception).lower())

    def test_detector_sees_an_abandoned_temp_directory(self):
        # tempfile names every directory `tmp<random>`, so the checks above are only worth
        # anything while that shape is really matched.
        with tempfile.TemporaryDirectory() as sandbox:
            (Path(sandbox) / "tmpabandoned").mkdir()
            self.assertEqual(temp_dirs(sandbox), {"tmpabandoned"})


if __name__ == "__main__":
    unittest.main()
