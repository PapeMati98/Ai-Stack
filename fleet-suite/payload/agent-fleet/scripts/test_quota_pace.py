from __future__ import annotations

import datetime as dt
import json
import unittest
from unittest import mock

import adaptive
import quota_pace


UTC = dt.timezone.utc
POOL = "xiaomi-mimo-token-plan-standard"


def snapshot(observed: str, **values: object) -> dict:
    return {
        "schema_version": 1,
        "snapshot_id": "quota-" + observed[:10],
        "agent": "mimo",
        "quota_pool_id": POOL,
        "observed_at": observed,
        "valid_for_hours": 6,
        "source": "provider-dashboard",
        "confidence": 1.0,
        **values,
    }


def pace(rows: list[dict], now: str, runs: list[dict] | None = None, **options: object) -> dict:
    return quota_pace.calculate_pool_pace(
        POOL,
        snapshots=rows,
        runs=runs or [],
        pool_ids={"mimo": POOL},
        now=dt.datetime.fromisoformat(now.replace("Z", "+00:00")),
        **options,
    )


class QuotaPaceTests(unittest.TestCase):
    def test_monthly_window_clips_previous_month_day(self) -> None:
        row = snapshot(
            "2026-03-14T00:00:00Z",
            monthly_remaining_fraction=0.5,
            monthly_reset_at="2026-03-31T00:00:00Z",
        )
        result = pace([row], "2026-03-14T00:00:00Z")
        elapsed = (dt.datetime(2026, 3, 14, tzinfo=UTC) - dt.datetime(2026, 2, 28, tzinfo=UTC)).total_seconds()
        duration = (dt.datetime(2026, 3, 31, tzinfo=UTC) - dt.datetime(2026, 2, 28, tzinfo=UTC)).total_seconds()
        self.assertAlmostEqual(result["tempo"], 0.5 / (elapsed / duration), places=5)
        self.assertEqual(result["window"], "monthly")

    def test_weekly_window_and_short_window_requires_declared_length(self) -> None:
        weekly = snapshot(
            "2026-09-21T00:00:00Z", weekly_remaining_fraction=0.5,
            weekly_reset_at="2026-09-25T00:00:00Z",
        )
        result = pace([weekly], "2026-09-21T00:00:00Z")
        self.assertAlmostEqual(result["tempo"], 0.5 / (3 / 7), places=5)
        self.assertEqual(result["window"], "weekly")
        short = snapshot(
            "2026-09-24T08:00:00Z", short_window_remaining_fraction=0.5,
            short_window_reset_at="2026-09-24T10:00:00Z",
        )
        self.assertIsNone(pace([short], "2026-09-24T08:00:00Z")["tempo"])
        self.assertAlmostEqual(
            pace([short], "2026-09-24T08:00:00Z", short_window_hours=4)["tempo"], 1.0
        )

    def test_mimo_two_store_snapshots_reconstruct_overburn(self) -> None:
        first = snapshot("2026-09-23T20:54:43Z", credits_remaining=8261600584)
        second = snapshot(
            "2026-09-24T08:20:00Z", credits_remaining=6313733884,
            monthly_remaining_fraction=0.574, monthly_reset_at="2026-10-22T23:59:59Z",
        )
        # Read the two synthetic store files through the normal quota loader.
        files = [mock.Mock() for _ in (first, second)]
        for file, row in zip(files, (first, second)):
            file.read_text.return_value = json.dumps(row)
        directory = mock.Mock()
        directory.is_dir.return_value = True
        directory.glob.return_value = files
        with mock.patch.object(adaptive, "quota_dir", return_value=directory):
            rows = adaptive.read_quota_records(mock.Mock())
        self.assertEqual(len(rows), 2)
        result = pace(rows, "2026-09-24T08:20:00Z", plan_capacity_credits=11000000000)
        self.assertAlmostEqual(result["tempo"], 9.5, delta=0.2)
        self.assertGreater(result["confidence"], 0.8)
        self.assertLess(result["confidence"], 1.0)
        self.assertEqual(result["source"], "provider-dashboard")
        # The first credits-only observation is usable because the second
        # observation establishes the same monthly window.
        first_only = pace(
            [first, second], "2026-09-23T21:00:00Z", plan_capacity_credits=11000000000
        )
        self.assertGreater(first_only["tempo"], 0)
        self.assertIsNone(pace([first], "2026-09-23T21:00:00Z", plan_capacity_credits=11000000000)["tempo"])

    def test_legacy_mimo_pool_ids_project_from_calibrated_fleet_tokens(self) -> None:
        first = snapshot(
            "2026-09-23T20:54:43Z", quota_pool_id="mimo", credits_remaining=8261600584,
        )
        second = snapshot(
            "2026-09-24T08:20:00Z", quota_pool_id="mimo",
            monthly_remaining_fraction=0.574, monthly_reset_at="2026-10-22T23:59:59Z",
        )
        runs = [
            {"agent": "mimo", "quota_pool_id": "mimo", "completed_at": "2026-09-24T00:00:00Z",
             "token_usage": {"total_tokens": 100}},
            {"agent": "mimo", "quota_pool_id": "mimo", "completed_at": "2026-09-24T09:20:00Z",
             "token_usage": {"total_tokens": 40}},
        ]
        result = pace(
            [first, second], "2026-09-24T10:20:00Z", runs,
            plan_capacity_credits=11000000000,
        )
        expected = 0.574 - (8261600584 / 11000000000 - 0.574) * 0.4
        self.assertEqual(result["source"], "fleet-projection")
        self.assertEqual(result["reason"], "monthly-projected-from-fleet-tokens")
        self.assertAlmostEqual(result["remaining_fraction"], expected, places=6)
        self.assertGreater(result["confidence"], 0.75)
        self.assertLess(result["confidence"], 0.8)

    def test_known_agents_share_only_their_configured_pool(self) -> None:
        pool = "shared-subscription"
        reset = "2026-09-29T00:00:00Z"
        rows = [
            snapshot("2026-09-23T00:00:00Z", agent="qwen", quota_pool_id="qwen",
                     weekly_remaining_fraction=0.8, weekly_reset_at=reset),
            snapshot("2026-09-24T00:00:00Z", agent="qwen-local", quota_pool_id="qwen-local",
                     weekly_remaining_fraction=0.7, weekly_reset_at=reset),
            snapshot("2026-09-24T01:00:00Z", agent="other", quota_pool_id=pool,
                     weekly_remaining_fraction=0.01, weekly_reset_at=reset),
        ]
        runs = [
            {"agent": "qwen", "pool_id": "qwen", "completed_at": "2026-09-23T12:00:00Z",
             "token_usage": {"total_tokens": 100}},
            {"agent": "qwen-local", "shared_pool_id": "qwen-local",
             "completed_at": "2026-09-24T01:00:00Z", "token_usage": {"total_tokens": 50}},
            {"agent": "other", "quota_pool_id": pool, "completed_at": "2026-09-23T12:00:00Z",
             "token_usage": {"total_tokens": 9000}},
            {"agent": "other", "quota_pool_id": pool, "completed_at": "2026-09-24T01:00:00Z",
             "token_usage": {"total_tokens": 9000}},
        ]
        result = quota_pace.calculate_pool_pace(
            pool, snapshots=rows, runs=runs,
            pool_ids={"qwen": pool, "qwen-local": pool, "other": "separate-subscription"},
            now=dt.datetime(2026, 9, 24, 2, tzinfo=UTC),
        )
        self.assertEqual(result["source"], "fleet-projection")
        self.assertAlmostEqual(result["remaining_fraction"], 0.65)

    def test_calibration_projects_only_measured_fleet_tokens_for_same_pool(self) -> None:
        rows = [
            snapshot("2026-09-23T00:00:00Z", weekly_remaining_fraction=0.8,
                     weekly_reset_at="2026-09-29T00:00:00Z"),
            snapshot("2026-09-24T00:00:00Z", weekly_remaining_fraction=0.7,
                     weekly_reset_at="2026-09-29T00:00:00Z"),
        ]
        runs = [
            {"agent": "mimo", "completed_at": "2026-09-23T12:00:00Z", "token_usage": {"total_tokens": 100}},
            {"agent": "mimo", "completed_at": "2026-09-24T01:00:00Z", "token_usage": {"total_tokens": 50}},
            {"agent": "other", "completed_at": "2026-09-24T01:00:00Z", "token_usage": {"total_tokens": 9999}},
        ]
        result = pace(rows, "2026-09-24T02:00:00Z", runs)
        self.assertAlmostEqual(result["remaining_fraction"], 0.65)
        self.assertEqual(result["confidence"], 0.8)
        self.assertEqual(result["source"], "fleet-projection")
        self.assertAlmostEqual(result["tempo"], 0.35 / (2 + 2 / 24) * 7, places=5)
        without_calibration = pace(rows, "2026-09-24T02:00:00Z")
        self.assertEqual(without_calibration["source"], "provider-dashboard")
        self.assertAlmostEqual(without_calibration["remaining_fraction"], 0.7)

    def test_unknown_run_pool_aliases_are_used_without_profile(self) -> None:
        rows = [
            snapshot("2026-09-23T00:00:00Z", weekly_remaining_fraction=0.8,
                     weekly_reset_at="2026-09-29T00:00:00Z"),
            snapshot("2026-09-24T00:00:00Z", weekly_remaining_fraction=0.7,
                     weekly_reset_at="2026-09-29T00:00:00Z"),
        ]
        runs = [
            {"agent": "other", "pool_id": POOL, "completed_at": "2026-09-23T12:00:00Z",
             "token_usage": {"total_tokens": 100}},
            {"agent": "other", "shared_pool_id": POOL, "completed_at": "2026-09-24T01:00:00Z",
             "token_usage": {"total_tokens": 50}},
        ]
        result = pace(rows, "2026-09-24T02:00:00Z", runs)
        self.assertEqual(result["source"], "fleet-projection")
        self.assertAlmostEqual(result["remaining_fraction"], 0.65)

    def test_unknown_snapshot_uses_its_recorded_pool(self) -> None:
        row = snapshot(
            "2026-09-24T00:00:00Z", agent="unprofiled", quota_pool_id=POOL,
            weekly_remaining_fraction=0.7, weekly_reset_at="2026-09-29T00:00:00Z",
        )
        result = pace([row], "2026-09-24T02:00:00Z")
        self.assertEqual(result["window"], "weekly")
        self.assertEqual(result["source"], "provider-dashboard")

    def test_newest_observation_wins_window_length_for_same_reset(self) -> None:
        # Two rows disagree on the length of the same short window (same
        # reset_at). The newest observation declares 1 h; the older one 5 h.
        # The window start must follow the newest sample, or the latest
        # observation falls outside the computed window and is skipped.
        older = snapshot(
            "2026-09-24T05:00:00Z", short_window_remaining_fraction=0.9,
            short_window_reset_at="2026-09-24T10:00:00Z", short_window_hours=5,
        )
        newer = snapshot(
            "2026-09-24T09:30:00Z", short_window_remaining_fraction=0.5,
            short_window_reset_at="2026-09-24T10:00:00Z", short_window_hours=1,
        )
        for rows in ([older, newer], [newer, older]):
            result = pace(rows, "2026-09-24T09:45:00Z")
            self.assertEqual(result["window"], "short")
            self.assertEqual(result["observed_at"], "2026-09-24T09:30:00Z")
            # Window start 09:00 (newest sample's 1 h), not 05:00: the
            # observation sits at 30/60 minutes of its own window.
            self.assertAlmostEqual(result["elapsed_fraction"], 0.5, places=5)
            self.assertAlmostEqual(result["tempo"], 1.0, places=5)

    def test_age_decay_and_early_window_damping(self) -> None:
        row = snapshot(
            "2026-09-23T00:00:00Z", weekly_remaining_fraction=0.9,
            weekly_reset_at="2026-09-29T00:00:00Z",
        )
        self.assertEqual(pace([row], "2026-09-23T12:00:00Z")["confidence"], 1.0)
        self.assertAlmostEqual(pace([row], "2026-09-24T00:00:00Z")["confidence"], 0.8)
        self.assertEqual(pace([row], "2026-09-26T00:00:00Z")["confidence"], 0.0)
        # The same observation is 0.001 of a seven-day window after reset.
        early = snapshot(
            "2026-09-22T00:10:00Z", weekly_remaining_fraction=0.99,
            weekly_reset_at="2026-09-29T00:00:00Z",
        )
        self.assertLess(pace([early], "2026-09-22T00:10:00Z")["confidence"], 0.05)
        # The older snapshot is ignored once its active window ends.
        self.assertIsNone(pace([row], "2026-09-30T00:00:00Z")["tempo"])

    def test_missing_data_is_neutral_and_never_guesses(self) -> None:
        result = pace([], "2026-09-24T00:00:00Z")
        self.assertEqual(result, {
            "tempo": None, "confidence": 0.0, "source": "none", "reason": "no-active-window-evidence"
        })
        self.assertIsNone(pace([
            snapshot("2026-09-24T00:00:00Z", credits_remaining=10)
        ], "2026-09-24T00:00:00Z", plan_capacity_credits=100)["tempo"])


if __name__ == "__main__":
    unittest.main()
