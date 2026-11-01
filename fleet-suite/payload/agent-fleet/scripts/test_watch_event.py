from __future__ import annotations

import datetime as dt
import hashlib
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock


SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

import fleet  # noqa: E402


RUN_ID = "run-20260925T120000Z-aaaaaaaaaa"
STARTED_AT = "2026-09-25T12:00:00Z"
PARENT_AGENT = "codex-fixture-parent-session"
OFFICE_STUB_SOURCE = """from __future__ import annotations

import json
import os
from pathlib import Path
import sys

root = Path(__file__).resolve().parent
log_path = Path(os.environ["FAKE_OFFICE_LOG"])
with log_path.open("a", encoding="utf-8") as handle:
    handle.write(json.dumps({"argv": sys.argv[1:]}, ensure_ascii=True) + "\\n")
if "who" in sys.argv:
    payload = {
        "status": "ok",
        "online": [
            {"agent": "unrelated-fixture-agent", "self": False},
            {"agent": "codex-fixture-parent-session", "self": True},
        ],
    }
    sys.stdout.write(json.dumps(payload))
    raise SystemExit(0)
if os.environ.get("FAKE_OFFICE_FAIL") == "1":
    sys.stdout.write(json.dumps({"status": "error", "code": "synthetic_failure"}))
    raise SystemExit(7)
target = "channel" if "--channel" in sys.argv else "parent"
sys.stdout.write(json.dumps({"status": "ok", "id": "message-" + target}))
raise SystemExit(0)
"""


class WatchEventTests(unittest.TestCase):
    def make_runtime(self) -> tuple[Path, Path]:
        # Python 3.13 forwards POSIX-only 0700 to Windows. The managed sandbox
        # cannot enter that ACL, so keep mkdtemp but let mkdir inherit normally.
        native_mkdir = os.mkdir
        with mock.patch.object(
            os,
            "mkdir",
            side_effect=lambda path, _mode=0o777: native_mkdir(path),
        ):
            root = Path(tempfile.mkdtemp(prefix="fleet-watch-event-"))
        runtime = root / "runtime"
        runtime.mkdir()
        launcher = runtime / "office.cmd"
        launcher.write_text("synthetic launcher\n", encoding="utf-8")
        (runtime / "office.py").write_text(
            OFFICE_STUB_SOURCE, encoding="utf-8", newline="\n"
        )
        source_fingerprint = fleet._watch_office_source_fingerprint(runtime)
        self.assertIsNotNone(source_fingerprint)
        (runtime / ".agent-office-runtime.json").write_text(
            json.dumps(
                {
                    "managed_by": "agent-office-installer",
                    "package_id": "agent-office",
                    "schema_version": 1,
                    "package_version": "1.0.0",
                    "tool": "runtime",
                    "package_fingerprint": "a" * 64,
                    "source_fingerprint": source_fingerprint,
                    "python_executable": sys.executable,
                }
            ),
            encoding="utf-8",
        )
        return root, launcher

    def safe_environment(self, root: Path, **extra: str) -> dict[str, str]:
        environment = {
            key: os.environ[key]
            for key in ("PATH", "SystemRoot", "WINDIR", "TEMP", "TMP")
            if key in os.environ
        }
        environment["COORDFS_HOME"] = str(root / "coord")
        environment["FAKE_OFFICE_LOG"] = str(root / "office-calls.jsonl")
        environment.update(extra)
        return environment

    def run_result(self, root: Path, *, status: str = "COMPLETED") -> dict[str, object]:
        run_dir = root / "evidence" / RUN_ID
        run_dir.mkdir(parents=True)
        workspace = root / "workspace with spaces"
        workspace.mkdir()
        return {
            "action": "run",
            "status": status,
            "run_id": RUN_ID,
            "workspace": str(workspace),
            "output_dir": str(run_dir),
            "started_at": STARTED_AT,
            "handoff": {
                "status": "completed",
                "summary": "RESULT CONTENT MUST NOT ENTER THE EVENT BODY",
            },
        }

    def calls(self, root: Path) -> list[list[str]]:
        path = root / "office-calls.jsonl"
        if not path.is_file():
            return []
        return [json.loads(line)["argv"] for line in path.read_text(encoding="utf-8").splitlines()]

    def send_calls(self, root: Path) -> list[list[str]]:
        return [row for row in self.calls(root) if "send" in row]

    def event_body(self, call: list[str]) -> tuple[str, dict[str, object]]:
        body = call[call.index("--body") + 1]
        return body, json.loads(body)

    def test_standalone_run_sends_channel_and_resolved_parent_copy(self) -> None:
        root, launcher = self.make_runtime()
        result = self.run_result(root)
        environment = self.safe_environment(
            root, CLAUDE_CODE_SESSION_ID="fixture-parent-session"
        )
        with (
            mock.patch.dict(os.environ, environment, clear=True),
            mock.patch.object(fleet, "office_command_path", return_value=launcher),
        ):
            finalized = fleet.finalize_run_watch_event(result)

        calls = self.calls(root)
        self.assertEqual(len(calls), 3)
        self.assertEqual(
            calls[0][:5],
            ["--harness", "claude", "--session-id", "fixture-parent-session", "who"],
        )
        sends = self.send_calls(root)
        self.assertEqual(len(sends), 2)
        self.assertIn("--channel", sends[0])
        self.assertEqual(sends[0][sends[0].index("--channel") + 1], "watch-events")
        self.assertIn("--to", sends[1])
        self.assertEqual(sends[1][sends[1].index("--to") + 1], PARENT_AGENT)
        for call in sends:
            self.assertEqual(
                call[:4], ["--harness", "fleet", "--session-id", RUN_ID]
            )
            self.assertEqual(call[call.index("--kind") + 1], "note")
        self.assertEqual(finalized["status"], "COMPLETED")
        self.assertEqual(finalized["watch_event"]["status"], "sent")
        self.assertEqual(
            finalized["watch_event"]["schema"],
            "clawops.child-terminal-emission/v1",
        )
        self.assertTrue(finalized["watch_event"]["attempted"])
        self.assertTrue(finalized["watch_event"]["channel_sent"])
        self.assertTrue(finalized["watch_event"]["parent_requested"])
        self.assertTrue(finalized["watch_event"]["parent_sent"])
        self.assertEqual(finalized["watch_event"]["channel"]["status"], "sent")
        self.assertEqual(finalized["watch_event"]["parent"]["status"], "sent")

    def test_agent_graph_child_skips_every_office_call_and_records_skip(self) -> None:
        root, launcher = self.make_runtime()
        result = self.run_result(root)
        environment = self.safe_environment(
            root,
            AGENT_GRAPH_RUN_ID="graph-fixture-run",
            OFFICE_PARENT_AGENT=PARENT_AGENT,
        )
        with (
            mock.patch.dict(os.environ, environment, clear=True),
            mock.patch.object(fleet, "office_command_path", return_value=launcher),
        ):
            finalized = fleet.finalize_run_watch_event(result)

        self.assertEqual(self.calls(root), [])
        self.assertEqual(finalized["status"], "COMPLETED")
        self.assertEqual(finalized["watch_event"]["status"], "skipped")
        self.assertFalse(finalized["watch_event"]["attempted"])
        self.assertFalse(finalized["watch_event"]["channel_sent"])
        self.assertFalse(finalized["watch_event"]["parent_sent"])
        self.assertEqual(
            finalized["watch_event"]["reason"], "agent-graph-child"
        )
        result_path = Path(finalized["watch_event"]["result_ref"])
        self.assertTrue(result_path.is_file())
        persisted = json.loads(result_path.read_text(encoding="ascii"))
        self.assertEqual(persisted["watch_event"], finalized["watch_event"])

    def test_run_without_parent_sends_only_channel_with_null_parent_fields(self) -> None:
        root, launcher = self.make_runtime()
        result = self.run_result(root)
        environment = self.safe_environment(root)
        with (
            mock.patch.dict(os.environ, environment, clear=True),
            mock.patch.object(fleet, "office_command_path", return_value=launcher),
        ):
            finalized = fleet.finalize_run_watch_event(result)

        sends = self.send_calls(root)
        self.assertEqual(len(sends), 1)
        self.assertIn("--channel", sends[0])
        _body, event = self.event_body(sends[0])
        self.assertIsNone(event["parent_agent"])
        self.assertIsNone(event["parent_session"])
        self.assertIsNone(event["parent_harness"])
        self.assertEqual(finalized["watch_event"]["status"], "sent")
        self.assertFalse(finalized["watch_event"]["parent_requested"])
        self.assertFalse(finalized["watch_event"]["parent_sent"])

    def test_same_terminal_result_is_emitted_only_once(self) -> None:
        root, launcher = self.make_runtime()
        result = self.run_result(root)
        environment = self.safe_environment(root)
        with (
            mock.patch.dict(os.environ, environment, clear=True),
            mock.patch.object(fleet, "office_command_path", return_value=launcher),
        ):
            first = fleet.finalize_run_watch_event(result)
            second = fleet.finalize_run_watch_event(result)

        self.assertEqual(len(self.send_calls(root)), 1)
        self.assertEqual(first["watch_event"], second["watch_event"])
        self.assertEqual(second["watch_event"]["status"], "sent")

    def test_office_failure_is_fail_open_for_terminal_run_status(self) -> None:
        root, launcher = self.make_runtime()
        result = self.run_result(root, status="BLOCKED")
        environment = self.safe_environment(
            root, OFFICE_PARENT_AGENT=PARENT_AGENT, FAKE_OFFICE_FAIL="1"
        )
        with (
            mock.patch.dict(os.environ, environment, clear=True),
            mock.patch.object(fleet, "office_command_path", return_value=launcher),
        ):
            finalized = fleet.finalize_run_watch_event(result)

        self.assertEqual(finalized["status"], "BLOCKED")
        self.assertEqual(finalized["watch_event"]["status"], "error")
        self.assertTrue(finalized["watch_event"]["attempted"])
        self.assertFalse(finalized["watch_event"]["channel_sent"])
        self.assertFalse(finalized["watch_event"]["parent_sent"])
        self.assertIn("channel_send_failed", finalized["watch_event"]["errors"])
        self.assertIn("parent_send_failed", finalized["watch_event"]["errors"])
        self.assertEqual(finalized["watch_event"]["channel"]["status"], "error")
        self.assertEqual(finalized["watch_event"]["parent"]["status"], "error")
        self.assertEqual(len(self.send_calls(root)), 2)
        persisted = json.loads(
            Path(finalized["watch_event"]["result_ref"]).read_text(encoding="ascii")
        )
        self.assertEqual(persisted["status"], "BLOCKED")
        self.assertEqual(persisted["watch_event"]["status"], "error")

    def test_body_is_one_line_metadata_only_and_matches_contract(self) -> None:
        root, launcher = self.make_runtime()
        result = self.run_result(root)
        explicit_parent = "codex-explicit-parent"
        environment = self.safe_environment(
            root,
            OFFICE_PARENT_AGENT=explicit_parent,
            CODEX_THREAD_ID="fixture-thread-parent",
        )
        with (
            mock.patch.dict(os.environ, environment, clear=True),
            mock.patch.object(fleet, "office_command_path", return_value=launcher),
        ):
            finalized = fleet.finalize_run_watch_event(result)

        calls = self.calls(root)
        self.assertFalse(any("who" in call for call in calls))
        sends = self.send_calls(root)
        self.assertEqual(len(sends), 2)
        channel_body, event = self.event_body(sends[0])
        parent_body, parent_event = self.event_body(sends[1])
        self.assertEqual(channel_body, parent_body)
        self.assertEqual(event, parent_event)
        self.assertEqual(len(channel_body.splitlines()), 1)
        self.assertEqual(
            set(event),
            {
                "schema",
                "event_id",
                "run_id",
                "source",
                "terminal_state",
                "result_ref",
                "parent_agent",
                "parent_session",
                "parent_harness",
                "workspace",
                "started_at",
                "observed_at",
            },
        )
        self.assertEqual(event["schema"], "clawops.child-terminal/v1")
        self.assertEqual(
            event["event_id"],
            hashlib.sha256((RUN_ID + "COMPLETED").encode("utf-8")).hexdigest(),
        )
        self.assertEqual(event["run_id"], RUN_ID)
        self.assertEqual(event["source"], "fleet")
        self.assertEqual(event["terminal_state"], "COMPLETED")
        self.assertEqual(event["parent_agent"], explicit_parent)
        self.assertEqual(event["parent_session"], "fixture-thread-parent")
        self.assertEqual(event["parent_harness"], "codex")
        self.assertEqual(event["workspace"], str(Path(result["workspace"]).resolve()))
        self.assertEqual(event["started_at"], STARTED_AT)
        self.assertEqual(
            event["result_ref"], str((Path(result["output_dir"]) / "result.json").resolve())
        )
        observed = dt.datetime.fromisoformat(
            str(event["observed_at"]).replace("Z", "+00:00")
        )
        self.assertEqual(observed.utcoffset(), dt.timedelta(0))
        self.assertNotIn("RESULT CONTENT", channel_body)
        self.assertEqual(finalized["watch_event"]["event"], event)
        self.assertEqual(
            Path(finalized["watch_event"]["result_ref"]),
            Path(event["result_ref"]),
        )

    def test_secret_shaped_parent_metadata_is_rejected_before_office(self) -> None:
        root, launcher = self.make_runtime()
        result = self.run_result(root)
        secret_shaped_session = "sk-" + ("fixture" * 4)
        environment = self.safe_environment(
            root, CODEX_THREAD_ID=secret_shaped_session
        )
        with (
            mock.patch.dict(os.environ, environment, clear=True),
            mock.patch.object(fleet, "office_command_path", return_value=launcher),
        ):
            finalized = fleet.finalize_run_watch_event(result)

        self.assertEqual(self.calls(root), [])
        self.assertEqual(finalized["status"], "COMPLETED")
        self.assertEqual(finalized["watch_event"]["status"], "error")
        self.assertIn("invalid_input", finalized["watch_event"]["errors"])
        persisted = json.loads(
            Path(finalized["watch_event"]["result_ref"]).read_text(encoding="ascii")
        )
        self.assertNotIn(secret_shaped_session, json.dumps(persisted["watch_event"]))

    def test_run_command_finalizes_the_worker_result_once(self) -> None:
        root, launcher = self.make_runtime()
        result = self.run_result(root)
        route = {
            "delegation_decision": "DELEGATE",
            "selected_model": "fixture-model",
            "selected_execution_profile_id": "fixture-execution",
            "task_profile": {"required_tools": []},
            "readiness": {"grok": {"ready": True}},
            "quota_snapshots": {"grok": {"status": "fresh"}},
            "rankings": [],
        }
        execution = {
            "execution_profile_id": "fixture-execution",
            "model": "fixture-model",
        }
        profiles = {
            "grok": {
                "display_name": "Synthetic Grok",
                "supported_modes": ["review"],
                "execution_profiles": {},
            }
        }
        captured = mock.Mock()
        captured.buffer = io.BytesIO()
        environment = self.safe_environment(
            root, OFFICE_PARENT_AGENT=PARENT_AGENT
        )
        with (
            mock.patch.dict(os.environ, environment, clear=True),
            mock.patch.object(fleet, "office_command_path", return_value=launcher),
            mock.patch.object(fleet, "load_profiles", return_value=profiles),
            mock.patch.object(
                fleet,
                "choose_agent",
                return_value=("grok", "review", route),
            ),
            mock.patch.object(
                fleet.routing_v2,
                "resolve_execution_profile",
                return_value=execution,
            ),
            mock.patch.object(fleet.routing_v2, "hard_gates", return_value=[]),
            mock.patch.object(
                fleet.routing_v2,
                "permits_explicit_canary",
                return_value=False,
            ),
            mock.patch.object(
                fleet, "execute_single", return_value=result
            ) as execute_single,
            mock.patch.object(fleet.sys, "stdout", captured),
        ):
            exit_code = fleet.main(
                [
                    "--state-dir",
                    str(root / "state"),
                    "run",
                    "--task",
                    "synthetic standalone run",
                    "--agent",
                    "grok",
                    "--kind",
                    "review",
                    "--mode",
                    "review",
                    "--workspace",
                    str(Path(result["workspace"])),
                    "--timeout",
                    "30",
                    "--knowledge-mode",
                    "none",
                ]
            )

        self.assertEqual(exit_code, fleet.EXIT_SUCCESS)
        execute_single.assert_called_once()
        output = json.loads(captured.buffer.getvalue().decode("ascii"))
        self.assertEqual(output["status"], "COMPLETED")
        self.assertEqual(output["watch_event"]["status"], "sent")
        self.assertEqual(len(self.send_calls(root)), 2)
        persisted = json.loads(
            Path(output["watch_event"]["result_ref"]).read_text(encoding="ascii")
        )
        self.assertEqual(persisted["watch_event"], output["watch_event"])


if __name__ == "__main__":
    unittest.main()
