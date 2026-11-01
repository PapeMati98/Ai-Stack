from __future__ import annotations

import datetime as dt
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import adaptive
import fleet
import routing_v2


class RoutingPaceTests(unittest.TestCase):
    def setUp(self) -> None:
        # Isolated state root: the full-fleet pace export creates
        # pace/latest.json, so tests must not share a checkout-relative dir.
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / "fleet-state"
        self.profiles = fleet.load_profiles(fleet.DEFAULT_PROFILES)
        self.assessment = json.loads(
            (fleet.SKILL_ROOT / "references" / "task-profile-v2-example.json").read_text(encoding="utf-8")
        )
        self.execution = routing_v2.resolve_execution_profile(
            "codex", self.profiles["codex"], complexity="medium", mode="implement",
            required_tools=["write"],
        )
        self.common = {
            "capability_fit": 90.0,
            "execution": self.execution,
            "assessment": self.assessment,
            "quota": {"status": "unknown", "headroom": None},
            "underuse_score": 50.0,
            "local": {"sample_count": 0, "active": False},
        }

    def test_overburn_drops_below_equivalent_pool_and_underuse_rises(self) -> None:
        steady, steady_parts, steady_penalties, _ = routing_v2.assignment_score(
            **self.common, pace={"tempo": 1.0, "confidence": 1.0}
        )
        overburn, overburn_parts, overburn_penalties, _ = routing_v2.assignment_score(
            **self.common, pace={"tempo": 9.5, "confidence": 1.0}
        )
        underused, underused_parts, underused_penalties, _ = routing_v2.assignment_score(
            **self.common, pace={"tempo": 0.3, "confidence": 1.0}
        )
        self.assertLess(overburn, steady)
        self.assertGreater(underused, steady)
        self.assertEqual(overburn_parts["subscription_utility"], 0.0)
        self.assertEqual(steady_parts["subscription_utility"], 50.0)
        self.assertEqual(underused_parts["subscription_utility"], 85.0)
        self.assertEqual(overburn_penalties["pace_overburn"], 35.0)
        self.assertNotIn("pace_overburn", steady_penalties)
        self.assertNotIn("pace_overburn", underused_penalties)

    def test_missing_pace_preserves_previous_score(self) -> None:
        no_pace = routing_v2.assignment_score(**self.common)
        unknown = routing_v2.assignment_score(
            **self.common, pace={"tempo": None, "confidence": 0.0, "source": "none"}
        )
        self.assertEqual(no_pace[0], unknown[0])
        self.assertEqual(no_pace[0], 71.86)  # Frozen score from the pre-pace formula.
        self.assertEqual(no_pace[1]["subscription_utility"], self.common["underuse_score"])
        self.assertNotIn("pace_overburn", no_pace[2])

    def test_monthly_zero_hard_blocks_when_fresh(self) -> None:
        current = dt.datetime(2026, 9, 24, 9, tzinfo=dt.timezone.utc)
        view = adaptive._quota_view({
            "observed_at": "2026-09-24T08:20:00Z",
            "valid_for_hours": 6,
            "source": "provider-dashboard",
            "confidence": 1.0,
            "monthly_remaining_fraction": 0.0,
            "monthly_reset_at": "2026-09-24T10:00:00Z",
        }, current=current)
        self.assertEqual(view["headroom"], 0.0)
        self.assertTrue(view["hard_blocking"])
        gates = routing_v2.quota_hard_gates(self.profiles["mimo"], view)
        self.assertFalse(next(row for row in gates if row["code"] == "quota-not-exhausted")["passed"])
        expiry, _ = adaptive._quota_expiry(view, adaptive.parse_timestamp(view["observed_at"]))
        self.assertEqual(expiry, current.replace(hour=10))

    def test_baseline_ranking_ignores_pace(self) -> None:
        profiles = {key: self.profiles[key] for key in ("codex", "mimo")}
        kwargs = dict(
            kind="implementation", complexity="medium", counts={},
            availability={"codex": True, "mimo": True},
            routing_mode="baseline",
        )
        plain = fleet.rank_profiles(profiles, **kwargs)
        with_pace = fleet.rank_profiles(
            profiles, **kwargs,
            quotas={
                "codex": {"status": "unknown", "headroom": None, "pace": {"tempo": 0.3, "confidence": 1.0}},
                "mimo": {"status": "unknown", "headroom": None, "pace": {"tempo": 9.5, "confidence": 1.0}},
            },
        )
        self.assertEqual([row["agent"] for row in plain], [row["agent"] for row in with_pace])
        self.assertEqual([row["score"] for row in plain], [row["score"] for row in with_pace])
        self.assertEqual([row["baseline_score"] for row in plain], [row["baseline_score"] for row in with_pace])

    def test_adaptive_ranking_exposes_pace_evidence(self) -> None:
        profile = {"codex": self.profiles["codex"]}
        assessment = adaptive.normalize_task_profile(
            adaptive.fallback_assessment(kind="implementation", complexity="medium", profiles=profile)
        )
        result = routing_v2.rank_execution_profiles(
            profile, kind="implementation", complexity="medium", counts={},
            readiness={"codex": {"ready": True, "reasons": []}}, assessment=assessment,
            quotas={"codex": {
                "status": "fresh", "headroom": 0.5, "confidence": 1.0,
                "pace": {"tempo": 9.5, "confidence": 1.0, "source": "provider-dashboard", "reason": "monthly-measured"},
            }},
            records=[], benchmark=None, mode="implement", routing_mode="adaptive",
        )
        row = result[0]
        self.assertEqual(row["pace"]["tempo"], 9.5)
        self.assertEqual(row["resource_components"]["pace"]["source"], "provider-dashboard")
        self.assertIn("pace_overburn", row["penalties"])
        self.assertIn("pace_reason=monthly-measured", row["reasons"])

    def test_quota_status_exposes_same_pool_pace_and_counts_export_write(self) -> None:
        current = dt.datetime.now(dt.timezone.utc)
        observed = current - dt.timedelta(minutes=2)
        reset = current + dt.timedelta(days=20)
        raw = {
            "schema_version": 1,
            "snapshot_id": "quota-synthetic",
            "agent": "mimo",
            "quota_pool_id": "xiaomi-mimo-token-plan-standard",
            "observed_at": observed.isoformat(),
            "valid_for_hours": 6,
            "source": "provider-dashboard",
            "confidence": 1.0,
            "monthly_remaining_fraction": 0.7,
            "monthly_reset_at": reset.isoformat(),
        }
        profiles = {"mimo": self.profiles["mimo"]}
        stdout = mock.Mock()
        stdout.buffer = io.BytesIO()
        stderr = mock.Mock()
        stderr.buffer = io.BytesIO()
        with (
            mock.patch.object(fleet, "load_profiles", return_value=profiles),
            mock.patch.object(adaptive, "latest_quotas", return_value={
                "mimo": adaptive._quota_view(raw, current=current)
            }),
            mock.patch.object(adaptive, "read_quota_records", return_value=[raw]),
            mock.patch.object(fleet, "read_usage", return_value=[]),
            mock.patch.object(fleet.quota_collectors, "collect_profile_quotas", return_value=({}, {})),
            mock.patch.object(fleet.sys, "stdout", stdout),
            mock.patch.object(fleet.sys, "stderr", stderr),
        ):
            code = fleet.main(["--state-dir", str(self.root), "quota-status"])
        self.assertEqual(code, fleet.EXIT_SUCCESS)
        result = json.loads(stdout.buffer.getvalue())
        pool_pace = result["quota_collection"]["xiaomi-mimo-token-plan-standard"]["pace"]
        self.assertEqual(pool_pace, result["quota_snapshots"]["mimo"]["pace"])
        self.assertEqual(pool_pace["window"], "monthly")
        self.assertGreater(pool_pace["tempo"], 0)
        # No quota-record write, but the pace/latest.json export is one
        # performed write and is reported truthfully.
        self.assertEqual(result["writes_performed"], 1)
        self.assertTrue((self.root / "pace" / "latest.json").is_file())

    def test_legacy_mimo_store_pool_uses_profile_capacity_and_run_tokens(self) -> None:
        current = dt.datetime.now(dt.timezone.utc)
        first_at = current - dt.timedelta(days=5)
        second_at = current - dt.timedelta(days=4)
        reset_at = current + dt.timedelta(days=20)
        first = {
            "agent": "mimo", "quota_pool_id": "mimo", "observed_at": first_at.isoformat(),
            "source": "provider-dashboard", "valid_for_hours": 6, "confidence": 1.0,
            "credits_remaining": 8261600584,
        }
        second = {
            "agent": "mimo", "quota_pool_id": "mimo", "observed_at": second_at.isoformat(),
            "source": "provider-dashboard", "valid_for_hours": 6, "confidence": 1.0,
            "monthly_remaining_fraction": 0.574, "monthly_reset_at": reset_at.isoformat(),
        }
        runs = [
            {"agent": "mimo", "completed_at": (current - dt.timedelta(days=4, hours=12)).isoformat(),
             "token_usage": {"total_tokens": 100}},
            {"agent": "mimo", "completed_at": (current - dt.timedelta(hours=1)).isoformat(),
             "token_usage": {"total_tokens": 40}},
        ]
        profiles = {"mimo": {
            "adapter": "mimo", "quota_pool_id": "xiaomi-mimo-token-plan-standard",
            "quota_collection": {"plan_capacity_credits": 11000000000},
        }}
        with (
            mock.patch.object(adaptive, "read_quota_records", return_value=[first, second]),
            mock.patch.object(fleet.quota_collectors, "collect_profile_quotas", return_value=({}, {})),
            mock.patch.object(fleet, "read_usage", return_value=runs),
        ):
            views, diagnostics = fleet.current_quota_views(self.root, profiles)
        pace = views["mimo"]["pace"]
        expected = 0.574 - (8261600584 / 11000000000 - 0.574) * 0.4
        self.assertEqual(views["mimo"]["quota_pool_id"], "mimo")
        self.assertEqual(pace["source"], "fleet-projection")
        self.assertEqual(pace["reason"], "monthly-projected-from-fleet-tokens")
        self.assertAlmostEqual(pace["remaining_fraction"], expected, places=6)
        self.assertGreater(pace["confidence"], 0.0)
        self.assertEqual(diagnostics["xiaomi-mimo-token-plan-standard"]["pace"], pace)

    def test_shared_profile_pool_assigns_one_pace_to_both_agents(self) -> None:
        current = dt.datetime.now(dt.timezone.utc)
        reset = (current + dt.timedelta(days=4)).isoformat()
        first_at = current - dt.timedelta(days=2)
        second_at = current - dt.timedelta(days=1)
        rows = [
            {"agent": "qwen", "quota_pool_id": "qwen", "observed_at": first_at.isoformat(),
             "source": "provider-dashboard", "valid_for_hours": 6, "confidence": 1.0,
             "weekly_remaining_fraction": 0.8, "weekly_reset_at": reset},
            {"agent": "qwen-local", "quota_pool_id": "qwen-local",
             "observed_at": second_at.isoformat(), "source": "provider-dashboard",
             "valid_for_hours": 6, "confidence": 1.0,
             "weekly_remaining_fraction": 0.7, "weekly_reset_at": reset},
            {"agent": "other", "quota_pool_id": "shared-subscription",
             "observed_at": (current - dt.timedelta(hours=3)).isoformat(),
             "source": "provider-dashboard", "valid_for_hours": 6, "confidence": 1.0,
             "weekly_remaining_fraction": 0.01, "weekly_reset_at": reset},
        ]
        runs = [
            {"agent": "qwen", "completed_at": (current - dt.timedelta(days=1, hours=12)).isoformat(),
             "token_usage": {"total_tokens": 100}},
            {"agent": "qwen-local", "completed_at": (current - dt.timedelta(hours=12)).isoformat(),
             "token_usage": {"total_tokens": 50}},
        ]
        profiles = {
            "qwen": {"adapter": "qwen", "quota_pool_id": "shared-subscription"},
            "qwen-local": {"adapter": "qwen", "quota_pool_id": "shared-subscription"},
            "other": {"adapter": "other", "quota_pool_id": "other-subscription"},
        }
        with (
            mock.patch.object(adaptive, "read_quota_records", return_value=rows),
            mock.patch.object(fleet.quota_collectors, "collect_profile_quotas", return_value=({}, {})),
            mock.patch.object(fleet, "read_usage", return_value=runs),
        ):
            views, diagnostics = fleet.current_quota_views(self.root, profiles)
        shared_pace = views["qwen"]["pace"]
        self.assertEqual(shared_pace, views["qwen-local"]["pace"])
        self.assertEqual(shared_pace, diagnostics["shared-subscription"]["pace"])
        self.assertEqual(shared_pace["source"], "fleet-projection")
        self.assertAlmostEqual(shared_pace["remaining_fraction"], 0.65)
        self.assertNotEqual(shared_pace, views["other"]["pace"])

    def test_profile_short_window_length_applies_to_legacy_store_pool(self) -> None:
        current = dt.datetime.now(dt.timezone.utc)
        row = {
            "agent": "qwen", "quota_pool_id": "qwen",
            "observed_at": (current - dt.timedelta(hours=1)).isoformat(),
            "source": "provider-dashboard", "valid_for_hours": 6, "confidence": 1.0,
            "short_window_remaining_fraction": 0.5,
            "short_window_reset_at": (current + dt.timedelta(hours=1)).isoformat(),
        }
        profiles = {"qwen": {
            "adapter": "qwen", "quota_pool_id": "shared-subscription",
            "quota_collection": {"short_window_hours": 4},
        }}
        with (
            mock.patch.object(adaptive, "read_quota_records", return_value=[row]),
            mock.patch.object(fleet.quota_collectors, "collect_profile_quotas", return_value=({}, {})),
            mock.patch.object(fleet, "read_usage", return_value=[]),
        ):
            views, diagnostics = fleet.current_quota_views(self.root, profiles)
        pace = views["qwen"]["pace"]
        self.assertEqual(pace["window"], "short")
        self.assertEqual(pace, diagnostics["shared-subscription"]["pace"])

    def test_local_ledger_never_contributes_authoritative_pace(self) -> None:
        current = dt.datetime.now(dt.timezone.utc)
        profile = {
            "qwen": {
                "adapter": "qwen", "quota_pool_id": "bailian-shared-flat-rate",
                "quota_collection": {"collector_mode": "local-token-ledger"},
            }
        }
        live = {
            "agent": "qwen", "quota_pool_id": "bailian-shared-flat-rate",
            "observed_at": (current - dt.timedelta(minutes=1)).isoformat(),
            "source": "local-token-ledger", "status": "fresh", "confidence": 0.7,
            "weekly_remaining_fraction": 0.5,
            "weekly_reset_at": (current + dt.timedelta(days=4)).isoformat(),
            "headroom": 0.5,
        }
        with (
            mock.patch.object(adaptive, "latest_quotas", return_value={
                "qwen": {"status": "unknown", "headroom": None, "confidence": 0.0}
            }),
            mock.patch.object(adaptive, "read_quota_records", return_value=[]),
            mock.patch.object(fleet.quota_collectors, "collect_profile_quotas", return_value=({"qwen": live}, {})),
            mock.patch.object(fleet, "read_usage", return_value=[]),
        ):
            views, diagnostics = fleet.current_quota_views(self.root, profile)
        pace = views["qwen"]["pace"]
        self.assertNotIn("window", pace)
        self.assertEqual(pace["confidence"], 0.0)
        self.assertIsNone(views["qwen"]["headroom"])
        self.assertEqual(views["qwen"]["estimated_weekly_remaining_fraction"], 0.5)
        self.assertEqual(diagnostics["bailian-shared-flat-rate"]["pace"], pace)


if __name__ == "__main__":
    unittest.main()
