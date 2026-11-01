"""Synthetic tests for Fleet 0.9.37 quota honesty: Kimi windows, availability
blocks, the context-window estimate, effective run limits and the two owner
policy knobs (tempo WARN/throttle, fail-capped unknown quota).

Evidence for every case is the Qwen 3.8 Max tokenomics run of 2026-09-26
(run-20260926T214101Z-6cabafa4bc). No network, no provider, no real state.
"""

from __future__ import annotations
def _redacted_fixture(kind, ordinal, binary=False):
    """Return a deterministic public synthetic fixture value.

    The result is newly authored public data derived only from the pinned
    rule label and a per-file ordinal. It is never derived from any original
    value. Runtime values retain the original detector category.
    """
    alphabet = 'abcdefghijklmnopqrstuvwxyz0123456789'
    seed = (len(kind) * 131 + int(ordinal) * 7919 + 17) & 0x7FFFFFFF
    chars = []
    for _ in range(40):
        seed = (seed * 1103515245 + 12345) & 0x7FFFFFFF
        chars.append(alphabet[seed % 36])
    body = ''.join(chars)
    if kind == 'vendor-token':
        body = 'gh' + 'p_' + body
    elif kind == 'aws-access-key-id':
        body = 'AK' + 'IA' + body[:16].upper()
    elif kind == 'private-key':
        marker = 'PRIVATE' + ' KEY'
        body = ('-----' + 'BEGIN ' + marker + '-----' + chr(10) + body + chr(10)
                + '-----' + 'END ' + marker + '-----')
    if binary:
        return body.encode('ascii')
    return body



import contextlib
import copy
import datetime as dt
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock


SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

import adaptive  # noqa: E402
import fleet  # noqa: E402
import grok_acp  # noqa: E402
import kimi_usage  # noqa: E402
import openclaw_runtime  # noqa: E402
import quota_availability  # noqa: E402
import quota_pace  # noqa: E402
import routing_v2  # noqa: E402

UTC = dt.timezone.utc


def iso(value: dt.datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def kimi_payload(now: dt.datetime, *, short: dict[str, str] | None = None) -> dict:
    """Legacy /usages answer shaped like the one behind the 2026-09-26 cache.

    Weekly summary 100/100 (exhausted) and a 300-minute window. The service
    serializes proto3 JSON: int64 as strings, enum names, camelCase keys.
    """
    detail = {"limit": "100", "resetTime": iso(now + dt.timedelta(hours=2))}
    detail.update({"used": "17"} if short is None else short)
    return {
        "usage": {
            "limit": "100",
            "used": "100",
            "resetTime": iso(now + dt.timedelta(hours=15)),
        },
        "limits": [
            {
                "window": {"duration": 300, "timeUnit": "TIME_UNIT_MINUTE"},
                "detail": detail,
            }
        ],
    }


class KimiUsageParseTests(unittest.TestCase):
    def setUp(self) -> None:
        self.now = dt.datetime.now(UTC)

    def windows(self, payload: dict) -> dict[str, dict]:
        return {row["scope"]: row for row in kimi_usage.parse_usage(payload)["windows"]}

    def test_real_shape_produces_weekly_and_short_percentages(self) -> None:
        result = kimi_usage.parse_usage(kimi_payload(self.now))
        windows = self.windows(kimi_payload(self.now))
        self.assertEqual(result["status"], "exhausted")
        self.assertEqual(result["scope"], "weekly")
        self.assertEqual(result["source"], "kimi-native-usage")
        self.assertEqual((windows["weekly"]["used"], windows["weekly"]["limit"]), (100.0, 100.0))
        self.assertEqual((windows["short-window"]["used"], windows["short-window"]["limit"]), (17.0, 100.0))
        self.assertEqual(windows["short-window"]["window_hours"], 5.0)

    def test_proto3_omitted_counter_after_reset_is_zero_not_a_crash(self) -> None:
        # Before 0.9.37 raw['used'] raised KeyError here, the native fallback
        # followed and the recheck ended as ValueError with no windows.
        for short in ({}, {"remaining": "100"}):
            with self.subTest(short=short):
                windows = self.windows(kimi_payload(self.now, short=short))
                self.assertEqual(windows["short-window"]["used"], 0.0)
                self.assertEqual(windows["weekly"]["used"], 100.0)
        windows = self.windows(kimi_payload(self.now, short={"remaining": "83"}))
        self.assertEqual(windows["short-window"]["used"], 17.0)

    def test_current_native_ratio_shape_is_read_as_percentages(self) -> None:
        payload = {
            "usages": {
                "limit_5h": {"used_ratio": 0.17, "reset_time": iso(self.now + dt.timedelta(hours=5))},
                "limit_7d": {"used_ratio": "1", "reset_time": iso(self.now + dt.timedelta(days=1))},
                "limit_month_total": {"used_ratio": 0.4},
                "limit_month_code": {"used_ratio": 0.3},
            },
            "boosterWallet": None,
        }
        result = kimi_usage.parse_usage(payload)
        windows = {row["scope"]: row for row in result["windows"]}
        self.assertEqual(result["status"], "exhausted")
        self.assertEqual(result["covered_scopes"], ["monthly", "short-window", "weekly"])
        self.assertEqual(windows["short-window"]["used"], 17.0)
        self.assertEqual(windows["short-window"]["window_hours"], 5.0)
        self.assertEqual(windows["weekly"]["used"], 100.0)
        self.assertEqual(windows["monthly"]["used"], 40.0)
        self.assertEqual(len(result["windows"]), 3)  # month_code is a breakdown

    def test_both_shapes_merge_only_scopes_the_legacy_answer_lacks(self) -> None:
        payload = kimi_payload(self.now)
        payload["usages"] = {
            "limit_5h": {"used_ratio": 0.99},
            "limit_month_total": {"used_ratio": 0.25},
        }
        windows = self.windows(payload)
        self.assertEqual(windows["short-window"]["used"], 17.0)
        self.assertEqual(windows["monthly"]["used"], 25.0)

    def test_unknown_stays_unknown(self) -> None:
        bad_payloads = (
            {},
            [],
            {"usages": {}},
            {"usages": {"limit_7d": {"reset_time": "x"}}},
            {"usage": {"used": "5"}},
            {"usage": {"used": "NaN", "limit": "100"}},
            {"usage": {"used": None, "limit": "100"}},
            {"usage": {"used": "-1", "limit": "100"}},
            {"usage": {"used": "5", "limit": "100"}, "limits": [{"window": {}}]},
            {"usage": {"used": "5", "limit": "100"}, "limits": "garbage"},
        )
        for payload in bad_payloads:
            with self.subTest(payload=payload):
                with self.assertRaises(ValueError):
                    kimi_usage.parse_usage(payload)


class KimiQueryAndFallbackTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name) / "kimi-home"
        (self.home / "credentials").mkdir(parents=True)
        (self.home / "config.toml").write_text(
            '[providers."managed:kimi-code"]\nbase_url = "https://api.kimi.com/coding/v1"\n',
            encoding="utf-8",
        )
        self.now = dt.datetime.now(UTC)

    def write_token(self, expires_at: float) -> None:
        (self.home / "credentials" / "kimi-code.json").write_text(
            json.dumps({"access_token": "synthetic-token", "expires_at": expires_at,
                        "expires_in": 900}),
            encoding="utf-8",
        )

    def test_expired_token_is_never_sent(self) -> None:
        self.write_token(time.time() - 5)
        opener = mock.Mock()
        opener.open.side_effect = AssertionError("network")
        with mock.patch.dict(os.environ, {"KIMI_CODE_BASE_URL": ""}):
            with self.assertRaisesRegex(ValueError, "expired"):
                kimi_usage.query(self.home, opener=opener)
        opener.open.assert_not_called()

    def test_live_token_reads_the_windows(self) -> None:
        self.write_token(time.time() + 600)
        response = mock.MagicMock()
        response.__enter__.return_value.read.return_value = json.dumps(
            kimi_payload(self.now)).encode("utf-8")
        opener = mock.Mock()
        opener.open.return_value = response
        with mock.patch.dict(os.environ, {"KIMI_CODE_BASE_URL": ""}):
            result = kimi_usage.query(self.home, opener=opener)
        self.assertEqual(result["status"], "exhausted")
        request = opener.open.call_args.args[0]
        self.assertEqual(request.full_url, "https://api.kimi.com/coding/v1/usages")

    def test_native_fallback_rereads_usage_after_cli_refresh(self) -> None:
        usage = kimi_usage.parse_usage(kimi_payload(self.now))
        with (
            mock.patch.object(kimi_usage, "native_probe",
                              side_effect=ValueError("Native Kimi availability check did not succeed")),
            mock.patch.object(kimi_usage, "query", return_value=usage) as query,
        ):
            result = kimi_usage.native_fallback(fleet, {}, Path(self.temp.name))
        query.assert_called_once()
        self.assertEqual(result["source"], "kimi-native-usage")
        self.assertEqual(len(result["windows"]), 2)

    def test_native_fallback_never_lets_usage_override_native(self) -> None:
        available = kimi_usage.parse_usage(kimi_payload(self.now, short={"used": "1"}) | {
            "usage": {"limit": "100", "used": "3"}})
        self.assertEqual(available["status"], "available")
        native_exhausted = {"status": "exhausted", "scope": "weekly",
                            "source": "native-provider-error", "reason": "usage-limit"}
        native_available = {"status": "available", "source": "native-cli-check",
                            "covered_scopes": ["daily", "monthly", "short-window", "weekly"]}
        with (
            mock.patch.object(kimi_usage, "native_probe", return_value=native_exhausted),
            mock.patch.object(kimi_usage, "query", return_value=dict(available)),
        ):
            kept = kimi_usage.native_fallback(fleet, {}, Path(self.temp.name))
        # The refusal stands; the contradicting weekly counter may not clear it.
        self.assertEqual({k: kept[k] for k in native_exhausted}, native_exhausted)
        self.assertEqual(kept["clear_requires"], "native-cli-check")
        with (
            mock.patch.object(kimi_usage, "native_probe", return_value=native_available),
            mock.patch.object(kimi_usage, "query", return_value=dict(available)),
        ):
            merged = kimi_usage.native_fallback(fleet, {}, Path(self.temp.name))
        # The native inference stays the proof; /usages only lends its windows.
        self.assertEqual(merged["source"], "native-cli-check")
        self.assertEqual(merged["windows"], available["windows"])
        self.assertNotIn("windows", native_available)
        self.assertEqual(merged["covered_scopes"], ["daily", "monthly", "short-window", "weekly"])
        exhausted = kimi_usage.parse_usage(kimi_payload(self.now))
        native_monthly = dict(native_exhausted, scope="monthly")
        with (
            mock.patch.object(kimi_usage, "native_probe", return_value=native_monthly),
            mock.patch.object(kimi_usage, "query", return_value=dict(exhausted)),
        ):
            both = kimi_usage.native_fallback(fleet, {}, Path(self.temp.name))
        # Windows kept, but the refusal's uncovered monthly scope wins.
        self.assertEqual((both["status"], both["scope"]), ("exhausted", "monthly"))
        self.assertEqual(len(both["windows"]), 2)
        with (
            mock.patch.object(kimi_usage, "native_probe", side_effect=OSError("no cli")),
            mock.patch.object(kimi_usage, "query") as query,
        ):
            with self.assertRaises(OSError):
                kimi_usage.native_fallback(fleet, {}, Path(self.temp.name))
        query.assert_not_called()
        with (
            mock.patch.object(kimi_usage, "native_probe", side_effect=ValueError("native")),
            mock.patch.object(kimi_usage, "query", side_effect=ValueError("usage")),
        ):
            with self.assertRaisesRegex(ValueError, "native"):
                kimi_usage.native_fallback(fleet, {}, Path(self.temp.name))

    def test_native_success_clears_an_exhaustion_of_any_scope(self) -> None:
        """Review of 0.9.37: the /usages re-read must not demote a native proof.

        check() writes scope `unknown` when an authoritative snapshot reports
        headroom <= 0; a CodeBuddy-style refusal keeps scope `credits`. Neither
        is a /usages window, so only the native-cli-check source can clear it.
        The re-read windows still reach the view as pace evidence, and one
        billed native probe is enough.
        """
        usage = kimi_usage.parse_usage(kimi_payload(self.now, short={"used": "1"}) | {
            "usage": {"limit": "100", "used": "3"}})
        self.assertEqual(usage["covered_scopes"], ["short-window", "weekly"])
        native_available = {"status": "available", "source": "native-cli-check",
                            "covered_scopes": ["daily", "monthly", "short-window", "weekly"],
                            "probe_evidence": "assistant-text"}
        now = time.time()
        for scope in ("unknown", "credits"):
            with self.subTest(scope=scope):
                root = Path(self.temp.name) / f"state-{scope}"
                prior = {"status": "unknown", "headroom": None, "confidence": 0.0}
                if scope == "unknown":
                    prior = {"status": "fresh", "authority": "authoritative",
                             "reported_headroom": 0.0, "headroom": 0.0,
                             "observed_at": quota_availability.stamp(now - 120)}
                else:
                    path = quota_availability.cache_path(root, "kimi")
                    path.parent.mkdir(parents=True)
                    path.write_text(json.dumps({
                        "schema_version": 1, "status": "exhausted", "scope": "credits",
                        "source": "native-provider-error", "reason": "usage-credits-exhausted",
                        "observed_at": quota_availability.stamp(now - 600),
                        "recheck_at": quota_availability.stamp(now - 300),
                    }), encoding="utf-8")
                with (
                    mock.patch.object(kimi_usage, "native_probe",
                                      side_effect=lambda *a, **k: copy.deepcopy(native_available)) as native,
                    mock.patch.object(kimi_usage, "query",
                                      side_effect=lambda *a, **k: copy.deepcopy(usage)),
                ):
                    for offset in (0, 60, 120):
                        state = quota_availability.check(
                            root, "kimi", prior, lock_factory=fleet.ReleasableKernelLock,
                            now=now + offset,
                            probe=lambda: kimi_usage.query(),
                            fallback=lambda: kimi_usage.native_fallback(fleet, {}, root))
                self.assertEqual(native.call_count, 1)
                self.assertEqual(state["status"], "available")
                self.assertEqual(state["source"], "native-cli-check")
                self.assertNotIn("error_type", state)
                view = quota_availability.overlay(prior, state, require_check=True)
                self.assertFalse(view["availability_blocked"])
                fleet._apply_kimi_native_windows(view, state)
                self.assertEqual(view["weekly_remaining_fraction"], 0.97)
                self.assertEqual(view["short_window_remaining_fraction"], 0.99)
                self.assertEqual(view["source"], "kimi-native-usage")

    def test_evidence_chain_recovers_the_percentage_signal(self) -> None:
        """2026-09-26: cache kept 100/100 + 17/100 behind recheck-required/ValueError."""
        root = Path(self.temp.name) / "state"
        path = quota_availability.cache_path(root, "kimi")
        path.parent.mkdir(parents=True)
        stale = self.now - dt.timedelta(hours=1)
        path.write_text(json.dumps({
            "schema_version": 1, "status": "recheck-required", "source": "kimi-native-usage",
            "scope": "weekly", "reason": "check-unavailable", "error_type": "ValueError",
            "covered_scopes": ["short-window", "weekly"],
            "observed_at": iso(stale), "recheck_at": iso(stale),
            "reset_at": iso(self.now + dt.timedelta(hours=15)),
            "windows": [
                {"limit": 100.0, "used": 100.0, "scope": "weekly",
                 "reset_at": iso(self.now + dt.timedelta(hours=15))},
                {"limit": 100.0, "used": 17.0, "scope": "short-window",
                 "reset_at": iso(stale)},
            ],
        }), encoding="utf-8")
        usage = kimi_usage.parse_usage(kimi_payload(self.now))
        profiles = {"kimi": {"adapter": "kimi", "quota_pool_id": "kimi"}}
        with (
            mock.patch.object(adaptive, "latest_quotas", return_value={}),
            mock.patch.object(fleet.quota_collectors, "collect_profile_quotas", return_value=({}, {})),
            mock.patch.object(fleet, "read_usage", return_value=[]),
            mock.patch.object(fleet, "_write_pace_latest", return_value=True),
            mock.patch.object(kimi_usage, "query",
                              side_effect=[ValueError("Kimi native access token expired"), usage]) as query,
            mock.patch.object(kimi_usage, "native_probe",
                              side_effect=ValueError("Native Kimi availability check did not succeed")),
        ):
            views, diagnostics = fleet.current_quota_views(root, profiles)
        self.assertEqual(query.call_count, 2)
        state = diagnostics["kimi"]["availability"]
        self.assertEqual(state["status"], "exhausted")
        self.assertNotIn("error_type", state)
        self.assertGreater(quota_availability.seconds(state["observed_at"]),
                           quota_availability.seconds(iso(stale)))
        view = views["kimi"]
        self.assertTrue(view["availability_blocked"])
        self.assertEqual(view["weekly_remaining_fraction"], 0.0)
        self.assertEqual(view["short_window_remaining_fraction"], 0.83)
        self.assertEqual(view["short_window_hours"], 5.0)
        self.assertEqual(view["source"], "kimi-native-usage")


def blocked_view(reason: str, status: str = "recheck-required") -> dict:
    return {
        "status": "unknown", "headroom": None, "confidence": 0.0,
        "availability_blocked": True,
        "availability": {"status": status, "reason": reason, "scope": "credits",
                         "source": "native-provider-error"},
    }


class AvailabilityBlockedRoutingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.profiles = fleet.load_profiles(fleet.DEFAULT_PROFILES)

    def test_readiness_is_blocked_with_the_named_refusal(self) -> None:
        readiness = {"grok": {"ready": True, "reasons": []},
                     "codex": {"ready": True, "reasons": []}}
        quotas = {"grok": blocked_view("grok-build-balance-exhausted-http402"),
                  "codex": {"status": "fresh", "headroom": 0.9, "confidence": 1.0}}
        fleet.block_unavailable_readiness(readiness, quotas)
        self.assertFalse(readiness["grok"]["ready"])
        self.assertIn("availability-blocked=grok-build-balance-exhausted-http402",
                      readiness["grok"]["reasons"])
        self.assertTrue(readiness["codex"]["ready"])

    def test_quota_gate_evidence_names_the_refusal(self) -> None:
        gates = routing_v2.quota_hard_gates(
            self.profiles["codebuddy"], blocked_view("plan-credits-exhausted", "exhausted"))
        gate = next(g for g in gates if g["code"] == "quota-not-exhausted")
        self.assertFalse(gate["passed"])
        self.assertIn("availability_blocked=exhausted:plan-credits-exhausted", gate["evidence"])

    def route(self, *, codex_ready: bool) -> dict:
        profiles = {name: self.profiles[name] for name in ("grok", "codex")}
        readiness = {
            "grok": {"ready": True, "reasons": []},
            "codex": {"ready": codex_ready, "reasons": [] if codex_ready else ["synthetic-missing"]},
        }
        quotas = {
            "grok": blocked_view("grok-build-balance-exhausted-http402"),
            "codex": {"status": "unknown", "headroom": None, "confidence": 0.0},
        }
        with (
            mock.patch.dict(os.environ, {"AGENT_FLEET_ROUTING_MODE": "baseline"}),
            mock.patch.object(fleet, "require_fresh_model_audit",
                              return_value={"status": "FRESH", "fresh": True, "routing_status": "OK"}),
            mock.patch.object(fleet, "live_readiness",
                              return_value=(copy.deepcopy(readiness), {})),
            mock.patch.object(fleet, "read_usage", return_value=[]),
            mock.patch.object(fleet, "current_quota_views", return_value=(quotas, {})),
            mock.patch.object(fleet, "load_fresh_model_benchmark", return_value=None),
            mock.patch.object(fleet, "read_claude_billing_policy", return_value={}),
            mock.patch.object(fleet, "apply_billing_last_resort_order",
                              side_effect=lambda ranked, policy: ranked),
        ):
            return fleet.route_task(
                "Review one synthetic file.", profiles=profiles, kind="review",
                complexity="low", root=Path.cwd(), cwd=Path.cwd(), allow_no_eligible=True)

    def test_baseline_fallback_never_launches_a_refused_account(self) -> None:
        # Before 0.9.37 grok stayed `launchable` and the --allow-no-eligible
        # baseline fallback selected it despite availability_blocked.
        with self.assertRaisesRegex(fleet.FleetError, "No launchable agent"):
            self.route(codex_ready=False)
        route = self.route(codex_ready=True)
        self.assertEqual(route["selected_agent"], "codex")
        grok = next(row for row in route["rankings"] if row["agent"] == "grok")
        self.assertFalse(grok["launchable"])
        self.assertFalse(grok["eligible"])
        self.assertIn("availability-blocked=grok-build-balance-exhausted-http402",
                      route["readiness"]["grok"]["reasons"])

    def test_adaptive_ranking_fails_runtime_readiness_with_reason(self) -> None:
        profiles = {name: self.profiles[name] for name in ("grok", "codex")}
        readiness = {"grok": {"ready": True, "reasons": []},
                     "codex": {"ready": True, "reasons": []}}
        quotas = {"grok": blocked_view("grok-build-balance-exhausted-http402"),
                  "codex": {"status": "fresh", "headroom": 0.9, "confidence": 1.0}}
        fleet.block_unavailable_readiness(readiness, quotas)
        assessment = adaptive.normalize_task_profile(adaptive.fallback_assessment(
            kind="review", complexity="low", profiles=profiles))
        with mock.patch.dict(os.environ, {"AGENT_FLEET_ROUTING_MODE": "adaptive"}):
            ranked = fleet.rank_profiles(
                profiles, kind="review", complexity="low", counts={},
                availability={name: row["ready"] for name, row in readiness.items()},
                assessment=assessment, quotas=quotas, readiness=readiness, mode="review")
        grok = next(row for row in ranked if row["agent"] == "grok")
        self.assertFalse(grok["eligible"])
        self.assertFalse(grok["launchable"])
        gate = next(g for g in grok["gate_results"] if g["code"] == "runtime-readiness")
        self.assertFalse(gate["passed"])
        self.assertIn("availability-blocked=grok-build-balance-exhausted-http402", gate["evidence"])


class ContextWindowGateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.profiles = fleet.load_profiles(fleet.DEFAULT_PROFILES)
        self.execution = routing_v2.resolve_execution_profile(
            "codex", self.profiles["codex"], complexity="low", mode="review", required_tools=[])

    def gate(self, task: str, window, *, assessment_tokens=None) -> dict:
        assessment = adaptive.normalize_task_profile({
            **adaptive.fallback_assessment(kind="review", complexity="low", profiles=self.profiles),
            "context_estimate": routing_v2.task_context_estimate(task),
        })
        if assessment_tokens is not None:
            assessment["context_load"] = {**assessment["context_load"],
                                          "estimated_tokens": assessment_tokens}
        gates = routing_v2.hard_gates(
            {**self.execution, "context_window_tokens": window},
            profile=self.profiles["codex"], assessment=assessment,
            readiness={"ready": True, "reasons": []},
            quota={"status": "fresh", "headroom": 0.9, "confidence": 1.0}, mode="review")
        return next(g for g in gates if g["code"] == "context-window")

    def test_estimator_is_ceil_bytes_over_four(self) -> None:
        self.assertEqual(routing_v2.estimate_context_tokens(0), 0)
        self.assertEqual(routing_v2.estimate_context_tokens(9), 3)
        self.assertIsNone(routing_v2.estimate_context_tokens(None))
        self.assertIsNone(routing_v2.estimate_context_tokens(True))
        estimate = routing_v2.task_context_estimate("zażółć")  # multi-byte counts bytes
        self.assertEqual(estimate, {"method": "utf8-bytes-div-4", "task_bytes": 10,
                                    "estimated_tokens": 3})
        self.assertEqual(routing_v2.context_prompt_byte_limit(1000), 3600)
        self.assertIsNone(routing_v2.context_prompt_byte_limit(None))

    def test_gate_compares_estimate_with_known_window(self) -> None:
        # Before 0.9.37 the evidence was always estimated=None and it passed.
        blocked = self.gate("x" * 8000, 1000)
        self.assertFalse(blocked["passed"])
        self.assertEqual(blocked["evidence"], "estimated=2000;source=utf8-bytes-div-4;window=1000")
        fits = self.gate("x" * 3600, 1000)
        self.assertTrue(fits["passed"])
        self.assertIn("estimated=900", fits["evidence"])

    def test_unknown_window_never_blocks_and_larger_assessment_estimate_wins(self) -> None:
        unknown = self.gate("x" * 8000, None)
        self.assertTrue(unknown["passed"])
        self.assertEqual(unknown["evidence"], "estimated=2000;source=utf8-bytes-div-4;window=None")
        # An assessment counts only when it is the larger estimate (0.9.37 review 4).
        own = self.gate("x" * 400, 1000, assessment_tokens=950)
        self.assertFalse(own["passed"])
        self.assertEqual(own["evidence"], "estimated=950;source=assessment;window=1000")

    def test_route_task_carries_the_estimate(self) -> None:
        profiles = {"codex": self.profiles["codex"]}
        with (
            mock.patch.object(fleet, "require_fresh_model_audit",
                              return_value={"status": "FRESH", "fresh": True, "routing_status": "OK"}),
            mock.patch.object(fleet, "live_readiness",
                              return_value=({"codex": {"ready": True, "reasons": []}}, {})),
            mock.patch.object(fleet, "read_usage", return_value=[]),
            mock.patch.object(fleet, "current_quota_views", return_value=(
                {"codex": {"status": "fresh", "headroom": 0.9, "confidence": 1.0}}, {})),
            mock.patch.object(fleet, "load_fresh_model_benchmark", return_value=None),
            mock.patch.object(fleet, "read_claude_billing_policy", return_value={}),
            mock.patch.object(fleet, "apply_billing_last_resort_order",
                              side_effect=lambda ranked, policy: ranked),
        ):
            route = fleet.route_task("x" * 400, profiles=profiles, kind="review",
                                     complexity="low", root=Path.cwd(), cwd=Path.cwd())
        self.assertEqual(route["task_profile"]["context_estimate"]["estimated_tokens"], 100)
        gate = next(g for g in route["gate_results"] if g["code"] == "context-window")
        self.assertIn("estimated=100;source=utf8-bytes-div-4", gate["evidence"])


QWEN_STREAM = "\n".join([
    json.dumps({"type": "system", "subtype": "init"}),
    json.dumps({"type": "assistant", "message": {"content": [
        {"type": "thinking", "thinking": "plan"},
        {"type": "tool_use", "id": "call-1", "name": "read_file", "input": {}},
    ]}}),
    json.dumps({"type": "assistant", "message": {"content": [
        {"type": "tool_use", "id": "call-1", "name": "read_file", "input": {}},
        {"type": "tool_use", "id": "call-2", "name": "grep", "input": {}},
    ]}}),
    "not json",
    json.dumps({"type": "result", "subtype": "success"}),
])


class DryRunRecordTests(unittest.TestCase):
    """max_prompt_bytes / max_model_tool_calls were null despite --max-tool-calls 256."""

    def execute_qwen(self, root: Path, *, window: int, mode: str = "review", **extra):
        profiles = fleet.load_profiles(fleet.DEFAULT_PROFILES)
        execution = {
            "execution_profile_id": "qwen-quota-gate-fixture",
            "execution_profile_fingerprint": "f" * 64,
            "lifecycle": "ACTIVE",
            "model": "qwen3.8-max",
            "runtime_model_id": "qwen3.8-max",
            "reasoning_level": "medium",
            "supported_modes": ["review"],
            "tools": ["filesystem-read"],
            "context_window_tokens": window,
        }
        readiness = {"ready": True, "reasons": []}
        quota = {"qwen": {"status": "fresh", "headroom": 1.0, "confidence": 1.0}}
        patches = [
            mock.patch.dict(os.environ, {
                "AGENT_FLEET_BILLING_MODE_PATH": str(root / "missing-billing.json"),
                "AGENT_FLEET_DEPTH": "0",
                "AGENT_FLEET_OFFICE": "0",
            }),
            mock.patch("fleet.resolve_executable", return_value="qwen.exe"),
            mock.patch("fleet.probe_command",
                       return_value={"launchable": True, "exit_code": 0, "output": "ok"}),
            mock.patch("fleet.routing_v2.readiness_from_probes", return_value=readiness),
            mock.patch("fleet.merge_credential_readiness", return_value=readiness),
            mock.patch("fleet.routing_v2.runtime_model_visible", return_value=(True, "visible")),
            mock.patch("fleet.current_quota_views", return_value=(quota, {})),
        ]
        with contextlib.ExitStack() as stack:
            for patch in patches:
                stack.enter_context(patch)
            return fleet.execute_single(
                "Review the bounded quota-gate fixture.",
                profiles=profiles, agent_id="qwen", mode=mode, kind="review",
                complexity="medium", model_override=None, workspace=root, timeout=60,
                allow_write=False, native_subagent_limit=0, enable_web=False,
                inherit_api_keys=False, dry_run=True, output_root=root / "evidence",
                root=root, execution_profile=execution, knowledge_mode="none", **extra)

    def test_record_shows_the_limits_fleet_enforced(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            result = self.execute_qwen(root, window=1_000_000)
        self.assertEqual(result["status"], "DRY_RUN")
        command = result["command"]
        flag = int(command[command.index("--max-tool-calls") + 1])
        self.assertEqual(result["max_model_tool_calls"], flag)
        self.assertEqual(result["max_prompt_bytes"], 3_600_000)
        self.assertLessEqual(result["prompt_bytes"], result["max_prompt_bytes"])

    def test_send_gate_refuses_a_prompt_over_the_window(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fits = self.execute_qwen(root, window=1_000_000)
            # Smallest window whose 90% bound is below this real prompt.
            window = fits["prompt_bytes"] // 4
            with self.assertRaisesRegex(fleet.FleetError, f"Context-window gate failed.*{window}-token"):
                self.execute_qwen(root, window=window)

    def test_effective_limits_per_adapter(self) -> None:
        qwen = ["qwen", "--max-tool-calls", "256", "--max-subagent-depth", "1"]
        self.assertEqual(fleet.effective_run_limits(
            "qwen", qwen, complexity="medium", max_prompt_bytes=None,
            max_model_tool_calls=None, context_window_tokens=1_000_000), (3_600_000, 256))
        self.assertEqual(fleet.effective_run_limits(
            "grok", ["grok"], complexity="high", max_prompt_bytes=None,
            max_model_tool_calls=None, context_window_tokens=None),
            (None, grok_acp.GROK_TOOL_LIMITS["high"]))
        self.assertEqual(fleet.effective_run_limits(
            "codex", ["codex"], complexity="low", max_prompt_bytes=24_000,
            max_model_tool_calls=None, context_window_tokens=1_000_000), (24_000, None))
        self.assertEqual(fleet.effective_run_limits(
            "openclaw", ["openclaw"], complexity="low", max_prompt_bytes=None,
            max_model_tool_calls=4, context_window_tokens=None),
            (openclaw_runtime.PINNED_OPENCLAW_MAX_MESSAGE_BYTES, 4))

    def test_observed_tool_calls_from_json_streams(self) -> None:
        self.assertEqual(fleet.observed_model_tool_calls("qwen", QWEN_STREAM), 2)
        codex = "\n".join([
            json.dumps({"type": "item.started", "item": {"id": "i1", "type": "command_execution"}}),
            json.dumps({"type": "item.started", "item": {"id": "i2", "type": "reasoning"}}),
            json.dumps({"type": "item.started", "item": {"id": "i3", "type": "file_change"}}),
            json.dumps({"type": "turn.completed"}),
        ])
        self.assertEqual(fleet.observed_model_tool_calls("codex", codex), 2)
        for adapter in ("claude", "kimi", "minimax", "grok"):
            self.assertIsNone(fleet.observed_model_tool_calls(adapter, QWEN_STREAM))
        # A complete stream without tool calls is a known zero.
        quiet = "\n".join([json.dumps({"type": "system", "subtype": "init"}),
                           json.dumps({"type": "result", "subtype": "success"})])
        self.assertEqual(fleet.observed_model_tool_calls("qwen", quiet), 0)

    def test_incomplete_stream_makes_the_count_unknown(self) -> None:
        # Review of 0.9.37: a cut event line was skipped and the record said
        # 0 (or a lower count) where the real count is unknown.
        truncated_last = QWEN_STREAM + "\n" + (
            '{"type":"assistant","message":{"content":[{"type":"tool_use","id":"call-3",')
        truncated_middle = QWEN_STREAM.replace(
            json.dumps({"type": "result", "subtype": "success"}),
            '{"type":"assistant","message":{"content":[{"type":"tool_use"\n'
            + json.dumps({"type": "result", "subtype": "success"}))
        codex_cut = "\n".join([
            json.dumps({"type": "item.started", "item": {"id": "i1", "type": "command_execution"}}),
            '{"type":"item.started","item":{"id":"i2","type":"file_ch',
        ])
        cases = {
            "qwen-truncated-last": ("qwen", truncated_last),
            "qwen-truncated-middle": ("qwen", truncated_middle),
            "codex-truncated": ("codex", codex_cut),
            "qwen-empty": ("qwen", ""),
            "codex-empty": ("codex", ""),
            "qwen-whitespace": ("qwen", "\n  \n"),
            "codex-no-events": ("codex", "Error: failed to start session\n"),
        }
        for name, (adapter, stdout) in cases.items():
            with self.subTest(case=name):
                self.assertIsNone(fleet.observed_model_tool_calls(adapter, stdout))

    def test_unreadable_event_makes_the_count_unknown_not_a_crash(self) -> None:
        # Tool arguments are model-controlled; json.loads raises RecursionError
        # past ~1000 levels. The count runs after the child finished, so an
        # exception here would lose stdout.txt and the run record.
        deep = "[" * 3000 + "]" * 3000
        qwen = "\n".join([
            QWEN_STREAM,
            '{"type":"assistant","message":{"content":[{"type":"tool_use","id":"t9",'
            '"name":"run_shell_command","input":{"x":' + deep + '}}]}}',
        ])
        codex = "\n".join([
            json.dumps({"type": "item.started", "item": {"id": "i1", "type": "command_execution"}}),
            '{"type":"item.started","item":{"id":"i2","type":"mcp_tool_call","arguments":'
            + deep + '}}',
        ])
        self.assertIsNone(fleet.observed_model_tool_calls("qwen", qwen))
        self.assertIsNone(fleet.observed_model_tool_calls("codex", codex))


class TempoThresholdTests(unittest.TestCase):
    def setUp(self) -> None:
        self.now = dt.datetime(2026, 9, 26, 21, 40, tzinfo=UTC)
        self.brake = {"active": False, "until": None, "reason": "no-ledger-tokens",
                      "allowed_fraction_window": None, "consumed_fraction_window": None,
                      "window_hours": 24, "evaluated_at": iso(self.now)}
        # bailian from the evidence: tempo 1.44, confidence 1.0 (measured).
        self.pace = {"tempo": 1.44, "confidence": 1.0, "source": "quota-snapshot",
                     "window": "monthly", "observed_at": iso(self.now - dt.timedelta(hours=1)),
                     "reset_at": iso(self.now + dt.timedelta(days=20)),
                     "remaining_fraction": 0.64, "elapsed_fraction": 0.25}
        self.config = {"enabled": True, "window_hours": 24, "burst_factor": 1.5,
                       "max_reading_age_hours": 48, "tempo_warn": 1.2, "tempo_throttle": 1.5}

    def apply(self, pace=None, config=None):
        return quota_pace.apply_tempo_thresholds(
            self.brake, self.pace if pace is None else pace,
            self.config if config is None else config, now=self.now)

    def test_without_thresholds_the_brake_is_unchanged(self) -> None:
        config = {key: value for key, value in self.config.items() if not key.startswith("tempo")}
        self.assertIs(self.apply(config=config), self.brake)

    def test_warn_never_blocks(self) -> None:
        result = self.apply()
        self.assertEqual(result["tempo_level"], "warn")
        self.assertFalse(result["active"])
        self.assertEqual(result["reason"], "no-ledger-tokens")

    def test_throttle_activates_a_releasing_brake(self) -> None:
        # Consistent measured pace: consumed 0.4 over elapsed 0.25 is tempo 1.6.
        result = self.apply(pace={**self.pace, "tempo": 1.6, "remaining_fraction": 0.6})
        self.assertEqual(result["tempo_level"], "throttle")
        self.assertTrue(result["active"])
        self.assertEqual(result["reason"], "tempo-exceeds-throttle")
        until = dt.datetime.fromisoformat(result["until"].replace("Z", "+00:00"))
        # Idle, the tempo reaches 1.5 once elapsed = 0.4 / 1.5 of the window.
        observed = self.now - dt.timedelta(hours=1)
        duration = (self.now + dt.timedelta(days=20) - observed) / 0.75
        expected = observed + duration * (0.4 / 1.5 - 0.25)
        self.assertLess(abs((until - expected).total_seconds()), 1.0)
        self.assertGreater(until, self.now)

    def weekly_brake(self, *, now, observed, remaining, elapsed, max_age):
        """Real pace and base brake from one weekly snapshot, no ledger tokens."""
        reset = observed + dt.timedelta(hours=168 * (1 - elapsed))
        snapshot = {"agent": "synthetic", "observed_at": iso(observed), "source": "quota-snapshot",
                    "weekly_remaining_fraction": remaining, "weekly_reset_at": iso(reset)}
        config = dict(self.config, max_reading_age_hours=max_age)
        kwargs = {"snapshots": [snapshot], "runs": [], "pool_ids": {"synthetic": "pool"}, "now": now}
        pace = quota_pace.calculate_pool_pace("pool", **kwargs)
        brake = quota_pace.calculate_pool_brake("pool", config=config, **kwargs)
        return quota_pace.apply_tempo_thresholds(brake, pace, config, now=now)

    def test_measured_throttle_releases_at_its_until(self) -> None:
        """Review of 0.9.37: a frozen observation-time tempo never released.

        Reading at 10% elapsed with 80% remaining is tempo 2.0; idle, the
        tempo falls to 1.5 after 5.6 h, and the brake must end there.
        """
        observed = self.now
        early = self.weekly_brake(now=observed + dt.timedelta(hours=0.5), observed=observed,
                                  remaining=0.8, elapsed=0.1, max_age=6)
        self.assertTrue(early["active"])
        self.assertEqual(early["tempo_level"], "throttle")
        until = dt.datetime.fromisoformat(early["until"].replace("Z", "+00:00"))
        self.assertLess(abs((until - (observed + dt.timedelta(hours=5.6))).total_seconds()), 1.0)
        later = self.weekly_brake(now=until + dt.timedelta(minutes=1), observed=observed,
                                  remaining=0.8, elapsed=0.1, max_age=6)
        self.assertFalse(later["active"])
        self.assertEqual(later["reason"], "no-ledger-tokens")
        self.assertEqual(later["tempo_level"], "warn")
        self.assertIsNone(later["until"])

    def test_reading_older_than_max_age_never_brakes(self) -> None:
        # Tempo 3.0 would still be 2.1 idle after 7 h, but the owner's
        # max_reading_age_hours=6 forbids braking on that reading.
        observed = self.now
        fresh = self.weekly_brake(now=observed + dt.timedelta(hours=5), observed=observed,
                                  remaining=0.7, elapsed=0.1, max_age=6)
        self.assertTrue(fresh["active"])
        stale = self.weekly_brake(now=observed + dt.timedelta(hours=7), observed=observed,
                                  remaining=0.7, elapsed=0.1, max_age=6)
        self.assertFalse(stale["active"])
        self.assertEqual(stale["reason"], "stale-reading")
        self.assertEqual(stale["tempo_level"], "stale")
        self.assertIsNone(stale["until"])

    def test_unknown_or_low_confidence_tempo_never_brakes(self) -> None:
        for pace in ({"tempo": None, "confidence": 0.0}, {**self.pace, "tempo": 9.0, "confidence": 0.3}):
            with self.subTest(pace=pace):
                result = self.apply(pace=pace)
                self.assertEqual(result["tempo_level"], "unknown")
                self.assertFalse(result["active"])

    def test_current_views_wire_thresholds_into_the_gate(self) -> None:
        profile = {"adapter": "qwen", "quota_pool_id": "synthetic-pool",
                   "quota_collection": {"pace_brake": self.config}}
        with (
            mock.patch.object(adaptive, "latest_quotas", return_value={}),
            mock.patch.object(adaptive, "read_quota_records", return_value=[]),
            mock.patch.object(fleet.quota_collectors, "collect_profile_quotas", return_value=({}, {})),
            mock.patch.object(fleet, "read_usage", return_value=[]),
            mock.patch.object(fleet, "_write_pace_latest", return_value=True),
            mock.patch.object(fleet.quota_pace, "calculate_pool_pace",
                              return_value={**self.pace, "tempo": 1.6}),
            mock.patch.object(fleet.quota_pace, "calculate_pool_brake", return_value=dict(self.brake)),
        ):
            views, _ = fleet.current_quota_views(
                Path("synthetic-state"), {"synthetic": profile}, now=self.now)
        brake = views["synthetic"]["pace_brake"]
        self.assertTrue(brake["active"])
        gate = next(g for g in routing_v2.quota_hard_gates(profile, views["synthetic"])
                    if g["code"] == "pace-brake-clear")
        self.assertFalse(gate["passed"])
        self.assertIn("reason=tempo-exceeds-throttle", gate["evidence"])


class FailCappedPolicyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.profiles = fleet.load_profiles(fleet.DEFAULT_PROFILES)
        self.unknown = {"status": "unknown", "headroom": None, "confidence": 0.0}

    def capped_profile(self) -> dict:
        profile = copy.deepcopy(self.profiles["codex"])
        profile.setdefault("quota_collection", {})["unknown_quota_mode"] = "fail-capped"
        return profile

    def test_defaults_unchanged_except_explicit_qwen_unknown_cap(self) -> None:
        for agent_id, profile in self.profiles.items():
            collection = profile.get("quota_collection") or {}
            brake = collection.get("pace_brake") or {}
            self.assertFalse({"tempo_warn", "tempo_throttle"} & set(brake), agent_id)
            if agent_id == "qwen":
                self.assertEqual(collection["unknown_quota_mode"], "fail-capped")
                self.assertFalse(collection["require_fresh_snapshot"])
                for mode in ("advisory", "review", "implement"):
                    gates = {g["code"]: g["passed"] for g in routing_v2.quota_hard_gates(
                        profile, self.unknown, mode=mode)}
                    self.assertTrue(gates["quota-not-exhausted"])
                    self.assertEqual(gates["quota-unknown-capped"], mode != "implement")
                continue
            if agent_id == "mistral":
                self.assertEqual(collection["unknown_quota_mode"], "fail-open", agent_id)
            else:
                self.assertNotIn("unknown_quota_mode", collection, agent_id)
            for mode in ("advisory", "review", "implement"):
                codes = {g["code"] for g in routing_v2.quota_hard_gates(
                    profile, self.unknown, mode=mode)}
                self.assertNotIn("quota-unknown-capped", codes)
                # By default the mode never changes a quota verdict.
                self.assertEqual(
                    routing_v2.quota_hard_gates(profile, self.unknown, mode=mode),
                    routing_v2.quota_hard_gates(profile, self.unknown))

    def test_fail_capped_admits_only_advisory_and_review_while_unknown(self) -> None:
        profile = self.capped_profile()
        for mode, passed in (("advisory", True), ("review", True), ("implement", False)):
            with self.subTest(mode=mode):
                gate = next(g for g in routing_v2.quota_hard_gates(profile, self.unknown, mode=mode)
                            if g["code"] == "quota-unknown-capped")
                self.assertIs(gate["passed"], passed)
                self.assertIn("policy=fail-capped;observed=false", gate["evidence"])
        fresh = {"status": "fresh", "headroom": 0.5, "confidence": 1.0}
        gate = next(g for g in routing_v2.quota_hard_gates(profile, fresh, mode="implement")
                    if g["code"] == "quota-unknown-capped")
        self.assertTrue(gate["passed"])

    def capped_route_patches(self, *, grok_ready: bool) -> list:
        readiness = {
            "codex": {"ready": True, "reasons": []},
            "grok": {"ready": grok_ready, "reasons": [] if grok_ready else ["synthetic-missing"]},
        }
        quotas = {"codex": dict(self.unknown),
                  "grok": {"status": "fresh", "headroom": 0.9, "confidence": 1.0}}
        return [
            mock.patch.dict(os.environ, {"AGENT_FLEET_ROUTING_MODE": "baseline"}),
            mock.patch.object(fleet, "require_fresh_model_audit",
                              return_value={"status": "FRESH", "fresh": True, "routing_status": "OK"}),
            mock.patch.object(fleet, "live_readiness",
                              return_value=(copy.deepcopy(readiness), {})),
            mock.patch.object(fleet, "read_usage", return_value=[]),
            mock.patch.object(fleet, "current_quota_views",
                              return_value=(copy.deepcopy(quotas), {})),
            mock.patch.object(fleet, "load_fresh_model_benchmark", return_value=None),
            mock.patch.object(fleet, "read_claude_billing_policy", return_value={}),
            mock.patch.object(fleet, "apply_billing_last_resort_order",
                              side_effect=lambda ranked, policy: ranked),
        ]

    def capped_profiles(self) -> dict:
        return {"codex": self.capped_profile(), "grok": copy.deepcopy(self.profiles["grok"])}

    def route_capped(self, *, mode: str, grok_ready: bool) -> dict:
        with contextlib.ExitStack() as stack:
            for patch in self.capped_route_patches(grok_ready=grok_ready):
                stack.enter_context(patch)
            return fleet.route_task(
                "Change one synthetic file.", profiles=self.capped_profiles(),
                kind="implementation", complexity="low", root=Path.cwd(), cwd=Path.cwd(),
                allow_no_eligible=True, mode=mode)

    def test_baseline_ranking_applies_the_capped_gate(self) -> None:
        # Review of 0.9.37: the base ranking called quota_hard_gates without
        # the mode, so a capped implement run stayed eligible=True.
        profiles = self.capped_profiles()
        for mode, passed in (("implement", False), ("review", True), ("advisory", True)):
            with self.subTest(mode=mode):
                ranked = fleet.rank_profiles(
                    profiles, kind="implementation", complexity="low", counts={},
                    availability={"codex": True, "grok": True},
                    quotas={"codex": dict(self.unknown),
                            "grok": {"status": "fresh", "headroom": 0.9, "confidence": 1.0}},
                    routing_mode="baseline", mode=mode)
                codex = next(row for row in ranked if row["agent"] == "codex")
                gate = next(g for g in codex["gate_results"] if g["code"] == "quota-unknown-capped")
                self.assertIs(gate["passed"], passed)
                self.assertIn(f"mode={mode}", gate["evidence"])
                self.assertEqual("gate-failed=quota-unknown-capped" in codex["reasons"], not passed)
                if not passed:
                    self.assertFalse(codex["eligible"])

    def test_route_task_never_selects_a_capped_profile_for_implement(self) -> None:
        route = self.route_capped(mode="implement", grok_ready=True)
        self.assertEqual(route["selected_agent"], "grok")
        codex = next(row for row in route["rankings"] if row["agent"] == "codex")
        self.assertFalse(codex["eligible"])
        self.assertTrue(codex["launchable"])
        # The --allow-no-eligible fallback overrides the quality floor, never
        # the owner's unknown-quota policy.
        with self.assertRaisesRegex(fleet.FleetError, "No launchable agent"):
            self.route_capped(mode="implement", grok_ready=False)
        self.assertEqual(self.route_capped(mode="review", grok_ready=False)["selected_agent"], "codex")

    def test_explicit_capped_request_names_the_policy_gate(self) -> None:
        for grok_ready in (True, False):
            with self.subTest(grok_ready=grok_ready), contextlib.ExitStack() as stack:
                for patch in self.capped_route_patches(grok_ready=grok_ready):
                    stack.enter_context(patch)
                with self.assertRaisesRegex(
                        fleet.FleetError,
                        "Requested agent codex is unavailable or fails hard gates:.*"
                        "gate-failed=quota-unknown-capped"):
                    fleet.choose_agent(
                        "codex", "Change one synthetic file.", profiles=self.capped_profiles(),
                        kind="implementation", complexity="low", root=Path.cwd(),
                        cwd=Path.cwd(), mode="implement")

    def test_send_gate_refuses_capped_implement_before_run_materialization(self) -> None:
        profiles = copy.deepcopy(self.profiles)
        profiles["codex"]["quota_collection"]["unknown_quota_mode"] = "fail-capped"
        kwargs = dict(task="PRIVATE TASK", profiles=profiles, agent_id="codex",
                      kind="implementation", complexity="low", model_override=None,
                      workspace=Path.cwd(), timeout=60, native_subagent_limit=0,
                      enable_web=False, inherit_api_keys=False, dry_run=False,
                      output_root=Path.cwd(), root=Path.cwd(), knowledge_mode="none",
                      required_inputs_file="synthetic-inputs.json")
        patches = [
            mock.patch.object(fleet, "resolve_executable", return_value="codex.exe"),
            mock.patch.object(fleet, "version_probe_environment", return_value={}),
            mock.patch.object(fleet, "profile_probe_environment", return_value={}),
            mock.patch.object(fleet, "probe_command",
                              return_value={"launchable": True, "exit_code": 0, "output": "ok"}),
            mock.patch.object(fleet, "profile_credential_status", return_value={"ready": True}),
            mock.patch.object(fleet, "merge_credential_readiness",
                              return_value={"ready": True, "reasons": []}),
            mock.patch.object(routing_v2, "readiness_from_probes",
                              return_value={"ready": True, "reasons": []}),
            mock.patch.object(routing_v2, "runtime_model_visible", return_value=(True, "visible")),
            mock.patch.object(fleet, "current_quota_views",
                              return_value=({"codex": dict(self.unknown)}, {})),
            mock.patch.dict(os.environ, {"AGENT_FLEET_DEPTH": "0"}),
            mock.patch.object(fleet, "create_run_dir"),
            mock.patch.object(fleet, "prepare_execution_workspace"),
            mock.patch.object(fleet, "load_required_inputs_manifest",
                              side_effect=fleet.FleetError("AFTER_GATE")),
        ]
        with contextlib.ExitStack() as stack:
            mocks = [stack.enter_context(patch) for patch in patches]
            create_run, inputs = mocks[-3], mocks[-1]
            for mode in ("implement", "review"):
                execution = routing_v2.resolve_execution_profile(
                    "codex", profiles["codex"], complexity="low", mode=mode, required_tools=[])
                call = dict(kwargs, mode=mode, allow_write=mode == "implement",
                            execution_profile=execution)
                if mode == "implement":
                    with self.assertRaisesRegex(
                            fleet.FleetError, "quota-unknown-capped.*policy=fail-capped"):
                        fleet.execute_single(**call)
                    inputs.assert_not_called()
                    create_run.assert_not_called()
                else:
                    with self.assertRaisesRegex(fleet.FleetError, "AFTER_GATE"):
                        fleet.execute_single(**call)
                    inputs.assert_called_once()

    def test_profile_loader_validates_the_knobs(self) -> None:
        raw = json.loads(Path(fleet.DEFAULT_PROFILES).read_text(encoding="utf-8"))
        local_pool = [name for name in ("qwen-local", "qwen-local-5070ti")]

        def load(mutate) -> dict:
            value = copy.deepcopy(raw)
            mutate(value["profiles"])
            with tempfile.TemporaryDirectory() as temporary:
                path = Path(temporary) / "profiles.json"
                path.write_text(json.dumps(value), encoding="utf-8")
                return fleet.load_profiles(path)

        def set_mode(profiles, names, mode):
            for name in names:
                profiles[name].setdefault("quota_collection", {})["unknown_quota_mode"] = mode

        with self.assertRaisesRegex(fleet.FleetError, "unknown_quota_mode must be"):
            load(lambda profiles: set_mode(profiles, ["codex"], "fail-sideways"))
        with self.assertRaisesRegex(fleet.FleetError, "disagree on unknown_quota_mode"):
            load(lambda profiles: set_mode(profiles, local_pool[:1], "fail-capped"))
        loaded = load(lambda profiles: set_mode(profiles, local_pool, "fail-capped"))
        self.assertEqual(loaded["qwen-local"]["quota_collection"]["unknown_quota_mode"], "fail-capped")

        def thresholds(warn, throttle):
            def mutate(profiles):
                profiles["mimo"]["quota_collection"]["pace_brake"].update(
                    tempo_warn=warn, tempo_throttle=throttle)
            return mutate

        with self.assertRaisesRegex(fleet.FleetError, "pace_brake is invalid"):
            load(thresholds(1.5, 1.2))
        with self.assertRaisesRegex(fleet.FleetError, "pace_brake is invalid"):
            load(thresholds(True, 1.5))
        loaded = load(thresholds(1.2, 1.5))
        self.assertEqual(loaded["mimo"]["quota_collection"]["pace_brake"]["tempo_throttle"], 1.5)

    def test_pool_members_must_share_one_pace_brake_policy(self) -> None:
        # Review of 0.9.37: current_quota_views computes one brake per pool
        # from the first enabled config, while each member's gate reads its
        # own config. A member without the brake dodged it and a member with
        # other thresholds got its neighbour's.
        raw = json.loads(Path(fleet.DEFAULT_PROFILES).read_text(encoding="utf-8"))
        pool = ("qwen-local", "qwen-local-5070ti")
        base = {"enabled": True, "window_hours": 24, "burst_factor": 1.5,
                "max_reading_age_hours": 48, "tempo_warn": 1.2, "tempo_throttle": 1.5}

        def load(first, second) -> dict:
            value = copy.deepcopy(raw)
            for name, brake in zip(pool, (first, second)):
                collection = value["profiles"][name].setdefault("quota_collection", {})
                if brake is None:
                    collection.pop("pace_brake", None)
                else:
                    collection["pace_brake"] = brake
            with tempfile.TemporaryDirectory() as temporary:
                path = Path(temporary) / "profiles.json"
                path.write_text(json.dumps(value), encoding="utf-8")
                return fleet.load_profiles(path)

        conflicts = {
            "one-member-unbraked": (base, None),
            "enabled-flags": (base, {**base, "enabled": False}),
            "throttle-threshold": (base, {**base, "tempo_throttle": 2.5}),
            "warn-threshold": (base, {**base, "tempo_warn": 1.0}),
            "window": (base, {**base, "window_hours": 12}),
        }
        for name, (first, second) in conflicts.items():
            for order, pair in enumerate(((first, second), (second, first))):
                with self.subTest(conflict=name, order=order):
                    with self.assertRaisesRegex(
                            fleet.FleetError,
                            "local-qwen-ollama-11439-zero-provider-cost members disagree on "
                            "pace_brake"):
                        load(*pair)
        loaded = load(base, copy.deepcopy(base))
        for name in pool:
            self.assertEqual(loaded[name]["quota_collection"]["pace_brake"], base)
        # A disabled brake is no brake: its parameters cannot diverge anything.
        load({**base, "enabled": False}, None)
        load({**base, "enabled": False}, {**base, "enabled": False, "window_hours": 12})



class RecheckRepairTests(unittest.TestCase):
    """Second review of 0.9.37 (after d1ac96a)."""

    def setUp(self) -> None:
        self.profiles = fleet.load_profiles(fleet.DEFAULT_PROFILES)
        self.now = dt.datetime.now(UTC)

    # HIGH: a /usages read taken after a native success that reports an
    # exhausted window is the newer signal and must not be cached as available.
    def test_usage_exhaustion_after_native_success_wins(self) -> None:
        native_available = {"status": "available", "source": "native-cli-check",
                            "covered_scopes": ["daily", "monthly", "short-window", "weekly"],
                            "probe_evidence": "assistant-text"}
        exhausted = kimi_usage.parse_usage(kimi_payload(self.now))
        self.assertEqual((exhausted["status"], exhausted["scope"]), ("exhausted", "weekly"))
        with (
            mock.patch.object(kimi_usage, "native_probe", return_value=dict(native_available)),
            mock.patch.object(kimi_usage, "query", return_value=dict(exhausted)),
        ):
            result = kimi_usage.native_fallback(fleet, {}, Path.cwd())
        self.assertEqual(result["status"], "exhausted")
        self.assertEqual(result["scope"], "weekly")
        self.assertEqual(result["source"], "kimi-native-usage")
        self.assertEqual(result["reason"], "usage-limit-after-native-check")
        self.assertEqual(result["reset_at"], exhausted["reset_at"])
        # A counter whose reset already passed is stale: the native proof stays.
        stale = kimi_payload(self.now)
        stale["usage"]["resetTime"] = iso(self.now - dt.timedelta(minutes=5))
        stale_usage = kimi_usage.parse_usage(stale)
        self.assertEqual(stale_usage["status"], "unknown")
        with (
            mock.patch.object(kimi_usage, "native_probe", return_value=dict(native_available)),
            mock.patch.object(kimi_usage, "query", return_value=dict(stale_usage)),
        ):
            kept = kimi_usage.native_fallback(fleet, {}, Path.cwd())
        self.assertEqual((kept["status"], kept["source"]), ("available", "native-cli-check"))
        # A stale weekly counter never hides a live spent short window.
        both = kimi_payload(self.now, short={"used": "100"})
        both["usage"]["resetTime"] = iso(self.now - dt.timedelta(minutes=5))
        with (
            mock.patch.object(kimi_usage, "native_probe", return_value=dict(native_available)),
            mock.patch.object(kimi_usage, "query", return_value=kimi_usage.parse_usage(both)),
        ):
            live = kimi_usage.native_fallback(fleet, {}, Path.cwd())
        self.assertEqual((live["status"], live["scope"]), ("exhausted", "short-window"))
        self.assertEqual(live["reset_at"], both["limits"][0]["detail"]["resetTime"])

    def test_check_caches_the_later_exhaustion(self) -> None:
        root = Path(tempfile.mkdtemp(prefix="fleet-kimi-recheck-"))
        native_available = {"status": "available", "source": "native-cli-check",
                            "covered_scopes": ["daily", "monthly", "short-window", "weekly"]}
        exhausted = kimi_usage.parse_usage(kimi_payload(self.now))

        def expired_token():
            raise ValueError("Kimi native access token expired")

        with (
            mock.patch.object(kimi_usage, "native_probe", return_value=dict(native_available)),
            mock.patch.object(kimi_usage, "query", return_value=dict(exhausted)),
        ):
            state = quota_availability.check(
                root, "kimi", {"status": "unknown", "headroom": None, "confidence": 0.0},
                lock_factory=fleet.ReleasableKernelLock, probe=expired_token,
                fallback=lambda: kimi_usage.native_fallback(fleet, {}, root))
        self.assertEqual(state["status"], "exhausted")
        view = quota_availability.overlay({"status": "unknown"}, state, require_check=True)
        self.assertTrue(view["availability_blocked"])

    # MEDIUM: baseline ranking must apply the context-window gate.
    def small_window(self, agent: str, window) -> dict:
        profile = copy.deepcopy(self.profiles[agent])
        for row in profile["execution_profiles"].values():
            row["context_window_tokens"] = window
        return profile

    def baseline_assessment(self, task: str) -> dict:
        return {**adaptive.fallback_assessment(kind="review", complexity="low",
                                               profiles=self.profiles),
                "context_estimate": routing_v2.task_context_estimate(task)}

    def test_baseline_ranking_applies_the_context_window_gate(self) -> None:
        profiles = {"codex": self.small_window("codex", 1000),
                    "grok": self.small_window("grok", 1_000_000)}
        fresh = {"status": "fresh", "headroom": 0.9, "confidence": 1.0}
        ranked = fleet.rank_profiles(
            profiles, kind="review", complexity="low", counts={},
            availability={"codex": True, "grok": True},
            assessment=self.baseline_assessment("x" * 8000),
            quotas={"codex": dict(fresh), "grok": dict(fresh)},
            routing_mode="baseline", mode="review")
        codex = next(row for row in ranked if row["agent"] == "codex")
        grok = next(row for row in ranked if row["agent"] == "grok")
        gate = next(g for g in codex["gate_results"] if g["code"] == "context-window")
        self.assertFalse(gate["passed"])
        self.assertIn("estimated=2000", gate["evidence"])
        self.assertIn("window=1000", gate["evidence"])
        self.assertFalse(codex["eligible"])
        self.assertIn("gate-failed=context-window", codex["reasons"])
        self.assertTrue(next(g for g in grok["gate_results"]
                             if g["code"] == "context-window")["passed"])
        # A task that fits keeps the small-window profile eligible; an unknown
        # window never blocks.
        fits = fleet.rank_profiles(
            {"codex": self.small_window("codex", 1000),
             "grok": self.small_window("grok", None)},
            kind="review", complexity="low", counts={},
            availability={"codex": True, "grok": True},
            assessment=self.baseline_assessment("x" * 400),
            quotas={"codex": dict(fresh), "grok": dict(fresh)},
            routing_mode="baseline", mode="review")
        for row in fits:
            self.assertTrue(next(g for g in row["gate_results"]
                                 if g["code"] == "context-window")["passed"], row["agent"])

    def test_baseline_fallback_never_selects_an_overflowing_window(self) -> None:
        profiles = {"codex": self.small_window("codex", 1000)}
        with (
            mock.patch.dict(os.environ, {"AGENT_FLEET_ROUTING_MODE": "baseline"}),
            mock.patch.object(fleet, "require_fresh_model_audit",
                              return_value={"status": "FRESH", "fresh": True, "routing_status": "OK"}),
            mock.patch.object(fleet, "live_readiness",
                              return_value=({"codex": {"ready": True, "reasons": []}}, {})),
            mock.patch.object(fleet, "read_usage", return_value=[]),
            mock.patch.object(fleet, "current_quota_views", return_value=(
                {"codex": {"status": "fresh", "headroom": 0.9, "confidence": 1.0}}, {})),
            mock.patch.object(fleet, "load_fresh_model_benchmark", return_value=None),
            mock.patch.object(fleet, "read_claude_billing_policy", return_value={}),
            mock.patch.object(fleet, "apply_billing_last_resort_order",
                              side_effect=lambda ranked, policy: ranked),
        ):
            with self.assertRaisesRegex(fleet.FleetError, "No launchable agent"):
                fleet.route_task("x" * 8000, profiles=profiles, kind="review",
                                 complexity="low", root=Path.cwd(), cwd=Path.cwd(),
                                 allow_no_eligible=True, mode="review")
            route = fleet.route_task("x" * 400, profiles=profiles, kind="review",
                                     complexity="low", root=Path.cwd(), cwd=Path.cwd(),
                                     allow_no_eligible=True, mode="review")
        self.assertEqual(route["selected_agent"], "codex")

    # MEDIUM: a stream without its terminal event is an unknown count.
    def test_stream_without_terminal_event_is_unknown(self) -> None:
        codex_started = "\n".join([
            json.dumps({"type": "thread.started", "thread_id": "t"}),
            json.dumps({"type": "turn.started"}),
        ])
        self.assertIsNone(fleet.observed_model_tool_calls("codex", codex_started))
        qwen_started = json.dumps({"type": "system", "subtype": "init"})
        self.assertIsNone(fleet.observed_model_tool_calls("qwen", qwen_started))
        codex_done = codex_started + "\n" + json.dumps({"type": "turn.completed"})
        self.assertEqual(fleet.observed_model_tool_calls("codex", codex_done), 0)
        codex_failed = codex_started + "\n" + json.dumps({"type": "turn.failed"})
        self.assertEqual(fleet.observed_model_tool_calls("codex", codex_failed), 0)

    def test_supervisor_count_of_an_interrupted_stream_is_unknown(self) -> None:
        started = json.dumps({"type": "turn.started"})
        tool = json.dumps({"type": "item.started",
                           "item": {"id": "i1", "type": "command_execution"}})
        done = json.dumps({"type": "turn.completed"})
        base = {"timed_out": False, "supervision_failed": False,
                "tool_budget_exceeded": False}
        # Timed out before any tool event: not a known zero.
        self.assertIsNone(fleet.recorded_model_tool_calls(
            "codex", {**base, "model_tool_calls": 0, "timed_out": True}, started))
        self.assertIsNone(fleet.recorded_model_tool_calls(
            "claude", {**base, "model_tool_calls": 0, "supervision_failed": True},
            json.dumps({"type": "system"})))
        # A clean exit that never reached its terminal event is unknown too.
        self.assertIsNone(fleet.recorded_model_tool_calls(
            "codex", {**base, "model_tool_calls": 1}, started + "\n" + tool))
        self.assertIsNone(fleet.recorded_model_tool_calls(
            "claude", {**base, "model_tool_calls": 0}, json.dumps({"type": "system"})))
        # Complete streams keep the supervisor count.
        self.assertEqual(fleet.recorded_model_tool_calls(
            "codex", {**base, "model_tool_calls": 1}, "\n".join([started, tool, done])), 1)
        self.assertEqual(fleet.recorded_model_tool_calls(
            "claude", {**base, "model_tool_calls": 2},
            json.dumps({"type": "result", "subtype": "success"})), 2)
        # The enforced limit stopped the child: its count is the evidence.
        self.assertEqual(fleet.recorded_model_tool_calls(
            "codex", {**base, "model_tool_calls": 3, "tool_budget_exceeded": True},
            started + "\n" + tool), 3)
        # No supervisor count: the stream parser decides.
        self.assertEqual(fleet.recorded_model_tool_calls(
            "qwen", {**base, "model_tool_calls": None}, QWEN_STREAM), 2)
        self.assertIsNone(fleet.recorded_model_tool_calls(
            "kimi", {**base, "model_tool_calls": None}, QWEN_STREAM))

    # MEDIUM: a limit Fleet does not enforce is never recorded as applied.
    def test_unenforced_tool_limit_is_not_recorded(self) -> None:
        for adapter in ("kimi", "codebuddy", "opencode", "antigravity"):
            with self.subTest(adapter=adapter):
                self.assertIsNone(fleet.effective_run_limits(
                    adapter, [adapter], complexity="low", max_prompt_bytes=None,
                    max_model_tool_calls=2, context_window_tokens=None)[1])
        for adapter in ("codex", "claude", "minimax", "muse", "grok", "openclaw"):
            with self.subTest(adapter=adapter):
                self.assertEqual(fleet.effective_run_limits(
                    adapter, [adapter], complexity="low", max_prompt_bytes=None,
                    max_model_tool_calls=2, context_window_tokens=None)[1], 2)

    def test_kimi_command_carries_no_tool_budget(self) -> None:
        # Why the Kimi limit is not recorded: its argv has no tool/step cap
        # and the supervisor counts only Codex and Claude events.
        run_dir = Path(tempfile.mkdtemp(prefix="fleet-kimi-argv-"))
        command, _, _ = fleet.build_command(
            "kimi", "kimi.exe", prompt="prompt", workspace=run_dir, mode="review",
            model="kimi-k3", complexity="low", timeout=60, run_dir=run_dir,
            native_subagent_limit=0, enable_web=False, materialize=False,
            max_model_tool_calls=2)
        self.assertFalse({"--max-steps", "--max-tool-calls", "--max-steps-per-turn"}
                         & set(command))


class ThirdReviewRepairTests(unittest.TestCase):
    """Third review of 0.9.37 (after 221d52b)."""

    def setUp(self) -> None:
        self.now = dt.datetime.now(UTC)
        self.native_weekly = {"status": "exhausted", "scope": "weekly",
                              "source": "native-provider-error", "reason": "usage-limit",
                              "reset_at": None}
        self.native_available = {"status": "available", "source": "native-cli-check",
                                 "covered_scopes": ["daily", "monthly", "short-window", "weekly"],
                                 "probe_evidence": "assistant-text"}

    def usage(self, *, weekly_used: str, monthly_used: str) -> dict:
        return kimi_usage.parse_usage({
            "usage": {"limit": "100", "used": weekly_used,
                      "resetTime": iso(self.now + dt.timedelta(days=3))},
            "limits": [{"window": {"duration": 1, "timeUnit": "TIME_UNIT_MONTH"},
                        "detail": {"limit": "100", "used": monthly_used,
                                   "resetTime": iso(self.now + dt.timedelta(hours=12))}}],
        })

    # MEDIUM: a weekly native refusal must not be rewritten as the monthly
    # window that /usages happens to report as spent.
    def test_native_refusal_keeps_its_scope_when_usage_does_not_confirm_it(self) -> None:
        monthly = self.usage(weekly_used="3", monthly_used="100")
        self.assertEqual((monthly["status"], monthly["scope"]), ("exhausted", "monthly"))
        with (
            mock.patch.object(kimi_usage, "native_probe", return_value=dict(self.native_weekly)),
            mock.patch.object(kimi_usage, "query", return_value=copy.deepcopy(monthly)),
        ):
            result = kimi_usage.native_fallback(fleet, {}, Path.cwd())
        self.assertEqual((result["status"], result["scope"]), ("exhausted", "weekly"))
        self.assertIsNone(result["reset_at"])
        self.assertEqual(result["clear_requires"], "native-cli-check")
        # When /usages confirms the refused window, its reset is the better one.
        confirmed = self.usage(weekly_used="100", monthly_used="100")
        with (
            mock.patch.object(kimi_usage, "native_probe", return_value=dict(self.native_weekly)),
            mock.patch.object(kimi_usage, "query", return_value=copy.deepcopy(confirmed)),
        ):
            agreed = kimi_usage.native_fallback(fleet, {}, Path.cwd())
        self.assertEqual((agreed["status"], agreed["scope"]), ("exhausted", "weekly"))
        self.assertEqual(agreed["reset_at"], iso(self.now + dt.timedelta(days=3)))
        self.assertNotIn("clear_requires", agreed)

    def test_monthly_reset_never_clears_an_unconfirmed_weekly_refusal(self) -> None:
        root = Path(tempfile.mkdtemp(prefix="fleet-kimi-weekly-refusal-"))
        prior = {"status": "unknown", "headroom": None, "confidence": 0.0}
        start = time.time()

        def expired_token():
            raise ValueError("Kimi native access token expired")

        monthly = self.usage(weekly_used="3", monthly_used="100")
        with (
            mock.patch.object(kimi_usage, "native_probe", return_value=dict(self.native_weekly)),
            mock.patch.object(kimi_usage, "query", return_value=copy.deepcopy(monthly)),
        ):
            first = quota_availability.check(
                root, "kimi", prior, lock_factory=fleet.ReleasableKernelLock, now=start,
                probe=expired_token, fallback=lambda: kimi_usage.native_fallback(fleet, {}, root))
        self.assertEqual((first["status"], first["scope"]), ("exhausted", "weekly"))
        # Monthly window has reset; /usages still reports the weekly counter as
        # not spent. Only a native check may clear the weekly refusal.
        reset = self.usage(weekly_used="3", monthly_used="0")
        self.assertEqual(reset["status"], "available")
        with (
            mock.patch.object(kimi_usage, "native_probe",
                              return_value=dict(self.native_weekly)) as native,
            mock.patch.object(kimi_usage, "query", side_effect=lambda *a, **k: copy.deepcopy(reset)),
        ):
            second = quota_availability.check(
                root, "kimi", prior, lock_factory=fleet.ReleasableKernelLock, now=start + 3 * 3600,
                probe=lambda: kimi_usage.query(),
                fallback=lambda: kimi_usage.native_fallback(fleet, {}, root))
        native.assert_called_once()
        self.assertEqual((second["status"], second["scope"]), ("exhausted", "weekly"))
        self.assertTrue(quota_availability.overlay(prior, second, require_check=True)["availability_blocked"])
        # A newer authoritative snapshot with headroom is the same counter.
        snapshot = {"status": "fresh", "authority": "authoritative", "headroom": 0.5,
                    "observed_at": quota_availability.stamp(start + 3 * 3600 + 60)}
        held = quota_availability.check(
            root, "kimi", snapshot, lock_factory=fleet.ReleasableKernelLock,
            now=start + 3 * 3600 + 120, probe=None)
        self.assertEqual((held["status"], held["scope"]), ("exhausted", "weekly"))
        # A /usages-only exhaustion in between keeps the native-check requirement.
        with (
            mock.patch.object(kimi_usage, "native_probe", return_value=dict(self.native_weekly)),
            mock.patch.object(kimi_usage, "query", side_effect=lambda *a, **k: copy.deepcopy(monthly)),
        ):
            third = quota_availability.check(
                root, "kimi", prior, lock_factory=fleet.ReleasableKernelLock, now=start + 6 * 3600,
                probe=lambda: kimi_usage.query(), fallback=None)
        self.assertEqual(third["status"], "exhausted")
        self.assertEqual(third["clear_requires"], "native-cli-check")
        # The weekly refusal ends only when a native inference succeeds.
        with (
            mock.patch.object(kimi_usage, "native_probe", return_value=dict(self.native_available)),
            mock.patch.object(kimi_usage, "query", side_effect=lambda *a, **k: copy.deepcopy(reset)),
        ):
            cleared = quota_availability.check(
                root, "kimi", prior, lock_factory=fleet.ReleasableKernelLock, now=start + 9 * 3600,
                probe=lambda: kimi_usage.query(),
                fallback=lambda: kimi_usage.native_fallback(fleet, {}, root))
        self.assertEqual((cleared["status"], cleared["source"]), ("available", "native-cli-check"))
        self.assertNotIn("clear_requires", cleared)

    # LOW: with both brakes active, `until` is when BOTH stop blocking.
    def test_two_active_brakes_publish_the_later_release(self) -> None:
        now = dt.datetime(2026, 9, 26, 12, 40, tzinfo=UTC)
        pace = {"tempo": 1.6, "confidence": 1.0, "source": "quota-snapshot",
                "window": "monthly", "observed_at": iso(now - dt.timedelta(hours=1)),
                "reset_at": iso(now + dt.timedelta(days=20)),
                "remaining_fraction": 0.6, "elapsed_fraction": 0.25}
        config = {"enabled": True, "window_hours": 24, "burst_factor": 1.5,
                  "max_reading_age_hours": 48, "tempo_warn": 1.2, "tempo_throttle": 1.5}
        base = {"active": True, "until": iso(now + dt.timedelta(minutes=20)),
                "reason": "window-consumption-exceeds-allowance", "window_hours": 24}
        tempo_only = quota_pace.apply_tempo_thresholds(dict(base, active=False, until=None),
                                                       pace, config, now=now)
        tempo_until = tempo_only["until"]
        self.assertGreater(tempo_until, base["until"])
        both = quota_pace.apply_tempo_thresholds(base, pace, config, now=now)
        self.assertTrue(both["active"])
        self.assertEqual(both["reason"], "window-consumption-exceeds-allowance")
        self.assertEqual(both["until"], tempo_until)
        # The allowance brake releasing later keeps its own, later `until`.
        late = dict(base, until=iso(now + dt.timedelta(days=30)))
        self.assertEqual(quota_pace.apply_tempo_thresholds(late, pace, config, now=now)["until"],
                         late["until"])


class FourthReviewRepairTests(unittest.TestCase):
    """Fourth review of 0.9.37 (after 0a30510)."""

    def setUp(self) -> None:
        self.now = dt.datetime.now(UTC)
        self.native_weekly = {"status": "exhausted", "scope": "weekly",
                              "source": "native-provider-error", "reason": "usage-limit",
                              "reset_at": None}
        self.native_available = {"status": "available", "source": "native-cli-check",
                                 "covered_scopes": ["daily", "monthly", "short-window", "weekly"],
                                 "probe_evidence": "assistant-text"}
        self.prior = {"status": "unknown", "headroom": None, "confidence": 0.0}

    def ratio_usage(self, *, weekly: float | None, five_hour: float = 0.1,
                    weekly_reset: dt.datetime | None = None) -> dict:
        usages = {"limit_5h": {"used_ratio": five_hour,
                               "reset_time": iso(self.now + dt.timedelta(hours=2))}}
        if weekly is not None:
            usages["limit_7d"] = {"used_ratio": weekly, "reset_time": iso(
                weekly_reset or self.now + dt.timedelta(days=3))}
        return kimi_usage.parse_usage({"usages": usages})

    # HIGH: a weekly refusal that /usages did not report may later be cleared
    # only by a native check, not by a /usages read that now shows the week.
    def test_refusal_of_an_omitted_window_is_cleared_only_natively(self) -> None:
        root = Path(tempfile.mkdtemp(prefix="fleet-kimi-omitted-week-"))
        start = time.time()
        omitted = self.ratio_usage(weekly=None)
        self.assertNotIn("weekly", omitted["covered_scopes"])

        def expired_token():
            raise ValueError("Kimi native access token expired")

        # The direct read fails (expired token); the native CLI refuses the
        # week and its re-read of /usages omits the weekly window.
        with (
            mock.patch.object(kimi_usage, "native_probe", return_value=dict(self.native_weekly)),
            mock.patch.object(kimi_usage, "query", side_effect=lambda *a, **k: copy.deepcopy(omitted)),
        ):
            first = quota_availability.check(
                root, "kimi", self.prior, lock_factory=fleet.ReleasableKernelLock, now=start,
                probe=expired_token,
                fallback=lambda: kimi_usage.native_fallback(fleet, {}, root))
        self.assertEqual((first["status"], first["scope"]), ("exhausted", "weekly"))
        self.assertEqual(first["clear_requires"], "native-cli-check")
        # Later /usages reports the week with capacity left; the native CLI
        # still refuses. The block must hold.
        shown = self.ratio_usage(weekly=0.4)
        self.assertEqual(shown["status"], "available")
        with (
            mock.patch.object(kimi_usage, "native_probe",
                              return_value=dict(self.native_weekly)) as native,
            mock.patch.object(kimi_usage, "query", side_effect=lambda *a, **k: copy.deepcopy(shown)),
        ):
            second = quota_availability.check(
                root, "kimi", self.prior, lock_factory=fleet.ReleasableKernelLock,
                now=start + 3600, probe=lambda: kimi_usage.query(),
                fallback=lambda: kimi_usage.native_fallback(fleet, {}, root))
        native.assert_called_once()
        self.assertEqual((second["status"], second["scope"]), ("exhausted", "weekly"))
        self.assertEqual(second["clear_requires"], "native-cli-check")
        # A native success clears it.
        with (
            mock.patch.object(kimi_usage, "native_probe", return_value=dict(self.native_available)),
            mock.patch.object(kimi_usage, "query", side_effect=lambda *a, **k: copy.deepcopy(shown)),
        ):
            cleared = quota_availability.check(
                root, "kimi", self.prior, lock_factory=fleet.ReleasableKernelLock,
                now=start + 7200, probe=lambda: kimi_usage.query(),
                fallback=lambda: kimi_usage.native_fallback(fleet, {}, root))
        self.assertEqual((cleared["status"], cleared["source"]), ("available", "native-cli-check"))
        self.assertNotIn("clear_requires", cleared)

    def test_uncovered_refusal_with_other_spent_window_requires_native_clear(self) -> None:
        spent_short = self.ratio_usage(weekly=None, five_hour=1.0)
        with (
            mock.patch.object(kimi_usage, "native_probe", return_value=dict(self.native_weekly)),
            mock.patch.object(kimi_usage, "query", return_value=copy.deepcopy(spent_short)),
        ):
            result = kimi_usage.native_fallback(fleet, {}, Path.cwd())
        self.assertEqual((result["status"], result["scope"]), ("exhausted", "weekly"))
        self.assertEqual(result["clear_requires"], "native-cli-check")

    # MEDIUM: a spent /usages window whose reset is already behind us is a
    # stale counter. It may not be cached as a 900 s exhaustion without a
    # native check.
    def test_direct_read_with_past_reset_runs_native_check(self) -> None:
        root = Path(tempfile.mkdtemp(prefix="fleet-kimi-stale-reset-"))
        stale = self.ratio_usage(weekly=1.0, weekly_reset=self.now - dt.timedelta(hours=1))
        self.assertEqual(stale["status"], "unknown")
        with (
            mock.patch.object(kimi_usage, "native_probe",
                              return_value=dict(self.native_available)) as native,
            mock.patch.object(kimi_usage, "query", side_effect=lambda *a, **k: copy.deepcopy(stale)),
        ):
            state = quota_availability.check(
                root, "kimi", self.prior, lock_factory=fleet.ReleasableKernelLock,
                now=time.time(), probe=lambda: kimi_usage.query(),
                fallback=lambda: kimi_usage.native_fallback(fleet, {}, root))
        native.assert_called_once()
        self.assertEqual((state["status"], state["source"]), ("available", "native-cli-check"))

    def test_past_reset_without_native_proof_is_inconclusive(self) -> None:
        root = Path(tempfile.mkdtemp(prefix="fleet-kimi-stale-inconclusive-"))
        stale = self.ratio_usage(weekly=1.0, weekly_reset=self.now - dt.timedelta(hours=1))
        start = time.time()
        with (
            mock.patch.object(kimi_usage, "native_probe",
                              side_effect=ValueError("Native Kimi availability check did not succeed")),
            mock.patch.object(kimi_usage, "query", side_effect=lambda *a, **k: copy.deepcopy(stale)),
        ):
            state = quota_availability.check(
                root, "kimi", self.prior, lock_factory=fleet.ReleasableKernelLock,
                now=start, probe=lambda: kimi_usage.query(),
                fallback=lambda: kimi_usage.native_fallback(fleet, {}, root))
        self.assertEqual(state["status"], "unknown")
        self.assertEqual(state["reason"], "check-unavailable")
        self.assertLessEqual(quota_availability.seconds(state["recheck_at"]), start + 61)
        # Without a fallback the stale read is inconclusive as well.
        root2 = Path(tempfile.mkdtemp(prefix="fleet-kimi-stale-nofallback-"))
        state2 = quota_availability.check(
            root2, "kimi", self.prior, lock_factory=fleet.ReleasableKernelLock,
            now=start, probe=lambda: copy.deepcopy(stale), fallback=None)
        self.assertEqual(state2["status"], "unknown")

    def test_live_spent_window_is_preferred_over_a_stale_one(self) -> None:
        mixed = kimi_usage.parse_usage({"usages": {
            "limit_5h": {"used_ratio": 1.0, "reset_time": iso(self.now - dt.timedelta(minutes=5))},
            "limit_7d": {"used_ratio": 1.0, "reset_time": iso(self.now + dt.timedelta(days=2))}}})
        self.assertEqual((mixed["status"], mixed["scope"]), ("exhausted", "weekly"))
        root = Path(tempfile.mkdtemp(prefix="fleet-kimi-live-spent-"))
        state = quota_availability.check(
            root, "kimi", self.prior, lock_factory=fleet.ReleasableKernelLock,
            now=time.time(), probe=lambda: copy.deepcopy(mixed), fallback=None)
        self.assertEqual((state["status"], state["scope"]), ("exhausted", "weekly"))

    def test_native_refusal_confirmed_only_by_a_stale_window_stands(self) -> None:
        stale = self.ratio_usage(weekly=1.0, weekly_reset=self.now - dt.timedelta(hours=1))
        with (
            mock.patch.object(kimi_usage, "native_probe", return_value=dict(self.native_weekly)),
            mock.patch.object(kimi_usage, "query", return_value=copy.deepcopy(stale)),
        ):
            result = kimi_usage.native_fallback(fleet, {}, Path.cwd())
        self.assertEqual((result["status"], result["scope"]), ("exhausted", "weekly"))
        self.assertIsNone(result["reset_at"])
        self.assertEqual(result["clear_requires"], "native-cli-check")

    # MEDIUM: an understated assessment may not shrink the task-text estimate.
    def test_context_gate_uses_the_larger_estimate(self) -> None:
        profiles = fleet.load_profiles(fleet.DEFAULT_PROFILES)
        execution = routing_v2.resolve_execution_profile(
            "codex", profiles["codex"], complexity="low", mode="review", required_tools=[])

        def gate(task: str, assessment_tokens: int) -> dict:
            assessment = adaptive.normalize_task_profile({
                **adaptive.fallback_assessment(kind="review", complexity="low", profiles=profiles),
                "context_estimate": routing_v2.task_context_estimate(task)})
            assessment["context_load"] = {**assessment["context_load"],
                                          "estimated_tokens": assessment_tokens}
            return routing_v2.context_window_gate(
                {**execution, "context_window_tokens": 1000}, assessment)

        understated = gate("x" * 8000, 100)
        self.assertFalse(understated["passed"])
        self.assertEqual(understated["evidence"],
                         "estimated=2000;source=utf8-bytes-div-4;window=1000")
        larger = gate("x" * 400, 5000)
        self.assertFalse(larger["passed"])
        self.assertEqual(larger["evidence"], "estimated=5000;source=assessment;window=1000")

    # LOW: local Qwen's -1 means "no limit"; the record says None.
    def test_unlimited_local_qwen_records_no_tool_limit(self) -> None:
        command = ["qwen", "--max-tool-calls", "-1", "--max-session-turns", "-1", "prompt"]
        self.assertIsNone(fleet.effective_run_limits(
            "qwen", command, complexity="medium", max_prompt_bytes=None,
            max_model_tool_calls=None, context_window_tokens=None)[1])
        self.assertEqual(fleet.qwen_tool_limit("medium", None, local=True), -1)



class FifthReviewRepairTests(unittest.TestCase):
    """Fifth review of 0.9.37 (after b501669)."""

    def setUp(self) -> None:
        self.profiles = fleet.load_profiles(fleet.DEFAULT_PROFILES)
        self.refusal = quota_availability.rejection(
            "codebuddy", "429 Credits exhausted. Please upgrade.", 0)

    def refused_pool(self, prefix: str) -> tuple[Path, float]:
        root = Path(tempfile.mkdtemp(prefix=prefix))
        refused_at = time.time() - 600
        quota_availability.record_rejection(
            root, "codebuddy-pro", self.refusal,
            lock_factory=fleet.ReleasableKernelLock, now=refused_at)
        return root, refused_at

    @staticmethod
    def dashboard(observed: float, *, headroom: float | None,
                  credits: float | None = None) -> dict:
        return {"status": "fresh", "authority": "authoritative", "source": "provider-dashboard",
                "observed_at": quota_availability.stamp(observed), "headroom": headroom,
                "reported_headroom": headroom, "credits_remaining": credits,
                "confidence": 1.0}

    def check(self, root: Path, prior: dict, now: float, probe=None) -> dict:
        return quota_availability.check(
            root, "codebuddy-pro", prior, lock_factory=fleet.ReleasableKernelLock,
            probe=probe, now=now)

    # HIGH: a CodeBuddy credit refusal has no native check, so a later fresh
    # authoritative dashboard reading that shows allowance must clear it.
    def test_fresh_dashboard_allowance_clears_a_credit_refusal(self) -> None:
        for name, prior_kwargs in (("headroom", {"headroom": 0.5}),
                                   ("credits", {"headroom": None, "credits": 1200.0})):
            with self.subTest(evidence=name):
                root, refused_at = self.refused_pool("fleet-codebuddy-refill-")
                now = refused_at + 600
                prior = self.dashboard(refused_at + 300, **prior_kwargs)
                state = self.check(root, prior, now)
                self.assertEqual(state["status"], "available")
                view = quota_availability.overlay(prior, state)
                self.assertFalse(view["availability_blocked"])
                gates = routing_v2.quota_hard_gates(self.profiles["codebuddy"], view)
                gate = next(g for g in gates if g["code"] == "quota-not-exhausted")
                self.assertTrue(gate["passed"], gate)
                # After the short recheck window the pool stays recovered.
                later = self.check(root, prior, now + 120)
                self.assertFalse(quota_availability.overlay(prior, later)["availability_blocked"])

    def test_credit_refusal_stands_without_later_positive_credit_evidence(self) -> None:
        cases = (
            ("reading-before-refusal", -300, {"headroom": 0.5}),
            ("zero-credits", 300, {"headroom": 0.5, "credits": 0.0}),
            ("no-number", 300, {"headroom": None}),
        )
        for name, offset, prior_kwargs in cases:
            with self.subTest(case=name):
                root, refused_at = self.refused_pool("fleet-codebuddy-stands-")
                now = refused_at + 600
                prior = self.dashboard(refused_at + offset, **prior_kwargs)
                state = self.check(root, prior, now)
                self.assertTrue(quota_availability.overlay(prior, state)["availability_blocked"])

    def test_pool_with_native_check_still_needs_it_for_credits(self) -> None:
        root, refused_at = self.refused_pool("fleet-credits-native-")
        prior = self.dashboard(refused_at + 300, headroom=0.5)

        def probe() -> dict:
            raise ValueError("synthetic native check unavailable")

        state = self.check(root, prior, refused_at + 600, probe=probe)
        self.assertTrue(quota_availability.overlay(prior, state)["availability_blocked"])

    # MEDIUM: an explicit request for a refused account names the refusal.
    def test_explicit_request_for_a_refused_account_names_the_refusal(self) -> None:
        profiles = {name: self.profiles[name] for name in ("grok", "codex")}
        readiness = {"grok": {"ready": True, "reasons": []},
                     "codex": {"ready": False, "reasons": ["synthetic-missing"]}}
        quotas = {"grok": blocked_view("grok-build-balance-exhausted-http402"),
                  "codex": {"status": "unknown", "headroom": None, "confidence": 0.0}}
        for routing_mode in ("baseline", "adaptive"):
            with self.subTest(routing_mode=routing_mode),                     mock.patch.dict(os.environ, {"AGENT_FLEET_ROUTING_MODE": routing_mode}),                     mock.patch.object(fleet, "require_fresh_model_audit",
                                      return_value={"status": "FRESH", "fresh": True,
                                                    "routing_status": "OK"}),                     mock.patch.object(fleet, "live_readiness",
                                      return_value=(copy.deepcopy(readiness), {})),                     mock.patch.object(fleet, "read_usage", return_value=[]),                     mock.patch.object(fleet, "current_quota_views", return_value=(quotas, {})),                     mock.patch.object(fleet, "load_fresh_model_benchmark", return_value=None),                     mock.patch.object(fleet, "read_claude_billing_policy", return_value={}),                     mock.patch.object(fleet, "apply_billing_last_resort_order",
                                      side_effect=lambda ranked, policy: ranked):
                with self.assertRaisesRegex(
                        fleet.FleetError,
                        "Requested agent grok is unavailable or fails hard gates:.*"
                        "availability-blocked=grok-build-balance-exhausted-http402"):
                    fleet.choose_agent(
                        "grok", "Review one synthetic file.", profiles=profiles,
                        kind="review", complexity="low", root=Path.cwd(),
                        cwd=Path.cwd(), mode="review")

    # LOW: MiniMax and Muse always run with a step cap; the record shows it.
    def test_default_step_caps_are_recorded(self) -> None:
        cases = (("minimax", ["minimax", "exec", "--max-steps", "128", "--output-format", "json"]),
                 ("muse", ["muse", "exec", "--max-model-steps", "256", "--prompt-file", "p"]))
        for adapter, command in cases:
            with self.subTest(adapter=adapter):
                expected = int(command[command.index("--max-steps" if adapter == "minimax"
                                                     else "--max-model-steps") + 1])
                self.assertEqual(fleet.effective_run_limits(
                    adapter, command, complexity="low", max_prompt_bytes=None,
                    max_model_tool_calls=None, context_window_tokens=None)[1], expected)



class SixthReviewRepairTests(unittest.TestCase):
    """Sixth review of 0.9.37 (after 4490f00)."""

    def setUp(self) -> None:
        self.profiles = fleet.load_profiles(fleet.DEFAULT_PROFILES)

    @staticmethod
    def not_exhausted(profile: dict, view: dict) -> dict:
        gates = routing_v2.quota_hard_gates(profile, view)
        return next(g for g in gates if g["code"] == "quota-not-exhausted")

    # HIGH: a fresh authoritative zero balance is exhausted even when a
    # weekly fraction still shows headroom and no refusal was ever stored.
    def test_fresh_zero_credit_balance_blocks_without_a_stored_refusal(self) -> None:
        for name, weekly in (("weekly-headroom", 0.5), ("credits-only", None)):
            with self.subTest(case=name):
                root = Path(tempfile.mkdtemp(prefix="fleet-zero-credits-"))
                adaptive.record_quota(
                    root, agent="codebuddy", short_remaining_fraction=None,
                    weekly_remaining_fraction=weekly, credits_remaining=0.0,
                    short_reset_at=None, weekly_reset_at=None, observed_at=None,
                    source="provider-dashboard", confidence=1.0,
                    note="synthetic zero balance", valid_for_hours=24,
                    quota_pool_id="codebuddy-pro")
                with mock.patch.object(fleet.quota_collectors, "collect_profile_quotas",
                                       return_value=({}, {})):
                    views, _ = fleet.current_quota_views(
                        root, {"codebuddy": self.profiles["codebuddy"]},
                        agent_ids=["codebuddy"])
                view = views["codebuddy"]
                self.assertEqual(view["status"], "fresh")
                gate = self.not_exhausted(self.profiles["codebuddy"], view)
                self.assertFalse(gate["passed"], gate)
                self.assertIn("credits_remaining=0", gate["evidence"])

    def test_positive_or_advisory_credit_balance_does_not_block(self) -> None:
        base = {"status": "fresh", "authority": "authoritative", "headroom": 0.5,
                "reported_headroom": 0.5, "hard_blocking": False, "confidence": 1.0}
        cases = (("positive", {**base, "credits_remaining": 1200.0}),
                 ("unrecorded", {**base, "credits_remaining": None}),
                 ("advisory-zero", {**base, "authority": "advisory", "credits_remaining": 0.0}),
                 ("stale-zero", {**base, "status": "stale", "headroom": None,
                                 "credits_remaining": 0.0}))
        for name, view in cases:
            with self.subTest(case=name):
                gate = self.not_exhausted(self.profiles["codebuddy"], view)
                self.assertTrue(gate["passed"], gate)

    # MEDIUM: a DEGRADED model audit still names an explicit refusal.
    def test_explicit_refusal_is_named_when_routing_is_degraded(self) -> None:
        profiles = {name: self.profiles[name] for name in ("grok", "codex")}
        readiness = {"grok": {"ready": True, "reasons": []},
                     "codex": {"ready": False, "reasons": ["synthetic-missing"]}}
        quotas = {"grok": blocked_view("grok-build-balance-exhausted-http402"),
                  "codex": {"status": "unknown", "headroom": None, "confidence": 0.0}}
        with mock.patch.object(fleet, "require_fresh_model_audit",
                               return_value={"status": "STALE", "fresh": False,
                                             "routing_status": "DEGRADED"}),                 mock.patch.object(fleet, "live_readiness",
                                  return_value=(copy.deepcopy(readiness), {})),                 mock.patch.object(fleet, "read_usage", return_value=[]),                 mock.patch.object(fleet, "current_quota_views", return_value=(quotas, {})),                 mock.patch.object(fleet, "load_fresh_model_benchmark", return_value=None),                 mock.patch.object(fleet, "read_claude_billing_policy", return_value={}),                 mock.patch.object(fleet, "apply_billing_last_resort_order",
                                  side_effect=lambda ranked, policy: ranked):
            with self.assertRaisesRegex(
                    fleet.FleetError,
                    "Requested agent grok is unavailable or fails hard gates:.*"
                    "availability-blocked=grok-build-balance-exhausted-http402"):
                fleet.choose_agent(
                    "grok", "Review one synthetic file.", profiles=profiles,
                    kind="review", complexity="low", root=Path.cwd(),
                    cwd=Path.cwd(), mode="review")


class SeventhReviewRepairTests(unittest.TestCase):
    """Seventh review of 0.9.37 (after 2dde97d)."""

    def setUp(self) -> None:
        self.profiles = fleet.load_profiles(fleet.DEFAULT_PROFILES)

    def capped_codebuddy(self) -> dict:
        profile = copy.deepcopy(self.profiles["codebuddy"])
        profile.setdefault("quota_collection", {})["unknown_quota_mode"] = "fail-capped"
        return profile

    @staticmethod
    def gate(profile: dict, view: dict, code: str) -> dict:
        gates = routing_v2.quota_hard_gates(profile, view, mode="implement")
        return next(g for g in gates if g["code"] == code)

    # MEDIUM: a fresh reading that carries only a credit balance is a numeric
    # quota observation, so fail-capped must not treat it as unknown.
    def test_fresh_credits_only_reading_is_an_observation(self) -> None:
        root = Path(tempfile.mkdtemp(prefix="fleet-credits-only-"))
        adaptive.record_quota(
            root, agent="codebuddy", short_remaining_fraction=None,
            weekly_remaining_fraction=None, credits_remaining=1200.0,
            short_reset_at=None, weekly_reset_at=None, observed_at=None,
            source="provider-dashboard", confidence=1.0,
            note="synthetic credits-only balance", valid_for_hours=24,
            quota_pool_id="codebuddy-pro")
        profile = self.capped_codebuddy()
        with mock.patch.object(fleet.quota_collectors, "collect_profile_quotas",
                               return_value=({}, {})):
            views, _ = fleet.current_quota_views(
                root, {"codebuddy": profile}, agent_ids=["codebuddy"])
        view = views["codebuddy"]
        self.assertEqual(view["status"], "fresh")
        self.assertIsNone(view["headroom"])
        capped = self.gate(profile, view, "quota-unknown-capped")
        self.assertTrue(capped["passed"], capped)
        self.assertIn("observed=true", capped["evidence"])
        self.assertTrue(self.gate(profile, view, "quota-not-exhausted")["passed"])

    def test_zero_or_stale_credit_balance_still_does_not_admit(self) -> None:
        profile = self.capped_codebuddy()
        base = {"status": "fresh", "authority": "authoritative", "headroom": None,
                "reported_headroom": None, "hard_blocking": False, "confidence": 1.0}
        zero = {**base, "credits_remaining": 0.0}
        # Observed, but exhausted on its own.
        self.assertTrue(self.gate(profile, zero, "quota-unknown-capped")["passed"])
        self.assertFalse(self.gate(profile, zero, "quota-not-exhausted")["passed"])
        strict = copy.deepcopy(profile)
        strict["quota_collection"]["minimum_confidence"] = 0.5
        for name, rules, view in (
                ("stale", profile, {**base, "status": "stale", "credits_remaining": 1200.0}),
                ("boolean", profile, {**base, "credits_remaining": True}),
                ("unrecorded", profile, {**base, "credits_remaining": None}),
                ("low-confidence", strict, {**base, "credits_remaining": 1200.0,
                                            "confidence": 0.1})):
            with self.subTest(case=name):
                gate = self.gate(rules, view, "quota-unknown-capped")
                self.assertFalse(gate["passed"], gate)


class EighthReviewRepairTests(unittest.TestCase):
    """Eighth review of 0.9.37 (after fc9a190)."""

    NON_FINITE = (("nan", float("nan")), ("inf", float("inf")))

    def setUp(self) -> None:
        self.profiles = fleet.load_profiles(fleet.DEFAULT_PROFILES)

    # P2: quota-record must not store a NaN/Infinity credit balance.
    def test_record_rejects_non_finite_credit_balance(self) -> None:
        root = Path(tempfile.mkdtemp(prefix="fleet-nonfinite-record-"))
        for name, value in self.NON_FINITE:
            with self.subTest(value=name):
                with self.assertRaises(adaptive.AdaptiveError):
                    adaptive.record_quota(
                        root, agent="codebuddy", short_remaining_fraction=None,
                        weekly_remaining_fraction=None, credits_remaining=value,
                        short_reset_at=None, weekly_reset_at=None, observed_at=None,
                        source="provider-dashboard", confidence=1.0,
                        note="synthetic non-finite balance", valid_for_hours=24,
                        quota_pool_id="codebuddy-pro")
        self.assertFalse((root / "quota").exists() and any((root / "quota").iterdir()))

    # P2: a non-finite balance (e.g. a legacy row) is not a quota observation.
    def test_non_finite_credit_balance_is_not_an_observation(self) -> None:
        profile = copy.deepcopy(self.profiles["codebuddy"])
        collection = profile.setdefault("quota_collection", {})
        collection["unknown_quota_mode"] = "fail-capped"
        collection["require_fresh_snapshot"] = True
        base = {"status": "fresh", "authority": "authoritative", "headroom": None,
                "reported_headroom": None, "hard_blocking": False, "confidence": 1.0}
        for name, value in self.NON_FINITE:
            with self.subTest(value=name):
                gates = routing_v2.quota_hard_gates(
                    profile, {**base, "credits_remaining": value}, mode="implement")
                by_code = {g["code"]: g for g in gates}
                self.assertFalse(by_code["quota-observed"]["passed"], by_code)
                self.assertFalse(by_code["quota-unknown-capped"]["passed"], by_code)

    # P2: a non-finite balance never clears a stored CodeBuddy credit refusal.
    def test_non_finite_credit_balance_keeps_a_credit_refusal(self) -> None:
        refusal = quota_availability.rejection(
            "codebuddy", "429 Credits exhausted. Please upgrade.", 0)
        for name, value in self.NON_FINITE:
            for headroom in (None, 0.5):
                with self.subTest(value=name, headroom=headroom):
                    root = Path(tempfile.mkdtemp(prefix="fleet-nonfinite-refusal-"))
                    refused_at = time.time() - 600
                    quota_availability.record_rejection(
                        root, "codebuddy-pro", refusal,
                        lock_factory=fleet.ReleasableKernelLock, now=refused_at)
                    prior = {"status": "fresh", "authority": "authoritative",
                             "source": "provider-dashboard",
                             "observed_at": quota_availability.stamp(refused_at + 300),
                             "headroom": headroom, "reported_headroom": headroom,
                             "credits_remaining": value, "confidence": 1.0}
                    state = quota_availability.check(
                        root, "codebuddy-pro", prior,
                        lock_factory=fleet.ReleasableKernelLock, now=refused_at + 600)
                    self.assertTrue(
                        quota_availability.overlay(prior, state)["availability_blocked"], state)
        self.assertFalse(quota_availability.positive(float("inf")))
        self.assertFalse(quota_availability.positive(float("nan")))


class NinthReviewRepairTests(unittest.TestCase):
    """Ninth review of 0.9.37: a stale window must not hide a fresh one.

    The pool pace keeps only the highest-tempo window. When that window's
    reading is older than max_reading_age_hours the tempo level was `stale`
    and the brake stayed clear, even though a second, fresh window of the
    same pool was over the throttle.
    """

    def setUp(self) -> None:
        self.now = dt.datetime(2026, 9, 27, 3, 0, tzinfo=UTC)
        self.config = {"enabled": True, "window_hours": 24, "burst_factor": 1.5,
                       "max_reading_age_hours": 6, "tempo_warn": 1.2, "tempo_throttle": 1.5}

    def weekly_row(self, *, age_hours: float, remaining: float) -> dict:
        # Reading at 10% of the week: consumed / 0.1 is its tempo.
        observed = self.now - dt.timedelta(hours=age_hours)
        reset = observed + dt.timedelta(hours=168 * 0.9)
        return {"agent": "synthetic", "observed_at": iso(observed), "source": "quota-snapshot",
                "weekly_remaining_fraction": remaining, "weekly_reset_at": iso(reset)}

    def short_row(self, *, remaining: float) -> dict:
        # Fresh 5 h window read 30 min ago at 20% elapsed.
        observed = self.now - dt.timedelta(minutes=30)
        reset = observed + dt.timedelta(hours=5 * 0.8)
        return {"agent": "synthetic", "observed_at": iso(observed), "source": "quota-snapshot",
                "short_window_remaining_fraction": remaining, "short_window_hours": 5,
                "short_window_reset_at": iso(reset)}

    def evaluate(self, snapshots: list[dict]) -> tuple[dict, dict]:
        kwargs = {"snapshots": snapshots, "runs": [], "pool_ids": {"synthetic": "pool"},
                  "now": self.now}
        pace = quota_pace.calculate_pool_pace("pool", **kwargs)
        windows = quota_pace.calculate_pool_pace_windows("pool", **kwargs)
        brake = quota_pace.calculate_pool_brake("pool", config=self.config, **kwargs)
        return pace, quota_pace.apply_tempo_thresholds(
            brake, pace, self.config, now=self.now, windows=windows)

    def test_stale_limiting_window_does_not_hide_a_fresh_window_over_throttle(self) -> None:
        # Stale weekly tempo 7.0 outranks the fresh short tempo (3.0 read,
        # 2.0 idle now), so the old code saw only the stale reading.
        pace, brake = self.evaluate([self.weekly_row(age_hours=10, remaining=0.3),
                                     self.short_row(remaining=0.4)])
        self.assertEqual(pace["window"], "weekly")
        self.assertTrue(brake["active"], brake)
        self.assertEqual(brake["reason"], "tempo-exceeds-throttle")
        self.assertEqual(brake["tempo_level"], "throttle")
        self.assertEqual(brake["tempo_window"], "short")
        self.assertAlmostEqual(brake["tempo_now"], 2.0, places=4)
        self.assertEqual(brake["tempo_stale_windows"], ["weekly"])
        # The short window releases once its idle tempo falls to 1.5:
        # consumed 0.6 over 5 h reaches 1.5 at 2 h into the window.
        until = dt.datetime.fromisoformat(brake["until"].replace("Z", "+00:00"))
        expected = self.now - dt.timedelta(hours=1.5) + dt.timedelta(hours=2)
        self.assertLess(abs((until - expected).total_seconds()), 1.0)

    def test_stale_window_over_throttle_never_brakes_and_is_not_reported_clear(self) -> None:
        pace, brake = self.evaluate([self.weekly_row(age_hours=10, remaining=0.3),
                                     self.short_row(remaining=0.95)])
        self.assertEqual(pace["window"], "weekly")
        self.assertFalse(brake["active"], brake)
        self.assertEqual(brake["tempo_level"], "within-pace")
        self.assertEqual(brake["tempo_window"], "short")
        # The stale window is unknown, and the view says so.
        self.assertEqual(brake["tempo_stale_windows"], ["weekly"])

    def test_every_fresh_window_is_checked_not_only_the_highest(self) -> None:
        # Both fresh. The short window has the higher tempo (2.0 idle) but
        # lets go 30 min from now; the weekly one (1.8 read, ~1.7 idle) is
        # over the throttle too and lets go ~2.4 h from now. The brake holds
        # until the later of the two, not only the limiting window's release.
        weekly = self.weekly_row(age_hours=1, remaining=0.82)
        short = self.short_row(remaining=0.4)
        pace, brake = self.evaluate([weekly, short])
        self.assertEqual(pace["window"], "short")
        self.assertTrue(brake["active"])
        self.assertEqual(brake["tempo_window"], "short")
        self.assertEqual(brake["tempo_stale_windows"], [])

        def alone(row: dict) -> dict:
            return quota_pace.apply_tempo_thresholds(
                {"active": False, "until": None, "reason": "no-ledger-tokens"},
                quota_pace.calculate_pool_pace("pool", snapshots=[row], runs=[],
                                               pool_ids={"synthetic": "pool"}, now=self.now),
                self.config, now=self.now)

        weekly_only, short_only = alone(weekly), alone(short)
        self.assertTrue(weekly_only["active"])
        self.assertGreater(weekly_only["until"], short_only["until"])
        self.assertEqual(brake["until"], weekly_only["until"])

    def test_current_views_brake_on_a_fresh_window_behind_a_stale_one(self) -> None:
        profile = {"adapter": "qwen", "quota_pool_id": "synthetic-pool",
                   "quota_collection": {"pace_brake": self.config}}
        rows = [self.weekly_row(age_hours=10, remaining=0.3), self.short_row(remaining=0.4)]
        with (
            mock.patch.object(adaptive, "latest_quotas", return_value={}),
            mock.patch.object(adaptive, "read_quota_records", return_value=rows),
            mock.patch.object(fleet.quota_collectors, "collect_profile_quotas", return_value=({}, {})),
            mock.patch.object(fleet, "read_usage", return_value=[]),
            mock.patch.object(fleet, "_write_pace_latest", return_value=True),
        ):
            views, _ = fleet.current_quota_views(
                Path("synthetic-state"), {"synthetic": profile}, now=self.now)
        brake = views["synthetic"]["pace_brake"]
        self.assertTrue(brake["active"], brake)
        gate = next(g for g in routing_v2.quota_hard_gates(profile, views["synthetic"])
                    if g["code"] == "pace-brake-clear")
        self.assertFalse(gate["passed"])
        self.assertIn("reason=tempo-exceeds-throttle", gate["evidence"])


class TenthReviewRepairTests(unittest.TestCase):
    """Tenth review of 0.9.37: a tool-event scan that could not read every
    event is an unknown count, never a lower one, and never aborts the run.

    Two members of the same class: the node-capped walk returned the ids it
    had reached as if the event were fully read (a real tool call behind 1000
    text blocks became 0), and the live supervisor turned a RecursionError
    from a deeply nested, model-controlled event into io_failed, killing the
    child with 126 (owner decision: telemetry is warn-only).
    """

    TEXT = {"type": "text", "text": "x"}

    def assistant(self, blocks: list[dict]) -> str:
        return json.dumps({"type": "assistant", "message": {"content": blocks}})

    def stream(self, *lines: str) -> str:
        return "\n".join([json.dumps({"type": "system", "subtype": "init"}), *lines,
                          json.dumps({"type": "result", "subtype": "success"})])

    def test_tool_call_behind_many_text_blocks_is_counted(self) -> None:
        tool = {"type": "tool_use", "id": "t1", "name": "grep", "input": {}}
        stdout = self.stream(self.assistant([tool] + [self.TEXT] * 1000))
        self.assertEqual(fleet.observed_model_tool_calls("qwen", stdout), 1)

    def test_event_past_the_scan_cap_is_unknown_not_a_lower_count(self) -> None:
        tool = {"type": "tool_use", "id": "t1", "name": "grep", "input": {}}
        wide = self.assistant([tool] + [self.TEXT] * 5000)
        self.assertIsNone(fleet.observed_model_tool_calls("qwen", self.stream(wide)))
        with self.assertRaises(fleet.ToolEventScanIncomplete):
            fleet.model_tool_event_ids(wide, "claude")
        base = {"timed_out": False, "supervision_failed": False,
                "tool_budget_exceeded": False}
        self.assertIsNone(fleet.recorded_model_tool_calls(
            "claude", {**base, "model_tool_calls": 0}, self.stream(wide)))

    def test_unreadable_live_event_is_rejected_as_incomplete(self) -> None:
        deep = '{"type":"item.started","item":{"id":"i2","type":"mcp_tool_call","arguments":' + (
            "[" * 3000 + "]" * 3000) + "}}"
        with self.assertRaises(fleet.ToolEventScanIncomplete):
            fleet.model_tool_event_ids(deep, "codex")
        # A line that is not an event at all is still just not a tool call.
        self.assertEqual(fleet.model_tool_event_ids("Reading prompt from stdin...", "codex"), set())

    def run_codex_stream(self, lines: list[str], limit: int) -> dict:
        code = "import sys\nfor line in sys.stdin.read().splitlines():\n    print(line, flush=True)\n"
        return fleet.run_bounded_child(
            [sys.executable, "-u", "-c", code], cwd=Path.cwd(), env=dict(os.environ),
            stdin_text="\n".join(lines) + "\n", timeout=20, adapter_id="codex",
            max_model_tool_calls=limit)

    def codex_lines(self) -> tuple[str, str, str, str]:
        deep = ('{"type":"item.started","item":{"id":"i2","type":"mcp_tool_call","arguments":'
                + "[" * 3000 + "]" * 3000 + "}}")
        tool = json.dumps({"type": "item.started", "item": {"id": "i1", "type": "command_execution"}})
        tool3 = json.dumps({"type": "item.started", "item": {"id": "i3", "type": "file_change"}})
        return deep, tool, tool3, json.dumps({"type": "turn.completed"})

    def test_supervisor_warns_and_records_unknown_instead_of_aborting(self) -> None:
        deep, tool, _, done = self.codex_lines()
        child = self.run_codex_stream([tool, deep, done], limit=5)
        self.assertEqual(child["exit_code"], 0, child["stderr"])
        self.assertFalse(child["supervision_failed"])
        self.assertFalse(child["tool_budget_exceeded"])
        self.assertTrue(child["model_tool_scan_incomplete"])
        self.assertIsNone(child["model_tool_calls"])
        self.assertIn("tool count is unknown", child["stderr"])
        self.assertIn('"turn.completed"', child["stdout"])
        self.assertIsNone(fleet.recorded_model_tool_calls("codex", child, child["stdout"]))

    def test_supervisor_still_enforces_the_limit_past_an_unreadable_event(self) -> None:
        deep, tool, tool3, done = self.codex_lines()
        child = self.run_codex_stream([tool, deep, tool3, done], limit=1)
        self.assertEqual(child["exit_code"], 125, child["stderr"])
        self.assertTrue(child["tool_budget_exceeded"])
        self.assertTrue(child["model_tool_scan_incomplete"])
        # A lower bound over the limit is still enforcement evidence.
        self.assertEqual(child["model_tool_calls"], 2)
        self.assertEqual(fleet.recorded_model_tool_calls("codex", child, child["stdout"]), 2)

    def test_clean_supervised_stream_reports_a_complete_scan(self) -> None:
        _, tool, tool3, done = self.codex_lines()
        child = self.run_codex_stream([tool, tool3, done], limit=5)
        self.assertEqual(child["exit_code"], 0, child["stderr"])
        self.assertFalse(child["model_tool_scan_incomplete"])
        self.assertEqual(child["model_tool_calls"], 2)



class EleventhReviewRepairTests(unittest.TestCase):
    """Eleventh review of 0.9.37 (after 6d70e9d).

    P2: a native Kimi refusal whose /usages re-read failed was cached without
    clear_requires=native-cli-check, so after its TTL a positive /usages read
    covering the same window cleared it with no native check. P3: a corrupt
    event line (starts with '{', is not JSON) was ignored by the live tool
    scan like a text banner instead of marking the scan incomplete.
    """

    def setUp(self) -> None:
        self.now = dt.datetime.now(UTC)
        self.native_weekly = {"status": "exhausted", "scope": "weekly",
                              "source": "native-provider-error", "reason": "usage-limit",
                              "reset_at": None}
        self.native_available = {"status": "available", "source": "native-cli-check",
                                 "covered_scopes": ["daily", "monthly", "short-window", "weekly"],
                                 "probe_evidence": "assistant-text"}
        self.prior = {"status": "unknown", "headroom": None, "confidence": 0.0}
        self.shown = kimi_usage.parse_usage({"usages": {
            "limit_5h": {"used_ratio": 0.1, "reset_time": iso(self.now + dt.timedelta(hours=2))},
            "limit_7d": {"used_ratio": 0.4, "reset_time": iso(self.now + dt.timedelta(days=3))}}})

    def test_refusal_with_failed_reread_requires_native_clear(self) -> None:
        for error in (ValueError("Kimi usage HTTP 401"), OSError("connection reset")):
            with (
                mock.patch.object(kimi_usage, "native_probe", return_value=dict(self.native_weekly)),
                mock.patch.object(kimi_usage, "query", side_effect=error),
            ):
                result = kimi_usage.native_fallback(fleet, {}, Path.cwd())
            self.assertEqual((result["status"], result["scope"]), ("exhausted", "weekly"))
            self.assertEqual(result["clear_requires"], "native-cli-check")

    def test_native_success_with_failed_reread_needs_no_native_clear(self) -> None:
        with (
            mock.patch.object(kimi_usage, "native_probe", return_value=dict(self.native_available)),
            mock.patch.object(kimi_usage, "query", side_effect=ValueError("Kimi usage HTTP 401")),
        ):
            result = kimi_usage.native_fallback(fleet, {}, Path.cwd())
        self.assertEqual((result["status"], result["source"]), ("available", "native-cli-check"))
        self.assertNotIn("clear_requires", result)

    def test_positive_usages_after_ttl_never_clears_a_refusal_with_failed_reread(self) -> None:
        root = Path(tempfile.mkdtemp(prefix="fleet-kimi-failed-reread-"))
        start = time.time()

        def expired_token():
            raise ValueError("Kimi native access token expired")

        self.assertEqual(self.shown["status"], "available")
        self.assertIn("weekly", self.shown["covered_scopes"])
        with (
            mock.patch.object(kimi_usage, "native_probe", return_value=dict(self.native_weekly)),
            mock.patch.object(kimi_usage, "query", side_effect=ValueError("Kimi usage HTTP 401")),
        ):
            first = quota_availability.check(
                root, "kimi", self.prior, lock_factory=fleet.ReleasableKernelLock, now=start,
                probe=expired_token,
                fallback=lambda: kimi_usage.native_fallback(fleet, {}, root))
        self.assertEqual((first["status"], first["scope"]), ("exhausted", "weekly"))
        self.assertEqual(first["clear_requires"], "native-cli-check")
        # Past the 900 s TTL /usages shows the same week with capacity left;
        # the native CLI still refuses. The block holds.
        with (
            mock.patch.object(kimi_usage, "native_probe",
                              return_value=dict(self.native_weekly)) as native,
            mock.patch.object(kimi_usage, "query", side_effect=lambda *a, **k: copy.deepcopy(self.shown)),
        ):
            second = quota_availability.check(
                root, "kimi", self.prior, lock_factory=fleet.ReleasableKernelLock,
                now=start + 1000, probe=lambda: kimi_usage.query(),
                fallback=lambda: kimi_usage.native_fallback(fleet, {}, root))
        native.assert_called_once()
        self.assertEqual((second["status"], second["scope"]), ("exhausted", "weekly"))
        self.assertEqual(second["clear_requires"], "native-cli-check")
        self.assertTrue(quota_availability.overlay(
            self.prior, second, require_check=True)["availability_blocked"])
        # Only a native success clears it.
        with (
            mock.patch.object(kimi_usage, "native_probe", return_value=dict(self.native_available)),
            mock.patch.object(kimi_usage, "query", side_effect=lambda *a, **k: copy.deepcopy(self.shown)),
        ):
            cleared = quota_availability.check(
                root, "kimi", self.prior, lock_factory=fleet.ReleasableKernelLock,
                now=start + 2000, probe=lambda: kimi_usage.query(),
                fallback=lambda: kimi_usage.native_fallback(fleet, {}, root))
        self.assertEqual((cleared["status"], cleared["source"]), ("available", "native-cli-check"))
        self.assertNotIn("clear_requires", cleared)

    def test_corrupt_event_line_marks_the_live_scan_incomplete(self) -> None:
        cut = '{"type":"item.started","item":{"id":"i2","type":"command_execution"'
        for adapter in ("codex", "claude"):
            with self.assertRaises(fleet.ToolEventScanIncomplete):
                fleet.model_tool_event_ids(cut, adapter)
            with self.assertRaises(fleet.ToolEventScanIncomplete):
                fleet.model_tool_event_ids("  {not json at all", adapter)
            # Text banners and blank lines are still not events.
            self.assertEqual(fleet.model_tool_event_ids("Reading prompt from stdin...", adapter), set())
            self.assertEqual(fleet.model_tool_event_ids("", adapter), set())

    def test_supervisor_records_unknown_count_after_a_corrupt_event_line(self) -> None:
        tool = json.dumps({"type": "item.started", "item": {"id": "i1", "type": "command_execution"}})
        cut = '{"type":"item.started","item":{"id":"i2","type":"file_change"'
        done = json.dumps({"type": "turn.completed"})
        code = "import sys\nfor line in sys.stdin.read().splitlines():\n    print(line, flush=True)\n"
        child = fleet.run_bounded_child(
            [sys.executable, "-u", "-c", code], cwd=Path.cwd(), env=dict(os.environ),
            stdin_text="\n".join(["banner text", tool, cut, done]) + "\n", timeout=20,
            adapter_id="codex", max_model_tool_calls=5)
        self.assertEqual(child["exit_code"], 0, child["stderr"])
        self.assertFalse(child["supervision_failed"])
        self.assertTrue(child["model_tool_scan_incomplete"])
        self.assertIsNone(child["model_tool_calls"])
        self.assertIsNone(fleet.recorded_model_tool_calls("codex", child, child["stdout"]))



class TwelfthReviewRepairTests(unittest.TestCase):
    """Twelfth review of 0.9.37 (after 65d6abcb).

    P2: an event whose field the scanner cannot handle (item.type=[] is
    unhashable in the Codex type check) raised TypeError out of the live
    scanner and aborted the run with exit 126. Every data-driven scanner
    error is an unreadable event: unknown count, warning only.
    P2: the /usages read ignored KIMI_CODE_HOME while the native check and
    the contractor run honour it, so the gate judged the wrong account.
    Both now resolve the account home through one resolver.
    """

    def odd_events(self) -> list[str]:
        return [
            json.dumps({"type": "item.started", "item": {"id": "i9", "type": []}}),
            json.dumps({"type": "item.started", "item": {"id": "i9", "type": {"k": 1}}}),
        ]

    def test_unhandled_event_field_is_an_incomplete_scan_not_a_crash(self) -> None:
        for line in self.odd_events():
            with self.assertRaises(fleet.ToolEventScanIncomplete):
                fleet.model_tool_event_ids(line, "codex")
            done = json.dumps({"type": "turn.completed"})
            self.assertIsNone(fleet.observed_model_tool_calls("codex", line + "\n" + done))

    def test_supervisor_warns_instead_of_aborting_on_an_unhandled_event_field(self) -> None:
        tool = json.dumps({"type": "item.started", "item": {"id": "i1", "type": "command_execution"}})
        done = json.dumps({"type": "turn.completed"})
        code = "import sys\nfor line in sys.stdin.read().splitlines():\n    print(line, flush=True)\n"
        child = fleet.run_bounded_child(
            [sys.executable, "-u", "-c", code], cwd=Path.cwd(), env=dict(os.environ),
            stdin_text="\n".join([tool, *self.odd_events(), done]) + "\n", timeout=20,
            adapter_id="codex", max_model_tool_calls=5)
        self.assertEqual(child["exit_code"], 0, child["stderr"])
        self.assertFalse(child["supervision_failed"])
        self.assertTrue(child["model_tool_scan_incomplete"])
        self.assertIsNone(child["model_tool_calls"])
        self.assertIsNone(fleet.recorded_model_tool_calls("codex", child, child["stdout"]))

    def make_home(self, root: Path, name: str, token: str) -> Path:
        home = root / name
        (home / "credentials").mkdir(parents=True)
        (home / "config.toml").write_text(
            '[providers."managed:kimi-code"]\ntype = "kimi"\n'
            'base_url = "https://api.kimi.com/coding/v1"\n'
            '[providers."managed:kimi-code".oauth]\nstorage = "file"\nkey = "synthetic"\n'
            '[models.k3]\nprovider = "managed:kimi-code"\nmodel = "k3"\n',
            encoding="utf-8")
        (home / "credentials" / "kimi-code.json").write_text(
            json.dumps({"access_token": token, "expires_at": time.time() + 600}),
            encoding="utf-8")
        return home

    def test_usage_read_follows_kimi_code_home_like_the_native_run(self) -> None:
        root = Path(tempfile.mkdtemp(prefix="fleet-kimi-homes-"))
        default_home = self.make_home(root / "user", ".kimi-code", (_redacted_fixture('bearer-token', 2)))
        other_home = self.make_home(root, "other-account", (_redacted_fixture('bearer-token', 1)))
        response = mock.MagicMock()
        response.__enter__.return_value.read.return_value = json.dumps(
            {"usages": {"limit_5h": {"used_ratio": 0.1}}}).encode("utf-8")
        opener = mock.Mock()
        opener.open.return_value = response
        with (
            mock.patch.object(Path, "home", return_value=root / "user"),
            mock.patch.dict(os.environ, {"KIMI_CODE_BASE_URL": "", "KIMI_CODE_HOME": str(other_home)}),
        ):
            self.assertEqual(kimi_usage.account_home(), other_home)
            kimi_usage.query(opener=opener)
            guarded = fleet.prepare_guarded_kimi_home(
                root / "run", model="k3", mode="advisory",
                native_subagent_limit=0, enable_web=False)
        request = opener.open.call_args.args[0]
        self.assertEqual(request.get_header("Authorization"), ("Bearer " + _redacted_fixture('bearer-token', 1)))
        self.assertEqual((guarded / "credentials").resolve(), (other_home / "credentials").resolve())
        opener.open.reset_mock()
        with (
            mock.patch.object(Path, "home", return_value=root / "user"),
            mock.patch.dict(os.environ, {"KIMI_CODE_BASE_URL": "", "KIMI_CODE_HOME": ""}),
        ):
            self.assertEqual(kimi_usage.account_home(), default_home)
            kimi_usage.query(opener=opener)
        request = opener.open.call_args.args[0]
        self.assertEqual(request.get_header("Authorization"), ("Bearer " + _redacted_fixture('bearer-token', 2)))



class ThirteenthReviewRepairTests(unittest.TestCase):
    """Thirteenth review of 0.9.37 (after a976ed2e), class-level.

    P2: a Kimi refusal observed during execution (native provider error line)
    was cached without clear_requires=native-cli-check, so after its TTL a
    positive /usages read covering the window cleared it without a native
    check. Every Kimi refusal proven by native execution now clears natively.
    """

    KIMI_403 = ("error: failed to run prompt: provider.api_error: 403 You've reached "
                "your weekly usage limit\n")

    setUp = EleventhReviewRepairTests.setUp

    def test_kimi_execution_refusal_requires_native_clear(self) -> None:
        denial = quota_availability.rejection("kimi", self.KIMI_403, 1)
        self.assertEqual((denial["status"], denial["scope"]), ("exhausted", "weekly"))
        self.assertEqual(denial["clear_requires"], "native-cli-check")

    def test_codebuddy_credit_refusal_keeps_its_own_clear_rule(self) -> None:
        denial = quota_availability.rejection("codebuddy", "429 Credits exhausted\n", 0)
        self.assertNotIn("clear_requires", denial)

    def test_positive_usages_after_ttl_never_clears_an_execution_refusal(self) -> None:
        root = Path(tempfile.mkdtemp(prefix="fleet-kimi-exec-refusal-"))
        start = time.time()
        denial = quota_availability.rejection("kimi", self.KIMI_403, 1)
        stored = quota_availability.record_rejection(
            root, "kimi", denial, lock_factory=fleet.ReleasableKernelLock, now=start)
        self.assertEqual(stored["clear_requires"], "native-cli-check")
        # Past the TTL /usages shows the same week with capacity; native still refuses.
        with (
            mock.patch.object(kimi_usage, "native_probe",
                              return_value=dict(self.native_weekly)) as native,
            mock.patch.object(kimi_usage, "query", side_effect=lambda *a, **k: copy.deepcopy(self.shown)),
        ):
            second = quota_availability.check(
                root, "kimi", self.prior, lock_factory=fleet.ReleasableKernelLock,
                now=start + 1000, probe=lambda: kimi_usage.query(),
                fallback=lambda: kimi_usage.native_fallback(fleet, {}, root))
        native.assert_called_once()
        self.assertEqual((second["status"], second["scope"]), ("exhausted", "weekly"))
        self.assertTrue(quota_availability.overlay(
            self.prior, second, require_check=True)["availability_blocked"])
        # A fresh authoritative snapshot with headroom does not clear it either.
        fresh = {"status": "fresh", "authority": "authoritative", "headroom": 0.6,
                 "observed_at": quota_availability.stamp(start + 1500)}
        with (
            mock.patch.object(kimi_usage, "native_probe",
                              return_value=dict(self.native_weekly)),
            mock.patch.object(kimi_usage, "query", side_effect=lambda *a, **k: copy.deepcopy(self.shown)),
        ):
            third = quota_availability.check(
                root, "kimi", fresh, lock_factory=fleet.ReleasableKernelLock,
                now=start + 2000, probe=lambda: kimi_usage.query(),
                fallback=lambda: kimi_usage.native_fallback(fleet, {}, root))
        self.assertEqual(third["status"], "exhausted")
        # Only a native success clears it.
        with (
            mock.patch.object(kimi_usage, "native_probe", return_value=dict(self.native_available)),
            mock.patch.object(kimi_usage, "query", side_effect=lambda *a, **k: copy.deepcopy(self.shown)),
        ):
            cleared = quota_availability.check(
                root, "kimi", self.prior, lock_factory=fleet.ReleasableKernelLock,
                now=start + 3000, probe=lambda: kimi_usage.query(),
                fallback=lambda: kimi_usage.native_fallback(fleet, {}, root))
        self.assertEqual((cleared["status"], cleared["source"]), ("available", "native-cli-check"))


if __name__ == "__main__":
    unittest.main()
