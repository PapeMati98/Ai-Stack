from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path


SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import fleet  # noqa: E402


# Synthetic, non-functional placeholder: only used to prove the excerpt is
# pushed through the existing redaction before it reaches any message.
FAKE_TOKEN = "sk-fake01234567890abcdef"


def valid_handoff() -> dict[str, object]:
    return {
        "status": "completed",
        "summary": "Reviewed the bounded slice and reported the findings.",
        "files_changed": [],
        "verification": ["Ran the focused diagnostic test."],
        "risks": [],
        "next_steps": [],
    }


def handoff_schema() -> dict[str, object]:
    return {
        "type": "object",
        "additionalProperties": False,
        "required": [
            "status",
            "summary",
            "files_changed",
            "verification",
            "risks",
            "next_steps",
        ],
        "properties": {
            "status": {
                "type": "string",
                "enum": ["completed", "partial", "blocked", "failed"],
            },
            "summary": {"type": "string", "minLength": 1, "maxLength": 16000},
        },
    }


def broken_handoff_text() -> str:
    """Model answer that starts and ends well but breaks in the middle."""
    head = (
        '{"status":"completed","summary":"REWIZJA 4 - poprawki wdrozeniowe",'
        '"files_changed":[],"verification":["sprawdzono logi wdrozenia"],'
        '"risks":["widoczny token dostepu ' + FAKE_TOKEN + '"],"next_steps":[]'
    )
    return head + ',"extra" "value"}'


def codebuddy_envelope(result_text: str) -> str:
    return json.dumps(
        [{"type": "result", "subtype": "success", "result": result_text}]
    )


def prompt_echo() -> str:
    """The handoff schema the worker prompt hands to the model.

    A raw provider transcript echoes it back, so a schema found there says
    nothing about the answer the model produced.
    """
    return (
        "Return exactly one JSON object matching this handoff schema: "
        + json.dumps(handoff_schema())
        + " and nothing else."
    )


def codebuddy_transcript(result_text: str | None) -> str:
    """Raw transcript: the echoed prompt plus, optionally, the model's result."""
    events: list[dict[str, object]] = [
        {"type": "user", "message": {"role": "user", "content": prompt_echo()}}
    ]
    if result_text is not None:
        events.append({"type": "result", "subtype": "success", "result": result_text})
    return json.dumps(events)


def broken_result_text() -> str:
    """Model answer that starts and ends well but breaks in the middle.

    Deliberately free of anything secret-looking so a parser position measured
    on the fixture is the same one measured on the redacted candidate.
    """
    return (
        '{"status":"completed","summary":"REWIZJA 4 - poprawki wdrozeniowe",'
        '"files_changed":[],"verification":["sprawdzono logi wdrozenia"],'
        '"risks":[],"next_steps":[]'
        ',"extra" "value"}'
    )


def minimax_envelope(output: str) -> str:
    return json.dumps(
        {"type": "exec.result", "status": "succeeded", "output": output}
    )


def no_candidate_text() -> str:
    return "Analiza zakonczona, brak obiektu JSON w odpowiedzi dostawcy."


def contract_text_missing_field() -> str:
    value = valid_handoff()
    value.pop("summary")
    return json.dumps(value)


def contract_text_too_long() -> str:
    value = valid_handoff()
    value["summary"] = "x" * 17000
    return json.dumps(value)


class HandoffRejectionDiagnosisTests(unittest.TestCase):
    def test_no_candidate_is_reported_as_no_candidate(self) -> None:
        text = no_candidate_text()
        category, _ = fleet.classify_handoff_rejection(text)
        self.assertEqual(category, "no-candidate")
        message = fleet.diagnose_handoff_rejection(text)
        self.assertIn("NO HANDOFF CANDIDATE", message)
        self.assertIn(str(len(text)), message)

    def test_empty_output_is_reported_as_no_candidate(self) -> None:
        self.assertEqual(fleet.classify_handoff_rejection("")[0], "no-candidate")
        self.assertEqual(fleet.classify_handoff_rejection("   \n")[0], "no-candidate")

    def test_malformed_json_reports_position_and_excerpt(self) -> None:
        broken = broken_handoff_text()
        try:
            json.loads(broken)
        except json.JSONDecodeError as exc:
            expected_position = exc.pos
            expected_reason = exc.msg
        else:  # pragma: no cover - the fixture is deliberately broken
            self.fail("fixture must not parse")

        category, _ = fleet.classify_handoff_rejection(broken)
        self.assertEqual(category, "malformed-json")
        message = fleet.diagnose_handoff_rejection(broken)
        self.assertIn("MALFORMED JSON", message)
        self.assertIn(f"at character {expected_position}", message)
        self.assertIn(expected_reason, message)
        self.assertIn("worktree", message)

    def test_malformed_json_inside_envelope_reports_position_and_excerpt(self) -> None:
        broken = broken_handoff_text()
        envelope = codebuddy_envelope(broken)
        category, _ = fleet.classify_handoff_rejection(envelope)
        self.assertEqual(category, "malformed-json")
        message = fleet.diagnose_handoff_rejection(envelope)
        self.assertIn("MALFORMED JSON", message)
        self.assertIn("Expecting ':' delimiter", message)

    def test_malformed_json_excerpt_is_redacted(self) -> None:
        broken = broken_handoff_text()
        self.assertIn(FAKE_TOKEN, broken)
        message = fleet.diagnose_handoff_rejection(broken)
        self.assertNotIn(FAKE_TOKEN, message)
        self.assertIn("[REDACTED]", message)
        self.assertEqual(fleet.redacted_handoff_excerpt("", 0), "")

    def test_excerpt_is_short_and_bounded(self) -> None:
        broken = broken_handoff_text()
        excerpt = fleet.redacted_handoff_excerpt(broken, len(broken) - 10)
        self.assertLessEqual(len(excerpt), fleet.HANDOFF_DIAGNOSIS_EXCERPT_CHARS + 32)
        message = fleet.diagnose_handoff_rejection(broken)
        self.assertLess(len(message), 1200)

    def test_schema_instead_of_data_is_reported_as_schema(self) -> None:
        schema = handoff_schema()
        text = minimax_envelope(json.dumps(schema))
        category, _ = fleet.classify_handoff_rejection(text)
        self.assertEqual(category, "schema-not-data")
        message = fleet.diagnose_handoff_rejection(text)
        self.assertIn("SCHEMA RETURNED INSTEAD OF DATA", message)
        self.assertIn("properties", message)
        self.assertIn("summary", message)

    def test_schema_definition_detection_does_not_fire_on_instances(self) -> None:
        self.assertTrue(fleet.handoff_value_is_schema_definition(handoff_schema()))
        self.assertFalse(fleet.handoff_value_is_schema_definition(valid_handoff()))
        self.assertFalse(fleet.handoff_value_is_schema_definition(None))

    def test_contract_mismatch_names_the_missing_field(self) -> None:
        text = contract_text_missing_field()
        category, _ = fleet.classify_handoff_rejection(text)
        self.assertEqual(category, "contract-mismatch")
        message = fleet.diagnose_handoff_rejection(text)
        self.assertIn("HANDOFF CONTRACT MISMATCH", message)
        self.assertIn("summary: missing required field", message)
        self.assertIn("worktree", message)

    def test_contract_mismatch_names_the_field_over_its_length_limit(self) -> None:
        category, _ = fleet.classify_handoff_rejection(contract_text_too_long())
        self.assertEqual(category, "contract-mismatch")
        message = fleet.diagnose_handoff_rejection(contract_text_too_long())
        self.assertIn("HANDOFF CONTRACT MISMATCH", message)
        self.assertIn("summary: exceeds maxLength 16000 (got 17000)", message)

    def test_contract_mismatch_reports_empty_min_items_list(self) -> None:
        value = valid_handoff()
        value["verification"] = []
        text = json.dumps(value)
        message = fleet.diagnose_handoff_rejection(text)
        self.assertIn("HANDOFF CONTRACT MISMATCH", message)
        self.assertIn("verification: requires at least 1 item(s), got 0", message)

    def test_the_four_rejection_causes_are_distinct(self) -> None:
        categories = {
            fleet.classify_handoff_rejection(no_candidate_text())[0],
            fleet.classify_handoff_rejection(broken_handoff_text())[0],
            fleet.classify_handoff_rejection(minimax_envelope(json.dumps(handoff_schema())))[0],
            fleet.classify_handoff_rejection(contract_text_missing_field())[0],
        }
        self.assertEqual(
            categories,
            {
                "no-candidate",
                "malformed-json",
                "schema-not-data",
                "contract-mismatch",
            },
        )

    def test_valid_handoff_is_still_accepted(self) -> None:
        handoff = valid_handoff()
        self.assertIsNotNone(fleet.validate_handoff(handoff))
        self.assertIsNone(
            fleet.handoff_semantic_error(handoff, mode="review", kind="review")
        )
        self.assertEqual(
            fleet.classify_handoff_rejection(json.dumps(handoff))[0], "unknown"
        )
        projection = fleet.project_provider_handoff(
            "codebuddy", codebuddy_envelope(json.dumps(handoff))
        )
        self.assertEqual(projection["handoff"], handoff)
        self.assertIsNone(projection["error"])

    def test_broken_and_schema_runs_are_still_rejected(self) -> None:
        broken_projection = fleet.project_provider_handoff(
            "codebuddy", codebuddy_envelope(broken_handoff_text())
        )
        self.assertIsNone(broken_projection["handoff"])
        schema_projection = fleet.project_provider_handoff(
            "minimax", minimax_envelope(json.dumps(handoff_schema()))
        )
        self.assertIsNone(schema_projection["handoff"])
        missing = json.loads(contract_text_missing_field())
        self.assertIsNone(fleet.validate_handoff(missing))
        for text in (
            broken_handoff_text(),
            minimax_envelope(json.dumps(handoff_schema())),
            contract_text_missing_field(),
            contract_text_too_long(),
        ):
            with self.subTest(text=text[:40]):
                self.assertIsNotNone(
                    fleet.handoff_semantic_error(
                        None, mode="review", kind="review", source_text=text
                    )
                )

    def test_empty_stdout_is_a_provider_failure_not_a_handoff_problem(self) -> None:
        reason = fleet.provider_failure_reason(
            stdout="", stderr="aborted", exit_code=1, timed_out=False
        )
        self.assertIsNotNone(reason)
        self.assertIn("PROVIDER FAILURE", reason)
        self.assertIn("no stdout", reason)
        message = fleet.diagnose_handoff_rejection("", provider_failure=reason)
        self.assertIn("PROVIDER FAILURE", message)
        self.assertNotIn("NO HANDOFF CANDIDATE", message)
        self.assertNotIn("MALFORMED JSON", message)
        self.assertNotIn("HANDOFF CONTRACT MISMATCH", message)
        self.assertEqual(
            message,
            fleet.handoff_semantic_error(
                None,
                mode="review",
                kind="review",
                source_text="",
                provider_failure=reason,
            ),
        )

    def test_provider_failure_reason_is_none_for_a_healthy_transport(self) -> None:
        self.assertIsNone(
            fleet.provider_failure_reason(
                stdout="{\"status\":\"completed\"}", stderr="", exit_code=0
            )
        )

    def test_timeout_and_missing_output_are_provider_failures(self) -> None:
        for kwargs in (
            {"timed_out": True},
            {"exit_code": 70},
            {"stdout": "   ", "stderr": "aborted"},
        ):
            with self.subTest(kwargs=kwargs):
                reason = fleet.provider_failure_reason(**kwargs)
                self.assertIsNotNone(reason)
                self.assertIn("PROVIDER FAILURE", reason)

    def test_diagnosis_without_source_text_keeps_the_previous_message(self) -> None:
        self.assertEqual(
            fleet.handoff_semantic_error(None, mode="review", kind="review"),
            "No structurally valid handoff was returned",
        )


class RawTranscriptDiagnosisTests(unittest.TestCase):
    """Diagnose the projected candidate, not the transcript echoing the prompt.

    Measured 2026-09-20 on run-20260920T184915Z-23147ffe4d: diagnosing the raw
    1141231 B stdout said "SCHEMA RETURNED INSTEAD OF DATA" because the prompt
    echo carries the handoff schema, while the model's own answer was malformed
    JSON. A confidently wrong diagnosis sends the parent the wrong way, so the
    raw transcript is only a last resort and never a schema accusation.
    Rejection is unchanged in every case below.
    """

    def test_prompt_echo_with_broken_result_is_malformed_json(self) -> None:
        transcript = codebuddy_transcript(broken_result_text())
        projection = fleet.project_provider_handoff("codebuddy", transcript)
        self.assertIsNone(projection["handoff"])
        self.assertIsNotNone(projection["error"])
        text, raw = fleet.handoff_diagnosis_text(projection, transcript)
        self.assertFalse(raw)
        self.assertEqual(text, projection["candidate_text"])
        try:
            json.loads(str(text))
        except json.JSONDecodeError as exc:
            expected_position = exc.pos
        else:  # pragma: no cover - the fixture is deliberately broken
            self.fail("candidate must not parse")
        message = fleet.diagnose_handoff_rejection(text, raw_transcript=raw)
        self.assertIn("MALFORMED JSON", message)
        self.assertNotIn("SCHEMA RETURNED", message)
        self.assertIn(f"at character {expected_position}", message)

    def test_raw_fallback_does_not_accuse_the_prompt_schema(self) -> None:
        transcript = codebuddy_transcript(broken_result_text())
        message = fleet.diagnose_handoff_rejection(transcript, raw_transcript=True)
        self.assertNotIn("SCHEMA RETURNED", message)
        self.assertIn("MALFORMED JSON", message)

    def test_schema_only_transcript_is_no_candidate_not_schema(self) -> None:
        transcript = codebuddy_transcript(None)
        projection = fleet.project_provider_handoff("codebuddy", transcript)
        self.assertIsNone(projection["handoff"])
        text, raw = fleet.handoff_diagnosis_text(projection, transcript)
        self.assertTrue(raw)
        self.assertIs(text, transcript)
        message = fleet.diagnose_handoff_rejection(text, raw_transcript=raw)
        self.assertIn("NO HANDOFF CANDIDATE", message)
        self.assertNotIn("SCHEMA RETURNED", message)
        self.assertNotIn("MALFORMED JSON", message)

    def test_minimax_schema_output_is_still_schema_not_data(self) -> None:
        envelope = minimax_envelope(json.dumps(handoff_schema()))
        projection = fleet.project_provider_handoff("minimax", envelope)
        self.assertIsNone(projection["handoff"])
        text, raw = fleet.handoff_diagnosis_text(projection, envelope)
        self.assertFalse(raw)
        message = fleet.diagnose_handoff_rejection(text, raw_transcript=raw)
        self.assertIn("SCHEMA RETURNED INSTEAD OF DATA", message)
        self.assertIn("properties", message)

    def test_valid_handoff_inside_a_transcript_still_passes(self) -> None:
        handoff = valid_handoff()
        transcript = codebuddy_transcript(json.dumps(handoff))
        projection = fleet.project_provider_handoff("codebuddy", transcript)
        self.assertEqual(projection["handoff"], handoff)
        self.assertIsNone(projection["error"])
        self.assertIsNone(projection["candidate_text"])
        self.assertIsNone(
            fleet.handoff_semantic_error(
                projection["handoff"],
                mode="review",
                kind="review",
                source_text=transcript,
            )
        )

    def test_candidate_text_is_bounded_and_redacted(self) -> None:
        broken = broken_handoff_text()
        self.assertIn(FAKE_TOKEN, broken)
        projection = fleet.project_provider_handoff(
            "codebuddy", codebuddy_transcript(broken)
        )
        candidate = projection["candidate_text"]
        self.assertIsInstance(candidate, str)
        self.assertNotIn(FAKE_TOKEN, candidate)
        self.assertIn("[REDACTED]", candidate)
        padded = broken + " tail " + ("y" * fleet.HANDOFF_CANDIDATE_TEXT_MAX_CHARS)
        padded_projection = fleet.project_provider_handoff(
            "codebuddy", codebuddy_transcript(padded)
        )
        padded_candidate = padded_projection["candidate_text"]
        self.assertIsInstance(padded_candidate, str)
        self.assertLessEqual(
            len(padded_candidate), fleet.HANDOFF_CANDIDATE_TEXT_MAX_CHARS
        )

    def test_transcript_failures_are_still_rejected(self) -> None:
        cases = (
            ("codebuddy", codebuddy_transcript(broken_result_text())),
            ("codebuddy", codebuddy_transcript(json.dumps(handoff_schema()))),
            ("minimax", minimax_envelope(broken_result_text())),
            ("minimax", minimax_envelope(json.dumps(handoff_schema()))),
        )
        for adapter_id, text in cases:
            with self.subTest(adapter_id=adapter_id, text=text[:40]):
                projection = fleet.project_provider_handoff(adapter_id, text)
                self.assertIsNone(projection["handoff"])
                self.assertIsNotNone(projection["error"])
                diagnosis, raw = fleet.handoff_diagnosis_text(projection, text)
                self.assertIsNotNone(
                    fleet.handoff_semantic_error(
                        None,
                        mode="review",
                        kind="review",
                        source_text=diagnosis,
                        raw_source=raw,
                    )
                )


class IdentityGateCauseTests(unittest.TestCase):
    """The run record must keep a transport failure as the parent-visible cause.

    Measured 2026-09-20 on run-20260920T144631Z-2d468bb9aa: zero stdout and an
    'aborted' stderr also failed the exact-model gate, whose message replaced the
    PROVIDER FAILURE text on the run record.
    """

    def aborted_codebuddy_run(self) -> tuple[str, str]:
        provider_failure = fleet.provider_failure_reason(
            stdout="", stderr="aborted", exit_code=1, timed_out=False
        )
        assert provider_failure is not None
        identity = fleet.codebuddy_model_identity("")
        self.assertEqual(identity["status"], "unavailable")
        identity_error = "CodeBuddy exact-model gate failed: " + str(
            identity.get("error") or identity["status"]
        )
        return provider_failure, identity_error

    def test_aborted_run_keeps_provider_failure_as_the_visible_cause(self) -> None:
        provider_failure, identity_error = self.aborted_codebuddy_run()
        final = fleet.identity_gate_error(provider_failure, identity_error)
        self.assertTrue(final.startswith("PROVIDER FAILURE"))
        self.assertIn("not a handoff problem", final)
        self.assertIn("CodeBuddy exact-model gate failed", final)

    def test_identity_gate_alone_is_unchanged_when_transport_is_healthy(self) -> None:
        for provider_failure in (None, ""):
            with self.subTest(provider_failure=provider_failure):
                self.assertEqual(
                    fleet.identity_gate_error(
                        provider_failure, "Grok exact-model gate failed: mismatch"
                    ),
                    "Grok exact-model gate failed: mismatch",
                )

    def test_combined_message_still_suppresses_preserved_disposition(self) -> None:
        provider_failure, identity_error = self.aborted_codebuddy_run()
        final = fleet.identity_gate_error(provider_failure, identity_error)
        disposition = fleet.preserved_artifact_disposition(
            {
                "status": [" M payload/agent-fleet/scripts/fleet.py"],
                "diff_stat": ["1 file changed"],
                "committed_diff_stat": [],
            },
            handoff=None,
            handoff_error=final,
        )
        self.assertIsNone(disposition)

    def test_combined_message_is_bounded(self) -> None:
        provider_failure, _ = self.aborted_codebuddy_run()
        long_identity = "CodeBuddy exact-model gate failed: " + ("x" * 5000)
        final = fleet.identity_gate_error(provider_failure, long_identity)
        self.assertLess(len(final), len(provider_failure) + 400)


class NestedSummaryHandoffTests(unittest.TestCase):
    """Extract the first complete handoff JSON object buried in summary prose."""

    def outer_with_summary(self, summary: str) -> dict[str, object]:
        value = valid_handoff()
        value["summary"] = summary
        return value

    def assert_unwraps_to(
        self, summary: str, inner: dict[str, object]
    ) -> None:
        outer = self.outer_with_summary(summary)
        unwrapped = fleet.unwrap_nested_summary_handoff(outer)
        self.assertEqual(unwrapped, inner)
        self.assertEqual(unwrapped, fleet.validate_handoff(inner))
        candidate = fleet.handoff_candidate_text(outer)
        self.assertIsNotNone(candidate)
        self.assertEqual(json.loads(str(candidate)), inner)
        string_candidate = fleet.handoff_candidate_text(summary)
        self.assertIsNotNone(string_candidate)
        self.assertEqual(json.loads(str(string_candidate)), inner)

    def test_pure_json_summary_still_unwraps(self) -> None:
        inner = valid_handoff()
        inner["summary"] = "pure nested handoff"
        self.assert_unwraps_to(json.dumps(inner), inner)

    def test_prose_before_json_unwraps(self) -> None:
        inner = valid_handoff()
        inner["summary"] = "inner-before"
        summary = "Przejrzalem zmiany i uwazam ze sa poprawne.\n" + json.dumps(inner)
        self.assert_unwraps_to(summary, inner)

    def test_prose_after_json_unwraps(self) -> None:
        inner = valid_handoff()
        inner["summary"] = "inner-after"
        summary = json.dumps(inner) + "\nTyle ode mnie."
        self.assert_unwraps_to(summary, inner)

    def test_prose_on_both_sides_unwraps(self) -> None:
        inner = valid_handoff()
        inner["summary"] = "inner-both-sides"
        summary = (
            "Przejrzalem zmiany i uwazam ze sa poprawne.\n"
            + json.dumps(inner)
            + "\nTyle ode mnie."
        )
        self.assert_unwraps_to(summary, inner)

    def test_brace_inside_string_value_does_not_break_matching(self) -> None:
        inner = valid_handoff()
        inner["summary"] = 'path uses a{b brace in the value'
        summary = "before " + json.dumps(inner) + " after"
        self.assert_unwraps_to(summary, inner)

    def test_escaped_quote_inside_string_value_does_not_break_matching(self) -> None:
        inner = valid_handoff()
        inner["summary"] = 'He said "nested" quotes already.'
        summary = "before " + json.dumps(inner) + " after"
        self.assert_unwraps_to(summary, inner)

    def test_unclosed_brace_returns_none_without_raising(self) -> None:
        summary = (
            'before {"status": "completed", "summary": "open", '
            '"files_changed": [], "verification": ["x"]'
        )
        outer = self.outer_with_summary(summary)
        try:
            unwrapped = fleet.unwrap_nested_summary_handoff(outer)
            candidate = fleet.handoff_candidate_text(outer)
        except (json.JSONDecodeError, RecursionError, ValueError) as exc:
            self.fail(f"unclosed nested JSON must not raise: {exc}")
        self.assertIsNone(unwrapped)
        self.assertIsNone(candidate)

    def test_two_objects_take_the_first_complete_valid_handoff(self) -> None:
        first = valid_handoff()
        first["summary"] = "first-complete"
        second = valid_handoff()
        second["summary"] = "second-complete"
        summary = json.dumps(first) + " and also " + json.dumps(second)
        self.assert_unwraps_to(summary, first)

    def test_two_objects_skip_a_complete_non_handoff(self) -> None:
        inner = valid_handoff()
        inner["summary"] = "second-is-the-handoff"
        summary = '{"verdict": "PASS"} and ' + json.dumps(inner)
        outer = self.outer_with_summary(summary)
        self.assertEqual(fleet.unwrap_nested_summary_handoff(outer), inner)
        candidate = fleet.handoff_candidate_text(outer)
        self.assertIsNotNone(candidate)
        self.assertEqual(json.loads(str(candidate)), {"verdict": "PASS"})

    def test_pass_word_in_prose_is_not_a_verdict(self) -> None:
        outer = self.outer_with_summary(
            "I think this is PASS and should be accepted."
        )
        self.assertIsNone(fleet.unwrap_nested_summary_handoff(outer))
        self.assertIsNone(fleet.handoff_candidate_text(outer))

    def test_empty_and_non_json_return_none_without_raising(self) -> None:
        unwrap_cases: list[object] = [
            None,
            "",
            "   ",
            "no object here",
            {"summary": ""},
            {"summary": "   "},
            {"summary": "still no json object"},
        ]
        for value in unwrap_cases:
            with self.subTest(value=value):
                try:
                    if isinstance(value, dict) or value is None:
                        unwrapped = fleet.unwrap_nested_summary_handoff(value)
                    else:
                        unwrapped = fleet.unwrap_nested_summary_handoff(
                            self.outer_with_summary(str(value))
                        )
                except (json.JSONDecodeError, RecursionError, ValueError) as exc:
                    self.fail(f"must not raise for {value!r}: {exc}")
                self.assertIsNone(unwrapped)
        for value in (
            None,
            "",
            "   ",
            {"summary": ""},
            {"summary": "   "},
            {"summary": "still no json object"},
        ):
            with self.subTest(candidate=value):
                try:
                    candidate = fleet.handoff_candidate_text(value)
                except (json.JSONDecodeError, RecursionError, ValueError) as exc:
                    self.fail(f"must not raise for {value!r}: {exc}")
                self.assertIsNone(candidate)

    def test_extracted_object_uses_the_same_handoff_validation(self) -> None:
        incomplete = {
            "status": "completed",
            "summary": "missing the rest of the contract",
        }
        extra = valid_handoff()
        extra["unexpected"] = "field"
        for nested in (incomplete, extra):
            with self.subTest(nested=nested):
                outer = self.outer_with_summary("prose " + json.dumps(nested))
                self.assertIsNone(fleet.validate_handoff(nested))
                self.assertIsNone(fleet.unwrap_nested_summary_handoff(outer))


if __name__ == "__main__":
    unittest.main()
