"""v1 busy holds and exact durable INBOX consumption before relay delivery."""

from __future__ import annotations

import json
import unittest
from unittest import mock

from support import PACKAGE_ROOT  # noqa: F401

from coordfs.errors import CoordError
from coordfs.office import cursor_path
from coordfs.wake import Wake
import test_wake_relay as _relay
from test_wake_relay import RELAY, SENDER, TARGET


class BusyConsumedTests(unittest.TestCase):
    setUp = _relay.WakeRelayTests.setUp
    wake = _relay.WakeRelayTests.wake
    office = _relay.WakeRelayTests.office
    enroll = _relay.WakeRelayTests.enroll
    dispatch_files = _relay.WakeRelayTests.dispatch_files

    def tick(self, wake: Wake) -> dict:
        with mock.patch("coordfs.wake.subprocess.run") as process:
            result = wake.tick()
        process.assert_not_called()
        return result

    def attempts(self, wake: Wake) -> int:
        return wake._daily_attempts(wake._load_binding("desktop-binding"), wake._ledger_entries())

    def message(self, *, kind: str = "question") -> dict:
        return self.office(SENDER).send(TARGET, "synthetic untrusted body", kind=kind)

    def queued_message(self, wake: Wake) -> tuple[dict, dict]:
        self.enroll(wake, triggers=["message"])
        sent = self.message()
        result = self.tick(wake)
        self.assertEqual(result["counts"].get("dispatched"), 1)
        dispatch = wake._load_dispatch(self.dispatch_files()[0].stem)
        return sent, dispatch

    def test_busy_then_idle_preserves_unread_input_and_wakes_exactly_once(self) -> None:
        wake = self.wake()
        self.enroll(wake, triggers=["message"])
        self.office(TARGET).presence(ttl_s=900, status="busy")
        sent = self.message()
        initial_cursor = wake._read_cursor("desktop-binding")
        for _ in range(2):
            result = self.tick(wake)
            self.assertEqual(result["decisions"][0]["reason"], "recipient_busy")
            self.assertTrue(result["decisions"][0]["retry"])
            self.assertEqual(wake._read_cursor("desktop-binding"), initial_cursor)
            self.assertEqual(self.dispatch_files(), [])
        self.office(TARGET).presence(ttl_s=900, status="idle")
        resumed = self.tick(self.wake())
        self.assertEqual(resumed["counts"]["dispatched"], 1)
        self.assertEqual(resumed["decisions"][0]["trigger_ref"], sent["id"])
        self.assertEqual(self.tick(wake)["decisions"], [])
        self.assertEqual(len(self.dispatch_files()), 1)

    def test_later_nonwakeable_input_cannot_advance_past_a_busy_question(self) -> None:
        wake = self.wake()
        self.enroll(wake, triggers=["message"])
        self.office(TARGET).presence(ttl_s=900, status="busy")
        sent = self.message()
        self.message(kind="note")
        result = self.tick(wake)
        self.assertEqual([row["trigger_ref"] for row in result["decisions"]], [sent["id"]])
        self.assertEqual(wake._read_cursor("desktop-binding"), {"offset": 0, "head": ""})
        self.office(TARGET).presence(ttl_s=900, status="idle")
        self.assertEqual(self.tick(wake)["counts"]["dispatched"], 1)
        self.assertEqual(len(self.dispatch_files()), 1)

    def test_busy_then_read_then_idle_does_not_wake_or_charge(self) -> None:
        wake = self.wake()
        self.enroll(wake, triggers=["message"])
        self.office(TARGET).presence(ttl_s=900, status="busy")
        self.message()
        self.tick(wake)
        read = self.office(TARGET).read()
        self.assertTrue(read["cursor_advanced"])
        self.office(TARGET).presence(ttl_s=900, status="idle")
        result = self.tick(self.wake())
        self.assertEqual(result["decisions"][0]["reason"], "message_already_read")
        self.assertEqual(self.dispatch_files(), [])
        self.assertEqual(self.attempts(wake), 0)
        self.assertEqual(self.tick(wake)["decisions"], [])

    def test_read_during_active_turn_is_consumed_before_the_busy_gate(self) -> None:
        wake = self.wake()
        self.enroll(wake, triggers=["message"])
        self.office(TARGET).presence(ttl_s=900, status="busy")
        self.message()
        self.office(TARGET).read()
        result = self.tick(wake)
        self.assertEqual(result["decisions"][0]["reason"], "message_already_read")
        self.assertNotIn("retry", result["decisions"][0])
        self.assertEqual(self.dispatch_files(), [])

    def test_partial_inbox_read_suppresses_only_the_exact_consumed_message(self) -> None:
        wake = self.wake()
        self.enroll(wake, triggers=["message"])
        first, second = self.message(), self.message()
        self.office(TARGET).read(limit=1)
        result = self.tick(wake)
        self.assertEqual([(row["trigger_ref"], row["reason"]) for row in result["decisions"]],
                         [(first["id"], "message_already_read"), (second["id"], "relay_pending")])
        self.assertEqual(len(self.dispatch_files()), 1)

    def test_peek_and_kind_filter_do_not_consume_for_wake(self) -> None:
        for read_arguments in ({"peek": True}, {"kind": "question"}):
            with self.subTest(read_arguments=read_arguments):
                self.setUp()
                wake = self.wake()
                self.enroll(wake, triggers=["message"])
                self.message()
                read = self.office(TARGET).read(**read_arguments)
                self.assertEqual(read["count"], 1)
                self.assertFalse(read["cursor_advanced"])
                self.assertEqual(self.tick(wake)["counts"]["dispatched"], 1)
                dispatch = wake._load_dispatch(self.dispatch_files()[0].stem)
                wake.relay_claim(dispatch["dispatch_id"], relay_agent=RELAY)
                self.assertEqual(self.attempts(wake), 1)

    def test_queued_then_read_is_cancelled_before_claim_without_charge(self) -> None:
        wake = self.wake()
        _sent, dispatch = self.queued_message(wake)
        self.office(TARGET).read()
        with self.assertRaises(CoordError) as consumed:
            self.wake().relay_claim(dispatch["dispatch_id"], relay_agent=RELAY)
        self.assertEqual(consumed.exception.code, "relay_dispatch_cancelled")
        self.assertEqual(consumed.exception.payload["reason"], "message_already_read")
        current = wake._load_dispatch(dispatch["dispatch_id"])
        self.assertEqual((current["status"], current["cancel_reason"]), ("cancelled", "message_already_read"))
        self.assertEqual(self.attempts(wake), 0)
        self.assertEqual(self.tick(wake)["decisions"], [])
        self.assertEqual(len(self.dispatch_files()), 1)

    def test_pending_listing_safely_consumes_a_read_queue(self) -> None:
        wake = self.wake()
        _sent, dispatch = self.queued_message(wake)
        self.office(TARGET).read()
        self.assertEqual(wake.relay_pending()["dispatches"], [])
        self.assertEqual(wake._load_dispatch(dispatch["dispatch_id"])["status"], "cancelled")
        self.assertEqual(self.attempts(wake), 0)

    def test_claim_then_read_blocks_native_seam_without_refunding_pickup(self) -> None:
        wake = self.wake()
        _sent, dispatch = self.queued_message(wake)
        wake.relay_claim(dispatch["dispatch_id"], relay_agent=RELAY)
        self.office(TARGET).read()
        with self.assertRaises(CoordError) as consumed:
            self.wake().relay_native_begin(dispatch["dispatch_id"], relay_agent=RELAY)
        self.assertEqual(consumed.exception.code, "message_already_read")
        self.assertEqual(wake._load_dispatch(dispatch["dispatch_id"])["status"], "cancelled")
        self.assertEqual(self.attempts(wake), 1)

    def test_read_between_tick_gate_and_queue_is_checked_again(self) -> None:
        wake = self.wake()
        self.enroll(wake, triggers=["message"])
        self.message()
        create = wake._create_relay_dispatch

        def consume_then_create(*args, **kwargs):
            self.office(TARGET).read()
            return create(*args, **kwargs)

        with mock.patch.object(wake, "_create_relay_dispatch", side_effect=consume_then_create):
            result = self.tick(wake)
        self.assertEqual(result["decisions"][0]["reason"], "message_already_read")
        self.assertEqual(self.dispatch_files(), [])
        self.assertEqual(self.attempts(wake), 0)

    def test_read_after_claim_eligibility_is_checked_before_pickup(self) -> None:
        wake = self.wake()
        _sent, dispatch = self.queued_message(wake)
        eligibility = wake._relay_eligibility_reason

        def consume_after_gate(current):
            reason = eligibility(current)
            self.office(TARGET).read()
            return reason

        with mock.patch.object(wake, "_relay_eligibility_reason", side_effect=consume_after_gate):
            with self.assertRaises(CoordError) as consumed:
                wake.relay_claim(dispatch["dispatch_id"], relay_agent=RELAY)
        self.assertEqual(consumed.exception.code, "message_already_read")
        self.assertEqual(self.attempts(wake), 0)
        self.assertEqual(wake._load_dispatch(dispatch["dispatch_id"])["status"], "cancelled")

    def test_read_after_native_eligibility_is_checked_before_submission(self) -> None:
        wake = self.wake()
        _sent, dispatch = self.queued_message(wake)
        wake.relay_claim(dispatch["dispatch_id"], relay_agent=RELAY)
        eligibility = wake._relay_eligibility_reason

        def consume_after_gate(current):
            reason = eligibility(current)
            self.office(TARGET).read()
            return reason

        with mock.patch.object(wake, "_relay_eligibility_reason", side_effect=consume_after_gate):
            with self.assertRaises(CoordError) as consumed:
                wake.relay_native_begin(dispatch["dispatch_id"], relay_agent=RELAY)
        self.assertEqual(consumed.exception.code, "message_already_read")
        self.assertEqual(self.attempts(wake), 1)
        self.assertEqual(wake._load_dispatch(dispatch["dispatch_id"])["status"], "cancelled")

    def test_missing_cursor_is_fresh_but_malformed_cursor_holds_without_skipping(self) -> None:
        malformed = ["{", "null", "[]", '{"offset":true,"head":""}',
                     '{"offset":1,"head":""}', '{"offset":0,"head":"","seq":"0"}']
        for raw in malformed:
            with self.subTest(raw=raw):
                self.setUp()
                wake = self.wake()
                self.enroll(wake, triggers=["message"])
                self.message()
                self.message(kind="note")
                path = cursor_path(self.home, TARGET, "inbox")
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(raw, encoding="utf-8")
                result = self.tick(wake)
                self.assertEqual([row["reason"] for row in result["decisions"]], ["recipient_read_cursor_unavailable"])
                self.assertEqual(wake._read_cursor("desktop-binding"), {"offset": 0, "head": ""})
                self.assertEqual(self.dispatch_files(), [])
                self.assertEqual(self.attempts(wake), 0)
                # Recover through Office's real read, rather than resetting the cursor.
                self.office(TARGET).read()
                self.assertEqual(self.tick(wake)["counts"].get("dispatched", 0), 0)

    def test_malformed_or_uncertain_cursor_after_queue_holds_claim_for_later(self) -> None:
        for cursor in (None, {"offset": 0, "head": "f" * 32, "seq": 0},
                       {"offset": 1, "head": "replace-with-message", "seq": 0},
                       {"offset": 0, "head": "replace-with-message", "seq": 999}):
            with self.subTest(cursor=cursor):
                self.setUp()
                wake = self.wake()
                sent, dispatch = self.queued_message(wake)
                path = cursor_path(self.home, TARGET, "inbox")
                path.parent.mkdir(parents=True, exist_ok=True)
                data = None if cursor is None else dict(cursor)
                if data is not None and data["head"] == "replace-with-message":
                    data["head"] = sent["id"]
                path.write_text(json.dumps(data), encoding="utf-8")
                with self.assertRaises(CoordError) as held:
                    self.wake().relay_claim(dispatch["dispatch_id"], relay_agent=RELAY)
                self.assertEqual(held.exception.code, "recipient_read_cursor_unavailable")
                self.assertEqual(wake._load_dispatch(dispatch["dispatch_id"])["status"], "pending")
                self.assertEqual(self.attempts(wake), 0)

    def rotating_sender(self):
        sender = self.office(SENDER)
        sender.inbox_max_bytes = 1024
        sender.inbox_retain = 4
        return sender

    def test_read_cursor_in_newer_generation_consumes_retained_older_triggers(self) -> None:
        wake = self.wake()
        self.enroll(wake, triggers=["message"])
        sender = self.rotating_sender()
        for _ in range(3):
            sender.send(TARGET, "x" * 800, kind="question")
        self.office(TARGET).read(limit=10)
        cursor = self.office(TARGET)._read_cursor("inbox")
        self.assertGreater(cursor["seq"], 0)
        result = self.tick(wake)
        self.assertEqual([row["reason"] for row in result["decisions"]], ["message_already_read"] * 3)
        self.assertEqual(self.dispatch_files(), [])

    def test_legacy_retained_cursor_inherits_generation_without_false_consumption(self) -> None:
        wake = self.wake()
        self.enroll(wake, triggers=["message"])
        sender = self.rotating_sender()
        sender.send(TARGET, "x" * 800, kind="question")
        self.office(TARGET).read()
        path = cursor_path(self.home, TARGET, "inbox")
        cursor = json.loads(path.read_text(encoding="utf-8"))
        cursor.pop("seq")
        path.write_text(json.dumps(cursor), encoding="utf-8")
        newer = sender.send(TARGET, "y" * 800, kind="question")
        result = self.tick(wake)
        self.assertEqual([row["reason"] for row in result["decisions"]], ["message_already_read", "relay_pending"])
        dispatch = wake._load_dispatch(self.dispatch_files()[0].stem)
        self.assertEqual(dispatch["trigger_ref"], newer["id"])

    def test_evicted_cursor_generation_and_unretained_queued_trigger_fail_closed(self) -> None:
        wake = self.wake()
        _sent, dispatch = self.queued_message(wake)
        self.office(TARGET).read()
        sender = self.rotating_sender()
        sender.inbox_retain = 1
        for _ in range(4):
            sender.send(TARGET, "x" * 800, kind="question")
        result = self.tick(wake)
        self.assertEqual([row["reason"] for row in result["decisions"]], ["recipient_read_cursor_unavailable"])
        with self.assertRaises(CoordError) as held:
            wake.relay_claim(dispatch["dispatch_id"], relay_agent=RELAY)
        self.assertEqual(held.exception.code, "recipient_read_cursor_unavailable")
        self.assertEqual(wake._load_dispatch(dispatch["dispatch_id"])["status"], "pending")
        self.assertEqual(self.attempts(wake), 0)

    def test_malformed_rotation_ledger_cannot_be_treated_as_unread(self) -> None:
        wake = self.wake()
        self.enroll(wake, triggers=["message"])
        self.message()
        stream = self.office(TARGET)._inbox_stream(TARGET)
        stream.ledger.write_text("{", encoding="utf-8")
        result = self.tick(wake)
        self.assertEqual(result["decisions"][0]["reason"], "recipient_read_cursor_unavailable")
        self.assertEqual(self.dispatch_files(), [])


if __name__ == "__main__":
    unittest.main()
