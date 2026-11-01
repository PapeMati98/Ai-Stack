from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path


SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

import fleet  # noqa: E402


def sample_handoff() -> dict[str, object]:
    return {
        "status": "completed",
        "summary": "Synthetic MiniMax envelope fixture.",
        "files_changed": [],
        "verification": ["Synthetic verification only."],
        "risks": [],
        "next_steps": [],
    }


def minimax_envelope(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "schemaVersion": 1,
        "type": "exec.result",
        "status": "succeeded",
        "model": {"providerId": "minimax", "modelId": "MiniMax-M3"},
        "output": json.dumps(sample_handoff()),
        "usage": {
            "inputTokens": 11,
            "outputTokens": 7,
            "totalTokens": 18,
        },
        "durationMs": 42,
    }
    payload.update(overrides)
    return payload


# Four-line MiniMax stdout shape measured 20260921T095729Z: envelope then MCP logs.
# Values are synthetic; nothing is copied from a live machine.
FOUR_LINE_STDOUT = "\n".join(
    [
        json.dumps(minimax_envelope()),
        "[11:58:58.101] WARN: [OFFICIAL] Plugin MCP tools/list synthetic-plugin",
        "[11:58:58.102] INFO: All MCP connections closed",
        "[11:58:58.113] WARN: MCP server transport close synthetic-plugin",
    ]
)


class FleetMiniMaxEnvelopeTests(unittest.TestCase):
    def test_four_line_envelope_plus_mcp_logs_is_not_failed(self) -> None:
        with self.assertRaises(json.JSONDecodeError) as caught:
            json.loads(FOUR_LINE_STDOUT.strip())
        self.assertIn("Extra data", str(caught.exception))

        projection = fleet.project_provider_handoff("minimax", FOUR_LINE_STDOUT)
        identity = fleet.minimax_model_identity(
            FOUR_LINE_STDOUT, expected_runtime_model="minimax/MiniMax-M3"
        )

        self.assertIsNone(projection["error"])
        self.assertEqual(projection["terminal_reason"], "succeeded")
        self.assertNotEqual(projection["terminal_reason"], "failed")
        self.assertNotEqual(str(projection.get("error") or ""), "FAILED")
        self.assertEqual(projection["handoff"], sample_handoff())
        self.assertEqual(identity["status"], "verified")
        self.assertEqual(identity["observed_runtime_models"], ["minimax/MiniMax-M3"])
        self.assertNotIn("Extra data", str(identity.get("error") or ""))

    def test_envelope_alone_without_logs_still_succeeds(self) -> None:
        text = json.dumps(minimax_envelope())
        projection = fleet.project_provider_handoff("minimax", text)
        identity = fleet.minimax_model_identity(
            text, expected_runtime_model="minimax/MiniMax-M3"
        )
        self.assertIsNone(projection["error"])
        self.assertEqual(projection["terminal_reason"], "succeeded")
        self.assertEqual(projection["handoff"], sample_handoff())
        self.assertEqual(identity["status"], "verified")
        self.assertEqual(identity["observed_runtime_models"], ["minimax/MiniMax-M3"])

    def test_text_starting_with_log_line_is_rejected(self) -> None:
        text = "\n".join(
            [
                "[11:58:58.101] WARN: [OFFICIAL] Plugin MCP tools/list {synthetic}",
                json.dumps(minimax_envelope()),
            ]
        )
        projection = fleet.project_provider_handoff("minimax", text)
        identity = fleet.minimax_model_identity(
            text, expected_runtime_model="minimax/MiniMax-M3"
        )
        self.assertIsNone(projection["handoff"])
        self.assertIsNotNone(projection["error"])
        self.assertIn("exec.result", str(projection["error"]))
        self.assertEqual(identity["status"], "unavailable")
        self.assertNotEqual(identity.get("observed_runtime_models"), ["minimax/MiniMax-M3"])

    def test_truncated_object_returns_readable_error_without_raising(self) -> None:
        text = (
            '{"schemaVersion":1,"type":"exec.result","status":"succeeded",'
            '"model":{"providerId":"minimax","modelId":"MiniMax-M3"}'
        )
        projection = fleet.project_provider_handoff("minimax", text)
        identity = fleet.minimax_model_identity(
            text, expected_runtime_model="minimax/MiniMax-M3"
        )
        self.assertIsNone(projection["handoff"])
        self.assertIsInstance(projection["error"], str)
        self.assertIn("truncated", str(projection["error"]).casefold())
        self.assertEqual(identity["status"], "unavailable")
        self.assertIn("truncated", str(identity.get("error") or "").casefold())

    def test_complete_object_with_other_type_is_rejected(self) -> None:
        text = json.dumps(
            minimax_envelope(type="exec.progress")
        )
        projection = fleet.project_provider_handoff("minimax", text)
        identity = fleet.minimax_model_identity(
            text, expected_runtime_model="minimax/MiniMax-M3"
        )
        self.assertIsNone(projection["handoff"])
        self.assertEqual(
            projection["error"], "MiniMax output is not an exec.result envelope"
        )
        self.assertEqual(identity["status"], "unavailable")
        self.assertEqual(
            identity["error"], "MiniMax output is not an exec.result envelope"
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
