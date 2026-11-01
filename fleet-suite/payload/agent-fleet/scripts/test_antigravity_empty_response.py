import json
import sys
import unittest
from pathlib import Path


sys.path.insert(0, str(Path(__file__).parent))

import fleet


def sample_handoff() -> dict[str, object]:
    return {
        "status": "completed",
        "summary": "Antigravity returned a terminal handoff.",
        "files_changed": [],
        "verification": ["Synthetic response parsed."],
        "risks": [],
        "next_steps": [],
    }


def result_stream(**result: object) -> str:
    return json.dumps({"event": "result", "result": result})


class AntigravityEmptyResponseTests(unittest.TestCase):
    def test_empty_response_rejects_schema_echo_without_validating_it(self) -> None:
        schema_echo = {
            "type": "object",
            "properties": {
                "status": {"type": "string"},
                "summary": {"type": "string"},
                "files_changed": {"type": "array"},
                "verification": {"type": "array"},
                "risks": {"type": "array"},
                "next_steps": {"type": "array"},
            },
        }
        for response in ("", "  \r\n\t"):
            validator_calls: list[dict[str, object]] = []

            def validator(candidate: dict[str, object]) -> str | None:
                validator_calls.append(candidate)
                return None

            with self.subTest(response=repr(response)):
                projected = fleet.project_provider_handoff(
                    "antigravity",
                    result_stream(
                        status="SUCCESS",
                        response=response,
                        json_schema=schema_echo,
                    ),
                    handoff_validator=validator,
                )

                self.assertIsNone(projected["handoff"])
                self.assertEqual(projected["reason"], "EMPTY_RESPONSE")
                self.assertIn("EMPTY_RESPONSE", projected["error"])
                self.assertIn("EMPTY_RESPONSE", projected["validation_error"])
                self.assertEqual(validator_calls, [])

    def test_valid_response_is_the_only_candidate_validated(self) -> None:
        handoff = sample_handoff()
        schema_echo = {
            "status": {"type": "string"},
            "summary": {"type": "string"},
            "files_changed": {"type": "array"},
            "verification": {"type": "array"},
            "risks": {"type": "array"},
            "next_steps": {"type": "array"},
        }
        validator_calls: list[dict[str, object]] = []

        def validator(candidate: dict[str, object]) -> str | None:
            validator_calls.append(candidate)
            return None

        projected = fleet.project_provider_handoff(
            "antigravity",
            result_stream(
                status="SUCCESS",
                response=json.dumps(handoff),
                json_schema=schema_echo,
            ),
            handoff_validator=validator,
        )

        self.assertEqual(projected["handoff"], handoff)
        self.assertEqual(projected["source"], "antigravity.result.response")
        self.assertEqual(projected["terminal_reason"], "success")
        self.assertIsNone(projected["error"])
        self.assertGreaterEqual(len(validator_calls), 1)
        self.assertTrue(all(candidate == handoff for candidate in validator_calls))
        self.assertNotIn(schema_echo, validator_calls)

    def test_valid_structured_output_survives_empty_response(self) -> None:
        handoff = sample_handoff()

        for response in ("", " \r\n\t", None, {}):
            with self.subTest(response=repr(response)):
                projected = fleet.project_provider_handoff(
                    "antigravity",
                    result_stream(
                        status="SUCCESS",
                        response=response,
                        structured_output=handoff,
                        json_schema={"type": "object"},
                    ),
                )

                self.assertEqual(projected["handoff"], handoff)
                self.assertEqual(
                    projected["source"], "antigravity.result.structured_output"
                )
                self.assertEqual(projected["terminal_reason"], "success")
                self.assertIsNone(projected["error"])

    def test_denied_action_names_are_in_empty_response_diagnosis(self) -> None:
        projected = fleet.project_provider_handoff(
            "antigravity",
            result_stream(
                status="SUCCESS",
                response="",
                json_schema={"type": "object"},
                denied_actions=[
                    {"action": "read_file", "display_name": "ViewFile"},
                    {"action": "write_file"},
                ],
            ),
        )

        self.assertEqual(projected["reason"], "EMPTY_RESPONSE")
        self.assertIn("EMPTY_RESPONSE", projected["error"])
        self.assertIn("read_file", projected["error"])
        self.assertIn("ViewFile", projected["error"])
        self.assertIn("write_file", projected["error"])


if __name__ == "__main__":
    unittest.main()
