"""W2 remote protocol parity: closed unary tool-call and the six ordinary Wake ops.

Real in-process forced-command gate (no SSH, no credentials) behind the actual
OfficeRemoteClient framing, plus client-side timeout accounting tests. Every
Office home is a synthetic fresh tmp_path.
"""

from __future__ import annotations

import io
import json
import threading
import time
from pathlib import Path

import pytest

from test_office_remote import FakeProcess, make_config

from coordfs import office_ssh_gate
from coordfs.leases import format_ts
from coordfs.office import Office
from coordfs.office_remote import (
    MAX_CALL_TIMEOUT_S,
    MAX_RELAY_WATCH_CALL_TIMEOUT_S,
    MAX_REQUEST_BYTES,
    OfficeRemoteClient,
    OfficeRemoteConfig,
)
from coordfs.office_ssh_gate import run as run_gate
from coordfs.wake import PULL_DRIVER, Wake


# -- in-process real gate behind the real client transport ---------------------

class _ResponsePipe:
    def __init__(self) -> None:
        self.ready = threading.Event()
        self.data = b""
        self.offset = 0

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
        # Match the real pipe API used by the accepted client cleanup path.
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


def _client(home: Path, factory: _GateFactory | None = None, **overrides: object) -> OfficeRemoteClient:
    config = OfficeRemoteConfig(
        host="office.example.invalid", user="agentoffice",
        identity_file="unused-test-key", known_hosts_file="unused-test-hosts",
        request_timeout_s=5, **overrides,  # type: ignore[arg-type]
    )
    return OfficeRemoteClient(config, popen_factory=factory or _GateFactory(home))


def _raw_gate(home: Path, op: str, args: dict, *,
              identity: dict | None = None, principal: str = "ws-office",
              tail: bytes = b"") -> tuple[int, dict]:
    header = {"v": 1, "op": op, "identity": identity or {"agent_id": "codex-testhost-alice"}, "args": args}
    stdout = io.BytesIO()
    code = run_gate(io.BytesIO(json.dumps(header).encode("utf-8") + b"\n" + tail), stdout,
                    principal=principal, home=home, environ={})
    return code, json.loads(stdout.getvalue())


# -- tool-call through the real gate -------------------------------------------

def test_tool_call_client_framing_and_sender_invariants_through_real_gate(tmp_path: Path) -> None:
    home = tmp_path / "coord"
    factory = _GateFactory(home)
    client = _client(home, factory)
    identity = {"harness": "kimi", "host": "ailab", "session_id": "test-sender"}

    sent = client.request("tool-call", identity, {
        "name": "office_send",
        "arguments": {"channel": "ops", "body": "synthetic unary tool call"},
    })

    assert sent["status"] == "ok", sent
    assert sent["id"]
    frame = bytes(factory.processes[0].stdin.data)
    assert frame.endswith(b"\n") and len(frame) <= MAX_REQUEST_BYTES
    assert json.loads(frame) == {
        "v": 1,
        "op": "tool-call",
        "identity": identity,
        "args": {"name": "office_send",
                 "arguments": {"channel": "ops", "body": "synthetic unary tool call"}},
    }

    received = client.request("read", {"harness": "codex", "host": "ailab", "session_id": "test-peer"},
                              {"channel": "ops", "peek": True})
    envelope = received["messages"][0]["untrusted_message"]
    assert envelope["body"] == "synthetic unary tool call"
    assert envelope["from"] == "kimi-ailab-test-sender"
    assert envelope["transport_principal"] == "ws-office"


def test_tool_call_zero_wait_is_a_legal_poll_through_real_gate(tmp_path: Path) -> None:
    client = _client(tmp_path / "coord")
    result = client.request("tool-call", {"agent_id": "kimi-ailab-test-waiter"},
                            {"name": "office_wait", "arguments": {"timeout_s": 0}})
    assert result["status"] == "ok", result


def test_tool_call_structured_errors_stay_sanitized(tmp_path: Path) -> None:
    client = _client(tmp_path / "coord")
    identity = {"agent_id": "kimi-ailab-test-errors"}

    unknown_args = client.request("tool-call", identity, {
        "name": "office_send",
        "arguments": {"channel": "ops", "body": "x", "shell": "rm -rf /"},
    })
    assert unknown_args == {"status": "error", "code": "invalid_params"}

    spoofed = client.request("tool-call", identity, {
        "name": "office_send",
        "arguments": {"channel": "ops", "body": "x", "transport_principal": "hermes"},
    })
    assert spoofed == {"status": "error", "code": "forbidden_field"}


def test_tool_call_unknown_admin_and_malformed_calls_fail_before_any_write(tmp_path: Path) -> None:
    home = tmp_path / "coord"
    client = _client(home)
    identity = {"agent_id": "codex-testhost-alice"}
    cases = [
        ({"name": "office_secret_rotate", "arguments": {}}, "unknown_tool"),
        ({"name": "office_wake_intake", "arguments": {}}, "unknown_tool"),
        ({"name": "office_wake_close; rm -rf /", "arguments": {}}, "unknown_tool"),
        ({"name": "office_who", "arguments": {}, "profile": "contractor"}, "invalid_args"),
    ]
    for args, code in cases:
        result = client.request("tool-call", identity, args)
        assert result == {"status": "error", "code": code}, (args, result)
        assert not home.exists()

    # Malformed shapes the client also refuses locally must still fail closed at
    # the gate itself, before any state access.
    for args in ({"name": "office_who"},
                 {"name": "office_who", "arguments": ["not", "a", "dict"]},
                 {"name": 7, "arguments": {}}):
        code, result = _raw_gate(home, "tool-call", args)
        assert (code, result["code"]) == (1, "invalid_args"), args
        assert not home.exists()


def test_owner_and_admin_operations_stay_outside_the_remote_surface(tmp_path: Path) -> None:
    home = tmp_path / "coord"
    for op in ("wake-intake", "wake-verify", "wake-enroll", "wake-stop", "wake-start",
               "wake-tick", "wake-pull", "wake-pull-arm", "wake-pull-confirm",
               "wake-relay-expect", "wake-monitor"):
        code, result = _raw_gate(home, op, {})
        assert (code, result["code"]) == (1, "unknown_op"), op
        assert not home.exists()
    code, result = _raw_gate(home, "tool-call",
                             {"name": "office_who", "arguments": {}}, tail=b"{}\n")
    assert (code, result["code"]) == (1, "trailing_data")
    assert not home.exists()


def test_tool_call_identity_substitution_is_refused_before_dispatch(tmp_path: Path) -> None:
    home = tmp_path / "coord"
    code, result = _raw_gate(home, "tool-call", {"name": "office_who", "arguments": {}},
                             identity={"agent_id": "codex-testhost-alice"}, principal="hermes")
    assert (code, result["code"]) == (1, "identity_mismatch")
    assert not home.exists()


def test_tool_call_new_tool_requires_explicit_remote_review(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(office_ssh_gate, "TOOLS",
                        [*office_ssh_gate.TOOLS, {"name": "office_secret_rotate"}])
    result = _client(tmp_path / "coord").request(
        "tool-call", {"agent_id": "codex-testhost-alice"}, {"name": "office_who", "arguments": {}})
    assert result == {"status": "error", "code": "mcp_tools_refused"}


# -- the six ordinary Wake ops through the real gate ----------------------------

def test_wake_read_ops_on_a_fresh_home(tmp_path: Path) -> None:
    home = tmp_path / "coord"
    client = _client(home)
    identity = {"agent_id": "kimi-ailab-test-wake"}

    listed = client.request("wake-list", identity, {})
    assert (listed["status"], listed["bindings"]) == ("ok", [])

    correlations = client.request("wake-correlations", identity, {})
    assert (correlations["status"], correlations["correlations"], correlations["open"]) == ("ok", [], 0)

    stop = client.request("wake-stop-status", identity, {})
    assert (stop["status"], stop["stop_active"]) == ("ok", False)

    ledger = client.request("wake-ledger", identity, {})
    assert (ledger["status"], ledger["entries"], ledger["count"]) == ("ok", [], 0)
    bounded = client.request("wake-ledger", identity, {"limit": 1000, "binding_id": "lead-pull"})
    assert bounded["status"] == "ok"

    missing_binding = client.request("wake-pull-disarm", identity, {"binding_id": "no-such-binding"})
    assert missing_binding == {"status": "error", "code": "wake_binding_not_found"}

    # The task lock lives under the wake dirs, which exist on a live home after
    # any enrollment; cancel_task takes that lock before the not-found refusal.
    Wake(home=home)._ensure_dirs()
    missing_task = client.request("wake-task-cancel", identity, {"task_id": "no-such-task"})
    assert missing_task == {"status": "error", "code": "wake_task_not_found"}


def test_wake_ops_reject_invalid_args_before_state_access(tmp_path: Path) -> None:
    home = tmp_path / "coord"
    client = _client(home)
    identity = {"agent_id": "codex-testhost-alice"}
    cases = [
        ("wake-list", {"verbose": True}),
        ("wake-correlations", {"open": True}),
        ("wake-stop-status", {"tick": True}),
        ("wake-task-cancel", {}),
        ("wake-task-cancel", {"task_id": 7}),
        ("wake-pull-disarm", {}),
        ("wake-pull-disarm", {"binding_id": ["lead-pull"]}),
        ("wake-ledger", {"limit": 0}),
        ("wake-ledger", {"limit": True}),
        ("wake-ledger", {"limit": 1001}),
        ("wake-ledger", {"limit": "50"}),
        ("wake-ledger", {"binding_id": 7}),
    ]
    for op, args in cases:
        result = client.request(op, identity, args)
        assert result == {"status": "error", "code": "invalid_args"}, (op, args, result)
        assert not home.exists()


def _enroll_verified_pull_binding(home: Path, agent: str, workspace: Path) -> None:
    workspace.mkdir(parents=True, exist_ok=True)
    wake = Wake(home=home)
    enrolled = wake.enroll(
        binding_id="lead-pull", office_agent=agent, driver=PULL_DRIVER,
        native_thread_id="local_123e4567-e89b-12d3-a456-426614174000",
        workspace=str(workspace), enrolled_by="owner",
        allowed_wakers=["claude-ailab-peer"], allowed_triggers=["message"],
        wake_kinds=["question", "handoff"], budget_per_day=20, max_attempts_per_trigger=1,
        expires_at=format_ts(time.time() + 86400), owner_confirm=True,
        correlation_required=True,
    )
    assert enrolled["status"] == "ok", enrolled
    verified = wake.verify("lead-pull", owner_confirm=True)
    assert verified["status"] == "ok", verified


def test_wake_caller_owned_operations_keep_server_side_caller(tmp_path: Path) -> None:
    home = tmp_path / "coord"
    owner = "kimi-ailab-test-wake"
    peer = "claude-ailab-peer"
    _enroll_verified_pull_binding(home, owner, tmp_path / "workspace")
    Office(agent_id=peer, home=home).presence(ttl_s=86400, status="busy")
    sent = Office(agent_id=owner, home=home).send(peer, "synthetic wake request", kind="question")
    expected = Wake(home=home).expect(binding_id="lead-pull", caller=owner, task_id="t1",
                                      correlation_id="c1", counterpart=peer,
                                      request_message_id=sent["id"], max_wakes=3)
    assert expected["status"] == "ok", expected

    client = _client(home)

    listed = client.request("wake-list", {"agent_id": peer}, {})
    assert listed["count"] == 1
    assert listed["bindings"][0]["binding_id"] == "lead-pull"

    forbidden_cancel = client.request("wake-task-cancel", {"agent_id": peer}, {"task_id": "t1"})
    assert forbidden_cancel == {"status": "error", "code": "wake_correlation_forbidden"}

    forbidden_disarm = client.request("wake-pull-disarm", {"agent_id": peer},
                                      {"binding_id": "lead-pull"})
    assert forbidden_disarm == {"status": "error", "code": "wake_pull_forbidden"}

    cancelled = client.request("wake-task-cancel", {"agent_id": owner}, {"task_id": "t1"})
    assert cancelled["status"] == "ok", cancelled
    assert cancelled["code"] == "wake_task_cancelled"
    assert "c1" in cancelled["cancelled_correlations"]

    correlations = client.request("wake-correlations", {"agent_id": owner}, {})
    assert any(row["correlation_id"] == "c1" and row["status"] == "cancelled"
               for row in correlations["correlations"])

    disarmed = client.request("wake-pull-disarm", {"agent_id": owner}, {"binding_id": "lead-pull"})
    assert (disarmed["status"], disarmed["code"], disarmed["changed"]) == (
        "ok", "wake_pull_disarmed", False)


# -- client timeout accounting ---------------------------------------------------

def _timeout_client(**overrides: object) -> tuple[OfficeRemoteClient, FakeProcess]:
    process = FakeProcess()
    client = OfficeRemoteClient(make_config(**overrides),
                                popen_factory=lambda *_args, **_kwargs: process)
    return client, process


def _no_spawn_client() -> OfficeRemoteClient:
    return OfficeRemoteClient(make_config(),
                              popen_factory=lambda *_args, **_kwargs: pytest.fail("must not spawn"))


def test_tool_call_blocking_wait_hint_bounds() -> None:
    client, process = _timeout_client()
    assert client.request("tool-call", {"agent_id": "a"},
                          {"name": "office_wait", "arguments": {"timeout_s": 0}}) == {"status": "ok"}
    assert process.last_wait_timeout == 10.0  # zero poll plus one connect budget

    client, process = _timeout_client()
    client.request("tool-call", {"agent_id": "a"}, {"name": "office_wait", "arguments": {}})
    assert process.last_wait_timeout == 40.0  # local default 30 s plus one connect budget

    client, process = _timeout_client()
    client.request("tool-call", {"agent_id": "a"},
                   {"name": "office_project_wait_channel", "arguments": {"timeout_s": 240}})
    assert process.last_wait_timeout == 250.0

    client, process = _timeout_client(connect_timeout_s=60)
    client.request("tool-call", {"agent_id": "a"},
                   {"name": "office_project_wait", "arguments": {"timeout_s": 240}})
    assert process.last_wait_timeout == MAX_CALL_TIMEOUT_S == 300.0

    client, process = _timeout_client()
    client.request("tool-call", {"agent_id": "a"},
                   {"name": "office_send", "arguments": {"body": "x"}})
    assert process.last_wait_timeout == 1.0  # no hint for a non-blocking tool


def test_tool_call_office_ask_wait_hint_bounds() -> None:
    # office_ask writes its question before it blocks; a plain 30 s client
    # timeout would kill the transport after the write and report a failure.
    client, process = _timeout_client()
    assert client.request("tool-call", {"agent_id": "a"},
                          {"name": "office_ask", "arguments": {"body": "q", "timeout_s": 0}}
                          ) == {"status": "ok"}
    assert process.last_wait_timeout == 10.0  # zero poll plus one connect budget

    client, process = _timeout_client()
    client.request("tool-call", {"agent_id": "a"},
                   {"name": "office_ask", "arguments": {"body": "q"}})
    assert process.last_wait_timeout == 40.0  # local default 30 s plus one connect

    client, process = _timeout_client()
    client.request("tool-call", {"agent_id": "a"},
                   {"name": "office_ask", "arguments": {"body": "q", "timeout_s": 240}})
    assert process.last_wait_timeout == 250.0

    client, process = _timeout_client(connect_timeout_s=60)
    client.request("tool-call", {"agent_id": "a"},
                   {"name": "office_ask", "arguments": {"body": "q", "timeout_s": 240}})
    assert process.last_wait_timeout == MAX_CALL_TIMEOUT_S == 300.0

    client = _no_spawn_client()
    with pytest.raises(ValueError, match="wait timeout is out of bounds"):
        client.request("tool-call", {"agent_id": "a"},
                       {"name": "office_ask", "arguments": {"body": "q", "timeout_s": 241}})


def test_tool_call_rejects_out_of_bounds_hints_before_spawning() -> None:
    client = _no_spawn_client()
    for arguments in ({"timeout_s": 241}, {"timeout_s": -1}, {"timeout_s": True},
                      {"timeout_s": "30"}):
        with pytest.raises(ValueError, match="wait timeout is out of bounds"):
            client.request("tool-call", {"agent_id": "a"},
                           {"name": "office_wait", "arguments": arguments})
    for args in ({"name": "office_wait"},
                 {"name": "office_wait", "arguments": ["not-a-dict"]},
                 {"arguments": {}}):
        with pytest.raises(ValueError, match="tool-call requires"):
            client.request("tool-call", {"agent_id": "a"}, args)


def test_relay_watch_counts_the_connect_budget_once() -> None:
    client, process = _timeout_client()
    client.request("wake-relay-watch", {"agent_id": "relay"},
                   {"binding_id": ["relay-canary-s"], "timeout_s": 3600})
    assert process.last_wait_timeout == 3620.0  # watch + 10 s slack + one connect

    client, process = _timeout_client(connect_timeout_s=60)
    client.request("wake-relay-watch", {"agent_id": "relay"},
                   {"binding_id": ["relay-canary-s"], "timeout_s": 3600})
    assert process.last_wait_timeout == MAX_RELAY_WATCH_CALL_TIMEOUT_S == 3670.0
