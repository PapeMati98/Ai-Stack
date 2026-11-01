from __future__ import annotations

import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from support import PACKAGE_ROOT

from coordfs.office import HANDLING_TEXT, Office
from coordfs.office_cli import main as office_main

TESTS_ROOT = PACKAGE_ROOT / "tests"
TASK_CHANNEL = "checkpoint-task"


class OfficeCheckpointTests(unittest.TestCase):
    def setUp(self) -> None:
        # Keep test-owned files inside this checkout so TemporaryDirectory cleanup
        # stays within the task workspace.
        self.temp = tempfile.TemporaryDirectory(prefix="office-checkpoint-", dir=TESTS_ROOT)
        self.root = Path(self.temp.name)
        self.home = self.root / "coord-home"
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        self.harness = "checkpoint-test"
        self.session_id = "native-session"

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _args(self, *, peek: bool = False, remote: bool = False, limit: int = 5) -> list[str]:
        args = ["--harness", self.harness, "--session-id", self.session_id]
        if remote:
            args.extend(["--remote-config", str(self.root / "remote-config.json")])
        else:
            args.extend(["--home", str(self.home)])
        args.extend([
            "checkpoint",
            "--channel", TASK_CHANNEL,
            "--workspace", str(self.workspace),
            "--limit", str(limit),
        ])
        if peek:
            args.append("--peek")
        return args

    def _invoke(self, args: list[str], *, env: dict[str, str] | None = None) -> tuple[int, dict]:
        output = io.StringIO()
        with mock.patch.dict(os.environ, env or {}, clear=False), redirect_stdout(output):
            code = office_main(args)
        self.assertTrue(output.getvalue().strip())
        return code, json.loads(output.getvalue())

    def _seed_messages(self) -> Office:
        target = Office(
            harness=self.harness,
            session_id=self.session_id,
            home=self.home,
        )
        self.assertEqual(
            target.presence(ttl_s=900, status="idle", workspace=str(self.workspace))["status"],
            "ok",
        )
        sender = Office(agent_id="sender-host-session", home=self.home)
        self.assertEqual(sender.send(target.agent_id, "private untrusted body")["status"], "ok")
        self.assertEqual(
            sender.send(None, "channel untrusted body", channel=TASK_CHANNEL)["status"],
            "ok",
        )
        return target

    def test_checkpoint_reads_both_streams_and_peek_preserves_cursors(self) -> None:
        target = self._seed_messages()

        code, first = self._invoke(self._args(peek=True))
        self.assertEqual(code, 0)
        self.assertEqual(first["status"], "ok")
        self.assertEqual(first["presence"]["meta"]["status"], "busy")
        self.assertEqual(first["channel_name"], TASK_CHANNEL)
        self.assertEqual(first["inbox"]["messages"][0]["untrusted_message"]["body"], "private untrusted body")
        self.assertEqual(first["channel"]["messages"][0]["untrusted_message"]["body"], "channel untrusted body")
        self.assertEqual(first["inbox"]["handling"], HANDLING_TEXT)
        self.assertEqual(first["channel"]["handling"], HANDLING_TEXT)
        self.assertFalse(first["inbox"]["cursor_advanced"])
        self.assertFalse(first["channel"]["cursor_advanced"])

        code, consumed = self._invoke(self._args())
        self.assertEqual(code, 0)
        self.assertTrue(consumed["inbox"]["cursor_advanced"])
        self.assertTrue(consumed["channel"]["cursor_advanced"])
        self.assertEqual(target.read(peek=True)["messages"], [])
        self.assertEqual(target.read(channel=TASK_CHANNEL, peek=True)["messages"], [])

    def test_invalid_flags_are_rejected_before_any_presence_or_read(self) -> None:
        invalid_cases = (
            (["--ttl", "0"], "invalid_ttl"),
            (["--limit", "0"], "invalid_limit"),
            (["--channel", "nul"], "invalid_channel"),
            (["--workspace", ""], "invalid_workspace"),
            (["--status", "x" * 201], "invalid_status"),
        )
        for flags, expected_code in invalid_cases:
            with self.subTest(flag=flags[0]):
                with mock.patch.object(Office, "presence") as presence, mock.patch.object(Office, "read") as read:
                    code, payload = self._invoke(self._args() + flags)

                self.assertEqual(code, 1)
                self.assertEqual(payload["code"], expected_code)
                presence.assert_not_called()
                read.assert_not_called()
                self.assertFalse(self.home.exists())

    def test_checkpoint_refuses_process_fallback_identity_before_writing(self) -> None:
        no_session = {
            "COORDFS_SESSION_ID": "",
            "AGENT_SESSION_ID": "",
            "CLAUDE_CODE_SESSION_ID": "",
            "CODEX_SESSION_ID": "",
            "CODEX_THREAD_ID": "",
        }
        args = ["--home", str(self.home), "--harness", self.harness,
                "checkpoint", "--channel", TASK_CHANNEL, "--workspace", str(self.workspace)]
        code, payload = self._invoke(args, env=no_session)

        self.assertEqual(code, 1)
        self.assertEqual(payload["code"], "native_identity_required")
        self.assertFalse(self.home.exists())

    def test_partial_read_failure_keeps_each_result_and_returns_nonzero(self) -> None:
        self._seed_messages()
        inbox_result = {"status": "ok", "code": "read", "handling": HANDLING_TEXT, "messages": []}
        channel_result = {"status": "error", "code": "fixture_read_failure", "message": "channel unavailable"}
        with mock.patch.object(Office, "read", side_effect=[inbox_result, channel_result]) as read:
            code, payload = self._invoke(self._args())

        self.assertEqual(code, 1)
        self.assertEqual(payload["status"], "partial")
        self.assertEqual(payload["failed_steps"], ["channel"])
        self.assertEqual(payload["inbox"], inbox_result)
        self.assertEqual(payload["channel"], channel_result)
        self.assertEqual(read.call_count, 2)
        self.assertIsNone(read.call_args_list[0].kwargs["channel"])
        self.assertEqual(read.call_args_list[1].kwargs["channel"], TASK_CHANNEL)

    def test_remote_checkpoint_uses_existing_routes_without_local_fallback(self) -> None:
        calls: list[tuple[str, str | None]] = []

        def remote_dispatch(args):
            calls.append((args.command, getattr(args, "channel", None)))
            if args.command == "read" and args.channel == TASK_CHANNEL:
                return {"status": "degraded", "code": "remote_read_failed", "reason": "timeout"}
            return {"status": "ok", "code": args.command, "handling": HANDLING_TEXT}

        with (
            mock.patch("coordfs.office_cli_remote.load_remote_config", return_value=object()) as load_config,
            mock.patch("coordfs.office_cli_remote.dispatch_remote", side_effect=remote_dispatch),
            mock.patch.object(Office, "presence", side_effect=AssertionError("local presence fallback")),
            mock.patch.object(Office, "read", side_effect=AssertionError("local read fallback")),
        ):
            code, payload = self._invoke(self._args(remote=True))

        self.assertEqual(code, 1)
        self.assertEqual(payload["status"], "partial")
        self.assertEqual(payload["failed_steps"], ["channel"])
        self.assertEqual(calls, [("presence", None), ("read", None), ("read", TASK_CHANNEL), ("checkpoint", None)])
        self.assertIn("project_banner", payload)
        load_config.assert_called_once()


if __name__ == "__main__":
    unittest.main()
