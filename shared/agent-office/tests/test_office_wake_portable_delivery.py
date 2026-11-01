from __future__ import annotations

import threading
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from coordfs import office_pipe
from coordfs import office as office_module
from coordfs.office import Office


class PortableWakeDeliveryTests(unittest.TestCase):
    def setUp(self) -> None:
        temp_root = Path(__file__).resolve().parents[3] / "task-inputs" / "office-wake-rsi" / "task-temp"
        temp_root.mkdir(parents=True, exist_ok=True)
        self.temp = TemporaryDirectory(prefix="office-portable-wake-", dir=temp_root)
        self.home = Path(self.temp.name) / "coord"
        self.sender = Office(agent_id="codex-testhost-sender", home=self.home)
        self.receiver = Office(agent_id="minimax-testhost-receiver", home=self.home)
        self.receiver.presence(ttl_s=300)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_later_durable_message_wakes_polling_waiter_once(self) -> None:
        with patch.object(office_pipe, "SUPPORTED", False):
            started = threading.Event()
            outcome: dict[str, object] = {}

            def wait_for_message() -> None:
                started.set()
                outcome["result"] = self.receiver.wait(timeout_s=4)
                outcome["finished_at"] = time.monotonic()

            waiter = threading.Thread(target=wait_for_message)
            waiter.start()
            self.assertTrue(started.wait(1))
            time.sleep(0.08)

            send_started = time.monotonic()
            sent = self.sender.send(
                "minimax-testhost-receiver", "later message", dedupe_key="portable-wake-1"
            )
            duplicate = self.sender.send(
                "minimax-testhost-receiver", "later message", dedupe_key="portable-wake-1"
            )
            waiter.join(2)

            self.assertFalse(waiter.is_alive())
            self.assertEqual((sent["status"], sent["code"]), ("ok", "sent"))
            self.assertEqual(sent["notification_status"], "fallback")
            self.assertNotIn("notification_error", sent)
            self.assertEqual(sent["waiters_woken"], 0)
            self.assertEqual(sent["wait_transport"], "durable_polling")
            self.assertTrue(duplicate["duplicate"])
            self.assertEqual(duplicate["id"], sent["id"])

            result = outcome["result"]
            self.assertEqual(result["code"], "message")
            self.assertTrue(result["new"])
            self.assertEqual(result["wait_transport"], "durable_polling")
            self.assertLess(outcome["finished_at"] - send_started, 1.25)

            read = self.receiver.read()
            self.assertEqual(read["count"], 1)
            self.assertEqual(read["messages"][0]["untrusted_message"]["id"], sent["id"])

    def test_native_notification_failure_remains_degraded(self) -> None:
        with patch.object(office_module, "default_wait_transport", return_value="named_pipe"), \
                patch.object(office_module, "pipe_notify", return_value=(0, "named-pipe notify failed (5)")):
            sent = self.sender.send("minimax-testhost-receiver", "durable despite pipe failure")

        self.assertEqual((sent["status"], sent["code"]), ("ok", "sent"))
        self.assertEqual(sent["notification_status"], "degraded")
        self.assertEqual(sent["notification_error"], "named-pipe notify failed (5)")
        self.assertEqual(sent["waiters_woken"], 0)
        self.assertNotIn("wait_transport", sent)

    def test_quiet_timeout_and_cancellation_do_not_report_a_message(self) -> None:
        with patch.object(office_pipe, "SUPPORTED", False):
            quiet = self.receiver.wait(timeout_s=0.16)
            self.assertEqual(quiet["code"], "timeout")
            self.assertFalse(quiet["new"])
            self.assertEqual(quiet["notifications"], 0)
            self.assertEqual(quiet["wait_transport"], "durable_polling")

            cancel = threading.Event()
            timer = threading.Timer(0.06, cancel.set)
            timer.start()
            try:
                cancelled = self.receiver.wait(timeout_s=2, cancel=cancel)
            finally:
                timer.join(1)
            self.assertEqual(cancelled["code"], "cancelled")
            self.assertFalse(cancelled["new"])
            self.assertEqual(cancelled["notifications"], 0)
            self.assertEqual(cancelled["wait_transport"], "durable_polling")


if __name__ == "__main__":
    unittest.main()
