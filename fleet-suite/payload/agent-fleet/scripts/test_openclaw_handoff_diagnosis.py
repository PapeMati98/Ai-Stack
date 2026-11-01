"""The OpenClaw --json envelope is transport, never the handoff candidate.

RSI HD-20261014-317ede (fleet-adapter:openclaw-local-qwen, vllm/Qwen3.8-27B). Nine runs on
0.9.74-0.9.79 ended with exit 0, an envelope {"ok": true, "status": "ok", "final": <prose>, ...}
and no JSON handoff in the assistant text. The generic projection found no candidate, the
diagnosis fell back to the raw transcript and judged the envelope itself, so the run record
said "HANDOFF CONTRACT MISMATCH ... status: expected one of blocked/completed/failed/partial,
got 'ok'" and pointed at a schema defect that never existed. Eight answers were short prose
(36-312 output tokens); one high-complexity review was cut at the pinned maxTokens output cap
(usage.output 2223 >= 2048, reasoning tokens count against it).

Every fixture below is synthetic. Nothing is copied from a live run record or a project log.
"""

from __future__ import annotations

import contextlib
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import fleet  # noqa: E402
import openclaw_runtime  # noqa: E402


PROSE = (
    "I've completed the review of the two synthetic files. The helper returns the "
    "expected value and the guard is called once; nothing else stands out."
)


def sample_handoff() -> dict[str, object]:
    return {
        "status": "completed",
        "summary": "Reviewed the bounded synthetic slice; no defects found.",
        "files_changed": [],
        "verification": ["read probe.txt -> one line as expected"],
        "risks": [],
        "next_steps": [],
    }


def envelope(
    *,
    final: str = PROSE,
    payloads: list[dict[str, object]] | None = None,
    output_tokens: int | None = 128,
    status: str = "ok",
    error: dict[str, str] | None = None,
) -> str:
    usage = (
        {"input": 11826, "output": output_tokens, "cacheRead": 0, "cacheWrite": 0,
         "total": 11826 + output_tokens, "cost": 0}
        if output_tokens is not None
        else None
    )
    body: dict[str, object] = {
        "ok": status == "ok",
        "status": status,
        "final": final,
        "payloads": payloads if payloads is not None else [{"text": final, "mediaUrl": None}],
        "usage": usage,
        "costUsd": 0,
        "codeModeEngaged": False,
        "assistantTurns": 2,
        "toolSummary": {"calls": 1, "tools": {"read": 1}, "failures": 0},
        "model": "Qwen3.8-27B",
        "provider": "vllm",
        "sessionId": "00000000-0000-4000-8000-000000000001",
    }
    if error is not None:
        body["error"] = error
    return json.dumps(body)


class OpenClawProjectionTests(unittest.TestCase):
    def test_prose_final_is_missing_handoff_not_envelope_contract_mismatch(self) -> None:
        projection = fleet.project_provider_handoff("openclaw", envelope())
        self.assertIsNone(projection["handoff"])
        self.assertEqual(projection["candidate_text"], PROSE + "\n" + PROSE)
        self.assertIsNone(projection["source"])
        self.assertTrue(projection["error"].startswith(fleet.OPENCLAW_FINAL_WITHOUT_HANDOFF))
        self.assertIn(f"({len(PROSE)} chars, 128 output tokens)", projection["error"])
        self.assertIn("answered in prose", projection["error"])
        self.assertNotIn("CONTRACT MISMATCH", projection["error"])
        self.assertNotIn("got 'ok'", projection["error"])
        self.assertNotIn("maxTokens", projection["error"])
        self.assertEqual(projection["no_candidate_detail"], projection["error"])

    def test_output_cap_is_named_when_usage_output_reaches_max_tokens(self) -> None:
        cap = openclaw_runtime.OPENCLAW_LOCAL_MAX_OUTPUT_TOKENS
        self.assertEqual(cap, 8192)
        projection = fleet.project_provider_handoff(
            "openclaw", envelope(final=PROSE * 40, output_tokens=cap + 175)
        )
        self.assertIsNone(projection["handoff"])
        self.assertIn(f"maxTokens={cap}", projection["error"])
        self.assertIn(f"{cap + 175} output tokens", projection["error"])
        self.assertNotIn("answered in prose", projection["error"])
        boundary = fleet.project_provider_handoff(
            "openclaw", envelope(output_tokens=cap)
        )
        self.assertIn(f"maxTokens={cap}", boundary["error"])
        below = fleet.project_provider_handoff(
            "openclaw", envelope(output_tokens=cap - 1)
        )
        self.assertNotIn("maxTokens", below["error"])

    def test_unknown_usage_still_names_the_missing_handoff(self) -> None:
        projection = fleet.project_provider_handoff(
            "openclaw", envelope(output_tokens=None)
        )
        self.assertTrue(projection["error"].startswith(fleet.OPENCLAW_FINAL_WITHOUT_HANDOFF))
        self.assertIn(f"{len(PROSE)} chars)", projection["error"])
        self.assertNotIn("output tokens", projection["error"])
        self.assertNotIn("maxTokens", projection["error"])

    def test_handoff_embedded_in_final_is_projected(self) -> None:
        handoff = sample_handoff()
        final = "Done. Final handoff:\n" + json.dumps(handoff) + "\n"
        projection = fleet.project_provider_handoff("openclaw", envelope(final=final))
        self.assertEqual(projection["handoff"], handoff)
        self.assertEqual(projection["source"], "openclaw.envelope-text")
        self.assertIsNone(projection["error"])
        self.assertIsNone(projection["no_candidate_detail"])
        self.assertIsNone(projection["validation_error"])
        self.assertIs(projection["provider_failure_leads"], True)

    def test_handoff_only_in_payload_text_is_projected(self) -> None:
        handoff = sample_handoff()
        projection = fleet.project_provider_handoff(
            "openclaw",
            envelope(final="", payloads=[{"text": json.dumps(handoff), "mediaUrl": None}]),
        )
        self.assertEqual(projection["handoff"], handoff)
        self.assertEqual(projection["source"], "openclaw.envelope-text")
        self.assertEqual(projection["candidate_text"], json.dumps(handoff))

    def test_candidate_text_keeps_final_and_every_payload_in_order(self) -> None:
        text = fleet.openclaw_envelope_candidate_text(
            {"final": "alpha", "payloads": [{"text": "alpha"}, {"text": "beta"}, {"text": " "}, "junk", {"text": None}]}
        )
        self.assertEqual(text, "alpha\nalpha\nbeta")
        self.assertEqual(fleet.openclaw_envelope_candidate_text({"final": "", "payloads": []}), "")

    def test_last_candidate_choice_matches_the_generic_scan(self) -> None:
        # Review finding P1: a later failed handoff must not lose to an earlier completed
        # one because a repeated payload text was dropped. Same choice as the generic scan.
        failed = dict(sample_handoff(), status="failed", summary="The check failed.",
                      next_steps=["Fix the probe."], verification=[])
        completed = sample_handoff()
        final = "Earlier draft:\n" + json.dumps(completed) + "\nFinal answer:\n" + json.dumps(failed)
        text = envelope(
            final=final,
            payloads=[{"text": json.dumps(completed), "mediaUrl": None}, {"text": final, "mediaUrl": None}],
        )
        projection = fleet.project_provider_handoff("openclaw", text)
        self.assertEqual(projection["handoff"]["status"], "failed")
        self.assertEqual(projection["handoff"], fleet.parse_handoff_with_metadata(text)[0])
        reversed_order = envelope(
            final=json.dumps(failed),
            payloads=[{"text": json.dumps(failed), "mediaUrl": None}, {"text": json.dumps(completed), "mediaUrl": None}],
        )
        again = fleet.project_provider_handoff("openclaw", reversed_order)
        self.assertEqual(again["handoff"]["status"], "completed")
        self.assertEqual(again["handoff"], fleet.parse_handoff_with_metadata(reversed_order)[0])

    def test_bool_or_non_int_usage_output_is_treated_as_unknown(self) -> None:
        for value in (True, "128", 12.5, None):
            with self.subTest(value=value):
                body = json.loads(envelope())
                body["usage"] = {"input": 10, "output": value}
                projection = fleet.project_provider_handoff("openclaw", json.dumps(body))
                self.assertTrue(projection["error"].startswith(fleet.OPENCLAW_FINAL_WITHOUT_HANDOFF))
                self.assertNotIn("output tokens", projection["error"])
                self.assertNotIn("maxTokens", projection["error"])

    def test_schema_breaking_model_object_is_reported_as_the_models_defect(self) -> None:
        broken = {"status": "done", "summary": "Looked at it.", "files_changed": [], "verification": ["x"],
                  "risks": [], "next_steps": []}
        projection = fleet.project_provider_handoff(
            "openclaw", envelope(final="Result:\n" + json.dumps(broken))
        )
        self.assertIsNone(projection["handoff"])
        diagnosis = fleet.diagnose_handoff_rejection(
            projection["candidate_text"], no_candidate_detail=projection.get("no_candidate_detail")
        )
        self.assertIn("got 'done'", diagnosis)
        self.assertNotIn("got 'ok'", diagnosis)

    def test_error_and_timeout_envelopes_report_the_envelope_status(self) -> None:
        failed = fleet.project_provider_handoff(
            "openclaw",
            envelope(status="error", final="", payloads=[],
                     error={"message": "provider refused the request", "kind": "provider_error"}),
        )
        self.assertIsNone(failed["handoff"])
        self.assertTrue(failed["error"].startswith("OPENCLAW ENVELOPE ERROR"))
        self.assertIn("provider refused the request", failed["error"])
        self.assertEqual(failed["candidate_text"], "")
        timed = fleet.project_provider_handoff(
            "openclaw", envelope(status="timeout", final="partial thoughts", payloads=[])
        )
        self.assertTrue(timed["error"].startswith("OPENCLAW ENVELOPE TIMEOUT"))
        self.assertEqual(timed["candidate_text"], "partial thoughts")
        self.assertEqual(timed["envelope_failure"], timed["error"])
        self.assertEqual(failed["envelope_failure"], failed["error"])
        cut = fleet.project_provider_handoff(
            "openclaw", envelope(status="timeout", final='Result:\n{"status": "completed", "summary": "cut', payloads=[])
        )
        self.assertTrue(cut["envelope_failure"].startswith("OPENCLAW ENVELOPE TIMEOUT"))
        self.assertIsNone(fleet.project_provider_handoff("openclaw", envelope()).get("envelope_failure"))
        ok_without_text = fleet.project_provider_handoff(
            "openclaw", envelope(final="", payloads=[])
        )
        self.assertTrue(ok_without_text["error"].startswith("OPENCLAW ENVELOPE ERROR"))
        self.assertIn("without a final text", ok_without_text["error"])

    def test_missing_or_invalid_envelope_keeps_the_raw_transcript_diagnosis(self) -> None:
        for text in ("", "   ", "Traceback (most recent call last): boom", "[1, 2]"):
            with self.subTest(text=text):
                projection = fleet.project_provider_handoff("openclaw", text)
                self.assertIsNone(projection["handoff"])
                self.assertNotIn("candidate_text", projection)
                self.assertTrue(projection["error"].startswith("OpenClaw envelope unavailable"))
                self.assertTrue(projection["no_candidate_detail"].startswith(fleet.OPENCLAW_ENVELOPE_MISSING))
        diagnosis_text, raw = fleet.handoff_diagnosis_text(
            fleet.project_provider_handoff("openclaw", "not json"), "not json"
        )
        self.assertEqual((diagnosis_text, raw), ("not json", True))

    def test_other_adapters_keep_the_generic_projection(self) -> None:
        handoff = sample_handoff()
        for adapter in ("kimi", "synthetic-generic"):
            with self.subTest(adapter=adapter):
                projection = fleet.project_provider_handoff(adapter, "note\n" + json.dumps(handoff))
                self.assertEqual(projection["handoff"], handoff)
                self.assertEqual(projection["source"], "generic-provider-output")
                self.assertNotIn("no_candidate_detail", projection)


class NoCandidateDetailDiagnosisTests(unittest.TestCase):
    def test_detail_replaces_the_size_only_sentence(self) -> None:
        self.assertEqual(
            fleet.diagnose_handoff_rejection(PROSE, no_candidate_detail="WHY: no answer"),
            "WHY: no answer",
        )
        self.assertEqual(
            fleet.diagnose_handoff_rejection(PROSE),
            fleet.no_candidate_message(PROSE),
        )
        self.assertTrue(fleet.diagnose_handoff_rejection(PROSE).startswith("NO HANDOFF CANDIDATE"))

    def test_provider_failure_leads_and_the_detail_follows(self) -> None:
        self.assertEqual(
            fleet.diagnose_handoff_rejection(
                PROSE, provider_failure="PROVIDER FAILURE: exit 1.", no_candidate_detail="WHY: x"
            ),
            "PROVIDER FAILURE: exit 1. Handoff diagnosis: WHY: x",
        )
        self.assertEqual(
            fleet.diagnose_handoff_rejection(PROSE, provider_failure="PROVIDER FAILURE: exit 1."),
            "PROVIDER FAILURE: exit 1.",
        )

    def test_detail_is_ignored_when_a_candidate_exists(self) -> None:
        broken = json.dumps({"status": "ok", "summary": "x", "files_changed": [], "verification": [],
                             "risks": [], "next_steps": []})
        diagnosis = fleet.diagnose_handoff_rejection(broken, no_candidate_detail="WHY: ignored")
        self.assertIn("CONTRACT MISMATCH", diagnosis)
        self.assertNotIn("WHY: ignored", diagnosis)

    def test_semantic_error_passes_the_detail_through(self) -> None:
        error = fleet.handoff_semantic_error(
            None, mode="review", kind="review", source_text=PROSE, no_candidate_detail="WHY: y"
        )
        self.assertEqual(error, "WHY: y")


class OpenClawRunConfigTests(unittest.TestCase):
    def test_run_config_pins_the_named_output_cap(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = openclaw_runtime.build_run_config(mode="review", workspace=Path(directory))
        model = config["models"]["providers"][openclaw_runtime.OPENCLAW_LOCAL_PROVIDER]["models"][0]
        self.assertEqual(model["maxTokens"], openclaw_runtime.OPENCLAW_LOCAL_MAX_OUTPUT_TOKENS)
        self.assertEqual(model["maxTokens"], 8192)
        self.assertTrue(model["reasoning"])


class OpenClawExecuteSingleTests(unittest.TestCase):
    """execute_single with a mocked provider child: the run record carries the new diagnosis."""

    def run_single(self, stdout: str, *, exit_code: int = 0, handoff_validator=None) -> dict:
        profiles = fleet.load_profiles(fleet.DEFAULT_PROFILES)
        calls: list[dict[str, object]] = []

        def run_provider_child(command, **kwargs):
            calls.append({"command": list(command), **kwargs})
            return {
                "child": {
                    "stdout": stdout,
                    "stderr": "",
                    "exit_code": exit_code,
                    "timed_out": False,
                    "model_tool_calls": None,
                    "tool_budget_exceeded": False,
                },
                "queue_wait_seconds": 0.0,
                "execution_serialized": False,
            }

        with tempfile.TemporaryDirectory() as temp_root:
            root = Path(temp_root)
            patches = [
                mock.patch.dict(
                    os.environ,
                    {
                        "AGENT_FLEET_DEPTH": "0",
                        "AGENT_FLEET_OFFICE": "0",
                        "AGENT_FLEET_BILLING_MODE_PATH": str(root / "missing-billing.json"),
                    },
                )
            ]
            patches.extend(
                mock.patch.object(obj, name, return_value=value)
                for obj, name, value in (
                    (fleet, "resolve_executable", str(openclaw_runtime.PINNED_OPENCLAW_EXECUTABLE)),
                    (fleet, "probe_command", {"launchable": True, "exit_code": 0, "output": "ok"}),
                    (fleet, "profile_runtime_environment", {}),
                    (fleet, "profile_credential_status", {"ready": True}),
                    (fleet, "merge_credential_readiness", {"ready": True, "reasons": []}),
                    (fleet.routing_v2, "readiness_from_probes", {"ready": True, "reasons": []}),
                    (fleet.routing_v2, "runtime_model_visible", (True, "visible")),
                    (fleet.routing_v2, "quota_hard_gates", []),
                    (fleet, "fleet_runtime_attestation", {"status": "VERIFIED"}),
                    (fleet, "local_runtime_status", {"routing_ready": True, "status": "READY", "runtime_not_ready": False}),
                    (fleet, "read_usage", []),
                    (fleet.adaptive, "latest_quotas", {}),
                    (fleet.quota_collectors, "collect_profile_quotas", ({}, {})),
                )
            )
            with contextlib.ExitStack() as stack:
                for patcher in patches:
                    stack.enter_context(patcher)
                stack.enter_context(mock.patch.object(fleet, "run_provider_child", run_provider_child))
                for name in ("require_openclaw_native_executable", "ensure_runtime"):
                    if hasattr(openclaw_runtime, name):
                        stack.enter_context(mock.patch.object(openclaw_runtime, name, return_value=None))
                if hasattr(openclaw_runtime, "runtime_status"):
                    stack.enter_context(
                        mock.patch.object(openclaw_runtime, "runtime_status", return_value={"status": "READY"})
                    )
                result = fleet.execute_single(
                    "Synthetic OpenClaw prose-answer fixture.",
                    profiles=profiles,
                    agent_id="openclaw-local-qwen",
                    mode="advisory",
                    kind="review",
                    complexity="low",
                    model_override=None,
                    workspace=root,
                    timeout=300,
                    allow_write=False,
                    native_subagent_limit=0,
                    enable_web=False,
                    inherit_api_keys=False,
                    dry_run=False,
                    output_root=root / "evidence",
                    root=root,
                    knowledge_mode="none",
                    handoff_validator=handoff_validator,
                )
        self.assertEqual(len(calls), 1, "no retry or continuation for a prose answer")
        return result

    def test_prose_answer_is_recorded_as_missing_handoff(self) -> None:
        result = self.run_single(envelope())
        self.assertEqual(result["status"], "FAILED")
        self.assertIsNone(result["handoff"])
        self.assertTrue(result["handoff_parse_error"].startswith(fleet.OPENCLAW_FINAL_WITHOUT_HANDOFF))
        self.assertNotIn("CONTRACT MISMATCH", result["handoff_parse_error"])
        self.assertNotIn("got 'ok'", result["handoff_parse_error"])
        self.assertEqual(result["model_identity"]["status"], "verified")
        self.assertEqual(result["completion_reason"], "process-exit-0")
        self.assertEqual(result["transport_outcome"], {"status": "complete", "reason": "process-exit-0"})
        attempt = result["provider_attempts"][0]
        self.assertTrue(attempt["handoff_error"].startswith(fleet.OPENCLAW_FINAL_WITHOUT_HANDOFF))
        self.assertEqual(result["token_usage"]["output_tokens"], 128)

    def test_handoff_in_final_still_completes(self) -> None:
        handoff = sample_handoff()
        result = self.run_single(envelope(final="Final:\n" + json.dumps(handoff)))
        self.assertEqual(result["status"], "COMPLETED")
        self.assertEqual(result["handoff"], handoff)
        self.assertEqual(result["handoff_projection_source"], "openclaw.envelope-text")
        self.assertIsNone(result["handoff_parse_error"])

    def test_validator_rejection_follows_a_leading_provider_failure(self) -> None:
        # Review finding P2: a stored validation_error must not hide the process failure.
        handoff = sample_handoff()

        def reject(value):
            return "synthetic validator rejection"

        failed = self.run_single(envelope(final="Final:\n" + json.dumps(handoff)), exit_code=1,
                                 handoff_validator=reject)
        self.assertEqual(failed["status"], "FAILED")
        self.assertIsNone(failed["handoff"])
        self.assertTrue(failed["handoff_parse_error"].startswith("PROVIDER FAILURE"))
        self.assertIn("Handoff diagnosis: synthetic validator rejection", failed["handoff_parse_error"])
        self.assertEqual(failed["completion_reason"], "process-exit-1")
        self.assertEqual(failed["transport_outcome"], {"status": "failed", "reason": "exit-code-1"})
        attempt = failed["provider_attempts"][0]
        self.assertTrue(attempt["handoff_error"].startswith("PROVIDER FAILURE"))
        self.assertIn("Handoff diagnosis: synthetic validator rejection", attempt["handoff_error"])
        clean = self.run_single(envelope(final="Final:\n" + json.dumps(handoff)), handoff_validator=reject)
        self.assertEqual(clean["status"], "FAILED")
        self.assertEqual(clean["handoff_parse_error"], "synthetic validator rejection")
        self.assertEqual(clean["provider_attempts"][0]["handoff_error"], "synthetic validator rejection")

    def test_non_ok_envelope_with_unclosed_json_keeps_the_envelope_status(self) -> None:
        # Review round 2 finding: the cut assistant text must not be re-classified as
        # MALFORMED JSON in provider_attempts[].handoff_error; the envelope status leads.
        cut = envelope(status="timeout", final='Result:\n{"status": "completed", "summary": "cut', payloads=[])
        clean = self.run_single(cut)
        self.assertEqual(clean["status"], "FAILED")
        self.assertIsNone(clean["handoff"])
        self.assertEqual(clean["completion_reason"], "process-exit-0")
        attempt_error = clean["provider_attempts"][0]["handoff_error"]
        self.assertTrue(attempt_error.startswith("OPENCLAW ENVELOPE TIMEOUT"), attempt_error)
        self.assertNotIn("MALFORMED JSON", attempt_error)
        self.assertNotIn("MALFORMED JSON", str(clean["handoff_parse_error"]))
        failed = self.run_single(cut, exit_code=1)
        self.assertEqual(failed["status"], "FAILED")
        attempt_error = failed["provider_attempts"][0]["handoff_error"]
        self.assertTrue(attempt_error.startswith("PROVIDER FAILURE"), attempt_error)
        self.assertIn("Handoff diagnosis: OPENCLAW ENVELOPE TIMEOUT", attempt_error)
        self.assertNotIn("MALFORMED JSON", attempt_error)
        self.assertEqual(failed["completion_reason"], "process-exit-1")
        errored = self.run_single(
            envelope(status="error", final='{"status": "failed", "summary": "cut', payloads=[],
                     error={"message": "provider refused the request", "kind": "provider_error"}),
        )
        attempt_error = errored["provider_attempts"][0]["handoff_error"]
        self.assertTrue(attempt_error.startswith("OPENCLAW ENVELOPE ERROR"), attempt_error)
        self.assertIn("provider refused the request", attempt_error)
        self.assertNotIn("MALFORMED JSON", attempt_error)

    def test_prose_answer_with_provider_failure_keeps_the_failure_first(self) -> None:
        result = self.run_single(envelope(), exit_code=1)
        self.assertEqual(result["status"], "FAILED")
        self.assertTrue(result["handoff_parse_error"].startswith("PROVIDER FAILURE"))
        self.assertIn("Handoff diagnosis: " + fleet.OPENCLAW_FINAL_WITHOUT_HANDOFF, result["handoff_parse_error"])


if __name__ == "__main__":
    unittest.main()
