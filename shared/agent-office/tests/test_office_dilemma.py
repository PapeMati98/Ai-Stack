"""Dilemma packets: worker -> architect/lead -> worker with one answer and an explicit ACK."""

from __future__ import annotations

import json

from office_support import OfficeTestCase

from coordfs.office_mcp import OfficeMcpServer

CHANNEL = "task-dilemma"
OPTIONS = {"A": "cache w pamieci procesu", "B": "cache w Redis"}


class DilemmaTests(OfficeTestCase):
    def test_open_answer_ack_round_trip(self) -> None:
        opened = self.alice.dilemma_open(CHANNEL, "Gdzie trzymac cache sesji?", OPTIONS, correlation_id="cache-1")
        self.assertEqual((opened["status"], opened["correlation_id"]), ("ok", "cache-1"))
        self.assertEqual(self.alice.dilemma_status(opened["id"])["state"], "open")

        frames = self.bob.read(channel=CHANNEL)["messages"]
        packet = json.loads(frames[0]["untrusted_message"]["body"])
        self.assertEqual((packet["schema"], packet["options"]), ("office.dilemma/v1", OPTIONS))

        answer = self.bob.dilemma_answer(opened["id"], "B", "wiele procesow wymaga wspolnego stanu")
        self.assertEqual((answer["status"], answer["kind"]), ("ok", "decision"))
        status = self.alice.dilemma_status(opened["id"])
        self.assertEqual((status["state"], status["answer"]["choice"], status["answer"]["from"]),
                         ("answered", "B", "claude-testhost-bob"))

        self.alice.ack(answer["id"], note="przechodze na Redis w module sesji")
        self.assertEqual(self.alice.dilemma_status(opened["id"])["state"], "acknowledged")

    def test_one_open_dilemma_per_sender_and_channel(self) -> None:
        first = self.alice.dilemma_open(CHANNEL, "pytanie 1", OPTIONS, correlation_id="q1")
        second = self.alice.dilemma_open(CHANNEL, "pytanie 2", OPTIONS, correlation_id="q2")
        self.assertEqual(second["code"], "dilemma_already_open")
        other_channel = self.alice.dilemma_open("inny-kanal", "pytanie 2", OPTIONS, correlation_id="q2")
        self.assertEqual(other_channel["status"], "ok")
        self.bob.dilemma_answer(first["id"], "A", "prosciej")
        third = self.alice.dilemma_open(CHANNEL, "pytanie 3", OPTIONS, correlation_id="q3")
        self.assertEqual(third["status"], "ok")

    def test_retry_with_same_correlation_id_does_not_duplicate(self) -> None:
        first = self.alice.dilemma_open(CHANNEL, "pytanie", OPTIONS, correlation_id="retry-1")
        again = self.alice.dilemma_open(CHANNEL, "pytanie", OPTIONS, correlation_id="retry-1")
        self.assertTrue(again["duplicate"])
        self.assertEqual(first["id"], again["id"])
        self.assertEqual(len(self.bob.read(channel=CHANNEL)["messages"]), 1)

    def test_single_answer_no_self_answer_and_valid_choice(self) -> None:
        opened = self.alice.dilemma_open(CHANNEL, "pytanie", OPTIONS, correlation_id="one")
        self.assertEqual(self.alice.dilemma_answer(opened["id"], "A", "sam")["code"], "dilemma_self_answer")
        self.assertEqual(self.bob.dilemma_answer(opened["id"], "Z", "x")["code"], "invalid_dilemma_choice")
        first = self.bob.dilemma_answer(opened["id"], "A", "uzasadnienie")
        retry = self.bob.dilemma_answer(opened["id"], "A", "uzasadnienie")
        self.assertTrue(retry["duplicate"])
        self.assertEqual(retry["id"], first["id"])
        carol = self.office("qwen-testhost-carol")
        self.assertEqual(carol.dilemma_answer(opened["id"], "B", "inaczej")["code"], "dilemma_already_answered")
        self.assertEqual(self.bob.dilemma_answer(opened["id"], "other", "zmiana zdania")["code"], "dedupe_conflict")

    def test_answer_is_not_a_new_dilemma_and_plain_messages_are_not_dilemmas(self) -> None:
        plain = self.alice.send(None, "zwykla wiadomosc", channel=CHANNEL)
        self.assertEqual(self.bob.dilemma_answer(plain["id"], "A", "x")["code"], "not_a_dilemma")
        self.assertEqual(self.bob.dilemma_status(plain["id"])["code"], "not_a_dilemma")

    def test_invalid_packets_are_refused(self) -> None:
        self.assertEqual(self.alice.dilemma_open(CHANNEL, "q", {"A": "tylko jedna"})["code"], "invalid_dilemma")
        self.assertEqual(self.alice.dilemma_open(CHANNEL, "", OPTIONS)["code"], "invalid_dilemma")
        self.assertEqual(self.alice.dilemma_open(CHANNEL, "q", {"z spacja": "x", "B": "y"})["code"], "invalid_dilemma")

    def test_to_copies_the_packet_to_the_architect_inbox(self) -> None:
        self.announce("claude-testhost-bob")
        opened = self.alice.dilemma_open(CHANNEL, "pytanie", OPTIONS, correlation_id="wake-1", to="claude-testhost-bob")
        self.assertEqual(opened["inbox_copy"]["to"], "claude-testhost-bob")
        inbox = self.bob.read()["messages"]
        self.assertEqual(json.loads(inbox[0]["untrusted_message"]["body"])["correlation_id"], "wake-1")


class DilemmaConcurrencyTests(OfficeTestCase):
    def _race(self, calls):
        import threading

        barrier = threading.Barrier(len(calls))
        results = [None] * len(calls)

        def run(index, call):
            barrier.wait()
            results[index] = call()

        threads = [threading.Thread(target=run, args=(i, c)) for i, c in enumerate(calls)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        return results

    def test_concurrent_opens_by_one_sender_leave_one_open_dilemma(self) -> None:
        offices = [self.office("codex-testhost-alice", mutex_timeout_s=10.0) for _ in range(6)]
        calls = [
            (lambda o=o, i=i: o.dilemma_open(CHANNEL, f"pytanie {i}", OPTIONS, correlation_id=f"race-{i}"))
            for i, o in enumerate(offices)
        ]
        results = self._race(calls)
        self.assertEqual(sum(1 for r in results if r["status"] == "ok"), 1)
        self.assertTrue(all(r["code"] == "dilemma_already_open" for r in results if r["status"] != "ok"))

    def test_concurrent_answers_leave_exactly_one_answer(self) -> None:
        opened = self.alice.dilemma_open(CHANNEL, "pytanie", OPTIONS, correlation_id="race-answer")
        answerers = [self.office(f"arch{i}", mutex_timeout_s=10.0) for i in range(6)]
        calls = [
            (lambda o=o: o.dilemma_answer(opened["id"], "A", "uzasadnienie"))
            for o in answerers
        ]
        results = self._race(calls)
        self.assertEqual(sum(1 for r in results if r["status"] == "ok"), 1)
        self.assertTrue(all(r["code"] == "dilemma_already_answered" for r in results if r["status"] != "ok"))
        frames = self.bob.read(channel=CHANNEL)["messages"]
        answers = [f for f in frames if f["untrusted_message"]["kind"] == "decision"]
        self.assertEqual(len(answers), 1)


class DilemmaContractorTests(OfficeTestCase):
    def _server(self, office, peers=()) -> OfficeMcpServer:
        return OfficeMcpServer(office, heartbeat_ttl_s=None, profile="contractor", channels=[CHANNEL], peers=list(peers))

    def test_contractor_can_open_and_read_status_on_task_channel_only(self) -> None:
        worker = self._server(self.alice, peers=["claude-testhost-bob"])
        refused = worker.call_tool("office_dilemma_open", {"channel": "tablica", "question": "q", "options": OPTIONS})
        self.assertEqual(refused["code"], "outside_profile")
        not_peer = worker.call_tool(
            "office_dilemma_open", {"channel": CHANNEL, "question": "q", "options": OPTIONS, "to": "gemini-testhost-mallory"}
        )
        self.assertEqual(not_peer["code"], "outside_profile")
        opened = worker.call_tool("office_dilemma_open", {"channel": CHANNEL, "question": "q", "options": OPTIONS})
        self.assertEqual(opened["status"], "ok")
        architect = self._server(self.bob)
        answer = architect.call_tool(
            "office_dilemma_answer", {"message_id": opened["id"], "choice": "A", "rationale": "powod"}
        )
        self.assertEqual(answer["status"], "ok")
        self.assertEqual(worker.call_tool("office_dilemma_status", {"message_id": opened["id"]})["state"], "answered")
        foreign = self.bob.dilemma_open("tablica", "q", OPTIONS)
        self.assertEqual(
            worker.call_tool("office_dilemma_status", {"message_id": foreign["id"]})["code"], "outside_profile"
        )


if __name__ == "__main__":
    import unittest

    unittest.main()
