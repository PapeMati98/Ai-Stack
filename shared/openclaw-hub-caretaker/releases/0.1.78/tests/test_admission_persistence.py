"""Admission cursor must survive a process restart, not just a class attribute."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from caretaker.connectors import FilesConnector, collect_sources, event_to_dict


TASK_TMP = _ROOT / ".test-tmp"
TASK_TMP.mkdir(parents=True, exist_ok=True)


def _age(path: Path, seconds_ago: float = 3600.0) -> None:
    stamp = time.time() - seconds_ago
    os.utime(str(path), (stamp, stamp))


def _spec(root: Path, state_dir: Path, *, source_id: str = "owner-notes-persist"):
    return {
        "tool": "files",
        "kind": "files",
        "source_id": source_id,
        "state_dir": str(state_dir),
        "roots": [{"path": str(root), "project_slug": "przyklad", "max_depth": 4}],
        "extensions": [".md"],
        "quiet_minutes": 15,
    }


def _simulate_process_restart() -> None:
    FilesConnector._admission_state.clear()


def _admitted_with_body(collection):
    return [
        event
        for event in collection.events
        if event.event_type == "owner_document.v1"
        and not event_to_dict(event).get("rejection_reason")
        and event.excerpt
    ]


class AdmissionPersistenceTests(unittest.TestCase):
    def setUp(self):
        FilesConnector._admission_state.clear()

    def test_stable_event_survives_process_restart_and_is_not_duplicated(self):
        body = "stable owner knowledge after restart"
        with tempfile.TemporaryDirectory(dir=str(TASK_TMP)) as d:
            base = Path(d)
            root = base / "notes"
            state_dir = base / "state"
            root.mkdir()
            state_dir.mkdir()
            note = root / "alpha.md"
            note.write_text(body, encoding="utf-8")
            _age(note)
            spec = _spec(root, state_dir)

            first = collect_sources([spec])
            self.assertFalse(_admitted_with_body(first))

            _simulate_process_restart()
            self.assertFalse(FilesConnector._admission_state)

            second = collect_sources([spec])
            admitted = _admitted_with_body(second)
            self.assertEqual(len(admitted), 1)
            self.assertEqual(admitted[0].excerpt, body)

            _simulate_process_restart()
            third = collect_sources([spec])
            self.assertEqual(_admitted_with_body(third), [])

            state_files = list(state_dir.glob("files-admission-*.json"))
            self.assertEqual(len(state_files), 1)
            raw = state_files[0].read_text(encoding="utf-8")
            self.assertNotIn(body, raw)
            payload = json.loads(raw)
            self.assertEqual(set(payload.keys()), {"pending", "stable"})
            self.assertTrue(payload["stable"])
            blob = json.dumps(payload)
            self.assertNotIn("excerpt", blob)
            self.assertNotIn("content", blob)

    def test_corrupt_state_is_missing_and_reported_without_raising(self):
        body = "owner knowledge after corrupt cursor"
        with tempfile.TemporaryDirectory(dir=str(TASK_TMP)) as d:
            base = Path(d)
            root = base / "notes"
            state_dir = base / "state"
            root.mkdir()
            state_dir.mkdir()
            note = root / "beta.md"
            note.write_text(body, encoding="utf-8")
            _age(note)
            spec = _spec(root, state_dir, source_id="owner-notes-corrupt")

            collect_sources([spec])
            state_files = list(state_dir.glob("files-admission-*.json"))
            self.assertEqual(len(state_files), 1)
            state_files[0].write_text("{not-json", encoding="utf-8")

            _simulate_process_restart()
            collection = collect_sources([spec])
            reasons = [issue.reason for issue in collection.issues]
            self.assertIn("admission_state_unreadable", reasons)
            self.assertFalse(_admitted_with_body(collection))

            _simulate_process_restart()
            recovered = collect_sources([spec])
            admitted = _admitted_with_body(recovered)
            self.assertEqual(len(admitted), 1)
            self.assertEqual(admitted[0].excerpt, body)


if __name__ == "__main__":
    unittest.main()
