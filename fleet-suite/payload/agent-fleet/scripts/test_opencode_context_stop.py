"""Owned synthetic private-child tests; no provider, credentials, network or deletion."""
import contextlib
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

import fleet
import routing_v2
from process_supervision import ContextEventUnreadable
from test_fleet_context_budget import step_finish


class OpenCodeContextStopTests(unittest.TestCase):
    def test_opt_in_only_and_existing_defaults_unchanged(self):
        self.assertIsNone(fleet.profile_context_stop_tokens({'adapter': 'opencode'}))
        self.assertEqual(fleet.profile_context_stop_tokens({'adapter': 'opencode', 'context_stop_tokens': 64000}), 64000)
        for invalid in (None, True, 63999, 4000001, 64000.0, '64000'):
            with self.subTest(invalid=invalid), self.assertRaises(fleet.FleetError):
                fleet.profile_context_stop_tokens({'adapter': 'opencode', 'context_stop_tokens': invalid})
        for other in ('grok', 'kimi', 'codex', 'claude', 'muse'):
            with self.subTest(adapter=other), self.assertRaises(fleet.FleetError):
                fleet.profile_context_stop_tokens({'adapter': other, 'context_stop_tokens': 64000})
        profiles = fleet.load_profiles(fleet.DEFAULT_PROFILES)
        self.assertNotIn('context_stop_tokens', profiles['opencode'])
        self.assertEqual(profiles['mimo']['context_stop_tokens'], 200000)

    def test_counts_fresh_and_both_cache_kinds_not_output_reasoning_or_total(self):
        event = step_finish('numeric', fresh=1000, read=62000, write=1001, output=999999, reasoning=888888)
        event['part']['tokens']['total'] = 7777777
        self.assertEqual(fleet.opencode_stream_context_tokens(json.dumps(event)), 64001)
        self.assertIsNone(fleet.opencode_stream_context_tokens('{"type":"result","usage":{"input_tokens":999999}}'))
        self.assertIsNone(fleet.opencode_stream_context_tokens('{"type":"text","text":"64001"}'))

    def test_unknown_malformed_and_invalid_counter_types_are_not_zero(self):
        string_input = step_finish('bad-string', fresh=0, read=1)
        string_input['part']['tokens']['input'] = '64000'
        for line in ('{"type":"step_finish",', '{"type":"step_finish"}',
                     json.dumps(step_finish('bad', fresh=True, read=1)),
                     json.dumps(step_finish('bad', fresh=-1, read=1)),
                     json.dumps(string_input)):
            with self.subTest(line=line), self.assertRaises(ContextEventUnreadable) as err:
                fleet.opencode_stream_context_tokens(line)
            self.assertIn('OpenCode', str(err.exception))

    def test_mimo_shared_parser_error_label_is_preserved(self):
        with self.assertRaises(ContextEventUnreadable) as err:
            fleet.mimo_stream_context_tokens('{"type":"step_finish"}')
        self.assertIn('MiMo', str(err.exception))

    def test_actual_saved_native_numeric_events_match_prior_measurement(self):
        events = json.loads(Path(__file__).with_name('opencode-native-numeric-events.json').read_bytes())
        inputs = [fleet.opencode_stream_context_tokens(json.dumps(v)) for v in events]
        self.assertEqual(inputs, [7527, 9881, 13437, 18246])

    def fixture(self, *, enabled=True, input_tokens=64001, model='glm-5.3-flash', malformed=False):
        root = Path(tempfile.mkdtemp(prefix='opencode-stop-owned-'))
        profiles = fleet.load_profiles(fleet.DEFAULT_PROFILES)
        if enabled:
            profiles['opencode']['context_stop_tokens'] = 64000
        execution = routing_v2.resolve_execution_profile('opencode', profiles['opencode'],
            complexity='low', mode='review', required_tools=[])
        events = [step_finish('first', fresh=1000, read=0),
                  step_finish('second', fresh=input_tokens, read=0)]
        if malformed:
            events[-1]['part']['tokens'].pop('input')
        native_identity = f'level=INFO message=stream providerID=zai-coding-plan modelID={model} session.id=ses-synthetic'
        code = (
            'from pathlib import Path\nimport json,sys,time\n'
            "Path('retained-work.txt').write_text('owned partial work')\n"
            f'print({native_identity!r},file=sys.stderr,flush=True)\n'
            f'events={events!r}\n'
            'for event in events: print(json.dumps(event),flush=True); time.sleep(0.03)\n'
            'time.sleep(0.8)\n'
            "Path('late.txt').write_text('child reached completion')\n"
        )
        original_child = fleet.run_provider_child
        launches = []
        def substitute_synthetic(command, **kwargs):
            launches.append(kwargs.get('max_context_tokens'))
            return original_child([sys.executable, '-u', '-c', code], **kwargs)
        env = dict(os.environ, AGENT_FLEET_DEPTH='0', AGENT_FLEET_OFFICE='0',
            AGENT_FLEET_BILLING_MODE_PATH=str(root / 'missing-billing.json'))
        patches = [
            mock.patch.dict(os.environ, env, clear=True),
            mock.patch.object(fleet, 'resolve_executable', return_value=str(root / 'synthetic.exe')),
            mock.patch.object(fleet, 'probe_command', return_value={'launchable': True, 'exit_code': 0, 'output': 'ok'}),
            mock.patch.object(fleet, 'profile_credential_status', return_value={'ready': True}),
            mock.patch.object(fleet, 'merge_credential_readiness', return_value={'ready': True, 'reasons': []}),
            mock.patch.object(fleet, 'apply_profile_credential'),
            mock.patch.object(fleet, 'validate_opencode_resolved_config', return_value={'status': 'synthetic'}),
            mock.patch.object(fleet, 'remove_guarded_opencode_home'),
            mock.patch.object(routing_v2, 'readiness_from_probes', return_value={'ready': True, 'reasons': []}),
            mock.patch.object(routing_v2, 'runtime_model_visible', return_value=(True, 'visible')),
            mock.patch.object(routing_v2, 'quota_hard_gates', return_value=[]),
            mock.patch.object(fleet, 'current_quota_views', return_value=({'opencode': {'status': 'fresh', 'headroom': 1.0, 'confidence': 1.0}}, {})),
            mock.patch.object(fleet, 'run_provider_child', side_effect=substitute_synthetic),
        ]
        with contextlib.ExitStack() as stack:
            for patch in patches:
                stack.enter_context(patch)
            result = fleet.execute_single('Owned synthetic numeric fixture only.', profiles=profiles,
                agent_id='opencode', mode='review', kind='review', complexity='low', model_override=None,
                workspace=root, timeout=5, allow_write=False, native_subagent_limit=0, enable_web=False,
                inherit_api_keys=False, dry_run=False, output_root=root / 'evidence', root=root,
                execution_profile=execution, knowledge_mode='none')
        record = json.loads(Path(result['usage_record']).read_text(encoding='utf-8'))
        return result, record, root, launches

    def test_actual_private_process_stop_is_partial_and_work_retained(self):
        result, record, root, launches = self.fixture()
        self.assertEqual(launches, [64000], 'No automatic retry or same-history continuation')
        self.assertEqual(result['status'], 'PARTIAL')
        self.assertEqual(result['exit_code'], 127)
        self.assertEqual(record['completion_reason'], 'observed-context-limit-needs-parent-review')
        self.assertEqual(record['context_guard']['status'], 'exceeded')
        self.assertEqual(record['context_guard']['observed_peak_tokens'], 64001)
        self.assertEqual(record['model_identity']['status'], 'verified')
        self.assertEqual((root / 'retained-work.txt').read_text(), 'owned partial work')
        self.assertFalse((root / 'late.txt').exists())
        self.assertTrue(result['parent_acceptance_required'])

    def test_wrong_model_cannot_be_promoted_to_partial_by_the_stop(self):
        result, record, root, _ = self.fixture(model='foreign-model')
        self.assertEqual(record['model_identity']['status'], 'mismatch')
        self.assertNotEqual(result['status'], 'PARTIAL')
        self.assertNotEqual(result['status'], 'COMPLETED')
        self.assertTrue(record['context_guard']['exceeded'])
        self.assertTrue((root / 'retained-work.txt').exists())

    def test_absent_limit_leaves_old_behavior_and_unknown_is_not_below(self):
        result, record, root, launches = self.fixture(enabled=False)
        self.assertEqual(launches, [None])
        self.assertEqual(result['exit_code'], 0)
        self.assertEqual(record['context_guard']['status'], 'not-configured')
        self.assertTrue((root / 'late.txt').exists())

    def test_equal_boundary_runs_to_completion_without_auto_accept(self):
        result, record, root, _ = self.fixture(input_tokens=64000)
        self.assertEqual(result['exit_code'], 0)
        self.assertEqual(record['context_guard']['status'], 'observed-below')
        self.assertTrue((root / 'late.txt').exists())
        self.assertNotEqual(result['status'], 'COMPLETED', 'No valid final handoff in this fixture')

    def test_missing_measurement_cannot_prove_below_limit(self):
        result, record, root, _ = self.fixture(malformed=True)
        self.assertEqual(result['exit_code'], 0)
        self.assertEqual(record['context_guard']['status'], 'unknown')
        self.assertTrue(record['context_guard']['scan_incomplete'])
        self.assertEqual(record['context_guard']['observed_peak_tokens'], 1000)
        self.assertTrue((root / 'late.txt').exists())

    def test_other_adapter_refused_before_any_private_child_is_launched(self):
        with mock.patch.object(fleet.subprocess, 'Popen') as spawn:
            with self.assertRaises(fleet.FleetError):
                fleet.run_bounded_child([sys.executable], cwd=Path.cwd(), env={}, stdin_text=None,
                    timeout=1, adapter_id='grok', max_model_tool_calls=None, max_context_tokens=64000)
            spawn.assert_not_called()


if __name__ == '__main__':
    unittest.main()
