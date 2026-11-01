"""Regression tests for caretaker 0.1.38:
- Sanitized stderr with canonical:<code_part>:<reason_part> bounded to <= 120 chars.
- Fleet maker tool denial and placeholder detection raising maker_tool_use_or_placeholder_prohibited.
- Robust redaction of user paths including spaces and subdirectories.
- Non-false-positive acceptance of legitimate body prose mentioning permission issues.
- Format error registration and targeted repair prompt.
- Failure classification for canonical prefixed errors.
"""
import json
import subprocess
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from caretaker.publication import PublicationError, is_format_error, repair_prompt
from caretaker.pubstate import classify_failure
from curate_once import (
    _decision_from_maker,
    _fleet_maker_rule,
    _format_canonical_error,
    command,
)


# The identities in these fixtures are fictional, and their user paths are never opened.
class CanonicalSanitization0138Tests(unittest.TestCase):
    def test_canonical_format_and_length_cap(self):
        long_reason = "A" * 300
        formatted = _format_canonical_error("test_code", long_reason)
        self.assertTrue(formatted.startswith("canonical:test_code:"))
        self.assertLessEqual(len(formatted), 120)
        self.assertTrue(formatted.isprintable())

    def test_user_paths_redacted_in_canonical_error(self):
        raw = r"Error reading file C:\Users\synthetic-fixture-owner-0138\AppData\Local\Temp\foo.txt: failed"
        formatted = _format_canonical_error("io_error", raw)
        self.assertNotIn(r"Users\synthetic-fixture-owner-0138", formatted)
        self.assertIn("[USER_PATH]", formatted)
        self.assertLessEqual(len(formatted), 120)

    def test_user_paths_with_spaces_and_subdirs_redacted(self):
        raw = r"Failed to open C:\Users\Synthetic Fixture User\Private\secret.txt: failed"
        formatted = _format_canonical_error("io", raw)
        self.assertNotIn("Synthetic Fixture User", formatted)
        self.assertNotIn("secret.txt", formatted)
        self.assertIn("[USER_PATH]", formatted)
        self.assertLessEqual(len(formatted), 120)

    def test_unix_user_paths_redacted(self):
        raw = "Error accessing /home/synthetic-fixture-developer/secret/path: permission denied"
        formatted = _format_canonical_error("access_denied", raw)
        self.assertNotIn("/home/synthetic-fixture-developer", formatted)
        self.assertIn("[USER_PATH]", formatted)

    def test_newlines_and_unprintable_chars_sanitized(self):
        raw = "line 1\r\n\tline 2\x00\x08line 3"
        formatted = _format_canonical_error("multiline", raw)
        self.assertNotIn("\n", formatted)
        self.assertNotIn("\r", formatted)
        self.assertNotIn("\x00", formatted)
        self.assertTrue(formatted.isprintable())
        self.assertLessEqual(len(formatted), 120)

    def test_command_stderr_json_sanitized_to_canonical(self):
        err = r"transaction rolled back: baseline_changed in C:\Users\synthetic-fixture-owner-0138\repo"
        completed = subprocess.CompletedProcess(
            ["curation.py", "publish"], 1, "", json.dumps({"status": "BLOCKED", "error": err})
        )
        with patch("curate_once.subprocess.run", return_value=completed):
            with self.assertRaises(PublicationError) as caught:
                command(["curation.py", "publish"])
        msg = str(caught.exception)
        self.assertTrue(msg.startswith("canonical:baseline_changed:"))
        self.assertNotIn(r"Users\synthetic-fixture-owner-0138", msg)
        self.assertLessEqual(len(msg), 120)
        self.assertEqual(classify_failure(msg), "transient")


    def test_code_never_leaks_path_from_colon_error(self):
        raw = "/home/synthetic-fixture-ann/secret: something went wrong"
        formatted = _format_canonical_error("/home/synthetic-fixture-ann/secret", raw)
        self.assertNotIn("synthetic-fixture-ann", formatted)
        self.assertIn("canonical:error:", formatted)

    def test_escaped_json_user_paths_redacted(self):
        raw = '{"msg": "C:\\\\Users\\\\Synthetic Fixture User\\\\Private\\\\secret.txt failed"}'
        formatted = _format_canonical_error("json_err", raw)
        self.assertNotIn("Synthetic Fixture User", formatted)
        self.assertNotIn("secret.txt", formatted)
        self.assertIn("[USER_PATH]", formatted)

    def test_user_paths_with_comma_in_username_redacted(self):
        raw = r"Failed to open C:\Users\Synthetic, Fixture User\Private\secret.txt: failed"
        formatted = _format_canonical_error("io_err", raw)
        self.assertNotIn("Synthetic", formatted)
        self.assertNotIn("Fixture User", formatted)
        self.assertNotIn("secret.txt", formatted)
        self.assertIn("[USER_PATH]", formatted)

    def test_escaped_json_with_comma_in_username_redacted(self):
        raw = '{"path": "C:\\\\Users\\\\Synthetic, Fixture User\\\\Private\\\\secret.txt"}'
        formatted = _format_canonical_error("json_err", raw)
        self.assertNotIn("Synthetic", formatted)
        self.assertNotIn("Fixture User", formatted)
        self.assertNotIn("secret.txt", formatted)
        self.assertIn("[USER_PATH]", formatted)

    def test_user_paths_with_apostrophe_in_username_redacted(self):
        raw = r"Failed to open C:\Users\Fixture'Person\Private\secret.txt: failed"
        formatted = _format_canonical_error("io_err", raw)
        self.assertNotIn("Person", formatted)
        self.assertNotIn("secret.txt", formatted)
        self.assertIn("[USER_PATH]", formatted)

class ToolUseAndPlaceholderRejectionTests(unittest.TestCase):
    def test_rejects_summary_equals_test(self):
        final_payload = {
            "status": "completed",
            "summary": "test",
            "files_changed": [],
            "verification": ["test"],
            "risks": [],
            "next_steps": [],
        }
        env = SimpleNamespace(final=json.dumps(final_payload))
        with self.assertRaises(PublicationError) as caught:
            _decision_from_maker(env)
        self.assertEqual(str(caught.exception), "maker_tool_use_or_placeholder_prohibited")
        self.assertTrue(is_format_error(str(caught.exception)))

    def test_rejects_dict_summary_with_test_placeholder(self):
        final_payload = {
            "status": "completed",
            "summary": {"body": "test", "summary": "test", "dispositions": {}},
            "files_changed": [],
            "verification": ["test"],
        }
        env = SimpleNamespace(final=json.dumps(final_payload))
        with self.assertRaises(PublicationError) as caught:
            _decision_from_maker(env)
        self.assertEqual(str(caught.exception), "maker_tool_use_or_placeholder_prohibited")

    def test_rejects_string_with_permission_denial(self):
        final_payload = {
            "status": "completed",
            "summary": 'Tool call failed: permission denied for Write tool',
            "files_changed": [],
        }
        env = SimpleNamespace(final=json.dumps(final_payload))
        with self.assertRaises(PublicationError) as caught:
            _decision_from_maker(env)
        self.assertEqual(str(caught.exception), "maker_tool_use_or_placeholder_prohibited")

    def test_accepts_clean_valid_decision(self):
        decision = {
            "body": "Page content with valid synthesis.",
            "summary": "Valid publication summary.",
            "dispositions": {},
        }
        env = SimpleNamespace(final=json.dumps({"summary": json.dumps(decision), "status": "completed"}))
        result = _decision_from_maker(env)
        self.assertEqual(result, decision)

    def test_accepts_valid_decision_with_permission_denied_in_body(self):
        decision = {
            "body": "Raport z audytu uprawnień: użytkownik otrzymał odmowę (Permission denied) przy próbie dostępu do panelu admina.",
            "summary": "Zaktualizowano stronę audytu uprawnień.",
            "dispositions": {},
        }
        env = SimpleNamespace(final=json.dumps({"summary": json.dumps(decision), "status": "completed"}))
        result = _decision_from_maker(env)
        self.assertEqual(result, decision)

    def test_fleet_maker_rule_explicitly_forbids_tools_and_test_placeholder(self):
        rule = _fleet_maker_rule()
        self.assertIn("DO NOT use tools", rule)
        self.assertIn("Write", rule)
        self.assertIn("Edit", rule)
        self.assertIn("test", rule)

    def test_repair_prompt_forbids_tools_on_placeholder_error(self):
        frozen = {
            "target": "wiki/test.md",
            "before": None,
            "sources": [],
            "proposals": [],
        }
        prompt = repair_prompt(frozen, "maker_tool_use_or_placeholder_prohibited")
        self.assertIn("DO NOT use tools", prompt)
        self.assertIn("placeholder", prompt)


class CanonicalClassification0138Tests(unittest.TestCase):
    def test_canonical_transient_failures(self):
        transient_cases = [
            "canonical:baseline_changed:transaction rolled back: baseline_changed",
            "canonical:curator_lock_held:curator_lock held by another agent",
            "canonical:transaction_rollback:lock_held",
            "canonical:accounted_elsewhere:proposals accounted elsewhere",
            "canonical:error:collision detected during commit",
        ]
        for c in transient_cases:
            with self.subTest(case=c):
                self.assertEqual(classify_failure(c), "transient")

    def test_canonical_deterministic_failures(self):
        deterministic_cases = [
            "canonical:exit_2:schema validation error",
            "canonical:error:syntax error near SELECT",
            "canonical:validation_failed:missing required field",
        ]
        for c in deterministic_cases:
            with self.subTest(case=c):
                self.assertEqual(classify_failure(c), "deterministic")
