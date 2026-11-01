"""Tests for HD-d5e011: Mistral evidence-only public handoff candidate preservation."""
from __future__ import annotations

import contextlib
import copy
import json
import os
from pathlib import Path
import tempfile
from typing import Any
import unittest
from unittest import mock

import fleet
import mistral_runtime


def finished_export(**updates) -> dict[str, Any]:
    value: dict[str, Any] = {
        "schema_version": 1,
        "vibe_version": "2.26.1",
        "outcome": "finished",
        "exit_code": 0,
        "stop_reason": None,
        "error": None,
        "usage": {
            "input_tokens": 120,
            "output_tokens": 50,
            "cached_input_tokens": 20,
            "total_tokens": 170,
        },
        "cost_usd": 0.05,
        "session_id": "82df39a9-d90f-4b9b-8e89-63a0a1a7b780",
    }
    value.update(updates)
    return value


def review_handoff(**updates) -> dict[str, Any]:
    value: dict[str, Any] = {
        "status": "completed",
        "summary": "Verified review complete: no regressions detected.",
        "files_changed": [],
        "verification": ["deterministic unit tests passed"],
        "risks": [],
        "next_steps": [],
    }
    value.update(updates)
    return value


def assistant_stdout(handoff_dict: dict[str, Any]) -> str:
    entry = {
        "type": "message",
        "role": "assistant",
        "generation_status": "completed",
        "content": [{"type": "text", "text": json.dumps(handoff_dict)}],
    }
    return json.dumps([entry])


class MistralPublicCandidateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.profiles = fleet.load_profiles(fleet.DEFAULT_PROFILES)

    def _execute_mocked_mistral(
        self,
        temp_root: Path,
        *,
        mode: str = "review",
        child_exit_code: int = 0,
        timed_out: bool = False,
        identity_status: str = "verified",
        export_data: dict[str, Any] | None = None,
        tool_telemetry: dict[str, Any] | None = None,
        handoff_dict: dict[str, Any] | None = None,
        tool_budget_exceeded: bool = True,
        bound_policy: dict[str, Any] | None = None,
    ) -> tuple[dict[str, Any], Any]:
        if export_data is None:
            export_data = finished_export()
        if handoff_dict is None:
            handoff_dict = review_handoff()
        if bound_policy is None:
            bound_policy = {
                "tools": ["fleet_read_file"],
                "mode": mode,
                "limits": {"max_tool_calls": 5},
                "run_dir": str(temp_root),
                "scope": ["."],
                "inputs": [],
            }

        stdout_payload = assistant_stdout(handoff_dict)
        child = {
            "exit_code": child_exit_code,
            "timed_out": timed_out,
            "stdout": stdout_payload,
            "stderr": "",
            "model_tool_calls": 5,
            "tool_budget_exceeded": tool_budget_exceeded,
        }
        identity = {
            "status": identity_status,
            "configured_status": "verified",
            "error": "synthetic identity mismatch" if identity_status != "verified" else None,
        }

        mock_tool_mod = mock.MagicMock()
        mock_tool_mod.read_telemetry.return_value = tool_telemetry

        patches = [
            mock.patch.dict(
                os.environ,
                {
                    "AGENT_FLEET_DEPTH": "0",
                    "AGENT_FLEET_OFFICE": "0",
                    "AGENT_FLEET_BILLING_MODE_PATH": str(temp_root / "missing.json"),
                },
            ),
            mock.patch.object(fleet, "resolve_executable", return_value="synthetic-vibe.exe"),
            mock.patch.object(fleet, "probe_command", return_value={"launchable": True, "exit_code": 0, "output": "ok"}),
            mock.patch.object(fleet, "profile_runtime_environment", return_value={}),
            mock.patch.object(fleet, "profile_credential_status", return_value={"ready": True}),
            mock.patch.object(fleet, "merge_credential_readiness", return_value={"ready": True, "reasons": []}),
            mock.patch.object(fleet.routing_v2, "readiness_from_probes", return_value={"ready": True, "reasons": []}),
            mock.patch.object(fleet.routing_v2, "runtime_model_visible", return_value=(True, "visible")),
            mock.patch.object(fleet, "fleet_runtime_attestation", return_value={"status": "VERIFIED"}),
            mock.patch.object(fleet, "read_usage", return_value=[]),
            mock.patch.object(fleet.adaptive, "latest_quotas", return_value={}),
            mock.patch.object(fleet.quota_collectors, "collect_profile_quotas", return_value=({}, {})),
            mock.patch.object(fleet, "build_command", return_value=(["synthetic-vibe"], "", None)),
            mock.patch.object(fleet, "prepare_execution_workspace", return_value=(temp_root, None, None)),
            mock.patch.object(mistral_runtime, "bind_tool_policy", return_value=bound_policy),
            mock.patch.object(mistral_runtime, "require_native_login", return_value={"ready": True, "reason": "synthetic"}),
            mock.patch.object(mistral_runtime, "prepare_mistral_run", return_value=temp_root),
            mock.patch.object(mistral_runtime, "harden_mistral_environment", return_value={}),
            mock.patch.object(
                mistral_runtime,
                "environment_attestation",
                return_value={
                    "adapter": "mistral",
                    "status": "PREPARED",
                    "tool_telemetry": tool_telemetry,
                },
            ),
            mock.patch.object(mistral_runtime, "tool_module", return_value=mock_tool_mod),
            mock.patch.object(mistral_runtime, "read_native_export", return_value=export_data),
            mock.patch.object(mistral_runtime, "read_model_witness", return_value={}),
            mock.patch.object(mistral_runtime, "mistral_model_identity", return_value=identity),
        ]

        with contextlib.ExitStack() as stack:
            for patcher in patches:
                stack.enter_context(patcher)
            provider = stack.enter_context(
                mock.patch.object(
                    fleet,
                    "run_provider_child",
                    return_value={"child": child, "provider_execution_seconds": 0.1},
                )
            )
            result = fleet.execute_single(
                "Synthetic native review task.",
                profiles=self.profiles,
                agent_id="mistral",
                mode=mode,
                kind="review",
                complexity="low",
                model_override=None,
                workspace=temp_root,
                timeout=10,
                allow_write=mode == "implement",
                native_subagent_limit=0,
                enable_web=False,
                inherit_api_keys=False,
                dry_run=False,
                output_root=temp_root / "evidence",
                root=temp_root,
                knowledge_mode="none",
            )
        return result, provider

    def test_positive_candidate_preserved_on_budget_denial(self) -> None:
        with tempfile.TemporaryDirectory() as temp_root_str:
            root = Path(temp_root_str)
            telemetry = {"calls": 5, "budget_exceeded": True}
            result, provider = self._execute_mocked_mistral(
                root,
                mode="review",
                child_exit_code=0,
                timed_out=False,
                identity_status="verified",
                export_data=finished_export(),
                tool_telemetry=telemetry,
                handoff_dict=review_handoff(),
                tool_budget_exceeded=True,
            )

            # Never retry: exactly 1 provider call
            self.assertEqual(provider.call_count, 1)

            # Main status remains failed / denied; never auto-promoted or auto-accepted
            self.assertEqual(result["status"], "FAILED")
            self.assertIsNone(result["handoff"])
            self.assertFalse(result["handoff_valid"])
            self.assertTrue(result["parent_acceptance_required"])
            self.assertIn("Mistral tool telemetry is missing or native tool budget was exhausted", result["handoff_parse_error"])

            # Evidence-only candidate is populated
            candidate = result["public_handoff_candidate"]
            self.assertIsNotNone(candidate)
            self.assertEqual(candidate["authority"], "evidence-only")
            self.assertEqual(candidate["original_handoff"]["status"], "completed")
            self.assertEqual(candidate["original_handoff"]["summary"], "Verified review complete: no regressions detected.")
            self.assertEqual(candidate["model_identity"]["status"], "verified")
            self.assertTrue(candidate["requires_explicit_parent_adoption"])
            self.assertIn("tool budget was exhausted", candidate["rejection_reason"])

            # Attempt 1 stdout path and hash verification
            expected_stdout_path = str((root / "evidence" / result["run_id"] / "attempt-1.stdout.txt").resolve())
            self.assertEqual(candidate["stdout_path"], expected_stdout_path)
            self.assertTrue(Path(candidate["stdout_path"]).is_file())
            raw_stdout = Path(candidate["stdout_path"]).read_text(encoding="utf-8")
            self.assertEqual(candidate["stdout_sha256"], fleet.sha256_text(raw_stdout))

            # Persisted record matches
            record = json.loads(Path(result["usage_record"]).read_text(encoding="utf-8"))
            self.assertEqual(record["run_status"], "FAILED")
            self.assertIsNone(record["handoff"])
            self.assertEqual(record["public_handoff_candidate"], candidate)

    def test_candidate_suppressed_on_negative_controls(self) -> None:
        valid_telemetry = {"calls": 5, "budget_exceeded": True}

        test_cases = [
            ("implement_mode", {"mode": "implement"}),
            ("advisory_mode", {"mode": "advisory"}),
            ("nonzero_exit", {"child_exit_code": 1}),
            ("timeout", {"timed_out": True}),
            ("identity_unavailable", {"identity_status": "unavailable"}),
            ("identity_mismatch", {"identity_status": "mismatch"}),
            ("missing_telemetry", {"tool_telemetry": None}),
            ("unfinished_export_outcome", {"export_data": finished_export(outcome="token_limit")}),
            ("export_error_present", {"export_data": finished_export(error={"code": "infrastructure_failure"})}),
            ("export_stop_reason_present", {"export_data": finished_export(stop_reason="interrupted")}),
            ("partial_handoff", {"handoff_dict": review_handoff(status="partial")}),
            ("failed_handoff", {"handoff_dict": review_handoff(status="failed")}),
            ("budget_not_exceeded", {"tool_budget_exceeded": False, "tool_telemetry": {"calls": 2, "budget_exceeded": False}}),
        ]

        for name, overrides in test_cases:
            with self.subTest(case=name), tempfile.TemporaryDirectory() as temp_root_str:
                root = Path(temp_root_str)
                kwargs: dict[str, Any] = {
                    "mode": "review",
                    "child_exit_code": 0,
                    "timed_out": False,
                    "identity_status": "verified",
                    "export_data": finished_export(),
                    "tool_telemetry": valid_telemetry,
                    "handoff_dict": review_handoff(),
                    "tool_budget_exceeded": True,
                }
                kwargs.update(overrides)
                result, provider = self._execute_mocked_mistral(root, **kwargs)

                self.assertIsNone(
                    result.get("public_handoff_candidate"),
                    f"Candidate must be None for negative control: {name}",
                )
                if name == "budget_not_exceeded":
                    self.assertIsNotNone(result["handoff"])
                else:
                    self.assertIsNone(result["handoff"])
                record = json.loads(Path(result["usage_record"]).read_text(encoding="utf-8"))
                self.assertIsNone(record.get("public_handoff_candidate"))


if __name__ == "__main__":
    unittest.main()
