"""A multi-line body has to travel through stdin, not through the command line.

Measured on this stack (2026-09-21): the same three-line text sent as
`--body "L1<newline>L2<newline>L3"` was stored as three lines by
`python office.py`, and as its first line only when the very same command went
through `cmd.exe /d /s /c` -- both via `python office.py` and via `office.cmd`.
The text is gone before any launcher starts, so no quoting trick and no edit of
`office.cmd` can bring it back, and the sender gets `status: ok` plus an id
either way: a whole day of coordination arrived as bare titles.

Standard input is not part of the command line, so `send` and `ask` can take
their body from there with `--body-stdin`. stdin is read only when that flag
asks for it: an invocation that never asks must not block on a terminal.
"""

from __future__ import annotations

import io
import json
import subprocess
import sys
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from typing import Any
from unittest import mock

from office_support import NO_WINDOW, OFFICE_PY, PACKAGE_ROOT, OfficeTestCase

from coordfs.office import MAX_BODY_BYTES, inbox_path
from coordfs.office_cli import build_parser, main

SECRET_BODY = "Authorization: Bearer " + "abcdefghijklmnopqrstuvwxyz0123456789"
THREE_LINES = "L1\nL2\nL3\n"


class _RefusingStdin:
    """A stdin that fails the test the moment anything reads it or even looks at it."""

    def __init__(self) -> None:
        self.reads = 0

    @property
    def buffer(self) -> "_RefusingStdin":
        self.reads += 1
        raise AssertionError("stdin must not be touched unless --body-stdin is given")

    def read(self, *_args: object, **_kwargs: object) -> str:
        self.reads += 1
        raise AssertionError("stdin must not be read unless --body-stdin is given")


class StdinBodyTests(OfficeTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.announce("claude-testhost-bob", "codex-testhost-alice")

    def run_stdin_cli(self, args: list[str], stdin_bytes: bytes = b"") -> tuple[int, dict[str, Any]]:
        """Run the CLI with stdin as a pipe carrying exactly these bytes."""
        completed = subprocess.run(
            [sys.executable, "-I", str(OFFICE_PY), *args],
            cwd=str(self.tmp.name),
            env=self.cli_env(),
            input=stdin_bytes,
            capture_output=True,
            check=False,
            creationflags=NO_WINDOW,
        )
        stdout = completed.stdout.decode("utf-8", "replace")
        self.assertTrue(stdout.strip(), completed.stderr)
        return completed.returncode, json.loads(stdout)

    @staticmethod
    def _stored(result: dict[str, Any]) -> list[str]:
        return [item["untrusted_message"]["body"] for item in result["messages"]]

    # -- (a) a three-line body survives stdin ---------------------------------

    def test_three_line_body_from_stdin_is_stored_in_full(self) -> None:
        code, sent = self.run_stdin_cli(
            ["--agent", "codex-testhost-alice", "send", "--to", "claude-testhost-bob", "--body-stdin"],
            THREE_LINES.encode("utf-8"),
        )
        self.assertEqual(code, 0, sent)
        self.assertEqual(sent["status"], "ok", sent)
        read = self.bob.read()
        self.assertEqual(read["count"], 1, read)
        stored = self._stored(read)[0]
        self.assertEqual(stored, THREE_LINES)
        self.assertEqual(len(stored.splitlines()), 3, stored)
        self.assertEqual(stored.encode("utf-8").count(b"\n"), 3)

    def test_three_line_body_from_stdin_on_a_channel(self) -> None:
        body = "header\n- first point\n- second point\n"
        code, sent = self.run_stdin_cli(
            ["--agent", "codex-testhost-alice", "send", "--channel", "tablica", "--body-stdin"],
            body.encode("utf-8"),
        )
        self.assertEqual(code, 0, sent)
        read = self.bob.read(channel="tablica")
        self.assertEqual(self._stored(read), [body])

    def test_ask_takes_its_body_from_stdin(self) -> None:
        body = "question over\nmore than one line\n"
        code, payload = self.run_stdin_cli(
            ["--agent", "codex-testhost-alice", "ask", "--to", "claude-testhost-bob", "--timeout", "0", "--body-stdin"],
            body.encode("utf-8"),
        )
        self.assertEqual(code, 0, payload)
        self.assertIn(payload["code"], {"queued", "timeout"}, payload)
        self.assertEqual(self._stored(self.bob.read()), [body])

    # -- (b) + (g) --body is unchanged and stdin stays unread ------------------

    def test_body_argument_is_unchanged_and_stdin_is_left_unread(self) -> None:
        # stdin carries a body that cannot be sent: if it were read, the send
        # would come back `too_large` (or hang on a terminal that never closes).
        ignored = b"z" * (MAX_BODY_BYTES + 100)
        code, sent = self.run_stdin_cli(
            ["--agent", "codex-testhost-alice", "send", "--to", "claude-testhost-bob", "--body", THREE_LINES], ignored
        )
        self.assertEqual(code, 0, sent)
        self.assertEqual(sent["status"], "ok", sent)
        self.assertEqual(self._stored(self.bob.read()), [THREE_LINES])

    def test_a_send_without_body_stdin_never_touches_stdin(self) -> None:
        refusing = _RefusingStdin()
        out = io.StringIO()
        with mock.patch.object(sys, "stdin", refusing), redirect_stdout(out):
            code = main(
                # `--home` is a global option: it belongs before the subcommand, not after it.
                ["--agent", "codex-testhost-alice", "--home", str(self.home),
                 "send", "--to", "claude-testhost-bob", "--body", "from the argument"]
            )
        payload = json.loads(out.getvalue())
        self.assertEqual(code, 0, payload)
        self.assertEqual(payload["code"], "sent", payload)
        self.assertEqual(refusing.reads, 0, "stdin was read although --body-stdin was not given")
        self.assertEqual(self._stored(self.bob.read()), ["from the argument"])

    def test_both_body_commands_expose_the_stdin_flag(self) -> None:
        for command in ("send", "ask"):
            with self.subTest(command=command):
                args = build_parser().parse_args([command, "--to", "claude-testhost-bob", "--body-stdin"])
                self.assertTrue(args.body_stdin)
                self.assertIsNone(args.body)

    # -- (c) both at once is a readable error ----------------------------------

    def test_body_and_body_stdin_together_is_a_readable_error(self) -> None:
        for argv in (
            ["send", "--to", "claude-testhost-bob", "--body", "argument text", "--body-stdin"],
            ["ask", "--to", "claude-testhost-bob", "--timeout", "0", "--body", "argument text", "--body-stdin"],
        ):
            with self.subTest(command=argv[0]):
                code, payload = self.run_stdin_cli(
                    ["--agent", "codex-testhost-alice", *argv], b"stdin text\n"
                )
                self.assertEqual(code, 1, payload)
                self.assertEqual(payload["status"], "error", payload)
                self.assertEqual(payload["code"], "invalid_body", payload)
                self.assertIn("mutually exclusive", payload["message"])
                self.assertIn("--body-stdin", payload["message"])
        self.assertFalse(inbox_path(self.home, "claude-testhost-bob").exists())

    def test_no_body_at_all_is_a_readable_error(self) -> None:
        code, payload = self.run_stdin_cli(["--agent", "codex-testhost-alice", "send", "--to", "claude-testhost-bob"])
        self.assertEqual(code, 1, payload)
        self.assertEqual(payload["status"], "error", payload)
        self.assertEqual(payload["code"], "invalid_body", payload)
        self.assertIn("--body-stdin", payload["message"])
        self.assertFalse(inbox_path(self.home, "claude-testhost-bob").exists())

    # -- (d) empty stdin is the empty-body error -------------------------------

    def test_empty_stdin_is_refused_exactly_like_an_empty_body_argument(self) -> None:
        reference = self.alice.send("claude-testhost-bob", "")
        self.assertEqual(reference["code"], "invalid_body")
        for stdin_bytes in (b"", b"\n", b"   \n"):
            with self.subTest(stdin=stdin_bytes):
                code, payload = self.run_stdin_cli(
                    ["--agent", "codex-testhost-alice", "send", "--to", "claude-testhost-bob", "--body-stdin"], stdin_bytes
                )
                self.assertEqual(code, 1, payload)
                self.assertEqual(payload["status"], reference["status"])
                self.assertEqual(payload["code"], reference["code"])
                self.assertEqual(payload["message"], reference["message"])
        self.assertFalse(inbox_path(self.home, "claude-testhost-bob").exists())
        self.assertEqual(self.bob.read()["count"], 0)

    # -- (e) the 16 KB cap is the same for both --------------------------------

    def test_stdin_over_16_kib_is_refused_exactly_like_a_body_argument(self) -> None:
        reference = self.alice.send("claude-testhost-bob", "a" * (MAX_BODY_BYTES + 1))
        self.assertEqual(reference["code"], "too_large")
        code, payload = self.run_stdin_cli(
            ["--agent", "codex-testhost-alice", "send", "--to", "claude-testhost-bob", "--body-stdin"],
            b"a" * (MAX_BODY_BYTES + 1),
        )
        self.assertEqual(code, 1, payload)
        self.assertEqual(payload["status"], reference["status"])
        self.assertEqual(payload["code"], reference["code"])
        self.assertEqual(payload["message"], reference["message"])
        self.assertFalse(inbox_path(self.home, "claude-testhost-bob").exists())

    def test_stdin_exactly_at_the_cap_is_delivered(self) -> None:
        code, sent = self.run_stdin_cli(
            ["--agent", "codex-testhost-alice", "send", "--to", "claude-testhost-bob", "--body-stdin"],
            b"a" * MAX_BODY_BYTES,
        )
        self.assertEqual(code, 0, sent)
        self.assertEqual(len(self._stored(self.bob.read())[0]), MAX_BODY_BYTES)

    # -- (f) the secret preflight is the same for both -------------------------

    def test_secret_shaped_stdin_is_refused_before_write(self) -> None:
        reference = self.alice.send("claude-testhost-bob", SECRET_BODY)
        self.assertEqual(reference["code"], "secret_detected")
        code, payload = self.run_stdin_cli(
            ["--agent", "codex-testhost-alice", "send", "--to", "claude-testhost-bob", "--body-stdin"],
            SECRET_BODY.encode("utf-8"),
        )
        self.assertEqual(code, 1, payload)
        self.assertEqual(payload["status"], reference["status"])
        self.assertEqual(payload["code"], reference["code"])
        self.assertEqual(payload["message"], reference["message"])
        self.assertFalse(inbox_path(self.home, "claude-testhost-bob").exists())
        self.assertEqual(self.bob.read()["count"], 0)

    # -- (h) UTF-8 outside ASCII ----------------------------------------------

    def test_utf8_outside_ascii_survives_stdin(self) -> None:
        body = "Za\u017c\u00f3\u0142\u0107 g\u0119\u015bl\u0105 ja\u017a\u0144\n\u03b7 \u03b3\u03bb\u03ce\u03c3\u03c3\u03b1\n\U0001f642 ok\n"
        code, sent = self.run_stdin_cli(
            ["--agent", "codex-testhost-alice", "send", "--to", "claude-testhost-bob", "--body-stdin"], body.encode("utf-8")
        )
        self.assertEqual(code, 0, sent)
        stored = self._stored(self.bob.read())[0]
        self.assertEqual(stored, body)
        self.assertEqual(len(stored.encode("utf-8")), len(body.encode("utf-8")))

    def test_stdin_that_is_not_utf8_is_a_readable_error(self) -> None:
        code, payload = self.run_stdin_cli(
            ["--agent", "codex-testhost-alice", "send", "--to", "claude-testhost-bob", "--body-stdin"], b"kodowanie \xff\xfe"
        )
        self.assertEqual(code, 1, payload)
        self.assertEqual(payload["status"], "error", payload)
        self.assertEqual(payload["code"], "invalid_body", payload)
        self.assertIn("UTF-8", payload["message"])
        self.assertFalse(inbox_path(self.home, "claude-testhost-bob").exists())


class SkillDocumentsTheTrapTests(unittest.TestCase):
    """The trap cost a day of coordination; the skill has to name it."""

    def test_skill_says_multi_line_bodies_go_through_stdin(self) -> None:
        text = (Path(PACKAGE_ROOT) / "skill" / "agent-office" / "SKILL.md").read_text(
            encoding="utf-8"
        )
        self.assertIn("--body-stdin", text)
        self.assertIn("cmd.exe", text)
        self.assertIn("newline", text)


if __name__ == "__main__":
    unittest.main()
