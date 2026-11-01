"""kind=files through the real maintenance tick (no glob, no scans table)."""

from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import maintenance
from caretaker.connectors import FILE_EVENT_TYPE, FilesConnector, collect_sources


TASK_TMP = Path(__file__).resolve().parents[1] / ".test-tmp"
DUMMY_AWS = "AKIA" + ("A" * 16)


def _age(path: Path, seconds_ago: float = 3600.0) -> None:
    stamp = time.time() - seconds_ago
    os.utime(str(path), (stamp, stamp))


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


def _write_jsonl(path: Path, rows: list[dict]) -> Path:
    path.write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
        encoding="utf-8",
    )
    return path


def _control_fixture(root: Path, sources: list[dict]) -> dict:
    base_config = root / "base-config.json"
    base_config.write_text(
        json.dumps(
            {
                "project_slug": "base",
                "agent": "hub-caretaker",
                "session_id": "base-session",
                "state_dir": str(root / "base-state"),
                "hub": {"vault": str(root / "vault")},
                "allowlist": {"tools": ["codex", "files"]},
                "primary": {},
                "office": {},
                "limits": {},
            }
        ),
        encoding="utf-8",
    )
    return {
        "state_dir": str(root / "control-state"),
        "base_config": str(base_config),
        "python": "python",
        "sources": sources,
        "known_projects": ["przyklad"],
        "max_sources_per_tick": 8,
        "max_projects_per_tick": 0,
        "publication": {"enabled": False},
        "retry_epoch": "epoch-files-1",
    }


def _queue_rows(state: Path):
    db = sqlite3.connect(state / "intake.sqlite")
    rows = list(db.execute("SELECT id, project, payload FROM queue"))
    scans = list(db.execute("SELECT path FROM scans"))
    db.close()
    payloads = [json.loads(r[2]) for r in rows]
    return rows, payloads, scans


class FilesSourceTickIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        FilesConnector._admission_state.clear()
        FilesConnector._rejection_state.clear()
        TASK_TMP.mkdir(parents=True, exist_ok=True)

    def test_historical_file_enters_opted_in_new_lane_once(self) -> None:
        with tempfile.TemporaryDirectory(dir=str(TASK_TMP)) as directory:
            root = Path(directory)
            (root / 'vault').mkdir()
            notes = root / 'notes'
            notes.mkdir()
            note = notes / 'old-project.md'
            note.write_text('Verified historical project design notes.', encoding='utf-8')
            _age(note, 86400)
            source = {
                'id': 'owner-notes-epoch2', 'tool': 'files', 'kind': 'files',
                'status': 'supported', 'scheduled': True,
                'file_observation_mode': 'scan_time',
                'roots': [{'path': str(notes), 'project_slug': 'przyklad', 'max_depth': 2}],
                'extensions': ['.md'], 'quiet_minutes': 0,
            }
            control = _control_fixture(root, [source])
            control['new_event_epoch'] = {'enabled': True, 'activate': True}
            control['document_epoch'] = {
                'enabled': True, 'source_ids': ['owner-notes-epoch2']}
            state = Path(control['state_dir'])
            state.mkdir(parents=True, exist_ok=True)
            maintenance._locked_tick(control, state)
            second = maintenance._locked_tick(control, state)
            self.assertEqual(second['new_lane']['admitted_events'], 1)
            self.assertEqual(second['new_lane']['pending_events'], 1)
            third = maintenance._locked_tick(control, state)
            self.assertEqual(third['new_events'], 0)
            self.assertEqual(third['new_lane']['admitted_events'], 1)

    def test_cutover_rescans_old_source_with_new_identity_without_legacy_drain(self) -> None:
        with tempfile.TemporaryDirectory(dir=str(TASK_TMP)) as directory:
            root = Path(directory)
            (root / 'vault').mkdir()
            notes = root / 'notes'
            notes.mkdir()
            note = notes / 'design.md'
            note.write_text('Historical owner project architecture.', encoding='utf-8')
            _age(note, 86400)
            old = {
                'id': 'owner-files-v1', 'tool': 'files', 'kind': 'files',
                'status': 'supported', 'scheduled': True,
                'roots': [{'path': str(notes), 'project_slug': 'przyklad', 'max_depth': 2}],
                'extensions': ['.md'], 'quiet_minutes': 0,
            }
            control = _control_fixture(root, [old])
            state = Path(control['state_dir'])
            state.mkdir(parents=True, exist_ok=True)
            maintenance._locked_tick(control, state)
            maintenance._locked_tick(control, state)
            db = sqlite3.connect(state / 'intake.sqlite')
            old_ids = {row[0] for row in db.execute('SELECT id FROM queue')}
            self.assertEqual(len(old_ids), 1)
            db.close()

            new = dict(old, id='owner-files-v2', file_observation_mode='scan_time')
            control['sources'] = [new]
            control['new_event_epoch'] = {'enabled': True, 'activate': True}
            control['document_epoch'] = {
                'enabled': True, 'source_ids': ['owner-files-v2']}
            first = maintenance._locked_tick(control, state)
            self.assertEqual(first['new_lane']['baseline_events'], 1)
            second = maintenance._locked_tick(control, state)
            self.assertEqual(second['new_lane']['admitted_events'], 1)
            self.assertEqual(second['new_lane']['baseline_events'], 1)
            db = sqlite3.connect(state / 'intake.sqlite')
            new_ids = {row[0] for row in db.execute('SELECT id FROM queue')} - old_ids
            self.assertEqual(len(new_ids), 1)
            self.assertEqual(db.execute('SELECT count(*) FROM queue WHERE forwarded=0').fetchone()[0], 2)
            db.close()

    def test_excluded_subtree_is_not_admitted(self) -> None:
        with tempfile.TemporaryDirectory(dir=str(TASK_TMP)) as directory:
            root = Path(directory)
            notes = root / 'notes'
            logs = notes / 'logs'
            logs.mkdir(parents=True)
            good = notes / 'design.md'
            ignored = logs / 'trace.md'
            good.write_text('Reusable design evidence.', encoding='utf-8')
            ignored.write_text('Transient log should stay outside the Hub.', encoding='utf-8')
            _age(good)
            _age(ignored)
            source = {
                'id': 'owner-notes-excluded', 'tool': 'files', 'kind': 'files',
                'status': 'supported', 'scheduled': True,
                'roots': [{'path': str(notes), 'project_slug': 'przyklad', 'max_depth': 3}],
                'extensions': ['.md'], 'quiet_minutes': 0,
                'exclude_directory_names': ['logs'],
            }
            control = _control_fixture(root, [source])
            state = Path(control['state_dir'])
            state.mkdir(parents=True, exist_ok=True)
            maintenance._locked_tick(control, state)
            maintenance._locked_tick(control, state)
            _rows, events, _scans = _queue_rows(state)
            self.assertEqual(len(events), 1)
            self.assertEqual(events[0]['source_ref']['relative_path'], 'design.md')

    def test_files_source_without_glob_does_not_raise_keyerror(self) -> None:
        with tempfile.TemporaryDirectory(dir=str(TASK_TMP)) as directory:
            root = Path(directory)
            notes = root / "notes"
            notes.mkdir()
            note = notes / "alpha.md"
            note.write_text("owner note without glob", encoding="utf-8")
            _age(note)
            source = {
                "id": "owner-notes",
                "tool": "files",
                "kind": "files",
                "status": "supported",
                "scheduled": True,
                "roots": [{"path": str(notes), "project_slug": "przyklad", "max_depth": 4}],
                "extensions": [".md"],
                "quiet_minutes": 0,
            }
            self.assertNotIn("glob", source)
            control = _control_fixture(root, [source])
            state = Path(control["state_dir"])
            state.mkdir(parents=True, exist_ok=True)
            report = maintenance._locked_tick(control, state)
            self.assertNotEqual(report.get("status"), "ALREADY_RUNNING")
            reasons = [str(item.get("reason", "")) for item in report.get("source_issues", [])]
            self.assertFalse(any("KeyError" == r or "glob" in r.lower() for r in reasons))

    def test_stable_file_reaches_queue_as_owner_document(self) -> None:
        with tempfile.TemporaryDirectory(dir=str(TASK_TMP)) as directory:
            root = Path(directory)
            notes = root / "notes"
            notes.mkdir()
            body = "stable owner knowledge for the hub"
            note = notes / "guide.md"
            note.write_text(body, encoding="utf-8")
            _age(note)
            source = {
                "id": "owner-notes-queue",
                "tool": "files",
                "kind": "files",
                "roots": [{"path": str(notes), "project_slug": "przyklad", "max_depth": 2}],
                "extensions": [".md", ".txt"],
                "quiet_minutes": 0,
            }
            control = _control_fixture(root, [source])
            state = Path(control["state_dir"])
            state.mkdir(parents=True, exist_ok=True)
            maintenance._locked_tick(control, state)
            report = maintenance._locked_tick(control, state)
            rows, payloads, scans = _queue_rows(state)
            admitted = [
                p
                for p in payloads
                if p.get("event_type") == FILE_EVENT_TYPE and not p.get("rejection_reason")
            ]
            self.assertTrue(admitted, msg=f"report={report} payloads={payloads}")
            event = admitted[0]
            self.assertEqual(event["event_type"], "owner_document.v1")
            self.assertIn(body, event.get("excerpt", ""))
            self.assertEqual(event.get("relative_path"), "guide.md")
            self.assertEqual(len(rows), len(admitted))
            self.assertEqual(scans, [])

    def test_secret_rejected_file_is_not_queued_with_content(self) -> None:
        with tempfile.TemporaryDirectory(dir=str(TASK_TMP)) as directory:
            root = Path(directory)
            notes = root / "notes"
            notes.mkdir()
            secret_line = f"key = {DUMMY_AWS} keep-this-unique-fragment"
            note = notes / "leaked.md"
            note.write_text(secret_line, encoding="utf-8")
            _age(note)
            source = {
                "id": "owner-notes-secret",
                "tool": "files",
                "kind": "files",
                "roots": [{"path": str(notes), "project_slug": "przyklad", "max_depth": 2}],
                "extensions": [".md"],
                "quiet_minutes": 0,
            }
            control = _control_fixture(root, [source])
            state = Path(control["state_dir"])
            state.mkdir(parents=True, exist_ok=True)
            maintenance._locked_tick(control, state)
            maintenance._locked_tick(control, state)
            _rows, payloads, _scans = _queue_rows(state)
            dumped = json.dumps(payloads, ensure_ascii=False)
            self.assertNotIn(DUMMY_AWS, dumped)
            self.assertNotIn("keep-this-unique-fragment", dumped)
            self.assertNotIn(secret_line, dumped)
            self.assertFalse(
                any(
                    p.get("excerpt")
                    for p in payloads
                    if p.get("event_type") == FILE_EVENT_TYPE
                )
            )

    def test_files_source_collected_once_per_tick_as_whole(self) -> None:
        with tempfile.TemporaryDirectory(dir=str(TASK_TMP)) as directory:
            root = Path(directory)
            notes = root / "notes"
            notes.mkdir()
            (notes / "a.md").write_text("first file", encoding="utf-8")
            (notes / "b.md").write_text("second file", encoding="utf-8")
            _age(notes / "a.md")
            _age(notes / "b.md")
            source = {
                "id": "owner-notes-once",
                "tool": "files",
                "kind": "files",
                "roots": [{"path": str(notes), "project_slug": "przyklad", "max_depth": 2}],
                "extensions": [".md"],
                "quiet_minutes": 0,
            }
            control = _control_fixture(root, [source])
            state = Path(control["state_dir"])
            state.mkdir(parents=True, exist_ok=True)
            calls: list[list] = []
            real = collect_sources

            def wrapped(specs):
                calls.append(list(specs))
                return real(specs)

            with patch("maintenance.collect_sources", side_effect=wrapped):
                maintenance._locked_tick(control, state)
                maintenance._locked_tick(control, state)
            self.assertEqual(len(calls), 2)
            for batch in calls:
                self.assertEqual(len(batch), 1)
                spec = batch[0]
                kind = spec.get("kind") if isinstance(spec, dict) else getattr(spec, "kind", "")
                self.assertEqual(kind, "files")

    def test_one_bad_source_does_not_stop_remaining_sources(self) -> None:
        with tempfile.TemporaryDirectory(dir=str(TASK_TMP)) as directory:
            root = Path(directory)
            notes = root / "notes"
            notes.mkdir()
            note = notes / "keep.md"
            note.write_text("keep this file event", encoding="utf-8")
            _age(note)
            jsonl_path = _write_jsonl(root / "codex.jsonl", _codex_rows())
            bad = {
                "id": "files-bad",
                "tool": "files",
                "kind": "files",
                "roots": [{"path": str(notes), "project_slug": "x", "max_depth": 2}],
            }
            good_files = {
                "id": "files-good",
                "tool": "files",
                "kind": "files",
                "roots": [{"path": str(notes), "project_slug": "przyklad", "max_depth": 2}],
                "extensions": [".md"],
                "quiet_minutes": 0,
            }
            good_jsonl = {
                "id": "codex-good",
                "tool": "codex",
                "kind": "jsonl",
                "glob": str(jsonl_path),
            }
            control = _control_fixture(root, [bad, good_jsonl, good_files])
            state = Path(control["state_dir"])
            state.mkdir(parents=True, exist_ok=True)
            maintenance._locked_tick(control, state)
            report = maintenance._locked_tick(control, state)
            issues = report.get("source_issues") or []
            matching = [
                item
                for item in issues
                if item.get("source") == "files-bad" or item.get("source_key") == "files-bad"
            ]
            self.assertTrue(matching, msg=issues)
            self.assertEqual(matching[0].get("status"), "error")
            self.assertTrue(matching[0].get("reason"))
            _rows, payloads, scans = _queue_rows(state)
            tools = {p.get("tool") for p in payloads}
            self.assertIn("codex", tools)
            self.assertIn("files", tools)
            self.assertTrue(any(p.get("event_type") == FILE_EVENT_TYPE for p in payloads))
            self.assertTrue(
                any("Codex completed safely." in (p.get("excerpt") or "") for p in payloads)
            )
            self.assertTrue(any(str(jsonl_path) == s[0] or jsonl_path.name in s[0] for s in scans))

    def test_jsonl_glob_path_still_queues_events(self) -> None:
        with tempfile.TemporaryDirectory(dir=str(TASK_TMP)) as directory:
            root = Path(directory)
            jsonl_path = _write_jsonl(root / "codex.jsonl", _codex_rows())
            source = {
                "id": "codex-only",
                "tool": "codex",
                "kind": "jsonl",
                "glob": str(jsonl_path),
            }
            control = _control_fixture(root, [source])
            state = Path(control["state_dir"])
            state.mkdir(parents=True, exist_ok=True)
            report = maintenance._locked_tick(control, state)
            self.assertGreaterEqual(report.get("new_events", 0), 1)
            _rows, payloads, scans = _queue_rows(state)
            self.assertTrue(payloads)
            self.assertTrue(any(p.get("tool") == "codex" for p in payloads))
            self.assertTrue(scans)


if __name__ == "__main__":
    unittest.main()
