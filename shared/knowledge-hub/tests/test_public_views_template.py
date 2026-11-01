"""Synthetic tests for the generic public Hub view templates.

Covers only the mail-row migration in views_generator.render_where:
OLD -> generic NEW, idempotent re-render, legacy mail_direct.py row
migration, fail-closed duplicates/unknown rows, and byte-identical
unrelated library sections. No real data, network, or Hub access.
"""

import importlib.util
import tempfile
import unittest
from pathlib import Path

_GEN = (Path(__file__).resolve().parent.parent
        / "skills" / "llm-wiki" / "scripts" / "views_generator.py")
_spec = importlib.util.spec_from_file_location("views_generator", _GEN)
vg = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(vg)

HEADER = ("---\ntitle: \"WHERE\"\nlast_verified: 2026-01-01\n---\n\n"
          "| Capability | Source | Gate |\n|---|---|---|\n")
LIBRARY = ("| Library A | vault/library/a.md | open |\n"
           "| Library B | vault/library/b.md | review |\n")
MK_OLD = "| Mail knowledge & extraction | legacy harvest notes | gate |\n"
# Synthetic legacy row: stable mail_direct.py + owner-consent markers only,
# no workstation root or real private text.
LEGACY_ROW = ("| Mail and calendar | `mail_direct.py` (skill email, read-only; "
              "owner consent 2026-10-04) | only with the owner's instruction "
              "for the task |\n")
UNKNOWN_ROW = "| Mail and calendar | some unrecognized local export | open |\n"


def render(src: str) -> str:
    with tempfile.TemporaryDirectory() as tmp:
        live = Path(tmp) / "WHERE.md"
        live.write_text(src, encoding="utf-8")
        return vg.render_where(live)


def library_of(text: str) -> str:
    return "\n".join(l for l in text.splitlines() if l.startswith("| Library "))


class PublicViewsTemplateTest(unittest.TestCase):
    def test_old_row_migrates_to_generic(self):
        out = render(HEADER + vg.MAIL_ROW_OLD + "\n" + MK_OLD + LIBRARY)
        self.assertIn(vg.MAIL_ROW_NEW, out)
        self.assertNotIn(vg.MAIL_ROW_OLD, out)
        self.assertIn(vg.MAIL_KNOWLEDGE_ROW_NEW, out)

    def test_current_row_rerender_is_idempotent(self):
        once = render(HEADER + vg.MAIL_ROW_NEW + "\n" + MK_OLD + LIBRARY)
        twice = render(once)
        self.assertEqual(once, twice)
        self.assertEqual(once.count(vg.MAIL_ROW_NEW), 1)

    def test_legacy_generated_row_migrates(self):
        out = render(HEADER + LEGACY_ROW + MK_OLD + LIBRARY)
        self.assertIn(vg.MAIL_ROW_NEW, out)
        self.assertNotIn("owner consent", out)
        self.assertNotIn("2026-10-04", out)

    def test_duplicate_rows_fail_closed(self):
        with self.assertRaises(AssertionError):
            render(HEADER + vg.MAIL_ROW_OLD + "\n"
                   + vg.MAIL_ROW_NEW + "\n" + MK_OLD + LIBRARY)
        with self.assertRaises(AssertionError):
            render(HEADER + LEGACY_ROW + vg.MAIL_ROW_OLD + "\n"
                   + MK_OLD + LIBRARY)
        with self.assertRaises(AssertionError):
            render(HEADER + LEGACY_ROW + LEGACY_ROW + MK_OLD + LIBRARY)

    def test_unknown_row_fails_closed(self):
        with self.assertRaises(AssertionError):
            render(HEADER + UNKNOWN_ROW + MK_OLD + LIBRARY)

    def test_library_sections_byte_identical(self):
        src = HEADER + vg.MAIL_ROW_OLD + "\n" + MK_OLD + LIBRARY
        self.assertEqual(library_of(render(src)), library_of(src))

    def test_no_private_residue_in_output(self):
        out = render(HEADER + vg.MAIL_ROW_OLD + "\n" + MK_OLD + LIBRARY)
        for bad in ("AL Lab", "2026-10-04", "owner consent"):
            self.assertNotIn(bad, out)
        self.assertIn("<configured-code-root>", out)
        self.assertIn("<private-archive-root>", out)

    def test_people_whitelist_still_strict(self):
        person = {"id": "p1", "name": "N", "orgs": ["o"], "role": "R",
                  "projects": ["s"], "email": "e@x", "style": "s",
                  "phone": "1", "contacts": []}
        self.assertEqual(set(vg.person_business_fields(person)),
                         set(vg.PEOPLE_ALLOWED_FIELDS))


if __name__ == "__main__":
    unittest.main()
