"""Managed controller tests over real loopback HTTP; no real browser/accounts."""

import copy
import hashlib
import http.client
import io
import json
import os
import queue
import secrets
import socket
import subprocess
import sys
import threading
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import managed
from broker import ACTIONS, Broker, Policy, Reject, json_bytes
from managed import (CLIENT_SID_RE, CLIENT_TOKEN_RE, Controller, ManagedBroker, ManagedGrant, config_from_dict,
                     extension_config, make_managed_server, run_cli, run_mcp, summary)
from mcp import tool_specs

DEFAULT_ROOT = Path(__file__).resolve().parents[4] / "task-evidence" / "chrome-relay-tests"
SOURCE_ROOT = Path(__file__).resolve().parents[1]
ROOT = Path(os.environ.get("TEAM_TEST_ROOT", str(DEFAULT_ROOT))).resolve()
if ROOT.is_relative_to(SOURCE_ROOT):
    raise RuntimeError("TEAM_TEST_ROOT must resolve outside the chrome-relay source directory")
PYTHON = sys.executable
SCRIPT = SOURCE_ROOT / "managed.py"
EXTENSION_TOKEN = "E" * 32
CONTROLLER_KEY = "K" * 40
ORIGIN = "http://127.0.0.1:8123"
# Shared with test_extension.cjs: the extension must derive the same value.
GOLDEN_GENERATION = "587dc3e21cfba84bad4e7ea386509508"


def config_dict(state_dir, port=9444, **grant_changes):
    grant = {"grant_id": "grant-test-1", "profile_id": "synthetic-profile", "allowed_origins": [ORIGIN],
             "allowed_actions": sorted(ACTIONS - {"chrome_saved_login"}), "extension_id": "a" * 32, "persistent": True, "synthetic": True,
             "harnesses": {"codex": {"transport": "mcp", "native_session_env": "CODEX_THREAD_ID"},
                           "claude": {"transport": "mcp"}, "kimi": {"transport": "cli"}}}
    grant.update(grant_changes)
    return {"schema": managed.SCHEMA, "grant": grant, "broker_base": "http://127.0.0.1:%d" % port,
            "state_dir": str(state_dir), "extension_token": EXTENSION_TOKEN, "controller_key": CONTROLLER_KEY,
            "client_ttl_s": 60}


class ManagedHarness(unittest.TestCase):
    def setUp(self):
        self.folder = ROOT / ("managed-" + secrets.token_hex(8))
        self.folder.mkdir(parents=True, exist_ok=True)
        self.now = time.time()
        self.raw = config_dict(self.folder)
        self.server = None
        self.start()

    def tearDown(self):
        self.shutdown()

    def start(self, raw=None):
        self.raw = copy.deepcopy(raw or self.raw)
        self.config = config_from_dict(self.raw)
        self.broker = ManagedBroker(self.config.grant, self.config.state_dir, self.config.client_ttl_s,
                                    request_timeout=0.7, poll_timeout=0.02, clock=lambda: self.now)
        self.server = make_managed_server(self.broker, port=0)
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        self.thread.start()
        # The port is operational, not grant scope: it never changes the hash.
        self.raw["broker_base"] = "http://127.0.0.1:%d" % self.server.server_address[1]
        self.config = config_from_dict(self.raw)

    def shutdown(self):
        if self.server is not None:
            self.broker.close()
            self.server.shutdown()
            self.server.server_close()
            self.thread.join(2)
            self.server = None

    def restart(self, raw=None):
        self.shutdown()
        self.start(raw)

    def http(self, path, value=None, headers=None, method=None, raw=None):
        conn = http.client.HTTPConnection(*self.server.server_address, timeout=3)
        sent = {"Content-Type": "application/json"}
        sent.update(headers or {})
        body = raw if raw is not None else (json_bytes(value) if value is not None else None)
        try:
            conn.request(method or ("POST" if body is not None else "GET"), path, body=body, headers=sent)
            response = conn.getresponse()
            data = response.read()
            return response.status, json.loads(data) if data else None, data
        finally:
            conn.close()

    def register(self, harness="claude", transport="mcp", key=CONTROLLER_KEY, headers=None, **extra):
        sent = {"Authorization": "Bearer " + key} if key is not None else {}
        sent.update(headers or {})
        status, body, _ = self.http("/managed/register", {"harness": harness, "transport": transport, **extra}, sent)
        return status, body

    def client(self, harness="claude", transport="mcp"):
        status, body = self.register(harness, transport)
        self.assertEqual(status, 200, body)
        return body["session_id"], body["token"]

    def mcp(self, client, action, args=None, tab=None, rid=None):
        value = {"request_id": rid or secrets.token_hex(8), "action": action, "args": args or {}}
        if tab is not None:
            value["tab_id"] = tab
        sid, token = client
        return self.http("/mcp", value, {"Authorization": "Bearer " + token, "X-Session-Id": sid})[:2]

    def start_mcp(self, client, action, args=None, tab=None, rid=None):
        holder = {}
        thread = threading.Thread(target=lambda: holder.update(response=self.mcp(client, action, args, tab, rid)), daemon=True)
        thread.start()
        return holder, thread

    def extension(self, path, value=None):
        headers = {"Authorization": "Bearer " + EXTENSION_TOKEN, "X-Profile-Id": "synthetic-profile"}
        return self.http(path, value, headers)[:2]

    def attach(self):
        self.assertEqual(self.extension("/extension/event", {"type": "attach", "profile_id": "synthetic-profile", "session_id": "", "tab_id": 1, "reason": "owner_attach"})[0], 200)

    def owner_stop(self):
        self.assertEqual(self.extension("/extension/event", {"type": "stop", "profile_id": "synthetic-profile", "session_id": "", "tab_id": 0, "reason": "owner_stop"})[0], 200)

    def command(self):
        deadline = time.monotonic() + 0.6
        while time.monotonic() < deadline:
            status, command = self.extension("/extension/poll")
            if status == 200:
                return command
        self.fail("no command dispatched")

    def no_command(self):
        deadline = time.monotonic() + 0.2
        while time.monotonic() < deadline:
            self.assertEqual(self.extension("/extension/poll")[0], 204)

    def result(self, command, data, tab=None, status="ok"):
        value = {"request_id": command["request_id"], "session_id": command["session_id"], "profile_id": command["profile_id"], "status": status, "data": data}
        if tab is not None:
            value["tab_id"] = tab
        return self.extension("/extension/result", value)[0]

    def finish(self, holder, thread):
        thread.join(3)
        self.assertFalse(thread.is_alive())
        return holder["response"]

    def open(self, client, tab, rid=None):
        holder, thread = self.start_mcp(client, "chrome_open", {"url": ORIGIN + "/"}, rid=rid)
        command = self.command()
        self.assertEqual(command["session_id"], client[0])
        self.assertEqual(self.result(command, {"owned": True}, tab), 200)
        status, body = self.finish(holder, thread)
        self.assertEqual((status, body["status"]), (200, "ok"))
        return body

    def registry(self):
        return json.loads((self.folder / "clients.json").read_bytes())


class BrowserRestartTests(ManagedHarness):
    def restart_event(self, **changes):
        value = {"type": "managed_browser_restart", "profile_id": "synthetic-profile", "session_id": "",
                 "tab_id": 0, "reason": "browser_restart", "generation": self.broker.generation}
        value.update(changes)
        return self.extension("/extension/event", value)

    def test_browser_restart_invalidates_ownership_without_clearing_grant_or_stop(self):
        client = self.client()
        self.attach()
        self.open(client, 10, "initial-open")
        self.assertEqual(self.restart_event()[0], 200)
        self.assertFalse(self.broker.stop)
        self.assertTrue(self.broker.authorized)
        self.assertTrue(self.broker.technical_block)
        self.assertEqual(self.broker.manual_tab, 0)
        self.assertEqual(self.broker.owned[client[0]], set())
        self.assertEqual(self.mcp(client, "chrome_read", tab=10)[1]["status"], "denied")
        self.owner_stop()
        self.assertEqual(self.restart_event()[0], 200)
        self.assertTrue(self.broker.stop)
        self.register()
        self.assertTrue(self.broker.stop)

    def test_restart_observation_survives_but_dispatched_mutation_never_replays(self):
        client = self.client()
        self.attach()
        pending, thread = self.start_mcp(client, "chrome_open", {"url": ORIGIN + "/"}, rid="pending-mutation")
        command = self.command()
        observed, observer = self.start_mcp(client, "chrome_attachment", rid="after-restart")
        time.sleep(.025)
        self.assertEqual(self.restart_event()[0], 200)
        self.assertEqual(self.finish(pending, thread)[1]["status"], "unknown")
        self.assertEqual(self.result(command, {"owned": True}, 10), 409)
        attachment = self.command()
        self.assertEqual(attachment["action"], "chrome_attachment")
        self.assertEqual(self.result(attachment, {"stopped": False, "authorized": True, "technical_block": False,
            "owner_attached": False, "manual_tab_id": 0, "owned_tab_ids": [], "profile_id": "synthetic-profile"}), 200)
        self.assertFalse(self.finish(observed, observer)[1]["data"]["technical_block"])
        self.assertEqual(self.mcp(client, "chrome_open", {"url": ORIGIN + "/"}, rid="pending-mutation")[1]["status"], "unknown")
        self.no_command()
        self.open(client, 11)

    def test_restart_event_is_generation_and_extension_bound(self):
        self.attach()
        for changes in ({"generation": "0" * 32}, {"tab_id": 1}, {"session_id": "foreign"}, {"reason": "owner_attach"}, {"extra": True}):
            self.assertNotEqual(self.restart_event(**changes)[0], 200)
        client = self.client()
        value = {"type": "managed_browser_restart", "profile_id": "synthetic-profile", "session_id": "", "tab_id": 0,
                 "reason": "browser_restart", "generation": self.broker.generation}
        status, _, _ = self.http("/extension/event", value, {"Authorization": "Bearer " + client[1], "X-Profile-Id": "synthetic-profile"})
        self.assertEqual(status, 401)
        self.assertEqual(self.broker.manual_tab, 1)


class RegistrationTests(ManagedHarness):
    def test_register_requires_controller_key_and_rejects_browser_calls(self):
        sid, token = self.client()
        self.assertTrue(CLIENT_SID_RE.fullmatch(sid) and CLIENT_TOKEN_RE.fullmatch(token))
        self.assertEqual(self.register(key=None)[0], 401)
        self.assertEqual(self.register(key="W" * 40)[0], 401)
        self.assertEqual(self.register(key=token)[0], 401)  # A client capability cannot mint others.
        self.assertEqual(self.register(key=EXTENSION_TOKEN)[0], 401)
        for header in ({"Origin": "chrome-extension://" + "a" * 32}, {"Origin": "null"}, {"Sec-Fetch-Site": "same-origin"},
                       {"Sec-Fetch-Mode": "cors"}, {"Referer": ORIGIN + "/"}):
            self.assertEqual(self.register(headers=header), (403, {"error": "origin"}))
        self.assertEqual(self.register(headers={"Host": "localhost:%d" % self.server.server_address[1]})[0], 403)
        status, body, _ = self.http("/managed/register", raw=b"{}", headers={"Authorization": "Bearer " + CONTROLLER_KEY, "Content-Type": "text/plain"})
        self.assertEqual(status, 415)
        self.assertEqual(self.http("/managed/register", method="OPTIONS")[0], 405)
        self.assertEqual(self.http("/managed/register", headers={"Authorization": "Bearer " + CONTROLLER_KEY})[0], 404)
        self.assertEqual(self.register("qwen"), (403, {"error": "harness"}))
        self.assertEqual(self.register("not-a-harness"), (400, {"error": "unknown_harness"}))
        self.assertEqual(self.register("kimi", "mcp"), (403, {"error": "transport"}))
        self.assertEqual(self.register("claude", native_session_id="x", native_session_source="env:CODEX_THREAD_ID")[0], 400)
        self.assertEqual(self.register("codex", native_session_id="t-1", native_session_source="env:OTHER")[0], 400)
        self.assertEqual(self.register(profile_id="other")[0], 400)
        # Capability material is only returned once; storage keeps a hash.
        stored = (self.folder / "clients.json").read_bytes() + (self.folder / "audit.jsonl").read_bytes()
        for secret in (token, CONTROLLER_KEY, EXTENSION_TOKEN):
            self.assertNotIn(secret.encode(), stored)
        self.assertEqual(self.registry()["clients"][sid]["token_sha256"], hashlib.sha256(token.encode()).hexdigest())

    def test_native_session_recorded_only_when_supplied(self):
        status, body = self.register("codex", native_session_id="thread-42", native_session_source="env:CODEX_THREAD_ID")
        self.assertEqual(status, 200)
        record = self.registry()["clients"][body["session_id"]]
        self.assertEqual((record["native_session_id"], record["native_session_source"]), ("thread-42", "env:CODEX_THREAD_ID"))
        self.assertNotIn("verified", json.dumps(record))
        sid, _ = self.client("codex")
        self.assertNotIn("native_session_id", self.registry()["clients"][sid])
        self.assertEqual(managed.native_session({"native_session_env": "CODEX_THREAD_ID"}, {"CODEX_THREAD_ID": "thread-1"}), ("thread-1", "env:CODEX_THREAD_ID"))
        self.assertIsNone(managed.native_session({"native_session_env": "CODEX_THREAD_ID"}, {"PID": str(os.getpid())}))
        self.assertIsNone(managed.native_session({}, {"CODEX_THREAD_ID": "thread-1"}))

    def test_client_expiry_renew_release(self):
        client = self.client()
        sid, token = client
        auth = {"Authorization": "Bearer " + token, "X-Session-Id": sid}
        self.now += 50
        self.assertEqual(self.http("/managed/renew", {}, auth)[:2], (200, {"session_id": sid, "expires_in_s": 60}))
        self.now += 50  # 100s after registration, 50s after renewal.
        self.assertEqual(self.mcp(client, "chrome_open", {"url": ORIGIN + "/"})[1]["data"]["reason"], "stopped")
        self.now += 11
        self.assertEqual(self.mcp(client, "chrome_open", {"url": ORIGIN + "/"}), (401, {"error": "expired"}))
        self.assertEqual(self.http("/managed/renew", {}, auth)[:2], (401, {"error": "authorization"}))
        self.assertNotIn(sid, self.registry()["clients"])
        other = self.client()
        other_auth = {"Authorization": "Bearer " + other[1], "X-Session-Id": other[0]}
        self.assertEqual(self.http("/managed/release", {}, other_auth)[:2], (200, {"released": True}))
        self.assertEqual(self.mcp(other, "chrome_attachment"), (401, {"error": "authorization"}))
        self.assertEqual(self.registry()["clients"], {})
        # Wrong client capability cannot renew/release another client.
        third = self.client()
        self.assertEqual(self.http("/managed/release", {}, {"Authorization": "Bearer " + other[1], "X-Session-Id": third[0]})[0], 401)
        self.assertIn(third[0], self.registry()["clients"])

    def test_client_bound_and_expired_sweep(self):
        clients = [self.client() for _ in range(managed.MAX_CLIENTS)]
        self.assertEqual(self.register(), (429, {"error": "bound"}))
        self.now += 61
        self.client()  # Expired leases are swept before the bound is applied.
        self.assertEqual(len(self.registry()["clients"]), 1)
        self.assertEqual(self.mcp(clients[0], "chrome_attachment"), (401, {"error": "authorization"}))


class IsolationTests(ManagedHarness):
    def test_two_concurrent_clients_are_isolated(self):
        a, b = self.client("claude"), self.client("codex")
        self.attach()
        self.open(a, 11)
        self.open(b, 12)
        self.assertEqual(self.mcp(b, "chrome_read", tab=11)[1]["data"]["reason"], "ownership")
        self.assertEqual(self.mcp(a, "chrome_close", tab=12)[1]["data"]["reason"], "ownership")
        self.assertEqual(self.http("/mcp", {"request_id": "x1", "action": "chrome_attachment", "args": {}}, {"Authorization": "Bearer " + b[1], "X-Session-Id": a[0]})[0], 401)
        # Concurrent reads are dispatched separately and routed to their owner.
        first, first_thread = self.start_mcp(a, "chrome_read", tab=11)
        second, second_thread = self.start_mcp(b, "chrome_read", tab=12)
        commands = [self.command(), self.command()]
        self.assertEqual({(c["session_id"], c["tab_id"]) for c in commands}, {(a[0], 11), (b[0], 12)})
        for command in commands:
            text = "page-" + str(command["tab_id"])
            self.assertEqual(self.result(command, {"excerpt": text, "refs": [{"ref": "r1_1", "role": "button", "name": "Go"}], "truncated": False}, command["tab_id"]), 200)
        read_a, read_b = self.finish(first, first_thread)[1], self.finish(second, second_thread)[1]
        self.assertEqual((read_a["session_id"], read_a["data"]["excerpt"]), (a[0], "page-11"))
        self.assertEqual((read_b["session_id"], read_b["data"]["excerpt"]), (b[0], "page-12"))
        # One client's opaque ref is meaningless to another client/tab.
        ref = read_a["data"]["refs"][0]["ref"]
        self.assertEqual(self.mcp(b, "chrome_click", {"ref": ref}, tab=12)[1]["data"]["reason"], "stale_ref")
        attachment, thread = self.start_mcp(b, "chrome_attachment")
        command = self.command()
        self.result(command, {"stopped": False, "authorized": True, "technical_block": False, "owner_attached": True, "manual_tab_id": 1, "owned_tab_ids": [11, 12], "profile_id": "synthetic-profile"})
        self.assertEqual(self.finish(attachment, thread)[1]["status"], "unknown")  # Claiming A's tab is refused.
        # Releasing A never touches B.
        self.assertEqual(self.http("/managed/release", {}, {"Authorization": "Bearer " + a[1], "X-Session-Id": a[0]})[0], 200)
        self.assertEqual(self.broker.owned, {b[0]: {12}})


class PersistenceTests(ManagedHarness):
    def test_stop_persists_across_restart_and_is_never_self_cleared(self):
        client = self.client()
        self.attach()
        self.owner_stop()
        self.restart()
        self.assertTrue(self.broker.stop)
        self.assertEqual(self.mcp(client, "chrome_open", {"url": ORIGIN + "/"})[1]["data"]["reason"], "stopped")
        fresh = self.client()  # Registration, renewal and restart never clear STOP.
        self.http("/managed/renew", {}, {"Authorization": "Bearer " + fresh[1], "X-Session-Id": fresh[0]})
        self.restart()
        self.assertEqual(self.mcp(fresh, "chrome_open", {"url": ORIGIN + "/"})[1]["data"]["reason"], "stopped")
        self.attach()  # Only the owner's explicit attach releases it.
        self.assertFalse(self.broker.stop)
        self.restart()
        self.assertFalse(self.broker.stop)

    def test_persistent_grant_survives_restart_without_expiry(self):
        client = self.client()
        self.attach()
        self.open(client, 21)
        generation = self.broker.generation
        self.restart()
        self.assertEqual(self.broker.generation, generation)
        self.assertEqual(self.broker.policy.expires_at, float("inf"))
        self.assertFalse(self.broker._expire())
        self.assertEqual(self.broker.owned[client[0]], {21})
        self.assertIn(client[0], self.registry()["clients"])

    def test_no_replay_after_restart(self):
        client = self.client()
        self.attach()
        self.open(client, 31, rid="open-1")
        holder, thread = self.start_mcp(client, "chrome_close", tab=31, rid="close-1")
        self.command()  # Dispatched, never answered before the broker dies.
        self.restart()
        self.finish(holder, thread)
        self.attach()
        self.assertEqual(self.mcp(client, "chrome_open", {"url": ORIGIN + "/"}, rid="open-1")[1]["status"], "ok")
        status, body = self.mcp(client, "chrome_close", tab=31, rid="close-1")
        self.assertEqual((status, body["status"]), (200, "unknown"))
        self.no_command()
        self.assertEqual(self.mcp(client, "chrome_open", {"url": ORIGIN + "/x"}, rid="open-1"), (409, {"error": "request_id_conflict"}))
        # After release the SID is gone; nothing under it can be replayed.
        self.http("/managed/release", {}, {"Authorization": "Bearer " + client[1], "X-Session-Id": client[0]})
        self.restart()
        self.assertEqual(self.mcp(client, "chrome_open", {"url": ORIGIN + "/"}, rid="open-1")[0], 401)
        self.no_command()

    def test_grant_and_scope_changes_revoke_clients_and_set_stop(self):
        changes = [{"allowed_actions": ["chrome_attachment", "chrome_read"]}, {"allowed_origins": ["http://127.0.0.1:8124"]},
                   {"grant_id": "grant-test-2"}, {"harnesses": {"claude": {"transport": "mcp"}}}, "controller"]
        for change in changes:
            with self.subTest(change=change):
                client = self.client()
                self.attach()
                generation = self.broker.generation
                raw = copy.deepcopy(self.raw)
                if change == "controller":
                    raw["controller_key"] = "N" * 40
                else:
                    raw["grant"].update(change)
                self.restart(raw)
                self.assertTrue(self.broker.stop)
                self.assertEqual(self.registry()["clients"], {})
                self.assertEqual(self.mcp(client, "chrome_attachment"), (401, {"error": "authorization"}))
                # The extension-visible scope moves the wire generation; controller
                # trust and harness enrollment are broker-side and revoke clients.
                if change == "controller" or "harnesses" in change:
                    self.assertEqual(self.broker.generation, generation)
                else:
                    self.assertNotEqual(self.broker.generation, generation)
                if change == "controller":
                    self.assertEqual(self.register()[0], 401)
                    status, _ = self.register(key="N" * 40)
                    self.assertEqual(status, 200)
                    self.raw["controller_key"] = "N" * 40
        # A non-scope operational change (client TTL) keeps grant and clients.
        status, body = self.register(key="N" * 40)
        self.assertEqual(status, 200)
        client = body["session_id"], body["token"]
        generation = self.broker.generation
        raw = copy.deepcopy(self.raw)
        raw["client_ttl_s"] = 120
        self.restart(raw)
        self.assertIn(client[0], self.registry()["clients"])
        self.assertEqual(self.broker.generation, generation)

    def test_corrupt_registry_fails_closed(self):
        self.client()
        self.shutdown()
        (self.folder / "clients.json").write_bytes(b'{"schema":"chrome-relay-clients/1","policy_hash":')
        self.start()
        self.assertTrue(self.broker.state_failed and self.broker.stop)
        self.assertEqual(self.register(), (503, {"error": "state_io"}))


class ConfigTests(unittest.TestCase):
    def setUp(self):
        self.folder = ROOT / ("managed-config-" + secrets.token_hex(8))

    def test_persistent_is_explicit_and_validated(self):
        raw = config_dict(self.folder)
        self.assertTrue(config_from_dict(raw).grant.persistent)
        for persistent in (False, 1, "true"):
            bad = copy.deepcopy(raw)
            bad["grant"]["persistent"] = persistent
            with self.assertRaises(Reject):
                config_from_dict(bad)
        both = copy.deepcopy(raw)
        both["grant"]["expires_at"] = "2099-01-01T00:00:00Z"
        neither = copy.deepcopy(raw)
        del neither["grant"]["persistent"]
        for bad in (both, neither):
            with self.assertRaises(Reject):
                config_from_dict(bad)
        far = copy.deepcopy(raw)
        del far["grant"]["persistent"]
        far["grant"]["expires_at"] = "9999-01-01T00:00:00Z"
        grant = config_from_dict(far).grant
        self.assertFalse(grant.persistent)  # A huge expiry is still an expiry, never persistent.
        self.assertEqual(grant.expires_ms, 253370764800000)

    def test_config_rejects_unsafe_values(self):
        raw = config_dict(self.folder)
        cases = [("state_dir", "relative/dir"), ("broker_base", "http://localhost:9444"), ("broker_base", "http://127.0.0.1:9444/x"),
                 ("controller_key", EXTENSION_TOKEN), ("controller_key", "short"), ("client_ttl_s", 5), ("extra", 1)]
        for key, value in cases:
            bad = copy.deepcopy(raw)
            bad[key] = value
            with self.subTest(key=key, value=value), self.assertRaises(Reject):
                config_from_dict(bad)
        grant_cases = [("allowed_actions", ["chrome_sign_in"]), ("allowed_origins", ["http://example.com"]),
                       ("harnesses", {"unknown": {"transport": "mcp"}}), ("harnesses", {"codex": {"transport": "stdio"}}),
                       ("harnesses", {"codex": {"transport": "mcp", "native_session_env": "OPENAI_API_KEY"}}),
                       ("harnesses", {"glm": {"transport": "mcp"}, "zcode": {"transport": "cli"}}), ("profile_id", "bad id")]
        for key, value in grant_cases:
            bad = copy.deepcopy(raw)
            bad["grant"][key] = value
            with self.subTest(key=key, value=value), self.assertRaises(Reject):
                config_from_dict(bad)
        alias = copy.deepcopy(raw)
        alias["grant"]["harnesses"] = {"glm/zcode": {"transport": "mcp"}}
        self.assertEqual(dict(config_from_dict(alias).grant.harnesses), {"glm": {"transport": "mcp"}})

    def test_generation_vector_and_extension_config(self):
        config = config_from_dict(config_dict(self.folder))
        self.assertEqual(config.grant.wire_generation(), GOLDEN_GENERATION)
        value = extension_config(config)
        self.assertEqual(value["schema"], 2)
        self.assertEqual(value["mode"], "managed")
        self.assertIs(value["persistent"], True)
        self.assertNotIn("session_ids", value)
        self.assertNotIn("controller_key", json.dumps(value))
        self.assertNotIn(CONTROLLER_KEY, json.dumps(value))
        # Legacy policy is untouched: still a different, legacy generation.
        legacy = Policy.from_dict({"profile_id": "synthetic-profile", "allowed_origins": [ORIGIN], "allowed_actions": ["chrome_read"],
                                   "sessions": {"a": "A" * 32}, "extension_token": EXTENSION_TOKEN, "extension_id": "a" * 32,
                                   "expires_at": 1760198400123, "synthetic": True})
        self.assertNotIsInstance(legacy, ManagedGrant)
        with self.assertRaises(Reject):
            ManagedBroker(legacy, self.folder)

    def test_sign_in_is_only_the_owner_bound_argument_free_saved_login_tool(self):
        names = {tool["name"] for tool in tool_specs()}
        self.assertEqual(names, set(ACTIONS))
        self.assertEqual({name for name in names if "sign" in name or "login" in name or "autofill" in name}, {"chrome_saved_login"})
        login = next(tool for tool in tool_specs() if tool["name"] == "chrome_saved_login")
        self.assertEqual(set(login["inputSchema"]["properties"]), {"request_id", "tab_id"})
        self.assertIn('optional owner "saved_login" binding', managed.__doc__)
        self.assertIn("submitted/unverified", managed.__doc__)


class CommandLineTests(unittest.TestCase):
    def setUp(self):
        self.folder = ROOT / ("managed-cli-" + secrets.token_hex(8))
        self.folder.mkdir(parents=True, exist_ok=True)
        self.state = self.folder / "state"
        self.path = self.folder / "config.json"
        # A port that was just free: the launcher must never reach a real broker.
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        self.path.write_bytes(json_bytes(config_dict(self.state, port)))

    def run_script(self, *args, stdin=b""):
        return subprocess.run([PYTHON, "-B", str(SCRIPT), *args], input=stdin, capture_output=True, timeout=30,
                              shell=False, creationflags=managed.CREATE_NO_WINDOW if os.name == "nt" else 0)

    def test_validate_prints_no_secrets_and_writes_nothing(self):
        done = self.run_script("validate", "--config", str(self.path))
        self.assertEqual(done.returncode, 0, done.stderr)
        value = json.loads(done.stdout)
        self.assertEqual((value["status"], value["generation"], value["persistent"]), ("ok", GOLDEN_GENERATION, True))
        self.assertEqual(value["harnesses"], {"claude": "mcp", "codex": "mcp", "kimi": "cli"})
        for secret in (EXTENSION_TOKEN, CONTROLLER_KEY):
            self.assertNotIn(secret.encode(), done.stdout + done.stderr)
        self.assertFalse(self.state.exists())
        self.assertEqual(sorted(path.name for path in self.folder.iterdir()), ["config.json"])
        relative = self.run_script("validate", "--config", "config.json")
        self.assertEqual((relative.returncode, json.loads(relative.stdout)), (1, {"status": "error", "reason": "config"}))

    def test_launcher_accepts_no_overrides(self):
        for extra in (["--profile-id", "x"], ["--session-id", "x"], ["--token", "x"], ["--origin", ORIGIN], ["--broker-base", "http://127.0.0.1:1"], ["--conf", str(self.path)]):
            with self.subTest(extra=extra):
                done = self.run_script("mcp", "--config", str(self.path), "--harness", "claude", *extra)
                self.assertEqual(done.returncode, 2)
                self.assertEqual(done.stdout, b"")
        # A broker that is not running fails closed with clean stdout.
        done = self.run_script("mcp", "--config", str(self.path), "--harness", "claude")
        self.assertEqual((done.returncode, done.stdout), (0, b""))
        self.assertFalse(self.state.exists())

    def test_main_invalid_mcp_config_exposes_tools_and_returns_config_blocker(self):
        messages = [{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "1"}}},
                    {"jsonrpc": "2.0", "method": "notifications/initialized"},
                    {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
                    {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "chrome_attachment", "arguments": {"request_id": "config-denied"}}}]
        missing = self.folder / "missing-config.json"
        done = self.run_script("mcp", "--config", str(missing), "--harness", "codex",
                               stdin=b"".join(json_bytes(item) + b"\n" for item in messages))
        self.assertEqual(done.returncode, 0, done.stderr)
        replies = [json.loads(line) for line in done.stdout.splitlines()]
        self.assertEqual(len(replies[1]["result"]["tools"]), 8)
        self.assertTrue(replies[2]["result"]["isError"])
        payload = json.loads(replies[2]["result"]["content"][0]["text"])
        self.assertEqual(payload["request_id"], "config-denied")
        self.assertEqual(payload["data"]["blocker"], "BLOCKED_MANAGED_BROWSER_UNAVAILABLE")
        self.assertEqual(payload["data"]["detail"], "config")
        self.assertNotIn(str(missing).encode(), done.stdout + done.stderr)

    def test_main_invalid_cli_config_emits_blocker_for_each_line(self):
        missing = self.folder / "missing-cli-config.json"
        lines = json_bytes({"name": "chrome_attachment", "arguments": {}}) + b"\nnot-json\n"
        done = self.run_script("cli", "--config", str(missing), "--harness", "kimi", stdin=lines)
        self.assertEqual(done.returncode, 3)
        replies = [json.loads(line) for line in done.stdout.splitlines()]
        self.assertEqual(len(replies), 2)
        for reply in replies:
            self.assertEqual(reply["status"], "denied")
            self.assertEqual(reply["data"]["blocker"], "BLOCKED_MANAGED_BROWSER_UNAVAILABLE")
            self.assertEqual(reply["data"]["detail"], "config")


class LauncherTests(ManagedHarness):
    INIT_PARAMS = {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "test", "version": "1"}}

    @classmethod
    def initialize_lines(cls, *calls):
        messages = [{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": cls.INIT_PARAMS},
                    {"jsonrpc": "2.0", "method": "notifications/initialized"}]
        messages.extend(calls)
        return b"".join(json_bytes(message) + b"\n" for message in messages)

    @staticmethod
    def call(rpc_id, name="chrome_attachment", arguments=None):
        return {"jsonrpc": "2.0", "id": rpc_id, "method": "tools/call",
                "params": {"name": name, "arguments": arguments or {}}}

    @staticmethod
    def tool_payload(replies, rpc_id):
        reply = next(item for item in replies if item.get("id") == rpc_id)
        return json.loads(reply["result"]["content"][0]["text"])

    class RecordingController:
        def __init__(self, delegate):
            self.delegate = delegate
            self.sessions = []

        def register(self, *args):
            session = self.delegate.register(*args)
            self.sessions.append(session)
            return session

        def renew(self, *args):
            return self.delegate.renew(*args)

        def release(self, *args):
            return self.delegate.release(*args)

    class QueueInput:
        def __init__(self, initial=b""):
            self.lines = queue.Queue()
            for line in initial.splitlines(keepends=True):
                self.lines.put(line)

        def feed(self, data):
            for line in data.splitlines(keepends=True):
                self.lines.put(line)

        def close(self):
            self.lines.put(b"")

        def readline(self, _size=-1):
            return self.lines.get(timeout=30)

    class LockedOutput:
        def __init__(self):
            self.value = bytearray()
            self.condition = threading.Condition()

        def write(self, data):
            with self.condition:
                self.value.extend(data)
                self.condition.notify_all()
            return len(data)

        def flush(self):
            return None

        def wait_lines(self, count, timeout=10):
            deadline = time.monotonic() + timeout
            with self.condition:
                while len(self.value.splitlines()) < count:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise AssertionError("timed out waiting for MCP output")
                    self.condition.wait(remaining)
                return bytes(self.value)

        def getvalue(self):
            with self.condition:
                return bytes(self.value)

    def run_real_launcher(self, messages, *, idle_release_s=managed.IDLE_RELEASE_S, controller=None, popen=subprocess.Popen,
                          environ=None):
        output = self.LockedOutput()
        if environ is None:
            environ = {key: os.environ[key] for key in ("SYSTEMROOT", "SystemRoot") if key in os.environ}
        status = run_mcp(self.config, "claude", environ=environ, popen=popen,
                         controller=controller or Controller(self.config), stdin=io.BytesIO(messages), stdout=output,
                         idle_release_s=idle_release_s)
        return status, output.getvalue()

    def test_initialize_and_tools_list_do_not_register(self):
        calls = []

        def forbidden_popen(*_args, **_kwargs):
            calls.append(True)
            raise AssertionError("no child before a valid tools/call")

        status, output = self.run_real_launcher(self.initialize_lines({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}),
                                                popen=forbidden_popen)
        replies = [json.loads(line) for line in output.splitlines()]
        self.assertEqual(status, 0)
        self.assertEqual(len(replies[1]["result"]["tools"]), 8)
        self.assertEqual(self.broker.clients, {})
        self.assertFalse((self.folder / "clients.json").exists())
        self.assertEqual(calls, [])

    def test_startup_harness_rejection_keeps_unavailable_mcp_mode(self):
        calls = []

        class Child:
            def wait(self):
                return 7

        def popen(argv, **kwargs):
            calls.append((argv, kwargs))
            return Child()

        status = run_mcp(self.config, "kimi", environ={"SYSTEMROOT": "C:\\Windows"}, popen=popen,
                         stdin=io.BytesIO(), stdout=self.LockedOutput())
        self.assertEqual(status, 7)
        self.assertEqual(len(calls), 1)
        argv, kwargs = calls[0]
        self.assertEqual(argv, [sys.executable, "-B", str(SOURCE_ROOT / "mcp.py")])
        self.assertEqual(kwargs["env"], {"SYSTEMROOT": "C:\\Windows", "PYTHONDONTWRITEBYTECODE": "1",
                                         "CHROME_RELAY_UNAVAILABLE": "transport"})
        self.assertIsNone(kwargs["stdin"])
        self.assertIsNone(kwargs["stdout"])
        self.assertIs(kwargs["stderr"], subprocess.DEVNULL)
        self.assertTrue(kwargs["close_fds"])
        self.assertFalse(kwargs["shell"])
        if os.name == "nt":
            self.assertEqual(kwargs["creationflags"], managed.CREATE_NO_WINDOW)
        self.assertEqual(self.broker.clients, {})

    def test_first_call_registers_once_and_uses_real_child_response(self):
        recorder = self.RecordingController(Controller(self.config))
        captured = []

        def popen(argv, **kwargs):
            captured.append((argv, dict(kwargs["env"]), kwargs.copy()))
            return subprocess.Popen(argv, **kwargs)

        messages = self.initialize_lines(self.call(2))
        status, output = self.run_real_launcher(messages, controller=recorder, popen=popen,
                                                environ={"SYSTEMROOT": "C:\\Windows", "CODEX_THREAD_ID": "thread-7"})
        replies = [json.loads(line) for line in output.splitlines()]
        result = self.tool_payload(replies, 2)
        self.assertEqual(status, 0)
        self.assertEqual(len(recorder.sessions), 1)
        sid, token = recorder.sessions[0]
        self.assertEqual(result["session_id"], sid)
        self.assertIn(result["status"], ("ok", "denied", "unknown"))
        self.assertNotIn(sid, self.registry()["clients"])  # EOF releases the active slot.
        self.assertEqual(len(captured), 1)
        argv, child_env, kwargs = captured[0]
        self.assertEqual(argv, [sys.executable, "-B", str(SOURCE_ROOT / "mcp.py")])
        self.assertTrue(CLIENT_SID_RE.fullmatch(child_env["CHROME_RELAY_SESSION_ID"]))
        self.assertEqual(child_env["CHROME_RELAY_SESSION_ID"], sid)
        self.assertEqual(child_env["CHROME_RELAY_TOKEN"], token)
        self.assertNotIn(token, " ".join(argv))
        self.assertNotIn(CONTROLLER_KEY, " ".join(argv))
        self.assertEqual(set(child_env), {"SYSTEMROOT", "CHROME_RELAY_BROKER", "CHROME_RELAY_SESSION_ID",
                                         "CHROME_RELAY_TOKEN", "PYTHONDONTWRITEBYTECODE"})
        self.assertEqual(kwargs["stdin"], subprocess.PIPE)
        self.assertEqual(kwargs["stdout"], subprocess.PIPE)
        self.assertIs(kwargs["stderr"], subprocess.DEVNULL)
        self.assertFalse(kwargs["shell"])
        self.assertTrue(kwargs["close_fds"])
        if os.name == "nt":
            self.assertEqual(kwargs["creationflags"], managed.CREATE_NO_WINDOW)
        for secret in (token, CONTROLLER_KEY, EXTENSION_TOKEN):
            self.assertNotIn(secret.encode(), output)

    def test_seventeen_local_launchers_leave_slots_free_for_a_call(self):
        gate = threading.Event()
        condition = threading.Condition()
        waiting = [0]

        class HeldInput:
            def __init__(self):
                self.lines = LauncherTests.initialize_lines({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}).splitlines(keepends=True)

            def readline(self, _size=-1):
                if self.lines:
                    return self.lines.pop(0)
                with condition:
                    waiting[0] += 1
                    condition.notify_all()
                gate.wait(10)
                return b""

        outputs = [self.LockedOutput() for _ in range(17)]
        results = [None] * 17
        threads = [threading.Thread(target=lambda i=i: results.__setitem__(i, run_mcp(
            self.config, "claude", environ={}, popen=lambda *_a, **_k: self.fail("unexpected child"),
            controller=Controller(self.config), stdin=HeldInput(), stdout=outputs[i]))) for i in range(17)]
        for thread in threads:
            thread.start()
        deadline = time.monotonic() + 10
        with condition:
            while waiting[0] < 17 and time.monotonic() < deadline:
                condition.wait(deadline - time.monotonic())
        self.assertEqual(waiting[0], 17)
        self.assertEqual(self.broker.clients, {})
        gate.set()
        for thread in threads:
            thread.join(10)
        self.assertTrue(all(not thread.is_alive() for thread in threads))
        self.assertEqual(results, [0] * 17)
        self.assertTrue(all(len(json.loads(output.getvalue().splitlines()[1])["result"]["tools"]) == 8 for output in outputs))
        recorder = self.RecordingController(Controller(self.config))
        status, output = self.run_real_launcher(self.initialize_lines(self.call(3)), controller=recorder)
        self.assertEqual(status, 0)
        self.assertEqual(len(recorder.sessions), 1)
        payload = self.tool_payload([json.loads(line) for line in output.splitlines()], 3)
        self.assertIn("session_id", payload, payload)
        self.assertEqual(payload["session_id"], recorder.sessions[0][0])

    def test_idle_release_frees_slot_and_next_call_gets_fresh_sid(self):
        source = self.QueueInput(self.initialize_lines({"jsonrpc": "2.0", "id": 4, "method": "tools/list"}, self.call(2)))
        output = self.LockedOutput()
        recorder = self.RecordingController(Controller(self.config))
        result = {}
        parent_env = {key: os.environ[key] for key in ("SYSTEMROOT", "SystemRoot") if key in os.environ}
        thread = threading.Thread(target=lambda: result.setdefault("status", run_mcp(
            self.config, "claude", environ=parent_env, controller=recorder, stdin=source, stdout=output,
            idle_release_s=0.3)))
        thread.start()
        first_output = output.wait_lines(3)
        first_payload = self.tool_payload([json.loads(line) for line in first_output.splitlines()], 2)
        self.assertIn("session_id", first_payload, first_payload)
        first_sid = first_payload["session_id"]
        deadline = time.monotonic() + 5
        while self.broker.clients and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertEqual(self.broker.clients, {})
        source.feed(json_bytes(self.call(3)) + b"\n")
        output.wait_lines(4)
        source.close()
        thread.join(10)
        self.assertFalse(thread.is_alive())
        replies = [json.loads(line) for line in output.getvalue().splitlines()]
        second_sid = self.tool_payload(replies, 3)["session_id"]
        self.assertEqual(result["status"], 0)
        self.assertEqual(len(recorder.sessions), 2)
        self.assertNotEqual(first_sid, second_sid)
        self.assertNotIn(second_sid, self.registry()["clients"])

    def test_bound_rejection_returns_local_blocker_without_child_and_backoff(self):
        class RejectingController:
            def __init__(self):
                self.calls = 0

            def register(self, *_args):
                self.calls += 1
                raise Reject("bound", 429)

        controller = RejectingController()
        output = self.LockedOutput()
        lines = self.initialize_lines(self.call(2), self.call(3))
        status = run_mcp(self.config, "claude", environ={}, controller=controller,
                         popen=lambda *_a, **_k: self.fail("bound must not start a child"),
                         stdin=io.BytesIO(lines), stdout=output)
        replies = [json.loads(line) for line in output.getvalue().splitlines()]
        self.assertEqual(status, 0)
        self.assertEqual(controller.calls, 1)
        for rpc_id in (2, 3):
            reply = next(item for item in replies if item.get("id") == rpc_id)
            self.assertTrue(reply["result"]["isError"])
            payload = json.loads(reply["result"]["content"][0]["text"])
            self.assertEqual(payload["data"]["blocker"], "BLOCKED_MANAGED_BROWSER_UNAVAILABLE")
            self.assertEqual(payload["data"]["detail"], "bound")

    def test_invalid_tool_arguments_are_local_protocol_errors(self):
        output = self.LockedOutput()
        lines = self.initialize_lines(self.call(2, "chrome_open", {}))
        status = run_mcp(self.config, "claude", environ={},
                         controller=Controller(self.config),
                         popen=lambda *_a, **_k: self.fail("invalid arguments must not start a child"),
                         stdin=io.BytesIO(lines), stdout=output)
        replies = [json.loads(line) for line in output.getvalue().splitlines()]
        self.assertEqual(status, 0)
        reply = next(item for item in replies if item.get("id") == 2)
        self.assertEqual(reply["error"]["code"], -32602)
        self.assertEqual(self.broker.clients, {})
        self.assertFalse((self.folder / "clients.json").exists())

    def test_unexpected_child_exit_returns_unknown_without_replaying(self):
        writes = []

        class PipeReader:
            def __init__(self):
                self.lines = queue.Queue()

            def readline(self, _size=-1):
                return self.lines.get()

            def close(self):
                self.lines.put(b"")

        class PipeWriter:
            def __init__(self, output):
                self.output = output
                self.closed = False

            def write(self, data):
                writes.append(data)
                value = json.loads(data)
                if value.get("method") == "initialize":
                    result = {"jsonrpc": "2.0", "id": value["id"], "result": {"protocolVersion": "2025-06-18"}}
                    self.output.lines.put(json_bytes(result) + b"\n")
                elif value.get("method") == "tools/call":
                    self.output.lines.put(b"")  # Exit with the request still outstanding.
                return len(data)

            def flush(self):
                return None

            def close(self):
                if not self.closed:
                    self.closed = True
                    self.output.lines.put(b"")

        class Child:
            def __init__(self):
                self.stdout = PipeReader()
                self.stdin = PipeWriter(self.stdout)

            def wait(self, timeout=None):
                return 0

            def terminate(self):
                self.stdout.lines.put(b"")

        children = []

        def popen(argv, **kwargs):
            children.append((argv, kwargs))
            return Child()

        recorder = self.RecordingController(Controller(self.config))
        source = self.QueueInput(self.initialize_lines(self.call(2)))
        output = self.LockedOutput()
        result = {}
        thread = threading.Thread(target=lambda: result.setdefault("status", run_mcp(
            self.config, "claude", environ={key: os.environ[key] for key in ("SYSTEMROOT", "SystemRoot") if key in os.environ},
            controller=recorder, popen=popen, stdin=source, stdout=output)))
        thread.start()
        try:
            rendered = output.wait_lines(2)
        finally:
            source.close()
            thread.join(10)
        replies = [json.loads(line) for line in rendered.splitlines()]
        self.assertTrue(any(json.loads(line).get("method") == "tools/call" for line in writes), writes)
        payload = self.tool_payload(replies, 2)
        self.assertFalse(thread.is_alive())
        self.assertEqual(result["status"], 0)
        self.assertEqual(payload, {"status": "unknown", "data": {"reason": "transport"}})
        self.assertEqual(len(children), 1)
        self.assertEqual(len(recorder.sessions), 1)
        self.assertNotIn(recorder.sessions[0][0], self.registry()["clients"])

    def test_eof_releases_slot_and_stdout_never_contains_capabilities(self):
        recorder = self.RecordingController(Controller(self.config))
        status, output = self.run_real_launcher(self.initialize_lines(self.call(2)), controller=recorder)
        sid, token = recorder.sessions[0]
        self.assertEqual(status, 0)
        self.assertNotIn(sid, self.registry()["clients"])
        for secret in (token, CONTROLLER_KEY, EXTENSION_TOKEN):
            self.assertNotIn(secret.encode(), output)

    def test_cli_fallback_for_harness_without_mcp(self):
        lines = io.BytesIO(json_bytes({"name": "chrome_open", "arguments": {"request_id": "cli-1", "url": ORIGIN + "/"}}) + b"\n" + b"not json\n")
        out = io.BytesIO()
        self.assertEqual(run_cli(self.config, "kimi", lines, out, environ={}), 0)
        first, second = [json.loads(line) for line in out.getvalue().splitlines()]
        self.assertEqual((first["status"], first["data"]["reason"]), ("denied", "stopped"))
        self.assertTrue(CLIENT_SID_RE.fullmatch(first["session_id"]))
        self.assertEqual(second, {"status": "denied", "data": {"reason": "invalid"}})
        self.assertEqual(self.registry()["clients"], {})
        denied_input = io.BytesIO(b"one\ntwo\n")
        denied_output = io.BytesIO()
        self.assertEqual(run_cli(self.config, "claude", denied_input, denied_output, environ={}), 3)
        replies = [json.loads(line) for line in denied_output.getvalue().splitlines()]
        self.assertEqual(len(replies), 2)
        for reply in replies:
            self.assertEqual(reply["data"]["blocker"], "BLOCKED_MANAGED_BROWSER_UNAVAILABLE")
            self.assertEqual(reply["data"]["detail"], "transport")

    def test_cli_registration_failure_blocks_each_line_then_returns_three(self):
        class RejectingController:
            def register(self, *_):
                raise Reject("register")

        output = io.BytesIO()
        result = run_cli(self.config, "kimi", io.BytesIO(b"one\ntwo\n"), output, environ={},
                         controller=RejectingController())
        self.assertEqual(result, 3)
        replies = [json.loads(line) for line in output.getvalue().splitlines()]
        self.assertEqual(len(replies), 2)
        for reply in replies:
            self.assertEqual(reply["data"]["reason"], "relay_unavailable")
            self.assertEqual(reply["data"]["blocker"], "BLOCKED_MANAGED_BROWSER_UNAVAILABLE")
            self.assertEqual(reply["data"]["detail"], "broker_unreachable_or_register")


if __name__ == "__main__":
    unittest.main()
