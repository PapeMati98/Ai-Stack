"""Hardening checks for caretaker.publication.synthesis_prompt."""

import unittest

from caretaker.publication import synthesis_prompt


def _prompt() -> str:
    frozen = {
        "target": "wiki/projects/example.md",
        "before": "short existing page",
        "sources": {},
        "proposals": [],
    }
    return synthesis_prompt(frozen)


class SynthesisPromptHardeningTests(unittest.TestCase):
    def test_forbids_deleting_negating_sentences_when_shortening(self):
        prompt = _prompt()
        self.assertIn(
            "A sentence stating that something is absent carries as much information as a sentence stating that something exists",
            prompt,
        )
        self.assertIn("must not be deleted when shortening", prompt)
        self.assertIn("deleting a negation reverses the meaning", prompt)

    def test_requires_keeping_source_contradictions(self):
        prompt = _prompt()
        self.assertIn(
            "Keep conflicting claims from different sources as an explicit contradiction",
            prompt,
        )
        self.assertIn("never fuse them into one coherent story", prompt)

    def test_requires_keeping_open_security_and_ownership_limits(self):
        prompt = _prompt()
        self.assertIn(
            "Keep still-open security and ownership limitations",
            prompt,
        )

    def test_forbids_describing_unimplemented_candidate_as_live(self):
        prompt = _prompt()
        self.assertIn(
            "Do not describe an unimplemented candidate as accepted, approved, or live",
            prompt,
        )

    def test_requires_keeping_earlier_dated_facts(self):
        prompt = _prompt()
        self.assertIn(
            "Keep already-recorded earlier-dated facts",
            prompt,
        )

    def test_response_shape_unchanged(self):
        prompt = _prompt()
        self.assertIn("Return JSON only with keys body, summary, dispositions", prompt)
        self.assertEqual(prompt.count("keys body, summary, dispositions"), 1)
