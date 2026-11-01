"""Real MCP stdio subprocess and CLI adversarial tests; no external packages."""

import io
import argparse
import json
import os
import subprocess
import sys
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from broker import MAX_BODY, json_bytes
from cli import line_count, logs
from mcp import Client, MCP, operation, serve, tool_specs
from test_broker import HTTPHarness

RUNTIME = Path(__file__).resolve().parents[1]
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def initialize():
    return {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "synthetic-test", "version": "1"}}}


def initialized():
    return {"jsonrpc": "2.0", "method": "notifications/initialized"}


class MCPProcessTests(HTTPHarness):
    def environment(self, sid="native-a"):
        env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1", PYTHONIOENCODING="utf-8", CHROME_RELAY_BROKER="http://127.0.0.1:" + str(self.server.server_address[1]), CHROME_RELAY_SESSION_ID=sid, CHROME_RELAY_TOKEN=self.policy_raw["sessions"][sid])
        env.pop("CHROME_RELAY_CLIENT_FILE", None)
        return env

    def process(self, script, args=(), data=b"", env=None):
        result = subprocess.run([sys.executable, "-B", str(RUNTIME / script), *args], input=data, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, cwd=RUNTIME, env=env or self.environment(),
                                creationflags=NO_WINDOW, timeout=5)
        self.assertEqual(result.stderr, b"")
        for token in ("A" * 32, "B" * 32, "E" * 32):
            self.assertNotIn(token.encode(), result.stdout)
        return result

    def test_actual_stdio_initialize_notifications_tools_list_and_owned_read(self):
        self.attach()
        self.open()
        command_holder = {}

        def executor():
            command = self.command()
            command_holder.update(command)
            self.assertEqual(self.result(command, {"excerpt": "Synthetic ordinary content", "refs": [{"ref": "r1_0", "role": "button", "name": "Continue draft"}], "truncated": False}, 10)[0], 200)

        thread = threading.Thread(target=executor, daemon=True)
        thread.start()
        messages = [initialize(), initialized(), {"jsonrpc": "2.0", "id": "list", "method": "tools/list"},
                    {"jsonrpc": "2.0", "id": "read", "method": "tools/call", "params": {"name": "chrome_read", "arguments": {"tab_id": 10, "request_id": "mcp-read"}}}]
        result = self.process("mcp.py", data=b"\n".join(json_bytes(item) for item in messages) + b"\n")
        thread.join(2)
        self.assertFalse(thread.is_alive())
        self.assertEqual(result.returncode, 0)
        replies = [json.loads(line) for line in result.stdout.splitlines()]
        self.assertEqual(len(replies), 3)  # Notification has no JSON-RPC response.
        self.assertEqual(replies[0]["result"]["protocolVersion"], "2025-06-18")
        self.assertEqual({item["name"] for item in replies[1]["result"]["tools"]}, {item["name"] for item in tool_specs()})
        payload = json.loads(replies[2]["result"]["content"][0]["text"])
        self.assertFalse(replies[2]["result"]["isError"])
        self.assertEqual(payload["request_id"], "mcp-read")
        self.assertEqual(payload["tab_id"], 10)
        self.assertEqual(payload["data"]["excerpt"], "Synthetic ordinary content")
        self.assertRegex(payload["data"]["refs"][0]["ref"], r"^ref_[a-f0-9]{24}$")
        self.assertEqual(command_holder["session_id"], "native-a")
        (self.folder / "mcp-transcript.json").write_text(json.dumps(replies, indent=2), encoding="utf-8")

    def test_unavailable_subprocess_keeps_tools_visible_and_denies_valid_calls(self):
        messages = [initialize(), initialized(),
                    {"jsonrpc": "2.0", "id": "list", "method": "tools/list"},
                    {"jsonrpc": "2.0", "id": "call", "method": "tools/call", "params": {"name": "chrome_attachment", "arguments": {"request_id": "blocked-call"}}},
                    {"jsonrpc": "2.0", "id": "bad", "method": "tools/call", "params": {"name": "chrome_close", "arguments": {}}}]
        env = {"PYTHONDONTWRITEBYTECODE": "1", "PYTHONIOENCODING": "utf-8",
               "CHROME_RELAY_UNAVAILABLE": "broker_unreachable_or_register"}
        for key in ("SYSTEMROOT", "SystemRoot"):
            if key in os.environ:
                env[key] = os.environ[key]
        result = self.process("mcp.py", data=b"\n".join(json_bytes(item) for item in messages) + b"\n", env=env)
        self.assertEqual(result.returncode, 0)
        replies = [json.loads(line) for line in result.stdout.splitlines()]
        names = {item["name"] for item in tool_specs()}
        self.assertEqual(len(names), 8)
        self.assertEqual({item["name"] for item in replies[1]["result"]["tools"]}, names)
        self.assertEqual(replies[1]["result"]["tools"], tool_specs())
        self.assertEqual(replies[2]["result"]["isError"], True)
        payload = json.loads(replies[2]["result"]["content"][0]["text"])
        self.assertEqual(payload["request_id"], "blocked-call")
        self.assertEqual(payload["status"], "denied")
        self.assertEqual(payload["data"]["blocker"], "BLOCKED_MANAGED_BROWSER_UNAVAILABLE")
        self.assertEqual(payload["data"]["detail"], "broker_unreachable_or_register")
        self.assertEqual(replies[3]["error"]["code"], -32602)

    def test_unavailable_reason_validation_and_missing_client_config(self):
        messages = [initialize(), initialized(),
                    {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "chrome_attachment"}}]
        for unavailable, expected in (("BAD reason", "invalid_reason"), (None, "client_config")):
            output = io.BytesIO()
            if unavailable is not None:
                with patch.dict(os.environ, {"CHROME_RELAY_UNAVAILABLE": unavailable}, clear=True), \
                     patch.object(Client, "from_environment", side_effect=AssertionError("unavailable mode loaded client config")):
                    self.assertEqual(serve(io.BytesIO(b"\n".join(json_bytes(item) for item in messages) + b"\n"), output), 0)
            else:
                with patch.dict(os.environ, clear=True):
                    self.assertEqual(serve(io.BytesIO(b"\n".join(json_bytes(item) for item in messages) + b"\n"), output), 0)
            replies = [json.loads(line) for line in output.getvalue().splitlines()]
            payload = json.loads(replies[-1]["result"]["content"][0]["text"])
            self.assertEqual(payload["data"]["blocker"], "BLOCKED_MANAGED_BROWSER_UNAVAILABLE")
            self.assertEqual(payload["data"]["detail"], expected)
            self.assertNotIn("token", json.dumps(payload).casefold())

    def test_mcp_foreign_tab_denial_and_profile_override_has_no_side_effect(self):
        self.attach()
        self.open("native-b", 20)
        messages = [initialize(), initialized(),
                    {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "chrome_close", "arguments": {"tab_id": 20, "request_id": "foreign-close"}}},
                    {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "chrome_read", "arguments": {"tab_id": 20, "profile_id": "other-profile"}}},
                    {"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "Runtime.evaluate", "arguments": {"expression": "alert(1)"}}}]
        result = self.process("mcp.py", data=b"\n".join(json_bytes(item) for item in messages) + b"\n")
        replies = [json.loads(line) for line in result.stdout.splitlines()]
        self.assertTrue(replies[1]["result"]["isError"])
        self.assertEqual(json.loads(replies[1]["result"]["content"][0]["text"])["data"]["reason"], "ownership")
        self.assertEqual(replies[2]["error"]["code"], -32602)
        self.assertEqual(replies[3]["error"]["code"], -32602)
        self.assertEqual(self.broker.owned["native-b"], {20})
        self.assertEqual(self.http("/extension/poll", extension=True)[0], 204)

    def test_stdio_freezes_session_config_before_first_message(self):
        self.attach()
        self.open("native-b", 20)
        config = {"broker_base": self.environment()["CHROME_RELAY_BROKER"], "session_id": "native-a", "token": "A" * 32}
        for configured in (True, False):
            with self.subTest(configured=configured):
                private = self.folder / ("startup-client-" + str(configured) + ".json")
                if configured:
                    private.write_text(json.dumps(config), encoding="utf-8")

                class RotatingInput(io.BytesIO):
                    def readline(self, size=-1):
                        # Simulate config rotation/provisioning after runtime startup.
                        private.write_text(json.dumps({**config, "session_id": "native-b", "token": "B" * 32}), encoding="utf-8")
                        return super().readline(size)

                rid = "startup-bound-" + str(configured)
                messages = [initialize(), initialized(),
                            {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "chrome_read", "arguments": {"tab_id": 20, "request_id": rid}}}]
                output = io.BytesIO()
                with patch.dict(os.environ, {"CHROME_RELAY_CLIENT_FILE": str(private)}):
                    self.assertEqual(serve(RotatingInput(b"\n".join(json_bytes(item) for item in messages) + b"\n"), output), 0)
                replies = [json.loads(line) for line in output.getvalue().splitlines()]
                if configured:
                    response = json.loads(replies[1]["result"]["content"][0]["text"])
                    self.assertEqual(response["status"], "denied")
                    self.assertEqual(response["data"]["reason"], "ownership")
                    self.assertIn(("native-a", rid), self.broker.requests)
                else:
                    response = json.loads(replies[1]["result"]["content"][0]["text"])
                    self.assertTrue(replies[1]["result"]["isError"])
                    self.assertEqual(response["data"]["blocker"], "BLOCKED_MANAGED_BROWSER_UNAVAILABLE")
                    self.assertEqual(response["data"]["detail"], "client_config")
                    self.assertNotIn(("native-a", rid), self.broker.requests)
                self.assertNotIn(("native-b", rid), self.broker.requests)
                self.assertEqual(self.http("/extension/poll", extension=True)[0], 204)

    def test_cli_call_private_config_and_bounded_metadata_diagnostics(self):
        self.attach()
        self.open()
        env = self.environment()
        private = self.folder / "private-client.json"
        private.write_text(json.dumps({"broker_base": env["CHROME_RELAY_BROKER"], "session_id": "native-a", "token": env["CHROME_RELAY_TOKEN"]}), encoding="utf-8")
        env.pop("CHROME_RELAY_TOKEN")
        env["CHROME_RELAY_CLIENT_FILE"] = str(private)
        result = self.process("cli.py", ["call"], json_bytes({"name": "chrome_close", "arguments": {"tab_id": 1, "request_id": "manual-close"}}), env)
        self.assertEqual(result.returncode, 1)
        self.assertEqual(json.loads(result.stdout)["data"]["reason"], "ownership")
        audit = self.folder / "audit.jsonl"
        result = self.process("cli.py", ["logs", "3", "--audit", str(audit)])
        self.assertEqual(result.returncode, 0)
        self.assertLessEqual(len(json.loads(result.stdout)["events"]), 3)
        malicious = self.folder / "not-an-audit.txt"
        malicious.write_text('token=' + "A" * 32 + '\n' + json.dumps({"event": "result", "status": "ok", "op": "chrome_read", "count": 0, "request_id": "A" * 32}) + '\n', encoding="utf-8")
        self.assertEqual(logs(malicious, 100), [])
        marker = self.folder / "shell-injection-marker"
        result = subprocess.run([sys.executable, "-B", str(RUNTIME / "cli.py"), "logs", "3 & echo hacked > " + str(marker), "--audit", str(audit)], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                cwd=RUNTIME, env=self.environment(), creationflags=NO_WINDOW, timeout=5)
        self.assertEqual(result.returncode, 2)
        self.assertFalse(marker.exists())
        self.assertNotIn(b"A" * 32, result.stderr)

    def test_client_never_follows_redirect_or_uses_non_loopback_config(self):
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        calls = []

        class Redirect(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def do_POST(self):
                calls.append(self.path)
                self.send_response(302)
                self.send_header("Location", "http://127.0.0.1:1/leak")
                body = b'{}'
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        server = ThreadingHTTPServer(("127.0.0.1", 0), Redirect)
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        thread.start()
        try:
            client = Client("http://127.0.0.1:" + str(server.server_address[1]), "native-a", "A" * 32)
            self.assertEqual(client.call("chrome_attachment", {"request_id": "redirect-test"})["status"], "denied")
            self.assertEqual(calls, ["/mcp"])
        finally:
            server.shutdown()
            server.server_close()
            thread.join(2)
        for base in ("https://example.test", "http://localhost:9333", "http://127.0.0.1:9333/path", "http://token@127.0.0.1:9333", "http://127.0.0.1:9333?token=secret"):
            with self.assertRaises(ValueError):
                Client(base, "native-a", "A" * 32)

    def test_subprocess_invalid_utf8_oversize_and_duplicate_json_are_safe(self):
        result = self.process("mcp.py", data=b'\xff\n{"jsonrpc":"2.0","method":"ping","id":1,"id":2}\n')
        self.assertEqual(result.returncode, 0)
        self.assertEqual([json.loads(line)["error"]["code"] for line in result.stdout.splitlines()], [-32700, -32700])
        result = self.process("mcp.py", data=b"x" * (MAX_BODY + 5))
        self.assertEqual(result.returncode, 1)
        self.assertEqual(json.loads(result.stdout)["error"]["code"], -32700)


class ProtocolTests(unittest.TestCase):
    def test_standard_handshake_strict_params_and_notification_silence(self):
        server = MCP()
        self.assertEqual(server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})["error"]["code"], -32000)
        self.assertIn("result", server.handle(initialize()))
        self.assertIsNone(server.handle(initialized()))
        self.assertIsNone(server.handle({"jsonrpc": "2.0", "method": "notifications/cancelled", "params": {"requestId": 2}}))
        self.assertEqual(server.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {"profile_id": "other"}})["error"]["code"], -32602)
        self.assertEqual(server.handle({"jsonrpc": "2.0", "id": 3, "method": "missing"})["error"]["code"], -32601)
        self.assertEqual(server.handle([initialize()])["error"]["code"], -32600)
        self.assertEqual(server.handle({"jsonrpc": "2.0", "id": True, "method": "ping"})["error"]["code"], -32600)

    def test_tool_validation_is_strict_and_uses_standard_schema(self):
        for item in tool_specs():
            self.assertFalse(item["inputSchema"]["additionalProperties"])
            self.assertNotIn("profile_id", item["inputSchema"]["properties"])
            self.assertNotIn("session_id", item["inputSchema"]["properties"])
        with self.assertRaises(ValueError):
            operation("chrome_read", {"tab_id": True})
        with self.assertRaises(ValueError):
            operation("chrome_wait", {"tab_id": 10})
        with self.assertRaises(ValueError):
            operation("chrome_type", {"tab_id": 10, "ref": "ref_" + "a" * 24, "text": "\U0001f600" * 501})
        self.assertEqual(operation("chrome_attachment", {"request_id": "stable"}), {"request_id": "stable", "action": "chrome_attachment", "args": {}})

    def test_unavailable_handle_validates_before_returning_blocker(self):
        server = MCP(None, "harness")
        self.assertIn("result", server.handle(initialize()))
        self.assertIsNone(server.handle(initialized()))
        blocked = server.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                                 "params": {"name": "chrome_attachment", "arguments": {"request_id": "rid"}}})
        self.assertTrue(blocked["result"]["isError"])
        payload = json.loads(blocked["result"]["content"][0]["text"])
        self.assertEqual(payload["request_id"], "rid")
        self.assertEqual(payload["data"]["detail"], "harness")
        bad = server.handle({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                             "params": {"name": "chrome_close", "arguments": {}}})
        self.assertEqual(bad["error"]["code"], -32602)

    def test_bounded_log_count_rejects_shell_values(self):
        for value in ("0", "-1", "1001", "2;whoami", "2&calc", "2\n3", "1.0"):
            with self.assertRaises(argparse.ArgumentTypeError):
                line_count(value)
        self.assertEqual(line_count("1000"), 1000)


if __name__ == "__main__":
    unittest.main()
