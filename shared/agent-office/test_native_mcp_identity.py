"""Native Codex MCP identity binding: actual Office methods, retained evidence paths."""

from __future__ import annotations

import io
import json
import os
import sys
import threading
import unittest
import uuid
from pathlib import Path
from unittest import mock

PACKAGE_ROOT = Path(__file__).resolve().parent
if str(PACKAGE_ROOT) not in sys.path:
    sys.path.insert(0, str(PACKAGE_ROOT))

from coordfs.native_mcp_identity import (
    NativeIdentityGate,
    bind_office_to_thread,
    extract_thread_id,
    is_weak_codex_fallback,
    normalize_thread_id,
)
from coordfs.office import Office, inbox_path
from coordfs.office_mcp import OfficeMcpServer

UUID_A = "123e4567-e89b-12d3-a456-426614174000"
UUID_B = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
SESSION_KEYS = ("COORDFS_SESSION_ID", "AGENT_SESSION_ID", "CODEX_SESSION_ID", "CODEX_THREAD_ID")

# Retained task evidence root: per-test unique directories are kept (never
# deleted) so the parent can review them. Optional AGENT_OFFICE_TEST_ROOT
# overrides the safe workspace fallback. No TemporaryDirectory, no
# cleanup/rmtree/unlink anywhere in this file.
EVIDENCE_ROOT = Path(
    os.environ.get("AGENT_OFFICE_TEST_ROOT", str(PACKAGE_ROOT / "native-mcp-test-evidence"))
)


def evidence_home(test_name: str) -> Path:
    """A fresh retained per-test home directory; never reused, never deleted."""
    EVIDENCE_ROOT.mkdir(parents=True, exist_ok=True)
    path = EVIDENCE_ROOT / f"{test_name}-{os.getpid()}-{uuid.uuid4().hex[:12]}" / "home"
    path.mkdir(parents=True, exist_ok=False)
    return path


def clean_env():
    return mock.patch.dict(os.environ, {k: "" for k in SESSION_KEYS})


class NativeBase(unittest.TestCase):
    def setUp(self):
        self.home = evidence_home(self._testMethodName)
        self._env = clean_env()
        self._env.start()

    def tearDown(self):
        # Evidence directories are intentionally retained; only the env patch ends.
        self._env.stop()

    def weak_codex(self, **kw):
        opts = {"harness": "codex", "host": "testhost", "home": self.home,
                "mutex_timeout_s": 1.0}
        opts.update(kw)
        return Office(**opts)

    def server(self, office, **kw):
        opts = {"heartbeat_ttl_s": None}
        opts.update(kw)
        return OfficeMcpServer(office, **opts)

    def wire(self, server, rid, name, args, meta=True, tid=UUID_A):
        params = {"name": name, "arguments": args}
        if meta:
            params["_meta"] = {"threadId": tid}
        return server.handle_message(
            {"jsonrpc": "2.0", "id": rid, "method": "tools/call", "params": params})

    def content(self, response):
        result = response["result"]
        if "structuredContent" in result:
            return result["structuredContent"]
        return json.loads(result["content"][0]["text"])


class HelperTests(NativeBase):
    def test_normalize_and_extract(self):
        self.assertEqual(normalize_thread_id(UUID_A.upper()), UUID_A)
        self.assertIsNone(normalize_thread_id("short"))
        self.assertIsNone(normalize_thread_id(123))
        self.assertIsNone(normalize_thread_id("x" * 36))
        tid, state = extract_thread_id({"_meta": {"threadId": UUID_A}})
        self.assertEqual((tid, state), (UUID_A, "ok"))
        self.assertEqual(extract_thread_id({}), (None, "missing"))
        self.assertEqual(extract_thread_id({"arguments": {"thread_id": UUID_A}}), (None, "missing"))
        self.assertEqual(extract_thread_id({"_meta": {"threadId": "bad"}}), (None, "invalid"))

    def test_eligibility_only_weak_codex_fallback(self):
        office = self.weak_codex()
        self.assertEqual(office.identity["strength"], "weak")
        self.assertTrue(is_weak_codex_fallback(office.identity))
        self.assertFalse(is_weak_codex_fallback(Office(agent_id="alice", home=self.home).identity))
        self.assertFalse(is_weak_codex_fallback(
            Office(harness="zcode", home=self.home, mutex_timeout_s=1.0).identity))
        self.assertFalse(is_weak_codex_fallback(
            Office(harness="codex", host="h", session_id="s", home=self.home).identity))


class BindingTests(NativeBase):
    def test_first_presence_binds(self):
        office = self.weak_codex()
        fallback = office.agent_id
        server = self.server(office)
        payload = self.content(self.wire(server, 1, "office_presence", {"ttl_s": 60}))
        self.assertEqual(payload["status"], "ok")
        self.assertEqual(office.agent_id, f"codex-testhost-{UUID_A}")
        self.assertEqual(office.identity["source"], "mcp-native-thread-meta")
        self.assertEqual(office.identity["strength"], "strong")
        self.assertEqual(office.identity["harness"], "codex")
        self.assertEqual(office.identity["host"], "testhost")
        self.assertFalse(inbox_path(self.home, fallback).exists())

    def test_question_roundtrip_on_native_inbox(self):
        office = self.weak_codex()
        server = self.server(office)
        self.wire(server, 1, "office_presence", {"ttl_s": 86400})
        native = office.agent_id
        peer = Office(agent_id="peer-one", home=self.home, mutex_timeout_s=1.0)
        peer.presence(ttl_s=86400)
        sent = peer.send(native, "are you there?", kind="question")
        self.assertEqual(sent["status"], "ok")
        read = self.content(self.wire(server, 2, "office_read", {}))
        self.assertEqual(read["status"], "ok")
        self.assertEqual(len(read["messages"]), 1)
        mid = read["messages"][0]["untrusted_message"]["id"]
        ack = self.content(self.wire(server, 3, "office_ack", {"message_id": mid, "note": "got it"}))
        self.assertEqual(ack["status"], "ok")
        reply = self.content(self.wire(
            server, 4, "office_send", {"to": "peer-one", "body": "yes, here"}))
        self.assertEqual(reply["status"], "ok")
        back = peer.read()
        self.assertTrue(any(m["untrusted_message"]["body"] == "yes, here"
                            for m in back["messages"]))

    def test_missing_and_malformed_have_zero_writes(self):
        office = self.weak_codex()
        fallback = office.agent_id
        server = self.server(office)
        direct = server.call_tool("office_presence", {"ttl_s": 60})
        self.assertEqual(direct["code"], "native_identity_required")
        bad = self.content(self.wire(server, 1, "office_presence", {"ttl_s": 60}, tid="nope"))
        self.assertEqual(bad["code"], "native_identity_invalid")
        self.assertEqual(office.agent_id, fallback)
        self.assertIsNone(server._native.bound)
        self.assertFalse(inbox_path(self.home, fallback).exists())
        self.assertEqual(office.who()["online"], [])

    def test_changed_id_cannot_use_other_inbox(self):
        office = self.weak_codex()
        server = self.server(office)
        self.wire(server, 1, "office_presence", {"ttl_s": 86400})
        peer = Office(agent_id="peer-two", home=self.home, mutex_timeout_s=1.0)
        peer.presence(ttl_s=86400)
        peer.send(office.agent_id, "private", kind="question")
        miss = self.content(self.wire(server, 2, "office_read", {}, tid=UUID_B))
        self.assertEqual(miss["code"], "native_identity_mismatch")
        miss_send = self.content(self.wire(
            server, 3, "office_send", {"to": "peer-two", "body": "x"}, tid=UUID_B))
        self.assertEqual(miss_send["code"], "native_identity_mismatch")
        ok = self.content(self.wire(server, 4, "office_read", {}))
        self.assertEqual(len(ok["messages"]), 1)

    def test_repeated_same_id_works(self):
        office = self.weak_codex()
        server = self.server(office)
        for rid in (1, 2, 3):
            payload = self.content(self.wire(server, rid, "office_who", {}))
            self.assertEqual(payload["status"], "ok")
        self.assertEqual(server._native.bound, UUID_A)

    def test_invalid_requests_do_not_bind(self):
        office = self.weak_codex()
        server = self.server(office)
        bad_tool = server.handle_message(
            {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
             "params": {"name": "office_hack", "_meta": {"threadId": UUID_A}}})
        self.assertIn("error", bad_tool)
        forbidden = self.content(self.wire(
            server, 2, "office_send", {"to": "x", "body": "y", "from": "spoof"}))
        self.assertEqual(forbidden["code"], "forbidden_field")
        unknown = self.content(self.wire(server, 3, "office_read", {"execute": True}))
        self.assertEqual(unknown["code"], "invalid_params")
        notified = server.handle_message(
            {"jsonrpc": "2.0", "method": "tools/call",
             "params": {"name": "office_presence", "arguments": {},
                        "_meta": {"threadId": UUID_A}}})
        self.assertIsNone(notified)
        meta_arg = self.content(self.wire(
            server, 4, "office_read", {"_meta": {"threadId": UUID_A}}))
        self.assertEqual(meta_arg["code"], "invalid_params")
        self.assertIsNone(server._native.bound)
        payload = self.content(self.wire(server, 5, "office_presence", {"ttl_s": 60}))
        self.assertEqual(payload["status"], "ok")

    def test_blocking_wait_pin(self):
        office = self.weak_codex()
        server = self.server(office)
        self.wire(server, 1, "office_presence", {"ttl_s": 86400})
        miss = self.content(self.wire(
            server, 2, "office_wait", {"timeout_s": 1}, tid=UUID_B))
        self.assertEqual(miss["code"], "native_identity_mismatch")
        hit = self.content(self.wire(server, 3, "office_wait", {"timeout_s": 1}))
        self.assertEqual(hit["status"], "ok")

    def test_concurrent_different_ids_one_binding(self):
        office = self.weak_codex()
        server = self.server(office)
        tids = [f"11111111-2222-4333-8444-{i:012x}" for i in range(8)]
        results = [None] * len(tids)
        barrier = threading.Barrier(len(tids))

        def attempt(idx):
            barrier.wait(timeout=10)
            results[idx] = self.wire(
                server, idx + 1, "office_presence", {"ttl_s": 60}, tid=tids[idx])

        threads = [threading.Thread(target=attempt, args=(i,)) for i in range(len(tids))]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)
        codes = [self.content(r)["code"] for r in results]
        self.assertEqual(codes.count("present"), 1)
        self.assertEqual(codes.count("native_identity_mismatch"), len(tids) - 1)
        self.assertIn(server._native.bound, tids)

    def test_explicit_strong_and_non_codex_unchanged(self):
        alice = Office(agent_id="alice", home=self.home, mutex_timeout_s=1.0)
        server = self.server(alice)
        payload = self.content(self.wire(server, 1, "office_presence", {"ttl_s": 60}))
        self.assertEqual(payload["status"], "ok")
        self.assertEqual(alice.agent_id, "alice")
        zed = Office(harness="zcode", home=self.home, mutex_timeout_s=1.0)
        zserver = self.server(zed)
        before = zed.agent_id
        zpayload = self.content(self.wire(zserver, 1, "office_presence", {"ttl_s": 60}))
        self.assertEqual(zpayload["status"], "ok")
        self.assertEqual(zed.agent_id, before)
        self.assertFalse(zserver._native.active)

    def test_contractor_scope_intact(self):
        office = Office(agent_id="worker-one", home=self.home, mutex_timeout_s=1.0)
        server = OfficeMcpServer(office, heartbeat_ttl_s=None, profile="contractor",
                                 channels=("team",), peers=("lead-dev",))
        refused = server.call_tool("office_read", {"channel": "other"})
        self.assertEqual(refused["code"], "outside_profile")
        refused_send = server.call_tool("office_send", {"to": "stranger", "body": "hi"})
        self.assertEqual(refused_send["code"], "outside_profile")


class PinValidationTests(NativeBase):
    """First VALID request binds: semantically invalid first requests refuse
    with the pin unset, no heartbeat and no Office side effect, and a later
    valid request with a different native UUID still binds."""

    def check_unbound(self, office, server, fallback):
        self.assertIsNone(server._native.bound)
        self.assertFalse(server._native.just_bound)
        self.assertFalse(server._heartbeat_started)
        self.assertEqual(office.agent_id, fallback)
        self.assertFalse(inbox_path(self.home, fallback).exists())
        self.assertEqual(office.who()["online"], [])

    def test_missing_send_body_does_not_bind(self):
        office = self.weak_codex()
        fallback = office.agent_id
        server = OfficeMcpServer(office, heartbeat_ttl_s=60)
        payload = self.content(self.wire(server, 1, "office_send", {"to": "peer-one"}))
        self.assertEqual(payload["status"], "error")
        self.assertEqual(payload["code"], "invalid_body")
        self.check_unbound(office, server, fallback)
        ok = self.content(self.wire(
            server, 2, "office_presence", {"ttl_s": 60}, tid=UUID_B))
        self.assertEqual(ok["status"], "ok")
        self.assertEqual(server._native.bound, UUID_B)
        self.assertEqual(office.agent_id, f"codex-testhost-{UUID_B}")

    def test_invalid_presence_ttl_does_not_bind(self):
        office = self.weak_codex()
        fallback = office.agent_id
        server = OfficeMcpServer(office, heartbeat_ttl_s=60)
        payload = self.content(self.wire(server, 1, "office_presence", {"ttl_s": 0}))
        self.assertEqual(payload["status"], "error")
        self.assertEqual(payload["code"], "invalid_ttl")
        self.check_unbound(office, server, fallback)
        ok = self.content(self.wire(
            server, 2, "office_presence", {"ttl_s": 60}, tid=UUID_B))
        self.assertEqual(ok["status"], "ok")
        self.assertEqual(server._native.bound, UUID_B)

    def test_denied_contractor_channel_does_not_bind(self):
        office = self.weak_codex()
        fallback = office.agent_id
        server = OfficeMcpServer(office, heartbeat_ttl_s=60, profile="contractor",
                                 channels=("team",), peers=("lead",))
        payload = self.content(self.wire(
            server, 1, "office_send", {"channel": "other", "body": "hi"}))
        self.assertEqual(payload["status"], "error")
        self.assertEqual(payload["code"], "outside_profile")
        self.check_unbound(office, server, fallback)
        ok = self.content(self.wire(
            server, 2, "office_send", {"channel": "team", "body": "hi"}, tid=UUID_B))
        self.assertEqual(ok["status"], "ok")
        self.assertEqual(server._native.bound, UUID_B)

    def test_invalid_blocking_args_do_not_bind(self):
        office = self.weak_codex()
        fallback = office.agent_id
        server = OfficeMcpServer(office, heartbeat_ttl_s=60)
        bad_wait = self.content(self.wire(
            server, 1, "office_wait", {"timeout_s": -1}))
        self.assertEqual(bad_wait["status"], "error")
        self.assertEqual(bad_wait["code"], "invalid_timeout")
        bad_ask = self.content(self.wire(
            server, 2, "office_ask", {"to": "someone"}))
        self.assertEqual(bad_ask["status"], "error")
        self.assertEqual(bad_ask["code"], "invalid_body")
        self.check_unbound(office, server, fallback)
        ok = self.content(self.wire(
            server, 3, "office_wait", {"timeout_s": 1}, tid=UUID_B))
        self.assertEqual(ok["status"], "ok")
        self.assertEqual(server._native.bound, UUID_B)


class ServeJourneyTests(NativeBase):
    def _serve(self, server, lines):
        raw = b"".join(json.dumps(line).encode() + b"\n" for line in lines)
        stdout = io.BytesIO()
        stats = server.serve(io.BytesIO(raw), stdout)
        out = [json.loads(line) for line in stdout.getvalue().decode().splitlines() if line]
        return stats, out

    def test_serve_binds_and_starts_heartbeat(self):
        office = self.weak_codex()
        fallback = office.agent_id
        server = OfficeMcpServer(office, heartbeat_ttl_s=60)
        stats, out = self._serve(server, [
            {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
             "params": {"name": "office_presence", "arguments": {"ttl_s": 60},
                        "_meta": {"threadId": UUID_A}}},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
             "params": {"name": "office_wait", "arguments": {"timeout_s": 1},
                        "_meta": {"threadId": UUID_A}}},
        ])
        self.assertEqual(len(out), 2)
        native = f"codex-testhost-{UUID_A}"
        self.assertEqual(office.agent_id, native)
        self.assertIsNotNone(stats["presence"])
        self.assertEqual(stats["presence"]["agent"], native)
        self.assertFalse(server.heartbeat._thread.is_alive())
        self.assertEqual(stats["presence"]["code"], "present")
        agents = {item["agent"] for item in office.who()["online"]}
        self.assertNotIn(fallback, agents)

    def test_serve_without_binding_advertises_nothing(self):
        office = self.weak_codex()
        fallback = office.agent_id
        server = OfficeMcpServer(office, heartbeat_ttl_s=60)
        stats, out = self._serve(server, [
            {"jsonrpc": "2.0", "id": 1, "method": "ping"},
        ])
        self.assertEqual(out[0]["result"], {})
        self.assertIsNone(stats["presence"])
        self.assertFalse(server._heartbeat_started)
        self.assertEqual(office.who()["online"], [])
        self.assertEqual(office.agent_id, fallback)

    def test_serve_invalid_blocking_does_not_bind(self):
        office = self.weak_codex()
        fallback = office.agent_id
        server = OfficeMcpServer(office, heartbeat_ttl_s=60)
        stats, out = self._serve(server, [
            {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
             "params": {"name": "office_wait", "arguments": {"timeout_s": "soon"},
                        "_meta": {"threadId": UUID_A}}},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
             "params": {"name": "office_presence", "arguments": {"ttl_s": 60},
                        "_meta": {"threadId": UUID_B}}},
        ])
        self.assertEqual(len(out), 2)
        first = out[0]["result"]["structuredContent"]
        self.assertEqual(first["status"], "error")
        self.assertEqual(first["code"], "invalid_timeout")
        second = out[1]["result"]["structuredContent"]
        self.assertEqual(second["status"], "ok")
        self.assertEqual(server._native.bound, UUID_B)
        self.assertEqual(office.agent_id, f"codex-testhost-{UUID_B}")
        self.assertIsNotNone(stats["presence"])
        self.assertEqual(stats["presence"]["agent"], f"codex-testhost-{UUID_B}")
        agents = {item["agent"] for item in office.who()["online"]}
        self.assertNotIn(fallback, agents)


MESSAGE_ID = "0123456789abcdef0123456789abcdef"
PROJECT_WAIT = "office_project_wait"
PROJECT_WAIT_CHANNEL = "office_project_wait_channel"


def _project_args(*omit):
    args = {
        "project_id": "test-project",
        "task_id": "t",
        "thread_id": "th",
        "run_id": "r",
        "timeout_s": 0,
    }
    for key in omit:
        del args[key]
    return args


def _full_malformed():
    """Full-profile matrix. Every project call carries timeout_s 0."""
    return (
        ("office_dilemma_open", {}, "invalid_channel"),
        ("office_dilemma_open", {"channel": "team"}, "invalid_dilemma"),
        ("office_dilemma_answer", {}, "invalid_reply_to"),
        ("office_dilemma_status", {}, "invalid_reply_to"),
        ("office_delivery", {}, "invalid_reply_to"),
        ("office_delivery", {"message_id": MESSAGE_ID}, "invalid_channel"),
        (PROJECT_WAIT, _project_args("project_id"), "invalid_project_id"),
        (PROJECT_WAIT, _project_args("task_id"), "invalid_task_id"),
        (PROJECT_WAIT_CHANNEL, _project_args("task_id"), "invalid_task_id"),
        (PROJECT_WAIT_CHANNEL, _project_args(), "invalid_channel"),
    )


def _contractor_dilemma():
    """Allowed team channel keeps field errors; missing or denied stays outside_profile."""
    denied = {"channel": "other"}
    team = {"channel": "team"}
    denied_delivery = {"channel": "other", "message_id": MESSAGE_ID}
    return (
        ("office_dilemma_open", dict(team), "invalid_dilemma"),
        ("office_delivery", dict(team), "invalid_reply_to"),
        ("office_dilemma_open", {}, "outside_profile"),
        ("office_dilemma_open", dict(denied), "outside_profile"),
        ("office_dilemma_answer", {}, "outside_profile"),
        ("office_dilemma_status", {}, "outside_profile"),
        ("office_delivery", {}, "outside_profile"),
        ("office_delivery", dict(denied), "outside_profile"),
        ("office_delivery", {"message_id": MESSAGE_ID}, "outside_profile"),
        ("office_delivery", dict(denied_delivery), "outside_profile"),
    )


def _project_malformed():
    return tuple(case for case in _full_malformed() if case[0] in {PROJECT_WAIT, PROJECT_WAIT_CHANNEL})


class FreshMalformedPinTests(NativeBase):
    """Malformed first calls carry UUID_A and must not pin or write.

    A later presence with UUID_B still binds. Each case uses a new retained
    evidence home. Dilemma cases also run on the contractor profile.
    """

    def _hosted(self, contractor):
        self.home = evidence_home(self._testMethodName)
        office = self.weak_codex()
        extra = {"heartbeat_ttl_s": 60}
        if contractor:
            extra.update(profile="contractor", channels=("team",), peers=("lead",))
        return office, self.server(office, **extra), office.agent_id

    def _online_lease_files(self):
        root = self.home / "office"
        if not root.exists():
            return []
        found = []
        for path in root.rglob("*"):
            if not path.is_file():
                continue
            parts = [part.lower() for part in path.relative_to(root).parts]
            if "online" in parts or "leases" in parts or "lease" in path.name.lower():
                found.append(path.relative_to(self.home).as_posix())
        return sorted(found)

    def _assert_rejected(self, office, server, fallback, payload, code):
        self.assertIsInstance(payload, dict)
        rendered = json.dumps(payload, sort_keys=True, default=str)[:2000]
        self.assertEqual(payload.get("status"), "error", rendered)
        self.assertEqual(payload.get("code"), code, rendered)
        self.assertIsNone(server._native.bound)
        self.assertFalse(server._native.just_bound)
        self.assertFalse(server._heartbeat_started)
        self.assertEqual(office.agent_id, fallback)
        self.assertFalse(inbox_path(self.home, fallback).exists())
        self.assertFalse((self.home / "office" / "projects").exists())
        self.assertEqual(self._online_lease_files(), [])
        self.assertEqual(office.who()["online"], [])
        self.assertTrue(self.home.exists())

    def _bind_uuid_b(self, office, server):
        payload = self.content(self.wire(
            server, 2, "office_presence", {"ttl_s": 60}, tid=UUID_B))
        self.assertEqual(payload["status"], "ok")
        self.assertEqual(server._native.bound, UUID_B)
        self.assertEqual(office.agent_id, f"codex-testhost-{UUID_B}")

    def _via_handle(self, server, tool, args):
        response = self.wire(server, 1, tool, args, tid=UUID_A)
        if "result" not in response:
            self.fail(json.dumps(response, default=str)[:2000])
        return self.content(response)

    def _via_call_tool(self, server, tool, args):
        payload = server.call_tool(
            tool, args, _wire_params={"_meta": {"threadId": UUID_A}})
        if isinstance(payload, dict) and "status" not in payload and "structuredContent" in payload:
            return payload["structuredContent"]
        if isinstance(payload, dict) and "status" not in payload and "result" in payload:
            return self.content(payload)
        return payload

    def _via_serve(self, server, tool, args):
        line = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": tool,
                "arguments": args,
                "_meta": {"threadId": UUID_A},
            },
        }
        stdout = io.BytesIO()
        stats = server.serve(io.BytesIO(json.dumps(line).encode() + b"\n"), stdout)
        parsed = [json.loads(item) for item in stdout.getvalue().decode().splitlines() if item]
        self.assertEqual(len(parsed), 1, stdout.getvalue()[:2000])
        if "result" not in parsed[0]:
            self.fail(json.dumps(parsed[0], default=str)[:2000])
        self.assertIsNone(stats["presence"])
        return self.content(parsed[0])

    def _run_cases(self, cases, contractor, transport):
        homes = set()
        for tool, args, code in cases:
            label = tuple(sorted(args.items()))
            with self.subTest(transport=transport, tool=tool, code=code, args=label):
                office, server, fallback = self._hosted(contractor)
                self.assertNotIn(self.home, homes)
                homes.add(self.home)
                sent = dict(args)
                if transport == "handle":
                    payload = self._via_handle(server, tool, sent)
                elif transport == "call_tool":
                    payload = self._via_call_tool(server, tool, sent)
                elif transport == "serve":
                    payload = self._via_serve(server, tool, sent)
                else:
                    raise AssertionError(transport)
                self._assert_rejected(office, server, fallback, payload, code)
                self._bind_uuid_b(office, server)

    def test_handle_full_malformed_does_not_pin(self):
        self._run_cases(_full_malformed(), contractor=False, transport="handle")

    def test_call_tool_full_malformed_does_not_pin(self):
        self._run_cases(_full_malformed(), contractor=False, transport="call_tool")

    def test_handle_contractor_dilemma_does_not_pin(self):
        self._run_cases(_contractor_dilemma(), contractor=True, transport="handle")

    def test_call_tool_contractor_dilemma_does_not_pin(self):
        self._run_cases(_contractor_dilemma(), contractor=True, transport="call_tool")

    def test_serve_project_wait_malformed_does_not_pin(self):
        self._run_cases(_project_malformed(), contractor=False, transport="serve")


if __name__ == "__main__":
    unittest.main()
