"""Owner B1/B2/B4 bridge tests: no provider initialization or dispatch."""
import copy
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

import model_tiers
import routing_advisor
import todo_fleet
import todo_graph
import todo_pair
from test_model_tiers import CATALOG
from test_todo_fleet import FakeFleet

# Python 3.13 mkdir(0700) replaces the inherited Windows sandbox ACL. Retain
# that ACL only for disposable synthetic fixtures, including existing tests.
if os.name == 'nt' and not getattr(os.mkdir, '_routing_fixture_acl', False):
    _mkdir = os.mkdir
    def fixture_mkdir(path, mode=0o777, *, dir_fd=None):
        root = Path(os.environ['TEAM_TEST_ROOT']).resolve()
        if mode == 0o700 and Path(path).resolve().is_relative_to(root):
            mode = 0o777
        return _mkdir(path, mode, dir_fd=dir_fd)
    fixture_mkdir._routing_fixture_acl = True
    os.mkdir = fixture_mkdir

# Synthetic routing runs must not publish TODO journals into the real user home.
# Scoped to this module; the caller's explicit telemetry setting is restored on
# teardown. Child processes inherit the scoped value. Nothing changes at import.
_test_telemetry = patch.dict(os.environ, {"AGENT_TODO_TELEMETRY": "off"})


def setUpModule():
    _test_telemetry.start()


def tearDownModule():
    _test_telemetry.stop()


class RoutingRecheckTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix='routing-b-', dir=os.environ['TEAM_TEST_ROOT']))
        catalog = self.root / 'catalog.json'
        catalog.write_text(json.dumps(CATALOG), encoding='utf-8')
        environment = patch.dict(os.environ, {
            'AGENT_MODEL_CATALOG': str(catalog),
            'AGENT_FLEET_RUNS_DIR': str(self.root / 'runs'),
            'AGENT_MODEL_TIERS_INDEX': str(self.root / 'usage-index.json'),
            'AGENT_MODEL_TIERS_PICKS': str(self.root / 'picks.json'),
            'AGENT_MODEL_AVAILABILITY': str(self.root / 'availability.json'),
        })
        environment.start()
        self.addCleanup(environment.stop)
        self.profiles = {'opencode': {'supported_modes': ['advisory', 'review']},
                         'maker': {'supported_modes': ['implement', 'advisory', 'review']}}
        c = lambda a: {'agent': a, 'model': 'm-model', 'score': 85, 'reason': 'fixture'}
        self.task = {'id': 'b', 'text': 'fixture', 'scope': ['src'], 'covers': ['b'],
                     'done_when': ['behavior'], 'checks': [['python', '-B', '-c', 'pass']],
                     'maker': [c('opencode')], 'reviewer': [c('maker')]}
        self.plan = {'version': 1, 'goal': 'B', 'workspace': str(self.root), 'max_parallel': 1,
                     'requirements': ['b'], 'planner': {'model': 'parent', 'source': 'owner'},
                     'tasks': [self.task]}

    def test_B1_validate_rejects_unsupported_declared_maker_and_preserves_review(self):
        with self.assertRaisesRegex(ValueError, 'maker opencode lacks supported_modes=implement'):
            todo_graph.validate(self.plan, profiles=self.profiles)
        self.task['maker'], self.task['reviewer'] = self.task['reviewer'], self.task['maker']
        self.assertIs(todo_graph.validate(self.plan, profiles=self.profiles), self.plan)
        self.task['maker'], self.task['reviewer'] = self.task['reviewer'], self.task['maker']
        self.task['mode'] = 'advisory'
        self.assertIs(todo_graph.validate(self.plan, profiles=self.profiles), self.plan)

    def test_B1_native_run_pair_and_validate_fail_before_dispatch(self):
        runtime = self.root / 'runtime'
        path = runtime / 'agent-fleet/references/profiles.json'
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps({'profiles': self.profiles}), encoding='utf-8')
        todo_path = self.root / 'todo.json'
        todo_path.write_text(json.dumps(self.plan), encoding='utf-8')
        with patch.object(todo_fleet, 'resolve_fleet_root', return_value=runtime), \
                patch.object(todo_graph.FleetAdapter, 'invoke') as dispatch:
            for operation in (lambda: todo_fleet.validate_native_plan(self.plan),
                              lambda: todo_graph.main(['validate', '--todo', str(todo_path)]),
                              lambda: todo_graph.run(self.plan, self.root / 'graph'),
                              lambda: todo_pair.run_pair(self.plan, self.root / 'pair')):
                with self.assertRaisesRegex(ValueError, 'supported_modes=implement'):
                    operation()
            dispatch.assert_not_called()

    def test_B1_bridge_cannot_substitute_malformed_declared_plan(self):
        fleet = FakeFleet()
        fleet.DEFAULT_PROFILES = self.profiles
        request = {'goal': 'B', 'workspace': str(self.root), 'role': 'maker', 'task': self.task}
        with patch.object(fleet, 'live_readiness') as ready, patch.object(fleet, 'current_quota_views') as quotas:
            with self.assertRaisesRegex(ValueError, 'supported_modes=implement'):
                todo_fleet.execute(request, self.root / 'out', fleet=fleet)
            ready.assert_not_called()
            quotas.assert_not_called()
        self.assertEqual(fleet.execute_calls, [])

    def test_B1_bridge_review_cannot_hide_invalid_declared_maker(self):
        fleet = FakeFleet()
        fleet.DEFAULT_PROFILES = self.profiles
        request = {'goal': 'B', 'workspace': str(self.root), 'role': 'reviewer', 'task': self.task}
        with patch.object(fleet, 'live_readiness') as ready:
            with self.assertRaisesRegex(ValueError, 'maker opencode lacks supported_modes=implement'):
                todo_fleet.execute(request, self.root / 'out', fleet=fleet)
            ready.assert_not_called()
        self.assertEqual(fleet.execute_calls, [])

    def test_B2_pool_block_applies_even_with_tier_rollback(self):
        fleet = FakeFleet(quota={'availability': {'status': 'recheck-required',
                                                'source': 'native-provider-attempt'},
                                'availability_blocked': True})
        self.task.update(mode='advisory', maker=[{'agent': 'maker', 'model': 'm-model',
                                                'score': 85, 'reason': 'fixture'}])
        request = {'goal': 'B', 'workspace': str(self.root), 'role': 'maker', 'task': self.task}
        (self.root / 'out').mkdir()
        with patch.dict(os.environ, {'AGENT_GRAPH_TIER_ROUTING': '0'}):
            result = todo_fleet.execute(request, self.root / 'out', fleet=fleet)
        self.assertEqual(result['status'], 'BLOCKED')
        self.assertEqual(fleet.execute_calls, [])

    def test_B4_unknown_idle_pool_does_not_beat_fresh_qualified_pool_on_recency(self):
        catalog_path = self.root / 'catalog.json'
        catalog_path.write_text(json.dumps(CATALOG), encoding='utf-8')
        catalog = model_tiers.load_catalog(catalog_path)
        cands = [{'agent': 'busy', 'model': 'k-busy', 'score': 85, 'availability': 'available'},
                 {'agent': 'idle', 'model': 'k-idle', 'score': 80, 'availability': 'unknown'}]
        ordered = model_tiers.order_candidates(cands, role='maker', task={}, catalog=catalog,
                                              usage={('busy', 'k-busy'): 100})
        self.assertEqual(ordered[0]['agent'], 'busy')
        self.assertTrue(ordered[1]['allowed'])
        self.assertEqual(ordered[1]['availability'], 'unknown')
        self.assertEqual(ordered[1]['recency_penalty'], 0)
        cands[1]['availability'] = 'available'
        ordered = model_tiers.order_candidates(cands, role='maker', task={}, catalog=catalog,
                                              usage={('busy', 'k-busy'): 100})
        self.assertEqual(ordered[0]['agent'], 'idle')

    def test_B4_fresh_native_evidence_without_numeric_quota_and_boundary(self):
        now = int(time.time())
        stamp = lambda x: __import__('datetime').datetime.fromtimestamp(x, __import__('datetime').timezone.utc).isoformat()
        row = {'status': 'stale', 'headroom': None,
               'availability': {'status': 'available', 'source': 'native-run', 'observed_at': stamp(now - 86399)}}
        self.assertEqual(model_tiers.availability_state(row, now=now), 'available')
        self.assertIsNone(row['headroom'])
        row['availability']['observed_at'] = stamp(now - 86400)
        self.assertEqual(model_tiers.availability_state(row, now=now), 'unknown')
        row['availability'].update(status='recheck-required', source='native-provider-attempt')
        self.assertEqual(model_tiers.availability_state(row, now=now), 'recheck-required')

    def test_B5_graph_ignores_local_ledger_available_or_blocked_claims(self):
        now = int(time.time())
        stamp = __import__('datetime').datetime.fromtimestamp(now, __import__('datetime').timezone.utc).isoformat()
        for status in ('available', 'exhausted', 'recheck-required'):
            row = {'source': 'local-token-ledger', 'status': 'fresh', 'current': True,
                   'authority': 'authoritative', 'availability': {
                       'status': status, 'source': 'local-token-ledger', 'observed_at': stamp}}
            self.assertEqual(model_tiers.availability_state(row, now=now), 'unknown')
        row['availability'].update(source='native-provider-error', status='exhausted')
        self.assertEqual(model_tiers.availability_state(row, now=now), 'exhausted')

    def test_B4_advisor_uses_native_freshness(self):
        from types import SimpleNamespace
        fleet = SimpleNamespace(routing_v2=SimpleNamespace(
            quota_hard_gates=lambda *a: [], RoutingV2Error=ValueError,
            resolve_execution_profile=lambda *a, **k: {'lifecycle': 'ACTIVE', 'model_provider_id': 'fixture'}))
        profile = {'adapter': 'muse', 'routing_enabled': True, 'advisor_billing': 'subscription',
                   'models': {'low': 'muse-spark-1.3'}, 'supported_modes': ['advisory', 'review']}
        stamp = __import__('datetime').datetime.now(__import__('datetime').timezone.utc).isoformat()
        quota = {'status': 'stale', 'availability': {'status': 'available', 'source': 'native-run', 'observed_at': stamp}}
        result = routing_advisor.make_snapshot(fleet, {'muse': profile}, {'muse': quota},
                                               {'muse': {'ready': True}}, {'mode': 'advisory'})
        self.assertTrue(result['candidates'])
        self.assertTrue(all(c['status'] == 'available' for c in result['candidates']))
        self.assertTrue(all(c['quota_remaining'] is None for c in result['candidates']))
        quota['availability'].update(status='recheck-required', source='native-provider-attempt')
        result = routing_advisor.make_snapshot(fleet, {'muse': profile}, {'muse': quota},
                                               {'muse': {'ready': True}}, {'mode': 'advisory'})
        self.assertTrue(result['candidates'])
        self.assertTrue(all(c['status'] == 'unknown' and not c['enabled'] for c in result['candidates']))
        self.assertTrue(any('pool-recheck-required' in row['reason']
                            for row in result['local_diagnostics']))

    def test_B4_substitution_excludes_recheck_required_pools(self):
        path = self.root / 'catalog.json'
        path.write_text(json.dumps(CATALOG), encoding='utf-8')
        rows = model_tiers.propose(role='maker', min_tier='K',
                                  catalog=model_tiers.load_catalog(path), usage={},
                                  availability={'idle': 'recheck-required', 'busy': 'available'}, limit=None)
        self.assertNotIn('idle', [c['agent'] for c in rows])
        self.assertEqual(rows[0]['agent'], 'busy')


if __name__ == '__main__':
    unittest.main()
