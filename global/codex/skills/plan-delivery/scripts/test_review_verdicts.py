import unittest
from planner_witness import has_explicit_pass


class ReviewVerdictTests(unittest.TestCase):
    def test_bare_and_colon_explanation(self):
        for text in ('FIDELITY PASS', 'FIDELITY PASS: all requirements covered'):
            self.assertTrue(has_explicit_pass(text, 'FIDELITY'))

    def test_provider_dash_explanations(self):
        for separator in ('-', '\u2013', '\u2014'):
            with self.subTest(separator=separator):
                self.assertTrue(has_explicit_pass('FIDELITY PASS ' + separator + ' covered', 'FIDELITY'))

    def test_label_colon(self):
        self.assertTrue(has_explicit_pass('FIDELITY: PASS', 'FIDELITY'))

    def test_contradiction_with_dash_is_refused(self):
        for verdict in ('REPAIR', 'FAIL', 'BLOCKED'):
            for separator in (':', '-', '\u2013', '\u2014'):
                text = 'FIDELITY PASS\nFIDELITY ' + verdict + ' ' + separator + ' defect'
                self.assertFalse(has_explicit_pass(text, 'FIDELITY'))

    def test_colon_label_contradiction_is_refused(self):
        self.assertFalse(has_explicit_pass('FIDELITY PASS\nFIDELITY: REPAIR', 'FIDELITY'))

    def test_unlabelled_or_quoted_or_wrong_dimension_is_refused(self):
        for text in ('Probably PASS', '> FIDELITY PASS', 'SUFFICIENCY PASS',
                     'FIDELITY PASSED', 'FIDELITY PASS but really unknown', None):
            self.assertFalse(has_explicit_pass(text, 'FIDELITY'))

    def test_duplicate_unambiguous_passes(self):
        self.assertTrue(has_explicit_pass('FIDELITY PASS\nFIDELITY: PASS: verified', 'FIDELITY'))


if __name__ == '__main__':
    unittest.main()
