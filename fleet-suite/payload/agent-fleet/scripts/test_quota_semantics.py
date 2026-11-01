"""Synthetic read-only quota and availability contracts; no owner runtime."""
import copy
import datetime as dt
import unittest
from pathlib import Path
from unittest import mock

import quota_semantics as qs

NOW = dt.datetime(2026, 10, 6, 12, tzinfo=dt.timezone.utc)
POOL = "bailian-shared-flat-rate"
PROFILE = {"quota_pool_id": POOL, "quota_collection": {
    "collector_mode": "local-token-ledger", "capacity_source": "inferred-console"}}


def stamp(delta=0):
    return (NOW + dt.timedelta(seconds=delta)).isoformat().replace("+00:00", "Z")


def proof(**changes):
    return {"schema_version": 1, "status": "available", "source": "native-run",
            "observed_at": stamp(-60), "expires_at": stamp(86340), **changes}


def window(remaining=0, reset=3600, **changes):
    return {"scope": "weekly", "remaining_fraction": remaining,
            "reset_at": stamp(reset) if reset is not None else None, **changes}


def numeric(**changes):
    return {"status": "fresh", "source": "provider-dashboard",
            "quota_pool_id": POOL, "authority": "authoritative",
            "observed_at": stamp(-120), "headroom": 0,
            "reported_headroom": 0, "hard_blocking": True, **changes}


class QuotaSemanticsTests(unittest.TestCase):
    def annotate(self, row, **kwargs):
        before = copy.deepcopy(row)
        result = qs.annotate_view("qwen", row, PROFILE, now=NOW, **kwargs)
        self.assertEqual(row, before, "annotation must not mutate its inputs")
        return result

    def test_ledger_at_and_over_capacity_never_exhausts(self):
        for fraction in (0, -0.2):
            with self.subTest(fraction=fraction):
                result = self.annotate(numeric(source="local-token-ledger",
                    remaining_tokens=0, weekly_remaining_fraction=fraction,
                    weekly_reset_at=stamp(3600), windows=[window()]))
                self.assertIsNone(result["headroom"])
                self.assertIsNone(result["weekly_remaining_fraction"])
                self.assertEqual(result["estimated_weekly_remaining_fraction"], fraction)
                self.assertEqual(result["authority"], "advisory")
                self.assertFalse(result["hard_blocking"])
                self.assertFalse(result["availability_blocked"])

    def test_native_success_proves_availability_without_numeric_allowance(self):
        result = self.annotate({"status": "unknown", "headroom": None,
                               "availability_state": proof()})
        self.assertEqual(result["availability_status"], "available")
        self.assertTrue(result["availability_verified"])
        self.assertIsNone(result["headroom"])
        self.assertEqual(result["status"], "unknown")
        self.assertFalse(result["current"])
        self.assertIsNotNone(result["blocker"])

    def test_newer_ledger_does_not_veto_native_success(self):
        result = self.annotate(numeric(source="local-token-ledger",
                    observed_at=stamp(-5), availability_state=proof()))
        self.assertTrue(result["availability_verified"])
        self.assertFalse(result["availability_blocked"])

    def test_invalid_or_expired_proofs_are_unknown(self):
        cases = [proof(observed_at=stamp(-86400)), proof(observed_at=stamp(60)),
                 proof(observed_at=None), proof(source="untrusted"),
                 proof(expires_at=stamp(-1)), proof(expires_at="invalid"),
                 proof(quota_pool_id="another-pool"), proof(pool_id="another-pool"),
                 proof(function="code_review"), proof(model="other-model")]
        for state in cases:
            with self.subTest(state=state):
                result = self.annotate({"status": "unknown", "availability_state": state})
                self.assertEqual(result["availability_status"], "unknown")
                self.assertFalse(result["availability_verified"])
                self.assertFalse(result["availability_blocked"])

    def test_kimi_native_usage_obeys_cache_expiry(self):
        for seconds, expected in ((300, "available"), (-1, "unknown")):
            state = proof(source="kimi-native-usage", expires_at=stamp(seconds))
            result = self.annotate({"status": "unknown", "availability_state": state})
            self.assertEqual(result["availability_status"], expected)
            self.assertEqual(result["availability_verified"], expected == "available")
            self.assertIsNone(result.get("weekly_remaining_fraction"))

    def test_native_refusal_survives_ledger_and_reset_only_requests_recheck(self):
        for seconds, expected in ((300, "exhausted"), (-1, "recheck-required")):
            state = proof(status="exhausted", source="native-provider-error",
                evidence_source="local-token-ledger", reset_at=stamp(seconds),
                recheck_at=stamp(seconds))
            result = self.annotate(numeric(source="local-token-ledger",
                                           availability_state=state))
            self.assertEqual(result["availability_status"], expected)
            self.assertTrue(result["availability_blocked"])
            self.assertFalse(result["availability_verified"])
            if seconds < 0:
                self.assertIsNone(result["blocking_reset_at"])
                self.assertIsNone(result["availability"]["reset_at"])

    def test_newer_authoritative_exhaustion_beats_success(self):
        result = self.annotate(numeric(observed_at=stamp(-5),
            weekly_remaining_fraction=0, weekly_reset_at=stamp(3600),
            availability_state=proof()))
        self.assertEqual(result["availability_status"], "exhausted")
        self.assertTrue(result["availability_blocked"])
        self.assertFalse(result["availability_verified"])

    def test_wrong_pool_numeric_evidence_does_not_block(self):
        result = self.annotate(numeric(quota_pool_id="different"))
        self.assertFalse(result["availability_blocked"])
        self.assertFalse(result["hard_blocking"])
        self.assertIsNone(result["headroom"])

    def test_later_reset_of_all_blocking_windows_is_selected(self):
        result = self.annotate(numeric(short_window_remaining_fraction=0,
            short_window_reset_at=stamp(1200), weekly_remaining_fraction=0,
            weekly_reset_at=stamp(259200)))
        self.assertEqual(result["blocking_reset_at"], stamp(259200))
        self.assertEqual(result["availability"]["reset_at"], stamp(259200))

    def test_unknown_blocking_reset_never_promises_short_reset(self):
        result = self.annotate(numeric(short_window_remaining_fraction=0,
            short_window_reset_at=stamp(1200), weekly_remaining_fraction=0))
        self.assertEqual(result["availability_status"], "exhausted")
        self.assertIsNone(result["blocking_reset_at"])
        self.assertIsNone(result["availability"]["reset_at"])

    def test_elapsed_numeric_reset_is_recheck_not_automatic_available(self):
        result = self.annotate(numeric(weekly_remaining_fraction=0,
                                      weekly_reset_at=stamp(-1)))
        self.assertEqual(result["availability_status"], "recheck-required")
        self.assertTrue(result["availability_blocked"])
        self.assertIsNone(result["blocking_reset_at"])

    def test_unrelated_function_and_model_windows_do_not_block_coding(self):
        cases = [[window(function="code_review"), window(0.5, function="coding")],
                 [window(model="other"), window(0.5, model="requested")],
                 [window(quota_pool_id="other")]]
        for windows in cases:
            with self.subTest(windows=windows):
                result = self.annotate(numeric(windows=windows), model="requested")
                self.assertFalse(result["availability_blocked"])
                self.assertFalse(result["hard_blocking"])
                self.assertIsNone(result["blocking_reset_at"])

    def test_scoped_exhaustion_blocks_the_requested_function_only(self):
        row = numeric(windows=[window(function="code_review")])
        result = self.annotate(row, function="code_review")
        self.assertTrue(result["availability_blocked"])
        self.assertEqual(result["blocking_reset_at"], stamp(3600))

    def test_cached_scoped_windows_do_not_block_unrelated_function(self):
        state = proof(status="exhausted", source="kimi-native-usage",
            windows=[window(function="code_review")], recheck_at=stamp(300))
        result = self.annotate({"status": "unknown", "availability_state": state})
        self.assertFalse(result["availability_blocked"])

    def test_invalid_fractions_never_create_exhaustion(self):
        for value in (True, float("nan"), float("inf"), float("-inf"), "0"):
            with self.subTest(value=value):
                result = qs.annotate_view("qwen", numeric(windows=[window(value)]),
                                          PROFILE, now=NOW)
                self.assertFalse(result["availability_blocked"])
                self.assertFalse(result["hard_blocking"])

    def test_stale_or_expired_numeric_evidence_cannot_overrule_new_success(self):
        for changes in ({"status": "stale"}, {"effective_expires_at": stamp(-1)},
                        {"observed_at": stamp(60)}, {"observed_at": "bad"}):
            result = self.annotate(numeric(**changes, windows=[window()],
                                          availability_state=proof()))
            self.assertTrue(result["availability_verified"])
            self.assertFalse(result["availability_blocked"])

    def test_last_good_and_failed_attempt_dates_are_preserved(self):
        fields = {"last_good": {"observed_at": stamp(-120), "headroom": 0.7},
                  "attempt_error": {"reason": "server", "status": "unknown"},
                  "last_attempt_at": stamp(-5)}
        result = self.annotate({"status": "stale", "observed_at": stamp(-120), **fields})
        for key, value in fields.items():
            self.assertEqual(result[key], value)
        self.assertEqual(result["observed_at"], stamp(-120))

    def test_snapshot_binds_pool_and_reads_without_probe_or_write(self):
        root = Path("synthetic-only-root")
        rows = {"qwen": {"status": "unknown", "headroom": None}}
        with mock.patch.object(qs.adaptive, "latest_quotas", autospec=True, return_value=rows) as latest, \
             mock.patch.object(qs.quota_availability, "read_cache", return_value=proof()) as cache, \
             mock.patch.object(qs.quota_availability, "check", side_effect=AssertionError("probe")), \
             mock.patch.object(qs.adaptive, "atomic_write", side_effect=AssertionError("write")):
            result = qs.read_snapshot(root, {"qwen": PROFILE}, now=NOW)
        self.assertEqual(latest.call_args.kwargs["pool_ids"], {"qwen": POOL})
        self.assertEqual(cache.call_args.args[0], qs.quota_availability.cache_path(root, POOL))
        self.assertTrue(result["qwen"]["availability_verified"])

    def test_corrupt_cache_returns_sanitized_unknown(self):
        with mock.patch.object(qs.quota_availability, "read_cache",
                               side_effect=ValueError("private path and payload")):
            self.assertEqual(qs.read_availability_state(Path("synthetic"), POOL), {})

    def test_corrupt_store_does_not_disclose_exception_or_invent_quota(self):
        with mock.patch.object(qs.adaptive, "latest_quotas", autospec=True,
                               side_effect=ValueError("private payload")), \
             mock.patch.object(qs, "read_availability_state", return_value=proof()):
            row = qs.read_snapshot(Path("synthetic"), {"qwen": PROFILE}, now=NOW)["qwen"]
        self.assertEqual(row["status"], "unknown")
        self.assertIsNone(row["headroom"])
        self.assertEqual(row["quota_read_error"], "quota-store-unreadable")
        self.assertNotIn("private payload", str(row))
        self.assertTrue(row["availability_verified"])

    def test_cached_last_good_error_and_attempt_date_are_preserved(self):
        fields = {"last_good": {"observed_at": stamp(-600)},
                  "attempt_error": {"reason": "timeout"}, "last_attempt_at": stamp(-5)}
        state = proof(status="recheck-required", source="availability-check", **fields)
        result = self.annotate({"status": "unknown", "availability_state": state})
        self.assertTrue(result["availability_blocked"])
        self.assertIsNone(result["observed_at"])
        for key, value in fields.items():
            self.assertEqual(result[key], value)
            self.assertEqual(result["availability"][key], value)

    def test_unknown_cached_window_reset_cannot_use_top_level_reset(self):
        state = proof(status="exhausted", source="kimi-native-usage",
            recheck_at=stamp(300), reset_at=stamp(1200),
            windows=[window(reset=1200), window(reset=None, scope="monthly")])
        result = self.annotate({"status": "unknown", "availability_state": state})
        self.assertTrue(result["availability_blocked"])
        self.assertIsNone(result["blocking_reset_at"])
        self.assertIsNone(result["availability"]["reset_at"])

    def test_kimi_cache_expiry_retains_refusal_as_recheck(self):
        state = proof(status="exhausted", source="kimi-native-usage",
            expires_at=stamp(-1), recheck_at=stamp(300), windows=[window()])
        result = self.annotate({"status": "unknown", "availability_state": state})
        self.assertEqual(result["availability_status"], "recheck-required")
        self.assertTrue(result["availability_blocked"])

    def test_expired_scoped_window_is_not_current_numeric_exhaustion(self):
        result = self.annotate(numeric(windows=[window(expires_at=stamp(-1))],
                                      availability_state=proof()))
        self.assertTrue(result["availability_verified"])
        self.assertFalse(result["availability_blocked"])
        self.assertFalse(result["hard_blocking"])
        self.assertIsNone(result["headroom"])

    def test_unknown_failed_attempts_are_not_exhaustion(self):
        for reason in ("auth-401", "auth-403", "not-found-404", "timeout", "server-500", "empty"):
            result = self.annotate({"status": "unknown", "availability_state": {
                "status": "unknown", "source": "availability-check", "reason": reason,
                "observed_at": stamp(-5)}})
            self.assertFalse(result["availability_blocked"])
            self.assertEqual(result["availability_status"], "unknown")
            self.assertIsNone(result.get("headroom"))

    def test_native_unknown_monthly_reset_cannot_be_replaced_by_short_counter(self):
        state = proof(status="exhausted", source="native-provider-error", scope="monthly",
                      reset_at=None, recheck_at=stamp(300))
        result = self.annotate(numeric(observed_at=stamp(-5), availability_state=state,
            short_window_remaining_fraction=0, short_window_reset_at=stamp(1200)))
        self.assertTrue(result["availability_blocked"])
        self.assertIsNone(result["blocking_reset_at"])
        self.assertEqual(result["availability"]["source"], "native-provider-error")
        self.assertEqual(result["availability"]["observed_at"], state["observed_at"])


if __name__ == "__main__":
    unittest.main()
