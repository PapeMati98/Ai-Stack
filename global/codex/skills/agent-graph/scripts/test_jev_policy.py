from datetime import datetime, timedelta, timezone
import unittest

from jev_policy import PolicyError, consultation_policy, recommend

NOW = datetime(2026, 9, 25, 12, 0, 0, tzinfo=timezone.utc)


def iso(dt):
    return dt.isoformat().replace("+00:00", "Z")


def make_task(**overrides):
    task = {
        "schema_version": 1,
        "brief": "Implement a bounded feature slice.",
        "mode": "implement",
        "required_tools": ["edit", "test"],
        "scope": ["repo/module_a"],
        "resources": [],
        "depends_on": [],
        "estimated_context_tokens": 4000,
        "domain": "coding",
        "high_risk": False,
    }
    task.update(overrides)
    return task


def make_candidate(candidate_id, lineage, **overrides):
    candidate = {
        "id": candidate_id,
        "agent": f"agent-{candidate_id}",
        "model": f"vendor/{lineage}-model",
        "lineage": lineage,
        "enabled": True,
        "training_allowed": False,
        "billing": "subscription",
        "tools": ["edit", "test", "read"],
        "modes": ["implement", "review"],
        "fit": {"coding": 0.8},
        "status": "available",
        "quota_remaining": 0.9,
        "quota_observed_at": iso(NOW - timedelta(seconds=30)),
        "quota_reset_at": None,
        "context_window_tokens": 100000,
        "capacity_remaining": 3,
    }
    candidate.update(overrides)
    return candidate


def make_snapshot(candidates, active_assignments=None, **overrides):
    snapshot = {
        "schema_version": 1,
        "captured_at": iso(NOW - timedelta(seconds=10)),
        "candidates": candidates,
        "active_assignments": active_assignments or [],
        "capacity": 4,
    }
    snapshot.update(overrides)
    return snapshot


def make_decision(fit=None, **overrides):
    decision = {
        "model": "typesafe/jev-1.13-20260917",
        "provider": "TypeSafe",
        "decision_id": "dec-1",
        "difficulty": {"value": "low", "confidence": 0.9},
        "parallelism": {"value": "sequential", "confidence": 0.9},
        "consultation": {"value": "none", "confidence": 0.9},
        "fit": fit or {},
        "usage": {"input_tokens": 10, "output_tokens": 5, "cost_usd": 0.00001},
        "pricing_basis": "input_per_token",
    }
    decision.update(overrides)
    return decision


class SchemaValidationTests(unittest.TestCase):
    def test_optional_model_metadata_is_truthful_and_validated(self):
        candidates = [make_candidate("a", "lineage-a", model_class="unknown", coding_rank=None),
                      make_candidate("b", "lineage-b", model_class="opus", coding_rank=2)]
        result = recommend(make_task(), make_snapshot(candidates), make_decision(), now=NOW)
        self.assertEqual({row["id"]: row["model_class"] for row in result["maker"]},
                         {"a": "unknown", "b": "opus"})
        for changes in ({"model_class": "made-up"}, {"model_class": []},
                        {"coding_rank": True}, {"coding_rank": float("nan")},
                        {"coding_rank": -1}):
            with self.assertRaises(PolicyError):
                recommend(make_task(), make_snapshot([make_candidate("bad", "lineage", **changes)]),
                          make_decision(), now=NOW)

    def test_rejects_malformed_task(self):
        candidate = make_candidate("a", "lineage-a")
        snapshot = make_snapshot([candidate])
        with self.assertRaises(PolicyError):
            recommend({"not": "a task"}, snapshot, make_decision(), now=NOW)

    def test_rejects_wrong_schema_version(self):
        task = make_task(schema_version=2)
        snapshot = make_snapshot([make_candidate("a", "lineage-a")])
        with self.assertRaises(PolicyError):
            recommend(task, snapshot, make_decision(), now=NOW)

    def test_rejects_malformed_snapshot(self):
        task = make_task()
        with self.assertRaises(PolicyError):
            recommend(task, {"candidates": []}, make_decision(), now=NOW)

    def test_rejects_duplicate_candidate_ids(self):
        task = make_task()
        snapshot = make_snapshot([make_candidate("a", "lineage-a"), make_candidate("a", "lineage-b")])
        with self.assertRaises(PolicyError):
            recommend(task, snapshot, make_decision(), now=NOW)

    def test_rejects_out_of_range_fit_score(self):
        task = make_task()
        snapshot = make_snapshot([make_candidate("a", "lineage-a")])
        with self.assertRaises(PolicyError):
            recommend(task, snapshot, make_decision(fit={"a": {"score": 9, "confidence": 0.9}}), now=NOW)


class EligibilityTests(unittest.TestCase):
    def _excluded_reasons(self, candidate, task=None):
        task = task or make_task()
        snapshot = make_snapshot([candidate, make_candidate("reviewer-only", "lineage-b")])
        result = recommend(task, snapshot, make_decision(), now=NOW)
        for entry in result["excluded"]:
            if entry["id"] == candidate["id"]:
                return entry["reasons"]
        return []

    def test_disabled_excluded(self):
        self.assertIn("DISABLED", self._excluded_reasons(make_candidate("a", "lineage-a", enabled=False)))

    def test_training_allowed_excluded(self):
        self.assertIn("TRAINING_NOT_ALLOWED", self._excluded_reasons(make_candidate("a", "lineage-a", training_allowed=True)))

    def test_mode_mismatch_excluded(self):
        self.assertIn("MODE_UNSUPPORTED", self._excluded_reasons(make_candidate("a", "lineage-a", modes=["advisory"])))

    def test_missing_tool_excluded(self):
        self.assertIn("TOOLS_UNSUPPORTED", self._excluded_reasons(make_candidate("a", "lineage-a", tools=["read"])))

    def test_unknown_context_window_excluded(self):
        self.assertIn("CONTEXT_UNKNOWN", self._excluded_reasons(make_candidate("a", "lineage-a", context_window_tokens=None)))

    def test_insufficient_context_window_excluded(self):
        candidate = make_candidate("a", "lineage-a", context_window_tokens=1000)
        self.assertIn("CONTEXT_INSUFFICIENT", self._excluded_reasons(candidate, make_task(estimated_context_tokens=4000)))

    def test_capacity_zero_excluded(self):
        self.assertIn("CAPACITY_ZERO", self._excluded_reasons(make_candidate("a", "lineage-a", capacity_remaining=0)))

    def test_exhausted_excluded(self):
        self.assertIn("EXHAUSTED", self._excluded_reasons(make_candidate("a", "lineage-a", status="exhausted")))

    def test_exhausted_not_cleared_by_unknown_quota(self):
        candidate = make_candidate(
            "a", "lineage-a", status="exhausted", quota_remaining=None, quota_observed_at=None,
        )
        self.assertIn("EXHAUSTED", self._excluded_reasons(candidate))

    def test_fresh_depleted_quota_excluded(self):
        candidate = make_candidate("a", "lineage-a", quota_remaining=0.0, quota_observed_at=iso(NOW - timedelta(seconds=5)))
        self.assertIn("QUOTA_DEPLETED", self._excluded_reasons(candidate))

    def test_stale_quota_not_treated_as_depleted(self):
        candidate = make_candidate(
            "a", "lineage-a", quota_remaining=0.0, quota_observed_at=iso(NOW - timedelta(seconds=1000)),
        )
        self.assertNotIn("QUOTA_DEPLETED", self._excluded_reasons(candidate))

    def test_unknown_status_not_excluded_by_itself(self):
        candidate = make_candidate("a", "lineage-a", status="unknown", quota_remaining=None, quota_observed_at=None)
        self.assertEqual(self._excluded_reasons(candidate), [])


class StalenessAndRankingTests(unittest.TestCase):
    def test_stale_snapshot_requires_parent_review(self):
        task = make_task()
        snapshot = make_snapshot(
            [make_candidate("a", "lineage-a"), make_candidate("b", "lineage-b")],
            captured_at=iso(NOW - timedelta(seconds=1000)),
        )
        result = recommend(task, snapshot, make_decision(), now=NOW)
        self.assertTrue(result["requires_parent_review"])
        self.assertIn("STALE_SNAPSHOT", result["requires_parent_review_reasons"])
        self.assertTrue(result["stale_snapshot"])

    def test_fresh_known_quota_outranks_stale_unknown_quota_at_equal_fit(self):
        fresh = make_candidate("fresh", "lineage-a", quota_remaining=0.9, quota_observed_at=iso(NOW - timedelta(seconds=10)))
        stale = make_candidate("stale", "lineage-b", quota_remaining=0.9, quota_observed_at=iso(NOW - timedelta(seconds=10000)))
        decision = make_decision(fit={
            "fresh": {"score": 3.0, "confidence": 0.9},
            "stale": {"score": 3.0, "confidence": 0.9},
        })
        result = recommend(make_task(), make_snapshot([fresh, stale]), decision, now=NOW)
        maker_ids = [entry["id"] for entry in result["maker"]]
        self.assertEqual(maker_ids[0], "fresh")

    def test_subscription_preferred_over_api_at_equal_score(self):
        sub = make_candidate("sub", "lineage-a", billing="subscription")
        api = make_candidate("api", "lineage-b", billing="api")
        decision = make_decision(fit={
            "sub": {"score": 3.0, "confidence": 0.9},
            "api": {"score": 3.0, "confidence": 0.9},
        })
        result = recommend(make_task(), make_snapshot([sub, api]), decision, now=NOW)
        maker_ids = [entry["id"] for entry in result["maker"]]
        self.assertEqual(maker_ids[0], "sub")

    def test_low_confidence_forces_parent_review(self):
        candidate = make_candidate("a", "lineage-a")
        other = make_candidate("b", "lineage-b")
        decision = make_decision(fit={"a": {"score": 3.0, "confidence": 0.2}, "b": {"score": 3.0, "confidence": 0.9}})
        result = recommend(make_task(), make_snapshot([candidate, other]), decision, now=NOW)
        self.assertTrue(result["requires_parent_review"])
        self.assertIn("LOW_CONFIDENCE", result["requires_parent_review_reasons"])

    def test_high_risk_always_requires_parent_review(self):
        candidate = make_candidate("a", "lineage-a")
        other = make_candidate("b", "lineage-b")
        decision = make_decision(fit={"a": {"score": 3.5, "confidence": 0.95}, "b": {"score": 3.0, "confidence": 0.9}})
        result = recommend(make_task(high_risk=True), make_snapshot([candidate, other]), decision, now=NOW)
        self.assertIn("HIGH_RISK_TASK", result["requires_parent_review_reasons"])

    def test_load_spreads_across_active_assignments_at_equal_score(self):
        loaded = make_candidate("loaded", "lineage-a")
        idle = make_candidate("idle", "lineage-b")
        decision = make_decision(fit={
            "loaded": {"score": 3.0, "confidence": 0.9},
            "idle": {"score": 3.0, "confidence": 0.9},
        })
        assignments = [{"candidate_id": "loaded", "lineage": "lineage-a", "scope": ["other/x"], "resources": []}]
        result = recommend(make_task(), make_snapshot([loaded, idle], assignments), decision, now=NOW)
        maker_ids = [entry["id"] for entry in result["maker"]]
        self.assertEqual(maker_ids[0], "idle")


class MakerReviewerLineageTests(unittest.TestCase):
    def test_maker_and_reviewer_have_distinct_lineage(self):
        candidate_a = make_candidate("a", "lineage-a")
        candidate_b = make_candidate("b", "lineage-b")
        decision = make_decision(fit={"a": {"score": 3.5, "confidence": 0.9}, "b": {"score": 3.0, "confidence": 0.9}})
        result = recommend(make_task(), make_snapshot([candidate_a, candidate_b]), decision, now=NOW)
        self.assertEqual(result["maker"][0]["id"], "a")
        self.assertTrue(result["reviewer"])
        self.assertNotEqual(result["reviewer"][0]["lineage"], result["maker"][0]["lineage"])

    def test_missing_distinct_reviewer_escalates_instead_of_self_review(self):
        candidate_a = make_candidate("a", "lineage-a")
        candidate_b = make_candidate("b", "lineage-a")  # same lineage, different CLI label
        decision = make_decision(fit={"a": {"score": 3.5, "confidence": 0.9}, "b": {"score": 3.0, "confidence": 0.9}})
        result = recommend(make_task(), make_snapshot([candidate_a, candidate_b]), decision, now=NOW)
        self.assertEqual(result["reviewer"], [])
        self.assertIn("NO_DISTINCT_REVIEWER", result["requires_parent_review_reasons"])
        self.assertTrue(result["requires_parent_review"])

    def test_no_eligible_maker_escalates(self):
        candidate = make_candidate("a", "lineage-a", enabled=False)
        result = recommend(make_task(), make_snapshot([candidate]), make_decision(), now=NOW)
        self.assertEqual(result["maker"], [])
        self.assertIn("NO_ELIGIBLE_MAKER", result["requires_parent_review_reasons"])


class ParallelismTests(unittest.TestCase):
    def test_dependencies_force_sequential(self):
        task = make_task(depends_on=["other-task"])
        result = recommend(task, make_snapshot([make_candidate("a", "lineage-a"), make_candidate("b", "lineage-b")]), make_decision(), now=NOW)
        self.assertEqual(result["parallelism"]["recommendation"], "sequential")
        self.assertEqual(result["parallelism"]["max_parallel"], 1)

    def test_conflicting_active_scope_forces_sequential(self):
        task = make_task(scope=["repo/module_a"])
        assignments = [{"candidate_id": "x", "lineage": "lineage-x", "scope": ["repo/module_a"], "resources": []}]
        result = recommend(task, make_snapshot([make_candidate("a", "lineage-a"), make_candidate("b", "lineage-b")], assignments), make_decision(), now=NOW)
        self.assertEqual(result["parallelism"]["recommendation"], "sequential")
        self.assertIn("ACTIVE_SCOPE_OR_RESOURCE_CONFLICT", result["parallelism"]["reasons"])

    def test_traversal_path_treated_as_unknown_and_serialized(self):
        task = make_task(scope=["../escape"])
        assignments = [{"candidate_id": "x", "lineage": "lineage-x", "scope": ["repo/other"], "resources": []}]
        result = recommend(task, make_snapshot([make_candidate("a", "lineage-a"), make_candidate("b", "lineage-b")], assignments), make_decision(), now=NOW)
        self.assertEqual(result["parallelism"]["recommendation"], "sequential")

    def test_unknown_scope_with_no_active_assignments_stays_sequential(self):
        # No active assignments to conflict with must not be read as proof of
        # disjointness when the task's own scope cannot even be canonicalized.
        task = make_task(scope=["../escape"])
        decision = make_decision(parallelism={"value": "parallel", "confidence": 0.95})
        result = recommend(
            task,
            make_snapshot([make_candidate("a", "lineage-a"), make_candidate("b", "lineage-b")], capacity=4),
            decision,
            now=NOW,
        )
        self.assertEqual(result["parallelism"]["recommendation"], "sequential")
        self.assertEqual(result["parallelism"]["max_parallel"], 1)
        self.assertIn("UNKNOWN_TASK_SCOPE_OR_RESOURCES", result["parallelism"]["reasons"])

    def test_unknown_resources_with_no_active_assignments_stays_sequential(self):
        task = make_task(scope=["repo/module_a"], resources=["/etc/passwd"])
        decision = make_decision(parallelism={"value": "parallel", "confidence": 0.95})
        result = recommend(
            task,
            make_snapshot([make_candidate("a", "lineage-a"), make_candidate("b", "lineage-b")], capacity=4),
            decision,
            now=NOW,
        )
        self.assertEqual(result["parallelism"]["recommendation"], "sequential")
        self.assertIn("UNKNOWN_TASK_SCOPE_OR_RESOURCES", result["parallelism"]["reasons"])

    def test_disjoint_scope_and_capacity_allows_parallel(self):
        task = make_task(scope=["repo/module_a"])
        assignments = [{"candidate_id": "x", "lineage": "lineage-x", "scope": ["repo/module_b"], "resources": []}]
        decision = make_decision(parallelism={"value": "parallel", "confidence": 0.9})
        result = recommend(task, make_snapshot([make_candidate("a", "lineage-a"), make_candidate("b", "lineage-b")], assignments, capacity=4), decision, now=NOW)
        self.assertEqual(result["parallelism"]["recommendation"], "parallel")
        self.assertGreaterEqual(result["parallelism"]["max_parallel"], 2)

    def test_default_max_parallel_is_one_without_evidence(self):
        task = make_task()
        result = recommend(task, make_snapshot([make_candidate("a", "lineage-a")]), make_decision(), now=NOW)
        self.assertEqual(result["parallelism"]["max_parallel"], 1)


class ConsultationPolicyTests(unittest.TestCase):
    def test_routine_task_recommends_none(self):
        task = make_task()
        snapshot = make_snapshot([make_candidate("a", "lineage-a"), make_candidate("b", "lineage-b")])
        result = consultation_policy(task, snapshot, make_decision())
        self.assertEqual(result["recommendation"], "none")
        self.assertEqual(result["consultants"], [])

    def test_high_risk_recommends_subscription_consultants_from_distinct_lineages(self):
        task = make_task(high_risk=True)
        candidates = [
            make_candidate("a", "lineage-a", billing="subscription"),
            make_candidate("b", "lineage-b", billing="local"),
            make_candidate("c", "lineage-a", billing="subscription"),
        ]
        snapshot = make_snapshot(candidates)
        result = consultation_policy(task, snapshot, make_decision())
        self.assertEqual(result["recommendation"], "subscription")
        lineages = [c["lineage"] for c in result["consultants"]]
        self.assertEqual(len(lineages), len(set(lineages)))

    def test_api_requires_budget_and_justification(self):
        task = make_task(high_risk=True)
        snapshot = make_snapshot([make_candidate("a", "lineage-a", billing="subscription")])
        result = consultation_policy(
            task, snapshot, make_decision(), explicit_api_models=("vendor/other-model",),
        )
        self.assertEqual(result["api_consultants"], [])
        self.assertIn("API_BUDGET_MISSING_OR_INVALID", result["reasons"])
        self.assertIn("API_JUSTIFICATION_MISSING", result["reasons"])

    def test_api_approved_with_explicit_budget_and_justification(self):
        task = make_task(high_risk=True)
        snapshot = make_snapshot([make_candidate("a", "lineage-a", billing="subscription")])
        result = consultation_policy(
            task, snapshot, make_decision(),
            api_budget_usd=0.5, justification="need external second opinion for novel domain risk",
            explicit_api_models=("vendor/other-model",),
        )
        self.assertEqual([c["model"] for c in result["api_consultants"]], ["vendor/other-model"])

    def test_api_refused_when_model_available_via_subscription_alias(self):
        task = make_task(high_risk=True)
        snapshot = make_snapshot([make_candidate("a", "lineage-a", billing="subscription", model="typesafe/jev-1.13")])
        result = consultation_policy(
            task, snapshot, make_decision(),
            api_budget_usd=1.0, justification="justified",
            explicit_api_models=("typesafe/jev-1.13-20260917",),
        )
        self.assertEqual(result["api_consultants"], [])
        self.assertIn("API_REFUSED_SUBSCRIPTION_AVAILABLE", result["reasons"])

    def test_api_dispatch_does_not_follow_from_jev_signal_alone(self):
        task = make_task()
        snapshot = make_snapshot([make_candidate("a", "lineage-a", billing="subscription")])
        decision = make_decision(consultation={"value": "api_candidate", "confidence": 0.95})
        result = consultation_policy(task, snapshot, decision)
        self.assertEqual(result["api_consultants"], [])
        self.assertNotEqual(result["recommendation"], "api")


class TierRoutingTests(unittest.TestCase):
    def test_jev_weights_match_catalog_policy(self):
        import json
        from pathlib import Path
        import jev_policy
        policy = json.loads((Path(__file__).resolve().parent.parent / "references" / "model-catalog.json")
                            .read_text(encoding="utf-8"))["policy"]
        self.assertEqual(jev_policy.RECENCY_WEIGHT, policy["recency_max_penalty"])
        self.assertEqual(jev_policy.CONSERVE_WEIGHT, policy["conserve_penalty"])
        self.assertEqual(jev_policy.SIMILAR_FIT_BAND, policy["similar_score_band"])

    def _fit(self, *ids):
        return make_decision(fit={i: {"score": 3.0, "confidence": 0.9} for i in ids})

    def test_less_used_same_tier_model_ranks_first_and_score_is_unchanged(self):
        busy = make_candidate("busy", "lineage-a", tier="K", recent_share=0.9)
        idle = make_candidate("idle", "lineage-b", tier="K", recent_share=0.05)
        base = recommend(make_task(), make_snapshot([make_candidate("busy", "lineage-a"),
                                                     make_candidate("idle", "lineage-b")]),
                         self._fit("busy", "idle"), now=NOW)
        result = recommend(make_task(), make_snapshot([busy, idle]), self._fit("busy", "idle"), now=NOW)
        self.assertEqual(result["maker"][0]["id"], "idle")
        self.assertEqual({e["id"]: e["score"] for e in result["maker"]},
                         {e["id"]: e["score"] for e in base["maker"]})
        self.assertNotIn("_rank", result["maker"][0])

    def test_balancing_crosses_one_grid_step_but_not_the_similar_fit_band(self):
        near = make_decision(fit={"busy": {"score": 3.2, "confidence": 0.9},
                                  "idle": {"score": 3.0, "confidence": 0.9}})
        busy = make_candidate("busy", "lineage-a", tier="K", recent_share=1.0)
        idle = make_candidate("idle", "lineage-b", tier="K", recent_share=0.0)
        result = recommend(make_task(), make_snapshot([busy, idle]), near, now=NOW)
        scores = {e["id"]: e["score"] for e in result["maker"]}
        self.assertGreater(scores["busy"], scores["idle"])
        self.assertEqual(result["maker"][0]["id"], "idle")
        far = make_decision(fit={"busy": {"score": 4.0, "confidence": 0.9},
                                 "idle": {"score": 1.0, "confidence": 0.9}})
        result = recommend(make_task(), make_snapshot([busy, idle]), far, now=NOW)
        self.assertEqual(result["maker"][0]["id"], "busy")

    def test_conserved_lineage_yields_at_equal_fit(self):
        claude = make_candidate("claude", "anthropic", tier="M", conserve=True)
        qwen = make_candidate("qwen", "qwen", tier="M", conserve=False)
        result = recommend(make_task(), make_snapshot([claude, qwen]), self._fit("claude", "qwen"), now=NOW)
        self.assertEqual(result["maker"][0]["id"], "qwen")

    def test_catalog_deny_is_excluded(self):
        denied = make_candidate("sonnet", "anthropic", tier="DENY")
        ok = make_candidate("glm", "zhipu", tier="K")
        result = recommend(make_task(), make_snapshot([denied, ok]), self._fit("sonnet", "glm"), now=NOW)
        self.assertNotIn("sonnet", [e["id"] for e in result["maker"]])
        reasons = [r for e in result.get("excluded", result.get("maker_excluded", [])) for r in e.get("reasons", [])]
        self.assertIn("CATALOG_DENY", reasons)



class ConsultationTierTests(unittest.TestCase):
    def test_council_excludes_deny_and_prefers_less_used_equal_fit(self):
        task = make_task(high_risk=True)
        candidates = [
            make_candidate("sonnet", "anthropic", tier="DENY"),
            make_candidate("busy", "lineage-a", tier="K", recent_share=1.0, quota_remaining=1.0),
            make_candidate("idle", "lineage-a", tier="K", recent_share=0.0, quota_remaining=0.1),
        ]
        result = consultation_policy(task, make_snapshot(candidates), make_decision())
        ids = [c["id"] for c in result["consultants"]]
        self.assertNotIn("sonnet", ids)
        self.assertEqual(ids[0], "idle")

if __name__ == "__main__":
    unittest.main()
