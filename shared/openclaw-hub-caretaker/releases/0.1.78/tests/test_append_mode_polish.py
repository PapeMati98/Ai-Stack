"""Append-mode polish: owned heading, preserved page summary, additive source index."""
from __future__ import annotations

import json
import re
import tempfile
import unittest
from pathlib import Path

from caretaker.publication import (
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


OLD_INDEX = (
    "# Test\n"
    "SSH alias lab-box is configured.\n"
    "## Status Summary\n"
    "As of 2026-09-17 the stack is reviewed.\n"
    "## Source References\n"
    "- src-old: prior citation kept verbatim.\n"
)

NO_INDEX = (
    "# Test\n"
    "SSH alias lab-box is configured.\n"
    "## Status Summary\n"
    "As of 2026-09-17 the stack is reviewed.\n"
)

SECTION = (
    "Dated tool canary report (src-one); deployment unverified. "
    "Producer-reported only."
)
MODEL_HEADING_SECTION = (
    "## Publication Run (2026-09-20)\n\n" + SECTION
)


def _vault(root: Path, body: str | None, slug: str = "test", page_summary: str = "Whole page summary."):
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
            "---\ntitle: Test\nstatus: reviewed\n"
            f"summary: {json.dumps(page_summary)}\n"
            "last_verified: \"2026-01-01\"\n"
            f"sources: []\nproject_slug: {slug}\n---\n\n" + body
        )
        target.write_text(page, encoding="utf-8")
        return vault, page
    return vault, ""


def _decision(body: str = SECTION, summary: str = "Only the 09-12 session."):
    return {
        "body": body,
        "summary": summary,
        "dispositions": {
            "prop-one": {
                "disposition": "synthesized",
                "reason": "Dated and scope-limited report integrated.",
            }
        },
    }


def _published(root: Path, slug: str = "test") -> str:
    return (
        root / "good" / "payload" / "wiki" / "projects" / f"{slug}.md"
    ).read_text(encoding="utf-8")


class AppendModePolishTests(unittest.TestCase):
    def test_prompt_forbids_model_top_level_section_heading(self):
        frozen = {
            "target": "wiki/projects/example.md",
            "before": "short existing page",
            "sources": {},
            "proposals": [],
        }
        prompt = synthesis_prompt(frozen)
        self.assertIn("Do not emit your own top-level section heading", prompt)
        self.assertIn("code owns the dated heading", prompt)
        self.assertIn("Do not emit your own top-level section heading", SYNTHESIS_OUTPUT_CONTRACT)
        repair = repair_prompt(frozen, "body_has_frontmatter")
        self.assertIn("Do not emit your own top-level section heading", repair)

    def test_model_heading_does_not_leave_empty_or_stacked_headings(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            vault, _ = _vault(root, OLD_INDEX)
            frozen = freeze(vault, ["proposals/agent/one.md"], "wiki/projects/test.md")
            build_candidate(frozen, _decision(MODEL_HEADING_SECTION), root / "good", "2026-09-20")
            text = _published(root)
            body = existing_page_body(text)
            caretaker = section_heading("2026-09-20").rstrip()
            self.assertIn(caretaker, body)
            self.assertNotIn("## Publication Run (2026-09-20)", body)
            for line in body.splitlines():
                if line.startswith("## "):
                    self.assertTrue(line.strip("# ").strip())
            stacked = re.search(r"^## .+\n\s*\n## ", body, re.M)
            self.assertIsNone(stacked)

    def test_existing_page_summary_is_not_replaced_by_section_summary(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            vault, _ = _vault(root, OLD_INDEX, page_summary="Whole page summary.")
            frozen = freeze(vault, ["proposals/agent/one.md"], "wiki/projects/test.md")
            build_candidate(
                frozen,
                _decision(summary="Only the 09-12 session."),
                root / "good",
                "2026-09-20",
            )
            text = _published(root)
            header = text.split("\n---\n", 1)[0]
            self.assertIn("Whole page summary.", header)
            self.assertNotIn("Only the 09-12 session.", header)
            self.assertIn("As of 2026-09-17 the stack is reviewed.", text)

    def test_new_page_uses_model_summary_for_the_page(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            vault, _ = _vault(root, None, slug="new-slug")
            frozen = freeze(
                vault, ["proposals/agent/one.md"], "wiki/projects/new-slug.md"
            )
            build_candidate(
                frozen,
                _decision(summary="Only the 09-12 session."),
                root / "good",
                "2026-09-20",
            )
            text = _published(root, "new-slug")
            header = text.split("\n---\n", 1)[0]
            self.assertIn("Only the 09-12 session.", header)

    def test_new_source_ids_appear_in_source_index(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            vault, _ = _vault(root, OLD_INDEX)
            frozen = freeze(vault, ["proposals/agent/one.md"], "wiki/projects/test.md")
            build_candidate(frozen, _decision(), root / "good", "2026-09-20")
            text = _published(root)
            body = existing_page_body(text)
            idx_chunks = [
                chunk
                for chunk in re.split(r"(?=^#{2,3} Source References)", body, flags=re.M)
                if chunk.lstrip().startswith("#") and "Source References" in chunk.split("\n", 1)[0]
            ]
            indexed = "\n".join(idx_chunks)
            self.assertIn("src-one", indexed)

    def test_prior_facts_intact_and_index_change_is_additive(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            vault, _ = _vault(root, OLD_INDEX)
            frozen = freeze(vault, ["proposals/agent/one.md"], "wiki/projects/test.md")
            prior_body = existing_page_body(frozen["before"])
            build_candidate(frozen, _decision(), root / "good", "2026-09-20")
            published = (
                root / "good" / "payload" / "wiki" / "projects" / "test.md"
            ).read_bytes()
            self.assertIn(prior_body.encode("utf-8"), published)
            self.assertIn(b"- src-old: prior citation kept verbatim.", published)
            self.assertIn(b"SSH alias lab-box is configured.", published)
            self.assertIn(b"As of 2026-09-17 the stack is reviewed.", published)

    def test_page_without_source_index_still_indexes_new_sources(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            vault, _ = _vault(root, NO_INDEX)
            frozen = freeze(vault, ["proposals/agent/one.md"], "wiki/projects/test.md")
            build_candidate(frozen, _decision(), root / "good", "2026-09-20")
            body = existing_page_body(_published(root))
            self.assertIn("Source References", body)
            self.assertIn("src-one", body)
            self.assertIn("SSH alias lab-box is configured.", body)

    def test_compose_strips_model_heading_even_without_build_candidate(self):
        glued = compose_appended_body(OLD_INDEX, MODEL_HEADING_SECTION, "2026-09-20")
        self.assertIn(section_heading("2026-09-20").rstrip(), glued)
        self.assertNotIn("## Publication Run", glued)
        self.assertIn("Publication Run (2026-09-20)", glued)
        self.assertFalse(re.search(r"^## .+\n\s*\n## ", glued, re.M))

    def test_heading_only_body_keeps_title_text_not_empty_section(self):
        heading_only = "## Publication Run 2026-09-20 report"
        self.assertGreaterEqual(len(heading_only), 20)
        glued = compose_appended_body(OLD_INDEX, heading_only, "2026-09-20")
        caretaker = section_heading("2026-09-20").rstrip()
        self.assertIn(caretaker, glued)
        self.assertNotIn("## Publication Run 2026-09-20 report", glued)
        self.assertIn("Publication Run 2026-09-20 report", glued)
        after = glued.split(caretaker, 1)[1]
        prose = [
            line
            for line in after.splitlines()
            if line.strip() and not line.startswith("#")
        ]
        self.assertTrue(prose)
        self.assertFalse(re.search(r"^## .+\n\s*\n## ", glued, re.M))

    def test_fact_bearing_heading_is_kept_as_content(self):
        fact = "## Redis migration completed"
        glued = compose_appended_body(OLD_INDEX, fact + "\n\n" + SECTION, "2026-09-20")
        self.assertNotIn("## Redis migration completed", glued)
        self.assertIn("Redis migration completed", glued)
        self.assertIn("Dated tool canary report", glued)

    def test_new_page_h1_title_is_not_dropped(self):
        body = "# New project\n" + SECTION
        glued = compose_appended_body("", body, "2026-09-20")
        self.assertNotRegex(glued, r"(?m)^# New project$")
        self.assertIn("New project", glued)
        self.assertIn(section_heading("2026-09-20").rstrip(), glued)


if __name__ == "__main__":
    unittest.main()
