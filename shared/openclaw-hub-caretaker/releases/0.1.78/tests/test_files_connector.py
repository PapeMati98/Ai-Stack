"""kind=files wiring through collect_sources / discover_source."""

from __future__ import annotations

import hashlib
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
    FILE_SPEAKER,
    NORMALIZED_EVENT_TYPE,
    UNASSIGNED_PROJECT,
    USER_MESSAGE_EVENT_TYPE,
    FilesConnector,
    SourceSpec,
    collect_sources,
    discover_source,
    event_to_dict,
    export_normalized_events,
)
from caretaker.ingest import _normalize_normalized_event
from caretaker.model import _record_json
from caretaker.pipeline import SynthesisRecord


TASK_TMP = _ROOT / ".test-tmp"
TASK_TMP.mkdir(parents=True, exist_ok=True)

DUMMY_AWS = "AKIA" + ("A" * 16)


def _age(path: Path, seconds_ago: float = 3600.0) -> None:
    stamp = time.time() - seconds_ago
    os.utime(str(path), (stamp, stamp))


def _spec_mapping(root: Path, *, source_id: str = "owner-notes", **extra):
    mapping = {
        "tool": "files",
        "kind": "files",
        "source_id": source_id,
        "roots": extra.pop("roots", [{"path": str(root), "project_slug": extra.pop("project_slug", "przyklad"), "max_depth": extra.pop("max_depth", 4)}]),
        "extensions": extra.pop("extensions", [".md", ".txt"]),
        "quiet_minutes": extra.pop("quiet_minutes", 15),
    }
    mapping.update(extra)
    return mapping


def _collect_stable(sources, ticks: int = 2):
    collection = None
    for _ in range(ticks):
        collection = collect_sources(sources)
    return collection


class FilesConnectorPipelineTests(unittest.TestCase):
    def setUp(self):
        FilesConnector._admission_state.clear()

    def test_discover_and_collect_recognize_kind_files(self):
        with tempfile.TemporaryDirectory(dir=str(TASK_TMP)) as d:
            root = Path(d) / "notes"
            root.mkdir()
            note = root / "alpha.md"
            note.write_text("owner note body", encoding="utf-8")
            _age(note)

            mapping = _spec_mapping(root, source_id="owner-notes-discover")
            discovery = discover_source(mapping)
            self.assertEqual(discovery.kind, "files")
            self.assertEqual(discovery.status, "supported")

            spec = SourceSpec(
                tool="files",
                path="",
                kind="files",
                source_id="owner-notes-discover",
                roots=mapping["roots"],
                extensions=(".md", ".txt"),
                quiet_minutes=15,
            )
            collection = _collect_stable([spec])
            self.assertTrue(
                any(event.event_type == FILE_EVENT_TYPE for event in collection.events)
            )
            self.assertIsInstance(FilesConnector(), FilesConnector)

    def test_accepted_file_has_relative_path_sha256_mtime_and_source_id(self):
        with tempfile.TemporaryDirectory(dir=str(TASK_TMP)) as d:
            root = Path(d) / "docs"
            nested = root / "sub"
            nested.mkdir(parents=True)
            body = "stable owner knowledge"
            path = nested / "guide.md"
            path.write_text(body, encoding="utf-8")
            _age(path)
            digest = hashlib.sha256(body.encode("utf-8")).hexdigest()

            collection = _collect_stable(
                [_spec_mapping(root, source_id="owner-notes-accepted", extensions=[".md"], project_slug="przyklad")]
            )
            admitted = [e for e in collection.events if not event_to_dict(e).get("rejection_reason")]
            self.assertEqual(len(admitted), 1)
            event = admitted[0]
            payload = event_to_dict(event)
            self.assertEqual(payload["relative_path"], "sub/guide.md")
            self.assertEqual(payload["sha256"], digest)
            self.assertEqual(payload["source_id"], "owner-notes-accepted")
            self.assertTrue(payload["mtime"])
            self.assertEqual(payload["cursor"], digest)
            self.assertEqual(event.source_ref["relative_path"], "sub/guide.md")
            self.assertEqual(event.source_ref["sha256"], digest)
            self.assertEqual(event.source_ref["source_id"], "owner-notes-accepted")
            self.assertEqual(
                hashlib.sha256(body.encode("utf-8")).hexdigest(),
                event.source_ref["sha256"],
            )

    def test_file_event_type_is_distinct_from_turn_and_user_message(self):
        with tempfile.TemporaryDirectory(dir=str(TASK_TMP)) as d:
            root = Path(d)
            path = root / "note.md"
            path.write_text("hello", encoding="utf-8")
            _age(path)
            collection = _collect_stable(
                [_spec_mapping(root, source_id="owner-notes-type", project_slug="x", max_depth=2, extensions=[".md"])]
            )
            self.assertTrue(collection.events)
            event = collection.events[0]
            self.assertEqual(event.event_type, FILE_EVENT_TYPE)
            self.assertNotEqual(event.event_type, NORMALIZED_EVENT_TYPE)
            self.assertNotEqual(event.event_type, USER_MESSAGE_EVENT_TYPE)
            self.assertNotEqual(FILE_EVENT_TYPE, "completed_turn.v1")
            self.assertNotEqual(FILE_EVENT_TYPE, "user_message.v1")

    def test_secret_rejection_stores_no_content_fragment(self):
        secret_line = f"key = {DUMMY_AWS} keep-this-unique-fragment"
        with tempfile.TemporaryDirectory(dir=str(TASK_TMP)) as d:
            root = Path(d)
            path = root / "leaked.md"
            path.write_text(secret_line, encoding="utf-8")
            _age(path)
            collection = _collect_stable(
                [_spec_mapping(root, source_id="owner-notes-secret", project_slug="x", max_depth=2, extensions=[".md"])]
            )
            dumped = json.dumps(
                {
                    "events": [event_to_dict(e) for e in collection.events],
                    "issues": [i.to_dict() for i in collection.issues],
                },
                ensure_ascii=False,
            )
            self.assertNotIn(DUMMY_AWS, dumped)
            self.assertNotIn("keep-this-unique-fragment", dumped)
            self.assertNotIn(secret_line, dumped)
            rejected = [
                event_to_dict(e)
                for e in collection.events
                if event_to_dict(e).get("rejection_reason")
            ]
            self.assertTrue(rejected)
            payload = rejected[0]
            self.assertEqual(payload["excerpt"], "")
            self.assertEqual(payload["relative_path"], "leaked.md")
            self.assertTrue(payload.get("rejection_reason"))
            self.assertTrue(payload.get("rule_id"))
            self.assertNotIn(DUMMY_AWS, payload["title"])
            record = _normalize_normalized_event(payload, "files", 1)
            self.assertIsNotNone(record)
            self.assertEqual(record.body, "")
            self.assertNotIn("keep-this-unique-fragment", json.dumps(record.raw, ensure_ascii=False))

    def test_root_without_project_slug_is_unassigned_not_guessed(self):
        with tempfile.TemporaryDirectory(dir=str(TASK_TMP)) as d:
            guessed = Path(d) / "guessed-project-name"
            guessed.mkdir()
            path = guessed / "note.md"
            path.write_text("plain owner text", encoding="utf-8")
            _age(path)
            collection = _collect_stable(
                [
                    {
                        "tool": "files",
                        "kind": "files",
                        "source_id": "owner-notes-unassigned",
                        "roots": [{"path": str(guessed), "max_depth": 2}],
                        "extensions": [".md"],
                    }
                ]
            )
            admitted = [
                event_to_dict(e)
                for e in collection.events
                if not event_to_dict(e).get("rejection_reason")
            ]
            self.assertEqual(len(admitted), 1)
            self.assertEqual(admitted[0]["project"], UNASSIGNED_PROJECT)
            self.assertNotEqual(admitted[0]["project"], "guessed-project-name")
            self.assertNotIn("guessed-project-name", admitted[0]["project"])

    def test_unchanged_file_not_reemitted_and_payloads_are_byte_identical(self):
        with tempfile.TemporaryDirectory(dir=str(TASK_TMP)) as d:
            root = Path(d)
            path = root / "stable.md"
            path.write_text("same forever", encoding="utf-8")
            _age(path)
            spec = _spec_mapping(root, source_id="owner-notes-idempotent", extensions=[".md"])
            first = _collect_stable([spec])
            admitted = [e for e in first.events if not event_to_dict(e).get("rejection_reason")]
            self.assertEqual(len(admitted), 1)
            payload_a = json.dumps(event_to_dict(admitted[0]), sort_keys=True, separators=(",", ":"))
            second = collect_sources([spec])
            self.assertFalse(
                [e for e in second.events if not event_to_dict(e).get("rejection_reason")]
            )
            FilesConnector._admission_state.clear()
            again = _collect_stable([spec])
            admitted_b = [e for e in again.events if not event_to_dict(e).get("rejection_reason")]
            payload_b = json.dumps(event_to_dict(admitted_b[0]), sort_keys=True, separators=(",", ":"))
            self.assertEqual(payload_a, payload_b)

    def test_identical_content_two_paths_yield_two_events(self):
        with tempfile.TemporaryDirectory(dir=str(TASK_TMP)) as d:
            root = Path(d)
            body = "duplicate note body"
            (root / "a.md").write_text(body, encoding="utf-8")
            (root / "b.md").write_text(body, encoding="utf-8")
            _age(root / "a.md")
            _age(root / "b.md")
            collection = _collect_stable(
                [_spec_mapping(root, source_id="owner-notes-dup", extensions=[".md"])]
            )
            admitted = [event_to_dict(e) for e in collection.events if not event_to_dict(e).get("rejection_reason")]
            paths = sorted(item["relative_path"] for item in admitted)
            self.assertEqual(paths, ["a.md", "b.md"])
            self.assertEqual(len({item["event_id"] for item in admitted}), 2)

    def test_deferred_overflow_admits_tail_on_later_tick(self):
        with tempfile.TemporaryDirectory(dir=str(TASK_TMP)) as d:
            root = Path(d)
            (root / "a.md").write_text("one", encoding="utf-8")
            (root / "b.md").write_text("two", encoding="utf-8")
            _age(root / "a.md")
            _age(root / "b.md")
            spec = _spec_mapping(
                root,
                source_id="owner-notes-overflow",
                extensions=[".md"],
                max_files_per_tick=1,
            )
            collect_sources([spec])
            second = collect_sources([spec])
            admitted_second = [
                event_to_dict(e)["relative_path"]
                for e in second.events
                if not event_to_dict(e).get("rejection_reason")
            ]
            self.assertEqual(len(admitted_second), 1)
            self.assertTrue(any(issue.reason == "deferred_overflow" for issue in second.issues))
            third = collect_sources([spec])
            admitted_third = [
                event_to_dict(e)["relative_path"]
                for e in third.events
                if not event_to_dict(e).get("rejection_reason")
            ]
            self.assertEqual(len(admitted_third), 1)
            self.assertNotEqual(admitted_second[0], admitted_third[0])

    def test_mutate_between_scan_and_excerpt_skips_stale_bytes(self):
        with tempfile.TemporaryDirectory(dir=str(TASK_TMP)) as d:
            root = Path(d)
            path = root / "race.md"
            original = "original gated body"
            path.write_text(original, encoding="utf-8")
            _age(path)
            spec = _spec_mapping(root, source_id="owner-notes-race", extensions=[".md"])
            collect_sources([spec])
            import caretaker.files as files_mod

            orig_scan = files_mod.scan_files

            def mutating_scan(*args, **kwargs):
                candidates, rejections, stats = orig_scan(*args, **kwargs)
                for candidate in candidates:
                    Path(candidate.absolute_path).write_text(
                        "mutated-after-gate-secret-fragment", encoding="utf-8"
                    )
                return candidates, rejections, stats

            with patch.object(files_mod, "scan_files", side_effect=mutating_scan):
                collection = collect_sources([spec])
            dumped = json.dumps(
                [event_to_dict(e) for e in collection.events], ensure_ascii=False
            )
            self.assertNotIn("mutated-after-gate-secret-fragment", dumped)
            self.assertTrue(
                any(issue.reason == "file_changed_after_admission" for issue in collection.issues)
            )

    def test_injected_environment_block_stripped_from_excerpt(self):
        with tempfile.TemporaryDirectory(dir=str(TASK_TMP)) as d:
            root = Path(d)
            body = "keep this fact\n<environment>\ninjected command\n</environment>\nmore fact\n"
            (root / "doc.md").write_text(body, encoding="utf-8")
            _age(root / "doc.md")
            collection = _collect_stable(
                [_spec_mapping(root, source_id="owner-notes-inject", extensions=[".md"])]
            )
            admitted = [event_to_dict(e) for e in collection.events if not event_to_dict(e).get("rejection_reason")]
            self.assertEqual(len(admitted), 1)
            excerpt = admitted[0]["excerpt"]
            self.assertIn("keep this fact", excerpt)
            self.assertNotIn("<environment>", excerpt)
            self.assertNotIn("injected command", excerpt)

    def test_synthesis_record_speaker_is_not_assistant(self):
        with tempfile.TemporaryDirectory(dir=str(TASK_TMP)) as d:
            root = Path(d)
            (root / "note.md").write_text("owner file text", encoding="utf-8")
            _age(root / "note.md")
            collection = _collect_stable(
                [_spec_mapping(root, source_id="owner-notes-speaker", extensions=[".md"])]
            )
            event = collection.events[0]
            payload = event_to_dict(event)
            self.assertNotEqual(payload.get("speaker"), "assistant")
            self.assertEqual(payload.get("speaker"), FILE_SPEAKER)
            record = _normalize_normalized_event(payload, "files", 1)
            self.assertIsNotNone(record)
            self.assertNotEqual(record.speaker, "assistant")
            rendered = _record_json(
                SynthesisRecord(
                    record_index=0,
                    identity_tool="files",
                    native_session_id=record.native_session_id,
                    cursor=record.cursor,
                    content_hash="abc",
                    title=record.title,
                    excerpt=record.body,
                    speaker=record.speaker,
                    observed_at=record.observed_at,
                    event_type=record.event_type,
                ),
                0,
            )
            self.assertNotEqual(rendered["speaker"], "assistant")

    def test_export_manifest_attributes_files_source(self):
        with tempfile.TemporaryDirectory(dir=str(TASK_TMP)) as d:
            root = Path(d)
            (root / "note.md").write_text("export me", encoding="utf-8")
            _age(root / "note.md")
            spec = _spec_mapping(root, source_id="owner-notes-export", extensions=[".md"])
            collect_sources([spec])
            out_dir = Path(d) / "export"
            out_dir.mkdir()
            result = export_normalized_events([spec], out_dir)
            manifest = json.loads(Path(result.manifest_path).read_text(encoding="utf-8"))
            files_sources = [row for row in manifest["sources"] if row.get("kind") == "files" or row.get("tool") == "files"]
            self.assertTrue(files_sources)
            self.assertGreaterEqual(files_sources[0]["records_emitted"], 1)
            cursors = json.loads(Path(result.cursor_path).read_text(encoding="utf-8"))
            self.assertTrue(cursors.get("sources"))


if __name__ == "__main__":
    loader = unittest.TestLoader()
    suite = loader.loadTestsFromModule(sys.modules[__name__])
    import tests.test_connectors as _test_connectors
    import tests.test_ingest as _test_ingest

    suite.addTests(loader.loadTestsFromModule(_test_connectors))
    suite.addTests(loader.loadTestsFromModule(_test_ingest))
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    sys.exit(0 if result.wasSuccessful() else 1)
