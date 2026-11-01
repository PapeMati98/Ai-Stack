from __future__ import annotations

import io
import json
import threading
import time
import unittest
from argparse import Namespace
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from coordfs import office_cli_remote as remote
from coordfs.office import Office
from coordfs.office_remote import OfficeRemoteClient, OfficeRemoteConfig
from coordfs.office_ssh_gate import run as run_gate


AGENT_ID = "codex-peer-linux-session"
PEER_HOST = "peer-linux"


class _ResponsePipe:
    def __init__(self) -> None:
        self.ready = threading.Event()
        self.data = b""
        self.offset = 0

    def supply(self, data: bytes) -> None:
        self.data = data
        self.ready.set()

    def read(self, size: int = -1) -> bytes:
        if not self.ready.wait(5):
            raise AssertionError("forced gate did not finish")
        if self.offset >= len(self.data):
            return b""
        end = len(self.data) if size < 0 else min(len(self.data), self.offset + size)
        chunk = self.data[self.offset:end]
        self.offset = end
        return chunk

    def close(self) -> None:
        pass


class _RequestPipe:
    def __init__(self, process: "_GateProcess") -> None:
        self.process = process
        self.data = bytearray()

    def write(self, data: bytes | memoryview) -> int:
        self.data.extend(data)
        return len(data)

    def close(self) -> None:
        output = io.BytesIO()
        self.process.returncode = run_gate(
            io.BytesIO(self.data), output, principal="ws-office", peer_host=self.process.peer_host,
            home=self.process.home, environ={},
        )
        self.process.stdout.supply(output.getvalue())


class _GateProcess:
    def __init__(self, home: Path, peer_host: str) -> None:
        self.home = home
        self.peer_host = peer_host
        self.stdout = _ResponsePipe()
        self.stdin = _RequestPipe(self)
        self.returncode: int | None = None

    def wait(self, timeout: float | None = None) -> int:
        if not self.stdout.ready.wait(5 if timeout is None else min(timeout, 5)):
            raise AssertionError("forced gate did not return")
        assert self.returncode is not None
        return self.returncode

    def kill(self) -> None:
        self.returncode = -9
        self.stdout.supply(b"")


class _GateFactory:
    def __init__(self, home: Path, peer_host: str) -> None:
        self.home = home
        self.peer_host = peer_host

    def __call__(self, *_args: object, **_kwargs: object) -> _GateProcess:
        return _GateProcess(self.home, self.peer_host)


def _gate_request(home: Path, identity: dict[str, str], args: dict[str, object], *,
                  peer_host: str = PEER_HOST) -> tuple[int, dict[str, object]]:
    frame = {"v": 1, "op": "wait", "identity": identity, "args": args}
    output = io.BytesIO()
    code = run_gate(io.BytesIO(json.dumps(frame).encode("utf-8") + b"\n"), output,
                    principal="ws-office", peer_host=peer_host, home=home, environ={})
    return code, json.loads(output.getvalue())


def _args(*, agent: str | None = AGENT_ID, harness: str = "codex", session_id: str = "session",
          timeout: float = 3) -> Namespace:
    return Namespace(command="wait", remote_config="synthetic-config", home=None,
                     agent=agent, harness=harness, session_id=session_id,
                     timeout=timeout, channel=None)


class RemoteIdentityWaitTests(unittest.TestCase):
    def setUp(self) -> None:
        temp_root = Path(__file__).resolve().parents[3] / "task-inputs" / "office-wake-rsi" / "task-temp"
        temp_root.mkdir(parents=True, exist_ok=True)
        self.temp = TemporaryDirectory(prefix="office-remote-wait-", dir=temp_root)
        self.home = Path(self.temp.name) / "coord"
        self.config = OfficeRemoteConfig(
            host="office.example.invalid", user="office", identity_file="synthetic-key",
            known_hosts_file="synthetic-known-hosts", connect_timeout_s=1, request_timeout_s=4,
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _dispatch_remote(self, args: Namespace):
        factory = _GateFactory(self.home, PEER_HOST)
        client = OfficeRemoteClient(self.config, popen_factory=factory)
        with patch.object(remote, "load_remote_config", return_value=self.config), \
                patch.object(remote, "OfficeRemoteClient", return_value=client):
            return remote.dispatch_remote(args)

    def test_explicit_canonical_self_id_is_host_bound_and_wakes_on_later_message(self) -> None:
        identity = remote._remote_identity(_args())
        self.assertEqual(identity, {"harness": "codex", "host": PEER_HOST, "session_id": "session"})

        outcome: dict[str, object] = {}
        ready = threading.Event()

        def remote_wait() -> None:
            ready.set()
            outcome["result"] = self._dispatch_remote(_args())
            outcome["finished_at"] = time.monotonic()

        waiter = threading.Thread(target=remote_wait)
        waiter.start()
        self.assertTrue(ready.wait(1))
        time.sleep(0.08)

        send_started = time.monotonic()
        sent = Office(agent_id="claude-peer-linux-sender", home=self.home).send(
            AGENT_ID, "later remote message", new_recipient=True
        )
        waiter.join(4)

        self.assertFalse(waiter.is_alive())
        result = outcome["result"]
        self.assertEqual(sent["code"], "sent")
        self.assertEqual(result["agent"], AGENT_ID)
        self.assertEqual(result["code"], "message")
        self.assertTrue(result["new"])
        self.assertLess(outcome["finished_at"] - send_started, 1.25)

    def test_valid_structured_identity_works_but_cannot_read_another_inbox(self) -> None:
        identity = {"harness": "codex", "host": PEER_HOST, "session_id": "session"}
        other = "codex-peer-linux-other"
        sent = Office(agent_id="claude-peer-linux-sender", home=self.home).send(
            other, "private to other session", new_recipient=True
        )
        self.assertEqual(sent["code"], "sent")

        code, waited = _gate_request(self.home, identity, {"timeout_s": 0})
        self.assertEqual(code, 0)
        self.assertEqual(waited["agent"], AGENT_ID)
        self.assertEqual(waited["code"], "timeout")
        self.assertEqual(Office(agent_id=other, home=self.home).read()["count"], 1)

        code, refused = _gate_request(self.home, identity, {"timeout_s": 0, "agent_id": other})
        self.assertEqual((code, refused["code"]), (1, "invalid_args"))

    def test_explicit_identity_remains_bound_to_peer_host_and_allowed_harness(self) -> None:
        other_host = remote._remote_identity(_args(agent="codex-other-host-session"))
        code, refused = _gate_request(self.home, other_host, {"timeout_s": 0})
        self.assertEqual((code, refused["code"]), (1, "peer_host_mismatch"))

        other_harness = remote._remote_identity(_args(
            agent="unlisted-peer-linux-session", harness="unlisted"
        ))
        code, refused = _gate_request(self.home, other_harness, {"timeout_s": 0})
        self.assertEqual((code, refused["code"]), (1, "harness_not_allowed"))


if __name__ == "__main__":
    unittest.main()
