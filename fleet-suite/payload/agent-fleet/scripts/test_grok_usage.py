"""Regression tests for native Grok usage projection."""

import json
import math
import unittest
from pathlib import Path

from grok_usage import extract_native_grok_usage

FIXTURE_PATH = Path(__file__).with_name("native-numeric-fixture.json")


def _envelope(usage=None, **overrides):
    payload = {
        "sessionId": "synthetic-metadata-fixture",
        "modelId": "grok-4.7",
    }
    if usage is not None:
        payload["usage"] = usage
    payload.update(overrides)
    return payload


class NativeGrokUsageTests(unittest.TestCase):
    def test_numeric_fixture_keeps_exact_known_counters(self):
        payload = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
        projected = extract_native_grok_usage(payload)
        self.assertEqual(
            {
                "status": "known",
                "source": "provider-json",
                "confidence": 1.0,
                "adapter": "grok",
                "input_tokens": 75220,
                "output_tokens": 5053,
                "cache_read_tokens": 46080,
                "cache_write_tokens": 0,
                "reasoning_tokens": 4148,
                "total_tokens": 80273,
                "fresh_input_tokens": None,
                "cost_usd": None,
                "total_derived": False,
                "fresh_input_semantics": "unverified",
            },
            projected,
        )
        self.assertIsNone(projected["cost_usd"])
        self.assertNotIn("costUsdTicks", projected)

    def test_model_usage_mirror_is_not_summed(self):
        payload = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
        payload["modelUsage"]["grok-4.7"] = {
            "inputTokens": 1,
            "outputTokens": 1,
            "cachedReadTokens": 1,
            "cacheCreationTokens": 1,
            "reasoningTokens": 1,
            "totalTokens": 2,
            "costUsdTicks": 99,
        }
        projected = extract_native_grok_usage(payload)
        self.assertEqual(75220, projected["input_tokens"])
        self.assertEqual(5053, projected["output_tokens"])
        self.assertEqual(46080, projected["cache_read_tokens"])
        self.assertEqual(0, projected["cache_write_tokens"])
        self.assertEqual(4148, projected["reasoning_tokens"])
        self.assertEqual(80273, projected["total_tokens"])
        self.assertFalse(projected["total_derived"])
        self.assertIsNone(projected["cost_usd"])

    def test_missing_and_malformed_usage_are_unknown(self):
        for payload in (
            _envelope(),
            _envelope(usage=None),
            _envelope(usage=[]),
            _envelope(usage="75220"),
            _envelope(usage=80273),
        ):
            projected = extract_native_grok_usage(payload)
            self.assertEqual("unknown", projected["status"])
            self.assertEqual("provider-json", projected["source"])
            self.assertEqual(0.0, projected["confidence"])
            self.assertEqual("grok", projected["adapter"])
            self.assertFalse(projected["total_derived"])
            self.assertIsNone(projected["fresh_input_tokens"])
            self.assertEqual("unverified", projected["fresh_input_semantics"])
            self.assertIsNone(projected["cost_usd"])
            for name in (
                "input_tokens",
                "output_tokens",
                "cache_read_tokens",
                "cache_write_tokens",
                "reasoning_tokens",
                "total_tokens",
            ):
                self.assertIsNone(projected[name])

    def test_invalid_counters_do_not_erase_valid_siblings(self):
        usage = {
            "inputTokens": True,
            "outputTokens": 5,
            "cachedReadTokens": 1.5,
            "cacheCreationTokens": "0",
            "reasoningTokens": -3,
            "totalTokens": 5,
            "costUsdTicks": 379569200,
            "apiDurationMs": 10,
            "modelCalls": 2,
            "numTurns": 2,
        }
        projected = extract_native_grok_usage(_envelope(usage=usage))
        self.assertIsNone(projected["input_tokens"])
        self.assertEqual(5, projected["output_tokens"])
        self.assertIsNone(projected["cache_read_tokens"])
        self.assertIsNone(projected["cache_write_tokens"])
        self.assertIsNone(projected["reasoning_tokens"])
        self.assertEqual(5, projected["total_tokens"])
        self.assertEqual("known", projected["status"])
        self.assertEqual(1.0, projected["confidence"])
        self.assertFalse(projected["total_derived"])
        self.assertIsNone(projected["cost_usd"])
        self.assertIsNone(projected["fresh_input_tokens"])

    def test_nonfinite_floats_are_rejected(self):
        for bad in (math.nan, math.inf, -math.inf, 1.0):
            projected = extract_native_grok_usage(
                _envelope(
                    usage={
                        "inputTokens": bad,
                        "outputTokens": 2,
                        "totalTokens": 2,
                    }
                )
            )
            self.assertIsNone(projected["input_tokens"])
            self.assertEqual(2, projected["output_tokens"])
            self.assertEqual(2, projected["total_tokens"])

    def test_explicit_zero_is_known(self):
        projected = extract_native_grok_usage(
            _envelope(
                usage={
                    "inputTokens": 0,
                    "outputTokens": 0,
                    "cachedReadTokens": 0,
                    "cacheCreationTokens": 0,
                    "reasoningTokens": 0,
                    "totalTokens": 0,
                    "costUsdTicks": 0,
                }
            )
        )
        self.assertEqual(0, projected["input_tokens"])
        self.assertEqual(0, projected["output_tokens"])
        self.assertEqual(0, projected["cache_read_tokens"])
        self.assertEqual(0, projected["cache_write_tokens"])
        self.assertEqual(0, projected["reasoning_tokens"])
        self.assertEqual(0, projected["total_tokens"])
        self.assertEqual("known", projected["status"])
        self.assertEqual(1.0, projected["confidence"])
        self.assertFalse(projected["total_derived"])
        self.assertIsNone(projected["cost_usd"])

    def test_absent_total_is_derived_only_from_valid_io(self):
        projected = extract_native_grok_usage(
            _envelope(usage={"inputTokens": 10, "outputTokens": 4, "cachedReadTokens": 7})
        )
        self.assertEqual(14, projected["total_tokens"])
        self.assertTrue(projected["total_derived"])
        self.assertEqual(0.8, projected["confidence"])
        self.assertEqual("known", projected["status"])
        self.assertEqual(7, projected["cache_read_tokens"])
        self.assertIsNone(projected["fresh_input_tokens"])

    def test_invalid_total_is_not_replaced_by_arithmetic(self):
        for bad_total in ("14", 14.0, True, -1, None, math.nan):
            projected = extract_native_grok_usage(
                _envelope(
                    usage={
                        "inputTokens": 10,
                        "outputTokens": 4,
                        "totalTokens": bad_total,
                    }
                )
            )
            self.assertIsNone(projected["total_tokens"])
            self.assertFalse(projected["total_derived"])
            self.assertEqual(1.0, projected["confidence"])
            self.assertEqual(10, projected["input_tokens"])
            self.assertEqual(4, projected["output_tokens"])

    def test_inconsistent_smaller_total_is_dropped(self):
        projected = extract_native_grok_usage(
            _envelope(usage={"inputTokens": 10, "outputTokens": 4, "totalTokens": 11})
        )
        self.assertIsNone(projected["total_tokens"])
        self.assertFalse(projected["total_derived"])
        self.assertEqual(1.0, projected["confidence"])
        self.assertEqual("known", projected["status"])

    def test_larger_reported_total_is_retained(self):
        projected = extract_native_grok_usage(
            _envelope(
                usage={
                    "inputTokens": 10,
                    "outputTokens": 4,
                    "reasoningTokens": 3,
                    "totalTokens": 20,
                }
            )
        )
        self.assertEqual(20, projected["total_tokens"])
        self.assertFalse(projected["total_derived"])
        self.assertEqual(1.0, projected["confidence"])
        self.assertEqual(10, projected["input_tokens"])
        self.assertEqual(4, projected["output_tokens"])

    def test_incomplete_counters_are_not_zero_filled(self):
        projected = extract_native_grok_usage(_envelope(usage={"outputTokens": 8}))
        self.assertEqual(8, projected["output_tokens"])
        self.assertIsNone(projected["input_tokens"])
        self.assertIsNone(projected["total_tokens"])
        self.assertIsNone(projected["cache_read_tokens"])
        self.assertIsNone(projected["cache_write_tokens"])
        self.assertIsNone(projected["reasoning_tokens"])
        self.assertFalse(projected["total_derived"])
        self.assertEqual("known", projected["status"])

    def test_unrelated_and_nested_envelopes_return_none(self):
        native = _envelope(usage={"inputTokens": 3, "outputTokens": 1, "totalTokens": 4})
        samples = (
            None,
            [],
            "sessionId grok-4.7 inputTokens 3",
            {"usage": {"inputTokens": 3, "outputTokens": 1, "totalTokens": 4}},
            {"sessionId": "", "modelId": "grok-4.7", "usage": {"inputTokens": 1}},
            {"sessionId": "s", "modelId": "gpt-4", "usage": {"inputTokens": 1}},
            {"sessionId": 1, "modelId": "grok-4.7"},
            {"task": native},
            {"messages": [native]},
            {"modelUsage": {"grok-4.7": {"inputTokens": 9, "outputTokens": 1, "totalTokens": 10}}},
        )
        for sample in samples:
            self.assertIsNone(extract_native_grok_usage(sample))

    def test_non_token_fields_never_become_cost_or_fresh_input(self):
        projected = extract_native_grok_usage(
            _envelope(
                usage={
                    "costUsdTicks": 379569200,
                    "apiDurationMs": 78514,
                    "modelCalls": 3,
                    "numTurns": 3,
                    "cachedReadTokens": 46080,
                }
            )
        )
        self.assertEqual(46080, projected["cache_read_tokens"])
        self.assertIsNone(projected["input_tokens"])
        self.assertIsNone(projected["fresh_input_tokens"])
        self.assertEqual("unverified", projected["fresh_input_semantics"])
        self.assertIsNone(projected["cost_usd"])
        self.assertIsNone(projected["total_tokens"])
        self.assertFalse(projected["total_derived"])


if __name__ == "__main__":
    unittest.main()
