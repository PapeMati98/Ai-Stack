import json
import unittest
from pathlib import Path
import adaptive
import grok_usage

class IntegratedGrokUsageTests(unittest.TestCase):
    def extract(self, usage, **extra):
        payload = {'sessionId': 'synthetic', 'modelId': 'grok-4.7', 'usage': usage, **extra}
        return adaptive.extract_token_usage(json.dumps(payload), adapter='grok')

    def test_real_numeric_cache_survives_without_fresh_claim(self):
        payload = json.loads(Path(__file__).with_name('native-numeric-fixture.json').read_bytes())
        result = adaptive.extract_token_usage(json.dumps(payload), adapter='grok')
        for field, expected in [('input_tokens', 75220), ('output_tokens', 5053), ('cache_read_tokens', 46080), ('cache_write_tokens', 0), ('reasoning_tokens', 4148), ('total_tokens', 80273)]:
            self.assertEqual(result[field], expected)
        self.assertIsNone(result['fresh_input_tokens'])
        self.assertIsNone(result['cost_usd'])
        self.assertFalse(result['total_derived'])

    def test_native_run_root_not_nested_model_or_task_usage(self):
        result = self.extract({'inputTokens': 4, 'outputTokens': 2, 'totalTokens': 6, 'cachedReadTokens': 3}, modelUsage={'grok-4.7': {'inputTokens': 400, 'outputTokens': 200, 'totalTokens': 600, 'cachedReadTokens': 300}}, payload={'usage': {'inputTokens': 999999, 'totalTokens': 999999}})
        self.assertEqual(result['total_tokens'], 6)
        self.assertEqual(result['cache_read_tokens'], 3)

    def test_recognized_missing_usage_never_falls_back_to_nested(self):
        payload = {'sessionId': 'synthetic', 'modelId': 'grok-4.7', 'modelUsage': {'grok-4.7': {'inputTokens': 90, 'outputTokens': 5}}}
        result = adaptive.extract_token_usage(json.dumps(payload), adapter='grok')
        self.assertEqual(result['status'], 'unknown')
        self.assertIsNone(result['total_tokens'])

    def test_recognized_invalid_usage_never_uses_nested_tokens(self):
        for value in [None, [], 'invalid', False]:
            with self.subTest(value=value):
                result = self.extract(value, modelUsage={'grok-4.7': {'inputTokens': 10}})
                self.assertEqual(result['status'], 'unknown')

    def test_absent_total_derives_only_two_known_operands(self):
        result = self.extract({'inputTokens': 10, 'outputTokens': 2, 'cachedReadTokens': 100})
        self.assertEqual(result['total_tokens'], 12)
        self.assertTrue(result['total_derived'])
        self.assertEqual(result['cache_read_tokens'], 100)
        for usage in [{'inputTokens': 10}, {'outputTokens': 2}, {'inputTokens': 10, 'outputTokens': None}]:
            self.assertIsNone(self.extract(usage)['total_tokens'])

    def test_present_invalid_total_is_not_replaced(self):
        for bad in [None, True, 3.5, '12', -1, float('nan'), float('inf')]:
            with self.subTest(value=bad):
                result = self.extract({'inputTokens': 10, 'outputTokens': 2, 'totalTokens': bad})
                self.assertIsNone(result['total_tokens'])
                self.assertFalse(result['total_derived'])

    def test_inconsistent_reported_total_remains_unknown(self):
        result = self.extract({'inputTokens': 10, 'outputTokens': 2, 'totalTokens': 3})
        self.assertIsNone(result['total_tokens'])
        self.assertFalse(result['total_derived'])
        self.assertEqual(result['input_tokens'], 10)

    def test_reported_total_not_cache_or_reasoning_arithmetic(self):
        result = self.extract({'inputTokens': 10, 'outputTokens': 2, 'totalTokens': 25, 'cachedReadTokens': 40, 'cacheCreationTokens': 5, 'reasoningTokens': 7})
        self.assertEqual(result['total_tokens'], 25)
        self.assertIsNone(result['fresh_input_tokens'])

    def test_explicit_zero_is_known_and_preserved(self):
        result = self.extract({k: 0 for k in ['inputTokens', 'outputTokens', 'totalTokens', 'cachedReadTokens', 'cacheCreationTokens', 'reasoningTokens']})
        self.assertEqual(result['status'], 'known')
        self.assertEqual(result['total_tokens'], 0)
        self.assertEqual(result['cache_read_tokens'], 0)

    def test_invalid_individual_counters_do_not_round_or_zero(self):
        mapping = {'inputTokens': 'input_tokens', 'outputTokens': 'output_tokens', 'cachedReadTokens': 'cache_read_tokens', 'cacheCreationTokens': 'cache_write_tokens', 'reasoningTokens': 'reasoning_tokens'}
        for native, target in mapping.items():
            for bad in [True, False, -1, 2.0, '2', None, float('nan'), float('inf')]:
                with self.subTest(native=native, value=bad):
                    result = self.extract({native: bad, 'totalTokens': 50})
                    self.assertIsNone(result[target])
                    self.assertEqual(result['total_tokens'], 50)

    def test_only_valid_cache_still_known_partial(self):
        result = self.extract({'cachedReadTokens': 9})
        self.assertEqual(result['status'], 'known')
        self.assertEqual(result['cache_read_tokens'], 9)
        self.assertIsNone(result['input_tokens'])
        self.assertIsNone(result['total_tokens'])

    def test_only_cost_or_model_calls_does_not_invent_tokens(self):
        result = self.extract({'costUsdTicks': 379569200, 'modelCalls': 3, 'numTurns': 3, 'apiDurationMs': 78514})
        self.assertEqual(result['status'], 'unknown')
        self.assertIsNone(result['cost_usd'])
        self.assertIsNone(result['total_tokens'])

    def test_helper_rejects_other_and_nested_envelopes(self):
        for payload in [None, [], {}, {'sessionId': 's', 'modelId': 'claude-x', 'usage': {'inputTokens': 10}}, {'nested': {'sessionId': 's', 'modelId': 'grok-4.7', 'usage': {'inputTokens': 10}}}]:
            self.assertIsNone(grok_usage.extract_native_grok_usage(payload))

    def test_legacy_grok_keeps_counters_but_unknown_fresh(self):
        result = adaptive.extract_token_usage(json.dumps({'usage': {'input_tokens': 10, 'output_tokens': 2, 'total_tokens': 12, 'cache_read_input_tokens': 5}}), adapter='grok')
        self.assertEqual(result['input_tokens'], 10)
        self.assertEqual(result['cache_read_tokens'], 5)
        self.assertEqual(result['total_tokens'], 12)
        self.assertIsNone(result['fresh_input_tokens'])

    def test_unknown_native_shape_is_explicit(self):
        result = adaptive.extract_token_usage('no usage', adapter='grok')
        self.assertEqual(result['status'], 'unknown')
        self.assertIsNone(result['fresh_input_tokens'])

