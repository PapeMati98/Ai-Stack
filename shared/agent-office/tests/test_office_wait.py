"""Criterion 2: office wait wakes under 1 s on a new message and respects the timeout."""

from __future__ import annotations

import threading
import time

from office_support import OfficeTestCase


class WaitTests(OfficeTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.announce("claude-testhost-bob")

    def _wait_in_thread(self, timeout_s: float) -> tuple[threading.Thread, dict]:
        holder: dict = {}

        def runner() -> None:
            started = time.monotonic()
            holder["result"] = self.bob.wait(timeout_s=timeout_s)
            holder["elapsed"] = time.monotonic() - started

        thread = threading.Thread(target=runner)
        thread.start()
        return thread, holder

    def test_wakes_under_one_second_after_send(self) -> None:
        thread, holder = self._wait_in_thread(timeout_s=10)
        time.sleep(0.3)
        self.alice.send("claude-testhost-bob", "wake up")
        sent_at = time.monotonic()
        thread.join(timeout=5)
        self.assertFalse(thread.is_alive())
        woke_after = time.monotonic() - sent_at
        self.assertEqual(holder["result"]["code"], "message", holder)
        self.assertTrue(holder["result"]["new"])
        self.assertLess(woke_after, 1.0, f"wake latency {woke_after:.3f}s")
        self.assertEqual(self.bob.read()["count"], 1)

    def test_wakes_in_backoff_tier_two_still_under_one_second(self) -> None:
        thread, holder = self._wait_in_thread(timeout_s=10)
        time.sleep(1.6)
        sent_at = time.monotonic()
        self.alice.send("claude-testhost-bob", "later")
        thread.join(timeout=5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(holder["result"]["code"], "message")
        self.assertLess(time.monotonic() - sent_at, 1.0)

    def test_timeout_is_respected_without_messages(self) -> None:
        started = time.monotonic()
        result = self.bob.wait(timeout_s=0.4)
        elapsed = time.monotonic() - started
        self.assertEqual(result["code"], "timeout")
        self.assertFalse(result["new"])
        self.assertGreaterEqual(elapsed, 0.39)
        self.assertLess(elapsed, 1.0)
        self.assertEqual(result["notifications"], 0)

    def test_zero_timeout_returns_immediately(self) -> None:
        self.assertEqual(self.bob.wait(timeout_s=0)["code"], "timeout")
        self.alice.send("claude-testhost-bob", "already there")
        self.assertEqual(self.bob.wait(timeout_s=0)["code"], "message")

    def test_wait_does_not_consume_and_read_clears(self) -> None:
        self.alice.send("claude-testhost-bob", "one")
        self.assertEqual(self.bob.wait(timeout_s=0.1)["code"], "message")
        self.assertEqual(self.bob.wait(timeout_s=0.1)["code"], "message")
        self.assertEqual(self.bob.read()["count"], 1)
        self.assertEqual(self.bob.wait(timeout_s=0.1)["code"], "timeout")

    def test_invalid_timeout_is_rejected(self) -> None:
        self.assertEqual(self.bob.wait(timeout_s=-1)["code"], "invalid_timeout")
        self.assertEqual(self.bob.wait(timeout_s=10_000)["code"], "invalid_timeout")

    def test_cli_wait_times_out_and_exits_zero(self) -> None:
        started = time.monotonic()
        code, payload = self.run_cli(["--agent", "claude-testhost-bob", "wait", "--timeout", "0.3"])
        self.assertEqual(code, 0)
        self.assertEqual(payload["code"], "timeout")
        self.assertLess(time.monotonic() - started, 5.0)

    def test_cli_accepts_agent_after_wait(self) -> None:
        code, payload = self.run_cli(["wait", "--agent", "claude-testhost-bob", "--timeout", "0"])
        self.assertEqual((code, payload["agent"]), (0, "claude-testhost-bob"))
