"""Codex Desktop native Office bridge tests: actual Wake + remote gate.

Every Office home is a synthetic fresh tmp_path. Tests exercise the real
in-process forced-command gate behind the actual OfficeRemoteClient framing
with authenticated actors — not mocks. Claude default behavior is preserved.
"""

from __future__ import annotations

import io
import json
import threading
import time
import uuid
from pathlib import Path

import pytest

from coordfs import office_ssh_gate
from coordfs.leases import format_ts
from coordfs.office import Office
from coordfs.office_remote import OfficeRemoteClient, OfficeRemoteConfig
from coordfs.office_ssh_gate import run as run_gate
from coordfs.wake import (
    CODEX_RELAY_DRIVER,
    DEFAULT_RELAY_CLAIM_TIMEOUT_S,
    RELAY_DRIVER,
    Wake,
)


# -- in-process real gate behind the real client transport ---------------------

class _ResponsePipe:
    def __init__(self) -> None:
        self.ready = threading.Event()
        self.data = b""
        self.offset = 0
        self.closed = False

    def supply(self, data: bytes) -> None:
        self.data = data
        self.ready.set()

    def read(self, size: int = -1) -> bytes:
        assert self.ready.wait(3), "gate did not finish"
        if self.offset >= len(self.data):
            return b""
        end = len(self.data) if size < 0 else min(len(self.data), self.offset + size)
        chunk = self.data[self.offset:end]
        self.offset = end
        return chunk

    def close(self) -> None:
        self.closed = True


class _RequestPipe:
    def __init__(self, process: "_GateProcess") -> None:
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
        output = io.BytesIO()
        self.process.returncode = run_gate(
            io.BytesIO(self.data), output, principal="ws-office",
            home=self.process.home, environ={},
        )
        self.process.stdout.supply(output.getvalue())


class _GateProcess:
    def __init__(self, home: Path) -> None:
        self.home = home
        self.stdout = _ResponsePipe()
        self.stdin = _RequestPipe(self)
        self.returncode: int | None = None

    def wait(self, timeout: float | None = None) -> int:
        assert self.stdout.ready.wait(3 if timeout is None else min(timeout, 3)), "gate did not respond"
        assert self.returncode is not None
        return self.returncode

    def kill(self) -> None:
        self.returncode = -9
        self.stdout.supply(b"")


class _GateFactory:
    def __init__(self, home: Path) -> None:
        self.home = home
        self.processes: list[_GateProcess] = []

    def __call__(self, *_args: object, **_kwargs: object) -> _GateProcess:
        process = _GateProcess(self.home)
        self.processes.append(process)
        return process


def _client(home: Path, factory: _GateFactory | None = None) -> OfficeRemoteClient:
    config = OfficeRemoteConfig(
        host="office.example.invalid", user="agentoffice",
        identity_file="unused-test-key", known_hosts_file="unused-test-hosts",
        request_timeout_s=5,
    )
    return OfficeRemoteClient(config, popen_factory=factory or _GateFactory(home))


def _raw_gate(home: Path, op: str, args: dict, *,
              identity: dict | None = None, principal: str = "ws-office",
              tail: bytes = b"") -> tuple[int, dict]:
    header = {"v": 1, "op": op,
              "identity": identity or {"agent_id": "codex-testhost-alice"},
              "args": args}
    stdout = io.BytesIO()
    code = run_gate(io.BytesIO(json.dumps(header).encode("utf-8") + b"\n" + tail), stdout,
                    principal=principal, home=home, environ={})
    return code, json.loads(stdout.getvalue())


# -- helpers -------------------------------------------------------------------

CODEX_UUID = "a1b2c3d4-e5f6-4a7b-8c9d-0e1f2a3b4c5d"
CLAUDE_THREAD = "local_" + CODEX_UUID
CODEX_WORKSPACE = r"C:\Users\codex\workspace"


@pytest.fixture(autouse=True)
def office_server_path_flavor(monkeypatch):
    # This module exercises the Linux server's gate for a Windows Desktop.
    # Local Windows directory checks are covered separately in cross-platform tests.
    monkeypatch.setattr('coordfs.wake._native_workspace_flavor', lambda: 'posix')


def _make_codex_binding(
    home: Path,
    *,
    binding_id: str = "codex-bind-001",
    agent: str = "codex-ailab-00000000-0000-0000-0000-000000000001",
    native_thread_id: str = CODEX_UUID,
    workspace: str = CODEX_WORKSPACE,
    now_fn=None,
) -> Wake:
    wake = Wake(home=home, now=now_fn or time.time)
    wake.enroll(
        binding_id=binding_id,
        office_agent=agent,
        driver=CODEX_RELAY_DRIVER,
        native_thread_id=native_thread_id,
        workspace=workspace,
        enrolled_by="test-owner",
        allowed_wakers=["*"],
        allowed_triggers=["message"],
        expires_at="2030-01-01T00:00:00+00:00",
        owner_confirm=True,
    )
    wake.verify(binding_id, owner_confirm=True)
    return wake


def _make_codex_dispatch(
    home: Path,
    *,
    binding_id: str = "codex-bind-001",
    now_fn=None,
) -> dict:
    wake = Wake(home=home, now=now_fn or time.time)
    binding = wake._load_binding(binding_id)
    message = Office(agent_id="codex-ailab-00000000-0000-0000-0000-000000000003", home=home, now=now_fn or time.time).send(
        to=binding["office_agent"], body="Synthetic Codex dispatch", new_recipient=True
    )
    assert message.get("status") == "ok", message
    return wake._create_relay_dispatch(
        binding,
        text="test codex wake payload",
        trigger="message",
        trigger_ref=message["id"],
    )


def _make_claude_binding(
    home: Path,
    *,
    binding_id: str = "claude-bind-001",
    agent: str = "claude-ailab-00000000-0000-0000-0000-000000000002",
    now_fn=None,
) -> Wake:
    wake = Wake(home=home, now=now_fn or time.time)
    wake.enroll(
        binding_id=binding_id,
        office_agent=agent,
        driver=RELAY_DRIVER,
        native_thread_id=CLAUDE_THREAD,
        workspace=r"C:\Users\claude\workspace",
        enrolled_by="test-owner",
        allowed_wakers=["*"],
        allowed_triggers=["message"],
        expires_at="2030-01-01T00:00:00+00:00",
        owner_confirm=True,
    )
    wake.verify(binding_id, owner_confirm=True)
    return wake


def _make_claude_dispatch(
    home: Path,
    *,
    binding_id: str = "claude-bind-001",
    now_fn=None,
) -> dict:
    wake = Wake(home=home, now=now_fn or time.time)
    binding = wake._load_binding(binding_id)
    message = Office(agent_id="claude-ailab-00000000-0000-0000-0000-000000000004", home=home, now=now_fn or time.time).send(
        to=binding["office_agent"], body="Synthetic Claude dispatch", new_recipient=True
    )
    assert message.get("status") == "ok", message
    return wake._create_relay_dispatch(
        binding,
        text="test claude wake payload",
        trigger="message",
        trigger_ref=message["id"],
    )


# -- Codex binding validation ---------------------------------------------------

def test_codex_binding_requires_exact_uuid_thread(tmp_path: Path) -> None:
    home = tmp_path / "coord"
    wake = Wake(home=home)
    for bad_id in (
        "local_a1b2c3d4-e5f6-4a7b-8c9d-0e1f2a3b4c5d",  # local_ prefix
        "process-12345",                                # process ID
        "some-title-text",                              # title
        "A1B2C3D4-E5F6-4A7B-8C9D-0E1F2A3B4C5D",       # uppercase
        "a1b2c3d4-e5f6-4a7b-8c9d",                     # truncated
    ):
        with pytest.raises(Exception) as exc_info:
            wake.enroll(
                binding_id="codex-bad-thread",
                office_agent="codex-ailab-x-00000000-0000-0000-0000-000000000001",
                driver=CODEX_RELAY_DRIVER,
                native_thread_id=bad_id,
                workspace=CODEX_WORKSPACE,
                enrolled_by="test",
                allowed_wakers=["*"],
                expires_at="2030-01-01T00:00:00+00:00",
                owner_confirm=True,
            )
        assert "invalid_native_thread_id" in str(getattr(exc_info.value, "code", ""))


def test_codex_binding_rejects_non_windows_workspace(tmp_path: Path) -> None:
    home = tmp_path / "coord"
    wake = Wake(home=home)
    with pytest.raises(Exception) as exc_info:
        wake.enroll(
            binding_id="codex-bad-ws",
            office_agent="codex-ailab-x-00000000-0000-0000-0000-000000000001",
            driver=CODEX_RELAY_DRIVER,
            native_thread_id=CODEX_UUID,
            workspace="/home/codex/workspace",
            enrolled_by="test",
            allowed_wakers=["*"],
            expires_at="2030-01-01T00:00:00+00:00",
            owner_confirm=True,
        )
    assert "invalid_workspace" in str(getattr(exc_info.value, "code", ""))


def test_codex_binding_accepts_valid_uuid_and_windows_workspace(tmp_path: Path) -> None:
    home = tmp_path / "coord"
    wake = _make_codex_binding(home)
    binding = wake._load_binding("codex-bind-001")
    assert binding["driver"] == CODEX_RELAY_DRIVER
    assert binding["native_thread_id"] == CODEX_UUID
    assert binding["workspace"] == CODEX_WORKSPACE


# -- Codex claim through real remote gate ---------------------------------------

def test_codex_claim_via_remote_gate(tmp_path: Path) -> None:
    home = tmp_path / "coord"
    _make_codex_binding(home)
    dispatch = _make_codex_dispatch(home)
    dispatch_id = dispatch["dispatch_id"]
    relay_agent = "codex-ailab-00000000-0000-0000-0000-000000000001"

    client = _client(home)
    identity = {"agent_id": relay_agent}
    result = client.request("wake-relay-claim", identity, {
        "dispatch_id": dispatch_id,
        "relay_agent": relay_agent,
        "driver": CODEX_RELAY_DRIVER,
    })
    assert result["status"] == "ok", result
    assert result["dispatch"]["status"] == "claimed"
    assert result["dispatch"]["relay_agent"] == relay_agent


def test_mismatched_driver_refused(tmp_path: Path) -> None:
    home = tmp_path / "coord"
    _make_codex_binding(home)
    dispatch = _make_codex_dispatch(home)
    dispatch_id = dispatch["dispatch_id"]
    relay_agent = "codex-ailab-00000000-0000-0000-0000-000000000001"

    client = _client(home)
    identity = {"agent_id": relay_agent}
    result = client.request("wake-relay-claim", identity, {
        "dispatch_id": dispatch_id,
        "relay_agent": relay_agent,
        "driver": RELAY_DRIVER,  # wrong driver for Codex binding
    })
    assert result["status"] == "error"
    assert result["code"] == "relay_driver_mismatch"


def test_mismatched_thread_refused_at_enroll(tmp_path: Path) -> None:
    home = tmp_path / "coord"
    wake = Wake(home=home)
    with pytest.raises(Exception):
        wake.enroll(
            binding_id="codex-bad",
            office_agent="codex-ailab-x-00000000-0000-0000-0000-000000000001",
            driver=CODEX_RELAY_DRIVER,
            native_thread_id=CLAUDE_THREAD,  # local_ prefix not allowed
            workspace=CODEX_WORKSPACE,
            enrolled_by="test",
            allowed_wakers=["*"],
            expires_at="2030-01-01T00:00:00+00:00",
            owner_confirm=True,
        )


def test_foreign_actor_refused(tmp_path: Path) -> None:
    home = tmp_path / "coord"
    _make_codex_binding(home)
    dispatch = _make_codex_dispatch(home)
    dispatch_id = dispatch["dispatch_id"]

    client = _client(home)
    identity = {"agent_id": "codex-ailab-00000000-0000-0000-0000-000000000001"}
    result = client.request("wake-relay-claim", identity, {
        "dispatch_id": dispatch_id,
        "relay_agent": "someone-else-entirely",
        "driver": CODEX_RELAY_DRIVER,
    })
    assert result["status"] == "error"
    assert result["code"] == "identity_mismatch"


@pytest.mark.parametrize("reference_kind", ["fabricated", "foreign_inbox"])
@pytest.mark.parametrize("phase", ["claim", "native_begin"])
def test_trigger_must_be_real_message_in_exact_recipient_inbox(
    tmp_path: Path, reference_kind: str, phase: str
) -> None:
    """A plausible ID, even a real message for another recipient, cannot wake."""
    home = tmp_path / "coord"
    wake = _make_codex_binding(home)
    dispatch = _make_codex_dispatch(home)
    dispatch_id = dispatch["dispatch_id"]
    relay_agent = "codex-ailab-00000000-0000-0000-0000-000000000001"
    client = _client(home)
    identity = {"agent_id": relay_agent}
    if phase == "native_begin":
        claimed = client.request("wake-relay-claim", identity, {
            "dispatch_id": dispatch_id, "relay_agent": relay_agent,
            "driver": CODEX_RELAY_DRIVER,
        })
        assert claimed["status"] == "ok", claimed
    if reference_kind == "fabricated":
        trigger_ref = "f" * 32
    else:
        foreign = Office(
            agent_id="codex-ailab-00000000-0000-0000-0000-000000000003", home=home
        ).send(
            to="codex-ailab-00000000-0000-0000-0000-000000000009",
            body="Synthetic unrelated message", new_recipient=True,
        )
        assert foreign["status"] == "ok", foreign
        trigger_ref = foreign["id"]
    # Alter only the synthetic stored reference. Native-begin must independently
    # revalidate it after a successful claim, rather than trusting prior gates.
    current = wake._load_dispatch(dispatch_id)
    current["trigger_ref"] = trigger_ref
    wake._atomic_replace_json(wake._dispatch_path(dispatch_id), current)
    result = client.request(
        "wake-relay-claim" if phase == "claim" else "wake-relay-native-begin",
        identity, {
            "dispatch_id": dispatch_id, "relay_agent": relay_agent,
            "driver": CODEX_RELAY_DRIVER,
        },
    )
    assert result["status"] == "error", result
    assert result["code"] == "recipient_read_cursor_unavailable", result
    refused = wake._load_dispatch(dispatch_id)
    assert refused["status"] == ("pending" if phase == "claim" else "claimed")
    assert not refused.get("native_attempt_at")
    assert not refused.get("receipt_turn_id")


# -- STOP ------------------------------------------------------------------------

def test_stop_cancels_pending_codex_dispatch(tmp_path: Path) -> None:
    home = tmp_path / "coord"
    wake = _make_codex_binding(home)
    dispatch = _make_codex_dispatch(home)
    dispatch_id = dispatch["dispatch_id"]

    wake.stop()
    result = wake.relay_pending(driver=CODEX_RELAY_DRIVER)
    assert result["count"] == 0

    reloaded = wake._load_dispatch(dispatch_id)
    assert reloaded["status"] == "cancelled"
    assert reloaded["cancel_reason"] == "global_stop"


# -- Busy deferral ----------------------------------------------------------------

def test_busy_deferral_on_codex_dispatch(tmp_path: Path) -> None:
    home = tmp_path / "coord"
    _make_codex_binding(home)
    dispatch = _make_codex_dispatch(home)
    dispatch_id = dispatch["dispatch_id"]

    client = _client(home)
    identity = {"agent_id": "codex-ailab-00000000-0000-0000-0000-000000000001"}
    result = client.request("wake-relay-defer-busy", identity, {
        "dispatch_id": dispatch_id,
        "driver": CODEX_RELAY_DRIVER,
    })
    assert result["status"] == "ok", result
    assert result["dispatch"]["busy_defer_count"] == 1


# -- Duplicate claim ---------------------------------------------------------------

def test_duplicate_claim_refused(tmp_path: Path) -> None:
    home = tmp_path / "coord"
    _make_codex_binding(home)
    dispatch = _make_codex_dispatch(home)
    dispatch_id = dispatch["dispatch_id"]
    relay_agent = "codex-ailab-00000000-0000-0000-0000-000000000001"

    client = _client(home)
    identity = {"agent_id": relay_agent}
    first = client.request("wake-relay-claim", identity, {
        "dispatch_id": dispatch_id,
        "relay_agent": relay_agent,
        "driver": CODEX_RELAY_DRIVER,
    })
    assert first["status"] == "ok"

    second = client.request("wake-relay-claim", identity, {
        "dispatch_id": dispatch_id,
        "relay_agent": relay_agent,
        "driver": CODEX_RELAY_DRIVER,
    })
    assert second["status"] == "error"
    assert second["code"] == "relay_dispatch_fenced"


# -- Claimed crash / native-begin crash --------------------------------------------

def test_claimed_crash_yields_delivery_unknown(tmp_path: Path) -> None:
    base_time = time.time()
    clock = {"t": base_time}
    home = tmp_path / "coord"
    _make_codex_binding(home, now_fn=lambda: clock["t"])
    dispatch = _make_codex_dispatch(home, now_fn=lambda: clock["t"])
    dispatch_id = dispatch["dispatch_id"]
    relay_agent = "codex-ailab-00000000-0000-0000-0000-000000000001"

    wake = Wake(home=home, now=lambda: clock["t"])
    wake.relay_claim(dispatch_id, relay_agent=relay_agent, driver=CODEX_RELAY_DRIVER)

    # Simulate crash: advance clock past claim timeout
    clock["t"] = base_time + DEFAULT_RELAY_CLAIM_TIMEOUT_S + 60

    pending = wake.relay_pending(driver=CODEX_RELAY_DRIVER)
    assert pending["count"] == 0  # not resent

    reloaded = wake._load_dispatch(dispatch_id)
    assert reloaded["status"] == "delivery_unknown"
    assert reloaded["unknown_reason"] == "claim_receipt_timeout"


def test_native_begin_crash_yields_delivery_unknown(tmp_path: Path) -> None:
    base_time = time.time()
    clock = {"t": base_time}
    home = tmp_path / "coord"
    _make_codex_binding(home, now_fn=lambda: clock["t"])
    dispatch = _make_codex_dispatch(home, now_fn=lambda: clock["t"])
    dispatch_id = dispatch["dispatch_id"]
    relay_agent = "codex-ailab-00000000-0000-0000-0000-000000000001"

    wake = Wake(home=home, now=lambda: clock["t"])
    wake.relay_claim(dispatch_id, relay_agent=relay_agent, driver=CODEX_RELAY_DRIVER)
    wake.relay_native_begin(dispatch_id, relay_agent=relay_agent, driver=CODEX_RELAY_DRIVER)

    # After native_begin the dispatch is already fenced as delivery_unknown
    reloaded = wake._load_dispatch(dispatch_id)
    assert reloaded["status"] == "delivery_unknown"
    assert reloaded["unknown_reason"] == "native_submission_unconfirmed"

    # A crash after native_begin: never resent
    clock["t"] = base_time + DEFAULT_RELAY_CLAIM_TIMEOUT_S + 60
    pending = wake.relay_pending(driver=CODEX_RELAY_DRIVER)
    assert pending["count"] == 0


# -- Real receipt -------------------------------------------------------------------

def test_codex_receipt_with_message_id(tmp_path: Path) -> None:
    home = tmp_path / "coord"
    _make_codex_binding(home)
    dispatch = _make_codex_dispatch(home)
    dispatch_id = dispatch["dispatch_id"]
    relay_agent = "codex-ailab-00000000-0000-0000-0000-000000000001"

    wake = Wake(home=home)
    wake.relay_claim(dispatch_id, relay_agent=relay_agent, driver=CODEX_RELAY_DRIVER)
    result = wake.relay_receipt(
        dispatch_id,
        status="delivered",
        message_id=str(uuid.uuid4()),
        driver=CODEX_RELAY_DRIVER,
    )
    assert result["status"] == "ok"
    assert result["dispatch"]["status"] == "delivered"
    assert result["dispatch"]["receipt_status"] == "delivered"
    assert "receipt_message_id" in result["dispatch"]


def test_codex_receipt_with_turn_id_only(tmp_path: Path) -> None:
    """Native tool result may have turnId but no message_id; never fabricate."""
    home = tmp_path / "coord"
    _make_codex_binding(home)
    dispatch = _make_codex_dispatch(home)
    dispatch_id = dispatch["dispatch_id"]
    relay_agent = "codex-ailab-00000000-0000-0000-0000-000000000001"

    wake = Wake(home=home)
    wake.relay_claim(dispatch_id, relay_agent=relay_agent, driver=CODEX_RELAY_DRIVER)
    turn_id = str(uuid.uuid4())
    result = wake.relay_receipt(
        dispatch_id,
        status="delivered",
        turn_id=turn_id,
        driver=CODEX_RELAY_DRIVER,
    )
    assert result["status"] == "ok"
    assert result["dispatch"]["status"] == "delivered"
    assert result["dispatch"]["receipt_turn_id"] == turn_id
    # Never fabricate a message_id
    assert "receipt_message_id" not in result["dispatch"]


# -- Actual stage observation ---------------------------------------------------------

def test_codex_stage_observation(tmp_path: Path) -> None:
    home = tmp_path / "coord"
    _make_codex_binding(home)
    dispatch = _make_codex_dispatch(home)
    dispatch_id = dispatch["dispatch_id"]
    relay_agent = "codex-ailab-00000000-0000-0000-0000-000000000001"

    wake = Wake(home=home)
    wake.relay_claim(dispatch_id, relay_agent=relay_agent, driver=CODEX_RELAY_DRIVER)
    wake.relay_receipt(
        dispatch_id,
        status="delivered",
        message_id=str(uuid.uuid4()),
        driver=CODEX_RELAY_DRIVER,
    )

    started = wake.relay_observe(dispatch_id, stage="target_started", driver=CODEX_RELAY_DRIVER)
    assert started["status"] == "ok"
    assert started["dispatch"]["status"] == "target_started"

    finished = wake.relay_observe(dispatch_id, stage="target_finished", driver=CODEX_RELAY_DRIVER)
    assert finished["status"] == "ok"
    assert finished["dispatch"]["status"] == "target_finished"
    assert "target_started" in finished["dispatch"]["observed_stages"]
    assert "target_finished" in finished["dispatch"]["observed_stages"]


# -- Preserve Claude default -----------------------------------------------------------

def test_claude_default_preserved(tmp_path: Path) -> None:
    home = tmp_path / "coord"
    _make_claude_binding(home)
    dispatch = _make_claude_dispatch(home)
    dispatch_id = dispatch["dispatch_id"]
    relay_agent = "claude-ailab-00000000-0000-0000-0000-000000000002"

    client = _client(home)
    identity = {"agent_id": relay_agent}

    # Default driver is Claude
    claim = client.request("wake-relay-claim", identity, {
        "dispatch_id": dispatch_id,
        "relay_agent": relay_agent,
    })
    assert claim["status"] == "ok", claim
    assert claim["dispatch"]["status"] == "claimed"

    receipt = client.request("wake-relay-receipt", identity, {
        "dispatch_id": dispatch_id,
        "status": "delivered",
        "message_id": str(uuid.uuid4()),
    })
    assert receipt["status"] == "ok", receipt
    assert receipt["dispatch"]["status"] == "delivered"


def test_unknown_driver_refused(tmp_path: Path) -> None:
    home = tmp_path / "coord"
    _make_codex_binding(home)
    dispatch = _make_codex_dispatch(home)
    dispatch_id = dispatch["dispatch_id"]
    relay_agent = "codex-ailab-00000000-0000-0000-0000-000000000001"

    client = _client(home)
    identity = {"agent_id": relay_agent}
    result = client.request("wake-relay-claim", identity, {
        "dispatch_id": dispatch_id,
        "relay_agent": relay_agent,
        "driver": "unknown-driver-name",
    })
    assert result["status"] == "error"
    assert result["code"] == "invalid_args"
