"""Regression tests for canonical errors and Fleet maker envelopes."""
import json
import subprocess
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from caretaker.publication import PublicationError
from caretaker.pubstate import classify_failure
from curate_once import _decision_from_maker, command


class CanonicalCommandTests(unittest.TestCase):
    def test_structured_stderr_error_is_preserved(self):
        reason = "transaction rolled back: baseline_changed"
        completed = subprocess.CompletedProcess(
            ["curation.py", "publish"], 2, "", json.dumps({"status": "BLOCKED", "error": reason})
        )
        with patch("curate_once.subprocess.run", return_value=completed):
            with self.assertRaises(PublicationError) as caught:
                command(["curation.py", "publish"])
        self.assertEqual(str(caught.exception), "canonical:baseline_changed:" + reason)
        self.assertEqual(classify_failure(str(caught.exception)), "transient")

    def test_single_line_fallback_and_opaque_multiline(self):
        for stderr, expected in (
            ("curator_lock_held\n", "canonical:curator_lock_held:curator_lock_held"),
            ('{"status":"BLOCKED"}', 'canonical:exit_2:{"status":"BLOCKED"}'),
            ("error line\nextra line\n", "canonical:exit_2:extra line"),
            ("x" * 201, "canonical:exit_2:" + ("x" * (120 - len("canonical:exit_2:")))),
        ):
            completed = subprocess.CompletedProcess(["curation.py"], 2, "", stderr)
            with self.subTest(stderr=stderr[:20]), patch("curate_once.subprocess.run", return_value=completed):
                with self.assertRaises(PublicationError) as caught:
                    command(["curation.py"])
                self.assertEqual(str(caught.exception), expected)
                self.assertLessEqual(len(str(caught.exception)), 120)

    def test_json_error_after_other_stderr_line(self):
        completed = subprocess.CompletedProcess(
            ["curation.py"], 2, "", 'diagnostic\n{"error":"curator_lock_held"}\n'
        )
        with patch("curate_once.subprocess.run", return_value=completed):
            with self.assertRaisesRegex(PublicationError, "^canonical:curator_lock_held:curator_lock_held$"):
                command(["curation.py"])


class MakerEnvelopeTests(unittest.TestCase):
    def test_dict_summary_is_decision(self):
        decision = {"body": "body", "dispositions": {}}
        envelope = SimpleNamespace(final=json.dumps({"summary": decision, "status": "completed"}))
        self.assertEqual(_decision_from_maker(envelope), decision)

    def test_crlf_fenced_string_summary_is_decision(self):
        decision = {"body": "body", "dispositions": {}}
        summary = "\r\n```json\r\n" + json.dumps(decision) + "\r\n```\r\n"
        envelope = SimpleNamespace(final=json.dumps({"summary": summary, "status": "completed"}))
        self.assertEqual(_decision_from_maker(envelope), decision)


class CurationCollisionTests(unittest.TestCase):
    def test_transient_collisions_and_deterministic_errors(self):
        for reason in (
            "curator_lock_held", "CURATOR_LOCK conflict", "baseline_changed",
            "transaction rolled back: concurrent writer", "lock_held",
            "proposals_accounted_elsewhere",
            "canonical:baseline_changed:transaction rolled back: baseline_changed",
            "canonical:curator_lock_held:curator_lock_held",
            "canonical:transaction_rollback:transaction rolled back",
        ):
            with self.subTest(reason=reason):
                self.assertEqual(classify_failure(reason), "transient")
        self.assertEqual(classify_failure("maker_response_not_json"), "deterministic")
        self.assertEqual(classify_failure("canonical:exit_2:syntax error"), "deterministic")
