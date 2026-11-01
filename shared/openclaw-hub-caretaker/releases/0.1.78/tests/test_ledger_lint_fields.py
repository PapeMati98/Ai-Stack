"""Hub lint fields on ledger and newly created project pages."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from caretaker.publication import build_candidate, fm, freeze, sha


HUB_REQUIRED = ("title", "type", "status", "summary", "last_verified", "sources")
PROJECT_SUMMARY = (
    "Integrated 09-12 acceptance/D1 drafts and admission block into fleet history. "
    "Drafts remain unbound; no deployment."
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
        },
        "prop-two": {
            "disposition": "historical",
            "reason": "Second proposal in this snapshot.",
        },
        "prop-three": {
            "disposition": "unresolved",
            "reason": "Third proposal remains unresolved.",
        },
    },
}
NEW_BODY = (
    "# New project\nDated tool canary report (src-one); deployment unverified.\n"
    "Producer-reported only."
)
NEW_DECISION = {
    "body": NEW_BODY,
    "summary": "Tool canary report.",
    "dispositions": {
        "prop-one": {
            "disposition": "synthesized",
            "reason": "Dated and scope-limited report integrated.",
        }
    },
}


def _write_source(vault: Path, source_id: str, payload: bytes) -> None:
    src = vault / "raw/sources/2026/09" / source_id
    src.mkdir(parents=True)
    (src / "original.md").write_bytes(payload)
    meta = {
        "source_id": source_id,
        "classification": "internal",
        "local_only": False,
        "original_file": "original.md",
        "sha256": sha(payload),
        "byte_length": len(payload),
    }
    (src / "source.json").write_text(json.dumps(meta), encoding="utf-8")


def _vault_with_history(root: Path) -> Path:
    vault = root / "vault"
    _write_source(
        vault,
        "src-one",
        b"Dated report: local tool canary passed; deployment remains unverified.",
    )
    _write_source(vault, "src-two", b"Second snapshot-only report.")
    _write_source(vault, "src-three", b"Third snapshot-only report.")
    proposals = vault / "proposals/agent"
    proposals.mkdir(parents=True)
    for pid, sid in (
        ("prop-one", "src-one"),
        ("prop-two", "src-two"),
        ("prop-three", "src-three"),
    ):
        (proposals / f"{pid}.md").write_text(
            f"---\nid: {pid}\nclassification: internal\nsource_ids:\n  - {sid}\n"
            "target_pages:\n  - wiki/projects/agent-stack.md\n---\nReport.\n",
            encoding="utf-8",
        )
    target = vault / "wiki/projects/agent-stack.md"
    target.parent.mkdir(parents=True)
    historical = [f"hist-{i:02d}" for i in range(1, 17)]
    sources_yaml = "\n".join(f'  - "{item}"' for item in historical)
    old = (
        "---\n"
        "title: Agent stack\n"
        "type: project\n"
        "status: reviewed\n"
        f"summary: {json.dumps(PROJECT_SUMMARY)}\n"
        'last_verified: "2026-01-01"\n'
        "verification_scope: producer-reports\n"
        f"sources:\n{sources_yaml}\n"
        "project_slug: agent-stack\n"
        "continues: wiki/projects/agent-stack.md\n"
        "---\n\n"
        "# Agent stack\nExisting project facts remain on this page.\n"
    )
    target.write_text(old, encoding="utf-8")
    return vault


def _assert_hub_required(text: str) -> None:
    for key in HUB_REQUIRED:
        value = fm(text, key)
        if value is None or value == "":
            raise AssertionError(f"missing required frontmatter field {key}")


class LedgerLintFieldsTests(unittest.TestCase):
    def test_ledger_and_new_page_satisfy_hub_lint_fields(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            vault = _vault_with_history(root)
            frozen = freeze(
                vault,
                [
                    "proposals/agent/prop-one.md",
                    "proposals/agent/prop-two.md",
                    "proposals/agent/prop-three.md",
                ],
                "wiki/projects/agent-stack.md",
            )
            observed = "2026-09-20"
            build_candidate(frozen, DECISION, root / "good", observed)
            snap = frozen["snapshot_sha256"][:20]
            ledger_path = (
                root / "good" / "payload" / "wiki" / "history" / f"openclaw-{snap}.md"
            )
            self.assertTrue(ledger_path.is_file())
            ledger = ledger_path.read_text(encoding="utf-8")
            header, _body = ledger.split("\n---\n", 1)
            _assert_hub_required(ledger)
            self.assertEqual(fm(ledger, "last_verified"), "unverified")
            self.assertEqual(
                fm(ledger, "sources"), ["src-one", "src-three", "src-two"]
            )
            self.assertEqual(len(fm(ledger, "sources")), 3)
            self.assertNotIn("hist-01", fm(ledger, "sources"))
            self.assertIsNone(fm(ledger, "continues"))
            self.assertNotIn("continues:", header)
            self.assertEqual(fm(ledger, "project_slug"), "agent-stack")
            self.assertNotEqual(fm(ledger, "summary"), PROJECT_SUMMARY)
            self.assertIn(snap, str(fm(ledger, "summary")))
            self.assertIn("prop-one", str(fm(ledger, "summary")))
            self.assertEqual(fm(ledger, "verification_scope"), "producer-reports")
            self.assertNotEqual(fm(ledger, "status"), "verified")
            self.assertIn(fm(ledger, "status"), {"draft", "reviewed"})

            page = (
                root / "good" / "payload" / "wiki" / "projects" / "agent-stack.md"
            ).read_text(encoding="utf-8")
            self.assertEqual(fm(page, "last_verified"), "2026-01-01")
            self.assertNotEqual(fm(page, "last_verified"), observed)

            new_vault = root / "new-vault"
            src = new_vault / "raw/sources/2026/09/src-one"
            src.mkdir(parents=True)
            data = b"Dated report: local tool canary passed; deployment remains unverified."
            (src / "original.md").write_bytes(data)
            (src / "source.json").write_text(
                json.dumps(
                    {
                        "source_id": "src-one",
                        "classification": "internal",
                        "local_only": False,
                        "original_file": "original.md",
                        "sha256": sha(data),
                        "byte_length": len(data),
                    }
                ),
                encoding="utf-8",
            )
            proposal = new_vault / "proposals/agent/one.md"
            proposal.parent.mkdir(parents=True)
            proposal.write_text(
                "---\nid: prop-one\nclassification: internal\nsource_ids:\n  - src-one\n"
                "target_pages:\n  - wiki/projects/new-slug.md\n---\nReport.\n",
                encoding="utf-8",
            )
            new_frozen = freeze(
                new_vault, ["proposals/agent/one.md"], "wiki/projects/new-slug.md"
            )
            self.assertEqual(new_frozen["before"], "")
            build_candidate(new_frozen, NEW_DECISION, root / "new-good", observed)
            new_page = (
                root / "new-good" / "payload" / "wiki" / "projects" / "new-slug.md"
            ).read_text(encoding="utf-8")
            _assert_hub_required(new_page)
            self.assertEqual(fm(new_page, "last_verified"), "unverified")
            self.assertEqual(fm(new_page, "status"), "draft")
            self.assertNotEqual(fm(new_page, "status"), "verified")
            self.assertEqual(fm(new_page, "verification_scope"), "producer-reports")


if __name__ == "__main__":
    unittest.main()
