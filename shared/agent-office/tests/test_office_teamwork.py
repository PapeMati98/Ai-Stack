"""Mid-task collaboration primitives: retry-safe send, explicit ACK, delivery state and
the contractor MCP profile for delegated CLIs without a shell."""

from __future__ import annotations

import io
import json

from office_support import OfficeTestCase

from coordfs.office_mcp import CONTRACTOR_TOOLS, OfficeMcpServer

CHANNEL = "task-canary"


class RetrySafeSendTests(OfficeTestCase):
    def test_same_key_and_payload_returns_the_stored_message_once(self) -> None:
        first = self.alice.send(None, "kontrakt v2", kind="decision", channel=CHANNEL, dedupe_key="contract-v2")
        again = self.alice.send(None, "kontrakt v2", kind="decision", channel=CHANNEL, dedupe_key="contract-v2")
        self.assertEqual(first["status"], "ok")
        self.assertFalse(first["duplicate"])
        self.assertTrue(again["duplicate"])
        self.assertEqual(again["id"], first["id"])
        self.assertEqual(again["waiters_woken"], 0)
        read = self.bob.read(channel=CHANNEL)
        self.assertEqual([m["untrusted_message"]["id"] for m in read["messages"]], [first["id"]])

    def test_same_key_with_other_payload_is_a_conflict(self) -> None:
        self.alice.send(None, "kontrakt v2", channel=CHANNEL, dedupe_key="contract-v2")
        conflict = self.alice.send(None, "kontrakt v3", channel=CHANNEL, dedupe_key="contract-v2")
        self.assertEqual((conflict["status"], conflict["code"]), ("error", "dedupe_conflict"))

    def test_key_is_per_sender_and_invalid_keys_are_refused(self) -> None:
        a = self.alice.send(None, "x", channel=CHANNEL, dedupe_key="k1")
        b = self.bob.send(None, "x", channel=CHANNEL, dedupe_key="k1")
        self.assertNotEqual(a["id"], b["id"])
        self.assertFalse(b["duplicate"])
        bad = self.alice.send(None, "x", channel=CHANNEL, dedupe_key="spacja w kluczu")
        self.assertEqual(bad["code"], "invalid_dedupe_key")

    def test_send_without_key_keeps_the_old_payload(self) -> None:
        sent = self.alice.send(None, "x", channel=CHANNEL)
        self.assertNotIn("duplicate", sent)


class AckAndDeliveryTests(OfficeTestCase):
    def test_written_received_acknowledged_are_distinct_states(self) -> None:
        post = self.alice.send(None, "kontrakt v2", kind="decision", channel=CHANNEL)
        state = self.alice.delivery(post["id"], channel=CHANNEL)
        self.assertEqual((state["state"], state["received_by"], state["acknowledged_by"]), ("written", [], []))

        self.bob.read(channel=CHANNEL, peek=True)
        self.assertEqual(self.alice.delivery(post["id"], channel=CHANNEL)["state"], "written")
        self.bob.read(channel=CHANNEL, kind="decision")
        self.assertEqual(self.alice.delivery(post["id"], channel=CHANNEL)["state"], "written")

        self.bob.read(channel=CHANNEL)
        received = self.alice.delivery(post["id"], channel=CHANNEL)
        self.assertEqual((received["state"], received["received_by"]), ("received", ["claude-testhost-bob"]))

        ack = self.bob.ack(post["id"], note="zastosowalem v2 w consumer_b")
        self.assertEqual((ack["status"], ack["ack_of"], ack["channel"]), ("ok", post["id"], CHANNEL))
        done = self.alice.delivery(post["id"], channel=CHANNEL)
        self.assertEqual((done["state"], done["acknowledged_by"]), ("acknowledged", ["claude-testhost-bob"]))
        self.assertEqual(done["acks"][0]["ack_id"], ack["id"])

    def test_ack_is_idempotent_and_visible_to_the_author(self) -> None:
        post = self.alice.send(None, "kontrakt v2", channel=CHANNEL)
        self.alice.read(channel=CHANNEL)
        first = self.bob.ack(post["id"])
        second = self.bob.ack(post["id"])
        self.assertTrue(second["duplicate"])
        self.assertEqual(first["id"], second["id"])
        frames = self.alice.read(channel=CHANNEL)["messages"]
        self.assertEqual(len(frames), 1)
        message = frames[0]["untrusted_message"]
        self.assertEqual((message["ack_of"], message["reply_to"], message["from"]), (post["id"], post["id"], "claude-testhost-bob"))

    def test_ack_of_own_or_unknown_message_is_refused(self) -> None:
        post = self.alice.send(None, "x", channel=CHANNEL)
        self.assertEqual(self.alice.ack(post["id"])["code"], "ack_own_message")
        self.assertEqual(self.bob.ack("0" * 32)["code"], "unknown_message")

    def test_inbox_ack_goes_back_to_the_sender(self) -> None:
        self.announce("codex-testhost-alice", "claude-testhost-bob")
        question = self.alice.send("claude-testhost-bob", "czy przyjmujesz v2?", kind="question")
        ack = self.bob.ack(question["id"], note="tak")
        self.assertEqual(ack["to"], "codex-testhost-alice")
        inbox = self.alice.read()["messages"]
        self.assertEqual(inbox[-1]["untrusted_message"]["ack_of"], question["id"])

    def test_message_after_first_read_stays_pending(self) -> None:
        self.alice.send(None, "kontrakt v1", channel=CHANNEL)
        self.assertEqual(len(self.bob.read(channel=CHANNEL)["messages"]), 1)
        late = self.alice.send(None, "kontrakt v2", kind="decision", channel=CHANNEL)
        again = self.bob.read(channel=CHANNEL)["messages"]
        self.assertEqual([m["untrusted_message"]["id"] for m in again], [late["id"]])


class ContractorProfileTests(OfficeTestCase):
    def _server(self, **kwargs) -> OfficeMcpServer:
        options = {"heartbeat_ttl_s": None, "profile": "contractor", "channels": [CHANNEL], "peers": ["codex-testhost-alice"]}
        options.update(kwargs)
        return OfficeMcpServer(self.bob, **options)

    def _serve(self, server: OfficeMcpServer, lines: list[dict]) -> list[dict]:
        raw = b"".join(json.dumps(line).encode("utf-8") + b"\n" for line in lines)
        stdout = io.BytesIO()
        server.serve(io.BytesIO(raw), stdout)
        return [json.loads(line) for line in stdout.getvalue().decode("utf-8").splitlines() if line]

    def test_tools_list_is_the_minimal_surface(self) -> None:
        responses = self._serve(self._server(), [{"jsonrpc": "2.0", "id": 1, "method": "tools/list"}])
        names = {tool["name"] for tool in responses[0]["result"]["tools"]}
        self.assertEqual(names, set(CONTRACTOR_TOOLS))
        self.assertTrue({"office_read", "office_send", "office_presence", "office_wait", "office_ack"} <= names)

    def test_tools_outside_the_profile_are_unknown(self) -> None:
        responses = self._serve(self._server(), [
            {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "office_who", "arguments": {}}},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
             "params": {"name": "office_project_register", "arguments": {"project_id": "p", "checkout": "C:/x"}}},
            {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "office_ask",
             "arguments": {"to": "codex-testhost-alice", "body": "x", "timeout_s": 0}}},
        ])
        by_id = {item["id"]: item for item in responses}
        self.assertEqual(by_id[1]["error"]["code"], -32602)
        self.assertEqual(by_id[2]["error"]["code"], -32602)
        # A contractor result carries its payload once, as JSON text (no structuredContent).
        self.assertEqual(json.loads(by_id[3]["result"]["content"][0]["text"])["code"], "outside_profile")

    def test_channel_and_peer_allowlists_are_enforced(self) -> None:
        server = self._server()
        self.assertEqual(server.call_tool("office_read", {"channel": "tablica"})["code"], "outside_profile")
        self.assertEqual(server.call_tool("office_send", {"channel": "tablica", "body": "x"})["code"],
                         "outside_profile")
        self.assertEqual(server.call_tool("office_send", {"to": "gemini-testhost-mallory", "body": "x"})["code"], "outside_profile")
        self.assertEqual(server.call_tool("office_delivery", {"message_id": "0" * 32, "channel": "tablica"})["code"],
                         "outside_profile")
        self.assertEqual(server.call_tool("office_wait", {"channel": "tablica", "timeout_s": 0})["code"],
                         "outside_profile")
        self.assertEqual(server.call_tool("office_send", {"channel": CHANNEL, "body": "ok"})["status"], "ok")
        self.announce("codex-testhost-alice")
        self.assertEqual(server.call_tool("office_send", {"to": "codex-testhost-alice", "body": "ok"})["status"], "ok")
        self.assertEqual(server.call_tool("office_read", {})["status"], "ok")

    def test_hash_prefixed_to_is_checked_as_a_channel(self) -> None:
        server = self._server()
        refused = server.call_tool("office_send", {"to": "#alice", "body": "x"})
        self.assertEqual(refused["code"], "outside_profile")
        self.assertNotIn("codex-testhost-alice", [c["name"] for c in self.alice.channels()["channels"]])
        self.assertEqual(server.call_tool("office_send", {"to": "#" + CHANNEL, "body": "ok"})["status"], "ok")

    def test_blocking_wait_through_serve_enforces_the_profile(self) -> None:
        responses = self._serve(self._server(), [
            {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
             "params": {"name": "office_wait", "arguments": {"channel": "tablica", "timeout_s": 0}}},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
             "params": {"name": "office_wait", "arguments": {"channel": CHANNEL, "timeout_s": 0}}},
        ])
        by_id = {item["id"]: json.loads(item["result"]["content"][0]["text"]) for item in responses}
        self.assertEqual(by_id[1]["code"], "outside_profile")
        self.assertEqual(by_id[2]["status"], "ok")

    def test_reply_or_ack_to_a_foreign_channel_is_refused(self) -> None:
        foreign = self.alice.send(None, "inny kanal", channel="tablica")
        server = self._server()
        self.assertEqual(server.call_tool("office_send", {"reply_to": foreign["id"], "body": "x"})["code"],
                         "outside_profile")
        self.assertEqual(server.call_tool("office_ack", {"message_id": foreign["id"]})["code"], "outside_profile")

    def test_contractor_reads_acks_and_delivery_through_mcp(self) -> None:
        post = self.alice.send(None, "kontrakt v2", kind="decision", channel=CHANNEL, dedupe_key="v2")
        server = self._server()
        read = server.call_tool("office_read", {"channel": CHANNEL})
        self.assertEqual(read["messages"][0]["untrusted_message"]["id"], post["id"])
        ack = server.call_tool("office_ack", {"message_id": post["id"], "note": "zastosowane"})
        self.assertEqual(ack["status"], "ok")
        state = server.call_tool("office_delivery", {"message_id": post["id"], "channel": CHANNEL})
        self.assertEqual((state["state"], state["acknowledged_by"]), ("acknowledged", ["claude-testhost-bob"]))

    def test_full_profile_is_unchanged_and_contractor_needs_a_channel(self) -> None:
        full = OfficeMcpServer(self.bob, heartbeat_ttl_s=None)
        self.assertIsNone(full._profile_violation("office_who", {}))
        with self.assertRaises(ValueError):
            OfficeMcpServer(self.bob, heartbeat_ttl_s=None, profile="contractor", channels=[])


if __name__ == "__main__":
    import unittest

    unittest.main()
