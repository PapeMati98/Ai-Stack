"""Missing project pages must be created, not abort the whole batch."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from caretaker.publication import (
    PublicationError,
    build_candidate,
    continuation_stub,
    fm,
    freeze,
    safe_file,
    sha,
)


BODY = (
    "# New project\nDated tool canary report (src-one); deployment unverified.\n"
    "Producer-reported only."
)
DECISION = {
    "body": BODY,
    "summary": "Tool canary report.",
    "dispositions": {
        "prop-one": {
            "disposition": "synthesized",
            "reason": "Dated and scope-limited report integrated.",
            "knowledge_class": "durable-fact",
        }
    },
}


def _write_source_and_proposal(vault: Path, slug: str) -> None:
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
        f"target_pages:\n  - wiki/projects/{slug}.md\n---\nReport.\n",
        encoding="utf-8",
    )


def _attest_draft(vault: Path, state: Path, target: str, page: bytes) -> None:
    run = state / "curation" / "prior-run"
    candidate = run / "candidate"
    candidate.mkdir(parents=True)
    manifest = candidate / "publication.json"
    manifest.write_text(json.dumps({
        "outputs": [{"path": target, "sha256": sha(page)}]
    }), encoding="utf-8")
    manifest_sha = sha(manifest.read_bytes())
    event_id = "evt-prior-test"
    (run / "result.json").write_text(json.dumps({
        "status": "PUBLISHED", "manifest": str(manifest),
        "manifest_sha256": manifest_sha,
        "publication": {"event_id": event_id, "manifest_sha256": manifest_sha},
    }), encoding="utf-8")
    event = vault / "governance/curations" / (event_id + ".json")
    event.parent.mkdir(parents=True)
    event.write_text(json.dumps({
        "event_id": event_id, "manifest_sha256": manifest_sha,
        "review": {"verdict": "PASS", "maker": "openclaw-caretaker",
                   "manifest_sha256": manifest_sha,
                   "reviewer": {"id": "independent-reviewer"}},
    }), encoding="utf-8")


class PublicationNewPageTests(unittest.TestCase):
    def test_freeze_missing_target_is_empty_not_filenotfound(self):
        with tempfile.TemporaryDirectory() as d:
            vault = Path(d) / "vault"
            _write_source_and_proposal(vault, "new-slug")
            frozen = freeze(
                vault, ["proposals/agent/one.md"], "wiki/projects/new-slug.md"
            )
            self.assertEqual(frozen["before"], "")
            self.assertIsNone(frozen["preimage_sha256"])
            self.assertEqual(frozen["target"], "wiki/projects/new-slug.md")

    def test_publish_creates_project_page_with_hub_frontmatter(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            vault = root / "vault"
            _write_source_and_proposal(vault, "new-slug")
            frozen = freeze(
                vault, ["proposals/agent/one.md"], "wiki/projects/new-slug.md"
            )
            manifest = build_candidate(frozen, DECISION, root / "good", "2026-09-20")
            payload = root / "good" / "payload" / "wiki" / "projects" / "new-slug.md"
            self.assertTrue(payload.is_file())
            text = payload.read_text(encoding="utf-8")
            self.assertTrue(text.startswith("---\n"))
            self.assertEqual(fm(text, "type"), "project")
            self.assertEqual(fm(text, "project_slug"), "new-slug")
            self.assertEqual(fm(text, "title"), "new-slug")
            self.assertEqual(fm(text, "summary"), "Tool canary report.")
            self.assertIn("src-one", fm(text, "sources"))
            self.assertIn("Dated tool canary report (src-one)", text)
            value = json.loads(manifest.read_text(encoding="utf-8"))
            self.assertIsNone(value["outputs"][0]["preimage_sha256"])

    def test_new_page_is_reviewed_but_never_verified(self):
        """A durable source-grounded new page is reviewed content, not live fact."""
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            vault = root / "vault"
            _write_source_and_proposal(vault, "new-slug")
            frozen = freeze(
                vault, ["proposals/agent/one.md"], "wiki/projects/new-slug.md"
            )
            build_candidate(frozen, DECISION, root / "good", "2026-09-20")
            text = (
                root / "good" / "payload" / "wiki" / "projects" / "new-slug.md"
            ).read_text(encoding="utf-8")
            status = fm(text, "status")
            self.assertEqual(status, "reviewed")
            self.assertNotEqual(status, "verified")
            self.assertEqual(fm(text, "last_verified"), "unverified")
            self.assertEqual(fm(text, "verification_scope"), "producer-reports")

    def test_metadata_only_new_page_stays_draft(self):
        """A directory/metadata receipt is not durable project understanding."""
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            vault = root / "vault"
            _write_source_and_proposal(vault, "new-slug")
            frozen = freeze(
                vault, ["proposals/agent/one.md"], "wiki/projects/new-slug.md"
            )
            inventory = {
                "body": "# Discovery receipt\n"
                        "Directory discovery listed 45807 files in the scanned tree "
                        "(src-one).",
                "summary": "Directory inventory receipt.",
                "dispositions": {
                    "prop-one": {
                        "disposition": "synthesized",
                        "reason": "Metadata receipt only.",
                        "knowledge_class": "inventory-metadata",
                    }
                },
            }
            build_candidate(frozen, inventory, root / "inventory", "2026-09-20")
            text = (
                root / "inventory" / "payload" / "wiki" / "projects" / "new-slug.md"
            ).read_text(encoding="utf-8")
            self.assertEqual(fm(text, "status"), "draft")
            self.assertEqual(fm(text, "last_verified"), "unverified")
            self.assertIsNone(fm(text, "last_reviewed"))

    def test_existing_page_behavior_unchanged(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            vault = root / "vault"
            _write_source_and_proposal(vault, "test")
            target = vault / "wiki/projects/test.md"
            target.parent.mkdir(parents=True)
            old = (
                "---\ntitle: Test\nstatus: reviewed\nlast_verified: \"2026-01-01\"\n"
                "sources: []\nproject_slug: test\n---\n\n# Test\nOld unresolved question.\n"
            )
            target.write_text(old, encoding="utf-8")
            frozen = freeze(vault, ["proposals/agent/one.md"], "wiki/projects/test.md")
            self.assertEqual(frozen["before"], old.replace("\r\n", "\n"))
            self.assertEqual(frozen["preimage_sha256"], sha(target.read_bytes()))
            manifest = build_candidate(frozen, DECISION, root / "good", "2026-09-17")
            page = (root / "good" / "payload" / "wiki" / "projects" / "test.md").read_text(
                encoding="utf-8"
            )
            self.assertEqual(fm(page, "status"), "reviewed")
            self.assertEqual(fm(page, "last_verified"), "2026-01-01")
            self.assertEqual(fm(page, "last_reviewed"), "2026-09-17")
            self.assertEqual(fm(page, "title"), "Test")
            self.assertEqual(target.read_text(encoding="utf-8"), old)
            value = json.loads(manifest.read_text(encoding="utf-8"))
            self.assertEqual(value["outputs"][0]["preimage_sha256"], sha(target.read_bytes()))

    def test_caretaker_draft_grows_into_reviewed_without_claiming_verification(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            vault = root / "vault"
            _write_source_and_proposal(vault, "test")
            target = vault / "wiki/projects/test.md"
            target.parent.mkdir(parents=True)
            prior = (
                '---\ntitle: "test"\ntype: "project"\nstatus: "draft"\n'
                'summary: "First source."\nlast_verified: "unverified"\n'
                'verification_scope: "producer-reports"\nsources: ["src-one"]\n'
                'project_slug: "test"\n---\n\n## Caretaker publication 2026-09-19\n'
                'First source (src-one).\n'
            )
            target.write_text(prior, encoding="utf-8")
            state = root / "state"
            _attest_draft(vault, state, "wiki/projects/test.md", target.read_bytes())
            frozen = freeze(vault, ["proposals/agent/one.md"], "wiki/projects/test.md",
                            attestation_state_dir=state)
            build_candidate(frozen, DECISION, root / "next", "2026-09-20")
            page = (root / "next/payload/wiki/projects/test.md").read_text(encoding="utf-8")
            self.assertEqual(fm(page, "status"), "reviewed")
            self.assertNotEqual(fm(page, "status"), "verified")
            self.assertEqual(fm(page, "last_verified"), "unverified")
            self.assertEqual(fm(page, "verification_scope"), "producer-reports")
            self.assertIn("First source (src-one)", page)
            self.assertIn("Dated tool canary report (src-one)", page)
            target.write_text(prior + "\nOwner edit.\n", encoding="utf-8")
            with self.assertRaisesRegex(PublicationError, "target is not reviewed"):
                freeze(vault, ["proposals/agent/one.md"], "wiki/projects/test.md",
                       attestation_state_dir=state)

    def test_unrelated_draft_remains_blocked(self):
        with tempfile.TemporaryDirectory() as d:
            vault = Path(d) / "vault"
            _write_source_and_proposal(vault, "test")
            target = vault / "wiki/projects/test.md"
            target.parent.mkdir(parents=True)
            target.write_text(
                '---\ntitle: "test"\ntype: "project"\nstatus: "draft"\n'
                'summary: "Other owner."\nlast_verified: "unverified"\n'
                'verification_scope: "producer-reports"\nsources: ["src-one"]\n'
                'project_slug: "wrong-project"\n---\n\n## Caretaker publication 2026-09-19\n',
                encoding="utf-8",
            )
            with self.assertRaisesRegex(PublicationError, "target is not reviewed"):
                freeze(vault, ["proposals/agent/one.md"], "wiki/projects/test.md")

    def test_same_slug_spoofed_caretaker_heading_is_blocked(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            vault = root / "vault"
            _write_source_and_proposal(vault, "test")
            target = vault / "wiki/projects/test.md"
            target.parent.mkdir(parents=True)
            target.write_text(
                '---\ntitle: "test"\ntype: "project"\nstatus: "draft"\n'
                'summary: "Looks like Caretaker."\nlast_verified: "unverified"\n'
                'verification_scope: "producer-reports"\nsources: ["src-one"]\n'
                'project_slug: "test"\n---\n\n'
                '## Caretaker publication 2026-09-19\nSpoofed (src-one).\n',
                encoding="utf-8",
            )
            with self.assertRaisesRegex(PublicationError, "target is not reviewed"):
                freeze(vault, ["proposals/agent/one.md"], "wiki/projects/test.md",
                       attestation_state_dir=root / "state")

    def test_malformed_attestation_record_does_not_block_valid_one(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            vault = root / "vault"
            _write_source_and_proposal(vault, "test")
            target = vault / "wiki/projects/test.md"
            target.parent.mkdir(parents=True)
            target.write_text(
                '---\ntitle: "test"\ntype: "project"\nstatus: "draft"\n'
                'summary: "First source."\nlast_verified: "unverified"\n'
                'verification_scope: "producer-reports"\nsources: ["src-one"]\n'
                'project_slug: "test"\n---\n\n'
                '## Caretaker publication 2026-09-19\nFirst source (src-one).\n',
                encoding="utf-8",
            )
            state = root / "state"
            bad = state / "curation" / "000-malformed"
            bad.mkdir(parents=True)
            (bad / "result.json").write_text('[]', encoding="utf-8")
            _attest_draft(vault, state, "wiki/projects/test.md", target.read_bytes())
            frozen = freeze(vault, ["proposals/agent/one.md"], "wiki/projects/test.md",
                            attestation_state_dir=state)
            self.assertEqual(frozen["target"], "wiki/projects/test.md")

    def test_caretaker_draft_continuation_becomes_reviewed_and_can_grow(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            vault = root / "vault"
            _write_source_and_proposal(vault, "test")
            base_text = (
                '---\ntitle: "test"\ntype: "project"\nstatus: "draft"\n'
                'summary: "First source."\nlast_verified: "unverified"\n'
                'verification_scope: "producer-reports"\nsources: ["src-one"]\n'
                'project_slug: "test"\n---\n\n'
                '## Caretaker publication 2026-09-19\nFirst source (src-one).\n'
            )
            stub = continuation_stub("wiki/projects/test.md", base_text, 2)
            self.assertEqual(fm(stub, "status"), "draft")
            self.assertEqual(fm(stub, "last_verified"), "unverified")
            self.assertEqual(fm(stub, "verification_scope"), "producer-reports")
            target = vault / "wiki/projects/test-cont-2.md"
            target.parent.mkdir(parents=True)
            prior = stub + "\n## Caretaker publication 2026-09-20\nMore (src-one).\n"
            prior = prior.replace('sources: []', 'sources: ["src-one"]')
            target.write_text(prior, encoding="utf-8")
            state = root / "state"
            _attest_draft(vault, state, "wiki/projects/test-cont-2.md", target.read_bytes())
            frozen = freeze(vault, ["proposals/agent/one.md"], "wiki/projects/test-cont-2.md",
                            attestation_state_dir=state)
            build_candidate(frozen, DECISION, root / "next", "2026-09-21")
            page = (root / "next/payload/wiki/projects/test-cont-2.md").read_text(encoding="utf-8")
            self.assertEqual(fm(page, "status"), "reviewed")
            self.assertEqual(fm(page, "last_verified"), "unverified")
            self.assertEqual(fm(page, "project_slug"), "test")
            self.assertIn("More (src-one)", page)

    def test_target_path_still_uses_safe_file(self):
        with tempfile.TemporaryDirectory() as d:
            vault = Path(d) / "vault"
            vault.mkdir()
            for name in ["../x", "C:/x", "wiki/../x", "/absolute", "a\\b"]:
                with self.subTest(name=name), self.assertRaises(PublicationError):
                    safe_file(vault, name)
            with self.assertRaises(PublicationError):
                freeze(vault, ["proposals/agent/one.md"], "../etc/passwd.md")
            with self.assertRaises(PublicationError):
                freeze(vault, ["proposals/agent/one.md"], "wiki/projects/../secret.md")

    def test_publication_validations_still_apply_to_new_page(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            vault = root / "vault"
            _write_source_and_proposal(vault, "new-slug")
            frozen = freeze(
                vault, ["proposals/agent/one.md"], "wiki/projects/new-slug.md"
            )
            with self.assertRaises(PublicationError):
                build_candidate(
                    frozen,
                    DECISION | {"body": "Uncited claim with no source ID at all."},
                    root / "bad-cite",
                    "2026-09-20",
                )
            with self.assertRaises(PublicationError):
                build_candidate(
                    frozen,
                    DECISION | {"body": "tiny"},
                    root / "bad-short",
                    "2026-09-20",
                )
            with self.assertRaises(PublicationError):
                build_candidate(
                    frozen,
                    DECISION | {"body": "---\nfrontmatter is not a body\n"},
                    root / "bad-fm",
                    "2026-09-20",
                )
            with self.assertRaises(PublicationError):
                freeze(vault, ["proposals/agent/one.md"], "wiki/notes/new-slug.md")
            meta_path = vault / "raw/sources/2026/09/src-one/source.json"
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            meta["classification"] = "restricted"
            meta_path.write_text(json.dumps(meta), encoding="utf-8")
            with self.assertRaises(PublicationError):
                freeze(vault, ["proposals/agent/one.md"], "wiki/projects/new-slug.md")


if __name__ == "__main__":
    unittest.main()
