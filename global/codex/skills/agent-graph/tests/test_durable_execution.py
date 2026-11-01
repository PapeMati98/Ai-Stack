"""S1..S7 durable-execution acceptance tests for durable_supervisor/todo_graph.

No real model/Fleet calls: every execution uses an injected RecordingAdapter
in todo_graph's explicit ``legacy-test`` context.
"""
import importlib.util
import json
import os
import subprocess
import sys
import threading
import textwrap
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

os.environ["AGENT_GRAPH_WATCH_EVENTS"] = "0"

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))


def _load(name):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / (name + ".py"))
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


todo_graph = _load("todo_graph")
durable = _load("durable_supervisor")

# Synthetic durable runs must not publish TODO journals into the real user home.
# Scoped to this module; the caller's explicit telemetry setting is restored on
# teardown. Supervised child processes inherit the scoped value. Nothing changes
# at import time.
_test_telemetry = patch.dict(os.environ, {"AGENT_TODO_TELEMETRY": "off"})


def setUpModule():
    _test_telemetry.start()


def tearDownModule():
    _test_telemetry.stop()

FUTURE = "2999-01-01T00:00:00Z"
PAST = "2000-01-01T00:00:00Z"


def pool_wait(reason="Pool availability: cooldown"):
    return {"status": "RESUMABLE_POOL_WAIT", "reason": reason,
            "pool_wait_reasons": [reason], "next_check_at": FUTURE,
            "lineage": "pool-lineage"}


class RecordingAdapter:
    """FakeAdapter convention from scripts/test_todo_graph.py, with counters."""

    def __init__(self, behavior=None):
        self.behavior = behavior or self.default_behavior
        self.calls = []
        self.lock = threading.Lock()
        self.active = 0
        self.peak = 0

    @staticmethod
    def default_behavior(plan, task, role, workspace, folder, feedback, maker=None):
        if role == "maker":
            Path(workspace, task["scope"][0]).write_text("made\n", encoding="utf-8")
            return {"status": "CANDIDATE", "lineage": "maker-lineage"}
        return {"status": "REVIEWED", "verdict": "ACCEPT", "checked": list(task["done_when"]),
                "findings": [], "lineage": "reviewer-lineage"}

    def invoke(self, plan, task, role, workspace, folder, feedback, maker=None):
        with self.lock:
            self.calls.append((task["id"], role))
            self.active += 1
            self.peak = max(self.peak, self.active)
        try:
            return self.behavior(plan, task, role, workspace, folder, feedback, maker)
        finally:
            with self.lock:
                self.active -= 1

    def count(self, role=None, task_id=None):
        return sum(1 for t, r in self.calls
                   if (role is None or r == role) and (task_id is None or t == task_id))


class DurableCase(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.root = Path(self.tmp.name)
        # todo_graph puts workspace locks under Path.home()/.agents/todo-locks;
        # redirect home into the test temp dir so runs stay hermetic.
        self._home_patch = patch.dict(
            os.environ, {"USERPROFILE": str(self.root), "HOME": str(self.root)})
        self._home_patch.start()
        self.ws = self.root / "ws"
        self.ws.mkdir()
        self.cdir = self.root / "evidence" / "campaign"
        self.lockbox = str(self.root / "lockbox" / "ref.json")

    def tearDown(self):
        self._home_patch.stop()
        self.tmp.cleanup()

    def campaign(self, units, cid="camp-1", budget=None, **extra):
        doc = {
            "schema_version": "arbor-campaign/v1",
            "campaign_id": cid,
            "goal": "durable test campaign",
            "workspace": str(self.ws),
            "baseline_ref": "base-1",
            "lockbox_ref": self.lockbox,
            "tree_budget": budget if budget is not None else {
                "max_parallel": 2, "max_total_units": 8, "max_depth": 1,
                "allowed_pools": ["fleet"], "allowed_write_globs": ["*.txt", "sub/*.txt"]},
            "units": units,
        }
        doc.update(extra)
        return doc

    def unit(self, uid, scope=None, **kw):
        u = {"id": uid, "text": "do " + uid, "scope": scope or [uid + ".txt"],
             "done_when": ["evidence for " + uid],
             "checks": [[sys.executable, "-c", "pass"]]}
        u.update(kw)
        return u

    def read_json(self, path):
        return json.loads(Path(path).read_text(encoding="utf-8"))

    def seed_interrupted_after_receipt(self, campaign, cdir, unit_id="unit-a"):
        """Seed an interrupted RUNNING unit with a trustworthy maker receipt."""
        plan = durable.campaign_to_plan(campaign)
        Path(cdir).mkdir(parents=True, exist_ok=True)
        (Path(cdir) / "todo.json").write_text(json.dumps(plan), encoding="utf-8")
        (self.ws / "unit-a.txt").write_text("made\n", encoding="utf-8")
        folder = Path(cdir) / unit_id
        maker_dir = folder / "1" / "maker"
        maker_dir.mkdir(parents=True, exist_ok=True)
        ledger = self.root / "ledger"
        ledger.mkdir(exist_ok=True)
        usage = {
            "run_id": "fleet-run-1", "agent": "fleet", "model": "fleet-default",
            "model_provider_id": "maker-lineage",
            "runtime_attestation": {
                "status": "VERIFIED", "package_version": "0.9.55",
                "package_fingerprint": "1" * 64, "payload_fingerprint": "2" * 64,
                "payload_file_count": 185, "runtime_root": str(self.root / "synthetic-runtime"),
            },
            "worktree_base_commit_sha": "0" * 40,
            "worktree_base_provenance": "preserved", "exit_code": 0,
            "provider_terminal_reason": "completed", "execution_workspace": str(self.ws),
        }
        usage_file = ledger / "usage.json"
        usage_file.write_text(json.dumps(usage), encoding="utf-8")
        receipt = {"status": "CANDIDATE", "lineage": "maker-lineage",
                   "model": "fleet-default", "run_id": "fleet-run-1",
                   "usage_record": str(usage_file)}
        (maker_dir / "result.json").write_text(json.dumps(receipt), encoding="utf-8")
        (maker_dir / "process.json").write_text(
            json.dumps({"pid": 99999999, "started": 1234567890.0}), encoding="utf-8")
        progress = {"status": "RUNNING", "round": 1,
                    "history": [{"role": "maker", "result": receipt}]}
        (folder / "progress.json").write_text(json.dumps(progress), encoding="utf-8")
        (Path(cdir) / "state.json").write_text(json.dumps({
            "plan_hash": todo_graph.digest(plan), "version": todo_graph.VERSION,
            "nodes": {unit_id: {"status": "RUNNING", "round": 1,
                                "history": [{"role": "maker", "result": receipt}]}},
        }), encoding="utf-8")
        return receipt


class TestS1NormalAcceptance(DurableCase):
    def test_s1_normal_execution_and_independent_acceptance(self):
        adapter = RecordingAdapter()
        result = durable.run_campaign(self.campaign([self.unit("unit-a")]), self.cdir, adapter=adapter)
        self.assertEqual(result["units"]["unit-a"], "ACCEPTED")
        # Independent acceptance: maker and reviewer are distinct lineages/roles.
        self.assertEqual(adapter.count("maker", "unit-a"), 1)
        self.assertEqual(adapter.count("reviewer", "unit-a"), 1)
        progress = self.read_json(self.cdir / "unit-a" / "progress.json")
        roles = [h["role"] for h in progress["history"]]
        self.assertIn("maker", roles)
        self.assertIn("reviewer", roles)
        maker = next(h["result"] for h in progress["history"] if h["role"] == "maker")
        reviewer = next(h["result"] for h in progress["history"] if h["role"] == "reviewer")
        self.assertNotEqual(maker["lineage"], reviewer["lineage"])
        self.assertEqual(reviewer["verdict"], "ACCEPT")
        self.assertTrue((self.ws / "unit-a.txt").is_file())


class TestS2ParentDisconnect(DurableCase):
    def test_s2_spawn_detached_windowless_and_state_preserved(self):
        calls = {}

        class FakePopen:
            def __init__(self, argv, **kw):
                calls["argv"] = argv
                calls["kw"] = kw
                self.pid = 424242

        campaign = self.campaign([self.unit("unit-a")])
        with patch.object(durable, "_find_hidden_launch", return_value=None), \
                patch.object(subprocess, "Popen", FakePopen):
            out = durable.spawn_detached(campaign, self.cdir)
        self.assertEqual(out["status"], "SPAWNED")
        self.assertEqual(out["model_calls"], 0)
        flags = calls["kw"].get("creationflags", 0)
        self.assertTrue(flags & getattr(subprocess, "CREATE_NO_WINDOW", 0))
        self.assertTrue(flags & getattr(subprocess, "DETACHED_PROCESS", 0))
        # State/result survive the parent: campaign persisted, status readable.
        self.assertTrue((self.cdir / "campaign.json").is_file())
        self.assertEqual(self.read_json(self.cdir / "campaign.json")["campaign_id"], "camp-1")

    def test_s2_spawn_detached_prefers_hidden_launch(self):
        calls = {}

        class FakePopen:
            def __init__(self, argv, **kw):
                calls["argv"] = argv
                self.pid = 424243

        with patch.object(durable, "_find_hidden_launch", return_value="HiddenLaunch.exe"), \
                patch.object(subprocess, "Popen", FakePopen):
            out = durable.spawn_detached(self.campaign([self.unit("unit-a")]), self.cdir)
        self.assertEqual(out["status"], "SPAWNED")
        self.assertEqual(out.get("launcher"), "HiddenLaunch.exe")
        specs = list(self.cdir.glob("*hidden*")) + list(self.cdir.rglob("*.xml"))
        self.assertTrue(specs, "windowless HiddenLaunch spec should be written")
        import xml.etree.ElementTree as ET
        root = ET.parse(self.cdir / "hidden-launch.xml").getroot()
        self.assertEqual(root.get("isolate-desktop"), "true")


    def test_s2_spawn_detached_child_takes_over_after_launcher_exits(self):
        # Real production detachment: a short-lived launcher seeds a live
        # owner record, then calls spawn_detached and exits. The detached
        # supervisor must find the prior owner dead, take over with a new
        # generation, and finish the (empty) campaign after the parent is
        # gone -- with zero model calls and state preserved on disk.
        # Scope note: this proves detached takeover/state preservation only.
        # The detached child is fleet-native (adapter=None; see
        # spawn_detached), so no synthetic adapter can be injected into it
        # from this test; detached task execution with a synthetic adapter
        # is a parent-owned runtime dependency, not something to fake here.
        scripts = Path(durable.__file__).resolve().parent
        launcher_path = self.root / "launcher.py"
        spawn_path = self.root / "spawn-result.json"
        launcher_src = "\n".join([
            "import importlib.util, json, os, sys",
            "from pathlib import Path",
            "SCRIPTS = __SCRIPTS__",
            "sys.path.insert(0, SCRIPTS)",
            "def _load(name):",
            "    spec = importlib.util.spec_from_file_location(name, Path(SCRIPTS) / (name + '.py'))",
            "    mod = importlib.util.module_from_spec(spec)",
            "    sys.modules[name] = mod",
            "    spec.loader.exec_module(mod)",
            "    return mod",
            "todo_graph = _load('todo_graph')",
            "durable = _load('durable_supervisor')",
            "campaign = json.loads(__CAMPAIGN__)",
            "cdir = Path(__CDIR__)",
            "cdir.mkdir(parents=True, exist_ok=True)",
            "seed = {'schema_version': durable.DURABLE_STATE_SCHEMA,",
            "        'campaign_id': campaign['campaign_id'], 'baseline_ref': campaign['baseline_ref'],",
            "        'pid': os.getpid(), 'process_started_at': todo_graph._process_start_time(os.getpid()),",
            "        'generation': 1, 'status': 'RUNNING', 'model_calls': 0,",
            "        'started_at': durable._now(), 'updated_at': durable._now()}",
            "(cdir / 'durable-state.json').write_text(json.dumps(seed), encoding='utf-8')",
            "out = durable.spawn_detached(campaign, cdir, action='resume')",
            "Path(__SPAWN__).write_text(json.dumps({'launcher_pid': os.getpid(), 'spawn': out}), encoding='utf-8')",
            "os._exit(0)",
            "",
        ])
        launcher_src = (launcher_src
                        .replace("__SCRIPTS__", repr(str(scripts)))
                        .replace("__CAMPAIGN__", repr(json.dumps(self.campaign([]))))
                        .replace("__CDIR__", repr(str(self.cdir)))
                        .replace("__SPAWN__", repr(str(spawn_path))))
        launcher_path.write_text(launcher_src, encoding="utf-8")
        proc = subprocess.run([sys.executable, "-B", str(launcher_path)],
                              capture_output=True, text=True, timeout=60,
                              creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        spawn_info = self.read_json(spawn_path)
        self.assertEqual(spawn_info["spawn"]["status"], "SPAWNED")
        self.assertEqual(spawn_info["spawn"]["model_calls"], 0)
        launcher_pid = spawn_info["launcher_pid"]
        # The launcher has exited; only the detached child can now advance
        # the durable state from generation 1 to a completed generation 2.
        state = {}
        deadline = time.time() + 60
        while time.time() < deadline:
            try:
                state = self.read_json(self.cdir / "durable-state.json")
            except (OSError, ValueError):
                state = {}
            if state.get("generation") == 2 and state.get("status") == "EMPTY":
                break
            time.sleep(0.1)
        self.assertEqual(state.get("generation"), 2, state)
        self.assertEqual(state.get("status"), "EMPTY", state)
        self.assertNotEqual(state.get("pid"), launcher_pid)
        self.assertEqual(state.get("model_calls"), 0)
        self.assertTrue((self.cdir / "campaign.json").is_file())
        # Evidence depends on the launcher actually selected: HiddenLaunch
        # writes hidden-launch.xml (+ its own logs); only the CREATE_NO_WINDOW
        # fallback writes supervisor.log.
        if spawn_info["spawn"].get("launcher") == "HiddenLaunch.exe":
            self.assertTrue((self.cdir / "hidden-launch.xml").is_file())
        else:
            self.assertTrue((self.cdir / "supervisor.log").is_file())
        # Wait for the detached child to actually exit so its supervisor.log
        # handle is released before TemporaryDirectory cleanup.
        child_pid = state.get("pid")
        if child_pid:
            deadline = time.time() + 30
            while time.time() < deadline:
                try:
                    import ctypes
                    handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, int(child_pid))
                    if not handle:
                        break
                    code = ctypes.c_ulong(0)
                    ctypes.windll.kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
                    ctypes.windll.kernel32.CloseHandle(handle)
                    if code.value != 259:  # STILL_ACTIVE
                        break
                except Exception:
                    break
                time.sleep(0.1)


    def test_s2_detached_worker_executes_nonempty_task_and_parent_independently_accepts(self):
        # Stronger than an empty spawn_detached takeover: a separate launcher
        # process starts a windowless worker for one real unit and exits
        # (os._exit) without waiting. The worker runs the same public
        # durable.run_campaign path; the parent then independently reads the
        # result file, artifact, calls ledger and verified campaign status.
        scripts = Path(durable.__file__).resolve().parent
        campaign = self.campaign([self.unit("unit-a")])
        self.cdir.mkdir(parents=True, exist_ok=True)
        campaign_path = self.cdir / "campaign.json"
        campaign_path.write_text(json.dumps(campaign), encoding="utf-8")
        result_path = self.cdir / "worker-result.json"
        calls_path = self.cdir / "worker-calls.jsonl"
        worker_path = self.root / "detached_worker.py"
        launcher_path = self.root / "detached_launcher.py"
        launcher_result = self.cdir / "launcher-result.json"
        worker_log = self.cdir / "detached-worker.log"
        worker_path.write_text(textwrap.dedent("""
            import json, sys
            from pathlib import Path
            sys.path.insert(0, __SCRIPTS__)
            import durable_supervisor as durable
            campaign = json.loads(Path(__CAMPAIGN__).read_text(encoding="utf-8"))
            class Adapter:
                def invoke(self, plan, task, role, workspace, folder, feedback, maker=None):
                    with open(__CALLS__, "a", encoding="utf-8") as fh:
                        fh.write(json.dumps({"task": task["id"], "role": role}) + "\\n")
                    if role == "maker":
                        Path(workspace, task["scope"][0]).write_text("made\\n", encoding="utf-8")
                        return {"status": "CANDIDATE", "lineage": "maker-lineage"}
                    return {"status": "REVIEWED", "verdict": "ACCEPT",
                            "checked": ["evidence for " + task["id"]], "findings": [],
                            "lineage": "reviewer-lineage"}
            payload = {}
            try:
                result = durable.run_campaign(campaign, __CDIR__, adapter=Adapter())
                payload = {"status": result["status"], "units": result["units"],
                           "model_calls": result["model_calls"]}
            except Exception as exc:
                payload = {"status": "ERROR", "error": type(exc).__name__ + ": " + str(exc)}
            Path(__RESULT__).write_text(json.dumps(payload), encoding="utf-8")
            raise SystemExit(0 if payload.get("status") == "ACCEPTED_CANDIDATE" else 3)
            """).replace("__SCRIPTS__", repr(str(scripts)))
                       .replace("__CAMPAIGN__", repr(str(campaign_path)))
                       .replace("__CALLS__", repr(str(calls_path)))
                       .replace("__CDIR__", repr(str(self.cdir)))
                       .replace("__RESULT__", repr(str(result_path))), encoding="utf-8")
        launcher_path.write_text(textwrap.dedent("""
            import json, os, subprocess, sys
            from pathlib import Path
            flags = 0
            if os.name == "nt":
                flags = subprocess.CREATE_NO_WINDOW | subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
            log = open(__LOG__, "ab")
            proc = subprocess.Popen([sys.executable, "-B", __WORKER__], stdout=log,
                                    stderr=subprocess.STDOUT, cwd=__CDIR__,
                                    creationflags=flags, close_fds=True)
            Path(__LAUNCHER_RESULT__).write_text(json.dumps({"worker_pid": proc.pid}), encoding="utf-8")
            os._exit(0)
            """).replace("__LOG__", repr(str(worker_log)))
                       .replace("__WORKER__", repr(str(worker_path)))
                       .replace("__CDIR__", repr(str(self.cdir)))
                       .replace("__LAUNCHER_RESULT__", repr(str(launcher_result))), encoding="utf-8")

        launcher_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
        launcher = subprocess.Popen([sys.executable, "-B", str(launcher_path)],
                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                    creationflags=launcher_flags)
        self.assertEqual(launcher.wait(timeout=30), 0, "launcher must exit without waiting for worker")
        worker_pid = json.loads(launcher_result.read_text(encoding="utf-8"))["worker_pid"]
        self.assertNotEqual(worker_pid, launcher.pid)
        deadline = time.time() + 60
        while time.time() < deadline and not result_path.exists():
            time.sleep(0.1)
        self.assertTrue(result_path.exists(),
                        "detached worker did not finish after launcher exit; log="
                        + (worker_log.read_text(encoding="utf-8", errors="replace")
                           if worker_log.exists() else "<missing>"))
        payload = json.loads(result_path.read_text(encoding="utf-8"))
        self.assertEqual(payload, {"status": "ACCEPTED_CANDIDATE",
                                   "units": {"unit-a": "ACCEPTED"}, "model_calls": 2})
        calls = [json.loads(line) for line in calls_path.read_text(encoding="utf-8").splitlines()]
        self.assertEqual(calls, [{"task": "unit-a", "role": "maker"},
                                 {"task": "unit-a", "role": "reviewer"}])
        self.assertEqual((self.ws / "unit-a.txt").read_text(encoding="utf-8"), "made\n")
        result = json.loads((self.cdir / "results" / "unit-a.json").read_text(encoding="utf-8"))
        self.assertEqual(result["schema_version"], "arbor-unit-result/v1")
        self.assertEqual(result["status"], "ACCEPTED")
        self.assertEqual(result["dedupe_key"], "camp-1:unit-a")
        self.assertEqual(len((self.cdir / "experience.jsonl")
                               .read_text(encoding="utf-8").splitlines()), 1)
        status = durable.campaign_status(campaign, self.cdir)
        self.assertEqual(status["units"]["unit-a"], "ACCEPTED")
        self.assertEqual(status["model_calls"], 2)
        # Do not remove the temporary tree while the detached worker is alive.
        deadline = time.time() + 30
        while time.time() < deadline:
            if todo_graph._process_alive(worker_pid) is False:
                break
            time.sleep(0.1)
        else:
            self.fail(f"detached worker pid {worker_pid} still running at test end")


class TestS3Restarts(DurableCase):
    def _reviewer_pool_wait_once(self):
        state = {"done": False}

        def behavior(plan, task, role, workspace, folder, feedback, maker=None):
            if role == "maker":
                Path(workspace, task["scope"][0]).write_text("made\n", encoding="utf-8")
                return {"status": "CANDIDATE", "lineage": "maker-lineage"}
            if not state["done"]:
                state["done"] = True
                return pool_wait("Pool availability: reviewer cooldown")
            return RecordingAdapter.default_behavior(plan, task, role, workspace, folder, feedback, maker)

        return RecordingAdapter(behavior)

    def test_s3_worker_restart_after_receipt_goes_review_pending_no_double_maker(self):
        adapter = self._reviewer_pool_wait_once()
        campaign = self.campaign([self.unit("unit-a")])
        first = durable.run_campaign(campaign, self.cdir, adapter=adapter)
        self.assertEqual(first["status"], "RESUMABLE_POOL_WAIT")
        progress = self.read_json(self.cdir / "unit-a" / "progress.json")
        # Maker receipt retained and parked in review_pending before reviewer ran.
        self.assertIn("review_pending", progress)
        self.assertEqual(progress["review_pending"]["maker"]["status"], "CANDIDATE")
        self.assertTrue((self.ws / "unit-a.txt").is_file())
        makers_before = adapter.count("maker")
        # Expire cooldown, then a fresh supervisor/worker resumes.
        node_path = self.cdir / "state.json"
        state = self.read_json(node_path)
        state["nodes"]["unit-a"]["next_check_at"] = PAST
        node_path.write_text(json.dumps(state), encoding="utf-8")
        resumed = durable.resume_campaign(campaign, self.cdir, adapter=adapter)
        self.assertEqual(resumed["units"]["unit-a"], "ACCEPTED")
        self.assertEqual(adapter.count("maker"), makers_before, "maker must not run twice")

    def test_s3_interrupted_after_receipt_recovers_without_double_maker(self):
        campaign = self.campaign([self.unit("unit-a")])
        receipt = self.seed_interrupted_after_receipt(campaign, self.cdir)
        adapter = RecordingAdapter()
        with patch.object(todo_graph, "_workspace_base", return_value="0" * 40):
            result = durable.resume_campaign(campaign, self.cdir, adapter=adapter)
        self.assertEqual(result["units"]["unit-a"], "ACCEPTED")
        self.assertEqual(adapter.count("maker"), 0, "authoritative receipt must not replay maker")
        self.assertEqual(adapter.count("reviewer"), 1)
        self.assertEqual((self.ws / "unit-a.txt").read_text(encoding="utf-8"), "made\n")
        progress = self.read_json(self.cdir / "unit-a" / "progress.json")
        self.assertTrue(any(h.get("role") == "recovery" for h in progress["history"]))
        self.assertEqual(progress["history"][0]["result"]["run_id"], receipt["run_id"])

    def test_s3_worker_restart_before_receipt_preserves_and_blocks_safely(self):
        campaign = self.campaign([self.unit("unit-a")])
        plan = durable.campaign_to_plan(campaign)
        self.cdir.mkdir(parents=True)
        (self.cdir / "todo.json").write_text(json.dumps(plan), encoding="utf-8")
        (self.cdir / "unit-a").mkdir()
        artifact = self.cdir / "unit-a" / "partial-artifact.txt"
        artifact.write_text("synthetic-partial\n", encoding="utf-8")
        (self.cdir / "unit-a" / "progress.json").write_text(
            json.dumps({"status": "RUNNING", "round": 1, "history": []}), encoding="utf-8")
        (self.cdir / "state.json").write_text(json.dumps(
            {"plan_hash": todo_graph.digest(plan), "version": todo_graph.VERSION,
             "nodes": {"unit-a": {"status": "RUNNING"}}}), encoding="utf-8")
        adapter = RecordingAdapter()
        result = durable.resume_campaign(campaign, self.cdir, adapter=adapter)
        # No trustworthy maker receipt: no blind replay, artifacts/state preserved.
        self.assertNotEqual(result["units"].get("unit-a"), "ACCEPTED")
        self.assertEqual(adapter.count("maker"), 0)
        self.assertTrue((self.cdir / "unit-a" / "progress.json").is_file())
        self.assertEqual(artifact.read_text(encoding="utf-8"), "synthetic-partial\n")

    def test_s3_supervisor_restart_takeover_new_generation_finishes_graph(self):
        adapter = self._reviewer_pool_wait_once()
        campaign = self.campaign([self.unit("unit-a"), self.unit("unit-b", depends_on=["unit-a"])])
        first = durable.run_campaign(campaign, self.cdir, adapter=adapter)
        self.assertEqual(first["status"], "RESUMABLE_POOL_WAIT")
        state_path = self.cdir / "state.json"
        st = self.read_json(state_path); st["nodes"]["unit-a"]["next_check_at"] = PAST
        state_path.write_text(json.dumps(st), encoding="utf-8")
        gen_before = self.read_json(self.cdir / "durable-state.json")["generation"]
        # Simulate orphaned run: previous supervisor pid is dead.
        durable_path = self.cdir / "durable-state.json"
        stale = self.read_json(durable_path)
        stale["pid"] = 99999999
        durable_path.write_text(json.dumps(stale), encoding="utf-8")
        fresh = RecordingAdapter()
        again = durable.resume_campaign(campaign, self.cdir, adapter=fresh)
        gen_after = self.read_json(durable_path)["generation"]
        self.assertGreater(gen_after, gen_before)
        self.assertEqual(again["units"]["unit-a"], "ACCEPTED")
        self.assertEqual(again["units"]["unit-b"], "ACCEPTED")
        self.assertEqual(fresh.count("maker", "unit-a"), 0, "takeover must not replay maker for unit-a")
        self.assertEqual(fresh.count("maker", "unit-b"), 1)


class TestS4IdempotentEvents(DurableCase):
    def test_s4_repeated_event_no_double_effect_and_payload_conflict_rejected(self):
        folder = self.root / "run"
        first = todo_graph.record_durable_event(folder, "evt-1", "hash-a")
        second = todo_graph.record_durable_event(folder, "evt-1", "hash-a")
        self.assertFalse(first["duplicate"])
        self.assertTrue(second["duplicate"])
        registry = self.read_json(folder / "durable-events.json")
        self.assertEqual(len(registry["events"]), 1)
        with self.assertRaises(ValueError):
            todo_graph.record_durable_event(folder, "evt-1", "hash-b")

    def test_s4_append_experience_and_results_are_idempotent(self):
        unit = self.unit("unit-a", dedupe_key="camp-1:unit-a")
        campaign = self.campaign([unit])
        adapter = RecordingAdapter()
        durable.run_campaign(campaign, self.cdir, adapter=adapter)
        makers_before = adapter.count("maker"); artifact_before = (self.ws / "unit-a.txt").read_bytes()
        second = RecordingAdapter()
        durable.resume_campaign(campaign, self.cdir, adapter=second)
        self.assertEqual(second.count(), 0, "repeat resume must not re-execute maker or reviewer")
        self.assertEqual(adapter.count("maker"), makers_before)
        self.assertEqual((self.ws / "unit-a.txt").read_bytes(), artifact_before)
        exp = self.cdir / "experience.jsonl"
        lines = [json.loads(x) for x in exp.read_text(encoding="utf-8").splitlines() if x.strip()]
        self.assertEqual(len([x for x in lines if x.get("unit_id") == "unit-a"]), 1)
        again = durable.append_experience(self.cdir, {"unit_id": "unit-a", "lesson_id": "l-1"})
        self.assertTrue(again.get("duplicate") or again.get("appended") is False)
        prefix = exp.read_bytes()
        durable.append_experience(self.cdir, {"unit_id": "unit-b", "lesson_id": "l-2", "summary": "second"})
        self.assertTrue(exp.read_bytes().startswith(prefix), "experience.jsonl must be append-only")
        self.assertEqual(len(exp.read_text(encoding="utf-8").splitlines()), len(prefix.decode().splitlines()) + 1)


    def test_s4_nondefault_dedupe_key_repeat_delivery_no_second_effect(self):
        # Repeated delivery of the same unit carrying a NON-default
        # dedupe_key: campaign_to_plan carries the key into the plan task,
        # campaign execution claims it in the run-folder event registry
        # (durable-events.json) before any maker runs, and re-delivery
        # must not produce a second maker/reviewer effect, a second
        # artifact write, or a second experience line.
        key = "camp-1:unit-a:delivery-7"
        unit = self.unit("unit-a", dedupe_key=key)
        campaign = self.campaign([unit])
        plan = durable.campaign_to_plan(campaign)
        self.assertEqual(plan["tasks"][0]["dedupe_key"], key)
        adapter = RecordingAdapter()
        result = durable.run_campaign(campaign, self.cdir, adapter=adapter)
        self.assertEqual(result["units"]["unit-a"], "ACCEPTED")
        record = self.read_json(self.cdir / "results" / "unit-a.json")
        self.assertEqual(record["dedupe_key"], key)
        self.assertEqual(record["status"], "ACCEPTED")
        registry = self.read_json(self.cdir / "durable-events.json")
        self.assertIn(key, registry["events"])
        artifact_before = (self.ws / "unit-a.txt").read_bytes()
        exp_before = (self.cdir / "experience.jsonl").read_bytes()
        second = RecordingAdapter()
        again = durable.resume_campaign(campaign, self.cdir, adapter=second)
        self.assertEqual(again["units"]["unit-a"], "ACCEPTED")
        self.assertEqual(second.count(), 0,
                         "repeat delivery must not re-run maker or reviewer")
        self.assertEqual((self.ws / "unit-a.txt").read_bytes(), artifact_before)
        self.assertEqual((self.cdir / "experience.jsonl").read_bytes(), exp_before)
        registry_after = self.read_json(self.cdir / "durable-events.json")
        self.assertEqual(registry_after["events"], registry["events"],
                         "repeat delivery must not add a second registry effect")

    def test_s4_campaign_redelivery_conflict_blocked_without_second_effect(self):
        # Campaign-path conflict: redelivering the same unit under the same
        # explicit dedupe_key with a changed payload is blocked by the
        # durable event registry before any adapter call.
        unit = self.unit("unit-a")
        unit["dedupe_key"] = "camp-1:unit-a:delivery-7"
        campaign = self.campaign([unit])
        first_adapter = RecordingAdapter()
        first = durable.run_campaign(campaign, self.cdir, adapter=first_adapter)
        self.assertEqual(first["status"], "ACCEPTED_CANDIDATE")
        self.assertTrue((self.cdir / "durable-events.json").exists())
        artifact_before = (self.ws / "unit-a.txt").read_bytes()
        experience_before = (self.cdir / "experience.jsonl").read_bytes()
        # Cumulative campaign consumption before the conflict: maker + reviewer.
        self.assertEqual(first["campaign_model_calls"], 2)
        self.assertEqual(
            durable.campaign_status(campaign, self.cdir)["model_calls"], 2,
            "status.model_calls must report durable campaign consumption "
            "(maker + reviewer) before any conflict")

        conflicting_unit = self.unit("unit-a")
        conflicting_unit["dedupe_key"] = "camp-1:unit-a:delivery-7"
        conflicting_unit["scope"] = ["other.txt"]
        conflicting_unit["text"] = "different payload"
        conflicting = self.campaign([conflicting_unit])
        second_adapter = RecordingAdapter()
        blocked = durable.resume_campaign(conflicting, self.cdir, adapter=second_adapter)
        self.assertEqual(blocked["status"], "BLOCKED")
        self.assertIn("Durable event conflict", blocked["reason"])
        self.assertEqual(blocked["model_calls"], 0)
        self.assertEqual(second_adapter.calls, [])
        self.assertFalse((self.ws / "other.txt").exists())
        self.assertEqual((self.ws / "unit-a.txt").read_bytes(), artifact_before)
        self.assertEqual((self.cdir / "experience.jsonl").read_bytes(), experience_before)
        registry = self.read_json(self.cdir / "durable-events.json")
        self.assertEqual(len(registry["events"]), 1)
        # Regression (durable_supervisor conflict handler): rejecting the
        # redelivery made zero NEW calls (blocked["model_calls"] == 0 above is
        # the per-attempt count and stays correct), but it must not zero the
        # campaign's durable consumption. History stays at maker + reviewer.
        self.assertEqual(
            durable.campaign_status(conflicting, self.cdir)["model_calls"], 2,
            "conflict rejection must not zero status.model_calls history")
        durable_state = self.read_json(self.cdir / "durable-state.json")
        self.assertEqual(
            durable_state["model_calls"], 2,
            "conflict rejection must not zero durable-state.json model_calls")

    def test_s4_duplicate_dedupe_key_across_units_rejected(self):
        first = self.unit("unit-a", dedupe_key="shared-key")
        second = self.unit("unit-b", dedupe_key="shared-key")
        campaign = self.campaign([first, second])
        with self.assertRaisesRegex(ValueError, "duplicate dedupe_key"):
            durable.validate_campaign(campaign)
        adapter = RecordingAdapter()
        with self.assertRaisesRegex(ValueError, "duplicate dedupe_key"):
            durable.run_campaign(campaign, self.cdir, adapter=adapter)
        self.assertEqual(adapter.calls, [])


class TestS5PoolWait(DurableCase):
    def test_s5_pool_loss_resumable_wait_and_resume_without_maker_replay(self):
        calls = {"maker": 0}

        def behavior(plan, task, role, workspace, folder, feedback, maker=None):
            if role == "maker":
                calls["maker"] += 1
                if calls["maker"] == 1:
                    return pool_wait()
                Path(workspace, task["scope"][0]).write_text("made\n", encoding="utf-8")
                return {"status": "CANDIDATE", "lineage": "maker-lineage"}
            return RecordingAdapter.default_behavior(plan, task, role, workspace, folder, feedback, maker)

        adapter = RecordingAdapter(behavior)
        campaign = self.campaign([self.unit("unit-a")])
        first = durable.run_campaign(campaign, self.cdir, adapter=adapter)
        self.assertEqual(first["status"], "RESUMABLE_POOL_WAIT")
        self.assertEqual(first["units"]["unit-a"], "RESUMABLE_POOL_WAIT")
        # Still cooling: resume must not call any model.
        calls_before = len(adapter.calls)
        waiting = durable.resume_campaign(campaign, self.cdir, adapter=adapter)
        self.assertEqual(waiting["status"], "RESUMABLE_POOL_WAIT")
        self.assertEqual(waiting["model_calls"], 0)
        self.assertEqual(len(adapter.calls), calls_before)
        # Availability returns (cooldown expired): resume finishes, maker runs once more
        # only because the first maker produced no candidate/receipt.
        state_path = self.cdir / "state.json"
        state = self.read_json(state_path)
        for node in state["nodes"].values():
            if isinstance(node, dict):
                node["next_check_at"] = PAST
        state_path.write_text(json.dumps(state), encoding="utf-8")
        done = durable.resume_campaign(campaign, self.cdir, adapter=adapter)
        self.assertEqual(done["units"]["unit-a"], "ACCEPTED")


class TestS6ScopeTreeCancel(DurableCase):
    def test_s6_tree_budget_limits_and_child_cannot_expand(self):
        with self.assertRaises(ValueError):
            durable.validate_campaign(self.campaign(
                [self.unit("unit-a")], budget={"max_depth": 2}))
        with self.assertRaises(ValueError):
            durable.validate_campaign(self.campaign(
                [self.unit("unit-a"), self.unit("unit-b")],
                budget={"max_parallel": 1, "max_total_units": 1, "max_depth": 1,
                        "allowed_pools": ["fleet"], "allowed_write_globs": ["*.txt"]}))
        with self.assertRaises(ValueError):  # depth chain a->b->c exceeds max_depth=1
            durable.validate_campaign(self.campaign([
                self.unit("unit-a"), self.unit("unit-b", depends_on=["unit-a"]),
                self.unit("unit-c", depends_on=["unit-b"])]))
        with self.assertRaises(ValueError):  # scope outside allowed_write_globs
            durable.validate_campaign(self.campaign([self.unit("unit-a", scope=["evil.py"])]))
        with self.assertRaises(ValueError):  # pool outside allowed_pools
            durable.validate_campaign(self.campaign(
                [self.unit("unit-a", pool="other-pool")]))
        # Child (task-level) budget may not expand the tree budget.
        plan = durable.campaign_to_plan(self.campaign([self.unit("unit-a")]))
        plan["tasks"][0]["tree_budget"] = {"max_parallel": 99}
        with self.assertRaises(ValueError):
            todo_graph.validate(plan)

    def test_s6_cancel_tasks_cancels_descendants(self):
        campaign = self.campaign([self.unit("unit-a"),
                                  self.unit("unit-b", depends_on=["unit-a"]),
                                  self.unit("unit-c")])
        plan = durable.campaign_to_plan(campaign)
        folder = self.root / "run"
        state = todo_graph.cancel_tasks(folder, plan, task_ids=["unit-a"], reason="test-cancel")
        self.assertEqual(state["nodes"]["unit-a"]["status"], "CANCELLED")
        self.assertEqual(state["nodes"]["unit-b"]["status"], "CANCELLED")
        self.assertNotEqual(state["nodes"].get("unit-c", {}).get("status"), "CANCELLED")
        # Campaign-level cancel also goes through todo_graph.cancel_tasks.
        out = durable.cancel_campaign(campaign, self.cdir, unit_ids=["unit-a", "unit-b", "unit-c"])
        self.assertIn(out["status"], ("CANCELLED", "CANCEL_REQUESTED", "COMPLETED"))
        self.assertEqual(out["model_calls"], 0)



    def test_s6_cancel_after_maker_preserves_cumulative_model_calls(self):
        # Regression (P2): cancellation used to save model_calls=0 even after
        # the maker had already run, and campaign_status preferred that zero
        # over node history, erasing durable consumption (SKILL.md counters).
        # Maker runs, reviewer is stopped by pool cooldown, then cancel.
        def behavior(plan, task, role, workspace, folder, feedback, maker=None):
            if role == "maker":
                Path(workspace, task["scope"][0]).write_text("made\n", encoding="utf-8")
                return {"status": "CANDIDATE", "lineage": "maker-lineage"}
            return pool_wait("Pool availability: reviewer cooldown")

        adapter = RecordingAdapter(behavior)
        campaign = self.campaign([self.unit("unit-a")])
        first = durable.run_campaign(campaign, self.cdir, adapter=adapter)
        self.assertEqual(first["status"], "RESUMABLE_POOL_WAIT")
        self.assertEqual(adapter.count("maker", "unit-a"), 1)
        self.assertEqual(adapter.count("reviewer", "unit-a"), 1)
        # Durable consumption so far is exactly the one confirmed maker call;
        # the reviewer's pool-wait response is not a model call.
        self.assertEqual(durable.campaign_status(campaign, self.cdir)["model_calls"], 1)
        calls_before_cancel = list(adapter.calls)

        cancelled = durable.cancel_campaign(campaign, self.cdir, unit_ids=["unit-a"])
        self.assertEqual(cancelled["status"], "CANCELLED")
        self.assertEqual(cancelled["units"]["unit-a"], "CANCELLED")
        self.assertEqual(adapter.calls, calls_before_cancel,
                         "cancel itself must not invoke any model")
        # Cumulative usage survives cancellation in the return value, in
        # durable-state.json, and through campaign_status (which must not
        # let a stored zero override node history).
        self.assertEqual(cancelled["model_calls"], 1)
        self.assertEqual(cancelled.get("campaign_model_calls"), 1)
        self.assertEqual(self.read_json(self.cdir / "durable-state.json")["model_calls"], 1)
        status = durable.campaign_status(campaign, self.cdir)
        self.assertEqual(status["status"], "CANCELLED")
        self.assertEqual(status["model_calls"], 1)
        self.assertEqual(status["units"]["unit-a"], "CANCELLED")
        # Node history still evidences the maker invocation.
        state = self.read_json(self.cdir / "state.json")
        self.assertEqual(durable._model_calls(state["nodes"]), 1)

    def test_s6_max_parallel_peak_and_child_expansion_rejected(self):
        def slow(plan, task, role, workspace, folder, feedback, maker=None):
            if role == "maker":
                time.sleep(0.35)
            return RecordingAdapter.default_behavior(plan, task, role, workspace, folder, feedback, maker)
        adapter = RecordingAdapter(slow)
        campaign = self.campaign([self.unit("unit-a"), self.unit("unit-b"), self.unit("unit-c")])
        out = durable.run_campaign(campaign, self.cdir, adapter=adapter)
        self.assertTrue(all(v == "ACCEPTED" for v in out["units"].values()), out)
        self.assertLessEqual(adapter.peak, 2)
        self.assertGreaterEqual(adapter.peak, 2)
        for budget in ({"allowed_pools": ["fleet", "other"]}, {"max_parallel": 99}):
            plan = durable.campaign_to_plan(self.campaign([self.unit("unit-a")]))
            plan["tasks"][0]["tree_budget"] = budget
            with self.assertRaises((ValueError, RuntimeError)):
                todo_graph.validate(plan)
        # Child scope expansion: parent allows only unit-a.txt; a wider,
        # valid relative child allowed_scope must still be rejected as an
        # expansion (not merely as path traversal).
        plan = durable.campaign_to_plan(self.campaign([self.unit("unit-a")]))
        plan["tree_budget"]["allowed_scope"] = ["unit-a.txt"]
        plan["tasks"][0]["tree_budget"] = {"allowed_scope": ["other.txt"]}
        with self.assertRaisesRegex(ValueError, "allowed_scope cannot expand|allowed_scope excludes"):
            todo_graph.validate(plan)

    def test_s7_lockbox_leak_via_inputs_and_visible_text_rejected_before_adapter(self):
        # Inputs case: a *valid relative* input pointing at a lockbox that
        # lives inside the synthetic workspace. This must be rejected by
        # lockbox isolation itself (not by relative-path validation, which
        # an absolute lockbox path would trip first).
        vault = self.ws / "vault"; vault.mkdir()
        inside_lockbox = vault / "ref.json"
        inside_lockbox.write_text("{}", encoding="utf-8")
        unit = self.unit("unit-a")
        unit["inputs"] = ["vault/ref.json"]
        adapter = RecordingAdapter()
        with self.assertRaisesRegex(ValueError, "lockbox"):
            durable.run_campaign(
                self.campaign([unit], lockbox_ref=str(inside_lockbox)),
                self.root / "evidence-inputs" / "campaign", adapter=adapter)
        self.assertEqual(adapter.count(), 0)
        # Visible-text case: lockbox path spelled out in maker-visible text.
        unit = self.unit("unit-a")
        unit["text"] = "do unit-a using " + self.lockbox
        adapter = RecordingAdapter()
        with self.assertRaisesRegex(ValueError, "lockbox"):
            durable.run_campaign(self.campaign([unit]),
                                 self.root / "evidence-text" / "campaign",
                                 adapter=adapter)
        self.assertEqual(adapter.count(), 0)

class TestS7ZeroCallsAndCampaignIntegration(DurableCase):
    def test_s7_empty_queue_zero_model_calls(self):
        adapter = RecordingAdapter()
        result = durable.run_campaign(self.campaign([]), self.cdir, adapter=adapter)
        self.assertEqual(result["status"], "EMPTY")
        self.assertEqual(result["model_calls"], 0)
        self.assertEqual(adapter.calls, [])

    def test_s7_lockbox_isolated_results_and_experience_written(self):
        campaign = self.campaign([self.unit("unit-a", dedupe_key="camp-1:unit-a")])
        plan = durable.campaign_to_plan(campaign)
        self.assertNotIn("lockbox", json.dumps(plan))
        durable.ensure_lockbox_isolated(campaign)
        bad = self.campaign([self.unit("unit-a", scope=["."])])
        with self.assertRaises(ValueError):
            durable.validate_campaign(bad)
        adapter = RecordingAdapter()
        result = durable.run_campaign(campaign, self.cdir, adapter=adapter)
        self.assertEqual(result["units"]["unit-a"], "ACCEPTED")
        record = self.read_json(self.cdir / "results" / "unit-a.json")
        self.assertEqual(record["schema_version"], "arbor-unit-result/v1")
        self.assertEqual(record["unit_id"], "unit-a")
        self.assertEqual(record["dedupe_key"], "camp-1:unit-a")
        self.assertEqual(record["status"], "ACCEPTED")
        self.assertIn("artifact_hashes", record)
        self.assertIn("check_results", record)
        exp_lines = (self.cdir / "experience.jsonl").read_text(encoding="utf-8").splitlines()
        self.assertEqual(len([x for x in exp_lines if x.strip()]), 1)
        rec = json.loads(exp_lines[0])
        self.assertEqual(rec.get("schema_version"), "arbor-experience/v1")


if __name__ == "__main__":
    unittest.main(verbosity=2)
