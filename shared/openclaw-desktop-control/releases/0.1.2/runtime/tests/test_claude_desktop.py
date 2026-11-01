"""Unit tests for Claude Desktop adapter. Injected transport only."""

from __future__ import annotations

import logging
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
for extra in (ROOT, ROOT / "runtime", ROOT / "automation"):
    text = str(extra)
    if text not in sys.path:
        sys.path.insert(0, text)

from openclaw_ops.adapters import Adapter, ProviderStatus, ResumeResult, StaleFenceError
from runtime.claude_desktop import (
    CAUSE_NONZERO_EXIT,
    CAUSE_TIMEOUT,
    CAUSE_UNRECOGNIZED_SESSION,
    ClaudeDesktopAdapter,
    TransportResult,
    build_resume_argv,
)

SECRET_PROMPT = "SECRET-PROMPT-MUST-NOT-APPEAR-IN-LOGS"


class ScriptedTransport:
    def __init__(self, result: TransportResult) -> None:
        self.result = result
        self.calls = []

    def run(self, argv, timeout):
        self.calls.append((list(argv), timeout))
        return self.result


class RecordingLogger(logging.Logger):
    def __init__(self) -> None:
        super().__init__("test-claude-desktop")
        self.lines = []

    def info(self, msg, *args, **kwargs):
        text = msg % args if args else str(msg)
        self.lines.append(text)


def _task(**extra):
    base = {
        "task_id": "t1",
        "generation": 1,
        "native_thread": "sess-abc",
        "prompt": SECRET_PROMPT,
    }
    base.update(extra)
    return base


class TestClaudeDesktopAdapter(unittest.TestCase):
    def test_protocol_matches_ops_adapter(self) -> None:
        adapter = ClaudeDesktopAdapter(
            transport=ScriptedTransport(TransportResult(returncode=0, stdout="ok"))
        )
        self.assertIsInstance(adapter, Adapter)
        self.assertTrue(hasattr(adapter, "get_status"))
        self.assertTrue(hasattr(adapter, "lookup"))
        self.assertTrue(hasattr(adapter, "resume"))
        self.assertTrue(adapter.supports_idempotent_lookup)
        self.assertTrue(adapter.supports_fencing)
        status = adapter.get_status(_task())
        self.assertIsInstance(status, ProviderStatus)
        result = adapter.resume(_task(), "key-1", 1)
        self.assertIsInstance(result, ResumeResult)
        self.assertEqual(result.ack, "acked")
        self.assertEqual(adapter.lookup("key-1"), result.native_run_id)

    def test_injected_transport_receives_resume_argv(self) -> None:
        transport = ScriptedTransport(TransportResult(returncode=0, stdout="ok"))
        adapter = ClaudeDesktopAdapter(transport=transport, timeout=33)
        adapter.resume(_task(), "k", 1)
        self.assertEqual(len(transport.calls), 1)
        argv, timeout = transport.calls[0]
        self.assertEqual(argv, build_resume_argv("sess-abc", SECRET_PROMPT))
        self.assertEqual(timeout, 33)
        self.assertEqual(argv[:4], ["claude", "--resume", "sess-abc", "--print"])

    def test_timeout_nonzero_and_unrecognized_are_distinct_causes(self) -> None:
        cases = [
            (
                TransportResult(returncode=-1, timed_out=True, stderr="hang"),
                CAUSE_TIMEOUT,
            ),
            (
                TransportResult(returncode=2, stderr="boom"),
                CAUSE_NONZERO_EXIT,
            ),
            (
                TransportResult(
                    returncode=1,
                    stderr="Error: No conversation found for session sess-missing",
                ),
                CAUSE_UNRECOGNIZED_SESSION,
            ),
        ]
        seen = []
        for result, expected in cases:
            adapter = ClaudeDesktopAdapter(transport=ScriptedTransport(result))
            outcome = adapter.resume(_task(), "k-" + expected, 1)
            self.assertEqual(outcome.ack, "unknown")
            status = adapter.get_status(_task())
            self.assertEqual(status.failure_cause, expected)
            seen.append(status.failure_cause)
        self.assertEqual(len(set(seen)), 3)
        self.assertEqual(
            set(seen),
            {CAUSE_TIMEOUT, CAUSE_NONZERO_EXIT, CAUSE_UNRECOGNIZED_SESSION},
        )

    def test_stale_fence_matches_ops_adapter(self) -> None:
        transport = ScriptedTransport(TransportResult(returncode=0, stdout="ok"))
        adapter = ClaudeDesktopAdapter(transport=transport)
        adapter.resume(_task(), "k-fence", 2)
        with self.assertRaises(StaleFenceError):
            adapter.resume(_task(), "k-fence-2", 1)
        self.assertEqual(len(transport.calls), 1)

    def test_prompt_never_reaches_logs(self) -> None:
        logger = RecordingLogger()
        adapter = ClaudeDesktopAdapter(
            transport=ScriptedTransport(TransportResult(returncode=0, stdout="ok")),
            logger=logger,
        )
        adapter.resume(_task(), "k", 1)
        blob = "\n".join(logger.lines + adapter._log_records)
        self.assertNotIn(SECRET_PROMPT, blob)
        self.assertNotIn("SECRET-PROMPT", blob)


if __name__ == "__main__":
    unittest.main()
