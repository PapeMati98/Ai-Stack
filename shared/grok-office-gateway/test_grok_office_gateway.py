#!/usr/bin/env python3
"""Unit tests for Grok Office Gateway and Client."""

from __future__ import annotations

import hashlib
import http.client
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request

# Ensure the module directory is in sys.path
_MODULE_DIR = Path(__file__).parent.resolve()
if str(_MODULE_DIR) not in sys.path:
    sys.path.insert(0, str(_MODULE_DIR))

from grok_office_gateway import (
    GatewayConfig,
    GrokOfficeHTTPServer,
    GrokOfficeHandler,
    OfficeBackend,
    create_server,
    hash_token_stdin,
)
from grok_office_client import run_client


def find_office_runtime() -> Path | None:
    """Find real coordfs package path if available."""
    env_rt = os.environ.get("OFFICE_RUNTIME")
    if env_rt and (Path(env_rt) / "coordfs").exists():
        return Path(env_rt)
    home_rt = Path.home() / ".agents" / "runtime" / "agent-office"
    if (home_rt / "coordfs").exists():
        return home_rt
    repo_rt = _MODULE_DIR.parent / "agent-office"
    if (repo_rt / "coordfs").exists():
        return repo_rt
    return None


class FakeBackend:
    """Mock OfficeBackend recording calls and providing configurable responses."""

    def __init__(self, channel: str | None = "grok-bot"):
        self.channel = channel
        self.send_calls: list[dict] = []
        self.read_calls: list[dict] = []
        self.ack_calls: list[dict] = []
        self.presence_calls: list[dict] = []

        self.send_result: dict = {
            "id": "11112222333344445555666677778888",
            "ts": 1700000000,
            "status": "sent",
            "code": None,
        }
        self.read_result: dict = {
            "messages": [],
            "remaining": 0,
            "cursor_advanced": False,
        }
        self.ack_result: dict = {"status": "ok"}
        self.presence_result: dict = {"status": "ok"}
        self.exception_to_raise: Exception | None = None

    def send(self, to=None, body="", kind="note", reply_to=None, channel=None, dedupe_key=None, channel_scope=None, new_recipient=False):
        if self.exception_to_raise:
            raise self.exception_to_raise
        scope = channel_scope if channel_scope is not None else ([self.channel] if self.channel else None)
        self.send_calls.append({
            "to": to,
            "body": body,
            "kind": kind,
            "reply_to": reply_to,
            "channel": channel,
            "dedupe_key": dedupe_key,
            "channel_scope": scope,
            "new_recipient": new_recipient,
        })
        return self.send_result

    def read(self, limit=20, peek=False, channel=None, max_body_bytes=8192, max_response_bytes=60000):
        if self.exception_to_raise:
            raise self.exception_to_raise
        self.read_calls.append({
            "limit": limit,
            "peek": peek,
            "channel": channel,
            "max_body_bytes": max_body_bytes,
            "max_response_bytes": max_response_bytes,
        })
        return self.read_result

    def ack(self, message_id, note=None, channel_scope=None):
        if self.exception_to_raise:
            raise self.exception_to_raise
        scope = channel_scope if channel_scope is not None else ([self.channel] if self.channel else None)
        self.ack_calls.append({"message_id": message_id, "note": note, "channel_scope": scope})
        return self.ack_result

    def presence(self, ttl_s=900, status="grok-bot gateway"):
        if self.exception_to_raise:
            raise self.exception_to_raise
        self.presence_calls.append({"ttl_s": ttl_s, "status": status})
        return self.presence_result


class CustomOfficeReject(Exception):
    def __init__(self, code="office_rejected"):
        super().__init__(f"Reject: {code}")
        self.code = code


class TestGrokOfficeGateway(unittest.TestCase):
    """Test suite covering gateway HTTP behaviors, auth, validation, and client interactions."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp(prefix="grok_test_")
        self.token_file = Path(self.temp_dir) / "token.sha256"
        self.test_token = "valid-secret-token-12345"
        self.token_file.write_text(
            hashlib.sha256(self.test_token.encode("utf-8")).hexdigest().lower(),
            encoding="utf-8",
        )

        self.backend = FakeBackend()
        self.config_dict = {
            "listen_host": "127.0.0.1",
            "listen_port": 0,
            "path_prefix": "/grok-office",
            "office_runtime": self.temp_dir,
            "office_home": self.temp_dir,
            "agent_id": "grokbot-cloud-agent-office",
            "identity": {
                "harness": "grokbot",
                "host": "cloud",
                "session_id": "agent-office",
            },
            "transport_principal": "grok-bot",
            "token_sha256_file": str(self.token_file),
            "channel": "grok-bot",
            "targets": {
                "agent-office-thread": "claude-ailab-main",
            },
            "allowed_kinds": ["note", "question"],
            "max_body_bytes": 8192,
            "max_read_limit": 20,
            "rate_limit_requests": 120,
            "rate_limit_window_s": 600.0,
        }

        self.cfg = GatewayConfig(self.config_dict)
        self.server = create_server(self.cfg, backend=self.backend)
        self.server_port = self.server.server_address[1]
        self.base_url = f"http://127.0.0.1:{self.server_port}/grok-office"

        self.server_thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.server_thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.server_thread.join(timeout=2.0)

    def _request(
        self,
        endpoint: str,
        method: str = "GET",
        headers: dict | None = None,
        data: dict | str | bytes | None = None,
        raw_headers: bool = False,
    ) -> tuple[int, dict]:
        url = f"{self.base_url}{endpoint}"
        req_headers = {}
        if not raw_headers:
            req_headers["Authorization"] = f"Bearer {self.test_token}"
        if headers:
            req_headers.update(headers)

        body_bytes = None
        if data is not None:
            if isinstance(data, dict):
                body_bytes = json.dumps(data).encode("utf-8")
                if "Content-Type" not in req_headers:
                    req_headers["Content-Type"] = "application/json"
            elif isinstance(data, str):
                body_bytes = data.encode("utf-8")
            elif isinstance(data, bytes):
                body_bytes = data

        req = urllib.request.Request(url, data=body_bytes, headers=req_headers, method=method)
        try:
            with urllib.request.urlopen(req) as resp:
                raw_body = resp.read().decode("utf-8")
                return resp.status, json.loads(raw_body)
        except urllib.error.HTTPError as exc:
            err_body = exc.read().decode("utf-8")
            try:
                parsed = json.loads(err_body)
            except Exception:
                parsed = {"raw": err_body}
            return exc.code, parsed

    def test_health_ok(self):
        """Cover: health ok with valid token."""
        status, data = self._request("/v1/health")
        self.assertEqual(status, 200)
        self.assertEqual(data["status"], "ok")
        self.assertEqual(data["agent"], "grokbot-cloud-agent-office")
        self.assertEqual(data["channel"], "grok-bot")
        self.assertEqual(data["targets"], ["agent-office-thread"])
        self.assertEqual(data["target_agents"], {"agent-office-thread": "claude-ailab-main"})

        # Presence check
        self.assertEqual(len(self.backend.presence_calls), 1)
        self.assertEqual(self.backend.presence_calls[0]["ttl_s"], 900)
        self.assertEqual(self.backend.presence_calls[0]["status"], "grok-bot gateway")

    def test_auth_failures(self):
        """Cover: 401 for missing, wrong and malformed tokens."""
        # 1. Missing token
        status, data = self._request("/v1/health", raw_headers=True)
        self.assertEqual(status, 401)
        self.assertEqual(data, {"status": "error", "code": "unauthorized"})

        # 2. Wrong token
        status, data = self._request("/v1/health", headers={"Authorization": "Bearer wrong-token-xyz"}, raw_headers=True)
        self.assertEqual(status, 401)
        self.assertEqual(data, {"status": "error", "code": "unauthorized"})

        # 3. Malformed auth header (Basic instead of Bearer)
        status, data = self._request("/v1/health", headers={"Authorization": "Basic dXNlcjpwYXNz"}, raw_headers=True)
        self.assertEqual(status, 401)
        self.assertEqual(data, {"status": "error", "code": "unauthorized"})

        # 4. Empty Bearer token
        status, data = self._request("/v1/health", headers={"Authorization": "Bearer "}, raw_headers=True)
        self.assertEqual(status, 401)
        self.assertEqual(data, {"status": "error", "code": "unauthorized"})

    def test_token_hash_rotation(self):
        """Cover: token hash rotation picked up."""
        new_token = "new-rotated-token-99999"
        new_hash = hashlib.sha256(new_token.encode("utf-8")).hexdigest().lower()
        self.token_file.write_text(new_hash, encoding="utf-8")

        # Invalidate cached hash to simulate elapsed rotation interval
        self.server.token_validator.invalidate()

        # Old token now fails
        status, data = self._request("/v1/health", headers={"Authorization": f"Bearer {self.test_token}"}, raw_headers=True)
        self.assertEqual(status, 401)

        # New token succeeds
        status, data = self._request("/v1/health", headers={"Authorization": f"Bearer {new_token}"}, raw_headers=True)
        self.assertEqual(status, 200)
        self.assertEqual(data["status"], "ok")

    def test_wrong_prefix_404(self):
        """Cover: 404 for wrong prefix."""
        wrong_url = f"http://127.0.0.1:{self.server_port}/wrong-prefix/v1/health"
        req = urllib.request.Request(wrong_url, headers={"Authorization": f"Bearer {self.test_token}"})
        try:
            with urllib.request.urlopen(req) as resp:
                status = resp.status
                data = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            status = exc.code
            data = json.loads(exc.read().decode("utf-8"))

        self.assertEqual(status, 404)
        self.assertEqual(data, {"status": "error", "code": "not_found"})

        # Subpath not found
        status, data = self._request("/v1/nonexistent")
        self.assertEqual(status, 404)
        self.assertEqual(data, {"status": "error", "code": "not_found"})

    def test_send_to_channel_and_alias(self):
        """Cover: send to channel and to alias."""
        # 1. Send to channel
        status, data = self._request(
            "/v1/send",
            method="POST",
            data={"target": "channel", "body": "hello channel", "kind": "note"},
        )
        self.assertEqual(status, 200)
        self.assertEqual(len(self.backend.send_calls), 1)
        call = self.backend.send_calls[-1]
        self.assertIsNone(call["to"])
        self.assertEqual(call["channel"], "grok-bot")
        self.assertEqual(call["body"], "hello channel")
        self.assertEqual(call["kind"], "note")

        # 2. Send to alias
        status, data = self._request(
            "/v1/send",
            method="POST",
            data={
                "target": "agent-office-thread",
                "body": "hello alias",
                "kind": "question",
                "reply_to": "0123456789abcdef0123456789abcdef",
                "dedupe_key": "custom_dedupe_123",
            },
        )
        self.assertEqual(status, 200)
        self.assertEqual(len(self.backend.send_calls), 2)
        call = self.backend.send_calls[-1]
        self.assertEqual(call["to"], "claude-ailab-main")
        self.assertIsNone(call["channel"])
        self.assertEqual(call["body"], "hello alias")
        self.assertEqual(call["kind"], "question")
        self.assertEqual(call["reply_to"], "0123456789abcdef0123456789abcdef")
        self.assertEqual(call["dedupe_key"], "custom_dedupe_123")

    def test_target_not_allowed(self):
        """Cover: target_not_allowed."""
        status, data = self._request(
            "/v1/send",
            method="POST",
            data={"target": "unauthorized-alias", "body": "hello"},
        )
        self.assertEqual(status, 400)
        self.assertEqual(data, {"status": "error", "code": "target_not_allowed"})

    def test_kind_not_allowed(self):
        """Cover: kind not allowed."""
        status, data = self._request(
            "/v1/send",
            method="POST",
            data={"target": "channel", "body": "hello", "kind": "handoff"},
        )
        self.assertEqual(status, 400)
        self.assertEqual(data, {"status": "error", "code": "kind_not_allowed"})

    def test_authority_field_refused(self):
        """Cover: authority_field_refused for each listed field."""
        forbidden_fields = [
            "from",
            "sender",
            "agent_id",
            "principal",
            "transport_principal",
            "owner",
            "token",
            "secret",
            "authority",
            "credential",
        ]
        for field in forbidden_fields:
            payload = {"target": "channel", "body": "test", field: "malicious_spoof"}
            status, data = self._request("/v1/send", method="POST", data=payload)
            self.assertEqual(status, 400, f"Field {field} was not refused with 400")
            self.assertEqual(
                data,
                {"status": "error", "code": "authority_field_refused"},
                f"Field {field} did not return authority_field_refused",
            )

    def test_unknown_and_duplicate_key_refused(self):
        """Cover: unknown key and duplicate key refused."""
        # Unknown key
        status, data = self._request(
            "/v1/send",
            method="POST",
            data={"target": "channel", "body": "test", "unexpected_field": "val"},
        )
        self.assertEqual(status, 400)
        self.assertEqual(data, {"status": "error", "code": "unknown_field"})

        # Duplicate key
        raw_duplicate = '{"target": "channel", "target": "channel", "body": "test"}'
        status, data = self._request(
            "/v1/send",
            method="POST",
            headers={"Content-Type": "application/json"},
            data=raw_duplicate,
        )
        self.assertEqual(status, 400)
        self.assertEqual(data, {"status": "error", "code": "duplicate_key"})

    def test_body_too_large(self):
        """Cover: body too large -> 413."""
        # 1. Body exceeding max_body_bytes
        oversized_body = "x" * 9000
        status, data = self._request(
            "/v1/send",
            method="POST",
            data={"target": "channel", "body": oversized_body},
        )
        self.assertEqual(status, 413)
        self.assertEqual(data, {"status": "error", "code": "body_too_large"})

        # 2. Content-Length exceeding max_body_bytes + 2048
        huge_raw = json.dumps({"target": "channel", "body": "a" * 12000})
        status, data = self._request(
            "/v1/send",
            method="POST",
            headers={"Content-Type": "application/json"},
            data=huge_raw,
        )
        self.assertEqual(status, 413)
        self.assertEqual(data, {"status": "error", "code": "payload_too_large"})

    def test_invalid_reply_to(self):
        """Cover: invalid reply_to."""
        # Non-32 length
        status, data = self._request(
            "/v1/send",
            method="POST",
            data={"target": "channel", "body": "test", "reply_to": "12345"},
        )
        self.assertEqual(status, 400)
        self.assertEqual(data, {"status": "error", "code": "invalid_reply_to"})

        # Non-hex characters
        status, data = self._request(
            "/v1/send",
            method="POST",
            data={
                "target": "channel",
                "body": "test",
                "reply_to": "0123456789abcdef0123456789abcdefg!",
            },
        )
        self.assertEqual(status, 400)
        self.assertEqual(data, {"status": "error", "code": "invalid_reply_to"})

    def test_rate_limit(self):
        """Cover: rate limit -> 429."""
        # Set rate limit to 3 requests per 60 seconds
        self.server.rate_limiter = type(self.server.rate_limiter)(max_requests=3, window_s=60.0)

        # 3 allowed requests
        for _ in range(3):
            status, _ = self._request("/v1/health")
            self.assertEqual(status, 200)

        # 4th request rate-limited
        status, data = self._request("/v1/health")
        self.assertEqual(status, 429)
        self.assertEqual(data, {"status": "error", "code": "rate_limited"})

    def test_inbox_and_channel_extraction(self):
        """Cover: inbox/channel extraction of untrusted_message."""
        self.backend.read_result = {
            "messages": [
                {
                    "untrusted_message": {
                        "id": "11112222333344445555666677778888",
                        "ts": 1700000001,
                        "from": "claude-ailab-main",
                        "kind": "question",
                        "reply_to": "00000000000000000000000000000000",
                        "body": "Wrapped untrusted message body",
                    },
                },
                {
                    "id": "22223333444455556666777788889999",
                    "ts": 1700000002,
                    "from": "peer-agent",
                    "kind": "note",
                    "reply_to": None,
                    "body": "Flat message body",
                },
            ],
            "remaining": 4,
            "cursor_advanced": True,
        }

        # 1. Test inbox
        status, data = self._request("/v1/inbox?limit=5&peek=1")
        self.assertEqual(status, 200)
        self.assertEqual(data["status"], "ok")
        self.assertEqual(data["remaining"], 4)
        self.assertTrue(data["cursor_advanced"])
        self.assertEqual(len(data["messages"]), 2)

        msg0 = data["messages"][0]
        self.assertEqual(msg0["id"], "11112222333344445555666677778888")
        self.assertEqual(msg0["from"], "claude-ailab-main")
        self.assertEqual(msg0["kind"], "question")
        self.assertEqual(msg0["reply_to"], "00000000000000000000000000000000")
        self.assertEqual(msg0["body"], "Wrapped untrusted message body")
        self.assertEqual(msg0["trust"], "untrusted")

        msg1 = data["messages"][1]
        self.assertEqual(msg1["id"], "22223333444455556666777788889999")
        self.assertEqual(msg1["from"], "peer-agent")
        self.assertEqual(msg1["body"], "Flat message body")
        self.assertEqual(msg1["trust"], "untrusted")

        # 2. Test channel
        status, data = self._request("/v1/channel?limit=2")
        self.assertEqual(status, 200)
        self.assertEqual(self.backend.read_calls[-1]["channel"], "grok-bot")
        self.assertTrue(self.backend.read_calls[-1]["peek"])

    def test_ack(self):
        """Cover: ack."""
        valid_id = "0123456789abcdef0123456789abcdef"
        status, data = self._request(
            "/v1/ack",
            method="POST",
            data={"message_id": valid_id, "note": "all processed"},
        )
        self.assertEqual(status, 200)
        self.assertEqual(data, {"status": "ok"})
        self.assertEqual(len(self.backend.ack_calls), 1)
        self.assertEqual(self.backend.ack_calls[0]["message_id"], valid_id)
        self.assertEqual(self.backend.ack_calls[0]["note"], "all processed")

        # Invalid message id
        status, data = self._request("/v1/ack", method="POST", data={"message_id": "short"})
        self.assertEqual(status, 400)
        self.assertEqual(data, {"status": "error", "code": "invalid_message_id"})

    def test_office_exception_handling(self):
        """Cover: Office exception with code -> 400 with that code."""
        # 1. Custom rejection with .code -> HTTP 400
        self.backend.exception_to_raise = CustomOfficeReject("rate_exceeded")
        status, data = self._request("/v1/send", method="POST", data={"target": "channel", "body": "test"})
        self.assertEqual(status, 400)
        self.assertEqual(data, {"status": "error", "code": "rate_exceeded"})

        # 2. Generic exception -> HTTP 502 office_error, no traceback
        self.backend.exception_to_raise = RuntimeError("Database file is locked")
        status, data = self._request("/v1/send", method="POST", data={"target": "channel", "body": "test"})
        self.assertEqual(status, 502)
        self.assertEqual(data, {"status": "error", "code": "office_error"})
        self.assertNotIn("Traceback", json.dumps(data))
        self.backend.exception_to_raise = None

    def test_no_token_or_body_in_captured_stderr(self):
        """Cover: no token or body appears in captured stderr logs."""
        secret_body_text = "SECRET_DATA_CONTENT_ALPHA_OMEGA"
        capture_stderr = io.StringIO()
        old_stderr = sys.stderr
        sys.stderr = capture_stderr
        try:
            self._request(
                "/v1/send",
                method="POST",
                data={"target": "channel", "body": secret_body_text},
            )
        finally:
            sys.stderr = old_stderr

        logged_text = capture_stderr.getvalue()
        self.assertNotIn(self.test_token, logged_text)
        self.assertNotIn(secret_body_text, logged_text)
        self.assertIn("/v1/send", logged_text)
        self.assertIn("200", logged_text)

    def test_client_subcommands_and_ask_in_process(self):
        """Cover: client health, send, inbox, channel, ack and ask in-process."""
        client_env = {
            "GROK_OFFICE_URL": self.base_url,
            "GROK_OFFICE_TOKEN": self.test_token,
        }

        # Client health
        buf = io.StringIO()
        code = run_client(["health"], env=client_env, stdout=buf)
        self.assertEqual(code, 0)
        self.assertIn("grokbot-cloud-agent-office", buf.getvalue())

        # Client send
        buf = io.StringIO()
        code = run_client(["send", "--target", "channel", "--body", "client note text"], env=client_env, stdout=buf)
        self.assertEqual(code, 0)

        # Client inbox
        buf = io.StringIO()
        code = run_client(["inbox", "--limit", "5", "--peek"], env=client_env, stdout=buf)
        self.assertEqual(code, 0)

        # Client channel
        buf = io.StringIO()
        code = run_client(["channel", "--limit", "5"], env=client_env, stdout=buf)
        self.assertEqual(code, 0)

        # Client ack
        buf = io.StringIO()
        code = run_client(["ack", "0123456789abcdef0123456789abcdef", "--note", "cli ack"], env=client_env, stdout=buf)
        self.assertEqual(code, 0)

        # Client ask with automated reply response
        sent_id = "55556666777788889999000011112222"
        reply_msg_id = "444455556666777788889999aaaabbbb"
        self.backend.send_result = {"id": sent_id, "ts": 1700000050, "status": "sent"}
        self.backend.read_result = {
            "messages": [
                {
                    "untrusted_message": {
                        "id": reply_msg_id,
                        "ts": 1700000060,
                        "from": "claude-ailab-main",
                        "kind": "note",
                        "reply_to": sent_id,
                        "body": "Client ask answer",
                    },
                }
            ],
            "remaining": 0,
            "cursor_advanced": False,
        }

        buf = io.StringIO()
        code = run_client(
            [
                "ask",
                "--target",
                "agent-office-thread",
                "--body",
                "Ask question via client",
                "--timeout-s",
                "5",
                "--poll-s",
                "0.05",
            ],
            env=client_env,
            stdout=buf,
        )
        self.assertEqual(code, 0)
        resp_json = json.loads(buf.getvalue().strip())
        self.assertEqual(resp_json["body"], "Client ask answer")
        self.assertEqual(resp_json["trust"], "untrusted")
        self.assertEqual(len(self.backend.ack_calls), 2)
        self.assertEqual(self.backend.ack_calls[-1]["message_id"], reply_msg_id)

    def test_cli_hash_token_stdin(self):
        """Cover: CLI --hash-token-stdin."""
        secret = "super-secret-operator-pass"
        expected_hash = hashlib.sha256(secret.encode("utf-8")).hexdigest().lower()

        old_stdin = sys.stdin
        old_stdout = sys.stdout
        sys.stdin = io.StringIO(secret + "\n")
        sys.stdout = io.StringIO()
        try:
            hash_token_stdin()
            output = sys.stdout.getvalue().strip()
        finally:
            sys.stdin = old_stdin
            sys.stdout = old_stdout

        self.assertEqual(output, expected_hash)

    def test_regression_finding_1_redirect_refusal_and_ask_timeout(self):
        """Regression test for Finding 1:
        1. Custom opener refuses ALL redirects (returns redirect_refused, exit code 2).
        2. ask with --timeout-s exits with code 3 on timeout.
        3. Every HTTP call uses explicit timeout.
        """
        redirect_hit = []
        from http.server import HTTPServer, BaseHTTPRequestHandler

        class RedirectServerHandler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                if self.path == "/target":
                    redirect_hit.append(True)
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    self.wfile.write(b'{"status":"leaked"}')
                else:
                    self.send_response(302)
                    self.send_header("Location", f"http://127.0.0.1:{redirect_port}/target")
                    self.end_headers()

            def do_POST(self):
                self.do_GET()

        redirect_server = HTTPServer(("127.0.0.1", 0), RedirectServerHandler)
        redirect_port = redirect_server.server_address[1]
        t = threading.Thread(target=redirect_server.serve_forever, daemon=True)
        t.start()
        try:
            client_env = {
                "GROK_OFFICE_URL": f"http://127.0.0.1:{redirect_port}",
                "GROK_OFFICE_TOKEN": self.test_token,
            }
            # Test health against redirecting server
            buf = io.StringIO()
            code = run_client(["health"], env=client_env, stdout=buf)
            self.assertEqual(code, 2)
            out = json.loads(buf.getvalue().strip())
            self.assertEqual(out.get("status"), "error")
            self.assertEqual(out.get("code"), "redirect_refused")
            self.assertEqual(len(redirect_hit), 0, "Redirect destination must NOT be reached")

            # Test send against redirecting server
            buf = io.StringIO()
            code = run_client(["send", "--target", "channel", "--body", "test"], env=client_env, stdout=buf)
            self.assertEqual(code, 2)
            out = json.loads(buf.getvalue().strip())
            self.assertEqual(out.get("status"), "error")
            self.assertEqual(out.get("code"), "redirect_refused")
            self.assertEqual(len(redirect_hit), 0, "Redirect destination must NOT be reached")
        finally:
            redirect_server.shutdown()
            redirect_server.server_close()
            t.join(timeout=2.0)

        # 2. Test ask timeout exits with code 3
        self.backend.send_result = {"id": "11112222333344445555666677778888", "ts": 1700000000, "status": "sent"}
        self.backend.read_result = {"messages": [], "remaining": 0, "cursor_advanced": False}

        client_env = {
            "GROK_OFFICE_URL": self.base_url,
            "GROK_OFFICE_TOKEN": self.test_token,
        }
        buf = io.StringIO()
        start = time.monotonic()
        code = run_client(
            [
                "ask",
                "--target",
                "agent-office-thread",
                "--body",
                "Testing ask timeout",
                "--timeout-s",
                "0.25",
                "--poll-s",
                "0.05",
            ],
            env=client_env,
            stdout=buf,
        )
        duration = time.monotonic() - start
        self.assertEqual(code, 3, "ask must exit with code 3 on timeout")
        out = json.loads(buf.getvalue().strip())
        self.assertEqual(out.get("status"), "error")
        self.assertEqual(out.get("code"), "timeout")
        self.assertLess(duration, 3.0, "ask must not hang indefinitely")

    def test_regression_finding_2_unknown_route_logging(self):
        """Regression test for Finding 2:
        For unknown routes log a fixed route name 'unknown' (never the raw path or query).
        Known routes log only their fixed names.
        """
        secret_path = "top_secret_endpoint_xyz"
        secret_query = "token_leak=supersecret987"
        capture_stderr = io.StringIO()
        old_stderr = sys.stderr
        sys.stderr = capture_stderr
        try:
            # 1. Request to unknown subpath under prefix
            self._request(f"/v1/{secret_path}?{secret_query}")

            # 2. Request to wrong prefix
            wrong_url = f"http://127.0.0.1:{self.server_port}/wrong-path/{secret_path}?{secret_query}"
            req = urllib.request.Request(wrong_url, headers={"Authorization": f"Bearer {self.test_token}"})
            try:
                urllib.request.urlopen(req)
            except urllib.error.HTTPError:
                pass

            # 3. Request to known route
            self._request("/v1/health")
        finally:
            sys.stderr = old_stderr

        logged_text = capture_stderr.getvalue()
        # Raw secret path and query must NEVER appear in logs
        self.assertNotIn(secret_path, logged_text)
        self.assertNotIn(secret_query, logged_text)
        self.assertNotIn("supersecret987", logged_text)
        self.assertNotIn("wrong-path", logged_text)

        # Fixed names MUST appear
        self.assertIn(" unknown 404 ", logged_text)
        self.assertIn(" /v1/health 200 ", logged_text)

    def test_regression_finding_3_ack_message_id_validation(self):
        """Regression test for Finding 3:
        POST /v1/ack must accept only a 32-character lowercase hex message_id (same validation as reply_to);
        everything else -> 400 invalid_message_id.
        """
        # 1. Valid 32-character lowercase hex message_id -> 200
        valid_id = "0123456789abcdef0123456789abcdef"
        status, data = self._request("/v1/ack", method="POST", data={"message_id": valid_id})
        self.assertEqual(status, 200)
        self.assertEqual(data, {"status": "ok"})

        # 2. 32-character UPPERCASE hex -> 400 invalid_message_id
        upper_id = "0123456789ABCDEF0123456789ABCDEF"
        status, data = self._request("/v1/ack", method="POST", data={"message_id": upper_id})
        self.assertEqual(status, 400)
        self.assertEqual(data, {"status": "error", "code": "invalid_message_id"})

        # 3. 32-character mixed-case hex -> 400 invalid_message_id
        mixed_id = "0123456789abcdef0123456789abcdeF"
        status, data = self._request("/v1/ack", method="POST", data={"message_id": mixed_id})
        self.assertEqual(status, 400)
        self.assertEqual(data, {"status": "error", "code": "invalid_message_id"})

        # 4. Safe ID with dashes (previously accepted) -> 400 invalid_message_id
        safe_id = "valid-safe-id-with-dashes-12345"
        status, data = self._request("/v1/ack", method="POST", data={"message_id": safe_id})
        self.assertEqual(status, 400)
        self.assertEqual(data, {"status": "error", "code": "invalid_message_id"})

        # 5. Short hex (16 chars) -> 400 invalid_message_id
        short_id = "0123456789abcdef"
        status, data = self._request("/v1/ack", method="POST", data={"message_id": short_id})
        self.assertEqual(status, 400)
        self.assertEqual(data, {"status": "error", "code": "invalid_message_id"})

        # 6. Long hex (33 chars) -> 400 invalid_message_id
        long_id = "0123456789abcdef0123456789abcdef0"
        status, data = self._request("/v1/ack", method="POST", data={"message_id": long_id})
        self.assertEqual(status, 400)
        self.assertEqual(data, {"status": "error", "code": "invalid_message_id"})

        # 7. Non-hex char ('g') -> 400 invalid_message_id
        non_hex_id = "0123456789abcdef0123456789abcdeg"
        status, data = self._request("/v1/ack", method="POST", data={"message_id": non_hex_id})
        self.assertEqual(status, 400)
        self.assertEqual(data, {"status": "error", "code": "invalid_message_id"})

        # 8. Non-string message_id -> 400 invalid_message_id
        status, data = self._request("/v1/ack", method="POST", data={"message_id": 1234567890})
        self.assertEqual(status, 400)
        self.assertEqual(data, {"status": "error", "code": "invalid_message_id"})

        # 9. Verify same validation applies to reply_to in POST /v1/send
        status, data = self._request(
            "/v1/send",
            method="POST",
            data={"target": "channel", "body": "test", "reply_to": upper_id},
        )
        self.assertEqual(status, 400)
        self.assertEqual(data, {"status": "error", "code": "invalid_reply_to"})

    def test_regression_finding_4_methods_dispatch_and_405(self):
        """Regression test for Finding 4:
        Any HTTP method other than GET and POST (PUT, DELETE, PATCH, OPTIONS, TRACE, HEAD, CONNECT, unknown)
        must return a JSON 405 {status:error, code:method_not_allowed} (HEAD may return 405 without body);
        implemented via generic dispatcher so no default HTML 501 page can be produced.
        """
        methods_with_body = ["PUT", "DELETE", "PATCH", "OPTIONS", "TRACE", "CONNECT", "FOOBAR"]

        for m in methods_with_body:
            conn = http.client.HTTPConnection("127.0.0.1", self.server_port)
            try:
                conn.request(
                    m,
                    "/grok-office/v1/health",
                    headers={"Authorization": f"Bearer {self.test_token}"},
                )
                resp = conn.getresponse()
                raw_body = resp.read()
                self.assertEqual(
                    resp.status, 405, f"Method {m} did not return 405, got {resp.status}"
                )
                self.assertIn("application/json", resp.getheader("Content-Type", ""))
                parsed = json.loads(raw_body.decode("utf-8"))
                self.assertEqual(parsed, {"status": "error", "code": "method_not_allowed"})
                self.assertNotIn(b"501", raw_body)
                self.assertNotIn(b"<html", raw_body.lower())
            finally:
                conn.close()

        # Test HEAD method: must return 405 with empty body
        conn = http.client.HTTPConnection("127.0.0.1", self.server_port)
        try:
            conn.request(
                "HEAD",
                "/grok-office/v1/health",
                headers={"Authorization": f"Bearer {self.test_token}"},
            )
            resp = conn.getresponse()
            raw_body = resp.read()
            self.assertEqual(resp.status, 405)
            self.assertEqual(len(raw_body), 0, "HEAD 405 must have empty body")
            self.assertIn("application/json", resp.getheader("Content-Type", ""))
        finally:
            conn.close()

    def test_real_office_health_with_host_qualified_identity(self):
        """Regression (live smoke 2026-10-23): presence under a transport principal needs a
        host-qualified identity; with `identity` config, /v1/health returns 200 on real Office."""
        runtime_path = find_office_runtime()
        if runtime_path is None:
            self.skipTest("Real Office runtime (coordfs) not found in env or default paths")
        real_home = Path(tempfile.mkdtemp(prefix="grok_real_office_health_"))
        cfg_dict = dict(self.config_dict)
        cfg_dict.update({
            "office_home": str(real_home),
            "office_runtime": str(runtime_path),
            "agent_id": "grokbot-cloud-agent-office",
            "identity": {"harness": "grokbot", "host": "cloud", "session_id": "agent-office"},
        })
        cfg = GatewayConfig(cfg_dict)
        backend = OfficeBackend.from_config(cfg)
        server = create_server(cfg, backend=backend)
        port = server.server_address[1]
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            req = urllib.request.Request(
                f"http://127.0.0.1:{port}/grok-office/v1/health",
                headers={"Authorization": f"Bearer {self.test_token}"},
            )
            with urllib.request.urlopen(req) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            self.assertEqual(data["status"], "ok")
            self.assertEqual(data["agent"], "grokbot-cloud-agent-office")
            bad = dict(cfg_dict)
            bad["identity"] = {"harness": "other", "host": "cloud", "session_id": "agent-office"}
            with self.assertRaises(ValueError):
                OfficeBackend.from_config(GatewayConfig(bad))
            bad["identity"] = {"harness": "grokbot", "host": "cloud"}
            with self.assertRaises(ValueError):
                GatewayConfig(bad)
        finally:
            server.shutdown()
            server.server_close()

    def test_real_office_round_trip(self):
        """Cover: real-Office round trip.

        Gateway sends a question to target agent id, second Office instance reads it,
        verifies transport_principal 'grok-bot' and from == gateway agent id, replies
        with reply_to, and the gateway inbox returns that reply.
        """
        runtime_path = find_office_runtime()
        if runtime_path is None:
            self.skipTest("Real Office runtime (coordfs) not found in env or default paths")

        if str(runtime_path) not in sys.path:
            sys.path.insert(0, str(runtime_path))

        from coordfs.office import Office

        real_home = Path(tempfile.mkdtemp(prefix="grok_real_office_"))

        real_cfg_dict = dict(self.config_dict)
        real_cfg_dict["office_home"] = str(real_home)
        real_cfg_dict["office_runtime"] = str(runtime_path)
        real_cfg_dict["targets"] = {"agent-office-thread": "target-worker-agent"}

        real_cfg = GatewayConfig(real_cfg_dict)
        real_backend = OfficeBackend.from_config(real_cfg)

        real_server = create_server(real_cfg, backend=real_backend)
        port = real_server.server_address[1]
        server_thread = threading.Thread(target=real_server.serve_forever, daemon=True)
        server_thread.start()

        try:
            target_office = Office(
                agent_id="target-worker-agent",
                home=real_home,
                transport_principal="local-worker",
            )
            target_office.presence(ttl_s=900, status="ready")

            # 1. Gateway sends question to target alias
            send_url = f"http://127.0.0.1:{port}/grok-office/v1/send"
            send_payload = {
                "target": "agent-office-thread",
                "body": "Real Office round-trip question",
                "kind": "question",
            }
            req = urllib.request.Request(
                send_url,
                data=json.dumps(send_payload).encode("utf-8"),
                headers={
                    "Authorization": f"Bearer {self.test_token}",
                    "Content-Type": "application/json",
                },
                method="POST",
            )
            with urllib.request.urlopen(req) as resp:
                send_data = json.loads(resp.read().decode("utf-8"))
            sent_msg_id = send_data["id"]

            # 2. Target Office reads the question
            read_res = target_office.read(limit=10, peek=False)
            msgs = read_res.get("messages", []) if isinstance(read_res, dict) else getattr(read_res, "messages", [])
            self.assertGreaterEqual(len(msgs), 1)

            found_msg = None
            for m in msgs:
                m_id = m.get("id") or m.get("untrusted_message", {}).get("id")
                if m_id == sent_msg_id:
                    found_msg = m
                    break

            self.assertIsNotNone(found_msg, "Sent message not found in target inbox")

            # Verify transport_principal 'grok-bot' and from == gateway agent id
            m_from = found_msg.get("from") or found_msg.get("untrusted_message", {}).get("from")
            m_tp = found_msg.get("transport_principal") or found_msg.get("untrusted_message", {}).get("transport_principal")
            m_body = found_msg.get("body") or found_msg.get("untrusted_message", {}).get("body")

            self.assertEqual(m_from, "grokbot-cloud-agent-office")
            self.assertEqual(m_tp, "grok-bot")
            self.assertEqual(m_body, "Real Office round-trip question")

            # 3. Target Office replies with reply_to
            target_office.send(
                to="grokbot-cloud-agent-office",
                body="Real Office round-trip answer",
                kind="note",
                reply_to=sent_msg_id,
                new_recipient=True,
            )

            # 4. Gateway inbox endpoint returns that reply
            inbox_url = f"http://127.0.0.1:{port}/grok-office/v1/inbox?limit=10&peek=1"
            req_inbox = urllib.request.Request(
                inbox_url,
                headers={"Authorization": f"Bearer {self.test_token}"},
                method="GET",
            )
            with urllib.request.urlopen(req_inbox) as resp:
                inbox_data = json.loads(resp.read().decode("utf-8"))

            inbox_msgs = inbox_data.get("messages", [])
            reply_found = None
            for m in inbox_msgs:
                if m.get("reply_to") == sent_msg_id:
                    reply_found = m
                    break

            self.assertIsNotNone(reply_found, "Target reply was not returned by gateway inbox endpoint")
            self.assertEqual(reply_found["body"], "Real Office round-trip answer")
            self.assertEqual(reply_found["trust"], "untrusted")

            # 5. Client ask against running real-office server
            def _async_replier():
                for _ in range(60):
                    time.sleep(0.05)
                    read_step = target_office.read(limit=10, peek=False)
                    step_msgs = read_step.get("messages", []) if isinstance(read_step, dict) else getattr(read_step, "messages", [])
                    for sm in step_msgs:
                        sb = sm.get("body") or sm.get("untrusted_message", {}).get("body")
                        if sb == "Client ask in real office":
                            sm_id = sm.get("id") or sm.get("untrusted_message", {}).get("id")
                            target_office.send(
                                to="grokbot-cloud-agent-office",
                                body="Async answer for client ask",
                                kind="note",
                                reply_to=sm_id,
                                new_recipient=True,
                            )
                            return

            reply_thread = threading.Thread(target=_async_replier, daemon=True)
            reply_thread.start()

            client_env = {
                "GROK_OFFICE_URL": f"http://127.0.0.1:{port}/grok-office",
                "GROK_OFFICE_TOKEN": self.test_token,
            }
            buf = io.StringIO()
            ask_code = run_client(
                [
                    "ask",
                    "--target",
                    "agent-office-thread",
                    "--body",
                    "Client ask in real office",
                    "--timeout-s",
                    "6",
                    "--poll-s",
                    "0.05",
                ],
                env=client_env,
                stdout=buf,
            )
            self.assertEqual(ask_code, 0)
            ask_result = json.loads(buf.getvalue().strip())
            self.assertEqual(ask_result["body"], "Async answer for client ask")
            self.assertEqual(ask_result["trust"], "untrusted")

        finally:
            real_server.shutdown()
            real_server.server_close()
            server_thread.join(timeout=2.0)

    def test_regression_a_contract_violation_and_backend_signatures(self):
        """Regression test for Defect A:
        1. Send result without a valid 32-hex id -> HTTP 502 code office_contract_violation.
        2. Verify channel_scope and new_recipient are pinned without guessing or fallbacks.
        """
        # Missing id
        self.backend.send_result = {"status": "ok"}
        status, data = self._request("/v1/send", method="POST", data={"target": "channel", "body": "test"})
        self.assertEqual(status, 502)
        self.assertEqual(data, {"status": "error", "code": "office_contract_violation"})

        # Invalid non-hex id
        self.backend.send_result = {"id": "non-hex-id-not-valid-32-chars!!", "status": "ok"}
        status, data = self._request("/v1/send", method="POST", data={"target": "channel", "body": "test"})
        self.assertEqual(status, 502)
        self.assertEqual(data, {"status": "error", "code": "office_contract_violation"})

        # Short id
        self.backend.send_result = {"id": "1234567890abcdef", "status": "ok"}
        status, data = self._request("/v1/send", method="POST", data={"target": "channel", "body": "test"})
        self.assertEqual(status, 502)
        self.assertEqual(data, {"status": "error", "code": "office_contract_violation"})

        # String result (no guessing)
        self.backend.send_result = "11112222333344445555666677778888"
        status, data = self._request("/v1/send", method="POST", data={"target": "channel", "body": "test"})
        self.assertEqual(status, 502)
        self.assertEqual(data, {"status": "error", "code": "office_contract_violation"})

        # Tuple result (no guessing)
        self.backend.send_result = ("11112222333344445555666677778888", 1700000000)
        status, data = self._request("/v1/send", method="POST", data={"target": "channel", "body": "test"})
        self.assertEqual(status, 502)
        self.assertEqual(data, {"status": "error", "code": "office_contract_violation"})

        # Restore valid send_result
        self.backend.send_result = {"id": "11112222333344445555666677778888", "status": "sent"}

    def test_regression_b_real_office_channel_scope(self):
        """Regression test for Defect B (real Office):
        Replying (reply_to) to, or acking, a message stored on any channel other than cfg.channel
        must be refused with status error code outside_channel_scope -> HTTP 400; nothing written to foreign channel.
        """
        runtime_path = find_office_runtime()
        if runtime_path is None:
            self.skipTest("Real Office runtime (coordfs) not found")
        if str(runtime_path) not in sys.path:
            sys.path.insert(0, str(runtime_path))
        from coordfs.office import Office

        real_home = Path(tempfile.mkdtemp(prefix="grok_real_scope_"))
        real_cfg_dict = dict(self.config_dict)
        real_cfg_dict.update({
            "office_home": str(real_home),
            "office_runtime": str(runtime_path),
            "channel": "grok-bot",
            "identity": {"harness": "grokbot", "host": "cloud", "session_id": "agent-office"},
        })
        real_cfg = GatewayConfig(real_cfg_dict)
        real_backend = OfficeBackend.from_config(real_cfg)
        real_server = create_server(real_cfg, backend=real_backend)
        port = real_server.server_address[1]
        t = threading.Thread(target=real_server.serve_forever, daemon=True)
        t.start()
        try:
            foreign_office = Office(agent_id="foreign-agent-1", home=real_home, transport_principal="local-worker")
            foreign_res = foreign_office.send(to=None, channel="foreign-channel", body="Notice on foreign channel")
            foreign_id = foreign_res["id"]

            headers = {"Authorization": f"Bearer {self.test_token}", "Content-Type": "application/json"}

            # 1. Try to ack foreign channel message via gateway
            req_ack = urllib.request.Request(
                f"http://127.0.0.1:{port}/grok-office/v1/ack",
                data=json.dumps({"message_id": foreign_id}).encode("utf-8"),
                headers=headers,
                method="POST",
            )
            try:
                with urllib.request.urlopen(req_ack) as resp:
                    status = resp.status
                    data = json.loads(resp.read().decode("utf-8"))
            except urllib.error.HTTPError as exc:
                status = exc.code
                data = json.loads(exc.read().decode("utf-8"))
            self.assertEqual(status, 400)
            self.assertEqual(data.get("code"), "outside_channel_scope")

            # 2. Try to send with reply_to foreign channel message via gateway
            req_send = urllib.request.Request(
                f"http://127.0.0.1:{port}/grok-office/v1/send",
                data=json.dumps({
                    "target": "channel",
                    "reply_to": foreign_id,
                    "body": "Replying to foreign channel post",
                }).encode("utf-8"),
                headers=headers,
                method="POST",
            )
            try:
                with urllib.request.urlopen(req_send) as resp:
                    status = resp.status
                    data = json.loads(resp.read().decode("utf-8"))
            except urllib.error.HTTPError as exc:
                status = exc.code
                data = json.loads(exc.read().decode("utf-8"))
            self.assertEqual(status, 400)
            self.assertEqual(data.get("code"), "outside_channel_scope")

            # 3. Verify nothing written to foreign channel
            read_foreign = foreign_office.read(channel="foreign-channel", peek=True)
            msgs = read_foreign.get("messages", [])
            self.assertEqual(len(msgs), 1)
            msg0_id = msgs[0].get("id") or msgs[0].get("untrusted_message", {}).get("id")
            self.assertEqual(msg0_id, foreign_id)
        finally:
            real_server.shutdown()
            real_server.server_close()
            t.join(timeout=2.0)

    def test_regression_c_real_office_dedupe_and_status_mapping(self):
        """Regression test for Defect C (real Office):
        status 'error' with code dedupe_conflict -> HTTP 409;
        identical retry -> 200 with same id.
        """
        runtime_path = find_office_runtime()
        if runtime_path is None:
            self.skipTest("Real Office runtime (coordfs) not found")
        if str(runtime_path) not in sys.path:
            sys.path.insert(0, str(runtime_path))
        from coordfs.office import Office

        real_home = Path(tempfile.mkdtemp(prefix="grok_real_dedupe_"))
        real_cfg_dict = dict(self.config_dict)
        real_cfg_dict.update({
            "office_home": str(real_home),
            "office_runtime": str(runtime_path),
            "targets": {"agent-office-thread": "target-worker-agent"},
            "identity": {"harness": "grokbot", "host": "cloud", "session_id": "agent-office"},
        })
        real_cfg = GatewayConfig(real_cfg_dict)
        real_backend = OfficeBackend.from_config(real_cfg)
        real_server = create_server(real_cfg, backend=real_backend)
        port = real_server.server_address[1]
        t = threading.Thread(target=real_server.serve_forever, daemon=True)
        t.start()
        try:
            target_office = Office(agent_id="target-worker-agent", home=real_home, transport_principal="local-worker")
            target_office.presence(ttl_s=900, status="ready")

            headers = {"Authorization": f"Bearer {self.test_token}", "Content-Type": "application/json"}
            payload = {
                "target": "agent-office-thread",
                "body": "First dedupe attempt body",
                "kind": "question",
                "dedupe_key": "unique-dedupe-key-xyz",
            }
            send_url = f"http://127.0.0.1:{port}/grok-office/v1/send"

            # 1. First send -> 200
            req1 = urllib.request.Request(send_url, data=json.dumps(payload).encode("utf-8"), headers=headers, method="POST")
            with urllib.request.urlopen(req1) as resp:
                data1 = json.loads(resp.read().decode("utf-8"))
            self.assertEqual(data1["status"], "ok")
            first_id = data1["id"]

            # 2. Identical retry -> 200 with same id
            req2 = urllib.request.Request(send_url, data=json.dumps(payload).encode("utf-8"), headers=headers, method="POST")
            with urllib.request.urlopen(req2) as resp:
                data2 = json.loads(resp.read().decode("utf-8"))
            self.assertEqual(data2["status"], "ok")
            self.assertEqual(data2["id"], first_id)

            # 3. Same dedupe_key with changed body -> 409 dedupe_conflict
            conflict_payload = dict(payload)
            conflict_payload["body"] = "Different body with same key"
            req3 = urllib.request.Request(send_url, data=json.dumps(conflict_payload).encode("utf-8"), headers=headers, method="POST")
            try:
                with urllib.request.urlopen(req3) as resp:
                    status3 = resp.status
                    data3 = json.loads(resp.read().decode("utf-8"))
            except urllib.error.HTTPError as exc:
                status3 = exc.code
                data3 = json.loads(exc.read().decode("utf-8"))
            self.assertEqual(status3, 409)
            self.assertEqual(data3.get("status"), "error")
            self.assertEqual(data3.get("code"), "dedupe_conflict")
        finally:
            real_server.shutdown()
            real_server.server_close()
            t.join(timeout=2.0)

    def test_regression_d_real_office_peek_and_advance(self):
        """Regression test for Defect D (real Office):
        Reads never consume implicitly: GET inbox peek twice returns same messages;
        peek=0 -> 400 consume_not_supported;
        advance through 2nd of 3 leaves exactly 3rd unread.
        """
        runtime_path = find_office_runtime()
        if runtime_path is None:
            self.skipTest("Real Office runtime (coordfs) not found")
        if str(runtime_path) not in sys.path:
            sys.path.insert(0, str(runtime_path))
        from coordfs.office import Office

        real_home = Path(tempfile.mkdtemp(prefix="grok_real_adv_"))
        real_cfg_dict = dict(self.config_dict)
        real_cfg_dict.update({
            "office_home": str(real_home),
            "office_runtime": str(runtime_path),
            "targets": {"agent-office-thread": "sender-worker-agent"},
            "identity": {"harness": "grokbot", "host": "cloud", "session_id": "agent-office"},
        })
        real_cfg = GatewayConfig(real_cfg_dict)
        real_backend = OfficeBackend.from_config(real_cfg)
        real_server = create_server(real_cfg, backend=real_backend)
        port = real_server.server_address[1]
        t = threading.Thread(target=real_server.serve_forever, daemon=True)
        t.start()
        try:
            sender_office = Office(agent_id="sender-worker-agent", home=real_home, transport_principal="local-worker")
            # Send 3 messages to grokbot inbox
            m1 = sender_office.send(to="grokbot-cloud-agent-office", body="Message 1", kind="note", new_recipient=True)
            m2 = sender_office.send(to="grokbot-cloud-agent-office", body="Message 2", kind="note", new_recipient=True)
            m3 = sender_office.send(to="grokbot-cloud-agent-office", body="Message 3", kind="note", new_recipient=True)

            headers = {"Authorization": f"Bearer {self.test_token}"}
            inbox_url = f"http://127.0.0.1:{port}/grok-office/v1/inbox"

            # 1. Peek twice returns same messages
            req_p1 = urllib.request.Request(inbox_url, headers=headers)
            with urllib.request.urlopen(req_p1) as resp:
                data_p1 = json.loads(resp.read().decode("utf-8"))
            req_p2 = urllib.request.Request(inbox_url, headers=headers)
            with urllib.request.urlopen(req_p2) as resp:
                data_p2 = json.loads(resp.read().decode("utf-8"))

            self.assertEqual(len(data_p1["messages"]), 3)
            self.assertEqual(len(data_p2["messages"]), 3)
            self.assertEqual([m["id"] for m in data_p1["messages"]], [m["id"] for m in data_p2["messages"]])

            # 2. peek=0 -> 400 consume_not_supported
            req_bad = urllib.request.Request(f"{inbox_url}?peek=0", headers=headers)
            try:
                with urllib.request.urlopen(req_bad) as resp:
                    status_bad = resp.status
                    data_bad = json.loads(resp.read().decode("utf-8"))
            except urllib.error.HTTPError as exc:
                status_bad = exc.code
                data_bad = json.loads(exc.read().decode("utf-8"))
            self.assertEqual(status_bad, 400)
            self.assertEqual(data_bad.get("code"), "consume_not_supported")

            # 3. Advance through 2nd of 3
            through_id = m2["id"]
            adv_headers = {"Authorization": f"Bearer {self.test_token}", "Content-Type": "application/json"}
            req_adv = urllib.request.Request(
                f"http://127.0.0.1:{port}/grok-office/v1/advance",
                data=json.dumps({"source": "inbox", "through_id": through_id}).encode("utf-8"),
                headers=adv_headers,
                method="POST",
            )
            with urllib.request.urlopen(req_adv) as resp:
                adv_status = resp.status
                adv_data = json.loads(resp.read().decode("utf-8"))
            self.assertEqual(adv_status, 200)
            self.assertEqual(adv_data, {"status": "ok", "advanced": 2})

            # 4. Leaves exactly 3rd unread
            req_p3 = urllib.request.Request(inbox_url, headers=headers)
            with urllib.request.urlopen(req_p3) as resp:
                data_p3 = json.loads(resp.read().decode("utf-8"))
            self.assertEqual(len(data_p3["messages"]), 1)
            self.assertEqual(data_p3["messages"][0]["id"], m3["id"])
        finally:
            real_server.shutdown()
            real_server.server_close()
            t.join(timeout=2.0)

    def test_regression_e_real_office_correlation_filter_backlog(self):
        """Regression test for Defect E (real Office):
        25 unrelated unread messages then the real reply; GET /v1/inbox?reply_to=<id>
        returns exactly the reply.
        """
        runtime_path = find_office_runtime()
        if runtime_path is None:
            self.skipTest("Real Office runtime (coordfs) not found")
        if str(runtime_path) not in sys.path:
            sys.path.insert(0, str(runtime_path))
        from coordfs.office import Office

        real_home = Path(tempfile.mkdtemp(prefix="grok_real_filter_"))
        real_cfg_dict = dict(self.config_dict)
        real_cfg_dict.update({
            "office_home": str(real_home),
            "office_runtime": str(runtime_path),
            "targets": {"agent-office-thread": "worker-peer"},
            "identity": {"harness": "grokbot", "host": "cloud", "session_id": "agent-office"},
        })
        real_cfg = GatewayConfig(real_cfg_dict)
        real_backend = OfficeBackend.from_config(real_cfg)
        real_server = create_server(real_cfg, backend=real_backend)
        port = real_server.server_address[1]
        t = threading.Thread(target=real_server.serve_forever, daemon=True)
        t.start()
        try:
            worker_office = Office(agent_id="worker-peer", home=real_home, transport_principal="local-worker")
            worker_office.presence(ttl_s=900, status="ready")

            # 25 unrelated messages to grokbot
            for i in range(25):
                worker_office.send(to="grokbot-cloud-agent-office", body=f"Unrelated message {i}", kind="note", new_recipient=True)

            target_reply_to = "99998888777766665555444433332222"
            # Now the real reply
            real_reply = worker_office.send(
                to="grokbot-cloud-agent-office",
                body="The actual awaited reply",
                kind="note",
                reply_to=target_reply_to,
                new_recipient=True,
            )

            # Another 5 unrelated messages
            for i in range(5):
                worker_office.send(to="grokbot-cloud-agent-office", body=f"Later unrelated message {i}", kind="note", new_recipient=True)

            # Filter with reply_to
            headers = {"Authorization": f"Bearer {self.test_token}"}
            filter_url = f"http://127.0.0.1:{port}/grok-office/v1/inbox?reply_to={target_reply_to}"
            req = urllib.request.Request(filter_url, headers=headers)
            with urllib.request.urlopen(req) as resp:
                data = json.loads(resp.read().decode("utf-8"))

            self.assertEqual(data["status"], "ok")
            self.assertEqual(len(data["messages"]), 1)
            self.assertEqual(data["messages"][0]["id"], real_reply["id"])
            self.assertEqual(data["messages"][0]["reply_to"], target_reply_to)
            self.assertEqual(data["messages"][0]["body"], "The actual awaited reply")
        finally:
            real_server.shutdown()
            real_server.server_close()
            t.join(timeout=2.0)

    def test_regression_g_client_ask_stable_dedupe_and_filtering(self):
        """Regression test for Defect G:
        Client ask uses stable dedupe key sha256(target + '\n' + kind + '\n' + body)[:40] without time bucket;
        fetches target_agents from health, polls inbox?reply_to=<sent_id>&peek=1;
        accepts only reply_to == sent_id AND from == target_agents[alias];
        ignores body matches or messages from other senders.
        """
        client_env = {
            "GROK_OFFICE_URL": self.base_url,
            "GROK_OFFICE_TOKEN": self.test_token,
        }

        # Stable dedupe key check
        target = "agent-office-thread"
        body = "Stable dedupe question content"
        expected_dedupe = hashlib.sha256(f"{target}\nquestion\n{body}".encode("utf-8")).hexdigest()[:40]

        sent_id = "0123456789abcdef0123456789abcdef"
        self.backend.send_result = {"id": sent_id, "ts": 1700000000, "status": "sent"}

        accepted_msg_id = "aaaabbbbccccddddeeeeffff00001111"
        self.backend.read_result = {
            "messages": [
                {
                    "id": "wrong111111111111111111111111111",
                    "from": "claude-ailab-main",
                    "reply_to": None,
                    "body": f"Mentioning {sent_id} in body only",
                },
                {
                    "id": "wrong222222222222222222222222222",
                    "from": "impostor-agent",
                    "reply_to": sent_id,
                    "body": "Spoofed reply from impostor",
                },
                {
                    "id": accepted_msg_id,
                    "from": "claude-ailab-main",
                    "reply_to": sent_id,
                    "body": "Legitimate answer from correct agent",
                },
            ],
            "remaining": 0,
            "cursor_advanced": False,
        }

        buf = io.StringIO()
        code = run_client(
            [
                "ask",
                "--target",
                target,
                "--body",
                body,
                "--timeout-s",
                "5",
                "--poll-s",
                "0.05",
            ],
            env=client_env,
            stdout=buf,
        )
        self.assertEqual(code, 0)
        accepted = json.loads(buf.getvalue().strip())
        self.assertEqual(accepted["id"], accepted_msg_id)
        self.assertEqual(accepted["from"], "claude-ailab-main")
        self.assertEqual(accepted["body"], "Legitimate answer from correct agent")

        # Verify sent dedupe key
        last_send = self.backend.send_calls[-1]
        self.assertEqual(last_send["dedupe_key"], expected_dedupe)

        # Verify client advance subcommand
        adv_buf = io.StringIO()
        adv_code = run_client(
            [
                "advance",
                "--source",
                "inbox",
                "--through-id",
                accepted_msg_id,
            ],
            env=client_env,
            stdout=adv_buf,
        )
        self.assertEqual(adv_code, 0)

    # --- Codex executed reproductions 2026-10-23 (ACK-only, large backlog, concurrent advance) ---

    def _real_gateway(self, prefix):
        runtime_path = find_office_runtime()
        if runtime_path is None:
            self.skipTest("Real Office runtime (coordfs) not found")
        if str(runtime_path) not in sys.path:
            sys.path.insert(0, str(runtime_path))
        from coordfs.office import Office
        home = Path(tempfile.mkdtemp(prefix=prefix))
        cfg_dict = dict(self.config_dict)
        cfg_dict.update({
            "office_home": str(home),
            "office_runtime": str(runtime_path),
            "targets": {"agent-office-thread": "target-worker-agent"},
            "identity": {"harness": "grokbot", "host": "cloud", "session_id": "agent-office"},
        })
        cfg = GatewayConfig(cfg_dict)
        server = create_server(cfg, backend=OfficeBackend.from_config(cfg))
        th = threading.Thread(target=server.serve_forever, daemon=True)
        th.start()
        self.addCleanup(lambda: (server.shutdown(), server.server_close(), th.join(timeout=2.0)))
        worker = Office(agent_id="target-worker-agent", home=home, transport_principal="local-worker")
        worker.presence(ttl_s=900, status="ready")
        return server.server_address[1], worker

    def _call(self, port, path, method="GET", body=None):
        data = json.dumps(body).encode("utf-8") if body is not None else None
        headers = {"Authorization": f"Bearer {self.test_token}"}
        if data is not None:
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(f"http://127.0.0.1:{port}/grok-office/v1/{path}", data=data,
                                     headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode("utf-8"))

    def test_regression_ack_only_is_not_a_result_real_office(self):
        port, worker = self._real_gateway("grok_real_ackonly_")
        status, sent = self._call(port, "send", "POST",
                                  {"target": "agent-office-thread", "kind": "question", "body": "Q needing work"})
        self.assertEqual(status, 200)
        qid = sent["id"]
        inbox = worker.read(limit=10, peek=False)
        self.assertTrue(any((m.get("untrusted_message") or m).get("id") == qid for m in inbox["messages"]))
        ack = worker.ack(qid, "seen only")
        self.assertEqual(ack.get("status"), "ok")
        status, data = self._call(port, f"inbox?reply_to={qid}")
        self.assertEqual(status, 200)
        self.assertEqual(data["messages"], [], "an ACK envelope must never be returned as a reply")
        status, data = self._call(port, "inbox")
        acks = [m for m in data["messages"] if m.get("ack_of") == qid]
        self.assertEqual(len(acks), 1, "plain inbox must keep ack_of so clients can tell receipts apart")
        real = worker.send(to="grokbot-cloud-agent-office", body="Real result", kind="note",
                           reply_to=qid, new_recipient=True)
        status, data = self._call(port, f"inbox?reply_to={qid}")
        self.assertEqual([m["id"] for m in data["messages"]], [real["id"]])

    def test_regression_client_ask_rejects_ack_envelope(self):
        client_env = {"GROK_OFFICE_URL": self.base_url, "GROK_OFFICE_TOKEN": self.test_token}
        sent_id = "0123456789abcdef0123456789abcdef"
        self.backend.send_result = {"id": sent_id, "ts": 1700000000, "status": "sent"}
        self.backend.read_result = {
            "messages": [{"id": "acacacacacacacacacacacacacacacac", "from": "claude-ailab-main",
                          "reply_to": sent_id, "ack_of": sent_id, "body": "ACK only"}],
            "remaining": 0, "cursor_advanced": False,
        }
        buf = io.StringIO()
        code = run_client(["ask", "--target", "agent-office-thread", "--body", "ack-only probe",
                           "--timeout-s", "1", "--poll-s", "0.05"], env=client_env, stdout=buf)
        self.assertEqual(code, 3, "ask must time out instead of accepting an ACK as the result")

    def test_regression_large_backlog_reply_visible_real_office(self):
        port, worker = self._real_gateway("grok_real_bigbacklog_")
        big = "x" * 7900
        for i in range(25):
            worker.send(to="grokbot-cloud-agent-office", body=f"{i} {big}", kind="note", new_recipient=True)
        target = "99998888777766665555444433332222"
        real = worker.send(to="grokbot-cloud-agent-office", body="awaited", kind="note",
                           reply_to=target, new_recipient=True)
        status, data = self._call(port, f"inbox?reply_to={target}")
        self.assertEqual(status, 200)
        self.assertEqual([m["id"] for m in data["messages"]], [real["id"]])
        status, data = self._call(port, "inbox?limit=1")
        self.assertEqual(status, 200)
        self.assertGreaterEqual(data["remaining"] + len(data["messages"]), 26, "filter must not consume")

    def test_regression_backlog_beyond_scan_window_is_explicit_real_office(self):
        port, worker = self._real_gateway("grok_real_backlog205_")
        from coordfs.office import Office
        # Office rate-limits each sender (60/min): spread the noise over 5 senders.
        senders = [Office(agent_id=f"noise-sender-{n}", home=worker.home, transport_principal="local-worker")
                   for n in range(5)]
        for i in range(205):
            res = senders[i % 5].send(to="grokbot-cloud-agent-office", body=f"noise {i}", kind="note",
                                      new_recipient=True)
            self.assertEqual(res.get("status"), "ok", res)
        target = "11112222333344445555666677778888"
        worker.send(to="grokbot-cloud-agent-office", body="late answer", kind="note",
                    reply_to=target, new_recipient=True)
        status, data = self._call(port, f"inbox?reply_to={target}")
        self.assertEqual(status, 200)
        self.assertEqual(data["messages"], [])
        self.assertTrue(data["scan_incomplete"], "must signal an incomplete scan, not a silent empty result")
        status, data = self._call(port, "inbox?limit=1")
        self.assertEqual(data["remaining"] + len(data["messages"]), 206, "nothing may be consumed")

    def test_regression_concurrent_advance_is_atomic_real_office(self):
        port, worker = self._real_gateway("grok_real_advrace_")
        ids = [worker.send(to="grokbot-cloud-agent-office", body=f"m{i}", kind="note",
                           new_recipient=True)["id"] for i in range(4)]
        results = []
        barrier = threading.Barrier(2)

        def go():
            barrier.wait()
            results.append(self._call(port, "advance", "POST", {"source": "inbox", "through_id": ids[1]}))

        threads = [threading.Thread(target=go) for _ in range(2)]
        for th in threads:
            th.start()
        for th in threads:
            th.join(timeout=30)
        codes = sorted(code for code, _ in results)
        self.assertEqual(codes, [200, 404])
        status, data = self._call(port, "inbox")
        self.assertEqual([m["id"] for m in data["messages"]], ids[2:])


if __name__ == "__main__":
    unittest.main()
