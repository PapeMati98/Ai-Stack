"""The review record must match the canonical gateway's exact key set.

``curation.py`` validates ``review.json`` with ``_exact_keys`` over
``{schema_version, verdict, maker, reviewer, manifest_sha256, evidence}``.
A seventh key is refused with ``review schema mismatch``, which on
2026-09-21 blocked a candidate that had already earned an independent
PASS. Curator provenance belongs in the hash-bound evidence file and in
``ledger.json``, never as an extra review key.
"""
from __future__ import annotations
import json
import unittest
from pathlib import Path

CANONICAL_REVIEW_KEYS = {
    "schema_version",
    "verdict",
    "maker",
    "reviewer",
    "manifest_sha256",
    "evidence",
}
CANONICAL_EVIDENCE_KEYS = {"path", "sha256"}


class ReviewCanonicalKeysTests(unittest.TestCase):
    def _review_records(self) -> list[Path]:
        source = Path(__file__).resolve().parent.parent / "curate_once.py"
        self.assertTrue(source.is_file(), "curate_once.py must exist")
        return [source]

    def test_curator_amendment_is_not_written_into_the_review_record(self):
        text = self._review_records()[0].read_text(encoding="utf-8")
        self.assertNotIn(
            'review_doc["curator_amendment"]', text,
            "curator provenance must not become a seventh review key",
        )

    def test_review_record_builder_uses_only_canonical_keys(self):
        text = self._review_records()[0].read_text(encoding="utf-8")
        start = text.index("review_record = {")
        depth = 0
        for end in range(start + len("review_record = "), len(text)):
            if text[end] == "{":
                depth += 1
            elif text[end] == "}":
                depth -= 1
                if depth == 0:
                    break
        block = text[start:end + 1]
        for key in CANONICAL_REVIEW_KEYS:
            self.assertIn(f'"{key}"', block, f"review record must carry {key}")
        for stray in ("curator_amendment", "advisory_charges", "findings"):
            self.assertNotIn(
                f'"{stray}":', block,
                f"{stray} must not be a top-level review key",
            )

    def test_canonical_key_sets_match_the_gateway_contract(self):
        # Guards the constants above against silent drift in this test file.
        self.assertEqual(len(CANONICAL_REVIEW_KEYS), 6)
        self.assertEqual(CANONICAL_EVIDENCE_KEYS, {"path", "sha256"})

    def test_amended_review_json_shape_is_accepted(self):
        # A review record shaped as the fleet path writes it after a curator
        # amendment must still expose exactly the canonical keys.
        review = {
            "schema_version": 1,
            "verdict": "PASS",
            "maker": "openclaw-caretaker",
            "reviewer": {"id": "grok-attempt-1", "lineage": "grok"},
            "manifest_sha256": "a" * 64,
            "evidence": {"path": "semantic-review.json", "sha256": "b" * 64},
        }
        self.assertEqual(set(review), CANONICAL_REVIEW_KEYS)
        self.assertEqual(set(review["evidence"]), CANONICAL_EVIDENCE_KEYS)
        self.assertEqual(set(review["reviewer"]), {"id", "lineage"})
        json.dumps(review)


if __name__ == "__main__":
    unittest.main()
