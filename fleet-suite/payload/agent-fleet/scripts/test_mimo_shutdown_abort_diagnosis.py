"""HD-20261018-1abe4d: MiMo shutdown abort is not a provider failure and a
rejected MiMo handoff is diagnosed from the assistant text, not tool output.

Synthetic streams only; no provider process is started.
"""
from __future__ import annotations

import json
import unittest

import fleet
import mimo_runtime

SESSION = "ses_ffe5test00000000000000000"
MESSAGE = "msg_test000000000000000000000"


def _event(kind: str, part: dict) -> dict:
    return {"type": kind, "timestamp": 1792242774887, "sessionID": SESSION, "part": part}


def step_start() -> dict:
    return _event("step_start", {"type": "step-start", "messageID": MESSAGE})


def step_finish(reason: str) -> dict:
    return _event(
        "step_finish",
        {
            "type": "step-finish",
            "reason": reason,
            "messageID": MESSAGE,
            "tokens": {"total": 100, "input": 80, "output": 20, "reasoning": 0,
                       "cache": {"write": 0, "read": 0}},
        },
    )


def text_event(text: str) -> dict:
    return _event("text", {"type": "text", "messageID": MESSAGE, "text": text})


def read_event(output: str) -> dict:
    return _event(
        "tool_use",
        {
            "type": "tool",
            "tool": "read",
            "callID": "call_0000000000000000",
            "state": {"status": "completed", "input": {"file_path": "D:/ws/config.py"},
                      "output": output},
        },
    )


def stream(*events: dict) -> str:
    return "\n".join(json.dumps(event) for event in events) + "\n"


# A read result with line-number prefixes: the raw-transcript diagnosis used to
# report "MALFORMED JSON ... context: '{ 29: ...'" over exactly this shape.
FILE_READ_OUTPUT = (
    "<path>D:/ws/config.py</path>\n<type>file</type>\n<content>\n"
    "28: NAMES = {\n29:   \"FIRST_NAME\",\n30:   \"SECOND_NAME\",\n31: }\n"
    "</content>"
)

HANDOFF_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "status": {"type": "string", "enum": ["completed", "partial", "blocked", "failed"]},
        "summary": {"type": "string"},
        "files_changed": {"type": "array", "items": {"type": "string"}},
        "verification": {"type": "array", "items": {"type": "string"}},
        "risks": {"type": "array", "items": {"type": "string"}},
        "next_steps": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["status", "summary", "files_changed", "verification", "risks", "next_steps"],
}

VALID_HANDOFF = {
    "status": "completed",
    "summary": "Reviewed the configuration module; no defect found.",
    "files_changed": [],
    "verification": ["python -m unittest test_config passed 4/4"],
    "risks": [],
    "next_steps": [],
}

STDOUT_SCHEMA_ANSWER = stream(
    step_start(),
    text_event("Reading the configuration module first."),
    read_event(FILE_READ_OUTPUT),
    step_finish("tool-calls"),
    step_start(),
    text_event("Review complete. " + json.dumps(HANDOFF_SCHEMA)),
    step_finish("stop"),
)

STDOUT_PROSE_ANSWER = stream(
    step_start(),
    read_event(FILE_READ_OUTPUT),
    step_finish("tool-calls"),
    step_start(),
    text_event("I inspected the module and found no defect worth reporting."),
    step_finish("stop"),
)

STDOUT_VALID_ANSWER = stream(
    step_start(),
    read_event(FILE_READ_OUTPUT),
    step_finish("tool-calls"),
    step_start(),
    text_event(json.dumps(VALID_HANDOFF)),
    step_finish("stop"),
)

STDOUT_NO_ANSWER = stream(
    step_start(),
    text_event("Reading the configuration module first."),
    read_event(FILE_READ_OUTPUT),
    step_finish("tool-calls"),
)

# Shape of the MiMo 0.1.15 INFO log at process end (measured 2026-10-17/18 on
# stack runs): the checkpoint-writer subagent is still streaming when the
# instance is disposed and the session processor logs "Aborted process".
STDERR_SHUTDOWN_ABORT = "\n".join(
    [
        "INFO  2026-10-18T00:21:41 +0ms service=server-proxy version=0.1.15 args=[\"run\"]",
        "INFO  2026-10-18T00:21:41 +2ms service=session.processor session.id=" + SESSION
        + " messageID=" + MESSAGE + " process",
        "INFO  2026-10-18T00:21:41 +1ms service=llm providerID=xiaomi modelID=mimo-v2.6-pro "
        "session.id=" + SESSION + " small=false agent=checkpoint-writer mode=subagent stream",
        "WARN  2026-10-18T00:21:48 +6486ms service=session.checkpoint drained=0 timedOut=1 "
        "drain timed out, writers still pending",
        "INFO  2026-10-18T00:21:48 +0ms service=server-proxy directory=D:/ws disposing instance",
        "INFO  2026-10-18T00:21:48 +3ms service=bus type=* unsubscribing",
        "ERROR 2026-10-18T00:21:48 +1ms service=session.processor session.id=" + SESSION
        + " messageID=" + MESSAGE + " error=Aborted process",
        "INFO  2026-10-18T00:21:48 +16ms service=config path=C:/mimocode.json loading",
        "WARN  2026-10-18T00:21:48 +6ms service=session.checkpoint sessionID=" + SESSION
        + " status=cancelled checkpoint writer did not succeed",
    ]
) + "\n"

STDERR_OTHER_ABORT = STDERR_SHUTDOWN_ABORT + (
    "ERROR 2026-10-18T00:21:49 +1ms service=session.processor session.id=" + SESSION
    + " error=Run aborted: provider closed the stream\n"
)

STDERR_CLEAN = "\n".join(
    line for line in STDERR_SHUTDOWN_ABORT.splitlines() if "Aborted" not in line
) + "\n"


def provider_failure(stdout: str, stderr: str, adapter_id: str = "mimo") -> str | None:
    return fleet.provider_failure_reason(
        adapter_id=adapter_id, stdout=stdout, stderr=stderr, exit_code=0
    )


class ShutdownAbortTests(unittest.TestCase):
    def test_shutdown_abort_after_completed_answer_is_not_a_provider_failure(self) -> None:
        for stdout in (STDOUT_SCHEMA_ANSWER, STDOUT_PROSE_ANSWER, STDOUT_VALID_ANSWER):
            with self.subTest(answer=stdout[-80:]):
                self.assertIsNone(provider_failure(stdout, STDERR_SHUTDOWN_ABORT))

    def test_helper_recognises_only_the_exact_shutdown_line(self) -> None:
        self.assertTrue(
            mimo_runtime.mimo_shutdown_abort_only(STDOUT_SCHEMA_ANSWER, STDERR_SHUTDOWN_ABORT)
        )
        self.assertFalse(
            mimo_runtime.mimo_shutdown_abort_only(STDOUT_SCHEMA_ANSWER, STDERR_OTHER_ABORT)
        )
        self.assertFalse(mimo_runtime.mimo_shutdown_abort_only(STDOUT_SCHEMA_ANSWER, STDERR_CLEAN))
        self.assertFalse(mimo_runtime.mimo_shutdown_abort_only(STDOUT_NO_ANSWER, STDERR_SHUTDOWN_ABORT))
        self.assertFalse(mimo_runtime.mimo_shutdown_abort_only("", STDERR_SHUTDOWN_ABORT))
        error_stream = stream(
            {"type": "error", "error": {"name": "ProviderError", "message": "insufficient credits"}}
        )
        self.assertFalse(mimo_runtime.mimo_shutdown_abort_only(error_stream, STDERR_SHUTDOWN_ABORT))
        self.assertFalse(mimo_runtime.mimo_shutdown_abort_only(None, STDERR_SHUTDOWN_ABORT))
        self.assertFalse(mimo_runtime.mimo_shutdown_abort_only(STDOUT_SCHEMA_ANSWER, None))

    def test_abort_without_completed_answer_keeps_the_marker(self) -> None:
        reason = provider_failure(STDOUT_NO_ANSWER, STDERR_SHUTDOWN_ABORT)
        self.assertIsNotNone(reason)
        self.assertIn("PROVIDER FAILURE", reason)
        self.assertIn("stderr reported 'aborted'", reason)
        empty = provider_failure("", STDERR_SHUTDOWN_ABORT)
        self.assertIn("the provider produced no stdout", empty)
        self.assertIn("stderr reported 'aborted'", empty)

    def test_other_abort_wording_keeps_the_marker(self) -> None:
        reason = provider_failure(STDOUT_SCHEMA_ANSWER, STDERR_OTHER_ABORT)
        self.assertIsNotNone(reason)
        self.assertIn("stderr reported 'aborted'", reason)

    def test_other_adapters_keep_the_marker(self) -> None:
        for adapter_id in ("codebuddy", "generic"):
            with self.subTest(adapter=adapter_id):
                reason = provider_failure("", "run aborted\n", adapter_id)
                self.assertIsNotNone(reason)
                self.assertIn("stderr reported 'aborted'", reason)
        # Even a MiMo-shaped shutdown log on another adapter keeps the marker:
        # the exemption is bound to the MiMo native stream.
        reason = provider_failure(STDOUT_SCHEMA_ANSWER, STDERR_SHUTDOWN_ABORT, "codebuddy")
        self.assertIsNotNone(reason)
        self.assertIn("stderr reported 'aborted'", reason)


class AssistantTextDiagnosisTests(unittest.TestCase):
    def _diagnose(self, stdout: str) -> str | None:
        projection = fleet.project_provider_handoff("mimo", stdout)
        text, raw = fleet.handoff_diagnosis_text(projection, stdout)
        return fleet.handoff_semantic_error(
            projection.get("handoff"),
            mode="review",
            kind="review",
            source_text=text,
            provider_failure=provider_failure(stdout, STDERR_SHUTDOWN_ABORT),
            raw_source=raw,
        )

    def test_projection_exposes_the_judged_candidate_text(self) -> None:
        projection = fleet.project_provider_handoff("mimo", STDOUT_SCHEMA_ANSWER)
        self.assertIsNone(projection["handoff"])
        self.assertIn("candidate_text", projection)
        self.assertEqual(
            projection["candidate_text"], mimo_runtime.mimo_terminal_text(STDOUT_SCHEMA_ANSWER)
        )
        self.assertNotIn("29:", projection["candidate_text"])
        text, raw = fleet.handoff_diagnosis_text(projection, STDOUT_SCHEMA_ANSWER)
        self.assertFalse(raw)
        self.assertEqual(text, projection["candidate_text"])

    def test_schema_answer_is_named_instead_of_tool_output(self) -> None:
        error = self._diagnose(STDOUT_SCHEMA_ANSWER)
        self.assertIsNotNone(error)
        self.assertTrue(error.startswith("SCHEMA RETURNED INSTEAD OF DATA"), error)
        self.assertNotIn("PROVIDER FAILURE", error)
        self.assertNotIn("MALFORMED JSON", error)
        self.assertNotIn("29:", error)

    def test_prose_answer_is_a_missing_candidate_not_malformed_tool_output(self) -> None:
        error = self._diagnose(STDOUT_PROSE_ANSWER)
        self.assertIsNotNone(error)
        self.assertTrue(error.startswith("NO HANDOFF CANDIDATE"), error)
        self.assertNotIn("MALFORMED JSON", error)
        self.assertNotIn("PROVIDER FAILURE", error)

    def test_valid_handoff_with_shutdown_abort_is_accepted(self) -> None:
        projection = fleet.project_provider_handoff("mimo", STDOUT_VALID_ANSWER)
        self.assertIsNotNone(projection["handoff"])
        self.assertEqual(projection["handoff"]["status"], "completed")
        self.assertEqual(projection["source"], "mimo.assistant-text")
        self.assertIsNone(self._diagnose(STDOUT_VALID_ANSWER))

    def test_error_event_keeps_its_native_error(self) -> None:
        error_stream = stream(
            {"type": "error", "error": {"name": "ProviderError", "message": "insufficient credits"}}
        )
        projection = fleet.project_provider_handoff("mimo", error_stream)
        self.assertIsNone(projection["handoff"])
        self.assertEqual(projection["terminal_reason"], "failed")
        self.assertEqual(projection["error"], "insufficient credits")
        reason = provider_failure(error_stream, STDERR_SHUTDOWN_ABORT)
        self.assertIsNotNone(reason)
        self.assertIn("stderr reported 'aborted'", reason)

    def test_native_error_projection_still_exposes_the_judged_text(self) -> None:
        # Measured 2026-10-18 on a stack run: a native APIError (SSE read timed
        # out) after two reads was diagnosed from the raw transcript as
        # "MALFORMED JSON ... context: '{ 40: ...'" over a file the model read.
        stdout = stream(
            step_start(),
            text_event("Reading the module first."),
            read_event(FILE_READ_OUTPUT),
            {"type": "error", "error": {"name": "APIError",
                                        "data": {"message": "SSE read timed out"}}},
        )
        projection = fleet.project_provider_handoff("mimo", stdout)
        self.assertIsNone(projection["handoff"])
        self.assertEqual(projection["terminal_reason"], "failed")
        self.assertEqual(projection["error"], "SSE read timed out")
        self.assertEqual(projection["candidate_text"], mimo_runtime.mimo_terminal_text(stdout))
        text, raw = fleet.handoff_diagnosis_text(projection, stdout)
        self.assertFalse(raw)
        self.assertNotIn("29:", text)
        error = fleet.handoff_semantic_error(
            None, mode="review", kind="review", source_text=text,
            provider_failure=provider_failure(stdout, STDERR_SHUTDOWN_ABORT), raw_source=raw,
        )
        self.assertNotIn("29:", error)
        self.assertNotIn("MALFORMED JSON", error)


if __name__ == "__main__":
    unittest.main()
