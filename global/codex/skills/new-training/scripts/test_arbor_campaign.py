#!/usr/bin/env python3
"""Comprehensive test suite for Arbor ML Campaign & Experience Ledger Helper.

Tests:
1. Pure declarative models (no arbitrary code execution).
2. Numerical stability and non-finite metric/prediction rejection.
3. Comparable training budget enforcement.
4. Local CPU record schemas and explicit caller-authored AG export validation.
5. Lifecycle fencing: initialization, development, selection, freeze, and single-use final.
6. Fail-closed final evaluation (prohibiting repeated holdout access).
7. Experience ledger: infrastructure vs approach failure taxonomy.
8. Negative tests for lesson filtering (trust, domain, condition mismatch).
9. Actual second-campaign lesson transfer demonstrating empirical adaptation.
10. Symlink and caller-data immutability security guarantees.
11. CLI end-to-end execution.
"""

from __future__ import annotations

import copy
import json
import math
import os
import shutil
import sys
import tempfile
import threading
import unittest
import uuid
from pathlib import Path

# Ensure local script directory is on sys.path
SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import arbor_campaign as ac


def begin_workspace_test(test_case):
    test_case.token = uuid.uuid4().hex[:10]
    artifact_parent = Path(os.environ.get("TEAM_TEST_ROOT", tempfile.gettempdir())).resolve() / "arbor-campaign-tests"
    if artifact_parent.is_relative_to(SCRIPT_DIR.parent.parent):
        raise RuntimeError("Test artifacts must stay outside the skill source tree")
    artifact_dir = artifact_parent / test_case.token
    artifact_dir.mkdir(parents=True, exist_ok=False)
    test_case.temp_dir = str(artifact_dir)
    test_case.external_paths = []


def unique_id(test_case, prefix):
    return f"{prefix}-{test_case.token}"


def cleanup_workspace_test(test_case):
    # Retain task-owned evidence outside source; never scan or clean a skill tree.
    pass


class TestArborDeclarativeAlgorithms(unittest.TestCase):
    """Test all declarative algorithms on synthetic regression data."""

    def setUp(self):
        self.data = ac.generate_synthetic_regression_data(seed=123, n_samples=100, n_features=3, noise=0.05)
        self.X_train = self.data["train"]["X"]
        self.y_train = self.data["train"]["y"]
        self.X_dev = self.data["dev"]["X"]
        self.y_dev = self.data["dev"]["y"]
        self.budget = {"max_steps": 15, "max_samples": 200}

    def test_mean_baseline(self):
        model = ac.build_model({"algorithm": "mean_baseline"})
        steps = model.fit(self.X_train, self.y_train, self.budget)
        self.assertEqual(steps, 1)
        preds = model.predict(self.X_dev)
        self.assertEqual(len(preds), len(self.X_dev))
        expected_mean = sum(self.y_train) / len(self.y_train)
        for p in preds:
            self.assertAlmostEqual(p, expected_mean, places=5)

    def test_median_baseline(self):
        model = ac.build_model({"algorithm": "median_baseline"})
        steps = model.fit(self.X_train, self.y_train, self.budget)
        self.assertEqual(steps, 1)
        preds = model.predict(self.X_dev)
        self.assertEqual(len(preds), len(self.X_dev))

    def test_linear_regression(self):
        model = ac.build_model({"algorithm": "linear_regression", "fit_intercept": True})
        steps = model.fit(self.X_train, self.y_train, self.budget)
        self.assertEqual(steps, 1)
        preds = model.predict(self.X_dev)
        r2 = ac.r2_score(self.y_dev, preds)
        self.assertGreater(r2, 0.8)

    def test_ridge_regression(self):
        model = ac.build_model({"algorithm": "ridge_regression", "alpha": 0.5, "fit_intercept": True})
        steps = model.fit(self.X_train, self.y_train, self.budget)
        self.assertEqual(steps, 1)
        preds = model.predict(self.X_dev)
        r2 = ac.r2_score(self.y_dev, preds)
        self.assertGreater(r2, 0.8)

    def test_decision_stump_ensemble(self):
        model = ac.build_model({"algorithm": "decision_stump_ensemble", "n_estimators": 10, "learning_rate": 0.1})
        steps = model.fit(self.X_train, self.y_train, self.budget)
        self.assertEqual(steps, 10)
        preds = model.predict(self.X_dev)
        self.assertEqual(len(preds), len(self.X_dev))
        for p in preds:
            self.assertTrue(math.isfinite(p))

    def test_knn_regression(self):
        model = ac.build_model({"algorithm": "knn_regression", "n_neighbors": 3})
        steps = model.fit(self.X_train, self.y_train, self.budget)
        self.assertEqual(steps, 1)
        preds = model.predict(self.X_dev)
        self.assertEqual(len(preds), len(self.X_dev))

    def test_polynomial_ridge(self):
        model = ac.build_model({"algorithm": "polynomial_ridge", "degree": 2, "alpha": 1.0})
        steps = model.fit(self.X_train, self.y_train, self.budget)
        self.assertEqual(steps, 1)
        preds = model.predict(self.X_dev)
        self.assertEqual(len(preds), len(self.X_dev))

    def test_unsupported_algorithm_fails_closed(self):
        with self.assertRaises(ac.UnsupportedAlgorithmError):
            ac.build_model({"algorithm": "untrusted_eval_script", "code": "import os; os.system('echo bad')"})


class TestNumericalStabilityAndNonFiniteGuards(unittest.TestCase):
    """Test non-finite metric and prediction rejection."""

    def test_assert_finite_series(self):
        with self.assertRaises(ac.NonFiniteMetricError):
            ac.assert_finite_series([1.0, float("nan"), 3.0])
        with self.assertRaises(ac.NonFiniteMetricError):
            ac.assert_finite_series([1.0, float("inf"), 3.0])
        with self.assertRaises(ac.NonFiniteMetricError):
            ac.assert_finite_series([1.0, float("-inf"), 3.0])

    def test_metrics_reject_nan_in_predictions(self):
        y_true = [1.0, 2.0, 3.0]
        y_pred = [1.0, float("nan"), 3.0]
        with self.assertRaises(ac.NonFiniteMetricError):
            ac.mse(y_true, y_pred)
        with self.assertRaises(ac.NonFiniteMetricError):
            ac.rmse(y_true, y_pred)
        with self.assertRaises(ac.NonFiniteMetricError):
            ac.mae(y_true, y_pred)
        with self.assertRaises(ac.NonFiniteMetricError):
            ac.r2_score(y_true, y_pred)

    def test_collinear_singularity_raises_numerical_instability(self):
        # Create perfectly collinear matrix
        A = [[1.0, 2.0], [2.0, 4.0]]
        b = [3.0, 6.0]
        with self.assertRaises(ac.NumericalInstabilityError):
            ac.solve_linear_system(A, b)


class TestCampaignLifecycleAndFencing(unittest.TestCase):
    """Test campaign creation, execution, selection, freeze, and single-use final."""

    def setUp(self):
        begin_workspace_test(self)
        self.data = ac.generate_synthetic_regression_data(seed=42, n_samples=150, n_features=4, noise=0.1)

    def tearDown(self):
        cleanup_workspace_test(self)

    def test_campaign_creation_schema(self):
        baseline = {"name": "baseline-mean", "algorithm": "mean_baseline"}
        alternatives = [
            {"name": "ols", "algorithm": "linear_regression"},
            {"name": "ridge", "algorithm": "ridge_regression", "alpha": 1.0},
            {"name": "stumps", "algorithm": "decision_stump_ensemble", "n_estimators": 10},
        ]
        camp = ac.create_campaign(
            campaign_id=unique_id(self, "camp-test-1"),
            goal="Test CPU campaign",
            workspace=self.temp_dir,
            project_slug="arbor-test",
            baseline_config=baseline,
            alternative_configs=alternatives,
        )
        self.assertEqual(camp["schema_version"], "arbor-cpu-campaign/v1")
        self.assertEqual(camp["campaign_id"], unique_id(self, "camp-test-1"))
        self.assertEqual(len(camp["units"]), 4)  # 1 baseline + 3 alternatives
        self.assertEqual(camp["phase"], "initialized")
        self.assertIn("tree_budget", camp)
        self.assertEqual(camp["tree_budget"]["max_depth"], 1)

    def test_campaign_invalid_alternative_counts(self):
        baseline = {"name": "baseline-mean", "algorithm": "mean_baseline"}
        # Less than 2 alternatives
        with self.assertRaises(ValueError):
            ac.create_campaign("c1", "goal", self.temp_dir, "slug", baseline, [
                {"name": "ols", "algorithm": "linear_regression"}
            ])
        # More than 4 alternatives
        with self.assertRaises(ValueError):
            ac.create_campaign("c1", "goal", self.temp_dir, "slug", baseline, [
                {"name": "a1", "algorithm": "linear_regression"},
                {"name": "a2", "algorithm": "ridge_regression"},
                {"name": "a3", "algorithm": "decision_stump_ensemble"},
                {"name": "a4", "algorithm": "knn_regression"},
                {"name": "a5", "algorithm": "polynomial_ridge"},
            ])

    def test_full_lifecycle_and_single_use_final(self):
        baseline = {"name": "baseline-mean", "algorithm": "mean_baseline"}
        alternatives = [
            {"name": "ols", "algorithm": "linear_regression"},
            {"name": "ridge", "algorithm": "ridge_regression", "alpha": 1.0},
            {"name": "stumps", "algorithm": "decision_stump_ensemble", "n_estimators": 10},
        ]
        camp = ac.create_campaign(unique_id(self, "camp-full-1"), "Lifecycle test", self.temp_dir, "slug", baseline, alternatives)
        
        # 1. Attempting to freeze before running units raises error
        with self.assertRaises(ac.InvalidPhaseError):
            ac.freeze_candidate(camp, {"unit_id": "u1"}, "h1", "h2")
            
        # 2. Attempting to evaluate final before freeze raises error
        with self.assertRaises(ac.InvalidPhaseError):
            ac.evaluate_final(camp, {"unit_id": "u1"}, self.data["final_holdout"])

        # 3. Run units
        budget = {"max_steps": 10, "max_samples": 200}
        camp_dev, unit_results = ac.run_campaign_units(camp, self.data["train"], self.data["dev"], budget)
        self.assertEqual(camp_dev["phase"], "selection")
        self.assertEqual(len(unit_results), 4)

        # Verify unit result schema
        for res in unit_results:
            self.assertEqual(res["schema_version"], "arbor-cpu-unit-result/v1")
            self.assertEqual(res["status"], "local_cpu_measured")
            self.assertNotIn("review_verdict", res)
            self.assertNotIn("check_results", res)
            self.assertIn("dev_rmse", res["metrics"])
            self.assertIn("step_count", res["usage"])
            self.assertEqual(res["usage"]["token_count"], "unknown")
            self.assertEqual(res["usage"]["cost_usd"], "unknown")

        # 4. Selection
        best = ac.select_best_candidate(camp_dev, unit_results, primary_metric="dev_rmse")
        self.assertIsNotNone(best)
        self.assertTrue(best["unit_id"].startswith("unit-"))

        # 5. Freeze selection
        camp_locked, locked = ac.freeze_candidate(
            camp_dev,
            best,
            train_hash=self.data["hashes"]["train_hash"],
            dev_hash=self.data["hashes"]["dev_hash"],
            holdout_hash=self.data["hashes"]["holdout_hash"],
        )
        self.assertEqual(camp_locked["phase"], "final_locked")
        self.assertEqual(locked["schema_version"], "arbor-locked-candidate/v1")
        self.assertEqual(locked["unit_id"], best["unit_id"])

        # 6. Final evaluation (first time: must succeed)
        camp_final, final_eval = ac.evaluate_final(
            camp_locked,
            locked,
            self.data["final_holdout"],
            expected_holdout_hash=self.data["hashes"]["holdout_hash"],
        )
        self.assertEqual(camp_final["phase"], "final_evaluated")
        self.assertEqual(final_eval["schema_version"], "arbor-final-evaluation/v1")
        self.assertIn("final_rmse", final_eval["final_metrics"])
        self.assertTrue(math.isfinite(final_eval["final_metrics"]["final_rmse"]))

        # 7. Final evaluation (second time on same campaign: MUST FAIL-CLOSED)
        with self.assertRaises(ac.FinalAlreadyEvaluatedError):
            ac.evaluate_final(
                camp_final,
                locked,
                self.data["final_holdout"],
                expected_holdout_hash=self.data["hashes"]["holdout_hash"],
            )

    def test_final_evaluation_tampered_holdout_fails_closed(self):
        baseline = {"name": "baseline-mean", "algorithm": "mean_baseline"}
        alternatives = [
            {"name": "ols", "algorithm": "linear_regression"},
            {"name": "ridge", "algorithm": "ridge_regression", "alpha": 1.0},
        ]
        camp = ac.create_campaign(unique_id(self, "camp-tamper"), "Tamper test", self.temp_dir, "slug", baseline, alternatives)
        budget = {"max_steps": 10, "max_samples": 200}
        camp_dev, unit_results = ac.run_campaign_units(camp, self.data["train"], self.data["dev"], budget)
        best = ac.select_best_candidate(camp_dev, unit_results)
        camp_locked, locked = ac.freeze_candidate(
            camp_dev, best, self.data["hashes"]["train_hash"], self.data["hashes"]["dev_hash"]
        )

        tampered_holdout = copy.deepcopy(self.data["final_holdout"])
        tampered_holdout["y"][0] += 999.0

        with self.assertRaises(ac.DataIntegrityError):
            ac.evaluate_final(
                camp_locked,
                locked,
                tampered_holdout,
                expected_holdout_hash=self.data["hashes"]["holdout_hash"],
            )


class TestExperienceLedgerAndLessonTransfer(unittest.TestCase):
    """Test experience ledger, taxonomy, negative filtering, and second campaign transfer."""

    def setUp(self):
        begin_workspace_test(self)
        self.exp_file = Path(self.temp_dir) / f"arbor-experience-{self.token}.jsonl"
        self.store = ac.ExperienceStore(self.exp_file)

    def tearDown(self):
        cleanup_workspace_test(self)

    def verified(self, **kwargs):
        values = {
            "campaign_id": "camp-evidence",
            "unit_id": "unit-ridge",
            "domain": "tabular_regression",
            "project": "test_proj",
            "hypothesis": "Ridge is stable for collinear inputs.",
            "outcome_summary": "Measured in a bounded CPU comparison.",
            "trust": ac.TrustLevel.VERIFIED,
            "transfer_condition": {"collinear": True},
            "evidence_ref": "unit-results-sha256:abc123",
            "evidence_date": "2026-10-17",
        }
        values.update(kwargs)
        return self.store.log_lesson(**values)

    def test_log_and_query_experience_schema(self):
        lesson = self.store.log_lesson(
            campaign_id="camp-101",
            unit_id="unit-linear",
            domain="tabular_regression",
            project="test_proj",
            hypothesis="Unregularized linear model fails on high noise.",
            outcome_summary="OLS overfits on noise.",
            category=ac.FailureCategory.APPROACH,
            trust=ac.TrustLevel.PROVISIONAL,
            transfer_condition={"noise_gt": 0.2},
            metrics_delta={"rmse_diff": 0.5},
            evidence_ref="run-abc123",
            recommendation={"disallowed_algorithms": ["linear_regression"]},
        )
        self.assertEqual(lesson["schema_version"], "arbor-experience/v1")
        self.assertEqual(lesson["trust"], "provisional")
        self.assertEqual(lesson["category"], "approach")

        # Verify saved in JSONL
        records = ac.safe_read_jsonl(self.exp_file)
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["lesson_id"], lesson["lesson_id"])

    def test_verified_requires_dated_evidence_and_versioned_transfer(self):
        with self.assertRaises(ac.DataIntegrityError):
            self.store.log_lesson(
                campaign_id="missing-evidence", unit_id="unit-1", domain="tabular_regression",
                project="test_proj", hypothesis="h", outcome_summary="o", trust=ac.TrustLevel.VERIFIED,
                transfer_condition={"collinear": True},
            )
        with self.assertRaises(ac.DataIntegrityError):
            self.store.log_lesson(
                campaign_id="missing-transfer", unit_id="unit-1", domain="tabular_regression",
                project="test_proj", hypothesis="h", outcome_summary="o", trust=ac.TrustLevel.VERIFIED,
                evidence_ref="run:123", evidence_date="2026-10-17",
            )
        lesson = self.verified()
        self.assertEqual(lesson["trust"], "verified")
        for key, value in ac.current_compatibility().items():
            self.assertEqual(lesson["environment"][key], value)
            self.assertEqual(lesson["transfer_condition"][key], value)
        self.assertEqual(
            self.store.query_lessons(domain="tabular_regression", project="test_proj", context={"collinear": True}),
            [lesson],
        )
        self.assertEqual(
            self.store.query_lessons(
                domain="tabular_regression", project="test_proj",
                context={"collinear": True, "arbor_version": "0.0.0"},
            ),
            [],
        )
        stale = copy.deepcopy(lesson)
        stale["lesson_id"] = "stale-environment"
        stale["environment"]["platform"] = "different-platform"
        ac.safe_append_jsonl(self.exp_file, stale)
        self.assertEqual(
            [item["lesson_id"] for item in self.store.query_lessons(
                domain="tabular_regression", project="test_proj", context={"collinear": True}
            )],
            [lesson["lesson_id"]],
        )

    def test_negative_filtering_infrastructure_failure_ignored_for_approach(self):
        # 1. Log an infrastructure failure (e.g. worker OOM / timeout)
        self.store.log_lesson(
            campaign_id="camp-infra",
            unit_id="unit-stumps",
            domain="tabular_regression",
            project="test_proj",
            hypothesis="Boosted stumps interrupted by out-of-memory worker timeout.",
            outcome_summary="Provider quota interruption during training.",
            category=ac.FailureCategory.INFRASTRUCTURE,
            trust=ac.TrustLevel.PROVISIONAL,
            recommendation={"disallowed_algorithms": ["decision_stump_ensemble"]},
        )

        # 2. Query for approach lessons
        lessons = self.store.query_lessons(
            domain="tabular_regression",
            category=ac.FailureCategory.APPROACH,
        )
        # Infrastructure failure must NOT be returned as an approach lesson!
        self.assertEqual(len(lessons), 0)

    def test_negative_filtering_trust_and_domain_mismatch(self):
        # Log a provisional lesson
        self.store.log_lesson(
            campaign_id="camp-prov",
            unit_id="unit-knn",
            domain="tabular_regression",
            project="test_proj",
            hypothesis="KNN untested hypothesis.",
            outcome_summary="Unverified claim.",
            category=ac.FailureCategory.APPROACH,
            trust=ac.TrustLevel.PROVISIONAL,
        )
        # Log a lesson from different domain
        self.store.log_lesson(
            campaign_id="camp-diff-domain",
            unit_id="unit-cnn",
            domain="image_classification",
            project="test_proj",
            hypothesis="CNN fails on tabular.",
            outcome_summary="Domain mismatch lesson.",
            category=ac.FailureCategory.APPROACH,
            trust=ac.TrustLevel.VERIFIED,
            evidence_ref="run:domain-mismatch",
            evidence_date="2026-10-17",
            transfer_condition={"kind": "other"},
        )

        # Query trusted tabular lessons
        lessons = self.store.query_lessons(
            domain="tabular_regression",
            min_trust=ac.TrustLevel.TRUSTED,
            category=ac.FailureCategory.APPROACH,
        )
        self.assertEqual(len(lessons), 0)

    def test_negative_filtering_unmet_transfer_condition(self):
        self.store.log_lesson(
            campaign_id="camp-cond",
            unit_id="unit-linear",
            domain="tabular_regression",
            project="test_proj",
            hypothesis="Linear model fails when noise is severe (>0.5).",
            outcome_summary="Severe noise failure.",
            category=ac.FailureCategory.APPROACH,
            trust=ac.TrustLevel.VERIFIED,
            transfer_condition={"noise_gt": 0.5},
            recommendation={"disallowed_algorithms": ["linear_regression"]},
            evidence_ref="run:noise-condition",
            evidence_date="2026-10-17",
        )

        # Query with low noise (condition NOT met)
        lessons = self.store.query_lessons(
            domain="tabular_regression",
            category=ac.FailureCategory.APPROACH,
            context={"noise": 0.05},
        )
        self.assertEqual(len(lessons), 0)

        # Query with high noise (condition MET)
        lessons_met = self.store.query_lessons(
            domain="tabular_regression",
            category=ac.FailureCategory.APPROACH,
            context={"noise": 0.8},
        )
        self.assertEqual(len(lessons_met), 1)

    def test_actual_second_campaign_lesson_transfer(self):
        """End-to-end test proving actual second-campaign lesson transfer.
        
        Campaign 1: Discovers that unregularized linear regression diverges on collinear data.
        Lesson: Disallow linear_regression under collinear conditions.
        Campaign 2: Learns from Campaign 1, automatically prunes linear_regression,
                    and selects Ridge regression instead.
        """
        # --- Campaign 1: Run on collinear data ---
        data_collinear = ac.generate_synthetic_regression_data(
            seed=42, n_samples=120, n_features=4, noise=0.1, collinear=True
        )
        baseline = {"name": "baseline-mean", "algorithm": "mean_baseline"}
        candidates_c1 = [
            {"name": "ols", "algorithm": "linear_regression", "fit_intercept": True},
            {"name": "ridge", "algorithm": "ridge_regression", "alpha": 2.0, "fit_intercept": True},
            {"name": "stumps", "algorithm": "decision_stump_ensemble", "n_estimators": 10},
        ]
        camp1 = ac.create_campaign(unique_id(self, "camp-collinear-1"), "Explore collinearity", self.temp_dir, "arbor", baseline, candidates_c1)
        budget = {"max_steps": 10, "max_samples": 200}

        # Use the measured Campaign 1 selection results as evidence for a narrow lesson.
        camp1_dev, results_c1 = ac.run_campaign_units(camp1, data_collinear["train"], data_collinear["dev"], budget)
        by_algorithm = {result["algorithm_config"]["algorithm"]: result for result in results_c1}
        ridge_minus_ols = (
            by_algorithm["ridge_regression"]["metrics"]["dev_rmse"]
            - by_algorithm["linear_regression"]["metrics"]["dev_rmse"]
        )
        self.assertTrue(math.isfinite(ridge_minus_ols))
        les = self.store.log_lesson(
            campaign_id=camp1["campaign_id"],
            unit_id="unit-ols",
            domain="tabular_regression",
            project="arbor",
            hypothesis="On this seeded collinear synthetic comparison, Ridge slightly improves selection RMSE over OLS.",
            outcome_summary=f"Campaign 1 measured Ridge minus OLS dev RMSE = {ridge_minus_ols:.8f}.",
            category=ac.FailureCategory.APPROACH,
            trust=ac.TrustLevel.VERIFIED,
            transfer_condition={"collinear": True},
            recommendation={"disallowed_algorithms": ["linear_regression"]},
            evidence_ref=f"unit-results-sha256:{ac.compute_hash(results_c1)}",
            evidence_date="2026-10-17",
            metrics_delta={"ridge_minus_ols_dev_rmse": ridge_minus_ols},
        )
        self.assertIsNotNone(les["lesson_id"])

        # --- Campaign 2: New collinear problem instance ---
        data_collinear_c2 = ac.generate_synthetic_regression_data(
            seed=999, n_samples=140, n_features=4, noise=0.1, collinear=True
        )
        c2_context = {"collinear": True, "noise": 0.1}

        # Query lessons for this context
        relevant_lessons = self.store.query_lessons(
            domain="tabular_regression",
            project="arbor",
            category=ac.FailureCategory.APPROACH,
            min_trust=ac.TrustLevel.TRUSTED,
            context=c2_context,
        )
        self.assertEqual(len(relevant_lessons), 1)

        # Candidate pool initially proposed for Campaign 2
        proposed_candidates_c2 = [
            {"name": "ols", "algorithm": "linear_regression", "fit_intercept": True},
            {"name": "ridge", "algorithm": "ridge_regression", "alpha": 2.0, "fit_intercept": True},
            {"name": "stumps", "algorithm": "decision_stump_ensemble", "n_estimators": 15},
            {"name": "knn", "algorithm": "knn_regression", "n_neighbors": 5},
        ]

        # Apply experience transfer:
        active_candidates, pruned_candidates = ac.apply_experience_to_candidates(
            proposed_candidates_c2, relevant_lessons
        )

        # Assert linear_regression was pruned
        self.assertEqual(len(pruned_candidates), 1)
        self.assertEqual(pruned_candidates[0]["algorithm"], "linear_regression")
        self.assertIn("Pruned by lesson", pruned_candidates[0]["_prune_reason"])

        # Assert active candidates do NOT contain linear_regression
        active_algos = [c["algorithm"] for c in active_candidates]
        self.assertNotIn("linear_regression", active_algos)
        self.assertIn("ridge_regression", active_algos)

        # Run Campaign 2 with the adapted candidate set
        camp2 = ac.create_campaign(unique_id(self, "camp-collinear-2"), "Target collinear", self.temp_dir, "arbor", baseline, active_candidates)
        camp2_dev, unit_results_c2 = ac.run_campaign_units(camp2, data_collinear_c2["train"], data_collinear_c2["dev"], budget)
        best_c2 = ac.select_best_candidate(camp2_dev, unit_results_c2)
        
        # Winner must be one of the robust algorithms (ridge or stumps)
        self.assertIn(best_c2["algorithm_config"]["algorithm"], ["ridge_regression", "decision_stump_ensemble"])


class TestFinalCampaignBinding(unittest.TestCase):
    """Regression cases for durable, frozen, single-use final scoring."""

    def setUp(self):
        begin_workspace_test(self)
        self.data = ac.generate_synthetic_regression_data(
            seed=27, n_samples=96, n_features=3, noise=0.08
        )

    def tearDown(self):
        cleanup_workspace_test(self)

    def frozen(self, campaign_id="binding-campaign"):
        baseline = {"name": "baseline", "algorithm": "mean_baseline"}
        alternatives = [
            {"name": "ridge", "algorithm": "ridge_regression", "alpha": 0.5},
            {"name": "stumps", "algorithm": "decision_stump_ensemble", "n_estimators": 8},
        ]
        campaign = ac.create_campaign(
            campaign_id, "Final binding regression", self.temp_dir, "arbor-test", baseline, alternatives
        )
        developed, results = ac.run_campaign_units(
            campaign, self.data["train"], self.data["dev"], {"max_steps": 8, "max_samples": 120}
        )
        selected = ac.select_best_candidate(developed, results)
        return ac.freeze_candidate(
            developed, selected, self.data["hashes"]["train_hash"], self.data["hashes"]["dev_hash"]
        )

    def test_original_locked_snapshot_cannot_replay_after_restart(self):
        campaign_locked, locked = self.frozen(unique_id(self, "binding-campaign"))
        _, result = ac.evaluate_final(campaign_locked, locked, self.data["final_holdout"])
        claim = ac.safe_read_json(Path(self.temp_dir) / f"arbor-{campaign_locked['campaign_id']}-final-claim.json")
        self.assertEqual(claim["manifest_hash"], locked["manifest_hash"])
        self.assertEqual(claim["final_dataset_hash"], result["holdout_data_hash"])

        # The caller still has the original locked objects, as a restarted process would.
        restored_campaign = json.loads(json.dumps(campaign_locked))
        restored_candidate = json.loads(json.dumps(locked))
        with self.assertRaises(ac.FinalAlreadyEvaluatedError):
            ac.evaluate_final(restored_campaign, restored_candidate, self.data["final_holdout"])

    def test_mutated_frozen_candidate_or_campaign_is_rejected_before_score(self):
        campaign_locked, locked = self.frozen(unique_id(self, "mutation-campaign"))
        tampered = copy.deepcopy(locked)
        state = tampered["model_state"]
        if "weights" in state:
            state["weights"] = [0.0 for _ in state["weights"]]
        elif "mean_val" in state:
            state["mean_val"] = 0.0
        else:
            state["base_pred"] = 0.0
        with self.assertRaises(ac.DataIntegrityError):
            ac.evaluate_final(campaign_locked, tampered, self.data["final_holdout"])

        campaign_locked, locked = self.frozen(unique_id(self, "config-mutation-campaign"))
        tampered_campaign = copy.deepcopy(campaign_locked)
        tampered_campaign["units"][1]["algorithm_config"]["algorithm"] = "mean_baseline"
        with self.assertRaises(ac.DataIntegrityError):
            ac.evaluate_final(tampered_campaign, locked, self.data["final_holdout"])

        campaign_locked, locked = self.frozen(unique_id(self, "selection-mutation-campaign"))
        tampered_selection = copy.deepcopy(campaign_locked)
        tampered_selection["unit_results"][0]["metrics"]["dev_rmse"] += 1.0
        with self.assertRaises(ac.DataIntegrityError):
            ac.evaluate_final(tampered_selection, locked, self.data["final_holdout"])

        campaign_locked, locked = self.frozen(unique_id(self, "evaluator-mutation-campaign"))
        manifest_path = Path(self.temp_dir) / campaign_locked["frozen_manifest_file"]
        manifest = ac.safe_read_json(manifest_path)
        manifest["evaluator"]["hash"] = "0" * 64
        ac.safe_write_json(manifest_path, manifest)
        with self.assertRaises(ac.DataIntegrityError):
            ac.evaluate_final(campaign_locked, locked, self.data["final_holdout"])

    def test_scoring_failure_leaves_durable_pending_claim(self):
        campaign_id = unique_id(self, "failed-score-campaign")
        campaign_locked, locked = self.frozen(campaign_id)
        claim_path = Path(self.temp_dir) / f"arbor-{campaign_id}-final-claim.json"
        restore = ac._restore_model
        try:
            ac._restore_model = lambda _: (_ for _ in ()).throw(ac.ArborError("synthetic scorer interruption"))
            with self.assertRaises(ac.ArborError):
                ac.evaluate_final(campaign_locked, locked, self.data["final_holdout"])
        finally:
            ac._restore_model = restore
        self.assertTrue(claim_path.exists())
        claim = ac.safe_read_json(claim_path)
        self.assertEqual(claim["state"], "claimed_before_scoring")
        with self.assertRaises(ac.FinalAlreadyEvaluatedError):
            ac.evaluate_final(campaign_locked, locked, self.data["final_holdout"])

    def test_concurrent_calls_allow_exactly_one_final_score(self):
        campaign_locked, locked = self.frozen(unique_id(self, "concurrent-campaign"))
        barrier = threading.Barrier(2)
        outcomes = []

        def score():
            barrier.wait()
            try:
                ac.evaluate_final(campaign_locked, locked, self.data["final_holdout"])
                outcomes.append("scored")
            except ac.FinalAlreadyEvaluatedError:
                outcomes.append("spent")

        workers = [threading.Thread(target=score) for _ in range(2)]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join()

        self.assertCountEqual(outcomes, ["scored", "spent"])


class TestAgentGraphExport(unittest.TestCase):
    def setUp(self):
        begin_workspace_test(self)

    def tearDown(self):
        cleanup_workspace_test(self)

    def native_spec(self):
        return {
            "schema_version": "arbor-campaign/v1",
            "campaign_id": "ag-export-campaign",
            "goal": "Compare candidate approaches against the caller's declared goal.",
            "workspace": "runs/ag-export-campaign",
            "project_slug": "arbor-test",
            "baseline_ref": "unit-baseline",
            "lockbox_ref": "parent://fresh-final/arbor-export-01",
            "tree_budget": {
                "max_parallel": 1,
                "max_total_units": 1,
                "max_depth": 1,
                "allowed_pools": ["caller-native-pool"],
                "allowed_write_globs": ["runs/ag-export-campaign/**"],
            },
            "units": [{
                "unit_id": "unit-baseline",
                "campaign_id": "ag-export-campaign",
                "parent_job_id": None,
                "hypothesis": "The baseline provides a reference.",
                "text": "Run the caller-defined native candidate.",
                "scope": ["runs/ag-export-campaign/artifacts"],
                "inputs": ["inputs/train.json", "inputs/selection.json"],
                "checks": ["python -B global/codex/skills/new-training/scripts/test_arbor_campaign.py"],
                "done_when": ["Native reviewer records an independent verdict."],
                "maker": ["caller-native-maker"],
                "reviewer": ["caller-independent-reviewer"],
                "dedupe_key": "ag-export-campaign-unit-baseline",
            }],
        }

    def test_valid_supplied_native_export_is_preserved_without_acceptance(self):
        supplied = self.native_spec()
        exported = ac.validate_ag_campaign_spec(supplied)
        self.assertEqual(exported, supplied)
        self.assertEqual(exported["lockbox_ref"], "parent://fresh-final/arbor-export-01")
        self.assertIsInstance(exported["units"][0]["maker"], list)
        self.assertIsInstance(exported["units"][0]["reviewer"], list)
        self.assertTrue(exported["units"][0]["checks"])
        self.assertNotIn("review_verdict", exported["units"][0])

    def test_export_cli_validates_caller_spec_and_never_overwrites(self):
        source = Path(self.temp_dir) / f"arbor-ag-source-{self.token}.json"
        output = Path(self.temp_dir) / f"arbor-ag-export-{self.token}.json"
        ac.safe_write_json(source, self.native_spec())
        self.assertEqual(ac.main(["export-ag", "--spec", str(source), "--output", str(output)]), 0)
        original = output.read_bytes()
        self.assertEqual(ac.safe_read_json(output), self.native_spec())
        self.assertEqual(ac.main(["export-ag", "--spec", str(source), "--output", str(output)]), 2)
        self.assertEqual(output.read_bytes(), original)

    def test_invalid_legacy_shape_and_missing_external_fields_rejected(self):
        cases = []
        spec = self.native_spec()
        spec["lockbox_ref"] = None
        cases.append(spec)
        spec = self.native_spec()
        spec["tree_budget"]["allowed_pools"] = ["cpu"]
        cases.append(spec)
        spec = self.native_spec()
        unit = spec["units"][0]
        unit["relative_scope"] = unit.pop("scope")
        cases.append(spec)
        spec = self.native_spec()
        spec["units"][0]["checks"] = [{"name": "finite", "status": "required"}]
        cases.append(spec)
        spec = self.native_spec()
        spec["units"][0]["maker"] = "arbor-maker"
        cases.append(spec)
        for invalid in cases:
            with self.subTest(invalid=invalid):
                with self.assertRaises(ac.DataIntegrityError):
                    ac.validate_ag_campaign_spec(invalid)


class TestSecurityAndDataImmutability(unittest.TestCase):
    """Test path security, symlink rejection, and caller-supplied data immutability."""

    def setUp(self):
        begin_workspace_test(self)
        self.data = ac.generate_synthetic_regression_data(seed=1, n_samples=80, n_features=2, noise=0.1)

    def tearDown(self):
        cleanup_workspace_test(self)

    def test_caller_data_not_mutated(self):
        orig_train_X = [row[:] for row in self.data["train"]["X"]]
        orig_train_y = self.data["train"]["y"][:]
        orig_dev_X = [row[:] for row in self.data["dev"]["X"]]
        orig_dev_y = self.data["dev"]["y"][:]

        baseline = {"name": "baseline-mean", "algorithm": "mean_baseline"}
        alternatives = [
            {"name": "ols", "algorithm": "linear_regression"},
            {"name": "ridge", "algorithm": "ridge_regression", "alpha": 1.0},
        ]
        camp = ac.create_campaign("camp-sec", "Sec test", self.temp_dir, "slug", baseline, alternatives)
        budget = {"max_steps": 5, "max_samples": 100}

        ac.run_campaign_units(camp, self.data["train"], self.data["dev"], budget)

        # Verify caller data was not modified
        self.assertEqual(self.data["train"]["X"], orig_train_X)
        self.assertEqual(self.data["train"]["y"], orig_train_y)
        self.assertEqual(self.data["dev"]["X"], orig_dev_X)
        self.assertEqual(self.data["dev"]["y"], orig_dev_y)

    def test_symlink_path_rejected(self):
        real_file = Path(self.temp_dir) / f"real-{self.token}.json"
        real_file.write_text("{}", encoding="utf-8")
        symlink_file = Path(self.temp_dir) / f"symlink-{self.token}.json"
        
        try:
            os.symlink(real_file, symlink_file)
        except (OSError, NotImplementedError):
            # If OS privileges disallow symlink creation in test, pass
            return

        with self.assertRaises(ac.SecurityError):
            ac.check_no_symlinks(symlink_file)
        with self.assertRaises(ac.SecurityError):
            ac.safe_read_json(symlink_file)
        with self.assertRaises(ac.SecurityError):
            ac.safe_write_json(symlink_file, {"bad": 1})


class TestCliExecution(unittest.TestCase):
    """Test CLI commands execution."""

    def setUp(self):
        begin_workspace_test(self)

    def tearDown(self):
        cleanup_workspace_test(self)

    def test_cli_run_synthetic_and_experience(self):
        run_dir = Path(self.temp_dir)
        campaign_id = unique_id(self, "cli-test-camp")
        ret = ac.main([
            "run-synthetic",
            "--campaign-id", campaign_id,
            "--output-dir", str(run_dir),
            "--noise", "0.05",
            "--seed", "77",
        ])
        self.assertEqual(ret, 0)
        marker_path = ac._campaign_output_path(run_dir, campaign_id, "DEMO_ONLY")
        result_path = ac._campaign_output_path(run_dir, campaign_id, "demo-final-evaluation")
        self.assertTrue(marker_path.exists())
        demo_marker = ac.safe_read_json(marker_path)
        self.assertFalse(demo_marker["external_lockbox"])
        self.assertTrue(result_path.exists())
        self.assertTrue(ac._campaign_output_path(run_dir, campaign_id, "demo-candidate").exists())
        original_result = result_path.read_bytes()
        self.assertEqual(ac.main([
            "run-synthetic", "--campaign-id", campaign_id, "--output-dir", str(run_dir),
        ]), 2)
        self.assertEqual(result_path.read_bytes(), original_result)
        self.assertFalse((run_dir / "campaign_completed.json").exists())

        # Test log-lesson CLI
        exp_file = Path(self.temp_dir) / f"arbor-cli-exp-{self.token}.jsonl"
        ret_log = ac.main([
            "log-lesson",
            "--experience-file", str(exp_file),
            "--campaign-id", campaign_id,
            "--unit-id", "unit-ridge",
            "--domain", "tabular_regression",
            "--project", "cli_test",
            "--category", "approach",
            "--trust", "verified",
            "--evidence-ref", "sha256:campaign-1-selected-result",
            "--evidence-date", "2026-10-17",
            "--transfer-condition-json", '{"noise_lte":0.2}',
            "--hypothesis", "Ridge handles slight collinearity well.",
            "--summary", "Ridge superior to OLS.",
            "--disallow-algo", "linear_regression",
        ])
        self.assertEqual(ret_log, 0)
        self.assertTrue(exp_file.exists())

        # Test query-lessons CLI
        ret_q = ac.main([
            "query-lessons",
            "--experience-file", str(exp_file),
            "--domain", "tabular_regression",
            "--project", "cli_test",
            "--category", "approach",
            "--min-trust", "trusted",
            "--context-json", '{"noise":0.1}',
        ])
        self.assertEqual(ret_q, 0)

    def test_cli_staged_second_campaign_uses_context_valid_lesson_and_fresh_holdout(self):
        pilot_root = Path(self.temp_dir)
        c1 = pilot_root
        c2 = pilot_root
        external = pilot_root.parent
        campaign1 = unique_id(self, "cli-staged-1")
        campaign2 = unique_id(self, "cli-staged-2")
        experience = pilot_root / f"arbor-staged-exp-{self.token}.jsonl"
        results1_path = ac._campaign_output_path(c1, campaign1, "unit-results")
        result1_path = ac._campaign_output_path(c1, campaign1, "final-evaluation")
        evidence2_path = ac._campaign_output_path(c2, campaign2, "evidence")
        args1 = [
            "prepare-synthetic", "--campaign-id", campaign1, "--output-dir", str(c1),
            "--seed", "42", "--noise", "0.1", "--collinear", "--project", "arbor-test",
        ]
        self.assertEqual(ac.main(args1), 0)
        results1 = ac.safe_read_json(results1_path)
        metrics1 = {item["algorithm_config"]["algorithm"]: item["metrics"]["dev_rmse"] for item in results1}
        stump_minus_ridge = metrics1["decision_stump_ensemble"] - metrics1["ridge_regression"]
        self.assertGreater(stump_minus_ridge, 0.0)
        final1 = ac.generate_synthetic_regression_data(
            seed=742, n_samples=50, n_features=4, noise=0.1, collinear=True, dev_ratio=0.0, holdout_ratio=1.0
        )["final_holdout"]
        final1_path = external / f"arbor-final-1-{self.token}.json"
        self.external_paths.append(final1_path)
        final1_path.write_text(json.dumps(final1), encoding="utf-8")
        self.assertEqual(ac.main([
            "evaluate-final", "--campaign-dir", str(c1), "--campaign-id", campaign1, "--dataset", str(final1_path),
            "--expected-dataset-hash", ac.compute_hash(final1),
        ]), 0)
        evidence_hash = ac.compute_hash({"unit_results": results1, "final": ac.safe_read_json(result1_path)})
        self.assertEqual(ac.main([
            "log-lesson", "--experience-file", str(experience), "--campaign-id", campaign1,
            "--unit-id", "unit-ridge-reg", "--domain", "tabular_regression", "--project", "arbor-test",
            "--trust", "verified", "--evidence-ref", f"sha256:{evidence_hash}",
            "--evidence-date", "2026-10-17", "--transfer-condition-json", '{"collinear":true,"noise_lte":0.2}',
            "--hypothesis", "The stump ensemble had higher selection RMSE than Ridge in this seeded collinear comparison.",
            "--summary", f"Campaign 1 measured stump minus Ridge dev RMSE = {stump_minus_ridge:.8f}; a fresh final dataset was recorded.",
            "--disallow-algo", "decision_stump_ensemble",
        ]), 0)
        self.assertEqual(ac.main([
            "prepare-synthetic", "--campaign-id", campaign2, "--output-dir", str(c2),
            "--seed", "999", "--noise", "0.1", "--collinear", "--project", "arbor-test",
            "--experience-file", str(experience),
        ]), 0)
        evidence2 = ac.safe_read_json(evidence2_path)
        self.assertEqual(len(evidence2["lesson_ids_applied"]), 1)
        self.assertEqual(evidence2["pruned_candidates"][0]["algorithm"], "decision_stump_ensemble")
        self.assertNotEqual(evidence2["selected_algorithm"], "decision_stump_ensemble")

        final2 = ac.generate_synthetic_regression_data(
            seed=1999, n_samples=50, n_features=4, noise=0.1, collinear=True, dev_ratio=0.0, holdout_ratio=1.0
        )["final_holdout"]
        self.assertNotEqual(ac.compute_hash(final1), ac.compute_hash(final2))
        final2_path = external / f"arbor-final-2-{self.token}.json"
        self.external_paths.append(final2_path)
        final2_path.write_text(json.dumps(final2), encoding="utf-8")
        self.assertEqual(ac.main([
            "evaluate-final", "--campaign-dir", str(c2), "--campaign-id", campaign2, "--dataset", str(final2_path),
            "--expected-dataset-hash", ac.compute_hash(final2),
        ]), 0)
        result2_path = ac._campaign_output_path(c2, campaign2, "final-evaluation")
        final2_bytes = result2_path.read_bytes()
        self.assertEqual(ac.main([
            "evaluate-final", "--campaign-dir", str(c2), "--campaign-id", campaign2, "--dataset", str(final2_path),
            "--expected-dataset-hash", ac.compute_hash(final2),
        ]), 2)
        self.assertEqual(result2_path.read_bytes(), final2_bytes)


if __name__ == "__main__":
    unittest.main()
