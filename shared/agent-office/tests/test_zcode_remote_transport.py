"""Synthetic end-to-end tests for ZCode's closed remote desktop-input lease RPCs."""

from __future__ import annotations

import io
import json
import sys
import threading
import time
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
if str(PACKAGE_ROOT) not in sys.path:
    sys.path.insert(0, str(PACKAGE_ROOT))

from coordfs.desktop_adapters import USER_IDLE_REQUIRED_S  # noqa: E402
from coordfs.leases import CoordBoard, lease_digest, format_ts  # noqa: E402
from coordfs.office import Office  # noqa: E402
from coordfs.office_remote import OfficeRemoteClient, OfficeRemoteConfig  # noqa: E402
from coordfs.office_ssh_gate import run as run_gate  # noqa: E402
from coordfs.zcode_desktop_tool_host import (  # noqa: E402
    TargetRegistry,
    ZCodeDesktopTarget,
    ZCodeDesktopToolHost,
)
from coordfs.wake import Wake, ZCODE_RELAY_DRIVER  # noqa: E402


SESSION = "sess_c68ed66d-0fe5-4746-82b3-48fe201bd47d"
OTHER_SESSION = "sess_99999999-8888-4777-8666-555555555555"
RELAY = "codex-ailab-01a0ac55-48ac-7742-9b18-8fa624150d4b"
FOREIGN_RELAY = "codex-ailab-ffffffff-ffff-4fff-8fff-ffffffffffff"
BINDING = "zcode-desktop-target"
OTHER_BINDING = "zcode-desktop-target-other"


class _ResponsePipe:
    def __init__(self) -> None:
        self.ready = threading.Event()
        self.data = b""
        self.offset = 0

    def supply(self, data: bytes) -> None:
        self.data = data
        self.ready.set()

    def read(self, size: int = -1) -> bytes:
        assert self.ready.wait(3), "synthetic SSH gate did not finish"
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
    def __init__(self, server: "_GateServer") -> None:
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


class _GateServer:
    """Runs the real SSH gate and Wake code with synthetic stdio only."""

    def __init__(self, home: Path) -> None:
        self.home = home
        self.frames: list[dict] = []

    def handle(self, raw: bytes) -> bytes:
        frame = json.loads(raw.split(b"\n", 1)[0].decode("utf-8"))
        self.frames.append(frame)
        stdout = io.BytesIO()
        code = run_gate(
            io.BytesIO(raw), stdout, principal="ws-office", home=self.home, environ={}
        )
        assert code in (0, 1)
        return stdout.getvalue()


def _remote_client(server: _GateServer) -> OfficeRemoteClient:
    config = OfficeRemoteConfig(
        host="office.example.invalid",
        user="agentoffice",
        identity_file="synthetic-key",
        known_hosts_file="synthetic-known-hosts",
        request_timeout_s=5,
    )
    return OfficeRemoteClient(
        config,
        popen_factory=lambda *_args, **_kwargs: _SyntheticProcess(server),
    )


def _enroll(home: Path, binding_id: str, native_session_id: str, office_agent: str) -> None:
    wake = Wake(home=home)
    wake.enroll(
        binding_id=binding_id,
        office_agent=office_agent,
        driver=ZCODE_RELAY_DRIVER,
        native_thread_id=native_session_id,
        workspace=r"D:\synthetic\zcode-target",
        enrolled_by="synthetic-owner-test",
        allowed_wakers=[RELAY],
        allowed_triggers=["message"],
        wake_kinds=["question"],
        expires_at=format_ts(time.time() + 3600),
        owner_confirm=True,
    )
    wake.verify(binding_id, owner_confirm=True)


def _claim(client: OfficeRemoteClient, binding_id: str, actor: str = RELAY) -> dict:
    return client.request(
        "wake-desktop-input-claim",
        {"agent_id": actor},
        {"binding_id": binding_id, "ttl_s": 180},
    )


def _release(
    client: OfficeRemoteClient, binding_id: str, digest: str, actor: str = RELAY
) -> dict:
    return client.request(
        "wake-desktop-input-release",
        {"agent_id": actor},
        {"binding_id": binding_id, "expected_lease_digest": digest},
    )


def test_real_remote_client_gate_claim_release_is_fenced_by_binding(tmp_path: Path) -> None:
    home = tmp_path / "office"
    _enroll(home, BINDING, SESSION, "zcode-ailab-" + SESSION)
    _enroll(home, OTHER_BINDING, OTHER_SESSION, "zcode-ailab-" + OTHER_SESSION)
    server = _GateServer(home)
    client = _remote_client(server)

    claimed = _claim(client, BINDING)
    assert (claimed["status"], claimed["code"]) == ("ok", "claimed")
    lease = claimed["lease"]
    assert lease["resource_id"] == "data:desktop-input"
    assert lease["holder_agent"] == RELAY
    assert lease["meta"] == {"binding_id": BINDING, "actor": RELAY}
    digest = lease_digest(lease)
    assert claimed["lease_digest"] == digest

    wrong_scope = _release(client, OTHER_BINDING, digest)
    assert (wrong_scope["status"], wrong_scope["code"]) == (
        "conflict",
        "lease_meta_mismatch",
    )
    still_held = CoordBoard(home=home).who("data:desktop-input", kind="data")
    assert still_held["holder_agent"] == RELAY
    assert still_held["lease"]["meta"] == {"binding_id": BINDING, "actor": RELAY}

    released = _release(client, BINDING, digest)
    assert (released["status"], released["code"]) == ("ok", "released")
    assert CoordBoard(home=home).who("data:desktop-input", kind="data")["holder_agent"] is None
    assert [frame["op"] for frame in server.frames] == [
        "wake-desktop-input-claim",
        "wake-desktop-input-release",
        "wake-desktop-input-release",
    ]


def test_real_remote_client_gate_rejects_foreign_actor_for_claim_and_release(
    tmp_path: Path,
) -> None:
    home = tmp_path / "office"
    _enroll(home, BINDING, SESSION, "zcode-ailab-" + SESSION)
    server = _GateServer(home)
    client = _remote_client(server)

    foreign_claim = _claim(client, BINDING, FOREIGN_RELAY)
    assert (foreign_claim["status"], foreign_claim["code"]) == (
        "error",
        "identity_mismatch",
    )
    assert CoordBoard(home=home).who("data:desktop-input", kind="data")["holder_agent"] is None

    claimed = _claim(client, BINDING)
    assert claimed["status"] == "ok"
    foreign_release = _release(client, BINDING, claimed["lease_digest"], FOREIGN_RELAY)
    assert (foreign_release["status"], foreign_release["code"]) == (
        "error",
        "identity_mismatch",
    )
    held = CoordBoard(home=home).who("data:desktop-input", kind="data")
    assert held["holder_agent"] == RELAY
    assert lease_digest(held["lease"]) == lease_digest(claimed["lease"])
    assert [frame["op"] for frame in server.frames] == [
        "wake-desktop-input-claim",
        "wake-desktop-input-claim",
        "wake-desktop-input-release",
    ]


def test_remote_host_settles_unknown_only_from_verified_office_reply(
    tmp_path: Path, monkeypatch,
) -> None:
    home = tmp_path / "office"
    target_agent = "zcode-ailab-" + SESSION
    _enroll(home, BINDING, SESSION, target_agent)
    origin = Office(agent_id=RELAY, home=home).send(
        target_agent,
        "Synthetic wake trigger; body is never returned by proof lookup",
        kind="question",
        new_recipient=True,
    )
    wake = Wake(home=home)
    # The synthetic host is Linux; the Windows ZCode workspace is remote
    # metadata. Match Wake's supported cross-platform behavior explicitly.
    monkeypatch.setattr("coordfs.wake._native_workspace_flavor", lambda: "posix")
    ticked = wake.tick(binding_id=BINDING)
    dispatches = [row for row in ticked["decisions"] if row.get("dispatch_id")]
    assert dispatches, json.dumps(ticked, sort_keys=True)
    dispatch_id = dispatches[0]["dispatch_id"]
    pending = wake.relay_pending(binding_id=BINDING, driver=ZCODE_RELAY_DRIVER)
    assert pending["count"] == 1
    assert pending["dispatches"][0]["dispatch_id"] == dispatch_id
    assert pending["dispatches"][0]["trigger_ref"] == origin["id"]

    server = _GateServer(home)
    client = _remote_client(server)
    zcode_target = ZCodeDesktopTarget(
        binding_id=BINDING,
        native_session_id=SESSION,
        office_agent=target_agent,
        project_slug="synthetic-zcode-desktop",
        workspace=r"D:\synthetic\zcode-target",
        relay_identity=RELAY,
        authorized=True,
    )
    host = ZCodeDesktopToolHost(client, TargetRegistry((zcode_target,)))
    now = time.time()
    observation = {
        "schema": "zcode-desktop-app-observation/v1",
        "source": "zcode_desktop_host",
        "sampled_at": now,
        "observed_at": now,
        "user_idle_s": USER_IDLE_REQUIRED_S,
        "app_activity": "idle",
        "draft": False,
        "lock": False,
        "quota": "ok",
        "cu_lease_owner": None,
        "cu_lease_binding_id": None,
        "native_session_id": SESSION,
        "project_slug": "synthetic-zcode-desktop",
        "workspace": r"D:\synthetic\zcode-target",
    }

    ready = host.prepare(BINDING, observation)
    assert ready["code"] == "submit_once"
    assert ready["submit"]["arguments"]["session_id"] == SESSION
    assert ready["dispatch_id"] == dispatch_id
    unknown = host.settle(dispatch_id, None)
    assert unknown["code"] == "receipt_unknown"
    assert unknown["resent"] is False
    assert "wake-desktop-input-release" in [frame["op"] for frame in server.frames]

    waiting = host.reconcile(dispatch_id, "target_finished")
    assert waiting["code"] == "ack_missing"
    assert waiting["resent"] is False
    assert wake._load_dispatch(dispatch_id)["status"] == "delivery_unknown"

    reply = Office(agent_id=target_agent, home=home).send(
        RELAY,
        "Synthetic correlated reply from the ZCode target",
        kind="note",
        reply_to=origin["id"],
        new_recipient=True,
    )
    observed = host.reconcile(dispatch_id, "target_finished")
    assert observed["code"] == "observed"
    assert observed["completed"] is True
    settled = wake._load_dispatch(dispatch_id)
    assert settled["status"] == "target_finished"
    assert settled["receipt_status"] == "delivered"
    assert settled["receipt_message_id"] == reply["id"]
    assert "receipt_turn_id" not in settled
    assert "native" not in json.dumps(settled.get("receipt_detail", "")).lower()
    assert [frame["op"] for frame in server.frames].count("wake-relay-claim") == 1
    assert [frame["op"] for frame in server.frames].count("wake-relay-native-begin") == 1
    assert [frame["op"] for frame in server.frames].count("wake-relay-ack") == 2
    receipt = next(
        frame for frame in server.frames if frame["op"] == "wake-relay-receipt"
    )
    assert receipt["args"]["status"] == "delivered"
    assert receipt["args"]["message_id"] == reply["id"]
    assert receipt["args"].get("turn_id") is None
