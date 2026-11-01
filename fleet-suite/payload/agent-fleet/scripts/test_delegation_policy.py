import contextlib
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

import delegation_policy
import fleet


def facts(**changes):
    value = dict(scope="bounded", risk="low", context_ready=True,
                 protected_change=False, independent_review=False,
                 specialist_gain=False, parallel_slices=0, economics=None)
    value.update(changes)
    return value


class DelegationPolicyTests(unittest.TestCase):
    def test_long_bounded_work_is_not_automatically_delegated(self):
        result = delegation_policy.decide(facts(economics=dict(
            local_minutes=120, delegated_minutes=120, coordination_minutes=10)))
        self.assertEqual((result["lane"], result["delegation_mode"]), ("direct", "SKIP"))

    def test_short_independent_review_is_worth_delegating(self):
        result = delegation_policy.decide(facts(independent_review=True))
        self.assertEqual((result["lane"], result["delegation_mode"]), ("direct", "ONE"))
        self.assertTrue(result["review_required"])
        self.assertFalse(result["planning_contract_required"])

    def test_positive_gain_and_parallelism(self):
        result = delegation_policy.decide(facts(parallel_slices=3, parallel_disjoint=True, economics=dict(
            local_minutes=60, delegated_minutes=25, coordination_minutes=10)))
        self.assertEqual(result["lane"], "direct")
        self.assertEqual(result["estimated_minutes_saved"], 25)
        self.assertTrue(result["parallel_recommended"])
        self.assertNotEqual(result["delegation_mode"], "PARALLEL_IMPLEMENT")

    def test_two_known_small_bugs_do_not_become_project_delivery(self):
        result = delegation_policy.decide(facts(parallel_slices=2, parallel_disjoint=True, specialist_gain=True))
        self.assertEqual(result["lane"], "direct")
        self.assertFalse(result["planning_contract_required"])
        self.assertTrue(result["parallel_recommended"])
        self.assertFalse(result["authorization_granted"])

    def test_unproven_parallelism_uses_serial_without_ceremony_escalation(self):
        for change in ({}, {"parallel_disjoint": False}):
            result = delegation_policy.decide(facts(parallel_slices=2, specialist_gain=True, **change))
            self.assertEqual(result["lane"], "direct")
            self.assertFalse(result["parallel_recommended"])
            self.assertTrue(result["parent_acceptance_required"])
            self.assertTrue(result["product_acceptance_required"])

    def test_medium_risk_keeps_compact_even_without_delegation(self):
        for count in (0, 1, 2, 6, 64):
            for review in (False, True):
                result = delegation_policy.decide(facts(
                    risk="medium", parallel_slices=count, independent_review=review))
                self.assertEqual(result["lane"], "compact")
                self.assertTrue(result["planning_contract_required"])

    def test_hard_risk_and_coordinated_scope_never_get_lightweight(self):
        for changes in (dict(risk="high"), dict(risk="critical"),
                        dict(protected_change=True), dict(scope="coordinated")):
            for count in (0, 1, 2, 6):
                result = delegation_policy.decide(facts(**changes, parallel_slices=count))
                self.assertEqual(result["lane"], "full")
                self.assertTrue(result["planning_contract_required"])
                self.assertFalse(result["authorization_granted"])

    def test_protected_work_keeps_full_governance_without_fleet(self):
        result = delegation_policy.decide(facts(protected_change=True))
        self.assertEqual((result["lane"], result["delegation_mode"]), ("full", "SKIP"))
        self.assertTrue(result["review_required"])
        self.assertFalse(result["authorization_granted"])

    def test_unknown_requires_recon(self):
        for value in (facts(risk="unknown"), facts(context_ready=False)):
            result = delegation_policy.decide(value)
            self.assertEqual(result["delegation_mode"], "DEFER")
            self.assertEqual(result["provider_calls"], 0)

    def test_invalid_facts_fail(self):
        for value in (facts(parallel_slices=True), facts(parallel_disjoint="yes"), facts(risk="safe"),
                      facts(context_ready="yes"), facts(extra=True),
                      facts(economics=dict(local_minutes=float("nan"),
                            delegated_minutes=1, coordination_minutes=0))):
            with self.assertRaises(ValueError):
                delegation_policy.decide(value)

    def test_calculator_never_loads_profiles_or_state(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            scope = root / "scope.json"
            scope.write_text(json.dumps(facts()), encoding="utf-8")
            result = subprocess.run([sys.executable, "-B", str(Path(fleet.__file__)),
                "--profiles", str(root / "missing.json"), "--state-dir", str(root / "state"),
                "delegation-policy", "--scope-file", str(scope)], capture_output=True, text=True, timeout=20)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(result.stdout)["lane"], "direct")
            self.assertFalse((root / "state").exists())


class SkipTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.profiles = fleet.load_profiles(fleet.DEFAULT_PROFILES)
        self.assessment = fleet.adaptive.fallback_assessment(
            kind="review", complexity="low", profiles=self.profiles)
        self.assessment.update(delegation_value=0, delegation_mode="SKIP")
        self.assessment.pop("assessment_source")
        self.path = self.root / "assessment.json"
        self.path.write_text(json.dumps(self.assessment), encoding="utf-8")

    def test_skip_does_not_probe_models_or_quota(self):
        with mock.patch.object(fleet, "require_fresh_model_audit", side_effect=AssertionError("radar")), \
             mock.patch.object(fleet, "live_readiness", side_effect=AssertionError("readiness")), \
             mock.patch.object(fleet, "current_quota_views", side_effect=AssertionError("quota")):
            result = fleet.route_task("bounded review", profiles=self.profiles, kind="review",
                complexity="low", root=self.root, cwd=self.root, assessment_file=str(self.path))
        self.assertEqual(result["status"], "SKIPPED")
        self.assertIsNone(result["selected_agent"])

    def test_force_still_reaches_readiness_guards(self):
        with mock.patch.object(fleet, "require_fresh_model_audit", side_effect=fleet.FleetError("guard")):
            with self.assertRaisesRegex(fleet.FleetError, "guard"):
                fleet.route_task("bounded review", profiles=self.profiles, kind="review",
                    complexity="low", root=self.root, cwd=self.root,
                    assessment_file=str(self.path), force_delegation=True)

    def test_run_and_council_skip_successfully_without_executor(self):
        for action in ("run", "council"):
            output = io.TextIOWrapper(io.BytesIO(), encoding="utf-8", write_through=True)
            with mock.patch.object(fleet, "require_fresh_model_audit", side_effect=AssertionError("radar")), \
                 contextlib.redirect_stdout(output):
                code = fleet.main(["--state-dir", str(self.root / "state"), action,
                    "--task", "bounded review", "--workspace", str(self.root),
                    "--kind", "review", "--complexity", "low", "--assessment-file", str(self.path)])
            self.assertEqual(code, 0)
            self.assertEqual(json.loads(output.buffer.getvalue())["status"], "SKIPPED")
            self.assertFalse((self.root / "state").exists())

    def test_council_force_is_forwarded(self):
        with mock.patch.object(fleet, "route_task", side_effect=fleet.FleetError("sentinel")) as route:
            fleet.main(["council", "--task", "bounded review", "--workspace", str(self.root),
                        "--assessment-file", str(self.path), "--force-delegation"])
        self.assertTrue(route.call_args.kwargs["force_delegation"])


if __name__ == "__main__":
    unittest.main()
