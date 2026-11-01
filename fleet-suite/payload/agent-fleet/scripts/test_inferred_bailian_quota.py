"""Synthetic regression for an inferred ledger incorrectly exhausting Bailian.

No host/provider files or network. Run through _run_synthetic_checks.py so
all synthetic directories are retained rather than automatically deleted.
"""
import copy
import datetime as dt
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import adaptive
import fleet
import quota_collectors
import routing_v2

POOL = "bailian-shared-flat-rate"
NOW = dt.datetime(2026, 9, 29, 12, tzinfo=dt.timezone.utc)


def profile(directory):
    return {"adapter": "qwen", "quota_pool_id": POOL, "quota_collection": {
        "collector_mode": "local-token-ledger", "ledger_dir": str(directory),
        "ledger_glob": "token-usage-*.jsonl", "window_capacity_tokens": 1000,
        "window_seconds": 604800, "window_reset_anchor": "2026-09-23T12:00:00Z",
        "capacity_source": "inferred-from-provider-console-2026-08-06",
        "confidence": 0.7, "require_fresh_snapshot": False,
        "unknown_quota_mode": "fail-capped", "minimum_confidence": 0.6}}


def gate(view, mode="review"):
    return {row["code"]: row["passed"] for row in routing_v2.quota_hard_gates(
        profile(Path("unused")), view, mode=mode)}


class InferredBailianQuotaTests(unittest.TestCase):
    def row(self, used=1500, pool=POOL, source=None):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            (directory / "token-usage-synthetic.jsonl").write_text(json.dumps({
                "id": "synthetic-one", "timestamp": "2026-09-29T11:00:00Z",
                "totalTokens": used, "cachedTokens": 1400}) + "\n", encoding="utf-8")
            config = profile(directory)["quota_collection"]
            if source is not None:
                config["capacity_source"] = source
            return quota_collectors.collect_local_token_ledger(config, pool_id=pool, now=NOW)

    def test_inferred_exceeded_ledger_keeps_usage_but_real_balance_unknown(self):
        row = self.row()
        self.assertEqual(row["used_tokens"], 1500)
        self.assertEqual(row["estimated_remaining_tokens"], 0)
        self.assertEqual(row["estimated_weekly_remaining_fraction"], 0.0)
        self.assertIsNone(row["headroom"])
        self.assertIsNone(row["remaining_tokens"])
        self.assertIsNone(row["weekly_remaining_fraction"])
        self.assertEqual(row["status"], "unknown")
        self.assertEqual(row["authority"], "advisory")
        self.assertFalse(row["hard_blocking"])
        self.assertTrue(gate(row)["quota-not-exhausted"])

    def test_inferred_positive_estimate_is_not_a_verified_balance(self):
        row = self.row(used=250)
        self.assertEqual(row["estimated_remaining_tokens"], 750)
        self.assertIsNone(row["headroom"])
        self.assertEqual(row["status"], "unknown")

    def test_every_local_ledger_is_advisory_regardless_of_declared_pool_or_capacity(self):
        for kwargs in [{"pool": "unrelated-pool"}, {"source": "owner-verified-test-capacity"}]:
            with self.subTest(kwargs=kwargs):
                row = self.row(**kwargs)
                self.assertEqual(row["status"], "unknown")
                self.assertIsNone(row["headroom"])
                self.assertIsNone(row["remaining_tokens"])
                self.assertEqual(row["used_tokens"], 1500)
                self.assertEqual(row["estimated_remaining_tokens"], 0)

    def test_legacy_inferred_row_is_normalized_before_availability_gate(self):
        legacy = {"source": "local-token-ledger", "status": "fresh",
                  "headroom": 0.0, "weekly_remaining_fraction": 0.0,
                  "remaining_tokens": 0, "confidence": 0.7, "quota_pool_id": POOL}
        row = fleet.annotate_quota_views({"qwen": legacy}, {"qwen": profile(Path("unused"))})["qwen"]
        self.assertEqual(row["status"], "unknown")
        self.assertIsNone(row["headroom"])
        self.assertFalse(row["hard_blocking"])
        self.assertTrue(gate(row)["quota-not-exhausted"])
        self.assertFalse(gate(row, "implement")["quota-unknown-capped"])
        self.assertEqual(legacy["headroom"], 0.0)  # do not mutate intake/history

    def test_native_refusal_still_blocks_normalized_ledger(self):
        legacy = {"source": "local-token-ledger", "status": "fresh", "headroom": 0.0,
                  "confidence": 0.7, "availability_blocked": True,
                  "availability": {"status": "exhausted", "reason": "synthetic native refusal"}}
        row = fleet.annotate_quota_views({"qwen": legacy}, {"qwen": profile(Path("unused"))})["qwen"]
        self.assertFalse(gate(row)["quota-not-exhausted"])

    def test_provider_dashboard_is_not_normalized(self):
        for headroom in [0.0, 0.8545]:
            dashboard = {"source": "provider-dashboard", "status": "fresh", "headroom": headroom,
                         "confidence": 0.95, "authority": "authoritative", "hard_blocking": headroom == 0.0,
                         "observed_at": NOW.isoformat(), "max_sample_age_seconds": 3600}
            row = fleet.annotate_quota_views({"qwen": dashboard}, {"qwen": profile(Path("unused"))}, now=NOW)["qwen"]
            self.assertEqual(row["headroom"], headroom)
            self.assertEqual(row["status"], "fresh")
            self.assertEqual(gate(row)["quota-not-exhausted"], headroom > 0.0)

    def test_expired_dashboard_and_no_dashboard_cannot_revive_estimated_exhaustion(self):
        live = self.row()
        for stored in [{}, {"qwen": {"source": "provider-dashboard", "status": "stale",
                                    "headroom": 0.0, "confidence": 0.95}}]:
            with self.subTest(stored=stored), tempfile.TemporaryDirectory() as temporary:
                directory = Path(temporary)
                with mock.patch.object(adaptive, "latest_quotas", return_value=copy.deepcopy(stored)), \
                     mock.patch.object(quota_collectors, "collect_profile_quotas", return_value=({"qwen": live}, {})), \
                     mock.patch.object(fleet, "read_usage", return_value=[]), \
                     mock.patch.object(adaptive, "read_quota_records", return_value=[]), \
                     mock.patch.object(fleet, "_write_pace_latest"):
                    views, _ = fleet.current_quota_views(directory, {"qwen": profile(directory)}, now=NOW)
                row = views["qwen"]
                self.assertEqual(row["status"], "unknown")
                self.assertIsNone(row["headroom"])
                self.assertTrue(gate(row)["quota-not-exhausted"])

    def test_profile_uses_bounded_unknown_policy(self):
        profiles = json.loads((Path(__file__).parent.parent / "references/profiles.json").read_text(encoding="utf-8"))["profiles"]
        config = profiles["qwen"]["quota_collection"]
        self.assertFalse(config["require_fresh_snapshot"])
        self.assertEqual(config["unknown_quota_mode"], "fail-capped")
