import importlib.util
import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'payload' / 'agent-fleet' / 'scripts'))
import fleet
import routing_v2


class RoutingDelta(unittest.TestCase):
    def setUp(self):
        self.profiles = fleet.load_profiles(ROOT / 'payload/agent-fleet/references/profiles.json')

    def selected(self, agent, mode, complexity, **overrides):
        return routing_v2.resolve_execution_profile(
            agent, self.profiles[agent], complexity=complexity, mode=mode,
            required_tools=[], **overrides)['model']

    def test_astra_high_planning_and_review(self):
        for mode in ('advisory', 'review'):
            with self.subTest(mode=mode):
                self.assertEqual(self.selected('codex', mode, 'high'), 'gpt-6-astra')

    def test_gpt6_default_routing(self):
        for mode in ('advisory', 'review', 'implement'):
            with self.subTest(mode=mode):
                self.assertEqual(self.selected('codex', mode, 'low'), 'gpt-6-luna')
                self.assertEqual(self.selected('codex', mode, 'medium'), 'gpt-6-sol')
        self.assertEqual(self.selected('codex', 'implement', 'high'), 'gpt-6-sol')

    def test_gpt6_profiles_limited_and_5_6_explicit(self):
        execution = self.profiles['codex']['execution_profiles']
        for profile_id in ('codex-gpt-6-luna', 'codex-gpt-6-sol-medium',
                           'codex-gpt-6-sol-high'):
            self.assertEqual(execution[profile_id]['lifecycle'], 'LIMITED')
            self.assertLessEqual(execution[profile_id]['evidence_confidence'], 0.3)
        for model in ('gpt-5.6-luna', 'gpt-5.6-terra', 'gpt-5.6-sol'):
            self.assertEqual(self.selected('codex', 'implement', 'medium',
                                           model_override=model), model)

    def test_critical_high_implementation_passes_risk_ceiling(self):
        execution = routing_v2.resolve_execution_profile(
            'codex', self.profiles['codex'], complexity='high',
            mode='implement', required_tools=['write'])
        self.assertEqual(execution['execution_profile_id'], 'codex-gpt-6-sol-high')
        self.assertEqual(execution['max_risk'], 'critical')
        assessment = json.loads((ROOT / 'payload/agent-fleet/references/task-profile-v2-example.json').read_text())
        assessment['risk'] = 'critical'
        gates = routing_v2.hard_gates(
            execution, profile=self.profiles['codex'], assessment=assessment,
            readiness={'ready': True, 'reasons': []},
            quota={'status': 'unknown', 'headroom': None}, mode='implement')
        risk_gate = next(gate for gate in gates if gate['code'] == 'risk-ceiling')
        self.assertTrue(risk_gate['passed'], risk_gate)

    def test_gpt6_limited_profiles_pass_explicit_lifecycle_gate(self):
        assessment = json.loads((ROOT / 'payload/agent-fleet/references/task-profile-v2-example.json').read_text())
        for profile_id in ('codex-gpt-6-luna', 'codex-gpt-6-sol-medium',
                           'codex-gpt-6-sol-high'):
            with self.subTest(profile_id=profile_id):
                execution = routing_v2.resolve_execution_profile(
                    'codex', self.profiles['codex'], complexity='high',
                    mode='implement', required_tools=['write'],
                    execution_profile_override=profile_id)
                gates = routing_v2.hard_gates(
                    execution, profile=self.profiles['codex'], assessment=assessment,
                    readiness={'ready': True, 'reasons': []},
                    quota={'status': 'unknown', 'headroom': None}, mode='implement')
                lifecycle = next(gate for gate in gates if gate['code'] == 'lifecycle')
                self.assertTrue(lifecycle['passed'], lifecycle)
                self.assertTrue(routing_v2.lifecycle_allows_explicit_run('LIMITED'))

    def test_security_review_stays_on_dedicated_gpt5_6_sol(self):
        execution = self.profiles['codex']['execution_profiles']
        dedicated = execution['codex-gpt-5-6-sol-security-review']
        self.assertEqual(dedicated['model'], 'gpt-5.6-sol')
        self.assertEqual(dedicated['lifecycle'], 'ACTIVE')
        self.assertEqual(dedicated['capabilities']['security-review'], 98)
        for profile_id in ('codex-gpt-6-sol-medium', 'codex-gpt-6-sol-high'):
            self.assertEqual(
                execution[profile_id]['capabilities'].get('security-review', 0), 0)
        codex_auditors = [
            item for item in routing_v2.qualified_security_review_executions(self.profiles)
            if item['agent'] == 'codex'
        ]
        self.assertEqual(
            [item['execution_profile_id'] for item in codex_auditors],
            ['codex-gpt-5-6-sol-security-review'])

    def test_radar_mapping_candidate_has_no_benchmark_claim(self):
        mapping = json.loads((ROOT / 'payload/model-radar/references/arena-model-map.json').read_text())
        for model in ('gpt-6-luna', 'gpt-6-sol'):
            row = next(item for item in mapping['mappings']
                       if item['profile'] == 'codex' and item['model'] == model)
            self.assertEqual(row['confidence'], 'candidate')
            self.assertEqual(row['arena_ids'], [model])
        snapshot = json.loads((ROOT / 'payload/model-radar/MODEL-BENCHMARK-SNAPSHOT.json').read_text())
        for model in ('gpt-6-luna', 'gpt-6-sol'):
            self.assertNotIn(f'codex@{model}', snapshot['models'])

    def test_qwen_max_not_legacy_glm(self):
        for mode in ('advisory', 'review', 'implement'):
            self.assertEqual(self.selected('qwen', mode, 'high'), 'qwen3.8-max')

    def test_fable_unchanged(self):
        self.assertEqual(self.selected('claude', 'advisory', 'high'), 'claude-opus-5-5')

    def test_explicit_sol_still_supported(self):
        self.assertEqual(self.selected('codex', 'review', 'high', model_override='gpt-5.6-sol'), 'gpt-5.6-sol')


if __name__ == '__main__':
    unittest.main()
