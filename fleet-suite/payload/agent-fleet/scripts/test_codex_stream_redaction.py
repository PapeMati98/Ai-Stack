"""Codex stream redaction, damaged-line tolerance, and codex-final fallback.

Measured 2026-09-24: a correct Codex handoff identical to codex-final.json was
rejected as MALFORMED JSON after Fleet redaction broke a command_execution JSONL
line that carried synthetic secret-looking test code, and the Codex parser then
stopped at that first invalid line and threw away the final agent_message.

All secret-like values below are assembled from fragments at runtime so no
literal secret pattern lives in this file.
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path


SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import fleet  # noqa: E402


def valid_handoff() -> dict[str, object]:
    return {
        "status": "completed",
        "summary": "Reviewed the bounded slice and reported the findings.",
        "files_changed": [],
        "verification": ["Ran the focused stream redaction test."],
        "risks": [],
        "next_steps": [],
    }


def synthetic_secret() -> str:
    """A secret-looking value built from fragments, never stored literally."""
    return "".join(("syn", "thetic", "-", "k" * 24))


def quoted_key_name() -> str:
    return "api" + "_" + "key"


def header_key_name() -> str:
    return "authori" + "zation"


def header_tool_code() -> str:
    """Tool code whose JSONL form puts a secret in escaped quotes.

    ``json.dumps`` of this snippet yields ``\\"authorization\\": \\"Bearer ...\\"``
    inside aggregated_output. The header pattern used to consume the backslash
    of the closing escaped quote and leave a raw quote that closed the JSONL
    string early (Expecting ',' delimiter).
    """
    secret = synthetic_secret()
    return (
        '{"' + header_key_name() + '": "Bearer ' + secret + '"}'
    )


def quoted_secret_tool_code() -> str:
    secret = synthetic_secret()
    return f'assert {quoted_key_name()}="{secret}" == expected'


def assistant(value: str) -> dict[str, object]:
    return {"type": "item.completed", "item": {"type": "agent_message", "text": value}}


def command_execution(output: str) -> dict[str, object]:
    return {
        "type": "item.completed",
        "item": {"type": "command_execution", "aggregated_output": output},
    }


def stream(*events: object) -> str:
    return "\n".join(
        event if isinstance(event, str) else json.dumps(event) for event in events
    )


def stream_with_secret_and_final() -> tuple[str, str, dict[str, object]]:
    secret = synthetic_secret()
    tool_code = header_tool_code() + "\n" + quoted_secret_tool_code()
    final = valid_handoff()
    events = [
        {"type": "turn.started"},
        command_execution(tool_code),
        assistant(json.dumps(final)),
        {"type": "turn.completed"},
    ]
    return "\n".join(json.dumps(event) for event in events), secret, final


class JsonlRedactionKeepsJsonValidTests(unittest.TestCase):
    def test_command_execution_secret_is_hidden_and_lines_stay_json(self) -> None:
        raw, secret, final = stream_with_secret_and_final()
        self.assertIn(secret, raw)

        redacted = fleet.redact_sensitive_jsonl(raw)

        self.assertNotIn(secret, redacted)
        self.assertIn("[REDACTED]", redacted)
        for line in redacted.splitlines():
            if line.strip():
                json.loads(line)
        projection = fleet.project_provider_handoff("codex", redacted)
        self.assertEqual(projection["handoff"], final)
        self.assertIsNone(projection["error"])

    def test_raw_redaction_would_break_the_same_line(self) -> None:
        # Pins the regression shape: raw-text redaction on the JSONL line eats
        # the escaped quote next to the secret and leaves invalid JSON.
        secret = synthetic_secret()
        raw_line = json.dumps(command_execution(header_tool_code()))
        self.assertIn(secret, raw_line)
        self.assertIn('\\"', raw_line)

        broken = fleet.redact_sensitive_output(raw_line)

        self.assertNotIn(secret, broken)
        with self.assertRaises(json.JSONDecodeError):
            json.loads(broken)

        fixed = fleet.redact_sensitive_jsonl(raw_line)
        self.assertNotIn(secret, fixed)
        json.loads(fixed)

    def test_unmatched_json_line_is_returned_byte_identical(self) -> None:
        line = json.dumps(
            {
                "type": "item.completed",
                "item": {"type": "agent_message", "text": "plain text only"},
            }
        )
        self.assertEqual(fleet.redact_sensitive_jsonl(line), line)

    def test_non_json_falls_back_to_raw_redaction(self) -> None:
        secret = synthetic_secret()
        raw = f"{quoted_key_name()}={secret}\n"
        redacted = fleet.redact_sensitive_jsonl(raw)
        self.assertNotIn(secret, redacted)
        self.assertIn("[REDACTED]", redacted)

    def test_whole_json_envelope_is_redacted_structurally(self) -> None:
        secret = synthetic_secret()
        envelope = json.dumps({"output": header_tool_code()})
        redacted = fleet.redact_sensitive_jsonl(envelope)
        self.assertNotIn(secret, redacted)
        json.loads(redacted)


class DamagedStreamLineTests(unittest.TestCase):
    def test_one_damaged_line_keeps_final_handoff_with_warning(self) -> None:
        final = valid_handoff()
        text = stream(
            {"type": "turn.started"},
            "this line is not json",
            assistant(json.dumps(final)),
            {"type": "turn.completed"},
        )

        projection = fleet.project_provider_handoff("codex", text)

        self.assertEqual(projection["handoff"], final)
        self.assertIsNone(projection["error"])
        warnings = projection["stream_event_errors"]
        self.assertEqual(len(warnings), 1)
        self.assertEqual(warnings[0]["code"], "STREAM_EVENT_INVALID")
        self.assertEqual(warnings[0]["line"], 2)

    def test_damaged_command_execution_line_does_not_hide_later_handoff(self) -> None:
        final = valid_handoff()
        smashed = json.dumps(command_execution(header_tool_code()))
        # Force the historical corruption shape on one middle line: drop the
        # backslash of the closing escaped quote so the line stops being JSON.
        corrupted = smashed.replace('\\"}', '"}', 1)
        self.assertNotEqual(corrupted, smashed)
        with self.assertRaises(json.JSONDecodeError):
            json.loads(corrupted)
        text = stream(
            {"type": "turn.started"},
            corrupted,
            assistant(json.dumps(final)),
            {"type": "turn.completed"},
        )

        projection = fleet.project_provider_handoff("codex", text)

        self.assertEqual(projection["handoff"], final)
        self.assertIsNone(projection["error"])
        self.assertTrue(projection["stream_event_errors"])
        self.assertEqual(projection["stream_event_errors"][0]["line"], 2)

    def test_tool_output_still_never_replaces_final_answer(self) -> None:
        old = valid_handoff()
        old["summary"] = "Old report stored in tool output only."
        final = valid_handoff()
        text = stream(
            {"type": "turn.started"},
            "not json",
            command_execution(json.dumps(old)),
            assistant(json.dumps(final)),
            {"type": "turn.completed"},
        )

        projection = fleet.project_provider_handoff("codex", text)

        self.assertEqual(projection["handoff"], final)
        self.assertNotEqual(projection["handoff"], old)


class CodexFinalFallbackTests(unittest.TestCase):
    def test_missing_stream_handoff_falls_back_to_output_last_message(self) -> None:
        final = valid_handoff()
        text = stream(
            {"type": "turn.started"},
            {"type": "turn.completed"},
        )

        projection = fleet.project_provider_handoff(
            "codex", text, final_message_text=json.dumps(final)
        )

        self.assertEqual(projection["handoff"], final)
        self.assertIsNone(projection["error"])
        self.assertEqual(projection["source"], "codex.output-last-message")

    def test_stream_handoff_wins_over_output_last_message(self) -> None:
        from_stream = valid_handoff()
        from_stream["summary"] = "Stream final answer wins."
        from_file = valid_handoff()
        from_file["summary"] = "Output last message must not replace it."
        text = stream(
            {"type": "turn.started"},
            assistant(json.dumps(from_stream)),
            {"type": "turn.completed"},
        )

        projection = fleet.project_provider_handoff(
            "codex", text, final_message_text=json.dumps(from_file)
        )

        self.assertEqual(projection["handoff"], from_stream)
        self.assertEqual(
            projection["source"], "codex.item.completed.agent_message"
        )

    def test_tool_output_still_does_not_replace_final_answer_from_file(self) -> None:
        stored = valid_handoff()
        stored["summary"] = "Stored tool report only."
        final = valid_handoff()
        final["summary"] = "Explicit output last message."
        text = stream(
            {"type": "turn.started"},
            command_execution(json.dumps(stored)),
            {"type": "turn.completed"},
        )

        projection = fleet.project_provider_handoff(
            "codex", text, final_message_text=json.dumps(final)
        )

        self.assertEqual(projection["handoff"], final)
        self.assertEqual(projection["source"], "codex.output-last-message")

    def test_unusable_output_last_message_does_not_invent_a_handoff(self) -> None:
        text = stream({"type": "turn.started"}, {"type": "turn.completed"})
        projection = fleet.project_provider_handoff(
            "codex", text, final_message_text="not a handoff"
        )
        self.assertIsNone(projection["handoff"])
        self.assertIsNotNone(projection["error"])


class StreamEventDiagnosisTests(unittest.TestCase):
    def test_damaged_line_diagnosis_is_stream_event_invalid(self) -> None:
        tool_text = '{"status":"completed","summary":"broken tool fragment"'
        text = stream(
            {"type": "turn.started"},
            tool_text + " not closed",
            {"type": "turn.completed"},
        )

        projection = fleet.project_provider_handoff("codex", text)

        self.assertIsNone(projection["handoff"])
        warnings = projection["stream_event_errors"]
        self.assertEqual(len(warnings), 1)
        message = fleet.diagnose_handoff_rejection(
            text,
            raw_transcript=True,
            stream_event_errors=warnings,
        )
        self.assertIn("STREAM_EVENT_INVALID", message)
        self.assertIn("line 2", message)
        self.assertNotIn("MALFORMED JSON", message)

    def test_semantic_error_names_stream_line_not_tool_text(self) -> None:
        tool_text = '{"status":"completed","extra" "value"}'
        text = stream(
            {"type": "turn.started"},
            "{ this is not json",
            command_execution(tool_text),
            {"type": "turn.completed"},
        )

        projection = fleet.project_provider_handoff("codex", text)

        self.assertIsNone(projection["handoff"])
        message = fleet.handoff_semantic_error(
            projection["handoff"],
            mode="review",
            kind="review",
            source_text=text,
            raw_source=True,
            stream_event_errors=projection.get("stream_event_errors"),
        )
        self.assertIsInstance(message, str)
        self.assertIn("STREAM_EVENT_INVALID", message)
        self.assertIn("line 2", message)
        self.assertNotIn("MALFORMED JSON", message)

    def test_projection_error_is_stream_event_invalid_not_malformed_json(self) -> None:
        tool_text = '{"status":"completed","extra" "value"}'
        text = stream(
            {"type": "turn.started"},
            "plain garbage line",
            command_execution(tool_text),
            {"type": "turn.completed"},
        )

        projection = fleet.project_provider_handoff("codex", text)

        self.assertIsNone(projection["handoff"])
        self.assertIn("STREAM_EVENT_INVALID", str(projection["error"]))
        self.assertNotIn("MALFORMED JSON", str(projection["error"]))

    def test_recovered_handoff_with_warning_is_not_a_rejection(self) -> None:
        final = valid_handoff()
        text = stream(
            {"type": "turn.started"},
            "not json",
            assistant(json.dumps(final)),
            {"type": "turn.completed"},
        )
        projection = fleet.project_provider_handoff("codex", text)
        self.assertEqual(projection["handoff"], final)
        self.assertIsNone(
            fleet.handoff_semantic_error(
                projection["handoff"],
                mode="review",
                kind="review",
                stream_event_errors=projection.get("stream_event_errors"),
            )
        )

    def test_without_stream_errors_malformed_candidate_still_says_malformed(self) -> None:
        broken = '{"status":"completed","extra" "value"}'
        message = fleet.diagnose_handoff_rejection(broken)
        self.assertIn("MALFORMED JSON", message)
        self.assertNotIn("STREAM_EVENT_INVALID", message)


class LegacyReceiptProjectionTests(unittest.TestCase):
    """Reparse receipts written before stream_event_errors existed still validate.

    Feedback 2026-09-24: strict equality of the recomputed projection against
    the stored receipt projection broke every legacy receipt, because the new
    diagnostic key was absent from those files.
    """

    def setUp(self) -> None:
        # mkdtemp without cleanup: this suite must not delete files or trees.
        self.root = Path(tempfile.mkdtemp(prefix="fleet-legacy-receipt-"))
        self.run_id = "run-20260913T185734Z-541679f4e0"
        self.out = self.root / "output" / self.run_id
        self.out.mkdir(parents=True)
        self.stdout = self.out / "attempt-1.stdout.txt"
        self.final = valid_handoff()
        self.stdout.write_text(
            stream(
                {"type": "turn.started"},
                assistant(json.dumps(self.final)),
                {"type": "turn.completed"},
            ),
            encoding="utf-8",
        )
        self.record = {
            "run_id": self.run_id,
            "adapter": "codex",
            "run_status": "COMPLETED",
            "completed_at": "2026-09-13T19:02:29Z",
            "exit_code": 0,
            "timed_out": False,
            "output_dir": str(self.out),
            "runtime_attestation": {
                "status": "VERIFIED",
                "package_version": "0.8.67",
            },
            "model": "gpt-6-astra",
            "model_identity": {"status": "not-applicable"},
            "provider_attempts": [
                {
                    "stdout_path": str(self.stdout),
                    "exit_code": 0,
                    "timed_out": False,
                }
            ],
            "handoff": valid_handoff(),
            "evaluation": {"decision": "not-accepted"},
        }
        (self.root / "runs").mkdir()
        self.record_path = self.root / "runs" / (self.run_id + ".json")
        self.record_path.write_text(json.dumps(self.record), encoding="utf-8")
        self.result = fleet.reparse_run_handoff(
            self.root,
            run_id=self.run_id,
            expected_record_sha256=fleet.sha256_bytes(self.record_path.read_bytes()),
            expected_stdout_sha256=fleet.sha256_bytes(self.stdout.read_bytes()),
        )
        self.receipt_path = Path(self.result["receipt_path"])
        self.active_path = Path(self.result["effective_record_path"])

    def rewrite_receipt_projection(self, mutate) -> None:
        receipt = json.loads(self.receipt_path.read_text(encoding="utf-8"))
        mutate(receipt["projection"])
        payload = (
            json.dumps(receipt, sort_keys=True, ensure_ascii=True, indent=2) + "\n"
        ).encode("utf-8")
        self.receipt_path.write_bytes(payload)
        projected = json.loads(self.active_path.read_text(encoding="utf-8"))
        projected["_handoff_reparse"]["receipt_sha256"] = fleet.sha256_bytes(payload)
        self.active_path.write_bytes(fleet.canonical_json_bytes(projected))

    def test_stored_projection_without_stream_event_errors_still_validates(self) -> None:
        receipt = json.loads(self.receipt_path.read_text(encoding="utf-8"))
        self.assertIn("stream_event_errors", receipt["projection"])
        self.rewrite_receipt_projection(
            lambda projection: projection.pop("stream_event_errors")
        )

        loaded, path = fleet.load_run_record(self.root, self.run_id)

        self.assertEqual(loaded["handoff"], self.final)
        self.assertEqual(path, self.active_path)

    def test_stored_projection_with_wrong_handoff_still_rejected(self) -> None:
        def mutate(projection: dict) -> None:
            projection.pop("stream_event_errors", None)
            projection["handoff"] = valid_handoff()
            projection["handoff"]["summary"] = "Forged summary"

        self.rewrite_receipt_projection(mutate)
        with self.assertRaises(fleet.FleetError):
            fleet.load_run_record(self.root, self.run_id)

    def test_stored_projection_with_wrong_source_still_rejected(self) -> None:
        def mutate(projection: dict) -> None:
            projection.pop("stream_event_errors", None)
            projection["source"] = "forged.source"

        self.rewrite_receipt_projection(mutate)
        with self.assertRaises(fleet.FleetError):
            fleet.load_run_record(self.root, self.run_id)


if __name__ == "__main__":
    unittest.main(verbosity=2)
