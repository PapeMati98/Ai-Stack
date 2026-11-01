"""Read-only archive size reporting for channels and the coord doctor."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

from support import PACKAGE_ROOT  # noqa: F401  (adds the package root to sys.path)

from coordfs.doctor import doctor
from coordfs.events import append_event
from coordfs.leases import CoordBoard
from coordfs.office import Office, channel_path, inbox_path
from coordfs.store import events_path


def _archive_files(path: Path) -> list[Path]:
    return sorted(path.parent.glob(f"archived-{path.name}.*"))


def _archive_measurement(paths: list[Path]) -> dict[str, int]:
    return {
        "files": len(paths),
        "bytes": sum(path.stat().st_size for path in paths),
    }


def _tree_snapshot(root: Path) -> dict[str, int]:
    return {
        str(path.relative_to(root)): path.stat().st_size
        for path in root.rglob("*")
        if path.is_file()
    }


class ArchiveSizeTests(unittest.TestCase):
    def _office(self, home: Path, agent_id: str, **overrides: Any) -> Office:
        options: dict[str, Any] = {
            "agent_id": agent_id,
            "home": home,
            "now": lambda: 1_700_000_000.0,
            "mutex_timeout_s": 1.0,
            "rate_limit": 200,
        }
        options.update(overrides)
        return Office(**options)

    def _rotate_inbox_and_channel(self, home: Path) -> tuple[Path, Path]:
        sender = self._office(home, "codex-testhost-alice", inbox_max_bytes=300, inbox_retain=1)
        receiver = self._office(home, "claude-testhost-bob", inbox_max_bytes=300, inbox_retain=1)
        receiver.presence(ttl_s=86_400)
        for index in range(12):
            body = f"inbox-{index}-" + "x" * 180
            self.assertEqual(sender.send("claude-testhost-bob", body)["status"], "ok")
            channel_body = f"channel-{index}-" + "y" * 180
            self.assertEqual(sender.send(None, channel_body, channel="dev")["status"], "ok")
        inbox = inbox_path(home, "claude-testhost-bob")
        channel = channel_path(home, "dev")
        self.assertGreater(len(_archive_files(inbox)), 0)
        self.assertGreater(len(_archive_files(channel)), 0)
        return inbox, channel

    def test_channels_and_doctor_report_archive_files_and_bytes_without_writes(self) -> None:
        home = Path(tempfile.mkdtemp(prefix="office-archive-size-"))
        inbox, channel = self._rotate_inbox_and_channel(home)
        board = CoordBoard(home=home, cwd=home, now=lambda: 1_700_000_000.0)
        for index in range(12):
            append_event(
                home,
                "archive-size-test",
                now_ts=1_700_000_000.0 + index,
                max_bytes=300,
                retain=1,
                payload="z" * 180,
            )
        events = events_path(home)

        before = _tree_snapshot(home)
        listing = self._office(home, "qwen-testhost-carol").channels()
        after_listing = _tree_snapshot(home)
        self.assertEqual(before, after_listing)
        rows = {item["name"]: item for item in listing["channels"]}
        self.assertEqual(
            rows["dev"]["archived_generations"], len(_archive_files(channel))
        )
        self.assertEqual(
            rows["dev"]["archived_bytes"],
            sum(path.stat().st_size for path in _archive_files(channel)),
        )
        self.assertEqual(
            set(rows["dev"]),
            {
                "name",
                "address",
                "bytes",
                "rotated_generations",
                "archived_generations",
                "archived_bytes",
                "messages",
                "last_ts",
                "pending",
            },
        )
        self.assertEqual(rows["dev"]["address"], "#dev")
        self.assertEqual(rows["dev"]["rotated_generations"], 1)
        self.assertEqual(rows["tablica"]["archived_generations"], 0)
        self.assertEqual(rows["tablica"]["archived_bytes"], 0)

        before_doctor = _tree_snapshot(home)
        with mock.patch.dict(os.environ, {"COORDFS_HOME": str(home)}):
            result = doctor(board)
        after_doctor = _tree_snapshot(home)
        self.assertEqual(before_doctor, after_doctor)
        expected_inboxes = _archive_measurement(_archive_files(inbox))
        expected_channels = _archive_measurement(_archive_files(channel))
        expected_events = _archive_measurement(_archive_files(events))
        self.assertEqual(result["office_archives"]["inboxes"], expected_inboxes)
        self.assertEqual(result["office_archives"]["channels"], expected_channels)
        self.assertEqual(result["office_archives"]["events"], expected_events)
        self.assertEqual(
            result["office_archives"]["total"],
            {
                "files": (
                    expected_inboxes["files"]
                    + expected_channels["files"]
                    + expected_events["files"]
                ),
                "bytes": (
                    expected_inboxes["bytes"]
                    + expected_channels["bytes"]
                    + expected_events["bytes"]
                ),
            },
        )
        self.assertEqual(result["events_bytes"], events.stat().st_size)

    def test_clean_state_reports_zero_archives_and_preserves_existing_doctor_fields(
        self,
    ) -> None:
        home = Path(tempfile.mkdtemp(prefix="office-archive-clean-"))
        board = CoordBoard(home=home, cwd=home, now=lambda: 1_700_000_000.0)
        before = _tree_snapshot(home)
        with mock.patch.dict(os.environ, {"COORDFS_HOME": str(home)}):
            result = doctor(board)
        after = _tree_snapshot(home)

        self.assertEqual(before, after)
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["code"], "doctor_ok")
        self.assertEqual(result["events_bytes"], 0)
        self.assertEqual(
            result["office_archives"],
            {
                "inboxes": {"files": 0, "bytes": 0},
                "channels": {"files": 0, "bytes": 0},
                "events": {"files": 0, "bytes": 0},
                "total": {"files": 0, "bytes": 0},
            },
        )


if __name__ == "__main__":
    unittest.main()
