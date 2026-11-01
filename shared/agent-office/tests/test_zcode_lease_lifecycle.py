"""Lease-lifetime repair tests for the ZCode Desktop external-CU tool host.

Synthetic only: injected Office client, mutable clock, external submit and
foreground-restore callbacks. No Desktop, no CU invocation, no network.
"""

from __future__ import annotations

import hashlib
import sys
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
if str(PACKAGE_ROOT) not in sys.path:
    sys.path.insert(0, str(PACKAGE_ROOT))

from coordfs.desktop_adapters import MAX_LEASE_TTL_S, USER_IDLE_REQUIRED_S
from coordfs.zcode_desktop_tool_host import (
    SUBMIT_SETTLE_DEADLINE_S,
    ZCodeDesktopToolHost,
    TargetRegistry,
    ZCodeDesktopTarget,
)

SESSION = "sess_c68ed66d-0fe5-4746-82b3-48fe201bd47d"
CALL = "abcdefab-cdef-4abc-8def-1234567890ab"
DISPATCH = "b" * 32
AGENT = "zcode-ailab-" + SESSION
RELAY = "codex-ailab-01a0ac55-48ac-7742-9b18-8fa624150d4b"
BINDING = "zcode-desktop-target"
SLUG = "synthetic-zcode-desktop"
WORKSPACE = "D:\\synthetic\\zcode-desktop-target"
NOW = 1_800_000_000.0
DIGEST_A = hashlib.sha256(b"lease-a").hexdigest()
DIGEST_B = hashlib.sha256(b"lease-b").hexdigest()


class Clock:
    def __init__(self) -> None:
        self.now = NOW

    def __call__(self) -> float:
        return self.now


class FakeOffice:
    def __init__(self, clock: Clock) -> None:
        self.clock = clock
        self.calls: list[str] = []
        self.digest = DIGEST_A
        self.lease_ok = True
        self.holder = RELAY
        self.receipts: list[dict] = []
        self.releases: list[dict] = []
        self.rows = [
            {
                "dispatch_id": DISPATCH,
                "binding_id": BINDING,
                "target_local_id": SESSION,
                "text": "OFFICE-BODY " + SESSION,
                "status": "pending",
                "trigger_ref": "c" * 32,
                "created_at": "2030-01-01T00:00:00+00:00",
            }
        ]

    def request(self, op, identity, args, timeout_s=None):
        self.calls.append(op)
        if op == "wake-stop-status":
            return {"status": "ok", "code": "wake_stop_status", "stop_active": False, "since": None}
        if op == "wake-relay-pending":
            return {
                "status": "ok",
                "code": "wake_relay_pending",
                "count": 1,
                "dispatches": [dict(self.rows[0])],
            }
        if op == "wake-desktop-input-claim":
            if not self.lease_ok:
                return {"status": "warning", "code": "held"}
            return {
                "status": "ok",
                "code": "claimed",
                "lease": {
                    "resource_id": "data:desktop-input",
                    "holder_agent": self.holder,
                    "acquired_at": "2030-01-01T00:00:00Z",
                    "ttl_s": MAX_LEASE_TTL_S,
                    "file_hash": "",
                    "meta": {"binding_id": BINDING, "actor": self.holder},
                },
                "lease_digest": self.digest,
            }
        if op == "wake-desktop-input-release":
            if self.holder != RELAY or args['expected_lease_digest'] != self.digest:
                return {"status":"error", "code":"lease_mismatch"}
            self.releases.append(dict(args))
            return {"status": "ok", "code": "released"}
        if op == "wake-relay-claim":
            return {"status": "ok", "code": "wake_relay_claimed",
                    "dispatch": {"dispatch_id": args["dispatch_id"], "status": "claimed"}}
        if op == "wake-relay-native-begin":
            return {"status": "ok", "code": "wake_native_submission_fenced",
                    "dispatch": {"dispatch_id": args["dispatch_id"], "status": "delivery_unknown"}}
        if op == "wake-relay-receipt":
            self.receipts.append(dict(args))
            return {"status": "ok", "code": "wake_relay_receipt", "idempotent": False}
        raise AssertionError(op)


def observation():
    return {
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


def make():
    clock = Clock()
    office = FakeOffice(clock)
    target = ZCodeDesktopTarget(
        binding_id=BINDING, native_session_id=SESSION, office_agent=AGENT,
        project_slug=SLUG, workspace=WORKSPACE, relay_identity=RELAY, authorized=True,
    )
    host = ZCodeDesktopToolHost(office, TargetRegistry((target,)), now=clock)
    return host, office, clock


def prepared(host):
    ready = host.prepare(BINDING, observation())
    assert ready["code"] == "submit_once", ready
    return ready


def test_deadline_is_sixty_seconds() -> None:
    assert SUBMIT_SETTLE_DEADLINE_S == 60


def test_callback_trace_proves_submit_restore_then_own_release() -> None:
    host, office, _ = make()
    prepared(host)
    events: list[str] = []

    def submit_fn(submit):
        events.append("submit")
        assert submit["once"] is True
        assert set(submit["arguments"]) == {"session_id", "project_slug", "workspace", "text"}
        return {"status": "failed", "call_id": CALL}

    def restore_fn() -> None:
        events.append("restore_foreground")

    releases_before = len(office.releases)
    trace = host.run_external_submit(DISPATCH, submit_fn, restore_fn)
    assert trace["code"] == "submit_trace"
    assert trace["phases"] == ["fenced", "submitted", "foreground_restored", "lease_released"]
    assert trace["restore"] == "restored"
    assert trace["settle_code"] == "receipt_recorded"
    assert trace["resent"] is False
    assert events == ["submit", "restore_foreground"]
    assert len(office.releases) == releases_before + 1
    assert office.releases[-1] == {"binding_id": BINDING, "expected_lease_digest": DIGEST_A}


def test_submit_error_still_restores_and_never_resubmits() -> None:
    host, office, _ = make()
    prepared(host)
    restored: list[str] = []

    def boom(submit):
        raise TimeoutError("synthetic CU timeout")

    trace = host.run_external_submit(DISPATCH, boom, lambda: restored.append("yes"))
    assert trace["code"] == "submit_failed"
    assert trace["submit_error"] == "TimeoutError"
    assert trace["phases"] == ["fenced", "submitted", "foreground_restored", "lease_released"]
    assert restored == ["yes"]
    assert trace["delivery"] == "unknown"
    assert office.receipts == []

    again = host.run_external_submit(DISPATCH, lambda s: {"status": "ok", "call_id": CALL})
    assert again["code"] == "duplicate_submit"
    assert again["resent"] is False


def test_unknown_receipt_never_repeats_submit() -> None:
    host, office, _ = make()
    prepared(host)
    trace = host.run_external_submit(DISPATCH, lambda s: {"status": "queued", "call_id": CALL}, lambda: None)
    assert trace["settle_code"] == "receipt_recorded"
    assert trace["delivery"] == "unproven"
    assert len(office.receipts) == 1
    again = host.run_external_submit(DISPATCH, lambda s: {"status": "failed", "call_id": CALL})
    assert again["code"] == "duplicate_submit"
    assert again["resent"] is False
    assert len(office.receipts) == 1


def test_renew_within_deadline_keeps_own_lease() -> None:
    host, office, clock = make()
    prepared(host)
    clock.now += 30
    renewed = host.renew_lease(DISPATCH)
    assert renewed["code"] == "lease_renewed"
    assert renewed["remaining_s"] == 30
    assert office.releases == []


def test_hard_deadline_expires_without_renewal_or_resubmit() -> None:
    host, office, clock = make()
    prepared(host)
    clock.now += 61
    assert host.renew_lease(DISPATCH)["code"] == "lease_expired"
    assert len(office.releases) == 1
    late = host.run_external_submit(DISPATCH, lambda s: {"status": "failed", "call_id": CALL})
    assert late["code"] == "lease_expired"
    assert late["resent"] is False
    assert host.settle(DISPATCH, {"status": "failed", "call_id": CALL})["code"] in {
        "receipt_idempotent", "lease_expired",
    }
    assert office.receipts == []


def test_foreign_lease_drift_never_releases_foreign_lease() -> None:
    host, office, _ = make()
    prepared(host)
    office.digest = DIGEST_B  # server lease moved to another owner/digest
    office.holder = "codex-ailab-00000000-0000-0000-0000-000000000009"
    drifted = host.renew_lease(DISPATCH)
    assert drifted["code"] == "lease_unavailable"
    assert drifted["resent"] is False
    assert office.releases == []
    unknown = host.settle(DISPATCH, {"status": "failed", "call_id": CALL})
    assert unknown["code"] == "receipt_recorded"
    assert unknown["delivery"] == "error"
    assert unknown["office_receipt"] == "error"
    assert office.receipts == [
        {
            "dispatch_id": DISPATCH,
            "status": "error",
            "detail": "tool_error",
            "driver": "zcode-desktop-relay",
            "message_id": CALL,
        }
    ]
    repeat = host.settle(DISPATCH, {"status": "failed", "call_id": CALL})
    assert repeat["code"] == "receipt_idempotent"
    assert office.releases == []


def test_presend_lease_drift_never_submits() -> None:
    host, office, _ = make()
    prepared(host)
    office.digest = DIGEST_B  # server lease moved between prepare and submit
    office.holder = "codex-ailab-00000000-0000-0000-0000-000000000009"
    sent: list[str] = []
    trace = host.run_external_submit(
        DISPATCH, lambda s: sent.append("yes") or {"status": "failed", "call_id": CALL},
    )
    assert trace["code"] == "lease_unavailable"
    assert trace["resent"] is False
    assert sent == []
    assert office.releases == []
    assert office.receipts == []


def test_restore_failure_never_claims_foreground_or_release() -> None:
    host, office, _ = make()
    prepared(host)

    def broken_restore() -> None:
        raise RuntimeError("synthetic focus restore failure")

    trace = host.run_external_submit(
        DISPATCH, lambda s: {"status": "failed", "call_id": CALL}, broken_restore,
    )
    assert trace["code"] == "restore_failed"
    assert trace["restore"] == "restore_failed"
    assert trace["phases"] == ["fenced", "submitted", "restore_failed"]
    assert trace["settle_code"] == "unsettled"
    # Owned lease retained for confirmed restoration or natural expiry.
    assert office.releases == []
    assert office.receipts == []
