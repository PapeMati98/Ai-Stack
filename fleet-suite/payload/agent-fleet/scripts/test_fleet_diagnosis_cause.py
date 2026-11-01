"""Fleet must report the cause it actually saw, not the nearest schema field.

Three defects measured 2026-09-21 sent the parent and the operators after a
broken handoff contract or a broken model while the provider had simply run out
of quota, tokens or capacity:

1. minimax run-20260921T173824Z-2f91ff0d52 (exit 4, terminal `failed`): the CLI
   result envelope - the CLI's own wrapper, carrying `error` - was judged
   against the handoff contract, so the run record said "files_changed: missing
   required field ... unexpected field(s): durationMs, error, model, runId,
   schemaVersion".
2. muse run-20260921T190540Z-8a5e4b9747 (exit 1): a Knowledge Hub file read
   delivered as `tool.result` was taken for the model's final answer, so the
   diagnosis quoted line-number prefixes ("2| \"schema_version\": 1 ...") as
   MALFORMED JSON while the real cause was HTTP 429 quota exhaustion.
3. an opencode/glm-5.3 reviewer run that finished correctly - exit 0, terminal
   `stop`, 434 s of real work - was rejected because one literal control
   character sat inside a JSON string: "the parser stopped at character 2003
   (Invalid control character at)".

Every fixture below is synthetic. Nothing is copied from a live run record.
"""

from __future__ import annotations

import json
import sys
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
        "verification": ["Ran the focused diagnostic test."],
        "risks": [],
        "next_steps": [],
    }


def failed_minimax_envelope() -> str:
    """Synthetic MiniMax exec.result envelope: terminal failure + `error`."""
    return json.dumps(
        {
            "schemaVersion": 1,
            "type": "exec.result",
            "status": "failed",
            "model": {"providerId": "minimax", "modelId": "MiniMax-M3"},
            "runId": "synthetic-run-id",
            "durationMs": 4321,
            "error": (
                "Token Plan limit reached: the account has no remaining token "
                "plan quota"
            ),
        }
    )


def muse_stream(*, tool_result_text: str | None, final_text: str | None) -> str:
    """Synthetic Muse JSONL stream: model config, a file read, optional answer."""
    events: list[dict[str, object]] = [
        {
            "record_type": "event",
            "payload_type": "run.model.configured",
            "payload": {
                "provider_id": "meta",
                "model_id": "muse-spark-1.3",
                "source": "startup",
            },
        }
    ]
    if tool_result_text is not None:
        events.append(
            {
                "record_type": "event",
                "payload_type": "tool.result",
                "payload": {
                    "tool_use_id": "read-1",
                    "text": tool_result_text,
                },
            }
        )
    if final_text is not None:
        events.append(
            {
                "record_type": "event",
                "payload_type": "run.terminal.completed",
                "payload": {
                    "run_stream": "synthetic-diagnosis-run",
                    "terminal": "completed",
                    "text": final_text,
                },
            }
        )
    return "\n".join(json.dumps(event) for event in events)


def handoff_text_with_literal_control_character(character: str = "\n") -> str:
    """A handoff that is correct except for one raw control character."""
    return (
        '{"status":"completed","summary":"Reviewed the bounded slice.'
        + character
        + 'Second line of the summary.","files_changed":[],'
        '"verification":["Ran the focused diagnostic test."],"risks":[],'
        '"next_steps":[]}'
    )


def opencode_stream(text: str) -> str:
    return "\n".join(
        [
            json.dumps({"type": "text", "part": {"text": text}}),
            json.dumps({"type": "step_finish", "part": {"reason": "stop"}}),
        ]
    )


class ProviderEnvelopeErrorDiagnosisTests(unittest.TestCase):
    """A failed CLI result envelope is a provider cause, not a schema defect."""

    def test_failed_envelope_is_classified_as_a_provider_error(self) -> None:
        text = failed_minimax_envelope()
        self.assertEqual(
            fleet.classify_handoff_rejection(text)[0], "provider-error"
        )

    def test_failed_envelope_reports_the_provider_cause_not_schema_fields(self) -> None:
        text = failed_minimax_envelope()
        projection = fleet.project_provider_handoff("minimax", text)
        provider_failure = fleet.provider_failure_reason(
            stdout=text, stderr="", exit_code=4
        )
        assert provider_failure is not None

        self.assertIsNone(projection["handoff"])
        self.assertEqual(projection["terminal_reason"], "failed")

        message = fleet.handoff_semantic_error(
            None,
            mode="review",
            kind="review",
            source_text=text,
            provider_failure=provider_failure,
            raw_source=True,
        )
        self.assertIsInstance(message, str)
        self.assertIn("Token Plan limit reached", message)
        self.assertIn("exited with code 4", message)
        self.assertNotIn("HANDOFF CONTRACT MISMATCH", message)
        self.assertNotIn("missing required field", message)
        self.assertNotIn("unexpected field(s)", message)

    def test_terminal_provider_failure_leads_over_a_handoff_complaint(self) -> None:
        provider_failure = fleet.provider_failure_reason(
            stdout='{"status":"completed"}', stderr="", exit_code=1
        )
        assert provider_failure is not None
        message = fleet.diagnose_handoff_rejection(
            json.dumps({"status": "completed"}),
            provider_failure=provider_failure,
        )
        self.assertTrue(message.startswith("PROVIDER FAILURE"))
        self.assertNotIn("HANDOFF CONTRACT MISMATCH", message.split("Handoff")[0])

    def test_a_real_handoff_is_never_read_as_a_provider_envelope(self) -> None:
        self.assertIsNone(fleet.provider_error_envelope_detail(valid_handoff()))
        self.assertIsNone(fleet.provider_error_envelope_detail(None))
        self.assertIsNone(fleet.provider_error_envelope_detail({"status": "partial"}))
        self.assertEqual(
            fleet.classify_handoff_rejection(
                json.dumps({"status": "completed", "verification": []})
            )[0],
            "contract-mismatch",
        )

    def test_valid_handoff_is_still_accepted(self) -> None:
        handoff = valid_handoff()
        projection = fleet.project_provider_handoff(
            "minimax",
            json.dumps(
                {
                    "type": "exec.result",
                    "status": "succeeded",
                    "output": json.dumps(handoff),
                }
            ),
        )
        self.assertEqual(projection["handoff"], handoff)
        self.assertIsNone(projection["error"])


class ToolResultIsNotTheAnswerTests(unittest.TestCase):
    """A tool result never competes with the model's final answer."""

    def test_tool_result_is_not_taken_for_the_terminal_answer(self) -> None:
        stored = valid_handoff()
        stored["summary"] = "Stored record the model read from the Knowledge Hub."
        stream = muse_stream(tool_result_text=json.dumps(stored), final_text=None)

        projection = fleet.project_provider_handoff("muse", stream)

        self.assertIsNone(projection["handoff"])
        self.assertIsNotNone(projection["error"])
        self.assertNotIn("MALFORMED JSON", str(projection["error"]))
        # A tool result is not the run's terminal outcome either.
        self.assertNotEqual(projection["terminal_reason"], "completed")

    def test_numbered_file_read_is_not_taken_for_the_terminal_answer(self) -> None:
        numbered = "\n".join(
            [
                "1| {",
                '2|   "schema_version": 1,',
                '3|   "vault_path": "D:/synthetic/knowledge-hub"',
                "4| }",
            ]
        )
        projection = fleet.project_provider_handoff(
            "muse", muse_stream(tool_result_text=numbered, final_text=None)
        )
        self.assertIsNone(projection["handoff"])
        self.assertIsNotNone(projection["error"])

    def test_final_answer_after_a_tool_result_is_still_accepted(self) -> None:
        stored = valid_handoff()
        stored["summary"] = "Stored record the model read from the Knowledge Hub."
        final = valid_handoff()
        final["summary"] = "The model's own answer after reading the file."
        stream = muse_stream(
            tool_result_text=json.dumps(stored), final_text=json.dumps(final)
        )

        projection = fleet.project_provider_handoff("muse", stream)

        self.assertEqual(projection["handoff"], final)
        self.assertEqual(projection["terminal_reason"], "completed")
        self.assertIsNone(projection["error"])

    def test_tool_output_detection_is_bounded(self) -> None:
        self.assertTrue(fleet.event_is_tool_output("tool.result", {"text": "x"}))
        self.assertTrue(fleet.event_is_tool_output("TOOL_RESULT", {"text": "x"}))
        self.assertTrue(
            fleet.event_is_tool_output("task.lifecycle.status", {"tool_use_id": "r1"})
        )
        self.assertFalse(
            fleet.event_is_tool_output(
                "run.terminal.completed", {"terminal": "completed", "text": "x"}
            )
        )
        self.assertFalse(
            fleet.event_is_tool_output(
                "run.model.configured", {"provider_id": "meta"}
            )
        )


class LiteralControlCharacterRecoveryTests(unittest.TestCase):
    """One raw control character must not throw away a finished run."""

    def test_literal_control_character_is_repaired_and_recorded(self) -> None:
        broken = handoff_text_with_literal_control_character()
        with self.assertRaises(json.JSONDecodeError) as caught:
            json.loads(broken)
        self.assertIn("Invalid control character", str(caught.exception))

        handoff, recovery = fleet.parse_handoff_with_metadata(broken)

        self.assertIsNotNone(handoff)
        self.assertEqual(recovery, "literal-control-character")
        self.assertEqual(
            handoff["summary"],
            "Reviewed the bounded slice.\nSecond line of the summary.",
        )
        self.assertEqual(fleet.validate_handoff(handoff), handoff)

    def test_opencode_run_with_a_control_character_is_accepted(self) -> None:
        projection = fleet.project_provider_handoff(
            "opencode", opencode_stream(handoff_text_with_literal_control_character())
        )
        self.assertIsNotNone(projection["handoff"])
        self.assertIsNone(projection["error"])
        self.assertEqual(projection["terminal_reason"], "stop")
        self.assertIn("literal-control-character", str(projection["source"]))

    def test_repair_escapes_every_control_character_inside_a_string(self) -> None:
        for character, escaped in (
            ("\n", "\\n"),
            ("\r", "\\r"),
            ("\t", "\\t"),
            ("\x00", "\\u0000"),
            ("\x1f", "\\u001f"),
        ):
            with self.subTest(character=repr(character)):
                repaired = fleet.repair_invalid_json_control_characters(
                    '{"summary":"a' + character + 'b"}'
                )
                self.assertEqual(repaired, '{"summary":"a' + escaped + 'b"}')
                self.assertEqual(json.loads(repaired), {"summary": "a" + character + "b"})

    def test_repair_leaves_text_outside_strings_alone(self) -> None:
        text = '{\n"status":"completed"\n}'
        self.assertEqual(fleet.repair_invalid_json_control_characters(text), text)

    def test_repair_does_not_accept_a_candidate_that_is_still_invalid(self) -> None:
        self.assertIsNone(
            fleet.parse_handoff_with_metadata(
                '{"status":"completed","summary":"only two fields"}\n'
            )[0]
        )

    def test_backslash_repair_still_records_its_own_tag(self) -> None:
        # `\d` is not a JSON escape, so the candidate only parses after the
        # existing invalid-backslash-escape repair - which must keep its tag.
        broken = (
            '{"status":"completed","summary":"path C:\\documents\\report.txt",'
            '"files_changed":[],"verification":["ran"],"risks":[],'
            '"next_steps":[]}'
        )
        with self.assertRaises(json.JSONDecodeError):
            json.loads(broken)
        handoff, recovery = fleet.parse_handoff_with_metadata(broken)
        self.assertIsNotNone(handoff)
        self.assertEqual(recovery, "invalid-backslash-escape")

    def test_unbroken_handoff_keeps_no_recovery_tag(self) -> None:
        handoff, recovery = fleet.parse_handoff_with_metadata(
            json.dumps(valid_handoff())
        )
        self.assertEqual(handoff, valid_handoff())
        self.assertIsNone(recovery)


if __name__ == "__main__":
    unittest.main()
