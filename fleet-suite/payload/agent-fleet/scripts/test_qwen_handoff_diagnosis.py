"""Synthetic regressions for Qwen handoff diagnosis provenance (RSI HD-20261015-133802).

The Qwen projection in ``fleet.project_provider_handoff`` must surface the
selected terminal candidate through ``candidate_text`` and any structured
validator rejection through ``validation_error`` so that downstream diagnosis
never falls back to scanning the raw stream-json envelope. Otherwise the raw
transcript can echo earlier tool/user/assistant handoffs and the parent
mistakes them for the model's own malformed answer.

Every fixture below is synthetic. Nothing is copied from a live run record
or a project log.
"""

from __future__ import annotations

import json
import contextlib
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import fleet


PROMPT_SCHEMA = (
    "Return exactly one JSON object matching this handoff schema: "
    '{"type":"object","required":["status","summary","files_changed",'
    '"verification","risks","next_steps"],"properties":{"status":'
    '{"type":"string","enum":["completed","partial","blocked","failed"]},'
    '"summary":{"type":"string","minLength":1}}}'
    " and nothing else."
)


def valid_handoff(summary: str = "Synthetic bounded Qwen result.") -> dict[str, object]:
    return {
        "status": "completed",
        "summary": summary,
        "files_changed": [],
        "verification": ["Ran a synthetic focused check."],
        "risks": [],
        "next_steps": [],
    }


def _assistant_event(text: str) -> dict[str, object]:
    return {
        "type": "assistant",
        "message": {"role": "assistant", "content": [{"type": "text", "text": text}]},
    }


def _user_prompt_event(text: str = PROMPT_SCHEMA) -> dict[str, object]:
    return {"type": "user", "message": {"role": "user", "content": text}}


def _tool_result_event() -> dict[str, object]:
    return {
        "type": "user",
        "message": {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": "read-1",
                    "is_error": False,
                    "content": "synthetic bounded tool output",
                }
            ],
        },
    }


def _success_terminal(result) -> dict[str, object]:
    return {"type": "result", "subtype": "success", "result": result}


def _failure_terminal(subtype: str = "error_max_turns", message: str = "max 8 turns") -> dict[str, object]:
    return {"type": "result", "subtype": subtype, "result": message}


def encode(*events: dict[str, object]) -> str:
    return "\n".join(json.dumps(event) for event in events)


def diagnose(envelope_text: str, *, handoff_validator=None):
    projection = fleet.project_provider_handoff(
        "qwen", envelope_text, handoff_validator=handoff_validator
    )
    diagnosis_text, raw_source = fleet.handoff_diagnosis_text(projection, envelope_text)
    # Match run_delegated's projected_handoff_error: a stored validator error
    # wins before generic candidate classification.
    semantic_error = projection.get("validation_error") or fleet.handoff_semantic_error(
        projection.get("handoff"),
        mode="review",
        kind="review",
        source_text=diagnosis_text,
        raw_source=raw_source,
        no_candidate_detail=projection.get("no_candidate_detail"),
    )
    return projection, diagnosis_text, raw_source, semantic_error


class QwenHandoffDiagnosisTests(unittest.TestCase):
    def test_unrelated_object_does_not_hide_later_malformed_handoff(self):
        for malformed in (
            '{"status":"completed","summary":',
            '{"extension":{"meta":1},"status":"completed","summary":',
        ):
            with self.subTest(malformed=malformed):
                _, candidate, raw, error = diagnose(encode(_success_terminal('Config: {"model":"x"} now '+malformed)))
                self.assertEqual(candidate, malformed)
                self.assertFalse(raw)
                self.assertIn('MALFORMED JSON', error)

    def test_schema_example_does_not_hide_later_malformed_data(self):
        schema = json.dumps({'type':'object','required':['status'],'properties':{'status':{'type':'string'}}})
        malformed = '{"status":"completed","summary":'
        _, candidate, raw, error = diagnose(encode(_success_terminal(schema+' then '+malformed)))
        self.assertEqual(candidate, malformed)
        self.assertFalse(raw)
        self.assertIn('MALFORMED JSON', error)

    def test_native_failure_detail_is_bounded_redacted_and_preserves_scalar_or_nested_reason(self):
        fake = 'sk-'+'synthetic-'+'ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789'
        text = 'token '+fake+' '+'x'*(fleet.PROVIDER_ERROR_DETAIL_CHARS*4)
        _, _, raw, error = diagnose(encode(_failure_terminal('error_'+'s'*200,text)))
        self.assertFalse(raw)
        self.assertNotIn(fake,error)
        self.assertIn('[REDACTED]',error)
        self.assertLessEqual(len(error),len('QWEN TERMINAL FAILURE: native subtype ')+80+2+fleet.PROVIDER_ERROR_DETAIL_CHARS)
        for detail, expected in ((7,'7'),(0,'0'),({'padding':'x'*1000,'message':'Meaningful nested reason.'},'Meaningful nested reason.')):
            with self.subTest(detail_type=type(detail).__name__,expected=expected):
                _,_,_,error=diagnose(encode(_failure_terminal('error_during_execution',detail)))
                self.assertIn(expected,error)

    def test_malformed_handoff_with_foreign_first_key_remains_malformed(self):
        text = '{"extension":0,"status":"completed","summary":'
        projection, candidate, raw, error = diagnose(encode(_success_terminal(text)))
        self.assertIsNone(projection['handoff'])
        self.assertEqual(candidate, text)
        self.assertFalse(raw)
        self.assertIn('MALFORMED JSON', error)

    def test_native_failure_keeps_subtype_and_reason_without_handoff_recovery(self):
        reason = 'Synthetic native run stopped before finalization.'
        projection, _, raw, error = diagnose(encode(
            _assistant_event(json.dumps(valid_handoff())),
            _failure_terminal('error_during_execution', reason),
        ))
        self.assertIsNone(projection['handoff'])
        self.assertFalse(raw)
        self.assertIn('QWEN TERMINAL FAILURE', error)
        self.assertIn('error_during_execution', error)
        self.assertIn(reason, error)

    def test_dict_validator_rejection_retains_existing_assistant_fallback(self):
        fallback = valid_handoff('Fallback accepted.')
        primary = valid_handoff('Primary rejected.')
        projection, _, raw, error = diagnose(encode(
            _assistant_event(json.dumps(fallback)), _success_terminal(primary),
        ), handoff_validator=lambda h: 'Rejected primary.' if h['summary']=='Primary rejected.' else None)
        self.assertEqual(projection['handoff'], fallback)
        self.assertEqual(projection['source'], 'qwen.terminalAssistantText')
        self.assertIsNone(projection['validation_error'])
        self.assertFalse(raw)
        self.assertIsNone(error)

    def test_execute_single_exports_actual_candidate_and_validator_diagnosis(self):
        profiles = fleet.load_profiles(fleet.DEFAULT_PROFILES)
        rejection = "Synthetic run schema rejection."
        for final, validator, code, expected in (
            ('CSS .x { display: flex; }', None, 0, 'NO HANDOFF CANDIDATE'),
            (json.dumps(valid_handoff()), lambda _: rejection, 0, rejection),
            (json.dumps(valid_handoff()), lambda _: rejection, 1, 'PROVIDER FAILURE'),
        ):
            with self.subTest(code=code, expected=expected), tempfile.TemporaryDirectory() as temp_root:
                root = Path(temp_root)
                stream = encode(_user_prompt_event(), _assistant_event(final), _success_terminal(final))
                child = {'exit_code': code, 'timed_out': False, 'stdout': stream,
                         'stderr': 'aborted' if code else '', 'model_tool_calls': 0,
                         'tool_budget_exceeded': False}
                patches = [mock.patch.dict(os.environ, {
                    'AGENT_FLEET_DEPTH': '0', 'AGENT_FLEET_OFFICE': '0',
                    'AGENT_FLEET_BILLING_MODE_PATH': str(root / 'missing-billing.json')})]
                patches.extend(mock.patch.object(obj, name, return_value=value)
                    for obj, name, value in (
                        (fleet, 'resolve_executable', 'synthetic-qwen.exe'),
                        (fleet, 'probe_command', {'launchable': True, 'exit_code': 0, 'output': 'ok'}),
                        (fleet, 'profile_runtime_environment', {}),
                        (fleet, 'profile_credential_status', {'ready': True}),
                        (fleet, 'merge_credential_readiness', {'ready': True, 'reasons': []}),
                        (fleet.routing_v2, 'readiness_from_probes', {'ready': True, 'reasons': []}),
                        (fleet.routing_v2, 'runtime_model_visible', (True, 'visible')),
                        (fleet, 'fleet_runtime_attestation', {'status': 'VERIFIED'}),
                        (fleet, 'read_usage', []),
                        (fleet.adaptive, 'latest_quotas', {}),
                        (fleet.quota_collectors, 'collect_profile_quotas', ({}, {})),
                        (fleet, 'qwen_model_registration', {'status': 'registered', 'registry_source': 'fixture'}),
                        (fleet, 'qwen_model_identity', {'status': 'verified', 'expected_model': 'qwen3.8-max',
                                                      'observed_model': 'qwen3.8-max'}),
                        (fleet, 'run_provider_child', {'child': child}),
                    ))
                with contextlib.ExitStack() as stack:
                    for patcher in patches:
                        stack.enter_context(patcher)
                    result = fleet.execute_single(
                        'Synthetic Qwen diagnosis fixture.', profiles=profiles, agent_id='qwen',
                        mode='review', kind='review', complexity='low', model_override='qwen3.8-max',
                        workspace=root, timeout=5, allow_write=False, native_subagent_limit=0,
                        enable_web=False, inherit_api_keys=False, dry_run=False,
                        output_root=root / 'evidence', root=root, knowledge_mode='none',
                        handoff_validator=validator)
                self.assertEqual(result['status'], 'FAILED')
                self.assertIn(expected, result['handoff_parse_error'])
                self.assertIn(expected, result['provider_attempts'][0]['handoff_error'])
                if code:
                    self.assertTrue(result['handoff_parse_error'].startswith('PROVIDER FAILURE'))
                    self.assertIn(rejection, result['handoff_parse_error'])
                if expected == 'NO HANDOFF CANDIDATE':
                    self.assertNotIn('MALFORMED JSON', result['handoff_parse_error'])

    def test_valid_result_wins_over_different_assistant_text(self):
        result = valid_handoff("Authoritative terminal result.")
        other = valid_handoff("Different assistant answer.")
        projection, _, _, error = diagnose(encode(
            _assistant_event(json.dumps(other)), _success_terminal(result)
        ))
        self.assertEqual(projection["handoff"], result)
        self.assertEqual(projection["source"], "qwen.terminalResult")
        self.assertIsNone(error)

    def test_fallback_validator_rejection_is_preserved(self):
        error = "Synthetic fallback contract rejection."
        projection, _, raw, actual = diagnose(encode(
            _assistant_event(json.dumps(valid_handoff())), _success_terminal("Plain response.")
        ), handoff_validator=lambda _: error)
        self.assertIsNone(projection["handoff"])
        self.assertFalse(raw)
        self.assertEqual(projection["validation_error"], error)
        self.assertEqual(actual, error)

    def test_malformed_handoff_after_css_is_the_diagnosed_candidate(self):
        final = 'CSS .x { display: flex; }\n{"status":"completed","summary":'
        projection, candidate, raw, error = diagnose(encode(_success_terminal(final)))
        self.assertIsNone(projection["handoff"])
        self.assertFalse(raw)
        self.assertEqual(candidate, '{"status":"completed","summary":')
        self.assertIn("MALFORMED JSON", error)
        self.assertNotIn("display", error)

    def test_failure_terminal_never_recovers_a_valid_assistant_handoff(self):
        projection, _, raw, _ = diagnose(encode(
            _assistant_event(json.dumps(valid_handoff())), _failure_terminal()
        ))
        self.assertIsNone(projection["handoff"])
        self.assertEqual(projection["terminal_reason"], "error_max_turns")
        self.assertFalse(raw)

    def test_valid_dict_result_returns_selected_terminal_candidate(self) -> None:
        handoff = valid_handoff("Dict result wins.")
        envelope = encode(
            _user_prompt_event(),
            _tool_result_event(),
            _success_terminal(handoff),
        )

        projection, diagnosis_text, raw_source, semantic_error = diagnose(envelope)

        self.assertEqual(projection["handoff"], handoff)
        self.assertEqual(projection["source"], "qwen.terminalResult")
        self.assertIsNone(projection["validation_error"])
        self.assertIsNone(semantic_error)
        self.assertFalse(raw_source)

    def test_valid_string_result_is_parsed_without_raw_fallback(self) -> None:
        handoff = valid_handoff("String result parsed cleanly.")
        projection, diagnosis_text, raw_source, semantic_error = diagnose(
            encode(_success_terminal(json.dumps(handoff)))
        )

        self.assertEqual(projection["handoff"], handoff)
        self.assertEqual(projection["source"], "qwen.terminalResult")
        self.assertEqual(diagnosis_text, json.dumps(handoff))
        self.assertFalse(raw_source)
        self.assertIsNone(semantic_error)

    def test_structured_dict_validator_rejection_is_preserved_not_swallowed(self) -> None:
        # ``validate_handoff`` passes (structurally a dict with the right keys),
        # but a stricter validator rejects it. The rejection must surface as
        # ``validation_error``; the diagnosis must NOT fall back to scanning
        # the raw transcript, where earlier tool/user events live.
        good_shape = valid_handoff("Looks fine to validate_handoff but not the schema.")
        envelope = encode(
            _user_prompt_event(),
            _tool_result_event(),
            _success_terminal(good_shape),
        )

        def reject(value):
            return "SYNTHETIC_VALIDATOR: rejected status='completed' without a custom gate."

        projection, diagnosis_text, raw_source, semantic_error = diagnose(
            envelope, handoff_validator=reject
        )

        self.assertIsNone(projection["handoff"])
        self.assertEqual(projection["validation_error"],
                         "SYNTHETIC_VALIDATOR: rejected status='completed' without a custom gate.")
        self.assertEqual(projection["error"], projection["validation_error"])
        self.assertFalse(raw_source)
        self.assertEqual(semantic_error, projection["validation_error"])
        # Raw transcript contains the echoed prompt schema and a tool_result.
        # Neither should leak into the diagnosis text or error.
        self.assertNotIn(PROMPT_SCHEMA, diagnosis_text)
        self.assertNotIn("synthetic bounded tool output", diagnosis_text)
        self.assertNotIn("synthetic bounded tool output", str(semantic_error))

    def test_invalid_string_result_falls_back_to_valid_assistant_text(self) -> None:
        handoff = valid_handoff("Assistant text recovers.")
        envelope = encode(
            _assistant_event("not even close"),
            _assistant_event("But the assistant here returns a real handoff:\n" + json.dumps(handoff)),
            _success_terminal('{"status":"failed","summary":"'),
        )

        projection, diagnosis_text, raw_source, semantic_error = diagnose(envelope)

        self.assertEqual(projection["handoff"], handoff)
        self.assertEqual(projection["source"], "qwen.terminalAssistantText")
        self.assertIsNone(projection["validation_error"])
        self.assertFalse(raw_source)
        self.assertIsNone(semantic_error)

    def test_invalid_string_result_with_recoverable_assistant_does_not_swallow_validator(self) -> None:
        handoff = valid_handoff("Recovery keeps validator honest.")
        malformed = '{"status":"completed","summary":'
        envelope = encode(
            _assistant_event("Partial lead-in: " + malformed),
            _assistant_event("Then a complete one:\n" + json.dumps(handoff)),
            _success_terminal(malformed + " trailing noise"),
        )

        projection, diagnosis_text, raw_source, semantic_error = diagnose(envelope)

        self.assertEqual(projection["handoff"], handoff)
        self.assertEqual(projection["source"], "qwen.terminalAssistantText")
        self.assertFalse(raw_source)
        self.assertIsNone(semantic_error)

    def test_truly_malformed_final_is_diagnosed_from_terminal_text_only(self) -> None:
        # The terminal result is a string that is malformed JSON, AND the
        # assistant text contains earlier prose that quotes braces. The raw
        # transcript also contains the prompt's schema. None of that should
        # bleed into the diagnosis.
        malformed = '{"status":"completed","summary":'
        envelope = encode(
            _user_prompt_event(),
            _assistant_event('Note: a CSS selector like `.x { color: red }` and a TS line `const t: {a:number} = ...`.'),
            _success_terminal(malformed),
        )

        projection, diagnosis_text, raw_source, semantic_error = diagnose(envelope)

        self.assertIsNone(projection["handoff"])
        self.assertEqual(projection["terminal_reason"], "success")
        self.assertEqual(diagnosis_text, malformed)
        self.assertFalse(raw_source)
        self.assertIn("MALFORMED JSON", str(semantic_error))
        self.assertNotIn(PROMPT_SCHEMA, str(semantic_error))
        self.assertNotIn("CSS selector", str(semantic_error))
        self.assertNotIn("TS line", str(semantic_error))

    def test_prose_with_quoted_braces_is_a_no_candidate_diagnosis(self) -> None:
        # Quoted CSS/TS braces are not a malformed JSON handoff.
        prose = (
            'A CSS rule like `body { background: #fff }` and a TS alias '
            '`type T = { a: number }` appear in the prose. Review is complete.'
        )
        envelope = encode(
            _user_prompt_event(),
            _assistant_event(prose),
            _success_terminal(prose),
        )

        projection, diagnosis_text, raw_source, semantic_error = diagnose(envelope)

        self.assertIsNone(projection["handoff"])
        self.assertEqual(projection["terminal_reason"], "success")
        self.assertFalse(raw_source)
        self.assertEqual(diagnosis_text, "")
        self.assertNotEqual(diagnosis_text, envelope)
        self.assertNotIn(PROMPT_SCHEMA, str(semantic_error))
        self.assertIn("NO HANDOFF CANDIDATE", str(semantic_error))
        self.assertNotIn("MALFORMED JSON", str(semantic_error))

    def test_empty_or_missing_assistant_falls_back_without_raw_transcript(self) -> None:
        handoff = valid_handoff("Valid terminal text.")
        projection, diagnosis_text, raw_source, semantic_error = diagnose(
            encode(_success_terminal(json.dumps(handoff)))
        )
        self.assertEqual(projection["handoff"], handoff)
        self.assertEqual(diagnosis_text, json.dumps(handoff))
        self.assertFalse(raw_source)
        self.assertIsNone(semantic_error)

        projection, diagnosis_text, raw_source, semantic_error = diagnose(
            encode(_success_terminal("not a handoff at all"))
        )
        self.assertIsNone(projection["handoff"])
        self.assertEqual(projection["terminal_reason"], "success")
        self.assertEqual(diagnosis_text, "")
        self.assertFalse(raw_source)
        self.assertNotIn(PROMPT_SCHEMA, str(semantic_error))

    def test_missing_or_malformed_envelope_reports_envelope_failure_not_prompt_schema(self) -> None:
        cases = (
            "",
            "   \n  ",
            "not even close to JSON",
            "{not even json}",
            '{"type":"not_result"}',
            "Traceback (most recent call last): boom",
        )
        for raw in cases:
            with self.subTest(raw=raw):
                projection, diagnosis_text, raw_source, semantic_error = diagnose(raw)

                self.assertIsNone(projection["handoff"])
                self.assertIsNone(projection["source"])
                self.assertIsNone(projection["terminal_reason"])
                self.assertEqual(diagnosis_text, "")
                self.assertFalse(raw_source)
                self.assertNotIn("MALFORMED JSON", semantic_error)
                self.assertIsInstance(semantic_error, str)
                self.assertNotIn(PROMPT_SCHEMA, semantic_error)
                # The error on the projection must be the envelope failure,
                # not a hallucinated handoff defect.
                self.assertNotIn("CONTRACT MISMATCH", projection["error"])
                # PROMPT_SCHEMA never bleeds through.
                self.assertNotIn("Required handoff fields", projection["error"])

    def test_non_success_terminal_is_diagnosed_without_raw_fallback(self) -> None:
        envelope = encode(
            _user_prompt_event(),
            _tool_result_event(),
            _failure_terminal(),
        )

        projection, diagnosis_text, raw_source, semantic_error = diagnose(envelope)

        self.assertIsNone(projection["handoff"])
        self.assertEqual(projection["terminal_reason"], "error_max_turns")
        self.assertFalse(raw_source)
        # Diagnosis refers to the terminal text, never to the prompt schema or tool output.
        self.assertNotIn(PROMPT_SCHEMA, str(semantic_error))
        self.assertNotIn("synthetic bounded tool output", str(semantic_error))

    def test_ancillary_earlier_assistant_handoff_never_counts_as_candidate(self) -> None:
        # The earlier assistant event already produced a complete handoff; the
        # terminal result, however, is broken. The diagnostic must reject the
        # broken terminal result without falling back to the earlier one.
        earlier = valid_handoff("Earlier assistant handoff should NOT be picked up.")
        envelope = encode(
            _user_prompt_event(),
            _assistant_event("Earlier:\n" + json.dumps(earlier)),
            _user_prompt_event("New bounded request after the prior answer."),
            _success_terminal('{"status":"completed","summary":'),
        )

        projection, diagnosis_text, raw_source, semantic_error = diagnose(envelope)

        self.assertIsNone(projection["handoff"])
        self.assertFalse(raw_source)
        self.assertEqual(diagnosis_text, '{"status":"completed","summary":')
        self.assertNotIn(json.dumps(earlier), str(semantic_error))
        self.assertIn("MALFORMED JSON", str(semantic_error))
        self.assertNotIn(PROMPT_SCHEMA, str(semantic_error))

    def test_tool_user_handoffs_in_envelope_do_not_count(self) -> None:
        # Tool/user events happen to contain valid handoff-shaped payloads in
        # the transcript. The projection must ignore them and only judge the
        # selected terminal candidate.
        tool_payload = valid_handoff("Tool-injected handoff should NOT be picked up.")
        envelope = encode(
            {"type": "user", "message": {"content": json.dumps(tool_payload)}},
            _tool_result_event(),
            _success_terminal('{"status":"partial","summary":"cut off'),
        )

        projection, diagnosis_text, raw_source, semantic_error = diagnose(envelope)

        self.assertIsNone(projection["handoff"])
        self.assertEqual(projection["terminal_reason"], "success")
        self.assertFalse(raw_source)
        self.assertNotIn("Tool-injected handoff", str(semantic_error))
        self.assertIn("MALFORMED JSON", str(semantic_error))


if __name__ == "__main__":
    unittest.main()
