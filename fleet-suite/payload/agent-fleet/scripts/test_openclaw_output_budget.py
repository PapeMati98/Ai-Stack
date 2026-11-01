"""HD90dab7: output-budget regression; factory maxTokens 8192, validator rejections intact."""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

import openclaw_runtime  # noqa: E402


def _model_of(config: dict) -> dict:
    return config["models"]["providers"][openclaw_runtime.OPENCLAW_LOCAL_PROVIDER]["models"][0]


class OpenClawOutputBudgetTests(unittest.TestCase):
    def test_output_cap_constant_is_8192_and_finite(self) -> None:
        self.assertEqual(openclaw_runtime.OPENCLAW_LOCAL_MAX_OUTPUT_TOKENS, 8192)
        self.assertGreater(openclaw_runtime.OPENCLAW_LOCAL_MAX_OUTPUT_TOKENS, 2048)

    def test_factory_max_tokens_8192_all_modes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            for mode in ("implement", "review", "advisory"):
                config = openclaw_runtime.build_run_config(mode=mode, workspace=workspace)
                model = _model_of(config)
                self.assertEqual(model["maxTokens"], 8192)
                self.assertEqual(
                    config["agents"]["defaults"]["model"]["primary"],
                    openclaw_runtime.OPENCLAW_LOCAL_PROVIDER_MODEL,
                )
                self.assertEqual(config["agents"]["defaults"]["model"]["fallbacks"], [])
                self.assertIs(model["reasoning"], True)
                self.assertEqual(model["id"], openclaw_runtime.OPENCLAW_LOCAL_SERVED_MODEL)

    def test_factory_context_bounds_and_defaults_unchanged(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            default = openclaw_runtime.build_run_config(mode="review", workspace=workspace)
            self.assertEqual(_model_of(default)["contextWindow"], 16384)
        self.assertEqual(openclaw_runtime.OPENCLAW_LOCAL_DEFAULT_CONTEXT_TOKENS, 16384)
        self.assertEqual(openclaw_runtime.OPENCLAW_LOCAL_MIN_CONTEXT_TOKENS, 16384)
        self.assertEqual(openclaw_runtime.OPENCLAW_LOCAL_MAX_CONTEXT_TOKENS, 524288)

    def test_tool_restrictions_unchanged(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            review = openclaw_runtime.build_run_config(mode="review", workspace=workspace)
            implement = openclaw_runtime.build_run_config(mode="implement", workspace=workspace)
            advisory = openclaw_runtime.build_run_config(mode="advisory", workspace=workspace)
        self.assertEqual(list(review["tools"]["allow"]), list(openclaw_runtime.REVIEW_ALLOWED_TOOLS))
        self.assertEqual(list(review["tools"]["deny"]), list(openclaw_runtime.REVIEW_DENIED_TOOLS))
        self.assertEqual(list(implement["tools"]["allow"]), list(openclaw_runtime.IMPLEMENT_ALLOWED_TOOLS))
        self.assertEqual(list(implement["tools"]["deny"]), list(openclaw_runtime.IMPLEMENT_DENIED_TOOLS))
        self.assertEqual(list(advisory["tools"]["allow"]), list(openclaw_runtime.REVIEW_ALLOWED_TOOLS))
        self.assertEqual(list(advisory["tools"]["deny"]), list(openclaw_runtime.REVIEW_DENIED_TOOLS))
        for config in (review, implement, advisory):
            openclaw_runtime.validate_run_config_payload(config)

    def test_validator_still_rejects_invented_root_keys(self) -> None:
        payload = openclaw_runtime.build_run_config(
            mode="review", workspace=Path(tempfile.gettempdir())
        )
        payload["schema_version"] = 1
        with self.assertRaisesRegex(
            openclaw_runtime.OpenClawRuntimeError, "unsupported root keys"
        ):
            openclaw_runtime.validate_run_config_payload(payload)

    def test_validator_still_rejects_foreign_tool_names(self) -> None:
        payload = openclaw_runtime.build_run_config(
            mode="review", workspace=Path(tempfile.gettempdir())
        )
        payload["tools"]["allow"] = ["Read"]
        with self.assertRaisesRegex(
            openclaw_runtime.OpenClawRuntimeError, "non-native tool names"
        ):
            openclaw_runtime.validate_run_config_payload(payload)

    def test_validator_still_rejects_overlapping_allow_and_deny(self) -> None:
        payload = openclaw_runtime.build_run_config(
            mode="review", workspace=Path(tempfile.gettempdir())
        )
        payload["tools"]["deny"].append("read")
        with self.assertRaisesRegex(
            openclaw_runtime.OpenClawRuntimeError, "same native tool"
        ):
            openclaw_runtime.validate_run_config_payload(payload)

    def test_truncated_tool_call_envelope_is_rejected_not_accepted(self) -> None:
        truncated = '{"ok": true, "status": "ok", "final": "do", "payloads": [{"text":'
        result = openclaw_runtime.envelope_to_result(truncated)
        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], "error")
        self.assertEqual(result["error"]["kind"], "invalid_envelope")
        self.assertTrue(result["usage_unknown"])

    def test_empty_stdout_envelope_is_rejected(self) -> None:
        result = openclaw_runtime.envelope_to_result("")
        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], "error")
        self.assertEqual(result["error"]["kind"], "empty_envelope")

    def test_non_ok_envelopes_stay_not_ok_without_retry(self) -> None:
        for status, kind in (("timeout", "timeout"), ("error", "fallback_exhausted")):
            envelope = json.dumps(
                {
                    "ok": False,
                    "status": status,
                    "final": "",
                    "payloads": [],
                    "model": "Qwen3.8-27B",
                    "provider": "vllm",
                    "sessionId": "11111111-1111-4111-8111-111111111111",
                    "error": {"message": "native failure", "kind": kind},
                }
            )
            result = openclaw_runtime.envelope_to_result(envelope)
            self.assertFalse(result["ok"])
            self.assertEqual(result["status"], status)
            self.assertEqual(result["error"]["kind"], kind)

    def test_ok_without_final_or_payload_is_rejected(self) -> None:
        envelope = json.dumps(
            {
                "ok": True,
                "status": "ok",
                "final": "",
                "payloads": [],
                "model": "Qwen3.8-27B",
                "provider": "vllm",
                "sessionId": "11111111-1111-4111-8111-111111111111",
            }
        )
        result = openclaw_runtime.envelope_to_result(envelope)
        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], "error")
        self.assertEqual(result["error"]["kind"], "empty_successful_envelope")


if __name__ == "__main__":
    unittest.main()
