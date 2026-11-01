"""Ingest tests for the real Codex native JSONL schemas.

The fixtures here follow the documented Codex CLI session JSONL dialect:
``session_meta``, ``response_item``, and ``event_msg``.  The parser must:

- preserve native session identity (the ``session_id`` and stable ``id``),
- skip still-active / uncompleted turns,
- skip reasoning and tool-output items,
- never expose secrets or reasoning text into the body.
"""

import json
import tempfile
import unittest
from pathlib import Path

from caretaker.filter import filter_records
from caretaker.ingest import read_inputs

HERE = Path(__file__).resolve().parent
FIXTURES = HERE / "fixtures"


class CodexSessionMetaTests(unittest.TestCase):

    def test_session_meta_admitted_with_native_identity(self):
        recs, _, _ = read_inputs([
            (str(FIXTURES / "codex_session_meta_real.jsonl"),
             "session_jsonl", "codex"),
        ])
        # Both session_meta lines carry a distinct id and session_id.
        self.assertEqual(len(recs), 2)
        sessions = sorted(r.native_session_id for r in recs)
        self.assertEqual(sessions, ["codex-real-1", "codex-real-2"])
        cursors = sorted(r.cursor for r in recs)
        # The parser uses ``session_meta:<id>`` so the cursor survives
        # a re-run and is replay-stable.
        self.assertTrue(all(c.startswith("session_meta:") for c in cursors))
        self.assertEqual(len(set(cursors)), 2)


class CodexResponseItemTests(unittest.TestCase):

    def test_response_item_keeps_assistant_text_only(self):
        recs, _, _ = read_inputs([
            (str(FIXTURES / "codex_response_item_real.jsonl"),
             "session_jsonl", "codex"),
        ])
        # Reasoning and tool-role items are skipped.  Still-active items
        # are skipped.  Only the completed assistant text remains.
        accepted_cursors = sorted(r.cursor for r in recs)
        self.assertEqual(len(recs), 2)
        # resp-0003 (tool role) and resp-0004 (status=active) are dropped.
        self.assertNotIn("response_item:resp-0003", accepted_cursors)
        self.assertNotIn("response_item:resp-0004", accepted_cursors)
        for rec in recs:
            self.assertNotIn("Hidden reasoning", rec.body)
            self.assertNotIn("Streaming assistant", rec.body)

    def test_response_item_native_session_identity_preserved(self):
        recs, _, _ = read_inputs([
            (str(FIXTURES / "codex_response_item_real.jsonl"),
             "session_jsonl", "codex"),
        ])
        sessions = sorted({r.native_session_id for r in recs})
        self.assertEqual(sessions, ["codex-real-1"])


class CodexEventMsgTests(unittest.TestCase):

    def test_event_msg_keeps_user_and_agent_messages(self):
        recs, _, _ = read_inputs([
            (str(FIXTURES / "codex_event_msg_real.jsonl"),
             "session_jsonl", "codex"),
        ])
        # agent_message + user_message remain; tool_started, token_count,
        # agent_reasoning are dropped.
        cursors = sorted(r.cursor for r in recs)
        self.assertIn("event_msg:evt-0001", cursors)
        self.assertIn("event_msg:evt-0005", cursors)
        self.assertNotIn("event_msg:evt-0002", cursors)
        self.assertNotIn("event_msg:evt-0003", cursors)
        self.assertNotIn("event_msg:evt-0004", cursors)
        for rec in recs:
            # Secret-bearing user_message still gets a row, but the
            # filter is what rejects it.
            if rec.cursor == "event_msg:evt-0005":
                self.assertIn("api_key", rec.body)

    def test_secret_and_reasoning_filtered_after_ingest(self):
        recs, _, _ = read_inputs([
            (str(FIXTURES / "codex_event_msg_real.jsonl"),
             "session_jsonl", "codex"),
        ])
        outcomes = filter_records(
            recs, allowlist_tools=("codex",), max_body_bytes=8192,
            max_excerpt_bytes=400,
        )
        reasons = [o.reason for o in outcomes]
        # Secret-bearing event is filtered out.
        self.assertIn("secret_detected", reasons)


class CodexAllRealSchemaTests(unittest.TestCase):

    def test_full_real_schema_pipeline_keeps_only_safe_records(self):
        triples = [
            (str(FIXTURES / "codex_session_meta_real.jsonl"),
             "session_jsonl", "codex"),
            (str(FIXTURES / "codex_response_item_real.jsonl"),
             "session_jsonl", "codex"),
            (str(FIXTURES / "codex_event_msg_real.jsonl"),
             "session_jsonl", "codex"),
        ]
        recs, _, _ = read_inputs(triples)
        # Sanity: every record still carries a native session id so the
        # pipeline can keep the native identity stable across runs.
        self.assertTrue(all(r.native_session_id for r in recs))
        outcomes = filter_records(
            recs, allowlist_tools=("codex",), max_body_bytes=8192,
            max_excerpt_bytes=400,
        )
        # No reasoning, no tool-only payloads, no still-active items, no
        # secrets.  Every accepted record's body must be free of those.
        for outcome, rec in zip(outcomes, recs):
            if outcome.accepted:
                self.assertNotIn("reasoning", rec.body.lower())
                self.assertNotIn("tool_use", rec.body.lower())
                self.assertNotIn("sk-", rec.body)


if __name__ == "__main__":
    unittest.main()
