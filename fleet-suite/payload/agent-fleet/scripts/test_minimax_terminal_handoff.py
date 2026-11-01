from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path


SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

import fleet  # noqa: E402


def valid_handoff(**overrides: object) -> dict[str, object]:
    handoff: dict[str, object] = {
        "status": "completed",
        "summary": "Synthetic MiniMax work completed.",
        "files_changed": ["synthetic/module.py"],
        "verification": ["Synthetic focused test passed."],
        "risks": [],
        "next_steps": [],
    }
    handoff.update(overrides)
    return handoff


def exec_result(output: str) -> str:
    return json.dumps(
        {
            "schemaVersion": 1,
            "type": "exec.result",
            "status": "succeeded",
            "model": {"providerId": "minimax", "modelId": "MiniMax-M3"},
            "output": output,
            "usage": {"inputTokens": 13, "outputTokens": 21, "totalTokens": 34},
            "durationMs": 55,
        }
    )


class MiniMaxTerminalHandoffTests(unittest.TestCase):
    def test_schema_type_echo_is_the_only_field_removed_before_validation(self) -> None:
        echoed = valid_handoff(type="object")
        decoded_output = (
            "Implementation complete. Preparing the final handoff.\n\n"
            + json.dumps(echoed)
        )
        envelope = exec_result(decoded_output)

        outer = json.loads(envelope)
        fragment = outer["output"][outer["output"].index("{") :]
        self.assertEqual(json.loads(fragment), echoed)

        projection = fleet.project_provider_handoff("minimax", envelope)

        self.assertIsNone(projection["error"])
        self.assertEqual(projection["terminal_reason"], "succeeded")
        self.assertEqual(projection["handoff"], valid_handoff())
        self.assertNotIn("type", projection["handoff"])
        self.assertEqual(
            projection["source"],
            "minimax.exec.result.output.recovered-schema-echo",
        )

    def test_schema_echo_recovery_does_not_hide_a_second_contract_defect(self) -> None:
        echoed = valid_handoff(type="object", unexpected="not allowed")
        projection = fleet.project_provider_handoff(
            "minimax", exec_result(json.dumps(echoed))
        )

        self.assertIsNone(projection["handoff"])
        self.assertIsNone(projection["source"])
        self.assertEqual(
            projection["error"],
            "MiniMax exec result has no valid terminal handoff",
        )

    def test_malformed_decoded_output_is_rejected_with_decoded_diagnosis(self) -> None:
        decoded_output = (
            '{"status":"completed","summary":"Reviewed boundary marked '
            '"pure no-I/O" and confirmed it.","files_changed":[],'
            '"verification":["Synthetic review."],"risks":[],"next_steps":[]}'
        )
        envelope = exec_result(decoded_output)

        with self.assertRaises(json.JSONDecodeError):
            json.loads(decoded_output)
        self.assertEqual(json.loads(envelope)["output"], decoded_output)

        projection = fleet.project_provider_handoff("minimax", envelope)

        self.assertIsNone(projection["handoff"])
        self.assertEqual(
            projection["error"],
            "MiniMax exec result has no valid terminal handoff",
        )
        self.assertEqual(projection["candidate_text"], decoded_output)

        category, diagnosis = fleet.classify_handoff_rejection(
            projection["candidate_text"]
        )
        self.assertEqual(category, "malformed-json")
        self.assertIsInstance(diagnosis, str)
        self.assertIn('"pure no-I/O"', diagnosis)
        self.assertNotIn(r'\"pure no-I/O\"', diagnosis)


if __name__ == "__main__":
    unittest.main(verbosity=2)
