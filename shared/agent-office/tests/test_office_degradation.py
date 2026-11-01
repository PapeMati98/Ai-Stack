"""Criterion 7: a missing, corrupted, or locked office home never stops the agent (fail-open)."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
from pathlib import Path
from unittest import mock

from office_support import NO_WINDOW, OFFICE_PY, OfficeTestCase

from coordfs.office import Office, cursor_path, diagnostics_path, inbox_mutex_path, inbox_path, office_root

OPS = ("send", "read", "wait", "presence", "who", "leave")


def _run_all(office: Office) -> dict[str, dict]:
    return {
        "send": office.send(OfficeTestCase.SOMEONE_ID, "hello"),
        "read": office.read(),
        "wait": office.wait(timeout_s=0.2),
        "presence": office.presence(ttl_s=5),
        "who": office.who(),
        "leave": office.leave(),
    }


class DegradationTests(OfficeTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.announce("claude-testhost-bob")

    def test_home_blocked_by_a_file_degrades_every_operation_without_raising(self) -> None:
        blocker = Path(self.tmp.name) / "blocked-home"
        blocker.write_text("not a directory", encoding="utf-8")
        office = Office(agent_id=self.LONELY_ID, home=blocker, mutex_timeout_s=0.2)
        results = _run_all(office)
        for op in OPS:
            with self.subTest(op=op):
                self.assertEqual(results[op]["status"], "degraded", results[op])
                self.assertEqual(results[op]["code"], "office_unavailable")
                self.assertEqual(results[op]["op"], op)
        self.assertTrue(blocker.is_file())

    def test_blocked_home_degrades_send_without_resolving_a_recipient(self) -> None:
        """Recipient resolution is best effort: a blocked home degrades `send`, never raises.

        `_send` reads the coordination board to vouch for a direct address. With the state
        directory blocked that read fails, and the failure has to leave through `_guard` as
        `degraded` -- not as an exception and not as an `unknown_recipient` refusal, which
        would blame the recipient for an office that cannot be read.
        """
        blocker = Path(self.tmp.name) / "blocked-send-home"
        blocker.write_text("not a directory", encoding="utf-8")
        office = Office(agent_id=self.LONELY_ID, home=blocker, mutex_timeout_s=0.2)
        result = office.send(self.SOMEONE_ID, "hello")
        self.assertEqual(result["status"], "degraded", result)
        self.assertEqual(result["code"], "office_unavailable")
        self.assertEqual(result["op"], "send")
        self.assertNotEqual(result["code"], "unknown_recipient")
        self.assertTrue(blocker.is_file(), "the blocker must not be replaced by a directory")

    def test_cli_exits_zero_when_home_is_unavailable(self) -> None:
        blocker = Path(self.tmp.name) / "blocked-cli-home"
        blocker.write_text("x", encoding="utf-8")
        for args in (
            ["--agent", self.ALICE_ID, "send", "--to", self.BOB_ID, "--body", "hi"],
            ["--agent", self.ALICE_ID, "read"],
            ["--agent", self.ALICE_ID, "wait", "--timeout", "0.1"],
            ["--agent", self.ALICE_ID, "presence"],
            ["--agent", self.ALICE_ID, "who"],
        ):
            with self.subTest(args=args):
                code, payload = self.run_cli(args, COORDFS_HOME=str(blocker))
                self.assertEqual(code, 0, payload)
                self.assertEqual(payload["status"], "degraded")

    def test_office_directory_removed_mid_session_recovers(self) -> None:
        self.alice.send("claude-testhost-bob", "before")
        self.assertEqual(self.bob.read()["count"], 1)
        shutil.rmtree(office_root(self.home))
        self.assertFalse(office_root(self.home).exists())
        self.assertEqual(self.bob.read()["count"], 0)
        self.assertEqual(self.bob.wait(timeout_s=0.1)["code"], "timeout")
        self.assertEqual(self.alice.send("claude-testhost-bob", "after")["status"], "ok")
        self.assertEqual(self.bob.read()["messages"][0]["untrusted_message"]["body"], "after")
        shutil.rmtree(self.home)
        self.assertEqual(self.alice.presence(ttl_s=5)["status"], "ok")
        self.assertEqual(self.alice.who()["count"], 1)

    def test_foreign_inbox_mutex_degrades_send_and_clears_later(self) -> None:
        self.alice.send("claude-testhost-bob", "warm up")
        mutex = inbox_mutex_path(self.home, "claude-testhost-bob")
        mutex.write_bytes(b"held-by-another-process")
        alice = self.office("codex-testhost-alice", mutex_timeout_s=0.2)
        result = alice.send("claude-testhost-bob", "blocked")
        self.assertEqual(result["status"], "degraded", result)
        self.assertIn("busy", result["error"])
        mutex.unlink()
        self.assertEqual(alice.send("claude-testhost-bob", "unblocked")["status"], "ok")
        bodies = [item["untrusted_message"]["body"] for item in self.bob.read()["messages"]]
        self.assertEqual(bodies, ["warm up", "unblocked"])

    def test_corrupted_inbox_and_cursor_do_not_raise(self) -> None:
        self.alice.send("claude-testhost-bob", "good one")
        self.bob.read()
        cursor = cursor_path(self.home, "claude-testhost-bob", "inbox")
        cursor.write_bytes(b"\x00\x01 not json")
        self.alice.send("claude-testhost-bob", "second")
        result = self.bob.read()
        self.assertEqual(result["status"], "ok")
        self.assertEqual(
            [item["untrusted_message"]["body"] for item in result["messages"]],
            ["good one", "second"],
            "corrupted cursor resets to the start of the inbox rather than failing",
        )
        inbox = inbox_path(self.home, "claude-testhost-bob")
        inbox.write_bytes(b"\xff\xfe\x00garbage without newline")
        self.assertEqual(self.bob.read()["status"], "ok")
        self.assertEqual(self.bob.wait(timeout_s=0.1)["code"], "timeout")
        self.assertEqual(self.alice.send("claude-testhost-bob", "still works")["status"], "ok")
        self.assertEqual(self.bob.read()["count"], 1)

    def test_guard_turns_lookup_and_attribute_errors_into_degraded(self) -> None:
        for exc in (KeyError("meta"), AttributeError("NoneType has no attribute get"), IndexError("0")):
            with self.subTest(exc=type(exc).__name__):
                with mock.patch.object(self.alice, "_board_or_raise", side_effect=exc):
                    who = self.alice.who()
                    presence = self.alice.presence(ttl_s=5)
                with mock.patch.object(self.alice, "_read_cursor", side_effect=exc):
                    read = self.alice.read()
                for payload, op in ((who, "who"), (presence, "presence"), (read, "read")):
                    self.assertEqual(payload["status"], "degraded", payload)
                    self.assertEqual(payload["op"], op)
                    self.assertIn(type(exc).__name__, payload["error"])

    def test_readonly_home_degrades_instead_of_raising(self) -> None:
        self.alice.send("claude-testhost-bob", "seed")
        inbox_dir = inbox_path(self.home, "claude-testhost-bob").parent
        (inbox_dir / "claude-testhost-bob.jsonl.lock").mkdir()
        alice = self.office("codex-testhost-alice", mutex_timeout_s=0.2)
        try:
            result = alice.send("claude-testhost-bob", "mutex path is a directory")
            self.assertEqual(result["status"], "degraded", result)
        finally:
            (inbox_dir / "claude-testhost-bob.jsonl.lock").rmdir()
        self.assertEqual(alice.send("claude-testhost-bob", "recovered")["status"], "ok")


class DegradedDiagnosticsTests(OfficeTestCase):
    """Fail-open must not be silent: every degraded result leaves a trace and a diagnostic record."""

    def test_programming_error_leaves_trace_in_payload_and_diagnostics_file(self) -> None:
        with mock.patch.object(self.alice, "_board_or_raise", side_effect=KeyError("meta")):
            who = self.alice.who()
        self.assertEqual(who["status"], "degraded", who)
        self.assertIn("KeyError", who["error"])
        self.assertTrue(who["trace"], "the payload must carry a frame tail")
        for frame in who["trace"]:
            self.assertRegex(frame, re.compile(r"^.+:\d+ .+$"))
        path = Path(who["diagnostic"])
        self.assertEqual(path, diagnostics_path(self.home))
        records = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
        last = records[-1]
        self.assertEqual(last["op"], "who")
        self.assertEqual(last["agent"], "codex-testhost-alice")
        self.assertIn("KeyError", last["error"])
        self.assertIn("_who", last["traceback"], "the full traceback must name our own frame")
        self.assertEqual(last["trace"], who["trace"])
        # A second failure appends; the first record is kept.
        with mock.patch.object(self.alice, "_read_cursor", side_effect=AttributeError("boom")):
            read = self.alice.read()
        self.assertEqual(read["status"], "degraded")
        records = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
        self.assertEqual([record["op"] for record in records], ["who", "read"])
        # The office itself still works afterwards.
        self.assertEqual(self.alice.who()["status"], "ok")

    def test_unavailable_home_falls_back_to_stderr_with_exit_zero(self) -> None:
        blocker = Path(self.tmp.name) / "blocked-diag-home"
        blocker.write_text("x", encoding="utf-8")
        completed = subprocess.run(
            [sys.executable, "-I", str(OFFICE_PY), "--agent", self.ALICE_ID, "who"],
            env=self.cli_env(COORDFS_HOME=str(blocker)),
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
            creationflags=NO_WINDOW,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        payload = json.loads(completed.stdout)
        self.assertEqual(payload["status"], "degraded")
        self.assertEqual(payload["diagnostic"], "stderr")
        self.assertTrue(payload["trace"])
        self.assertIn("office diagnostic:", completed.stderr)
        self.assertIn(payload["error"].split(":", 1)[0], completed.stderr)
        self.assertTrue(blocker.is_file(), "the blocker must not be replaced by a directory")

    def test_diagnostics_file_is_bounded_by_rotation(self) -> None:
        with mock.patch("coordfs.office.DIAGNOSTICS_MAX_BYTES", 2048):
            for _ in range(12):
                with mock.patch.object(self.alice, "_board_or_raise", side_effect=KeyError("meta")):
                    self.assertEqual(self.alice.who()["status"], "degraded")
        path = diagnostics_path(self.home)
        self.assertTrue(path.with_name(path.name + ".1").is_file(), "old records rotate away")
        self.assertFalse(path.with_name(path.name + ".2").exists(), "only one rotated generation is kept")
        self.assertLess(path.stat().st_size, 2048 + 4096 + 1024, "current generation stays bounded")
