"""Oversized cited sections stay source-bound across page splits.

A valid section whose citations sit near the end must publish as one
lossless manifest. Pagination may repeat only the source ids that close
the continued span. It must not treat a packet-wide index as grounding,
and it must still refuse foreign, malformed, missing, or indivisible text.
"""
from __future__ import annotations

import json
import re
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from caretaker.publication import (
    PAGE_BODY_LIMIT,
    SOURCE_ID_RE,
    PublicationError,
    bind_review,
    build_candidate,
    existing_page_body,
    fm,
    freeze,
    sha,
)


SID_A = "src-20261022T010000000Z-session-ground-a-aaaaaaaa"
SID_B = "src-20261022T010100000Z-session-ground-b-bbbbbbbb"
SID_C = "src-20261022T010200000Z-session-ground-c-cccccccc"
FOREIGN = "src-20261022T010300000Z-session-foreign-dddddddd"
MALFORMED = "src-20261022T01000000Z-session-bad-eeeeeeee"
BINDING_PREFIX = (
    "Pagination binding for this continued section "
    "(not a new claim; names only sources already cited in this section span): "
)
UNIT = "e\u0301"


def _write_source(vault: Path, sid: str) -> None:
    src = vault / "raw/sources/2026/10" / sid
    src.mkdir(parents=True)
    data = f"Synthetic producer note for {sid}.\n".encode()
    (src / "original.md").write_bytes(data)
    (src / "source.json").write_text(json.dumps({
        "source_id": sid,
        "classification": "internal",
        "local_only": False,
        "original_file": "original.md",
        "sha256": sha(data),
        "byte_length": len(data),
    }), encoding="utf-8")


def _vault(root: Path, source_ids: tuple[str, ...] = (SID_A, SID_B, SID_C)):
    vault = root / "vault"
    for sid in source_ids:
        _write_source(vault, sid)
    listed = "\n".join(f"  - {sid}" for sid in source_ids)
    proposal = vault / "proposals/agent/one.md"
    proposal.parent.mkdir(parents=True)
    proposal.write_text(
        "---\nid: prop-one\nclassification: internal\nsource_ids:\n"
        + listed
        + "\ntarget_pages:\n  - wiki/projects/ground.md\n---\nReport.\n",
        encoding="utf-8",
    )
    page = vault / "wiki/projects/ground.md"
    page.parent.mkdir(parents=True)
    prior = (
        "---\ntitle: \"Ground\"\nstatus: reviewed\n"
        "summary: \"Prior summary stays.\"\n"
        "last_verified: \"2026-01-01\"\nsources: []\n"
        "project_slug: \"ground\"\n---\n\n"
        "# Ground\n\nPrior safe fact stays.\n"
    )
    page.write_text(prior, encoding="utf-8")
    kept = vault / "wiki/projects/kept-safe.md"
    kept.write_text(
        "---\ntitle: \"Kept\"\nstatus: reviewed\nsummary: \"Untouched.\"\n"
        "last_verified: \"2026-01-01\"\nsources: []\n"
        "project_slug: \"kept-safe\"\n---\n\n# Kept\n\nDo not rewrite.\n",
        encoding="utf-8",
    )
    return vault, page.read_bytes(), kept.read_bytes()


def _decision(body: str) -> dict:
    return {
        "body": body,
        "summary": "Section from synthetic producer notes.",
        "dispositions": {"prop-one": {
            "disposition": "synthesized",
            "reason": "Cited section retained.",
        }},
    }


def _paragraphs(count: int, label: str, units: int) -> list[str]:
    return [
        f"{label} {index:02d} keeps compound text {UNIT * units}."
        for index in range(count)
    ]


def _long_cited_body() -> str:
    parts = _paragraphs(12, "Claim", 600)
    parts.append(f"Terminal citation for the section only ({SID_A}).")
    return "\n\n".join(parts)


def _project_pages(manifest: Path) -> list[dict]:
    record = json.loads(manifest.read_text(encoding="utf-8"))
    return [row for row in record["outputs"] if row["path"].startswith("wiki/projects/")]


def _read_page(manifest: Path, row: dict) -> str:
    payload = (manifest.parent / row["payload_path"]).read_bytes()
    if len(payload) > PAGE_BODY_LIMIT:
        raise AssertionError(f"{row['path']} is {len(payload)} bytes")
    return payload.decode("utf-8")


def _claim_paragraphs(page_text: str) -> list[str]:
    body = existing_page_body(page_text).split("\n### Source References", 1)[0]
    kept = []
    for para in body.split("\n\n"):
        if para.startswith("## ") or para.startswith("# "):
            continue
        if para.startswith(BINDING_PREFIX) or para.startswith("Continuation of previous"):
            continue
        if para.strip() in {"", "Prior safe fact stays."}:
            continue
        kept.append(para.strip("\n"))
    return kept


class SectionGroundingProgressTests(unittest.TestCase):
    def test_long_section_with_terminal_citation_publishes_lossless_manifest(self):
        body = _long_cited_body()
        self.assertLessEqual(len(body), 16000)
        self.assertGreater(len(body.encode("utf-8")), PAGE_BODY_LIMIT)
        self.assertNotIn(SID_B, body)
        self.assertNotIn(SID_C, body)
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            vault, prior, kept = _vault(root)
            frozen = freeze(vault, ["proposals/agent/one.md"], "wiki/projects/ground.md")
            self.assertEqual(frozen["target"], "wiki/projects/ground.md")
            manifest = build_candidate(frozen, _decision(body), root / "candidate", "2026-10-22T00:00:00Z")
            record = json.loads(manifest.read_text(encoding="utf-8"))
            self.assertEqual(sorted(record["source_ids"]), sorted([SID_A, SID_B, SID_C]))
            for row in record["outputs"]:
                payload = (manifest.parent / row["payload_path"]).read_bytes()
                self.assertLessEqual(len(payload), PAGE_BODY_LIMIT)
                self.assertEqual(row["sha256"], sha(payload))
            pages = _project_pages(manifest)
            self.assertGreaterEqual(len(pages), 2)
            self.assertEqual(pages[0]["path"], "wiki/projects/ground-cont-2.md")
            self.assertTrue(all("-cont-" in row["path"] for row in pages))
            self.assertFalse((root / "candidate/payload/wiki/projects/ground.md").exists())
            texts = [_read_page(manifest, row) for row in pages]
            self.assertEqual(fm(texts[0], "project_slug"), "ground")
            self.assertEqual(fm(texts[0], "continues"), "wiki/projects/ground.md")
            self.assertIsNone(pages[0]["preimage_sha256"])
            for index, text in enumerate(texts[1:], start=1):
                self.assertEqual(fm(text, "project_slug"), "ground")
                self.assertEqual(fm(text, "continues"), pages[index - 1]["path"])
                self.assertIsNone(pages[index]["preimage_sha256"])
            claims = [para for text in texts for para in _claim_paragraphs(text)]
            self.assertEqual(claims, body.split("\n\n"))
            self.assertEqual(sum("Terminal citation" in text for text in texts), 1)
            self.assertNotIn("Terminal citation", texts[0])
            self.assertIn(BINDING_PREFIX + SID_A, texts[0])
            for text in texts:
                section = existing_page_body(text)
                self.assertIn(SID_A, section)
                self.assertNotIn(SID_B, section)
                self.assertNotIn(SID_C, section)
                self.assertGreater(len(section.encode("utf-8")), len(section))
            for text in texts:
                for para in existing_page_body(text).split("\n\n"):
                    if not para.startswith(BINDING_PREFIX):
                        continue
                    tokens = SOURCE_ID_RE.findall(para)
                    self.assertEqual(tokens, [SID_A])
            joined = "\n".join(texts)
            self.assertEqual(joined.count(UNIT), body.count(UNIT))
            saved = json.loads((manifest.parent / "decision.json").read_text(encoding="utf-8"))
            self.assertEqual(saved["body"], body)
            self.assertEqual((vault / "wiki/projects/ground.md").read_bytes(), prior)
            self.assertEqual((vault / "wiki/projects/kept-safe.md").read_bytes(), kept)
            self.assertFalse((root / "candidate/payload/wiki/projects/kept-safe.md").exists())
            digest = sha(manifest.read_bytes())
            review_path = bind_review(manifest.parent, {
                "ok": True,
                "status": "ok",
                "provider": "muse-code",
                "model": "muse-spark-1.3",
                "sessionId": "reviewer-ground",
                "final": json.dumps({
                    "verdict": "PASS",
                    "findings": [],
                    "manifest_sha256": digest,
                }),
            })
            review = json.loads(review_path.read_text(encoding="utf-8"))
            self.assertEqual(review["verdict"], "PASS")
            self.assertEqual(review["manifest_sha256"], digest)
            self.assertNotEqual(review["maker"], review["reviewer"]["id"])
            with self.assertRaisesRegex(PublicationError, "independent review did not accept exact publication"):
                bind_review(manifest.parent, {
                    "ok": True,
                    "status": "ok",
                    "provider": "muse-code",
                    "model": "muse-spark-1.3",
                    "sessionId": "reviewer-ground",
                    "final": json.dumps({
                        "verdict": "PASS",
                        "findings": [],
                        "manifest_sha256": "0" * 64,
                    }),
                })

    def test_distinct_spans_do_not_inherit_each_others_sources(self):
        alpha = _paragraphs(6, "Alpha", 800)
        alpha.append(f"Alpha close ({SID_A}).")
        beta = _paragraphs(3, "Beta", 400)
        beta.append(f"Beta close ({SID_B}).")
        body = "\n\n".join(alpha + beta)
        self.assertLessEqual(len(body), 16000)
        self.assertNotIn(SID_C, body)
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            vault, prior, kept = _vault(root)
            frozen = freeze(vault, ["proposals/agent/one.md"], "wiki/projects/ground.md")
            manifest = build_candidate(frozen, _decision(body), root / "candidate", "2026-10-22T00:00:00Z")
            pages = _project_pages(manifest)
            texts = [_read_page(manifest, row) for row in pages]
            claims = [para for text in texts for para in _claim_paragraphs(text)]
            self.assertEqual(claims, body.split("\n\n"))
            alpha_only = [text for text in texts if "Alpha " in text and "Beta " not in text]
            beta_only = [text for text in texts if "Beta " in text and "Alpha " not in text]
            self.assertTrue(alpha_only)
            self.assertTrue(beta_only)
            for text in alpha_only:
                section = existing_page_body(text)
                self.assertIn(SID_A, section)
                self.assertNotIn(SID_B, section)
                self.assertNotIn(SID_C, section)
            for text in beta_only:
                section = existing_page_body(text)
                self.assertIn(SID_B, section)
                self.assertNotIn(SID_A, section)
                self.assertNotIn(SID_C, section)
            self.assertEqual((vault / "wiki/projects/ground.md").read_bytes(), prior)
            self.assertEqual((vault / "wiki/projects/kept-safe.md").read_bytes(), kept)

    def test_short_cited_section_stays_on_the_canonical_page(self):
        body = f"Short valid claim with terminal citation ({SID_A})."
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            vault, prior, _kept = _vault(root)
            frozen = freeze(vault, ["proposals/agent/one.md"], "wiki/projects/ground.md")
            manifest = build_candidate(frozen, _decision(body), root / "candidate", "2026-10-22T00:00:00Z")
            pages = _project_pages(manifest)
            self.assertEqual([row["path"] for row in pages], ["wiki/projects/ground.md"])
            text = _read_page(manifest, pages[0])
            self.assertNotIn(BINDING_PREFIX, text)
            self.assertIn(body, text)
            self.assertIn("Prior safe fact stays.", text)
            self.assertEqual((vault / "wiki/projects/ground.md").read_bytes(), prior)

    def test_missing_foreign_malformed_and_indivisible_text_stay_pending(self):
        cases = {
            "synthesized proposal lacks citation": "\n\n".join(
                _paragraphs(10, "Loose", 700)
            ),
            "citation_outside_publication": "\n\n".join(
                _paragraphs(4, "Foreign", 400) + [f"Cites {SID_A} and {FOREIGN}."]
            ),
            "citation_outside_publication_malformed": "\n\n".join(
                _paragraphs(4, "Malformed", 400) + [f"Cites {SID_A} and {MALFORMED}."]
            ),
            "section_split_unanchored_keep_pending": "\n\n".join(
                [f"Open ({SID_A})."] + _paragraphs(10, "Loose", 600)
            ),
            "proposal_item_too_long_keep_pending": (UNIT * 7960) + " " + SID_A,
        }
        expected = {
            "synthesized proposal lacks citation": "synthesized proposal lacks citation",
            "citation_outside_publication": "citation_outside_publication",
            "citation_outside_publication_malformed": "citation_outside_publication",
            "section_split_unanchored_keep_pending": "section_split_unanchored_keep_pending",
            "proposal_item_too_long_keep_pending": "proposal_item_too_long_keep_pending",
        }
        for key, body in cases.items():
            self.assertLessEqual(len(body), 16000, key)
            with tempfile.TemporaryDirectory() as name:
                root = Path(name)
                vault, prior, kept = _vault(root)
                frozen = freeze(vault, ["proposals/agent/one.md"], "wiki/projects/ground.md")
                with self.assertRaisesRegex(PublicationError, re.escape(expected[key])):
                    build_candidate(frozen, _decision(body), root / "candidate", "2026-10-22T00:00:00Z")
                self.assertFalse((root / "candidate").exists(), key)
                self.assertEqual((vault / "wiki/projects/ground.md").read_bytes(), prior, key)
                self.assertEqual((vault / "wiki/projects/kept-safe.md").read_bytes(), kept, key)


if __name__ == "__main__":
    unittest.main()
