"""In-process JSON-RPC behaviour of the stdio MCP server."""

from __future__ import annotations

import io
import json
import threading
from unittest import mock

from office_support import OfficeTestCase

from coordfs.office_mcp import TOOLS, OfficeMcpServer


class McpProtocolTests(OfficeTestCase):
    def _server(self) -> OfficeMcpServer:
        return OfficeMcpServer(self.alice, heartbeat_ttl_s=None)

    def _serve(self, server: OfficeMcpServer, lines: list[object]) -> list[dict]:
        raw = b"".join((line if isinstance(line, bytes) else json.dumps(line).encode("utf-8")) + b"\n" for line in lines)
        stdout = io.BytesIO()
        server.serve(io.BytesIO(raw), stdout)
        return [json.loads(line) for line in stdout.getvalue().decode("utf-8").splitlines() if line]

    def test_initialize_tools_list_and_ping(self) -> None:
        responses = self._serve(
            self._server(),
            [
                {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2024-11-05"}},
                {"jsonrpc": "2.0", "method": "notifications/initialized"},
                {"jsonrpc": "2.0", "id": 2, "method": "ping"},
                {"jsonrpc": "2.0", "id": 3, "method": "tools/list"},
            ],
        )
        self.assertEqual([item["id"] for item in responses], [1, 2, 3])
        self.assertEqual(responses[0]["result"]["protocolVersion"], "2024-11-05")
        self.assertIn("tools", responses[0]["result"]["capabilities"])
        self.assertEqual(responses[1]["result"], {})
        tools = responses[2]["result"]["tools"]
        self.assertEqual({tool["name"] for tool in tools}, {tool["name"] for tool in TOOLS})
        for tool in tools:
            self.assertFalse(tool["inputSchema"].get("additionalProperties", True), tool["name"])
        self.assertEqual(tools, list(TOOLS))

    def test_errors_for_parse_unknown_method_and_bad_params(self) -> None:
        responses = self._serve(
            self._server(),
            [
                b"{not json",
                {"jsonrpc": "2.0", "id": 5, "method": "resources/list"},
                {"jsonrpc": "2.0", "id": 6, "method": "tools/call", "params": {"name": "office_hack"}},
                {"jsonrpc": "2.0", "id": 7, "method": "tools/call", "params": {"name": "office_who", "arguments": []}},
                {"jsonrpc": "1.0", "id": 8, "method": "ping"},
            ],
        )
        codes = [(item.get("id"), item["error"]["code"]) for item in responses]
        self.assertEqual(codes, [(None, -32700), (5, -32601), (6, -32602), (7, -32602), (None, -32600)])

    def test_unknown_arguments_are_rejected_and_flagged_as_error(self) -> None:
        server = self._server()
        response = server.handle_message(
            {"jsonrpc": "2.0", "id": 9, "method": "tools/call", "params": {"name": "office_read", "arguments": {"execute": True}}}
        )
        self.assertTrue(response["result"]["isError"])
        self.assertEqual(response["result"]["structuredContent"]["code"], "invalid_params")
        self.assertEqual(response["result"]["structuredContent"]["fields"], ["execute"])

    def test_degraded_office_is_not_reported_as_tool_error(self) -> None:
        blocker = self.home.parent / "blocker-file"
        blocker.write_text("x", encoding="utf-8")
        server = OfficeMcpServer(self.office("codex-testhost-alice", home=blocker, mutex_timeout_s=0.2), heartbeat_ttl_s=None)
        response = server.handle_message(
            {"jsonrpc": "2.0", "id": 10, "method": "tools/call", "params": {"name": "office_who"}}
        )
        self.assertFalse(response["result"]["isError"])
        self.assertEqual(response["result"]["structuredContent"]["status"], "degraded")

    def test_batch_requests_and_notifications(self) -> None:
        responses = self._serve(
            self._server(),
            [[{"jsonrpc": "2.0", "id": 1, "method": "ping"}, {"jsonrpc": "2.0", "method": "notifications/initialized"}]],
        )
        self.assertEqual(responses, [[{"jsonrpc": "2.0", "id": 1, "result": {}}]])

    def test_blocking_notifications_invalid_ids_and_batches_never_dispatch(self) -> None:
        server = self._server()
        messages = [
            {"jsonrpc": "2.0", "method": "tools/call", "params": {"name": "office_wait", "arguments": {}}},
            {"jsonrpc": "2.0", "method": "tools/call", "params": {"name": "office_ask", "arguments": {"body": "q"}}},
            {"jsonrpc": "2.0", "id": [], "method": "tools/call", "params": {"name": "office_wait", "arguments": {}}},
            [{"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "office_ask", "arguments": {"body": "q"}}}],
        ]
        with mock.patch.object(server.office, "wait", side_effect=AssertionError("wait dispatched")), \
             mock.patch.object(server.office, "ask", side_effect=AssertionError("ask dispatched")):
            responses = self._serve(server, messages)
        self.assertEqual([response.get("id") if isinstance(response, dict) else response[0].get("id")
                          for response in responses], [None, 3])

    def test_ask_uses_worker_so_ping_is_emitted_before_ask_finishes(self) -> None:
        server = self._server()
        entered = threading.Event()
        def blocking_ask(*_args, cancel=None, progress=None, **_kwargs):
            entered.set()
            cancel.wait(2)
            return {"status": "ok", "code": "cancelled"}
        lines = [
            {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "office_ask", "arguments": {"body": "q", "channel": "team"}}},
            {"jsonrpc": "2.0", "id": 2, "method": "ping"},
        ]
        with mock.patch.object(server.office, "ask", side_effect=blocking_ask):
            responses = self._serve(server, lines)
        self.assertTrue(entered.is_set())
        self.assertEqual([response["id"] for response in responses], [2, 1])

    def test_presence_tool_and_send_round_trip_through_server(self) -> None:
        server = self._server()
        self.announce("claude-testhost-bob")
        presence = server.call_tool("office_presence", {"ttl_s": 30, "status": "reviewing"})
        self.assertEqual(presence["code"], "present")
        sent = server.call_tool(
            "office_send",
            {"to": "claude-testhost-bob", "body": "via mcp", "kind": "handoff", "refs": ["docs/mcp-handoff.md"]},
        )
        self.assertEqual(sent["code"], "sent")
        who = server.call_tool("office_who")
        own = next(item for item in who["online"] if item["agent"] == self.ALICE_ID)
        self.assertEqual(own["status"], "reviewing")
        self.assertEqual(self.bob.read()["messages"][0]["untrusted_message"]["kind"], "handoff")
