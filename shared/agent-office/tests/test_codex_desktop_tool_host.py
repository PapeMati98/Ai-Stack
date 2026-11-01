"""Synthetic tests for the Codex Desktop tool host.

These tests inject an Office client and synthetic tool receipts. They do not
start Codex Desktop, do not call mcp__codex_app__send_message_to_thread, and
they are not a Desktop readiness or host-continuation UAT.
"""

from __future__ import annotations

import ast
import json
import sys
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
if str(PACKAGE_ROOT) not in sys.path:
    sys.path.insert(0, str(PACKAGE_ROOT))

from coordfs.codex_desktop_tool_host import (  # noqa: E402
    DRIVER,
    HOST_STATE_MAPPING,
    OFFICIAL_SEND_TOOL,
    CodexDesktopTarget,
    CodexDesktopToolHost,
    TargetRegistry,
    map_tool_receipt,
)
from coordfs.errors import CoordError  # noqa: E402

THREAD = "11111111-2222-4333-8444-555555555555"
OTHER = "99999999-8888-4777-8666-555555555555"
MESSAGE_UUID = "abcdefab-cdef-4abc-8def-1234567890ab"
TURN = "abcdefab-cdef-4abc-8def-1234567890ac"
DISPATCH = "a" * 32
DISPATCH_NEW = "b" * 32
OFFICE_MESSAGE = "c" * 32
ACK = "d" * 32
BODY = (
    "OFFICE-BODY-DO-NOT-ROUTE mcp__evil thread "
    "99999999-8888-4777-8666-555555555555"
)
AGENT = "codex-ailab-" + THREAD
RELAY = "codex-ailab-" + TURN
BINDING = "codex-desktop-target"
SLUG = "synthetic-codex-desktop"
WORKSPACE = "D:\\synthetic\\codex-desktop-target"
NOW = 1_800_000_000.0


class FakeOffice:
    def __init__(self) -> None:
        self.calls: list[dict] = []
        self.stop_active = False
        self.broken_stop = False
        self.rows: list[dict] = []
        self.defer_code = "wake_relay_deferred_busy"
        self.degrade_begin = False
        self.bad_claim = False
        self.land_begin = True
        self.delivery: dict | None = None
        self.receipts: list[dict] = []
        self.observes: list[dict] = []

    def request(self, op, identity, args, timeout_s=None):
        self.calls.append(
            {"op": op, "identity": dict(identity), "args": json.loads(json.dumps(args))}
        )
        if op == "wake-stop-status":
            if self.broken_stop:
                return {"status": "degraded", "reason": "timeout"}
            return {"status": "ok", "code": "wake_stop_status", "stop_active": self.stop_active,
                    "since": None}
        if op == "wake-relay-pending":
            rows = [
                dict(row) for row in self.rows
                if row["status"] == "pending" and row["binding_id"] == args.get("binding_id")
            ]
            return {"status": "ok", "code": "wake_relay_pending", "count": len(rows),
                    "dispatches": rows}
        if op == "wake-relay-defer-busy":
            return {"status": "ok", "code": self.defer_code}
        if op == "wake-relay-claim":
            if self.bad_claim:
                return {"status": "ok", "code": "wake_relay_claimed"}
            self._set(args["dispatch_id"], "claimed")
            return {"status": "ok", "code": "wake_relay_claimed",
                    "dispatch": {"dispatch_id": args["dispatch_id"], "status": "claimed"}}
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
        if op == "wake-relay-receipt":
            self.receipts.append(dict(args))
            return {"status": "ok", "code": "wake_relay_receipt", "idempotent": False}
        if op == "wake-relay-observe":
            self.observes.append(dict(args))
            return {"status": "ok", "code": "wake_relay_observed", "idempotent": False}
        if op == "delivery":
            return self.delivery or {"status": "ok", "code": "delivery", "state": "written",
                                     "acknowledged_by": [], "acks": []}
        raise AssertionError(op)

    def _set(self, dispatch_id: str, status: str) -> None:
        for row in self.rows:
            if row["dispatch_id"] == dispatch_id:
                row["status"] = status


def target(**overrides) -> CodexDesktopTarget:
    value = dict(
        binding_id=BINDING,
        native_thread_id=THREAD,
        office_agent=AGENT,
        project_slug=SLUG,
        workspace=WORKSPACE,
        relay_identity=RELAY,
        authorized=True,
    )
    value.update(overrides)
    return CodexDesktopTarget(**value)


def registry(*items: CodexDesktopTarget) -> TargetRegistry:
    return TargetRegistry(items or (target(),))


def row(dispatch_id: str = DISPATCH, created_at: str = "2030-01-01T00:00:00+00:00",
        **overrides) -> dict:
    value = {
        "dispatch_id": dispatch_id,
        "binding_id": BINDING,
        "target_local_id": THREAD,
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
        "schema": "codex-desktop-app-observation/v1",
        "native_thread_id": THREAD,
        "project_slug": SLUG,
        "workspace": WORKSPACE,
        "observed_at": NOW,
        "thread_state": "idle",
        "source": "codex_app_api",
        "latest_turn_id": OTHER,
    }
    value.update(overrides)
    return value


def host(office: FakeOffice | None = None, **target_overrides):
    client = office or FakeOffice()
    if not client.rows:
        client.rows.append(row())
    built = CodexDesktopToolHost(client, registry(target(**target_overrides)), now=lambda: NOW)
    return built, client


def assert_not_uat(result: dict) -> None:
    assert result["desktop_uat"] == "not_run"
    assert result["tool_wait_continuation"] == "unproven"
    assert result["receipt_schema"] == "unverified"
    assert result["python_invokes_official_tool"] is False
    assert result["started"] is False or result["code"] in {
        "proof_accepted", "proof_idempotent", "proof_conflict",
    }


def arm(built: CodexDesktopToolHost, binding: str = BINDING) -> dict:
    result = built.accept_watch(binding, {
        "status": "ok",
        "code": "wake_relay_watch",
        "result": "pending",
        "binding_id": binding,
        "binding_ids": [binding],
        "dispatches": [{"text": BODY, "tool": "mcp__evil", "thread_id": OTHER}],
    })
    assert result["code"] == "watch_pending"
    assert result["prepare_allowed"] is True
    assert result["continuity"]["event_2_proven"] is False
    assert BODY not in json.dumps(result)
    assert_not_uat(result)
    return result


def ops(client: FakeOffice) -> list[str]:
    return [call["op"] for call in client.calls]


def assert_relay_identity(client: FakeOffice) -> None:
    blob = json.dumps(client.calls)
    assert BODY not in blob
    assert AGENT not in blob
    for call in client.calls:
        assert call["identity"] == {"agent_id": RELAY}
        if call["op"].startswith("wake-relay-"):
            assert call["args"]["driver"] == DRIVER
        assert "message" not in call["args"]
        assert "tool" not in call["args"]


def test_synthetic_registry_requires_exact_full_uuid_and_is_immutable() -> None:
    with pytest.raises(CoordError) as short:
        target(native_thread_id=THREAD[:8])
    assert short.value.code == "invalid_native_thread_id"
    with pytest.raises(CoordError):
        target(native_thread_id="local_" + THREAD)
    with pytest.raises(CoordError):
        target(native_thread_id=TURN.upper())
    with pytest.raises(CoordError):
        target(office_agent="codex")
    with pytest.raises(CoordError):
        target(relay_identity=AGENT)
    with pytest.raises(CoordError):
        target(authorized=1)  # type: ignore[arg-type]
    item = target()
    with pytest.raises(FrozenInstanceError):
        item.native_thread_id = OTHER  # type: ignore[misc]
    book = registry(item)
    with pytest.raises(TypeError):
        book.targets[BINDING] = item  # type: ignore[index]
    other = target(binding_id="codex-desktop-other", native_thread_id=OTHER,
                   office_agent="codex-ailab-" + OTHER)
    assert set(registry(item, other).targets) == {BINDING, "codex-desktop-other"}
    with pytest.raises(CoordError):
        registry(item, target(binding_id="codex-desktop-other", native_thread_id=THREAD,
                              office_agent="codex-ailab-" + THREAD))
    with pytest.raises(CoordError):
        registry(item, target())
    with pytest.raises(TypeError):
        HOST_STATE_MAPPING["desktop_uat"] = "passed"  # type: ignore[index]
    assert HOST_STATE_MAPPING["desktop_uat"] == "not_run"
    assert HOST_STATE_MAPPING["python_invokes_official_tool"] is False
    assert HOST_STATE_MAPPING["stored_or_queued_tool_receipt"] == "not_started"


def test_synthetic_fresh_observation_reads_office_and_hides_body() -> None:
    built, client = host()
    seen = built.observe(BINDING, observation())
    assert seen["code"] == "observed"
    assert seen["dispatch_id"] == DISPATCH
    assert seen["submit"] is None
    assert seen["action"] == "eligible_for_one_prepare"
    assert ops(client) == ["wake-stop-status", "wake-relay-pending"]
    assert BODY not in json.dumps(seen)
    assert_not_uat(seen)
    assert_relay_identity(client)

    stale, stale_client = host()
    refused = stale.observe(BINDING, observation(observed_at=NOW - 60))
    assert refused["code"] == "stale_observation"
    assert stale_client.calls == []
    mismatched = stale.observe(BINDING, observation(project_slug="other-project"))
    assert mismatched["code"] == "unverified_project"
    assert stale.observe(BINDING, observation(native_thread_id=OTHER))["code"] == "target_mismatch"
    assert stale.observe(BINDING, observation(workspace=WORKSPACE + "\\other"))["code"] == "unverified_project"
    assert stale_client.calls == []


def test_synthetic_busy_quota_stop_unknown_and_unverified_refuse_before_send() -> None:
    busy, busy_client = host()
    arm(busy)
    deferred = busy.prepare(BINDING, observation(thread_state="busy"))
    assert deferred["status"] == "deferred"
    assert deferred["code"] == "target_busy"
    assert deferred["submit"] is None
    assert ops(busy_client) == ["wake-stop-status", "wake-relay-pending", "wake-relay-defer-busy"]
    assert_relay_identity(busy_client)

    limited, limited_client = host()
    limited_client.defer_code = "wake_relay_busy_limit"
    arm(limited)
    limit = limited.prepare(BINDING, observation(thread_state="busy"))
    assert limit["code"] == "target_busy_limit"
    assert limit["terminal"] is True
    assert "wake-relay-claim" not in ops(limited_client)

    quota, quota_client = host()
    arm(quota)
    refused = quota.prepare(BINDING, observation(thread_state="quota"))
    assert refused["code"] == "target_quota"
    assert ops(quota_client) == ["wake-stop-status"]

    unknown, unknown_client = host()
    arm(unknown)
    assert unknown.prepare(BINDING, observation(thread_state="mystery"))["code"] == "target_unknown"
    assert unknown_client.calls == []

    stopped, stopped_client = host()
    stopped_client.stop_active = True
    arm(stopped)
    assert stopped.prepare(BINDING, observation())["code"] == "global_stop"
    assert ops(stopped_client) == ["wake-stop-status"]

    broken, broken_client = host()
    broken_client.broken_stop = True
    arm(broken)
    assert broken.prepare(BINDING, observation())["code"] == "stop_unavailable"
    assert "wake-relay-claim" not in ops(broken_client)

    denied, denied_client = host(authorized=False)
    assert denied.prepare(BINDING, observation())["code"] == "unverified_project"
    assert denied_client.calls == []
    for result in (deferred, limit, refused):
        assert result["started"] is False
        assert result["submit"] is None
        assert OFFICIAL_SEND_TOOL not in json.dumps(result)


def test_synthetic_message_body_cannot_supply_route_or_tools() -> None:
    built, client = host()
    client.rows = [
        row(DISPATCH_NEW, "2030-01-01T00:00:02+00:00", text="newer " + BODY),
        row(),
        row("e" * 32, "2030-01-01T00:00:03+00:00", status="claimed"),
    ]
    arm(built)
    ready = built.prepare(BINDING, observation())
    assert ready["code"] == "submit_once"
    assert ready["dispatch_id"] == DISPATCH
    submit = ready["submit"]
    assert submit["tool"] == OFFICIAL_SEND_TOOL
    assert submit["once"] is True
    assert set(submit["arguments"]) == {"threadId", "hostId", "prompt"}
    assert submit["arguments"]["threadId"] == THREAD
    assert submit["arguments"]["hostId"] == "local"
    assert submit["arguments"]["prompt"] == BODY
    assert ready["continuity"]["event_2_proven"] is False
    assert_not_uat(ready)
    claimed = [call for call in client.calls if call["op"] == "wake-relay-claim"]
    begun = [call for call in client.calls if call["op"] == "wake-relay-native-begin"]
    assert [call["args"]["dispatch_id"] for call in claimed] == [DISPATCH]
    assert [call["args"]["dispatch_id"] for call in begun] == [DISPATCH]
    assert claimed[0]["args"]["relay_agent"] == RELAY
    assert_relay_identity(client)


def test_synthetic_one_submit_and_interruption_after_presubmit() -> None:
    built, client = host()
    assert built.prepare(BINDING, observation())["code"] == "watch_required"
    assert client.calls == []
    arm(built)
    ready = built.prepare(BINDING, observation())
    assert ready["code"] == "submit_once"
    missing = built.settle(DISPATCH, None)
    assert missing["code"] == "receipt_missing"
    assert missing["delivery"] == "unknown"
    assert missing["started"] is False
    assert client.receipts == []
    arm(built)
    again = built.prepare(BINDING, observation())
    assert again["code"] == "no_pending"
    assert again["submit"] is None
    assert ops(client).count("wake-relay-native-begin") == 1
    assert ops(client).count("wake-relay-claim") == 1

    lying, lying_client = host()
    lying_client.land_begin = False
    arm(lying)
    assert lying.prepare(BINDING, observation())["code"] == "submit_once"
    arm(lying)
    duplicate = lying.prepare(BINDING, observation())
    assert duplicate["code"] in {"duplicate_submit", "no_pending"}
    assert duplicate["submit"] is None
    assert ops(lying_client).count("wake-relay-native-begin") == 1
    assert OFFICIAL_SEND_TOOL not in json.dumps(duplicate)

    lost, lost_client = host()
    lost_client.degrade_begin = True
    arm(lost)
    unconfirmed = lost.prepare(BINDING, observation())
    assert unconfirmed["code"] == "begin_unconfirmed"
    assert unconfirmed["submit"] is None
    restarted = CodexDesktopToolHost(lost_client, registry(), now=lambda: NOW)
    arm(restarted)
    assert restarted.prepare(BINDING, observation())["code"] == "no_pending"
    assert ops(lost_client).count("wake-relay-native-begin") == 1
    assert lost.settle(DISPATCH, {"status": "queued", "message_id": MESSAGE_UUID})["code"] == "not_fenced"


def test_synthetic_receipt_parser_does_not_invent_start_or_ids() -> None:
    secret = {"status": "queued", "messageId": MESSAGE_UUID, "transcript": "SECRET-TRANSCRIPT"}
    parsed = map_tool_receipt(secret)
    assert parsed["delivery"] == "unproven"
    assert parsed["started"] is False
    assert parsed["completed"] is False
    assert parsed["office_status"] == "unconfirmed"
    assert parsed["office_message_id"] == MESSAGE_UUID
    assert "SECRET-TRANSCRIPT" not in json.dumps(parsed)
    assert map_tool_receipt({"status": "stored", "message_id": MESSAGE_UUID})["started"] is False
    assert map_tool_receipt({"status": "started", "message_id": MESSAGE_UUID})["office_status"] == "unconfirmed"
    assert map_tool_receipt({"status": "completed", "message_id": MESSAGE_UUID})["completed"] is False
    assert map_tool_receipt({"status": "queued", "message_id": "not-a-uuid"})["code"] == "receipt_unknown"
    unknown = map_tool_receipt({"message_id": MESSAGE_UUID})
    assert unknown["code"] == "receipt_unknown"
    assert "office_message_id" not in unknown
    assert map_tool_receipt(["queued", MESSAGE_UUID])["code"] == "receipt_unknown"
    assert map_tool_receipt("{" + "x" * 5000 + "}")["code"] == "receipt_unknown"
    encoded = json.dumps({"state": "Queued", "turn_id": TURN})
    turned = map_tool_receipt(encoded)
    assert turned["office_message_id"] is None
    assert turned["turn_id"] == TURN
    assert turned["started"] is False
    failed = map_tool_receipt({"status": "error", "message_id": MESSAGE_UUID, "turnId": TURN})
    assert failed["office_status"] == "error"
    assert failed["started"] is False

    built, client = host()
    arm(built)
    assert built.prepare(BINDING, observation())["code"] == "submit_once"
    recorded = built.settle(DISPATCH, secret)
    assert recorded["code"] == "receipt_recorded"
    assert recorded["started"] is False
    assert recorded["delivery"] == "unproven"
    assert recorded["office_receipt"] == "unconfirmed"
    assert "SECRET-TRANSCRIPT" not in json.dumps(recorded)
    assert client.receipts == [{
        "dispatch_id": DISPATCH,
        "status": "unconfirmed",
        "message_id": MESSAGE_UUID,
        "detail": "unverified_tool_receipt",
        "driver": DRIVER,
    }]
    assert client.observes == []
    again = built.settle(DISPATCH, secret)
    assert again["code"] == "receipt_idempotent"
    assert client.receipts == client.receipts[:1]
    conflict = built.settle(DISPATCH, {"status": "queued", "message_id": TURN})
    assert conflict["code"] == "receipt_conflict"
    assert len(client.receipts) == 1

    errored, error_client = host()
    arm(errored)
    errored.prepare(BINDING, observation())
    error_result = errored.settle(DISPATCH, {"status": "failed", "message_id": MESSAGE_UUID})
    assert error_result["office_receipt"] == "error"
    assert error_result["started"] is False
    assert error_client.receipts[0]["status"] == "error"
    assert errored.reconcile(DISPATCH, _proof("completed"), _ack())["code"] == "not_reconcilable"
    assert error_client.observes == []


def _proof(phase: str, **overrides) -> dict:
    value = {
        "schema": "codex-desktop-thread-proof/v1",
        "native_thread_id": THREAD,
        "message_id": MESSAGE_UUID,
        "phase": phase,
        "thread_ended": phase == "completed",
        "observed_at": NOW,
    }
    value.update(overrides)
    return value


def _ack(**overrides) -> dict:
    value = {
        "schema": "codex-desktop-office-ack/v1",
        "message_id": OFFICE_MESSAGE,
        "ack_id": ACK,
        "from_agent": AGENT,
        "channel": "office-wake",
    }
    value.update(overrides)
    return value


def _deliver() -> dict:
    return {
        "status": "ok",
        "code": "delivery",
        "state": "acknowledged",
        "acknowledged_by": [AGENT],
        "acks": [{"agent": AGENT, "ack_id": ACK, "body": "do-not-copy"}],
        "written": {"body": "secret mail"},
    }


def test_synthetic_reconcile_requires_ended_thread_and_office_ack() -> None:
    built, client = host()
    client.delivery = _deliver()
    arm(built)
    built.prepare(BINDING, observation())
    built.settle(DISPATCH, {"status": "stored", "message_id": MESSAGE_UUID, "turn_id": TURN})
    assert built.reconcile(DISPATCH, _proof("completed", thread_ended=False), _ack())["code"] == "invalid_proof"
    assert built.reconcile(DISPATCH, _proof("completed", message_id=TURN), _ack())["code"] == "invalid_proof"
    assert built.reconcile(DISPATCH, _proof("completed"), _ack(message_id="e" * 32))["code"] == "ack_correlation_mismatch"
    assert built.reconcile(DISPATCH, _proof("completed"), _ack(from_agent=RELAY))["code"] == "ack_correlation_mismatch"
    client.delivery = {"status": "ok", "code": "delivery", "state": "written",
                       "acknowledged_by": [], "acks": []}
    assert built.reconcile(DISPATCH, _proof("completed"), _ack())["code"] == "ack_missing"
    assert client.observes == []
    client.delivery = _deliver()
    accepted = built.reconcile(DISPATCH, _proof("completed"), _ack())
    assert accepted["code"] == "proof_accepted"
    assert accepted["started"] is True
    assert accepted["completed"] is True
    assert accepted["desktop_uat"] == "not_run"
    assert accepted["tool_wait_continuation"] == "unproven"
    assert client.observes == [{
        "dispatch_id": DISPATCH, "stage": "target_finished", "driver": DRIVER,
    }]
    assert "secret mail" not in json.dumps(accepted)
    assert "do-not-copy" not in json.dumps(accepted)
    assert built.reconcile(DISPATCH, _proof("completed"), _ack())["code"] == "proof_idempotent"
    assert len(client.observes) == 1

    started, started_client = host()
    started_client.delivery = _deliver()
    arm(started)
    started.prepare(BINDING, observation())
    settled = started.settle(DISPATCH, {"status": "started", "message_id": MESSAGE_UUID})
    assert settled["started"] is False
    assert started_client.observes == []
    proved = started.reconcile(DISPATCH, _proof("started"), _ack())
    assert proved["started"] is True
    assert proved["completed"] is False
    assert started_client.observes[0]["stage"] == "target_started"
    assert proved["desktop_uat"] == "not_run"


def test_synthetic_watch_known_event_does_not_prove_tool_wait() -> None:
    built, client = host()
    request = built.watch_request(BINDING)
    assert request["op"] == "wake-relay-watch"
    assert request["executes"] is False
    assert request["listener"] is False
    assert request["args"]["binding_id"] == [BINDING]
    assert request["args"]["driver"] == DRIVER
    assert client.calls == []
    timeout = built.accept_watch(BINDING, {
        "status": "ok", "code": "wake_relay_watch", "result": "timeout",
        "binding_ids": [BINDING],
    })
    assert timeout["code"] == "watch_timeout"
    assert timeout["prepare_allowed"] is False
    assert built.prepare(BINDING, observation())["code"] == "watch_required"
    foreign = built.accept_watch(BINDING, {
        "status": "ok", "code": "wake_relay_watch", "result": "pending",
        "binding_ids": [BINDING, "other-binding"],
    })
    assert foreign["code"] == "watch_unknown"
    assert client.calls == []
    assert_not_uat(request)
    assert_not_uat(timeout)


def test_real_thread_only_shape_requires_new_turn_and_durable_target_ack() -> None:
    built, client = host()
    arm(built)
    built.prepare(BINDING, observation())
    unknown = built.settle(DISPATCH, {"threadId": THREAD})
    assert unknown["delivery"] == "unknown"
    assert client.receipts == []
    assert built.reconcile(DISPATCH, _proof("completed", message_id=None, turn_id=OTHER), _ack())["code"] == "invalid_proof"
    proof = _proof("completed", message_id=None, turn_id=TURN)
    assert built.reconcile(DISPATCH, proof, _ack())["code"] == "ack_missing"
    assert client.receipts == []
    client.delivery = _deliver()
    result = built.reconcile(DISPATCH, proof, _ack())
    assert result["completed"] is True
    assert client.receipts == [{"dispatch_id":DISPATCH, "status":"delivered", "turn_id":TURN,
        "detail":"correlated_office_ack_and_new_native_turn", "driver":DRIVER}]
    assert "message_id" not in client.receipts[0]
    assert built.reconcile(DISPATCH, proof, _ack())["code"] == "proof_idempotent"
    assert len(client.receipts) == 1


def test_attested_drive_root_is_valid_metadata_but_traversal_is_not() -> None:
    assert target(workspace="D:\\").workspace == "D:\\"
    assert target(workspace="/synthetic/office").workspace == "/synthetic/office"
    with pytest.raises(CoordError):
        target(workspace="D:\\synthetic\\..\\outside")


def test_synthetic_module_imports_stay_on_the_protocol_seam() -> None:
    source = Path(PACKAGE_ROOT, "coordfs", "codex_desktop_tool_host.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            modules.add((node.module or "").split(".")[0])
    assert modules <= {"json", "math", "re", "time", "dataclasses", "types", "typing", "coordfs", "__future__"}
    assert "FROZEN" not in source
    assert "subprocess" not in source
    assert "event_2_proven\": True" not in source
    assert "desktop_uat\": \"passed\"" not in source
