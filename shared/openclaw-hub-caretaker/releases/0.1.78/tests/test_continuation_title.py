"""Continuation stubs keep a single readable title suffix, not a nested chain."""
from __future__ import annotations

import unittest

from caretaker.publication import (
    continuation_stub,
    continuation_target,
    fm,
)


def _prior_page(
    title: str,
    *,
    slug: str = "agent-stack",
    status: str = "reviewed",
    last_verified: str = "2026-09-20",
    continues: str | None = None,
) -> str:
    cont = f'\ncontinues: "{continues}"' if continues else ""
    return (
        "---\n"
        f'title: "{title}"\n'
        "type: project\n"
        f"status: {status}\n"
        f'last_verified: "{last_verified}"\n'
        "sources: []\n"
        f"project_slug: {slug}\n"
        f"{cont}\n"
        "---\n\n"
        f"# {title}\nbody\n"
    )


class ContinuationTitleTests(unittest.TestCase):
    def test_observed_nested_suffix_is_replaced_not_appended(self):
        prior = _prior_page(
            "agent-stack (continued 2)",
            continues="wiki/projects/agent-stack.md",
        )
        stub = continuation_stub("wiki/projects/agent-stack-cont-2.md", prior, 3)
        self.assertEqual(fm(stub, "title"), "agent-stack (continued 3)")
        self.assertNotIn("(continued 2) (continued 3)", fm(stub, "title"))

    def test_first_rotation_from_plain_title(self):
        prior = _prior_page("agent-stack")
        stub = continuation_stub("wiki/projects/agent-stack.md", prior, 2)
        self.assertEqual(fm(stub, "title"), "agent-stack (continued 2)")

    def test_later_rotations_do_not_lengthen_suffix_chain(self):
        prior = _prior_page(
            "agent-stack (continued 2) (continued 3)",
            continues="wiki/projects/agent-stack-cont-2.md",
        )
        stub = continuation_stub("wiki/projects/agent-stack-cont-3.md", prior, 4)
        self.assertEqual(fm(stub, "title"), "agent-stack (continued 4)")
        self.assertEqual(fm(stub, "title").count("(continued"), 1)

    def test_project_parentheses_that_are_not_continuation_suffix_are_kept(self):
        prior = _prior_page("agent-stack (beta)")
        stub = continuation_stub("wiki/projects/agent-stack.md", prior, 2)
        self.assertEqual(fm(stub, "title"), "agent-stack (beta) (continued 2)")

    def test_stub_metadata_and_file_names_unchanged_except_title(self):
        prior = _prior_page(
            "agent-stack (continued 2)",
            slug="agent-stack",
            status="verified",
            last_verified="2026-09-21",
            continues="wiki/projects/agent-stack.md",
        )
        previous = "wiki/projects/agent-stack-cont-2.md"
        stub = continuation_stub(previous, prior, 3)
        self.assertEqual(fm(stub, "project_slug"), "agent-stack")
        self.assertEqual(fm(stub, "continues"), previous)
        self.assertEqual(fm(stub, "status"), "verified")
        self.assertEqual(fm(stub, "sources"), [])
        self.assertEqual(fm(stub, "last_verified"), "2026-09-21")
        self.assertEqual(continuation_target("agent-stack", 3), "wiki/projects/agent-stack-cont-3.md")
        self.assertEqual(continuation_target("agent-stack", 2), previous)


if __name__ == "__main__":
    unittest.main()
