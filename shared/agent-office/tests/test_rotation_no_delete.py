"""Regression tests: rotation archives bytes instead of deleting stream generations."""

from __future__ import annotations

import ast
import errno
import inspect
import json
from pathlib import Path
from typing import Any
from unittest import mock

from office_support import OfficeTestCase
from support import CoordTestCase

from coordfs import events as events_module
from coordfs.events import append_event, rotate_if_needed
from coordfs.office import channel_ledger_path, channel_path, inbox_ledger_path, inbox_path
from coordfs.store import events_path


def _active_paths(path: Path, retain: int) -> list[Path]:
    paths = [path.with_name(f"{path.name}.{index}") for index in range(retain, 0, -1)]
    paths.append(path)
    return [candidate for candidate in paths if candidate.is_file()]


def _ledger(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


class EventRotationNoDeleteTests(CoordTestCase):
    def test_many_event_rotations_preserve_every_written_byte_and_archive_names(self) -> None:
        self.home.mkdir(parents=True, exist_ok=True)
        target = events_path(self.home)
        expected_bytes = 0
        for index in range(36):
            fields = {"index": index, "payload": f"event-{index:03d}-" + "x" * 90}
            payload = append_event(
                self.home,
                "rotation-test",
                now_ts=1_700_000_000.0 + index,
                max_bytes=320,
                retain=2,
                **fields,
            )
            expected_bytes += len(
                (json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode(
                    "utf-8"
                )
            )

        active = _active_paths(target, 2)
        archives = sorted(target.parent.glob(f"archived-{target.name}.2-*"))
        self.assertGreater(len(archives), 2, "the test must rotate past retain several times")
        self.assertEqual(sum(path.stat().st_size for path in active + archives), expected_bytes)
        self.assertEqual(len({path.name for path in archives}), len(archives))

    def test_existing_archive_is_never_overwritten(self) -> None:
        self.home.mkdir(parents=True, exist_ok=True)
        target = events_path(self.home)
        target.write_bytes(b"live\n")
        oldest = target.with_name(f"{target.name}.1")
        oldest.write_bytes(b"oldest\n")
        reserved = target.with_name("reserved-archive")
        reserved.write_bytes(b"keep-me\n")

        with self.assertRaises(FileExistsError):
            rotate_if_needed(target, 1, 1, 1, archive_path=reserved)

        self.assertEqual(reserved.read_bytes(), b"keep-me\n")
        self.assertEqual(oldest.read_bytes(), b"oldest\n")
        self.assertEqual(target.read_bytes(), b"live\n")

    def test_hard_link_unavailable_aborts_before_any_active_file_moves(self) -> None:
        self.home.mkdir(parents=True, exist_ok=True)
        target = events_path(self.home)
        target.write_bytes(b"live\n")
        oldest = target.with_name(f"{target.name}.1")
        oldest.write_bytes(b"oldest\n")
        archive = target.with_name("reserved-archive")

        unavailable = OSError(errno.ENOTSUP, "synthetic hard-link unsupported")
        with mock.patch.object(events_module.os, "link", side_effect=unavailable):
            with self.assertRaisesRegex(
                OSError, r"hard-link creation failed .*rotation aborted before active files moved"
            ):
                rotate_if_needed(target, 1, 1, 1, archive_path=archive)

        self.assertFalse(archive.exists())
        self.assertEqual(oldest.read_bytes(), b"oldest\n")
        self.assertEqual(target.read_bytes(), b"live\n")

    def test_rotation_function_has_no_stream_unlink_or_remove_call(self) -> None:
        source = "\n".join(
            (
                inspect.getsource(events_module._move_to_archive),
                inspect.getsource(events_module.rotate_if_needed),
            )
        )
        tree = ast.parse(source)
        forbidden: list[str] = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            if isinstance(node.func, ast.Name) and node.func.id in {"unlink", "unlink_with_backoff", "remove"}:
                forbidden.append(node.func.id)
            if isinstance(node.func, ast.Attribute) and node.func.attr in {"unlink", "remove"}:
                forbidden.append(node.func.attr)
        self.assertEqual(forbidden, [])


class OfficeRotationNoDeleteTests(OfficeTestCase):
    CAP = 1024
    RETAIN = 2
    PAD = "x" * 180

    def _send_until_archives(
        self,
        sender: Any,
        *,
        ledger_path: Path,
        count: int,
        channel: str | None,
    ) -> tuple[int, int]:
        index = 0
        total_bytes = 0
        while True:
            body = f"message-{index:03d}-{self.PAD}"
            if channel is None:
                result = sender.send("claude-testhost-bob", body)
            else:
                result = sender.send(None, body, channel=channel)
            self.assertEqual(result["status"], "ok", result)
            total_bytes += result["bytes"]
            index += 1
            self.assertLess(index, 80, "rotation did not create the expected archives")
            if ledger_path.is_file() and len(_ledger(ledger_path).get("archives", [])) >= count:
                return index, total_bytes

    def _assert_stream_is_lossless(
        self,
        *,
        stream: Path,
        ledger_path: Path,
        total_bytes: int,
        reader: Any,
        channel: str | None,
    ) -> None:
        ledger = _ledger(ledger_path)
        archives = ledger["archives"]
        self.assertGreaterEqual(len(archives), 2)
        self.assertTrue(all(item["status"] == "archived" for item in archives), archives)
        archive_paths = [stream.parent / item["name"] for item in archives]
        self.assertTrue(all(path.is_file() for path in archive_paths), archive_paths)
        self.assertEqual(len({path.name for path in archive_paths}), len(archive_paths))
        active = _active_paths(stream, self.RETAIN)
        self.assertEqual(sum(path.stat().st_size for path in active + archive_paths), total_bytes)

        active_bodies: list[str] = []
        for path in active:
            active_bodies.extend(json.loads(line)["body"] for line in path.read_text(encoding="utf-8").splitlines())
        result = reader.read() if channel is None else reader.read(channel=channel)
        self.assertTrue(result["gap_detected"], result)
        returned = [item["untrusted_message"]["body"] for item in result["messages"]]
        self.assertEqual(returned, active_bodies, "the normal reader still drains only active generations")

    def test_inbox_rotation_archives_bytes_and_records_names_in_ledger(self) -> None:
        self.announce("claude-testhost-bob")
        sender = self.office(
            "codex-testhost-alice", inbox_max_bytes=self.CAP, inbox_retain=self.RETAIN, rate_limit=200
        )
        reader = self.office("claude-testhost-bob", inbox_max_bytes=self.CAP, inbox_retain=self.RETAIN)
        stream = inbox_path(self.home, "claude-testhost-bob")
        ledger_path = inbox_ledger_path(self.home, "claude-testhost-bob")

        _, first_total = self._send_until_archives(
            sender, ledger_path=ledger_path, count=1, channel=None
        )
        first_ledger = _ledger(ledger_path)
        first_archive = stream.parent / first_ledger["archives"][0]["name"]
        first_bytes = first_archive.read_bytes()
        _, second_total = self._send_until_archives(
            sender, ledger_path=ledger_path, count=2, channel=None
        )
        self.assertEqual(first_archive.read_bytes(), first_bytes, "a later rotation overwrote an archive")
        self._assert_stream_is_lossless(
            stream=stream,
            ledger_path=ledger_path,
            total_bytes=first_total + second_total,
            reader=reader,
            channel=None,
        )

    def test_channel_rotation_archives_bytes_and_records_names_in_ledger(self) -> None:
        sender = self.office(
            "codex-testhost-alice", inbox_max_bytes=self.CAP, inbox_retain=self.RETAIN, rate_limit=200
        )
        reader = self.office("claude-testhost-bob", inbox_max_bytes=self.CAP, inbox_retain=self.RETAIN)
        stream = channel_path(self.home, "dev")
        ledger_path = channel_ledger_path(self.home, "dev")

        _, total_bytes = self._send_until_archives(
            sender, ledger_path=ledger_path, count=2, channel="dev"
        )
        self._assert_stream_is_lossless(
            stream=stream,
            ledger_path=ledger_path,
            total_bytes=total_bytes,
            reader=reader,
            channel="dev",
        )

    def test_failed_archive_is_recorded_without_claiming_an_active_eviction(self) -> None:
        sender = self.office("codex-testhost-alice", inbox_max_bytes=1, inbox_retain=1)
        stream = sender._channel_stream("dev")
        stream.path.parent.mkdir(parents=True, exist_ok=True)
        stream.path.write_bytes(b"live\n")
        oldest = stream.path.with_name(f"{stream.path.name}.1")
        oldest.write_bytes(b"oldest\n")

        unavailable = OSError(errno.ENOTSUP, "synthetic hard-link unsupported")
        with mock.patch.object(events_module.os, "link", side_effect=unavailable):
            with self.assertRaisesRegex(OSError, "rotation aborted before active files moved"):
                sender._rotate_log(stream, sender.inbox_max_bytes)

        ledger = _ledger(stream.ledger)
        self.assertEqual(ledger["seq"], 0)
        self.assertEqual(ledger["slots"], {})
        self.assertEqual(ledger["evicted_seq"], -1)
        self.assertIsNone(ledger["evicted_end"])
        self.assertEqual(len(ledger["archives"]), 1)
        self.assertEqual(ledger["archives"][0]["status"], "failed")
        self.assertFalse((stream.path.parent / ledger["archives"][0]["name"]).exists())
        self.assertEqual(oldest.read_bytes(), b"oldest\n")
        self.assertEqual(stream.path.read_bytes(), b"live\n")
