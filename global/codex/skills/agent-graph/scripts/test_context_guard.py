"""Deterministic tests for the bounded maker->reviewer evidence guard.

Synthetic secrets and a synthetic redactor only; no Fleet runtime, no models,
no network. The guard must redact recognizable secret shapes, preserve benign
content byte-identical where possible and fail closed on bound breaches.
"""
import base64
import json
from pathlib import Path
import sys
import unittest

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parent))

import context_guard
from context_guard import GuardError


class SyntheticRedactor:
    """Minimal stand-in for Fleet's redact_sensitive_value."""

    def __init__(self, *, fail=False, malformed=False):
        self.fail = fail
        self.malformed = malformed

    def __call__(self, value):
        if self.fail:
            raise RuntimeError("synthetic redactor failure")
        if isinstance(value, str):
            if self.malformed:
                return None
            return value.replace("api_key=secret-value", "api_key=[REDACTED]")
        if isinstance(value, list):
            return [self(item) for item in value]
        if isinstance(value, dict):
            return {key: self(item) for key, item in value.items()}
        return value


def b64(text):
    return base64.b64encode(text.encode("utf-8")).decode("ascii")


class StructuredJsonTests(unittest.TestCase):
    def test_sensitive_json_keys_redacted_shape_and_order_preserved(self):
        raw = json.dumps({"note": "benign", "db_password": "hunter2-secret",
                          "api_key": "synthetic-api-key", "count": 3}, indent=2)
        out = context_guard.guard_text(raw, SyntheticRedactor())
        parsed = json.loads(out)
        self.assertEqual(parsed["db_password"], "[REDACTED]")
        self.assertEqual(parsed["api_key"], "[REDACTED]")
        self.assertEqual(parsed["note"], "benign")
        self.assertEqual(parsed["count"], 3)
        self.assertEqual(list(parsed), ["note", "db_password", "api_key", "count"])
        self.assertNotIn("hunter2-secret", out)

    def test_redacted_key_collision_keeps_both_entries(self):
        raw = '{"API_KEY": "aaa-live-secret", "api_key": "bbb-live-secret"}'
        out = context_guard.guard_text(raw, SyntheticRedactor())
        parsed = json.loads(out)
        self.assertEqual(parsed, {"API_KEY": "[REDACTED]", "api_key": "[REDACTED]"})
        self.assertNotIn("live-secret", out)

    def test_nested_and_list_values_redacted_within_depth_bound(self):
        raw = json.dumps({"outer": [{"token": "t-live"}, {"ok": "fine"}]})
        out = context_guard.guard_text(raw, SyntheticRedactor())
        parsed = json.loads(out)
        self.assertEqual(parsed["outer"][0]["token"], "[REDACTED]")
        self.assertEqual(parsed["outer"][1]["ok"], "fine")

    def test_repeated_json_keys_never_silently_collapse_data(self):
        raw = '{"note": "first-value", "note": "second-value"}'
        with self.assertRaises(GuardError):
            context_guard.guard_text(raw, SyntheticRedactor())
        # Fragment is explicitly withheld; source itself remains unchanged.
        self.assertIn("first-value", raw)
        self.assertIn("second-value", raw)

    def test_embedded_json_pair_in_prose_redacted(self):
        raw = 'log {"db_password": "hunter2-live"} tail'
        out = context_guard.guard_text(raw, SyntheticRedactor())
        self.assertNotIn("hunter2-live", out)
        self.assertIn('"db_password": "[REDACTED]"', out)
        self.assertTrue(out.startswith("log "))
        self.assertTrue(out.endswith(" tail"))

    def test_escaped_json_pair_in_prose_redacted(self):
        raw = 'log {\\"api_key\\": \\"live-value-123\\"} tail'
        out = context_guard.guard_text(raw, SyntheticRedactor())
        self.assertNotIn("live-value-123", out)
        self.assertIn('[REDACTED]', out)

    def test_benign_json_returned_byte_identical(self):
        raw = '{  "path": "src/keys.py",\n  "id": "3f6b2a44-9c21-4c0a-8f7e-2c1f2a9d5e11",\n  "ok": true\n}'
        self.assertEqual(context_guard.guard_text(raw, SyntheticRedactor()), raw)

    def test_jsonl_lines_redacted_without_changing_line_count(self):
        raw = '{"password": "hunter2-secret"}\nplain api_key=secret-value line\nbenign'
        out = context_guard.guard_text(raw, SyntheticRedactor())
        lines = out.split("\n")
        self.assertEqual(len(lines), 3)
        self.assertNotIn("hunter2-secret", lines[0])
        self.assertIn("[REDACTED]", json.loads(lines[0])["password"])
        self.assertEqual(lines[1], "plain api_key=[REDACTED] line")
        self.assertEqual(lines[2], "benign")

    def test_deep_json_withholds_instead_of_raw_fallback(self):
        raw = '{"a":' * 40 + '"api_key=secret-value"' + "}" * 40
        with self.assertRaises(GuardError):
            context_guard.guard_text(raw, SyntheticRedactor())


class ParentRegressionTests(unittest.TestCase):
    def test_escaped_key_in_embedded_json(self):
        raw = 'public {"pa\\u0073sword":"private-value","note":"retained"} end'
        out = context_guard.guard_text(raw, SyntheticRedactor())
        self.assertNotIn("private-value", out)
        self.assertIn("retained", out)
        self.assertTrue(out.startswith("public "))
        self.assertTrue(out.endswith(" end"))

    def test_secret_key_collision_preserves_both_values_and_benign_key(self):
        first, second = "ghp_" + "a" * 32, "ghp_" + "b" * 32
        raw = json.dumps({first: {"password": "private-value"}, second: "second-value",
                          "[REDACTED]": "public-original"})
        out = context_guard.guard_text(raw, SyntheticRedactor())
        self.assertNotIn(first, out)
        self.assertNotIn(second, out)
        self.assertNotIn("private-value", out)
        result = json.loads(out)
        self.assertEqual(len(result), 3)
        self.assertEqual(result["[REDACTED]"], "public-original")
        self.assertIn("second-value", result.values())

    def test_duplicate_escaped_keys_withhold(self):
        raw = '{"pa\\u0073sword":"first","pa\\u0073sword":"second"}'
        with self.assertRaises(GuardError):
            context_guard.guard_text(raw, SyntheticRedactor())

    def test_deep_escaped_key_withholds_without_value_in_error(self):
        raw = '{"a":' * 40 + '{"pa\\u0073sword":"private-value"}' + '}' * 40
        with self.assertRaises(GuardError) as error:
            context_guard.guard_text(raw, SyntheticRedactor())
        self.assertNotIn("private-value", str(error.exception))

    def test_quoted_brackets_do_not_trip_depth_bound(self):
        raw = json.dumps({"note": "[" * 100 + "quote \"" + "}" * 100})
        self.assertEqual(context_guard.guard_text(raw, SyntheticRedactor()), raw)

    def test_embedded_json_parse_attempts_bounded(self):
        with self.assertRaises(GuardError):
            context_guard.guard_text("[not-json " * 600, SyntheticRedactor())


class PemFragmentTests(unittest.TestCase):
    def test_complete_pem_block_redacted(self):
        raw = ("before\n-----BEGIN RSA " "PRIVATE KEY-----\nMIIEvwIBADANBgkq\n"
               "-----END RSA PRIVATE KEY-----\nafter")
        out = context_guard.guard_text(raw, SyntheticRedactor())
        self.assertNotIn("PRIVATE KEY", out)
        self.assertNotIn("MIIEvwIBADANBgkq", out)
        self.assertIn("before", out)
        self.assertIn("after", out)

    def test_incomplete_pem_begin_redacted_without_swallowing_prose(self):
        raw = ("intro\n-----BEGIN " "PRIVATE KEY-----\nMIIEvwIBADANBgkq\n"
               "ABCD1234EFGH5678\ntrailing prose line\n")
        out = context_guard.guard_text(raw, SyntheticRedactor())
        self.assertNotIn("BEGIN", out)
        self.assertNotIn("MIIEvwIBADANBgkq", out)
        self.assertNotIn("ABCD1234EFGH5678", out)
        self.assertIn("trailing prose line", out)
        self.assertIn("intro", out)

    def test_orphan_pem_end_redacts_bounded_body_before_it(self):
        raw = "MIIEvwIBADANBgkq\nABCD1234\n-----END PRIVATE KEY-----\nafter\n"
        out = context_guard.guard_text(raw, SyntheticRedactor())
        self.assertNotIn("PRIVATE KEY", out)
        self.assertNotIn("MIIEvwIBADANBgkq", out)
        self.assertIn("after", out)


class Base64SecretTests(unittest.TestCase):
    def test_base64_encoded_recognizable_secret_redacted(self):
        token = b64("ghp_" + "a1b2c3d4" * 4)
        raw = "log line with blob " + token + " done"
        out = context_guard.guard_text(raw, SyntheticRedactor())
        self.assertNotIn(token, out)
        self.assertNotIn("ghp_", out)
        self.assertIn("log line with blob", out)
        self.assertIn("done", out)

    def test_base64_secret_inside_long_blob_redacted(self):
        token = b64("padding " * 400 + "sk-ant-" + "x" * 24 + " tail")
        raw = "blob " + token + " end"
        out = context_guard.guard_text(raw, SyntheticRedactor())
        self.assertNotIn(token, out)
        self.assertIn("blob ", out)
        self.assertIn(" end", out)

    def test_benign_base64_not_redacted_merely_for_being_encoded(self):
        token = b64("a benign note, definitely not a secret")
        raw = "value " + token + " tail"
        self.assertEqual(context_guard.guard_text(raw, SyntheticRedactor()), raw)

    def test_benign_long_alphanumeric_run_preserved(self):
        raw = "x" * 5000
        self.assertEqual(context_guard.guard_text(raw, SyntheticRedactor()), raw)


class FailClosedTests(unittest.TestCase):
    def test_oversize_input_raises_without_content(self):
        with self.assertRaises(GuardError) as ctx:
            context_guard.guard_text("a" * (context_guard.MAX_TEXT_BYTES + 1),
                                     SyntheticRedactor())
        self.assertNotIn("a" * 64, str(ctx.exception))

    def test_throwing_redactor_propagates_failure(self):
        with self.assertRaises(RuntimeError):
            context_guard.guard_text("api_key=secret-value", SyntheticRedactor(fail=True))

    def test_malformed_redactor_return_raises(self):
        with self.assertRaises(GuardError):
            context_guard.guard_text("benign", SyntheticRedactor(malformed=True))

    def test_missing_redactor_and_non_string_raise(self):
        with self.assertRaises(GuardError):
            context_guard.guard_text("benign", None)
        with self.assertRaises(GuardError):
            context_guard.guard_text(b"bytes", SyntheticRedactor())

    def test_b64_scan_bound_withholds(self):
        raw = " ".join(b64("benign note number %d" % i) for i in range(600))
        with self.assertRaises(GuardError):
            context_guard.guard_text(raw, SyntheticRedactor())


class BenignPreservationTests(unittest.TestCase):
    def test_unicode_uuid_and_paths_byte_identical(self):
        raw = ("Zażółć gęślą jaźń\n"
               "id 3f6b2a44-9c21-4c0a-8f7e-2c1f2a9d5e11\n"
               "path C:/repo/src/mod.py and /var/log/app.log\n"
               "run run-20261006T191711Z-13b14c9718\n")
        self.assertEqual(context_guard.guard_text(raw, SyntheticRedactor()), raw)

    def test_git_sha_and_short_tokens_preserved(self):
        raw = "commit 4f01e817dadf1f09e8dd32dcf763ed6c5bc7ce62 ok"
        self.assertEqual(context_guard.guard_text(raw, SyntheticRedactor()), raw)


class FinalValidatorTests(unittest.TestCase):
    def test_recognizable_leftovers_detected(self):
        self.assertTrue(context_guard.contains_recognizable_secret("ghp_" + "a" * 20))
        self.assertTrue(context_guard.contains_recognizable_secret("-----BEGIN " "PRIVATE KEY-----"))
        self.assertTrue(context_guard.contains_recognizable_secret(
            '{"password": "hunter2-live"}'))
        self.assertTrue(context_guard.contains_recognizable_secret(
            "blob " + b64("xai-" + "k" * 20)))

    def test_redacted_and_benign_pass(self):
        self.assertFalse(context_guard.contains_recognizable_secret(
            '{"password": "[REDACTED]", "note": "benign"}'))
        self.assertFalse(context_guard.contains_recognizable_secret(
            "id 3f6b2a44-9c21-4c0a-8f7e-2c1f2a9d5e11 path src/a.py"))
        self.assertFalse(context_guard.contains_recognizable_secret(""))
        self.assertTrue(context_guard.contains_recognizable_secret(None))
        self.assertTrue(context_guard.contains_recognizable_secret(
            "a" * (context_guard.MAX_TEXT_BYTES + 1)))


if __name__ == "__main__":
    unittest.main()
