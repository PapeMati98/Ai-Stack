"""Additional tests for real independent-review findings, after the first Graph."""
from types import MappingProxyType
import unittest
import contract_tests as base

class ReviewRegression(unittest.TestCase):
    @classmethod
    def setUpClass(cls): cls.m=base.load_library()
    def test_mapping_objects_and_nested_tokens_are_supported(self):
        row=MappingProxyType({'run_id':'mapping','tokens':MappingProxyType({'total_tokens':17})})
        self.assertEqual(self.m.normalize_run(row)['tokens']['total_tokens'],17)
    def test_noniterable_or_record_shaped_population_is_controlled(self):
        for value in (None,3,'text',{'run_id':'not-a-population'}):
            with self.subTest(kind=type(value).__name__),self.assertRaises(ValueError): self.m.summarize_runs(value)
    def test_finite_duration_addition_overflow_is_controlled(self):
        rows=[{'run_id':'one','duration_seconds':1e308},{'run_id':'two','duration_seconds':1e308}]
        with self.assertRaises(ValueError): self.m.summarize_runs(rows)
    def test_mixed_large_integer_float_overflow_is_controlled(self):
        rows=[{'run_id':'one','duration_seconds':10**1000},{'run_id':'two','duration_seconds':1.0}]
        with self.assertRaises(ValueError): self.m.summarize_runs(rows)

class CliReviewRegression(unittest.TestCase):
    run_cli=base.CliContract.run_cli
    fixture=base.CliContract.fixture
    def test_non_json_constants_are_rejected_even_in_ignored_fields(self):
        for value in ('NaN','Infinity','-Infinity'):
            payload='[{"run_id":"synthetic-nonjson","extra":'+value+'}]'
            result=self.run_cli([self.fixture(payload)])
            self.assertEqual(result.returncode,2,result.stderr)
            self.assertNotIn(b'Traceback',result.stderr)
            self.assertNotIn(b'synthetic-nonjson',result.stderr+result.stdout)
    def test_output_integer_conversion_limit_is_controlled(self):
        number='9'*4300
        payload='[{"run_id":"one","tokens":{"total_tokens":'+number+'}},{"run_id":"two","tokens":{"total_tokens":'+number+'}}]'
        result=self.run_cli([self.fixture(payload)])
        self.assertEqual(result.returncode,2,result.stderr)
        self.assertTrue(result.stderr)
        self.assertNotIn(b'Traceback',result.stderr)
        self.assertEqual(result.stdout,b'')

if __name__=='__main__': unittest.main(verbosity=2)
