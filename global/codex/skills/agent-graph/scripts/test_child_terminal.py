import hashlib
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch


SCRIPTS = Path(__file__).parent


def load(name):
    spec = importlib.util.spec_from_file_location(name + "_under_test", SCRIPTS / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


office_bridge = load("office_bridge")
child_terminal = load("child_terminal")
todo_graph = load("todo_graph")

# Synthetic child runs must not publish TODO journals into the real user home.
# Scoped to this module; the caller's explicit telemetry setting is restored on
# teardown. Child processes inherit the scoped value. Nothing changes at import.
_test_telemetry = patch.dict(os.environ, {"AGENT_TODO_TELEMETRY": "off"})


def setUpModule():
    _test_telemetry.start()


def tearDownModule():
    _test_telemetry.stop()


class FakeAdapter:
    def invoke(self, plan, task, role, workspace, folder, feedback, maker=None):
        if role == "maker":
            return {"status": "CANDIDATE", "lineage": "maker-lineage"}
        return {
            "status": "REVIEWED",
            "verdict": "ACCEPT",
            "checked": list(task["done_when"]),
            "findings": [],
            "lineage": "reviewer-lineage",
        }


class ChildTerminalTests(unittest.TestCase):
    parent_names = (
        "OFFICE_PARENT_AGENT",
        "CLAUDE_CODE_SESSION_ID",
        "CODEX_THREAD_ID",
        "CODEX_SESSION_ID",
    )

    @staticmethod
    def _mkdtemp():
        # The managed Windows sandbox denies descendants when mkdtemp forwards
        # its POSIX-only 0700 mode. Keep mkdtemp, but let Windows use its normal
        # inherited ACL by omitting that mode at the actual mkdir call.
        mkdir = os.mkdir
        with patch.object(os, "mkdir", side_effect=lambda path, mode=0o777: mkdir(path)):
            return tempfile.mkdtemp(prefix="agent-graph-child-terminal-")

    def setUp(self):
        self.root = Path(self._mkdtemp())
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        self.office_home = self.root / "coord"
        self.office_home.mkdir()
        self.runtime = self.root / "runtime"
        self.runtime.mkdir()
        self.log = self.root / "office-argv.jsonl"
        self._write_fake_office()

    def _write_fake_office(self):
        script = self.runtime / "office.py"
        script.write_text(
            """import json
import os
from pathlib import Path
import sys

argv = sys.argv[1:]
with Path(os.environ[\"FAKE_OFFICE_LOG\"]).open(\"a\", encoding=\"utf-8\") as stream:
    stream.write(json.dumps(argv, ensure_ascii=True) + \"\\n\")
command = \"who\" if \"who\" in argv else \"send\" if \"send\" in argv else \"unknown\"
if command == \"who\":
    agents = os.environ.get(\"FAKE_PARENT_AGENTS\")
    if agents is not None:
        online = [{\"agent\": agent, \"self\": False} for agent in agents.split(\",\") if agent]
    else:
        agent = os.environ.get(\"FAKE_PARENT_AGENT\")
        is_self = os.environ.get(\"FAKE_PARENT_SELF\", \"1\") == \"1\"
        online = [{\"agent\": agent, \"self\": is_self}] if agent else []
    print(json.dumps({\"status\": \"ok\", \"online\": online}))
elif os.environ.get(\"FAKE_OFFICE_FAIL\") == \"1\":
    raise SystemExit(9)
else:
    print(json.dumps({\"status\": \"ok\", \"code\": \"sent\"}))
""",
            encoding="utf-8",
        )
        marker = {
            "managed_by": "agent-office-installer",
            "package_id": "agent-office",
            "schema_version": 1,
            "package_version": "1.0.0",
            "tool": "runtime",
            "package_fingerprint": "a" * 64,
            "source_fingerprint": office_bridge._runtime_source_fingerprint(self.runtime),
            "python_executable": sys.executable,
        }
        (self.runtime / ".agent-office-runtime.json").write_text(json.dumps(marker), encoding="utf-8")

    def _plan(self, check=None):
        task = {
            "id": "one",
            "text": "synthetic child",
            "covers": ["REQ-1"],
            "done_when": ["synthetic acceptance"],
            "scope": ["artifact.txt"],
            "inputs": [],
            "depends_on": [],
            "maker": [{"agent": "maker", "model": "m", "reason": "fit", "score": 80}],
            "reviewer": [{"agent": "reviewer", "model": "r", "reason": "fit", "score": 80}],
            # Implement tasks need one executable check (agent-graph validation rule).
            "checks": [check] if check else [[sys.executable, "-c", "pass"]],
            "requires_approval": [],
        }
        return {
            "version": 1,
            "execution_context": "legacy-test",
            "goal": "synthetic graph",
            "planner": {"model": "parent", "source": "test"},
            "requirements": ["REQ-1"],
            "max_parallel": 1,
            "workspace": str(self.workspace),
            "tasks": [task],
        }

    def _environment(self, **extra):
        values = {
            "AGENT_TODO_TELEMETRY": "off",
            "AGENT_GRAPH_WATCH_EVENTS": "1",
            "COORDFS_HOME": str(self.office_home),
            "FAKE_OFFICE_LOG": str(self.log),
        }
        values.update(extra)
        return values

    def _run(self, run_name="run-terminal", **environment):
        run_dir = self.root / run_name
        with patch.dict(os.environ, self._environment(**environment), clear=False):
            for name in self.parent_names:
                if name not in environment:
                    os.environ.pop(name, None)
            with patch.object(child_terminal, "_runtime_root", return_value=self.runtime):
                with patch.object(todo_graph.Path, "home", return_value=self.root):
                    result = todo_graph.run(
                        self._plan(), run_dir, adapter=FakeAdapter(), emit_terminal=True,
                    )
        return result, run_dir

    def _calls(self):
        if not self.log.exists():
            return []
        return [json.loads(line) for line in self.log.read_text(encoding="utf-8").splitlines()]

    @staticmethod
    def _option(argv, name):
        return argv[argv.index(name) + 1]

    def _send_events(self):
        return [(argv, json.loads(self._option(argv, "--body"))) for argv in self._calls() if "send" in argv]

    def test_run_sends_one_channel_and_one_parent_event_with_exact_contract(self):
        parent = "claude-host-parent-session"
        result, run_dir = self._run(
            CLAUDE_CODE_SESSION_ID="parent-session",
            FAKE_PARENT_AGENT=parent,
        )
        self.assertEqual(result["status"], "ACCEPTED_CANDIDATE")
        calls = self._calls()
        self.assertEqual(sum("who" in argv for argv in calls), 1)
        sends = self._send_events()
        self.assertEqual(len(sends), 2)
        channel = next((argv, body) for argv, body in sends if "--channel" in argv)
        direct = next((argv, body) for argv, body in sends if "--to" in argv)
        self.assertEqual(self._option(channel[0], "--channel"), "watch-events")
        self.assertEqual(self._option(direct[0], "--to"), parent)
        self.assertEqual(channel[1], direct[1])
        body = channel[1]
        expected_keys = {
            "schema", "event_id", "run_id", "source", "terminal_state", "result_ref",
            "parent_agent", "parent_session", "parent_harness", "workspace", "started_at", "observed_at",
        }
        self.assertEqual(set(body), expected_keys)
        self.assertEqual(body["schema"], "clawops.child-terminal/v1")
        self.assertEqual(body["run_id"], run_dir.name)
        self.assertEqual(body["source"], "agent-graph")
        self.assertEqual(body["terminal_state"], "ACCEPTED_CANDIDATE")
        self.assertEqual(body["result_ref"], str((run_dir / "state.json").resolve()))
        self.assertEqual(body["parent_agent"], parent)
        self.assertEqual(body["parent_session"], "parent-session")
        self.assertEqual(body["parent_harness"], "claude")
        self.assertEqual(body["workspace"], str(self.workspace.resolve()))
        expected_id = hashlib.sha256((run_dir.name + "ACCEPTED_CANDIDATE").encode("utf-8")).hexdigest()
        self.assertEqual(body["event_id"], expected_id)
        self.assertNotIn("goal", json.dumps(body))
        self.assertNotIn("nodes", json.dumps(body))
        receipt = todo_graph.read(run_dir / "watch-event.json")
        self.assertTrue(receipt["channel_sent"])
        self.assertTrue(receipt["parent_sent"])
        for argv, _ in sends:
            self.assertEqual(self._option(argv, "--harness"), "agent-graph")
            self.assertEqual(self._option(argv, "--session-id"), run_dir.name)
            self.assertEqual(self._option(argv, "--kind"), "note")

    def test_programmatic_run_does_not_emit_without_explicit_opt_in(self):
        run_dir = self.root / "run-programmatic-default"
        with patch.dict(os.environ, self._environment(), clear=False):
            for name in self.parent_names:
                os.environ.pop(name, None)
            with patch.object(child_terminal, "_runtime_root", return_value=self.runtime):
                with patch.object(todo_graph.Path, "home", return_value=self.root):
                    result = todo_graph.run(self._plan(), run_dir, adapter=FakeAdapter())
        self.assertEqual(result["status"], "ACCEPTED_CANDIDATE")
        self.assertEqual(self._calls(), [])
        self.assertFalse((run_dir / "watch-event.json").exists())

    def test_cli_run_explicitly_enables_terminal_event(self):
        todo_path = self.root / "todo.json"
        todo_path.write_text(json.dumps(self._plan()), encoding="utf-8")
        run_dir = self.root / "run-cli"
        captured = {}

        def fake_run(plan, folder, **kwargs):
            captured.update(kwargs)
            return {"status": "ACCEPTED_CANDIDATE", "nodes": {}}

        argv = [
            "todo_graph.py", "run", "--todo", str(todo_path), "--run-dir", str(run_dir),
        ]
        with patch.object(sys, "argv", argv):
            with patch.object(todo_graph, "run", side_effect=fake_run):
                with patch("builtins.print"):
                    exit_code = todo_graph.main()
        self.assertEqual(exit_code, 0)
        self.assertIs(captured.get("emit_terminal"), True)

    def test_without_parent_variables_sends_only_channel_and_uses_null_parent(self):
        _result, run_dir = self._run(run_name="run-no-parent")
        sends = self._send_events()
        self.assertEqual(len(sends), 1)
        self.assertIn("--channel", sends[0][0])
        self.assertIsNone(sends[0][1]["parent_agent"])
        self.assertIsNone(sends[0][1]["parent_session"])
        self.assertIsNone(sends[0][1]["parent_harness"])
        receipt = todo_graph.read(run_dir / "watch-event.json")
        self.assertTrue(receipt["channel_sent"])
        self.assertFalse(receipt["parent_requested"])

    def test_same_terminal_state_is_emitted_only_once_for_the_run(self):
        self._run(run_name="run-idempotent")
        self._run(run_name="run-idempotent")
        self.assertEqual(len(self._send_events()), 1)

    def test_explicit_parent_agent_has_precedence_and_skips_who(self):
        explicit = "explicit-parent-address"
        self._run(
            run_name="run-explicit-parent",
            OFFICE_PARENT_AGENT=explicit,
            CLAUDE_CODE_SESSION_ID="parent-session",
            FAKE_PARENT_AGENT="discovered-parent-address",
        )
        calls = self._calls()
        self.assertFalse(any("who" in argv for argv in calls))
        sends = self._send_events()
        direct = next((argv, body) for argv, body in sends if "--to" in argv)
        self.assertEqual(self._option(direct[0], "--to"), explicit)
        self.assertEqual(direct[1]["parent_agent"], explicit)

    def test_codex_thread_precedes_session_for_parent_lookup(self):
        self._run(
            run_name="run-codex-parent",
            CODEX_THREAD_ID="codex-thread-parent",
            CODEX_SESSION_ID="codex-session-fallback",
            FAKE_PARENT_AGENT="codex-parent-address",
        )
        who = next(argv for argv in self._calls() if "who" in argv)
        self.assertEqual(self._option(who, "--harness"), "codex")
        self.assertEqual(self._option(who, "--session-id"), "codex-thread-parent")
        body = self._send_events()[0][1]
        self.assertEqual(body["parent_session"], "codex-thread-parent")
        self.assertEqual(body["parent_harness"], "codex")

    def test_who_uses_single_unambiguous_agent_without_self_marker(self):
        self._run(
            run_name="run-single-agent-without-self",
            CLAUDE_CODE_SESSION_ID="missing-parent-session",
            FAKE_PARENT_AGENT="resolved-parent-address",
            FAKE_PARENT_SELF="0",
        )
        sends = self._send_events()
        self.assertEqual(len(sends), 2)
        direct = next((argv, body) for argv, body in sends if "--to" in argv)
        self.assertEqual(self._option(direct[0], "--to"), "resolved-parent-address")
        self.assertEqual(direct[1]["parent_agent"], "resolved-parent-address")

    def test_who_does_not_resolve_multiple_agents_without_self_marker(self):
        self._run(
            run_name="run-ambiguous-agents-without-self",
            CLAUDE_CODE_SESSION_ID="ambiguous-parent-session",
            FAKE_PARENT_AGENTS="first-agent-address,second-agent-address",
        )
        sends = self._send_events()
        self.assertEqual(len(sends), 1)
        self.assertIn("--channel", sends[0][0])
        self.assertIsNone(sends[0][1]["parent_agent"])

    def test_secret_shaped_metadata_is_rejected_without_changing_run_result(self):
        shaped = "gh" + "p_" + ("a" * 16)
        result, run_dir = self._run(
            run_name="run-secret-shaped-parent",
            OFFICE_PARENT_AGENT=shaped,
        )
        self.assertEqual(result["status"], "ACCEPTED_CANDIDATE")
        self.assertEqual(self._send_events(), [])
        receipt = todo_graph.read(run_dir / "watch-event.json")
        self.assertEqual(receipt["status"], "error")
        self.assertIn("invalid_input", receipt["errors"])

    def test_office_failure_is_fail_open_and_is_recorded(self):
        result, run_dir = self._run(run_name="run-office-failure", FAKE_OFFICE_FAIL="1")
        self.assertEqual(result["status"], "ACCEPTED_CANDIDATE")
        self.assertEqual(todo_graph.read(run_dir / "state.json")["status"], "ACCEPTED_CANDIDATE")
        receipt = todo_graph.read(run_dir / "watch-event.json")
        self.assertFalse(receipt["channel_sent"])
        self.assertEqual(receipt["status"], "error")
        self.assertIn("channel_send_failed", receipt["errors"])

    def test_run_exception_emits_blocked_and_preserves_exception(self):
        run_dir = self.root / "run-exception"
        with patch.dict(os.environ, self._environment(), clear=False):
            for name in self.parent_names:
                os.environ.pop(name, None)
            with patch.object(child_terminal, "_runtime_root", return_value=self.runtime):
                with patch.object(todo_graph.Path, "home", return_value=self.root):
                    with patch.object(todo_graph, "_run", side_effect=RuntimeError("synthetic failure")):
                        with self.assertRaisesRegex(RuntimeError, "synthetic failure"):
                            todo_graph.run(
                                self._plan(), run_dir, adapter=FakeAdapter(), emit_terminal=True,
                            )
        sends = self._send_events()
        self.assertEqual(len(sends), 1)
        self.assertEqual(sends[0][1]["terminal_state"], "BLOCKED")
        receipt = todo_graph.read(run_dir / "watch-event.json")
        self.assertTrue(receipt["channel_sent"])

    def test_check_child_receives_agent_graph_run_id(self):
        observed = self.workspace / "observed-run-id.txt"
        check = [
            sys.executable,
            "-c",
            "import os,pathlib,sys;pathlib.Path(sys.argv[1]).write_text(os.environ.get('AGENT_GRAPH_RUN_ID',''),encoding='utf-8')",
            str(observed),
        ]
        run_dir = self.root / "run-child-env"
        with patch.dict(os.environ, self._environment(), clear=False):
            for name in self.parent_names:
                os.environ.pop(name, None)
            with patch.object(child_terminal, "_runtime_root", return_value=self.runtime):
                with patch.object(todo_graph.Path, "home", return_value=self.root):
                    result = todo_graph.run(self._plan(check), run_dir, adapter=FakeAdapter())
        self.assertEqual(result["status"], "ACCEPTED_CANDIDATE")
        self.assertEqual(observed.read_text(encoding="utf-8"), run_dir.name)

    def test_fleet_bridge_receives_agent_graph_run_id(self):
        observed = self.root / "fleet-observed-run-id.txt"
        bridge = self.root / "fake-todo-fleet.py"
        bridge.write_text(
            """import json
import os
from pathlib import Path
import sys

output = Path(sys.argv[sys.argv.index(\"--output\") + 1])
output.parent.mkdir(parents=True, exist_ok=True)
output.write_text(json.dumps({\"status\": \"CANDIDATE\", \"lineage\": \"fake\"}), encoding=\"utf-8\")
Path(os.environ[\"FAKE_FLEET_ENV_LOG\"]).write_text(
    os.environ.get(\"AGENT_GRAPH_RUN_ID\", \"\"), encoding=\"utf-8\"
)
""",
            encoding="utf-8",
        )
        adapter = todo_graph.FleetAdapter(bridge)
        plan = self._plan()
        task = plan["tasks"][0]
        with patch.dict(os.environ, {"FAKE_FLEET_ENV_LOG": str(observed)}, clear=False):
            result = adapter.invoke(
                plan,
                task,
                "maker",
                self.workspace,
                self.root / "fleet-call",
                {},
                child_env={"AGENT_GRAPH_RUN_ID": "run-from-graph"},
            )
        self.assertEqual(result["status"], "CANDIDATE")
        self.assertEqual(observed.read_text(encoding="utf-8"), "run-from-graph")


if __name__ == "__main__":
    unittest.main()
