"""Owner documents larger than max_excerpt_bytes split losslessly."""
from __future__ import annotations

import hashlib
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

from caretaker.connectors import (
    FILE_EVENT_TYPE,
    FilesConnector,
    collect_sources,
    event_to_dict,
)

TASK_TMP = _ROOT / ".test-tmp"
TASK_TMP.mkdir(parents=True, exist_ok=True)

DUMMY_AWS = "AKIA" + ("A" * 16)
MAX_EXCERPT = 1600


def _age(path: Path, seconds_ago: float = 3600.0) -> None:
    stamp = time.time() - seconds_ago
    os.utime(str(path), (stamp, stamp))


def _spec(root: Path, *, source_id: str, **extra):
    mapping = {
        "tool": "files",
        "kind": "files",
        "source_id": source_id,
        "roots": [{"path": str(root), "project_slug": extra.pop("project_slug", "przyklad"), "max_depth": extra.pop("max_depth", 4)}],
        "extensions": extra.pop("extensions", [".md", ".txt"]),
        "quiet_minutes": extra.pop("quiet_minutes", 15),
        "max_excerpt_bytes": extra.pop("max_excerpt_bytes", MAX_EXCERPT),
        "max_file_bytes": extra.pop("max_file_bytes", 262144),
    }
    mapping.update(extra)
    return mapping


def _collect_stable(sources, ticks: int = 2):
    collection = None
    for _ in range(ticks):
        collection = collect_sources(sources)
    return collection


def _admitted(collection):
    return [
        event_to_dict(e)
        for e in collection.events
        if e.event_type == FILE_EVENT_TYPE and not event_to_dict(e).get("rejection_reason")
    ]


class DocumentChunkingTests(unittest.TestCase):
    def setUp(self):
        FilesConnector._admission_state.clear()

    def test_oversized_document_chunks_reassemble_without_loss_or_dup(self):
        body = ("owner-knowledge-block-%04d\n" % 0) * 400
        self.assertGreater(len(body.encode("utf-8")), MAX_EXCERPT * 3)
        with tempfile.TemporaryDirectory(dir=str(TASK_TMP)) as d:
            root = Path(d)
            path = root / "long.md"
            path.write_bytes(body.encode("utf-8"))
            _age(path)
            collection = _collect_stable([_spec(root, source_id="chunk-reassemble")])
            admitted = _admitted(collection)
            self.assertGreaterEqual(len(admitted), 4)
            counts = {item["chunk_count"] for item in admitted}
            self.assertEqual(counts, {len(admitted)})
            indexes = [item["chunk_index"] for item in admitted]
            self.assertEqual(indexes, list(range(len(admitted))))
            joined = "".join(item["excerpt"] for item in admitted)
            self.assertEqual(joined, body)
            self.assertEqual(len(joined), len(body))
            for item in admitted:
                self.assertTrue(item["excerpt"])
                self.assertLessEqual(len(item["excerpt"].encode("utf-8")), MAX_EXCERPT)
                self.assertEqual(item["event_type"], FILE_EVENT_TYPE)
                self.assertEqual(item["completion_kind"], "owner_document")

    def test_chunk_index_and_count_are_consistent(self):
        body = "abcdefghij" * 500
        with tempfile.TemporaryDirectory(dir=str(TASK_TMP)) as d:
            root = Path(d)
            (root / "note.md").write_bytes(body.encode("utf-8"))
            _age(root / "note.md")
            admitted = _admitted(_collect_stable([_spec(root, source_id="chunk-meta")]))
            total = admitted[0]["chunk_count"]
            self.assertEqual(total, len(admitted))
            self.assertGreater(total, 1)
            for i, item in enumerate(admitted):
                self.assertEqual(item["chunk_index"], i)
                self.assertEqual(item["chunk_count"], total)

    def test_same_file_chunks_have_distinct_cursors_and_rescan_is_idle(self):
        body = ("stable-document-line\n" * 300)
        self.assertGreater(len(body.encode("utf-8")), MAX_EXCERPT)
        with tempfile.TemporaryDirectory(dir=str(TASK_TMP)) as d:
            root = Path(d)
            (root / "stable.md").write_bytes(body.encode("utf-8"))
            _age(root / "stable.md")
            spec = _spec(root, source_id="chunk-cursors")
            first = _collect_stable([spec])
            admitted = _admitted(first)
            self.assertGreater(len(admitted), 1)
            cursors = [item["cursor"] for item in admitted]
            self.assertEqual(len(cursors), len(set(cursors)))
            digest = hashlib.sha256(body.encode("utf-8")).hexdigest()
            for item in admitted:
                self.assertNotEqual(item["cursor"], digest)
                self.assertTrue(item["cursor"].startswith(digest))
            second = collect_sources([spec])
            self.assertEqual(_admitted(second), [])

    def test_secret_past_first_chunk_rejects_whole_file(self):
        prefix = "safe-owner-text-" * 200
        self.assertGreater(len(prefix.encode("utf-8")), MAX_EXCERPT)
        body = prefix + "\nkey = " + DUMMY_AWS + " keep-this-unique-fragment\n"
        with tempfile.TemporaryDirectory(dir=str(TASK_TMP)) as d:
            root = Path(d)
            (root / "leaked.md").write_bytes(body.encode("utf-8"))
            _age(root / "leaked.md")
            collection = _collect_stable([_spec(root, source_id="chunk-secret-tail")])
            dumped = json.dumps(
                {
                    "events": [event_to_dict(e) for e in collection.events],
                    "issues": [i.to_dict() for i in collection.issues],
                },
                ensure_ascii=False,
            )
            self.assertNotIn(DUMMY_AWS, dumped)
            self.assertNotIn("keep-this-unique-fragment", dumped)
            admitted = _admitted(collection)
            self.assertEqual(admitted, [])
            for event in collection.events:
                payload = event_to_dict(event)
                self.assertEqual(payload.get("excerpt"), "")
                self.assertTrue(payload.get("rejection_reason"))

    def test_file_with_content_never_emits_empty_owner_document_excerpt(self):
        bodies = [
            "short owner note",
            ("wide-body-segment\n" * 250),
        ]
        with tempfile.TemporaryDirectory(dir=str(TASK_TMP)) as d:
            root = Path(d)
            for name, body in zip(("short.md", "long.md"), bodies):
                (root / name).write_bytes(body.encode("utf-8"))
                _age(root / name)
            collection = _collect_stable([_spec(root, source_id="chunk-no-empty")])
            admitted = _admitted(collection)
            self.assertTrue(admitted)
            for item in admitted:
                self.assertEqual(item["event_type"], FILE_EVENT_TYPE)
                self.assertTrue(item["excerpt"].strip(), msg=item.get("relative_path"))
                self.assertNotEqual(item["excerpt"], "")

    def test_whitespace_only_file_emits_untransferable_issue_not_event(self):
        with tempfile.TemporaryDirectory(dir=str(TASK_TMP)) as d:
            root = Path(d)
            (root / "blank.md").write_bytes(b"\n\n  \n \n")
            _age(root / "blank.md")
            collection = _collect_stable([_spec(root, source_id="chunk-ws-only")])
            self._assert_untransferable_no_excerpt(collection)

    def test_injected_only_file_emits_untransferable_issue_not_event(self):
        body = "<environment>\ninjected context that must not become knowledge\n</environment>\n"
        with tempfile.TemporaryDirectory(dir=str(TASK_TMP)) as d:
            root = Path(d)
            (root / "injected.md").write_bytes(body.encode("utf-8"))
            _age(root / "injected.md")
            collection = _collect_stable([_spec(root, source_id="chunk-injected-only")])
            dumped = self._assert_untransferable_no_excerpt(collection)
            self.assertNotIn("injected context that must not become knowledge", dumped)

    def _assert_untransferable_no_excerpt(self, collection):
        admitted = _admitted(collection)
        self.assertEqual(admitted, [])
        dumped = json.dumps(
            {
                "events": [event_to_dict(e) for e in collection.events],
                "issues": [i.to_dict() for i in collection.issues],
            },
            ensure_ascii=False,
        )
        for event in collection.events:
            payload = event_to_dict(event)
            self.assertFalse((payload.get("excerpt") or "").strip())
        reasons = [issue.reason for issue in collection.issues]
        self.assertEqual(reasons.count("document_untransferable"), 1)
        self.assertEqual(len(collection.issues), 1)
        self.assertEqual(collection.issues[0].status, "skipped")
        return dumped


if __name__ == "__main__":
    unittest.main()
