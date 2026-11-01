"""0.9.49: a costly native availability probe (Grok) is lazy, backed off and bounded.

Reproduces the 2026-09-22 stale HTTP 402 that blocked Grok for six days and the
0.9.46 behaviour that re-probed an available pool every five minutes.
"""
import concurrent.futures
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

import fleet
import grok_availability
import quota_availability as qa

GROK_402_LINE = (
    "Grok quota exhausted: the native CLI reported 'Grok Build usage balance exhausted' "
    "(HTTP 402) before prompt execution; zero tokens were generated. Renew or top up the "
    "Grok Build subscription, then re-run `fleet.py doctor --deep` before routing to Grok again."
)
AVAILABLE = {'status': 'available', 'source': 'native-cli-check', 'covered_scopes': ['credits'],
             'probe_tokens': 15924}
EXHAUSTED = {'status': 'exhausted', 'source': 'native-cli-check', 'scope': 'credits',
             'reason': 'grok-build-balance-exhausted-http402', 'reset_at': None}


class CountingProbe:
    def __init__(self, *results):
        self.results = list(results)
        self.calls = 0

    def __call__(self):
        self.calls += 1
        result = self.results.pop(0) if len(self.results) > 1 else self.results[0]
        if isinstance(result, Exception):
            raise result
        return dict(result)


class CostlyProbeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.now = 2000000000.0  # fixed clock, 2033-05-18

    def tearDown(self):
        self.temp.cleanup()

    def check(self, probe, now, **kwargs):
        return qa.check(self.root, 'grok-pool', {'status': 'unknown', 'headroom': None},
                        probe=probe, lock_factory=fleet.ReleasableKernelLock, now=now,
                        costly_probe=True, **kwargs)

    def write_cache(self, value):
        path = qa.cache_path(self.root, 'grok-pool')
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value), encoding='utf-8')
        return path

    def reject(self, now):
        denial = qa.rejection('grok', GROK_402_LINE, 1)
        return qa.record_rejection(self.root, 'grok-pool', denial,
                                   lock_factory=fleet.ReleasableKernelLock, now=now)

    def test_stale_402_from_six_days_ago_is_rechecked_once_then_stays_passive(self):
        # The exact cache shape that blocked Grok from 2026-09-22 to 2026-09-28.
        self.write_cache({'schema_version': 1, 'status': 'recheck-required', 'scope': 'unknown',
                          'source': 'native-provider-error',
                          'reason': 'grok-build-balance-exhausted-http402',
                          'observed_at': qa.stamp(self.now - 6 * 86400),
                          'recheck_at': qa.stamp(self.now - 60), 'reset_at': None})
        probe = CountingProbe(AVAILABLE)
        state = self.check(probe, self.now)
        self.assertEqual((state['status'], probe.calls), ('available', 1))
        self.assertEqual(state['probe_tokens'], 15924)  # visible cost telemetry
        # 100 preflights over the next day, including far past the 300 s TTL.
        for i in range(100):
            state = self.check(probe, self.now + 1 + i * 900)
            self.assertEqual(state['status'], 'available')
        self.assertEqual(probe.calls, 1)

    def test_fresh_refusal_waits_for_backoff_and_escalates(self):
        self.reject(self.now)
        probe = CountingProbe(EXHAUSTED)
        # Blocked and silent until the 15-minute step elapses.
        for offset in (0, 60, 301, 899):
            state = self.check(probe, self.now + offset)
            self.assertEqual(state['status'], 'exhausted')
        self.assertEqual(probe.calls, 0)
        t = self.now + 900
        self.assertEqual(self.check(probe, t)['block_step'], 1)
        self.assertEqual(probe.calls, 1)
        # Next paid check only after the 1 h step, then 6 h.
        self.check(probe, t + 3599)
        self.assertEqual(probe.calls, 1)
        t += 3600
        self.assertEqual(self.check(probe, t)['block_step'], 2)
        self.assertEqual(probe.calls, 2)
        self.check(probe, t + 21599)
        self.assertEqual(probe.calls, 2)
        t += 21600
        self.assertEqual(self.check(probe, t)['block_step'], 3)
        t += 86400
        self.assertEqual(self.check(probe, t)['block_step'], 3)  # capped at 24 h
        self.assertEqual(probe.calls, 4)

    def test_known_reset_outlasts_backoff(self):
        self.reject(self.now)
        reset = self.now + 5 * 86400
        probe = CountingProbe(dict(EXHAUSTED, reset_at=qa.stamp(reset)), AVAILABLE)
        self.check(probe, self.now + 900)
        self.assertEqual(probe.calls, 1)
        for offset in (3600, 21600, 86400, 4 * 86400):
            self.assertEqual(self.check(probe, self.now + 900 + offset)['status'], 'exhausted')
        self.assertEqual(probe.calls, 1)
        self.assertEqual(self.check(probe, reset + 1)['status'], 'available')
        self.assertEqual(probe.calls, 2)

    def test_failed_probe_is_diagnosed_unknown_not_a_block(self):
        self.reject(self.now)
        probe = CountingProbe(OSError('SECRET transport detail'))
        state = self.check(probe, self.now + 900)
        self.assertEqual(state['status'], 'unknown')
        self.assertEqual(state['reason'], 'check-unavailable')
        self.assertEqual(state['last_block']['reason'], 'grok-build-balance-exhausted-http402')
        self.assertFalse(qa.overlay({'status': 'unknown'}, state)['availability_blocked'])
        self.assertNotIn('SECRET', qa.cache_path(self.root, 'grok-pool').read_text())
        # Unknown is passive: no further paid probes on later preflights.
        for offset in range(1000, 90000, 3000):
            self.check(probe, self.now + offset)
        self.assertEqual(probe.calls, 1)

    def test_unproven_available_does_not_clear_a_block(self):
        self.reject(self.now)
        probe = CountingProbe({'status': 'available', 'source': 'quota-snapshot'})
        state = self.check(probe, self.now + 900)
        self.assertEqual(state['status'], 'unknown')
        self.assertEqual(state['reason'], 'check-unavailable')

    def test_daily_probe_budget_yields_unknown_without_calling_the_probe(self):
        self.reject(self.now)
        probe = CountingProbe(EXHAUSTED)
        self.check(probe, self.now + 900, probes_per_day=1)
        self.assertEqual(probe.calls, 1)
        state = self.check(probe, self.now + 900 + 3600, probes_per_day=1)
        self.assertEqual(probe.calls, 1)
        self.assertEqual((state['status'], state['reason']), ('unknown', 'probe-budget-exhausted'))
        self.assertFalse(qa.overlay({'status': 'unknown'}, state)['availability_blocked'])
        self.assertEqual(state['last_block']['status'], 'exhausted')

    def test_budget_counter_resets_on_the_next_utc_day(self):
        self.reject(self.now)
        probe = CountingProbe(EXHAUSTED)
        self.check(probe, self.now + 900, probes_per_day=1)
        next_day = self.now + 2 * 86400
        self.reject(next_day)  # a real run refused again the day after
        state = self.check(probe, next_day + 86400, probes_per_day=1)
        self.assertEqual(probe.calls, 2)
        self.assertEqual(state['probe_count'], 1)

    def test_parallel_preflights_share_one_paid_probe(self):
        self.reject(self.now)
        probe_calls = []

        def probe():
            probe_calls.append(1)
            time.sleep(.05)
            return dict(AVAILABLE)
        with concurrent.futures.ThreadPoolExecutor(max_workers=16) as pool:
            states = list(pool.map(lambda _: self.check(probe, self.now + 900), range(32)))
        self.assertEqual(len(probe_calls), 1)
        self.assertTrue(all(s['status'] == 'available' for s in states))

    def test_never_blocked_pool_is_never_probed(self):
        probe = CountingProbe(AVAILABLE)
        for offset in range(0, 100 * 60, 60):
            self.assertEqual(self.check(probe, self.now + offset)['status'], 'unknown')
        self.assertEqual(probe.calls, 0)
        self.assertFalse(qa.cache_path(self.root, 'grok-pool').exists())

    def test_repeated_refusals_escalate_through_record_rejection(self):
        first = self.reject(self.now)
        self.assertEqual(first['block_step'], 0)
        self.check(CountingProbe(OSError('down')), self.now + 900)  # unknown, step 1
        second = self.reject(self.now + 1000)
        self.assertEqual(second['block_step'], 2)
        probe = CountingProbe(AVAILABLE)
        self.check(probe, self.now + 1000 + 3600)
        self.assertEqual(probe.calls, 0)  # 6 h step, not 15 min
        self.check(probe, self.now + 1000 + 21600)
        self.assertEqual(probe.calls, 1)

    def test_refusal_history_older_than_two_days_restarts_backoff(self):
        self.reject(self.now)
        self.check(CountingProbe(EXHAUSTED), self.now + 900)
        later = self.reject(self.now + 3 * 86400)
        self.assertEqual(later['block_step'], 0)

    def test_verified_native_run_clears_block_state_for_free(self):
        self.reject(self.now)
        self.check(CountingProbe(OSError('down')), self.now + 900)
        state = qa.record_native_success(self.root, 'grok-pool',
                                         lock_factory=fleet.ReleasableKernelLock, now=self.now + 950)
        self.assertEqual((state['status'], state['source'], state['block_step']),
                         ('available', 'native-run', 0))
        probe = CountingProbe(AVAILABLE)
        self.check(probe, self.now + 5 * 86400)
        self.assertEqual(probe.calls, 0)
        # A later refusal starts again at the shortest step.
        self.assertEqual(self.reject(self.now + 6 * 86400)['block_step'], 0)

    def test_success_of_an_older_run_never_erases_a_newer_refusal(self):
        run_started = self.now
        self.reject(self.now + 30)  # a concurrent run hit HTTP 402 after this run began
        state = qa.record_native_success(self.root, 'grok-pool', lock_factory=fleet.ReleasableKernelLock,
                                         now=self.now + 60, run_started_at=qa.stamp(run_started))
        self.assertEqual(state['status'], 'exhausted')
        # A run that started after the refusal is newer evidence and clears it.
        state = qa.record_native_success(self.root, 'grok-pool', lock_factory=fleet.ReleasableKernelLock,
                                         now=self.now + 120, run_started_at=qa.stamp(self.now + 90))
        self.assertEqual(state['status'], 'available')

    def test_native_success_creates_no_cache_and_no_rewrite(self):
        self.assertEqual(qa.record_native_success(self.root, 'grok-pool',
                                                  lock_factory=fleet.ReleasableKernelLock), {})
        self.assertFalse(qa.cache_path(self.root, 'grok-pool').exists())
        path = self.write_cache({'schema_version': 1, 'status': 'available',
                                 'source': 'native-cli-check', 'observed_at': qa.stamp(self.now),
                                 'recheck_at': qa.stamp(self.now + 900)})
        before = path.read_bytes()
        qa.record_native_success(self.root, 'grok-pool', lock_factory=fleet.ReleasableKernelLock)
        self.assertEqual(path.read_bytes(), before)

    def test_authoritative_exhaustion_snapshot_reblocks_an_available_pool(self):
        self.write_cache({'schema_version': 1, 'status': 'available', 'source': 'native-run',
                          'observed_at': qa.stamp(self.now - 3600),
                          'recheck_at': qa.stamp(self.now - 2700), 'block_step': 0})
        prior = {'status': 'fresh', 'authority': 'authoritative', 'reported_headroom': 0.0,
                 'headroom': 0.0, 'observed_at': qa.stamp(self.now - 60)}
        probe = CountingProbe(AVAILABLE)
        state = qa.check(self.root, 'grok-pool', prior, probe=probe,
                         lock_factory=fleet.ReleasableKernelLock, now=self.now, costly_probe=True)
        self.assertEqual(state['status'], 'exhausted')
        self.assertEqual(probe.calls, 0)  # the fresh refusal waits for its backoff


class GrokRejectionParserTests(unittest.TestCase):
    def test_fleet_402_line_is_a_credit_exhaustion(self):
        denial = qa.rejection('grok', 'noise\n' + GROK_402_LINE + '\n', 1)
        self.assertEqual((denial['status'], denial['scope'], denial['reason']),
                         ('exhausted', 'credits', 'grok-build-balance-exhausted-http402'))

    def test_quoted_prose_is_not_a_rejection(self):
        self.assertIsNone(qa.rejection('grok', 'The model said: ' + GROK_402_LINE, 1))
        self.assertIsNone(qa.rejection('grok', 'HTTP 402 somewhere', 1))
        self.assertIsNone(qa.rejection('grok', '', 0))

    def test_other_adapters_ignore_the_grok_line(self):
        for adapter in ('kimi', 'codex', 'muse', 'claude'):
            self.assertIsNone(qa.rejection(adapter, GROK_402_LINE, 1))


class FleetWiringTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.profiles = {'grok': {'adapter': 'grok', 'quota_pool_id': 'grok-pool',
                                  'models': {'low': 'grok-4.7'}}}

    def tearDown(self):
        self.temp.cleanup()

    def views(self):
        with patch.object(fleet.adaptive, 'latest_quotas',
                          return_value={'grok': {'status': 'unknown', 'headroom': None}}), \
                patch.object(fleet.quota_collectors, 'collect_profile_quotas', return_value=({}, {})):
            return fleet.current_quota_views(self.root, self.profiles)[0]

    def test_available_grok_is_not_reprobed_by_preflights_after_ttl(self):
        path = qa.cache_path(self.root, 'grok-pool')
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps({'schema_version': 1, 'status': 'available',
                                    'source': 'native-cli-check',
                                    'observed_at': qa.stamp(time.time() - 7200),
                                    'recheck_at': qa.stamp(time.time() - 6900)}), encoding='utf-8')
        with patch.object(grok_availability, 'native_probe') as probe:
            for _ in range(10):
                views = self.views()
            probe.assert_not_called()
        self.assertFalse(views['grok']['availability_blocked'])

    def test_recent_refusal_is_blocked_without_probe(self):
        qa.record_rejection(self.root, 'grok-pool', qa.rejection('grok', GROK_402_LINE, 1),
                            lock_factory=fleet.ReleasableKernelLock, now=time.time() - 60)
        with patch.object(grok_availability, 'native_probe') as probe:
            views = self.views()
            probe.assert_not_called()
        self.assertTrue(views['grok']['availability_blocked'])


class ExecuteSingleAvailabilityTests(unittest.TestCase):
    """The real execute_single path persists Grok availability evidence (review P2)."""

    SESSION = "22222222-2222-4222-8222-222222222222"

    def setUp(self):
        import os
        from test_fleet import SCRIPT_DIR, sample_handoff
        env = mock_env = patch.dict(os.environ, {
            "AGENT_FLEET_BILLING_MODE_PATH": str(SCRIPT_DIR / "__missing_billing_mode_fixture__.json"),
            "AGENT_FLEET_DEPTH": "0", "AGENT_FLEET_OFFICE": "0"})
        mock_env.start()
        self.addCleanup(env.stop)
        self.sample_handoff = sample_handoff
        self.profiles = fleet.load_profiles(fleet.DEFAULT_PROFILES)
        self.pool = str(self.profiles['grok'].get('quota_pool_id', 'grok'))
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def envelope(self, handoff):
        return json.dumps({"sessionId": self.SESSION, "text": json.dumps(handoff),
                           "structuredOutput": handoff, "stopReason": "end_turn", "num_turns": 1,
                           "usage": {"inputTokens": 40, "outputTokens": 2, "totalTokens": 42},
                           "modelId": "grok-4.7"})

    def acp_result(self, *, exit_code, stdout, stderr):
        return {"exit_code": exit_code, "stdout": stdout, "stderr": stderr, "timed_out": False,
                "model_tool_calls": 0, "tool_budget_exceeded": False, "session_id": self.SESSION,
                "prompt_count": 1, "attempts": [{"prompt": 1, "kind": "initial"}],
                "continuation_triggers": [], "continuation_skip_reason": None,
                "duration_seconds": 0.1, "transport": fleet.grok_acp.TRANSPORT_ID}

    def run_grok(self, run_acp):
        execution_profile = {"execution_profile_id": "grok-4-7-balanced",
                             "execution_profile_fingerprint": "f" * 64, "lifecycle": "ACTIVE",
                             "model": "grok-4.7", "reasoning_level": "medium",
                             "supported_modes": ["review"], "tools": []}
        with patch("fleet.resolve_executable", return_value="grok.exe"), \
                patch("fleet.probe_command", return_value={"launchable": True, "exit_code": 0, "output": "ok"}), \
                patch("fleet.routing_v2.readiness_from_probes", return_value={"ready": True, "reasons": []}), \
                patch("fleet.routing_v2.runtime_model_visible", return_value=(True, "visible")), \
                patch("fleet.adaptive.latest_quotas", return_value={}), \
                patch("fleet.grok_acp.build_environment", side_effect=lambda env, **_: env), \
                patch("fleet.grok_acp.run_session", side_effect=run_acp), \
                patch.object(grok_availability, "native_probe") as paid_probe:
            result = fleet.execute_single(
                "Review one fixture file.", profiles=self.profiles, agent_id="grok", mode="review",
                kind="review", complexity="medium", model_override=None, workspace=self.root,
                timeout=60, allow_write=False, native_subagent_limit=0, enable_web=False,
                inherit_api_keys=False, dry_run=False, output_root=self.root / "evidence",
                root=self.root, execution_profile=execution_profile, knowledge_mode="none")
        paid_probe.assert_not_called()  # no paid probe on the preflight or post-run views
        return result

    def cache(self):
        path = qa.cache_path(self.root, self.pool)
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None

    def test_real_run_402_line_persists_a_credit_block(self):
        stderr = fleet.grok_acp.quota_exhaustion_error()
        result = self.run_grok(lambda _cmd, **_: self.acp_result(exit_code=1, stdout="", stderr=stderr))
        self.assertNotEqual(result["status"], "COMPLETED")
        cache = self.cache()
        self.assertEqual((cache["status"], cache["scope"], cache["reason"]),
                         ("exhausted", "credits", "grok-build-balance-exhausted-http402"))
        self.assertEqual(cache["block_step"], 0)
        self.assertNotIn("Renew or top up", json.dumps(cache))  # no stderr text persisted

    def test_verified_native_success_clears_a_diagnosed_unknown(self):
        path = qa.cache_path(self.root, self.pool)
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps({"schema_version": 1, "status": "unknown", "reason": "check-unavailable",
                                    "observed_at": qa.stamp(time.time() - 3600),
                                    "recheck_at": qa.stamp(time.time() + 3600), "block_step": 1}),
                        encoding="utf-8")
        handoff = self.sample_handoff()
        result = self.run_grok(lambda _cmd, **_: self.acp_result(
            exit_code=0, stdout=self.envelope(handoff), stderr=""))
        self.assertEqual(result["model_identity"]["status"], "verified")
        cache = self.cache()
        self.assertEqual((cache["status"], cache["source"], cache["block_step"]),
                         ("available", "native-run", 0))

    def test_success_of_a_run_started_before_a_concurrent_402_keeps_the_block(self):
        handoff = self.sample_handoff()

        def run_acp(_cmd, **_):
            # A concurrent Grok run is refused while this one is still in flight.
            time.sleep(0.05)
            qa.record_rejection(self.root, self.pool, qa.rejection("grok", GROK_402_LINE, 1),
                                lock_factory=fleet.ReleasableKernelLock)
            return self.acp_result(exit_code=0, stdout=self.envelope(handoff), stderr="")
        result = self.run_grok(run_acp)
        self.assertEqual(result["model_identity"]["status"], "verified")
        self.assertEqual(self.cache()["status"], "exhausted")

    def test_never_blocked_pool_gets_no_cache_from_a_success(self):
        handoff = self.sample_handoff()
        self.run_grok(lambda _cmd, **_: self.acp_result(exit_code=0, stdout=self.envelope(handoff), stderr=""))
        self.assertIsNone(self.cache())


if __name__ == '__main__':
    unittest.main()
