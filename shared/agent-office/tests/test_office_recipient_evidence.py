"""Recipient contract r2: an unknown address is refused, never silently delivered.

Evidence that a canonical id is an agent: a live presence lease, a registration marker
(written on presence and on every successful send by that agent), or a stored message
whose office-derived `from` is that id. A genuine first contact needs new_recipient.
"""

from __future__ import annotations

import json
import unittest
from unittest import mock
from pathlib import Path

from support import PACKAGE_ROOT  # noqa: F401  (puts the package on sys.path)

from coordfs.office import Office, inbox_path, known_agent_path
from coordfs.office_mcp import OfficeMcpServer
from test_wake import writable_mkdtemp

LEAD = "claude-host-lead"
WORKER = "codex-host-worker"
TYPO = "codex-host-worksr"


class RecipientEvidenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.home = Path(writable_mkdtemp(prefix="office-recipient-r2-")) / "coord"

    def office(self, agent: str) -> Office:
        return Office(agent_id=agent, home=self.home)

    def test_first_contact_without_flag_is_refused_and_leaves_no_inbox(self) -> None:
        result = self.office(LEAD).send(WORKER, "zadanie")
        self.assertEqual((result["status"], result["code"]), ("error", "unknown_recipient"))
        self.assertIn("--new-recipient", result["message"])
        self.assertFalse(inbox_path(self.home, WORKER).exists())

    def test_explicit_first_contact_is_delivered_and_marked(self) -> None:
        result = self.office(LEAD).send(WORKER, "zadanie", new_recipient=True)
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["recipient_state"], "unregistered")
        self.assertIn("warning", result)
        stored = json.loads(inbox_path(self.home, WORKER).read_text(encoding="utf-8").splitlines()[-1])
        self.assertIs(stored["first_contact"], True)
        # The recipient is not registered by being addressed: a typo stays unknown.
        self.assertFalse(known_agent_path(self.home, WORKER).exists())

    def test_sender_becomes_addressable_after_its_first_send(self) -> None:
        self.office(LEAD).send(WORKER, "zadanie", new_recipient=True)
        reply = self.office(WORKER).send(LEAD, "odpowiedz")
        # LEAD sent before, so it is registered; WORKER's reply needs no flag.
        self.assertEqual(reply["status"], "ok")
        self.assertNotIn("recipient_state", reply)
        # WORKER is now registered by its own send: LEAD can write to it without the flag.
        again = self.office(LEAD).send(WORKER, "kolejne")
        self.assertEqual(again["status"], "ok")
        self.assertNotIn("recipient_state", again)

    def test_typo_of_a_registered_agent_is_refused(self) -> None:
        self.assertEqual(self.office(WORKER).presence(ttl_s=5)["status"], "ok")
        result = self.office(LEAD).send(TYPO, "zadanie")
        self.assertEqual(result["code"], "unknown_recipient")
        self.assertFalse(inbox_path(self.home, TYPO).exists())

    def test_historical_sender_without_marker_is_accepted_and_registered(self) -> None:
        # A message stored before markers existed: its office-derived `from` is evidence.
        legacy = inbox_path(self.home, LEAD)
        legacy.parent.mkdir(parents=True, exist_ok=True)
        legacy.write_text(json.dumps({
            "id": "a" * 32, "ts": "2026-09-20T10:00:00Z", "from": WORKER, "to": LEAD,
            "kind": "note", "body": "stara wiadomosc", "reply_to": None, "trust": "untrusted",
        }) + "\n", encoding="utf-8")
        self.assertFalse(known_agent_path(self.home, WORKER).exists())
        result = self.office(LEAD).send(WORKER, "odpowiedz")
        self.assertEqual(result["status"], "ok")
        self.assertTrue(known_agent_path(self.home, WORKER).is_file())

    def test_historical_backfill_io_error_does_not_fail_the_send(self) -> None:
        legacy = inbox_path(self.home, LEAD)
        legacy.parent.mkdir(parents=True, exist_ok=True)
        legacy.write_text(json.dumps({
            "id": "c" * 32, "ts": "2026-09-20T10:00:00Z", "from": WORKER, "to": LEAD,
            "kind": "note", "body": "stara", "reply_to": None, "trust": "untrusted",
        }) + "\n", encoding="utf-8")
        with mock.patch.object(Office, "_remember_agent", side_effect=OSError("disk")):
            result = self.office(LEAD).send(WORKER, "odpowiedz")
        self.assertEqual((result["status"], result["code"]), ("ok", "sent"))

    def test_an_inbox_file_alone_is_not_evidence(self) -> None:
        stale = inbox_path(self.home, TYPO)
        stale.parent.mkdir(parents=True, exist_ok=True)
        stale.write_text(json.dumps({
            "id": "b" * 32, "ts": "2026-09-20T10:00:00Z", "from": LEAD, "to": TYPO,
            "kind": "note", "body": "do literowki", "reply_to": None, "trust": "untrusted",
        }) + "\n", encoding="utf-8")
        result = self.office(LEAD).send(TYPO, "znowu")
        self.assertEqual(result["code"], "unknown_recipient")

    def test_new_recipient_must_be_boolean(self) -> None:
        result = self.office(LEAD).send(WORKER, "zadanie", new_recipient="yes")
        self.assertEqual((result["status"], result["code"]), ("error", "invalid_new_recipient"))

    def test_contractor_profile_refuses_first_contact(self) -> None:
        server = OfficeMcpServer(self.office(LEAD), heartbeat_ttl_s=None, profile="contractor",
                                 channels=["task-r2"], peers=[WORKER])
        result = server.call_tool("office_send", {"to": WORKER, "body": "x", "new_recipient": True})
        payload = result if "status" in result else json.loads(result["content"][0]["text"])
        self.assertEqual(payload["code"], "contractor_first_contact_refused")


    def test_contractor_rejects_a_non_boolean_flag_as_invalid(self) -> None:
        server = OfficeMcpServer(self.office(LEAD), heartbeat_ttl_s=None, profile="contractor",
                                 channels=["task-r2"], peers=[WORKER])
        result = server.call_tool("office_send", {"to": WORKER, "body": "x", "new_recipient": "yes"})
        payload = result if "status" in result else json.loads(result["content"][0]["text"])
        self.assertEqual(payload["code"], "invalid_new_recipient")

    def test_dilemma_with_unknown_copy_recipient_writes_nothing(self) -> None:
        result = self.office(LEAD).dilemma_open(
            "task-r2", "Ktora sciezka?", {"A": "pierwsza", "B": "druga"}, to=TYPO,
        )
        self.assertEqual((result["status"], result["code"]), ("error", "unknown_recipient"))
        self.assertEqual(self.office(WORKER).read(channel="task-r2")["messages"], [])
        self.assertFalse(inbox_path(self.home, TYPO).exists())

    def test_dilemma_refuses_a_harness_alias_without_delivering(self) -> None:
        first = Office(harness="codex", session_id="s1", home=self.home)
        second = Office(harness="codex", session_id="s2", home=self.home)
        self.assertEqual(first.presence(ttl_s=60)["status"], "ok")
        self.assertEqual(second.presence(ttl_s=60)["status"], "ok")
        lead = self.office(LEAD)
        result = lead.dilemma_open("task-r2", "Ktora?", {"A": "a", "B": "b"}, to="codex")
        self.assertEqual((result["status"], result["code"]), ("error", "not_an_address"), result)
        self.assertEqual(first.read()["count"], 0)
        self.assertEqual(second.read()["count"], 0)
        self.assertEqual(lead.read(channel="task-r2")["count"], 0, "the refused alias also prevents the channel post")
        self.assertFalse(inbox_path(self.home, "codex").exists())

    def test_dedupe_replay_still_registers_the_sender(self) -> None:
        self.assertEqual(self.office(LEAD).send("#task-r2", "raz", dedupe_key="k1")["status"], "ok")
        known_agent_path(self.home, LEAD).unlink()  # a marker lost after the first send
        replay = self.office(LEAD).send("#task-r2", "raz", dedupe_key="k1")
        self.assertIs(replay["duplicate"], True)
        self.assertTrue(known_agent_path(self.home, LEAD).is_file())

    def test_presence_keeps_its_lease_when_the_marker_write_fails(self) -> None:
        office = self.office(WORKER)
        with mock.patch.object(Office, "_remember_canonical_agent", side_effect=OSError("disk")):
            claimed = office.presence(ttl_s=5)
        self.assertEqual((claimed["status"], claimed["code"]), ("ok", "present"))


if __name__ == "__main__":
    unittest.main()
