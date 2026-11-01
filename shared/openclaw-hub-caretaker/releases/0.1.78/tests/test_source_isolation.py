"""One bad source must not abort the rest of a collect_sources tick."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from caretaker.connectors import (
    FILE_EVENT_TYPE,
    FilesConnector,
    SourceSpec,
    collect_sources,
)


TASK_TMP = _ROOT / ".test-tmp"
TASK_TMP.mkdir(parents=True, exist_ok=True)


def _age(path: Path, seconds_ago: float = 3600.0) -> None:
    stamp = time.time() - seconds_ago
    os.utime(str(path), (stamp, stamp))


def _write_jsonl(path: Path, rows: list[dict]) -> Path:
    path.write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
        "utf-8",
    )
    return path


def _codex_rows() -> list[dict]:
    return [
        {
            "type": "session_meta",
            "timestamp": "2026-09-17T12:00:00Z",
            "payload": {
                "id": "meta-1",
                "session_id": "codex-ok",
                "cwd": "C:/synthetic/project",
                "model": "Qwen3.8-27B",
                "title": "ok turn",
            },
        },
        {
            "type": "turn_context",
            "timestamp": "2026-09-17T12:00:01Z",
            "payload": {"turn_id": "turn-ok", "cwd": "C:/synthetic/project"},
        },
        {
            "type": "response_item",
            "timestamp": "2026-09-17T12:00:02Z",
            "payload": {
                "id": "user-1",
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": "Inspect."}],
            },
        },
        {
            "type": "response_item",
            "timestamp": "2026-09-17T12:00:03Z",
            "payload": {
                "id": "assistant-1",
                "type": "message",
                "role": "assistant",
                "phase": "final_answer",
                "content": [{"type": "output_text", "text": "Codex completed safely."}],
            },
        },
        {
            "type": "event_msg",
            "timestamp": "2026-09-17T12:00:04Z",
            "payload": {
                "type": "task_complete",
                "turn_id": "turn-ok",
                "completed_at": "2026-09-17T12:00:04Z",
                "last_agent_message": "Codex completed safely.",
            },
        },
    ]


class SourceIsolationTests(unittest.TestCase):
    def setUp(self) -> None:
        FilesConnector._admission_state.clear()

    def test_files_without_glob_does_not_raise_and_is_collected(self) -> None:
        with tempfile.TemporaryDirectory(dir=str(TASK_TMP)) as directory:
            root = Path(directory) / "notes"
            root.mkdir()
            note = root / "alpha.md"
            note.write_text("owner note without glob", encoding="utf-8")
            _age(note)
            mapping = {
                "id": "owner-notes-no-glob",
                "tool": "files",
                "kind": "files",
                "roots": [{"path": str(root), "project_slug": "przyklad", "max_depth": 4}],
                "extensions": [".md"],
                "quiet_minutes": 15,
            }
            self.assertNotIn("glob", mapping)
            collection = None
            for _ in range(2):
                collection = collect_sources([mapping])
            self.assertIsNotNone(collection)
            self.assertTrue(
                any(event.event_type == FILE_EVENT_TYPE for event in collection.events)
            )
            self.assertFalse(
                any(
                    "glob" in issue.reason
                    for issue in collection.issues
                    if issue.source_key == "owner-notes-no-glob"
                )
            )

    def test_files_missing_required_extensions_is_rejected_without_exception(self) -> None:
        mapping = {
            "id": "owner-notes-missing-ext",
            "tool": "files",
            "kind": "files",
            "roots": [{"path": "C:/synthetic/notes", "project_slug": "x", "max_depth": 2}],
            "quiet_minutes": 15,
        }
        collection = collect_sources([mapping])
        matching = [
            issue
            for issue in collection.issues
            if issue.source_key == "owner-notes-missing-ext"
        ]
        self.assertTrue(matching)
        issue = matching[0]
        self.assertEqual(issue.status, "error")
        self.assertEqual(issue.reason, "missing_required_field:extensions")
        self.assertEqual(collection.events, ())

    def test_one_bad_source_does_not_drop_events_from_good_sources(self) -> None:
        with tempfile.TemporaryDirectory(dir=str(TASK_TMP)) as directory:
            jsonl_path = _write_jsonl(Path(directory) / "codex.jsonl", _codex_rows())
            notes = Path(directory) / "notes"
            notes.mkdir()
            note = notes / "keep.md"
            note.write_text("keep this file event", encoding="utf-8")
            _age(note)
            good_jsonl = {
                "id": "codex-good",
                "tool": "codex",
                "kind": "jsonl",
                "glob": str(jsonl_path),
            }
            good_files = {
                "id": "files-good",
                "tool": "files",
                "kind": "files",
                "roots": [{"path": str(notes), "project_slug": "przyklad", "max_depth": 2}],
                "extensions": [".md"],
            }
            bad_files = {
                "id": "files-bad",
                "tool": "files",
                "kind": "files",
                "roots": [{"path": str(notes), "project_slug": "x", "max_depth": 2}],
            }
            collection = None
            for _ in range(2):
                collection = collect_sources([bad_files, good_jsonl, good_files])
            tools = {event.tool for event in collection.events}
            self.assertIn("codex", tools)
            self.assertIn("files", tools)
            self.assertTrue(
                any(event.excerpt == "Codex completed safely." for event in collection.events)
            )
            self.assertTrue(
                any(event.event_type == FILE_EVENT_TYPE for event in collection.events)
            )

    def test_bad_source_is_visible_with_id_and_stable_reason(self) -> None:
        with tempfile.TemporaryDirectory(dir=str(TASK_TMP)) as directory:
            jsonl_path = _write_jsonl(Path(directory) / "codex.jsonl", _codex_rows())
            bad = {
                "id": "files-bad-visible",
                "tool": "files",
                "kind": "files",
                "roots": [],
            }
            good = {
                "id": "codex-good-visible",
                "tool": "codex",
                "kind": "jsonl",
                "glob": str(jsonl_path),
            }
            collection = collect_sources([bad, good])
            matching = [
                issue
                for issue in collection.issues
                if issue.source_key == "files-bad-visible"
            ]
            self.assertEqual(len(matching), 1)
            self.assertEqual(matching[0].reason, "missing_required_field:extensions")
            self.assertEqual(matching[0].status, "error")
            self.assertTrue(any(event.tool == "codex" for event in collection.events))

    def test_unexpected_exception_in_one_source_is_reported_not_raised(self) -> None:
        with tempfile.TemporaryDirectory(dir=str(TASK_TMP)) as directory:
            jsonl_path = _write_jsonl(Path(directory) / "codex.jsonl", _codex_rows())
            files_root = Path(directory) / "notes"
            files_root.mkdir()
            (files_root / "x.md").write_text("ok", encoding="utf-8")
            _age(files_root / "x.md")
            files_mapping = {
                "id": "files-boom",
                "tool": "files",
                "kind": "files",
                "roots": [{"path": str(files_root), "project_slug": "p", "max_depth": 2}],
                "extensions": [".md"],
            }
            jsonl_mapping = {
                "id": "codex-survives",
                "tool": "codex",
                "kind": "jsonl",
                "glob": str(jsonl_path),
            }
            original = FilesConnector.collect

            def boom(self, spec):
                raise KeyError("glob")

            with patch.object(FilesConnector, "collect", boom):
                collection = collect_sources([files_mapping, jsonl_mapping])
            matching = [issue for issue in collection.issues if issue.source_key == "files-boom"]
            self.assertEqual(len(matching), 1)
            self.assertEqual(matching[0].reason, "source_config_key_error")
            self.assertEqual(matching[0].status, "error")
            self.assertTrue(any(event.tool == "codex" for event in collection.events))
            # Restore real collector for other tests in this class.
            self.assertTrue(callable(original))


if __name__ == "__main__":
    unittest.main()
