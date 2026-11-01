from __future__ import annotations

import os
import sys
import tempfile
import unittest
from contextlib import nullcontext
from pathlib import Path
from unittest import mock


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
if str(PACKAGE_ROOT) not in sys.path:
    sys.path.insert(0, str(PACKAGE_ROOT))

from coordfs.events import append_event, read_events  # noqa: E402
from coordfs.leases import CoordBoard, parse_lease  # noqa: E402
from coordfs.office import Office  # noqa: E402
from coordfs.store import lock_path  # noqa: E402


class CleanLeaseReadTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="coordfs-clean-reads-"))
        self.home = self.root / "coord-home"
        self.cwd = self.root / "cwd"
        self.cwd.mkdir()
        self.clock = [1_700_000_000.0]
        self.board = CoordBoard(
            home=self.home,
            cwd=self.cwd,
            now=lambda: self.clock[0],
            events_max_bytes_value=4096,
            events_retain_value=3,
        )

    def advance(self, seconds: float) -> None:
        self.clock[0] += seconds

    @staticmethod
    def file_state(root: Path) -> dict[str, tuple[bytes, int]]:
        return {
            path.relative_to(root).as_posix(): (path.read_bytes(), path.stat().st_mtime_ns)
            for path in sorted(root.rglob("*"))
            if path.is_file()
        }

    def test_ls_hides_expired_lease_without_removing_or_writing(self) -> None:
        claimed = self.board.claim("data:expired-for-ls", "agent-old", ttl_s=1)
        path = lock_path(self.home, claimed["resource_id"])
        self.advance(2)
        before = self.file_state(self.home)

        listed = self.board.ls()

        self.assertEqual(listed["leases"], [])
        self.assertEqual(listed["count"], 0)
        self.assertTrue(path.is_file(), "ls must leave the expired lock available for an explicit writer")
        self.assertEqual(self.file_state(self.home), before, "ls must not write, rewrite, or remove files")

    def test_who_reports_expired_lease_as_free_without_removing_or_writing(self) -> None:
        claimed = self.board.claim("data:expired-for-who", "agent-old", ttl_s=1)
        path = lock_path(self.home, claimed["resource_id"])
        self.advance(2)
        before = self.file_state(self.home)

        result = self.board.who("data:expired-for-who")

        self.assertEqual(result["code"], "free")
        self.assertIsNone(result["holder_agent"])
        self.assertIsNone(result["lease"])
        self.assertTrue(path.is_file(), "who must not reclaim an expired lock")
        self.assertEqual(self.file_state(self.home), before, "who must not write, rewrite, or remove files")

    def test_claim_reclaims_exact_expired_resource_and_records_expired_event(self) -> None:
        first = self.board.claim("data:claim-takeover", "agent-old", ttl_s=1)
        path = lock_path(self.home, first["resource_id"])
        self.advance(2)

        reclaimed = self.board.claim("data:claim-takeover", "agent-new", ttl_s=30)

        self.assertEqual(reclaimed["code"], "reclaimed")
        current = parse_lease(path.read_bytes())
        self.assertIsNotNone(current)
        self.assertEqual(current["holder_agent"], "agent-new")
        expired = [event for event in read_events(self.home) if event["type"] == "expired"]
        self.assertEqual(
            expired,
            [
                {
                    "ts": "2023-11-14T22:13:22Z",
                    "type": "expired",
                    "resource_id": first["resource_id"],
                    "holder_agent": "agent-old",
                }
            ],
        )

    def test_claim_of_new_scope_does_not_sweep_unrelated_expired_lock(self) -> None:
        old = self.board.claim("scope:old/", "agent-old", ttl_s=1, kind="scope")
        old_path = lock_path(self.home, old["resource_id"])
        self.advance(2)

        fresh = self.board.claim("scope:new/", "agent-new", ttl_s=30, kind="scope")

        self.assertEqual(fresh["code"], "claimed")
        self.assertTrue(old_path.is_file(), "claim may remove only the exact expired resource it takes over")
        self.assertFalse(
            any(event["type"] == "expired" for event in read_events(self.home)),
            "an unrelated expired resource must be left for explicit sweep or exact takeover",
        )

    def test_explicit_sweep_removes_expired_lock_and_records_events(self) -> None:
        claimed = self.board.claim("data:explicit-sweep", "agent-old", ttl_s=1)
        path = lock_path(self.home, claimed["resource_id"])
        self.advance(2)

        swept = self.board.sweep()

        self.assertEqual(swept["reclaimed"], [claimed["resource_id"]])
        self.assertEqual(swept["count"], 1)
        self.assertFalse(path.exists())
        events = read_events(self.home)
        self.assertEqual(events[-2]["type"], "expired")
        self.assertEqual(events[-2]["resource_id"], claimed["resource_id"])
        self.assertEqual(events[-2]["holder_agent"], "agent-old")
        self.assertEqual(events[-1]["type"], "swept")
        self.assertEqual(events[-1]["resources"], [claimed["resource_id"]])

    def test_office_who_hides_expired_presence_without_removing_or_writing(self) -> None:
        alice = Office(agent_id="codex-testhost-alice", home=self.home, now=lambda: self.clock[0])
        observer = Office(agent_id="codex-testhost-observer", home=self.home, now=lambda: self.clock[0])
        present = alice.presence(ttl_s=1)
        self.assertEqual(present["status"], "ok", present)
        path = lock_path(self.home, "agent:codex-testhost-alice")
        self.advance(2)
        before = self.file_state(self.home)

        result = observer.who()

        self.assertEqual(result["online"], [])
        self.assertEqual(result["count"], 0)
        self.assertTrue(path.is_file(), "Office who must leave expired presence on disk")
        self.assertEqual(self.file_state(self.home), before, "Office who must be a clean read")

    def test_office_recipient_lookup_ignores_expired_presence_without_removing_it(self) -> None:
        alice_id = "codex-testhost-alice"
        observer = Office(agent_id="codex-testhost-observer", home=self.home, now=lambda: self.clock[0])
        claimed = self.board.claim(
            "agent:codex-testhost-alice",
            alice_id,
            ttl_s=1,
            meta={"kind": "presence"},
            kind="agent",
        )
        self.assertEqual(claimed["status"], "ok")
        path = lock_path(self.home, "agent:codex-testhost-alice")
        self.advance(2)
        before = self.file_state(self.home)

        result = observer.send(alice_id, "not deliverable while offline")

        self.assertEqual(result["code"], "unknown_recipient")
        self.assertTrue(path.is_file(), "live-agent lookup must not reclaim expired presence")
        self.assertEqual(self.file_state(self.home), before, "live-agent lookup must be a clean read")

    def test_office_presence_reclaims_its_exact_expired_lease(self) -> None:
        alice = Office(agent_id="codex-testhost-alice", home=self.home, now=lambda: self.clock[0])
        self.assertEqual(alice.presence(ttl_s=1, status="old")["status"], "ok")
        self.advance(2)

        refreshed = alice.presence(ttl_s=30, status="new")

        self.assertEqual(refreshed["status"], "ok", refreshed)
        self.assertEqual(refreshed["lease_code"], "reclaimed")
        current = parse_lease(lock_path(self.home, "agent:codex-testhost-alice").read_bytes())
        self.assertIsNotNone(current)
        self.assertEqual(current["holder_agent"], "codex-testhost-alice")
        self.assertEqual(current["meta"]["status"], "new")
        expired = [event for event in read_events(self.home) if event["type"] == "expired"]
        self.assertEqual(len(expired), 1)
        self.assertEqual(expired[0]["resource_id"], "agent:codex-testhost-alice")
        self.assertEqual(expired[0]["holder_agent"], "codex-testhost-alice")


class DurableAppendTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="coordfs-fsync-"))
        self.home = self.root / "coord-home"
        self.home.mkdir()

    def test_message_append_fsyncs_after_flush(self) -> None:
        office = Office(agent_id="codex-testhost-sender", home=self.home, now=lambda: 1_700_000_000.0)
        sizes_seen_by_fsync: list[int] = []

        def observe_fsync(fd: int) -> None:
            sizes_seen_by_fsync.append(os.fstat(fd).st_size)

        with mock.patch("coordfs.office.exclusive_mutex", return_value=nullcontext()), mock.patch(
            "coordfs.office.os.fsync", side_effect=observe_fsync
        ) as fsync:
            result = office.send(None, "durable message", channel="durability")

        self.assertEqual(result["status"], "ok", result)
        fsync.assert_called_once()
        self.assertIsInstance(fsync.call_args.args[0], int)
        self.assertGreater(sizes_seen_by_fsync[0], 0, "fsync must observe bytes already flushed to the file")

    def test_event_append_fsyncs_after_flush(self) -> None:
        sizes_seen_by_fsync: list[int] = []

        def observe_fsync(fd: int) -> None:
            sizes_seen_by_fsync.append(os.fstat(fd).st_size)

        with mock.patch("coordfs.events.exclusive_mutex", return_value=nullcontext()), mock.patch(
            "coordfs.events.os.fsync", side_effect=observe_fsync
        ) as fsync:
            result = append_event(
                self.home,
                "durability_probe",
                now_ts=1_700_000_000.0,
                max_bytes=4096,
                retain=3,
            )

        self.assertEqual(result["type"], "durability_probe")
        fsync.assert_called_once()
        self.assertIsInstance(fsync.call_args.args[0], int)
        self.assertGreater(sizes_seen_by_fsync[0], 0, "fsync must observe bytes already flushed to the file")


if __name__ == "__main__":
    unittest.main()
