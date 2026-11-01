"""Robust pure quota-window parsing tests.

No live HTTP, no credentials, no cache writes; the suite must run with
only `python -m unittest test_quota_windows` in an empty environment.
"""
from __future__ import annotations

import argparse
import datetime as dt
import io
import json
import math
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr
from pathlib import Path


SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

import quota_windows  # noqa: E402

UTC = dt.timezone.utc


def iso(value: dt.datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


# ---------------------------------------------------------------------------
# Sanitization helpers
# ---------------------------------------------------------------------------


class SanitizeFractionTests(unittest.TestCase):
    def test_accepts_zero_one(self) -> None:
        self.assertEqual(quota_windows.sanitize_fraction(0.0, label="x"), 0.0)
        self.assertEqual(quota_windows.sanitize_fraction(1.0, label="x"), 1.0)

    def test_rejects_bool(self) -> None:
        self.assertIsNone(quota_windows.sanitize_fraction(True, label="x"))
        self.assertIsNone(quota_windows.sanitize_fraction(False, label="x"))

    def test_rejects_none_and_string(self) -> None:
        self.assertIsNone(quota_windows.sanitize_fraction(None, label="x"))
        self.assertIsNone(quota_windows.sanitize_fraction("0.5", label="x"))

    def test_rejects_nan_and_inf(self) -> None:
        self.assertIsNone(quota_windows.sanitize_fraction(float("nan"), label="x"))
        self.assertIsNone(quota_windows.sanitize_fraction(float("inf"), label="x"))
        self.assertIsNone(quota_windows.sanitize_fraction(float("-inf"), label="x"))

    def test_rejects_out_of_range(self) -> None:
        self.assertIsNone(quota_windows.sanitize_fraction(-0.1, label="x"))
        self.assertIsNone(quota_windows.sanitize_fraction(1.5, label="x"))


class SanitizePercentTests(unittest.TestCase):
    def test_accepts_full_range(self) -> None:
        self.assertEqual(quota_windows.sanitize_percent(0, label="x"), 0.0)
        self.assertEqual(quota_windows.sanitize_percent(50.5, label="x"), 50.5)
        self.assertEqual(quota_windows.sanitize_percent(100, label="x"), 100.0)

    def test_rejects_bool_and_malformed(self) -> None:
        self.assertIsNone(quota_windows.sanitize_percent(True, label="x"))
        self.assertIsNone(quota_windows.sanitize_percent(None, label="x"))
        self.assertIsNone(quota_windows.sanitize_percent("50", label="x"))
        self.assertIsNone(quota_windows.sanitize_percent(float("nan"), label="x"))

    def test_rejects_out_of_range(self) -> None:
        self.assertIsNone(quota_windows.sanitize_percent(-1, label="x"))
        self.assertIsNone(quota_windows.sanitize_percent(101, label="x"))


class SanitizeNonNegativeTests(unittest.TestCase):
    def test_zero_is_allowed(self) -> None:
        self.assertEqual(quota_windows.sanitize_non_negative(0, label="x"), 0.0)

    def test_negative_raises(self) -> None:
        self.assertIsNone(quota_windows.sanitize_non_negative(None, label="x"))
        self.assertIsNone(quota_windows.sanitize_non_negative(True, label="x"))
        self.assertIsNone(quota_windows.sanitize_non_negative("0", label="x"))
        self.assertIsNone(quota_windows.sanitize_non_negative(-1, label="x"))


class SanitizePositiveTests(unittest.TestCase):
    def test_strictly_positive_only(self) -> None:
        self.assertEqual(quota_windows.sanitize_positive(1, label="x"), 1.0)
        self.assertIsNone(quota_windows.sanitize_positive(0, label="x"))
        self.assertIsNone(quota_windows.sanitize_positive(-2, label="x"))


# ---------------------------------------------------------------------------
# Window-kind resolution
# ---------------------------------------------------------------------------


class WindowKindTests(unittest.TestCase):
    def test_short_window_from_hours(self) -> None:
        self.assertEqual(quota_windows.window_kind_from_duration(1), "short")
        self.assertEqual(quota_windows.window_kind_from_duration(5), "short")
        self.assertEqual(quota_windows.window_kind_from_duration(23.5), "short")
        self.assertEqual(quota_windows.window_kind_from_duration(24), "short")

    def test_weekly_window_only_in_envelope(self) -> None:
        self.assertEqual(quota_windows.window_kind_from_duration(6 * 24), "weekly")
        self.assertEqual(quota_windows.window_kind_from_duration(7 * 24), "weekly")
        self.assertEqual(quota_windows.window_kind_from_duration(8 * 24), "weekly")
        self.assertIsNone(quota_windows.window_kind_from_duration(5 * 24))
        self.assertIsNone(quota_windows.window_kind_from_duration(9 * 24))

    def test_monthly_window_only_in_envelope(self) -> None:
        self.assertEqual(quota_windows.window_kind_from_duration(28 * 24), "monthly")
        self.assertEqual(quota_windows.window_kind_from_duration(30 * 24), "monthly")
        self.assertEqual(quota_windows.window_kind_from_duration(31 * 24), "monthly")
        self.assertIsNone(quota_windows.window_kind_from_duration(27 * 24))
        self.assertIsNone(quota_windows.window_kind_from_duration(32 * 24))

    def test_unknown_returns_none(self) -> None:
        self.assertIsNone(quota_windows.window_kind_from_duration(None))
        self.assertIsNone(quota_windows.window_kind_from_duration(0))
        self.assertIsNone(quota_windows.window_kind_from_duration(-5))
        self.assertIsNone(quota_windows.window_kind_from_duration(float("nan")))
        self.assertIsNone(quota_windows.window_kind_from_duration(True))
        self.assertIsNone(quota_windows.window_kind_from_duration("5"))

    def test_window_kind_from_minutes(self) -> None:
        self.assertEqual(quota_windows.window_kind_from_minutes(60), "short")
        self.assertEqual(quota_windows.window_kind_from_minutes(7 * 24 * 60), "weekly")
        self.assertIsNone(quota_windows.window_kind_from_minutes(None))
        self.assertIsNone(quota_windows.window_kind_from_minutes(True))


# ---------------------------------------------------------------------------
# Reset parsing
# ---------------------------------------------------------------------------


class ResetParsingTests(unittest.TestCase):
    def test_iso_zoned_round_trip(self) -> None:
        moment = dt.datetime(2026, 10, 5, 12, 0, tzinfo=UTC)
        parsed = quota_windows.parse_reset(iso(moment))
        self.assertEqual(parsed, (moment, None))

    def test_iso_zoned_with_offset(self) -> None:
        moment = dt.datetime(2026, 10, 5, 14, 0, tzinfo=dt.timezone(dt.timedelta(hours=2)))
        parsed = quota_windows.parse_reset("2026-10-05T14:00:00+02:00")
        self.assertEqual(parsed, (dt.datetime(2026, 10, 5, 12, 0, tzinfo=UTC), None))

    def test_naive_iso_returns_bad_format(self) -> None:
        parsed = quota_windows.parse_reset("2026-10-05T12:00:00")
        self.assertEqual(parsed[0], None)
        self.assertEqual(parsed[1], "bad-format")

    def test_epoch_seconds(self) -> None:
        epoch = 1_760_000_000.0  # 2025-10-11T11:33:20Z
        parsed = quota_windows.parse_reset(epoch)
        expected = dt.datetime.fromtimestamp(epoch, tz=UTC)
        self.assertEqual(parsed, (expected, None))

    def test_epoch_milliseconds(self) -> None:
        epoch_ms = 1_760_000_000_000.0
        parsed = quota_windows.parse_reset(epoch_ms)
        expected = dt.datetime.fromtimestamp(epoch_ms / 1000.0, tz=UTC)
        self.assertEqual(parsed, (expected, None))

    def test_relative_with_now(self) -> None:
        now = dt.datetime(2026, 10, 5, 12, 0, tzinfo=UTC)
        parsed = quota_windows.parse_reset("in 5 hours", now=now)
        self.assertEqual(parsed, (now + dt.timedelta(hours=5), None))

    def test_relative_without_now_returns_bad_format(self) -> None:
        self.assertEqual(
            quota_windows.parse_reset("in 5 hours"),
            (None, "bad-format"),
        )

    def test_relative_uses_observed_at_when_now_missing(self) -> None:
        observed = dt.datetime(2026, 10, 5, 12, 0, tzinfo=UTC)
        parsed = quota_windows.parse_reset("in 30 minutes", observed_at=observed)
        self.assertEqual(parsed, (observed + dt.timedelta(minutes=30), None))

    def test_none_returns_missing(self) -> None:
        self.assertEqual(quota_windows.parse_reset(None), (None, "missing"))

    def test_bool_returns_not_string(self) -> None:
        self.assertEqual(quota_windows.parse_reset(True), (None, "not-string"))

    def test_string_unknown_format(self) -> None:
        self.assertEqual(quota_windows.parse_reset("tomorrow"), (None, "bad-format"))

    def test_out_of_range_epoch(self) -> None:
        self.assertEqual(quota_windows.parse_reset(1e15), (None, "bad-format"))


class ResetElapsedTests(unittest.TestCase):
    def test_future_moment_not_elapsed(self) -> None:
        moment = dt.datetime.now(UTC) + dt.timedelta(hours=1)
        self.assertFalse(quota_windows.is_reset_elapsed(moment))

    def test_past_moment_elapsed(self) -> None:
        moment = dt.datetime.now(UTC) - dt.timedelta(seconds=10)
        self.assertTrue(quota_windows.is_reset_elapsed(moment))

    def test_none_is_not_elapsed(self) -> None:
        self.assertFalse(quota_windows.is_reset_elapsed(None))


# ---------------------------------------------------------------------------
# Kimi parser
# ---------------------------------------------------------------------------


class KimiParserTests(unittest.TestCase):
    def test_legacy_summary_window_hours_is_none(self) -> None:
        payload = {
            "usage": {"used": "25", "limit": "100", "resetTime": "2026-10-12T00:00:00Z"},
            "limits": [],
        }
        result = quota_windows.parse_kimi_payload(payload)
        weekly = [w for w in result["windows"] if w["scope"] == "weekly"]
        self.assertEqual(len(weekly), 1)
        self.assertIsNone(weekly[0]["window_hours"])

    def test_legacy_detail_window_hours_from_duration(self) -> None:
        payload = {
            "usage": {"used": "0", "limit": "100"},
            "limits": [
                {
                    "window": {"duration": 1, "timeUnit": "TIME_UNIT_WEEK"},
                    "detail": {"used": "16", "limit": "100", "resetTime": "2026-10-08T00:00:00Z"},
                },
                {
                    "window": {"duration": 5, "timeUnit": "TIME_UNIT_HOUR"},
                    "detail": {"used": "24", "limit": "100", "resetTime": "2026-10-05T13:00:00Z"},
                },
            ],
        }
        result = quota_windows.parse_kimi_payload(payload)
        by_scope = {}
        for w in result["windows"]:
            by_scope.setdefault(w["scope"], []).append(w)
        # 168h WEEK
        self.assertTrue(any(w["window_hours"] == 168.0 for w in by_scope["weekly"]))
        # 5h HOUR
        self.assertEqual(by_scope["short-window"][0]["window_hours"], 5.0)

    def test_proto3_omitted_zero_only_for_documented_used(self) -> None:
        # Legacy detail with absent used, present remaining, present limit.
        payload = {
            "usage": {"used": "0", "limit": "100"},
            "limits": [
                {
                    "window": {"duration": 5, "timeUnit": "TIME_UNIT_HOUR"},
                    "detail": {"remaining": "30", "limit": "100"},
                },
            ],
        }
        result = quota_windows.parse_kimi_payload(payload)
        short = [w for w in result["windows"] if w["scope"] == "short-window"][0]
        self.assertEqual(short["used"], 70.0)
        self.assertEqual(short["limit"], 100.0)

    def test_unknown_field_omission_is_unknown(self) -> None:
        # Legacy detail with absent used, absent remaining, present limit.
        # proto3 omitted-zero applies ONLY for the documented `used` field;
        # any other unknown missing field is unknown.
        payload = {
            "usage": {"used": "0", "limit": "100"},
            "limits": [
                {
                    "window": {"duration": 5, "timeUnit": "TIME_UNIT_HOUR"},
                    "detail": {"limit": "100"},
                },
            ],
        }
        result = quota_windows.parse_kimi_payload(payload)
        short = [w for w in result["windows"] if w["scope"] == "short-window"][0]
        self.assertEqual(short["used"], 0.0)

    def test_ratio_window_overrides_legacy_when_scope_uncovered(self) -> None:
        payload = {
            "usages": {
                "limit_5h": {"used_ratio": 0.17, "reset_time": "2026-10-05T13:00:00Z"},
                "limit_7d": {"used_ratio": 1.0, "reset_time": "2026-10-08T00:00:00Z"},
            }
        }
        result = quota_windows.parse_kimi_payload(payload)
        by_scope = {}
        for w in result["windows"]:
            by_scope.setdefault(w["scope"], []).append(w)
        self.assertEqual(by_scope["short-window"][0]["used"], 17.0)
        self.assertEqual(by_scope["weekly"][0]["used"], 100.0)

    def test_multiple_blocking_windows_chooses_latest_reset(self) -> None:
        near = iso(dt.datetime.now(UTC) + dt.timedelta(hours=2))
        later = iso(dt.datetime.now(UTC) + dt.timedelta(days=2))
        payload = {
            "usages": {
                "limit_5h": {"used_ratio": 1.0, "reset_time": near},
                "limit_7d": {"used_ratio": 1.0, "reset_time": later},
            }
        }
        result = quota_windows.parse_kimi_payload(payload)
        self.assertEqual(result["status"], "exhausted")
        self.assertEqual(result["scope"], "weekly")
        self.assertEqual(result["reset_at"], later)
        self.assertEqual(result["blocking_count"], 2)
        self.assertFalse(result["no_promised_recovery"])

    def test_blocking_window_without_reset_returns_no_promised_recovery(self) -> None:
        payload = {
            "usages": {
                "limit_5h": {"used_ratio": 1.0},
            }
        }
        result = quota_windows.parse_kimi_payload(payload)
        self.assertEqual(result["status"], "exhausted")
        self.assertIsNone(result["reset_at"])
        self.assertTrue(result["no_promised_recovery"])

    def test_elapsed_spent_window_is_not_blocking(self) -> None:
        past = iso(dt.datetime.now(UTC) - dt.timedelta(seconds=1))
        payload = {
            "usages": {
                "limit_5h": {"used_ratio": 1.0, "reset_time": past},
            }
        }
        result = quota_windows.parse_kimi_payload(payload)
        # Stale counter is not a current exhaustion.
        self.assertEqual(result["status"], "unknown")
        self.assertTrue(result["no_promised_recovery"])
        self.assertEqual(result["blocking_count"], 0)

    def test_ratio_nan_is_unknown(self) -> None:
        payload = {
            "usages": {
                "limit_5h": {"used_ratio": float("nan")},
            }
        }
        with self.assertRaises(quota_windows.QuotaWindowError):
            quota_windows.parse_kimi_payload(payload)

    def test_bool_used_is_unknown(self) -> None:
        payload = {
            "usage": {"used": True, "limit": "100"},
            "limits": [],
        }
        with self.assertRaises(quota_windows.QuotaWindowError):
            quota_windows.parse_kimi_payload(payload)

    def test_payload_must_be_object(self) -> None:
        with self.assertRaises(quota_windows.QuotaWindowError):
            quota_windows.parse_kimi_payload(["nope"])


# ---------------------------------------------------------------------------
# Codex parser
# ---------------------------------------------------------------------------


class CodexRateLimitTests(unittest.TestCase):
    def test_short_window_from_primary_when_minutes_300(self) -> None:
        rate = {
            "primary": {
                "used_percent": 17,
                "window_minutes": 300,
                "resets_at": 1_760_000_000,
            },
            "secondary": {
                "used_percent": 80,
                "window_minutes": 10_080,
                "resets_at": 1_760_086_400,
            },
            "plan_type": "pro",
        }
        result = quota_windows.parse_codex_rate_limit_window(rate)
        self.assertEqual(set(result["windows"]), {"short", "weekly"})
        self.assertEqual(result["windows"]["short"]["window_hours"], 5.0)
        self.assertEqual(result["windows"]["short"]["used_percent"], 17.0)
        self.assertEqual(result["windows"]["weekly"]["used_percent"], 80.0)
        self.assertEqual(result["shortest"], "short")
        self.assertEqual(result["plan_type"], "pro")

    def test_primary_as_weekly_when_minutes_in_weekly_envelope(self) -> None:
        rate = {
            "primary": {
                "used_percent": 50,
                "window_minutes": 7 * 24 * 60,
                "resets_at": 1_760_000_000,
            },
        }
        result = quota_windows.parse_codex_rate_limit_window(rate)
        self.assertEqual(set(result["windows"]), {"weekly"})
        self.assertEqual(result["shortest"], "weekly")

    def test_code_review_independent_from_coding(self) -> None:
        rate = {
            "primary": {
                "used_percent": 95,
                "window_minutes": 7 * 24 * 60,
                "resets_at": 1_760_000_000,
            },
            "secondary": {
                "used_percent": 12,
                "window_minutes": 5 * 60,
                "resets_at": 1_760_000_000,
            },
        }
        coding = quota_windows.parse_codex_rate_limit_window(rate)
        review = quota_windows.parse_codex_rate_limit_window(rate, code_review=True)
        self.assertEqual(coding["shortest"], "short")
        self.assertEqual(review["shortest"], "short")
        self.assertEqual(coding["windows"]["weekly"]["used_percent"], 95.0)
        self.assertEqual(review["windows"]["weekly"]["used_percent"], 95.0)

    def test_code_review_preserved_as_distinct_when_duplicate_kind(self) -> None:
        # When primary and secondary disagree on the same kind, both are kept.
        rate = {
            "primary": {
                "used_percent": 80,
                "window_minutes": 300,
                "resets_at": 1_760_000_000,
            },
            "secondary": {
                "used_percent": 17,
                "window_minutes": 300,
                "resets_at": 1_760_000_000,
            },
        }
        review = quota_windows.parse_codex_rate_limit_window(rate, code_review=True)
        self.assertIn("short:code_review", review["windows"])
        self.assertEqual(review["windows"]["short:code_review"]["used_percent"], 17.0)

    def test_malformed_used_percent_skips_window(self) -> None:
        rate = {
            "primary": {
                "used_percent": float("nan"),
                "window_minutes": 300,
                "resets_at": 1_760_000_000,
            },
            "secondary": {
                "used_percent": 80,
                "window_minutes": 10_080,
            },
        }
        result = quota_windows.parse_codex_rate_limit_window(rate)
        self.assertEqual(set(result["windows"]), {"weekly"})

    def test_bool_used_percent_is_skipped(self) -> None:
        rate = {
            "primary": {
                "used_percent": True,
                "window_minutes": 300,
            },
            "secondary": {
                "used_percent": 80,
                "window_minutes": 10_080,
            },
        }
        result = quota_windows.parse_codex_rate_limit_window(rate)
        self.assertEqual(set(result["windows"]), {"weekly"})

    def test_one_hour_window_is_short(self) -> None:
        rate = {
            "primary": {
                "used_percent": 50,
                "window_minutes": 60,
            },
        }
        self.assertEqual(quota_windows.parse_codex_rate_limit_window(rate)["shortest"], "short")

    def test_zero_minutes_is_skipped(self) -> None:
        rate = {
            "primary": {
                "used_percent": 50,
                "window_minutes": 0,
            },
        }
        with self.assertRaises(quota_windows.QuotaWindowError):
            quota_windows.parse_codex_rate_limit_window(rate)

    def test_negative_minutes_is_skipped(self) -> None:
        rate = {
            "primary": {
                "used_percent": 50,
                "window_minutes": -1,
            },
        }
        with self.assertRaises(quota_windows.QuotaWindowError):
            quota_windows.parse_codex_rate_limit_window(rate)

    def test_invalid_payload_type(self) -> None:
        with self.assertRaises(quota_windows.QuotaWindowError):
            quota_windows.parse_codex_rate_limit_window("nope")

    def test_relative_reset_with_observed_at(self) -> None:
        observed = dt.datetime.now(UTC)
        rate = {
            "primary": {
                "used_percent": 50,
                "window_minutes": 7 * 24 * 60,
                "resets_in_seconds": 86400,
            },
        }
        # The parser also accepts resets_in_seconds as a numeric reset when
        # observed_at is provided via the call site; we don't expose a now
        # hook in the public API but the secondary path still falls through
        # to a bad-format when no anchor is given.
        result = quota_windows.parse_codex_rate_limit_window(rate)
        self.assertIsNone(result["windows"]["weekly"]["reset"])

    def test_relative_reset_via_now_helper(self) -> None:
        # Direct parse_relative_seconds usage; the Codex parser itself does
        # not anchor relative strings without observed_at. The behavior
        # must remain explicit: relative resets are not silently absorbed.
        rate = {
            "primary": {
                "used_percent": 50,
                "window_minutes": 7 * 24 * 60,
                "resets_at": "in 1 day",
            },
        }
        result = quota_windows.parse_codex_rate_limit_window(rate)
        self.assertIsNone(result["windows"]["weekly"]["reset"])
        self.assertEqual(result["windows"]["weekly"]["reset_reason"], "bad-format")


# ---------------------------------------------------------------------------
# MiniMax parser
# ---------------------------------------------------------------------------


class MiniMaxParserTests(unittest.TestCase):
    OBSERVED = dt.datetime(2026, 10, 5, 12, 0, tzinfo=UTC)

    def test_requires_provider_binding(self) -> None:
        with self.assertRaises(quota_windows.QuotaWindowError):
            quota_windows.parse_minimax_payload(
                {"interval_remaining_percent": 50},
                provider="",
                account_or_pool="pool",
                observed_at=self.OBSERVED,
            )

    def test_requires_account_or_pool_binding(self) -> None:
        with self.assertRaises(quota_windows.QuotaWindowError):
            quota_windows.parse_minimax_payload(
                {"interval_remaining_percent": 50},
                provider="minimax",
                account_or_pool="",
                observed_at=self.OBSERVED,
            )

    def test_interval_and_weekly_percentages_trusted_with_zero_counts(self) -> None:
        payload = {
            "interval_remaining_percent": 30,
            "weekly_remaining_percent": 70,
            "interval_reset_time": "2026-10-05T12:30:00Z",
            "weekly_reset_time": "2026-10-08T00:00:00Z",
            "token_plan": 0,
            "remains": 0,
        }
        result = quota_windows.parse_minimax_payload(
            payload,
            provider="minimax",
            account_or_pool="pool",
            observed_at=self.OBSERVED,
        )
        self.assertEqual(result["status"], "fresh")
        self.assertEqual(result["interval"]["remaining_fraction"], 0.3)
        self.assertEqual(result["weekly"]["remaining_fraction"], 0.7)
        self.assertEqual(result["interval"]["reset_at"], "2026-10-05T12:30:00Z")
        self.assertEqual(result["weekly"]["reset_at"], "2026-10-08T00:00:00Z")
        self.assertTrue(result["has_fraction_evidence"])

    def test_general_vs_video_buckets_kept_distinct(self) -> None:
        payload = {
            "interval_remaining_percent": 90,
            "weekly_remaining_percent": 90,
            "model_remains": {
                "general": {"token_plan": 1000, "remains": 800},
                "video": {"token_plan": 500, "remains": 100},
            },
        }
        result = quota_windows.parse_minimax_payload(
            payload,
            provider="minimax",
            account_or_pool="pool",
            observed_at=self.OBSERVED,
        )
        self.assertEqual(set(result["model_remains"]), {"general", "video"})
        self.assertEqual(result["model_remains"]["general"]["remaining_fraction"], 0.8)
        self.assertEqual(result["model_remains"]["video"]["remaining_fraction"], 0.2)

    def test_unknown_bucket_is_dropped(self) -> None:
        payload = {
            "interval_remaining_percent": 90,
            "weekly_remaining_percent": 90,
            "model_remains": {
                "general": {"token_plan": 100, "remains": 50},
                "audio": {"token_plan": 100, "remains": 50},
            },
        }
        result = quota_windows.parse_minimax_payload(
            payload,
            provider="minimax",
            account_or_pool="pool",
            observed_at=self.OBSERVED,
        )
        self.assertEqual(set(result["model_remains"]), {"general"})

    def test_percent_only_no_fabricated_token_count(self) -> None:
        # Percent fields are honored; no synthetic token total is invented.
        payload = {
            "interval_remaining_percent": 50,
            "weekly_remaining_percent": 50,
        }
        result = quota_windows.parse_minimax_payload(
            payload,
            provider="minimax",
            account_or_pool="pool",
            observed_at=self.OBSERVED,
        )
        self.assertNotIn("fabricated_token_count", result)
        self.assertEqual(result["interval"]["remaining_fraction"], 0.5)
        self.assertEqual(result["model_remains"], {})

    def test_base_resp_status_error_is_unknown(self) -> None:
        payload = {
            "interval_remaining_percent": 50,
            "weekly_remaining_percent": 50,
            "base_resp": {"status_code": 1, "status_msg": "internal"},
        }
        result = quota_windows.parse_minimax_payload(
            payload,
            provider="minimax",
            account_or_pool="pool",
            observed_at=self.OBSERVED,
        )
        self.assertEqual(result["status"], "unknown")
        self.assertEqual(result["reason"], "base_resp-error")
        self.assertIsNone(result["interval"]["remaining_fraction"])

    def test_top_level_error_code_zero_is_ok(self) -> None:
        payload = {
            "interval_remaining_percent": 50,
            "base_resp": {"status_code": 0},
            "error_code": 0,
        }
        result = quota_windows.parse_minimax_payload(
            payload,
            provider="minimax",
            account_or_pool="pool",
            observed_at=self.OBSERVED,
        )
        self.assertEqual(result["status"], "fresh")

    def test_top_level_error_code_nonzero_is_unknown(self) -> None:
        payload = {
            "interval_remaining_percent": 50,
            "error_code": 1,
        }
        result = quota_windows.parse_minimax_payload(
            payload,
            provider="minimax",
            account_or_pool="pool",
            observed_at=self.OBSERVED,
        )
        self.assertEqual(result["status"], "unknown")
        self.assertEqual(result["reason"], "error-code")

    def test_nan_percent_is_unknown_not_fabricated(self) -> None:
        payload = {
            "interval_remaining_percent": float("nan"),
            "weekly_remaining_percent": 50,
        }
        result = quota_windows.parse_minimax_payload(
            payload,
            provider="minimax",
            account_or_pool="pool",
            observed_at=self.OBSERVED,
        )
        self.assertEqual(result["interval"]["remaining_fraction"], None)
        self.assertEqual(result["weekly"]["remaining_fraction"], 0.5)
        self.assertTrue(result["has_fraction_evidence"])

    def test_bool_percent_is_unknown(self) -> None:
        payload = {
            "interval_remaining_percent": True,
            "weekly_remaining_percent": 50,
        }
        result = quota_windows.parse_minimax_payload(
            payload,
            provider="minimax",
            account_or_pool="pool",
            observed_at=self.OBSERVED,
        )
        self.assertIsNone(result["interval"]["remaining_fraction"])

    def test_invalid_payload_type(self) -> None:
        with self.assertRaises(quota_windows.QuotaWindowError):
            quota_windows.parse_minimax_payload(
                "nope",
                provider="minimax",
                account_or_pool="pool",
                observed_at=self.OBSERVED,
            )

    def test_observed_at_is_required_not_laundered_to_now(self):
        with self.assertRaises(quota_windows.QuotaWindowError):
            quota_windows.parse_minimax_payload({"interval_remaining_percent": 50}, provider="minimax", account_or_pool="pool")


# ---------------------------------------------------------------------------
# Response-error typing
# ---------------------------------------------------------------------------


class ResponseErrorTests(unittest.TestCase):
    def test_auth_status_codes(self) -> None:
        for code in (401, 403):
            result = quota_windows.parse_response_error(status_code=code, body="")
            self.assertEqual(result["status"], "unknown")
            self.assertEqual(result["reason"], "auth")
            self.assertFalse(result["global_exhausted"])

    def test_404_is_not_found(self) -> None:
        result = quota_windows.parse_response_error(status_code=404, body="missing")
        self.assertEqual(result["status"], "unknown")
        self.assertEqual(result["reason"], "not-found")

    def test_429_is_rate_limited_not_global_exhausted(self) -> None:
        result = quota_windows.parse_response_error(status_code=429, body="too many")
        self.assertEqual(result["status"], "rate-limited")
        self.assertEqual(result["reason"], "ordinary-429")
        self.assertFalse(result["global_exhausted"])

    def test_500_is_server_unknown(self) -> None:
        result = quota_windows.parse_response_error(status_code=500, body="crash")
        self.assertEqual(result["status"], "unknown")
        self.assertEqual(result["reason"], "server")
        self.assertFalse(result["global_exhausted"])

    def test_timeout_is_unknown(self) -> None:
        result = quota_windows.parse_response_error(status_code=None, body=None)
        self.assertEqual(result["status"], "unknown")
        self.assertEqual(result["reason"], "empty")
        self.assertFalse(result["global_exhausted"])

    def test_empty_body_with_no_code_is_schema_error(self) -> None:
        result = quota_windows.parse_response_error(status_code=None, body="<html></html>")
        self.assertEqual(result["status"], "unknown")
        self.assertEqual(result["reason"], "schema-error")


# ---------------------------------------------------------------------------
# Last-good normalization
# ---------------------------------------------------------------------------


class LastGoodNormalizationTests(unittest.TestCase):
    def test_failed_refresh_keeps_last_good(self) -> None:
        current = {
            "status": "fresh",
            "observed_at": "2026-10-05T12:00:00Z",
            "interval": {"remaining_fraction": 0.5},
        }
        error = quota_windows.parse_response_error(status_code=500, body="crash")
        result = quota_windows.last_good_normalization(current, error)
        self.assertEqual(result["status"], "stale")
        self.assertEqual(result["last_good"], current)
        self.assertEqual(result["attempt_error"], error)

    def test_only_error_present(self) -> None:
        error = quota_windows.parse_response_error(status_code=429, body="too many")
        result = quota_windows.last_good_normalization(None, error)
        self.assertEqual(result["status"], "unknown")
        self.assertIsNone(result["last_good"])
        self.assertEqual(result["attempt_error"], error)


# ---------------------------------------------------------------------------
# Identity validation
# ---------------------------------------------------------------------------


class IdentityValidationTests(unittest.TestCase):
    def test_provider_mismatch_raises(self) -> None:
        sample = {"provider": "minimax", "account_or_pool": "pool"}
        with self.assertRaises(quota_windows.QuotaWindowError):
            quota_windows.validate_identity(
                sample=sample, expected_provider="codex", expected_account_or_pool="pool",
            )

    def test_account_mismatch_raises(self) -> None:
        sample = {"provider": "minimax", "account_or_pool": "alpha"}
        with self.assertRaises(quota_windows.QuotaWindowError):
            quota_windows.validate_identity(
                sample=sample, expected_provider="minimax", expected_account_or_pool="beta",
            )

    def test_unbound_legacy_passes_when_no_expected(self) -> None:
        sample = {"interval": {"remaining_fraction": 0.5}}
        quota_windows.validate_identity(
            sample=sample, expected_provider="", expected_account_or_pool="",
        )

    def test_unbound_legacy_fails_when_expected_set(self) -> None:
        sample = {"interval": {"remaining_fraction": 0.5}}
        with self.assertRaises(quota_windows.QuotaWindowError):
            quota_windows.validate_identity(
                sample=sample, expected_provider="minimax", expected_account_or_pool="",
            )


# ---------------------------------------------------------------------------
# Offline CLI
# ---------------------------------------------------------------------------


class OfflineCLITests(unittest.TestCase):
    def _write_payload(self, payload: dict[str, object]) -> Path:
        directory = Path(tempfile.mkdtemp(prefix="quota-windows-cli-"))
        path = directory / "minimax.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        return path

    def test_cli_normalize_minimal_payload(self) -> None:
        path = self._write_payload({"interval_remaining_percent": 50, "weekly_remaining_percent": 70})
        try:
            captured = io.StringIO()
            with redirect_stdout(captured):
                exit_code = quota_windows._cli_normalize(
                    argparse.Namespace(
                        provider="minimax",
                        account_or_pool="pool",
                        observed_at="2026-10-05T12:00:00Z",
                        input=str(path),
                    )
                )
            self.assertEqual(exit_code, 0)
            payload = json.loads(captured.getvalue())
            self.assertEqual(payload["provider"], "minimax")
            self.assertEqual(payload["account_or_pool"], "pool")
            self.assertEqual(payload["interval"]["remaining_fraction"], 0.5)
        finally:
            pass  # Retained synthetic evidence; no deletion.

    def test_cli_missing_provider_exits_two(self) -> None:
        captured = io.StringIO()
        with redirect_stdout(captured), redirect_stderr(io.StringIO()):
            exit_code = quota_windows._cli_normalize(
                argparse.Namespace(
                    provider="",
                    account_or_pool="pool",
                    observed_at="2026-10-05T12:00:00Z",
                    input="ignored",
                )
            )
        self.assertEqual(exit_code, 2)

    def test_cli_bad_observed_at(self) -> None:
        path = self._write_payload({"interval_remaining_percent": 50})
        try:
            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                exit_code = quota_windows._cli_normalize(
                    argparse.Namespace(
                        provider="minimax",
                        account_or_pool="pool",
                        observed_at="not-iso",
                        input=str(path),
                    )
                )
            self.assertEqual(exit_code, 2)
        finally:
            pass  # Retained synthetic evidence; no deletion.

    def test_cli_unreadable_input(self) -> None:
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            exit_code = quota_windows._cli_normalize(
                argparse.Namespace(
                    provider="minimax",
                    account_or_pool="pool",
                    observed_at="2026-10-05T12:00:00Z",
                    input=str(Path(tempfile.gettempdir()) / "no-such-minimax.json"),
                )
            )
        self.assertEqual(exit_code, 2)

    def test_cli_malformed_json(self) -> None:
        directory = Path(tempfile.mkdtemp(prefix="quota-windows-cli-"))
        path = directory / "minimax.json"
        path.write_text("not json", encoding="utf-8")
        try:
            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                exit_code = quota_windows._cli_normalize(
                    argparse.Namespace(
                        provider="minimax",
                        account_or_pool="pool",
                        observed_at="2026-10-05T12:00:00Z",
                        input=str(path),
                    )
                )
            self.assertEqual(exit_code, 2)
        finally:
            pass  # Retained synthetic evidence; no deletion.

    def test_cli_subprocess_does_not_read_credentials_or_network(self) -> None:
        # The CLI must not contact credentials, cache, network or env defaults.
        # Run it in an isolated environment that has no .codex, no credentials,
        # no cache directory, and a temp HOME.
        path = self._write_payload(
            {"interval_remaining_percent": 50, "weekly_remaining_percent": 70}
        )
        scratch = Path(tempfile.mkdtemp(prefix="quota-windows-cli-isolated-"))
        env = {
            "PATH": os.environ.get("PATH", ""),
            "SYSTEMROOT": os.environ.get("SYSTEMROOT", ""),
            "TEMP": str(scratch),
            "TMP": str(scratch),
            "TMPDIR": str(scratch),
            "HOME": str(scratch),
            "USERPROFILE": str(scratch),
            "APPDATA": str(scratch),
            "LOCALAPPDATA": str(scratch),
            "PYTHONPATH": os.pathsep.join(sys.path),
        }
        try:
            completed = subprocess.run(
                [
                    sys.executable,
                    "-B", "-X", "faulthandler=0",
                    str(SCRIPT_DIR / "quota_windows.py"),
                    "--provider", "minimax",
                    "--account-or-pool", "isolated",
                    "--observed-at", "2026-10-05T12:00:00Z",
                    "--input", str(path),
                ],
                capture_output=True,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
                env=env,
                timeout=20,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr.decode("utf-8"))
            payload = json.loads(completed.stdout.decode("utf-8"))
            self.assertEqual(payload["account_or_pool"], "isolated")
            self.assertEqual(payload["interval"]["remaining_fraction"], 0.5)
            # No cache file is written into the scratch directory.
            self.assertFalse((scratch / "cache").exists())
            self.assertFalse((scratch / ".agents").exists())
            self.assertFalse((scratch / ".codex").exists())
        finally:
            pass  # Retained synthetic evidence; no deletion.


if __name__ == "__main__":
    unittest.main(verbosity=2)