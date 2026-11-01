"""Tests for the new prompt length handling.

The prompt builder must:
- always emit valid JSON,
- never byte-truncate a record mid-stream,
- cap the record count via ``max_records`` so the prompt size is bounded,
- return the unused tail so the caller can requeue the records.
"""

import json
import unittest

from caretaker.model import (SynthesisInput, SynthesisRecord, WikiExcerpt,
                              build_prompt)


def _records(n):
    out = []
    for i in range(n):
        out.append(SynthesisRecord(
            record_index=i, identity_tool="codex",
            native_session_id="s", cursor=f"c{i}",
            content_hash=f"h{i}", title=f"t{i}",
            excerpt="lorem ipsum " * 16,
        ))
    return out


class BuildPromptRecordsBoundedTests(unittest.TestCase):

    def test_prompt_is_always_valid_json(self):
        payload = SynthesisInput(records=_records(5), inventory_summary="{}",
                                 project_slug="p")
        prompt, _ = build_prompt(payload, max_bytes=4096, max_records=64)
        # Validating via json.loads gives a useful error if it isn't.
        parsed = json.loads(prompt)
        self.assertIn("records", parsed)
        self.assertGreaterEqual(len(parsed["records"]), 1)

    def test_max_records_bounds_record_count(self):
        payload = SynthesisInput(records=_records(20), inventory_summary="{}",
                                 project_slug="p")
        prompt, unprocessed = build_prompt(
            payload, max_bytes=10_000_000, max_records=4,
        )
        parsed = json.loads(prompt)
        self.assertEqual(len(parsed["records"]), 4)
        self.assertEqual(len(unprocessed), 16)
        # The unprocessed tail is a stable slice starting at index 4.
        self.assertEqual([r.cursor for r in unprocessed],
                         [f"c{i}" for i in range(4, 20)])

    def test_byte_budget_bounds_total_prompt_size(self):
        payload = SynthesisInput(records=_records(20), inventory_summary="{}",
                                 project_slug="p")
        prompt, unprocessed = build_prompt(
            payload, max_bytes=2048, max_records=64,
        )
        self.assertLessEqual(len(prompt.encode("utf-8")), 2048)
        parsed = json.loads(prompt)
        self.assertGreaterEqual(len(parsed["records"]), 1)
        self.assertGreater(len(unprocessed), 0)

    def test_very_small_budget_still_returns_valid_json(self):
        payload = SynthesisInput(records=_records(2),
                                 inventory_summary="x" * 50_000,
                                 project_slug="p")
        prompt, unprocessed = build_prompt(
            payload, max_bytes=32, max_records=64,
        )
        self.assertLessEqual(len(prompt.encode("utf-8")), 32)
        self.assertIsInstance(json.loads(prompt), dict)
        self.assertEqual(len(unprocessed), 2)

    def test_wiki_excerpts_are_truncated_before_records(self):
        # A large wiki excerpt must not crowd out the records; the prompt
        # builder bounds the record count first.
        big = "x" * 1024
        payload = SynthesisInput(
            records=_records(10),
            inventory_summary="{}",
            project_slug="p",
            wiki_excerpts=(
                WikiExcerpt(page="wiki/projects/p.md", title="p",
                             excerpt=big, status="verified",
                             classification="internal"),
            ),
        )
        prompt, unprocessed = build_prompt(
            payload, max_bytes=4096, max_records=4,
        )
        parsed = json.loads(prompt)
        # ``records`` count is capped even though the budget would have
        # admitted more if wiki_excerpts were absent.
        self.assertLessEqual(len(parsed["records"]), 4)
        self.assertGreater(len(unprocessed), 0)


class DecideTracksEvidencePagesTests(unittest.TestCase):

    def test_decision_extracts_evidence_pages(self):
        from caretaker.model import decide, SynthesisDecision, SynthesisRecord, SynthesisInput
        rec = SynthesisRecord(record_index=0, identity_tool="codex",
                              native_session_id="s", cursor="c",
                              content_hash="h", title="t", excerpt="e")
        env = json.dumps({
            "decision": "duplicate",
            "synthesis": "",
            "source_refs": ["h"],
            "rationale": "r",
            "evidence_pages": ["wiki/projects/p.md"],
        })
        d = decide(env, records=[rec],
                   payload=SynthesisInput(records=[rec], inventory_summary="{}",
                                          project_slug="p"),
                   model_identity="x")
        self.assertEqual(d.decision, "duplicate")
        self.assertEqual(d.evidence_pages, ("wiki/projects/p.md",))

    def test_decision_rejects_evidence_page_with_nul_or_too_long(self):
        from caretaker.model import decide, SynthesisRecord, SynthesisInput
        rec = SynthesisRecord(record_index=0, identity_tool="codex",
                              native_session_id="s", cursor="c",
                              content_hash="h", title="t", excerpt="e")
        env = json.dumps({
            "decision": "duplicate",
            "synthesis": "",
            "source_refs": ["h"],
            "rationale": "r",
            "evidence_pages": ["ok", "bad\0page", "x" * 5000],
        })
        d = decide(env, records=[rec],
                   payload=SynthesisInput(records=[rec], inventory_summary="{}",
                                          project_slug="p"),
                   model_identity="x")
        self.assertEqual(d.evidence_pages, ("ok",))


if __name__ == "__main__":
    unittest.main()
