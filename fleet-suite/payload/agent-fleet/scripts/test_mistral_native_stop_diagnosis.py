"""Every pinned native Vibe stop names its cause; nothing else gains the label (HD-20261013-80516b).

Vibe 2.26.1 ends a headless run with one RunOutcome and a fixed exit code (1 usage/config error,
2 infrastructure failure, 3 limits/refusal, 4 aborted) and writes mistral-output/export.json with
outcome + error. Fleet 0.9.73-0.9.84 read that export only for token_limit (0.9.78) and described
every other non-zero exit as "PROVIDER FAILURE ... exited with code N, no stdout" (process-exit-N).
"""
import contextlib
import copy
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import fleet
import mistral_runtime


_DEFAULT = object()


def export(outcome="infrastructure_failure", exit_code=2, error=_DEFAULT, **extra):
    if error is _DEFAULT:
        error = {"message": "synthetic native failure", "code": "synthetic"} if exit_code in (1, 2) else None
    value = {"schema_version": 1, "vibe_version": "2.26.1", "outcome": outcome,
             "exit_code": exit_code, "stop_reason": None, "error": error}
    value.update(extra)
    return value


API_ERROR = {"message": "Mistral API request timed out after 3 retries (HTTP 503)", "code": "api_timeout"}


class NativeStopClassificationTests(unittest.TestCase):
    def test_every_pinned_outcome_maps_to_its_exit_code(self):
        for outcome, code in mistral_runtime.MISTRAL_NATIVE_STOP_EXIT_CODES.items():
            rule = mistral_runtime.MISTRAL_NATIVE_STOP_ERROR_RULE[outcome]
            error = {"message": "synthetic", "code": "synthetic"} if rule == "required" else None
            with self.subTest(outcome=outcome):
                stop = mistral_runtime.mistral_native_stop(export(outcome, code, error=error), code)
                self.assertIsNotNone(stop)
                self.assertEqual(stop["outcome"], outcome)
                self.assertEqual(stop["reason"], "mistral-" + outcome.replace("_", "-"))
                self.assertEqual(stop["meaning"], mistral_runtime.MISTRAL_NATIVE_STOP_MEANING[outcome])
                # the fixed code of another outcome is refused
                other = next(c for c in (1, 2, 3, 4) if c != code)
                self.assertIsNone(mistral_runtime.mistral_native_stop(export(outcome, other, error=error), other))
                self.assertIsNone(mistral_runtime.mistral_native_stop(export(outcome, code, error=error), other))
                # the error rule is part of the pin: a combination Vibe never writes is not classified
                if rule == "required":
                    self.assertIsNone(mistral_runtime.mistral_native_stop(export(outcome, code, error=None), code))
                elif rule == "absent":
                    self.assertIsNone(mistral_runtime.mistral_native_stop(
                        export(outcome, code, error={"message": "x"}), code))
                else:
                    self.assertIsNotNone(mistral_runtime.mistral_native_stop(
                        export(outcome, code, error={"message": "x"}), code))

    def test_error_code_and_categories_never_carry_the_message(self):
        stop = mistral_runtime.mistral_native_stop(export(error=API_ERROR), 2)
        self.assertEqual(stop["error_code"], "api_timeout")
        self.assertTrue(stop["error_present"])
        self.assertIn("timed out", stop["error_categories"])
        self.assertIn("503", stop["error_categories"])
        self.assertIn("retries", stop["error_categories"])
        text = mistral_runtime.mistral_native_stop_diagnosis(stop)
        self.assertIn("MISTRAL NATIVE INFRASTRUCTURE FAILURE", text)
        self.assertIn("'api_timeout'", text)
        self.assertIn("timed out", text)
        self.assertNotIn("Mistral API request", text)
        self.assertNotIn("after 3 retries", text)
        withheld = mistral_runtime.mistral_native_stop_diagnosis(
            mistral_runtime.mistral_native_stop(export(error={"message": "secret-looking text sk-abc"}), 2))
        self.assertIn("withheld", withheld)
        self.assertNotIn("sk-abc", withheld)

    def test_stop_reason_is_only_the_fixed_turnstop_vocabulary(self):
        # Review round 2 (Codex): a forged export could carry the message in stop_reason; only Vibe's TurnStop
        # values pass, anything else is dropped (classification itself is unchanged).
        for value in ("interrupted", "limit", "length"):
            stop = mistral_runtime.mistral_native_stop(export("turn_limit", 3, stop_reason=value), 3)
            self.assertEqual(stop["stop_reason"], value)
            self.assertIn(f"Native stop reason '{value}'", mistral_runtime.mistral_native_stop_diagnosis(stop))
        for value in (API_ERROR["message"], "INTERRUPTED", "x" * 64, "", 5, ["limit"], {"limit": 1}):
            with self.subTest(value=value):
                stop = mistral_runtime.mistral_native_stop(export(error=API_ERROR, stop_reason=value), 2)
                self.assertIsNotNone(stop)
                self.assertIsNone(stop["stop_reason"])
                text = mistral_runtime.mistral_native_stop_diagnosis(stop)
                self.assertNotIn("stop reason", text)
                self.assertNotIn("Mistral API request", text)

    def test_unpinned_forged_or_zero_exit_stays_unclassified(self):
        values = [None, {}, "infrastructure_failure", [], export(exit_code=0), export(error=None)]
        for key, value in (("schema_version", 2), ("schema_version", True), ("vibe_version", "2.25.0"),
                           ("outcome", "finished"), ("outcome", "time_limit"), ("outcome", None),
                           ("exit_code", "2"), ("exit_code", True), ("error", "string"), ("error", []),
                           ("error", {"code": 5}), ("error", {"code": ""}), ("error", {"code": "x" * 65}),
                           ("error", {"code": "bad code!"})):
            modified = copy.deepcopy(export(error=API_ERROR))
            modified[key] = value
            values.append(modified)
        for value in values:
            with self.subTest(value=value):
                self.assertIsNone(mistral_runtime.mistral_native_stop(value, 2))
        self.assertIsNone(mistral_runtime.mistral_native_stop(export(exit_code=0, outcome="finished"), 0))
        self.assertIsNone(mistral_runtime.mistral_native_stop(export(), True))

    def test_missing_error_key_is_never_classified(self):
        # Review round 1 (Codex): export.get("error") read a missing key as "no error", so a token_limit export
        # without the key slipped past the 0.9.78 pin (which requires the key) into the new branch.
        for outcome, code in mistral_runtime.MISTRAL_NATIVE_STOP_EXIT_CODES.items():
            with self.subTest(outcome=outcome):
                value = export(outcome, code, error=None)
                del value["error"]
                self.assertIsNone(mistral_runtime.mistral_native_stop(value, code))
        token = export("token_limit", 3, error=None, stop_reason="interrupted")
        del token["error"]
        reason = fleet.provider_failure_reason(adapter_id="mistral", exit_code=3, native_export=token)
        self.assertIn("PROVIDER FAILURE", reason)
        self.assertNotIn("MISTRAL", reason)
        self.assertIn("pinned run contract", reason)
        self.assertEqual(fleet.provider_transport_outcome("mistral", exit_code=3, terminal_reason="failed",
            timed_out=False, native_export=token), {"status": "failed", "reason": "exit-code-3"})

    def test_absence_detail_distinguishes_missing_from_unpinned(self):
        self.assertIn("missing", mistral_runtime.mistral_export_absence_detail(None, 1))
        self.assertIn("pinned run contract", mistral_runtime.mistral_export_absence_detail({"schema_version": 1}, 1))
        self.assertIn("outside the pinned taxonomy",
                      mistral_runtime.mistral_export_absence_detail(export("finished", 1), 1))
        self.assertIsNone(mistral_runtime.mistral_export_absence_detail(None, 0))
        self.assertIsNone(mistral_runtime.mistral_export_absence_detail(None, "1"))


class NativeStopDiagnosisTests(unittest.TestCase):
    def test_infrastructure_failure_names_its_cause(self):
        reason = fleet.provider_failure_reason(adapter_id="mistral", exit_code=2, stdout="",
                                               native_export=export(error=API_ERROR))
        self.assertIn("MISTRAL NATIVE INFRASTRUCTURE FAILURE", reason)
        self.assertIn("exit 2", reason)
        self.assertNotIn("PROVIDER FAILURE", reason)
        self.assertNotIn("Mistral API request", reason)
        self.assertIn("MISTRAL NATIVE INFRASTRUCTURE FAILURE", fleet.identity_gate_error(reason, "identity missing"))
        for outcome, code, label in (("usage_error", 1, "USAGE ERROR"), ("config_error", 1, "CONFIG ERROR"),
                                     ("aborted", 4, "ABORTED"), ("turn_limit", 3, "TURN LIMIT"),
                                     ("refusal", 3, "REFUSAL"), ("deadline", 3, "DEADLINE")):
            error = {"message": "synthetic"} if code in (1, 4) else None
            with self.subTest(outcome=outcome):
                reason = fleet.provider_failure_reason(adapter_id="mistral", exit_code=code,
                                                       native_export=export(outcome, code, error=error))
                self.assertIn(f"MISTRAL NATIVE {label}", reason)
                self.assertNotIn("PROVIDER FAILURE", reason)

    def test_token_budget_branch_keeps_precedence_and_text(self):
        token = export("token_limit", 3, stop_reason="interrupted")
        reason = fleet.provider_failure_reason(adapter_id="mistral", exit_code=3, native_export=token)
        self.assertIn("MISTRAL TOKEN BUDGET EXHAUSTED", reason)
        self.assertNotIn("MISTRAL NATIVE", reason)
        self.assertEqual(fleet.provider_transport_outcome("mistral", exit_code=3, terminal_reason="failed",
            timed_out=False, native_export=token), {"status": "failed", "reason": "mistral-token-budget-exhausted"})
        # a token_limit export that carries an error is a combination Vibe does not produce: generic (0.9.78 pin)
        with_error = export("token_limit", 3, error={"code": "budget"})
        reason = fleet.provider_failure_reason(adapter_id="mistral", exit_code=3, native_export=with_error)
        self.assertIn("PROVIDER FAILURE", reason)
        self.assertNotIn("MISTRAL NATIVE", reason)
        self.assertIn("outside the pinned taxonomy", reason)

    def test_missing_export_is_named_in_the_generic_diagnosis(self):
        reason = fleet.provider_failure_reason(adapter_id="mistral", exit_code=1, stdout="", stderr="",
                                               native_export=None)
        self.assertIn("PROVIDER FAILURE", reason)
        self.assertIn("exited with code 1", reason)
        self.assertIn("export (mistral-output/export.json) is missing or unreadable", reason)
        self.assertIn("may have ended outside Vibe's own exit path", reason)
        unpinned = fleet.provider_failure_reason(adapter_id="mistral", exit_code=2, stdout="",
                                                 native_export={"schema_version": 1, "outcome": "infrastructure_failure"})
        self.assertIn("pinned run contract", unpinned)
        self.assertIn("PROVIDER FAILURE", unpinned)
        self.assertNotIn("export", fleet.provider_failure_reason(adapter_id="codex", exit_code=1, stdout="") or "")
        self.assertNotIn("export", fleet.provider_failure_reason(adapter_id="mistral", exit_code=0, stdout="") or "")

    def test_timeout_wins(self):
        self.assertIn("hit the parent timeout", fleet.provider_failure_reason(adapter_id="mistral", exit_code=2,
            timed_out=True, native_export=export(error=API_ERROR)))
        self.assertNotIn("export", fleet.provider_failure_reason(adapter_id="mistral", exit_code=1,
            timed_out=True, native_export=None))
        self.assertEqual(fleet.provider_transport_outcome("mistral", exit_code=2, terminal_reason="failed",
            timed_out=True, native_export=export()), {"status": "partial", "reason": "parent-timeout"})

    def test_transport_outcome_reason_and_status(self):
        for valid in (False, True):
            with self.subTest(valid=valid):
                self.assertEqual(fleet.provider_transport_outcome("mistral", exit_code=2, terminal_reason="failed",
                    timed_out=False, handoff_valid=valid, native_export=export()),
                    {"status": "failed", "reason": "mistral-infrastructure-failure"})
                self.assertEqual(fleet.provider_transport_outcome("mistral", exit_code=1, terminal_reason="failed",
                    timed_out=False, handoff_valid=valid, native_export=export("usage_error", 1)),
                    {"status": "failed", "reason": "mistral-usage-error"})
                self.assertEqual(fleet.provider_transport_outcome("mistral", exit_code=4, terminal_reason="failed",
                    timed_out=False, handoff_valid=valid, native_export=export("aborted", 4)),
                    {"status": "failed", "reason": "mistral-aborted"})
                self.assertEqual(fleet.provider_transport_outcome("mistral", exit_code=3, terminal_reason="failed",
                    timed_out=False, handoff_valid=valid, native_export=export("turn_limit", 3)),
                    {"status": "partial" if valid else "failed", "reason": "mistral-turn-limit"})
        self.assertEqual(fleet.provider_transport_outcome("mistral", exit_code=2, terminal_reason="failed",
            timed_out=False, native_export=None), {"status": "failed", "reason": "exit-code-2"})
        self.assertEqual(fleet.completed_run_status(2, None, adapter_id="mistral"), "FAILED")
        self.assertEqual(fleet.completed_run_status(2, {"status": "completed"}, adapter_id="mistral"), "FAILED")

    def test_foreign_adapters_and_legacy_calls_are_unchanged(self):
        for adapter in ("qwen", "codex", "grok", "minimax", None):
            with self.subTest(adapter=adapter):
                reason = fleet.provider_failure_reason(adapter_id=adapter, exit_code=2, native_export=export())
                self.assertIn("PROVIDER FAILURE", reason)
                self.assertNotIn("MISTRAL", reason)
                self.assertEqual(fleet.provider_transport_outcome(adapter or "codex", exit_code=2,
                    terminal_reason="failed", timed_out=False, native_export=export())["reason"], "exit-code-2")
        self.assertIn("PROVIDER FAILURE", fleet.provider_failure_reason(exit_code=2))
        self.assertEqual(mistral_runtime.MISTRAL_MIN_SESSION_TOKENS, 262144)
        self.assertEqual(mistral_runtime.MISTRAL_MAX_TOKENS, 1048576)
        self.assertEqual(mistral_runtime.mistral_terminal_status(export(error=API_ERROR))[0], "failed")


class ExecuteSingleTests(unittest.TestCase):
    def _run(self, native_export, exit_code, identity_status="verified"):
        profiles = fleet.load_profiles(fleet.DEFAULT_PROFILES)
        with tempfile.TemporaryDirectory() as temp_root:
            root = Path(temp_root)
            child = {"exit_code": exit_code, "timed_out": False, "stdout": "", "stderr": "",
                     "model_tool_calls": 0, "tool_budget_exceeded": False}
            identity = {"status": identity_status, "configured_status": "verified",
                        "error": "synthetic identity missing" if identity_status != "verified" else None}
            patches = [mock.patch.dict(os.environ, {"AGENT_FLEET_DEPTH": "0",
                "AGENT_FLEET_OFFICE": "0", "AGENT_FLEET_BILLING_MODE_PATH": str(root / "missing.json")})]
            patches.extend(mock.patch.object(obj, name, return_value=value) for obj, name, value in (
                (fleet, "resolve_executable", "synthetic-vibe.exe"),
                (fleet, "probe_command", {"launchable": True, "exit_code": 0, "output": "ok"}),
                (fleet, "profile_runtime_environment", {}),
                (fleet, "profile_credential_status", {"ready": True}),
                (fleet, "merge_credential_readiness", {"ready": True, "reasons": []}),
                (fleet.routing_v2, "readiness_from_probes", {"ready": True, "reasons": []}),
                (fleet.routing_v2, "runtime_model_visible", (True, "visible")),
                (fleet, "fleet_runtime_attestation", {"status": "VERIFIED"}),
                (fleet, "read_usage", []), (fleet.adaptive, "latest_quotas", {}),
                (fleet.quota_collectors, "collect_profile_quotas", ({}, {})),
                (fleet, "build_command", (["synthetic-vibe"], "", None)),
                (mistral_runtime, "bind_tool_policy", None),
                (mistral_runtime, "require_native_login", {"ready": True, "reason": "synthetic"}),
                (mistral_runtime, "prepare_mistral_run", root),
                (mistral_runtime, "harden_mistral_environment", {}),
                (mistral_runtime, "environment_attestation", {}),
                (mistral_runtime, "read_native_export", native_export),
                (mistral_runtime, "read_model_witness", {}),
                (mistral_runtime, "mistral_model_identity", identity),
            ))
            with contextlib.ExitStack() as stack:
                for patcher in patches:
                    stack.enter_context(patcher)
                provider = stack.enter_context(mock.patch.object(fleet, "run_provider_child",
                    return_value={"child": child, "provider_execution_seconds": 0}))
                result = fleet.execute_single("Synthetic native stop.", profiles=profiles,
                    agent_id="mistral", mode="review", kind="review", complexity="low",
                    model_override=None, workspace=root, timeout=5, allow_write=False,
                    native_subagent_limit=0, enable_web=False, inherit_api_keys=False, dry_run=False,
                    output_root=root / "evidence", root=root, knowledge_mode="none")
            record = json.loads(Path(result["usage_record"]).read_text(encoding="utf-8"))
        return provider.call_count, result, record

    def test_infrastructure_failure_is_recorded_without_retry_or_identity_promotion(self):
        for identity_status in ("verified", "unavailable", "mismatch"):
            with self.subTest(identity=identity_status):
                calls, result, record = self._run(export(error=API_ERROR), 2, identity_status)
                self.assertEqual(calls, 1)
                self.assertEqual(result["status"], "FAILED")
                self.assertIsNone(result["handoff"])
                self.assertEqual(result["model_identity"]["status"], identity_status)
                expected = {"status": "failed", "reason": "mistral-infrastructure-failure"}
                self.assertEqual(result["completion_reason"], "mistral-infrastructure-failure")
                self.assertEqual(result["transport_outcome"], expected)
                self.assertIn("MISTRAL NATIVE INFRASTRUCTURE FAILURE", result["handoff_parse_error"])
                self.assertNotIn("Mistral API request", json.dumps(record))
                self.assertEqual(len(result["provider_attempts"]), 1)
                attempt = result["provider_attempts"][0]
                self.assertEqual(attempt["transport_outcome"], expected)
                self.assertIn("MISTRAL NATIVE INFRASTRUCTURE FAILURE", attempt["handoff_error"])
                self.assertEqual(record["completion_reason"], "mistral-infrastructure-failure")
                self.assertEqual(record["run_status"], "FAILED")

    def test_forged_stop_reason_never_reaches_the_record(self):
        forged = export(error=API_ERROR, stop_reason=API_ERROR["message"])
        calls, result, record = self._run(forged, 2)
        self.assertEqual(calls, 1)
        self.assertEqual(result["completion_reason"], "mistral-infrastructure-failure")
        self.assertIn("MISTRAL NATIVE INFRASTRUCTURE FAILURE", result["handoff_parse_error"])
        self.assertNotIn("Mistral API request", result["handoff_parse_error"])
        self.assertNotIn("Mistral API request", result["provider_attempts"][0]["handoff_error"])
        self.assertNotIn("Mistral API request", json.dumps(record))
        self.assertNotIn("Mistral API request", json.dumps(result, default=str))

    def test_missing_error_key_keeps_generic_completion_reason(self):
        token = export("token_limit", 3, error=None, stop_reason="interrupted")
        del token["error"]
        calls, result, record = self._run(token, 3)
        self.assertEqual(calls, 1)
        self.assertEqual(result["status"], "FAILED")
        self.assertEqual(result["completion_reason"], "process-exit-3")
        self.assertEqual(result["transport_outcome"], {"status": "failed", "reason": "exit-code-3"})
        self.assertEqual(result["provider_attempts"][0]["transport_outcome"], {"status": "failed", "reason": "exit-code-3"})
        self.assertIn("PROVIDER FAILURE", result["handoff_parse_error"])
        self.assertIn("pinned run contract", result["handoff_parse_error"])
        self.assertEqual(record["completion_reason"], "process-exit-3")

    def test_missing_export_stays_generic_but_named(self):
        calls, result, record = self._run(None, 1)
        self.assertEqual(calls, 1)
        self.assertEqual(result["status"], "FAILED")
        self.assertEqual(result["completion_reason"], "process-exit-1")
        self.assertEqual(result["transport_outcome"], {"status": "failed", "reason": "exit-code-1"})
        self.assertIn("PROVIDER FAILURE", result["handoff_parse_error"])
        self.assertIn("export (mistral-output/export.json) is missing", result["handoff_parse_error"])
        self.assertIn("missing", result["provider_attempts"][0]["handoff_error"])
        self.assertEqual(record["completion_reason"], "process-exit-1")


if __name__ == "__main__":
    unittest.main()
