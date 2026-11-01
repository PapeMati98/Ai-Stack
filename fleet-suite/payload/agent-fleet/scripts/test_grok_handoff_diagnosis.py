"""Synthetic regressions for Grok handoff diagnosis provenance."""

from __future__ import annotations

import json
import unittest

import fleet


def valid_handoff(summary: str = "Synthetic bounded result.") -> dict[str, object]:
    return {
        "status": "completed",
        "summary": summary,
        "files_changed": [],
        "verification": ["Ran a synthetic focused check."],
        "risks": [],
        "next_steps": [],
    }


def diagnose(
    envelope: dict[str, object],
    *,
    handoff_validator=None,
    provider_failure: str | None = None,
):
    source_text = json.dumps(envelope)
    projection = fleet.project_provider_handoff(
        "grok", source_text, handoff_validator=handoff_validator
    )
    diagnosis_text, raw_source = fleet.handoff_diagnosis_text(
        projection, source_text
    )
    error = fleet.handoff_semantic_error(
        projection["handoff"],
        mode="review",
        kind="review",
        source_text=diagnosis_text,
        provider_failure=provider_failure,
        raw_source=raw_source,
        no_candidate_detail=projection.get("no_candidate_detail"),
    )
    return projection, diagnosis_text, error


def ancillary_metadata() -> dict[str, object]:
    office_handoff = valid_handoff("DO_NOT_EXPOSE_OFFICE_HANDOFF")
    return {
        "office": {
            "id": "synthetic-office-id",
            "kind": "task.completed",
            "title": "DO_NOT_EXPOSE_OFFICE_TITLE",
            "status": "completed",
            "handoff": office_handoff,
        },
        "tool_events": [
            {
                "kind": "tool.result",
                "rawOutput": office_handoff,
                "title": "DO_NOT_EXPOSE_TOOL_TITLE",
            }
        ],
    }


class GrokHandoffDiagnosisTests(unittest.TestCase):
    def test_absent_or_non_string_assistant_text_does_not_parse_ancillary_data(self):
        cases = (
            ("missing", {}),
            ("empty", {"text": ""}),
            ("non-string", {"text": {"status": "completed"}}),
        )
        for label, assistant_fields in cases:
            with self.subTest(text_state=label):
                envelope = {
                    "stopReason": "end_turn",
                    **assistant_fields,
                    "_fleet": ancillary_metadata(),
                }

                projection, diagnosis_text, error = diagnose(envelope)

                self.assertIsNone(projection["handoff"])
                self.assertEqual(diagnosis_text, "")
                self.assertIn("Grok terminal handoff absent", str(error))
                self.assertNotIn("DO_NOT_EXPOSE", json.dumps(projection))
                self.assertNotIn("DO_NOT_EXPOSE", str(error))

    def test_ancillary_complete_handoff_never_counts_as_candidate(self):
        envelope = {
            "stopReason": "end_turn",
            "_fleet": ancillary_metadata(),
        }

        projection, diagnosis_text, error = diagnose(envelope)

        self.assertIsNone(projection["handoff"])
        self.assertEqual(diagnosis_text, "")
        self.assertIsNone(projection["source"])
        self.assertIn("Grok terminal handoff absent", str(error))
        self.assertNotIn("DO_NOT_EXPOSE", str(error))

    def test_malformed_assistant_json_and_invalid_structured_handoff_are_diagnosed(self):
        malformed = {
            "stopReason": "end_turn",
            "text": '{"status":"completed","summary":',
        }
        malformed_projection, malformed_text, malformed_error = diagnose(malformed)
        self.assertEqual(malformed_projection["candidate_text"], malformed["text"])
        self.assertEqual(malformed_text, malformed["text"])
        self.assertIn("MALFORMED JSON", str(malformed_error))

        invalid_structured = {
            "status": "completed",
            "summary": "Synthetic but missing required fields.",
        }
        structured_envelope = {
            "stopReason": "end_turn",
            "structuredOutput": invalid_structured,
        }
        projection, diagnosis_text, error = diagnose(structured_envelope)
        self.assertIsNone(projection["handoff"])
        self.assertEqual(diagnosis_text, json.dumps(invalid_structured, separators=(",", ":")))
        self.assertIn("HANDOFF CONTRACT MISMATCH", str(error))

    def test_empty_terminal_text_does_not_hide_invalid_structured_candidate(self):
        invalid = {"status": "completed", "summary": "Synthetic missing fields"}
        for empty in ("", "   "):
            with self.subTest(text=empty):
                projection, candidate, error = diagnose({
                    "stopReason": "end_turn",
                    "structuredOutput": invalid,
                    "text": empty,
                    "_fleet": ancillary_metadata(),
                })
                self.assertIsNone(projection["handoff"])
                self.assertEqual(json.loads(candidate), invalid)
                self.assertIn("HANDOFF CONTRACT MISMATCH", str(error))
                self.assertNotIn("DO_NOT_EXPOSE", str(error))

    def test_valid_structured_and_terminal_handoffs_keep_existing_precedence(self):
        structured = valid_handoff("Structured candidate wins.")
        terminal = valid_handoff("Terminal text candidate.")
        structured_projection, _, structured_error = diagnose(
            {
                "stopReason": "end_turn",
                "structuredOutput": structured,
                "text": json.dumps(terminal),
            }
        )
        self.assertEqual(structured_projection["handoff"], structured)
        self.assertEqual(structured_projection["source"], "grok.structuredOutput")
        self.assertIsNone(structured_error)

        terminal_projection, terminal_text, terminal_error = diagnose(
            {"stopReason": "end_turn", "text": json.dumps(terminal)}
        )
        self.assertEqual(terminal_projection["handoff"], terminal)
        self.assertEqual(terminal_projection["source"], "grok.terminalText")
        self.assertEqual(terminal_text, json.dumps(terminal))
        self.assertIsNone(terminal_error)

    def test_invalid_structured_candidate_falls_back_to_valid_terminal_text(self):
        terminal = valid_handoff("Accepted terminal fallback.")
        projection, diagnosis_text, error = diagnose(
            {
                "stopReason": "end_turn",
                "structuredOutput": {"status": "completed", "summary": "invalid"},
                "text": json.dumps(terminal),
            }
        )

        self.assertEqual(projection["handoff"], terminal)
        self.assertEqual(projection["source"], "grok.terminalText")
        self.assertEqual(diagnosis_text, json.dumps(terminal))
        self.assertIsNone(error)

    def test_optional_validator_error_and_outer_envelope_errors_remain_visible(self):
        candidate = valid_handoff()
        validator_error = "Synthetic optional validator rejection."
        projection, diagnosis_text, semantic_error = diagnose(
            {"stopReason": "end_turn", "structuredOutput": candidate},
            handoff_validator=lambda _: validator_error,
        )

        self.assertIsNone(projection["handoff"])
        self.assertEqual(projection["validation_error"], validator_error)
        self.assertEqual(projection["error"], validator_error)
        self.assertEqual(diagnosis_text, json.dumps(candidate, separators=(",", ":")))
        self.assertIsInstance(semantic_error, str)
        self.assertEqual(
            fleet.grok_continuation_reason(
                json.dumps({"stopReason": "end_turn", "structuredOutput": candidate}),
                projection,
                validation_error=projection["validation_error"],
            ),
            "invalid-handoff-contract",
        )

        raw = '{"stopReason":'
        malformed_projection = fleet.project_provider_handoff("grok", raw)
        self.assertIn("Invalid Grok JSON envelope", malformed_projection["error"])
        raw_diagnosis, raw_source = fleet.handoff_diagnosis_text(
            malformed_projection, raw
        )
        self.assertIsInstance(
            fleet.handoff_semantic_error(
                None,
                mode="review",
                kind="review",
                source_text=raw_diagnosis,
                raw_source=raw_source,
                no_candidate_detail=malformed_projection.get("no_candidate_detail"),
            ),
            str,
        )

    def test_cancelled_run_stays_partial_without_continuation_or_inferred_source(self):
        envelope = {
            "stopReason": "cancelled",
            "_fleet": {
                **ancillary_metadata(),
                "permission_requests_denied": 3,
                "permission_denials": [{"reason": "synthetic-denial"}],
            },
        }
        source_text = json.dumps(envelope)
        projection, _, error = diagnose(envelope)

        self.assertEqual(projection["terminal_reason"], "cancelled")
        self.assertEqual(
            fleet.provider_transport_outcome(
                "grok",
                exit_code=0,
                terminal_reason=projection["terminal_reason"],
                timed_out=False,
            ),
            {"status": "partial", "reason": "cancelled"},
        )
        self.assertIsNone(fleet.grok_continuation_reason(source_text, projection))
        self.assertIn("native cancellation source is unknown", str(error))
        self.assertNotIn("permission", str(error).casefold())
        self.assertNotIn("user stop", str(error).casefold())

    def test_provider_failure_remains_primary_with_no_assistant_handoff(self):
        provider_failure = "PROVIDER FAILURE: synthetic transport exit 9"
        projection, _, error = diagnose(
            {"stopReason": "end_turn", "_fleet": ancillary_metadata()},
            provider_failure=provider_failure,
        )

        self.assertIsNone(projection["handoff"])
        self.assertTrue(str(error).startswith(provider_failure))
        self.assertIn("Grok terminal handoff absent", str(error))
        self.assertNotIn("DO_NOT_EXPOSE", str(error))


if __name__ == "__main__":
    unittest.main()
