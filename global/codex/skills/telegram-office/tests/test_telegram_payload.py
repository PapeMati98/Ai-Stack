"""Offline contract tests for scripts/telegram_payload.py (stdin JSON -> stdout JSON)."""

import json
import subprocess
import sys
import unittest
from pathlib import Path

HELPER = Path(__file__).resolve().parents[1] / "scripts" / "telegram_payload.py"

# The helper exposes AUTHORS as a closed harness list; the send-only candidate
# adds Hermes. Importing the helper here would run the stdio main(); we just
# import the module without invoking main() by reading the module dict.
import importlib.util as _ilu
_spec = _ilu.spec_from_file_location("telegram_payload_module", str(HELPER))
_helper_module = _ilu.module_from_spec(_spec)
_spec.loader.exec_module(_helper_module)
AUTHORS = _helper_module.AUTHORS


def run(payload):
    proc = subprocess.run(
        [sys.executable, "-B", str(HELPER)],
        input=json.dumps(payload).encode("utf-8"),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=30,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    try:
        body = json.loads(proc.stdout.decode("utf-8"))
    except Exception as exc:
        raise AssertionError("helper did not emit JSON: %r" % proc.stdout[:200]) from exc
    return proc.returncode, body


def run_raw(raw):
    proc = subprocess.run(
        [sys.executable, "-B", str(HELPER)],
        input=raw,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=30,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    try:
        body = json.loads(proc.stdout.decode("utf-8"))
    except Exception:
        body = None
    return proc.returncode, body


def compose(**kw):
    base = {"operation": "compose", "chat_id": 123, "format": "card",
            "card": {"kind": "status", "author": "Codex", "body": "ok"}}
    base.update(kw)
    return base


def triage(**kw):
    base = {"operation": "triage",
            "event": {"id": "e1", "revision": 1, "kind": "blockage"},
            "now": {"local_hour": 10, "iso": "2026-10-07T10:00:00Z"},
            "preferences": {"quiet_start": 22, "quiet_end": 7},
            "prior": {}}
    base.update(kw)
    return base


class ComposeTest(unittest.TestCase):
    def test_hermes_accepted_as_card_author(self):
        # Hermes is a harness whose Office tasks can produce author labels; it is
        # not a TEAM_TOOLS slot for the owner intake and never widens the
        # team picker. The helper accepts it exactly like the other harnesses.
        self.assertIn("Hermes", AUTHORS)
        code, out = run(compose(card={"kind": "status", "author": "Hermes",
                                      "body": "Syntetyczna wiadomosc Hermes."}))
        self.assertEqual(code, 0)
        self.assertTrue(out["ok"])
        text = out["result"]["requests"][0]["params"]["text"]
        self.assertIn("[Hermes]", text)

    def test_unknown_author_rejected(self):
        # The allowlist is closed; unknown authors must fail fast.
        code, out = run(compose(card={"kind": "status", "author": "Stranger",
                                      "body": "x"}))
        self.assertEqual(code, 1)
        self.assertFalse(out["ok"])
        self.assertEqual(out["error"]["code"], "MALFORMED")

    def test_send_only_envelope_rejects_buttons(self):
        # The send-only mode of telegram_service denies active buttons because
        # the relay has no inbound callback channel. Defense-in-depth lives in
        # the helper too.
        code, out = run({"operation": "compose", "format": "card",
                         "chat_id": 123, "send_only": True,
                         "card": {"kind": "decision", "author": "Hermes",
                                  "body": "syntetyczna",
                                  "buttons": [{"text": "A", "callback_data": "a"}]}})
        self.assertEqual(code, 1)
        self.assertFalse(out["ok"])
        self.assertEqual(out["error"]["code"], "SEND_ONLY_NO_BUTTONS")

    def test_send_only_envelope_rejects_poll(self):
        # Polls re-create an inbound channel through Telegram. They are denied
        # in send-only even when send_only=True is the only flag.
        code, out = run({"operation": "compose", "format": "poll",
                         "chat_id": 123, "send_only": True,
                         "poll": {"author": "Hermes",
                                  "question": "Q?",
                                  "options": ["A", "B"]}})
        self.assertEqual(code, 1)
        self.assertFalse(out["ok"])
        self.assertEqual(out["error"]["code"], "SEND_ONLY_DENIED")

    def test_card_buttons_html_ack(self):
        code, out = run(compose(card={
            "kind": "question", "author": "Claude", "title": "Raport?",
            "body": "Wybierz <wariant> & termin.", "project": "P1",
            "buttons": [{"text": "A", "callback_data": "optA"},
                        {"text": "Docs", "url": "https://example.com/d"}]}))
        self.assertEqual(code, 0)
        self.assertTrue(out["ok"])
        req = out["result"]["requests"][0]
        self.assertEqual(req["method"], "sendMessage")
        params = req["params"]
        self.assertEqual(params["chat_id"], 123)
        self.assertEqual(params["parse_mode"], "HTML")
        self.assertIn("&lt;wariant&gt;", params["text"])
        kb = params["reply_markup"]["inline_keyboard"]
        self.assertEqual(kb[0][0]["callback_data"], "optA")
        self.assertEqual(kb[1][0]["url"], "https://example.com/d")

    def test_poll_consultative(self):
        code, out = run({"operation": "compose", "chat_id": "123", "format": "poll",
                         "poll": {"author": "ZCode", "question": "Ktory wariant?",
                                  "options": ["A", "B"], "preface": "Burza mozgu"}})
        self.assertEqual(code, 0)
        self.assertTrue(out["ok"])
        self.assertTrue(out["result"]["meta"]["consultative_only"])
        methods = [r["method"] for r in out["result"]["requests"]]
        self.assertEqual(methods, ["sendMessage", "sendPoll"])
        poll_params = out["result"]["requests"][1]["params"]
        self.assertEqual([o["text"] for o in poll_params["options"]], ["A", "B"])

    def test_settled_card_rejects_buttons(self):
        code, out = run(compose(card={
            "kind": "result", "author": "Antigravity", "body": "done",
            "state": "settled",
            "buttons": [{"text": "X", "callback_data": "x"}]}))
        self.assertEqual(code, 1)
        self.assertFalse(out["ok"])
        self.assertEqual(out["error"]["code"], "SETTLED_NO_BUTTONS")

    def test_text_too_long(self):
        code, out = run(compose(card={
            "kind": "status", "author": "Codex", "body": "x" * 5000}))
        self.assertEqual(code, 1)
        self.assertEqual(out["error"]["code"], "TEXT_TOO_LONG")

    def test_invalid_callback_and_url(self):
        code, out = run(compose(card={
            "kind": "question", "author": "Codex", "body": "pick",
            "buttons": [{"text": "X", "callback_data": "has space"}]}))
        self.assertEqual(code, 1)
        self.assertEqual(out["error"]["code"], "INVALID_CALLBACK")
        code, out = run(compose(card={
            "kind": "question", "author": "Codex", "body": "pick",
            "buttons": [{"text": "X", "url": "http://plain.example/"}]}))
        self.assertEqual(code, 1)
        self.assertEqual(out["error"]["code"], "UNSAFE_URL")

    def test_missing_chat_id_fails_closed(self):
        code, out = run({"operation": "compose", "format": "card",
                         "card": {"kind": "status", "author": "Codex", "body": "x"}})
        self.assertEqual(code, 1)
        self.assertEqual(out["error"]["code"], "AMBIGUOUS_ADDRESSING")


class TriageTest(unittest.TestCase):
    def test_quiet_hours_defer_routine(self):
        code, out = run(triage(now={"local_hour": 23, "iso": "2026-10-07T23:00:00Z"}))
        self.assertEqual(code, 0)
        self.assertEqual(out["result"]["action"], "defer")
        self.assertEqual(out["result"]["reason"], "quiet_hours")

    def test_critical_pierces_quiet_and_mute(self):
        code, out = run(triage(
            event={"id": "e9", "revision": 1, "kind": "blockage", "severity": "critical"},
            now={"local_hour": 3, "iso": "2026-10-07T03:00:00Z"},
            preferences={"quiet_start": 22, "quiet_end": 7, "muted": True,
                         "critical_exceptions": [{"event_id": "e9", "revision": 1}]}))
        self.assertEqual(code, 0)
        self.assertEqual(out["result"]["action"], "send")
        self.assertEqual(out["result"]["reason"], "critical_exception")

    def test_duplicate_confirmed_suppresses(self):
        code, out = run(triage(prior={"confirmed_event_id": "e1", "confirmed_revision": 1}))
        self.assertEqual(code, 0)
        self.assertEqual(out["result"]["action"], "suppress")
        self.assertEqual(out["result"]["reason"], "duplicate_confirmed")

    def test_unknown_transport_reconciles_not_resends(self):
        code, out = run(triage(prior={"confirmed_event_id": "e1", "confirmed_revision": 0,
                                      "unknown_outcome": True}))
        self.assertEqual(code, 0)
        self.assertEqual(out["result"]["action"], "defer")
        self.assertEqual(out["result"]["reason"], "reconcile_unknown")

    def test_newer_revision_edits_same_card(self):
        code, out = run(triage(event={"id": "e1", "revision": 2, "kind": "outcome"},
                               prior={"confirmed_event_id": "e1", "confirmed_revision": 1,
                                      "confirmed_message_id": 55}))
        self.assertEqual(code, 0)
        self.assertEqual(out["result"]["action"], "edit")

    def test_nightly_briefing_without_schedule_suppresses(self):
        code, out = run(triage(event={"id": "nightly", "revision": 1, "kind": "briefing"}))
        self.assertEqual(code, 0)
        self.assertEqual(out["result"]["action"], "suppress")
        self.assertEqual(out["result"]["reason"], "no_schedule")

    def test_routine_chatter_digest(self):
        code, out = run(triage(event={"id": "c1", "revision": 1, "kind": "chatter"}))
        self.assertEqual(code, 0)
        self.assertEqual(out["result"]["action"], "digest")

    def test_expired_window_suppresses_with_settled_ui(self):
        code, out = run(triage(
            event={"id": "d1", "revision": 1, "kind": "decision",
                   "expires_at": "2026-10-06T00:00:00Z"}))
        self.assertEqual(code, 0)
        self.assertEqual(out["result"]["action"], "suppress")
        self.assertEqual(out["result"]["reason"], "expired")
        code, out = run(compose(card={
            "kind": "result", "author": "Codex", "body": "Decyzja zapadla.",
            "state": "settled"}))
        self.assertEqual(code, 0)
        req = out["result"]["requests"][0]
        self.assertNotIn("reply_markup", req["params"])
        self.assertIn("rozstrzygnieto", req["params"]["text"])

    def test_quiet_overnight_boundaries(self):
        code, out = run(triage(now={"local_hour": 21, "iso": "2026-10-07T21:00:00Z"}))
        self.assertEqual(out["result"]["action"], "send")
        code, out = run(triage(now={"local_hour": 22, "iso": "2026-10-07T22:00:00Z"}))
        self.assertEqual(out["result"]["action"], "defer")
        self.assertEqual(out["result"]["reason"], "quiet_hours")
        code, out = run(triage(now={"local_hour": 7, "iso": "2026-10-07T07:00:00Z"}))
        self.assertEqual(out["result"]["action"], "send")
        self.assertEqual(out["result"]["reason"], "material_event")

    def test_critical_never_overrides_dedupe_reconcile_expiry(self):
        code, out = run(triage(
            event={"id": "e1", "revision": 1, "kind": "blockage", "severity": "critical"},
            prior={"confirmed_event_id": "e1", "confirmed_revision": 1}))
        self.assertEqual(code, 0)
        self.assertEqual(out["result"]["action"], "suppress")
        self.assertEqual(out["result"]["reason"], "duplicate_confirmed")
        code, out = run(triage(
            event={"id": "e1", "revision": 2, "kind": "blockage", "severity": "critical"},
            prior={"confirmed_event_id": "e1", "confirmed_revision": 1,
                   "unknown_outcome": True}))
        self.assertEqual(code, 0)
        self.assertEqual(out["result"]["action"], "defer")
        self.assertEqual(out["result"]["reason"], "reconcile_unknown")
        code, out = run(triage(
            event={"id": "e1", "revision": 1, "kind": "blockage",
                   "severity": "critical", "expires_at": "2026-10-06T00:00:00Z"}))
        self.assertEqual(code, 0)
        self.assertEqual(out["result"]["action"], "suppress")
        self.assertEqual(out["result"]["reason"], "expired")


class AddressingTest(unittest.TestCase):
    def test_thread_and_reply_ids_pass_through(self):
        code, out = run(compose(message_thread_id=4, reply_to_message_id=90))
        self.assertEqual(code, 0)
        params = out["result"]["requests"][0]["params"]
        self.assertEqual(params["message_thread_id"], 4)
        self.assertEqual(params["reply_parameters"], {"message_id": 90})

    def test_non_int_topic_id_rejected(self):
        code, out = run(compose(message_thread_id="4"))
        self.assertEqual(code, 1)
        self.assertEqual(out["error"]["code"], "MALFORMED")
        code, out = run(compose(reply_to_message_id="90"))
        self.assertEqual(code, 1)
        self.assertEqual(out["error"]["code"], "MALFORMED")

    def test_project_context_lines_render(self):
        code, out = run(compose(card={
            "kind": "status", "author": "Claude", "body": "Praca trwa.",
            "project": "alpha-7", "stage": "droga", "updated_at": "2026-10-07"}))
        self.assertEqual(code, 0)
        text = out["result"]["requests"][0]["params"]["text"]
        self.assertIn("Projekt: alpha-7", text)
        self.assertIn("Etap: droga", text)
        self.assertIn("Aktualizacja: 2026-10-07", text)

    def test_title_shaped_chat_id_rejected(self):
        code, out = run(compose(chat_id="Moj Projekt Chat"))
        self.assertEqual(code, 1)
        self.assertEqual(out["error"]["code"], "AMBIGUOUS_ADDRESSING")

    def test_polish_callback_byte_cap(self):
        short_pl = "wariant_A1"
        code, out = run(compose(card={
            "kind": "question", "author": "Codex", "body": "pick",
            "buttons": [{"text": "A", "callback_data": short_pl}]}))
        self.assertEqual(code, 0)
        long_pl = "Zażółć gęślą jaźń " * 4  # 68 chars, well over 64 UTF-8 bytes
        self.assertGreater(len(long_pl.encode("utf-8")), 64)
        code, out = run(compose(card={
            "kind": "question", "author": "Codex", "body": "pick",
            "buttons": [{"text": "A", "callback_data": long_pl}]}))
        self.assertEqual(code, 1)
        self.assertEqual(out["error"]["code"], "INVALID_CALLBACK")

    def test_edit_message_id_returns_edit_request(self):
        code, out = run(compose(edit_message_id=55, card={
            "kind": "status", "author": "Codex", "body": "Postep 2/3.",
            "project": "alpha-7"}))
        self.assertEqual(code, 0)
        req = out["result"]["requests"][0]
        self.assertEqual(req["method"], "editMessageText")
        self.assertEqual(req["params"]["message_id"], 55)
        self.assertEqual(req["params"]["chat_id"], 123)
        self.assertTrue(out["result"]["meta"]["edit"])
        code, out = run({"operation": "compose", "chat_id": 123,
                         "format": "poll", "edit_message_id": 55,
                         "poll": {"author": "Codex", "question": "Q?",
                                  "options": ["A", "B"]}})
        self.assertEqual(code, 1)
        self.assertEqual(out["error"]["code"], "MALFORMED")


class FailClosedTest(unittest.TestCase):
    def test_oversized_input_rejected(self):
        code, body = run_raw(b'{"operation": "compose", ' + b'"pad": "' + b"x" * 70000 + b'"}')
        self.assertEqual(code, 1)
        self.assertEqual(body["error"]["code"], "INPUT_TOO_LARGE")

    def test_non_json_stdin_rejected(self):
        code, body = run_raw(b"this is not json")
        self.assertEqual(code, 1)
        self.assertEqual(body["error"]["code"], "MALFORMED")

    def test_unknown_operation_rejected(self):
        code, out = run({"operation": "broadcast"})
        self.assertEqual(code, 1)
        self.assertEqual(out["error"]["code"], "MALFORMED")

    def test_helper_writes_no_files(self):
        root = HELPER.parents[1]
        before = sorted(str(p.relative_to(root)) for p in root.rglob("*") if p.is_file())
        run(compose())
        run(triage())
        after = sorted(str(p.relative_to(root)) for p in root.rglob("*") if p.is_file())
        self.assertEqual(before, after)


class ForwardCasesTest(unittest.TestCase):
    def test_owner_asks_whether_claude_finished(self):
        code, out = run(compose(card={
            "kind": "result", "author": "Claude", "title": "Raport?",
            "body": "Raport gotowy, czeka na akcept.",
            "project": "alpha-7", "stage": "odbior"}))
        self.assertEqual(code, 0)
        text = out["result"]["requests"][0]["params"]["text"]
        self.assertIn("[Claude]", text)
        self.assertIn("Projekt: alpha-7", text)

    def test_agent_needs_approval_of_exact_variant(self):
        code, out = run(compose(now_iso="2026-10-07T12:00:00Z", card={
            "kind": "decision", "author": "Codex", "title": "Wariant?",
            "body": "Do wysylki sa dwie wersje.",
            "recommendation": "A, bo miesci sie na jednej stronie.",
            "consequences": "A -> szybsza akceptacja; B -> pelny kontekst.",
            "decision": {"scope_ref": "decision_42", "project_slug": "agent-stack",
                         "operation": "Select A or B for the report draft", "revision": 1,
                         "route_ref": "office_route_7", "expires_at": "2026-10-07T22:00:00Z"},
            "buttons": [{"text": "A", "callback_data": "varA"},
                        {"text": "B", "callback_data": "varB"}]}))
        self.assertEqual(code, 0)
        kb = out["result"]["requests"][0]["params"]["reply_markup"]["inline_keyboard"]
        self.assertEqual(len(kb), 2)

    def test_owner_requests_brainstorming_poll(self):
        code, out = run({"operation": "compose", "chat_id": 123, "format": "poll",
                         "poll": {"author": "Antigravity",
                                  "question": "Ktory kierunek burzy mozgu?",
                                  "options": ["A", "B", "C"]}})
        self.assertEqual(code, 0)
        self.assertTrue(out["result"]["meta"]["consultative_only"])
        self.assertEqual(out["result"]["requests"][-1]["method"], "sendPoll")

    def test_useful_completion_during_quiet_hours_defers(self):
        code, out = run(triage(
            event={"id": "done1", "revision": 1, "kind": "outcome"},
            now={"local_hour": 23, "iso": "2026-10-07T23:00:00Z"}))
        self.assertEqual(code, 0)
        self.assertEqual(out["result"]["action"], "defer")
        self.assertEqual(out["result"]["reason"], "quiet_hours")

    def test_routine_progress_and_duplicate_ack(self):
        code, out = run(triage(
            event={"id": "p1", "revision": 1, "kind": "progress"}))
        self.assertEqual(out["result"]["action"], "digest")
        code, out = run(triage(
            event={"id": "a1", "revision": 3, "kind": "ack"},
            prior={"confirmed_event_id": "a1", "confirmed_revision": 3}))
        self.assertEqual(code, 0)
        self.assertEqual(out["result"]["action"], "suppress")
        self.assertEqual(out["result"]["reason"], "duplicate_confirmed")


if __name__ == "__main__":
    unittest.main()
