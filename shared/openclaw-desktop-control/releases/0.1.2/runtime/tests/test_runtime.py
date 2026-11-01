"""Runtime controller tests: injected Bot API + native adapter, no network."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
for extra in (ROOT, ROOT / "transport", ROOT / "automation", ROOT / "native"):
    text = str(extra)
    if text not in sys.path:
        sys.path.insert(0, text)

from openclaw_control import models
from openclaw_control.store import ControlStore
from openclaw_control.telegram import OwnerConfig, intake_update
from openclaw_ops.ledger import Ledger
from runtime.config import load_config
from runtime.controller import Controller
from runtime.codex_adapter import CodexControlAdapter
from runtime.helpdesk_hook import HelpdeskHook
from runtime.registry import AdapterRegistry
from runtime.supervision import consider_resume, note_no_progress
from codex_desktop import CodexDesktop, Unavailable

THREAD = "01a0ac02-c538-7033-81be-58a15d8eb075"
CALLER = "01a0aaaa-5394-7d93-a00d-8663131f6837"
OWNER = 111
CHAT = 222
BOT = 555
TOKEN = "TEST-TOKEN-NOT-LIVE"
TOKEN_ENV = "TELEGRAM_BOT_TOKEN"


class App:
    caller_thread = CALLER

    def __init__(self):
        self.sent = []
        self.state = "idle"
        self.turn = "before"
        self.lose_ack = False

    def call(self, name, args):
        if name == "read_thread":
            return {
                "thread": {"id": THREAD, "status": {"type": self.state}},
                "turns": [{"id": self.turn, "status": "completed"}],
            }
        self.sent.append(args)
        if self.lose_ack:
            raise Unavailable("connection closed")
        return {"threadId": THREAD}


class FakeBot:
    def __init__(self):
        self.methods = []
        self.updates = []
        self.messages = []
        self.next_mid = 100
        self.me = {"id": BOT, "is_bot": True, "username": "t"}

    def opener(self, url, body, headers, timeout):
        method = str(url).rsplit("/", 1)[-1]
        self.methods.append(method)
        params = json.loads(body.decode("utf-8")) if body else {}
        if method == "getMe":
            raw = {"ok": True, "result": self.me}
        elif method == 'getWebhookInfo':
            raw = {'ok': True, 'result': {'url': ''}}
        elif method == "getUpdates":
            batch = list(self.updates)
            self.updates = []
            raw = {"ok": True, "result": batch}
        elif method == "sendMessage":
            self.next_mid += 1
            self.messages.append(params)
            raw = {"ok": True, "result": {"message_id": self.next_mid}}
        elif method == "editMessageText":
            self.messages.append(params)
            raw = {"ok": True, "result": {"message_id": params.get("message_id")}}
        elif method == "answerCallbackQuery":
            raw = {"ok": True, "result": True}
        elif method in ("setWebhook", "deleteWebhook"):
            raw = {"ok": False, "error_code": 400, "description": "forbidden"}
        else:
            raw = {"ok": False, "error_code": 400}
        return 200, url, json.dumps(raw).encode("utf-8")


def _write_cfg(tmp: str, **over) -> str:
    doc = {
        "enabled": True,
        "authority_db": os.path.join(tmp, "authority.db"),
        "owner": {"user_ids": [OWNER], "chat_id": CHAT, "bot_id": BOT},
        "telegram": {"enabled": True, "long_poll_timeout": 0, "host": "api.telegram.org"},
        "targets": [
            {
                "id": "codex-main",
                "label": "Codex",
                "provider": "codex-desktop",
                "native_thread": THREAD,
                "task_id": "t1",
                "generation": 1,
                "enabled": True,
                "verified": True,
            },
            {
                "id": "claude",
                "label": "Claude",
                "provider": "claude-desktop",
                "native_thread": "",
                "enabled": False,
                "verified": False,
            },
        ],
        "adapters": {
            "codex-desktop": {"enabled": True, "threads": {THREAD: {}}},
            "claude-desktop": {"enabled": False, "verified": False},
        },
        "supervision": {"auto_resume": False, "max_no_progress_attempts": 2},
        "helpdesk": {"enabled": False},
    }
    doc.update(over)
    path = os.path.join(tmp, "config.json")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(doc, fh)
    return path


def _msg(update_id, text, sender=OWNER, chat=CHAT, chat_type="private"):
    return {
        "update_id": update_id,
        "message": {
            "message_id": update_id * 10,
            "from": {"id": sender, "is_bot": False},
            "chat": {"id": chat, "type": chat_type},
            "text": text,
        },
    }


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cfg_path = _write_cfg(self.tmp.name)
        self.cfg = load_config(self.cfg_path)
        self.ledger = Ledger(self.cfg.authority_db)
        self.addCleanup(self.ledger.close)
        self.store = ControlStore(self.cfg.authority_db)
        self.addCleanup(self.store.close)
        self.root = Path(self.tmp.name)
        self.rollout = self.root / ("rollout-" + THREAD + ".jsonl")
        self.rollout.write_text("", encoding="utf-8")
        self.app = App()
        self.desktop = CodexDesktop(
            self.app, {THREAD: {"rollout_path": str(self.rollout.resolve())}}, self.root
        )
        self.adapter = CodexControlAdapter(self.desktop, clock=None)
        self.registry = AdapterRegistry()
        self.registry.register(
            "codex-desktop",
            self.adapter,
            verified=True,
            capabilities=self.adapter.capabilities,
        )
        self.registry.register(
            "claude-desktop",
            None,
            verified=False,
            reason="claude-unverified",
        )
        self.bot = FakeBot()
        self.ctrl = Controller(
            self.cfg,
            self.store,
            {TOKEN_ENV: TOKEN},
            self.bot.opener,
            self.registry,
            ledger=self.ledger,
            now=lambda: 1_700_000_000.0,
        )

    def test_status_json_and_limitations(self):
        doc = self.ctrl.status_doc()
        self.assertEqual(doc["kind"], "runtime-status")
        self.assertIn("limitations", doc["payload"])
        listing = {a["name"]: a for a in doc["payload"]["adapters"]}
        self.assertFalse(listing["claude-desktop"]["enabled"])
        self.assertFalse(listing["codex-desktop"]["supports_cancel"])
        self.assertFalse(listing["codex-desktop"]["supports_atomic_last_turn_cas"])
        blob = json.dumps(doc)
        self.assertNotIn(TOKEN, blob)

    def test_outsider_never_elicits_response(self):
        self.bot.updates = [_msg(1, "hej", sender=999)]
        self.ctrl.validate_bot()
        results = self.ctrl.poll_and_intake()
        self.assertEqual(results[0]["reason"], "wrong-sender")
        self.assertEqual(self.store.list_dispatches(), [])
        self.assertEqual(self.bot.messages, [])

    def test_raw_unicode_and_prefix_refusal(self):
        text = "  Żółć e\u0301 🦞\r\n`x`"
        self.store.set_selection(CHAT, "t1", 1, "codex-desktop", THREAD, "a", 1.0)
        intake_update(
            self.store, _msg(1, text), OwnerConfig((OWNER,), CHAT), 1.0
        )
        out = self.ctrl.dispatch_once()
        self.assertEqual(self.app.sent[0]["prompt"], text)
        self.assertTrue(out)
        # Prefix handling is a separate operation; respect same-target serialization.
        first = self.store.list_dispatches()[0]
        self.store.record_start(first['dispatch_id'], 1.5)
        self.store.record_result(first['dispatch_id'], models.COMPLETED, 1.6)
        intake_update(
            self.store,
            _msg(2, "/clear"),
            OwnerConfig((OWNER,), CHAT),
            2.0,
        )
        self.ctrl.dispatch_once()
        self.assertEqual(len(self.app.sent), 1)
        blocked = [d for d in self.store.list_dispatches() if d["state"] == "blocked"]
        self.assertTrue(blocked)

    def test_native_model_not_required(self):
        self.assertFalse(hasattr(self.adapter, "client"))
        self.assertEqual(self.adapter.capabilities["role"], "agent-delegation")
        st = self.adapter.status_of(THREAD)
        self.assertEqual(st["state"], "idle")
        self.assertFalse(st["atomic_compare_and_swap"])

    def test_stop_blocks_send_not_observed_start(self):
        self.store.set_selection(CHAT, "t1", 1, "codex-desktop", THREAD, "a", 1.0)
        intake_update(
            self.store, _msg(1, "start"), OwnerConfig((OWNER,), CHAT), 1.0
        )
        self.ctrl.dispatch_once()
        self.assertEqual(len(self.app.sent), 1)
        self.store.set_stop(models.STOP_SCOPE_ALL, 2.0, "owner")
        intake_update(
            self.store, _msg(2, "po stop"), OwnerConfig((OWNER,), CHAT), 2.0
        )
        self.ctrl.dispatch_once()
        self.assertEqual(len(self.app.sent), 1)

    def test_no_unsafe_auto_resume(self):
        d = consider_resume(
            self.store,
            self.adapter,
            "codex-main",
            THREAD,
            1_700_000_000.0,
            auto_resume=False,
            grant_ref="",
            grant_expires=0,
            unfinished_goal=True,
            last_turn_continuity=True,
        )
        self.assertEqual(d["decision"], "observe-only")
        self.assertEqual(d["reason"], "auto-resume-not-granted")
        note_no_progress(self.store, "codex-main", 1.0)
        note_no_progress(self.store, "codex-main", 2.0)
        d2 = consider_resume(
            self.store,
            self.adapter,
            "codex-main",
            THREAD,
            1_700_000_000.0,
            auto_resume=True,
            grant_ref="g1",
            grant_expires=9_000_000_000,
            unfinished_goal=True,
            last_turn_continuity=True,
            max_no_progress=2,
        )
        self.assertEqual(d2["decision"], "diagnosis")

    def test_unknown_readback_not_resend(self):
        self.app.lose_ack = True
        self.store.set_selection(CHAT, "t1", 1, "codex-desktop", THREAD, "a", 1.0)
        intake_update(
            self.store, _msg(1, "x"), OwnerConfig((OWNER,), CHAT), 1.0
        )
        self.ctrl.dispatch_once()
        self.assertEqual(len(self.app.sent), 1)
        row = self.store.list_dispatches()[0]
        self.assertEqual(row["state"], models.DELIVERY_UNKNOWN)
        self.ctrl.dispatch_once()
        self.assertEqual(len(self.app.sent), 1)

    def test_helpdesk_injected_callback(self):
        seen = {}

        def cb(store, now):
            seen["now"] = now
            return {"action": "quiet-no-new-items", "new": 0, "status": "ok"}

        hook = HelpdeskHook(cb)
        report = hook.tick(self.store, 9.0)
        self.assertEqual(report["new"], 0)
        self.assertEqual(seen["now"], 9.0)

    def test_sqlite_restart_and_concurrency(self):
        path = self.cfg.authority_db
        self.store.enqueue_dispatch(
            "t1", 1, "codex-desktop", THREAD, "a", [], 1.0
        )
        barrier = threading.Barrier(2)
        got = []

        def worker(name):
            s = ControlStore(path)
            try:
                barrier.wait(timeout=10)
                got.append(s.claim_one(name, 2.0))
            finally:
                s.close()

        threads = [
            threading.Thread(target=worker, args=("a",)),
            threading.Thread(target=worker, args=("b",)),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)
        self.assertEqual(len([g for g in got if g is not None]), 1)

    def test_initial_menu_once(self):
        self.ctrl.validate_bot()
        self.ctrl._send_menu(1.0, force=False)
        self.ctrl._send_menu(2.0, force=False)
        sends = [m for m in self.bot.messages if m.get("text", "").startswith("OpenClaw")]
        self.assertEqual(len(sends), 1)

    def test_webhook_methods_never_used(self):
        self.ctrl.validate_bot()
        self.bot.updates = []
        self.ctrl.poll_and_intake()
        self.assertNotIn("setWebhook", self.bot.methods)
        self.assertNotIn("deleteWebhook", self.bot.methods)

    def test_example_config_disabled_defaults(self):
        path = str(ROOT / "runtime" / "config.example.json")
        cfg = load_config(path)
        self.assertFalse(cfg.enabled)
        self.assertFalse(cfg.telegram_enabled)
        self.assertFalse(cfg.codex.enabled)
        self.assertFalse(cfg.claude_enabled)
        self.assertFalse(cfg.auto_resume)


if __name__ == "__main__":
    unittest.main()
