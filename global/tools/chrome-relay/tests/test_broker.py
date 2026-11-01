"""Actual loopback HTTP trust-boundary tests; no real browser/accounts."""

import copy
import hashlib
import http.client
import json
import os
import secrets
import socket
import subprocess
import sys
import threading
import time
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from broker import ACTIONS, Broker, MAX_BODY, Policy, Reject, Request, json_bytes, make_server

DEFAULT_ROOT = Path(__file__).resolve().parents[4] / "task-evidence" / "chrome-relay-tests"
SOURCE_ROOT = Path(__file__).resolve().parents[1]
ROOT = Path(os.environ.get("TEAM_TEST_ROOT", str(DEFAULT_ROOT))).resolve()
if ROOT.is_relative_to(SOURCE_ROOT):
    raise RuntimeError("TEAM_TEST_ROOT must resolve outside the chrome-relay source directory")


def policy_dict():
    return {"schema": 1, "profile_id": "synthetic-profile", "allowed_origins": ["http://127.0.0.1:8123"],
            "allowed_actions": sorted(ACTIONS - {"chrome_saved_login"}), "sessions": {"native-a": "A" * 32, "native-b": "B" * 32},
            "extension_token": "E" * 32, "extension_id": "a" * 32, "expires_at": (time.time() + 3600) * 1000, "synthetic": True}


class HTTPHarness(unittest.TestCase):
    def setUp(self):
        self.policy_raw = policy_dict()
        self.folder = ROOT / ("broker-" + secrets.token_hex(8))
        self.folder.mkdir(parents=True, exist_ok=True)
        self.broker = Broker(Policy.from_dict(self.policy_raw), self.folder / "state.json", self.folder / "audit.jsonl", request_timeout=0.7, poll_timeout=0.02)
        self.server = make_server(self.broker, port=0)
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        self.thread.start()
        self.assertEqual(self.server.server_address[0], "127.0.0.1")

    def tearDown(self):
        self.broker.close()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(2)

    def http(self, path, value=None, sid="native-a", extension=False, headers=None, raw=None, method=None):
        conn = http.client.HTTPConnection(*self.server.server_address, timeout=3)
        auth = {"Authorization": "Bearer " + (self.policy_raw["extension_token"] if extension else self.policy_raw["sessions"][sid]), "Content-Type": "application/json"}
        auth["X-Profile-Id" if extension else "X-Session-Id"] = self.policy_raw["profile_id"] if extension else sid
        auth.update(headers or {})
        body = json_bytes(value) if raw is None and value is not None else raw
        try:
            conn.request(method or ("GET" if value is None and raw is None else "POST"), path, body=body, headers=auth)
            response = conn.getresponse()
            payload = response.read()
            self.assertNotIn("Access-Control-Allow-Origin", dict(response.getheaders()))
            return response.status, json.loads(payload) if payload else None
        finally:
            conn.close()

    def attach(self):
        self.assertEqual(self.http("/extension/event", {"type": "attach", "profile_id": "synthetic-profile", "session_id": "", "tab_id": 1, "reason": "owner_attach"}, extension=True)[0], 200)

    def start(self, action, args=None, tab=None, sid="native-a", rid=None):
        value = {"request_id": rid or secrets.token_hex(8), "action": action, "args": args or {}}
        if tab is not None:
            value["tab_id"] = tab
        holder = {}
        thread = threading.Thread(target=lambda: holder.update(response=self.http("/mcp", value, sid)), daemon=True)
        thread.start()
        return value, holder, thread

    def command(self):
        deadline = time.monotonic() + 0.5
        while time.monotonic() < deadline:
            status, command = self.http("/extension/poll", extension=True)
            if status == 200:
                return command
        self.fail("No command dispatched by actual poll endpoint")

    def result(self, command, data, tab=None, status="ok", **changes):
        value = {"request_id": command["request_id"], "session_id": command["session_id"], "profile_id": command["profile_id"], "status": status, "data": data}
        if tab is not None:
            value["tab_id"] = tab
        value.update(changes)
        return self.http("/extension/result", value, extension=True)

    def finish(self, holder, thread):
        thread.join(2)
        self.assertFalse(thread.is_alive())
        self.assertIn("response", holder)
        return holder["response"]

    def open(self, sid="native-a", tab=10, rid=None):
        request, holder, thread = self.start("chrome_open", {"url": "http://127.0.0.1:8123/ordinary"}, sid=sid, rid=rid)
        command = self.command()
        self.assertEqual(self.result(command, {"owned": True}, tab)[0], 200)
        response = self.finish(holder, thread)
        self.assertEqual(response[1]["status"], "ok")
        return request, response[1]

    def read(self, sid="native-a", tab=10, excerpt="Hello", refs=None):
        _, holder, thread = self.start("chrome_read", tab=tab, sid=sid)
        command = self.command()
        payload = {"excerpt": excerpt, "refs": refs or [{"ref": "r1_0", "role": "button", "name": "Continue draft"}], "truncated": False}
        self.assertEqual(self.result(command, payload, tab)[0], 200)
        return self.finish(holder, thread)[1]

    def abandon(self, value, sid="native-a"):
        # Lose the submitting handler after admission, as an interrupted caller
        # would. The real HTTP server retains queued/dispatched executor state.
        def request(*args, **kwargs):
            req = Request(*args, **kwargs)
            def interrupted_wait(*_):
                raise RuntimeError("synthetic interrupted submitter")
            req.event.wait = interrupted_wait
            return req
        with patch("broker.Request", side_effect=request):
            with self.assertRaises(http.client.RemoteDisconnected):
                self.http("/mcp", value, sid=sid)
        return self.broker.requests[(sid, value["request_id"])]


class EffectiveRootPlacementTests(unittest.TestCase):
    def _run_with_root(self, root, *args):
        env = {key: os.environ[key] for key in ("PATH", "SYSTEMROOT", "WINDIR") if key in os.environ}
        env.update({"TEAM_TEST_ROOT": str(root), "PYTHONDONTWRITEBYTECODE": "1", "PYTHONNOUSERSITE": "1"})
        return subprocess.run(
            [sys.executable, "-B", *args],
            cwd=Path(__file__).resolve().parent,
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=15,
            check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )

    def test_effective_team_test_root_is_validated_before_artifacts(self):
        unsafe_root = SOURCE_ROOT / "tests" / "evidence" / ("team-test-root-" + secrets.token_hex(8))
        self.assertTrue(unsafe_root.is_relative_to(SOURCE_ROOT))
        self.assertFalse(unsafe_root.exists())

        rejected = self._run_with_root(unsafe_root, "-c", "import test_broker")
        self.assertNotEqual(rejected.returncode, 0, msg=rejected.stdout + rejected.stderr)
        self.assertIn("TEAM_TEST_ROOT must resolve outside the chrome-relay source directory", rejected.stderr)
        self.assertFalse(unsafe_root.exists(), "in-source root was created before the placement guard rejected it")

        outside_root = DEFAULT_ROOT.parent / ("team-test-root-" + secrets.token_hex(8))
        self.assertFalse(outside_root.is_relative_to(SOURCE_ROOT))
        self.assertFalse(outside_root.exists())
        accepted = self._run_with_root(
            outside_root,
            "-m",
            "unittest",
            "test_broker.TrustBoundaryTests.test_stop_cancels_inflight_and_persists_until_explicit_owner_attach",
        )
        self.assertEqual(accepted.returncode, 0, msg=accepted.stdout + accepted.stderr)
        self.assertIn("Ran 1 test", accepted.stderr)
        evidence_dirs = list(outside_root.glob("broker-*"))
        self.assertEqual(len(evidence_dirs), 1)
        self.assertTrue((evidence_dirs[0] / "state.json").is_file())
        self.assertTrue((evidence_dirs[0] / "audit.jsonl").is_file())


class WireGenerationTests(unittest.TestCase):
    def test_cross_language_golden_digest_and_expiry_millisecond_floor(self):
        raw = policy_dict()
        # Fixed synthetic inputs shared with compact JavaScript JSON encoding.
        expected = "1c77c827876b018249c11d9dd2a6030f"
        for expiry in (1760198400.1234, 1760198400123.4, "2025-10-11T16:00:00.123400Z"):
            with self.subTest(expiry=expiry):
                policy = Policy.from_dict({**raw, "expires_at": expiry})
                self.assertEqual(policy.wire_generation(), expected)
        node = Path("C:/Program Files/nodejs/node.exe") if os.name == "nt" else "node"
        env = {key: os.environ[key] for key in ("PATH", "SYSTEMROOT", "WINDIR") if key in os.environ}
        script = (
            "const {createHash}=require('node:crypto');"
            "const input=JSON.parse(require('node:fs').readFileSync(0,'utf8'));"
            "const bytes=JSON.stringify([input[0],input[1],Math.floor(input[2])]);"
            "process.stdout.write(createHash('sha256').update(bytes,'utf8').digest('hex').slice(0,32));"
        )
        actual = subprocess.run(
            [str(node), "-e", script], input=json_bytes([raw["profile_id"], raw["extension_token"], 1760198400123.4]),
            capture_output=True, env=env, timeout=10, check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        self.assertEqual(actual.returncode, 0, msg=actual.stderr.decode("utf-8", "replace"))
        self.assertEqual(actual.stdout.decode("ascii"), expected)

    def test_generation_uses_only_fixed_profile_token_and_integer_expiry(self):
        raw = {**policy_dict(), "expires_at": 1760198400.1234}
        expected = Policy.from_dict(raw).wire_generation()
        same = {**raw, "expires_at": 1760198400.1239,
                "allowed_actions": ["chrome_attachment"], "allowed_origins": ["https://app.example.test"],
                "sessions": {"different-native-session": "S" * 32}, "extension_id": "b" * 32}
        self.assertEqual(Policy.from_dict(same).wire_generation(), expected)
        for changes in ({"profile_id": "another-profile"}, {"extension_token": "F" * 32}, {"expires_at": 1760198400.124}):
            with self.subTest(field=next(iter(changes))):
                self.assertNotEqual(Policy.from_dict({**raw, **changes}).wire_generation(), expected)


class TrustBoundaryTests(HTTPHarness):
    def test_generation_wire_uuid_preserves_client_replay_stop_and_restart(self):
        self.attach()
        prefix = self.broker.policy.wire_generation() + ":"
        wires = set()
        for sid, tab in (("native-a", 10), ("native-b", 20)):
            value, holder, thread = self.start("chrome_open", {"url": "http://127.0.0.1:8123/ordinary"}, sid=sid, rid="stable-client-id")
            command = self.command()
            wire = command["request_id"]
            self.assertRegex(wire, r"^[a-f0-9]{32}:[a-f0-9]{32}$")
            self.assertTrue(wire.startswith(prefix))
            suffix = uuid.UUID(hex=wire[len(prefix):])
            self.assertEqual(suffix.version, 4)
            self.assertEqual(suffix.variant, uuid.RFC_4122)
            self.assertNotIn(wire, wires)
            wires.add(wire)
            # A generation is not a capability, nor is a client ID a wire ID.
            self.assertEqual(self.http("/extension/poll", extension=True, headers={"Authorization": "Bearer " + prefix[:-1]})[0], 401)
            for wrong in ("stable-client-id", suffix.hex, "0" * 32 + ":" + suffix.hex):
                self.assertEqual(self.result(command, {"owned": True}, tab, request_id=wrong)[0], 409)
            self.assertEqual(self.result(command, {"owned": True}, tab)[0], 200)
            response = self.finish(holder, thread)
            self.assertEqual(response[1], {"request_id": "stable-client-id", "session_id": sid,
                                          "profile_id": "synthetic-profile", "status": "ok",
                                          "data": {"owned": True}, "tab_id": tab})
            self.assertEqual(self.http("/mcp", value, sid=sid), response)
            self.assertEqual(self.http("/extension/poll", extension=True)[0], 204)
        self.assertEqual(self.http("/extension/event", {"type": "stop", "profile_id": "synthetic-profile",
                                                       "session_id": "", "tab_id": 1, "reason": "owner_stop"}, extension=True)[0], 200)
        self.assertEqual(self.http("/mcp", value, sid=sid), response)
        restored = Broker(self.broker.policy, self.folder / "state.json", poll_timeout=0)
        try:
            self.assertTrue(restored.stop)
            self.assertEqual(restored.generation, prefix[:-1])
            self.assertEqual(restored.submit(sid, value), response[1])
            self.assertIsNone(restored.poll())
        finally:
            restored.close()
        for text in ((self.folder / "state.json").read_text(), (self.folder / "audit.jsonl").read_text()):
            self.assertNotIn(self.policy_raw["extension_token"], text)
            for wire in wires:
                self.assertNotIn(wire, text)

    def test_new_generation_requires_owner_attach_and_rejects_late_old_wire(self):
        self.attach()
        value, holder, thread = self.start("chrome_open", {"url": "http://127.0.0.1:8123/ordinary"}, rid="across-generation")
        old = self.command()
        self.broker.close()
        self.assertEqual(self.finish(holder, thread)[1]["status"], "unknown")
        self.policy_raw["expires_at"] += 1000
        self.broker = Broker(Policy.from_dict(self.policy_raw), self.folder / "state.json", request_timeout=0.7, poll_timeout=0.02)
        self.server.broker = self.broker
        self.assertFalse(old["request_id"].startswith(self.broker.generation + ":"))
        self.assertTrue(self.broker.stop)
        self.assertFalse(self.broker.authorized)
        self.assertEqual(self.broker.owned, {"native-a": set(), "native-b": set()})
        self.assertEqual(self.result(old, {"owned": True}, 99)[0], 409)
        self.assertEqual(self.http("/mcp", value)[1]["data"]["reason"], "stopped")
        self.attach()
        _, holder, thread = self.start("chrome_open", value["args"], rid="fresh-generation-open")
        current = self.command()
        self.assertTrue(current["request_id"].startswith(self.broker.generation + ":"))
        self.assertEqual(self.result(old, {"owned": True}, 99)[0], 409)
        self.assertEqual(self.broker.owned["native-a"], set())
        self.assertEqual(self.result(current, {"owned": True}, 10)[0], 200)
        self.assertEqual(self.finish(holder, thread)[1]["request_id"], "fresh-generation-open")
        self.assertEqual(self.broker.owned["native-a"], {10})

    def test_default_evidence_is_outside_runtime_source(self):
        runtime = Path(__file__).resolve().parents[1]
        self.assertFalse(ROOT.is_relative_to(runtime))
        self.assertFalse(DEFAULT_ROOT.resolve().is_relative_to(runtime))
        self.assertTrue(DEFAULT_ROOT.resolve().is_relative_to(runtime.parents[2]))

    def test_observations_cross_lifetime_limit_without_losing_mutation_ids(self):
        self.attach()
        opened, response = self.open(rid="retained-open")
        raw = policy_dict()
        raw["allowed_actions"].remove("chrome_attachment")
        pressure = Broker(Policy.from_dict(raw), request_timeout=0.01, poll_timeout=0)
        try:
            for index in range(10005):
                action = ("chrome_read", "chrome_wait", "chrome_attachment")[index % 3]
                value = {"request_id": "pressure-" + str(index), "action": action,
                         "args": {"text": "ordinary"} if action == "chrome_wait" else {}}
                if action != "chrome_attachment":
                    value["tab_id"] = 10
                self.assertEqual(pressure.submit("native-a", value)["status"], "denied")
            self.assertLess(len(pressure.requests), 1000)
        finally:
            pressure.close()
        with patch("broker.MAX_OBSERVATIONS", 4):
            for index in range(15):
                self.read(excerpt="fresh observation " + str(index))
            self.assertLessEqual(len(self.broker.requests), 5)
            self.assertEqual(self.http("/mcp", opened)[1], response)
            changed = {**opened, "args": {"url": "http://127.0.0.1:8123/changed"}}
            self.assertEqual(self.http("/mcp", changed)[0], 409)
            self.assertEqual(self.http("/extension/poll", extension=True)[0], 204)
            restored = Broker(self.broker.policy, self.folder / "state.json", poll_timeout=0)
            try:
                self.assertEqual(restored.submit("native-a", opened)["data"], {"owned": True})
                self.assertIsNone(restored.poll())
                self.assertEqual(len(restored.requests), 1)
            finally:
                restored.close()

    def test_persistence_writes_only_changed_mutations_and_latches(self):
        self.attach()
        self.open()
        state = self.folder / "state.json"
        before = state.stat().st_size
        for _ in range(15):
            self.read()
        self.assertEqual(state.stat().st_size, before)
        sizes = []
        for index in range(60):
            value = {"request_id": "denied-close-" + str(index).zfill(3),
                     "action": "chrome_close", "tab_id": 99, "args": {}}
            before = state.stat().st_size
            self.assertEqual(self.http("/mcp", value)[1]["data"]["reason"], "ownership")
            sizes.append(state.stat().st_size - before)
        self.assertEqual(sizes, [0] * 60)
        restored = Broker(self.broker.policy, state, poll_timeout=0)
        try:
            self.assertEqual(len(restored.requests), 1)
            retry = {**value, "request_id": "denied-close-000"}
            self.assertEqual(restored.submit("native-a", retry)["data"]["reason"], "technical_block")
            self.assertIsNone(restored.poll())
        finally:
            restored.close()

    def test_denied_mutation_pressure_does_not_starve_another_session(self):
        self.attach()
        self.open(rid="retained-open")
        state = self.folder / "state.json"
        before = state.stat().st_size
        with patch("broker.MAX_REQUESTS", 8), patch("broker.MAX_OBSERVATIONS", 3):
            for index in range(30):
                denied = {"request_id": "foreign-close-" + str(index), "action": "chrome_close", "tab_id": 99, "args": {}}
                status, response = self.http("/mcp", denied)
                self.assertEqual(status, 200)
                self.assertEqual(response["status"], "denied")
                self.assertEqual(response["data"]["reason"], "ownership")
            self.assertEqual(state.stat().st_size, before)
            self.assertLessEqual(len(self.broker.requests), 4)
            self.open(sid="native-b", tab=20, rid="sibling-open")
        restored = Broker(self.broker.policy, state, poll_timeout=0)
        try:
            self.assertEqual(len(restored.requests), 2)
            self.assertEqual(restored.submit("native-a", denied)["data"]["reason"], "technical_block")
            self.assertIsNone(restored.poll())
        finally:
            restored.close()

    def test_dispatched_mutation_quota_is_per_session_and_preserves_retries(self):
        self.attach()
        with patch("broker.MAX_REQUESTS", 8):
            first, response = self.open(tab=10, rid="quota-open-0")
            for index in range(1, 4):
                self.open(tab=10 + index, rid="quota-open-" + str(index))
            excess = {"request_id": "quota-excess", "action": "chrome_open", "args": {"url": "http://127.0.0.1:8123/ordinary"}}
            self.assertEqual(self.http("/mcp", excess), (429, {"error": "bound"}))
            self.assertEqual(self.http("/mcp", first), (200, response))
            changed = {**first, "args": {"url": "http://127.0.0.1:8123/changed"}}
            self.assertEqual(self.http("/mcp", changed)[0], 409)
            self.open(sid="native-b", tab=20, rid="sibling-quota-open")
            restored = Broker(self.broker.policy, self.folder / "state.json", poll_timeout=0)
            try:
                self.assertFalse(restored.state_failed)
                self.assertEqual(restored.submit("native-a", first), response)
                self.assertEqual(restored.submit("native-b", {"request_id": "sibling-denied", "action": "chrome_close", "tab_id": 99, "args": {}})["data"]["reason"], "technical_block")
                self.assertIsNone(restored.poll())
            finally:
                restored.close()

    def test_submit_sweeps_abandoned_dispatched_observation_before_admission(self):
        self.attach()
        self.open()
        with patch("broker.MAX_REQUESTS", 4):
            value = {"request_id": "abandoned-read", "action": "chrome_read", "tab_id": 10, "args": {}}
            req = self.abandon(value)
            command = self.command()
            self.assertTrue(req.dispatched)
            req.deadline = time.monotonic() - 1
            # Its expired lock/table slot must not prevent an actual new read.
            self.assertEqual(self.read(excerpt="Recovered")["status"], "ok")
            self.assertEqual(req.result["status"], "unknown")
            self.assertEqual(req.result["data"]["reason"], "timeout")
            self.assertEqual(self.result(command, {"excerpt": "Late", "refs": [], "truncated": False}, 10)[0], 409)

    def test_poll_sweeps_abandoned_mutation_and_restart_never_replays_it(self):
        self.attach()
        value = {"request_id": "abandoned-open", "action": "chrome_open", "args": {"url": "http://127.0.0.1:8123/ordinary"}}
        req = self.abandon(value)
        command = self.command()
        self.broker.poll_timeout = 0.08
        req.deadline = time.monotonic() + 0.02
        self.assertEqual(self.http("/extension/poll", extension=True)[0], 204)
        self.assertIsNotNone(req.result)
        self.assertEqual(self.http("/mcp", value)[1]["status"], "unknown")
        self.assertEqual(self.result(command, {"owned": True}, 10)[0], 409)
        restored = Broker(self.broker.policy, self.folder / "state.json", poll_timeout=0)
        try:
            self.assertEqual(restored.submit("native-a", value)["status"], "unknown")
            self.assertEqual(len(restored.requests), 1)
            self.assertIsNone(restored.poll())
        finally:
            restored.close()

    def test_late_result_sweeps_deadline_without_caller_retry(self):
        self.attach()
        self.open()
        value = {"request_id": "late-read", "action": "chrome_read", "tab_id": 10, "args": {}}
        req = self.abandon(value)
        command = self.command()
        req.deadline = time.monotonic() - 1
        self.assertEqual(self.result(command, {"excerpt": "Late", "refs": [{"ref": "r1_0", "role": "button", "name": "Ordinary"}], "truncated": False}, 10)[0], 409)
        self.assertEqual(req.result["status"], "unknown")
        self.assertNotIn(("native-a", 10), self.broker.refs)

    def test_undispatched_abandoned_mutations_remain_bounded_without_journaling(self):
        self.attach()
        state = self.folder / "state.json"
        before = state.stat().st_size
        with patch("broker.MAX_REQUESTS", 8), patch("broker.MAX_OBSERVATIONS", 3):
            for index in range(12):
                value = {"request_id": "abandoned-queued-" + str(index), "action": "chrome_open", "args": {"url": "http://127.0.0.1:8123/ordinary"}}
                req = self.abandon(value)
                self.assertFalse(req.dispatched)
                req.deadline = time.monotonic() - 1
                self.assertEqual(self.http("/extension/poll", extension=True)[0], 204)
                self.assertEqual(req.result["data"]["reason"], "timeout")
            self.assertEqual(state.stat().st_size, before)
            self.assertLessEqual(len(self.broker.requests), 3)
            self.assertEqual(len(self.broker.queue), 0)
            self.assertEqual(len(self.broker.wires), 0)
            self.open(sid="native-b", tab=20, rid="sibling-after-abandonment")

    def test_executor_denied_mutation_still_never_replays_after_restart(self):
        self.attach()
        value, holder, thread = self.start("chrome_open", {"url": "http://127.0.0.1:8123/ordinary"}, rid="executor-denied")
        command = self.command()
        self.assertEqual(self.result(command, {"reason": "origin"}, status="denied")[0], 200)
        response = self.finish(holder, thread)
        self.assertEqual(response[1]["status"], "denied")
        with patch("broker.MAX_OBSERVATIONS", 3):
            for index in range(12):
                self.http("/mcp", {"request_id": "pressure-close-" + str(index), "action": "chrome_close", "tab_id": 99, "args": {}})
        self.assertEqual(self.http("/mcp", value), response)
        restored = Broker(self.broker.policy, self.folder / "state.json", poll_timeout=0)
        try:
            restored.extension_event({"type": "attach", "profile_id": "synthetic-profile", "session_id": "", "tab_id": 1, "reason": "owner_attach"})
            self.assertEqual(restored.submit("native-a", value), response[1])
            self.assertIsNone(restored.poll())
        finally:
            restored.close()

    def test_expired_mutation_denials_do_not_repeat_latch_journal_writes(self):
        raw = policy_dict()
        raw["expires_at"] = (time.time() - 1) * 1000
        state = self.folder / "expired-state.json"
        expired = Broker(Policy.from_dict(raw), state, poll_timeout=0)
        try:
            value = {"request_id": "first-expired", "action": "chrome_close", "tab_id": 99, "args": {}}
            self.assertEqual(expired.submit("native-a", value)["data"]["reason"], "expired")
            before = state.stat().st_size
            for index in range(12):
                self.assertEqual(expired.submit("native-a", {**value, "request_id": "expired-" + str(index)})["data"]["reason"], "expired")
                self.assertIsNone(expired.poll())
            self.assertEqual(state.stat().st_size, before)
            with patch("broker.time.time", return_value=expired.policy.expires_at - 60):
                self.assertEqual(expired.submit("native-a", {"request_id": "clock-rollback", "action": "chrome_attachment", "args": {}})["data"]["reason"], "expired")
                self.assertIsNone(expired.poll())
                with self.assertRaises(Reject) as rejected:
                    expired.extension_event({"type": "attach", "profile_id": "synthetic-profile", "session_id": "", "tab_id": 1, "reason": "owner_attach"})
                self.assertEqual(rejected.exception.reason, "expired")
            self.assertEqual(state.stat().st_size, before)
        finally:
            expired.close()

    def test_observation_eviction_preserves_inflight_and_clears_expired_queue(self):
        self.attach()
        self.open()
        with patch("broker.MAX_OBSERVATIONS", 4):
            value, holder, thread = self.start("chrome_read", tab=10, rid="pending-read")
            deadline = time.monotonic() + 0.5
            while ("native-a", value["request_id"]) not in self.broker.requests and time.monotonic() < deadline:
                time.sleep(0.001)
            for index in range(12):
                denied = {"request_id": "denied-read-" + str(index), "action": "chrome_read", "tab_id": 99, "args": {}}
                self.assertEqual(self.http("/mcp", denied)[1]["data"]["reason"], "ownership")
            self.assertIsNone(self.broker.requests[("native-a", value["request_id"])].result)
            command = self.command()
            self.assertEqual(self.result(command, {"excerpt": "Current", "refs": [], "truncated": False}, 10)[0], 200)
            self.assertEqual(self.finish(holder, thread)[1]["status"], "ok")
            transient = Broker(self.broker.policy, request_timeout=0.01, poll_timeout=0)
            try:
                for index in range(12):
                    result = transient.submit("native-a", {"request_id": "timeout-" + str(index), "action": "chrome_attachment", "args": {}})
                    self.assertEqual(result["data"]["reason"], "timeout")
                self.assertEqual(len(transient.requests), 4)
                self.assertEqual(len(transient.queue), 0)
                self.assertEqual(len(transient.wires), 0)
            finally:
                transient.close()

    def test_mutation_write_is_bounded_independently_of_other_owned_tabs(self):
        raw = policy_dict()
        for index in range(98):
            raw["sessions"]["synthetic-" + str(index)] = "synthetic-capability-" + str(index).zfill(16)
        state = self.folder / "many-sessions.json"
        relay = Broker(Policy.from_dict(raw), state, poll_timeout=0)
        try:
            for index, sid in enumerate(relay.policy.sessions):
                relay.owned[sid] = set(range(index * 20 + 1, index * 20 + 21))
            relay._persist()  # Synthetic retained ownership fixture, no browser.
            before = state.stat().st_size
            self.assertGreater(before, 10000)
            value = {"request_id": "bounded-write", "action": "chrome_close", "tab_id": 99, "args": {}}
            self.assertEqual(relay.submit("native-a", value)["status"], "denied")
            self.assertLess(state.stat().st_size - before, 2048)
        finally:
            relay.close()
        restored = Broker(relay.policy, state, poll_timeout=0)
        try:
            self.assertFalse(restored.state_failed)
            self.assertEqual(restored.owned, relay.owned)
            self.assertEqual(restored.submit("native-a", value)["status"], "denied")
            self.assertIsNone(restored.poll())
        finally:
            restored.close()

    def test_legacy_snapshot_append_recovery_and_torn_journal_fail_closed(self):
        self.attach()
        opened, response = self.open(rid="legacy-open")
        record = {"sid": "native-a", "rid": opened["request_id"], "action": "chrome_open", "tab": None,
                  "fingerprint": self.broker.requests[("native-a", opened["request_id"])].fingerprint,
                  "status": "ok", "dispatched": True, "result_tab": 10, "reason": "broker_restart", "mutation_data": {"owned": True}}
        observation = {**record, "rid": "legacy-read", "action": "chrome_read", "tab": 10}
        observation.pop("mutation_data")
        denied = [{**record, "rid": "legacy-denied-" + str(index), "action": "chrome_close", "tab": 99,
                   "status": "denied", "dispatched": False, "result_tab": 99, "reason": "ownership"} for index in range(30)]
        snapshot = {"schema": 1, "profile_id": "synthetic-profile", "policy_hash": self.broker.policy.fingerprint(),
                    "stop": False, "authorized": True, "manual_tab": 1, "epoch": 1,
                    "owned": {"native-a": [10], "native-b": []}, "requests": [record, observation, *denied]}
        path = self.folder / "legacy.json"
        original = json_bytes(snapshot)  # Previous writer did not add a newline.
        path.write_bytes(original)
        restored = Broker(self.broker.policy, path, request_timeout=0.01, poll_timeout=0)
        try:
            self.assertFalse(restored.state_failed)
            self.assertEqual(len(restored.requests), 1)
            self.assertEqual(restored.submit("native-a", opened), response)
            denied = {"request_id": "new-close", "action": "chrome_close", "tab_id": 99, "args": {}}
            self.assertEqual(restored.submit("native-a", denied)["status"], "denied")
            self.assertTrue(path.read_bytes().startswith(original + b"\n"))
        finally:
            restored.close()
        again = Broker(self.broker.policy, path, poll_timeout=0)
        try:
            self.assertFalse(again.state_failed)
            self.assertEqual(again.submit("native-a", opened), response)
            self.assertEqual(len(again.requests), 1)
            self.assertIsNone(again.poll())
        finally:
            again.close()
        torn = self.folder / "torn-state.json"
        torn.write_bytes(path.read_bytes() + b'{"schema":2')
        failed = Broker(self.broker.policy, torn, poll_timeout=0)
        try:
            self.assertTrue(failed.state_failed)
            self.assertTrue(failed.stop)
            self.assertEqual(failed.submit("native-a", opened)["data"]["reason"], "state_io")
            self.assertIsNone(failed.poll())
        finally:
            failed.close()

    def test_durable_write_failure_blocks_mutation_dispatch(self):
        self.attach()
        value, holder, thread = self.start("chrome_open", {"url": "http://127.0.0.1:8123/ordinary"}, rid="failed-flush")
        deadline = time.monotonic() + 0.5
        while ("native-a", value["request_id"]) not in self.broker.requests and time.monotonic() < deadline:
            time.sleep(0.001)
        with patch("broker.os.fsync", side_effect=OSError):
            self.assertEqual(self.http("/extension/poll", extension=True)[0], 204)
        self.assertEqual(self.finish(holder, thread)[1]["data"]["reason"], "state_io")
        self.assertTrue(self.broker.state_failed)
        self.assertTrue(self.broker.stop)
        self.assertEqual(self.http("/extension/poll", extension=True)[0], 204)
        restored = Broker(self.broker.policy, self.folder / "state.json", poll_timeout=0)
        try:
            restored.extension_event({"type": "attach", "profile_id": "synthetic-profile", "session_id": "", "tab_id": 1, "reason": "owner_attach"})
            self.assertEqual(restored.submit("native-a", value)["status"], "unknown")
            self.assertIsNone(restored.poll())
        finally:
            restored.close()

    def test_policy_requires_explicit_fixed_scope_and_is_immutable(self):
        raw = policy_dict()
        policy = Policy.from_dict(raw)
        raw["sessions"]["native-a"] = "Z" * 32
        self.assertEqual(policy.sessions["native-a"], "A" * 32)
        with self.assertRaises(TypeError):
            policy.sessions["new"] = "N" * 32
        for bad in ({}, {**policy_dict(), "synthetic": False}, {**policy_dict(), "allowed_origins": ["http://example.test"]}, {**policy_dict(), "extension_id": "z" * 32}, {**policy_dict(), "allowed_actions": ["Runtime.evaluate"]}, {**policy_dict(), "allowed_actions": [{}]}, {**policy_dict(), "sessions": {"a": "same-token-value-xxx", "b": "same-token-value-xxx"}}):
            with self.subTest(bad=str(list(bad))):
                with self.assertRaises(Reject):
                    Policy.from_dict(bad)
        with self.assertRaises(Reject):
            make_server(self.broker, "0.0.0.0")

    def test_auth_host_origin_profile_and_unknown_field_guards(self):
        value = {"request_id": "one", "action": "chrome_attachment", "args": {}}
        cases = [("/mcp", {"Authorization": "Bearer bad"}, 401), ("/mcp", {"X-Session-Id": "native-b"}, 401),
                 ("/mcp", {"Host": "attacker.test"}, 403), ("/mcp", {"Host": "127.0.0.1:1,evil"}, 403),
                 ("/mcp", {"Origin": "http://127.0.0.1:8123"}, 403), ("/mcp?token=canary", {}, 404)]
        for path, headers, expected in cases:
            self.assertEqual(self.http(path, value, headers=headers)[0], expected)
        self.assertEqual(self.http("/extension/poll", extension=True, headers={"Origin": "chrome-extension://" + "b" * 32})[0], 403)
        self.assertEqual(self.http("/extension/poll", extension=True, headers={"X-Profile-Id": "other-profile"})[0], 401)
        for extra in ({"profile_id": "other"}, {"session_id": "native-b"}, {"evaluate": "alert(1)"}):
            self.assertEqual(self.http("/mcp", {**value, **extra})[0], 400)

    def test_invalid_utf8_json_size_type_duplicate_header_and_disconnect_survive(self):
        for body in (b"\xff", b"{", b'{"request_id":"a","request_id":"b"}', b'{"x":NaN}', b'{"x":"\\ud800"}'):
            self.assertEqual(self.http("/mcp", raw=body)[0], 400)
        self.assertEqual(self.http("/mcp", raw=b"x" * (MAX_BODY + 1))[0], 413)
        self.assertEqual(self.http("/mcp", raw=b"{}", headers={"Content-Type": "text/plain"})[0], 415)
        self.assertEqual(self.http("/mcp", raw=b"{}", headers={"Transfer-Encoding": "chunked"})[0], 400)
        address = self.server.server_address
        raw_headers = (f"GET /extension/poll HTTP/1.1\r\nHost: 127.0.0.1:{address[1]}\r\nHost: evil.test\r\nAuthorization: Bearer " + "E" * 32 + "\r\nX-Profile-Id: synthetic-profile\r\n\r\n").encode()
        with socket.create_connection(address, 2) as stream:
            stream.sendall(raw_headers)
            self.assertIn(b"403", stream.recv(4096).split(b"\r\n", 1)[0])
        with socket.create_connection(address, 2) as stream:
            stream.sendall((f"POST /mcp HTTP/1.1\r\nHost: 127.0.0.1:{address[1]}\r\nAuthorization: Bearer " + "A" * 32 + "\r\nX-Session-Id: native-a\r\nContent-Type: application/json\r\nContent-Length: 400\r\n\r\n{").encode())
        self.assertEqual(self.http("/extension/poll", extension=True)[0], 204)
        self.assertEqual(self.http("/extension/poll", extension=True, headers={"Origin": "chrome-extension://" + "a" * 32})[0], 204)

    def test_early_auth_rejection_delivers_response_with_delayed_or_missing_body(self):
        body = json_bytes({"request_id": "untrusted-body", "action": "chrome_attachment", "args": {}})
        for delayed_body in (True, False):
            with self.subTest(delayed_body=delayed_body):
                conn = http.client.HTTPConnection(*self.server.server_address, timeout=2)
                try:
                    conn.putrequest("POST", "/mcp")
                    conn.putheader("Authorization", "Bearer bad")
                    conn.putheader("X-Session-Id", "native-a")
                    conn.putheader("Content-Type", "application/json")
                    conn.putheader("Content-Length", str(len(body)))
                    started = time.monotonic()
                    conn.endheaders()
                    if delayed_body:
                        # Header validation runs before the separately sent body.
                        # Closing with that body unread can reset Windows TCP.
                        time.sleep(0.03)
                        conn.send(body)
                    response = conn.getresponse()
                    self.assertEqual(response.status, 401)
                    self.assertEqual(json.loads(response.read()), {"error": "authorization"})
                    self.assertLess(time.monotonic() - started, 1)
                finally:
                    conn.close()
        self.assertEqual(self.broker.requests, {})
        self.assertEqual(self.http("/extension/poll", extension=True)[0], 204)

    def test_rejected_trickle_body_has_a_total_discard_deadline(self):
        conn = http.client.HTTPConnection(*self.server.server_address, timeout=2)
        stopped = threading.Event()
        writer = None
        try:
            conn.putrequest("POST", "/mcp")
            conn.putheader("Authorization", "Bearer bad")
            conn.putheader("X-Session-Id", "native-a")
            conn.putheader("Content-Type", "application/json")
            conn.putheader("Content-Length", "4096")
            started = time.monotonic()
            conn.endheaders()
            stream = conn.sock

            def trickle():
                try:
                    for _ in range(60):
                        if stopped.wait(0.02):
                            return
                        stream.sendall(b"x")
                except OSError:
                    pass  # The bounded denial closes the synthetic connection.

            writer = threading.Thread(target=trickle, daemon=True)
            writer.start()
            response = conn.getresponse()
            self.assertEqual(response.status, 401)
            self.assertEqual(json.loads(response.read()), {"error": "authorization"})
            self.assertLess(time.monotonic() - started, 0.9)
        finally:
            stopped.set()
            conn.close()
            if writer is not None:
                writer.join(2)
                self.assertFalse(writer.is_alive())
        self.assertEqual(self.broker.requests, {})

    def test_policy_expiry_deadline_wakes_inflight_wait_without_another_request(self):
        self.broker.close()
        raw = policy_dict()
        raw["expires_at"] = (time.time() + 0.3) * 1000
        self.broker = Broker(Policy.from_dict(raw), self.folder / "expiring-state.json", request_timeout=0.7, poll_timeout=0.02)
        self.server.broker = self.broker
        self.attach()
        started = time.monotonic()
        _, holder, thread = self.start("chrome_open", {"url": "http://127.0.0.1:8123/ordinary"})
        command = self.command()
        response = self.finish(holder, thread)
        self.assertEqual(response[1]["status"], "unknown")
        self.assertEqual(response[1]["data"]["reason"], "expired")
        self.assertLess(time.monotonic() - started, 0.6)
        self.assertEqual(self.result(command, {"owned": True}, 10)[0], 409)

    def test_open_scope_and_oauth_denials_never_dispatch(self):
        self.attach()
        for url in ("http://evil.test/", "https://evil.test/", "http://127.0.0.1:8123/oauth/authorize", "http://127.0.0.1:8123/%6fauth/authorize", "http://127.0.0.1:8123/ordinary?client_id=x", "http://token@127.0.0.1:8123/", "http://127.0.0.1:8123\\@evil.test/"):
            response = self.http("/mcp", {"request_id": secrets.token_hex(8), "action": "chrome_open", "args": {"url": url}})
            self.assertEqual(response[0], 400)
        self.assertEqual(self.http("/extension/poll", extension=True)[0], 204)

    def test_ownership_fresh_refs_redaction_and_ordinary_autonomous_click(self):
        self.attach()
        self.open("native-a", 10)
        self.open("native-b", 20)
        for sid, tab in (("native-a", 20), ("native-b", 10), ("native-a", 1)):
            for action in ("chrome_read", "chrome_close"):
                response = self.http("/mcp", {"request_id": secrets.token_hex(8), "action": action, "tab_id": tab, "args": {}}, sid)
                self.assertEqual(response[1]["data"]["reason"], "ownership")
        canary = "E" * 32
        read = self.read(excerpt="secret=" + canary + " token=sk-canary\nNormal text", refs=[{"ref": "r1_0", "role": "button", "name": "Continue draft"}, {"ref": "r1_1", "role": "textbox", "name": "Password"}])
        self.assertNotIn(canary, json.dumps(read))
        self.assertNotIn("sk-canary", json.dumps(read))
        self.assertEqual(len(read["data"]["refs"]), 1)
        ref = read["data"]["refs"][0]["ref"]
        response = self.http("/mcp", {"request_id": "foreign-ref", "action": "chrome_click", "tab_id": 20, "args": {"ref": ref}}, "native-b")
        self.assertEqual(response[1]["data"]["reason"], "stale_ref")
        _, holder, thread = self.start("chrome_click", {"ref": ref}, 10)
        command = self.command()
        self.assertEqual(command["args"], {"ref": "r1_0"})
        self.assertEqual(self.result(command, {"clicked": True}, 10)[0], 200)
        self.assertEqual(self.finish(holder, thread)[1]["status"], "ok")
        response = self.http("/mcp", {"request_id": "consumed-ref", "action": "chrome_click", "tab_id": 10, "args": {"ref": ref}})
        self.assertEqual(response[1]["data"]["reason"], "stale_ref")
        read2 = self.read()
        self.assertNotEqual(ref, read2["data"]["refs"][0]["ref"])
        persisted = (self.folder / "state.json").read_text()
        audit = (self.folder / "audit.jsonl").read_text()
        for text in (persisted, audit):
            for forbidden in (canary, "A" * 32, "B" * 32, "http://", "sk-canary", "Continue draft", "r1_0", ref):
                self.assertNotIn(forbidden, text)

    def test_exact_result_match_unsolicited_duplicate_and_bounds(self):
        self.attach()
        _, holder, thread = self.start("chrome_open", {"url": "http://127.0.0.1:8123/ordinary"})
        command = self.command()
        for changes in ({"session_id": "native-b"}, {"profile_id": "other"}, {"request_id": "no-request"}):
            self.assertEqual(self.result(command, {"owned": True}, 10, **changes)[0], 409)
        self.assertEqual(self.result(command, {"owned": True, "html": "canary"}, 10)[0], 400)
        self.assertEqual(self.result(command, {"owned": True}, 1)[0], 409)
        self.assertEqual(self.result(command, {"owned": True}, 10)[0], 200)
        self.assertEqual(self.finish(holder, thread)[1]["tab_id"], 10)
        self.assertEqual(self.result(command, {"owned": True}, 10)[0], 409)
        _, holder, thread = self.start("chrome_read", tab=10)
        command = self.command()
        self.assertEqual(self.result(command, {"excerpt": "x" * 4096, "refs": [], "truncated": False}, 10)[0], 400)
        self.assertEqual(self.result(command, {"excerpt": "ok", "refs": [], "truncated": False}, 20)[0], 409)
        self.assertEqual(self.result(command, {"excerpt": "ok", "refs": [], "truncated": False}, 10)[0], 200)
        self.assertEqual(self.finish(holder, thread)[1]["status"], "ok")

    def test_timeout_stable_retry_no_mutation_replay_and_restart(self):
        self.attach()
        value, holder, thread = self.start("chrome_open", {"url": "http://127.0.0.1:8123/ordinary"}, rid="stable-open")
        command = self.command()
        response = self.finish(holder, thread)
        self.assertEqual(response[1]["status"], "unknown")
        self.assertEqual(self.http("/mcp", copy.deepcopy(value)), response)
        self.assertEqual(self.http("/extension/poll", extension=True)[0], 204)
        self.assertEqual(self.result(command, {"owned": True}, 10)[0], 409)
        self.assertEqual(self.broker.owned["native-a"], set())
        changed = {**value, "args": {"url": "http://127.0.0.1:8123/other"}}
        self.assertEqual(self.http("/mcp", changed)[0], 409)
        with patch("broker.MAX_OBSERVATIONS", 4):
            for index in range(12):
                denied = {"request_id": "after-unknown-" + str(index), "action": "chrome_read", "tab_id": 99, "args": {}}
                self.assertEqual(self.http("/mcp", denied)[1]["status"], "denied")
        self.assertEqual(self.http("/mcp", value), response)
        self.assertEqual(self.http("/extension/poll", extension=True)[0], 204)
        restored = Broker(self.broker.policy, self.folder / "state.json", request_timeout=0.05, poll_timeout=0)
        self.assertTrue(restored.technical_block)
        self.assertEqual(restored.submit("native-a", value)["status"], "unknown")
        self.assertIsNone(restored.poll())
        restored.close()

    def test_stop_cancels_inflight_and_persists_until_explicit_owner_attach(self):
        self.attach()
        _, holder, thread = self.start("chrome_open", {"url": "http://127.0.0.1:8123/ordinary"})
        command = self.command()
        event = {"type": "stop", "profile_id": "synthetic-profile", "session_id": "", "tab_id": 1, "reason": "owner_stop"}
        self.assertEqual(self.http("/extension/event", event, extension=True)[0], 200)
        self.assertEqual(self.finish(holder, thread)[1]["status"], "unknown")
        self.assertEqual(self.result(command, {"owned": True}, 10)[0], 409)
        restored = Broker(self.broker.policy, self.folder / "state.json", poll_timeout=0)
        self.assertTrue(restored.stop)
        self.assertEqual(restored.owned["native-a"], set())
        self.assertEqual(self.http("/extension/poll", extension=True)[0], 204)
        _, holder, thread = self.start("chrome_attachment")
        command = self.command()
        data = {"stopped": False, "authorized": True, "technical_block": False, "owner_attached": True, "manual_tab_id": 1, "owned_tab_ids": [], "profile_id": "synthetic-profile"}
        self.assertEqual(self.result(command, data)[0], 200)
        self.assertTrue(self.finish(holder, thread)[1]["data"]["stopped"])
        self.assertTrue(self.broker.stop)
        self.attach()
        self.assertFalse(self.broker.stop)
        restored.close()

    def test_extension_stop_reasons_cancel_inflight_and_persist_stop(self):
        self.attach()
        for reason in ("expired", "origin", "manual_closed", "browser_restart"):
            with self.subTest(reason=reason):
                _, holder, thread = self.start("chrome_open", {"url": "http://127.0.0.1:8123/ordinary"})
                command = self.command()
                event = {"type": "stop", "profile_id": "synthetic-profile", "session_id": "", "tab_id": 1, "reason": reason}
                self.assertEqual(self.http("/extension/event", event, extension=True)[0], 200)
                response = self.finish(holder, thread)[1]
                self.assertEqual(response["status"], "unknown")
                self.assertEqual(response["data"]["reason"], "stopped")
                self.assertEqual(self.result(command, {"owned": True}, 10)[0], 409)

                restored = Broker(self.broker.policy, self.folder / "state.json", poll_timeout=0)
                try:
                    self.assertTrue(restored.stop)
                    self.assertEqual(self.http("/extension/poll", extension=True)[0], 204)
                finally:
                    restored.close()
                self.attach()

    def test_unknown_extension_stop_reason_is_rejected_without_stopping(self):
        self.attach()
        event = {"type": "stop", "profile_id": "synthetic-profile", "session_id": "", "tab_id": 1, "reason": "unexpected"}
        self.assertEqual(self.http("/extension/event", event, extension=True)[0], 400)
        self.assertFalse(self.broker.stop)
        self.assertTrue(self.broker.authorized)
        self.assertEqual(self.broker.manual_tab, 1)

    def test_disconnect_requires_authenticated_attachment_reconcile(self):
        self.attach()
        self.open()
        event = {"type": "disconnect", "profile_id": "synthetic-profile", "session_id": "", "tab_id": 0, "reason": "technical_disconnect"}
        self.assertEqual(self.http("/extension/event", event, extension=True)[0], 200)
        self.assertFalse(self.broker.stop)
        self.assertTrue(self.broker.technical_block)
        self.assertEqual(self.http("/extension/poll", extension=True)[0], 204)
        self.assertTrue(self.broker.technical_block)
        _, holder, thread = self.start("chrome_attachment")
        command = self.command()
        data = {"stopped": False, "authorized": True, "technical_block": False, "owner_attached": True, "manual_tab_id": 1, "owned_tab_ids": [10], "profile_id": "synthetic-profile"}
        self.assertEqual(self.result(command, data)[0], 200)
        self.assertFalse(self.finish(holder, thread)[1]["data"]["technical_block"])
        self.assertEqual(self.broker.owned["native-a"], {10})

    def test_expiry_cancels_and_late_result_cannot_restore_ownership(self):
        self.attach()
        _, holder, thread = self.start("chrome_open", {"url": "http://127.0.0.1:8123/ordinary"})
        command = self.command()
        object.__setattr__(self.broker.policy, "expires_at", time.time() - 1)
        self.assertEqual(self.result(command, {"owned": True}, 10)[0], 409)
        self.assertEqual(self.finish(holder, thread)[1]["data"]["reason"], "expired")
        self.assertTrue(self.broker.stop)
        self.assertEqual(self.broker.owned["native-a"], set())

    def test_queued_extension_stop_reason_is_denied_and_no_operation_is_replayed(self):
        self.attach()
        _, holder, thread = self.start("chrome_open", {"url": "http://127.0.0.1:8123/ordinary"})
        deadline = time.monotonic() + 0.5
        while not self.broker.queue and time.monotonic() < deadline:
            time.sleep(0.001)
        self.assertEqual(self.http("/extension/event", {"type": "stop", "profile_id": "synthetic-profile", "session_id": "", "tab_id": 1, "reason": "origin"}, extension=True)[0], 200)
        self.assertEqual(self.finish(holder, thread)[1]["status"], "denied")
        self.assertEqual(self.http("/extension/poll", extension=True)[0], 204)

    def test_equal_client_ids_are_namespaced_and_canonical_retries_do_not_replay(self):
        self.attach()
        first, _ = self.open("native-a", 10, rid="same-client-id")
        second, _ = self.open("native-b", 20, rid="same-client-id")
        self.assertEqual(self.broker.owned["native-a"], {10})
        self.assertEqual(self.broker.owned["native-b"], {20})
        changed_order = {"args": first["args"], "action": first["action"], "request_id": first["request_id"]}
        self.assertEqual(self.http("/mcp", changed_order)[1]["tab_id"], 10)
        self.assertEqual(self.http("/mcp", second, sid="native-b")[1]["tab_id"], 20)
        self.assertEqual(self.http("/extension/poll", extension=True)[0], 204)
        restored = Broker(self.broker.policy, self.folder / "state.json", poll_timeout=0)
        self.assertEqual(restored.submit("native-a", first)["data"], {"owned": True})
        self.assertIsNone(restored.poll())
        restored.close()

    def test_type_wait_close_positive_flow_and_ownership_removed(self):
        self.attach()
        self.open()
        read = self.read(refs=[{"ref": "r1_0", "role": "textbox", "name": "Draft title"}])
        ref = read["data"]["refs"][0]["ref"]
        text = "Draft \U0001f600"
        _, holder, thread = self.start("chrome_type", {"ref": ref, "text": text}, 10)
        command = self.command()
        self.assertEqual(command["args"]["text"], text)
        self.assertEqual(self.result(command, {"typed": True, "length": len(text.encode("utf-16-le")) // 2}, 10)[0], 200)
        self.assertEqual(self.finish(holder, thread)[1]["data"]["typed"], True)
        _, holder, thread = self.start("chrome_wait", {"text": "Draft"}, 10)
        command = self.command()
        self.assertEqual(self.result(command, {"matched": False, "reason": "not_observed"}, 10)[0], 200)
        self.assertFalse(self.finish(holder, thread)[1]["data"]["matched"])
        _, holder, thread = self.start("chrome_close", tab=10)
        command = self.command()
        self.assertEqual(self.result(command, {"closed": True}, 10)[0], 200)
        self.assertTrue(self.finish(holder, thread)[1]["data"]["closed"])
        self.assertEqual(self.broker.owned["native-a"], set())
        self.assertEqual(self.http("/mcp", {"request_id": "closed-read", "action": "chrome_read", "tab_id": 10, "args": {}})[1]["data"]["reason"], "ownership")

    def test_reconcile_rejects_foreign_and_manual_owned_tabs(self):
        self.attach()
        self.open("native-b", 20)
        _, holder, thread = self.start("chrome_attachment")
        command = self.command()
        data = {"stopped": False, "authorized": True, "technical_block": False, "owner_attached": True, "manual_tab_id": 1, "owned_tab_ids": [20], "profile_id": "synthetic-profile"}
        self.assertEqual(self.result(command, data)[0], 400)
        data["owned_tab_ids"] = [1]
        self.assertEqual(self.result(command, data)[0], 400)
        data["owned_tab_ids"] = []
        self.assertEqual(self.result(command, data)[0], 200)
        self.assertEqual(self.finish(holder, thread)[1]["data"]["owned_tab_ids"], [])

    def test_malformed_owned_journal_fails_closed_at_startup(self):
        self.attach()
        self.open(rid="prior-mutation")
        journal = (self.folder / "state.json").read_bytes()
        first = json.loads(journal.splitlines()[0])
        cases = [("root", b"[]\n")]
        for index, malformed in enumerate(([], None, 0, "not-a-map")):
            initial = {**first, "owned": malformed}
            delta = {key: first[key] for key in ("schema", "profile_id", "policy_hash")}
            delta.update(owned=malformed, requests=[])
            cases.append(("initial-" + str(index), json_bytes(initial) + b"\n"))
            cases.append(("delta-" + str(index), journal + json_bytes(delta) + b"\n"))
        for name, content in cases:
            with self.subTest(record=name):
                path = self.folder / ("malformed-" + name + ".json")
                path.write_bytes(content)
                restored = Broker(self.broker.policy, path, request_timeout=0.05, poll_timeout=0)
                try:
                    self.assertTrue(restored.state_failed)
                    self.assertTrue(restored.stop)
                    self.assertFalse(restored.authorized)
                    self.assertTrue(restored.technical_block)
                    self.assertEqual(restored.owned, {"native-a": set(), "native-b": set()})
                    self.assertEqual(restored.requests, {})
                    response = restored.submit("native-a", {"request_id": "state-check", "action": "chrome_attachment", "args": {}})
                    self.assertEqual(response["status"], "denied")
                    self.assertEqual(response["data"]["reason"], "state_io")
                    self.assertIsNone(restored.poll())
                    with self.assertRaises(Reject):
                        restored.extension_event({"type": "attach", "profile_id": "synthetic-profile", "session_id": "", "tab_id": 1, "reason": "owner_attach"})
                    self.assertTrue(restored.stop)
                finally:
                    restored.close()

    def test_corrupt_state_fail_closed_and_capabilities_not_reflected_as_ids(self):
        path = self.folder / "corrupt-state.json"
        path.write_text('{"schema":1}', encoding="utf-8")
        restored = Broker(self.broker.policy, path, request_timeout=0.05, poll_timeout=0)
        response = restored.submit("native-a", {"request_id": "state-check", "action": "chrome_attachment", "args": {}})
        self.assertEqual(response["status"], "denied")
        self.assertEqual(response["data"]["reason"], "state_io")
        self.assertTrue(restored.stop)
        restored.close()
        status, response = self.http("/mcp", {"request_id": "A" * 32, "action": "chrome_attachment", "args": {}})
        self.assertEqual(status, 400)
        self.assertNotIn("A" * 32, json.dumps(response))

    def diagnostics(self, **changes):
        value = {"reader_status": "empty", "ready_state": "complete", "document_count": 2,
                 "same_origin_frame_count": 1, "blocked_frame_count": 1,
                 "main_excerpt_chars": 0, "child_excerpt_chars": 120}
        value.update(changes)
        return value

    def read_result(self, command, payload, tab=10):
        return self.result(command, payload, tab)

    def test_read_accepts_bounded_diagnostics_and_safe_same_origin_url(self):
        self.attach()
        self.open("native-a", 10)
        _, holder, thread = self.start("chrome_read", tab=10)
        command = self.command()
        payload = {"excerpt": "", "refs": [{"ref": "r1_0", "role": "link", "name": "Invoices",
                                            "url": "http://127.0.0.1:8123/menu"}],
                   "truncated": False, "diagnostics": self.diagnostics()}
        self.assertEqual(self.read_result(command, payload)[0], 200)
        response = self.finish(holder, thread)[1]
        self.assertEqual(response["status"], "ok")
        self.assertEqual(response["data"]["diagnostics"], self.diagnostics())
        self.assertEqual(len(response["data"]["refs"]), 1)
        self.assertEqual(response["data"]["refs"][0]["url"], "http://127.0.0.1:8123/menu")
        self.assertEqual(response["data"]["refs"][0]["name"], "Invoices")

    def test_read_rejects_diagnostics_outside_frozen_bounds(self):
        self.attach()
        self.open("native-a", 10)
        _, holder, thread = self.start("chrome_read", tab=10)
        command = self.command()
        bad_cases = [
            self.diagnostics(reader_status="full"),
            self.diagnostics(ready_state="done"),
            self.diagnostics(document_count=65),
            self.diagnostics(blocked_frame_count=-1),
            self.diagnostics(main_excerpt_chars=6001),
            self.diagnostics(child_excerpt_chars="120"),
            {**self.diagnostics(), "page_title": "canary"},
            {"reader_status": "empty"},
        ]
        for index, diagnostics in enumerate(bad_cases):
            with self.subTest(case=index):
                payload = {"excerpt": "", "refs": [], "truncated": False, "diagnostics": diagnostics}
                self.assertEqual(self.read_result(command, payload)[0], 400)
        payload = {"excerpt": "", "refs": [], "truncated": False}
        self.assertEqual(self.read_result(command, payload)[0], 200)
        self.assertEqual(self.finish(holder, thread)[1]["status"], "ok")

    def test_read_omits_risky_ref_urls_without_failing(self):
        self.attach()
        self.open("native-a", 10)
        risky = [
            "http://127.0.0.1:8123/menu?token=abc",
            "http://127.0.0.1:8123/menu#access_token=abc",
            "http://127.0.0.1:8123/login/",
            "http://127.0.0.1:8123/oauth/start",
            "https://example.com/menu",
            "javascript:alert(1)",
            "http://user:pass@127.0.0.1:8123/menu",
            "http://127.0.0.1:8123/menu?eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.c2lnbmF0dXJl",
            "http://127.0.0.1:8123/menu?credentials=1",
            "http://127.0.0.1:8123/menu?sig=1",
            "http://127.0.0.1:8123/menu?oauth_token=abc",
            "http://127.0.0.1:8123/menu?csrf_token=abc",
            "http://127.0.0.1:8123/menu?%74oken=abc",
            "http://127.0.0.1:8123/menu;jsessionid=abc",
            "http://127.0.0.1:8123/menu?reset_token=abc",
            "http://127.0.0.1:8123/menu?%61uth=abc",
            "http://127.0.0.1:8123/menu?%61pi_key=abc",
            "http://127.0.0.1:8123/menu?%73ig=abc",
            "http://127.0.0.1:8123/menu?%252574oken=abc",
            "http://127.0.0.1:8123/menu%253Bjsessionid=abc",
            # URL fragment carries an auth/consent route (SPA hash routing).
            "http://127.0.0.1:8123/#/login",
            "http://127.0.0.1:8123/#/consent",
            "http://127.0.0.1:8123/#/signin",
            "http://127.0.0.1:8123/#/sign-in",
            "http://127.0.0.1:8123/#/oauth/callback",
            "http://127.0.0.1:8123/#/mfa/challenge",
            "http://127.0.0.1:8123/#%2Fconsent",
            "http://127.0.0.1:8123/menu#/authorize?client_id=demo",
            # SPA auth/consent route in a deeper fragment segment (any-segment).
            "http://127.0.0.1:8123/#/account/login",
            "http://127.0.0.1:8123/#/consent/allow",
            "http://127.0.0.1:8123/#/signin/callback",
            "http://127.0.0.1:8123/#/oauth/authorize/approve",
            "http://127.0.0.1:8123/#/mfa/verify",
            "http://127.0.0.1:8123/#/saml/response",
            "http://127.0.0.1:8123/#/passkey/enrol",
            "http://127.0.0.1:8123/#%2Faccount%2Flogin",
            # Hashbang SPA auth/consent routes.
            "http://127.0.0.1:8123/#!/login",
            "http://127.0.0.1:8123/#!/consent",
            "http://127.0.0.1:8123/#!/signin",
            "http://127.0.0.1:8123/#!/mfa/challenge",
            "http://127.0.0.1:8123/#!/oauth/callback",
            "http://127.0.0.1:8123/#!/account/login",
        ]
        for url in risky:
            with self.subTest(url=url):
                _, holder, thread = self.start("chrome_read", tab=10)
                command = self.command()
                payload = {"excerpt": "Useful page text survives omitted link", "refs": [
                    {"ref": "r1_0", "role": "link", "name": "Menu", "url": url},
                    {"ref": "r2_0", "role": "link", "name": "Safe menu", "url": "http://127.0.0.1:8123/menu"}
                ], "truncated": False}
                self.assertEqual(self.read_result(command, payload)[0], 200)
                response = self.finish(holder, thread)[1]
                self.assertEqual(response["status"], "ok")
                self.assertEqual(response["data"]["excerpt"], payload["excerpt"])
                self.assertNotIn("url", response["data"]["refs"][0])
                self.assertEqual(response["data"]["refs"][1]["url"], "http://127.0.0.1:8123/menu")
                self.assertNotIn(url, json.dumps(response))

    def test_read_fit_covers_diagnostics_and_urls_within_max_read(self):
        self.attach()
        self.open("native-a", 10)
        _, holder, thread = self.start("chrome_read", tab=10)
        command = self.command()
        refs = [{"ref": "r%d_0" % (index + 1), "role": "link", "name": "Target %d" % index,
                 "url": "http://127.0.0.1:8123/menu-%d" % index} for index in range(10)]
        # Filler sized so the raw extension payload stays within MAX_READ
        # while the broker's opaque ref wrapping pushes the fitted result
        # over the cap, engaging the fit loop (truncated True).
        payload = {"excerpt": "Billingo invoice row details. " * 95, "refs": refs, "truncated": False,
                   "diagnostics": self.diagnostics(reader_status="partial", child_excerpt_chars=6000)}
        self.assertLessEqual(len(json.dumps(payload).encode("utf-8")), 4096)
        self.assertEqual(self.read_result(command, payload)[0], 200)
        response = self.finish(holder, thread)[1]
        self.assertLessEqual(len(json.dumps(response["data"]).encode("utf-8")), 4096)
        self.assertTrue(response["data"]["truncated"])
        self.assertIn("diagnostics", response["data"])


    def test_large_menu_ref_remapping_preserves_useful_unicode_text(self):
        self.attach()
        self.open("native-a", 10)
        _, holder, thread = self.start("chrome_read", tab=10)
        command = self.command()
        refs = [{"ref": "r1_" + str(i), "role": "link", "name": "Information menu " + str(i),
                 "url": "http://127.0.0.1:8123/account/dashboard/ordinary/menu-" + str(i)} for i in range(28)]
        marker = "Zażółć gęślą jaźń 😀 useful dashboard"
        payload = {"excerpt": marker, "refs": refs, "truncated": False, "diagnostics": self.diagnostics()}
        self.assertLessEqual(len(json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()), 4096)
        self.assertEqual(self.read_result(command, payload)[0], 200)
        response = self.finish(holder, thread)[1]
        self.assertEqual(response["data"]["excerpt"], marker)
        self.assertLessEqual(len(json.dumps(response["data"], ensure_ascii=False, separators=(",", ":")).encode()), 4096)
        self.assertTrue(response["data"]["refs"])
        self.assertTrue(response["data"]["truncated"])
        self.assertEqual(response["data"]["diagnostics"]["reader_status"], "partial")

    def test_read_preserves_benign_spa_hash_routes_with_login_word(self):
        # Benign SPA hash routes whose name happens to contain an auth stem
        # joined with a non-segment separator (e.g. "-history", "AsUser") must
        # survive the any-segment fragment check and keep their safe URL.
        self.attach()
        self.open("native-a", 10)
        benign = [
            "http://127.0.0.1:8123/#/dashboard/login-history",
            "http://127.0.0.1:8123/#/profile/loginAsUser",
            "http://127.0.0.1:8123/#!/dashboard/login-history",
            "http://127.0.0.1:8123/menu#/dashboard/view",
            "http://127.0.0.1:8123/menu#/invoices/12345",
        ]
        for url in benign:
            with self.subTest(url=url):
                _, holder, thread = self.start("chrome_read", tab=10)
                command = self.command()
                payload = {"excerpt": "Benign page text", "refs": [
                    {"ref": "r1_0", "role": "link", "name": "Continue", "url": url}
                ], "truncated": False}
                self.assertEqual(self.read_result(command, payload)[0], 200)
                response = self.finish(holder, thread)[1]
                self.assertEqual(response["status"], "ok")
                self.assertEqual(response["data"]["refs"][0]["url"], url)

    def test_credential_url_detects_spa_auth_route_at_any_fragment_segment(self):
        from broker import credential_url
        from urllib.parse import urlsplit
        reject = [
            # Deeper fragment segments with an auth/consent stem.
            "http://127.0.0.1:8123/#/account/login",
            "http://127.0.0.1:8123/#/consent/allow",
            "http://127.0.0.1:8123/#/signin/callback",
            "http://127.0.0.1:8123/#/oauth/authorize/approve",
            "http://127.0.0.1:8123/#/mfa/verify",
            "http://127.0.0.1:8123/#/saml/response",
            "http://127.0.0.1:8123/#/passkey/enrol",
            "http://127.0.0.1:8123/#%2Faccount%2Flogin",
            "http://127.0.0.1:8123/#%2Fconsent%2Fallow",
            # Hashbang fragments.
            "http://127.0.0.1:8123/#!/login",
            "http://127.0.0.1:8123/#!/consent",
            "http://127.0.0.1:8123/#!/signin",
            "http://127.0.0.1:8123/#!/mfa/challenge",
            "http://127.0.0.1:8123/#!/oauth/callback",
            "http://127.0.0.1:8123/#!/account/login",
            "http://127.0.0.1:8123/#!/saml/response",
            "http://127.0.0.1:8123/#!%2Flogin",
            # Lower-case hashbang variants.
            "http://127.0.0.1:8123/#!/Login",
            "http://127.0.0.1:8123/#!/OAuth/Callback",
            # Existing first-segment fragment routes remain blocked.
            "http://127.0.0.1:8123/#/login",
            "http://127.0.0.1:8123/#/oauth/callback",
        ]
        for url in reject:
            with self.subTest(url=url):
                self.assertTrue(credential_url(urlsplit(url)), url)
        # Benign fragments whose name contains an auth stem at a non-segment
        # boundary must remain allowed.
        allow = [
            "http://127.0.0.1:8123/#/dashboard/login-history",
            "http://127.0.0.1:8123/#/profile/loginAsUser",
            "http://127.0.0.1:8123/#!/dashboard/login-history",
            "http://127.0.0.1:8123/menu#/dashboard/view",
            "http://127.0.0.1:8123/menu#/invoices/12345",
            "http://127.0.0.1:8123/#/",
            "http://127.0.0.1:8123/#!/",
        ]
        for url in allow:
            with self.subTest(url=url):
                self.assertFalse(credential_url(urlsplit(url)), url)

    def test_permits_url_rejects_spa_auth_route_fragments(self):
        # Policy.permits_url delegates the same auth/consent stem detection
        # and must reject every URL credential_url rejects. Synthetic-only
        # policy with the loopback origin keeps the test isolated.
        raw = policy_dict()
        policy = Policy.from_dict(raw)
        reject = [
            "http://127.0.0.1:8123/#/account/login",
            "http://127.0.0.1:8123/#!/login",
            "http://127.0.0.1:8123/#!/mfa/challenge",
            "http://127.0.0.1:8123/#/consent/allow",
            "http://127.0.0.1:8123/#%2Faccount%2Flogin",
        ]
        for url in reject:
            with self.subTest(url=url):
                with self.assertRaises(Reject) as raised:
                    policy.permits_url(url)
                self.assertEqual(raised.exception.reason, "oauth", url)
        # Benign same-origin URLs still pass through permits_url.
        allow = [
            "http://127.0.0.1:8123/menu",
            "http://127.0.0.1:8123/#/dashboard/login-history",
            "http://127.0.0.1:8123/#!/dashboard/login-history",
            "http://127.0.0.1:8123/menu#/dashboard/view",
        ]
        for url in allow:
            with self.subTest(url=url):
                self.assertEqual(policy.permits_url(url), url)

    def test_credential_url_detects_angular_path_param_fragments(self):
        # Regression: same-origin SPA fragment routes where the auth/consent
        # stem is followed by an Angular-style ";" path-parameter
        # ("#/login;returnUrl=...") used to slip past credential_url because
        # the existing terminator set only knew about "/" / end-of-segment.
        # The path-only ";" check at broker.py:_safe_ref_url is unaffected;
        # credential_url is the fail-closed detector for both path-param and
        # file-extension-style auth routes.
        from broker import credential_url
        from urllib.parse import urlsplit
        reject = [
            "http://127.0.0.1:8123/#/login;returnUrl=%2Fdashboard",
            "http://127.0.0.1:8123/#/sign-in;redirect=%2Fdashboard",
            "http://127.0.0.1:8123/#/consent;client_id=app",
            "http://127.0.0.1:8123/#/oauth/callback;state=abc",
            "http://127.0.0.1:8123/#/mfa/challenge;next=/dashboard",
            "http://127.0.0.1:8123/#/account/login;returnUrl=%2F",
            "http://127.0.0.1:8123/#!/sign-in;returnUrl=%2F",
            "http://127.0.0.1:8123/#%2Flogin%3BreturnUrl%3D%2Fdashboard",
            "http://127.0.0.1:8123/menu;#/login;returnUrl=%2Fdashboard",
        ]
        for url in reject:
            with self.subTest(url=url):
                self.assertTrue(credential_url(urlsplit(url)), url)

    def test_credential_url_detects_file_extension_auth_routes(self):
        # Regression: same-origin auth routes served through static file
        # extensions ("/login.php", "/signin.aspx", "#/login.html") used to
        # slip past credential_url because "." was not a segment terminator.
        from broker import credential_url
        from urllib.parse import urlsplit
        reject = [
            "http://127.0.0.1:8123/login.php",
            "http://127.0.0.1:8123/signin.php",
            "http://127.0.0.1:8123/log-in.aspx",
            "http://127.0.0.1:8123/oauth/start.php",
            "http://127.0.0.1:8123/saml/sso.aspx",
            "http://127.0.0.1:8123/mfa/challenge.json",
            "http://127.0.0.1:8123/#/login.html",
            "http://127.0.0.1:8123/#/consent.html",
            "http://127.0.0.1:8123/#/oauth/authorize.aspx",
            "http://127.0.0.1:8123/#/mfa/challenge.json",
            "http://127.0.0.1:8123/#!/login.php",
            "http://127.0.0.1:8123/#%2Flogin.html",
        ]
        for url in reject:
            with self.subTest(url=url):
                self.assertTrue(credential_url(urlsplit(url)), url)

    def test_credential_url_detects_rails_devise_underscore_routes(self):
        # Regression: Rails/Devise-style auth routes use underscore stems
        # ("/users/sign_in", "/log_in") which used to bypass the hyphen-only
        # stem list. The stem list now carries both spellings.
        from broker import credential_url
        from urllib.parse import urlsplit
        reject = [
            "http://127.0.0.1:8123/users/sign_in",
            "http://127.0.0.1:8123/users/log_in",
            "http://127.0.0.1:8123/users/sign-in",
            "http://127.0.0.1:8123/users/password",
            "http://127.0.0.1:8123/sign_in",
            "http://127.0.0.1:8123/log_in",
            "http://127.0.0.1:8123/admin/sign_in.json",
            "http://127.0.0.1:8123/admin/users/log_in/edit",
            "http://127.0.0.1:8123/#/users/sign_in",
            "http://127.0.0.1:8123/#/users/log_in",
            "http://127.0.0.1:8123/#/sign_in",
            "http://127.0.0.1:8123/#/log_in",
        ]
        for url in reject:
            with self.subTest(url=url):
                self.assertTrue(credential_url(urlsplit(url)), url)

    def test_credential_url_preserves_benign_underscore_and_hyphen_segments(self):
        # The widened stem list and terminator set must keep allowing benign
        # segments where the auth stem is joined with a non-separator hyphen
        # or underscore (e.g. login-history, signin_settings, authorize-foo).
        from broker import credential_url
        from urllib.parse import urlsplit
        allow = [
            "http://127.0.0.1:8123/dashboard/login-history",
            "http://127.0.0.1:8123/profile/loginAsUser",
            "http://127.0.0.1:8123/some_login_history",
            "http://127.0.0.1:8123/article/login-changes-everywhere",
            "http://127.0.0.1:8123/authorize-foo",
            "http://127.0.0.1:8123/menu/sign_in_options",
            "http://127.0.0.1:8123/#/dashboard/login-history",
            "http://127.0.0.1:8123/#/profile/loginAsUser",
            "http://127.0.0.1:8123/#!/dashboard/login-history",
            "http://127.0.0.1:8123/menu#/dashboard/view",
            "http://127.0.0.1:8123/menu#/invoices/12345",
            "http://127.0.0.1:8123/ordinary",
        ]
        for url in allow:
            with self.subTest(url=url):
                self.assertFalse(credential_url(urlsplit(url)), url)

    def test_read_omits_undercovered_auth_route_forms_without_failing(self):
        # End-to-end coverage for the three undercovered forms called out
        # by the parent diagnosis (Angular ";returnUrl=..." in fragments,
        # file-extension auth routes, Rails/Devise underscore auth routes).
        # The omitted URL must never leak into the read response and the
        # surrounding page text plus the safe ref must survive intact.
        self.attach()
        self.open("native-a", 10)
        risky = [
            "http://127.0.0.1:8123/#/login;returnUrl=%2Fdashboard",
            "http://127.0.0.1:8123/login.php",
            "http://127.0.0.1:8123/signin.aspx",
            "http://127.0.0.1:8123/#/login.html",
            "http://127.0.0.1:8123/users/sign_in",
            "http://127.0.0.1:8123/log_in",
            "http://127.0.0.1:8123/#!/oauth/callback;state=abc",
            "http://127.0.0.1:8123/menu?view=login",
            "http://127.0.0.1:8123/menu?action=sign_in",
            "http://127.0.0.1:8123/menu?route=%2Faccount%2Flogin.php",
            "http://127.0.0.1:8123/menu?mode=oauth2",
            "http://127.0.0.1:8123/menu?page=consent",
            "http://127.0.0.1:8123/menu?api-key=synthetic",
            "http://127.0.0.1:8123/menu?label=%73%6b-aaaaaaaaaaaaaaaaaaaa",
            "http://127.0.0.1:8123/menu?label=%25252574oken",
            "http://127.0.0.1:8123/menu?label=%FF",
        ]
        for url in risky:
            with self.subTest(url=url):
                _, holder, thread = self.start("chrome_read", tab=10)
                command = self.command()
                payload = {"excerpt": "Useful page text survives omitted link",
                           "refs": [{"ref": "r1_0", "role": "link", "name": "Risky", "url": url},
                                    {"ref": "r2_0", "role": "link", "name": "Safe",
                                     "url": "http://127.0.0.1:8123/menu"}],
                           "truncated": False}
                self.assertEqual(self.read_result(command, payload)[0], 200)
                response = self.finish(holder, thread)[1]
                self.assertEqual(response["status"], "ok")
                self.assertEqual(response["data"]["excerpt"], payload["excerpt"])
                self.assertNotIn("url", response["data"]["refs"][0])
                self.assertEqual(response["data"]["refs"][1]["url"], "http://127.0.0.1:8123/menu")
                self.assertNotIn(url, json.dumps(response))

    def test_safe_url_metadata_preserves_exact_encoded_url(self):
        self.attach()
        self.open("native-a", 10)
        url = "http://127.0.0.1:8123/menu?label=%E2%9C%93&author=ordinary&postcode=12"
        _, holder, thread = self.start("chrome_read", tab=10)
        command = self.command()
        payload = {"excerpt": "Useful menu text", "refs": [
            {"ref": "r1_0", "role": "link", "name": "Safe menu", "url": url}], "truncated": False}
        self.assertEqual(self.read_result(command, payload)[0], 200)
        response = self.finish(holder, thread)[1]
        self.assertEqual(response["status"], "ok")
        self.assertEqual(response["data"]["refs"][0]["url"], url)
        self.assertEqual(response["data"]["excerpt"], payload["excerpt"])

    def test_credential_label_detects_hungarian_billingo_auth_labels(self):
        # Regression: the previous regex only recognised English
        # "sign in" / "log in" labels. Hungarian Billingo login forms use
        # "Bejelentkezés" / "Jelszó" / "Hozzáférés" labels that would
        # otherwise remain clickable refs against the same-origin scope.
        from broker import credential_label
        hungarian = [
            "Bejelentkezés",
            "bejelentkezés",
            "Bejelentkezni",
            "Bejelentkezem",
            "Jelszó",
            "Jelszó megadása",
            "Hozzáférés engedélyezése",
            "Hozzáférés",
            "Bejelentkezés a fiókba",
        ]
        for label in hungarian:
            with self.subTest(label=label):
                self.assertTrue(credential_label(label), label)
        # Benign translated labels must remain clickable.
        benign = [
            "Ügyfél neve",
            "Számla sorszáma",
            "Bevétel",
            "Dátum",
            "Profil megtekintése",
            "Számlák listája",
        ]
        for label in benign:
            with self.subTest(label=label):
                self.assertFalse(credential_label(label), label)

    def test_read_omits_hungarian_auth_label_without_failing(self):
        # End-to-end: a translated Hungarian login button (e.g. the
        # "Jelszó" link on a Billingo password-change screen) must be
        # omitted from the response. The page text and the benign ref must
        # survive intact, and the dropped name must not appear anywhere in
        # the JSON payload.
        self.attach()
        self.open("native-a", 10)
        _, holder, thread = self.start("chrome_read", tab=10)
        command = self.command()
        payload = {"excerpt": "Hasznos számlaadatok maradnak a lapon.",
                   "refs": [
                       {"ref": "r1_0", "role": "link", "name": "Jelszó",
                        "url": "http://127.0.0.1:8123/profile"},
                       {"ref": "r2_0", "role": "link", "name": "Profil megtekintése",
                        "url": "http://127.0.0.1:8123/profile"},
                   ],
                   "truncated": False}
        self.assertEqual(self.read_result(command, payload)[0], 200)
        response = self.finish(holder, thread)[1]
        self.assertEqual(response["status"], "ok")
        self.assertEqual(response["data"]["excerpt"], payload["excerpt"])
        self.assertEqual(len(response["data"]["refs"]), 1)
        self.assertEqual(response["data"]["refs"][0]["name"], "Profil megtekintése")
        self.assertNotIn("Jelszó", json.dumps(response))

if __name__ == "__main__":
    unittest.main()
