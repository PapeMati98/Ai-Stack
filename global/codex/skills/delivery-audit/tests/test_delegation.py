from __future__ import annotations

import sys
from pathlib import Path
import unittest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

from delegation_policy import (  # noqa: E402
    DelegationStatus,
    DelegationValidationError,
    assess_delegation,
)


def packet(
    *,
    parent_tier: str = "M",
    eligible: bool | None = True,
    exception: str = "none",
    planned_tasks: int | None = 1,
    evidence_complete: bool | None = True,
    runs: list[dict[str, object]] | None = None,
) -> dict[str, object]:
    return {
        "delegation": {
            "parent_tier": parent_tier,
            "eligible": eligible,
            "exception": exception,
            "planned_tasks": planned_tasks,
        },
        "metrics": {
            "wall_seconds": None,
            "repair_rounds": None,
            "duplicate_work": None,
            "uat_required": False,
            "uat_status": "not-applicable",
            "evidence_complete": evidence_complete,
        },
        "runs": runs or [],
    }


def run(
    *,
    run_id: str = "run-worker-1",
    actor_id: str = "worker-1",
    tier: str = "W",
    role: str = "maker",
    status: str = "completed",
    verified: bool = True,
    provider: str = "provider-worker",
) -> dict[str, object]:
    return {
        "run_id": run_id,
        "actor_id": actor_id,
        "model_id": "model-worker",
        "provider": provider,
        "tier": tier,
        "role": role,
        "status": status,
        "verified": verified,
        "input_tokens": 100,
        "cached_input_tokens": 10,
        "output_tokens": 20,
        "tool_calls": 1,
    }


class DelegationPolicyTests(unittest.TestCase):
    def test_verified_completed_worker_receipt_proves_delegation(self) -> None:
        result = assess_delegation(packet(runs=[run()]))
        self.assertIs(result.status, DelegationStatus.DELEGATED)
        self.assertEqual(result.completed_child_runs, ("run-worker-1",))
        self.assertIsNone(result.finding_code)

    def test_verified_completed_kierownik_receipt_also_counts(self) -> None:
        result = assess_delegation(packet(runs=[run(tier="K")]))
        self.assertIs(result.status, DelegationStatus.DELEGATED)

    def test_declared_plan_without_a_child_receipt_is_underused(self) -> None:
        result = assess_delegation(packet(planned_tasks=0, runs=[]))
        self.assertIs(result.status, DelegationStatus.UNDERUSED)
        self.assertEqual(result.finding_code, "missing-delegation")

    def test_planned_task_count_does_not_prove_or_disprove_delegation(self) -> None:
        for planned_tasks in (None, 0, 4):
            with self.subTest(planned_tasks=planned_tasks):
                result = assess_delegation(packet(planned_tasks=planned_tasks, runs=[]))
                self.assertIs(result.status, DelegationStatus.UNDERUSED)

    def test_quota_and_credential_exceptions_are_not_underuse(self) -> None:
        for exception in ("quota-exhausted", "credentials"):
            with self.subTest(exception=exception):
                result = assess_delegation(packet(exception=exception, runs=[]))
                self.assertIs(result.status, DelegationStatus.EXCEPTION)
                self.assertIsNone(result.finding_code)

    def test_trivial_and_worker_node_exceptions_are_explicit(self) -> None:
        for exception in ("trivial", "worker-node"):
            with self.subTest(exception=exception):
                result = assess_delegation(packet(exception=exception, runs=[]))
                self.assertIs(result.status, DelegationStatus.EXCEPTION)
                self.assertEqual(result.reason, exception)

    def test_other_legitimate_exceptions_are_not_underuse(self) -> None:
        for exception in ("irreversible", "capability-unavailable", "explicit-owner-model"):
            with self.subTest(exception=exception):
                result = assess_delegation(packet(exception=exception, runs=[]))
                self.assertIs(result.status, DelegationStatus.EXCEPTION)

    def test_incomplete_or_unknown_evidence_is_unknown_not_underuse(self) -> None:
        for evidence_complete in (False, None):
            with self.subTest(evidence_complete=evidence_complete):
                result = assess_delegation(packet(evidence_complete=evidence_complete, runs=[]))
                self.assertIs(result.status, DelegationStatus.UNKNOWN)
                self.assertIsNone(result.finding_code)

    def test_unknown_eligibility_and_parent_tier_stay_unknown(self) -> None:
        self.assertIs(assess_delegation(packet(eligible=None)).status, DelegationStatus.UNKNOWN)
        self.assertIs(
            assess_delegation(packet(parent_tier="unknown")).status,
            DelegationStatus.UNKNOWN,
        )

    def test_ineligible_and_non_manager_parent_are_not_applicable(self) -> None:
        self.assertIs(assess_delegation(packet(eligible=False)).status, DelegationStatus.NOT_APPLICABLE)
        self.assertIs(assess_delegation(packet(parent_tier="K")).status, DelegationStatus.NOT_APPLICABLE)

    def test_unverified_failed_or_non_maker_runs_do_not_prove_delegation(self) -> None:
        cases = (
            (run(verified=False), DelegationStatus.UNKNOWN),
            (run(status="failed"), DelegationStatus.UNKNOWN),
            (run(role="reviewer"), DelegationStatus.UNDERUSED),
            (run(tier="D"), DelegationStatus.UNDERUSED),
        )
        for child, expected in cases:
            with self.subTest(run=child):
                result = assess_delegation(packet(runs=[child]))
                self.assertIs(result.status, expected)

    def test_parent_identity_cannot_substitute_for_a_child(self) -> None:
        parent_run = run(run_id="parent-run", actor_id="parent", tier="M", role="planner")
        same_actor = run(run_id="claimed-child", actor_id="parent", tier="W", role="maker")
        result = assess_delegation(packet(runs=[parent_run, same_actor]))
        self.assertIs(result.status, DelegationStatus.UNDERUSED)

    def test_attempted_child_without_success_is_unknown_not_missing_delegation(self) -> None:
        for child in (run(verified=False), run(status="partial"), run(status="failed"),
                      run(status="blocked"), run(status="unknown")):
            with self.subTest(status=child["status"], verified=child["verified"]):
                result = assess_delegation(packet(runs=[child]))
                self.assertIs(result.status, DelegationStatus.UNKNOWN)
                self.assertIsNone(result.finding_code)

    def test_unknown_fields_and_malformed_values_are_rejected(self) -> None:
        malformed = packet()
        malformed["delegation"]["extra"] = "ignored?"  # type: ignore[index]
        with self.assertRaises(DelegationValidationError):
            assess_delegation(malformed)

        with self.assertRaises(DelegationValidationError):
            assess_delegation(packet(runs=[run(run_id="../outside")]))

        with self.assertRaises(DelegationValidationError):
            assess_delegation(packet(runs=[run(verified=1)]))


if __name__ == "__main__":
    unittest.main()
