"""Append-only synthesis: code preserves frozen before; the model returns a section."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from caretaker.publication import (
    PAGE_BODY_LIMIT,
    PublicationError,
    SYNTHESIS_OUTPUT_CONTRACT,
    build_candidate,
    compose_appended_body,
    existing_page_body,
    freeze,
    repair_prompt,
    section_heading,
    sha,
    synthesis_prompt,
)


OLD_FACTS = (
    "# Test\n"
    "SSH alias lab-box is configured.\n"
    "Qwen Code 0.0.12 is installed.\n"
    "OpenCode GLM 5.3 Flash is not ready.\n"
    "GitHub connectivity works.\n"
    "agent-graph manifest has 14 files.\n"
    "ClawOps has 4 levels.\n"
    "GitHub no tokens stored.\n"
    "absent DPAPI config.\n"
)


def _vault(root: Path, body: str, slug: str = "test"):
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
        f"target_pages:\n  - wiki/projects/{slug}.md\n---\nReport.\n",
        encoding="utf-8",
    )
    if body is not None:
        target = vault / f"wiki/projects/{slug}.md"
        target.parent.mkdir(parents=True)
        page = (
            "---\ntitle: Test\nstatus: reviewed\nlast_verified: \"2026-01-01\"\n"
            f"sources: []\nproject_slug: {slug}\n---\n\n" + body
        )
        target.write_text(page, encoding="utf-8")
        return vault, page
    return vault, ""


SECTION = (
    "Dated tool canary report (src-one); deployment unverified. "
    "Producer-reported only."
)
DECISION = {
    "body": SECTION,
    "summary": "Tool canary report.",
    "dispositions": {
        "prop-one": {
            "disposition": "synthesized",
            "reason": "Dated and scope-limited report integrated.",
        }
    },
}


class AppendOnlySynthesisTests(unittest.TestCase):
    def test_prompt_does_not_ask_for_complete_page_body(self):
        frozen = {
            "target": "wiki/projects/example.md",
            "before": "short existing page",
            "sources": {},
            "proposals": [],
        }
        prompt = synthesis_prompt(frozen)
        self.assertNotIn("complete concise Markdown BODY", prompt)
        self.assertNotIn(
            "body is a complete concise Markdown BODY (no frontmatter) of the existing project page",
            prompt,
        )
        self.assertIn("ONLY a new Markdown section", prompt)
        self.assertIn("Do not return the existing page body", prompt)
        self.assertIn("keys body, summary, dispositions", prompt)

    def test_repair_prompt_matches_main_contract(self):
        frozen = {
            "target": "wiki/projects/example.md",
            "before": "short existing page",
            "sources": {"src-1": {"source_id": "src-1", "content": "fact"}},
            "proposals": [{"id": "prop-a"}],
        }
        main = synthesis_prompt(frozen)
        repair = repair_prompt(frozen, "body_has_frontmatter")
        self.assertIn(SYNTHESIS_OUTPUT_CONTRACT, main)
        self.assertIn(SYNTHESIS_OUTPUT_CONTRACT, repair)
        self.assertIn("ONLY a new Markdown section", repair)
        self.assertNotIn("body is a complete concise Markdown BODY (no frontmatter).", repair)

    def test_prior_body_survives_omitting_model_section_byte_for_byte(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            vault, old = _vault(root, OLD_FACTS)
            frozen = freeze(vault, ["proposals/agent/one.md"], "wiki/projects/test.md")
            omitting = {
                "body": SECTION,
                "summary": "New canary only.",
                "dispositions": {
                    "prop-one": {
                        "disposition": "synthesized",
                        "reason": "New report cited.",
                    }
                },
            }
            self.assertNotIn("SSH alias lab-box", omitting["body"])
            self.assertNotIn("absent DPAPI", omitting["body"])
            build_candidate(frozen, omitting, root / "good", "2026-09-20")
            published = (
                root / "good" / "payload" / "wiki" / "projects" / "test.md"
            ).read_bytes()
            prior_body = existing_page_body(frozen["before"]).encode("utf-8")
            self.assertIn(prior_body, published)
            self.assertTrue(prior_body)
            self.assertIn(b"SSH alias lab-box is configured.", published)
            self.assertIn(b"absent DPAPI config.", published)
            heading = section_heading("2026-09-20").encode("utf-8")
            self.assertIn(heading, published)
            self.assertIn(b"src-one", published)

    def test_empty_before_is_only_the_new_section(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            vault, _ = _vault(root, None, slug="new-slug")
            frozen = freeze(
                vault, ["proposals/agent/one.md"], "wiki/projects/new-slug.md"
            )
            self.assertEqual(frozen["before"], "")
            build_candidate(frozen, DECISION, root / "good", "2026-09-20")
            text = (
                root / "good" / "payload" / "wiki" / "projects" / "new-slug.md"
            ).read_text(encoding="utf-8")
            body = existing_page_body(text)
            expected = compose_appended_body("", SECTION, "2026-09-20")
            self.assertIn(expected.rstrip("\n"), text)
            self.assertIn(section_heading("2026-09-20").rstrip(), body)
            self.assertIn(SECTION, body)
            self.assertNotIn("SSH alias", text.split("\n---\n", 1)[-1][: len(expected) + 20])

    def test_dispositions_must_cover_every_proposal(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            vault, _ = _vault(root, OLD_FACTS)
            frozen = freeze(vault, ["proposals/agent/one.md"], "wiki/projects/test.md")
            with self.assertRaises(PublicationError) as ctx:
                build_candidate(
                    frozen,
                    {
                        "body": SECTION,
                        "summary": "x",
                        "dispositions": {},
                    },
                    root / "bad-disp",
                    "2026-09-20",
                )
            self.assertEqual(str(ctx.exception), "incomplete disposition coverage")

    def test_glue_overflow_rotates_instead_of_shortening(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            filler = "old-fact-keep " * 900
            vault, original = _vault(root, filler)
            self.assertLessEqual(len(original.encode("utf-8")), PAGE_BODY_LIMIT)
            frozen = freeze(vault, ["proposals/agent/one.md"], "wiki/projects/test.md")
            self.assertEqual(frozen["target"], "wiki/projects/test.md")
            long_section = (
                "Dated tool canary report (src-one); deployment unverified. "
                + ("new-fact " * 400)
            )
            self.assertGreater(len(long_section), 20)
            decision = {
                "body": long_section[:16000],
                "summary": "Overflow rotates.",
                "dispositions": {
                    "prop-one": {
                        "disposition": "synthesized",
                        "reason": "New facts on continuation.",
                    }
                },
            }
            glued = compose_appended_body(frozen["before"], decision["body"], "2026-09-20")
            self.assertGreater(len(glued.encode("utf-8")), PAGE_BODY_LIMIT - 200)
            manifest = build_candidate(frozen, decision, root / "good", "2026-09-20")
            value = json.loads(manifest.read_text(encoding="utf-8"))
            self.assertEqual(value["outputs"][0]["path"], "wiki/projects/test-cont-2.md")
            self.assertFalse(
                (root / "good" / "payload" / "wiki" / "projects" / "test.md").exists()
            )
            published = (
                root / "good" / "payload" / "wiki" / "projects" / "test-cont-2.md"
            ).read_text(encoding="utf-8")
            self.assertIn("continues", published)
            self.assertIn(section_heading("2026-09-20").rstrip(), published)
            self.assertIn("src-one", published)
            self.assertEqual(
                (vault / "wiki/projects/test.md").read_text(encoding="utf-8"),
                original,
            )
            self.assertIn("old-fact-keep", original)
            self.assertNotIn(filler[:80], published)

    def test_frontmatter_secret_and_citation_gates_remain(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            vault, _ = _vault(root, OLD_FACTS)
            frozen = freeze(vault, ["proposals/agent/one.md"], "wiki/projects/test.md")
            with self.assertRaises(PublicationError) as fm_err:
                build_candidate(
                    frozen,
                    DECISION | {"body": "---\nnot a body\n" + SECTION},
                    root / "bad-fm",
                    "2026-09-20",
                )
            self.assertEqual(str(fm_err.exception), "body_has_frontmatter")
            with self.assertRaises(PublicationError) as cite_err:
                build_candidate(
                    frozen,
                    DECISION | {"body": "Uncited claim with no source ID at all."},
                    root / "bad-cite",
                    "2026-09-20",
                )
            self.assertEqual(str(cite_err.exception), "synthesized proposal lacks citation")
            secret_body = (
                "Dated tool canary report (src-one); token sk-"
                + ("A" * 24)
                + " leaked."
            )
            with self.assertRaises(PublicationError) as secret_err:
                build_candidate(
                    frozen,
                    DECISION | {"body": secret_body},
                    root / "bad-secret",
                    "2026-09-20",
                )
            self.assertEqual(str(secret_err.exception), "unsafe body:secret")


if __name__ == "__main__":
    unittest.main()
