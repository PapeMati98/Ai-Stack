"""Capacity regression: upgraded Google/Meta plans affect real bridge selection.

Covers model_tiers.propose/order_candidates (the exact functions the
todo_fleet substitution bridge calls) with the on-disk catalog:
- upgraded weights + dated provenance are present, others untouched;
- before/after: the same matched M-tier pool flips from the lightly used
  incumbent to antigravity when only the Google/Meta weights change
  (stale 127/54 -> 979/3.7, 749/3.7);
- exhausted/recheck-required pools are skipped, unknown stays neutral
  (no exhaustion label, no recency penalty);
- better fit still wins; lower-tier and DENY entries cannot execute;
- reviewer can differ from maker via exclude_lineages.
"""
import copy
import sys
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).parent
sys.path.insert(0, str(SCRIPTS))
import model_tiers

STALE = {"antigravity": 127, "muse": 54}


def stale_catalog(cat):
    out = copy.deepcopy({k: v for k, v in cat.items() if k != "_path"})
    out["_path"] = cat["_path"]
    out["policy"]["plan_weights"].update(STALE)
    return out


class TestUpgradedPlanValues(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.catalog = model_tiers.load_catalog()

    def test_weights_match_owner_prices(self):
        weights = self.catalog["policy"]["plan_weights"]
        self.assertAlmostEqual(weights["antigravity"], round(979 / 3.7, 1))
        self.assertAlmostEqual(weights["muse"], round(749 / 3.7, 1))

    def test_other_subscriptions_untouched(self):
        weights = self.catalog["policy"]["plan_weights"]
        for agent, value in (("codex", 50), ("claude", 50), ("codebuddy", 10),
                             ("grok", 100), ("kimi", 100), ("mimo", 50),
                             ("minimax", 55), ("opencode", 80), ("qwen", 68)):
            self.assertEqual(weights[agent], value)

    def test_dated_provenance(self):
        unit = self.catalog["policy"]["plan_weights_unit"]
        self.assertIn("2026-10-16", unit)
        self.assertIn("979", unit)
        self.assertIn("749", unit)
        self.assertIn("3.7", unit)


class TestBeforeAfterSelection(unittest.TestCase):
    """Matched M-tier fit; the lightly used plan vs the heavily used Google plan.

    Stale weights over-penalise the big Google plan -> the incumbent wins (the
    reported default-to-incumbent behaviour). Upgraded weights -> antigravity
    wins. Owner decision 2026-10-24 moved Gemini 4 Argon High to tier M, so the
    matched pair has to sit in one tier: mimo-v2.6-pro (plan weight 50, like the
    codex pool of the previous D-tier pair) is the lightly used incumbent.
    """

    @classmethod
    def setUpClass(cls):
        cls.catalog = model_tiers.load_catalog()
        cls.cands = [
            {"agent": "mimo", "model": "mimo-v2.6-pro",
             "score": 90, "availability": "available"},
            {"agent": "antigravity", "model": "gemini-4-argon-high",
             "score": 90, "availability": "available"},
        ]
        cls.usage = {("mimo", "mimo-v2.6-pro"): 10.0,
                     ("antigravity", "gemini-4-argon-high"): 30.0}

    def first(self, catalog):
        ordered = model_tiers.order_candidates(
            self.cands, role="maker", task={}, catalog=catalog, usage=self.usage)
        return ordered[0]["agent"], ordered[0]["effective"]

    def test_stale_weights_prefer_incumbent(self):
        agent, _ = self.first(stale_catalog(self.catalog))
        self.assertEqual(agent, "mimo")

    def test_upgraded_weights_route_to_antigravity(self):
        agent, _ = self.first(self.catalog)
        self.assertEqual(agent, "antigravity")

    def test_bridge_ranking_changes_first_pick(self):
        # order_candidates is the ranking the todo_fleet bridge consumes;
        # propose() re-derives usage from the on-disk runs window, so the
        # before/after usage contrast is exercised at the ranking layer.
        before = model_tiers.order_candidates(
            self.cands, role="maker", task={},
            catalog=stale_catalog(self.catalog), usage=self.usage)
        after = model_tiers.order_candidates(
            self.cands, role="maker", task={},
            catalog=self.catalog, usage=self.usage)
        self.assertEqual(before[0]["agent"], "mimo")
        self.assertEqual(after[0]["agent"], "antigravity")

    def test_bridge_propose_lists_upgraded_pools(self):
        rows = model_tiers.propose(
            role="maker", min_tier="M", catalog=self.catalog,
            usage={}, limit=None)
        agents = {r["agent"] for r in rows}
        self.assertIn("antigravity", agents)
        self.assertIn("muse", agents)
        by_agent = {r["agent"]: r for r in rows}
        self.assertIn("gemini-4-argon-high", by_agent["antigravity"]["model"])


class TestBridgeGuards(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.catalog = model_tiers.load_catalog()

    def test_unavailable_pools_skipped(self):
        rows = model_tiers.propose(
            role="maker", min_tier="M", catalog=self.catalog, usage={},
            availability={"codex": "exhausted",
                          "claude": "recheck-required"}, limit=None)
        agents = {r["agent"] for r in rows}
        self.assertNotIn("codex", agents)
        self.assertNotIn("claude", agents)
        self.assertIn("antigravity", agents)
        self.assertIn("muse", agents)

    def test_unknown_is_neutral(self):
        scored = model_tiers.score_candidate(
            {"agent": "muse", "model": "muse-wonder-full-1.0",
             "score": 90, "availability": "unknown"},
            role="maker", task={}, catalog=self.catalog,
            usage={("muse", "muse-wonder-full-1.0"): 50.0})
        self.assertTrue(scored["allowed"])
        self.assertEqual(scored["recency_penalty"], 0.0)
        self.assertNotIn("exhaust", " ".join(scored["reasons"]))
        self.assertNotIn("full", " ".join(scored["reasons"]))

    def test_better_fit_still_wins(self):
        cands = [
            {"agent": "antigravity", "model": "gemini-4-argon-high",
             "score": 70, "availability": "available"},
            {"agent": "codex", "model": "gpt-6-astra",
             "score": 95, "availability": "available"},
        ]
        ordered = model_tiers.order_candidates(
            cands, role="maker", task={}, catalog=self.catalog, usage={})
        self.assertEqual(ordered[0]["agent"], "codex")

    def test_lower_tier_and_deny_cannot_execute(self):
        scored_low = model_tiers.score_candidate(
            {"agent": "qwen", "model": "deepseek-v4.1-flash",
             "score": 100, "availability": "available"},
            role="maker", task={"min_tier_execute": "D"},
            catalog=self.catalog, usage={})
        self.assertFalse(scored_low["allowed"])
        denied = model_tiers.lookup(self.catalog, "claude", "claude-sonnet-4")
        self.assertIsNotNone(denied)
        self.assertEqual(denied["tier"], "DENY")
        rows = model_tiers.propose(
            role="maker", min_tier="W", catalog=self.catalog,
            usage={}, limit=None)
        self.assertTrue(rows)
        for row in rows:
            entry = model_tiers.lookup(self.catalog, row["agent"], row["model"])
            self.assertIsNotNone(entry)
            self.assertNotEqual(entry["tier"], "DENY")

    def test_reviewer_differs_from_maker(self):
        maker_lineage = model_tiers.lookup(
            self.catalog, "antigravity", "gemini-4-argon-high")["lineage"]
        rows = model_tiers.propose(
            role="reviewer", min_tier="M", catalog=self.catalog, usage={},
            exclude_lineages={maker_lineage}, limit=None)
        self.assertTrue(rows)
        for row in rows:
            entry = model_tiers.lookup(self.catalog, row["agent"], row["model"])
            self.assertNotEqual(entry["lineage"], maker_lineage)


if __name__ == "__main__":
    unittest.main()
