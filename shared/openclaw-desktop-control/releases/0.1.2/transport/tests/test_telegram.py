"""Telegram intake: allowlist, literal payloads, edits, dedupe, receipts."""

import os
import tempfile
import unittest

from openclaw_control import models
from openclaw_control.store import ControlStore
from openclaw_control.telegram import OwnerConfig, intake_update

OWNER = 111
CHAT = 222


def _mkstore(tmp: str) -> ControlStore:
    return ControlStore(os.path.join(tmp, "authority.db"))


def _cfg() -> OwnerConfig:
    return OwnerConfig(owner_user_ids=(OWNER,), owner_chat_id=CHAT)


def _msg(update_id: int, text: str, **over: object) -> dict:
    msg: dict = {
        "message_id": update_id * 10,
        "from": {"id": OWNER, "is_bot": False},
        "chat": {"id": CHAT, "type": "private"},
        "text": text,
    }
    msg.update(over)
    return {"update_id": update_id, "message": msg}


class TelegramIntakeTest(unittest.TestCase):
    def test_valid_prompt_unbound_literal(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = _mkstore(tmp)
            try:
                res = intake_update(
                    store, _msg(1, "/clear nie jest komendą"), _cfg(), 1.0
                )
                self.assertEqual(res["kind"], "prompt-unbound")
                row = store.require_dispatch(res["dispatch_id"])
                self.assertEqual(row["raw_text"], "/clear nie jest komendą")
            finally:
                store.close()

    def test_stop_word_in_text_is_literal(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = _mkstore(tmp)
            try:
                res = intake_update(store, _msg(1, "STOP"), _cfg(), 1.0)
                self.assertIn(res["kind"], ("prompt", "prompt-unbound"))
                self.assertFalse(store.is_stopped())
            finally:
                store.close()

    def test_wrong_sender_group_forward_bot_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = _mkstore(tmp)
            try:
                cfg = _cfg()
                bad_sender = _msg(1, "hej")
                bad_sender["message"]["from"] = {
                    "id": 999, "is_bot": False
                }
                self.assertEqual(
                    intake_update(store, bad_sender, cfg, 1.0)["reason"],
                    "wrong-sender",
                )
                group = _msg(2, "hej")
                group["message"]["chat"] = {
                    "id": CHAT, "type": "group"
                }
                self.assertEqual(
                    intake_update(store, group, cfg, 1.0)["reason"],
                    "wrong-chat",
                )
                fwd = _msg(3, "hej", forward_date=123)
                self.assertEqual(
                    intake_update(store, fwd, cfg, 1.0)["reason"],
                    "forwarded",
                )
                via = _msg(4, "hej", via_bot={"id": 5})
                self.assertEqual(
                    intake_update(store, via, cfg, 1.0)["reason"],
                    "impersonation",
                )
                bot = _msg(5, "hej")
                bot["message"]["from"] = {"id": OWNER, "is_bot": True}
                self.assertEqual(
                    intake_update(store, bot, cfg, 1.0)["reason"],
                    "bot-sender",
                )
                # Nothing persisted for rejected senders.
                self.assertEqual(store.list_dispatches(), [])
            finally:
                store.close()

    def test_duplicate_update_ids_durable_reorder_safe(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = _mkstore(tmp)
            try:
                cfg = _cfg()
                first = intake_update(store, _msg(5, "piąta"), cfg, 1.0)
                # Higher id first, then lower: both accepted (reorder-safe).
                second = intake_update(store, _msg(3, "trzecia"), cfg, 2.0)
                self.assertIn(first["kind"], ("prompt", "prompt-unbound"))
                self.assertIn(second["kind"], ("prompt", "prompt-unbound"))
                dup = intake_update(store, _msg(5, "piąta"), cfg, 3.0)
                self.assertEqual(dup["kind"], "duplicate")
                self.assertEqual(len(store.list_dispatches()), 2)
            finally:
                store.close()

    def test_edit_is_new_correction(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = _mkstore(tmp)
            try:
                cfg = _cfg()
                first = intake_update(
                    store, _msg(1, "pierwsza wersja"), cfg, 1.0
                )
                edit = {
                    "update_id": 2,
                    "edited_message": {
                        "message_id": 10,
                        "from": {"id": OWNER, "is_bot": False},
                        "chat": {"id": CHAT, "type": "private"},
                        "text": "poprawiona wersja",
                    },
                }
                res = intake_update(store, edit, cfg, 2.0)
                self.assertEqual(res["kind"], "correction")
                self.assertEqual(
                    res["correction_of"], first["dispatch_id"]
                )
                self.assertNotEqual(
                    res["dispatch_id"], first["dispatch_id"]
                )
                # Original history untouched.
                orig = store.require_dispatch(first["dispatch_id"])
                self.assertEqual(orig["raw_text"], "pierwsza wersja")
                new = store.require_dispatch(res["dispatch_id"])
                self.assertEqual(new["raw_text"], "poprawiona wersja")
            finally:
                store.close()

    def test_large_voice_attachment_unsupported_never_truncated(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = _mkstore(tmp)
            try:
                cfg = _cfg()
                big = "x" * (models.TELEGRAM_TEXT_LIMIT + 1)
                res = intake_update(store, _msg(1, big), cfg, 1.0)
                self.assertEqual(res["kind"], "unsupported")
                self.assertEqual(res["reason"], "too-large")
                self.assertIn("receipt_id", res)
                voice = _msg(2, "")
                del voice["message"]["text"]
                voice["message"]["voice"] = {"duration": 5}
                res2 = intake_update(store, voice, cfg, 2.0)
                self.assertEqual(res2["kind"], "unsupported")
                photo = _msg(3, "")
                del photo["message"]["text"]
                photo["message"]["photo"] = [{"file_id": "a"}]
                res3 = intake_update(store, photo, cfg, 3.0)
                self.assertEqual(res3["kind"], "unsupported")
                # No truncated prompt stored anywhere.
                self.assertEqual(store.list_dispatches(), [])
            finally:
                store.close()

    def test_entities_kept_separate_raw_exact(self) -> None:
        import json

        with tempfile.TemporaryDirectory() as tmp:
            store = _mkstore(tmp)
            try:
                upd = _msg(1, "hej @jan /start")
                upd["message"]["entities"] = [
                    {"type": "mention", "offset": 4, "length": 4},
                    {"type": "bot_command", "offset": 9, "length": 6},
                ]
                res = intake_update(store, upd, _cfg(), 1.0)
                row = store.require_dispatch(res["dispatch_id"])
                self.assertEqual(row["raw_text"], "hej @jan /start")
                self.assertEqual(
                    json.loads(row["entities_json"]),
                    upd["message"]["entities"],
                )
            finally:
                store.close()

    def test_selection_binds_prompt(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = _mkstore(tmp)
            try:
                cfg = _cfg()
                action = store.issue_callback(
                    kind="target_select",
                    task_id="t1",
                    provider="p",
                    native_thread="n",
                    generation=3,
                    scope="",
                    now=1.0,
                )
                cb = {
                    "update_id": 1,
                    "callback_query": {
                        "id": "q1",
                        "from": {"id": OWNER, "is_bot": False},
                        "message": {
                            "message_id": 7,
                            "chat": {"id": CHAT, "type": "private"},
                        },
                        "data": action,
                    },
                }
                sel = intake_update(store, cb, cfg, 1.0)
                self.assertEqual(sel["kind"], "target_selected")
                res = intake_update(store, _msg(2, "treść"), cfg, 2.0)
                self.assertEqual(res["kind"], "prompt")
                row = store.require_dispatch(res["dispatch_id"])
                self.assertEqual(row["task_id"], "t1")
                self.assertEqual(row["generation"], 3)
                self.assertTrue(row["target_binding"])
            finally:
                store.close()


if __name__ == "__main__":
    unittest.main()
