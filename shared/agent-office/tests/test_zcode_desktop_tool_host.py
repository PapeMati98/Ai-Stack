"""Synthetic tests for the ZCode Desktop external-CU tool host.

These tests inject an Office client and synthetic CU receipts. They do not
start ZCode Desktop, do not invoke official CU tools, and they are not a
Desktop readiness UAT.
"""

from __future__ import annotations

import ast
import hashlib
import json
import sys
import threading
import time
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
if str(PACKAGE_ROOT) not in sys.path:
    sys.path.insert(0, str(PACKAGE_ROOT))

from coordfs.desktop_adapters import (  # noqa: E402
    MAX_LEASE_TTL_S,
    USER_IDLE_REQUIRED_S,
    classify_observation,
)
from coordfs.errors import CoordError  # noqa: E402
from coordfs.leases import lease_digest  # noqa: E402
from coordfs.office_remote import (  # noqa: E402
    ALLOWED_OPS as REMOTE_ALLOWED_OPS,
    OfficeRemoteClient,
    OfficeRemoteConfig,
)
from coordfs.zcode_desktop_tool_host import (  # noqa: E402
    DRIVER,
    OFFICIAL_CU_TOOL,
    TargetRegistry,
    ZCodeDesktopTarget,
    ZCodeDesktopToolHost,
    map_cu_receipt,
)

SESSION = "sess_c68ed66d-0fe5-4746-82b3-48fe201bd47d"
OTHER = "sess_99999999-8888-4777-8666-555555555555"
CALL = "abcdefab-cdef-4abc-8def-1234567890ab"
TURN = "abcdefab-cdef-4abc-8def-1234567890ac"
DISPATCH = "a" * 32
OFFICE_MESSAGE = "c" * 32
BODY = "OFFICE-BODY-DO-NOT-ROUTE " + OTHER
AGENT = "zcode-ailab-" + SESSION
RELAY = "codex-ailab-01a0ac55-48ac-7742-9b18-8fa624150d4b"
BINDING = "zcode-desktop-target"
SLUG = "synthetic-zcode-desktop"
WORKSPACE = "D:\\synthetic\\zcode-desktop-target"
NOW = 1_800_000_000.0


class FakeOffice:
    def __init__(self) -> None:
        self.calls: list[dict] = []
        self.stop_active = False
        self.broken_stop = False
        self.rows: list[dict] = []
        self.land_begin = True
        self.degrade_begin = False
        self.lease_ok = True
        self.lease_foreign = False
        self.lease_wrong_actor = False
        self.lease_digest = hashlib.sha256(b"synthetic-lease").hexdigest()
        self.receipts: list[dict] = []
        self.observes: list[dict] = []
        self.lease_claims: list[dict] = []
        self.lease_releases: list[dict] = []
        self.unsubmitted: list[dict] = []

    def request(self, op, identity, args, timeout_s=None):
        self.calls.append(
            {"op": op, "identity": dict(identity), "args": json.loads(json.dumps(args))}
        )
        if op == "wake-stop-status":
            if self.broken_stop:
                return {"status": "degraded", "reason": "timeout"}
            return {
                "status": "ok",
                "code": "wake_stop_status",
                "stop_active": self.stop_active,
                "since": None,
            }
        if op == "wake-relay-pending":
            rows = [
                dict(row)
                for row in self.rows
                if row["status"] == "pending" and row["binding_id"] == args.get("binding_id")
            ]
            return {
                "status": "ok",
                "code": "wake_relay_pending",
                "count": len(rows),
                "dispatches": rows,
            }
        if op == "wake-desktop-input-claim":
            self.lease_claims.append(dict(args))
            if not self.lease_ok:
                return {"status": "warning", "code": "held"}
            holder = RELAY if not self.lease_foreign else "zcode-ailab-" + OTHER
            binding = BINDING if not self.lease_foreign else "other-binding"
            actor = "codex-ailab-ffffffff-ffff-4fff-8fff-ffffffffffff" if self.lease_wrong_actor else holder
            lease = {
                "resource_id": "data:desktop-input",
                "holder_agent": holder,
                "acquired_at": "2030-01-01T00:00:00Z",
                "ttl_s": MAX_LEASE_TTL_S,
                "file_hash": "",
                "meta": {"binding_id": binding, "actor": actor},
            }
            return {
                "status": "ok",
                "code": "claimed",
                "lease": lease,
                "lease_digest": self.lease_digest,
            }
        if op == "wake-desktop-input-release":
            self.lease_releases.append(dict(args))
            return {"status": "ok", "code": "released"}
        if op == "wake-relay-claim":
            self._set(args["dispatch_id"], "claimed")
            return {
                "status": "ok",
                "code": "wake_relay_claimed",
                "dispatch": {"dispatch_id": args["dispatch_id"], "status": "claimed"},
            }
        if op == "wake-relay-native-begin":
            if self.land_begin:
                self._set(args["dispatch_id"], "delivery_unknown")
            if self.degrade_begin:
                return {"status": "degraded", "reason": "timeout"}
            return {
                "status": "ok",
                "code": "wake_native_submission_fenced",
                "dispatch": {
                    "dispatch_id": args["dispatch_id"],
                    "status": "delivery_unknown",
                    "text": BODY,
                },
            }
        if op == "wake-relay-native-unsubmitted":
            self.unsubmitted.append(dict(args))
            self._set(args["dispatch_id"], "pending")
            return {
                "status": "ok",
                "code": "wake_native_not_submitted",
                "dispatch": {"dispatch_id": args["dispatch_id"], "status": "pending"},
            }
        if op == "wake-relay-receipt":
            self.receipts.append(dict(args))
            return {"status": "ok", "code": "wake_relay_receipt", "idempotent": False}
        if op == "wake-relay-observe":
            self.observes.append(dict(args))
            return {"status": "ok", "code": "wake_relay_observed", "idempotent": False}
        raise AssertionError(op)

    def _set(self, dispatch_id: str, status: str) -> None:
        for row in self.rows:
            if row["dispatch_id"] == dispatch_id:
                row["status"] = status


def target(**overrides) -> ZCodeDesktopTarget:
    value = dict(
        binding_id=BINDING,
        native_session_id=SESSION,
        office_agent=AGENT,
        project_slug=SLUG,
        workspace=WORKSPACE,
        relay_identity=RELAY,
        authorized=True,
    )
    value.update(overrides)
    return ZCodeDesktopTarget(**value)


def registry(*items: ZCodeDesktopTarget) -> TargetRegistry:
    return TargetRegistry(items or (target(),))


def row(dispatch_id: str = DISPATCH, created_at: str = "2030-01-01T00:00:00+00:00", **overrides) -> dict:
    value = {
        "dispatch_id": dispatch_id,
        "binding_id": BINDING,
        "target_local_id": SESSION,
        "text": BODY,
        "status": "pending",
        "trigger_ref": OFFICE_MESSAGE,
        "created_at": created_at,
        "tool": "mcp__evil",
        "thread_id": OTHER,
    }
    value.update(overrides)
    return value


def observation(**overrides):
    value = {
        "schema": "zcode-desktop-app-observation/v1",
        "source": "zcode_desktop_host",
        "sampled_at": NOW,
        "observed_at": NOW,
        "user_idle_s": USER_IDLE_REQUIRED_S,
        "app_activity": "idle",
        "draft": False,
        "lock": False,
        "quota": "ok",
        "cu_lease_owner": None,
        "cu_lease_binding_id": None,
        "native_session_id": SESSION,
        "project_slug": SLUG,
        "workspace": WORKSPACE,
    }
    value.update(overrides)
    return value


def host(office: FakeOffice | None = None, **target_overrides):
    client = office or FakeOffice()
    if not client.rows:
        client.rows.append(row())
    built = ZCodeDesktopToolHost(client, registry(target(**target_overrides)), now=lambda: NOW)
    return built, client


def ops(client: FakeOffice) -> list[str]:
    return [call["op"] for call in client.calls]


def assert_relay_identity(client: FakeOffice) -> None:
    blob = json.dumps(client.calls)
    assert BODY not in json.dumps({k: v for c in client.calls for k, v in c["args"].items() if k != "text"})
    for call in client.calls:
        assert call["identity"] == {"agent_id": RELAY}
        if call["op"].startswith("wake-relay-"):
            assert call["args"]["driver"] == DRIVER
        assert "actor" not in call["args"]
        assert "agent_id" not in call["args"]
        assert "relay_agent" not in call["args"] or call["args"]["relay_agent"] == RELAY


def test_policy_threshold_is_fixed_and_immutable() -> None:
    assert USER_IDLE_REQUIRED_S == 120
    assert classify_observation(
        observation(user_idle_s=119),
        native_session_id=SESSION,
        project_slug=SLUG,
        workspace=WORKSPACE,
        binding_id=BINDING,
        now=NOW,
    )["kind"] == "idle_below_threshold"
    assert classify_observation(
        observation(user_idle_s=120),
        native_session_id=SESSION,
        project_slug=SLUG,
        workspace=WORKSPACE,
        binding_id=BINDING,
        now=NOW,
    )["kind"] == "idle"
    with pytest.raises(FrozenInstanceError):
        target().binding_id = "other"  # type: ignore[misc]
    book = registry(target())
    with pytest.raises(TypeError):
        book.targets[BINDING] = target()  # type: ignore[index]


def test_native_target_uses_real_zcode_session_and_cross_harness_codex_relay() -> None:
    configured = target()
    assert configured.native_session_id == "sess_c68ed66d-0fe5-4746-82b3-48fe201bd47d"
    assert configured.office_agent == "zcode-ailab-sess_c68ed66d-0fe5-4746-82b3-48fe201bd47d"
    assert configured.relay_identity == "codex-ailab-01a0ac55-48ac-7742-9b18-8fa624150d4b"
    assert row()["target_local_id"] == configured.native_session_id


@pytest.mark.parametrize(
    "native_session_id",
    [
        "c68ed66d-0fe5-4746-82b3-48fe201bd47d",  # missing sess_ prefix
        "sess_c68ed66d-0fe5-4746-82b3",  # truncated
        "sess_sess_c68ed66d-0fe5-4746-82b3-48fe201bd47d",  # double prefix
    ],
)
def test_native_session_rejects_naked_truncated_and_double_prefixed_ids(native_session_id: str) -> None:
    with pytest.raises(CoordError):
        target(native_session_id=native_session_id)


def test_rejects_absent_unknown_bool_fractional_stale_future_below_threshold() -> None:
    built, client = host()
    cases = (
        (dict(user_idle_s=None), "idle_observation_invalid"),
        (dict(user_idle_s=True), "idle_observation_invalid"),
        (dict(user_idle_s=False), "idle_observation_invalid"),
        (dict(user_idle_s=120.5), "idle_observation_invalid"),
        (dict(user_idle_s=120.0), "idle_observation_invalid"),
        (dict(user_idle_s="unknown"), "idle_observation_invalid"),
        (dict(user_idle_s=119), "idle_below_threshold"),
        (dict(user_idle_s=0), "idle_below_threshold"),
        (dict(sampled_at=NOW - 61), "stale_observation"),
        (dict(observed_at=NOW - 61), "stale_observation"),
        (dict(sampled_at=NOW + 30), "future_observation"),
        (dict(observed_at=NOW + 30), "future_observation"),
    )
    absent = dict(observation())
    absent.pop("user_idle_s")
    refused_absent = built.prepare(BINDING, absent)
    assert refused_absent["code"] == "idle_observation_invalid"
    assert "wake-relay-claim" not in ops(client)
    assert "wake-desktop-input-claim" not in ops(client)
    for overrides, code in cases:
        result = built.prepare(BINDING, observation(**overrides))
        assert result["code"] == code, overrides
        assert result["submit"] is None
        assert result["started"] is False
        assert "wake-relay-claim" not in ops(client)
        assert "wake-relay-native-begin" not in ops(client)
        assert "wake-desktop-input-claim" not in ops(client)


def test_fail_closed_before_claim_stop_busy_draft_lock_quota_unknown_source_mismatch() -> None:
    built, client = host()
    client.stop_active = True
    assert built.prepare(BINDING, observation())["code"] == "global_stop"
    assert "wake-relay-claim" not in ops(client)

    client = FakeOffice()
    client.broken_stop = True
    built, _ = host(client)
    assert built.prepare(BINDING, observation())["code"] == "stop_unavailable"
    assert "wake-relay-claim" not in ops(client)

    for overrides, code in (
        (dict(app_activity="busy"), "target_busy"),
        (dict(app_activity="active"), "target_busy"),
        (dict(draft=True), "target_draft"),
        (dict(lock=True), "target_lock"),
        (dict(quota="exhausted"), "target_quota"),
        (dict(app_activity="unknown"), "target_unknown"),
        (dict(quota="unknown"), "target_unknown"),
        (dict(source="other_source"), "source_mismatch"),
        (dict(native_session_id=OTHER), "target_mismatch"),
        (dict(project_slug="other-project"), "target_mismatch"),
        (dict(workspace=WORKSPACE + "\\other"), "target_mismatch"),
        (dict(cu_lease_owner=RELAY, cu_lease_binding_id="other-binding"), "cu_lease_foreign"),
    ):
        client = FakeOffice()
        built, _ = host(client)
        result = built.prepare(BINDING, observation(**overrides))
        assert result["code"] == code, overrides
        assert result["submit"] is None
        assert "wake-relay-claim" not in ops(client)
        assert "wake-desktop-input-claim" not in ops(client)


def test_valid_idle_produces_one_intent_after_lease_claim_and_begin() -> None:
    built, client = host()
    ready = built.prepare(BINDING, observation())
    assert ready["code"] == "submit_once"
    assert ready["status"] == "ready"
    assert ready["submit"]["tool"] == OFFICIAL_CU_TOOL
    assert ready["submit"]["once"] is True
    assert set(ready["submit"]["arguments"]) == {"session_id", "project_slug", "workspace", "text"}
    assert ready["submit"]["arguments"]["session_id"] == SESSION
    assert ready["submit"]["arguments"]["project_slug"] == SLUG
    assert ready["submit"]["arguments"]["workspace"] == WORKSPACE
    assert ready["submit"]["arguments"]["text"] == BODY
    assert ready["submit"]["binding_id"] == BINDING
    assert ready["submit"]["dispatch_id"] == DISPATCH
    assert ready["submit"]["relay_identity"] == RELAY
    assert ready["continuity"]["event_2_proven"] is False
    assert ready["idle_policy_s"] == 120
    assert ready["idle_policy_override"] is False
    assert ready["python_invokes_official_cu"] is False

    assert client.lease_claims == [{"binding_id": BINDING, "ttl_s": MAX_LEASE_TTL_S}]
    claim_ops = [call for call in client.calls if call["op"] == "wake-relay-claim"]
    begin_ops = [call for call in client.calls if call["op"] == "wake-relay-native-begin"]
    assert len(claim_ops) == 1
    assert len(begin_ops) == 1
    assert claim_ops[0]["args"]["dispatch_id"] == DISPATCH
    assert begin_ops[0]["args"]["dispatch_id"] == DISPATCH
    assert claim_ops[0]["args"]["relay_agent"] == RELAY
    # STOP is checked before selection/claim and again after claim before begin.
    assert ops(client).count("wake-stop-status") >= 3
    assert ops(client).index("wake-desktop-input-claim") < ops(client).index("wake-relay-claim")
    assert ops(client).index("wake-relay-claim") < ops(client).index("wake-relay-native-begin")
    assert_relay_identity(client)

    again = built.prepare(BINDING, observation())
    assert again["code"] in {"duplicate_submit", "no_pending"}
    assert again["submit"] is None
    assert ops(client).count("wake-relay-claim") == 1
    assert ops(client).count("wake-relay-native-begin") == 1
    assert OFFICIAL_CU_TOOL not in json.dumps(again)


def test_lease_required_before_claim_and_release_uses_opaque_digest() -> None:
    built, client = host()
    client.lease_ok = False
    refused = built.prepare(BINDING, observation())
    assert refused["code"] == "lease_unavailable"
    assert "wake-relay-claim" not in ops(client)

    built, client = host()
    client.lease_foreign = True
    refused = built.prepare(BINDING, observation())
    assert refused["code"] == "lease_unavailable"
    assert "wake-relay-claim" not in ops(client)

    built, client = host()
    client.lease_wrong_actor = True
    refused = built.prepare(BINDING, observation())
    assert refused["code"] == "lease_unavailable"
    assert "wake-relay-claim" not in ops(client)

    built, client = host()
    client.stop_active = False
    original_request = client.request

    def flip_after_claim(op, identity, args, timeout_s=None):
        result = original_request(op, identity, args, timeout_s=timeout_s)
        if op == "wake-relay-claim":
            client.stop_active = True
        return result

    client.request = flip_after_claim  # type: ignore[method-assign]
    refused = built.prepare(BINDING, observation())
    assert refused["code"] == "global_stop"
    assert refused["pre_submit"] == "released"
    assert "wake-relay-native-begin" not in ops(client)
    assert client.unsubmitted and client.unsubmitted[0]["dispatch_id"] == DISPATCH
    assert client.lease_releases == [
        {"binding_id": BINDING, "expected_lease_digest": client.lease_digest}
    ]

    built, client = host()
    original_request = client.request

    def flip_after_lease(op, identity, args, timeout_s=None):
        result = original_request(op, identity, args, timeout_s=timeout_s)
        if op == "wake-desktop-input-claim":
            client.stop_active = True
        return result

    client.request = flip_after_lease  # type: ignore[method-assign]
    refused = built.prepare(BINDING, observation())
    assert refused["code"] == "global_stop"
    assert client.unsubmitted == []
    assert "wake-relay-claim" not in ops(client)
    assert client.lease_releases == [
        {"binding_id": BINDING, "expected_lease_digest": client.lease_digest}
    ]

    built, client = host()
    ready = built.prepare(BINDING, observation())
    assert ready["code"] == "submit_once"
    settled = built.settle(DISPATCH, {"status": "error", "call_id": CALL})
    assert settled["code"] == "receipt_recorded"
    assert client.lease_releases[-1] == {
        "binding_id": BINDING,
        "expected_lease_digest": client.lease_digest,
    }
    releases_after_finalize = len(client.lease_releases)
    repeat = built.settle(DISPATCH, {"status": "error", "call_id": CALL})
    assert repeat["code"] == "receipt_idempotent"
    assert len(client.lease_releases) == releases_after_finalize
    for claim in client.lease_claims:
        assert set(claim) == {"binding_id", "ttl_s"}
        assert claim["ttl_s"] <= MAX_LEASE_TTL_S
    for release in client.lease_releases:
        assert set(release) == {"binding_id", "expected_lease_digest"}
        assert len(release["expected_lease_digest"]) == 64


def test_missing_or_unknown_cu_receipt_is_fenced_and_never_resent() -> None:
    built, client = host()
    ready = built.prepare(BINDING, observation())
    assert ready["code"] == "submit_once"
    missing = built.settle(DISPATCH, None)
    assert missing["code"] == "receipt_unknown"
    assert missing["delivery"] == "unknown"
    assert missing["resent"] is False
    assert client.receipts == []
    again = built.settle(DISPATCH, None)
    assert again["code"] == "receipt_idempotent"
    assert again["delivery"] == "unknown"
    assert client.receipts == []
    ambiguous = built.settle(DISPATCH, {"status": "queued"})
    assert ambiguous["code"] == "receipt_idempotent"
    assert ambiguous["resent"] is False
    assert client.receipts == []
    assert ops(client).count("wake-relay-claim") == 1
    assert ops(client).count("wake-relay-native-begin") == 1

    built, client = host()
    built.prepare(BINDING, observation())
    unknown_shape = built.settle(DISPATCH, {"threadId": SESSION})
    assert unknown_shape["code"] == "receipt_unknown"
    assert unknown_shape["office_receipt"] == "unwritten"
    assert client.receipts == []
    built2, client2 = host()
    built2.prepare(BINDING, observation())
    assert built2.settle(DISPATCH, {"status": "started"})["code"] == "receipt_unknown"


def test_actual_receipt_is_recorded_and_reconcile_is_idempotent() -> None:
    built, client = host()
    built.prepare(BINDING, observation())
    recorded = built.settle(DISPATCH, {"status": "failed", "call_id": CALL})
    assert recorded["code"] == "receipt_recorded"
    assert recorded["office_receipt"] == "error"
    assert recorded["started"] is False
    assert client.receipts == [
        {
            "dispatch_id": DISPATCH,
            "status": "error",
            "message_id": CALL,
            "detail": "tool_error",
            "driver": DRIVER,
        }
    ]
    same = built.settle(DISPATCH, {"status": "failed", "call_id": CALL})
    assert same["code"] == "receipt_idempotent"
    conflict = built.settle(DISPATCH, {"status": "failed", "call_id": TURN})
    assert conflict["code"] == "receipt_conflict"
    assert len(client.receipts) == 1

    started = built.reconcile(DISPATCH, "target_started")
    assert started["code"] == "observed"
    assert started["started"] is True
    assert started["completed"] is False
    assert client.observes == [
        {"dispatch_id": DISPATCH, "stage": "target_started", "driver": DRIVER}
    ]
    assert built.reconcile(DISPATCH, "target_started")["code"] == "observe_idempotent"
    assert len(client.observes) == 1
    finished = built.reconcile(DISPATCH, "target_finished")
    assert finished["completed"] is True
    assert built.reconcile(DISPATCH, "target_finished")["code"] == "observe_idempotent"
    assert len(client.observes) == 2
    assert built.reconcile(DISPATCH, "target_started")["code"] == "observe_idempotent"


def test_cu_receipt_parser_does_not_invent_start() -> None:
    assert map_cu_receipt(None)["code"] == "receipt_unknown"
    assert map_cu_receipt({"status": "queued"})["code"] == "receipt_unknown"
    assert map_cu_receipt({"call_id": "not-a-uuid", "status": "ok"})["code"] == "receipt_unknown"
    parsed = map_cu_receipt({"status": "queued", "call_id": CALL, "transcript": "SECRET"})
    assert parsed["office_status"] == "unconfirmed"
    assert parsed["started"] is False
    assert "SECRET" not in json.dumps(parsed)
    failed = map_cu_receipt({"status": "error", "call_id": CALL})
    assert failed["office_status"] == "error"


def test_registry_and_module_stay_on_protocol_seam() -> None:
    with pytest.raises(CoordError):
        ZCodeDesktopTarget(
            binding_id=BINDING,
            native_session_id=SESSION,
            office_agent=AGENT,
            project_slug=SLUG,
            workspace=WORKSPACE,
            relay_identity=AGENT,
            authorized=True,
        )
    with pytest.raises(CoordError):
        ZCodeDesktopTarget(
            binding_id=BINDING,
            native_session_id="local_" + SESSION,
            office_agent=AGENT,
            project_slug=SLUG,
            workspace=WORKSPACE,
            relay_identity=RELAY,
            authorized=True,
        )
    with pytest.raises(CoordError):
        target(office_agent="zcode-ailab-zcode-ailab-" + SESSION)
    with pytest.raises(CoordError):
        target(relay_identity="zcode-ailab-" + OTHER)
    source = (PACKAGE_ROOT / "coordfs" / "zcode_desktop_tool_host.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            modules.add((node.module or "").split(".")[0])
    assert modules <= {"json", "re", "time", "dataclasses", "types", "typing", "coordfs", "__future__"}
    assert "subprocess" not in source
    assert "FROZEN" not in source
    assert "import wake" not in source
    assert "from coordfs import wake" not in source


# -- synthetic server behind the real OfficeRemoteClient request path -----------

class _ResponsePipe:
    def __init__(self) -> None:
        self.ready = threading.Event()
        self.data = b""
        self.offset = 0

    def supply(self, data: bytes) -> None:
        self.data = data
        self.ready.set()

    def read(self, size: int = -1) -> bytes:
        assert self.ready.wait(3), "synthetic server did not finish"
        if self.offset >= len(self.data):
            return b""
        end = len(self.data) if size < 0 else min(len(self.data), self.offset + size)
        chunk = self.data[self.offset:end]
        self.offset = end
        return chunk

    def close(self) -> None:
        pass


class _RequestPipe:
    def __init__(self, process: "_SyntheticProcess") -> None:
        self.process = process
        self.data = bytearray()
        self.closed = False

    def write(self, data: bytes | memoryview) -> int:
        payload = bytes(data)
        self.data.extend(payload)
        return len(payload)

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        output = self.process.server.handle(bytes(self.data))
        self.process.returncode = 0
        self.process.stdout.supply(output)


class _SyntheticProcess:
    def __init__(self, server: "SyntheticOfficeServer") -> None:
        self.server = server
        self.stdout = _ResponsePipe()
        self.stdin = _RequestPipe(self)
        self.returncode: int | None = None

    def wait(self, timeout: float | None = None) -> int:
        assert self.stdout.ready.wait(3 if timeout is None else min(timeout, 3))
        assert self.returncode is not None
        return self.returncode

    def kill(self) -> None:
        self.returncode = -9
        self.stdout.supply(b"")


class SyntheticOfficeServer:
    """Frame-compatible stand-in for the remote gate, including desktop-input leases."""

    def __init__(self) -> None:
        self.fake = FakeOffice()
        self.frames: list[dict] = []

    def handle(self, raw: bytes) -> bytes:
        line = raw.split(b"\n", 1)[0]
        request = json.loads(line.decode("utf-8"))
        self.frames.append(request)
        response = self.fake.request(
            request["op"], request["identity"], request["args"]
        )
        return json.dumps(response, separators=(",", ":")).encode("utf-8") + b"\n"


class _SyntheticFactory:
    def __init__(self, server: SyntheticOfficeServer) -> None:
        self.server = server
        self.processes: list[_SyntheticProcess] = []

    def __call__(self, *_args: object, **_kwargs: object) -> _SyntheticProcess:
        process = _SyntheticProcess(self.server)
        self.processes.append(process)
        return process


@pytest.fixture
def remote_allow_desktop_input(monkeypatch):
    monkeypatch.setattr(
        "coordfs.office_remote.ALLOWED_OPS",
        REMOTE_ALLOWED_OPS
        | {"wake-desktop-input-claim", "wake-desktop-input-release"},
    )


def _remote_client(server: SyntheticOfficeServer) -> OfficeRemoteClient:
    config = OfficeRemoteConfig(
        host="office.example.invalid",
        user="agentoffice",
        identity_file="unused-test-key",
        known_hosts_file="unused-test-hosts",
        request_timeout_s=5,
    )
    return OfficeRemoteClient(config, popen_factory=_SyntheticFactory(server))


def test_synthetic_server_real_office_remote_client_path(remote_allow_desktop_input) -> None:
    server = SyntheticOfficeServer()
    server.fake.rows.append(row())
    client = _remote_client(server)
    built = ZCodeDesktopToolHost(client, registry(), now=lambda: NOW)

    for overrides, code in (
        (dict(app_activity="busy"), "target_busy"),
        (dict(draft=True), "target_draft"),
        (dict(lock=True), "target_lock"),
        (dict(quota="exhausted"), "target_quota"),
        (dict(app_activity="unknown"), "target_unknown"),
        (dict(source="other"), "source_mismatch"),
    ):
        refused = built.prepare(BINDING, observation(**overrides))
        assert refused["code"] == code
        assert refused["submit"] is None
        assert not any(frame["op"] == "wake-relay-claim" for frame in server.frames)

    server.fake.stop_active = True
    assert built.prepare(BINDING, observation())["code"] == "global_stop"
    server.fake.stop_active = False

    ready = built.prepare(BINDING, observation())
    assert ready["code"] == "submit_once"
    assert ready["submit"]["tool"] == OFFICIAL_CU_TOOL
    assert ready["submit"]["once"] is True
    assert ready["submit"]["arguments"]["session_id"] == SESSION
    ops_seen = [frame["op"] for frame in server.frames]
    assert ops_seen.count("wake-desktop-input-claim") == 1
    assert ops_seen.count("wake-relay-claim") == 1
    assert ops_seen.count("wake-relay-native-begin") == 1
    claim_frame = next(frame for frame in server.frames if frame["op"] == "wake-desktop-input-claim")
    assert set(claim_frame["args"]) == {"binding_id", "ttl_s"}
    assert claim_frame["args"]["ttl_s"] <= MAX_LEASE_TTL_S
    assert "actor" not in claim_frame["args"]
    assert claim_frame["identity"] == {"agent_id": RELAY}

    again = built.prepare(BINDING, observation())
    assert again["submit"] is None
    assert [frame["op"] for frame in server.frames].count("wake-relay-claim") == 1
