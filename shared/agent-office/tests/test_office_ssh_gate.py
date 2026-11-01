"""T6: synthetic forced-command transport tests, with no SSH connection."""

from __future__ import annotations

import io
import json
from pathlib import Path

from office_support import OfficeTestCase

from coordfs.office import Office, SERVER_ONLY_FIELDS, parse_envelope
from coordfs.office_ssh_gate import MAX_HEADER_BYTES, OPS, run
from coordfs import office_ssh_gate


class OfficeSshGateTests(OfficeTestCase):
    def call(self, op: str = "channels", args: dict | None = None, *,
             identity: dict | None = None, principal: str = "ws-office",
             tail: bytes = b"", environ: dict | None = None) -> tuple[int, dict]:
        header = {"v": 1, "op": op, "identity": identity or {"agent_id": "codex-testhost-alice"},
                  "args": {} if args is None else args}
        return self.raw(json.dumps(header).encode("utf-8") + b"\n" + tail,
                        principal=principal, environ=environ)

    def raw(self, data: bytes, *, principal: str = "ws-office",
            environ: dict | None = None) -> tuple[int, dict]:
        stdout = io.BytesIO()
        code = run(io.BytesIO(data), stdout, principal=principal, home=self.home,
                   environ={} if environ is None else environ)
        return code, json.loads(stdout.getvalue())

    def test_refused_frames_and_unknown_operations_never_create_home(self) -> None:
        good = {"v": 1, "op": "send", "identity": {"agent_id": "codex-testhost-alice"},
                "args": {"channel": "ops", "body": "test"}}
        frames = [
            b"{bad json\n", b"\xff\n", b"{}\n", b"[]\n",
            b" " * (MAX_HEADER_BYTES + 1) + b"\n",
            json.dumps(good).encode("utf-8"),
            json.dumps(good).encode("utf-8") + b"\n{}\n",
            json.dumps(good).encode("utf-8") + b"\n\x00",
            b'{"v":1,"v":1,"op":"send","identity":{"agent_id":"codex-testhost-alice"},"args":{"channel":"ops","body":"x"}}\n',
            b'{"v":1,"op":"send","identity":{"agent_id":"codex-testhost-alice"},"args":{"body":"x"}} {}\n',
        ]
        for raw in frames:
            with self.subTest(raw=raw[:50]):
                code, result = self.raw(raw)
                self.assertEqual(code, 1)
                self.assertEqual(result["status"], "error")
                self.assertFalse(self.home.exists())
        for op in ("unknown", "wake-enroll", "wake-verify", "wake-relay-expect",
                   "wake-relay-reconcile", "wake-stop", "wake-start", "wake-schedule-add"):
            with self.subTest(op=op):
                code, result = self.call(op)
                self.assertEqual(code, 1)
                self.assertEqual(result["code"], "unknown_op")
                self.assertFalse(self.home.exists())

    def test_injected_fields_types_principal_and_ssh_command_are_refused(self) -> None:
        cases = [
            ("send", {"channel": "ops", "body": "x", "from": "owner"}, None),
            ("send", {"channel": "ops", "body": "x", "transport_principal": "hermes"}, None),
            ("send", {"channel": "ops", "body": "x", "owner_confirm": True}, None),
            ("send", {"channel": "ops", "body": "x", "refs": ["ok", 7]}, None),
            ("wait", {"timeout_s": 241}, None),
            ("wait", {"timeout_s": True}, None),
            ("wake-relay-watch", {"binding_id": ["relay-canary-s"], "timeout_s": 0}, None),
            ("read", {"limit": True}, None),
            ("wake-relay-claim", {"dispatch_id": "x", "relay_agent": "r", "owner_confirm": True}, None),
            ("send", {"channel": "ops", "body": "x"}, {"agent_id": "codex-testhost-alice", "from": "root"}),
            ("send", {"channel": "ops", "body": "x"}, {"harness": "codex", "session_id": "s",
                                                            "principal": "hermes"}),
        ]
        for op, args, identity in cases:
            with self.subTest(op=op, args=args, identity=identity):
                code, result = self.call(op, args, identity=identity)
                self.assertEqual(code, 1)
                self.assertEqual(result["status"], "error")
                self.assertFalse(self.home.exists())
        code, result = self.call("send", {"channel": "ops", "body": "x"}, principal="unknown")
        self.assertEqual((code, result["code"]), (1, "unknown_principal"))
        self.assertFalse(self.home.exists())
        code, result = self.call("send", {"channel": "ops", "body": "x"}, principal="hermes")
        self.assertEqual((code, result["code"]), (1, "identity_mismatch"))
        self.assertFalse(self.home.exists())
        code, result = self.call("wake-relay-claim", {"dispatch_id": "d", "relay_agent": "other"})
        self.assertEqual((code, result["code"]), (1, "identity_mismatch"))
        self.assertFalse(self.home.exists())
        code, result = self.call("send", {"channel": "ops", "body": "x"},
                                 environ={"SSH_ORIGINAL_COMMAND": ""})
        self.assertEqual((code, result["code"]), (1, "ssh_command_refused"))
        self.assertFalse(self.home.exists())

    def test_all_documented_non_owner_ops_are_explicitly_allowed(self) -> None:
        expected = {"presence", "read", "send", "ack", "wait", "who", "channels", "delivery",
                    "wake-expect", "wake-consume", "wake-close", "wake-relay-watch",
                    "wake-relay-pending", "wake-relay-claim", "wake-relay-receipt",
                    "wake-relay-observe", "wake-relay-defer-busy", "wake-list",
                    "wake-task-cancel", "wake-correlations", "wake-pull-disarm",
                    "wake-stop-status", "wake-ledger", "wake-cli-pending",
                    "wake-cli-claim", "wake-cli-start", "wake-cli-receipt",
                    "wake-relay-native-begin", "wake-relay-native-unsubmitted", "wake-relay-ack",
                    "wake-desktop-input-claim", "wake-desktop-input-release",
                    "tool-call", "mcp"}
        self.assertEqual(OPS, expected)
        self.assertIn("transport_principal", SERVER_ONLY_FIELDS)

    def test_shell_metacharacters_are_message_data_and_principal_is_server_owned(self) -> None:
        body = "literal ; $(touch /tmp/should-not-run) | `whoami` && < >"
        code, sent = self.call("send", {"channel": "ops", "body": body})
        self.assertEqual(code, 0, sent)
        self.assertEqual(sent["status"], "ok")
        code, read = self.call("read", {"channel": "ops", "peek": True},
                               identity={"agent_id": "claude-testhost-bob"})
        self.assertEqual(code, 0, read)
        envelope = read["messages"][0]["untrusted_message"]
        self.assertEqual(envelope["body"], body)
        self.assertEqual(envelope["from"], "codex-testhost-alice")
        self.assertEqual(envelope["transport_principal"], "ws-office")

    def test_local_legacy_envelope_reads_and_dedupe_separates_principals(self) -> None:
        self.alice.send(None, "legacy", channel="ops", dedupe_key="same")
        code, read = self.call("read", {"channel": "ops", "peek": True},
                               identity={"agent_id": "claude-testhost-bob"})
        self.assertEqual(code, 0, read)
        self.assertNotIn("transport_principal", read["messages"][0]["untrusted_message"])
        code, result = self.call("send", {"channel": "ops", "body": "legacy", "dedupe_key": "same"})
        self.assertEqual((code, result["code"]), (1, "dedupe_conflict"))
        # The stored legacy line remains readable without the optional field.
        self.assertIsNotNone(parse_envelope(json.dumps(read["messages"][0]["untrusted_message"])))

    def test_mcp_receives_binary_stream_after_header(self) -> None:
        header = {"v": 1, "op": "mcp", "identity": {"agent_id": "codex-testhost-alice"}, "args": {}}
        request = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
        raw = json.dumps(header).encode() + b"\n" + json.dumps(request).encode() + b"\n"
        code, response = self.raw(raw)
        self.assertEqual(code, 0, response)
        names = {tool["name"] for tool in response["result"]["tools"]}
        self.assertIn("office_send", names)
        self.assertFalse(any("owner" in name or "admin" in name for name in names))

    def test_mcp_new_tool_requires_explicit_remote_review(self) -> None:
        original = office_ssh_gate.TOOLS
        office_ssh_gate.TOOLS = [*original, {"name": "office_secret_rotate"}]
        try:
            code, response = self.call("mcp")
            self.assertEqual((code, response["code"]), (1, "mcp_tools_refused"))
        finally:
            office_ssh_gate.TOOLS = original

    def test_contractor_mcp_profile_limits_remote_tools(self) -> None:
        header = {"v": 1, "op": "mcp", "identity": {"agent_id": "codex-testhost-alice"},
                  "args": {"profile": "contractor", "channels": ["task-office"],
                           "peers": ["claude-testhost-bob"], "heartbeat_ttl_s": 0,
                           "contractor_limits": {"read_page": 2}}}
        request = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
        raw = json.dumps(header).encode() + b"\n" + json.dumps(request).encode() + b"\n"
        code, response = self.raw(raw)
        self.assertEqual(code, 0, response)
        names = {tool["name"] for tool in response["result"]["tools"]}
        self.assertIn("office_read", names)
        self.assertNotIn("office_work_register", names)

    def test_mcp_profile_limits_are_validated_before_state_creation(self) -> None:
        for args in (
            {"profile": "contractor"},
            {"profile": "full", "contractor_limits": {"read_page": 2}},
            {"profile": "contractor", "channels": ["task"], "contractor_limits": {"read_page": 0}},
            {"profile": "contractor", "channels": ["task"], "contractor_limits": {"unknown": 1}},
        ):
            with self.subTest(args=args):
                code, result = self.call("mcp", args)
                self.assertEqual((code, result["code"]), (1, "invalid_args"))
                self.assertFalse(self.home.exists())

    def test_remote_presence_keeps_declared_host_and_session_metadata(self) -> None:
        code, result = self.call("presence", identity={"harness": "codex", "host": "test-host",
                                                       "session_id": "session-a"})
        self.assertEqual(code, 0, result)
        self.assertEqual(result["agent"], "codex-test-host-session-a")
        self.assertEqual(result["meta"]["host"], "test-host")
        self.assertEqual(result["meta"]["session"], "session-a")

    def test_project_messages_keep_principal_and_cannot_override_remote_sender(self) -> None:
        office = Office(harness="hermes", host="test-server", session_id="s1", home=self.home,
                        transport_principal="hermes")
        checkout = Path(self.tmp.name) / "checkout"
        checkout.mkdir()
        registered = office.project_register("office-w2", str(checkout))
        self.assertEqual(registered["status"], "ok", registered)
        sent = office.project_send_channel("office-w2", "task", "thread", "run", "ops", "test")
        self.assertEqual(sent["status"], "ok", sent)
        read = office.project_read_channel("office-w2", "task", "thread", "run", "ops", peek=True)
        self.assertEqual(read["messages"][0]["untrusted_message"]["transport_principal"], "hermes")
        refused = office.project_send_channel("office-w2", "task", "thread", "run", "ops", "spoof",
                                              harness="codex", host="test-host", session_id="other")
        self.assertEqual(refused["code"], "identity_mismatch", refused)


if __name__ == "__main__":
    import unittest
    unittest.main()
