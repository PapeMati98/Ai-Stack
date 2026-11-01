"""MiniMax's pinned native structured-output failure may omit its output field."""
from __future__ import annotations

import contextlib
import json
import os
import unittest
import uuid
from pathlib import Path
from unittest import mock

import fleet


EXIT = 4


def model() -> dict[str, str]:
    return {
        "providerId": "minimax",
        "modelId": "MiniMax-M3",
        "providerSource": "provider",
        "providerKind": "minimax-managed",
        "structuredOutputMode": "native_strict",
    }


def structured_failure(*, include_output: bool = False, output: object = None) -> dict[str, object]:
    value: dict[str, object] = {
        "schemaVersion": 1,
        "type": "exec.result",
        "status": "failed",
        "runId": "synthetic-run",
        "sessionId": "synthetic-session",
        "turnId": "synthetic-turn",
        "error": {
            "category": "runtime",
            "code": "STRUCTURED_OUTPUT_INVALID",
            "message": "Structured output was not valid JSON.",
            "retryable": True,
        },
        "model": model(),
        "usage": {"inputTokens": 10, "outputTokens": 5, "totalTokens": 15},
        "durationMs": 1,
    }
    if include_output:
        value["output"] = output
    return value


def limit_exceeded() -> dict[str, object]:
    return {
        "schemaVersion": 1,
        "type": "exec.result",
        "status": "limit_exceeded",
        "runId": "synthetic-run",
        "sessionId": "synthetic-session",
        "turnId": "synthetic-turn",
        "model": model(),
        "durationMs": 1,
    }


def run_execute_single(stdout: str, exit_code: int, *, timed_out: bool = False,
                       identity_status: str = "verified") -> dict[str, object]:
    profiles = fleet.load_profiles(fleet.DEFAULT_PROFILES)
    root = Path(os.environ["TEAM_TEST_ROOT"]) / ("minimax-omitted-" + uuid.uuid4().hex)
    root.mkdir()
    child = {
        "exit_code": exit_code,
        "timed_out": timed_out,
        "stdout": stdout,
        "stderr": "",
        "model_tool_calls": None,
        "tool_budget_exceeded": False,
    }
    identity = {
        "status": identity_status,
        "error": None if identity_status == "verified" else "synthetic identity missing",
    }
    values = (
        ("resolve_executable", "synthetic-mcode.exe"),
        ("probe_command", {"launchable": True, "exit_code": 0, "output": "ok"}),
        ("profile_runtime_environment", {}),
        ("profile_credential_status", {"ready": True}),
        ("merge_credential_readiness", {"ready": True, "reasons": []}),
        ("fleet_runtime_attestation", {"status": "VERIFIED"}),
        ("read_usage", []),
        ("build_command", (["synthetic-mcode"], "", None)),
        ("minimax_model_identity", identity),
    )
    with contextlib.ExitStack() as stack:
        stack.enter_context(mock.patch.dict(os.environ, {
            "AGENT_FLEET_DEPTH": "0",
            "AGENT_FLEET_OFFICE": "0",
            "AGENT_FLEET_BILLING_MODE_PATH": str(root / "missing.json"),
        }))
        for name, value in values:
            stack.enter_context(mock.patch.object(fleet, name, return_value=value))
        stack.enter_context(mock.patch.object(
            fleet.routing_v2, "readiness_from_probes", return_value={"ready": True, "reasons": []}
        ))
        stack.enter_context(mock.patch.object(
            fleet.routing_v2, "runtime_model_visible", return_value=(True, "visible")
        ))
        stack.enter_context(mock.patch.object(fleet.adaptive, "latest_quotas", return_value={}))
        stack.enter_context(mock.patch.object(
            fleet.quota_collectors, "collect_profile_quotas", return_value=({}, {})
        ))
        provider = stack.enter_context(mock.patch.object(
            fleet, "run_provider_child", return_value={"child": child, "provider_execution_seconds": 0}
        ))
        result = fleet.execute_single(
            "Synthetic MiniMax omitted-output diagnosis.",
            profiles=profiles,
            agent_id="minimax",
            mode="review",
            kind="review",
            complexity="low",
            model_override=None,
            workspace=root,
            timeout=5,
            allow_write=False,
            native_subagent_limit=0,
            enable_web=False,
            inherit_api_keys=False,
            dry_run=False,
            output_root=root / "evidence",
            root=root,
            knowledge_mode="none",
        )
    if provider.call_count != 1:
        raise AssertionError(f"provider called {provider.call_count} times")
    return result


class MiniMaxOmittedOutputDiagnosisTests(unittest.TestCase):
    def test_missing_and_null_output_are_recognized_only_under_exact_native_gates(self):
        for envelope in (structured_failure(), structured_failure(include_output=True)):
            with self.subTest(output="missing" if "output" not in envelope else "null"):
                text = json.dumps(envelope)
                reason = fleet.provider_failure_reason(
                    adapter_id="minimax", stdout=text, exit_code=EXIT
                )
                self.assertIn("MINIMAX STRUCTURED OUTPUT REJECTED", reason or "")
                self.assertEqual(
                    fleet.provider_transport_outcome(
                        "minimax", stdout=text, exit_code=EXIT, terminal_reason="failed", timed_out=False
                    ),
                    {"status": "failed", "reason": "minimax-structured-output-rejected"},
                )

        rejected = [
            (structured_failure(include_output=True, output="text"), EXIT),
            (structured_failure(include_output=True, output={}), EXIT),
            (structured_failure(), EXIT + 1),
        ]
        foreign_model = structured_failure()
        foreign_model["model"]["modelId"] = "foreign"  # type: ignore[index]
        rejected.append((foreign_model, EXIT))
        wrong_code = structured_failure()
        wrong_code["error"]["code"] = "RUNTIME_ERROR"  # type: ignore[index]
        rejected.append((wrong_code, EXIT))
        for envelope, code in rejected:
            with self.subTest(envelope=envelope, code=code):
                text = json.dumps(envelope)
                self.assertIsNone(fleet.minimax_structured_output_rejected(text, code))
                self.assertEqual(
                    fleet.provider_transport_outcome(
                        "minimax", stdout=text, exit_code=code, terminal_reason="failed", timed_out=False
                    )["reason"],
                    f"exit-code-{code}",
                )

        duplicate = json.dumps(structured_failure()).replace(
            '"status": "failed"', '"status": "failed", "status": "failed"'
        )
        self.assertIsNone(fleet.minimax_structured_output_rejected(duplicate, EXIT))
        self.assertIsNone(fleet.minimax_structured_output_rejected(
            "INFO " + json.dumps(structured_failure()), EXIT
        ))

    def test_execute_single_keeps_native_rejection_failed_without_promoting_identity(self):
        for envelope in (structured_failure(), structured_failure(include_output=True)):
            for identity_status in ("verified", "unavailable"):
                with self.subTest(output="missing" if "output" not in envelope else "null",
                                  identity=identity_status):
                    result = run_execute_single(
                        json.dumps(envelope), EXIT, identity_status=identity_status
                    )
                    self.assertEqual(result["status"], "FAILED")
                    self.assertIsNone(result["handoff"])
                    self.assertEqual(result["model_identity"]["status"], identity_status)
                    self.assertEqual(result["completion_reason"], "minimax-structured-output-rejected")
                    self.assertEqual(
                        result["transport_outcome"],
                        {"status": "failed", "reason": "minimax-structured-output-rejected"},
                    )
                    self.assertIn("MINIMAX STRUCTURED OUTPUT REJECTED", result["handoff_parse_error"])

    def test_timeout_and_native_step_budget_keep_higher_precedence(self):
        failed = json.dumps(structured_failure())
        timeout_reason = fleet.provider_failure_reason(
            adapter_id="minimax", stdout=failed, exit_code=EXIT, timed_out=True
        )
        self.assertIn("parent timeout", timeout_reason or "")
        self.assertNotIn("MINIMAX STRUCTURED OUTPUT REJECTED", timeout_reason or "")
        self.assertEqual(
            fleet.provider_transport_outcome(
                "minimax", stdout=failed, exit_code=EXIT, terminal_reason="failed", timed_out=True
            ),
            {"status": "partial", "reason": "parent-timeout"},
        )
        timed_out = run_execute_single(failed, EXIT, timed_out=True)
        self.assertEqual(timed_out["status"], "PARTIAL")
        self.assertEqual(timed_out["completion_reason"], "parent-timeout")
        self.assertIsNone(timed_out["handoff"])

        step_text = json.dumps(limit_exceeded())
        self.assertIn("MINIMAX STEP BUDGET EXHAUSTED", fleet.provider_failure_reason(
            adapter_id="minimax", stdout=step_text, exit_code=7
        ) or "")
        self.assertEqual(
            fleet.provider_transport_outcome(
                "minimax", stdout=step_text, exit_code=7, terminal_reason="limit_exceeded", timed_out=False
            ),
            {"status": "failed", "reason": "minimax-step-budget-exhausted"},
        )
        stepped = run_execute_single(step_text, 7)
        self.assertEqual(stepped["status"], "FAILED")
        self.assertEqual(stepped["completion_reason"], "minimax-step-budget-exhausted")
        self.assertIsNone(stepped["handoff"])


if __name__ == "__main__":
    unittest.main()
