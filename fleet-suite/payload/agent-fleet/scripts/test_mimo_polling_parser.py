"""HD-4429b6: MiMo polling text parser bottleneck repair and trust boundary regressions.

Verifies that mimo_events skips non-JSON polling text efficiently without
quadratic JSONDecodeError line counting over prefixes. Verifies that decoder
attempts scale with structured candidates ({, [, ") rather than characters,
while preserving trust boundaries for quoted strings, top-level arrays,
scalar noise, adjacent objects, malformed prefixes, and parse bounds.
"""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from unittest import mock

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

import mimo_runtime  # noqa: E402


POLLING_LINE = (
    "INFO  2026-10-16T10:01:44 +0ms service=server status=started method=GET "
    "path=/session/status request\n"
)
# 101 bytes per line * 4300 lines = 434,300 bytes (at least 410 KB)
REALISTIC_POLLING_410K = POLLING_LINE * 4300

REAL_SESSION_EVENT = {
    "type": "session",
    "providerID": "xiaomi",
    "modelID": "mimo-v2.6-pro",
    "sessionID": "ses_410k_test0000000000",
}


class MiMoPollingParserScalingTests(unittest.TestCase):
    """Deterministic regression: decoder attempts scale with candidates, not characters."""

    def test_realistic_410kb_polling_log_attempts_scale_with_candidates_not_characters(
        self,
    ) -> None:
        payload = REALISTIC_POLLING_410K + json.dumps(REAL_SESSION_EVENT) + "\n"
        self.assertGreaterEqual(len(payload), 410_000)

        call_count = 0
        orig_raw_decode = json.JSONDecoder.raw_decode

        def counting_raw_decode(decoder_self, s, idx=0):
            nonlocal call_count
            call_count += 1
            return orig_raw_decode(decoder_self, s, idx)

        with mock.patch.object(json.JSONDecoder, "raw_decode", counting_raw_decode):
            events = mimo_runtime.mimo_events(payload)

        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["modelID"], "mimo-v2.6-pro")
        self.assertEqual(events[0]["type"], "session")
        self.assertEqual(events[0]["providerID"], "xiaomi")
        # Exactly 1 attempt at the structured candidate '{', instead of the full stream
        # character attempts that caused the measured finalization stall on unchanged 0.9.125.
        self.assertEqual(call_count, 1)

    def test_polling_stream_with_mixed_candidates_scales_with_candidate_count(
        self,
    ) -> None:
        mixed_log = (
            "INFO  2026-10-16T09:51:46 +640ms service=server-proxy version=\"0.1.15\" args=[\"run\"]\n"
            + (POLLING_LINE * 100)
            + json.dumps({"type": "text", "part": {"text": "output content"}})
            + "\n"
        )
        call_count = 0
        orig_raw_decode = json.JSONDecoder.raw_decode

        def counting_raw_decode(decoder_self, s, idx=0):
            nonlocal call_count
            call_count += 1
            return orig_raw_decode(decoder_self, s, idx)

        with mock.patch.object(json.JSONDecoder, "raw_decode", counting_raw_decode):
            events = mimo_runtime.mimo_events(mixed_log)

        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["type"], "text")
        # Candidates encountered:
        # 1. '"0.1.15"' -> parsed as str, suppressed
        # 2. '["run"]' -> parsed as list, suppressed
        # 3. '{"type": ...}' -> parsed as dict, kept
        # Exactly 3 attempts over > 9,700 characters
        self.assertEqual(call_count, 3)

    def test_deterministic_regression_fails_on_125_and_passes_on_126(self) -> None:
        short_stream = (POLLING_LINE * 50) + json.dumps(REAL_SESSION_EVENT) + "\n"
        call_count = 0
        orig_raw_decode = json.JSONDecoder.raw_decode

        def counting_raw_decode(decoder_self, s, idx=0):
            nonlocal call_count
            call_count += 1
            return orig_raw_decode(decoder_self, s, idx)

        with mock.patch.object(json.JSONDecoder, "raw_decode", counting_raw_decode):
            events = mimo_runtime.mimo_events(short_stream)

        self.assertEqual(len(events), 1)
        # Unchanged 0.9.125 called raw_decode > 4800 times here; 0.9.126 calls it exactly once.
        self.assertLessEqual(call_count, 2)


class MiMoTrustBoundaryStringRegressionTests(unittest.TestCase):
    """Trust boundary: objects inside valid quoted JSON strings must not be harvested."""

    def test_objects_inside_valid_quoted_json_strings_are_not_harvested(
        self,
    ) -> None:
        payload = (
            "INFO logger prefix\n"
            '"valid string with {\\"modelID\\": \\"fake-model\\", \\"status\\": \\"fake\\"} object"\n'
            '{"modelID": "mimo-v2.6-pro", "type": "session", "providerID": "xiaomi"}\n'
        )
        events = mimo_runtime.mimo_events(payload)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["modelID"], "mimo-v2.6-pro")

    def test_escaped_quotes_inside_json_string_do_not_leak_braces(self) -> None:
        payload = (
            '"text with \\"escaped quotes\\" and {\\"fake\\": 1} inside"\n'
            '{"real": 2}\n'
        )
        events = mimo_runtime.mimo_events(payload)
        self.assertEqual(events, [{"real": 2}])

    def test_nested_braces_in_standalone_string_yields_empty_events(self) -> None:
        payload = '"{\\"outer\\": {\\"inner\\": {\\"key\\": 1}}}"'
        self.assertEqual(mimo_runtime.mimo_events(payload), [])

    def test_model_identity_rejects_untrusted_quoted_string_claims(self) -> None:
        stderr = (
            "INFO stream line\n"
            '"report: {\\"providerID\\": \\"xiaomi\\", \\"modelID\\": \\"mimo-auto\\"}"\n'
        )
        identity = mimo_runtime.mimo_model_identity(stderr)
        self.assertNotEqual(identity["status"], "verified")
        self.assertNotIn("mimo-auto", identity["observed_models"])


class MiMoTopLevelArrayRegressionTests(unittest.TestCase):
    """Top-level arrays in stream must be suppressed consistently with 0.9.125."""

    def test_top_level_array_in_stream_is_suppressed(self) -> None:
        payload = (
            "INFO log\n"
            '[{"suppressed": true, "modelID": "fake"}]\n'
            '{"real": 1}\n'
        )
        events = mimo_runtime.mimo_events(payload)
        self.assertEqual(events, [{"real": 1}])

    def test_top_level_array_of_scalars_in_stream(self) -> None:
        payload = 'INFO log\n["item1", "item2"]\n{"real": 1}\n'
        events = mimo_runtime.mimo_events(payload)
        self.assertEqual(events, [{"real": 1}])

    def test_pure_top_level_array_without_prefix_parses_dicts(self) -> None:
        payload = '[{"event": 1}, {"event": 2}]'
        events = mimo_runtime.mimo_events(payload)
        self.assertEqual(events, [{"event": 1}, {"event": 2}])

    def test_pure_top_level_array_filters_non_dicts(self) -> None:
        payload = '[{"event": 1}, "string", 42, {"event": 2}]'
        events = mimo_runtime.mimo_events(payload)
        self.assertEqual(events, [{"event": 1}, {"event": 2}])


class MiMoScalarNoiseRegressionTests(unittest.TestCase):
    """Scalar noise must not disrupt event extraction or trigger unnecessary decoding."""

    def test_scalar_noise_in_stream_is_ignored(self) -> None:
        payload = (
            'INFO 123 true null 45.6 "scalar string" false {"real": 1} 789'
        )
        events = mimo_runtime.mimo_events(payload)
        self.assertEqual(events, [{"real": 1}])

    def test_pure_scalar_noise_makes_zero_decoder_calls(self) -> None:
        call_count = 0
        orig_raw_decode = json.JSONDecoder.raw_decode

        def counting_raw_decode(decoder_self, s, idx=0):
            nonlocal call_count
            call_count += 1
            return orig_raw_decode(decoder_self, s, idx)

        with mock.patch.object(json.JSONDecoder, "raw_decode", counting_raw_decode):
            events = mimo_runtime.mimo_events("123 true null 45.6 false")
        self.assertEqual(events, [])
        self.assertEqual(call_count, 0)


class MiMoAdjacentObjectsRegressionTests(unittest.TestCase):
    """Adjacent and newline-delimited objects must all be parsed."""

    def test_adjacent_objects_without_whitespace(self) -> None:
        payload = '{"a": 1}{"b": 2}{"c": 3}'
        self.assertEqual(
            mimo_runtime.mimo_events(payload),
            [{"a": 1}, {"b": 2}, {"c": 3}],
        )

    def test_adjacent_objects_with_whitespace_and_newlines(self) -> None:
        payload = '{"a": 1}\r\n  {"b": 2} \t\n {"c": 3}'
        self.assertEqual(
            mimo_runtime.mimo_events(payload),
            [{"a": 1}, {"b": 2}, {"c": 3}],
        )


class MiMoInfoTextRegressionTests(unittest.TestCase):
    """INFO logger text must be preserved alongside structured events."""

    def test_info_logger_lines_without_json_produce_no_events(self) -> None:
        payload = (
            "INFO  2026-10-16T09:51:46 +640ms service=server-proxy version=0.1.15 args=[\"run\"]\n"
            "INFO  2026-10-16T09:51:47 +100ms service=session status=running\n"
        )
        self.assertEqual(mimo_runtime.mimo_events(payload), [])

    def test_interleaved_info_lines_and_json_events(self) -> None:
        payload = (
            "INFO line 1\n"
            '{"event": 1}\n'
            "INFO line 2\n"
            '{"event": 2}\n'
        )
        self.assertEqual(
            mimo_runtime.mimo_events(payload),
            [{"event": 1}, {"event": 2}],
        )


class MiMoMalformedPrefixRegressionTests(unittest.TestCase):
    """Malformed prefixes must not abort parsing of subsequent valid events."""

    def test_malformed_brace_prefix_allows_subsequent_event(self) -> None:
        payload = '{malformed not json\n{"real": 1}'
        self.assertEqual(mimo_runtime.mimo_events(payload), [{"real": 1}])

    def test_code_content_with_braces_in_tool_output(self) -> None:
        payload = (
            "<content>\n28: NAMES = {\n29:   \"FIRST_NAME\",\n30: }\n</content>\n"
            '{"status": "completed"}'
        )
        self.assertEqual(
            mimo_runtime.mimo_events(payload), [{"status": "completed"}]
        )

    def test_malformed_bracket_prefix(self) -> None:
        payload = '[unclosed array\n{"real": 1}'
        self.assertEqual(mimo_runtime.mimo_events(payload), [{"real": 1}])

    def test_malformed_quote_prefix(self) -> None:
        payload = '"unclosed string\n{"real": 1}'
        self.assertEqual(mimo_runtime.mimo_events(payload), [{"real": 1}])

    def test_trailing_malformed_object(self) -> None:
        payload = '{"real": 1}\n{"unclosed object without brace'
        self.assertEqual(mimo_runtime.mimo_events(payload), [{"real": 1}])


class MiMoParseBoundsRegressionTests(unittest.TestCase):
    """Parse bounds must be strictly enforced."""

    def test_max_json_objects_cap_is_enforced(self) -> None:
        payload = "\n".join(json.dumps({"i": i}) for i in range(600))
        events = mimo_runtime.mimo_events(payload)
        self.assertEqual(len(events), mimo_runtime.MIMO_MAX_JSON_OBJECTS)
        self.assertEqual(events[0]["i"], 0)
        self.assertEqual(events[511]["i"], 511)

    def test_max_parse_chars_truncation(self) -> None:
        padding = " " * mimo_runtime.MIMO_MAX_PARSE_CHARS
        payload = padding + '{"after_limit": true}'
        self.assertEqual(mimo_runtime.mimo_events(payload), [])

    def test_empty_and_whitespace_inputs(self) -> None:
        self.assertEqual(mimo_runtime.mimo_events(""), [])
        self.assertEqual(mimo_runtime.mimo_events("   \n\t  "), [])
        self.assertEqual(mimo_runtime.mimo_events(None), [])
        self.assertEqual(mimo_runtime.mimo_events(12345), [])


class MiMoE2EIdentityAndStatusTests(unittest.TestCase):
    """End-to-end integration: model identity and terminal status survive polling prefix."""

    def test_model_identity_with_410kb_polling_log_verifies_native_model(self) -> None:
        stream = REALISTIC_POLLING_410K + json.dumps(REAL_SESSION_EVENT) + "\n"
        identity = mimo_runtime.mimo_model_identity(stream)
        self.assertEqual(identity["status"], "verified")
        self.assertEqual(identity["observed_models"], ["mimo-v2.6-pro"])
        self.assertEqual(identity["observed_providers"], ["xiaomi"])

    def test_terminal_status_survives_polling_prefix(self) -> None:
        stream = REALISTIC_POLLING_410K + json.dumps({"status": "completed"}) + "\n"
        reason, error = mimo_runtime.mimo_terminal_status(stream)
        self.assertEqual(reason, "completed")
        self.assertIsNone(error)


if __name__ == "__main__":
    unittest.main()
