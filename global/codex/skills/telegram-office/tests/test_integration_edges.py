"""Product regressions found during parent integration, not implementation mirrors."""
import unittest

from test_telegram_payload import compose, run, triage


def scoped_decision():
    return {"kind": "decision", "author": "Codex", "body": "Approve isolated package abc?",
            "recommendation": "Activate the tested candidate",
            "decision": {"scope_ref": "d42", "project_slug": "agent-stack",
                         "operation": "Activate package abc in isolated test profile",
                         "revision": 1, "route_ref": "office_route_7",
                         "expires_at": "2026-10-07T22:00:00+02:00"},
            "buttons": [{"text": "Approve", "callback_data": "d42_a"}]}


class IntegrationEdgesTest(unittest.TestCase):
    def test_unknown_without_confirmed_event_blocks_even_critical(self):
        code, out = run(triage(
            event={"id": "e9", "revision": 1, "kind": "blockage", "severity": "critical"},
            preferences={"critical_exceptions": [{"event_id": "e9", "revision": 1}]},
            prior={"unknown_outcome": True}))
        self.assertEqual((code, out["result"]["action"]), (0, "defer"))
        self.assertEqual(out["result"]["reason"], "reconcile_unknown")

    def test_critical_label_cannot_bypass_owner_mute(self):
        code, out = run(triage(
            event={"id": "e9", "revision": 1, "kind": "blockage", "severity": "critical"},
            preferences={"muted": True}))
        self.assertEqual((code, out["result"]["reason"]), (0, "muted"))

    def test_critical_exception_is_exact_revision(self):
        code, out = run(triage(
            event={"id": "e9", "revision": 2, "kind": "blockage", "severity": "critical"},
            preferences={"muted": True, "critical_exceptions": [{"event_id": "e9", "revision": 1}]}))
        self.assertEqual((code, out["result"]["action"]), (0, "suppress"))

    def test_routine_traffic_cannot_be_promoted_to_critical(self):
        code, out = run(triage(
            event={"id": "e9", "revision": 1, "kind": "ack", "severity": "critical"},
            preferences={"critical_exceptions": [{"event_id": "e9", "revision": 1}]}))
        self.assertEqual((code, out["result"]["action"]), (0, "digest"))

    def test_expiry_compares_instants_across_timezones(self):
        code, out = run(triage(
            now={"local_hour": 2, "iso": "2026-10-07T00:30:00Z"},
            event={"id": "d1", "revision": 1, "kind": "decision",
                   "expires_at": "2026-10-07T02:00:00+02:00"}))
        self.assertEqual((code, out["result"]["reason"]), (0, "expired"))

    def test_expiry_without_timezone_is_not_guessed(self):
        code, out = run(triage(
            event={"id": "d1", "revision": 1, "kind": "decision",
                   "expires_at": "2026-10-07T02:00:00"}))
        self.assertEqual((code, out["ok"]), (1, False))

    def test_unscoped_approval_card_rejected(self):
        code, out = run(compose(card={"kind": "decision", "author": "Codex", "body": "Approve?",
                                      "recommendation": "Yes", "buttons": [{"text": "Yes", "callback_data": "x"}]}))
        self.assertEqual((code, out["error"]["code"]), (1, "UNSCOPED_DECISION"))

    def test_expired_card_edit_clears_buttons_without_claiming_approval(self):
        code, out = run(compose(edit_message_id=55, now_iso="2026-10-07T20:00:00Z",
                               card=scoped_decision()))
        self.assertEqual(code, 0)
        request = out["result"]["requests"][0]
        self.assertEqual(request["method"], "editMessageText")
        self.assertEqual(request["params"]["message_id"], 55)
        self.assertEqual(request["params"]["reply_markup"], {"inline_keyboard": []})
        self.assertIn("wygaslo", request["params"]["text"])
        self.assertNotIn("rozstrzygnieto", request["params"]["text"])

    def test_active_card_has_exact_project_operation_revision(self):
        code, out = run(compose(now_iso="2026-10-07T19:00:00Z", card=scoped_decision()))
        self.assertEqual(code, 0)
        text = out["result"]["requests"][0]["params"]["text"]
        self.assertIn("agent-stack", text)
        self.assertIn("package abc", text)
        self.assertIn("rev 1", text)

    def test_project_scope_mismatch_rejected(self):
        card = scoped_decision()
        card["project"] = "wrong-project"
        code, out = run(compose(now_iso="2026-10-07T19:00:00Z", card=card))
        self.assertEqual((code, out["error"]["code"]), (1, "UNSCOPED_DECISION"))

    def test_missing_card_reference_does_not_create_new_send(self):
        code, out = run(triage(event={"id": "e1", "revision": 2, "kind": "outcome"},
                               prior={"confirmed_event_id": "e1", "confirmed_revision": 1}))
        self.assertEqual((code, out["result"]["reason"]), (0, "missing_card_ref"))

    def test_current_poll_options_and_explicit_anonymity(self):
        request = {"operation": "compose", "chat_id": 123, "format": "poll",
                   "poll": {"author": "Codex", "question": "Directions?", "options": [str(i) for i in range(12)]}}
        code, out = run(request)
        self.assertEqual(code, 0)
        poll = out["result"]["requests"][0]["params"]
        self.assertEqual(len(poll["options"]), 12)
        self.assertTrue(poll["is_anonymous"])
        self.assertIn("[Codex]", poll["question"])
        self.assertTrue(out["result"]["meta"]["consultative_only"])
        request["poll"]["options"].append("13")
        self.assertEqual(run(request)[0], 1)

    def test_force_reply_cannot_be_put_on_an_edit(self):
        code, out = run(compose(edit_message_id=55, card={
            "kind": "question", "author": "Codex", "body": "Which date?", "force_reply": True}))
        self.assertEqual((code, out["ok"]), (1, False))

    def test_single_word_project_title_is_not_a_chat_id(self):
        for chat_id in ("g1", "agent-stack", 0, True):
            self.assertEqual(run(compose(chat_id=chat_id))[0], 1)

    def test_polish_callback_limit_counts_bytes_not_characters(self):
        card = {"kind": "question", "author": "Codex", "body": "Pick",
                "buttons": [{"text": "A", "callback_data": "ż" * 32}]}
        self.assertEqual(run(compose(card=card))[0], 0)
        card["buttons"][0]["callback_data"] = "ż" * 33
        self.assertEqual(run(compose(card=card))[0], 1)

    def test_malformed_preferences_do_not_silently_enable_send(self):
        self.assertEqual(run(triage(preferences={"muted": "true"}))[0], 1)
        self.assertEqual(run(triage(prior={"unknown_outcome": "true"}))[0], 1)

    def test_configured_briefing_sends_and_unconfigured_critical_does_not(self):
        code, out = run(triage(event={"id": "brief", "revision": 1, "kind": "briefing"},
                               preferences={"quiet_start": 22, "quiet_end": 7, "briefing_configured": True}))
        self.assertEqual((code, out["result"]["action"]), (0, "send"))
        code, out = run(triage(event={"id": "brief", "revision": 1, "kind": "briefing", "severity": "critical"}))
        self.assertEqual((code, out["result"]["reason"]), (0, "no_schedule"))

    def test_invalid_confirmed_revision_never_bypasses_dedupe(self):
        code, out = run(triage(prior={"confirmed_event_id": "e1", "confirmed_revision": "1"}))
        self.assertEqual((code, out["ok"]), (1, False))


if __name__ == "__main__":
    unittest.main()
