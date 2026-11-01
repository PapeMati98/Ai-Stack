"""New proposal publication on an already long project page."""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from caretaker.publication import (
    PAGE_BODY_LIMIT,
    PublicationError,
    bind_review,
    build_candidate,
    fm,
    freeze,
    sha,
)


def fixture(root: Path) -> tuple[Path, bytes]:
    vault = root / "vault"
    source = vault / "raw/sources/2026/09/src-one"
    source.mkdir(parents=True)
    original = b"Producer report: all dated claims cite src-one."
    (source / "original.md").write_bytes(original)
    (source / "source.json").write_text(json.dumps({
        "source_id": "src-one", "classification": "internal", "local_only": False,
        "original_file": "original.md", "sha256": sha(original),
        "byte_length": len(original),
    }), encoding="utf-8")
    proposal = vault / "proposals/agent/one.md"
    proposal.parent.mkdir(parents=True)
    proposal.write_text(
        "---\nid: prop-one\nclassification: internal\nsource_ids:\n  - src-one\n"
        "target_pages:\n  - wiki/projects/test.md\n---\nDated claims.\n",
        encoding="utf-8",
    )
    page = vault / "wiki/projects/test.md"
    page.parent.mkdir(parents=True)
    prior = (
        "---\r\ntitle: Test\r\nstatus: reviewed\r\n"
        "last_verified: \"2026-01-01\"\r\nsources: [src-old]\r\n"
        "project_slug: test\r\n---\r\n\r\n# Test\r\n"
        "Prior cited fact (src-old).\r\n" + "Old text. " * 1800
    ).encode("utf-8")
    assert len(prior) > PAGE_BODY_LIMIT
    page.write_bytes(prior)
    return vault, prior


def decision(body: str) -> dict:
    return {
        "body": body,
        "summary": "Dated claims from a producer report.",
        "dispositions": {"prop-one": {
            "disposition": "synthesized", "reason": "Cited dated claims retained.",
        }},
    }


class LongPageNewLaneTests(unittest.TestCase):
    def test_unrelated_continuation_name_is_never_overwritten(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            vault, prior = fixture(root)
            collision = vault / "wiki/projects/test-cont-2.md"
            unrelated = (
                "---\ntitle: Other\nstatus: reviewed\nsources: []\n"
                "project_slug: other\ncontinues: \"wiki/projects/other.md\"\n---\n"
            ).encode("utf-8")
            collision.write_bytes(unrelated)
            with self.assertRaisesRegex(PublicationError, "continuation_chain_conflict_keep_pending"):
                freeze(vault, ["proposals/agent/one.md"], "wiki/projects/test.md")
            self.assertEqual(collision.read_bytes(), unrelated)
            self.assertEqual((vault / "wiki/projects/test.md").read_bytes(), prior)

    def test_existing_continuation_is_appended_without_changing_vault(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            vault, prior = fixture(root)
            continuation = vault / "wiki/projects/test-cont-2.md"
            earlier = (
                "---\r\ntitle: Test (continued 2)\r\nstatus: reviewed\r\n"
                "sources: [src-old]\r\nproject_slug: test\r\n"
                "continues: \"wiki/projects/test.md\"\r\n---\r\n\r\n"
                "Previously published fact (src-old).\r\n"
            ).encode("utf-8")
            continuation.write_bytes(earlier)
            frozen = freeze(vault, ["proposals/agent/one.md"], "wiki/projects/test.md")
            self.assertEqual(frozen["target"], "wiki/projects/test-cont-2.md")
            manifest = build_candidate(
                frozen, decision("New dated fact from src-one; older uncertainty remains."),
                root / "candidate", "2026-09-23",
            )
            record = json.loads(manifest.read_text(encoding="utf-8"))
            self.assertEqual(record["outputs"][0]["path"], "wiki/projects/test-cont-2.md")
            updated = (root / "candidate/payload/wiki/projects/test-cont-2.md").read_text(encoding="utf-8")
            self.assertIn("Previously published fact (src-old).", updated)
            self.assertIn("New dated fact from src-one", updated)
            self.assertEqual((vault / "wiki/projects/test.md").read_bytes(), prior)
            self.assertEqual(continuation.read_bytes(), earlier)

    def test_long_section_uses_bounded_linked_pages_without_touching_prior(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            vault, prior = fixture(root)
            body = "\n\n".join(
                f"Fact {i:02d} from src-one. " + "x" * 960 for i in range(16)
            )
            self.assertLessEqual(len(body), 16000)
            frozen = freeze(vault, ["proposals/agent/one.md"], "wiki/projects/test.md")
            manifest = build_candidate(frozen, decision(body), root / "candidate", "2026-09-23")
            record = json.loads(manifest.read_text(encoding="utf-8"))
            pages = [row for row in record["outputs"] if row["path"].startswith("wiki/projects/")]
            self.assertGreaterEqual(len(pages), 2)
            self.assertEqual(pages[0]["path"], "wiki/projects/test-cont-2.md")
            self.assertEqual(len({row["path"] for row in pages}), len(pages))
            text = []
            for i, row in enumerate(pages):
                payload = (manifest.parent / row["payload_path"]).read_bytes()
                self.assertLessEqual(len(payload), PAGE_BODY_LIMIT)
                self.assertIsNone(row["preimage_sha256"])
                page_text = payload.decode("utf-8")
                expected_previous = "wiki/projects/test.md" if i == 0 else pages[i - 1]["path"]
                self.assertEqual(fm(page_text, "continues"), expected_previous)
                self.assertIn("src-one", page_text)
                text.append(page_text)
            for i in range(16):
                self.assertEqual(sum(f"Fact {i:02d} from src-one." in t for t in text), 1)
            self.assertEqual((vault / "wiki/projects/test.md").read_bytes(), prior)
            self.assertFalse((root / "candidate/payload/wiki/projects/test.md").exists())

    def test_indivisible_item_stays_pending_without_payload(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            vault, prior = fixture(root)
            frozen = freeze(vault, ["proposals/agent/one.md"], "wiki/projects/test.md")
            body = "One indivisible fact from src-one. " + "z" * 15800
            with self.assertRaisesRegex(PublicationError, "proposal_item_too_long_keep_pending"):
                build_candidate(frozen, decision(body), root / "candidate", "2026-09-23")
            self.assertFalse((root / "candidate").exists())
            self.assertEqual((vault / "wiki/projects/test.md").read_bytes(), prior)

    def test_retry_reuses_page_names_and_requires_exact_independent_review(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            vault, _ = fixture(root)
            body = "\n\n".join(
                f"Fact {i:02d} from src-one. " + "x" * 960 for i in range(16)
            )
            manifests = []
            for run in ("first", "retry"):
                frozen = freeze(vault, ["proposals/agent/one.md"], "wiki/projects/test.md")
                manifest = build_candidate(frozen, decision(body), root / run, "2026-09-23")
                manifests.append(manifest)
                with self.assertRaisesRegex(PublicationError, "independent review did not accept exact publication"):
                    bind_review(manifest.parent, {
                        "ok": True, "status": "ok", "provider": "muse-code",
                        "model": "muse-spark-1.3", "sessionId": "reviewer",
                        "final": json.dumps({"manifest_sha256": "wrong", "findings": []}),
                    })
                self.assertFalse((manifest.parent / "review.json").exists())
            self.assertEqual(manifests[0].read_bytes(), manifests[1].read_bytes())
            ledger = vault / "governance/curations/committed.json"
            ledger.parent.mkdir(parents=True)
            ledger.write_text(json.dumps({
                "proposals": [{"proposal_id": "prop-one", "disposition": "synthesized"}],
            }), encoding="utf-8")
            with self.assertRaisesRegex(PublicationError, "proposal_already_accounted_keep_pending"):
                freeze(vault, ["proposals/agent/one.md"], "wiki/projects/test.md")


if __name__ == "__main__":
    unittest.main()
