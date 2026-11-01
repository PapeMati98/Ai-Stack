import importlib.util
import os
import sys
import threading
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

# Never publish ClawOps Watch events from the test suite to the live Office board.
os.environ["AGENT_GRAPH_WATCH_EVENTS"] = "0"


MODULE_PATH = Path(__file__).with_name("todo_graph.py")
SPEC = importlib.util.spec_from_file_location("todo_graph_under_test", MODULE_PATH)
todo_graph = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = todo_graph
SPEC.loader.exec_module(todo_graph)

# Synthetic scheduler runs must never publish TODO journals into the real user
# home. The module fixture turns telemetry OFF for the whole module and the
# caller's explicit setting is restored on teardown, so an explicit
# Journal(root) keeps working. Nothing is changed at import time.
_test_telemetry = patch.dict(os.environ, {"AGENT_TODO_TELEMETRY": "off"})


def setUpModule():
    _test_telemetry.start()


def tearDownModule():
    _test_telemetry.stop()


class FakeAdapter:
    def __init__(self, behavior=None):
        self.behavior = behavior or self.default_behavior
        self.calls = []
        self.lock = threading.Lock()

    @staticmethod
    def default_behavior(plan, task, role, workspace, folder, feedback, maker=None):
        if role == "maker":
            return {"status": "CANDIDATE", "lineage": "maker-lineage"}
        return {
            "status": "REVIEWED",
            "verdict": "ACCEPT",
            "checked": list(task["done_when"]),
            "findings": [],
            "lineage": "reviewer-lineage",
        }

    def invoke(self, plan, task, role, workspace, folder, feedback, maker=None):
        with self.lock:
            self.calls.append((task["id"], role, feedback, maker))
        return self.behavior(plan, task, role, workspace, folder, feedback, maker)


class TodoGraphTests(unittest.TestCase):
    def make_plan(self, root, tasks, max_parallel=2, requirements=None):
        requirements = requirements or ["REQ-1"]
        return {
            "version": 1,
            "goal": "test TODO scheduler",
            "planner": {"model": "parent-model", "source": "parent"},
            "requirements": requirements,
            "max_parallel": max_parallel,
            "workspace": str(root),
            "execution_context": "legacy-test",
            "tasks": tasks,
        }

    def task(self, task_id, scope, depends_on=None, requires_approval=None, checks=None):
        return {
            "id": task_id,
            "text": "perform " + task_id,
            "covers": ["REQ-1"],
            "done_when": ["evidence for " + task_id],
            "scope": scope,
            "inputs": [],
            "depends_on": depends_on or [],
            "maker": [{"agent": "maker", "model": "m-model", "reason": "fit", "score": 80}],
            "reviewer": [{"agent": "reviewer", "model": "r-model", "reason": "fit", "score": 80}],
            "checks": checks if checks is not None else [[sys.executable, "-c", "pass"]],
            "requires_approval": requires_approval or [],
        }

    def run_plan(self, plan, adapter=None, approved=()):
        # 2026-09-21: this used to be `Path(self.temp.name)`. `self.temp` is already
        # a Path, so `.name` returned only its LAST COMPONENT (`tmp<random>`), and the
        # run directory `tmp<random>/run-<hex>` became a RELATIVE path. `todo_graph._run`
        # does `Path(folder).resolve()`, so it landed under the current working directory
        # - the shared-skill origin - once per run_plan call. Measured from `scripts/`:
        # 8 directories per `python -m unittest test_todo_graph`, and exactly 8 of the
        # 10 tests call run_plan (test_resume_preserves_previous_accepted_result uses an
        # absolute `self.temp / "run"`, test_validate_rejects_cycle... runs nothing).
        # teardown only removed the real %TEMP% directory, so those survived and got
        # mirrored by bootstrap into every agent tree. tempfile itself was never at fault:
        # TemporaryDirectory() always landed in %TEMP%. Join onto the Path, never onto
        # its `.name`. Detector: scripts/test_no_temp_leak.py.
        run_dir = self.temp / ("run-" + todo_graph.digest(plan)[:12])
        return todo_graph.run(plan, run_dir, adapter=adapter, approved=approved)

    def setUp(self):
        self.temp_context = TemporaryDirectory()
        self.temp = Path(self.temp_context.name)
        # Per-test scope: telemetry OFF for this synthetic run, restored to the
        # caller's value on teardown. Manual setUp/tearDown callers (the
        # telemetry isolation regression) must get their explicit ON back.
        self._telemetry_patch = patch.dict(os.environ, {"AGENT_TODO_TELEMETRY": "off"})
        self._telemetry_patch.start()
        self.addCleanup(self._telemetry_patch.stop)

    def tearDown(self):
        try:
            self.temp_context.cleanup()
        finally:
            self._telemetry_patch.stop()

    def test_independent_tasks_are_actually_concurrent(self):
        reached = threading.Barrier(2)
        maker_ids = set()

        def behavior(plan, task, role, workspace, folder, feedback, maker=None):
            if role == "maker":
                maker_ids.add(task["id"])
                reached.wait(timeout=3)
                return {"status": "CANDIDATE", "lineage": task["id"] + "-maker"}
            return {"status": "REVIEWED", "verdict": "ACCEPT", "checked": list(task["done_when"]), "findings": [], "lineage": task["id"] + "-reviewer"}

        adapter = FakeAdapter(behavior)
        plan = self.make_plan(self.temp, [self.task("a", ["a.txt"]), self.task("b", ["b.txt"])])
        result = self.run_plan(plan, adapter)
        self.assertEqual(result["status"], "ACCEPTED_CANDIDATE")
        self.assertEqual(maker_ids, {"a", "b"})

    def test_implement_task_without_executable_check_is_rejected(self):
        task = self.task("a", ["a.txt"], checks=[])
        with self.assertRaisesRegex(ValueError, "requires at least one executable check"):
            todo_graph.validate(self.make_plan(self.temp, [task]))

    def test_tool_call_override_requires_valid_role_and_bounded_integer(self):
        task = self.task("a", ["a.txt"])
        for invalid in ({"maker": True}, {"maker": 257}, {"other": 12}, 48):
            with self.subTest(invalid=invalid):
                task["max_model_tool_calls"] = invalid
                with self.assertRaisesRegex(ValueError, "max_model_tool_calls"):
                    todo_graph.validate(self.make_plan(self.temp, [task]))
        task["max_model_tool_calls"] = {"maker": 72, "reviewer": 18}
        todo_graph.validate(self.make_plan(self.temp, [task]))

    def test_overlapping_scopes_are_serialized(self):
        first_entered = threading.Event()
        release_first = threading.Event()
        second_entered = threading.Event()

        def behavior(plan, task, role, workspace, folder, feedback, maker=None):
            if role == "maker" and task["id"] == "a":
                first_entered.set()
                self.assertTrue(release_first.wait(timeout=3))
                return {"status": "CANDIDATE", "lineage": "a-maker"}
            if role == "maker" and task["id"] == "b":
                second_entered.set()
                return {"status": "CANDIDATE", "lineage": "b-maker"}
            return {"status": "REVIEWED", "verdict": "ACCEPT", "checked": list(task["done_when"]), "findings": [], "lineage": task["id"] + "-reviewer"}

        adapter = FakeAdapter(behavior)
        plan = self.make_plan(self.temp, [self.task("a", ["shared"]), self.task("b", ["shared/file"])])
        result_holder = []
        thread = threading.Thread(target=lambda: result_holder.append(self.run_plan(plan, adapter)))
        thread.start()
        self.assertTrue(first_entered.wait(timeout=3))
        self.assertFalse(second_entered.wait(timeout=0.15))
        release_first.set()
        thread.join(timeout=3)
        self.assertFalse(thread.is_alive())
        self.assertEqual(result_holder[0]["status"], "ACCEPTED_CANDIDATE")

    def test_dependencies_wait_for_accepted(self):
        maker_started = threading.Event()
        release = threading.Event()
        dependent_called = threading.Event()

        def behavior(plan, task, role, workspace, folder, feedback, maker=None):
            if task["id"] == "a" and role == "maker":
                maker_started.set()
                self.assertTrue(release.wait(timeout=3))
                return {"status": "CANDIDATE", "lineage": "a-maker"}
            if task["id"] == "b":
                dependent_called.set()
            if role == "maker":
                return {"status": "CANDIDATE", "lineage": task["id"] + "-maker"}
            return {"status": "REVIEWED", "verdict": "ACCEPT", "checked": list(task["done_when"]), "findings": [], "lineage": task["id"] + "-reviewer"}

        adapter = FakeAdapter(behavior)
        plan = self.make_plan(self.temp, [self.task("a", ["a"]), self.task("b", ["b"], depends_on=["a"])])
        result_holder = []
        thread = threading.Thread(target=lambda: result_holder.append(self.run_plan(plan, adapter)))
        thread.start()
        self.assertTrue(maker_started.wait(timeout=3))
        self.assertFalse(dependent_called.is_set())
        release.set()
        thread.join(timeout=3)
        self.assertEqual(result_holder[0]["status"], "ACCEPTED_CANDIDATE")

    def test_maker_and_reviewer_need_different_actual_lineages(self):
        adapter = FakeAdapter()
        plan = self.make_plan(self.temp, [self.task("a", ["a"])])
        result = self.run_plan(plan, adapter)
        self.assertEqual(result["nodes"]["a"]["status"], "ACCEPTED")

        same_lineage = FakeAdapter(lambda *args: {"status": "CANDIDATE", "lineage": "same"} if args[2] == "maker" else {"status": "REVIEWED", "verdict": "ACCEPT", "checked": list(args[1]["done_when"]), "findings": [], "lineage": "same"})
        result = self.run_plan(self.make_plan(self.temp, [self.task("same", ["same"])]), same_lineage)
        self.assertEqual(result["nodes"]["same"]["status"], "BLOCKED")
        self.assertIn("lineages", result["nodes"]["same"]["reason"])

    def test_failed_deterministic_check_cannot_be_accepted(self):
        check = [sys.executable, "-c", "import sys; sys.exit(7)"]
        adapter = FakeAdapter()
        plan = self.make_plan(self.temp, [self.task("a", ["a"], checks=[check])])
        with patch.object(todo_graph, "command", return_value={"exit_code": 7, "seconds": 0, "stdout": "fake"}):
            result = self.run_plan(plan, adapter)
        self.assertEqual(result["nodes"]["a"]["status"], "BLOCKED")
        self.assertNotEqual(result["nodes"]["a"].get("status"), "ACCEPTED")

    def test_bad_reviewer_handoff_does_not_trigger_recode(self):
        adapter = FakeAdapter(lambda plan, task, role, workspace, folder, feedback, maker=None: {"status": "CANDIDATE", "lineage": "maker"} if role == "maker" else {"status": "REVIEWED", "verdict": "ACCEPT", "checked": [], "findings": ["bad handoff"], "lineage": "reviewer"})
        plan = self.make_plan(self.temp, [self.task("a", ["a"])])
        result = self.run_plan(plan, adapter)
        self.assertEqual(result["nodes"]["a"]["status"], "BLOCKED")
        self.assertEqual([role for _, role, _, _ in adapter.calls], ["maker", "reviewer"])

    def test_rephrased_findings_do_not_keep_unchanged_artifact_running(self):
        reviews = []

        def behavior(plan, task, role, workspace, folder, feedback, maker=None):
            if role == "maker":
                return {"status": "CANDIDATE", "lineage": "maker"}
            reviews.append(len(reviews) + 1)
            # A third call returns ACCEPT so the regression fails without hanging.
            return {"status": "REVIEWED", "lineage": "reviewer",
                    "checked": list(task["done_when"]),
                    "verdict": "REPAIR" if len(reviews) < 3 else "ACCEPT",
                    "findings": ["Same defect, wording " + str(len(reviews))] if len(reviews) < 3 else []}

        adapter = FakeAdapter(behavior)
        task = self.task("a", ["a"])
        task["mode"] = "advisory"
        # Rollback AGENT_GRAPH_AUTOPSY=0 restores the stop-for-parent-diagnosis loop.
        with patch.dict(os.environ, {"AGENT_GRAPH_AUTOPSY": "0"}):
            result = self.run_plan(self.make_plan(self.temp, [task]), adapter)
        self.assertEqual(result["nodes"]["a"]["status"], "BLOCKED")
        self.assertEqual(reviews, [1, 2])
        self.assertEqual(len(adapter.calls), 4)
        self.assertNotIn("autopsy", [role for _, role, _, _ in adapter.calls])

    def test_changed_product_can_continue_past_two_repair_rounds(self):
        makers = []

        def behavior(plan, task, role, workspace, folder, feedback, maker=None):
            if role == "maker":
                makers.append(len(makers) + 1)
                (Path(workspace) / "a").write_text(str(len(makers)), encoding="utf-8")
                return {"status": "CANDIDATE", "lineage": "maker"}
            return {"status": "REVIEWED", "lineage": "reviewer",
                    "checked": list(task["done_when"]),
                    "verdict": "REPAIR" if len(makers) < 3 else "ACCEPT",
                    "findings": ["Repair incomplete"] if len(makers) < 3 else []}

        adapter = FakeAdapter(behavior)
        result = self.run_plan(self.make_plan(self.temp, [self.task("a", ["a"])]), adapter)
        self.assertEqual(result["nodes"]["a"]["status"], "ACCEPTED")
        self.assertEqual(makers, [1, 2, 3])
        # Two rejected rounds stay below the autopsy threshold.
        self.assertNotIn("autopsy", [role for _, role, _, _ in adapter.calls])

    def test_resume_preserves_previous_accepted_result(self):
        plan = self.make_plan(self.temp, [self.task("a", ["a"])])
        run_dir = self.temp / "run"
        accepted = {"status": "ACCEPTED", "artifact": todo_graph.snapshot(self.temp, ["a"]), "round": 1}
        todo_graph.write(run_dir / "state.json", {"plan_hash": todo_graph.digest(plan), "nodes": {"a": accepted}, "version": todo_graph.VERSION})
        adapter = FakeAdapter()
        result = todo_graph.run(plan, run_dir, adapter=adapter)
        self.assertEqual(result["nodes"]["a"], accepted)
        self.assertEqual(adapter.calls, [])

    def test_report_retry_reuses_checks_only_with_unchanged_inputs(self):
        task = self.task('a', ['a'])
        (self.temp / 'input.txt').write_text('one')
        task['inputs'] = ['input.txt']
        plan = self.make_plan(self.temp, [task])
        folder = self.temp / 'pair' / 'a'
        maker = {'status': 'PARTIAL', 'lineage': 'maker'}
        log = self.temp / 'test.stdout'; log.write_text('passed')
        tests = [{'exit_code': 0, 'stdout': str(log), 'seconds': 0}]
        pending = {'maker': maker, 'artifact': todo_graph.snapshot(self.temp, task['scope']),
                   'tests': tests, 'checks_digest': todo_graph.digest(task['checks']),
                   'inputs_digest': todo_graph.snapshot(self.temp, task['inputs'])}
        for changed in (False, True):
            with self.subTest(changed=changed):
                (self.temp / 'input.txt').write_text('two' if changed else 'one')
                todo_graph.write(folder / 'progress.json', {'round': 1, 'history': [], 'review_pending': pending})
                adapter = FakeAdapter()
                with patch.object(todo_graph, 'command', return_value=tests[0]) as check:
                    result = todo_graph.run_task(plan, task, folder, adapter)
                self.assertEqual(check.call_count, 1 if changed else 0)
                self.assertEqual(result['test_evidence_reused'], not changed)
                self.assertEqual([call[1] for call in adapter.calls], ['reviewer'])

    def test_mutation_after_review_invalidates_result(self):
        def behavior(plan, task, role, workspace, folder, feedback, maker=None):
            if role == "reviewer":
                (Path(workspace) / "a").write_text("review mutation", encoding="utf-8")
                return {"status": "REVIEWED", "verdict": "ACCEPT", "checked": list(task["done_when"]), "findings": [], "lineage": "reviewer"}
            return {"status": "CANDIDATE", "lineage": "maker"}

        result = self.run_plan(self.make_plan(self.temp, [self.task("a", ["a"])]), FakeAdapter(behavior))
        self.assertEqual(result["nodes"]["a"]["status"], "BLOCKED")
        self.assertIn("changed", result["nodes"]["a"]["reason"])

    def test_approval_wait_does_not_stop_other_independent_tasks(self):
        adapter = FakeAdapter()
        plan = self.make_plan(self.temp, [self.task("approval", ["a"], requires_approval=["owner-ok"]), self.task("free", ["b"])])
        result = self.run_plan(plan, adapter)
        self.assertEqual(result["nodes"]["approval"]["status"], "WAITING_APPROVAL")
        self.assertEqual(result["nodes"]["free"]["status"], "ACCEPTED")

    def test_validate_rejects_cycle_missing_requirement_and_path_traversal(self):
        base = self.task("a", ["a"])
        plan = self.make_plan(self.temp, [base], requirements=["REQ-missing"])
        with self.assertRaisesRegex(ValueError, "cover"):
            todo_graph.validate(plan)

        cycle = [self.task("a", ["a"], depends_on=["b"]), self.task("b", ["b"], depends_on=["a"])]
        cycle[1]["covers"] = ["REQ-1"]
        cycle[0]["covers"] = ["REQ-1"]
        with self.assertRaisesRegex(ValueError, "cycle"):
            todo_graph.validate(self.make_plan(self.temp, cycle))

        unsafe = self.task("unsafe", ["../outside"])
        with self.assertRaisesRegex(ValueError, "relative paths"):
            todo_graph.validate(self.make_plan(self.temp, [unsafe]))

    def test_relative_run_dir_is_refused_and_does_not_create_cwd_directory(self):
        relative = "tmp-relative-run"
        sandbox = self.temp / "cwd-sandbox"
        sandbox.mkdir()
        (self.temp / "src").mkdir()
        plan = self.make_plan(self.temp, [self.task("a", ["src"])])
        previous = Path.cwd()
        try:
            os.chdir(sandbox)
            with self.assertRaises(ValueError) as ctx:
                todo_graph.run(plan, relative, adapter=FakeAdapter())
        finally:
            os.chdir(previous)
        self.assertIn("absolute", str(ctx.exception).lower())
        self.assertFalse((sandbox / relative).exists())
        self.assertEqual([p for p in sandbox.iterdir() if p.name.startswith("tmp")], [])

    def test_absolute_run_dir_still_writes_evidence(self):
        (self.temp / "src").mkdir()
        plan = self.make_plan(self.temp, [self.task("a", ["src"])])
        run_dir = self.temp / "run-absolute"
        result = todo_graph.run(plan, run_dir, adapter=FakeAdapter())
        self.assertEqual(result["nodes"]["a"]["status"], "ACCEPTED")
        self.assertTrue((run_dir / "state.json").exists())


class AutopsyTests(unittest.TestCase):
    """Owner decision 2026-10-13: escalate a non-converging REPAIR loop to an autopsy."""
    make_plan = TodoGraphTests.make_plan
    task = TodoGraphTests.task
    run_plan = TodoGraphTests.run_plan
    setUp = TodoGraphTests.setUp
    tearDown = TodoGraphTests.tearDown

    @staticmethod
    def autopsy_reply(action, brief=("Fix the parser branch first",)):
        return {"status": "REVIEWED", "verdict": "REPAIR", "checked": [], "lineage": "autopsy-lineage",
                "agent": "director", "model": "d-model",
                "summary": "AUTOPSY_ACTION: " + action + "\nDiagnosis: maker keeps patching symptoms.",
                "findings": list(brief)}

    def scripted(self, rejected_rounds, action="CONTINUE", change=True):
        seen = {"makers": 0, "reviews": 0, "autopsy_tasks": [], "maker_tasks": [], "feedback": []}

        def behavior(plan, task, role, workspace, folder, feedback, maker=None):
            if role == "maker":
                seen["makers"] += 1
                seen["maker_tasks"].append(task)
                seen["feedback"].append(feedback)
                if change:
                    (Path(workspace) / "a").write_text(str(seen["makers"]), encoding="utf-8")
                if task.get("maker"):
                    agent, model = task["maker"][0]["agent"], task["maker"][0]["model"]
                else:
                    agent, model = "substitute", "sub-model"
                return {"status": "CANDIDATE", "lineage": "maker-" + agent, "agent": agent, "model": model}
            if role == "autopsy":
                seen["autopsy_tasks"].append(task)
                return self.autopsy_reply(action)
            seen["reviews"] += 1
            accept = seen["reviews"] > rejected_rounds
            agent, model = (task["reviewer"][0]["agent"], task["reviewer"][0]["model"]) if task.get("reviewer") else ("sub-reviewer", "sub-r-model")
            lineage = "reviewer" if task.get("reviewer") else "sub-reviewer-lineage"
            return {"status": "REVIEWED", "lineage": lineage, "agent": agent, "model": model,
                    "checked": list(task["done_when"]), "verdict": "ACCEPT" if accept else "REPAIR",
                    "findings": [] if accept else ["Defect " + str(seen["reviews"])]}
        return FakeAdapter(behavior), seen

    @staticmethod
    def roles(adapter):
        return [role for _, role, _, _ in adapter.calls]

    def with_autopsy(self, adapter, reply):
        original = adapter.behavior

        def behavior(plan, task, role, workspace, folder, feedback, maker=None):
            if role == "autopsy":
                return reply
            return original(plan, task, role, workspace, folder, feedback, maker)
        adapter.behavior = behavior
        return adapter

    def test_three_rejections_run_one_autopsy_then_continue_to_accept(self):
        adapter, seen = self.scripted(rejected_rounds=3)
        result = self.run_plan(self.make_plan(self.temp, [self.task("a", ["a"])]), adapter)
        node = result["nodes"]["a"]
        self.assertEqual(node["status"], "ACCEPTED")
        self.assertEqual(self.roles(adapter), ["maker", "reviewer"] * 3 + ["autopsy", "maker", "reviewer"])
        self.assertEqual([(a["trigger"], a["action"]) for a in node["autopsies"]], [("rejections", "CONTINUE")])
        # Manager floor; catalog substitution reaches Director only when no Manager qualifies.
        self.assertEqual(seen["autopsy_tasks"][0]["reviewer_min_tier"], "M")
        self.assertIn("AUTOPSY of this stuck TODO node", seen["autopsy_tasks"][0]["text"])
        brief = seen["feedback"][-1]["findings"]
        self.assertTrue(brief[0].startswith("AUTOPSY CONTINUE: AUTOPSY_ACTION: CONTINUE"))
        self.assertIn("Fix the parser branch first", brief)

    def test_director_floor_is_kept_when_task_requires_it(self):
        adapter, seen = self.scripted(rejected_rounds=3)
        task = self.task("a", ["a"])
        task["reviewer_min_tier"] = "D"
        self.run_plan(self.make_plan(self.temp, [task]), adapter)
        self.assertEqual(seen["autopsy_tasks"][0]["reviewer_min_tier"], "D")

    def test_failed_checks_count_as_rejected_rounds(self):
        adapter, _ = self.scripted(rejected_rounds=0)
        outcomes = iter([1, 1, 1, 0])
        with patch.object(todo_graph, "command",
                          side_effect=lambda *a, **k: {"exit_code": next(outcomes), "seconds": 0, "stdout": "fake"}):
            result = self.run_plan(self.make_plan(self.temp, [self.task("a", ["a"])]), adapter)
        self.assertEqual(result["nodes"]["a"]["status"], "ACCEPTED")
        self.assertEqual(self.roles(adapter), ["maker"] * 3 + ["autopsy", "maker", "reviewer"])

    def test_stagnation_runs_autopsy_instead_of_blocked(self):
        adapter, _ = self.scripted(rejected_rounds=2, change=False)
        result = self.run_plan(self.make_plan(self.temp, [self.task("a", ["a"])]), adapter)
        node = result["nodes"]["a"]
        self.assertEqual(node["status"], "ACCEPTED")
        self.assertEqual(self.roles(adapter),
                         ["maker", "reviewer", "maker", "reviewer", "autopsy", "maker", "reviewer"])
        self.assertEqual(node["autopsies"][0]["trigger"], "stagnation")

    def test_swap_maker_changes_executor_in_this_node(self):
        adapter, seen = self.scripted(rejected_rounds=3, action="SWAP_MAKER")
        task = self.task("a", ["a"])
        task["maker"] = [{"agent": "maker", "model": "m-model", "reason": "fit", "score": 80},
                         {"agent": "other", "model": "o-model", "reason": "alt", "score": 70}]
        result = self.run_plan(self.make_plan(self.temp, [task]), adapter)
        node = result["nodes"]["a"]
        self.assertEqual(node["status"], "ACCEPTED")
        self.assertEqual([c["agent"] for c in seen["maker_tasks"][-1]["maker"]], ["other"])
        self.assertTrue(node["autopsies"][0]["swapped"])
        self.assertEqual(node["excluded"]["maker"], [{"agent": "maker", "model": "m-model"}])

    def test_swap_without_alternative_continues_with_brief(self):
        adapter, seen = self.scripted(rejected_rounds=3, action="SWAP_MAKER")
        result = self.run_plan(self.make_plan(self.temp, [self.task("a", ["a"])]), adapter)
        self.assertEqual(result["nodes"]["a"]["status"], "ACCEPTED")
        self.assertTrue(result["nodes"]["a"]["autopsies"][0]["swapped"])
        self.assertEqual([c["agent"] for c in seen["maker_tasks"][-1]["maker"]], [])
        self.assertEqual(seen["maker_tasks"][-1]["excluded_candidates"]["maker"],
                         [{"agent": "maker", "model": "m-model"}])
        self.assertEqual(result["nodes"]["a"]["excluded"]["maker"],
                         [{"agent": "maker", "model": "m-model"}])

    def test_owner_question_waits_for_approval_and_retry_resumes(self):
        adapter, seen = self.scripted(rejected_rounds=3, action="OWNER_QUESTION")
        plan = self.make_plan(self.temp, [self.task("a", ["a"])])
        run_dir = self.temp / "run-owner"
        node = todo_graph.run(plan, run_dir, adapter=adapter)["nodes"]["a"]
        self.assertEqual(node["status"], "WAITING_APPROVAL")
        self.assertEqual(node["requires"], ["autopsy-answer:a"])
        self.assertEqual(node["question"], ["Fix the parser branch first"])
        self.assertEqual(self.roles(adapter)[-1], "autopsy")
        node = todo_graph.run(plan, run_dir, adapter=adapter, retry=["a"])["nodes"]["a"]
        self.assertEqual(node["status"], "ACCEPTED")
        self.assertNotIn("question", node)
        self.assertEqual(node["round"], 4)
        self.assertTrue(seen["feedback"][-1]["findings"][0].startswith("AUTOPSY OWNER_QUESTION"))

    def test_autopsy_transport_failure_blocks_with_reason(self):
        adapter, _ = self.scripted(rejected_rounds=5)
        self.with_autopsy(adapter, {"status": "BLOCKED", "reason": "No qualified candidate allowed by the model catalog tiers"})
        node = self.run_plan(self.make_plan(self.temp, [self.task("a", ["a"])]), adapter)["nodes"]["a"]
        self.assertEqual(node["status"], "BLOCKED")
        self.assertEqual(node["reason"], "AUTOPSY_FAILED: No qualified candidate allowed by the model catalog tiers")

    def test_marker_only_in_native_summary_still_drives_the_action(self):
        # HD-20261014-3a37bd / HD-20261014-00f3a8: native reviewers (Qwen 5/5 runs on
        # 2026-10-14) wrote AUTOPSY_ACTION in the outer envelope summary; todo_fleet
        # projects quality_gate.summary and exposes the outer one as native_summary.
        adapter, seen = self.scripted(rejected_rounds=3)
        self.with_autopsy(adapter, {
            "status": "REVIEWED", "verdict": "REPAIR", "checked": [], "lineage": "autopsy-lineage",
            "agent": "manager", "model": "m-model",
            "summary": "One deterministic teardown defect inside the maker allowlist.",
            "native_summary": "AUTOPSY_ACTION: CONTINUE - the loop is stuck on a test-teardown bug.",
            "findings": ["Register the reopened handle before rmSync"]})
        result = self.run_plan(self.make_plan(self.temp, [self.task("a", ["a"])]), adapter)
        self.assertEqual(result["nodes"]["a"]["status"], "ACCEPTED")
        self.assertEqual(self.roles(adapter), ["maker", "reviewer"] * 3 + ["autopsy", "maker", "reviewer"])
        brief = seen["feedback"][-1]["findings"]
        self.assertEqual(brief[0], "AUTOPSY CONTINUE: One deterministic teardown defect inside the maker allowlist.")
        self.assertEqual(brief[1], "Register the reopened handle before rmSync")
        self.assertEqual(result["nodes"]["a"]["autopsies"][0]["action"], "CONTINUE")

    def test_owner_question_marker_in_native_summary_waits_for_owner(self):
        adapter, seen = self.scripted(rejected_rounds=5)
        self.with_autopsy(adapter, {
            "status": "REVIEWED", "verdict": "REPAIR", "checked": [], "lineage": "autopsy-lineage",
            "summary": "REPAIR with autopsy OWNER_QUESTION: gate and scope contradict each other.",
            "native_summary": "AUTOPSY_ACTION: OWNER_QUESTION - only the owner can relax the gate.",
            "findings": ["Which wins: the exit-0 gate or the frozen frontend scope?"]})
        node = self.run_plan(self.make_plan(self.temp, [self.task("a", ["a"])]), adapter)["nodes"]["a"]
        self.assertEqual(node["status"], "WAITING_APPROVAL")
        self.assertEqual(node["reason"], "AUTOPSY_OWNER_QUESTION")
        self.assertEqual(node["question"], ["Which wins: the exit-0 gate or the frozen frontend scope?"])

    def test_prose_action_word_without_marker_still_blocks(self):
        # A bare CONTINUE/OWNER_QUESTION in prose is not an action; only the marker is.
        adapter, _ = self.scripted(rejected_rounds=5)
        self.with_autopsy(adapter, {
            "status": "REVIEWED", "verdict": "REPAIR", "checked": [], "lineage": "autopsy",
            "summary": "CONTINUE with the brief above; or OWNER_QUESTION if unsure.",
            "native_summary": "Chose SWAP_MAKER over CONTINUE.", "findings": ["x"]})
        node = self.run_plan(self.make_plan(self.temp, [self.task("a", ["a"])]), adapter)["nodes"]["a"]
        self.assertEqual(node["status"], "BLOCKED")
        self.assertEqual(node["reason"], "AUTOPSY_FAILED: no AUTOPSY_ACTION in review")

    def test_autopsy_accept_verdict_never_accepts_the_node(self):
        adapter, _ = self.scripted(rejected_rounds=5)
        self.with_autopsy(adapter, {"status": "REVIEWED", "verdict": "ACCEPT", "findings": [], "checked": [],
                                    "summary": "Looks fine", "lineage": "autopsy"})
        node = self.run_plan(self.make_plan(self.temp, [self.task("a", ["a"])]), adapter)["nodes"]["a"]
        self.assertEqual(node["status"], "BLOCKED")
        self.assertIn("AUTOPSY_FAILED", node["reason"])

    def test_autopsy_mutating_scope_blocks(self):
        adapter, _ = self.scripted(rejected_rounds=5)
        original = adapter.behavior

        def behavior(plan, task, role, workspace, folder, feedback, maker=None):
            if role == "autopsy":
                (Path(workspace) / "a").write_text("autopsy edit", encoding="utf-8")
            return original(plan, task, role, workspace, folder, feedback, maker)
        adapter.behavior = behavior
        node = self.run_plan(self.make_plan(self.temp, [self.task("a", ["a"])]), adapter)["nodes"]["a"]
        self.assertEqual(node["status"], "BLOCKED")
        self.assertIn("read-only autopsy", node["reason"])

    def test_accept_in_first_round_runs_no_autopsy(self):
        adapter, _ = self.scripted(rejected_rounds=0)
        result = self.run_plan(self.make_plan(self.temp, [self.task("a", ["a"])]), adapter)
        self.assertEqual(result["nodes"]["a"]["status"], "ACCEPTED")
        self.assertEqual(self.roles(adapter), ["maker", "reviewer"])

    def test_rollback_env_keeps_repairing_without_autopsy(self):
        adapter, _ = self.scripted(rejected_rounds=4)
        with patch.dict(os.environ, {"AGENT_GRAPH_AUTOPSY": "0"}):
            result = self.run_plan(self.make_plan(self.temp, [self.task("a", ["a"])]), adapter)
        self.assertEqual(result["nodes"]["a"]["status"], "ACCEPTED")
        self.assertNotIn("autopsy", self.roles(adapter))

    def test_autopsy_prefers_declared_manager_over_higher_scored_director(self):
        import model_tiers
        tiers = {"astra": "D", "opus": "M", "maker": "K"}
        adapter, seen = self.scripted(rejected_rounds=3)
        task = self.task("a", ["a"])
        task["reviewer"] = [{"agent": "astra", "model": "a-model", "reason": "d", "score": 95},
                            {"agent": "opus", "model": "o-model", "reason": "m", "score": 80}]
        with patch.object(model_tiers, "load_catalog", return_value={}), \
                patch.object(model_tiers, "lookup", side_effect=lambda cat, agent, model: {"tier": tiers.get(agent)}):
            self.run_plan(self.make_plan(self.temp, [task]), adapter)
            self.assertEqual([c["agent"] for c in seen["autopsy_tasks"][0]["reviewer"]], ["opus"])
            self.assertEqual([c["agent"] for c in todo_graph._autopsy_candidates(task, "D")], ["astra"])
            # Only a Director declared: an empty lane forces catalog substitution (M before D),
            # because a declared Director proves nothing about qualified Managers.
            only_d = {**task, "reviewer": task["reviewer"][:1]}
            self.assertEqual(todo_graph._autopsy_candidates(only_d, "M"), [])
            # Below-floor reviewers stay only to be disallowed and substituted; never maker candidates.
            low = {**task, "reviewer": [{"agent": "maker", "model": "k", "reason": "k", "score": 99}] + task["reviewer"][:1]}
            self.assertEqual([c["agent"] for c in todo_graph._autopsy_candidates(low, "M")], ["maker"])
            with patch.dict(os.environ, {"AGENT_GRAPH_TIER_ROUTING": "0"}):
                # Without tier routing no class floor is enforced: only declared M/D may run.
                self.assertEqual([c["agent"] for c in todo_graph._autopsy_candidates(only_d, "M")], ["astra"])
                self.assertEqual([c["agent"] for c in todo_graph._autopsy_candidates(low, "M")], ["astra"])
                self.assertEqual(todo_graph._autopsy_candidates({**task, "reviewer": low["reviewer"][:1]}, "M"), [])
                # Director stays as fallback but outside the legacy near-tie band of the Manager.
                both = todo_graph._autopsy_candidates(task, "M")
                self.assertEqual([(c["agent"], c["score"]) for c in both], [("opus", 80), ("astra", 74)])

    def test_unavailable_model_catalog_blocks_autopsy_with_reason(self):
        import model_tiers
        adapter, _ = self.scripted(rejected_rounds=5)
        for error in (FileNotFoundError("catalog"), AttributeError("'list' object has no attribute 'get'")):
            with self.subTest(error=type(error).__name__):
                adapter, _ = self.scripted(rejected_rounds=5)
                plan = self.make_plan(self.temp, [self.task("a", ["a"])])
                run_dir = self.temp / ("run-catalog-" + type(error).__name__)
                with patch.object(model_tiers, "load_catalog", side_effect=error):
                    node = todo_graph.run(plan, run_dir, adapter=adapter)["nodes"]["a"]
                self.assertEqual(node["status"], "BLOCKED")
                self.assertEqual(node["reason"], "AUTOPSY_FAILED: model catalog unavailable ("
                                 + type(error).__name__ + ")")
                self.assertNotIn("autopsy", self.roles(adapter))
                # The node keeps its own result and history, not a generic scheduler error.
                self.assertEqual(todo_graph.read(run_dir / "a" / "result.json")["round"], 3)

    def test_autopsy_receives_all_checks_with_original_indexes(self):
        adapter, seen = self.scripted(rejected_rounds=0)
        outcomes = iter([0, 1] * 3 + [0, 0])
        with patch.object(todo_graph, "command",
                          side_effect=lambda *a, **k: {"exit_code": next(outcomes), "seconds": 0, "stdout": "fake"}):
            task = self.task("a", ["a"], checks=[["first"], ["second"]])
            node = self.run_plan(self.make_plan(self.temp, [task]), adapter)["nodes"]["a"]
        self.assertEqual(node["status"], "ACCEPTED")
        call = next(c for c in adapter.calls if c[1] == "autopsy")
        self.assertEqual([t["exit_code"] for t in call[2]["tests"]], [0, 1])
        self.assertIn('"failed_checks": [{"index": 1, "exit_code": 1}]', seen["autopsy_tasks"][0]["text"])
        # The maker still gets only the failed check as repair evidence.
        self.assertEqual([t["exit_code"] for t in seen["feedback"][3]["failed_tests"]], [1])

    def test_continue_brief_survives_maker_failure_and_retry(self):
        adapter, seen = self.scripted(rejected_rounds=3)
        original = adapter.behavior
        fail_once = {"done": False}

        def behavior(plan, task, role, workspace, folder, feedback, maker=None):
            if role == "maker" and seen["makers"] == 3 and not fail_once["done"]:
                fail_once["done"] = True
                return {"status": "BLOCKED", "reason": "Pool availability: exhausted"}
            return original(plan, task, role, workspace, folder, feedback, maker)
        adapter.behavior = behavior
        plan = self.make_plan(self.temp, [self.task("a", ["a"])])
        run_dir = self.temp / "run-retry"
        node = todo_graph.run(plan, run_dir, adapter=adapter)["nodes"]["a"]
        self.assertEqual(node["status"], "BLOCKED")
        self.assertEqual(node["reason"], "Pool availability: exhausted")
        node = todo_graph.run(plan, run_dir, adapter=adapter, retry=["a"])["nodes"]["a"]
        self.assertEqual(node["status"], "ACCEPTED")
        self.assertTrue(seen["feedback"][-1]["findings"][0].startswith("AUTOPSY CONTINUE"))
        self.assertNotIn("next_feedback", node)

    def test_swapped_out_maker_returning_via_substitution_is_reported(self):
        adapter, seen = self.scripted(rejected_rounds=6, action="SWAP_MAKER")
        original = adapter.behavior

        def behavior(plan, task, role, workspace, folder, feedback, maker=None):
            result = original(plan, task, role, workspace, folder, feedback, maker)
            if role == "maker":
                # Catalog substitution picked the excluded executor again.
                result.update(agent="maker", model="m-model", lineage="maker-maker")
            return result
        adapter.behavior = behavior
        task = self.task("a", ["a"])
        task["maker"] = [{"agent": "maker", "model": "m-model", "reason": "fit", "score": 80},
                         {"agent": "other", "model": "o-model", "reason": "alt", "score": 70}]
        node = self.run_plan(self.make_plan(self.temp, [task]), adapter)["nodes"]["a"]
        self.assertTrue(node["autopsies"][0]["swap_bypassed"])
        self.assertEqual(seen["maker_tasks"][-1]["excluded_candidates"]["maker"],
                         [{"agent": "maker", "model": "m-model"}])
        self.assertIn('"swap_bypassed": true', seen["autopsy_tasks"][1]["text"])

    def test_fleet_adapter_sends_autopsy_through_reviewer_lane(self):
        folder = self.temp / "fleet-call"

        def fake_command(argv, cwd, out, timeout, env=None):
            todo_graph.write(out / "result.json", {"status": "REVIEWED"})
            return {"exit_code": 0}
        with patch.object(todo_graph, "command", side_effect=fake_command):
            todo_graph.FleetAdapter().invoke({"goal": "g"}, self.task("a", ["a"]), "autopsy",
                                             self.temp, folder, {"tests": []})
        self.assertEqual(todo_graph.read(folder / "request.json")["role"], "reviewer")

    def test_swap_singleton_maker_excludes_and_forwards_empty_lane(self):
        task = self.task("a", ["a"])
        task["maker"] = [{"agent": "single-m", "model": "sm-model"}]
        state = {}
        self.assertTrue(todo_graph._swap(task, state, "maker", {"agent": "single-m", "model": "sm-model"}))
        routed = todo_graph._node_task(task, state)
        self.assertEqual(routed["maker"], [])
        self.assertEqual(routed["excluded_candidates"]["maker"], [{"agent": "single-m", "model": "sm-model"}])
        self.assertEqual(task["maker"], [{"agent": "single-m", "model": "sm-model"}])

    def test_swap_singleton_reviewer_excludes_and_forwards_empty_lane(self):
        task = self.task("a", ["a"])
        task["reviewer"] = [{"agent": "single-r", "model": "sr-model"}]
        state = {}
        self.assertTrue(todo_graph._swap(task, state, "reviewer", {"agent": "single-r", "model": "sr-model"}))
        routed = todo_graph._node_task(task, state)
        self.assertEqual(routed["reviewer"], [])
        self.assertEqual(routed["excluded_candidates"]["reviewer"], [{"agent": "single-r", "model": "sr-model"}])
        self.assertEqual(task["reviewer"], [{"agent": "single-r", "model": "sr-model"}])

    def test_swap_catalog_selected_identity_outside_declared_list(self):
        task = self.task("a", ["a"])
        task["maker"] = [{"agent": "declared-m", "model": "dm-model", "reason": "fit", "score": 80}]
        state = {}
        self.assertTrue(todo_graph._swap(task, state, "maker", {"agent": "catalog-m", "model": "sub-model"}))
        routed = todo_graph._node_task(task, state)
        self.assertEqual(routed["maker"], task["maker"])
        self.assertIn({"agent": "catalog-m", "model": "sub-model"}, routed["excluded_candidates"]["maker"])

    def test_swap_identity_validation_and_deduplication(self):
        task = self.task("a", ["a"])
        state = {}
        self.assertFalse(todo_graph._swap(task, state, "maker", None))
        self.assertFalse(todo_graph._swap(task, state, "maker", {}))
        self.assertFalse(todo_graph._swap(task, state, "maker", {"agent": ""}))
        self.assertFalse(todo_graph._swap(task, state, "maker", {"agent": "   "}))
        self.assertFalse(todo_graph._swap(task, state, "maker", {"agent": 123}))
        self.assertFalse(todo_graph._swap(task, state, "maker", {"agent": "m", "model": 123}))
        self.assertFalse(todo_graph._swap(task, state, "maker", {"agent": "m", "model": "   "}))
        self.assertFalse(todo_graph._swap(task, state, "invalid_lane", {"agent": "m", "model": "mod"}))
        self.assertNotIn("excluded", state)

        self.assertTrue(todo_graph._swap(task, state, "maker", {"agent": "m", "model": "mod"}))
        self.assertEqual(state["excluded"]["maker"], [{"agent": "m", "model": "mod"}])
        self.assertTrue(todo_graph._swap(task, state, "maker", {"agent": "m", "model": "mod"}))
        self.assertEqual(len(state["excluded"]["maker"]), 1)
        self.assertEqual(state["excluded"]["maker"], [{"agent": "m", "model": "mod"}])

    def test_swap_exact_model_keeps_other_model_of_same_agent(self):
        task = self.task("a", ["a"])
        task["maker"] = [
            {"agent": "same-agent", "model": "model-1", "reason": "fit", "score": 85},
            {"agent": "same-agent", "model": "model-2", "reason": "alt", "score": 80},
        ]
        state = {}
        self.assertTrue(todo_graph._swap(task, state, "maker", {"agent": "same-agent", "model": "model-1"}))
        routed = todo_graph._node_task(task, state)
        self.assertEqual(routed["maker"], [{"agent": "same-agent", "model": "model-2", "reason": "alt", "score": 80}])
        self.assertEqual(routed["excluded_candidates"]["maker"], [{"agent": "same-agent", "model": "model-1"}])

    def test_retry_exclusion_persistence(self):
        adapter, seen = self.scripted(rejected_rounds=3, action="SWAP_MAKER")
        original = adapter.behavior
        fail_once = {"done": False}

        def behavior(plan, task, role, workspace, folder, feedback, maker=None):
            if role == "maker" and seen["makers"] == 3 and not fail_once["done"]:
                fail_once["done"] = True
                return {"status": "BLOCKED", "reason": "Pool availability: exhausted"}
            return original(plan, task, role, workspace, folder, feedback, maker)
        adapter.behavior = behavior

        plan = self.make_plan(self.temp, [self.task("a", ["a"])])
        run_dir = self.temp / "run-retry-swap"
        node = todo_graph.run(plan, run_dir, adapter=adapter)["nodes"]["a"]
        self.assertEqual(node["status"], "BLOCKED")
        self.assertEqual(node["reason"], "Pool availability: exhausted")
        self.assertEqual(node["excluded"]["maker"], [{"agent": "maker", "model": "m-model"}])

        node = todo_graph.run(plan, run_dir, adapter=adapter, retry=["a"])["nodes"]["a"]
        self.assertEqual(node["status"], "ACCEPTED")
        self.assertEqual(node["excluded"]["maker"], [{"agent": "maker", "model": "m-model"}])
        retried_maker_task = seen["maker_tasks"][-1]
        self.assertEqual(retried_maker_task["maker"], [])
        self.assertEqual(retried_maker_task["excluded_candidates"]["maker"], [{"agent": "maker", "model": "m-model"}])


if __name__ == "__main__":
    unittest.main()
