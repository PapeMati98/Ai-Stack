"""Tests for routing snapshot candidate metadata contracts.

Verifies optional recent_share and tier metadata contracts against jev_client.build_request.
"""
import copy
import math
import os
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import patch

import model_tiers
import routing_advisor as advisor
from jev_client import JevError, build_request
from test_jev_client import make_task
from test_routing_advisor import Router


class SnapshotMetadataContractTests(unittest.TestCase):
    def setUp(self):
        tiers = patch.dict(os.environ, {"AGENT_GRAPH_TIER_ROUTING": "1"})
        tiers.start()
        self.addCleanup(tiers.stop)
        self.now = datetime.now(timezone.utc)
        self.now_iso = self.now.isoformat().replace("+00:00", "Z")

    def _synthetic_available_quota(self):
        return {
            "current": True,
            "status": "fresh",
            "authority": "authoritative",
            "availability_verified": True,
            "availability": {
                "status": "available",
                "source": "provider-quota-endpoint",
                "observed_at": self.now_iso,
                "recheck_at": (self.now + timedelta(minutes=5)).isoformat().replace("+00:00", "Z"),
                "windows": [{"scope": "weekly", "used": 20, "limit": 100}],
            },
        }

    def snapshot(self, entry, readiness=None, scored=None, quota=None, agent="kimi", model="k3", profile=None):
        prof = profile or {
            "routing_enabled": True,
            "advisor_billing": "subscription",
            "models": {"low": model},
            "fit": {"implementation": 95},
        }
        with patch.object(model_tiers, "load_catalog", return_value={}), \
             patch.object(model_tiers, "recent_usage", return_value={}), \
             patch.object(model_tiers, "lookup", return_value=entry), \
             patch.object(model_tiers, "score_candidate", return_value=scored if scored is not None else {"recent_share": 0.25}):
            return advisor.make_snapshot(
                SimpleNamespace(routing_v2=Router),
                {agent: prof},
                {agent: quota or {}},
                readiness or {},
                make_task(),
            )

    def test_unknown_availability_omits_recent_share_and_accepted(self):
        """Unknown availability candidates must omit recent_share and pass build_request."""
        snap = self.snapshot({"tier": "K"}, scored={"recent_share": 0.25})
        row = snap["candidates"][0]
        self.assertEqual(row["status"], "unknown")
        self.assertEqual(row["tier"], "K")
        self.assertNotIn("recent_share", row)
        build_request(make_task(), advisor.public_snapshot(snap))

    def test_exhausted_candidate_omits_recent_share_and_accepted(self):
        """Exhausted candidates must omit recent_share and pass build_request."""
        exhausted_quota = {"availability_blocked": True}
        snap = self.snapshot({"tier": "K"}, quota=exhausted_quota, scored={"recent_share": 0.25})
        row = snap["candidates"][0]
        self.assertEqual(row["status"], "exhausted")
        self.assertFalse(row["enabled"])
        self.assertNotIn("recent_share", row)
        build_request(make_task(), advisor.public_snapshot(snap))

    def test_known_available_with_synthetic_evidence_retains_recent_share(self):
        """Known available candidate with synthetic quota and readiness retains measured recent_share."""
        quota = self._synthetic_available_quota()
        readiness = {"kimi": {"ready": True}}
        snap = self.snapshot({"tier": "K"}, readiness=readiness, quota=quota, scored={"recent_share": 0.35})
        row = snap["candidates"][0]
        self.assertEqual(row["status"], "available")
        self.assertEqual(row["tier"], "K")
        self.assertIn("recent_share", row)
        self.assertEqual(row["recent_share"], 0.35)
        build_request(make_task(), advisor.public_snapshot(snap))

    def test_unclassified_model_has_none_tier_and_omits_recent_share(self):
        """Unclassified model absent from catalog has tier None, no recent_share, and is accepted."""
        snap = self.snapshot(None, scored={"recent_share": 0.25})
        row = snap["candidates"][0]
        self.assertIsNone(row.get("tier"))
        self.assertNotIn("recent_share", row)
        build_request(make_task(), advisor.public_snapshot(snap))

    def test_valid_zero_and_one_recent_shares_retained(self):
        """Boundary values 0, 0.0, 1, and 1.0 are retained; unknown availability never invents fake 0."""
        quota = self._synthetic_available_quota()
        readiness = {"kimi": {"ready": True}}
        for valid_val in (0, 0.0, 1, 1.0):
            snap = self.snapshot({"tier": "K"}, readiness=readiness, quota=quota, scored={"recent_share": valid_val})
            row = snap["candidates"][0]
            self.assertEqual(row["status"], "available")
            self.assertIn("recent_share", row)
            self.assertEqual(row["recent_share"], valid_val)
            self.assertNotIsInstance(row["recent_share"], bool)
            build_request(make_task(), advisor.public_snapshot(snap))

        # Unknown availability candidate with 0 in score_candidate still must not invent fake 0
        snap_unknown = self.snapshot({"tier": "K"}, scored={"recent_share": 0.0})
        self.assertEqual(snap_unknown["candidates"][0]["status"], "unknown")
        self.assertNotIn("recent_share", snap_unknown["candidates"][0])
        build_request(make_task(), advisor.public_snapshot(snap_unknown))

    def test_mixed_pool_build_request_acceptance(self):
        """Mixed candidate pool (available, unknown, exhausted, unclassified) passes build_request."""
        profiles = {
            "kimi": {
                "routing_enabled": True,
                "advisor_billing": "subscription",
                "models": {"low": "k3"},
                "fit": {"implementation": 95},
            },
            "codex": {
                "routing_enabled": True,
                "advisor_billing": "subscription",
                "models": {"high": "gpt-6-sol"},
                "fit": {"implementation": 90},
            },
            "claude": {
                "routing_enabled": True,
                "advisor_billing": "subscription",
                "models": {"high": "claude-3-opus"},
                "fit": {"implementation": 85},
            },
            "unclassified-agent": {
                "routing_enabled": True,
                "advisor_billing": "subscription",
                "models": {"medium": "unknown-model"},
                "fit": {"implementation": 70},
            },
        }
        quotas = {
            "kimi": self._synthetic_available_quota(),
            "codex": {},
            "claude": {"availability_blocked": True},
            "unclassified-agent": {},
        }
        readiness = {"kimi": {"ready": True}}

        def mock_lookup(catalog, agent, model):
            entries = {
                "kimi": {"tier": "K"},
                "codex": {"tier": "D"},
                "claude": {"tier": "M"},
            }
            return entries.get(agent)

        def mock_score(candidate_desc, **kwargs):
            if candidate_desc.get("agent") == "kimi":
                return {"recent_share": 0.25}
            return {"recent_share": 0.5}

        with patch.object(model_tiers, "load_catalog", return_value={}), \
             patch.object(model_tiers, "recent_usage", return_value={}), \
             patch.object(model_tiers, "lookup", side_effect=mock_lookup), \
             patch.object(model_tiers, "score_candidate", side_effect=mock_score):
            snap = advisor.make_snapshot(
                SimpleNamespace(routing_v2=Router),
                profiles,
                quotas,
                readiness,
                make_task(),
            )

        self.assertEqual(len(snap["candidates"]), 4)
        cands_by_agent = {c["agent"]: c for c in snap["candidates"]}

        # Kimi is available with valid recent_share
        self.assertEqual(cands_by_agent["kimi"]["status"], "available")
        self.assertEqual(cands_by_agent["kimi"]["tier"], "K")
        self.assertEqual(cands_by_agent["kimi"]["recent_share"], 0.25)

        # Codex is unknown availability -> recent_share omitted
        self.assertEqual(cands_by_agent["codex"]["status"], "unknown")
        self.assertEqual(cands_by_agent["codex"]["tier"], "D")
        self.assertNotIn("recent_share", cands_by_agent["codex"])

        # Claude is exhausted -> recent_share omitted
        self.assertEqual(cands_by_agent["claude"]["status"], "exhausted")
        self.assertEqual(cands_by_agent["claude"]["tier"], "M")
        self.assertNotIn("recent_share", cands_by_agent["claude"])

        # Unclassified agent has no entry -> tier None, recent_share omitted
        self.assertIsNone(cands_by_agent["unclassified-agent"]["tier"])
        self.assertNotIn("recent_share", cands_by_agent["unclassified-agent"])

        # Full public snapshot passes build_request
        build_request(make_task(), advisor.public_snapshot(snap))

    def test_invalid_shares_rejected_by_client(self):
        """Explicit null, bool, nonfinite, and out-of-range shares are rejected by unchanged jev_client."""
        quota = self._synthetic_available_quota()
        readiness = {"kimi": {"ready": True}}
        snap = self.snapshot({"tier": "K"}, readiness=readiness, quota=quota, scored={"recent_share": 0.5})
        base_public = advisor.public_snapshot(snap)

        invalid_shares = [
            None,
            True,
            False,
            float("nan"),
            float("inf"),
            float("-inf"),
            -0.01,
            -1.0,
            1.01,
            2.0,
            "0.5",
        ]
        for bad_val in invalid_shares:
            bad_snap = copy.deepcopy(base_public)
            bad_snap["candidates"][0]["recent_share"] = bad_val
            with self.subTest(invalid_share=bad_val):
                with self.assertRaises(JevError) as ctx:
                    build_request(make_task(), bad_snap)
                self.assertEqual(ctx.exception.code, "invalid_input")

    def test_snapshot_omits_invalid_or_nonfinite_scored_shares(self):
        """make_snapshot omits recent_share if scored share is null, bool, nonfinite, or out-of-range."""
        quota = self._synthetic_available_quota()
        readiness = {"kimi": {"ready": True}}
        invalid_scored = [
            {"recent_share": None},
            {"recent_share": True},
            {"recent_share": False},
            {"recent_share": float("nan")},
            {"recent_share": float("inf")},
            {"recent_share": -0.5},
            {"recent_share": 1.5},
            {},
        ]
        for scored in invalid_scored:
            with self.subTest(scored=scored):
                snap = self.snapshot({"tier": "K"}, readiness=readiness, quota=quota, scored=scored)
                row = snap["candidates"][0]
                self.assertEqual(row["status"], "available")
                self.assertNotIn("recent_share", row)
                build_request(make_task(), advisor.public_snapshot(snap))


if __name__ == "__main__":
    unittest.main()
