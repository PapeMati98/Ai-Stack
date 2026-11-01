"""Managed persistent controller for the Fleet Chrome relay. Python 3.13 stdlib.

Optional mode beside the legacy static-session policy (broker.py --policy),
which stays unchanged. One owner grant fixes the profile, origins, actions,
extension and enrolled harnesses; short-lived client capabilities are
separate and are issued per MCP process by a controller-authorized route.

  managed.py validate --config ABS   check config; no browser, no writes
  managed.py broker   --config ABS   serve the managed loopback broker
  managed.py mcp      --config ABS --harness NAME   stdio MCP launcher; unavailable relay stays discoverable
  managed.py cli      --config ABS --harness NAME   semantic CLI fallback; unavailable relay denies each line

The launcher accepts no profile, origin, session or token overrides. Each
process handles MCP initialization and tools/list locally without taking a
broker slot. Its first valid tools/call registers a fresh random connection
SID (c-<32 hex>) whose capability reaches only its own child through the
environment, never argv or output. The slot is released after 20 minutes
without a tools/call and no pending response, or when the launcher exits.
Tabs owned by a released session stop belonging to that agent. A native
harness session ID is recorded only when the enrolled environment variable
actually supplies one; it is never labelled verified and a PID/UUID is never
substituted for it.

Persistence: a persistent grant is an explicit validated policy flag (not a
far expiry). STOP lives in the broker journal and is cleared only by the
owner's attach event; registering, renewing or restarting never clears it.
Grant scope (grant ID, origins, actions, extension, controller trust or
harness enrollment) is bound into the policy hash and wire generation, so a
change is a new setup decision: STOP is set, tombstones are dropped and all
client capabilities are revoked. Mutation tombstones survive restarts; a
released or expired client SID is random and never reissued, so its
request IDs cannot be replayed.

Sign-in: ordinary credential routes and fields stay denied (oauth/credential).
An optional owner "saved_login" binding (exact HTTPS origin, login pages and
form POST paths) enables only the argument-free chrome_saved_login tool: one
fixed trusted mouse press on the checked submit button of Chrome's own
autofilled form, behind the optional debugger permission granted in the owner
toolbar gesture. No credential, cookie or field value is read. The binding
digest joins the policy hash and wire generation only when present, so the
absent baseline is unchanged. The result is submitted/unverified, never
"authenticated", and a dispatched attempt is a durable per-tab tombstone.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import hmac
import http.client
import os
import queue
import re
import secrets
import subprocess
import sys
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from types import MappingProxyType

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from broker import ID_RE, MAX_BODY, MAX_REQUESTS, Broker, Handler, Policy, Reject, RelayServer, exact, ident, json_bytes, parse_json, saved_login_digest
from registration import TRANSPORTS, normalize_harness
from mcp import BLOCKER, MCP, operation as mcp_operation

SCHEMA = "chrome-relay-managed/1"
REGISTRY_SCHEMA = "chrome-relay-clients/1"
MAX_CLIENTS = 16
DEFAULT_CLIENT_TTL = 900
IDLE_RELEASE_S = 1200
REGISTER_RETRY_S = 2.0
CLIENT_SID_RE = re.compile(r"c-[a-f0-9]{32}\Z")
# "~" is outside ID_RE, so no request ID can ever embed a client capability.
CLIENT_TOKEN_RE = re.compile(r"crm~[a-f0-9]{64}\Z")
SECRET_RE = re.compile(r"[A-Za-z0-9._~+/-]{32,256}={0,2}\Z")
ENV_NAME_RE = re.compile(r"[A-Z][A-Z0-9_]{0,63}\Z")
# A native session hint must never be read from a credential-like variable.
ENV_SECRET_RE = re.compile(r"TOKEN|KEY|SECRET|PASS|COOKIE|AUTH|CRED|SESSION_FILE", re.I)
CONTROL_ROUTES = ("/managed/register", "/managed/renew", "/managed/release")
BROWSER_HEADERS = ("Origin", "Referer", "Sec-Fetch-Site", "Sec-Fetch-Mode", "Sec-Fetch-Dest", "Sec-Fetch-User")
CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
UNAVAILABLE_REASON_RE = re.compile(r"[a-z0-9_]{1,40}\Z")


@dataclass(frozen=True)
class ManagedGrant(Policy):
    grant_id: str = ""
    persistent: bool = False
    expires_ms: int | None = None
    harnesses: object = field(default_factory=dict)
    controller_key_sha256: str = ""

    def __post_init__(self):
        super().__post_init__()
        object.__setattr__(self, "harnesses", MappingProxyType({name: MappingProxyType(dict(item)) for name, item in dict(self.harnesses).items()}))

    def fingerprint(self):
        # Client sessions are deliberately absent: registering clients never
        # changes the grant. Every scope or trust input is present.
        data = ["managed/1", self.grant_id, self.profile_id, sorted(self.allowed_origins), sorted(self.allowed_actions),
                hashlib.sha256(self.extension_token.encode()).hexdigest(), self.extension_id, self.persistent,
                self.expires_ms, self.synthetic, self.controller_key_sha256,
                [[name, dict(self.harnesses[name])] for name in sorted(self.harnesses)]]
        if self.saved_login is not None:
            data.append(saved_login_digest(self.saved_login))
        return hashlib.sha256(json_bytes(data)).hexdigest()

    def wire_generation(self):
        # Mirrors policyGeneration() for schema-2 managed extension config.
        data = ["managed/1", self.grant_id, self.profile_id, self.extension_token, sorted(self.allowed_origins),
                sorted(self.allowed_actions), self.synthetic, None if self.persistent else self.expires_ms]
        if self.saved_login is not None:
            data.append(saved_login_digest(self.saved_login))
        return hashlib.sha256(json_bytes(data)).hexdigest()[:32]


@dataclass(frozen=True)
class ManagedConfig:
    grant: ManagedGrant
    broker_base: str
    port: int
    state_dir: Path
    controller_key: str = field(repr=False)
    client_ttl_s: int = DEFAULT_CLIENT_TTL


def _absolute(value):
    if not isinstance(value, str) or "\x00" in value or not os.path.isabs(value) or ".." in Path(value).parts:
        raise Reject("config")
    return Path(value)


def _broker_port(value):
    match = re.fullmatch(r"http://127\.0\.0\.1:([1-9][0-9]{0,4})/?", value) if isinstance(value, str) else None
    if not match or not 1 <= int(match.group(1)) <= 65535:
        raise Reject("config")
    return int(match.group(1))


def _expires_ms(value):
    try:
        if isinstance(value, str):
            stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if stamp.tzinfo is None:
                raise ValueError()
            delta = stamp - datetime(1970, 1, 1, tzinfo=timezone.utc)
            result = delta.days * 86400000 + delta.seconds * 1000 + delta.microseconds // 1000
        elif type(value) is int and value > 100000000000:
            result = value  # Integer JS milliseconds, as the extension stores it.
        else:
            raise ValueError()
    except (ValueError, OverflowError):
        raise Reject("config") from None
    if not 0 < result <= 9007199254740991:
        raise Reject("config")
    return result


def _harnesses(value):
    if not isinstance(value, dict) or not value:
        raise Reject("config")
    result = {}
    for name, item in value.items():
        key = normalize_harness(name)
        if key in result:
            raise Reject("config")
        exact(item, (), ("transport", "transports", "native_session_env"))
        if ("transport" in item) == ("transports" in item):
            raise Reject("config")
        if "transport" in item:
            if type(item["transport"]) is not str or item["transport"] not in TRANSPORTS:
                raise Reject("config")
            record = {"transport": item["transport"]}  # Preserve old grant hashes.
        else:
            values = item["transports"]
            if (type(values) is not list or not 1 <= len(values) <= len(TRANSPORTS)
                    or any(type(v) is not str or v not in TRANSPORTS for v in values)
                    or len(set(values)) != len(values)):
                raise Reject("config")
            record = {"transports": tuple(sorted(values))}
        if "native_session_env" in item:
            env = item["native_session_env"]
            if not isinstance(env, str) or not ENV_NAME_RE.fullmatch(env) or ENV_SECRET_RE.search(env):
                raise Reject("config")
            record["native_session_env"] = env
        result[key] = record
    return result


def _allowed_transports(enrolled):
    return (enrolled["transport"],) if "transport" in enrolled else enrolled["transports"]


def config_from_dict(value):
    exact(value, ("schema", "grant", "broker_base", "state_dir", "extension_token", "controller_key"), ("client_ttl_s",))
    if value["schema"] != SCHEMA:
        raise Reject("config")
    grant = value["grant"]
    exact(grant, ("grant_id", "profile_id", "allowed_origins", "allowed_actions", "extension_id", "harnesses"), ("persistent", "expires_at", "synthetic", "saved_login"))
    persistent = "persistent" in grant
    if persistent == ("expires_at" in grant) or (persistent and grant["persistent"] is not True):
        raise Reject("config")  # Exactly one of persistent=true or a real expiry.
    for secret in (value["extension_token"], value["controller_key"]):
        if not isinstance(secret, str) or not SECRET_RE.fullmatch(secret):
            raise Reject("config")
    ttl = value.get("client_ttl_s", DEFAULT_CLIENT_TTL)
    if type(ttl) is not int or not 60 <= ttl <= 86400:
        raise Reject("config")
    expires_ms = None if persistent else _expires_ms(grant["expires_at"])
    # Reuse the legacy validator for origins, actions, extension and secrets;
    # the controller key stands in as the one required (distinct) session.
    base = Policy.from_dict({"schema": 1, "profile_id": grant["profile_id"], "allowed_origins": grant["allowed_origins"],
                             "allowed_actions": grant["allowed_actions"], "sessions": {"controller": value["controller_key"]},
                             "extension_token": value["extension_token"], "extension_id": grant["extension_id"],
                             "expires_at": 1 if persistent else expires_ms, "synthetic": grant.get("synthetic", False),
                             **({"saved_login": grant["saved_login"]} if "saved_login" in grant else {})})
    managed = ManagedGrant(base.profile_id, base.allowed_origins, base.allowed_actions, {}, base.extension_token,
                           base.extension_id, float("inf") if persistent else expires_ms / 1000, base.synthetic,
                           saved_login=base.saved_login, grant_id=ident(grant["grant_id"]), persistent=persistent, expires_ms=expires_ms,
                           harnesses=_harnesses(grant["harnesses"]),
                           controller_key_sha256=hashlib.sha256(value["controller_key"].encode()).hexdigest())
    port = _broker_port(value["broker_base"])
    return ManagedConfig(managed, "http://127.0.0.1:%d" % port, port, _absolute(value["state_dir"]), value["controller_key"], ttl)


def load_config(path):
    try:
        with _absolute(path).open("rb") as stream:
            return config_from_dict(parse_json(stream.read(MAX_BODY + 1)))
    except (OSError, ValueError, TypeError, Reject):
        raise Reject("config") from None


def summary(config):
    """Non-secret description of a validated config; never tokens or keys."""
    grant = config.grant
    return {"status": "ok", "schema": SCHEMA, "grant_id": grant.grant_id, "profile_id": grant.profile_id,
            "generation": grant.wire_generation(), "persistent": grant.persistent,
            "expires_ms": grant.expires_ms, "allowed_origins": sorted(grant.allowed_origins),
            "allowed_actions": sorted(grant.allowed_actions), "synthetic": grant.synthetic,
            "broker_base": config.broker_base, "client_ttl_s": config.client_ttl_s,
            "harnesses": {name: (grant.harnesses[name]["transport"] if "transport" in grant.harnesses[name] else list(grant.harnesses[name]["transports"])) for name in sorted(grant.harnesses)},
            **({"saved_login": {"origin": grant.saved_login["origin"], "pages": list(grant.saved_login["pages"]), "posts": list(grant.saved_login["posts"])}} if grant.saved_login is not None else {})}


def extension_config(config):
    """Schema-2 extension config (holds the extension token: owner-private)."""
    grant = config.grant
    value = {"schema": 2, "mode": "managed", "grant_id": grant.grant_id, "profile_id": grant.profile_id,
             "extension_token": grant.extension_token, "broker_base": config.broker_base,
             "allowed_origins": sorted(grant.allowed_origins), "allowed_actions": sorted(grant.allowed_actions),
             "synthetic": grant.synthetic}
    if grant.saved_login is not None:
        value["saved_login"] = {"origin": grant.saved_login["origin"], "pages": list(grant.saved_login["pages"]), "posts": list(grant.saved_login["posts"])}
    if grant.persistent:
        value["persistent"] = True
    else:
        value["expires_at"] = grant.expires_ms
    return value


class ManagedBroker(Broker):
    """Broker whose sessions are controller-registered expiring clients."""

    def __init__(self, grant, state_dir, client_ttl_s=DEFAULT_CLIENT_TTL, request_timeout=10.0, poll_timeout=20.0, clock=time.time):
        if not isinstance(grant, ManagedGrant) or type(client_ttl_s) is not int or not 1 <= client_ttl_s <= 86400:
            raise Reject("policy")
        state_dir = Path(state_dir)
        self.clock = clock
        self.client_ttl_s = client_ttl_s
        self.clients = {}
        self.registry_path = state_dir / "clients.json"
        self.registry_failed = False
        self.registry_reset = False
        self._load_registry(grant)
        super().__init__(grant, state_dir / "state.jsonl", state_dir / "audit.jsonl", request_timeout, poll_timeout)
        with self.condition:
            if self.registry_failed:
                self.stop, self.authorized, self.technical_block, self.state_failed = True, False, True, True
            elif self.registry_reset:
                self.stop, self.authorized = True, False
                self._persist()
                self._save_registry()
            self._sweep_clients()

    # Registry -------------------------------------------------------------

    def _load_registry(self, grant):
        try:
            if not self.registry_path.exists():
                return
            with self.registry_path.open("rb") as stream:
                value = parse_json(stream.read(MAX_BODY + 1))
            exact(value, ("schema", "policy_hash", "clients"))
            if value["schema"] != REGISTRY_SCHEMA:
                raise Reject("state")
            if value["policy_hash"] != grant.fingerprint():
                # A grant/scope change revokes every client capability.
                self.registry_reset = True
                return
            clients = value["clients"]
            if not isinstance(clients, dict) or len(clients) > MAX_CLIENTS:
                raise Reject("state")
            for sid, record in clients.items():
                exact(record, ("token_sha256", "harness", "transport", "expires_at"), ("native_session_id", "native_session_source"))
                if not CLIENT_SID_RE.fullmatch(sid) or not isinstance(record["token_sha256"], str) or not re.fullmatch(r"[a-f0-9]{64}", record["token_sha256"]):
                    raise Reject("state")
                enrolled = grant.harnesses.get(record["harness"])
                if enrolled is None or record["transport"] not in _allowed_transports(enrolled) or type(record["expires_at"]) not in (int, float):
                    raise Reject("state")
                if ("native_session_id" in record) != ("native_session_source" in record):
                    raise Reject("state")
                if "native_session_id" in record:
                    ident(record["native_session_id"])
                    if record["native_session_source"] != "env:" + enrolled.get("native_session_env", ""):
                        raise Reject("state")
            self.clients = {sid: dict(record) for sid, record in clients.items()}
        except (OSError, ValueError, KeyError, TypeError, Reject):
            self.clients = {}
            self.registry_failed = True

    def _save_registry(self):
        if self.state_failed:
            return False
        value = {"schema": REGISTRY_SCHEMA, "policy_hash": self.policy_hash, "clients": self.clients}
        temporary = self.registry_path.with_name(self.registry_path.name + ".tmp")
        try:
            self.registry_path.parent.mkdir(parents=True, exist_ok=True)
            with temporary.open("wb") as stream:
                stream.write(json_bytes(value))
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.registry_path)
            return True
        except (OSError, ValueError):
            self.state_failed = True
            self._cancel("state_io")
            return False

    def _sweep_clients(self):
        now = self.clock()
        for sid in [sid for sid, record in self.clients.items() if now >= record["expires_at"]]:
            self._revoke(sid, "expired")

    def _revoke(self, sid, reason):
        if self.clients.pop(sid, None) is None:
            return
        for req in list(self.requests.values()):
            if req.sid == sid and req.result is None:
                self._finish(req, self._response(req, "unknown" if req.dispatched else "denied", reason, tab=req.tab))
        self._persist()  # Journal dispatched tombstones before forgetting the SID.
        self.refs = {key: refs for key, refs in self.refs.items() if key[0] != sid}
        self.owned.pop(sid, None)
        for key in [key for key in self.requests if key[0] == sid]:
            self.requests.pop(key)
        self.completed_observations = deque(req for req in self.completed_observations if req.sid != sid)
        self._save_registry()
        self._audit("client_" + ("expired" if reason == "expired" else "released"), "ok")

    # Broker session hooks -------------------------------------------------

    def _fresh_owned(self):
        return {sid: set() for sid in self.clients}

    def _session_known(self, sid):
        with self.lock:
            record = self.clients.get(sid)
            return record is not None and self.clock() < record["expires_at"]

    def _session_matches(self, sid, token):
        with self.lock:
            record = self.clients.get(sid) if isinstance(sid, str) else None
            digest = hashlib.sha256(token.encode()).hexdigest()
            if record is None or not hmac.compare_digest(digest, record["token_sha256"]):
                return False
            if self.clock() >= record["expires_at"]:
                self._revoke(sid, "expired")
                raise Reject("expired", 401)
            return True

    def _capability_tokens(self):
        return (self.policy.extension_token,)

    def _session_budget(self):
        return max(1, MAX_REQUESTS // MAX_CLIENTS)

    def _restore_state(self, saved, first):
        # Journal lines of revoked clients can never authenticate again.
        if isinstance(saved, dict):
            saved = dict(saved)
            if isinstance(saved.get("owned"), dict):
                saved["owned"] = {sid: ids for sid, ids in saved["owned"].items() if sid in self.clients}
            if isinstance(saved.get("requests"), list):
                saved["requests"] = [item for item in saved["requests"] if not isinstance(item, dict) or item.get("sid") in self.clients]
        return super()._restore_state(saved, first)

    # Controller operations ------------------------------------------------

    def extension_event(self, value):
        if not isinstance(value, dict) or value.get("type") != "managed_browser_restart":
            return super().extension_event(value)
        # Existing extension-only HTTP authentication protects this message.
        # Reconnect cannot grant authority or lift STOP.
        exact(value, ("type", "profile_id", "session_id", "tab_id", "reason", "generation"))
        if (value["profile_id"] != self.policy.profile_id or value["session_id"] != ""
                or type(value["tab_id"]) is not int or value["tab_id"] != 0
                or value["reason"] != "browser_restart" or value["generation"] != self.generation):
            raise Reject("identity", 409)
        with self.condition:
            if self.closed or self.state_failed:
                raise Reject("state_io", 503)
            if self._expire():
                raise Reject("expired", 403)
            self.epoch += 1
            self.technical_block = True
            self.refs.clear()
            for req in list(self.requests.values()):
                if req.result is not None:
                    continue
                if req.action == "chrome_attachment":
                    req.epoch = self.epoch
                else:
                    self._finish(req, self._response(req, "unknown" if req.dispatched else "denied", "technical_block", tab=req.tab))
            self.queue = type(self.queue)(req for req in self.queue if req.result is None and req.action == "chrome_attachment")
            self.manual_tab = 0
            self.owned = self._fresh_owned()
            self._audit("extension_browser_restart", "ok")
            self._persist()  # Durable tombstones and invalid IDs before ACK.
            if self.state_failed:
                raise Reject("state_io", 503)
            self.condition.notify_all()

    def register(self, value):
        exact(value, ("harness", "transport"), ("native_session_id", "native_session_source"))
        harness = normalize_harness(value["harness"])
        enrolled = self.policy.harnesses.get(harness)
        if enrolled is None:
            raise Reject("harness", 403)
        if value["transport"] not in _allowed_transports(enrolled):
            raise Reject("transport", 403)
        native = None
        if "native_session_id" in value or "native_session_source" in value:
            if "native_session_env" not in enrolled or value.get("native_session_source") != "env:" + enrolled["native_session_env"]:
                raise Reject("native_session")
            native = ident(value.get("native_session_id"))
        with self.condition:
            if self.closed or self.state_failed:
                raise Reject("state_io", 503)
            if self._expire():
                raise Reject("expired", 403)
            self._sweep_clients()
            if len(self.clients) >= MAX_CLIENTS:
                raise Reject("bound", 429)
            sid = "c-" + secrets.token_hex(16)
            token = "crm~" + secrets.token_hex(32)
            record = {"token_sha256": hashlib.sha256(token.encode()).hexdigest(), "harness": harness,
                      "transport": value["transport"], "expires_at": self.clock() + self.client_ttl_s}
            if native is not None:
                record["native_session_id"] = native
                record["native_session_source"] = value["native_session_source"]
            self.clients[sid] = record
            self.owned[sid] = set()
            if not self._save_registry():
                raise Reject("state_io", 503)
            self._audit("client_register", "ok")
        return {"session_id": sid, "token": token, "profile_id": self.policy.profile_id, "expires_in_s": self.client_ttl_s}

    def renew(self, sid):
        with self.condition:
            self._sweep_clients()
            record = self.clients.get(sid)
            if record is None or self.state_failed:
                raise Reject("expired", 401)
            record["expires_at"] = self.clock() + self.client_ttl_s
            if not self._save_registry():
                raise Reject("state_io", 503)
        return {"session_id": sid, "expires_in_s": self.client_ttl_s}

    def release(self, sid):
        with self.condition:
            self._revoke(sid, "cancelled")
        return {"released": True}


class ManagedHandler(Handler):
    def _control_guard(self):
        if len(self.headers.get_all("Host", [])) != 1 or self.headers.get("Host") != "127.0.0.1:" + str(self.server.server_address[1]):
            raise Reject("host", 403)
        if sum(len(key) + len(value) for key, value in self.headers.items()) > 8192:
            raise Reject("headers", 431)
        # The controller route is never reachable from a page or extension.
        if any(self.headers.get_all(name, []) for name in BROWSER_HEADERS):
            raise Reject("origin", 403)
        if self.headers.get_all("Transfer-Encoding", []):
            raise Reject("transfer_encoding")
        broker = self.server.broker
        if self.path != "/managed/register":
            return broker.authenticate(self.headers)  # Renew/release: own client capability only.
        authorization = self.headers.get("Authorization", "")
        if len(self.headers.get_all("Authorization", [])) != 1 or not authorization.startswith("Bearer ") or len(authorization) > 270:
            raise Reject("authorization", 401)
        digest = hashlib.sha256(authorization[7:].encode()).hexdigest()
        if not hmac.compare_digest(digest, broker.policy.controller_key_sha256):
            raise Reject("authorization", 401)
        return None

    def do_POST(self):
        if self.path not in CONTROL_ROUTES:
            return super().do_POST()
        self.body_read_started = False
        try:
            sid = self._control_guard()
            value = self._body()
            broker = self.server.broker
            if self.path == "/managed/register":
                result = broker.register(value)
            else:
                exact(value)
                result = broker.renew(sid) if self.path == "/managed/renew" else broker.release(sid)
            self._send(200, result)
        except Reject as error:
            if not self.body_read_started:
                self._discard_rejected_body()
            self._send(error.status, {"error": error.reason})
        except (OSError, ValueError, TypeError, RecursionError):
            self._send(400, {"error": "request_failed"})


def make_managed_server(broker, port=0):
    if not isinstance(broker, ManagedBroker) or type(port) is not int or not 0 <= port <= 65535:
        raise Reject("server")
    server = RelayServer(("127.0.0.1", port), ManagedHandler)
    server.broker = broker
    return server


class Controller:
    """Launcher-side client of the controller routes."""

    def __init__(self, config, timeout=10.0):
        self.config, self.timeout = config, timeout

    def _post(self, path, value, headers):
        connection = http.client.HTTPConnection("127.0.0.1", self.config.port, timeout=self.timeout)
        try:
            connection.request("POST", path, json_bytes(value), {"Content-Type": "application/json", **headers})
            response = connection.getresponse()
            return response.status, parse_json(response.read(MAX_BODY + 1))
        except (OSError, http.client.HTTPException, ValueError, Reject):
            return 0, {}
        finally:
            connection.close()

    def register(self, harness, transport, native=None):
        value = {"harness": harness, "transport": transport}
        if native is not None:
            value["native_session_id"], value["native_session_source"] = native
        status, result = self._post("/managed/register", value, {"Authorization": "Bearer " + self.config.controller_key})
        if status != 200 or not isinstance(result, dict) or not CLIENT_SID_RE.fullmatch(str(result.get("session_id"))) or not CLIENT_TOKEN_RE.fullmatch(str(result.get("token"))):
            raise Reject(result.get("error") if isinstance(result, dict) and isinstance(result.get("error"), str) and ID_RE.fullmatch(result["error"]) else "register")
        return result["session_id"], result["token"]

    def renew(self, sid, token):
        return self._post("/managed/renew", {}, {"Authorization": "Bearer " + token, "X-Session-Id": sid})[0] == 200

    def release(self, sid, token):
        return self._post("/managed/release", {}, {"Authorization": "Bearer " + token, "X-Session-Id": sid})[0] == 200


def native_session(enrolled, environ):
    """(value, source) only when the enrolled variable really supplies one."""
    name = enrolled.get("native_session_env")
    value = environ.get(name) if name else None
    if not isinstance(value, str) or not ID_RE.fullmatch(value):
        return None
    return value, "env:" + name


def child_environment(config, sid, token, environ):
    # Minimal environment: no inherited CHROME_RELAY_* overrides, no owner
    # credentials; only this process's own client capability.
    env = {key: environ[key] for key in ("SYSTEMROOT", "SystemRoot") if key in environ}
    env.update({"CHROME_RELAY_BROKER": config.broker_base, "CHROME_RELAY_SESSION_ID": sid, "CHROME_RELAY_TOKEN": token,
                "PYTHONDONTWRITEBYTECODE": "1"})
    return env


def unavailable_environment(environ, reason):
    """Minimal child environment for a visible, fail-closed MCP server."""
    safe_reason = reason if isinstance(reason, str) and UNAVAILABLE_REASON_RE.fullmatch(reason) else "invalid_reason"
    env = {key: environ[key] for key in ("SYSTEMROOT", "SystemRoot") if key in environ}
    env.update({"PYTHONDONTWRITEBYTECODE": "1", "CHROME_RELAY_UNAVAILABLE": safe_reason})
    return env


def run_unavailable_mcp(environ=None, reason="client_config", popen=subprocess.Popen):
    environ = os.environ if environ is None else environ
    child = popen([sys.executable, "-B", str(HERE / "mcp.py")], env=unavailable_environment(environ, reason),
                  stdin=None, stdout=None, stderr=subprocess.DEVNULL, close_fds=True,
                  shell=False,
                  creationflags=CREATE_NO_WINDOW if os.name == "nt" else 0)
    return child.wait()


def _unavailable_cli_result(reason):
    safe_reason = reason if isinstance(reason, str) and UNAVAILABLE_REASON_RE.fullmatch(reason) else "invalid_reason"
    return {"status": "denied", "data": {"reason": "relay_unavailable", "blocker": BLOCKER, "detail": safe_reason}}


def run_unavailable_cli(stdin, stdout, reason):
    """Emit one stable blocker result for each input line, then return 3."""
    response = json_bytes(_unavailable_cli_result(reason)) + b"\n"
    while True:
        line = stdin.readline(MAX_BODY + 2)
        if not line:
            return 3
        # Drain an overlong logical line so it still gets exactly one result.
        while len(line) > MAX_BODY + 1 and not line.endswith(b"\n"):
            line = stdin.readline(MAX_BODY + 2)
            if not line:
                break
        stdout.write(response)
        stdout.flush()


def _enrolled(config, harness, transport):
    name = normalize_harness(harness)
    enrolled = config.grant.harnesses.get(name)
    if enrolled is None:
        raise Reject("harness")
    if transport not in _allowed_transports(enrolled):
        raise Reject("transport")  # A harness without MCP uses `managed.py cli`.
    return name, enrolled


class Lease:
    """Renews one client capability while its owner is alive; releases it after."""

    def __init__(self, controller, sid, token, interval):
        self.controller, self.sid, self.token, self.interval = controller, sid, token, interval
        self.done = threading.Event()
        self.thread = threading.Thread(target=self._run, daemon=True)

    def _run(self):
        while not self.done.wait(self.interval):
            if not self.controller.renew(self.sid, self.token):
                return  # Expired/revoked: calls now deny honestly; never re-register silently.

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *_):
        self.done.set()
        self.thread.join(5)
        self.controller.release(self.sid, self.token)


def _write_mcp_output(stdout, lock, data):
    with lock:
        stdout.write(data)
        stdout.flush()


def _tool_reply(rpc_id, response):
    return {"jsonrpc": "2.0", "id": rpc_id,
            "result": {"content": [{"type": "text", "text": json_bytes(response).decode("utf-8")}],
                       "isError": response["status"] != "ok"}}


def _blocker_detail(reply, detail):
    """Change only the local MCP unavailable detail; never echo a raw error."""
    try:
        text_value = reply["result"]["content"][0]["text"]
        payload = parse_json(text_value.encode("utf-8"))
        payload["data"]["detail"] = detail
        reply["result"]["content"][0]["text"] = json_bytes(payload).decode("utf-8")
    except (KeyError, IndexError, TypeError, ValueError, Reject):
        pass
    return reply


def _safe_child_line(line, token, controller_key):
    """Keep accidental capability echoes out of MCP stdout."""
    if token.encode() not in line and controller_key.encode() not in line:
        return line
    try:
        value = parse_json(line.rstrip(b"\r\n"))
        rpc_id = value.get("id") if isinstance(value, dict) and type(value.get("id")) in (int, str) else None
        return json_bytes(MCP.error(rpc_id, -32603, "Internal error")) + b"\n"
    except (AttributeError, TypeError, ValueError, Reject):
        return b""


def _stop_mcp_child(active):
    """Close the child transport, wait briefly, then terminate an owned child."""
    child = active["child"]
    with active["lock"]:
        active["expected_exit"] = True
    try:
        child.stdin.close()
    except (AttributeError, OSError, ValueError):
        pass
    try:
        child.wait(timeout=5)
    except TypeError:  # Small in-process test doubles may expose only wait().
        child.wait()
    except subprocess.TimeoutExpired:
        try:
            child.terminate()
        except (AttributeError, OSError):
            pass
        try:
            child.wait(timeout=5)
        except (AttributeError, OSError, subprocess.TimeoutExpired):
            pass
    except (OSError, ValueError):
        pass
    try:
        child.stdout.close()
    except (AttributeError, OSError, ValueError):
        pass
    pump = active.get("pump")
    if pump is not None and pump is not threading.current_thread():
        pump.join(5)
    active["lease"].__exit__(None, None, None)


def _safe_register_reason(error):
    reason = "broker_unreachable_or_register" if error.reason == "register" else error.reason
    return reason if isinstance(reason, str) and UNAVAILABLE_REASON_RE.fullmatch(reason) else "broker_unreachable_or_register"


def run_mcp(config, harness, environ=None, popen=subprocess.Popen, controller=None, stdin=None, stdout=None,
            idle_release_s=IDLE_RELEASE_S, clock=time.monotonic):
    """Serve MCP locally until a valid call needs a fresh managed browser slot.

    Initialization and tools/list do not consume a controller client slot. A
    child session gets a new SID for each activation and is released after the
    idle window or EOF; tabs from a released SID no longer belong to its agent.
    """
    environ = os.environ if environ is None else environ
    if type(idle_release_s) not in (int, float) or idle_release_s < 0:
        raise ValueError("idle_release_s")
    try:
        name, enrolled = _enrolled(config, harness, "mcp")
    except Reject as error:
        reason = error.reason
        return run_unavailable_mcp(environ, reason, popen=popen)
    controller = controller or Controller(config)
    stdin = sys.stdin.buffer if stdin is None else stdin
    stdout = sys.stdout.buffer if stdout is None else stdout
    output_lock = threading.Lock()
    events = queue.Queue()
    local = MCP(None, "broker_unreachable_or_register")
    initialize_params = None
    active = None
    last_register_attempt = None
    last_register_failure = "broker_unreachable_or_register"

    def input_reader():
        while True:
            try:
                line = stdin.readline(MAX_BODY + 2)
            except (OSError, ValueError, queue.Empty):
                line = b""
            if not line:
                events.put(("stdin_eof", None))
                return
            events.put(("stdin_line", line))

    reader = threading.Thread(target=input_reader, name="chrome-relay-mcp-stdin", daemon=True)
    reader.start()

    def child_pump(session, startup_queue):
        child = session["child"]
        try:
            first = child.stdout.readline(MAX_BODY + 2)
        except (OSError, ValueError):
            first = b""
        startup_queue.put(first)
        try:
            while first:
                line = child.stdout.readline(MAX_BODY + 2)
                if not line:
                    break
                safe = _safe_child_line(line, session["token"], config.controller_key)
                if safe:
                    _write_mcp_output(stdout, output_lock, safe)
                try:
                    value = parse_json(line.rstrip(b"\r\n"))
                    rpc_id = value.get("id") if isinstance(value, dict) and type(value.get("id")) in (int, str) else None
                except (AttributeError, TypeError, ValueError, Reject):
                    rpc_id = None
                if rpc_id is not None:
                    with session["lock"]:
                        count = session["pending"].get(rpc_id, 0)
                        if count <= 1:
                            session["pending"].pop(rpc_id, None)
                        else:
                            session["pending"][rpc_id] = count - 1
                    events.put(("child_activity", session))
                first = line
        except (OSError, ValueError):
            pass
        finally:
            with session["lock"]:
                session["exiting"] = True
                unexpected = not session["expected_exit"]
                outstanding = list(session["pending"].items()) if unexpected else []
                session["pending"].clear()
            if unexpected:
                for rpc_id, count in outstanding:
                    for _ in range(count):
                        response = {"status": "unknown", "data": {"reason": "transport"}}
                        try:
                            _write_mcp_output(stdout, output_lock, json_bytes(_tool_reply(rpc_id, response)) + b"\n")
                        except (OSError, ValueError):
                            break
            events.put(("child_exit", session))

    def start_child_for_call(line, value, reply):
        nonlocal active, last_register_attempt, last_register_failure
        now = clock()
        if last_register_attempt is not None and now - last_register_attempt < REGISTER_RETRY_S:
            _write_mcp_output(stdout, output_lock, json_bytes(_blocker_detail(reply, last_register_failure)) + b"\n")
            return
        last_register_attempt = now
        try:
            sid, token = controller.register(name, "mcp", native_session(enrolled, environ))
        except Reject as error:
            last_register_failure = _safe_register_reason(error)
            _write_mcp_output(stdout, output_lock, json_bytes(_blocker_detail(reply, last_register_failure)) + b"\n")
            return
        last_register_attempt = None
        lease = Lease(controller, sid, token, max(5, config.client_ttl_s // 3))
        lease.__enter__()
        try:
            child = popen([sys.executable, "-B", str(HERE / "mcp.py")],
                          env=child_environment(config, sid, token, environ), stdin=subprocess.PIPE,
                          stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, close_fds=True, shell=False,
                          creationflags=CREATE_NO_WINDOW if os.name == "nt" else 0)
        except (OSError, ValueError, TypeError):
            lease.__exit__(None, None, None)
            _write_mcp_output(stdout, output_lock, json_bytes(_blocker_detail(reply, "child_start")) + b"\n")
            return
        session = {"child": child, "lease": lease, "sid": sid, "token": token,
                   "pending": {}, "lock": threading.Lock(), "last_call": clock(),
                   "expected_exit": False, "exiting": False}
        startup_queue = queue.Queue(maxsize=1)
        pump = threading.Thread(target=child_pump, args=(session, startup_queue),
                                name="chrome-relay-mcp-stdout", daemon=True)
        session["pump"] = pump
        active = session
        pump.start()
        init_id = "relay-init-" + secrets.token_hex(8)
        try:
            init = {"jsonrpc": "2.0", "id": init_id, "method": "initialize", "params": copy.deepcopy(initialize_params)}
            child.stdin.write(json_bytes(init) + b"\n")
            child.stdin.write(json_bytes({"jsonrpc": "2.0", "method": "notifications/initialized"}) + b"\n")
            child.stdin.flush()
            init_line = startup_queue.get(timeout=10)
            init_value = parse_json(init_line.rstrip(b"\r\n"))
            if (not isinstance(init_value, dict) or init_value.get("jsonrpc") != "2.0"
                    or init_value.get("id") != init_id or "result" not in init_value):
                raise Reject("child_start")
        except (OSError, ValueError, TypeError, queue.Empty, Reject):
            _stop_mcp_child(session)
            active = None
            _write_mcp_output(stdout, output_lock, json_bytes(_blocker_detail(reply, "child_start")) + b"\n")
            return
        if session["exiting"]:
            # The child completed initialization but died before receiving the
            # call. Treat its outcome as unknown; a mutation is never replayed.
            _write_mcp_output(stdout, output_lock,
                              json_bytes(_tool_reply(value["id"], {"status": "unknown", "data": {"reason": "transport"}})) + b"\n")
            _stop_mcp_child(session)
            active = None
            return
        state = _forward_to_child(session, line, value)
        if state == "exited":
            _write_mcp_output(stdout, output_lock,
                              json_bytes(_tool_reply(value["id"], {"status": "unknown", "data": {"reason": "transport"}})) + b"\n")
            _stop_mcp_child(session)
            active = None

    def _forward_to_child(session, line, value):
        method = value.get("method") if isinstance(value, dict) else None
        rpc_id = value.get("id") if isinstance(value, dict) and type(value.get("id")) in (int, str) else None
        is_call = method == "tools/call" and rpc_id is not None
        if is_call:
            session["last_call"] = clock()
            with session["lock"]:
                if session["exiting"]:
                    return "exited"
                session["pending"][rpc_id] = session["pending"].get(rpc_id, 0) + 1
        try:
            session["child"].stdin.write(line)
            session["child"].stdin.flush()
        except (BrokenPipeError, OSError, ValueError):
            return "pending" if is_call else "failed"
        return "sent"

    def handle_local(line):
        nonlocal initialize_params
        if len(line) > MAX_BODY + 1 or (len(line) > MAX_BODY and not line.endswith(b"\n")):
            _write_mcp_output(stdout, output_lock,
                              json_bytes(MCP.error(None, -32700, "Message exceeds limit")) + b"\n")
            return 1
        try:
            value = parse_json(line.rstrip(b"\r\n"))
        except Reject:
            reply = MCP.error(None, -32700, "Parse error")
            _write_mcp_output(stdout, output_lock, json_bytes(reply) + b"\n")
            return 0
        reply = local.handle(value)
        if (isinstance(value, dict) and value.get("method") == "initialize"
                and isinstance(reply, dict) and "result" in reply):
            initialize_params = copy.deepcopy(value.get("params", {}))
        if (isinstance(value, dict) and value.get("method") == "tools/call" and "id" in value
                and isinstance(reply, dict) and "result" in reply):
            params = value.get("params", {})
            try:
                exact(params, ("name",), ("arguments", "_meta"))
                if "_meta" in params and not isinstance(params["_meta"], dict):
                    raise Reject("params")
                mcp_operation(params["name"], params.get("arguments", {}))
            except (Reject, ValueError, TypeError, UnicodeError):
                pass  # MCP.handle already returned the local -32602 response.
            else:
                start_child_for_call(line, value, reply)
                return 0
        if reply is not None:
            _write_mcp_output(stdout, output_lock, json_bytes(reply) + b"\n")
        return 0

    while True:
        if active is not None:
            with active["lock"]:
                exiting = active["exiting"]
                pending = sum(active["pending"].values())
                last_call = active["last_call"]
            if exiting:
                _stop_mcp_child(active)
                active = None
                continue
            if pending == 0 and clock() - last_call >= idle_release_s:
                _stop_mcp_child(active)
                active = None
                continue
            timeout = max(0.0, last_call + idle_release_s - clock()) if pending == 0 else None
        else:
            timeout = None
        try:
            kind, payload = events.get(timeout=timeout)
        except queue.Empty:
            continue
        if kind == "child_exit":
            if active is payload:
                _stop_mcp_child(active)
                active = None
            continue
        if kind == "child_activity":
            continue
        if kind == "stdin_eof":
            if active is not None:
                _stop_mcp_child(active)
                active = None
            return 0
        if kind != "stdin_line":
            continue
        line = payload
        if active is not None:
            with active["lock"]:
                exiting = active["exiting"]
            if not exiting:
                try:
                    value = parse_json(line.rstrip(b"\r\n"))
                except Reject:
                    value = None
                forward_state = _forward_to_child(active, line, value if isinstance(value, dict) else {})
                if forward_state == "exited":
                    _stop_mcp_child(active)
                    active = None
                    if isinstance(value, dict) and value.get("method") == "tools/call" and type(value.get("id")) in (int, str):
                        response = {"status": "unknown", "data": {"reason": "transport"}}
                        _write_mcp_output(stdout, output_lock, json_bytes(_tool_reply(value["id"], response)) + b"\n")
                    elif handle_local(line):
                        return 1
                continue
            _stop_mcp_child(active)
            active = None
        if handle_local(line):
            if active is not None:
                _stop_mcp_child(active)
                active = None
            return 1


def run_cli(config, harness, stdin, stdout, environ=None, controller=None):
    """Semantic fallback: one JSON line {name, arguments} in, one result out."""
    from mcp import Client
    environ = os.environ if environ is None else environ
    try:
        name, enrolled = _enrolled(config, harness, "cli")
        controller = controller or Controller(config)
        sid, token = controller.register(name, "cli", native_session(enrolled, environ))
    except Reject as error:
        reason = "broker_unreachable_or_register" if error.reason == "register" else error.reason
        return run_unavailable_cli(stdin, stdout, reason)
    client = Client(config.broker_base, sid, token)
    with Lease(controller, sid, token, max(5, config.client_ttl_s // 3)):
        while True:
            line = stdin.readline(MAX_BODY + 2)
            if not line:
                return 0
            try:
                value = parse_json(line.rstrip(b"\r\n"))
                exact(value, ("name",), ("arguments",))
                reply = client.call(value["name"], value.get("arguments", {}))
            except (Reject, ValueError, TypeError):
                reply = {"status": "denied", "data": {"reason": "invalid"}}
            stdout.write(json_bytes(reply) + b"\n")
            stdout.flush()


def serve_broker(config):
    broker = ManagedBroker(config.grant, config.state_dir, config.client_ttl_s)
    server = make_managed_server(broker, config.port)
    try:
        server.serve_forever(poll_interval=0.2)
    finally:
        server.server_close()
        broker.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description="Managed Chrome relay controller; owner setup only", allow_abbrev=False)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("validate", "broker"):
        commands.add_parser(name, allow_abbrev=False).add_argument("--config", required=True)
    for name in ("mcp", "cli"):
        command = commands.add_parser(name, allow_abbrev=False)
        command.add_argument("--config", required=True)
        command.add_argument("--harness", required=True)
    args = parser.parse_args(argv)
    try:
        config = load_config(args.config)
    except Reject:
        if args.command == "mcp":
            return run_unavailable_mcp(os.environ, "config")
        if args.command == "cli":
            return run_unavailable_cli(sys.stdin.buffer, sys.stdout.buffer, "config")
        if args.command == "validate":
            sys.stdout.buffer.write(json_bytes({"status": "error", "reason": "config"}) + b"\n")
        return 1
    try:
        if args.command == "validate":
            sys.stdout.buffer.write(json_bytes(summary(config)) + b"\n")
            return 0
        if args.command == "broker":
            serve_broker(config)
            return 0
        if args.command == "mcp":
            return run_mcp(config, args.harness)
        return run_cli(config, args.harness, sys.stdin.buffer, sys.stdout.buffer)
    except Reject:
        return 1
    except KeyboardInterrupt:
        return 0
    except (OSError, ValueError, TypeError):
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
