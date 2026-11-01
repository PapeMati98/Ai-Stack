"""Rejection traces must survive a process restart without re-emitting.

Layer 1/2 (path and name) rejections must never open the file just to
fingerprint it. Dummy secret fragments only — never real credentials.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from caretaker.connectors import FilesConnector, collect_sources, event_to_dict


TASK_TMP = _ROOT / ".test-tmp"
TASK_TMP.mkdir(parents=True, exist_ok=True)

DUMMY_AWS = "AKIA" + ("A" * 16)
SECRET_BODY = f"key = {DUMMY_AWS} keep-this-unique-fragment"


def _age(path: Path, seconds_ago: float = 3600.0) -> None:
    stamp = time.time() - seconds_ago
    os.utime(str(path), (stamp, stamp))


def _spec(root: Path, state_dir: Path, *, source_id: str = "owner-notes-rej"):
    return {
        "tool": "files",
        "kind": "files",
        "source_id": source_id,
        "state_dir": str(state_dir),
        "roots": [{"path": str(root), "project_slug": "przyklad", "max_depth": 4}],
        "extensions": [".md", ".txt"],
        "quiet_minutes": 15,
    }


def _simulate_process_restart() -> None:
    FilesConnector._admission_state.clear()
    FilesConnector._rejection_state.clear()


def _rejected_events(collection):
    out = []
    for event in collection.events:
        payload = event_to_dict(event)
        if payload.get("completion_kind") == "secret_rejected" or payload.get(
            "rejection_reason"
        ):
            out.append(payload)
    return out


def _collect_until_rejected(spec, *, ticks: int = 4):
    last = None
    for _ in range(ticks):
        last = collect_sources([spec])
        rejected = _rejected_events(last)
        if rejected:
            return last, rejected
    return last, []


class RejectionCursorTests(unittest.TestCase):
    def setUp(self):
        FilesConnector._admission_state.clear()
        FilesConnector._rejection_state.clear()

    def test_unchanged_path_rejection_not_reemitted_after_process_restart(self):
        with tempfile.TemporaryDirectory(dir=str(TASK_TMP)) as d:
            base = Path(d)
            root = base / "notes"
            state_dir = base / "state"
            root.mkdir()
            state_dir.mkdir()
            blocked = root / "my_secret_notes.md"
            blocked.write_text("never-open-this-body", encoding="utf-8")
            _age(blocked)
            spec = _spec(root, state_dir, source_id="rej-path-stable")

            first = collect_sources([spec])
            rejected = _rejected_events(first)
            self.assertEqual(len(rejected), 1)
            self.assertEqual(rejected[0]["relative_path"], "my_secret_notes.md")
            self.assertEqual(rejected[0]["excerpt"], "")
            self.assertEqual(rejected[0].get("completion_kind"), "secret_rejected")

            _simulate_process_restart()
            self.assertFalse(FilesConnector._rejection_state)

            second = collect_sources([spec])
            self.assertEqual(_rejected_events(second), [])

            third = collect_sources([spec])
            self.assertEqual(_rejected_events(third), [])

    def test_changed_rejected_file_emits_trace_again(self):
        with tempfile.TemporaryDirectory(dir=str(TASK_TMP)) as d:
            base = Path(d)
            root = base / "notes"
            state_dir = base / "state"
            root.mkdir()
            state_dir.mkdir()
            blocked = root / "my_secret_notes.md"
            blocked.write_text("first-body", encoding="utf-8")
            _age(blocked)
            spec = _spec(root, state_dir, source_id="rej-path-changed")

            first = collect_sources([spec])
            self.assertEqual(len(_rejected_events(first)), 1)

            _simulate_process_restart()
            blocked.write_text("second-body-longer", encoding="utf-8")
            _age(blocked, 7200.0)

            second = collect_sources([spec])
            rejected = _rejected_events(second)
            self.assertEqual(len(rejected), 1)
            self.assertEqual(rejected[0]["excerpt"], "")

            _simulate_process_restart()
            third = collect_sources([spec])
            self.assertEqual(_rejected_events(third), [])

    def test_path_or_name_rejection_does_not_read_file_contents(self):
        with tempfile.TemporaryDirectory(dir=str(TASK_TMP)) as d:
            base = Path(d)
            root = base / "notes"
            state_dir = base / "state"
            root.mkdir()
            state_dir.mkdir()
            blocked = root / "my_secret_notes.md"
            blocked.write_text("must-not-be-read", encoding="utf-8")
            _age(blocked)
            spec = _spec(root, state_dir, source_id="rej-no-read")

            real_open = open

            def guarded_open(path, *args, **kwargs):
                target = os.fspath(path)
                if os.path.normcase(os.path.abspath(target)) == os.path.normcase(
                    os.path.abspath(str(blocked))
                ):
                    raise AssertionError("must not open path-rejected file")
                return real_open(path, *args, **kwargs)

            with mock.patch("builtins.open", guarded_open):
                with mock.patch.object(Path, "read_text", side_effect=AssertionError("read_text")):
                    with mock.patch.object(
                        Path, "read_bytes", side_effect=AssertionError("read_bytes")
                    ):
                        collection = collect_sources([spec])
            rejected = _rejected_events(collection)
            self.assertEqual(len(rejected), 1)
            self.assertEqual(rejected[0]["excerpt"], "")

    def test_rejection_trace_contains_no_content_fragment(self):
        with tempfile.TemporaryDirectory(dir=str(TASK_TMP)) as d:
            base = Path(d)
            root = base / "notes"
            state_dir = base / "state"
            root.mkdir()
            state_dir.mkdir()
            leaked = root / "leaked.md"
            leaked.write_text(SECRET_BODY, encoding="utf-8")
            _age(leaked)
            spec = _spec(root, state_dir, source_id="rej-no-leak")

            _collection, rejected = _collect_until_rejected(spec)
            self.assertTrue(rejected)
            dumped = json.dumps(rejected, ensure_ascii=False)
            self.assertNotIn(DUMMY_AWS, dumped)
            self.assertNotIn("keep-this-unique-fragment", dumped)
            self.assertNotIn(SECRET_BODY, dumped)
            self.assertEqual(rejected[0]["excerpt"], "")
            self.assertEqual(rejected[0].get("sha256") or "", "")

            state_files = list(state_dir.glob("files-rejection-*.json"))
            self.assertTrue(state_files)
            state_text = state_files[0].read_text(encoding="utf-8")
            self.assertNotIn(DUMMY_AWS, state_text)
            self.assertNotIn("keep-this-unique-fragment", state_text)
            self.assertNotIn(SECRET_BODY, state_text)

    def test_content_secret_not_reemitted_after_restart_until_file_changes(self):
        with tempfile.TemporaryDirectory(dir=str(TASK_TMP)) as d:
            base = Path(d)
            root = base / "notes"
            state_dir = base / "state"
            root.mkdir()
            state_dir.mkdir()
            leaked = root / "leaked.md"
            leaked.write_text(SECRET_BODY, encoding="utf-8")
            _age(leaked)
            spec = _spec(root, state_dir, source_id="rej-content-stable")

            _first, rejected = _collect_until_rejected(spec)
            self.assertEqual(len(rejected), 1)

            _simulate_process_restart()
            later = collect_sources([spec])
            later2 = collect_sources([spec])
            self.assertEqual(_rejected_events(later), [])
            self.assertEqual(_rejected_events(later2), [])

            leaked.write_text(SECRET_BODY + "\nmore", encoding="utf-8")
            _age(leaked, 7200.0)
            _simulate_process_restart()
            _again, rejected_again = _collect_until_rejected(spec)
            self.assertEqual(len(rejected_again), 1)
            self.assertEqual(rejected_again[0]["excerpt"], "")
            dumped = json.dumps(rejected_again, ensure_ascii=False)
            self.assertNotIn(DUMMY_AWS, dumped)

    def test_multi_root_same_relative_path_survives_restart_and_renews_on_second_root_change(self):
        with tempfile.TemporaryDirectory(dir=str(TASK_TMP)) as d:
            base = Path(d)
            root_a = base / "notes-a"
            root_b = base / "notes-b"
            state_dir = base / "state"
            root_a.mkdir()
            root_b.mkdir()
            state_dir.mkdir()
            blocked_a = root_a / "my_secret_notes.md"
            blocked_b = root_b / "my_secret_notes.md"
            blocked_a.write_text("never-open-a", encoding="utf-8")
            blocked_b.write_text("never-open-b", encoding="utf-8")
            _age(blocked_a)
            _age(blocked_b)
            spec = {
                "tool": "files",
                "kind": "files",
                "source_id": "rej-multi-root",
                "state_dir": str(state_dir),
                "roots": [
                    {"path": str(root_a), "project_slug": "alpha", "max_depth": 4},
                    {"path": str(root_b), "project_slug": "beta", "max_depth": 4},
                ],
                "extensions": [".md", ".txt"],
                "quiet_minutes": 15,
            }

            first = collect_sources([spec])
            rejected = _rejected_events(first)
            self.assertEqual(len(rejected), 2)
            for item in rejected:
                self.assertEqual(item["excerpt"], "")
                self.assertEqual(item.get("completion_kind"), "secret_rejected")

            _simulate_process_restart()
            second = collect_sources([spec])
            self.assertEqual(_rejected_events(second), [])

            blocked_b.write_text("never-open-b-changed-and-longer", encoding="utf-8")
            _age(blocked_b, 7200.0)
            _simulate_process_restart()
            third = collect_sources([spec])
            renewed = _rejected_events(third)
            self.assertEqual(len(renewed), 1)
            self.assertEqual(renewed[0]["excerpt"], "")
            self.assertEqual(renewed[0]["relative_path"], "my_secret_notes.md")
            self.assertEqual(renewed[0].get("project") or renewed[0].get("project_slug"), "beta")

            _simulate_process_restart()
            fourth = collect_sources([spec])
            self.assertEqual(_rejected_events(fourth), [])

    def test_slugless_root_does_not_reemit_after_process_restart(self):
        with tempfile.TemporaryDirectory(dir=str(TASK_TMP)) as d:
            base = Path(d)
            root = base / "unassigned-notes"
            state_dir = base / "state"
            root.mkdir()
            state_dir.mkdir()
            blocked = root / "my_secret_notes.md"
            blocked.write_text("never-open-slugless", encoding="utf-8")
            _age(blocked)
            spec = {
                "tool": "files",
                "kind": "files",
                "source_id": "rej-slugless",
                "state_dir": str(state_dir),
                "roots": [{"path": str(root), "max_depth": 4}],
                "extensions": [".md", ".txt"],
                "quiet_minutes": 15,
            }

            first = collect_sources([spec])
            rejected = _rejected_events(first)
            self.assertEqual(len(rejected), 1)
            self.assertEqual(rejected[0]["excerpt"], "")
            self.assertEqual(rejected[0].get("completion_kind"), "secret_rejected")

            _simulate_process_restart()
            self.assertFalse(FilesConnector._rejection_state)
            second = collect_sources([spec])
            self.assertEqual(_rejected_events(second), [])

            _simulate_process_restart()
            third = collect_sources([spec])
            self.assertEqual(_rejected_events(third), [])

            blocked.write_text("never-open-slugless-changed", encoding="utf-8")
            _age(blocked, 7200.0)
            _simulate_process_restart()
            fourth = collect_sources([spec])
            renewed = _rejected_events(fourth)
            self.assertEqual(len(renewed), 1)
            self.assertEqual(renewed[0]["excerpt"], "")

    def test_shared_slug_same_relative_path_is_per_root_not_aggregated(self):
        with tempfile.TemporaryDirectory(dir=str(TASK_TMP)) as d:
            base = Path(d)
            root_a = base / "notes-a"
            root_b = base / "notes-b"
            state_dir = base / "state"
            root_a.mkdir()
            root_b.mkdir()
            state_dir.mkdir()
            blocked_a = root_a / "my_secret_notes.md"
            blocked_b = root_b / "my_secret_notes.md"
            blocked_a.write_text("never-open-a", encoding="utf-8")
            blocked_b.write_text("never-open-b", encoding="utf-8")
            _age(blocked_a)
            _age(blocked_b)
            spec = {
                "tool": "files",
                "kind": "files",
                "source_id": "rej-shared-slug",
                "state_dir": str(state_dir),
                "roots": [
                    {"path": str(root_a), "project_slug": "shared", "max_depth": 4},
                    {"path": str(root_b), "project_slug": "shared", "max_depth": 4},
                ],
                "extensions": [".md", ".txt"],
                "quiet_minutes": 15,
            }

            first = collect_sources([spec])
            rejected = _rejected_events(first)
            self.assertEqual(len(rejected), 2)
            for item in rejected:
                self.assertEqual(item["excerpt"], "")

            _simulate_process_restart()
            second = collect_sources([spec])
            self.assertEqual(_rejected_events(second), [])

            blocked_b.write_text("never-open-b-changed-and-longer", encoding="utf-8")
            _age(blocked_b, 7200.0)
            _simulate_process_restart()
            third = collect_sources([spec])
            renewed = _rejected_events(third)
            self.assertEqual(len(renewed), 1)
            self.assertEqual(renewed[0]["excerpt"], "")
            self.assertEqual(renewed[0]["relative_path"], "my_secret_notes.md")

            _simulate_process_restart()
            fourth = collect_sources([spec])
            self.assertEqual(_rejected_events(fourth), [])


if __name__ == "__main__":
    unittest.main()
