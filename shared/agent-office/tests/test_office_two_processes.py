"""Criterion 1: two agent processes on one machine exchange messages both ways over stdio MCP."""

from __future__ import annotations

import time

from office_support import McpClient, OfficeTestCase

from coordfs.office_mcp import TOOLS


class TwoProcessExchangeTests(OfficeTestCase):
    def test_two_mcp_servers_exchange_messages_both_ways(self) -> None:
        alice = McpClient("codex-testhost-proc-alice", self.cli_env(), ttl_s=5)
        bob = McpClient("codex-testhost-proc-bob", self.cli_env(), ttl_s=5)
        try:
            init = alice.handshake()
            self.assertEqual(init["result"]["serverInfo"]["name"], "agent-office")
            self.assertEqual(init["result"]["protocolVersion"], "2025-06-18")
            bob.handshake()
            self.assertNotEqual(alice.pid, bob.pid)

            tools = alice.request("tools/list")["result"]["tools"]
            self.assertEqual(
                [tool["name"] for tool in tools],
                [tool["name"] for tool in TOOLS],
            )

            who = bob.call("office_who")["structuredContent"]
            self.assertEqual([item["agent"] for item in who["online"]], ["codex-testhost-proc-alice", "codex-testhost-proc-bob"])

            bob.send_request("tools/call", {"name": "office_wait", "arguments": {"timeout_s": 10}})
            time.sleep(0.2)
            sent_at = time.monotonic()
            sent = alice.call("office_send", {"to": "codex-testhost-proc-bob", "body": "ping from alice", "kind": "question"})
            self.assertEqual(sent["structuredContent"]["code"], "sent")
            woke = bob.receive()["result"]["structuredContent"]
            self.assertEqual(woke["code"], "message")
            self.assertLess(time.monotonic() - sent_at, 1.0)

            inbox = bob.call("office_read")["structuredContent"]
            self.assertEqual(inbox["count"], 1)
            first = inbox["messages"][0]["untrusted_message"]
            self.assertEqual(first["from"], "codex-testhost-proc-alice")
            self.assertEqual(first["to"], "codex-testhost-proc-bob")
            self.assertEqual(first["body"], "ping from alice")

            reply = bob.call(
                "office_send",
                {"to": "codex-testhost-proc-alice", "body": "pong from bob", "reply_to": first["id"]},
            )["structuredContent"]
            self.assertEqual(reply["code"], "sent")

            woke = alice.call("office_wait", {"timeout_s": 5})["structuredContent"]
            self.assertEqual(woke["code"], "message")
            back = alice.call("office_read")["structuredContent"]
            self.assertEqual(back["count"], 1)
            second = back["messages"][0]["untrusted_message"]
            self.assertEqual(second["from"], "codex-testhost-proc-bob")
            self.assertEqual(second["reply_to"], first["id"])
            self.assertEqual(second["trust"], "untrusted")
        finally:
            self.assertEqual(alice.close(), 0)
            self.assertEqual(bob.close(), 0)
