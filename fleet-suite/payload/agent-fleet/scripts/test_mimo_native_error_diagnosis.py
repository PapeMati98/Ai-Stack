"""HD-ce7fa0: synthetic native errors survive the actual persisted Fleet run.

The existing quota fixture mocks native launch/readiness and supplies a fake
Token Plan store. No real provider, auth file or network is accessed.
"""
from __future__ import annotations

import json
import unittest

import fleet
import mimo_runtime
import test_mimo_quota_refusal as quota_fixture

NATIVE_IDENTITY = (
    "INFO  2026-10-25T08:48:04 +1ms service=llm providerID=xiaomi "
    "modelID=mimo-v2.6-pro session.id=ses_synthetic small=false agent=plan mode=primary stream\n"
)
VALID_HANDOFF = {
    "status": "completed", "summary": "Reviewed the bounded synthetic module.",
    "files_changed": [], "verification": ["Synthetic check passed."],
    "risks": [], "next_steps": [],
}


def stream(*events):
    return "\n".join(json.dumps(event) for event in events) + "\n"


def native_error(error=None, *, tool_output=False):
    events = [{"type": "step_start", "part": {"type": "step-start"}}]
    if tool_output:
        events.append({"type": "tool_use", "part": {"type": "tool", "tool": "read",
            "state": {"status": "completed", "output": '28: SETTINGS = {\n29: "VALUE",\n30: }'}}})
    events.append({"type": "error", "error": error if error is not None else {
        "name": "APIError", "data": {"message": "SSE read timed out"}}})
    return stream(*events)


class MiMoNativeMessageTests(unittest.TestCase):
    def test_native_message_beats_generic_name_and_outer_message(self):
        raw = native_error({"name": "APIError", "message": "Generic failure",
                            "data": {"message": " SSE read timed out "}})
        self.assertEqual(mimo_runtime.mimo_terminal_status(raw), ("failed", "SSE read timed out"))

    def test_existing_fallbacks_and_non_message_payloads(self):
        cases = [
            ({"name": "APIError", "message": "outer", "data": None}, "outer"),
            ({"name": "APIError", "data": {"message": "  "}}, "APIError"),
            ({"name": "APIError", "data": {"message": {"request": "do not stringify"}}}, "APIError"),
            ({"name": "APIError", "data": "opaque payload"}, "APIError"),
            ({"name": "APIError", "data": {"error": {"request": "opaque"}}}, "APIError"),
            ({}, "provider error"),
            ("Transport stopped", "Transport stopped"),
        ]
        for error, expected in cases:
            with self.subTest(error=error):
                self.assertEqual(mimo_runtime.mimo_terminal_status(native_error(error)), ("failed", expected))

    def test_projection_keeps_native_error_contract(self):
        projection = fleet.project_provider_handoff("mimo", native_error(tool_output=True))
        self.assertIsNone(projection["handoff"])
        self.assertEqual(projection["terminal_reason"], "failed")
        self.assertEqual(projection["error"], "SSE read timed out")
        self.assertEqual(projection["validation_error"], "SSE read timed out")
        self.assertIs(projection["provider_failure_leads"], True)
        self.assertNotIn("29:", projection["candidate_text"])


class MiMoRunRecordTests(unittest.TestCase):
    def execute(self, stdout, *, exit_code=0, timed_out=False, stderr=""):
        result, record, _, _ = quota_fixture.ExecuteSingleTests().mimo_run(child={
            "exit_code": exit_code, "stdout": stdout, "stderr": NATIVE_IDENTITY + stderr,
            "timed_out": timed_out, "model_tool_calls": 0, "tool_budget_exceeded": False,
        })
        self.assertEqual(record["model_identity"]["status"], "verified")
        return result, record

    def test_exit_zero_native_error_is_persisted(self):
        for tool_output in (False, True):
            with self.subTest(tool_output=tool_output):
                result, record = self.execute(native_error(tool_output=tool_output))
                self.assertNotEqual(result["status"], "COMPLETED")
                self.assertIsNone(record["handoff"])
                self.assertEqual(record["handoff_parse_error"], "SSE read timed out")

    def test_transport_failure_leads_and_native_message_survives(self):
        _, record = self.execute(native_error(tool_output=True), exit_code=1,
                                 stderr="ERROR process aborted unexpectedly\n")
        diagnosis = record["handoff_parse_error"]
        self.assertTrue(diagnosis.startswith("PROVIDER FAILURE"), diagnosis)
        self.assertIn("SSE read timed out", diagnosis)
        self.assertNotIn("NO HANDOFF CANDIDATE", diagnosis)
        self.assertNotIn("MALFORMED JSON", diagnosis)
        self.assertNotIn("29:", diagnosis)

    def test_parent_timeout_still_leads_and_native_message_survives(self):
        _, record = self.execute(native_error(), exit_code=124, timed_out=True)
        self.assertEqual(record["completion_reason"], "parent-timeout")
        self.assertTrue(record["handoff_parse_error"].startswith("PROVIDER FAILURE"))
        self.assertIn("SSE read timed out", record["handoff_parse_error"])

    def test_outer_message_error_is_preserved(self):
        _, record = self.execute(native_error({"name": "ProviderError", "message": "Connection lost"}))
        self.assertEqual(record["handoff_parse_error"], "Connection lost")

    def test_missing_handoff_retains_its_own_diagnosis(self):
        _, record = self.execute(stream(
            {"type": "text", "part": {"type": "text", "text": "Reviewed the module."}},
            {"type": "step_finish", "part": {"reason": "stop"}},
        ))
        self.assertTrue(record["handoff_parse_error"].startswith("NO HANDOFF CANDIDATE"))
        self.assertNotIn("SSE read timed out", record["handoff_parse_error"])

    def test_valid_completion_remains_accepted(self):
        result, record = self.execute(stream(
            {"type": "text", "part": {"type": "text", "text": json.dumps(VALID_HANDOFF)}},
            {"type": "step_finish", "part": {"reason": "stop"}},
        ))
        self.assertEqual(result["status"], "COMPLETED")
        self.assertEqual(record["handoff"], VALID_HANDOFF)
        self.assertIsNone(record["handoff_parse_error"])



if __name__ == "__main__":
    unittest.main()
