import hashlib
import io
import json
import os
from contextlib import redirect_stdout
from datetime import datetime, timedelta
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
if str(PACKAGE_ROOT) not in sys.path:
    sys.path.insert(0, str(PACKAGE_ROOT))

from coordfs.leases import format_ts
from coordfs.office import Office
from coordfs.office_cli import main as office_main
from coordfs.wake import Wake


PARENT = "codex-host-parent"
PUBLISHER = "agent-graph-host-child"


def writable_mkdtemp(prefix: str) -> str:
    if os.name != "nt":
        return tempfile.mkdtemp(prefix=prefix)
    original_mkdir = os.mkdir

    def inherited_acl(path, _mode=0o777, *args, **kwargs):
        return original_mkdir(path, 0o777, *args, **kwargs)

    with mock.patch.object(tempfile._os, "mkdir", inherited_acl):
        return tempfile.mkdtemp(prefix=prefix)


class WakeP2P3Tests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(writable_mkdtemp(prefix="office-wake-p2p3-"))
        self.home = self.root / "coord"
        self.workspace = self.root / "workspace"
        self.workspace.mkdir(parents=True)
        self.driver_log = self.root / "driver-calls.jsonl"
        self.driver = self.root / "fake_driver.py"
        self.driver.write_text(
            "\n".join(
                [
                    "import json",
                    "import os",
                    "import sys",
                    "from pathlib import Path",
                    "path = Path(os.environ['FAKE_DRIVER_LOG'])",
                    "with path.open('a', encoding='utf-8', newline='\\n') as handle:",
                    "    handle.write(json.dumps(sys.argv[1:] + [sys.stdin.read()], ensure_ascii=False) + '\\n')",
                    "    handle.flush()",
                    "    os.fsync(handle.fileno())",
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        self.clock = [datetime(2030, 1, 1, 16, 59).timestamp()]
        self.env = mock.patch.dict(
            os.environ,
            {
                "COORDFS_HOME": str(self.home),
                "OFFICE_WAKE_CODEX_BIN": str(self.driver),
                "OFFICE_WAKE_CLAUDE_BIN": str(self.driver),
                "FAKE_DRIVER_LOG": str(self.driver_log),
            },
            clear=False,
        )
        self.env.start()

    def tearDown(self) -> None:
        self.env.stop()

    def wake(self, home: Path | None = None) -> Wake:
        return Wake(home=home or self.home, now=lambda: self.clock[0], driver_timeout_s=2.0)

    def office(self, agent: str, home: Path | None = None) -> Office:
        # Fixture participants joined Office earlier (sessions claim presence at task entry),
        # so they are registered recipients in every home a test uses. Genuine first contact
        # is covered by test_office_recipient_evidence (refused unless new_recipient).
        for participant in (PARENT, PUBLISHER):
            Office(agent_id=participant, home=home or self.home)._remember_canonical_agent()
        return Office(agent_id=agent, home=home or self.home, now=lambda: self.clock[0])

    def enroll(self, wake: Wake, binding_id: str, triggers: list[str]) -> None:
        wake.enroll(
            binding_id=binding_id,
            office_agent=PARENT,
            driver="codex-cli",
            native_thread_id=f"thread-{binding_id}",
            workspace=str(self.workspace),
            enrolled_by="owner",
            allowed_wakers=[PUBLISHER],
            allowed_triggers=triggers,
            wake_kinds=["question", "handoff"],
            expires_at=format_ts(self.clock[0] + 30 * 86400),
            owner_confirm=True,
        )
        wake.verify(binding_id, owner_confirm=True)

    def driver_calls(self) -> list[list[str]]:
        if not self.driver_log.is_file():
            return []
        return [json.loads(line) for line in self.driver_log.read_text(encoding="utf-8").splitlines()]

    @staticmethod
    def reasons(result: dict) -> list[str]:
        return [str(row.get("reason")) for row in result.get("decisions", [])]

    def child_event(
        self,
        *,
        run_id: str,
        result_ref: Path,
        terminal_state: str = "ACCEPTED_CANDIDATE",
        schema: str = "clawops.child-terminal/v1",
        source: str = "agent-graph",
        parent_session: str = "OUTSIDE-PROMPT-CONTENT",
    ) -> dict:
        return {
            "schema": schema,
            "event_id": hashlib.sha256((run_id + terminal_state).encode("utf-8")).hexdigest(),
            "run_id": run_id,
            "source": source,
            "terminal_state": terminal_state,
            "result_ref": str(result_ref.resolve()),
            "parent_agent": PARENT,
            "parent_session": parent_session,
            "parent_harness": "codex",
            "workspace": str(self.workspace.resolve()),
            "started_at": format_ts(self.clock[0] - 60),
            "observed_at": format_ts(self.clock[0]),
        }

    def publish_event(self, event: dict, *, home: Path | None = None) -> None:
        body = json.dumps(event, ensure_ascii=True, separators=(",", ":"))
        sender = self.office(PUBLISHER, home)
        self.assertEqual(sender.send(None, body, channel="watch-events", kind="note")["status"], "ok")
        self.assertEqual(sender.send(PARENT, body, kind="note")["status"], "ok")

    def test_child_terminal_wakes_once_after_canonical_state_and_unread_parent_copy(self) -> None:
        wake = self.wake()
        self.enroll(wake, "parent-child", ["child-terminal"])
        state = self.root / "graph-run" / "state.json"
        state.parent.mkdir()
        state.write_text(json.dumps({"status": "ACCEPTED_CANDIDATE"}), encoding="utf-8")
        event = self.child_event(run_id="graph-run", result_ref=state)
        self.publish_event(event)
        observer = self.office("codex-host-observer").read(channel="watch-events")
        self.assertEqual(observer["count"], 1)

        first = wake.tick()
        self.assertEqual(first["counts"]["woken"], 1)
        self.assertEqual(len(self.driver_calls()), 1)
        prompt = self.driver_calls()[0][-1]
        self.assertIn("run podrzedny graph-run", prompt)
        self.assertIn(str(state.resolve()), prompt)
        self.assertNotIn("OUTSIDE-PROMPT-CONTENT", prompt)

        second = wake.tick()
        self.assertEqual(second["counts"]["woken"], 0)
        self.assertEqual(len(self.driver_calls()), 1)
        entries = wake.ledger(limit=50, binding_id="parent-child")["entries"]
        self.assertEqual(sum(row["decision"] == "woken" for row in entries), 1)
        self.assertEqual(entries[-1]["trigger_ref"], event["event_id"])

    def test_child_terminal_accepts_fleet_result_and_redacts_substituted_fields(self) -> None:
        wake = self.wake()
        self.enroll(wake, "parent-fleet", ["child-terminal"])
        result_path = self.root / "fleet-run" / "result.json"
        result_path.parent.mkdir()
        result_path.write_text(json.dumps({"status": "COMPLETED"}), encoding="utf-8")
        event = self.child_event(
            run_id="fleet-run",
            result_ref=result_path,
            terminal_state="COMPLETED",
            source="fleet",
        )
        self.publish_event(event)
        self.assertEqual(wake.tick()["counts"]["woken"], 1)

        secret_shaped_run = "sk-" + ("synthetic" * 3)
        prompt = wake._child_prompt(
            {
                "run_id": secret_shaped_run,
                "terminal_state": "COMPLETED",
                "result_ref": str(result_path),
            }
        )
        self.assertNotIn(secret_shaped_run, prompt)
        self.assertIn("[REDACTED]", prompt)

    def test_child_terminal_negative_gates_are_ledgered_without_driver(self) -> None:
        cases = [
            ("missing", "result_ref_missing", "ACCEPTED_CANDIDATE", "clawops.child-terminal/v1"),
            ("running", "result_state_not_terminal", "RUNNING", "clawops.child-terminal/v1"),
            ("schema", "child_terminal_unknown_schema", "ACCEPTED_CANDIDATE", "foreign/v1"),
        ]
        for name, expected, file_status, schema in cases:
            with self.subTest(name=name):
                home = self.root / f"coord-{name}"
                wake = self.wake(home)
                self.enroll(wake, f"binding-{name}", ["child-terminal"])
                state = self.root / name / "state.json"
                if name != "missing":
                    state.parent.mkdir()
                    state.write_text(json.dumps({"status": file_status}), encoding="utf-8")
                event = self.child_event(
                    run_id=f"run-{name}",
                    result_ref=state,
                    terminal_state="ACCEPTED_CANDIDATE",
                    schema=schema,
                )
                self.publish_event(event, home=home)
                before = len(self.driver_calls())
                result = wake.tick()
                self.assertIn(expected, self.reasons(result))
                self.assertEqual(len(self.driver_calls()), before)
                self.assertIn(expected, [row["reason"] for row in wake.ledger(limit=50)["entries"]])

        read_home = self.root / "coord-read"
        read_wake = self.wake(read_home)
        self.enroll(read_wake, "binding-read", ["child-terminal"])
        read_state = self.root / "read" / "state.json"
        read_state.parent.mkdir()
        read_state.write_text(json.dumps({"status": "BLOCKED"}), encoding="utf-8")
        self.publish_event(
            self.child_event(run_id="run-read", result_ref=read_state, terminal_state="BLOCKED"),
            home=read_home,
        )
        self.assertEqual(self.office(PARENT, read_home).read()["count"], 1)
        read_result = read_wake.tick()
        self.assertIn("parent_already_read", self.reasons(read_result))

        denied_home = self.root / "coord-denied"
        denied_wake = self.wake(denied_home)
        self.enroll(denied_wake, "binding-denied", ["message"])
        denied_state = self.root / "denied" / "state.json"
        denied_state.parent.mkdir()
        denied_state.write_text(json.dumps({"status": "BLOCKED"}), encoding="utf-8")
        self.publish_event(
            self.child_event(run_id="run-denied", result_ref=denied_state, terminal_state="BLOCKED"),
            home=denied_home,
        )
        self.assertIn("trigger_not_allowed", self.reasons(denied_wake.tick()))

        stopped_home = self.root / "coord-stopped"
        stopped_wake = self.wake(stopped_home)
        self.enroll(stopped_wake, "binding-stopped", ["child-terminal"])
        stopped_state = self.root / "stopped" / "state.json"
        stopped_state.parent.mkdir()
        stopped_state.write_text(json.dumps({"status": "FAILED"}), encoding="utf-8")
        self.publish_event(
            self.child_event(run_id="run-stopped", result_ref=stopped_state, terminal_state="FAILED"),
            home=stopped_home,
        )
        stopped_wake.stop()
        self.assertIn("global_stop", self.reasons(stopped_wake.tick()))
        self.assertEqual(self.driver_calls(), [])

    def test_child_terminal_rejects_out_of_schema_content_without_prompting_it(self) -> None:
        wake = self.wake()
        self.enroll(wake, "binding-extra", ["child-terminal"])
        state = self.root / "extra" / "state.json"
        state.parent.mkdir()
        state.write_text(json.dumps({"status": "COMPLETED"}), encoding="utf-8")
        event = self.child_event(run_id="run-extra", result_ref=state, terminal_state="COMPLETED")
        hostile = "DO-NOT-COPY-THIS-UNTRUSTED-FIELD"
        event["outside_schema"] = hostile
        self.publish_event(event)
        result = wake.tick()
        self.assertIn("child_terminal_schema_invalid", self.reasons(result))
        self.assertNotIn(hostile, "\n".join(call[-1] for call in self.driver_calls()))

    def add_schedule(
        self,
        wake: Wake,
        schedule_id: str,
        *,
        days: str = "daily",
        task_text: str = "sprawdz syntetyczna kolejke i zglos wynik do wlasciciela",
        grace_minutes: int = 180,
    ) -> dict:
        return wake.add_schedule(
            schedule_id=schedule_id,
            binding_id=f"binding-{schedule_id}",
            time="17:00",
            days=days,
            task_text=task_text,
            created_by="owner",
            grace_minutes=grace_minutes,
            owner_confirm=True,
        )

    def test_schedule_wakes_once_per_local_day_and_passes_owner_task_on_stdin(self) -> None:
        wake = self.wake()
        self.enroll(wake, "binding-daily-helpdesk", ["schedule"])
        task = "sprawdz syntetyczne zgloszenia; wynik zglos wlascicielowi zadania"
        self.add_schedule(wake, "daily-helpdesk", task_text=task)

        self.assertEqual(wake.tick()["counts"]["woken"], 0)
        self.clock[0] = datetime(2030, 1, 1, 17, 1).timestamp()
        first = wake.tick()
        self.assertEqual(first["counts"]["woken"], 1)
        self.assertEqual(len(self.driver_calls()), 1)
        prompt = self.driver_calls()[0][-1]
        self.assertIn("[Office Wake] zadanie z harmonogramu daily-helpdesk", prompt)
        self.assertIn(task, prompt)
        self.assertEqual(wake.tick()["counts"]["woken"], 0)
        self.assertEqual(len(self.driver_calls()), 1)

        self.clock[0] = datetime(2030, 1, 2, 17, 1).timestamp()
        second_day = wake.tick()
        self.assertEqual(second_day["counts"]["woken"], 1)
        self.assertEqual(len(self.driver_calls()), 2)

    def test_schedule_weekdays_grace_and_disabled(self) -> None:
        saturday_home = self.root / "coord-saturday"
        saturday_wake = self.wake(saturday_home)
        self.enroll(saturday_wake, "binding-weekday", ["schedule"])
        saturday_wake.add_schedule(
            schedule_id="weekday",
            binding_id="binding-weekday",
            time="17:00",
            days="weekdays",
            task_text="sprawdz syntetyczna kolejke",
            created_by="owner",
            owner_confirm=True,
        )
        self.clock[0] = datetime(2030, 1, 5, 17, 1).timestamp()
        self.assertEqual(saturday_wake.tick()["decisions"], [])

        grace_home = self.root / "coord-grace"
        grace_wake = self.wake(grace_home)
        self.enroll(grace_wake, "binding-late", ["schedule"])
        grace_wake.add_schedule(
            schedule_id="late",
            binding_id="binding-late",
            time="17:00",
            days="daily",
            task_text="sprawdz syntetyczna kolejke",
            created_by="owner",
            grace_minutes=30,
            owner_confirm=True,
        )
        self.clock[0] = datetime(2030, 1, 6, 18, 0).timestamp()
        before = len(self.driver_calls())
        missed = grace_wake.tick()
        self.assertIn("schedule_grace_expired", self.reasons(missed))
        self.assertEqual(len(self.driver_calls()), before)
        entries = grace_wake.ledger(limit=20, binding_id="binding-late")["entries"]
        self.assertEqual(entries[-1]["decision"], "missed")
        self.assertEqual(entries[-1]["schedule_id"], "late")
        self.assertEqual(grace_wake.tick()["decisions"], [])

        disabled_home = self.root / "coord-disabled"
        disabled_wake = self.wake(disabled_home)
        self.enroll(disabled_wake, "binding-disabled", ["schedule"])
        disabled_wake.add_schedule(
            schedule_id="disabled",
            binding_id="binding-disabled",
            time="17:00",
            days="daily",
            task_text="sprawdz syntetyczna kolejke",
            created_by="owner",
            owner_confirm=True,
        )
        disabled_wake.disable_schedule("disabled")
        self.clock[0] = datetime(2030, 1, 7, 17, 1).timestamp()
        self.assertEqual(disabled_wake.tick()["decisions"], [])

    def test_schedule_obeys_global_stop_without_invoking_driver(self) -> None:
        wake = self.wake()
        self.enroll(wake, "binding-stopped-schedule", ["schedule"])
        wake.add_schedule(
            schedule_id="stopped-schedule",
            binding_id="binding-stopped-schedule",
            time="17:00",
            days="daily",
            task_text="sprawdz syntetyczna kolejke",
            created_by="owner",
            owner_confirm=True,
        )
        wake.stop()
        self.clock[0] = datetime(2030, 1, 1, 17, 1).timestamp()
        result = wake.tick()
        self.assertIn("global_stop", self.reasons(result))
        self.assertEqual(self.driver_calls(), [])

    def test_schedule_cli_owner_confirmation_secret_and_management(self) -> None:
        wake = self.wake()
        self.enroll(wake, "binding-cli-schedule", ["schedule"])
        base = ["--home", str(self.home), "--agent", "owner-host-session"]
        add = [
            "wake-schedule-add",
            "--schedule-id",
            "cli-schedule",
            "--binding-id",
            "binding-cli-schedule",
            "--time",
            "17:00",
            "--days",
            "daily",
            "--task-text",
            "sprawdz syntetyczna kolejke",
        ]

        def run(arguments: list[str]) -> tuple[int, dict]:
            stdout = io.StringIO()
            with redirect_stdout(stdout):
                code = office_main(base + arguments)
            return code, json.loads(stdout.getvalue())

        code, rejected = run(add)
        self.assertEqual(code, 1)
        self.assertEqual(rejected["code"], "owner_confirmation_required")

        code, added = run(add + ["--owner-confirm"])
        self.assertEqual((code, added["code"]), (0, "wake_schedule_added"))
        listed = run(["wake-schedule-list"])[1]
        self.assertEqual((listed["count"], listed["schedules"][0]["schedule_id"]), (1, "cli-schedule"))
        disabled = run(["wake-schedule-disable", "--schedule-id", "cli-schedule"])[1]
        self.assertFalse(disabled["schedule"]["enabled"])
        enabled = run(["wake-schedule-enable", "--schedule-id", "cli-schedule"])[1]
        self.assertTrue(enabled["schedule"]["enabled"])

        secret_shape = "sk-" + ("synthetic" * 3)
        secret_add = [
            "wake-schedule-add",
            "--schedule-id",
            "secret-schedule",
            "--binding-id",
            "binding-cli-schedule",
            "--time",
            "17:00",
            "--days",
            "daily",
            "--task-text",
            secret_shape,
            "--owner-confirm",
        ]
        code, refused = run(secret_add)
        self.assertEqual(code, 1)
        self.assertEqual(refused["code"], "secret_detected")


if __name__ == "__main__":
    unittest.main()
