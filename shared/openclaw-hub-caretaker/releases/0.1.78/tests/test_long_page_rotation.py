"""Oversized project pages rotate onto a continuation instead of blocking."""
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
    build_candidate,
    continuation_target,
    freeze,
    sha,
    synthesis_prompt,
)


def _vault_with_page(root: Path, body: str):
    vault = root / "vault"
    src = vault / "raw/sources/2026/09/src-one"
    src.mkdir(parents=True)
    data = b"Dated report: local tool canary passed; deployment remains unverified."
    (src / "original.md").write_bytes(data)
    meta = {
        "source_id": "src-one",
        "classification": "internal",
        "local_only": False,
        "original_file": "original.md",
        "sha256": sha(data),
        "byte_length": len(data),
    }
    (src / "source.json").write_text(json.dumps(meta), encoding="utf-8")
    proposal = vault / "proposals/agent/one.md"
    proposal.parent.mkdir(parents=True)
    proposal.write_text(
        "---\nid: prop-one\nclassification: internal\nsource_ids:\n  - src-one\n"
        "target_pages:\n  - wiki/projects/test.md\n---\nReport.\n",
        encoding="utf-8",
    )
    target = vault / "wiki/projects/test.md"
    target.parent.mkdir(parents=True)
    page = (
        "---\ntitle: Test\nstatus: reviewed\nlast_verified: \"2026-01-01\"\n"
        "sources: []\nproject_slug: test\n---\n\n# Test\n" + body
    )
    target.write_text(page, encoding="utf-8")
    return vault, page


def _continuation(vault: Path, number: int, previous: int | None, body: str):
    prior = ("wiki/projects/test.md" if previous is None else
             f"wiki/projects/test-cont-{previous}.md")
    path = vault / f"wiki/projects/test-cont-{number}.md"
    path.write_text(
        f"---\ntitle: Test (continued {number})\nstatus: reviewed\n"
        f"sources: []\nproject_slug: test\ncontinues: \"{prior}\"\n"
        "---\n\n" + body, encoding="utf-8")
    return path


class LongPageRotationTests(unittest.TestCase):
    def test_existing_last_continuation_with_room_is_appended_with_preimage(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            vault, original = _vault_with_page(root, "x" * (PAGE_BODY_LIMIT + 1))
            cont2 = _continuation(vault, 2, None, "Earlier section.\n")
            before = cont2.read_bytes()
            frozen = freeze(vault, ["proposals/agent/one.md"], "wiki/projects/test.md")
            self.assertEqual(frozen["target"], "wiki/projects/test-cont-2.md")
            self.assertEqual(frozen["preimage_sha256"], sha(before))
            decision = {'body': 'New dated report cites src-one and preserves earlier facts.',
                        'summary': 'Dated report.',
                        'dispositions': {'prop-one': {'disposition': 'synthesized',
                                                       'reason': 'New dated report.'}}}
            build_candidate(frozen, decision, root / 'candidate', '2026-09-27')
            output = root / 'candidate/payload/wiki/projects/test-cont-2.md'
            self.assertTrue(output.is_file())
            self.assertIn('Earlier section.', output.read_text(encoding='utf-8'))
            self.assertIn('New dated report', output.read_text(encoding='utf-8'))
            self.assertEqual(cont2.read_bytes(), before)
            self.assertEqual((vault / 'wiki/projects/test.md').read_text(encoding='utf-8'), original)
            self.assertFalse((root / 'candidate/payload/wiki/projects/test.md').exists())

    def test_last_continuation_with_insufficient_room_advances_after_decision(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            vault, _ = _vault_with_page(root, "x" * (PAGE_BODY_LIMIT + 1))
            cont2 = _continuation(vault, 2, None, "y" * (PAGE_BODY_LIMIT - 250))
            before = cont2.read_bytes()
            frozen = freeze(vault, ["proposals/agent/one.md"], "wiki/projects/test.md")
            self.assertEqual(frozen['target'], 'wiki/projects/test-cont-2.md')
            decision = {'body': 'src-one ' + ('new evidence ' * 20),
                        'summary': 'Dated report.',
                        'dispositions': {'prop-one': {'disposition': 'synthesized',
                                                       'reason': 'New dated report.'}}}
            build_candidate(frozen, decision, root / 'candidate', '2026-09-27')
            self.assertEqual(frozen['target'], 'wiki/projects/test-cont-3.md')
            self.assertTrue((root / 'candidate/payload/wiki/projects/test-cont-3.md').is_file())
            self.assertEqual(cont2.read_bytes(), before)

    def test_numbering_gap_uses_highest_existing_continuation(self):
        with tempfile.TemporaryDirectory() as d:
            vault, _ = _vault_with_page(Path(d), "x" * (PAGE_BODY_LIMIT + 1))
            _continuation(vault, 2, None, "second\n")
            last = _continuation(vault, 4, 2, "fourth\n")
            frozen = freeze(vault, ["proposals/agent/one.md"], "wiki/projects/test.md")
            self.assertEqual(frozen['target'], 'wiki/projects/test-cont-4.md')
            last.write_text(last.read_text(encoding='utf-8') + 'z' * PAGE_BODY_LIMIT,
                            encoding='utf-8')
            frozen = freeze(vault, ["proposals/agent/one.md"], "wiki/projects/test.md")
            self.assertEqual(frozen['target'], 'wiki/projects/test-cont-5.md')
            self.assertEqual(frozen['continuation_of'], 'wiki/projects/test-cont-4.md')

    def test_oversize_page_rotates_instead_of_pending_error(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            filler = ("old-fact-%05d " % i for i in range(4000))
            long_body = "".join(filler)
            vault, original = _vault_with_page(root, long_body)
            self.assertGreater(len(original.encode("utf-8")), PAGE_BODY_LIMIT)
            frozen = freeze(vault, ["proposals/agent/one.md"], "wiki/projects/test.md")
            self.assertEqual(frozen["target"], "wiki/projects/test-cont-2.md")
            self.assertEqual(frozen["target"], continuation_target("test", 2))
            self.assertEqual(frozen["continuation_of"], "wiki/projects/test.md")
            self.assertIn("wiki/projects/test.md", frozen["before"])
            self.assertIn("continues", frozen["before"])
            prompt = synthesis_prompt(frozen)
            self.assertNotIn("existing_page_too_long_keep_pending", prompt)
            self.assertLessEqual(len(frozen["before"].encode("utf-8")), PAGE_BODY_LIMIT)
            (vault / "wiki/projects/test.md").write_text(original, encoding="utf-8")
            self.assertEqual((vault / "wiki/projects/test.md").read_text(encoding="utf-8"), original)

            decision = {
                "body": (
                    "# Test (continued 2)\nContinuation of wiki/projects/test.md.\n"
                    "Dated tool canary report (src-one); deployment unverified."
                ),
                "summary": "Tool canary on continuation page.",
                "dispositions": {
                    "prop-one": {
                        "disposition": "synthesized",
                        "reason": "New facts go on the continuation page.",
                    }
                },
            }
            manifest = build_candidate(frozen, decision, root / "good", "2026-09-19")
            payload_page = root / "good" / "payload" / "wiki/projects/test-cont-2.md"
            self.assertTrue(payload_page.is_file())
            published = payload_page.read_text(encoding="utf-8")
            self.assertIn("continues", published)
            self.assertIn("wiki/projects/test.md", published)
            self.assertFalse((root / "good" / "payload" / "wiki/projects/test.md").exists())
            self.assertEqual((vault / "wiki/projects/test.md").read_text(encoding="utf-8"), original)
            self.assertIn(long_body[:40], original)
            self.assertNotIn(long_body[:80], published)
            value = json.loads(manifest.read_text(encoding="utf-8"))
            self.assertEqual(value["outputs"][0]["path"], "wiki/projects/test-cont-2.md")

    def test_existing_full_continuation_advances_to_next_index(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            vault, original = _vault_with_page(root, "x" * (PAGE_BODY_LIMIT + 10))
            cont2 = vault / "wiki/projects/test-cont-2.md"
            cont2.write_text(
                "---\ntitle: Test (continued 2)\nstatus: reviewed\nsources: []\n"
                "project_slug: test\ncontinues: \"wiki/projects/test.md\"\n---\n\n"
                + ("y" * (PAGE_BODY_LIMIT + 10)),
                encoding="utf-8",
            )
            frozen = freeze(vault, ["proposals/agent/one.md"], "wiki/projects/test.md")
            self.assertEqual(frozen["target"], "wiki/projects/test-cont-3.md")
            self.assertEqual(frozen["continuation_of"], "wiki/projects/test-cont-2.md")
            synthesis_prompt(frozen)
            self.assertEqual((vault / "wiki/projects/test.md").read_text(encoding="utf-8"), original)
            self.assertGreater(len(cont2.read_text(encoding="utf-8")), PAGE_BODY_LIMIT)

    def test_proposal_body_limit_and_frontmatter_errors_unchanged(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            vault, _ = _vault_with_page(root, "Old unresolved question.\n")
            frozen = freeze(vault, ["proposals/agent/one.md"], "wiki/projects/test.md")
            self.assertEqual(frozen["target"], "wiki/projects/test.md")
            with self.assertRaisesRegex(PublicationError, "body_too_long"):
                build_candidate(
                    frozen,
                    {
                        "body": "n" * 16001,
                        "summary": "s",
                        "dispositions": {
                            "prop-one": {"disposition": "unresolved", "reason": "too long"}
                        },
                    },
                    root / "too-long",
                    "2026-09-19",
                )
            with self.assertRaisesRegex(PublicationError, "body_has_frontmatter"):
                build_candidate(
                    frozen,
                    {
                        "body": "---\nnot a body",
                        "summary": "s",
                        "dispositions": {
                            "prop-one": {"disposition": "unresolved", "reason": "frontmatter"}
                        },
                    },
                    root / "fm",
                    "2026-09-19",
                )


if __name__ == "__main__":
    unittest.main()
