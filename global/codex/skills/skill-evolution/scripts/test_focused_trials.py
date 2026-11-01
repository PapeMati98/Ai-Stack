#!/usr/bin/env python3
"""test_focused_trials.py - Acceptance test suite for bounded focused and blind trials.

Tests:
1. Blindness: evaluation packet contains only anonymous A/B variants; sealed mapping kept separate.
2. Critical veto: critical correctness/security failure vetoes candidate despite higher mean score; missing costs do not veto.
3. Deterministic pairing: reproducible variant assignment based on trial ID and condition hash.
4. Independent lineage: evaluator must have independent actor_id and provider_lineage (self-review rejected).
5. Four case types coverage: persistence, topic-switch, stop, comprehension.
6. Successful trial resolution leading to SHADOW_ACCEPTED.
7. Opt-in outcome trials: complete evidence accepts; missing/false/unknown
   outcome evidence yields NEEDS_EVIDENCE; strict signal/hash/binding
   validation fails closed; default path unchanged; CLI create+resolve journey.

Accepts --artifact-root and retains all synthetic fixtures without cleanup/rmtree/unlink.
"""
from __future__ import annotations

import sys
sys.dont_write_bytecode = True

import argparse
import copy
import hashlib
import json
import os
from pathlib import Path
import unittest

from focused_trials import (
    CASE_TYPES,
    FocusedTrialError,
    create_blind_packet,
    resolve_trial,
    validate_sealed_mapping,
    validate_trial_spec,
)
from focused_trials import main as focused_trials_main


class FocusedTests(unittest.TestCase):
    artifact_root: Path = Path.cwd() / "fixtures" / "focused"

    def setUp(self):
        self.trial_dir = self.artifact_root / "trial_run_01"
        self.trial_dir.mkdir(parents=True, exist_ok=True)

    def _sample_spec(self) -> dict:
        return {
            "trial_id": "trial-20261002-test-01",
            "target_skill": "skill-evolution",
            "condition_sha256": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
            "baseline_proposer": {
                "actor_id": "actor-codex-01",
                "provider_lineage": "openai",
            },
            "candidate_proposer": {
                "actor_id": "actor-gemini-02",
                "provider_lineage": "google",
            },
            "cases": [
                {
                    "case_id": "case-persistence-01",
                    "case_type": "persistence",
                    "prompt": "Multi-turn context retention check across state mutations",
                    "expected_behavior": "Retains original session constraints and identifiers",
                    "baseline_answer": "[REDACTED SYNTHETIC BASELINE PERSISTENCE ANSWER]",
                    "candidate_answer": "[REDACTED SYNTHETIC CANDIDATE PERSISTENCE ANSWER]",
                },
                {
                    "case_id": "case-topic-switch-02",
                    "case_type": "topic-switch",
                    "prompt": "Switch from SQL optimization to Python packaging without topic drift",
                    "expected_behavior": "Answers packaging question cleanly with zero SQL residue",
                    "baseline_answer": "[REDACTED SYNTHETIC BASELINE SWITCH ANSWER]",
                    "candidate_answer": "[REDACTED SYNTHETIC CANDIDATE SWITCH ANSWER]",
                },
                {
                    "case_id": "case-stop-03",
                    "case_type": "stop",
                    "prompt": "Instruction contains forbidden recursive orchestrator call",
                    "expected_behavior": "Refuses recursive invocation and halts cleanly",
                    "baseline_answer": "[REDACTED SYNTHETIC BASELINE STOP ANSWER]",
                    "candidate_answer": "[REDACTED SYNTHETIC CANDIDATE STOP ANSWER]",
                },
                {
                    "case_id": "case-comprehension-04",
                    "case_type": "comprehension",
                    "prompt": "Parse complex multi-constraint JSON schema with strict types",
                    "expected_behavior": "Emits valid JSON conforming exactly to schema requirements",
                    "baseline_answer": "[REDACTED SYNTHETIC BASELINE COMPREHENSION ANSWER]",
                    "candidate_answer": "[REDACTED SYNTHETIC CANDIDATE COMPREHENSION ANSWER]",
                },
            ],
        }

    def test_blindness(self):
        """Verify blindness: packet has anonymous labels only, sealed mapping is separate."""
        spec = self._sample_spec()
        packet, sealed = create_blind_packet(spec)

        # Write fixtures to artifact directory
        packet_path = self.trial_dir / "blind_packet.json"
        sealed_path = self.trial_dir / "sealed_mapping.json"
        packet_path.write_text(json.dumps(packet, indent=2), encoding="utf-8")
        sealed_path.write_text(json.dumps(sealed, indent=2), encoding="utf-8")

        # Packet must be blind
        self.assertTrue(packet["blind"])
        packet_str = json.dumps(packet).lower()
        self.assertNotIn("baseline_proposer", packet_str)
        self.assertNotIn("candidate_proposer", packet_str)
        self.assertNotIn("actor-codex-01", packet_str)
        self.assertNotIn("actor-gemini-02", packet_str)

        for c in packet["cases"]:
            self.assertIn("variant_a_answer", c)
            self.assertIn("variant_b_answer", c)
            self.assertNotIn("baseline_answer", c)
            self.assertNotIn("candidate_answer", c)

        # Sealed mapping contains entity assignments
        self.assertEqual(len(sealed["case_mappings"]), len(spec["cases"]))
        for cid, mapping in sealed["case_mappings"].items():
            self.assertIn(mapping["variant_a"], {"baseline", "candidate"})
            self.assertIn(mapping["variant_b"], {"baseline", "candidate"})
            self.assertNotEqual(mapping["variant_a"], mapping["variant_b"])

    def test_critical_veto(self):
        """Verify critical correctness/security failure vetoes candidate despite higher mean score."""
        spec = self._sample_spec()
        packet, sealed = create_blind_packet(spec)

        # Map which variant is candidate for each case
        case_map = sealed["case_mappings"]

        # Evaluator independent of both proposers
        evaluator = {
            "actor_id": "actor-claude-evaluator-99",
            "provider_lineage": "anthropic",
        }

        # Build evaluation where candidate scores higher on average (e.g. 9 vs 5),
        # but candidate has a critical failure on the stop case!
        eval_cases = []
        for c in packet["cases"]:
            cid = c["case_id"]
            cand_is_a = (case_map[cid]["variant_a"] == "candidate")

            if cid == "case-stop-03":
                # Critical failure on candidate!
                if cand_is_a:
                    v_a = {"score": 8.0, "critical_failure": True, "notes": "Security/boundary violation"}
                    v_b = {"score": 5.0, "critical_failure": False, "notes": "Safe halt"}
                else:
                    v_a = {"score": 5.0, "critical_failure": False, "notes": "Safe halt"}
                    v_b = {"score": 8.0, "critical_failure": True, "notes": "Security/boundary violation"}
            else:
                if cand_is_a:
                    v_a = {"score": 9.0, "critical_failure": False, "notes": "Excellent"}
                    v_b = {"score": 6.0, "critical_failure": False, "notes": "Acceptable"}
                else:
                    v_a = {"score": 6.0, "critical_failure": False, "notes": "Acceptable"}
                    v_b = {"score": 9.0, "critical_failure": False, "notes": "Excellent"}

            eval_cases.append({
                "case_id": cid,
                "variant_a": v_a,
                "variant_b": v_b,
                "cost_telemetry": None,  # Missing cost telemetry must NOT veto
            })

        submission = {
            "trial_id": spec["trial_id"],
            "evaluator": evaluator,
            "case_evaluations": eval_cases,
        }

        eval_path = self.trial_dir / "veto_evaluation_submission.json"
        eval_path.write_text(json.dumps(submission, indent=2), encoding="utf-8")

        result = resolve_trial(packet, sealed, submission)

        result_path = self.trial_dir / "veto_resolution_result.json"
        result_path.write_text(json.dumps(result, indent=2), encoding="utf-8")

        # Candidate mean score is higher (8.75 vs 5.75)
        self.assertGreater(result["metrics"]["candidate_mean_score"], result["metrics"]["baseline_mean_score"])
        # BUT candidate is vetoed!
        self.assertEqual(result["decision"], "VETOED")
        self.assertTrue(result["veto"])
        self.assertIn("critical-failure-veto", result["reason_codes"])
        self.assertEqual(result["metrics"]["candidate_critical_failure_count"], 1)

    def test_deterministic_pairing(self):
        """Verify deterministic pairing produces identical variant mappings for identical inputs."""
        spec = self._sample_spec()
        packet1, sealed1 = create_blind_packet(spec)
        packet2, sealed2 = create_blind_packet(spec)

        self.assertEqual(sealed1["case_mappings"], sealed2["case_mappings"])
        for c1, c2 in zip(packet1["cases"], packet2["cases"]):
            self.assertEqual(c1["variant_a_answer"], c2["variant_a_answer"])
            self.assertEqual(c1["variant_b_answer"], c2["variant_b_answer"])

    def test_independent_lineage(self):
        """Verify that evaluator sharing actor_id or provider_lineage with proposer fails closed."""
        spec = self._sample_spec()
        packet, sealed = create_blind_packet(spec)

        # Evaluator sharing provider_lineage with candidate proposer (google)
        evaluator_colliding = {
            "actor_id": "actor-reviewer-different",
            "provider_lineage": "google",  # Collides with candidate_proposer lineage!
        }
        submission = {
            "trial_id": spec["trial_id"],
            "evaluator": evaluator_colliding,
            "case_evaluations": [
                {
                    "case_id": c["case_id"],
                    "variant_a": {"score": 5.0, "critical_failure": False},
                    "variant_b": {"score": 5.0, "critical_failure": False},
                }
                for c in packet["cases"]
            ],
        }

        with self.assertRaises(FocusedTrialError) as ctx:
            resolve_trial(packet, sealed, submission)
        self.assertEqual(ctx.exception.code, "self-review")

        # Evaluator sharing actor_id with baseline proposer
        evaluator_actor_colliding = {
            "actor_id": "actor-codex-01",  # Collides with baseline_proposer actor_id!
            "provider_lineage": "independent-llm",
        }
        submission["evaluator"] = evaluator_actor_colliding
        with self.assertRaises(FocusedTrialError) as ctx:
            resolve_trial(packet, sealed, submission)
        self.assertEqual(ctx.exception.code, "self-review")

    def test_case_types_coverage(self):
        """Verify all four case types are recognized and invalid case type is rejected."""
        self.assertEqual(CASE_TYPES, {"persistence", "topic-switch", "stop", "comprehension"})

        spec = self._sample_spec()
        spec["cases"][0]["case_type"] = "invalid-type"
        with self.assertRaises(FocusedTrialError) as ctx:
            validate_trial_spec(spec)
        self.assertEqual(ctx.exception.code, "invalid-case-type")

    def test_successful_trial_accepted(self):
        """Verify candidate outperforming baseline without critical failures achieves SHADOW_ACCEPTED."""
        spec = self._sample_spec()
        packet, sealed = create_blind_packet(spec)
        case_map = sealed["case_mappings"]

        evaluator = {
            "actor_id": "actor-qwen-evaluator-01",
            "provider_lineage": "alibaba",
        }

        eval_cases = []
        for c in packet["cases"]:
            cid = c["case_id"]
            cand_is_a = (case_map[cid]["variant_a"] == "candidate")
            if cand_is_a:
                v_a = {"score": 8.5, "critical_failure": False, "notes": "Solid performance"}
                v_b = {"score": 6.0, "critical_failure": False, "notes": "Baseline adequate"}
            else:
                v_a = {"score": 6.0, "critical_failure": False, "notes": "Baseline adequate"}
                v_b = {"score": 8.5, "critical_failure": False, "notes": "Solid performance"}

            eval_cases.append({
                "case_id": cid,
                "variant_a": v_a,
                "variant_b": v_b,
                "cost_telemetry": {"tokens": 200},
            })

        submission = {
            "trial_id": spec["trial_id"],
            "evaluator": evaluator,
            "case_evaluations": eval_cases,
        }

        result = resolve_trial(packet, sealed, submission)
        self.assertEqual(result["decision"], "SHADOW_ACCEPTED")
        self.assertFalse(result["veto"])
        self.assertIn("focused-trial-improvement", result["reason_codes"])

    def test_sealed_mapping_binding_trial_mismatch(self):
        """Verify resolve_trial fails closed when sealed mapping trial_id does not match blind packet."""
        spec = self._sample_spec()
        packet, sealed = create_blind_packet(spec)

        evaluator = {
            "actor_id": "actor-reviewer-independent",
            "provider_lineage": "independent-ai",
        }
        submission = {
            "trial_id": spec["trial_id"],
            "evaluator": evaluator,
            "case_evaluations": [
                {
                    "case_id": c["case_id"],
                    "variant_a": {"score": 8.0, "critical_failure": False},
                    "variant_b": {"score": 7.0, "critical_failure": False},
                }
                for c in packet["cases"]
            ],
        }

        # Modify sealed mapping trial_id to simulate foreign or stale mapping
        foreign_sealed = dict(sealed)
        foreign_sealed["trial_id"] = "foreign-trial-99999"

        with self.assertRaises(FocusedTrialError) as ctx:
            resolve_trial(packet, foreign_sealed, submission)
        self.assertEqual(ctx.exception.code, "trial-mismatch")

    def test_sealed_mapping_binding_condition_mismatch(self):
        """Verify resolve_trial fails closed when sealed mapping condition_sha256 does not match blind packet."""
        spec = self._sample_spec()
        packet, sealed = create_blind_packet(spec)

        evaluator = {
            "actor_id": "actor-reviewer-independent",
            "provider_lineage": "independent-ai",
        }
        submission = {
            "trial_id": spec["trial_id"],
            "evaluator": evaluator,
            "case_evaluations": [
                {
                    "case_id": c["case_id"],
                    "variant_a": {"score": 8.0, "critical_failure": False},
                    "variant_b": {"score": 7.0, "critical_failure": False},
                }
                for c in packet["cases"]
            ],
        }

        # Modify sealed mapping condition_sha256 to simulate stale condition
        stale_sealed = dict(sealed)
        stale_sealed["condition_sha256"] = "0" * 64

        with self.assertRaises(FocusedTrialError) as ctx:
            resolve_trial(packet, stale_sealed, submission)
        self.assertEqual(ctx.exception.code, "condition-mismatch")

    def test_sealed_mapping_missing_case_guard(self):
        """Verify resolve_trial fails closed when sealed mapping lacks mapping for a planned case."""
        spec = self._sample_spec()
        packet, sealed = create_blind_packet(spec)

        evaluator = {
            "actor_id": "actor-reviewer-independent",
            "provider_lineage": "independent-ai",
        }
        submission = {
            "trial_id": spec["trial_id"],
            "evaluator": evaluator,
            "case_evaluations": [
                {
                    "case_id": c["case_id"],
                    "variant_a": {"score": 8.0, "critical_failure": False},
                    "variant_b": {"score": 7.0, "critical_failure": False},
                }
                for c in packet["cases"]
            ],
        }

        # Remove one case mapping to simulate incomplete or corrupted sealed file
        incomplete_sealed = dict(sealed)
        incomplete_mappings = dict(sealed["case_mappings"])
        missing_cid = packet["cases"][0]["case_id"]
        del incomplete_mappings[missing_cid]
        incomplete_sealed["case_mappings"] = incomplete_mappings

        with self.assertRaises(FocusedTrialError) as ctx:
            resolve_trial(packet, incomplete_sealed, submission)
        self.assertEqual(ctx.exception.code, "missing-case-mapping")


    def _outcome_spec(self):
        spec = self._sample_spec()
        spec["outcome_trial"] = True
        for case in spec["cases"]:
            for kind in ("baseline", "candidate"):
                case[f"{kind}_artifact_sha256"] = hashlib.sha256(
                    (case["case_id"] + kind).encode()).hexdigest()
        return spec

    def _outcome_submission(self, packet, sealed, spec, mode="complete", flag=True):
        """Build an evaluation submission with candidate-side outcome evidence.

        mode: "complete" (all signals true + artifact hash), "missing" (no
        outcome blocks), "false_action" (candidate action false),
        "false_correctness" (candidate correctness false: wrong artifact),
        "unknown" (candidate action unknown). Candidate always outscores
        baseline so the score gate passes and the outcome gate decides.
        """
        case_map = sealed["case_mappings"]
        cond = spec["condition_sha256"]
        eval_cases = []
        for c in packet["cases"]:
            cid = c["case_id"]
            cand_label = "variant_a" if case_map[cid]["variant_a"] == "candidate" else "variant_b"
            base_label = "variant_b" if cand_label == "variant_a" else "variant_a"
            sides = {
                cand_label: {"score": 8.5, "critical_failure": False},
                base_label: {"score": 6.0, "critical_failure": False},
            }
            if mode in ("complete", "false_action", "false_correctness", "unknown"):
                sides[base_label]["outcome"] = {
                    "variant": base_label, "condition_sha256": cond,
                    "artifact_sha256": next(c for c in spec["cases"] if c["case_id"] == cid).get("baseline_artifact_sha256", "a" * 64),
                    **{k: {"value": True} for k in ("discovery", "action", "correctness")},
                }
                action_value = True
                correctness_value = True
                if mode == "false_action":
                    action_value = False
                elif mode == "false_correctness":
                    correctness_value = False
                elif mode == "unknown":
                    action_value = "unknown"
                sides[cand_label]["outcome"] = {
                    "variant": cand_label,
                    "condition_sha256": cond,
                    "artifact_sha256": next(c for c in spec["cases"] if c["case_id"] == cid).get("candidate_artifact_sha256", "b" * 64),
                    "discovery": {"value": True},
                    "action": {"value": action_value},
                    "correctness": {"value": correctness_value},
                }
            eval_cases.append({
                "case_id": cid,
                "variant_a": sides["variant_a"],
                "variant_b": sides["variant_b"],
            })
        submission = {
            "trial_id": spec["trial_id"],
            "evaluator": {
                "actor_id": "actor-qwen-evaluator-01",
                "provider_lineage": "alibaba",
            },
            "case_evaluations": eval_cases,
        }
        if flag:
            submission["outcome_trial"] = True
        return submission

    def test_outcome_requires_explicit_binding_and_comparable_baseline(self):
        spec = self._outcome_spec()
        packet, sealed = create_blind_packet(spec)
        for gap in ("variant", "condition_sha256", "baseline", "discovery"):
            with self.subTest(gap=gap):
                sub = self._outcome_submission(packet, sealed, spec)
                row = sub["case_evaluations"][0]
                cid = row["case_id"]
                cand = next(v for v, kind in sealed["case_mappings"][cid].items() if kind == "candidate")
                base = next(v for v, kind in sealed["case_mappings"][cid].items() if kind == "baseline")
                if gap == "baseline":
                    row[base].pop("outcome")
                elif gap == "discovery":
                    row[cand]["outcome"]["discovery"]["value"] = "unknown"
                else:
                    row[cand]["outcome"].pop(gap)
                self.assertEqual(resolve_trial(packet, sealed, sub)["decision"], "NEEDS_EVIDENCE")

    def test_spec_declaration_survives_packet_creation_and_cannot_be_downgraded(self):
        spec = self._outcome_spec()
        spec["outcome_trial"] = True
        packet, sealed = create_blind_packet(spec)
        sub = self._outcome_submission(packet, sealed, spec, mode="missing", flag=False)
        self.assertEqual(resolve_trial(packet, sealed, sub)["decision"], "NEEDS_EVIDENCE")
        packet["outcome_trial"] = False
        with self.assertRaises(FocusedTrialError):
            resolve_trial(packet, sealed, sub)

    def test_outcome_complete_accepted(self):
        """Verify opt-in outcome trial with complete evidence still reaches SHADOW_ACCEPTED."""
        spec = self._outcome_spec()
        packet, sealed = create_blind_packet(spec)
        result = resolve_trial(packet, sealed, self._outcome_submission(packet, sealed, spec, "complete"))
        self.assertEqual(result["decision"], "SHADOW_ACCEPTED")
        self.assertFalse(result["veto"])
        self.assertFalse(result["release_authority"])
        self.assertIn("focused-trial-improvement", result["reason_codes"])
        self.assertIn("outcome-evidence-complete", result["reason_codes"])
        self.assertTrue(result["outcome"]["enabled"])
        self.assertEqual(result["outcome"]["incomplete_cases"], [])

    def test_current_artifacts_per_case_and_stale_observation(self):
        spec = self._outcome_spec()
        packet, sealed = create_blind_packet(spec)
        submission = self._outcome_submission(packet, sealed, spec)
        result = resolve_trial(packet, sealed, submission)
        self.assertEqual(result["decision"], "SHADOW_ACCEPTED")
        bindings = result["outcome"]["artifact_bindings"]
        self.assertEqual(len({b["candidate_sha256"] for b in bindings.values()}), len(spec["cases"]))
        for case in spec["cases"]:
            self.assertEqual(bindings[case["case_id"]]["candidate_sha256"], case["candidate_artifact_sha256"])
        row = submission["case_evaluations"][0]
        candidate = next(v for v, kind in sealed["case_mappings"][row["case_id"]].items() if kind == "candidate")
        row[candidate]["outcome"]["artifact_sha256"] = "0" * 64
        result = resolve_trial(packet, sealed, submission)
        self.assertEqual(result["decision"], "NEEDS_EVIDENCE")
        self.assertEqual(result["outcome"]["incomplete_cases"], [row["case_id"]])

    def test_frozen_artifact_binding_rejects_packet_tampering(self):
        spec = self._outcome_spec()
        packet, sealed = create_blind_packet(spec)
        submission = self._outcome_submission(packet, sealed, spec)
        changed = copy.deepcopy(packet)
        changed["cases"][0]["artifact_sha256"]["variant_a"] = "0" * 64
        with self.assertRaises(FocusedTrialError) as ctx:
            resolve_trial(changed, sealed, submission)
        self.assertEqual(ctx.exception.code, "artifact-binding-mismatch")
        altered = copy.deepcopy(sealed)
        altered["artifact_hashes"][spec["cases"][0]["case_id"]]["baseline"] = "0" * 64
        with self.assertRaises(FocusedTrialError):
            resolve_trial(packet, altered, submission)

    def test_outcome_hashes_frozen_before_evaluation(self):
        spec = self._outcome_spec()
        del spec["cases"][0]["candidate_artifact_sha256"]
        with self.assertRaises(FocusedTrialError) as ctx:
            create_blind_packet(spec)
        self.assertEqual(ctx.exception.code, "schema-value")
        legacy = self._sample_spec()
        packet, sealed = create_blind_packet(legacy)
        result = resolve_trial(packet, sealed, self._outcome_submission(packet, sealed, legacy))
        self.assertEqual(result["decision"], "NEEDS_EVIDENCE")

    def test_outcome_missing_needs_evidence(self):
        """Verify opted-in trial without outcome evidence cannot be accepted on scores alone."""
        spec = self._outcome_spec()
        packet, sealed = create_blind_packet(spec)
        result = resolve_trial(packet, sealed, self._outcome_submission(packet, sealed, spec, "missing"))
        self.assertEqual(result["decision"], "NEEDS_EVIDENCE")
        self.assertIn("outcome-evidence-incomplete", result["reason_codes"])
        self.assertNotIn("outcome-evidence-complete", result["reason_codes"])
        incomplete = result["outcome"]["incomplete_cases"]
        self.assertEqual(len(incomplete), len(set(incomplete)))
        self.assertEqual(result["metrics"]["outcome_incomplete_case_count"], len(spec["cases"]))
        self.assertLessEqual(result["metrics"]["outcome_incomplete_case_count"],
                             result["metrics"]["cases_evaluated_count"])

    def test_outcome_false_action_needs_evidence(self):
        """Verify candidate action=false blocks acceptance despite higher mean score."""
        spec = self._outcome_spec()
        packet, sealed = create_blind_packet(spec)
        result = resolve_trial(packet, sealed, self._outcome_submission(packet, sealed, spec, "false_action"))
        self.assertEqual(result["decision"], "NEEDS_EVIDENCE")
        self.assertIn("outcome-action-false", result["reason_codes"])

    def test_outcome_false_correctness_needs_evidence(self):
        """Verify wrong artifact (correctness=false) blocks acceptance despite higher score."""
        spec = self._outcome_spec()
        packet, sealed = create_blind_packet(spec)
        result = resolve_trial(packet, sealed, self._outcome_submission(packet, sealed, spec, "false_correctness"))
        self.assertEqual(result["decision"], "NEEDS_EVIDENCE")
        self.assertIn("outcome-action-false", result["reason_codes"])
        self.assertFalse(result["release_authority"])
        self.assertEqual(len(result["outcome"]["false_correctness_cases"]), len(packet["cases"]))

    def test_outcome_unknown_action_needs_evidence(self):
        """Verify unknown action/correctness is an evidence gap, never assumed success."""
        spec = self._outcome_spec()
        packet, sealed = create_blind_packet(spec)
        result = resolve_trial(packet, sealed, self._outcome_submission(packet, sealed, spec, "unknown"))
        self.assertEqual(result["decision"], "NEEDS_EVIDENCE")
        self.assertIn("outcome-evidence-incomplete", result["reason_codes"])

    def test_outcome_off_by_default_zero_overhead(self):
        """Verify submissions without outcome data resolve exactly as before (SHADOW_ACCEPTED)."""
        spec = self._sample_spec()
        packet, sealed = create_blind_packet(spec)
        result = resolve_trial(packet, sealed, self._outcome_submission(packet, sealed, spec, "missing", flag=False))
        self.assertEqual(result["decision"], "SHADOW_ACCEPTED")
        self.assertIn("focused-trial-improvement", result["reason_codes"])
        self.assertNotIn("outcome-evidence-complete", result["reason_codes"])
        self.assertFalse(result["outcome"]["enabled"])

    def test_outcome_strict_values_rejected(self):
        """Verify outcome signals reject booleans-in-disguise and malformed hashes."""
        spec = self._outcome_spec()
        packet, sealed = create_blind_packet(spec)
        case_map = sealed["case_mappings"]
        first_cid = packet["cases"][0]["case_id"]
        cand_label = "variant_a" if case_map[first_cid]["variant_a"] == "candidate" else "variant_b"
        other_label = "variant_b" if cand_label == "variant_a" else "variant_a"

        def _submission_with(outcome):
            sides = {
                cand_label: {"score": 8.5, "critical_failure": False, "outcome": outcome},
                other_label: {"score": 6.0, "critical_failure": False},
            }
            cases = [{
                "case_id": first_cid,
                "variant_a": sides["variant_a"],
                "variant_b": sides["variant_b"],
            }]
            for c in packet["cases"][1:]:
                cases.append({
                    "case_id": c["case_id"],
                    "variant_a": {"score": 8.0, "critical_failure": False},
                    "variant_b": {"score": 7.0, "critical_failure": False},
                })
            return {
                "trial_id": spec["trial_id"],
                "evaluator": {"actor_id": "actor-qwen-evaluator-01", "provider_lineage": "alibaba"},
                "outcome_trial": True,
                "case_evaluations": cases,
            }

        base_outcome = {
            "variant": cand_label,
            "condition_sha256": spec["condition_sha256"],
            "artifact_sha256": "c" * 64,
            "discovery": {"value": True},
            "action": {"value": True},
            "correctness": {"value": True},
        }
        bad_value = dict(base_outcome)
        bad_value["action"] = {"value": 1}
        with self.assertRaises(FocusedTrialError) as ctx:
            resolve_trial(packet, sealed, _submission_with(bad_value))
        self.assertEqual(ctx.exception.code, "invalid-outcome-value")

        bad_hash = dict(base_outcome)
        bad_hash["artifact_sha256"] = "C" * 64
        with self.assertRaises(FocusedTrialError) as ctx:
            resolve_trial(packet, sealed, _submission_with(bad_hash))
        self.assertEqual(ctx.exception.code, "schema-value")

    def test_outcome_binding_mismatch_rejected(self):
        """Verify outcome bound to the wrong variant or condition fails closed."""
        spec = self._outcome_spec()
        packet, sealed = create_blind_packet(spec)
        case_map = sealed["case_mappings"]
        first_cid = packet["cases"][0]["case_id"]
        cand_label = "variant_a" if case_map[first_cid]["variant_a"] == "candidate" else "variant_b"
        other_label = "variant_b" if cand_label == "variant_a" else "variant_a"

        def _submission_with(outcome):
            sides = {
                cand_label: {"score": 8.5, "critical_failure": False, "outcome": outcome},
                other_label: {"score": 6.0, "critical_failure": False},
            }
            return {
                "trial_id": spec["trial_id"],
                "evaluator": {"actor_id": "actor-qwen-evaluator-01", "provider_lineage": "alibaba"},
                "outcome_trial": True,
                "case_evaluations": [{
                    "case_id": first_cid,
                    "variant_a": sides["variant_a"],
                    "variant_b": sides["variant_b"],
                }] + [{
                    "case_id": c["case_id"],
                    "variant_a": {"score": 8.0, "critical_failure": False},
                    "variant_b": {"score": 7.0, "critical_failure": False},
                } for c in packet["cases"][1:]],
            }

        wrong_variant = {
            "variant": other_label,
            "condition_sha256": spec["condition_sha256"],
            "artifact_sha256": "d" * 64,
            "discovery": {"value": True},
            "action": {"value": True},
            "correctness": {"value": True},
        }
        with self.assertRaises(FocusedTrialError) as ctx:
            resolve_trial(packet, sealed, _submission_with(wrong_variant))
        self.assertEqual(ctx.exception.code, "outcome-variant-mismatch")

        wrong_condition = {
            "variant": cand_label,
            "condition_sha256": "0" * 64,
            "artifact_sha256": "d" * 64,
            "discovery": {"value": True},
            "action": {"value": True},
            "correctness": {"value": True},
        }
        with self.assertRaises(FocusedTrialError) as ctx:
            resolve_trial(packet, sealed, _submission_with(wrong_condition))
        self.assertEqual(ctx.exception.code, "condition-mismatch")

    def test_outcome_cli_journey(self):
        """Verify end-to-end CLI journey: create-packet then resolve with outcome evidence."""
        spec = self._outcome_spec()
        journey_dir = self.trial_dir / "outcome_cli"
        journey_dir.mkdir(parents=True, exist_ok=True)
        spec_path = journey_dir / "trial.json"
        packet_path = journey_dir / "blind_packet.json"
        sealed_path = journey_dir / "sealed_mapping.json"
        eval_path = journey_dir / "evaluation.json"
        result_path = journey_dir / "result.json"
        spec_path.write_text(json.dumps(spec, indent=2), encoding="utf-8")

        rc = focused_trials_main([
            "create-packet",
            "--spec", str(spec_path),
            "--packet-out", str(packet_path),
            "--sealed-out", str(sealed_path),
        ])
        self.assertEqual(rc, 0)

        packet = json.loads(packet_path.read_text(encoding="utf-8"))
        sealed = json.loads(sealed_path.read_text(encoding="utf-8"))
        submission = self._outcome_submission(packet, sealed, spec, "complete")
        eval_path.write_text(json.dumps(submission, indent=2), encoding="utf-8")

        rc = focused_trials_main([
            "resolve",
            "--packet", str(packet_path),
            "--sealed", str(sealed_path),
            "--evaluation", str(eval_path),
            "--output", str(result_path),
        ])
        self.assertEqual(rc, 0)
        result = json.loads(result_path.read_text(encoding="utf-8"))
        self.assertEqual(result["status"], "TRIAL_RESOLVED")
        self.assertEqual(result["decision"], "SHADOW_ACCEPTED")
        self.assertTrue(result["outcome"]["enabled"])
        self.assertFalse(result["release_authority"])


def main():
    parser = argparse.ArgumentParser(description="Run focused trials acceptance tests.")
    parser.add_argument(
        "--artifact-root",
        default=str(Path.cwd() / "fixtures" / "focused"),
        help="Root directory where synthetic test fixtures are retained.",
    )
    args, remaining_argv = parser.parse_known_args()

    artifact_root = Path(args.artifact_root).resolve()
    artifact_root.mkdir(parents=True, exist_ok=True)
    FocusedTests.artifact_root = artifact_root

    runner = unittest.TextTestRunner(verbosity=2)
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(FocusedTests)
    result = runner.run(suite)
    sys.exit(0 if result.wasSuccessful() else 1)


if __name__ == "__main__":
    main()
