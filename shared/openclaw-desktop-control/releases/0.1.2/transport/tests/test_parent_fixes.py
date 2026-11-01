"""Regression cases for parent-discovered T4/T5/T7 transport defects.

These are product scenarios (crash, STOP race, edit-after-retarget, future
clock, prefix-capable adapter) — not implementation mirrors.
"""

from __future__ import annotations

import os
import tempfile
import unittest

from openclaw_control import delivery, models
from openclaw_control.adapters import FakeControlAdapter, NonLiteralAdapter
from openclaw_control.status_text import StatusTracker, render_status_pl
from openclaw_control.store import ControlStore, StoppedError
from openclaw_control.telegram import (
    CrashInjected,
    OwnerConfig,
    intake_update,
    recover_pending,
)

OWNER = 111
CHAT = 222


def _mkstore(tmp: str) -> ControlStore:
    return ControlStore(os.path.join(tmp, "authority.db"))


def _cfg() -> OwnerConfig:
    return OwnerConfig(owner_user_ids=(OWNER,), owner_chat_id=CHAT)


def _msg(update_id: int, text: str, message_id: int = 0, **over: object) -> dict:
    mid = message_id or update_id * 10
    msg: dict = {
        "message_id": mid,
        "from": {"id": OWNER, "is_bot": False},
        "chat": {"id": CHAT, "type": "private"},
        "text": text,
    }
    msg.update(over)
    return {"update_id": update_id, "message": msg}


class InboxCrashRecoveryTest(unittest.TestCase):
    def test_crash_after_stash_recovers_exact_prompt(self) -> None:
        text = "  nie zgub Żółć e\u0301 🦞\r\n"
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "authority.db")
            store = ControlStore(path)
            try:
                with self.assertRaises(CrashInjected):
                    intake_update(
                        store, _msg(7, text), _cfg(), 1.0,
                        crash_after="after-stash",
                    )
                self.assertEqual(store.list_dispatches(), [])
                pending = store.pending_inbox()
                self.assertEqual(len(pending), 1)
                self.assertEqual(int(pending[0]["processed"]), 0)
            finally:
                store.close()
            store2 = ControlStore(path)
            try:
                recovered = recover_pending(store2, _cfg(), 2.0)
                self.assertEqual(len(recovered), 1)
                self.assertIn(recovered[0]["kind"], ("prompt", "prompt-unbound"))
                row = store2.require_dispatch(recovered[0]["dispatch_id"])
                self.assertEqual(row["raw_text"], text)
                self.assertEqual(store2.pending_inbox(), [])
                dup = intake_update(store2, _msg(7, text), _cfg(), 3.0)
                self.assertEqual(dup["kind"], "duplicate")
                self.assertEqual(len(store2.list_dispatches()), 1)
            finally:
                store2.close()

    def test_crash_after_enqueue_rolls_back_then_inbox_replays(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = _mkstore(tmp)
            try:
                with self.assertRaises(CrashInjected):
                    intake_update(
                        store, _msg(3, "po rollback"), _cfg(), 1.0,
                        crash_after="after-enqueue",
                    )
                self.assertEqual(store.list_dispatches(), [])
                recovered = recover_pending(store, _cfg(), 2.0)
                self.assertEqual(recovered[0]["kind"], "prompt-unbound")
                row = store.require_dispatch(recovered[0]["dispatch_id"])
                self.assertEqual(row["raw_text"], "po rollback")
            finally:
                store.close()


class StoppedQueuedDispatchTest(unittest.TestCase):
    def test_claim_after_stop_does_not_dispatch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = _mkstore(tmp)
            try:
                row = delivery.enqueue_prompt(
                    store, "t-stop", 1, "p", "n", "czekaj", [], 1.0
                )
                store.set_stop(models.STOP_SCOPE_ALL, 2.0, "owner")
                claimed = store.claim_one("w", 3.0)
                self.assertIsNone(claimed)
                still = store.require_dispatch(row["dispatch_id"])
                self.assertEqual(still["state"], models.QUEUED)
                adapter = FakeControlAdapter()
                # Recheck-before-send: even a forced dispatching row unclaims.
                store.conn.execute(
                    "UPDATE cc_dispatches SET state=? WHERE dispatch_id=?",
                    (models.DISPATCHING, row["dispatch_id"]),
                )
                with self.assertRaises(StoppedError):
                    delivery.deliver(
                        store, adapter, row["dispatch_id"], 4.0,
                        claim_token="nope",
                    )
                self.assertEqual(adapter.deliver_calls, 0)
            finally:
                store.close()


class RecordStartAfterStopTest(unittest.TestCase):
    def test_observed_start_recorded_after_stop(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = _mkstore(tmp)
            try:
                row = delivery.enqueue_prompt(
                    store, "t", 1, "p", "n", "go", [], 1.0
                )
                claimed = store.claim_one("w", 1.0)
                delivery.deliver(
                    store, FakeControlAdapter(), claimed["dispatch_id"], 1.0
                )
                store.set_stop(models.STOP_SCOPE_ALL, 2.0, "owner")
                started = store.record_start(row["dispatch_id"], 3.0)
                self.assertEqual(started["state"], models.RUNNING)
                with self.assertRaises(StoppedError):
                    delivery.enqueue_prompt(
                        store, "t", 1, "p", "n", "kolejna", [], 4.0
                    )
            finally:
                store.close()


class OutboundIntentCrashTest(unittest.TestCase):
    def test_intent_before_send_survives_crash_no_duplicate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "authority.db")
            store = ControlStore(path)
            try:
                row = delivery.enqueue_prompt(
                    store, "t", 1, "p", "n", "tekst", [], 1.0
                )
                did = row["dispatch_id"]
                tracker = StatusTracker(store)
                text = render_status_pl("dispatch", models.QUEUED)
                first = tracker.decide(did, text)
                self.assertEqual(first["action"], "send")
                tracker.persist_intent(did, CHAT, text, 2.0)
                # Crash before ACK: intent is durable.
            finally:
                store.close()
            store2 = ControlStore(path)
            try:
                tracker = StatusTracker(store2)
                text = render_status_pl("dispatch", models.QUEUED)
                self.assertEqual(
                    tracker.decide(did, text)["action"], "wait"
                )
                recovered = tracker.recover_intents(3.0)
                self.assertEqual(len(recovered), 1)
                self.assertEqual(
                    tracker.decide(did, text)["action"], "wait"
                )
                self.assertEqual(
                    tracker.decide(did, text)["reason"], "uncertain-send"
                )
            finally:
                store2.close()


class EditBindsOriginalTargetTest(unittest.TestCase):
    def test_change_selection_then_edit_keeps_original_target(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = _mkstore(tmp)
            try:
                cfg = _cfg()
                a1 = store.issue_callback(
                    "target_select", "task-A", "codex-desktop", "thr-A",
                    1, "", 1.0,
                )
                intake_update(
                    store,
                    {
                        "update_id": 1,
                        "callback_query": {
                            "id": "q1",
                            "from": {"id": OWNER, "is_bot": False},
                            "message": {
                                "message_id": 9,
                                "chat": {"id": CHAT, "type": "private"},
                            },
                            "data": a1,
                        },
                    },
                    cfg,
                    1.0,
                )
                first = intake_update(
                    store, _msg(2, "pierwszy prompt", message_id=20), cfg, 2.0
                )
                self.assertEqual(first["kind"], "prompt")
                orig = store.require_dispatch(first["dispatch_id"])
                self.assertEqual(orig["task_id"], "task-A")
                self.assertEqual(orig["native_thread"], "thr-A")
                a2 = store.issue_callback(
                    "target_select", "task-B", "codex-desktop", "thr-B",
                    2, "", 3.0,
                )
                intake_update(
                    store,
                    {
                        "update_id": 3,
                        "callback_query": {
                            "id": "q2",
                            "from": {"id": OWNER, "is_bot": False},
                            "message": {
                                "message_id": 9,
                                "chat": {"id": CHAT, "type": "private"},
                            },
                            "data": a2,
                        },
                    },
                    cfg,
                    3.0,
                )
                sel = store.get_selection(CHAT)
                self.assertEqual(sel["task_id"], "task-B")
                edit = {
                    "update_id": 4,
                    "edited_message": {
                        "message_id": 20,
                        "from": {"id": OWNER, "is_bot": False},
                        "chat": {"id": CHAT, "type": "private"},
                        "text": "poprawiony prompt",
                    },
                }
                res = intake_update(store, edit, cfg, 4.0)
                self.assertEqual(res["kind"], "correction")
                self.assertEqual(res["correction_of"], first["dispatch_id"])
                corr = store.require_dispatch(res["dispatch_id"])
                self.assertEqual(corr["task_id"], "task-A")
                self.assertEqual(corr["native_thread"], "thr-A")
                self.assertEqual(corr["raw_text"], "poprawiony prompt")
                self.assertNotEqual(corr["task_id"], "task-B")
            finally:
                store.close()


class PrefixPerPayloadNotGlobalDisableTest(unittest.TestCase):
    def test_nonliteral_adapter_delivers_ordinary_text(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = _mkstore(tmp)
            try:
                adapter = NonLiteralAdapter()
                row = delivery.enqueue_prompt(
                    store, "t", 1, "p", "n", "zwykły tekst", [], 1.0
                )
                claimed = store.claim_one("w", 1.0)
                out = delivery.deliver(
                    store, adapter, claimed["dispatch_id"], 1.0
                )
                self.assertEqual(out["state"], models.DELIVERED)
                self.assertEqual(
                    adapter.readback(row["dispatch_id"]), "zwykły tekst"
                )
                store.record_start(row['dispatch_id'], 1.5)
                store.record_result(row['dispatch_id'], models.COMPLETED, 1.6)
                row2 = delivery.enqueue_prompt(
                    store, "t", 1, "p", "n", "  /clear", [], 2.0
                )
                claimed2 = store.claim_one("w", 2.0)
                blocked = delivery.deliver(
                    store, adapter, claimed2["dispatch_id"], 2.0
                )
                self.assertEqual(blocked["state"], models.BLOCKED)
                self.assertEqual(
                    store.require_dispatch(row2["dispatch_id"])["raw_text"],
                    "  /clear",
                )
            finally:
                store.close()


class FutureStatusRejectedTest(unittest.TestCase):
    def test_future_timestamp_is_stale(self) -> None:
        delivery.require_fresh_status(50.0, 50.0)
        delivery.require_fresh_status(49.5, 50.0)
        with self.assertRaises(delivery.StaleStatusError):
            delivery.require_fresh_status(52.0, 50.0)
        with self.assertRaises(delivery.StaleStatusError):
            delivery.require_fresh_status(39.0, 50.0)


if __name__ == "__main__":
    unittest.main()
