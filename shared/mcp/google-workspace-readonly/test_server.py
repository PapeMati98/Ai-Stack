"""Offline mock-only tests: no Google calls, no credentials, no network.

All Google HTTP is faked in-process. Any scratch fixtures live only below
the current test-owned fixture directory.
"""

import base64
import io
import json
import os
import subprocess
import sys
import unittest
from pathlib import Path

import server as ws

HERE = Path(__file__).resolve().parent

ACCOUNT = "owner@example.com"


def _config_root() -> str:
    if os.name == "nt":
        return r"C:\private\ws-oauth-test"
    return "/private/ws-oauth-test"


def _entrypoint() -> str:
    if os.name == "nt":
        return r"C:\Program Files\Google\Cloud SDK\google-cloud-sdk\bin\gcloud.py"
    return "/opt/google-cloud-sdk/bin/gcloud.py"


def make_policy(account: str = ACCOUNT) -> ws.Policy:
    return ws.load_policy(json.dumps({
        "account": account,
        "config_root": _config_root(),
        "gcloud_python_entrypoint": _entrypoint(),
    }))


def _cred_file(policy: ws.Policy) -> str:
    sep = "\\" if os.name == "nt" else "/"
    return policy.config_root + sep + "creds.json"


class FakeBackend:
    """Injectable transport/auth double: records GETs, serves queued data."""

    def __init__(self, policy, identity_email=None, get_handler=None):
        self.policy = policy
        self.identity_email = identity_email if identity_email is not None else policy.account
        self.get_handler = get_handler
        self.calls: list[tuple[str, dict]] = []
        self.identity_calls = 0

    def identity(self, timeout, maximum):
        self.identity_calls += 1
        if self.identity_email != self.policy.account:
            raise ws.SafeToolError("OAuth account does not match policy")
        return self.identity_email

    def _get(self, url, params, timeout, maximum):
        self.calls.append((url, dict(params)))
        if self.get_handler is None:
            raise AssertionError("unexpected GET " + url)
        return self.get_handler(url, dict(params))


def session_with(policy=None, **kwargs) -> tuple[ws.Session, FakeBackend]:
    policy = policy or make_policy()
    backend = FakeBackend(policy, **kwargs)
    return ws.Session(policy, backend), backend


def b64(text: str) -> str:
    return base64.urlsafe_b64encode(text.encode()).decode().rstrip("=")


class PolicyTest(unittest.TestCase):
    def test_exact_keys_accepted(self):
        policy = make_policy()
        self.assertEqual(policy.account, ACCOUNT)

    def test_unknown_policy_field_rejected(self):
        raw = json.dumps({"account": ACCOUNT, "config_root": _config_root(),
                          "gcloud_python_entrypoint": _entrypoint(), "extra": 1})
        with self.assertRaises(ws.PolicyError):
            ws.load_policy(raw)

    def test_missing_policy_field_rejected(self):
        raw = json.dumps({"account": ACCOUNT, "config_root": _config_root()})
        with self.assertRaises(ws.PolicyError):
            ws.load_policy(raw)

    def test_bad_account_rejected(self):
        for account in ["not-an-email", "", "a@b", 42, None]:
            with self.assertRaises(ws.PolicyError, msg=repr(account)):
                ws.load_policy(json.dumps({"account": account, "config_root": _config_root(),
                                           "gcloud_python_entrypoint": _entrypoint()}))

    def test_relative_paths_rejected(self):
        raw = json.dumps({"account": ACCOUNT, "config_root": "relative/path",
                          "gcloud_python_entrypoint": _entrypoint()})
        with self.assertRaises(ws.PolicyError):
            ws.load_policy(raw)

    def test_wrong_entrypoint_name_rejected(self):
        sep = "\\" if os.name == "nt" else "/"
        entry = _config_root() + sep + "other.py"
        raw = json.dumps({"account": ACCOUNT, "config_root": _config_root(),
                          "gcloud_python_entrypoint": entry})
        with self.assertRaises(ws.PolicyError):
            ws.load_policy(raw)

    def test_duplicate_json_keys_rejected(self):
        raw = ('{"account": "a@b.co", "account": "c@d.co", '
               '"config_root": "C:/x", "gcloud_python_entrypoint": "C:/x/gcloud.py"}')
        with self.assertRaises(ws.PolicyError):
            ws.load_policy(raw)

    def test_credential_outside_config_root_rejected(self):
        policy = make_policy()
        other = ("C:\\other\\creds.json" if os.name == "nt" else "/other/creds.json")
        with self.assertRaises(ws.PolicyError):
            ws.GoogleBackend(policy, other, session_factory=lambda p, c: None)


class ScopeTest(unittest.TestCase):
    def test_exact_scopes_accepted(self):
        ws.check_granted_scopes(set(ws.SCOPES))

    def test_widened_scopes_rejected(self):
        with self.assertRaises(ws.SafeToolError):
            ws.check_granted_scopes(set(ws.SCOPES) | {"https://www.googleapis.com/auth/gmail.send"})

    def test_narrowed_scopes_rejected(self):
        narrowed = set(ws.SCOPES) - {"https://www.googleapis.com/auth/drive.readonly"}
        with self.assertRaises(ws.SafeToolError):
            ws.check_granted_scopes(narrowed)

    def test_empty_scopes_rejected(self):
        with self.assertRaises(ws.SafeToolError):
            ws.check_granted_scopes([])


class RejectionIsolationTest(unittest.TestCase):
    def test_unknown_tool_never_touches_backend(self):
        session, backend = session_with()
        with self.assertRaises(ws.SafeToolError):
            ws.dispatch_tool(session, "gmail_send", {})
        with self.assertRaises(ws.SafeToolError):
            ws.dispatch_tool(session, "nope", {})
        self.assertEqual(backend.calls, [])
        self.assertEqual(backend.identity_calls, 0)

    def test_extra_keys_never_reach_backend(self):
        session, backend = session_with()
        for name, args in [
                ("workspace_context", {"x": 1}),
                ("gmail_search", {"query": "a", "bogus": 1}),
                ("gmail_read", {"message_id": "m1", "bogus": 1}),
                ("gmail_labels", {"bogus": 1}),
                ("drive_search", {"query": "a", "bogus": 1}),
                ("drive_metadata", {"file_id": "f1", "bogus": 1}),
                ("drive_read_text", {"file_id": "f1", "bogus": 1}),
                ("drive_storage", {"bogus": 1}),
        ]:
            with self.assertRaises(ws.SafeToolError, msg=name):
                ws.dispatch_tool(session, name, args)
        self.assertEqual(backend.calls, [])
        self.assertEqual(backend.identity_calls, 0)

    def test_bad_ids_rejected_before_auth(self):
        session, backend = session_with()
        for name, args in [
                ("gmail_read", {"message_id": "../evil"}),
                ("gmail_read", {"message_id": "has space"}),
                ("drive_metadata", {"file_id": "a/b"}),
                ("drive_read_text", {"file_id": ""}),
                ("gmail_search", {"query": "ok", "max_results": 51}),
                ("gmail_search", {"query": "ok", "max_results": 0}),
                ("drive_search", {"query": "x\x00y"}),
        ]:
            with self.assertRaises(ws.SafeToolError, msg=repr(args)):
                ws.dispatch_tool(session, name, args)
        self.assertEqual(backend.calls, [])
        self.assertEqual(backend.identity_calls, 0)


class ToolHappyPathTest(unittest.TestCase):
    def test_all_eight_tools(self):
        text_payload = {"mimeType": "multipart/mixed", "parts": [
            {"mimeType": "text/plain", "body": {"data": b64("hello world")}},
            {"mimeType": "text/html", "body": {"data": b64("<b>x</b>")}},
        ]}
        seen: list[str] = []

        def handler(url, params):
            seen.append(url)
            if url.endswith("/oauth2/v2/userinfo"):
                raise AssertionError("FakeBackend.identity answers userinfo")
            if url.endswith("/gmail/v1/users/me/profile"):
                return {"emailAddress": ACCOUNT}
            if url.endswith("/gmail/v1/users/me/messages"):
                return {"messages": [{"id": "m1", "threadId": "t1"}]}
            if "/gmail/v1/users/me/messages/" in url:
                return {"id": "m1", "threadId": "t1",
                        "payload": {"headers": [
                            {"name": "Subject", "value": "Hi"},
                            {"name": "X-Secret", "value": "drop"}],
                            **text_payload}}
            if url.endswith("/gmail/v1/users/me/labels"):
                return {"labels": [{"id": "INBOX", "name": "INBOX"}]}
            if url.endswith("/drive/v3/about"):
                return {"user": {"emailAddress": ACCOUNT},
                        "storageQuota": {"limit": "100", "usage": "40",
                                         "usageInDrive": "30", "usageInDriveTrash": "2"}}
            if url.endswith("/drive/v3/files"):
                return {"files": [{
                    "id": "f1", "name": "notes.txt", "mimeType": "text/plain",
                    "size": "9", "modifiedTime": "2026-01-01T00:00:00.000Z",
                    "md5Checksum": "d" * 32}]}
            if url.endswith("/export"):
                return {"text": "sheet,data"}
            if "/drive/v3/files/" in url and params.get("alt") == "media":
                return {"text": "file text"}
            if "/drive/v3/files/" in url:
                return {"id": "f1", "name": "notes.txt", "mimeType": "text/plain"}
            raise AssertionError("unexpected " + url)

        policy = make_policy()
        backend = FakeBackend(policy, get_handler=handler)
        session = ws.Session(policy, backend)
        self.assertEqual(ws.dispatch_tool(session, "workspace_context", {})["account"], ACCOUNT)
        search = ws.dispatch_tool(session, "gmail_search", {"query": "from:x"})
        self.assertEqual(search["messages"], [{"id": "m1", "threadId": "t1"}])
        read = ws.dispatch_tool(session, "gmail_read", {"message_id": "m1"})
        self.assertEqual(read["text"], "hello world")
        self.assertEqual(read["headers"], {"Subject": "Hi"})
        self.assertEqual(ws.dispatch_tool(session, "gmail_labels", {}),
                         {"labels": [{"id": "INBOX", "name": "INBOX"}]})
        dsearch = ws.dispatch_tool(session, "drive_search", {"query": "notes"})
        self.assertEqual(dsearch["files"][0]["id"], "f1")
        self.assertEqual(ws.dispatch_tool(session, "drive_metadata", {"file_id": "f1"})["name"],
                         "notes.txt")
        dtext = ws.dispatch_tool(session, "drive_read_text", {"file_id": "f1"})
        self.assertEqual(dtext["text"], "file text")
        storage = ws.dispatch_tool(session, "drive_storage", {})
        self.assertEqual(storage["storageQuota"]["usage"], "40")
        self.assertTrue(all(u.startswith("https://") for u in seen))

    def test_drive_doc_export_and_unsupported_binary(self):
        def handler(url, params):
            if url.endswith("/export"):
                return {"text": "doc text"}
            return {"id": "d1", "name": "Doc",
                    "mimeType": "application/vnd.google-apps.document"}

        policy = make_policy()
        out = ws.dispatch_tool(ws.Session(policy, FakeBackend(policy, get_handler=handler)),
                               "drive_read_text", {"file_id": "d1"})
        self.assertEqual(out["text"], "doc text")

        def binary(url, params):
            return {"id": "b1", "name": "clip.mp4", "mimeType": "video/mp4"}
        session2 = ws.Session(policy, FakeBackend(policy, get_handler=binary))
        with self.assertRaises(ws.SafeToolError) as ctx:
            ws.dispatch_tool(session2, "drive_read_text", {"file_id": "b1"})
        self.assertIn("unsupported format", str(ctx.exception))


class IdentityMismatchTest(unittest.TestCase):
    def test_policy_account_mismatch_rejected(self):
        session, _ = session_with(identity_email="stranger@example.com")
        with self.assertRaises(ws.SafeToolError):
            ws.dispatch_tool(session, "gmail_labels", {})

    def test_gmail_profile_mismatch_rejected(self):
        def handler(url, params):
            return {"emailAddress": "stranger@example.com"}
        session, _ = session_with(get_handler=handler)
        with self.assertRaises(ws.SafeToolError):
            ws.dispatch_tool(session, "gmail_labels", {})

    def test_drive_owner_mismatch_rejected(self):
        def handler(url, params):
            return {"user": {"emailAddress": "stranger@example.com"},
                    "storageQuota": {"usage": "1"}}
        session, _ = session_with(get_handler=handler)
        with self.assertRaises(ws.SafeToolError):
            ws.dispatch_tool(session, "drive_storage", {})

    def test_wrong_message_identity_rejected(self):
        def handler(url, params):
            if url.endswith("/profile"):
                return {"emailAddress": ACCOUNT}
            return {"id": "other", "payload": {}}
        session, _ = session_with(get_handler=handler)
        with self.assertRaises(ws.SafeToolError):
            ws.dispatch_tool(session, "gmail_read", {"message_id": "m1"})

    def test_wrong_file_identity_rejected(self):
        def handler(url, params):
            return {"id": "other", "name": "x", "mimeType": "text/plain"}
        session, _ = session_with(get_handler=handler)
        with self.assertRaises(ws.SafeToolError):
            ws.dispatch_tool(session, "drive_metadata", {"file_id": "f1"})


class MimeBoundTest(unittest.TestCase):
    def _deep_payload(self, depth: int):
        node: dict = {"mimeType": "text/plain", "body": {"data": b64("deep")}}
        for _ in range(depth):
            node = {"mimeType": "multipart/mixed", "parts": [node]}
        return node

    def test_recursion_depth_bounded(self):
        within = ws.gmail_text(self._deep_payload(ws.MAX_MIME_DEPTH - 1))
        self.assertIn("deep", within)
        over = ws.gmail_text(self._deep_payload(ws.MAX_MIME_DEPTH + 30))
        self.assertLessEqual(len(over.encode()), ws.TOOL_CONTENT_LIMIT_BYTES)

    def test_part_count_bounded(self):
        payload = {"mimeType": "multipart/mixed", "parts": [
            {"mimeType": "text/plain", "body": {"data": b64("p")}} for _ in range(200)]}
        text = ws.gmail_text(payload)
        self.assertLessEqual(len(text.encode()), ws.TOOL_CONTENT_LIMIT_BYTES)

    def test_decoded_content_capped(self):
        big = b64("A" * (ws.TOOL_CONTENT_LIMIT_BYTES + 5000))
        payload = {"mimeType": "text/plain", "body": {"data": big}}
        text = ws.gmail_text(payload)
        self.assertLessEqual(len(text.encode()), ws.TOOL_CONTENT_LIMIT_BYTES)


class FakeResponse:
    def __init__(self, status_code=200, text="", redirect=False, history=None, chunks=None):
        self.status_code = status_code
        self._text = text
        self.is_redirect = redirect
        self.history = history or []
        self._chunks = chunks
        self.closed = False

    def iter_content(self, chunk_size=4096):
        if self._chunks is not None:
            for chunk in self._chunks:
                yield chunk
            return
        data = self._text.encode()
        for i in range(0, len(data), chunk_size):
            yield data[i:i + chunk_size]

    def close(self):
        self.closed = True


class FakeTransportSession:
    """Stands in for requests AuthorizedSession; no network."""

    def __init__(self, handler):
        self._handler = handler
        self.trust_env = False
        self._google_refreshed = True
        self.requests: list[tuple[str, str]] = []

    def request(self, method, url, **kwargs):
        self.requests.append((method, url))
        return self._handler(method, url, kwargs)


def backend_with(handler, policy=None):
    policy = policy or make_policy()
    transport = FakeTransportSession(handler)
    backend = ws.GoogleBackend(policy, _cred_file(policy),
                               session_factory=lambda p, c: transport)
    return backend, transport


class TransportTest(unittest.TestCase):
    URL = "https://gmail.googleapis.com/gmail/v1/users/me/labels"

    def test_exact_allowlist_paths(self):
        seen: list[str] = []

        def handler(method, url, kwargs):
            seen.append(url)
            return FakeResponse(text="{}")

        backend, _ = backend_with(handler)
        for url in [
                "https://www.googleapis.com/oauth2/v2/userinfo",
                "https://gmail.googleapis.com/gmail/v1/users/me/profile",
                "https://gmail.googleapis.com/gmail/v1/users/me/messages",
                "https://gmail.googleapis.com/gmail/v1/users/me/messages/m1",
                "https://gmail.googleapis.com/gmail/v1/users/me/labels",
                "https://www.googleapis.com/drive/v3/about",
                "https://www.googleapis.com/drive/v3/files",
                "https://www.googleapis.com/drive/v3/files/f1",
                "https://www.googleapis.com/drive/v3/files/f1/export",
        ]:
            backend._get(url, {}, 5.0, 1024)
        self.assertEqual(len(seen), 9)

    def test_arbitrary_url_never_reaches_transport(self):
        def handler(method, url, kwargs):
            raise AssertionError("transport must not be reached")

        backend, transport = backend_with(handler)
        for url in [
                "https://gmail.googleapis.com/gmail/v1/users/me/messages/m1/delete",
                "https://evil.example.com/steal",
                "http://gmail.googleapis.com/gmail/v1/users/me/labels",
                "https://gmail.googleapis.com/gmail/v1/users/me/labels?x=1",
                "https://gmail.googleapis.com/gmail/v1/users/me/messages/%2e%2e",
        ]:
            with self.assertRaises(ws.SafeToolError, msg=url):
                backend._get(url, {}, 5.0, 1024)
        self.assertEqual(transport.requests, [])

    def test_redirect_rejected(self):
        def handler(method, url, kwargs):
            return FakeResponse(redirect=True, history=[object()], text="{}")
        backend, _ = backend_with(handler)
        with self.assertRaises(ws.SafeToolError) as ctx:
            backend._get(self.URL, {}, 5.0, 1024)
        self.assertEqual(str(ctx.exception), "redirect rejected")

    def test_timeout_redacted(self):
        def handler(method, url, kwargs):
            raise TimeoutError("underlying socket secret-timeout detail")
        backend, _ = backend_with(handler)
        with self.assertRaises(ws.SafeToolError) as ctx:
            backend._get(self.URL, {}, 5.0, 1024)
        self.assertNotIn("secret-timeout", str(ctx.exception))

    def test_malformed_response_rejected(self):
        def handler(method, url, kwargs):
            return FakeResponse(text="{not json")
        backend, _ = backend_with(handler)
        with self.assertRaises(ws.SafeToolError):
            backend._get(self.URL, {}, 5.0, 1024)

    def test_oversized_response_rejected(self):
        def handler(method, url, kwargs):
            return FakeResponse(chunks=[b"x" * 4096] * 600)
        backend, _ = backend_with(handler)
        with self.assertRaises(ws.SafeToolError) as ctx:
            backend._get(self.URL, {}, 5.0, 1024)
        self.assertEqual(str(ctx.exception), "response exceeds limit")

    def test_error_details_redacted(self):
        def handler(method, url, kwargs):
            raise RuntimeError("token ya29.SECRET client_secret s3cr3t")
        backend, _ = backend_with(handler)
        with self.assertRaises(ws.SafeToolError) as ctx:
            backend._get(self.URL, {}, 5.0, 1024)
        message = str(ctx.exception)
        self.assertNotIn("ya29", message)
        self.assertNotIn("s3cr3t", message)

    def test_first_call_forces_refresh_then_caches(self):
        refreshed: list[bool] = []

        class Creds:
            def refresh(self, request):
                refreshed.append(True)

        def handler(method, url, kwargs):
            return FakeResponse(text="{}")

        policy = make_policy()
        transport = FakeTransportSession(handler)
        transport._google_refreshed = False
        transport._google_creds = Creds()
        transport._google_bounded_request = object()
        backend = ws.GoogleBackend(policy, _cred_file(policy),
                                   session_factory=lambda p, c: transport)
        backend._get(self.URL, {}, 5.0, 1024)
        backend._get(self.URL, {}, 5.0, 1024)
        self.assertEqual(len(refreshed), 1)

    def test_failed_refresh_is_redacted(self):
        class Creds:
            def refresh(self, request):
                raise RuntimeError("invalid_grant secret detail")

        def handler(method, url, kwargs):
            return FakeResponse(text="{}")

        policy = make_policy()
        transport = FakeTransportSession(handler)
        transport._google_refreshed = False
        transport._google_creds = Creds()
        transport._google_bounded_request = object()
        backend = ws.GoogleBackend(policy, _cred_file(policy),
                                   session_factory=lambda p, c: transport)
        with self.assertRaises(ws.SafeToolError) as ctx:
            backend._get(self.URL, {}, 5.0, 1024)
        self.assertNotIn("invalid_grant", str(ctx.exception))


class ProtocolTest(unittest.TestCase):
    def _session(self, **kwargs):
        return session_with(**kwargs)[0]

    def _rpc(self, session, messages):
        stdin = io.BytesIO(b"".join(
            json.dumps(m).encode() + b"\n" for m in messages))
        stdout = io.BytesIO()
        code = ws.serve(stdin, stdout, session)
        self.assertEqual(code, 0)
        return [json.loads(line) for line in stdout.getvalue().splitlines()]

    def test_initialize_list_call_roundtrip(self):
        session = self._session()
        out = self._rpc(session, [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize",
             "params": {"protocolVersion": "2024-11-05"}},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
            {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
             "params": {"name": "workspace_context", "arguments": {}}},
        ])
        self.assertEqual(out[0]["result"]["protocolVersion"], "2024-11-05")
        names = {t["name"] for t in out[1]["result"]["tools"]}
        self.assertEqual(names, set(ws.TOOLS))
        self.assertTrue(all(t["annotations"]["readOnlyHint"] for t in out[1]["result"]["tools"]))
        body = json.loads(out[2]["result"]["content"][0]["text"])
        self.assertEqual(body["account"], ACCOUNT)

    def test_tool_errors_are_results_not_crashes(self):
        session = self._session()
        out = self._rpc(session, [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
             "params": {"name": "gmail_send", "arguments": {}}},
            {"jsonrpc": "2.0", "id": 3, "method": "bogus/method"},
        ])
        self.assertTrue(out[1]["result"]["isError"])
        self.assertEqual(out[2]["error"]["code"], -32601)

    def test_oversized_frame_rejected(self):
        session = self._session()
        stdin = io.BytesIO(b'{"jsonrpc":"2.0","id":1,"method":"ping","x":"'
                           + b"y" * (ws.MAX_MESSAGE_BYTES + 8) + b'"}\n')
        stdout = io.BytesIO()
        self.assertEqual(ws.serve(stdin, stdout, session), 1)

    def test_subprocess_protocol_end_to_end(self):
        child = "\n".join([
            "import sys",
            "sys.path.insert(0, sys.argv[1])",
            "import server as ws",
            f"policy = ws.Policy(account={ACCOUNT!r},"
            f" config_root={_config_root()!r},"
            f" gcloud_python_entrypoint={_entrypoint()!r})",
            "class B:",
            "    def identity(self, t, m): return policy.account",
            "    def _get(self, u, p, t, m):",
            "        return {'emailAddress': policy.account}",
            "sys.exit(ws.serve(sys.stdin.buffer, sys.stdout.buffer,"
            " ws.Session(policy, B())))",
        ])
        proc = subprocess.run(
            [sys.executable, "-B", "-I", "-c", child, str(HERE)],
            input=b"".join(json.dumps(m).encode() + b"\n" for m in [
                {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
                {"jsonrpc": "2.0", "id": 2, "method": "ping"},
                {"jsonrpc": "2.0", "id": 3, "method": "tools/list"},
                {"jsonrpc": "2.0", "id": 4, "method": "tools/call",
                 "params": {"name": "gmail_labels", "arguments": {}}},
            ]),
            capture_output=True, timeout=60,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        self.assertEqual(proc.returncode, 0, proc.stderr.decode()[-2000:])
        lines = proc.stdout.splitlines()
        self.assertEqual(len(lines), 4)
        replies = [json.loads(line) for line in lines]
        self.assertIn("serverInfo", replies[0]["result"])
        self.assertEqual(replies[1]["result"], {})
        self.assertEqual(len(replies[2]["result"]["tools"]), 8)
        self.assertFalse(replies[3]["result"]["isError"])


if __name__ == "__main__":
    unittest.main()
