"""Explicit verdicts retain their meaning when followed by an explanation."""
import unittest
import planner_witness


class VerdictFormatTests(unittest.TestCase):
    def test_standalone_and_explained_pass(self):
        for text in ('FIDELITY PASS', 'FIDELITY PASS: all original requirements preserved.',
                     '  FIDELITY PASS: complete\nSUFFICIENCY PASS: sufficient'):
            self.assertTrue(planner_witness.has_explicit_pass(text, 'FIDELITY'))

    def test_ambiguous_negative_or_embedded_text_is_not_pass(self):
        for text in ('NOT FIDELITY PASS', 'Expected FIDELITY PASS',
                     'FIDELITY PASSING', 'FIDELITY PASS if repaired',
                     'FIDELITY REPAIR', 'FIDELITY PASS\nFIDELITY REPAIR: missing requirement',
                     'FIDELITY PASS\nFIDELITY BLOCKED', '"FIDELITY PASS"'):
            self.assertFalse(planner_witness.has_explicit_pass(text, 'FIDELITY'))


if __name__ == '__main__':
    unittest.main()
