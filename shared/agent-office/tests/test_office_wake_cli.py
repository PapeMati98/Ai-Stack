"""Cross-process Office CLI wake proof on a temporary home.

Starts a waiter subprocess on a channel, sends one message through the Office
CLI, asserts the waiter wakes in under two seconds, then proves a second wait
sees no duplicate pending message. Transport-only: no polling loops and no
duplicate Office implementation; the CLI subprocesses are the system under
test. Requires local named-pipe wake support (Windows suite).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

OFFICE_PY = Path(__file__).resolve().parents[1] / "office.py"


def run_office(home: Path, agent: str, *args: str) -> dict:
    env = os.environ.copy()
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    completed = subprocess.run(
        [sys.executable, "-I", str(OFFICE_PY), "--home", str(home), "--agent", agent, *args],
        check=True,
        capture_output=True,
        text=True,
        env=env,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    return json.loads(completed.stdout)


@unittest.skipUnless(os.name == "nt", "local named-pipe wake requires Windows")
class OfficeWakeCliTests(unittest.TestCase):
    def test_channel_wake_under_two_seconds_and_no_duplicate(self) -> None:
        with tempfile.TemporaryDirectory(prefix="office-wake-cli-") as raw_home:
            home = Path(raw_home)
            channel = "wake-cli-proof"
            body = "cli-wake-proof"
            waiter = subprocess.Popen(
                [
                    sys.executable,
                    "-I",
                    str(OFFICE_PY),
                    "--home",
                    str(home),
                    "--agent",
                    "codex-testhost-waiter",
                    "wait",
                    "--channel",
                    channel,
                    "--timeout",
                    "10",
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
            try:
                time.sleep(0.25)
                sent = run_office(home, "codex-testhost-sender", "send", "--channel", channel, "--body", body)
                sent_at = time.monotonic()
                stdout, stderr = waiter.communicate(timeout=5)
                elapsed = time.monotonic() - sent_at
            finally:
                if waiter.poll() is None:
                    waiter.kill()
                    waiter.communicate()
            if waiter.returncode != 0:
                raise AssertionError(f"waiter failed: rc={waiter.returncode} stderr={stderr!r}")
            waited = json.loads(stdout)
            self.assertEqual(sent["status"], "ok")
            self.assertEqual(sent["code"], "sent")
            self.assertEqual(sent["waiters_woken"], 1)
            self.assertEqual(waited["status"], "ok")
            self.assertEqual(waited["code"], "message")
            self.assertTrue(waited["new"])
            self.assertLess(elapsed, 2.0)

            # Consume the pending message, then prove a second wait sees no
            # duplicate: it must time out with nothing new.
            read = run_office(home, "waiter", "read", "--channel", channel)
            self.assertEqual(read["count"], 1)
            again = run_office(home, "waiter", "wait", "--channel", channel, "--timeout", "0")
            self.assertEqual(again["code"], "timeout")
            self.assertFalse(again["new"])


if __name__ == "__main__":
    unittest.main()
