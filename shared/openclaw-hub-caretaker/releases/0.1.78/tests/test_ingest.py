"""Ingest tests."""

import unittest
import tempfile
from pathlib import Path

from caretaker.ingest import read_inputs

HERE = Path(__file__).resolve().parent
FIXTURES = HERE / "fixtures"
TASK_TMP = HERE.parent / ".test-tmp"


class IngestTests(unittest.TestCase):

    def test_codex_session(self):
        recs, unsup, stats = read_inputs([
            (str(FIXTURES / "codex_session.jsonl"), "session_jsonl", "codex"),
        ])
        self.assertGreater(len(recs), 0)
        self.assertEqual(unsup, [])
        self.assertTrue(all(r.identity_tool == "codex" for r in recs))
        self.assertTrue(all(r.source == "native" for r in recs))

    def test_openclaw_session(self):
        recs, unsup, stats = read_inputs([
            (str(FIXTURES / "openclaw_session.jsonl"), "session_jsonl",
             "openclaw"),
        ])
        self.assertGreater(len(recs), 0)
        self.assertTrue(all(r.identity_tool == "openclaw" for r in recs))
        # Provenance preserved.
        for r in recs:
            self.assertTrue(r.native_session_id)
            self.assertTrue(r.cursor)
            self.assertTrue(r.observed_at)

    def test_unsupported_reported_separately(self):
        recs, unsup, stats = read_inputs([
            (str(FIXTURES / "unsupported_session.jsonl"), "session_jsonl",
             "codex"),
        ])
        self.assertEqual(recs, [])
        self.assertEqual(len(unsup), 1)
        self.assertEqual(stats.lines_skipped_unsupported, 1)

    def test_partial_session_two_lines(self):
        recs, _, _ = read_inputs([
            (str(FIXTURES / "partial_session.jsonl"), "session_jsonl",
             "codex"),
        ])
        # Two non-blank lines with distinct cursors.
        cursors = sorted(r.cursor for r in recs)
        self.assertEqual(len(cursors), 2)
        self.assertEqual(cursors[0], "pr-1")
        self.assertEqual(cursors[1], "pr-2")

    def test_normalized_events(self):
        recs, _, _ = read_inputs([
            (str(FIXTURES / "normalized_event.jsonl"),
             "normalized_events", "openclaw"),
        ])
        # Secret-bearing line is admitted at ingest; filter is what rejects.
        self.assertEqual(len(recs), 3)
        self.assertTrue(all(r.source == "normalized" for r in recs))

    def test_incomplete_final_json_is_retried_after_append(self):
        TASK_TMP.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=str(TASK_TMP)) as d:
            path = Path(d) / "tail.jsonl"
            first = ('{"type":"response","timestamp":"2026-09-17T13:00:00Z",'
                     '"session_id":"tail-1","codex_run_id":"r1",'
                     '"cursor":"c1",'
                     '"content":"first"}')
            incomplete = ('{"type":"response","timestamp":"2026-09-17T13:01:00Z",'
                           '"session_id":"tail-1","codex_run_id":"r2",'
                           '"cursor":"c2",'
                           '"content":"second"')
            path.write_text(first + "\n" + incomplete, "utf-8")
            recs, _, stats = read_inputs([
                (str(path), "session_jsonl", "codex"),
            ])
            self.assertEqual([r.cursor for r in recs], ["c1"])
            self.assertEqual(stats.lines_skipped_invalid, 0)

            path.write_text(first + "\n" + incomplete + "}", "utf-8")
            recs, _, _ = read_inputs([
                (str(path), "session_jsonl", "codex"),
            ])
            self.assertEqual(sorted(r.cursor for r in recs), ["c1", "c2"])


if __name__ == "__main__":
    unittest.main()
