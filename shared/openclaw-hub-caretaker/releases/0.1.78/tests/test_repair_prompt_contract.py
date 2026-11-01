"""Repair-round prompt must restate the output contract and full findings."""

import unittest

from caretaker.publication import (
    FINDINGS_BOUND_NOTICE,
    PROMPT_BYTE_LIMIT,
    repair_prompt,
)


def _frozen(**kwargs):
    base = {
        "target": "wiki/projects/example.md",
        "before": "short existing page",
        "sources": {"src-1": {"source_id": "src-1", "content": "fact"}},
        "proposals": [{"id": "prop-a"}],
    }
    base.update(kwargs)
    return base


class RepairPromptContractTests(unittest.TestCase):
    def test_forbids_frontmatter_in_body(self):
        prompt = repair_prompt(_frozen(), "body_has_frontmatter")
        self.assertIn("no frontmatter", prompt)
        self.assertIn("Do not put YAML frontmatter in body", prompt)

    def test_repeats_summary_body_dispositions_and_source_id_contract(self):
        prompt = repair_prompt(_frozen(), "body_too_long")
        self.assertIn("summary is at most 300 characters", prompt)
        self.assertIn("Keep body within 16000 characters", prompt)
        self.assertIn("dispositions maps EVERY supplied proposal ID", prompt)
        self.assertIn("Cite every new fact with its exact source_id", prompt)
        self.assertIn(
            "Never include raw transcripts, secrets, reasoning or tool output",
            prompt,
        )

    def test_review_finding_is_not_called_a_format_defect(self):
        prompt = repair_prompt(_frozen(), "review_REPAIR", "claim is unsourced")
        self.assertIn("substantive finding", prompt)
        self.assertNotIn("one concrete format defect", prompt)
        self.assertIn("claim is unsourced", prompt)

    def test_format_defect_is_still_described_as_format_defect(self):
        prompt = repair_prompt(_frozen(), "body_has_frontmatter")
        self.assertIn("one concrete format defect", prompt)
        self.assertNotIn("substantive finding", prompt)

    def test_complete_long_reviewer_findings_reach_the_prompt(self):
        findings = [
            "Finding one: " + ("alpha claim with source mismatch. " * 10),
            "Finding two: " + ("beta claim lacks a citation. " * 12),
            "Finding three: " + ("gamma contradiction is fused. " * 11),
            "Finding four: " + ("delta open limitation dropped. " * 12),
        ]
        for item in findings:
            self.assertGreaterEqual(len(item), 300)
            self.assertLessEqual(len(item), 450)
        detail = "\n".join(findings)
        prompt = repair_prompt(_frozen(), "review_REPAIR", detail)
        for item in findings:
            self.assertIn(item, prompt)
        self.assertNotIn(FINDINGS_BOUND_NOTICE, prompt)

    def test_oversize_findings_are_truncated_explicitly_not_silently(self):
        huge = ["finding-%d: " % i + ("x" * 8000) for i in range(20)]
        detail = "\n".join(huge)
        prompt = repair_prompt(_frozen(), "review_REPAIR", detail)
        self.assertLessEqual(len(prompt.encode()), PROMPT_BYTE_LIMIT)
        self.assertIn(FINDINGS_BOUND_NOTICE, prompt)
        self.assertIn("finding-0:", prompt)
        self.assertNotIn("finding-19:", prompt)

    def test_secret_finding_is_filtered(self):
        secret = "sk-" + ("A" * 24)
        prompt = repair_prompt(
            _frozen(),
            "review_REPAIR",
            "safe finding about citations\npassword: hunter2-extra\n" + secret,
        )
        self.assertIn("safe finding about citations", prompt)
        self.assertNotIn(secret, prompt)
        self.assertNotIn("hunter2", prompt)

    def test_response_shape_unchanged(self):
        prompt = repair_prompt(_frozen(), "body_has_frontmatter")
        self.assertIn(
            "Return JSON only with keys body, summary, dispositions",
            prompt,
        )
        self.assertEqual(prompt.count("keys body, summary, dispositions"), 1)


if __name__ == "__main__":
    unittest.main()
