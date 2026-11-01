"""Targeted READ64 and wake-list paging checks using only synthetic homes."""

from __future__ import annotations

import json
import subprocess
import sys
import time
import unittest
import uuid
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
CHECKOUT_ROOT = PACKAGE_ROOT.parents[1]
if str(PACKAGE_ROOT) not in sys.path:
    sys.path.insert(0, str(PACKAGE_ROOT))

from coordfs.office import Office
from coordfs.errors import CoordError
from coordfs.office_remote import MAX_RESPONSE_BYTES, OfficeRemoteClient, OfficeRemoteConfig
from coordfs.projects import ProjectRegistry, ProjectTransport
from coordfs.leases import format_ts
from coordfs.wake import Wake


BODY = "x" * 1500
TASK_OUTPUT = CHECKOUT_ROOT / "task-output" / "office-response-budget"


def _line_size(value: dict) -> int:
    return len(json.dumps(value, ensure_ascii=False, separators=(",", ":"),
                          allow_nan=False).encode("utf-8")) + 1


def _synthetic_home() -> tuple[Path, Path]:
    run_root = TASK_OUTPUT / uuid.uuid4().hex
    home = run_root / "office-home"
    home.mkdir(parents=True)
    return run_root, home


def _client(home: Path) -> OfficeRemoteClient:
    gate_code = (
        "import sys; "
        f"sys.path.insert(0, {str(PACKAGE_ROOT)!r}); "
        "from coordfs.office_ssh_gate import main; raise SystemExit(main())"
    )
    gate_argv = [
        sys.executable, "-I", "-B", "-X", "utf8", "-c", gate_code,
        "--principal", "ws-office", "--home", str(home), "--peer-host", "testhost",
    ]

    def local_gate(_ssh_argv: list[str], **kwargs):
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        kwargs["cwd"] = PACKAGE_ROOT
        return subprocess.Popen(gate_argv, **kwargs)

    config = OfficeRemoteConfig(
        host="synthetic.invalid", user="test", identity_file="synthetic-key",
        known_hosts_file="synthetic-known-hosts", request_timeout_s=30,
    )
    return OfficeRemoteClient(config, popen_factory=local_gate)


def _drain(client: OfficeRemoteClient, op: str, identity: dict, args: dict,
           id_of) -> list[str]:
    delivered: list[str] = []
    for _ in range(100):
        result = client.request(op, identity, args)
        if result.get("status") != "ok":
            raise AssertionError(f"{op} returned {result!r}")
        if _line_size(result) > MAX_RESPONSE_BYTES:
            raise AssertionError(f"{op} exceeded the 64 KiB wire limit")
        rows = result.get("messages", [])
        delivered.extend(id_of(item) for item in rows)
        if result.get("remaining") == 0:
            return delivered
        if not rows:
            raise AssertionError(f"{op} made no progress with unread messages remaining")
    raise AssertionError(f"{op} did not drain the synthetic inbox")


class OfficeResponseBudgetTests(unittest.TestCase):
    def test_remote_gate_reads_and_tool_calls_deliver_all_80_once(self) -> None:
        _run_root, home = _synthetic_home()
        writers = [Office(agent_id=f"codex-host-writer-{index}", home=home) for index in range(2)]
        client = _client(home)

        direct_channel = "read64-direct"
        direct_ids = [writers[index % 2].send(None, BODY, channel=direct_channel)["id"]
                      for index in range(80)]
        direct = _drain(
            client, "read", {"harness": "codex", "host": "testhost", "session_id": "direct-reader"},
            {"channel": direct_channel, "limit": 80},
            lambda item: item["untrusted_message"]["id"],
        )
        self.assertEqual(direct, direct_ids)

        tool_channel = "read64-tool"
        tool_ids = [writers[index % 2].send(None, BODY, channel=tool_channel)["id"]
                    for index in range(80)]
        tool = _drain(
            client, "tool-call",
            {"harness": "codex", "host": "testhost", "session_id": "tool-reader"},
            {"name": "office_read", "arguments": {"channel": tool_channel, "limit": 80}},
            lambda item: item["untrusted_message"]["id"],
        )
        self.assertEqual(tool, tool_ids)

    def test_project_read_wrapper_delivers_all_80_once_with_bounded_lines(self) -> None:
        run_root, home = _synthetic_home()
        checkout = run_root / "checkout"
        checkout.mkdir()
        registry = ProjectRegistry(home=home)
        registry.register("alpha", checkout)
        identity_args = {"harness": "codex", "host": "testhost"}
        writers = [ProjectTransport(
            home=home, project_id="alpha", session_id=f"project-writer-{index}", **identity_args
        ) for index in range(2)]
        reader_session = "project-reader"
        stream_args = {"task_id": "task", "thread_id": "thread", "run_id": "run"}
        writer_streams = [writer.stream(**stream_args) for writer in writers]
        reader = ProjectTransport(
            home=home, project_id="alpha", session_id=reader_session, **identity_args
        )
        reader_stream = reader.stream(**stream_args)
        expected: list[str] = []
        for index in range(80):
            writer = writers[index % 2]
            sent = writer.send(reader, BODY, stream=writer_streams[index % 2])
            self.assertEqual(sent["status"], "ok", sent)
            expected.append(sent["delivery_id"])

        client = _client(home)
        received: list[str] = []
        args = {
            "name": "office_project_read",
            "arguments": {
                "project_id": "alpha", **stream_args, "limit": 80,
                **identity_args, "session_id": reader_session,
            },
        }
        identity = {"harness": "codex", "host": "testhost", "session_id": reader_session}
        for _ in range(100):
            result = client.request("tool-call", identity, args)
            self.assertEqual(result.get("status"), "ok", result)
            self.assertLessEqual(_line_size(result), MAX_RESPONSE_BYTES)
            messages = result.get("messages", [])
            received.extend(item["untrusted_message"]["application"]["delivery_id"]
                            for item in messages)
            if result.get("remaining") == 0:
                break
            self.assertTrue(messages, "project read made no progress")
        self.assertEqual(received, expected)

    def test_wake_list_paginates_and_conflict_scan_sees_binding_after_page_one(self) -> None:
        run_root, home = _synthetic_home()
        workspace = run_root / "workspace"
        workspace.mkdir()
        wake = Wake(home=home)
        expires_at = format_ts(time.time() + 3600)

        def enroll(binding_id: str, agent: str, *, correlation_required: bool = False) -> None:
            wake.enroll(
                binding_id=binding_id, office_agent=agent, driver="codex-cli",
                native_thread_id=f"thread-{binding_id}", workspace=str(workspace),
                enrolled_by="synthetic-test", allowed_wakers=["codex-host-waker"],
                allowed_triggers=["message"], wake_kinds=["question"],
                expires_at=expires_at, owner_confirm=True,
                correlation_required=correlation_required,
            )

        for index in range(20):
            enroll(f"a{index:02d}", f"codex-host-filler-{index:02d}")
        enroll("z-active", "codex-host-collision", correlation_required=True)

        first = wake.list_bindings()
        self.assertEqual(first["count"], 20)
        self.assertEqual(first["total_count"], 21)
        self.assertTrue(first["has_more"])
        self.assertEqual(first["next_offset"], 20)
        second = wake.list_bindings(offset=first["next_offset"])
        self.assertEqual(second["count"], 1)
        self.assertEqual(second["total_count"], 21)
        self.assertFalse(second["has_more"])
        self.assertEqual(second["bindings"][0]["binding_id"], "z-active")

        with self.assertRaises(CoordError) as refused:
            enroll("z-new", "codex-host-collision", correlation_required=True)
        self.assertEqual(getattr(refused.exception, "code", None), "wake_binding_conflict")


if __name__ == "__main__":
    unittest.main()
