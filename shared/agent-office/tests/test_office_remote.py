from __future__ import annotations

import io
import json
import subprocess
import threading

import pytest

from coordfs.office_remote import (
    IO_CHUNK_BYTES,
    MAX_REQUEST_BYTES,
    MAX_RESPONSE_BYTES,
    OfficeRemoteClient,
    OfficeRemoteConfig,
    build_request,
)


class RecordingStdin:
    def __init__(self) -> None:
        self.data = bytearray()
        self.closed = False
        self.closed_event = threading.Event()
        self.write_sizes: list[int] = []

    def write(self, data: bytes | memoryview) -> int:
        if self.closed:
            raise BrokenPipeError("closed")
        payload = bytes(data)
        self.write_sizes.append(len(payload))
        self.data.extend(payload)
        return len(payload)

    def flush(self) -> None:
        pass

    def close(self) -> None:
        self.closed = True
        self.closed_event.set()


class FakeProcess:
    def __init__(
        self,
        response: bytes = b'{"status":"ok"}',
        *,
        returncode: int = 0,
        timeout: bool = False,
        wait_for_stdin_eof: bool = False,
    ) -> None:
        self.stdin = RecordingStdin()
        self.stdout = io.BytesIO(response)
        self.returncode = returncode
        self.timeout = timeout
        self.wait_for_stdin_eof = wait_for_stdin_eof
        self.killed = False
        self.wait_calls = 0
        self.last_wait_timeout: float | None = None

    def wait(self, timeout: float | None = None) -> int:
        self.wait_calls += 1
        self.last_wait_timeout = timeout
        if self.timeout and not self.killed:
            raise subprocess.TimeoutExpired("fake-ssh", timeout)
        if self.wait_for_stdin_eof and not self.stdin.closed_event.wait(3):
            raise subprocess.TimeoutExpired("fake-ssh", timeout)
        return self.returncode

    def kill(self) -> None:
        self.killed = True
        self.returncode = -9
        self.stdin.close()


class SyntheticInput:
    """Produces bytes lazily so the relay test does not retain session history."""

    def __init__(self, byte_count: int) -> None:
        self.remaining = byte_count
        self.read_sizes: list[int] = []

    def read1(self, size: int) -> bytes:
        self.read_sizes.append(size)
        take = min(size, self.remaining)
        self.remaining -= take
        return b"x" * take


class RecordingOutput:
    def __init__(self) -> None:
        self.data = bytearray()
        self.flush_count = 0

    def write(self, data: bytes | memoryview) -> int:
        payload = bytes(data)
        self.data.extend(payload)
        return len(payload)

    def flush(self) -> None:
        self.flush_count += 1


def make_config(**overrides: object) -> OfficeRemoteConfig:
    values: dict[str, object] = {
        "host": "office.example.test",
        "user": "agentoffice",
        "identity_file": r"C:\keys\ws-office",
        "known_hosts_file": r"C:\keys\office_known_hosts",
        "request_timeout_s": 1.0,
    }
    values.update(overrides)
    return OfficeRemoteConfig(**values)  # type: ignore[arg-type]


def test_request_framing_and_ssh_security_options(monkeypatch: pytest.MonkeyPatch) -> None:
    process = FakeProcess(b'{"status":"accepted"}\n')
    call: dict[str, object] = {}

    def fake_popen(argv: list[str], **kwargs: object) -> FakeProcess:
        call["argv"] = argv
        call.update(kwargs)
        return process

    monkeypatch.setenv("SSH_ORIGINAL_COMMAND", "must-not-be-forwarded")
    client = OfficeRemoteClient(make_config(), popen_factory=fake_popen)
    result = client.request("presence", {"harness": "codex", "session_id": "synthetic"}, {"status": "busy"})

    assert result == {"status": "accepted"}
    assert bytes(process.stdin.data).endswith(b"\n")
    assert len(process.stdin.data) <= MAX_REQUEST_BYTES
    request = json.loads(process.stdin.data)
    assert request == {
        "v": 1,
        "op": "presence",
        "identity": {"harness": "codex", "session_id": "synthetic"},
        "args": {"status": "busy"},
    }

    argv = call["argv"]
    assert isinstance(argv, list)
    assert argv[-2:] == ["--", "agentoffice@office.example.test"]
    assert "-F" in argv and argv[argv.index("-F") + 1] == "none"
    options = [argv[index + 1] for index, value in enumerate(argv[:-1]) if value == "-o"]
    assert "BatchMode=yes" in options
    assert "StrictHostKeyChecking=yes" in options
    assert "IdentityAgent=none" in options
    assert r"UserKnownHostsFile=C:\keys\office_known_hosts" in options
    assert "GlobalKnownHostsFile=none" in options
    assert r"C:\keys\ws-office" in argv
    assert call["shell"] is False
    assert call["stderr"] == subprocess.DEVNULL
    assert "SSH_ORIGINAL_COMMAND" not in call["env"]  # type: ignore[operator]
    assert process.stdin.closed


def test_operation_allowlist_and_request_size_are_enforced() -> None:
    with pytest.raises(ValueError, match="operation is not allowed"):
        build_request("wake-stop", {}, {})
    with pytest.raises(ValueError, match="request exceeds size limit"):
        build_request("send", {}, {"body": "x" * MAX_REQUEST_BYTES})


def test_wait_timeout_is_capped_even_with_caller_timeout_override() -> None:
    client = OfficeRemoteClient(make_config(), popen_factory=lambda *_args, **_kwargs: pytest.fail("must not spawn"))

    with pytest.raises(ValueError, match="wait timeout is out of bounds"):
        client.request("wait", {}, {"timeout_s": 241}, timeout_s=0.1)


def test_relay_watch_keeps_a_long_event_wait_open_without_empty_model_turns() -> None:
    process = FakeProcess()
    client = OfficeRemoteClient(make_config(), popen_factory=lambda *_args, **_kwargs: process)

    result = client.request("wake-relay-watch", {"agent_id": "relay"},
                            {"binding_id": ["relay-canary-s"], "timeout_s": 3600})

    assert result == {"status": "ok"}
    assert process.last_wait_timeout is not None
    assert process.last_wait_timeout >= 3610


def test_relay_watch_rejects_unbounded_timeout_before_spawning() -> None:
    client = OfficeRemoteClient(make_config(), popen_factory=lambda *_args, **_kwargs: pytest.fail("must not spawn"))

    with pytest.raises(ValueError, match="watch timeout is out of bounds"):
        client.request("wake-relay-watch", {}, {"timeout_s": 3601})
    with pytest.raises(ValueError, match="watch timeout is out of bounds"):
        client.request("wake-relay-watch", {}, {"timeout_s": 0})


def test_ssh_start_failure_returns_generic_degraded_without_details() -> None:
    def unavailable(*_args: object, **_kwargs: object) -> FakeProcess:
        raise OSError("private path or user data must not leak")

    result = OfficeRemoteClient(make_config(), popen_factory=unavailable).request("who", {}, {})
    assert result == {"status": "degraded", "reason": "ssh_unavailable"}
    assert "private path" not in repr(result)


def test_nonzero_ssh_exit_returns_generic_degraded() -> None:
    process = FakeProcess(b"ssh error with private data", returncode=255)
    client = OfficeRemoteClient(make_config(), popen_factory=lambda *_args, **_kwargs: process)

    result = client.request("who", {}, {})

    assert result == {"status": "degraded", "reason": "ssh_unavailable"}
    assert "private data" not in repr(result)


def test_gate_semantic_refusal_keeps_its_code_on_exit_one() -> None:
    process = FakeProcess(b'{"status":"error","code":"identity_mismatch"}\n', returncode=1)
    client = OfficeRemoteClient(make_config(), popen_factory=lambda *_args, **_kwargs: process)

    assert client.request("who", {}, {}) == {"status": "error", "code": "identity_mismatch"}


@pytest.mark.parametrize("response", [
    b'{"status":"ok","code":"identity_mismatch"}',
    b'{"status":"error","code":"private path /secret"}',
    b'{"status":"error","code":"identity_mismatch","detail":"private data"}',
    b'{"status":"error"}',
    b'not-json',
])
def test_exit_one_requires_the_exact_sanitized_gate_error_shape(response: bytes) -> None:
    process = FakeProcess(response, returncode=1)
    client = OfficeRemoteClient(make_config(), popen_factory=lambda *_args, **_kwargs: process)

    assert client.request("who", {}, {}) == {"status": "degraded", "reason": "invalid_response"}


def test_ssh_failure_cannot_masquerade_as_a_gate_refusal() -> None:
    process = FakeProcess(b'{"status":"error","code":"identity_mismatch"}', returncode=255)
    client = OfficeRemoteClient(make_config(), popen_factory=lambda *_args, **_kwargs: process)

    assert client.request("who", {}, {}) == {"status": "degraded", "reason": "ssh_unavailable"}


def test_ssh_timeout_returns_degraded_without_stderr() -> None:
    process = FakeProcess(timeout=True)
    client = OfficeRemoteClient(make_config(), popen_factory=lambda *_args, **_kwargs: process)

    result = client.request("read", {}, {}, timeout_s=0.05)

    assert result == {"status": "degraded", "reason": "timeout"}
    assert process.killed


@pytest.mark.parametrize(
    "response",
    [b"not-json", b'{"one":1}{"two":2}', b"[]", b" " * (MAX_RESPONSE_BYTES + 1)],
    ids=["not-json", "multiple-json", "json-list", "oversized"],
)
def test_invalid_or_multiple_responses_are_rejected(response: bytes) -> None:
    process = FakeProcess(response)
    client = OfficeRemoteClient(make_config(), popen_factory=lambda *_args, **_kwargs: process)

    result = client.request("channels", {}, {})

    assert result == {"status": "degraded", "reason": "invalid_response"}


def test_mcp_relay_streams_bounded_binary_chunks_until_eof() -> None:
    process = FakeProcess(b"remote-mcp-bytes", wait_for_stdin_eof=True)
    source = SyntheticInput(IO_CHUNK_BYTES * 3 + 17)
    sink = RecordingOutput()
    client = OfficeRemoteClient(make_config(), popen_factory=lambda *_args, **_kwargs: process)

    result = client.relay_mcp(
        {"harness": "codex", "session_id": "synthetic"},
        input_stream=source,  # type: ignore[arg-type]
        output_stream=sink,  # type: ignore[arg-type]
    )

    header, separator, body = bytes(process.stdin.data).partition(b"\n")
    assert separator == b"\n"
    assert json.loads(header)["op"] == "mcp"
    assert len(body) == IO_CHUNK_BYTES * 3 + 17
    assert max(source.read_sizes) <= IO_CHUNK_BYTES
    assert max(process.stdin.write_sizes) <= IO_CHUNK_BYTES
    assert bytes(sink.data) == b"remote-mcp-bytes"
    assert sink.flush_count > 0
    assert process.stdin.closed
    assert result == {"status": "ok"}
