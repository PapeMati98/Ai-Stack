import json
import contextlib
from contextlib import contextmanager
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import fleet
import quota_availability as qa
import routing_v2

MESSAGE = ('Quota exhausted: Your token-plan 1-month quota has been exhausted. '
           'The quota will reset at 11-06 16:00:00 UTC. '
           '(cause: insufficient_quota: 429 Your token-plan 1-month quota has been exhausted.)')


def native(message=MESSAGE):
    return json.dumps({'type': 'result', 'subtype': 'error_during_execution',
                       'is_error': True, 'error': {'message': message}})


class QwenMonthlyExhaustionTests(unittest.TestCase):
    @contextmanager
    def retained_state(self):
        # Require the task's explicit fixture root. This helper never deletes
        # anything, including when a test fails or runs outside the parent runner.
        root = Path(os.environ['TEAM_TEST_ROOT'])
        yield Path(tempfile.mkdtemp(prefix='qwen-monthly-', dir=root))

    def rejection(self, message=MESSAGE, observed='2026-10-15T20:33:00Z', **changes):
        event = json.loads(native(message))
        event.update(changes)
        with patch.object(qa.time, 'time', return_value=qa.seconds(observed)):
            return qa.rejection('qwen', '', 1, stdout=json.dumps(event))

    def test_native_monthly_error_is_exhaustion(self):
        value = qa.rejection('qwen', '', 1, stdout=native())
        self.assertIsNotNone(value)
        self.assertEqual(value['status'], 'exhausted')
        self.assertEqual(value['scope'], 'monthly')
        self.assertEqual(value['source'], 'native-provider-error')

    def test_native_reset_replaces_stale_available_and_blocks_dispatch(self):
        now = qa.seconds('2026-10-15T20:33:00Z')
        with self.retained_state() as root:
            qa.record_native_success(root, 'shared-qwen', lock_factory=fleet.ReleasableKernelLock, now=now - 60)
            with patch.object(qa.time, 'time', return_value=now):
                denial = qa.rejection('qwen', '', 1, stdout=native())
            self.assertIsNotNone(denial)
            qa.record_rejection(root, 'shared-qwen', denial, lock_factory=fleet.ReleasableKernelLock, now=now)
            state = qa.check(root, 'shared-qwen', {}, lock_factory=fleet.ReleasableKernelLock, now=now + 86400)
            self.assertEqual(state['reset_at'], '2026-11-06T16:00:00Z')
            self.assertEqual(state['status'], 'exhausted')
            gates = routing_v2.quota_hard_gates({}, qa.overlay({}, state))
            self.assertFalse(next(g['passed'] for g in gates if g['code'] == 'quota-not-exhausted'))

    def test_assistant_quoted_error_is_not_pool_exhaustion(self):
        self.assertIsNone(qa.rejection('qwen', MESSAGE, 1, stdout=json.dumps({'type': 'assistant', 'message': MESSAGE})))

    def test_actual_native_repeated_reset_is_one_deadline(self):
        message = MESSAGE + ' The quota will reset at 11-06 16:00:00 UTC.'
        self.assertEqual(self.rejection(message)['reset_at'], '2026-11-06T16:00:00Z')

    def test_conflicting_malformed_and_absent_resets_do_not_invent_deadlines(self):
        for message in (MESSAGE + ' The quota will reset at 11-07 16:00:00 UTC.',
                        MESSAGE.replace('11-06', '02-30'),
                        MESSAGE.replace('11-06 16:00:00 UTC', 'invalid'),
                        MESSAGE.replace('The quota will reset at 11-06 16:00:00 UTC.', ''),
                        MESSAGE.replace('UTC', 'PST')):
            with self.subTest(message=message):
                result = self.rejection(message)
                self.assertEqual(result['status'], 'exhausted')
                self.assertIsNone(result['reset_at'])

    def test_december_january_rollover_and_expired_nonrollover_reset(self):
        value = self.rejection(MESSAGE.replace('11-06', '01-06'), observed='2026-12-15T20:33:00Z')
        self.assertEqual(value['reset_at'], '2027-01-06T16:00:00Z')
        value = self.rejection(MESSAGE.replace('11-06', '09-06'))
        self.assertEqual(value['reset_at'], '2026-09-06T16:00:00Z')

    def test_wrong_envelopes_are_not_refusals(self):
        for changes in ({'type': 'assistant'}, {'subtype': 'success'}, {'is_error': False},
                        {'is_error': 'true'}, {'error': MESSAGE}, {'error': {'message': 429}}):
            with self.subTest(changes=changes):
                self.assertIsNone(self.rejection(**changes))

    def test_throttling_weekly_daily_and_quoted_native_text_are_not_monthly(self):
        for message in ('HTTP 429: too many requests', MESSAGE.replace('1-month', '1-week'),
                        MESSAGE.replace('1-month', '1-day'), 'Example: ' + MESSAGE,
                        MESSAGE.replace('insufficient_quota: 429', 'rate_limit: 429'),
                        MESSAGE.replace('insufficient_quota: 429', 'insufficient_quota: 4290')):
            with self.subTest(message=message):
                self.assertIsNone(self.rejection(message))

    def test_invalid_exit_and_other_adapters_do_not_match(self):
        for code in (0, None, False, True, '1'):
            with self.subTest(code=code):
                self.assertIsNone(qa.rejection('qwen', '', code, stdout=native()))
        for adapter in ('kimi', 'claude', 'muse', 'grok', 'codebuddy'):
            with self.subTest(adapter=adapter):
                self.assertIsNone(qa.rejection(adapter, '', 1, stdout=native()))

    def test_multiple_or_nested_terminal_events_are_ambiguous(self):
        for output in (native() + '\n' + native(),
                       native() + '\n' + json.dumps({'type': 'result', 'subtype': 'success', 'is_error': False}),
                       json.dumps({'type': 'tool_result', 'content': json.loads(native())}),
                       json.dumps({'type': 'assistant', 'message': {'content': native()}})):
            with self.subTest(output=output):
                self.assertIsNone(qa.rejection('qwen', '', 1, stdout=output))

    def test_monthly_denial_is_not_cleared_by_weekly_headroom_and_expires_at_reset(self):
        now = qa.seconds('2026-10-15T20:33:00Z')
        with self.retained_state() as root:
            denial = self.rejection()
            qa.record_rejection(root, 'pool', denial, lock_factory=fleet.ReleasableKernelLock, now=now)
            prior = {'status': 'fresh', 'authority': 'authoritative', 'headroom': 1.0,
                     'observed_at': qa.stamp(now + 60)}
            before = qa.check(root, 'pool', prior, lock_factory=fleet.ReleasableKernelLock,
                              now=qa.seconds(denial['reset_at']) - 1)
            self.assertEqual(before['status'], 'exhausted')
            at = qa.check(root, 'pool', prior, lock_factory=fleet.ReleasableKernelLock,
                          now=qa.seconds(denial['reset_at']))
            self.assertEqual(at['status'], 'unknown')
            self.assertEqual(at['reason'], 'reset-passed-no-probe')

    def test_missing_reset_uses_fixed_backoff_not_permanent_block(self):
        now = qa.seconds('2026-10-15T20:33:00Z')
        denial = self.rejection(MESSAGE.replace('The quota will reset at 11-06 16:00:00 UTC.', ''))
        with self.retained_state() as root:
            qa.record_rejection(root, 'pool', denial, lock_factory=fleet.ReleasableKernelLock, now=now)
            before = qa.check(root, 'pool', {}, lock_factory=fleet.ReleasableKernelLock, now=now + 1)
            self.assertEqual(before['status'], 'exhausted')
            after = qa.check(root, 'pool', {}, lock_factory=fleet.ReleasableKernelLock,
                             now=now + qa.COSTLY_BACKOFF_S[0])
            self.assertEqual(after['status'], 'unknown')

    def test_provider_failure_reason_qwen_monthly_quota_exhausted(self):
        reason = fleet.provider_failure_reason(
            adapter_id="qwen", stdout=native(), stderr="", exit_code=1, timed_out=False
        )
        self.assertIn("QWEN MONTHLY QUOTA EXHAUSTED", reason)
        self.assertNotIn("PROVIDER FAILURE", reason)
        self.assertIn("handoff", reason.lower())

    def test_provider_failure_reason_negative_controls(self):
        echo = json.dumps({"type": "assistant", "message": MESSAGE})
        reason = fleet.provider_failure_reason(
            adapter_id="qwen", stdout=echo, stderr="", exit_code=1, timed_out=False
        )
        self.assertIn("PROVIDER FAILURE", reason)
        self.assertNotIn("QWEN MONTHLY QUOTA EXHAUSTED", reason)

        tool_echo = json.dumps({"type": "tool_result", "content": json.loads(native())})
        reason = fleet.provider_failure_reason(
            adapter_id="qwen", stdout=tool_echo, stderr="", exit_code=1, timed_out=False
        )
        self.assertIn("PROVIDER FAILURE", reason)
        self.assertNotIn("QWEN MONTHLY QUOTA EXHAUSTED", reason)

        generic_429 = native("HTTP 429: too many requests")
        reason = fleet.provider_failure_reason(
            adapter_id="qwen", stdout=generic_429, stderr="", exit_code=1, timed_out=False
        )
        self.assertIn("PROVIDER FAILURE", reason)
        self.assertNotIn("QWEN MONTHLY QUOTA EXHAUSTED", reason)

        reason_exit0 = fleet.provider_failure_reason(
            adapter_id="qwen", stdout=native(), stderr="", exit_code=0, timed_out=False
        )
        self.assertIsNone(reason_exit0)

        success_env = json.dumps({"type": "result", "subtype": "success", "is_error": False})
        reason = fleet.provider_failure_reason(
            adapter_id="qwen", stdout=success_env, stderr="", exit_code=1, timed_out=False
        )
        self.assertIn("PROVIDER FAILURE", reason)
        self.assertNotIn("QWEN MONTHLY QUOTA EXHAUSTED", reason)

        for other in ("claude", "grok", "mistral", "codebuddy", "kimi"):
            with self.subTest(adapter=other):
                reason = fleet.provider_failure_reason(
                    adapter_id=other, stdout=native(), stderr="", exit_code=1, timed_out=False
                )
                if reason is not None:
                    self.assertNotIn("QWEN MONTHLY QUOTA EXHAUSTED", reason)

    def test_parent_timeout_priority(self):
        reason = fleet.provider_failure_reason(
            adapter_id="qwen", stdout=native(), stderr="", exit_code=1, timed_out=True
        )
        self.assertIn("PROVIDER FAILURE", reason)
        self.assertNotIn("QWEN MONTHLY QUOTA EXHAUSTED", reason)
        outcome = fleet.provider_transport_outcome(
            "qwen", stdout=native(), exit_code=1, terminal_reason=None, timed_out=True
        )
        self.assertEqual(outcome, {"status": "partial", "reason": "parent-timeout"})

    def test_provider_transport_outcome_monthly_exhaustion(self):
        outcome = fleet.provider_transport_outcome(
            "qwen", stdout=native(), exit_code=1, terminal_reason=None, timed_out=False
        )
        self.assertEqual(outcome, {"status": "failed", "reason": "qwen-monthly-quota-exhausted"})

    def test_execute_single_qwen_monthly_quota_exhaustion(self):
        profiles = fleet.load_profiles(fleet.DEFAULT_PROFILES)
        with self.retained_state() as root:
            child = {
                "exit_code": 1,
                "timed_out": False,
                "stdout": native(),
                "stderr": "",
                "model_tool_calls": 0,
                "tool_budget_exceeded": False,
            }
            projection = {
                "handoff": None,
                "candidate_text": None,
                "terminal_reason": None,
                "source": None,
                "error": "Qwen terminal result has no valid handoff",
            }
            patches = [
                patch.dict(os.environ, {
                    "AGENT_FLEET_DEPTH": "0",
                    "AGENT_FLEET_OFFICE": "0",
                    "AGENT_FLEET_BILLING_MODE_PATH": str(root / "missing-billing.json"),
                }),
                patch.object(fleet, "resolve_executable", return_value="synthetic-qwen.exe"),
                patch.object(fleet, "probe_command", return_value={"launchable": True, "exit_code": 0, "output": "ok"}),
                patch.object(fleet, "profile_runtime_environment", return_value={}),
                patch.object(fleet, "profile_credential_status", return_value={"ready": True}),
                patch.object(fleet, "merge_credential_readiness", return_value={"ready": True, "reasons": []}),
                patch.object(fleet.routing_v2, "readiness_from_probes", return_value={"ready": True, "reasons": []}),
                patch.object(fleet.routing_v2, "runtime_model_visible", return_value=(True, "visible")),
                patch.object(fleet, "fleet_runtime_attestation", return_value={"status": "VERIFIED"}),
                patch.object(fleet, "read_usage", return_value=[]),
                patch.object(fleet.adaptive, "latest_quotas", return_value={}),
                patch.object(fleet.quota_collectors, "collect_profile_quotas", return_value=({}, {})),
                patch.object(fleet, "qwen_model_registration", return_value={"status": "registered", "registry_source": "fixture"}),
                patch.object(fleet, "project_provider_handoff", return_value=projection),
                patch.object(fleet, "qwen_model_identity", return_value={"status": "unavailable", "expected_model": "qwen3.8-max", "observed_models": [], "error": "Native execution refused"}),
                patch.object(fleet, "run_provider_child", return_value={"child": child}),
            ]
            with contextlib.ExitStack() as stack:
                for p in patches:
                    stack.enter_context(p)
                result = fleet.execute_single(
                    "Synthetic Qwen monthly refusal fixture.",
                    profiles=profiles,
                    agent_id="qwen",
                    mode="review",
                    kind="review",
                    complexity="low",
                    model_override="qwen3.8-max",
                    workspace=root,
                    timeout=5,
                    allow_write=False,
                    native_subagent_limit=0,
                    enable_web=False,
                    inherit_api_keys=False,
                    dry_run=False,
                    output_root=root / "evidence",
                    root=root,
                    knowledge_mode="none",
                )
            self.assertEqual(result["status"], "FAILED")
            self.assertEqual(result["completion_reason"], "qwen-monthly-quota-exhausted")
            record = json.loads(Path(result["usage_record"]).read_text(encoding="utf-8"))
            self.assertEqual(record["run_status"], "FAILED")
            self.assertEqual(record["completion_reason"], "qwen-monthly-quota-exhausted")
            expected_transport = {"status": "failed", "reason": "qwen-monthly-quota-exhausted"}
            self.assertEqual(result["transport_outcome"], expected_transport)
            attempt = result["provider_attempts"][0]
            self.assertEqual(attempt["transport_outcome"], expected_transport)
            self.assertIn("QWEN MONTHLY QUOTA EXHAUSTED", result["handoff_parse_error"])
            self.assertNotIn("PROVIDER FAILURE", result["handoff_parse_error"])
            self.assertIn("QWEN MONTHLY QUOTA EXHAUSTED", attempt["handoff_error"])
            self.assertNotIn("PROVIDER FAILURE", attempt["handoff_error"])
            self.assertEqual(result["exit_code"], 1)
            self.assertIsNone(result["handoff"])

    def test_shared_pool_blocks_both_qwen_aliases_without_native_calls(self):
        now = qa.seconds('2026-10-15T20:33:00Z')
        with self.retained_state() as root:
            with patch.object(qa.time, 'time', return_value=now):
                denial = qa.rejection('qwen', '', 1, stdout=native())
            self.assertIsNotNone(denial)
            qa.record_rejection(root, 'bailian-shared-flat-rate', denial, lock_factory=fleet.ReleasableKernelLock, now=now)
            state = qa.check(root, 'bailian-shared-flat-rate', {}, lock_factory=fleet.ReleasableKernelLock, now=now + 60)
            self.assertEqual(state['status'], 'exhausted')
            profiles = fleet.load_profiles(fleet.DEFAULT_PROFILES)
            qwen_profiles = profiles['qwen']['execution_profiles']
            for profile_key in ('qwen-3-8-max', 'qwen-3-8-flash-next-product'):
                ep = qwen_profiles[profile_key]
                gates = routing_v2.quota_hard_gates(ep, qa.overlay({}, state))
                quota_gate = next(g for g in gates if g['code'] == 'quota-not-exhausted')
                self.assertFalse(quota_gate['passed'])


if __name__ == "__main__":
    unittest.main()


