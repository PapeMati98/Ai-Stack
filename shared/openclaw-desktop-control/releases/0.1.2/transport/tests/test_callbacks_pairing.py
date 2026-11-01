"""Callbacks (forged/stale/replayed/generation) and pairing nonces."""

import os
import tempfile
import unittest

from openclaw_control import delivery, models, pairing
from openclaw_control.store import (
    CallbackError,
    ControlStore,
    GenerationMismatchError,
    PairingError,
    ReplayedCallbackError,
    UnknownCallbackError,
)
from openclaw_control.telegram import OwnerConfig, intake_update

OWNER = 111
CHAT = 222


def _mkstore(tmp: str) -> ControlStore:
    return ControlStore(os.path.join(tmp, "authority.db"))


def _cfg() -> OwnerConfig:
    return OwnerConfig(owner_user_ids=(OWNER,), owner_chat_id=CHAT)


def _cb_update(update_id: int, data: str) -> dict:
    return {
        "update_id": update_id,
        "callback_query": {
            "id": "q%d" % update_id,
            "from": {"id": OWNER, "is_bot": False},
            "message": {
                "message_id": 9,
                "chat": {"id": CHAT, "type": "private"},
            },
            "data": data,
        },
    }


class CallbackTest(unittest.TestCase):
    def test_consume_ok_then_replay_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = _mkstore(tmp)
            try:
                action = store.issue_callback(
                    "target_select", "t1", "p", "n", 2, "", 100.0
                )
                first = intake_update(
                    store, _cb_update(1, action), _cfg(), 101.0
                )
                self.assertEqual(first["kind"], "target_selected")
                replay = intake_update(
                    store, _cb_update(2, action), _cfg(), 102.0
                )
                self.assertEqual(replay["kind"], "rejected")
                self.assertEqual(
                    replay["reason"], "replayed-callback"
                )
            finally:
                store.close()

    def test_forged_callback_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = _mkstore(tmp)
            try:
                res = intake_update(
                    store, _cb_update(1, "forged-action-id"), _cfg(), 1.0
                )
                self.assertEqual(res["kind"], "rejected")
                self.assertEqual(res["reason"], "forged-callback")
                with self.assertRaises(UnknownCallbackError):
                    store.consume_callback("forged-action-id", 1.0)
            finally:
                store.close()

    def test_expired_callback_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = _mkstore(tmp)
            try:
                action = store.issue_callback(
                    "target_select", "t1", "p", "n", 1, "", 100.0,
                    ttl=10.0,
                )
                res = intake_update(
                    store, _cb_update(1, action), _cfg(), 200.0
                )
                self.assertEqual(res["kind"], "rejected")
                self.assertEqual(res["reason"], "expired-callback")
            finally:
                store.close()

    def test_generation_and_task_scope_checked(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = _mkstore(tmp)
            try:
                action = store.issue_callback(
                    "target_select", "t1", "p", "n", 4, "", 1.0
                )
                with self.assertRaises(GenerationMismatchError):
                    store.consume_callback(
                        action, 2.0, expected_generation=5
                    )
                with self.assertRaises(CallbackError):
                    store.consume_callback(
                        action, 2.0, expected_task="other"
                    )
                ok = store.consume_callback(
                    action, 2.0, expected_task="t1",
                    expected_generation=4,
                )
                self.assertEqual(ok["task_id"], "t1")
                with self.assertRaises(ReplayedCallbackError):
                    store.consume_callback(action, 3.0)
            finally:
                store.close()

    def test_stop_only_via_callback(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = _mkstore(tmp)
            try:
                action = store.issue_callback(
                    "stop", "", "", "", 0, "all", 1.0
                )
                res = intake_update(
                    store, _cb_update(1, action), _cfg(), 2.0
                )
                self.assertEqual(res["kind"], "stopped")
                self.assertEqual(res["scope"], "all")
                self.assertTrue(store.is_stopped())
            finally:
                store.close()

    def test_consume_and_bind_atomically(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = _mkstore(tmp)
            try:
                row = delivery.enqueue_prompt(
                    store, "", 0, "", "", "tekst", [], 1.0
                )
                action = store.issue_callback(
                    "target_select", "t9", "dp", "thr", 6, "", 1.0
                )
                out = store.consume_and_bind(
                    action, row["dispatch_id"], 2.0
                )
                disp = out["dispatch"]
                self.assertEqual(disp["task_id"], "t9")
                self.assertEqual(disp["generation"], 6)
                self.assertTrue(disp["target_binding"])
                # Replay of the same bind is rejected.
                row2 = delivery.enqueue_prompt(
                    store, "", 0, "", "", "tekst2", [], 3.0
                )
                with self.assertRaises(ReplayedCallbackError):
                    store.consume_and_bind(
                        action, row2["dispatch_id"], 4.0
                    )
            finally:
                store.close()


class PairingTest(unittest.TestCase):
    def test_issue_redeem_once(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = _mkstore(tmp)
            try:
                nonce = pairing.issue(store, 100.0)
                entry = pairing.redeem(
                    store, nonce, OWNER, CHAT, "private", 101.0
                )
                self.assertEqual(entry["sender_id"], OWNER)
                with self.assertRaises(PairingError):
                    pairing.redeem(
                        store, nonce, OWNER, CHAT, "private", 102.0
                    )
            finally:
                store.close()

    def test_expired_unknown_nonprivate_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = _mkstore(tmp)
            try:
                nonce = pairing.issue(store, 100.0, ttl=10.0)
                with self.assertRaises(PairingError):
                    pairing.redeem(
                        store, nonce, OWNER, CHAT, "private", 200.0
                    )
                with self.assertRaises(PairingError):
                    pairing.redeem(
                        store, "no-such-nonce", OWNER, CHAT,
                        "private", 101.0,
                    )
                nonce2 = pairing.issue(store, 300.0)
                with self.assertRaises(PairingError):
                    pairing.redeem(
                        store, nonce2, OWNER, CHAT, "group", 301.0
                    )
                # TTL honours the authority-host wall clock only.
                _ = models.PAIRING_TTL
            finally:
                store.close()


if __name__ == "__main__":
    unittest.main()
