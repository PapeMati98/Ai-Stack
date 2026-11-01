"""Evidence-ledger frontmatter must describe the snapshot, not the project page."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from caretaker.publication import build_candidate, fm, freeze, sha


PROJECT_SUMMARY = (
    "Integrated 09-12 acceptance/D1 drafts and admission block into fleet history. "
    "Drafts remain unbound; no deployment. 09-17 infra status and pending "
    "Z.AI/GitHub/MSI/VPS items preserved"
)
PROJECT_BODY = (
    "# Agent stack\nExisting project facts remain on this page.\n"
    "Dated tool canary report (src-one); deployment unverified.\n"
)
DECISION = {
    "body": PROJECT_BODY,
    "summary": PROJECT_SUMMARY,
    "dispositions": {
        "prop-one": {
            "disposition": "historical",
            "reason": "09-12 disposition recorded for this snapshot only.",
        }
    },
}


def _vault_with_continuation_page(root: Path) -> tuple[Path, str]:
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
        "target_pages:\n  - wiki/projects/agent-stack.md\n---\nReport.\n",
        encoding="utf-8",
    )
    target = vault / "wiki/projects/agent-stack.md"
    target.parent.mkdir(parents=True)
    old = (
        "---\n"
        "title: Agent stack\n"
        "type: project\n"
        "status: reviewed\n"
        f"summary: {json.dumps(PROJECT_SUMMARY)}\n"
        'last_verified: "2026-01-01"\n'
        "verification_scope: producer-reports\n"
        "sources: []\n"
        "project_slug: agent-stack\n"
        "continues: wiki/projects/agent-stack.md\n"
        "---\n\n"
        "# Agent stack\nExisting project facts remain on this page.\n"
    )
    target.write_text(old, encoding="utf-8")
    return vault, old


class LedgerFrontmatterTests(unittest.TestCase):
    def test_ledger_frontmatter_describes_snapshot_not_project_page(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            vault, old = _vault_with_continuation_page(root)
            frozen = freeze(
                vault, ["proposals/agent/one.md"], "wiki/projects/agent-stack.md"
            )
            observed = "2026-09-20"
            build_candidate(frozen, DECISION, root / "good", observed)
            snap = frozen["snapshot_sha256"][:20]
            ledger_path = (
                root / "good" / "payload" / "wiki" / "history" / f"openclaw-{snap}.md"
            )
            self.assertTrue(ledger_path.is_file())
            text = ledger_path.read_text(encoding="utf-8")
            header, body = text.split("\n---\n", 1)

            self.assertNotEqual(fm(text, "summary"), PROJECT_SUMMARY)
            self.assertNotIn(PROJECT_SUMMARY, header)
            self.assertIn(snap, str(fm(text, "summary")))
            self.assertIn("prop-one", str(fm(text, "summary")))
            self.assertIn("historical", str(fm(text, "summary")))
            self.assertIn("disposition", str(fm(text, "summary")).lower())

            self.assertIsNone(fm(text, "continues"))
            self.assertNotIn("continues:", header)
            self.assertEqual(fm(text, "project_slug"), "agent-stack")
            self.assertEqual(fm(text, "last_verified"), "unverified")

            self.assertEqual(fm(text, "type"), "evidence-ledger")
            self.assertEqual(fm(text, "verification_scope"), "producer-reports")
            status = fm(text, "status")
            self.assertIn(status, {"draft", "reviewed"})
            self.assertNotEqual(status, "verified")
            self.assertNotEqual(fm(text, "status"), "verified")
            self.assertEqual(fm(text, "last_reviewed"), observed)
            self.assertEqual(fm(text, "sources"), ["src-one"])

            expected_body = (
                "\n# Source disposition history\n\n"
                "Producer reports, not independent live verification. Originals retained.\n\n"
                "- prop-one — historical: 09-12 disposition recorded for this snapshot only. "
                "Sources: src-one\n"
            )
            self.assertEqual(body, expected_body)

            self.assertEqual(
                (vault / "wiki/projects/agent-stack.md").read_text(encoding="utf-8"),
                old,
            )
            page = (
                root / "good" / "payload" / "wiki" / "projects" / "agent-stack.md"
            ).read_text(encoding="utf-8")
            self.assertEqual(fm(page, "continues"), "wiki/projects/agent-stack.md")
            self.assertEqual(fm(page, "summary"), PROJECT_SUMMARY)
            self.assertEqual(fm(page, "type"), "project")
            self.assertIn("Existing project facts remain on this page.", page)


if __name__ == "__main__":
    unittest.main()
