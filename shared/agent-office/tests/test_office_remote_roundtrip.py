"""In-process W2 protocol round trip; no SSH connection or real credentials."""

from __future__ import annotations

import io
import json
import threading
from pathlib import Path

from coordfs.office_remote import OfficeRemoteClient, OfficeRemoteConfig
from coordfs.office_ssh_gate import run as run_gate


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


def _client(home: Path) -> OfficeRemoteClient:
    config = OfficeRemoteConfig(
        host="office.example.invalid", user="agentoffice",
        identity_file="unused-test-key", known_hosts_file="unused-test-hosts",
        request_timeout_s=5,
    )
    return OfficeRemoteClient(config, popen_factory=lambda *_args, **_kwargs: _GateProcess(home))


def test_send_and_read_across_remote_protocol_preserves_authenticated_principal(tmp_path: Path) -> None:
    client = _client(tmp_path / "coord")
    sent = client.request("send", {"harness": "codex", "host": "ailab", "session_id": "test-a"},
                          {"channel": "ops", "body": "synthetic W2 round trip"})
    assert sent["status"] == "ok"

    received = client.request("read", {"harness": "claude", "host": "lab-server", "session_id": "test-b"},
                              {"channel": "ops", "peek": True})
    assert received["status"] == "ok"
    message = received["messages"][0]["untrusted_message"]
    assert message["body"] == "synthetic W2 round trip"
    assert message["from"] == "codex-ailab-test-a"
    assert message["transport_principal"] == "ws-office"


def test_real_gate_refusal_survives_client_transport_without_a_local_write(tmp_path: Path) -> None:
    home = tmp_path / "coord"
    result = _client(home).request(
        "send", {"harness": "codex", "host": "ailab", "session_id": "test-refusal"},
        {"channel": "ops", "body": "synthetic handoff without refs", "kind": "handoff"},
    )

    assert result == {"status": "error", "code": "refs_required"}
    assert not home.exists()


def test_mcp_framing_round_trip_after_remote_header(tmp_path: Path) -> None:
    client = _client(tmp_path / "coord")
    source = io.BytesIO(json.dumps({"jsonrpc": "2.0", "id": 7, "method": "tools/list"}).encode() + b"\n")
    output = io.BytesIO()

    status = client.relay_mcp({"agent_id": "codex-ailab-test-mcp"},
                              input_stream=source, output_stream=output)

    assert status == {"status": "ok"}
    listed = json.loads(output.getvalue())
    assert listed["id"] == 7
    assert any(tool["name"] == "office_read" for tool in listed["result"]["tools"])
