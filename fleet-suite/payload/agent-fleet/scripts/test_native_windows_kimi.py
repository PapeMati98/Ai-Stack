from __future__ import annotations

import datetime as dt
import json
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock


SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

import fleet  # noqa: E402
import kimi_usage  # noqa: E402
import quota_availability  # noqa: E402
import adaptive  # noqa: E402

UTC = dt.timezone.utc


def iso(value: dt.datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


class KimiParseUsageWindowHoursTests(unittest.TestCase):
    def test_parse_usage_reports_window_hours_per_scope(self) -> None:
        payload = {
            "usage": {"used": "25", "limit": "100"},
            "limits": [
                {
                    "window": {"duration": 1, "timeUnit": "TIME_UNIT_WEEK"},
                    "detail": {"used": "16", "limit": "100"},
                },
                {
                    "window": {"duration": 5, "timeUnit": "TIME_UNIT_HOUR"},
                    "detail": {"used": "24", "limit": "100"},
                },
                {
                    "window": {"duration": 300, "timeUnit": "TIME_UNIT_MINUTE"},
                    "detail": {"used": "1", "limit": "100"},
                },
                {
                    "window": {"duration": 1, "timeUnit": "TIME_UNIT_DAY"},
                    "detail": {"used": "2", "limit": "100"},
                },
                {
                    "window": {"duration": 1, "timeUnit": "TIME_UNIT_MONTH"},
                    "detail": {"used": "3", "limit": "100"},
                },
                {
                    "window": {"duration": 1, "timeUnit": "TIME_UNIT_UNKNOWN"},
                    "detail": {"used": "4", "limit": "100"},
                },
            ],
        }
        result = kimi_usage.parse_usage(payload)
        by_scope = {}
        for window in result["windows"]:
            by_scope.setdefault(window["scope"], []).append(window)
        self.assertIsNone(by_scope["weekly"][0]["window_hours"])  # summary row
        self.assertEqual(by_scope["weekly"][1]["window_hours"], 168.0)
        self.assertEqual(by_scope["short-window"][0]["window_hours"], 5.0)
        self.assertEqual(by_scope["short-window"][1]["window_hours"], 5.0)
        self.assertEqual(by_scope["daily"][0]["window_hours"], 24.0)
        self.assertEqual(by_scope["monthly"][0]["window_hours"], 720.0)
        self.assertIsNone(by_scope["short-window"][2]["window_hours"])


class KimiNativeWindowsPaceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.now = dt.datetime.now(UTC)
        self.profiles = {"kimi": {"adapter": "kimi", "quota_pool_id": "kimi"}}

    def seed_cache(self, root: Path, state: dict) -> None:
        path = quota_availability.cache_path(root, "kimi")
        path.parent.mkdir(parents=True, exist_ok=True)
        value = {
            "schema_version": 1,
            "observed_at": quota_availability.stamp(time.time()),
            "recheck_at": quota_availability.stamp(time.time() + 300),
            **state,
        }
        path.write_text(json.dumps(value), encoding="utf-8")

    def views_without_network(self, root: Path):
        with (
            mock.patch.object(
                fleet.kimi_usage, "query", side_effect=AssertionError("network")
            ) as query,
            mock.patch.object(
                fleet.kimi_usage, "native_probe", side_effect=AssertionError("probe")
            ) as probe,
        ):
            views, diagnostics = fleet.current_quota_views(root, self.profiles)
        query.assert_not_called()
        probe.assert_not_called()
        return views, diagnostics

    def test_weekly_window_from_availability_state_feeds_pace(self) -> None:
        # Weekly 16/100 used at ~57 % of the window -> tempo ~0.28.
        reset = self.now + dt.timedelta(days=7 * 0.43)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "fleet-root"
            self.seed_cache(
                root,
                {
                    "status": "available",
                    "source": "kimi-native-usage",
                    "windows": [
                        {
                            "scope": "weekly",
                            "used": 16,
                            "limit": 100,
                            "reset_at": iso(reset),
                            "window_hours": 168.0,
                        }
                    ],
                },
            )
            views, diagnostics = self.views_without_network(root)
        view = views["kimi"]
        self.assertEqual(view["weekly_remaining_fraction"], 0.84)
        self.assertEqual(view["weekly_reset_at"], iso(reset))
        # Availability/exhaustion semantics stay untouched: the numeric fields
        # are pace evidence only, the view status is not rewritten.
        self.assertEqual(view["status"], "unknown")
        self.assertIs(view["availability_blocked"], False)
        pace = view["pace"]
        self.assertEqual(pace["window"], "weekly")
        self.assertEqual(pace["source"], "kimi-native-usage")
        self.assertAlmostEqual(pace["tempo"], 0.16 / 0.57, delta=0.02)
        self.assertEqual(diagnostics["kimi"]["pace"], pace)

    def test_short_window_uses_api_window_hours_without_profile_fallback(self) -> None:
        weekly_reset = self.now + dt.timedelta(days=7 * 0.43)
        short_reset = self.now + dt.timedelta(hours=1)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "fleet-root"
            self.seed_cache(
                root,
                {
                    "status": "available",
                    "source": "kimi-native-usage",
                    "windows": [
                        {
                            "scope": "weekly",
                            "used": 16,
                            "limit": 100,
                            "reset_at": iso(weekly_reset),
                            "window_hours": 168.0,
                        },
                        {
                            "scope": "short-window",
                            "used": 24,
                            "limit": 100,
                            "reset_at": iso(short_reset),
                            "window_hours": 5.0,
                        },
                    ],
                },
            )
            views, _ = self.views_without_network(root)
        view = views["kimi"]
        self.assertEqual(view["short_window_remaining_fraction"], 0.76)
        self.assertEqual(view["short_window_hours"], 5.0)
        # 5 h window from the API: 4 h elapsed, 24 % used -> tempo 0.3, which
        # beats the weekly 0.28 and proves the row-level window length is used
        # (no quota_collection.short_window_hours exists on this profile).
        pace = view["pace"]
        self.assertEqual(pace["window"], "short")
        self.assertAlmostEqual(pace["tempo"], 0.24 / 0.8, delta=0.02)

    def test_native_probe_state_without_windows_leaves_pace_unknown(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "fleet-root"
            self.seed_cache(
                root,
                {
                    "status": "available",
                    "source": "native-cli-check",
                    "covered_scopes": ["weekly", "monthly", "daily", "short-window"],
                },
            )
            views, _ = self.views_without_network(root)
        view = views["kimi"]
        self.assertIsNone(view.get("weekly_remaining_fraction"))
        self.assertIsNone(view["pace"]["tempo"])
        self.assertEqual(view["pace"]["source"], "none")
        self.assertEqual(view["pace"]["reason"], "no-active-window-evidence")

    def test_exhausted_state_stays_fail_closed_without_window_fractions(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "fleet-root"
            self.seed_cache(
                root,
                {
                    "status": "exhausted",
                    "scope": "weekly",
                    "source": "native-provider-error",
                    "reason": "usage-limit",
                    "reset_at": None,
                },
            )
            views, _ = self.views_without_network(root)
        view = views["kimi"]
        self.assertIs(view["availability_blocked"], True)
        self.assertIsNone(view.get("weekly_remaining_fraction"))
        self.assertIsNone(view["pace"]["tempo"])

    def test_failed_recheck_does_not_export_cached_exhausted_windows(self) -> None:
        # check() retains the previous source and windows on a failed recheck,
        # but marks that cached observation recheck-required.
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "fleet-root"
            self.seed_cache(
                root,
                {
                    "status": "exhausted",
                    "scope": "weekly",
                    "source": "kimi-native-usage",
                    "observed_at": iso(self.now - dt.timedelta(minutes=15)),
                    "recheck_at": iso(self.now - dt.timedelta(minutes=1)),
                    "windows": [
                        {
                            "scope": "weekly",
                            "used": 100,
                            "limit": 100,
                            "reset_at": iso(self.now + dt.timedelta(days=3)),
                            "window_hours": 168.0,
                        }
                    ],
                },
            )
            with (
                mock.patch.object(fleet.kimi_usage, "query", side_effect=OSError("offline")) as query,
                mock.patch.object(fleet.kimi_usage, "native_probe", side_effect=OSError("offline")) as probe,
            ):
                views, diagnostics = fleet.current_quota_views(root, self.profiles)
            query.assert_called_once()
            probe.assert_called_once()
            exported = json.loads((root / "pace" / "latest.json").read_text(encoding="utf-8"))
        view = views["kimi"]
        self.assertEqual(diagnostics["kimi"]["availability"]["status"], "recheck-required")
        self.assertEqual(diagnostics["kimi"]["availability"]["source"], "kimi-native-usage")
        self.assertIs(view["availability_blocked"], True)
        self.assertNotIn("weekly_remaining_fraction", view)
        self.assertIsNone(view["pace"]["tempo"])
        self.assertIsNone(exported["pools"]["kimi"]["tempo"])

class KimiNativeWindowSourceTests(unittest.TestCase):
    def test_confirmed_exhausted_usage_keeps_its_windows(self) -> None:
        now = dt.datetime.now(UTC)
        view = {"source": "none", "status": "unknown"}
        fleet._apply_kimi_native_windows(
            view,
            {
                "status": "exhausted",
                "source": "kimi-native-usage",
                "observed_at": iso(now),
                "recheck_at": iso(now + dt.timedelta(minutes=5)),
                "windows": [
                    {
                        "scope": "weekly",
                        "used": 100,
                        "limit": 100,
                        "reset_at": iso(now + dt.timedelta(days=3)),
                    }
                ],
            },
        )
        self.assertEqual(view["weekly_remaining_fraction"], 0.0)
        self.assertEqual(view["source"], "kimi-native-usage")

    def test_applied_windows_own_the_view_source(self) -> None:
        # A view that earlier carried a provider-dashboard label must report
        # kimi-native-usage once the /usages windows become its pace evidence.
        view = {"source": "provider-dashboard", "status": "fresh"}
        state = {
            "status": "available",
            "source": "kimi-native-usage",
            "observed_at": "2026-09-24T12:00:00Z",
            "recheck_at": iso(dt.datetime.now(UTC) + dt.timedelta(minutes=5)),
            "windows": [
                {
                    "scope": "weekly",
                    "used": 16,
                    "limit": 100,
                    "reset_at": "2026-09-27T12:15:00Z",
                    "window_hours": 168.0,
                }
            ],
        }
        fleet._apply_kimi_native_windows(view, state)
        self.assertEqual(view["source"], "kimi-native-usage")
        self.assertEqual(view["weekly_remaining_fraction"], 0.84)

    def test_unapplied_state_keeps_existing_source(self) -> None:
        view = {"source": "provider-dashboard", "status": "fresh"}
        fleet._apply_kimi_native_windows(
            view, {"source": "native-cli-check", "observed_at": "2026-09-24T12:00:00Z"}
        )
        self.assertEqual(view["source"], "provider-dashboard")
        self.assertNotIn("weekly_remaining_fraction", view)

    def test_leftover_provider_dashboard_windows_leave_the_relabelled_view(self) -> None:
        # A stored provider-dashboard monthly reading must not end up under
        # the kimi-native-usage label when /usages refreshes only the weekly
        # window: the unrefreshed fields are dropped from the view, and the
        # stored record keeps feeding pace with its own source.
        view = {
            "source": "provider-dashboard",
            "status": "fresh",
            "monthly_remaining_fraction": 0.7,
            "monthly_reset_at": "2026-10-14T12:00:00Z",
            "credits_remaining": 12345,
        }
        state = {
            "status": "available",
            "source": "kimi-native-usage",
            "observed_at": "2026-09-24T12:00:00Z",
            "recheck_at": iso(dt.datetime.now(UTC) + dt.timedelta(minutes=5)),
            "windows": [
                {
                    "scope": "weekly",
                    "used": 16,
                    "limit": 100,
                    "reset_at": "2026-09-27T12:15:00Z",
                    "window_hours": 168.0,
                }
            ],
        }
        fleet._apply_kimi_native_windows(view, state)
        self.assertEqual(view["source"], "kimi-native-usage")
        self.assertEqual(view["weekly_remaining_fraction"], 0.84)
        self.assertNotIn("monthly_remaining_fraction", view)
        self.assertNotIn("monthly_reset_at", view)
        self.assertNotIn("credits_remaining", view)

    def test_invalid_status_or_expired_cache_cannot_supply_windows(self) -> None:
        now = dt.datetime.now(UTC)
        base = {
            "source": "kimi-native-usage",
            "observed_at": iso(now - dt.timedelta(minutes=5)),
            "recheck_at": iso(now + dt.timedelta(minutes=5)),
            "windows": [
                {
                    "scope": "weekly",
                    "used": 16,
                    "limit": 100,
                    "reset_at": iso(now + dt.timedelta(days=3)),
                }
            ],
        }
        invalid = [
            {"status": "recheck-required"},
            {"status": "unknown"},
            {"status": "check-unavailable"},
            {"status": "available", "recheck_at": iso(now - dt.timedelta(seconds=1))},
            {"status": "available", "recheck_at": None},
        ]
        for override in invalid:
            with self.subTest(override=override):
                view = {"source": "provider-dashboard", "status": "fresh"}
                fleet._apply_kimi_native_windows(view, {**base, **override})
                self.assertEqual(view["source"], "provider-dashboard")
                self.assertNotIn("weekly_remaining_fraction", view)


class KimiMixedProvenancePaceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.now = dt.datetime.now(UTC)
        self.profiles = {"kimi": {"adapter": "kimi", "quota_pool_id": "kimi"}}

    def seed_cache(self, root: Path, state: dict) -> None:
        path = quota_availability.cache_path(root, "kimi")
        path.parent.mkdir(parents=True, exist_ok=True)
        value = {
            "schema_version": 1,
            "observed_at": quota_availability.stamp(time.time()),
            "recheck_at": quota_availability.stamp(time.time() + 300),
            **state,
        }
        path.write_text(json.dumps(value), encoding="utf-8")

    def test_monthly_pace_keeps_provider_dashboard_source(self) -> None:
        observed = self.now - dt.timedelta(minutes=2)
        monthly_reset = self.now + dt.timedelta(days=20)
        stored = {
            "schema_version": 1,
            "snapshot_id": "quota-stored-kimi",
            "agent": "kimi",
            "quota_pool_id": "kimi",
            "observed_at": observed.isoformat(),
            "valid_for_hours": 6,
            "source": "provider-dashboard",
            "confidence": 1.0,
            "monthly_remaining_fraction": 0.7,
            "monthly_reset_at": monthly_reset.isoformat(),
        }
        weekly_reset = self.now + dt.timedelta(days=7 * 0.43)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "fleet-root"
            self.seed_cache(
                root,
                {
                    "status": "available",
                    "source": "kimi-native-usage",
                    "windows": [
                        {
                            "scope": "weekly",
                            "used": 16,
                            "limit": 100,
                            "reset_at": iso(weekly_reset),
                            "window_hours": 168.0,
                        }
                    ],
                },
            )
            with (
                mock.patch.object(
                    fleet.kimi_usage, "query", side_effect=AssertionError("network")
                ),
                mock.patch.object(
                    fleet.kimi_usage,
                    "native_probe",
                    side_effect=AssertionError("probe"),
                ),
                mock.patch.object(
                    adaptive,
                    "latest_quotas",
                    return_value={"kimi": adaptive._quota_view(stored, current=self.now)},
                ),
                mock.patch.object(
                    adaptive, "read_quota_records", return_value=[stored]
                ),
                mock.patch.object(
                    fleet.quota_collectors,
                    "collect_profile_quotas",
                    return_value=({}, {}),
                ),
                mock.patch.object(fleet, "read_usage", return_value=[]),
            ):
                views, diagnostics = fleet.current_quota_views(root, self.profiles)
        view = views["kimi"]
        self.assertEqual(view["source"], "kimi-native-usage")
        self.assertEqual(view["weekly_remaining_fraction"], 0.84)
        self.assertNotIn("monthly_remaining_fraction", view)
        # The monthly window out-tempos the weekly one and wins the pace
        # pick; its evidence is the stored provider-dashboard record, so the
        # pace source must not claim kimi-native-usage.
        pace = view["pace"]
        self.assertEqual(pace["window"], "monthly")
        self.assertEqual(pace["source"], "provider-dashboard")
        self.assertAlmostEqual(pace["remaining_fraction"], 0.7, places=6)
        self.assertEqual(diagnostics["kimi"]["pace"], pace)


if __name__ == "__main__":
    unittest.main()
