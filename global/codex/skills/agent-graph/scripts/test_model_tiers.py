"""Tier routing: catalog, recency balancing, conservation, substitution and todo_fleet integration."""
from __future__ import annotations

import json
import os
import time
import unittest
from unittest.mock import patch
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory

import model_tiers
import todo_fleet
import todo_graph
import history_scout
from test_todo_fleet import FakeFleet, REAL_ZCODE


def strict_loads(text):
    """json.loads that refuses NaN/Infinity (what jq or ConvertFrom-Json would do)."""
    def refuse(value):
        raise ValueError("non-finite JSON constant " + value)
    return json.loads(text, parse_constant=refuse)

CATALOG = {
    "schema_version": 1,
    "policy": {"conserve_lineages_below_director": ["anthropic", "openai"], "conserve_penalty": 8,
               "recency_max_penalty": 15},
    "models": [
        {"model_id": "dir", "lineage": "openai", "tier": "D", "lifecycle": "active",
         "profiles": [{"agent": "codex", "model": "dir"}]},
        {"model_id": "mgr", "lineage": "anthropic", "tier": "M", "lifecycle": "active",
         "profiles": [{"agent": "claude", "model": "mgr"}]},
        {"model_id": "k-busy", "lineage": "xai", "tier": "K", "lifecycle": "active",
         "profiles": [{"agent": "busy", "model": "k-busy"}]},
        {"model_id": "k-idle", "lineage": "meta", "tier": "K", "lifecycle": "active",
         "profiles": [{"agent": "idle", "model": "k-idle"}]},
        {"model_id": "w1", "lineage": "deepseek", "tier": "W", "lifecycle": "active",
         "profiles": [{"agent": "worker", "model": "w1"}]},
        {"model_id": "Beam-1.0-501B-A23B", "lineage": "reflection", "tier": "W", "lifecycle": "active",
         "profiles": [{"agent": "opencode-beam", "model": "Beam-1.0-501B-A23B"}],
         "roles": ["execute", "review"]},
        {"model_id": "w-new", "lineage": "deepseek", "tier": "W", "lifecycle": "candidate",
         "profiles": [{"agent": "worker", "model": "w-new"}]},
        {"model_id": "old", "lineage": "anthropic", "tier": "DENY", "lifecycle": "deprecated",
         "deny_reason": "inefficient", "profiles": [{"agent": "claude", "model": "old"}]},
    ],
}


def run_name(ts: datetime, n: int) -> str:
    return "run-" + ts.strftime("%Y%m%dT%H%M%S") + f"Z-{n:010x}.json"


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.catalog_path = self.root / "catalog.json"
        self.catalog_path.write_text(json.dumps(CATALOG), encoding="utf-8")
        self.runs = self.root / "runs"
        self.runs.mkdir()
        self.env = {k: os.environ.get(k) for k in ("AGENT_MODEL_CATALOG", "AGENT_FLEET_RUNS_DIR",
                                                   "AGENT_MODEL_TIERS_INDEX", "AGENT_MODEL_AVAILABILITY",
                                                   "AGENT_GRAPH_TIER_ROUTING", "AGENT_MODEL_TIERS_PICKS")}
        os.environ["AGENT_MODEL_CATALOG"] = str(self.catalog_path)
        os.environ["AGENT_FLEET_RUNS_DIR"] = str(self.runs)
        os.environ["AGENT_MODEL_TIERS_INDEX"] = str(self.root / "index.json")
        os.environ["AGENT_MODEL_AVAILABILITY"] = str(self.root / "availability.json")
        os.environ["AGENT_GRAPH_TIER_ROUTING"] = "1"
        os.environ["AGENT_MODEL_TIERS_PICKS"] = str(self.root / "picks.json")
        self.addCleanup(self.restore_env)
        self.catalog = model_tiers.load_catalog()

    def restore_env(self):
        for k, v in self.env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def add_runs(self, agent, model, count, hours_ago=1):
        now = datetime.now(timezone.utc)
        for i in range(count):
            name = run_name(now - timedelta(hours=hours_ago, seconds=i), hash((agent, model, i)) & 0xFFFFFFFFFF)
            (self.runs / name).write_text(json.dumps({"agent": agent, "model": model}), encoding="utf-8")


class CatalogTests(Base):
    def test_validation_rejects_bad_catalog(self):
        bad = json.loads(json.dumps(CATALOG))
        bad["models"][0]["tier"] = "X"
        with self.assertRaises(ValueError):
            model_tiers.validate_catalog(bad)
        bad = json.loads(json.dumps(CATALOG))
        bad["models"][-1].pop("deny_reason")
        with self.assertRaises(ValueError):
            model_tiers.validate_catalog(bad)
        dup = json.loads(json.dumps(CATALOG))
        dup["models"][1]["profiles"].append({"agent": "codex", "model": "dir"})
        with self.assertRaises(ValueError):
            model_tiers.validate_catalog(dup)

    def test_real_catalog_is_valid_and_denies_owner_list(self):
        real = model_tiers.load_catalog(model_tiers.DEFAULT_CATALOG)
        denied = {m["model_id"] for m in real["models"] if m["tier"] == "DENY"}
        self.assertTrue({"sonnet", "deepseek-v4-pro", "gpt-5.6-terra"} <= denied)
        tiers = {m["model_id"]: m["tier"] for m in real["models"]}
        self.assertEqual(tiers["gpt-6-astra"], "D")
        self.assertEqual(tiers["claude-fable-5-1"], "D")
        self.assertEqual(tiers["grok-4.7"], "K")
        self.assertEqual(tiers["gpt-6-luna"], "W")

    def test_real_catalog_beam_is_exact_synthetic_worker_for_execute_and_review(self):
        real = model_tiers.load_catalog(model_tiers.DEFAULT_CATALOG)
        beam = model_tiers.lookup(real, "opencode-beam", "Beam-1.0-501B-A23B")
        self.assertEqual(beam["lineage"], "reflection")
        self.assertEqual(beam["tier"], "W")
        self.assertEqual(beam["lifecycle"], "active")
        self.assertEqual(beam["roles"], ["execute", "review"])

    def test_real_catalog_denies_retired_generations_by_pattern(self):
        real = model_tiers.load_catalog(model_tiers.DEFAULT_CATALOG)
        for agent, model in (("grok", "grok-4.6"), ("grok", "grok-4.5"), ("muse", "muse-spark-1.2"),
                             ("claude", "claude-sonnet-4.6"), ("claude", "claude-sonnet-5-5"),
                             ("codex", "gpt-5.6-sol"), ("qwen", "deepseek-v4-pro")):
            self.assertEqual((model_tiers.lookup(real, agent, model) or {}).get("tier"), "DENY", model)
        for agent, model in (("codex", "openai/gpt-5.6-luna"), ("muse", "muse-code/muse-spark-1.2"),
                             ("grok", "xai/grok-4.6"), ("qwen", "deepseek/deepseek-v4-pro")):
            self.assertEqual((model_tiers.lookup(real, agent, model) or {}).get("tier"), "DENY", model)
        for agent, model in (("grok", "grok-4.7"), ("muse", "muse-spark-1.3"), ("codex", "gpt-6-luna")):
            self.assertNotEqual(model_tiers.lookup(real, agent, model)["tier"], "DENY", model)
        self.assertIsNone(model_tiers.lookup(real, "grok", "grok-4.10"))


class SelectionTests(Base):
    def order(self, cands, task=None, role="maker"):
        usage = model_tiers.recent_usage()
        cands = [dict(c, availability='available') for c in cands]
        return model_tiers.order_candidates(cands, role=role, task=task or {}, catalog=self.catalog, usage=usage)

    def test_less_recently_used_model_of_same_tier_wins(self):
        self.add_runs("busy", "k-busy", 30)
        self.add_runs("idle", "k-idle", 2)
        ordered = self.order([{"agent": "busy", "model": "k-busy", "score": 85},
                              {"agent": "idle", "model": "k-idle", "score": 80}])
        self.assertEqual(ordered[0]["agent"], "idle")
        self.assertGreater(ordered[1]["recent_share"], 0.8)

    def test_much_better_fit_still_beats_balancing(self):
        self.add_runs("busy", "k-busy", 30)
        ordered = self.order([{"agent": "busy", "model": "k-busy", "score": 100},
                              {"agent": "idle", "model": "k-idle", "score": 60}])
        self.assertEqual(ordered[0]["agent"], "busy")

    def test_old_runs_decay(self):
        self.add_runs("busy", "k-busy", 30, hours_ago=160)
        self.add_runs("idle", "k-idle", 6, hours_ago=1)
        usage = model_tiers.recent_usage()
        self.assertLess(usage[("busy", "k-busy")], usage[("idle", "k-idle")])

    def test_runs_outside_window_ignored(self):
        self.add_runs("busy", "k-busy", 10, hours_ago=200)
        self.assertEqual(model_tiers.recent_usage(), {})

    def test_deny_and_min_tier(self):
        ordered = self.order([{"agent": "claude", "model": "old", "score": 100},
                              {"agent": "worker", "model": "w1", "score": 50},
                              {"agent": "idle", "model": "k-idle", "score": 40}],
                             task={"min_tier_execute": "K"})
        self.assertEqual(ordered[0]["agent"], "idle")
        by = {r["model"]: r for r in ordered}
        self.assertFalse(by["old"]["allowed"])
        self.assertFalse(by["w1"]["allowed"])
        self.assertIn("below-required-tier", by["w1"]["reasons"][0])

    def test_reviewer_tier_defaults_to_maker_requirement(self):
        ordered = self.order([{"agent": "worker", "model": "w1", "score": 90}], task={"min_tier_execute": "K"},
                             role="reviewer")
        self.assertFalse(ordered[0]["allowed"])
        ordered = self.order([{"agent": "worker", "model": "w1", "score": 90}],
                             task={"min_tier_execute": "K", "reviewer_min_tier": "W"}, role="reviewer")
        self.assertTrue(ordered[0]["allowed"])

    def test_conservation_of_claude_codex_below_director(self):
        ordered = self.order([{"agent": "claude", "model": "mgr", "score": 85},
                              {"agent": "idle", "model": "k-idle", "score": 80}])
        self.assertEqual(ordered[0]["agent"], "idle")
        # A Director requirement lifts the conservation penalty.
        ordered = self.order([{"agent": "codex", "model": "dir", "score": 85},
                              {"agent": "claude", "model": "mgr", "score": 85}], task={"min_tier_execute": "D"})
        self.assertEqual(ordered[0]["agent"], "codex")
        self.assertEqual(ordered[0]["conserve_penalty"], 0.0)

    def test_unclassified_is_allowed_but_yields_to_classified(self):
        ordered = self.order([{"agent": "x", "model": "y", "score": 80},
                              {"agent": "idle", "model": "k-idle", "score": 75}])
        self.assertEqual(ordered[0]["agent"], "idle")
        self.assertTrue(ordered[1]["allowed"])
        self.assertIn("unclassified-in-catalog", ordered[1]["reasons"])

    def test_unclassified_never_satisfies_a_declared_floor(self):
        for task, role in (({"min_tier_execute": "W"}, "maker"), ({"reviewer_min_tier": "D"}, "reviewer")):
            ordered = self.order([{"agent": "qwen-local", "model": "huihui_ai/Qwen3.8-abliterated", "score": 95}],
                                 task=task, role=role)
            self.assertFalse(ordered[0]["allowed"])
            self.assertIn("unclassified-below-tier-floor", ordered[0]["reasons"])

    def test_class_above_the_floor_yields_to_a_sufficient_one(self):
        ordered = self.order([{"agent": "codex", "model": "dir", "score": 90},
                              {"agent": "worker", "model": "w1", "score": 85}], task={"min_tier_execute": "W"})
        self.assertEqual(ordered[0]["agent"], "worker")
        self.assertIn("above-required-tier D>W", ordered[1]["reasons"])
        no_floor = self.order([{"agent": "idle", "model": "k-idle", "score": 90},
                               {"agent": "worker", "model": "w1", "score": 85}])
        self.assertEqual(no_floor[0]["agent"], "idle")

    def test_rollback_survives_a_structurally_broken_catalog(self):
        os.environ["AGENT_GRAPH_TIER_ROUTING"] = "0"
        self.catalog_path.write_text("[]", encoding="utf-8")
        fleet = TodoFleetIntegrationTests.fleet(self, ["idle"])
        result, _ = TodoFleetIntegrationTests.run_node(
            self, [{"agent": "idle", "model": "k-idle", "reason": "r", "score": 80}], fleet)
        self.assertEqual(result["status"], "CANDIDATE")

    def test_bigger_plan_takes_more_load_at_equal_usage(self):
        cat = json.loads(json.dumps(CATALOG))
        cat["policy"]["plan_weights"] = {"busy": 4.0}
        self.catalog_path.write_text(json.dumps(cat), encoding="utf-8")
        self.catalog = model_tiers.load_catalog()
        self.add_runs("busy", "k-busy", 10)
        self.add_runs("idle", "k-idle", 10)
        ordered = self.order([{"agent": "busy", "model": "k-busy", "score": 80},
                              {"agent": "idle", "model": "k-idle", "score": 80}])
        self.assertEqual(ordered[0]["agent"], "busy")
        self.assertEqual(model_tiers.plan_weight(self.catalog, "idle"), 1.0)

    def test_balancing_is_capped_by_similar_fit_band(self):
        self.add_runs("busy", "k-busy", 40)
        ordered = self.order([{"agent": "busy", "model": "k-busy", "score": 90},
                              {"agent": "idle", "model": "k-idle", "score": 75}])
        self.assertEqual(ordered[0]["agent"], "busy")

    def test_pick_claims_expire(self):
        model_tiers.note_pick("busy", "k-busy", now=time.time() - model_tiers.PICK_TTL_S - 5)
        self.assertEqual(model_tiers.recent_usage().get(("busy", "k-busy"), 0.0), 0.0)

    def test_picks_in_this_run_spread_parallel_nodes(self):
        cands = [{"agent": "busy", "model": "k-busy", "score": 80},
                 {"agent": "idle", "model": "k-idle", "score": 80}]
        first = self.order(cands)[0]
        model_tiers.note_pick(first["agent"], first["model"])
        second = self.order(cands)[0]
        self.assertNotEqual(first["agent"], second["agent"])

    def test_propose_smallest_sufficient_tier_balanced_and_skips_candidate_and_exhausted(self):
        self.add_runs("busy", "k-busy", 30)
        out = model_tiers.propose(role="maker", min_tier="K", catalog=self.catalog,
                                  usage=model_tiers.recent_usage(), limit=5,
                                  availability={'busy': 'available', 'idle': 'available'})
        agents = [o["agent"] for o in out]
        self.assertEqual(agents[0], "idle")
        self.assertNotIn("worker", agents)
        self.assertTrue(all(o["score"] % 5 == 0 and 0 <= o["score"] <= 100 for o in out))
        out = model_tiers.propose(role="maker", min_tier="W", catalog=self.catalog, usage={},
                                  availability={"pool-w": "exhausted"}, pools={"worker": "pool-w"})
        self.assertNotIn("worker", [o["agent"] for o in out])
        self.assertNotIn("w-new", [o["model"] for o in out])

    def test_substituted_metadata_does_not_disable_recency_penalty(self):
        # HD-ff44d6: substitution metadata is a label, not a policy bypass. A
        # substituted candidate is scored exactly like the ordinary one: the
        # existing recency penalty still applies.
        self.add_runs("busy", "k-busy", 10)
        usage = model_tiers.recent_usage(catalog=self.catalog)
        candidate = {"agent": "busy", "model": "k-busy", "score": 90, "availability": "available"}
        args = dict(role="maker", task={"min_tier_execute": "K"}, catalog=self.catalog, usage=usage)
        ordinary = model_tiers.score_candidate(candidate, **args)
        flagged = model_tiers.score_candidate(dict(candidate, substituted=True), **args)
        self.assertGreater(ordinary["recency_penalty"], 0)
        self.assertEqual(flagged["recency_penalty"], ordinary["recency_penalty"])
        self.assertEqual(flagged["recent_share"], ordinary["recent_share"])
        self.assertEqual(flagged["effective"], ordinary["effective"])
        self.assertLess(flagged["effective"], candidate["score"])

    def test_availability_stale_or_missing_is_unknown(self):
        now = time.time()
        fresh = datetime.fromtimestamp(now - 600, timezone.utc).isoformat()
        old = datetime.fromtimestamp(now - 5 * 3600, timezone.utc).isoformat()
        later = datetime.fromtimestamp(now + 3600, timezone.utc).isoformat()
        path = self.root / "availability.json"
        path.write_text(json.dumps({"pools": [
            {"pool_id": "a", "state": "exhausted", "observed_at": fresh, "expires_at": later,
             "source": "provider-quota-endpoint"},
            {"pool_id": "ledger", "state": "exhausted", "observed_at": fresh, "expires_at": later,
             "source": "local-token-ledger"},
            {"pool_id": "b", "state": "exhausted", "observed_at": old, "expires_at": later},
            {"pool_id": "c", "state": "exhausted", "observed_at": fresh, "expires_at": fresh}]}), encoding="utf-8")
        states = model_tiers.load_availability(now=now)
        self.assertEqual(states, {"a": "exhausted", "ledger": "unknown", "b": "unknown", "c": "unknown"})
        path.unlink()
        self.assertEqual(model_tiers.load_availability(now=now), {})


class TodoFleetIntegrationTests(Base):
    def run_node(self, candidates, fleet, task_extra=None, role="maker", maker=None, request_extra=None):
        workspace = self.root / "ws"
        workspace.mkdir(exist_ok=True)
        task = {"id": "slice-1", "text": "t", "mode": "advisory", "scope": ["src"], "done_when": ["e"],
                "maker": candidates, "reviewer": candidates, "complexity": "low", "review_complexity": "low"}
        task.update(task_extra or {})
        usage_path = self.root / "usage.json"
        todo_graph.write(usage_path, {"agent": None, "model_identity": {"status": "verified", "expected_model": "m-model"},
                                      "runtime_attestation": {"status": "VERIFIED"}, "model_provider_id": None})
        fleet.result.update({"usage_record": str(usage_path)})
        outdir = self.root / "out"
        outdir.mkdir(exist_ok=True)
        request = {"goal": "g", "role": role, "workspace": str(workspace), "task": task, "maker": maker}
        request.update(request_extra or {})
        if not getattr(fleet, "_model_override_hook_installed", False):
            orig_execute = fleet.execute_single
            def execute_with_override(*args, **kwargs):
                res = orig_execute(*args, **kwargs)
                if "model_override" in kwargs and kwargs["model_override"]:
                    res["model"] = kwargs["model_override"]
                return res
            fleet.execute_single = execute_with_override
            fleet._model_override_hook_installed = True
        with patch("artifact_recovery.dirty_manifest", return_value={}):
            return todo_fleet.execute(request, outdir, fleet=fleet), outdir

    def fleet(self, agents):
        fleet = FakeFleet()
        fleet.DEFAULT_PROFILES = {a: {'supported_modes': ['implement', 'advisory', 'review']} for a in agents}
        return fleet

    def beam_fleet(self, agents=None, admission="admitted"):
        fleet = self.fleet(agents or ["opencode-beam"])
        if "opencode-beam" in fleet.DEFAULT_PROFILES:
            fleet.DEFAULT_PROFILES["opencode-beam"].update({
                "adapter": "opencode",
                "enabled_providers": ["reflection"],
                "data_boundary": {"synthetic_only": True},
                "trial_expires_at": "pinned-trial",
                "models": {"low": "Beam-1.0-501B-A23B", "medium": "Beam-1.0-501B-A23B",
                           "high": "Beam-1.0-501B-A23B"},
            })
        fleet.beam_admission = admission
        return fleet

    @staticmethod
    def beam_candidate(score=90):
        return {"agent": "opencode-beam", "model": "Beam-1.0-501B-A23B",
                "reason": "synthetic trial", "score": score}

    def test_declared_beam_worker_passes_fleet_admission_before_readiness_and_dispatch(self):
        fleet = self.beam_fleet()
        result, outdir = self.run_node(
            [self.beam_candidate()], fleet, task_extra={"min_tier_execute": "W"})
        self.assertEqual(result["status"], "CANDIDATE")
        self.assertEqual(fleet.execute_calls[0]["agent_id"], "opencode-beam")
        self.assertEqual(fleet.execute_calls[0]["model_override"], "Beam-1.0-501B-A23B")
        self.assertGreaterEqual(len(fleet.beam_qualification_calls), 2)
        self.assertIn("opencode-beam", fleet.chosen)
        self.assertIn("opencode-beam", fleet.quota_profiles)
        selected = json.loads((outdir / "tier-selection.json").read_text(encoding="utf-8"))
        self.assertTrue(selected["ordered"][0]["allowed"])

    def test_declared_beam_outside_or_after_expiry_is_excluded_before_probes(self):
        for admission in ("outside", "expired"):
            with self.subTest(admission=admission):
                fleet = self.beam_fleet(admission=admission)
                result, _ = self.run_node(
                    [self.beam_candidate()], fleet, task_extra={"min_tier_execute": "W"})
                self.assertEqual(result["status"], "BLOCKED")
                self.assertEqual(fleet.execute_calls, [])
                self.assertNotIn("opencode-beam", fleet.chosen)
                self.assertNotIn("opencode-beam", fleet.quota_profiles)
                self.assertTrue(fleet.beam_qualification_calls)

    def test_declared_beam_wrong_model_is_refused_before_probes(self):
        fleet = self.beam_fleet()
        candidate = dict(self.beam_candidate(), model="reflection/Beam-1.0-501B-A23B")
        result, _ = self.run_node([candidate], fleet, task_extra={"min_tier_execute": "W"})
        self.assertEqual(result["status"], "BLOCKED")
        self.assertEqual(fleet.execute_calls, [])
        self.assertNotIn("opencode-beam", fleet.chosen)
        self.assertNotIn("opencode-beam", fleet.quota_profiles)
        self.assertFalse(fleet.beam_qualification_calls)

    def test_substituted_beam_is_admitted_only_after_fleet_workspace_trial_check(self):
        fleet = self.beam_fleet(["claude", "opencode-beam"])
        result, outdir = self.run_node(
            [{"agent": "claude", "model": "old", "reason": "denied", "score": 90}], fleet,
            task_extra={"min_tier_execute": "W"})
        self.assertEqual(result["status"], "CANDIDATE")
        self.assertEqual(fleet.execute_calls[0]["agent_id"], "opencode-beam")
        self.assertEqual(fleet.execute_calls[0]["model_override"], "Beam-1.0-501B-A23B")
        self.assertIn("opencode-beam", fleet.quota_profiles)
        self.assertIn("opencode-beam", fleet.chosen)
        selected = json.loads((outdir / "tier-selection.json").read_text(encoding="utf-8"))
        self.assertEqual(selected["substitution"]["picked"]["model"], "Beam-1.0-501B-A23B")

    def test_substituted_beam_outside_or_after_expiry_is_excluded_before_probes(self):
        for admission in ("outside", "expired"):
            with self.subTest(admission=admission):
                fleet = self.beam_fleet(["claude", "opencode-beam", "worker"], admission=admission)
                result, _ = self.run_node(
                    [{"agent": "claude", "model": "old", "reason": "denied", "score": 90}], fleet,
                    task_extra={"min_tier_execute": "W"})
                self.assertEqual(result["status"], "CANDIDATE")
                self.assertEqual(fleet.execute_calls[0]["agent_id"], "worker")
                self.assertNotIn("opencode-beam", fleet.quota_profiles)
                self.assertNotIn("opencode-beam", fleet.chosen)
                self.assertNotIn("opencode-beam", [call["agent_id"] for call in fleet.execute_calls])
                self.assertTrue(fleet.beam_qualification_calls)

    def test_beam_declared_route_rejects_knowledge_enrichment_before_readiness_and_quota(self):
        cases = (
            {"knowledge": {"source": "synthetic-fixture"}},
            {"text": history_scout.HISTORY_HEADER + "{}"},
        )
        for extra in cases:
            with self.subTest(extra=extra):
                fleet = self.beam_fleet()
                self.run_node(
                    [self.beam_candidate()], fleet,
                    task_extra={"min_tier_execute": "W", **extra})
                self.assertNotIn("opencode-beam", fleet.chosen)
                self.assertNotIn("opencode-beam", fleet.quota_profiles)
                self.assertNotIn("opencode-beam", [call["agent_id"] for call in fleet.execute_calls])

    def test_beam_substitution_rejects_knowledge_enrichment_before_beam_probes(self):
        cases = (
            {"knowledge": {"source": "synthetic-fixture"}},
            {"text": history_scout.HISTORY_HEADER + "{}"},
        )
        for extra in cases:
            with self.subTest(extra=extra):
                fleet = self.beam_fleet(["claude", "opencode-beam", "worker"])
                result, _ = self.run_node(
                    [{"agent": "claude", "model": "old", "reason": "denied", "score": 90}], fleet,
                    task_extra={"min_tier_execute": "W", **extra})
                self.assertEqual(result["status"], "CANDIDATE")
                self.assertEqual(fleet.execute_calls[0]["agent_id"], "worker")
                self.assertNotIn("opencode-beam", fleet.chosen)
                self.assertNotIn("opencode-beam", fleet.quota_profiles)
                self.assertNotIn("opencode-beam", [call["agent_id"] for call in fleet.execute_calls])
                self.assertFalse(fleet.beam_qualification_calls)

    def test_beam_prompt_and_execute_single_receive_no_office_context(self):
        fleet = self.beam_fleet()
        result, _ = self.run_node(
            [self.beam_candidate()], fleet,
            task_extra={"min_tier_execute": "W"},
            request_extra={
                "goal": "Project-wide private context that must not reach Beam",
                "constraints": ["owner constraint stays attached"],
                "office_channel": "private-task-channel",
                "office_parent": "codex-private-parent",
            })
        self.assertEqual(result["status"], "CANDIDATE")
        call = fleet.execute_calls[0]
        self.assertNotIn("office_channels", call)
        self.assertNotIn("office_parent", call)
        prompt = fleet.execute_args[0][0]
        self.assertNotIn("private-task-channel", prompt)
        self.assertNotIn("codex-private-parent", prompt)
        self.assertNotIn("Project-wide private context", prompt)
        self.assertNotIn("Office", prompt)
        payload = json.loads(prompt.split("\n", 1)[1])
        self.assertNotIn("office_channel", payload)
        self.assertNotIn("office_parent", payload)
        self.assertEqual(payload["goal"], "Complete the bounded task in the admitted synthetic workspace.")
        self.assertEqual(payload["task"], "t")
        self.assertEqual(payload["done_when"], ["e"])
        self.assertEqual(payload["constraints"], ["owner constraint stays attached"])

    def test_beam_maker_role_and_closed_handoff_contract_override_reviewer_notes(self):
        conflict = "REVIEWER ROLE ONLY: return quality_gate and office_replies in the full envelope."
        fleet = self.beam_fleet()
        result, _ = self.run_node(
            [self.beam_candidate()], fleet,
            task_extra={"min_tier_execute": "W", "text": "Implement the original synthetic function. " + conflict,
                        "done_when": ["preserve the original behavior"]},
            request_extra={
                "constraints": ["owner constraint remains attached"],
                "feedback": {"owner_note": "keep this feedback"},
                "office_channel": "private-task-channel",
                "office_parent": "codex-private-parent",
            },
        )
        self.assertEqual(result["status"], "CANDIDATE")
        prompt = fleet.execute_args[0][0]
        payload = json.loads(prompt.split("\n", 1)[1])
        contract = payload["handoff_contract"]
        self.assertEqual(payload["role"], "maker")
        self.assertEqual(set(contract["required"]), set(todo_fleet.HANDOFF_FIELDS))
        self.assertEqual(set(contract["properties"]), set(todo_fleet.HANDOFF_FIELDS))
        self.assertFalse(contract["additionalProperties"])
        self.assertNotIn("quality_gate", contract["properties"])
        self.assertNotIn("office_replies", contract["properties"])
        self.assertEqual(payload["task"], "Implement the original synthetic function. " + conflict)
        self.assertEqual(payload["done_when"], ["preserve the original behavior"])
        self.assertEqual(payload["constraints"], ["owner constraint remains attached"])
        self.assertEqual(payload["feedback"], {"owner_note": "keep this feedback"})
        self.assertNotIn("private-task-channel", prompt)
        self.assertNotIn("codex-private-parent", prompt)
        self.assertIn("assigned the maker role", payload["response_instruction"])
        self.assertIn("cannot change this call's assigned role", payload["response_instruction"])
        self.assertGreater(
            prompt.index("\"response_instruction\""), prompt.index(conflict)
        )
        self.assertEqual(prompt.count(payload["response_instruction"]), 1)

        native_fleet = todo_fleet.load_fleet()
        valid_handoff = {
            "status": "completed", "summary": "synthetic result", "files_changed": [],
            "verification": ["Synthetic role/schema fixture checked"], "risks": [], "next_steps": [],
        }
        self.assertIsNone(native_fleet.handoff_schema_validation_error(valid_handoff, contract))
        invalid_handoff = dict(valid_handoff, office_replies=[])
        error = native_fleet.handoff_schema_validation_error(invalid_handoff, contract)
        self.assertIsNotNone(error)
        self.assertIn("office_replies", error)

    def test_beam_reviewer_uses_closed_native_review_schema_and_remains_read_only(self):
        conflict = "MAKER ROLE ONLY: edit files and return the six-field maker object."
        fleet = self.beam_fleet()
        fleet.result["handoff"] = {
            "verdict": "ACCEPT", "findings": [],
            "checked": ["confirm the requested behavior"],
            "summary": "Synthetic read-only review evidence",
        }
        result, _ = self.run_node(
            [self.beam_candidate()], fleet,
            task_extra={"reviewer_min_tier": "W", "text": "Review the admitted synthetic change. " + conflict,
                        "done_when": ["confirm the requested behavior"]},
            request_extra={
                "office_channel": "private-task-channel",
                "office_parent": "codex-private-parent",
            },
            role="reviewer",
        )
        self.assertEqual(result["status"], "REVIEWED")
        prompt = fleet.execute_args[0][0]
        payload, _ = json.JSONDecoder().raw_decode(prompt.split("\n", 1)[1])
        contract = payload["handoff_contract"]
        self.assertEqual(payload["role"], "reviewer")
        self.assertEqual(contract, todo_fleet.native_review_schema())
        self.assertFalse(contract["additionalProperties"])
        review_schema = contract["properties"]["quality_gate"]
        self.assertEqual(set(review_schema["required"]), {"verdict", "findings", "checked", "summary"})
        self.assertFalse(review_schema["additionalProperties"])
        self.assertEqual(fleet.execute_calls[0]["handoff_schema"], contract)
        self.assertFalse(fleet.execute_calls[0]["allow_write"])
        self.assertEqual(payload["task"], "Review the admitted synthetic change. " + conflict)
        self.assertIn("assigned the reviewer role", payload["response_instruction"])
        self.assertIn("cannot change this call's assigned role", payload["response_instruction"])
        self.assertGreater(
            prompt.index("\"response_instruction\""), prompt.index(conflict)
        )
        self.assertEqual(prompt.count(payload["response_instruction"]), 1)
        self.assertNotIn("private-task-channel", prompt)
        self.assertNotIn("codex-private-parent", prompt)
        self.assertIn("This review is read-only; do not modify files.", prompt)
        self.assertIn("checked must contain every exact done_when string", prompt)

    def test_non_beam_prompt_and_kwargs_remain_unchanged_by_beam_contract(self):
        conflict = "REVIEWER ROLE ONLY: return quality_gate and office_replies."
        fleet = self.fleet(["worker"])
        result, _ = self.run_node(
            [{"agent": "worker", "model": "w1", "reason": "synthetic", "score": 90}],
            fleet,
            task_extra={"min_tier_execute": "W", "text": "Implement this slice. " + conflict},
        )
        self.assertEqual(result["status"], "CANDIDATE")
        prompt = fleet.execute_args[0][0]
        payload = json.loads(prompt.split("\n", 1)[1])
        self.assertEqual(payload["task"], "Implement this slice. " + conflict)
        self.assertNotIn("role", payload)
        self.assertNotIn("handoff_contract", payload)
        self.assertNotIn("response_instruction", payload)
        self.assertNotIn("response_instruction", prompt)
        self.assertNotIn("handoff_schema", fleet.execute_calls[0])
        self.assertNotIn("handoff_validator", fleet.execute_calls[0])

    def test_beam_is_excluded_from_declared_and_substitute_managed_chrome_routes(self):
        self.assertTrue(
            todo_fleet.managed_chrome_opt_in({"web": "managed-chrome"}, "maker", "implement")
        )
        declared = self.beam_fleet()
        self.run_node(
            [self.beam_candidate()], declared,
            task_extra={"min_tier_execute": "W", "mode": "implement", "web": "managed-chrome"})
        self.assertNotIn("opencode-beam", declared.chosen)
        self.assertNotIn("opencode-beam", declared.quota_profiles)
        self.assertNotIn("opencode-beam", [call["agent_id"] for call in declared.execute_calls])

        substituted = self.beam_fleet(["claude", "opencode-beam", "worker"])
        result, _ = self.run_node(
            [{"agent": "claude", "model": "old", "reason": "denied", "score": 90}], substituted,
            task_extra={"min_tier_execute": "W", "mode": "implement", "web": "managed-chrome"})
        self.assertEqual(result["status"], "CANDIDATE")
        self.assertEqual(substituted.execute_calls[0]["agent_id"], "worker")
        self.assertNotIn("opencode-beam", substituted.chosen)
        self.assertNotIn("opencode-beam", substituted.quota_profiles)
        self.assertNotIn("opencode-beam", [call["agent_id"] for call in substituted.execute_calls])

    def test_beam_review_keeps_existing_independent_provider_lineage_gate(self):
        candidate = [self.beam_candidate()]
        fleet = self.beam_fleet()
        fleet.execution_lineage = "reflection"
        fleet.result["handoff"] = {"verdict": "ACCEPT", "findings": [], "checked": ["e"], "summary": "Synthetic independent product evidence"}
        result, _ = self.run_node(
            candidate, fleet, task_extra={"reviewer_min_tier": "W"}, role="reviewer",
            maker={"lineage": "other-provider"})
        self.assertEqual(result["status"], "REVIEWED")
        self.assertEqual(fleet.execute_calls[0]["agent_id"], "opencode-beam")
        self.assertEqual(
            fleet.execute_calls[0]["execution_profile"]["model_provider_id"],
            "reflection/opencode-beam",
        )

        same_lineage = self.beam_fleet()
        same_lineage.execution_lineage = "reflection"
        blocked, _ = self.run_node(
            candidate, same_lineage, task_extra={"reviewer_min_tier": "W"}, role="reviewer",
            maker={"lineage": "reflection/opencode-beam"})
        self.assertEqual(blocked["status"], "BLOCKED")
        self.assertEqual(same_lineage.execute_calls, [])

    def test_balancing_picks_idle_subscription(self):
        self.add_runs("busy", "k-busy", 40)
        fleet = self.fleet(["busy", "idle"])
        cands = [{"agent": "busy", "model": "k-busy", "reason": "r", "score": 85},
                 {"agent": "idle", "model": "k-idle", "reason": "r", "score": 80}]
        result, outdir = self.run_node(cands, fleet)
        self.assertEqual(result["status"], "CANDIDATE")
        self.assertEqual(fleet.execute_calls[0]["agent_id"], "idle")
        sel = json.loads((outdir / "tier-selection.json").read_text(encoding="utf-8"))
        self.assertEqual(sel["ordered"][0]["agent"], "idle")

    @unittest.skipIf(REAL_ZCODE is None, "installed Fleet ZCode runtime unavailable")
    def test_tier_preferred_zcode_without_read_admission_yields_to_next_candidate(self):
        # Plutos 2026-10-11: recency preference put ZCode first for a missing directory scope.
        self.add_runs("busy", "k-busy", 40)
        fleet = self.fleet(["busy", "idle"])
        fleet.zcode_runtime = REAL_ZCODE
        fleet.TOOL_BUDGET_ENFORCING_ADAPTERS |= {"zcode"}
        fleet.DEFAULT_PROFILES["idle"]["adapter"] = "zcode"
        cands = [{"agent": "idle", "model": "k-idle", "reason": "r", "score": 85},
                 {"agent": "busy", "model": "k-busy", "reason": "r", "score": 85}]
        result, outdir = self.run_node(cands, fleet, role="reviewer",
                                        task_extra={"scope": ["reports/v30/phase2/history"]})
        self.assertEqual([call["agent_id"] for call in fleet.execute_calls], ["busy"])
        sel = json.loads((outdir / "tier-selection.json").read_text(encoding="utf-8"))
        self.assertEqual([row["agent"] for row in sel["ordered"]], ["busy"])
        skipped = json.loads((outdir / "preflight-skipped.json").read_text(encoding="utf-8"))
        self.assertIn("ZCode read admission refused", skipped[0]["reason"])

    def test_all_denied_candidates_are_substituted_from_catalog(self):
        fleet = self.fleet(["claude", "idle", "busy", "worker", "codex"])
        cands = [{"agent": "claude", "model": "old", "reason": "r", "score": 90}]
        result, outdir = self.run_node(cands, fleet, task_extra={"min_tier_execute": "K"})
        self.assertEqual(result["status"], "CANDIDATE")
        self.assertIn(fleet.execute_calls[0]["agent_id"], {"idle", "busy"})
        sel = json.loads((outdir / "tier-selection.json").read_text(encoding="utf-8"))
        self.assertEqual(sel["substitution"]["required_tier"], "K")
        self.assertTrue(sel["substitution"]["picked"]["substituted"])

    def test_substitution_skips_candidate_excluded_by_graph_autopsy_swap(self):
        self.add_runs("idle", "k-idle", 40)  # without the exclusion busy would win on balancing
        cands = [{"agent": "claude", "model": "old", "reason": "r", "score": 90}]
        fleet = self.fleet(["claude", "idle", "busy"])
        self.run_node(cands, fleet, task_extra={"min_tier_execute": "K"})
        self.assertEqual(fleet.execute_calls[0]["agent_id"], "busy")
        fleet = self.fleet(["claude", "idle", "busy"])
        excluded = {"maker": [{"agent": "busy", "model": "k-busy"}]}
        result, outdir = self.run_node(cands, fleet, task_extra={"min_tier_execute": "K",
                                                                 "excluded_candidates": excluded})
        self.assertEqual(result["status"], "CANDIDATE")
        self.assertEqual(fleet.execute_calls[0]["agent_id"], "idle")
        sel = json.loads((outdir / "tier-selection.json").read_text(encoding="utf-8"))
        self.assertEqual(sel["substitution"]["picked"]["agent"], "idle")

    def test_substitution_uses_Fleet_unknown_not_optional_snapshot_promotion(self):
        from unittest.mock import patch
        self.add_runs('busy', 'k-busy', 30)
        fleet = self.fleet(['claude', 'idle', 'busy', 'worker', 'codex'])
        fresh = dict(fleet.quota)
        def quota_views(root, profiles):
            return {agent: ({'status': 'unknown', 'headroom': None} if agent == 'idle' else fresh)
                    for agent in profiles}, {}
        fleet.current_quota_views = quota_views
        candidates = [{'agent': 'claude', 'model': 'old', 'reason': 'r', 'score': 90}]
        with patch.object(model_tiers, 'load_availability', return_value={'idle': 'available'}) as optional:
            result, _ = self.run_node(candidates, fleet, task_extra={'min_tier_execute': 'K'})
        self.assertEqual(result['status'], 'CANDIDATE')
        self.assertEqual(fleet.execute_calls[0]['agent_id'], 'busy')
        optional.assert_not_called()

    def test_tier_selection_evidence_is_strict_json(self):
        fleet = self.fleet(["claude", "idle", "busy", "worker", "codex"])
        cands = [{"agent": "claude", "model": "old", "reason": "r", "score": 90},
                 {"agent": "worker", "model": "w1", "reason": "r", "score": 80}]
        result, outdir = self.run_node(cands, fleet, task_extra={"min_tier_execute": "K"})
        sel = strict_loads((outdir / "tier-selection.json").read_text(encoding="utf-8"))
        self.assertTrue(all(r["effective"] is None for r in sel["ordered"] if not r["allowed"]))
        strict_loads(json.dumps(result))

    def test_pick_is_persisted_for_the_next_bridge_process(self):
        fleet = self.fleet(["busy", "idle"])
        cands = [{"agent": "busy", "model": "k-busy", "reason": "r", "score": 80},
                 {"agent": "idle", "model": "k-idle", "reason": "r", "score": 80}]
        self.run_node(cands, fleet)
        first = fleet.execute_calls[0]["agent_id"]
        claims = json.loads((self.root / "picks.json").read_text(encoding="utf-8"))
        self.assertEqual(claims[0][0], first)
        fleet2 = self.fleet(["busy", "idle"])
        self.run_node(cands, fleet2)
        self.assertNotEqual(fleet2.execute_calls[0]["agent_id"], first)

    def test_substitution_skips_pool_exhausted_by_provider_evidence(self):
        now = datetime.now(timezone.utc)
        (self.root / "availability.json").write_text(json.dumps({"pools": [
            {"pool_id": "pool-idle", "state": "exhausted", "source": "provider-quota-endpoint",
             "observed_at": now.isoformat(), "expires_at": (now + timedelta(hours=1)).isoformat()}]}),
            encoding="utf-8")
        self.add_runs("busy", "k-busy", 40)  # without the snapshot idle would win on balancing
        fleet = self.fleet(["claude", "idle", "busy"])
        fleet.DEFAULT_PROFILES["idle"].update({"quota_pool_id": "pool-idle"})
        original_views = fleet.current_quota_views
        def views(root, profiles):
            rows, diagnostics = original_views(root, profiles)
            if 'idle' in rows:
                rows['idle'] = {'availability': {'status': 'exhausted', 'source': 'native-provider-error'},
                                'availability_blocked': True}
            return rows, diagnostics
        fleet.current_quota_views = views
        cands = [{"agent": "claude", "model": "old", "reason": "denied", "score": 90}]
        result, outdir = self.run_node(cands, fleet, task_extra={"min_tier_execute": "K"})
        self.assertEqual(result["status"], "CANDIDATE")
        self.assertEqual(fleet.execute_calls[0]["agent_id"], "busy")

    def test_no_qualified_substitute_is_explicit_blocked(self):
        # Director required; the only Director (codex/dir) has no installed profile here.
        fleet = self.fleet(["claude"])
        cands = [{"agent": "claude", "model": "old", "reason": "denied", "score": 90}]
        result, outdir = self.run_node(cands, fleet, task_extra={"min_tier_execute": "D"})
        self.assertEqual(result["status"], "BLOCKED")
        self.assertEqual(fleet.execute_calls, [])
        sel = json.loads((outdir / "tier-selection.json").read_text(encoding="utf-8"))
        self.assertIsNone(sel["substitution"]["picked"])

    def test_rollback_never_picks_catalog_deny(self):
        os.environ["AGENT_GRAPH_TIER_ROUTING"] = "0"
        fleet = self.fleet(["claude", "idle"])
        cands = [{"agent": "claude", "model": "old", "reason": "r", "score": 95},
                 {"agent": "idle", "model": "k-idle", "reason": "r", "score": 80}]
        result, _ = self.run_node(cands, fleet)
        self.assertEqual(result["status"], "CANDIDATE")
        self.assertEqual(fleet.execute_calls[0]["agent_id"], "idle")
        fleet2 = self.fleet(["claude"])
        result, _ = self.run_node([cands[0]], fleet2)
        self.assertEqual(result["status"], "BLOCKED")
        self.assertEqual(fleet2.execute_calls, [])

    def test_old_todo_without_tier_fields_still_validates_and_bad_tier_is_refused(self):
        task = {"id": "a", "text": "t", "scope": ["s"], "done_when": ["d"], "depends_on": [],
                "maker": [{"agent": "codex", "model": "gpt-6-luna", "score": 90, "reason": "r"}],
                "reviewer": [{"agent": "grok", "model": "grok-4.7", "score": 90, "reason": "r"}],
                "checks": [["python", "-c", "0"]]}
        plan = {"version": 1, "goal": "g", "planner": {"model": "m", "source": "s"}, "workspace": str(self.root),
                "max_parallel": 1, "requirements": ["r"], "constraints": [], "tasks": [task]}
        plan["tasks"][0]["covers"] = ["r"]
        todo_graph.validate(json.loads(json.dumps(plan)))
        plan["tasks"][0]["reviewer_min_tier"] = None
        todo_graph.validate(json.loads(json.dumps(plan)))
        plan["tasks"][0]["min_tier_execute"] = "X"
        with self.assertRaises(ValueError):
            todo_graph.validate(plan)

    def test_rollback_switch_restores_legacy_choice(self):
        os.environ["AGENT_GRAPH_TIER_ROUTING"] = "0"
        self.add_runs("busy", "k-busy", 40)
        fleet = self.fleet(["busy", "idle"])
        cands = [{"agent": "busy", "model": "k-busy", "reason": "r", "score": 85},
                 {"agent": "idle", "model": "k-idle", "reason": "r", "score": 70}]
        result, outdir = self.run_node(cands, fleet)
        self.assertEqual(fleet.execute_calls[0]["agent_id"], "busy")
        self.assertFalse((outdir / "tier-selection.json").exists())

    def test_requested_candidate_preflight_refused_falls_back_and_preserves_diagnostics(self):
        # HD-ff44d6: requested DeepseekW refused with quota-unknown-capped then successful fallback.
        # Returns exact failed preflight reasons, requested vs selected model, no false exhaustion.
        cat = json.loads(json.dumps(CATALOG))
        cat["models"].append({
            "model_id": "w2", "lineage": "meta", "tier": "W", "lifecycle": "active",
            "profiles": [{"agent": "worker2", "model": "w2"}],
        })
        self.catalog_path.write_text(json.dumps(cat), encoding="utf-8")
        self.catalog = model_tiers.load_catalog()

        fleet = self.fleet(["worker", "worker2"])
        def hard_gates(execution, profile=None, assessment=None, readiness=None, quota=None, mode=None):
            if execution.get("model_provider_id", "").endswith("/worker"):
                return [{"code": "quota-unknown-capped", "passed": False}]
            return []
        fleet.routing_v2.hard_gates = hard_gates

        cands = [{"agent": "worker", "model": "w1", "reason": "requested deepseek worker", "score": 90}]
        result, outdir = self.run_node(cands, fleet, task_extra={"min_tier_execute": "W"})

        self.assertEqual(result["status"], "CANDIDATE")
        self.assertEqual(result["agent"], "worker2")
        self.assertEqual(result["model"], "w2")
        self.assertEqual(fleet.execute_calls[0]["agent_id"], "worker2")
        self.assertEqual(fleet.execute_calls[0]["model_override"], "w2")
        self.assertEqual(len(fleet.execute_calls), 1)

        # Preflight failed reasons must be preserved in returned response
        self.assertIn("preflight", result)
        reasons = [e["reason"] for e in result["preflight"] if e["agent"] == "worker"]
        self.assertTrue(any("quota-unknown-capped" in r for r in reasons))

        # Requested vs selected model must be clear
        self.assertEqual(result.get("requested_model"), "w1")
        self.assertEqual(result.get("selected_model"), "w2")
        self.assertTrue(result.get("substituted"))

        # No false exhaustion diagnosis
        self.assertFalse(result.get("resumable_pool_wait"))
        self.assertNotIn("pool_wait_reasons", result)

        # Disk artifacts
        skipped = json.loads((outdir / "preflight-skipped.json").read_text(encoding="utf-8"))
        self.assertTrue(any("quota-unknown-capped" in e["reason"] for e in skipped))

        sel = strict_loads((outdir / "tier-selection.json").read_text(encoding="utf-8"))
        self.assertEqual(sel["substitution"]["picked"]["agent"], "worker2")
        self.assertEqual(sel["substitution"]["picked"]["model"], "w2")
        self.assertTrue(any("quota-unknown-capped" in e["reason"] for e in sel["preflight"]))
        self.assertEqual(sel["requested"][0]["model"], "w1")
        self.assertEqual(sel["selected"]["model"], "w2")

    def test_competing_same_tier_pools_regardless_of_catalog_order_and_conserve_once(self):
        # HD-ff44d6: competing same-tier pools evaluated regardless of catalog/proposal order,
        # with conservation penalty applied exactly once (not twice).
        for order in ("claude_first", "idle_first"):
            with self.subTest(order=order):
                picks = self.root / "picks.json"
                if picks.exists():
                    picks.write_text("[]", encoding="utf-8")
                cat = json.loads(json.dumps(CATALOG))
                claude_entry = {
                    "model_id": "k-claude", "lineage": "anthropic", "tier": "K", "lifecycle": "active",
                    "profiles": [{"agent": "claude", "model": "k-claude"}],
                }
                if order == "claude_first":
                    cat["models"].insert(0, claude_entry)
                else:
                    cat["models"].append(claude_entry)

                self.catalog_path.write_text(json.dumps(cat), encoding="utf-8")
                self.catalog = model_tiers.load_catalog()

                fleet = self.fleet(["claude", "idle"])
                cands = [{"agent": "claude", "model": "old", "reason": "denied", "score": 90}]
                result, outdir = self.run_node(cands, fleet, task_extra={"min_tier_execute": "K"})

                self.assertEqual(result["status"], "CANDIDATE")
                # idle must win because claude has conservation penalty (90 vs 80)
                self.assertEqual(fleet.execute_calls[0]["agent_id"], "idle")
                self.assertEqual(fleet.execute_calls[0]["model_override"], "k-idle")
                self.assertEqual(len(fleet.execute_calls), 1)

                sel = strict_loads((outdir / "tier-selection.json").read_text(encoding="utf-8"))
                picked = sel["substitution"]["picked"]
                self.assertEqual(picked["agent"], "idle")
                self.assertEqual(picked["model"], "k-idle")
                claude_scored = next(c for c in sel["substitution"]["qualified"] if c["agent"] == "claude")
                # Conservation applied once: 90 - 8 = 82 -> 80 (never double-penalized to 72)
                self.assertEqual(claude_scored["score"], 80)

    def test_same_tier_actual_availability_beats_unknown_regardless_of_order(self):
        # HD-ff44d6: actual post-qualification availability (available beats unknown)
        # regardless of catalog or proposal order.
        for order in ("busy_first", "idle_first"):
            with self.subTest(order=order):
                picks = self.root / "picks.json"
                if picks.exists():
                    picks.write_text("[]", encoding="utf-8")
                cat = json.loads(json.dumps(CATALOG))
                busy_idx = next(i for i, m in enumerate(cat["models"]) if m["model_id"] == "k-busy")
                idle_idx = next(i for i, m in enumerate(cat["models"]) if m["model_id"] == "k-idle")
                busy_m = cat["models"][busy_idx]
                idle_m = cat["models"][idle_idx]
                rem = [m for i, m in enumerate(cat["models"]) if i not in (busy_idx, idle_idx)]
                if order == "busy_first":
                    cat["models"] = [busy_m, idle_m] + rem
                else:
                    cat["models"] = [idle_m, busy_m] + rem

                self.catalog_path.write_text(json.dumps(cat), encoding="utf-8")
                self.catalog = model_tiers.load_catalog()

                fleet = self.fleet(["claude", "busy", "idle"])
                fresh_quota = dict(fleet.quota)
                unknown_quota = {"status": "unknown", "headroom": None, "current": False}
                fleet.current_quota_views = lambda root, profiles: (
                    {agent: (unknown_quota if agent == "busy" else fresh_quota) for agent in profiles}, {}
                )

                cands = [{"agent": "claude", "model": "old", "reason": "denied", "score": 90}]
                result, outdir = self.run_node(cands, fleet, task_extra={"min_tier_execute": "K"})

                self.assertEqual(result["status"], "CANDIDATE")
                # idle (available) must beat busy (unknown) regardless of catalog order
                self.assertEqual(fleet.execute_calls[0]["agent_id"], "idle")
                self.assertEqual(fleet.execute_calls[0]["model_override"], "k-idle")
                self.assertEqual(len(fleet.execute_calls), 1)

                sel = strict_loads((outdir / "tier-selection.json").read_text(encoding="utf-8"))
                self.assertEqual(sel["substitution"]["picked"]["agent"], "idle")
                self.assertEqual(sel["substitution"]["picked"]["model"], "k-idle")

    def test_higher_tier_escalation_only_when_lower_tier_unqualified(self):
        # HD-ff44d6: smallest sufficient tier preference and higher-tier escalation only when needed.
        # w1 (worker) is tier W, k-idle (idle) is tier K.
        # Case 1: worker qualifies -> worker is selected (no escalation).
        fleet = self.fleet(["worker", "idle", "claude"])
        cands = [{"agent": "claude", "model": "old", "reason": "denied", "score": 90}]
        result, outdir = self.run_node(cands, fleet, task_extra={"min_tier_execute": "W"})
        self.assertEqual(result["status"], "CANDIDATE")
        self.assertEqual(fleet.execute_calls[0]["agent_id"], "worker")

        # Case 2: worker fails preflight gate -> escalates to tier K (idle selected).
        picks = self.root / "picks.json"
        if picks.exists():
            picks.write_text("[]", encoding="utf-8")
        fleet2 = self.fleet(["worker", "idle", "claude"])
        def hard_gates_worker_fail(execution, profile=None, assessment=None, readiness=None, quota=None, mode=None):
            if execution.get("model_provider_id", "").endswith("/worker"):
                return [{"code": "quota-unknown-capped", "passed": False}]
            return []
        fleet2.routing_v2.hard_gates = hard_gates_worker_fail
        result2, outdir2 = self.run_node(cands, fleet2, task_extra={"min_tier_execute": "W"})
        self.assertEqual(result2["status"], "CANDIDATE")
        self.assertEqual(fleet2.execute_calls[0]["agent_id"], "idle")
        self.assertEqual(len(fleet2.execute_calls), 1)
        self.assertTrue(any("quota-unknown-capped" in e["reason"] for e in result2["preflight"]))

    def test_proposal_order_cannot_preempt_conservation_ranking(self):
        # HD-ff44d6: the proposal order is advisory. A conserved Claude K listed
        # before an idle K must still lose to the idle K, both peers must be
        # preflighted, and only one native dispatch may happen.
        cat = json.loads(json.dumps(CATALOG))
        cat["models"].append({
            "model_id": "k-claude", "lineage": "anthropic", "tier": "K", "lifecycle": "active",
            "profiles": [{"agent": "claude", "model": "k-claude"}],
        })
        self.catalog_path.write_text(json.dumps(cat), encoding="utf-8")
        self.catalog = model_tiers.load_catalog()

        fleet = self.fleet(["claude", "idle"])
        events = []
        original_resolve = fleet.resolve_execution_profile
        original_execute = fleet.execute_single
        def resolve(agent, *args, **kw):
            events.append(("qualify", agent, kw.get("model_override")))
            return original_resolve(agent, *args, **kw)
        def execute(*args, **kw):
            events.append(("dispatch", kw["agent_id"]))
            return original_execute(*args, **kw)
        fleet.resolve_execution_profile = resolve
        fleet.execute_single = execute

        proposals = [dict(agent="claude", model="k-claude", score=95, reason="conserve"),
                     dict(agent="idle", model="k-idle", score=90, reason="balanced")]
        with patch.object(model_tiers, "propose", return_value=proposals):
            result, outdir = self.run_node(
                [dict(agent="claude", model="old", score=90, reason="denied")],
                fleet, task_extra={"min_tier_execute": "K"})

        self.assertEqual(result["status"], "CANDIDATE")
        self.assertEqual(result["agent"], "idle")
        self.assertEqual(fleet.execute_calls[0]["agent_id"], "idle")
        self.assertEqual(len(fleet.execute_calls), 1)

        dispatch_at = events.index(("dispatch", "idle"))
        # Both same-tier substitutes were qualified before the single dispatch.
        self.assertIn(("qualify", "claude", "k-claude"), events[:dispatch_at])
        self.assertIn(("qualify", "idle", "k-idle"), events[:dispatch_at])

        sel = strict_loads((outdir / "tier-selection.json").read_text(encoding="utf-8"))
        self.assertEqual(sel["substitution"]["picked"]["agent"], "idle")
        claude_scored = next(c for c in sel["substitution"]["qualified"] if c["agent"] == "claude")
        # Conservation applied once (90 - 8 = 82 -> 80), never bypassed by substitution.
        self.assertEqual(claude_scored["score"], 80)

    def test_pinned_candidate_preflight_refusal_does_not_expand_pin(self):
        # HD-ff44d6: Do not expand a pin when candidate fails preflight.
        fleet = self.fleet(["worker", "idle"])
        fleet.routing_v2.hard_gates = lambda *args, **kwargs: [{"code": "quota-unknown-capped", "passed": False}]
        cands = [{"agent": "worker", "model": "w1", "reason": "pinned worker", "score": 90}]
        result, outdir = self.run_node(
            cands, fleet, task_extra={"min_tier_execute": "W", "candidate_policy": "pin"}
        )
        self.assertEqual(result["status"], "BLOCKED")
        self.assertIn("Pinned candidate not available or allowed", result["reason"])
        self.assertEqual(fleet.execute_calls, [])
        self.assertTrue(any("quota-unknown-capped" in e["reason"] for e in result["preflight"]))


if __name__ == "__main__":
    unittest.main()
