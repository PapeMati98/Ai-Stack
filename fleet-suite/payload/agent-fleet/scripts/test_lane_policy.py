"""Deterministic C01-C14 matrix for the Agent Fleet adaptive lane policy.

Offline only: no provider calls, no readiness probes, no quota reads and no
runtime writes. Fleet-dependent checks prove CLI argv/dispatch compatibility
and the non-interactive Qwen adapter contract (prompt on stdin, no prompt in
argv, redacted persisted commands).
"""
import contextlib
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

import lane_policy
import fleet


VALID_SHA256 = "0" * 64
NO_ENV: dict = {}


def evaluate(payload, **kwargs):
    kwargs.setdefault("environ", NO_ENV)
    return lane_policy.evaluate(payload, **kwargs)


def evaluate_after_recon(payload, record, **kwargs):
    kwargs.setdefault("environ", NO_ENV)
    return lane_policy.evaluate_after_recon(payload, record, **kwargs)


def request(**changes):
    value = {
        "policy_version": 1,
        "requested_lane": None,
        "requested_model": "qwen3.8-max",
        "environment": "DEV",
        "external_effects": [],
        "hard_risk": {fact: False for fact in lane_policy.HARD_RISK_FACTS},
        "coordinated_scope": False,
        "delegation_benefit": False,
        "continuity_state": "FRESH",
        "continuity_durable_write": False,
        "d0_evidence": None,
        "recon": None,
        "signals": {
            "stale_continuity": False,
            "provenance_drift": False,
            "gate_regression": False,
        },
        "kill_switches": None,
        "provider_verification": None,
    }
    value.update(changes)
    return value


def d0(**changes):
    value = {
        "path": "continuity/d0-baseline.json",
        "sha256": VALID_SHA256,
        "run_id": "run-20260911T000000Z-0000000001",
        "parent_verified": True,
    }
    value.update(changes)
    return value


def recon(**changes):
    value = {
        "passes_used": 1,
        "lane_used": "compact-recon",
        "reads": ["git-log", "planning-contract"],
        "evidence_persistence": "ephemeral",
        "continuity_state_after": "FRESH",
        "receipt_sha256": VALID_SHA256,
        "revision": "rev-20260911",
    }
    value.update(changes)
    return value


def hard_risk(**facts):
    value = {fact: False for fact in lane_policy.HARD_RISK_FACTS}
    value.update(facts)
    return value


class C01LaneOrdinalTests(unittest.TestCase):
    def test_c01_ordinals_are_direct0_compact1_recon2_full3(self):
        self.assertEqual(
            lane_policy.LANE_ORDINALS,
            {"direct": 0, "compact": 1, "compact-recon": 2, "full": 3},
        )

    def test_c01_decision_exposes_stable_ordinal(self):
        decision = evaluate(request())
        self.assertEqual(decision["resolved_lane"], "direct")
        self.assertEqual(decision["resolved_lane_ordinal"], 0)


class C02HardRiskPrecedenceTests(unittest.TestCase):
    def test_c02_every_hard_risk_fact_forces_full(self):
        for fact in lane_policy.HARD_RISK_FACTS:
            with self.subTest(fact=fact):
                decision = evaluate(
                    request(
                        hard_risk=hard_risk(**{fact: True}),
                        delegation_benefit=False,
                    )
                )
                self.assertEqual(decision["resolved_lane"], "full")
                self.assertIn(f"HARD_RISK_{fact.upper()}", decision["reason_codes"])

    def test_c02_hard_risk_wins_before_continuity_and_scope(self):
        decision = evaluate(
            request(
                hard_risk=hard_risk(secrets_involved=True),
                continuity_state="MISSING",
                delegation_benefit=True,
            )
        )
        self.assertEqual(decision["status"], "ROUTED")
        self.assertEqual(decision["resolved_lane"], "full")
        self.assertIsNone(decision["recon_directive"])
        self.assertNotIn("LOCAL_CONTEXT_RECON_REQUIRED", decision["reason_codes"])

    def test_c02_hard_risk_cannot_be_de_escalated_by_requested_lane(self):
        decision = evaluate(
            request(
                hard_risk=hard_risk(material_decision=True),
                requested_lane="direct",
            )
        )
        self.assertEqual(decision["resolved_lane"], "full")
        self.assertIn("REQUESTED_LANE_DEESCALATION_REJECTED", decision["reason_codes"])


class C03ContinuityStateTests(unittest.TestCase):
    def test_c03_missing_stale_unknown_require_compact_recon(self):
        for state in ("MISSING", "STALE", "UNKNOWN"):
            with self.subTest(state=state):
                decision = evaluate(request(continuity_state=state))
                self.assertEqual(decision["status"], "RECON_REQUIRED")
                self.assertEqual(decision["resolved_lane"], "compact-recon")
                self.assertEqual(decision["resolved_lane_ordinal"], 2)
                self.assertIn("LOCAL_CONTEXT_RECON_REQUIRED", decision["reason_codes"])
                self.assertIsNotNone(decision["recon_directive"])

    def test_c03_fresh_continuity_does_not_trigger_recon(self):
        decision = evaluate(request(continuity_state="FRESH"))
        self.assertEqual(decision["status"], "ROUTED")
        self.assertEqual(decision["resolved_lane"], "direct")
        self.assertIsNone(decision["recon_directive"])

    def test_c03_durable_continuity_write_resolves_full(self):
        decision = evaluate(
            request(continuity_state="MISSING", continuity_durable_write=True)
        )
        self.assertEqual(decision["resolved_lane"], "full")
        self.assertIn(
            "DURABLE_CONTINUITY_WRITE_RESOLVES_FULL", decision["reason_codes"]
        )
        self.assertIsNone(decision["recon_directive"])


class C04ReconThenRecomputeTests(unittest.TestCase):
    def test_c04_exactly_one_separate_terminal_recon_then_recompute(self):
        first = evaluate(request(continuity_state="MISSING"))
        directive = first["recon_directive"]
        self.assertTrue(directive["terminal"])
        self.assertEqual(directive["max_passes"], 1)
        self.assertEqual(directive["same_pass_lanes_forbidden"], ["direct", "compact"])
        self.assertTrue(directive["recompute_lane_after"])
        self.assertEqual(first["authorization_granted"], False)

        second = evaluate_after_recon(
            request(
                continuity_state="MISSING",
                delegation_benefit=True,
                d0_evidence=d0(),
            ),
            recon(),
            verify_d0_file=lambda evidence: True,
            verify_recon_receipt=lambda record: True,
        )
        self.assertNotEqual(second["status"], "RECON_REQUIRED")
        self.assertEqual(second["resolved_lane"], "compact")
        self.assertIn("CONTINUITY_RESTORED_BY_RECON", second["reason_codes"])

    def test_c04_unresolved_continuity_after_recon_fails_closed_to_full(self):
        decision = evaluate_after_recon(
            request(continuity_state="STALE"),
            recon(continuity_state_after="STALE"),
        )
        self.assertEqual(decision["resolved_lane"], "full")
        self.assertIn(
            "CONTINUITY_UNRESOLVED_AFTER_RECON", decision["reason_codes"]
        )

    def test_c04_recon_may_not_run_as_direct_or_compact(self):
        for lane in ("direct", "compact"):
            with self.subTest(lane=lane):
                decision = evaluate_after_recon(
                    request(continuity_state="UNKNOWN"),
                    recon(lane_used=lane),
                )
                self.assertEqual(decision["resolved_lane"], "full")
                self.assertIn("RECON_LANE_VIOLATION", decision["reason_codes"])

    def test_c04_recon_record_without_trigger_state_is_refused(self):
        with self.assertRaises(lane_policy.LanePolicyError):
            evaluate(request(continuity_state="FRESH", recon=recon()))

    def test_c04_unverified_recon_receipt_fails_closed(self):
        decision = evaluate_after_recon(
            request(continuity_state="MISSING"),
            recon(),
        )
        self.assertEqual(decision["resolved_lane"], "full")
        self.assertIn("RECON_EVIDENCE_UNVERIFIED", decision["reason_codes"])


class C05D0EvidenceTests(unittest.TestCase):
    def test_c05_missing_d0_blocks_compact_with_d0_missing(self):
        decision = evaluate(request(delegation_benefit=True))
        self.assertEqual(decision["resolved_lane"], "full")
        self.assertIn("D0_MISSING", decision["reason_codes"])

    def test_c05_unverifiable_d0_resolves_full_with_d0_missing(self):
        variants = (
            d0(parent_verified=False),
            d0(sha256="not-a-digest"),
            d0(sha256="AB" * 32),
        )
        for evidence in variants:
            with self.subTest(sha=evidence["sha256"][:8]):
                decision = evaluate(
                    request(delegation_benefit=True, d0_evidence=evidence)
                )
                self.assertEqual(decision["resolved_lane"], "full")
                self.assertIn("D0_MISSING", decision["reason_codes"])

    def test_c05_parent_file_verification_failure_fails_closed(self):
        decision = evaluate(
            request(delegation_benefit=True, d0_evidence=d0()),
            verify_d0_file=lambda evidence: False,
        )
        self.assertEqual(decision["resolved_lane"], "full")
        self.assertIn("D0_MISSING", decision["reason_codes"])

    def test_c05_verified_d0_admits_compact_with_path_sha_and_run_id(self):
        decision = evaluate(
            request(delegation_benefit=True, d0_evidence=d0()),
            verify_d0_file=lambda evidence: True,
        )
        self.assertEqual(decision["resolved_lane"], "compact")
        self.assertIn("D0_VERIFIED", decision["reason_codes"])

    def test_c05_malformed_d0_shapes_are_refused(self):
        for evidence in (d0(path="   "), d0(run_id=""), {"path": "x"}):
            with self.assertRaises(lane_policy.LanePolicyError):
                evaluate(request(delegation_benefit=True, d0_evidence=evidence))


class C06RequestedLaneEscalationTests(unittest.TestCase):
    def test_c06_requested_lane_may_escalate(self):
        decision = evaluate(request(requested_lane="full"))
        self.assertEqual(decision["resolved_lane"], "full")
        self.assertIn("REQUESTED_LANE_ESCALATION", decision["reason_codes"])

    def test_c06_de_escalation_is_rejected_and_never_applied(self):
        decision = evaluate(
            request(
                hard_risk=hard_risk(restricted_data=True),
                requested_lane="compact",
            )
        )
        self.assertEqual(decision["resolved_lane"], "full")
        self.assertIn("REQUESTED_LANE_DEESCALATION_REJECTED", decision["reason_codes"])

    def test_c06_requested_lane_cannot_skip_the_recon_gate(self):
        decision = evaluate(
            request(continuity_state="MISSING", requested_lane="compact")
        )
        self.assertEqual(decision["status"], "RECON_REQUIRED")
        self.assertEqual(decision["resolved_lane"], "compact-recon")
        self.assertIn("REQUESTED_LANE_DEESCALATION_REJECTED", decision["reason_codes"])

    def test_c06_escalation_into_compact_still_requires_d0(self):
        decision = evaluate(
            request(requested_lane="compact", delegation_benefit=False)
        )
        self.assertEqual(decision["resolved_lane"], "full")
        self.assertIn("D0_MISSING", decision["reason_codes"])

    def test_c06_unknown_requested_lane_is_refused(self):
        with self.assertRaises(lane_policy.LanePolicyError):
            evaluate(request(requested_lane="express"))


class C07NoExceptionOrWaiverTests(unittest.TestCase):
    def test_c07_lane_exception_and_waiver_inputs_are_refused(self):
        for key in ("lane_exception", "waiver"):
            with self.subTest(key=key):
                payload = request()
                payload[key] = {"allow": True}
                with self.assertRaises(lane_policy.LanePolicyError):
                    evaluate(payload)

    def test_c07_no_decision_field_grants_exceptions_or_authority(self):
        decision = evaluate(request(requested_lane="full"))
        serialized = lane_policy.canonical_json(decision)
        self.assertNotIn("lane_exception", serialized)
        self.assertNotIn("waiver", serialized)
        self.assertFalse(decision["authorization_granted"])
        self.assertFalse(decision["node_acceptance_is_system_acceptance"])
        self.assertEqual(decision["writes_performed"], 0)
        self.assertEqual(decision["provider_calls"], 0)


class C08ReconAllowlistAndEphemeralEvidenceTests(unittest.TestCase):
    def test_c08_reads_outside_the_allowlist_fail_closed(self):
        decision = evaluate_after_recon(
            request(continuity_state="MISSING"),
            recon(reads=["git-log", "secrets-store"]),
        )
        self.assertEqual(decision["resolved_lane"], "full")
        self.assertIn("RECON_ALLOWLIST_VIOLATION", decision["reason_codes"])

    def test_c08_durable_recon_evidence_fails_closed(self):
        decision = evaluate_after_recon(
            request(continuity_state="MISSING"),
            recon(evidence_persistence="durable"),
        )
        self.assertEqual(decision["resolved_lane"], "full")
        self.assertIn("RECON_EVIDENCE_NOT_EPHEMERAL", decision["reason_codes"])

    def test_c08_second_recon_pass_is_refused(self):
        decision = evaluate_after_recon(
            request(continuity_state="MISSING"),
            recon(passes_used=2),
        )
        self.assertEqual(decision["resolved_lane"], "full")
        self.assertIn("RECON_PASS_LIMIT_EXCEEDED", decision["reason_codes"])

    def test_c08_directive_pins_allowlist_and_ephemeral_evidence(self):
        directive = lane_policy.recon_directive()
        self.assertEqual(
            directive["read_allowlist"], list(lane_policy.RECON_READ_ALLOWLIST)
        )
        self.assertEqual(directive["evidence_persistence"], "ephemeral")
        self.assertTrue(directive["durable_writes_forbidden"])
        self.assertEqual(directive["lane"], "compact-recon")


class C09ModelAliasTests(unittest.TestCase):
    def test_c09_exact_public_alias_resolves(self):
        decision = evaluate(request())
        self.assertEqual(decision["requested_model"], "qwen3.8-max")
        self.assertEqual(decision["resolved_model"], "qwen3.8-max")
        self.assertIn("MODEL_ALIAS_RESOLVED", decision["reason_codes"])

    def test_c09_dated_alias_is_never_publicly_requestable(self):
        decision = evaluate(request(requested_model="qwen3.8-max-0902"))
        self.assertEqual(decision["status"], "BLOCKED")
        self.assertIn("MODEL_ALIAS_UNRESOLVED", decision["reason_codes"])
        self.assertIsNone(decision["resolved_model"])
        self.assertNotIn(
            "qwen3.8-max-0902", lane_policy.canonical_json(decision)
        )

    def test_c09_wrong_case_or_foreign_alias_blocks_without_echo(self):
        for alias in ("Qwen3.8-Max", "qwen3.8-flash", "glm-5.2", " qwen3.8-max"):
            with self.subTest(alias=alias):
                decision = evaluate(request(requested_model=alias))
                self.assertEqual(decision["status"], "BLOCKED")
                self.assertIn("MODEL_ALIAS_UNRESOLVED", decision["reason_codes"])
                self.assertNotIn(alias.strip(), lane_policy.canonical_json(decision))
                self.assertEqual(decision["resolved_lane"], "full")

    def test_c09_verified_dated_snapshot_is_provenance_only(self):
        decision = evaluate(
            request(
                provider_verification={
                    "verified": True,
                    "served_model": "qwen3.8-max-0902",
                }
            ),
            verify_provider=lambda verification: True,
        )
        self.assertEqual(decision["status"], "ROUTED")
        self.assertEqual(decision["resolved_model"], "qwen3.8-max-0902")
        self.assertEqual(
            decision["provider_snapshot_provenance"], "qwen3.8-max-0902"
        )
        self.assertIn(
            "DATED_SNAPSHOT_PROVENANCE_ONLY", decision["reason_codes"]
        )
        self.assertEqual(decision["requested_model"], "qwen3.8-max")

    def test_c09_unverified_snapshot_or_substitution_blocks(self):
        unverified = evaluate(
            request(
                provider_verification={
                    "verified": False,
                    "served_model": "qwen3.8-max-0902",
                }
            )
        )
        self.assertEqual(unverified["status"], "BLOCKED")
        self.assertIn("MODEL_ALIAS_UNRESOLVED", unverified["reason_codes"])

        substituted = evaluate(
            request(
                provider_verification={
                    "verified": True,
                    "served_model": "qwen3.8-flash-0902",
                }
            )
        )
        self.assertEqual(substituted["status"], "BLOCKED")
        self.assertIn("SUBSTITUTION_REJECTED", substituted["reason_codes"])
        self.assertIn("MODEL_ALIAS_UNRESOLVED", substituted["reason_codes"])

    def test_c09_verified_exact_identity(self):
        decision = evaluate(
            request(
                provider_verification={
                    "verified": True,
                    "served_model": "qwen3.8-max",
                }
            ),
            verify_provider=lambda verification: True,
        )
        self.assertEqual(decision["resolved_model"], "qwen3.8-max")
        self.assertIn("MODEL_IDENTITY_VERIFIED", decision["reason_codes"])
        self.assertIsNone(decision["provider_snapshot_provenance"])

    def test_c09_self_attested_provider_verification_is_rejected(self):
        decision = evaluate(
            request(
                provider_verification={
                    "verified": True,
                    "served_model": "qwen3.8-max-0902",
                }
            )
        )
        self.assertEqual(decision["status"], "BLOCKED")
        self.assertIn("PROVIDER_ATTESTATION_UNVERIFIED", decision["reason_codes"])


class C10TelemetryTests(unittest.TestCase):
    def test_c10_decision_exposes_requested_and_resolved_pairs(self):
        decision = evaluate(
            request(requested_lane="full", delegation_benefit=True)
        )
        telemetry = decision["shadow_telemetry"]
        self.assertEqual(telemetry["requested_lane"], "full")
        self.assertEqual(telemetry["resolved_lane"], "full")
        self.assertEqual(telemetry["requested_model"], "qwen3.8-max")
        self.assertEqual(telemetry["resolved_model"], "qwen3.8-max")
        self.assertEqual(telemetry["reason_codes"], decision["reason_codes"])
        self.assertEqual(telemetry["mode"], "shadow")
        self.assertFalse(telemetry["runtime_activation"])
        self.assertEqual(
            telemetry["excluded_from_telemetry"],
            [
                "prompt-content",
                "secrets",
                "stale-quota-claims",
                "synthetic-assessments",
            ],
        )

    def test_c10_prompt_secret_quota_and_synthetic_inputs_never_reach_telemetry(self):
        probes = {
            "prompt": "SECRET-PROMPT-TEXT",
            "prompt_text": "SECRET-PROMPT-TEXT",
            "task_text": "SECRET-PROMPT-TEXT",
            "secrets": "TOKEN-VALUE",
            "token": "TOKEN-VALUE",
            "api_key": "KEY-VALUE",
            "quota": {"status": "stale"},
            "quota_snapshot": {"status": "stale"},
            "fallback_assessment": {"assessment_source": "fallback"},
            "assessment": {"assessment_source": "fallback"},
            "assessment_source": "fallback",
        }
        for key, value in probes.items():
            with self.subTest(key=key):
                payload = request()
                payload[key] = value
                with self.assertRaises(lane_policy.LanePolicyError) as caught:
                    evaluate(payload)
                message = str(caught.exception)
                self.assertNotIn(key, message)
                self.assertNotIn("SECRET-PROMPT-TEXT", message)
                self.assertNotIn("TOKEN-VALUE", message)
                self.assertNotIn("KEY-VALUE", message)

    def test_c10_unexpected_sensitive_key_is_not_echoed(self):
        payload = request()
        # Keep the adversarial fixture out of the source scanner's literal
        # assigned-secret pattern while preserving the runtime attack shape.
        field = "_".join(("api", "key"))
        secret_value = "-".join(("SECRET", "DO", "NOT", "ECHO"))
        malicious_key = field + "=" + secret_value
        payload[malicious_key] = secret_value
        with self.assertRaises(lane_policy.LanePolicyError) as caught:
            evaluate(payload)
        message = str(caught.exception)
        self.assertNotIn(malicious_key, message)
        self.assertNotIn("SECRET-DO-NOT-ECHO", message)

    def test_c10_structural_refusals_produce_no_decision(self):
        for payload in (
            request(policy_version=2),
            {key: value for key, value in request().items() if key != "signals"},
            dict(request(), environment="STAGING"),
            dict(request(), continuity_state="ANCIENT"),
            dict(request(), external_effects=["git push"]),
        ):
            with self.assertRaises(lane_policy.LanePolicyError):
                evaluate(payload)

    def test_c10_untrusted_request_bounds_fail_closed(self):
        with self.assertRaises(lane_policy.LanePolicyError):
            evaluate(request(requested_model="x" * (lane_policy.MAX_REQUEST_STRING_LENGTH + 1)))

        nested = value = {}
        for _ in range(lane_policy.MAX_REQUEST_DEPTH + 1):
            value["next"] = {}
            value = value["next"]
        with self.assertRaises(lane_policy.LanePolicyError):
            evaluate(nested)


class C11NodeAcceptanceTests(unittest.TestCase):
    def test_c11_compact_node_acceptance_leaves_system_pending(self):
        decision = evaluate(
            request(delegation_benefit=True, d0_evidence=d0()),
            verify_d0_file=lambda evidence: True,
        )
        self.assertEqual(decision["resolved_lane"], "compact")
        projection = lane_policy.acceptance_projection(
            decision, node_verdict="ACCEPT"
        )
        self.assertEqual(projection["node_acceptance"], "ACCEPT")
        self.assertEqual(projection["system_acceptance"], "PENDING")
        self.assertFalse(projection["authorization_granted"])
        self.assertIn(
            "NODE_ACCEPTANCE_IS_NOT_SYSTEM_ACCEPTANCE", projection["reason_codes"]
        )

    def test_c11_invalid_node_verdict_is_refused(self):
        decision = evaluate(request())
        with self.assertRaises(lane_policy.LanePolicyError):
            lane_policy.acceptance_projection(decision, node_verdict="MAYBE")


class C12RetestPolicyTests(unittest.TestCase):
    def event(self, gate, status="PASS", revision="rev-1", **changes):
        value = {
            "gate": gate,
            "status": status,
            "revision": revision,
            "receipt_sha256": VALID_SHA256,
            "parent_verified": True,
        }
        value.update(changes)
        return value

    def complete_events(self, status_by_gate=None, revision="rev-1"):
        status_by_gate = status_by_gate or {}
        return [
            self.event(gate, status_by_gate.get(gate, "PASS"), revision)
            for gate in sorted(lane_policy.RETEST_GATE_NAMES)
        ]

    def test_c12_fail_invalidates_prior_pass_and_requires_current_revision_retest(self):
        result = lane_policy.retest_policy(
            [
                self.event("quality"),
                self.event("security", "FAIL"),
            ],
            expected_revision="rev-1",
            verify_event=lambda event: True,
        )
        self.assertFalse(result["prior_pass_evidence_valid"])
        self.assertTrue(result["requires_full_retest"])
        self.assertEqual(result["retest_scope"], "current-revision")
        self.assertEqual(result["invalidated_by"], ["security"])

    def test_c12_blocked_gate_also_invalidates(self):
        result = lane_policy.retest_policy(
            self.complete_events({"quality": "BLOCKED"}),
            expected_revision="rev-1",
            verify_event=lambda event: True,
        )
        self.assertFalse(result["prior_pass_evidence_valid"])
        self.assertTrue(result["requires_full_retest"])

    def test_c12_stale_revision_evidence_requires_retest(self):
        result = lane_policy.retest_policy(
            self.complete_events(revision="rev-1")[:-1]
            + [self.event("security", revision="rev-2")],
            expected_revision="rev-1",
            verify_event=lambda event: True,
        )
        self.assertFalse(result["prior_pass_evidence_valid"])
        self.assertTrue(result["requires_full_retest"])
        self.assertEqual(result["mixed_revisions"], ["rev-1", "rev-2"])

    def test_c12_clean_same_revision_passes_stay_valid(self):
        result = lane_policy.retest_policy(
            self.complete_events(),
            expected_revision="rev-1",
            verify_event=lambda event: True,
        )
        self.assertTrue(result["prior_pass_evidence_valid"])
        self.assertFalse(result["requires_full_retest"])

    def test_c12_single_pass_cannot_reuse_prior_evidence(self):
        result = lane_policy.retest_policy(
            [self.event("quality")],
            expected_revision="rev-1",
            verify_event=lambda event: True,
        )
        self.assertFalse(result["prior_pass_evidence_valid"])
        self.assertIn("RETEST_GATE_SET_INCOMPLETE", result["reason_codes"])

    def test_c12_missing_parent_verifier_fails_closed(self):
        result = lane_policy.retest_policy(
            self.complete_events(),
            expected_revision="rev-1",
        )
        self.assertFalse(result["prior_pass_evidence_valid"])
        self.assertIn("RETEST_EVIDENCE_UNVERIFIED", result["reason_codes"])

    def test_c12_duplicate_gate_fails_closed(self):
        events = self.complete_events() + [self.event("quality")]
        result = lane_policy.retest_policy(
            events,
            expected_revision="rev-1",
            verify_event=lambda event: True,
        )
        self.assertFalse(result["prior_pass_evidence_valid"])
        self.assertIn("RETEST_DUPLICATE_GATE", result["reason_codes"])

    def test_c12_malformed_events_are_refused(self):
        for events in (
            [],
            [{"gate": "quality", "status": "PASS"}],
            [{"gate": "quality", "status": "MAYBE", "revision": "rev-1"}],
            [self.event("unknown")],
        ):
            with self.assertRaises(lane_policy.LanePolicyError):
                lane_policy.retest_policy(events)


class C13EnvironmentEffectTests(unittest.TestCase):
    def test_c13_dev_local_work_is_not_hard_risk(self):
        decision = evaluate(request(environment="DEV"))
        self.assertEqual(decision["resolved_lane"], "direct")
        self.assertNotIn("PRODUCTION_ENVIRONMENT", decision["reason_codes"])

    def test_c13_production_environment_forces_full(self):
        decision = evaluate(request(environment="PRODUCTION"))
        self.assertEqual(decision["resolved_lane"], "full")
        self.assertIn("PRODUCTION_ENVIRONMENT", decision["reason_codes"])

    def test_c13_declared_external_effect_forces_full(self):
        decision = evaluate(
            request(
                external_effects=["git-push"],
                hard_risk=hard_risk(production_or_external_effect=True),
            )
        )
        self.assertEqual(decision["resolved_lane"], "full")
        self.assertIn(
            "HARD_RISK_PRODUCTION_OR_EXTERNAL_EFFECT", decision["reason_codes"]
        )

    def test_c13_undeclared_external_effect_fails_closed(self):
        decision = evaluate(request(external_effects=["deploy-service"]))
        self.assertEqual(decision["resolved_lane"], "full")
        self.assertIn("HARD_RISK_MISDECLARED", decision["reason_codes"])


class KillSwitchAndFailClosedTests(unittest.TestCase):
    def test_baseline_kill_switch_forces_full_from_request(self):
        decision = evaluate(
            request(kill_switches={"baseline": True, "lanes": _no_lanes()})
        )
        self.assertEqual(decision["resolved_lane"], "full")
        self.assertIn("KILL_SWITCH_BASELINE", decision["reason_codes"])

    def test_baseline_kill_switch_forces_full_from_environment(self):
        decision = evaluate(
            request(),
            environ={lane_policy.BASELINE_KILL_SWITCH_ENV: "1"},
        )
        self.assertEqual(decision["resolved_lane"], "full")
        self.assertIn("KILL_SWITCH_BASELINE", decision["reason_codes"])

    def test_per_lane_kill_switches_force_full(self):
        cases = (
            ("direct", {}, "AGENT_FLEET_LANE_KILL_DIRECT"),
            (
                "compact",
                {"delegation_benefit": True, "d0_evidence": d0()},
                "AGENT_FLEET_LANE_KILL_COMPACT",
            ),
            (
                "compact-recon",
                {"continuity_state": "MISSING"},
                "AGENT_FLEET_LANE_KILL_COMPACT_RECON",
            ),
        )
        for lane, overrides, env_name in cases:
            with self.subTest(lane=lane):
                decision = evaluate(
                    request(**overrides),
                    environ={env_name: "true"},
                    verify_d0_file=lambda evidence: True,
                )
                self.assertEqual(decision["resolved_lane"], "full")
                self.assertIn("KILL_SWITCH_LANE", decision["reason_codes"])
                self.assertEqual(decision["kill_switch_applied_to"], lane)

    def test_request_lane_switches_force_full(self):
        decision = evaluate(
            request(
                delegation_benefit=True,
                d0_evidence=d0(),
                kill_switches={
                    "baseline": False,
                    "lanes": {"direct": False, "compact": True, "compact-recon": False},
                },
            ),
            verify_d0_file=lambda evidence: True,
        )
        self.assertEqual(decision["resolved_lane"], "full")
        self.assertEqual(decision["kill_switch_applied_to"], "compact")

    def test_stale_provenance_and_gate_signals_fail_closed_to_full(self):
        cases = (
            ({"stale_continuity": True}, "STALE_CONTINUITY_FAIL_CLOSED"),
            ({"provenance_drift": True}, "PROVENANCE_DRIFT_FAIL_CLOSED"),
            ({"gate_regression": True}, "GATE_REGRESSION_FAIL_CLOSED"),
        )
        for flags, code in cases:
            with self.subTest(code=code):
                signals = {
                    "stale_continuity": False,
                    "provenance_drift": False,
                    "gate_regression": False,
                }
                signals.update(flags)
                decision = evaluate(
                    request(
                        signals=signals,
                        continuity_state="FRESH",
                        delegation_benefit=True,
                        d0_evidence=d0(),
                    )
                )
                self.assertEqual(decision["resolved_lane"], "full")
                self.assertIn(code, decision["reason_codes"])

    def test_stale_signal_overrides_fresh_looking_continuity(self):
        decision = evaluate(
            request(
                signals={
                    "stale_continuity": True,
                    "provenance_drift": False,
                    "gate_regression": False,
                }
            )
        )
        self.assertEqual(decision["resolved_lane"], "full")
        self.assertIsNone(decision["recon_directive"])


def _no_lanes():
    return {"direct": False, "compact": False, "compact-recon": False}


class C14CliAndDispatchCompatibilityTests(unittest.TestCase):
    def _write_json(self, root, name, payload):
        path = root / name
        path.write_text(json.dumps(payload), encoding="utf-8")
        return path

    def test_c14_lane_policy_cli_emits_canonical_decision(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            path = self._write_json(root, "request.json", request())
            result = subprocess.run(
                [
                    sys.executable,
                    "-B",
                    str(Path(lane_policy.__file__)),
                    "--request-file",
                    str(path),
                ],
                capture_output=True,
                text=True,
                timeout=30,
            )
        self.assertEqual(result.returncode, 0, result.stderr)
        decision = json.loads(result.stdout)
        self.assertEqual(decision["action"], "lane-policy")
        self.assertEqual(decision["resolved_lane"], "direct")
        self.assertEqual(decision["writes_performed"], 0)

    def test_c14_lane_policy_cli_refuses_forbidden_input_with_exit_2(self):
        payload = request()
        payload["lane_exception"] = {"allow": True}
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            path = self._write_json(root, "request.json", payload)
            result = subprocess.run(
                [
                    sys.executable,
                    "-B",
                    str(Path(lane_policy.__file__)),
                    "--request-file",
                    str(path),
                ],
                capture_output=True,
                text=True,
                timeout=30,
            )
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stdout, "")
        refused = json.loads(result.stderr)
        self.assertEqual(refused["status"], "REFUSED")
        self.assertEqual(refused["error"], "REQUEST_REFUSED")

    def test_c14_lane_policy_cli_rejects_oversized_request_without_echo(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            path = root / "oversized-request.json"
            path.write_bytes(b"{" + b" " * lane_policy.MAX_REQUEST_BYTES + b"}")
            result = subprocess.run(
                [
                    sys.executable,
                    "-B",
                    str(Path(lane_policy.__file__)),
                    "--request-file",
                    str(path),
                ],
                capture_output=True,
                text=True,
                timeout=30,
            )
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stdout, "")
        refused = json.loads(result.stderr)
        self.assertEqual(refused["error"], "REQUEST_REFUSED")
        self.assertNotIn(str(path), result.stderr)

    def test_c14_delegation_policy_dispatch_remains_compatible(self):
        scope = {
            "scope": "bounded",
            "risk": "low",
            "context_ready": True,
            "protected_change": False,
            "independent_review": False,
            "specialist_gain": False,
            "parallel_slices": 0,
            "economics": None,
        }
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            scope_path = self._write_json(root, "scope.json", scope)
            result = subprocess.run(
                [
                    sys.executable,
                    "-B",
                    str(Path(fleet.__file__)),
                    "--profiles",
                    str(root / "missing.json"),
                    "--state-dir",
                    str(root / "state"),
                    "delegation-policy",
                    "--scope-file",
                    str(scope_path),
                ],
                capture_output=True,
                text=True,
                timeout=30,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(result.stdout)["lane"], "direct")
            self.assertFalse((root / "state").exists())

    def test_c14_fleet_dispatches_lane_policy_offline(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            path = self._write_json(root, "request.json", request())
            output = io.TextIOWrapper(
                io.BytesIO(), encoding="utf-8", write_through=True
            )
            with contextlib.redirect_stdout(output):
                code = fleet.main(
                    [
                        "--profiles",
                        str(root / "missing.json"),
                        "--state-dir",
                        str(root / "state"),
                        "lane-policy",
                        "--request-file",
                        str(path),
                    ]
                )
            self.assertEqual(code, 0)
            decision = json.loads(output.buffer.getvalue())
            self.assertEqual(decision["action"], "lane-policy")
            self.assertFalse((root / "state").exists())

    def test_c14_fleet_dispatch_rejects_oversized_request_before_parse(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            path = root / "oversized-request.json"
            path.write_bytes(b"{" + b" " * lane_policy.MAX_REQUEST_BYTES + b"}")
            result = subprocess.run(
                [
                    sys.executable,
                    "-B",
                    str(Path(fleet.__file__)),
                    "--profiles",
                    str(root / "missing.json"),
                    "--state-dir",
                    str(root / "state"),
                    "lane-policy",
                    "--request-file",
                    str(path),
                ],
                capture_output=True,
                text=True,
                timeout=30,
            )
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stdout, "")
        self.assertEqual(json.loads(result.stderr)["status"], "REFUSED")
        self.assertNotIn(str(path), result.stderr)
        self.assertFalse((root / "state").exists())

    def test_c14_fleet_dispatch_rejects_deep_request_after_bounded_parse(self):
        payload = request()
        nested = "ok"
        for _ in range(lane_policy.MAX_REQUEST_DEPTH + 1):
            nested = [nested]
        payload["external_effects"] = nested
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            path = self._write_json(root, "deep-request.json", payload)
            result = subprocess.run(
                [
                    sys.executable,
                    "-B",
                    str(Path(fleet.__file__)),
                    "--profiles",
                    str(root / "missing.json"),
                    "--state-dir",
                    str(root / "state"),
                    "lane-policy",
                    "--request-file",
                    str(path),
                ],
                capture_output=True,
                text=True,
                timeout=30,
            )
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stdout, "")
        self.assertEqual(json.loads(result.stderr)["status"], "REFUSED")
        self.assertNotIn(str(path), result.stderr)

    def test_c14_bounded_loader_rejects_directories(self):
        with tempfile.TemporaryDirectory() as temp:
            with self.assertRaises(lane_policy.LanePolicyError):
                lane_policy.load_bounded_request_file(Path(temp))


class QwenAdapterContractTests(unittest.TestCase):
    """Adapter smoke path: prompt is sent on stdin, never in argv."""

    def build(self, prompt, mode):
        with tempfile.TemporaryDirectory() as temp:
            return fleet.build_command(
                "qwen",
                "qwen.cmd",
                prompt=prompt,
                workspace=Path(temp),
                mode=mode,
                model="qwen3.8-max",
                complexity="high",
                timeout=300,
                run_dir=Path(temp),
                native_subagent_limit=0,
                enable_web=False,
                materialize=False,
            )

    def test_qwen_prompt_is_sent_on_stdin_with_terminating_newline(self):
        command, stdin_text, _ = self.build("Implement the lane policy.", "implement")
        self.assertNotIn("--prompt", command)
        self.assertEqual(stdin_text, "Implement the lane policy.\n")

    def test_qwen_prompt_newline_is_idempotent(self):
        command, stdin_text, _ = self.build("Already terminated.\n", "review")
        self.assertNotIn("--prompt", command)
        self.assertEqual(stdin_text, "Already terminated.\n")

    def test_qwen_persisted_command_stays_redacted(self):
        prompt = "Implement the lane policy."
        command, _, _ = self.build(prompt, "implement")
        redacted = fleet.redacted_command(command, prompt)
        self.assertNotIn(prompt, redacted)
        self.assertNotIn(prompt + "\n", redacted)
        self.assertNotIn(f"<TASK:{fleet.sha256_text(prompt)[:16]}>", redacted)
        self.assertIn("--approval-mode", redacted)
        self.assertIn("--safe-mode", redacted)

    def test_qwen_review_keeps_plan_mode_and_model_contract(self):
        command, _, _ = self.build("Review the candidate.", "review")
        self.assertEqual(command[command.index("--approval-mode") + 1], "plan")
        self.assertEqual(command[command.index("--model") + 1], "qwen3.8-max")
        self.assertEqual(command[command.index("--fallback-model") + 1], "")
        self.assertNotIn("--sandbox", command)


if __name__ == "__main__":
    unittest.main()
