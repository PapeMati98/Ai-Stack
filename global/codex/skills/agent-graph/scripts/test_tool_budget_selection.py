"""Task-local product reproduction using real bridge and synthetic provider views."""
import json, unittest
from unittest.mock import patch
import test_model_tiers as fixtures

class ToolBudgetSelectionTests(unittest.TestCase):
    setUp = fixtures.Base.setUp
    restore_env = fixtures.Base.restore_env
    run_node = fixtures.TodoFleetIntegrationTests.run_node
    fleet = fixtures.TodoFleetIntegrationTests.fleet

    @staticmethod
    def candidate(agent, model, score=90):
        return dict(agent=agent, model=model, reason='synthetic cap regression', score=score)

    def providers(self):
        fleet = self.fleet(['worker','busy','idle'])
        fleet.DEFAULT_PROFILES['worker']['adapter'] = 'qwen'
        fleet.DEFAULT_PROFILES['busy']['adapter'] = 'mimo'
        fleet.DEFAULT_PROFILES['idle']['adapter'] = 'claude'
        return fleet

    def test_declared_unsupported_cap_yields_to_enforcing_peer(self):
        fleet = self.providers()
        result, folder = self.run_node([self.candidate('busy','k-busy',95),self.candidate('idle','k-idle',85)], fleet,
            task_extra=dict(min_tier_execute='K', max_model_tool_calls={'maker':18}))
        self.assertEqual(result['status'],'CANDIDATE')
        self.assertEqual([c['agent_id'] for c in fleet.execute_calls],['idle'])
        self.assertEqual(fleet.execute_calls[0]['max_model_tool_calls'],18)
        self.assertTrue(any('not enforced by mimo' in row['reason'] for row in result['preflight']))

    def test_fallback_unsupported_cap_yields_to_enforcing_substitute(self):
        fleet = self.providers()
        result, folder = self.run_node([self.candidate('worker','w1')],fleet,
            task_extra=dict(min_tier_execute='K',max_model_tool_calls={'maker':18}))
        self.assertEqual(result['status'],'CANDIDATE')
        self.assertEqual([c['agent_id'] for c in fleet.execute_calls],['idle'])
        self.assertEqual(fleet.execute_calls[0]['max_model_tool_calls'],18)
        selected=json.loads((folder/'tier-selection.json').read_text())
        self.assertEqual(selected['substitution']['picked']['agent'],'idle')
        self.assertTrue(any('not enforced by mimo' in row['reason'] for row in selected['preflight']))

    def test_all_unsupported_block_before_pick_and_dispatch(self):
        fleet = self.providers()
        fleet.DEFAULT_PROFILES.pop('idle')
        fleet.DEFAULT_PROFILES.pop('worker')
        with patch.object(fixtures.model_tiers,'note_pick') as picks:
            result, _ = self.run_node([self.candidate('busy','k-busy')],fleet,
                task_extra=dict(min_tier_execute='K',max_model_tool_calls={'maker':18}))
        self.assertEqual(result['status'],'BLOCKED')
        self.assertEqual(fleet.execute_calls,[])
        picks.assert_not_called()

    def test_unrequested_cap_keeps_native_adapter_envelope(self):
        fleet=self.providers()
        fleet.DEFAULT_PROFILES.pop('idle')
        fleet.DEFAULT_PROFILES.pop('worker')
        result,_=self.run_node([self.candidate('busy','k-busy')],fleet,task_extra=dict(min_tier_execute='K'))
        self.assertEqual(result['status'],'CANDIDATE')
        self.assertIsNone(fleet.execute_calls[0]['max_model_tool_calls'])

    def test_exhausted_qwen_then_uncapped_mimo_selects_enforcing_peer(self):
        fleet=self.providers()
        original=fleet.current_quota_views
        def views(root, profiles):
            rows,diagnostics=original(root,profiles)
            if 'worker' in rows:
                rows['worker']={'availability':{'status':'exhausted','source':'native-provider-error'},'availability_blocked':True}
            return rows,diagnostics
        fleet.current_quota_views=views
        result,folder=self.run_node([self.candidate('worker','w1')],fleet,
            task_extra=dict(min_tier_execute='K',max_model_tool_calls={'maker':24}))
        self.assertEqual(result['status'],'CANDIDATE')
        self.assertEqual([c['agent_id'] for c in fleet.execute_calls],['idle'])
        self.assertEqual(fleet.execute_calls[0]['max_model_tool_calls'],24)
        reasons={r['agent']:r['reason'] for r in result['preflight']}
        self.assertIn('exhausted',reasons['worker'])
        self.assertIn('not enforced by mimo',reasons['busy'])

    def test_reviewer_declared_and_substituted_caps_are_enforced(self):
        for declared in (True,False):
            with self.subTest(declared=declared):
                fleet=self.providers()
                fleet.result['handoff']={'verdict':'ACCEPT','findings':[],'checked':['e'],'summary':'synthetic review'}
                candidates=([self.candidate('busy','k-busy',95),self.candidate('idle','k-idle',85)]
                            if declared else [self.candidate('worker','w1')])
                result,_=self.run_node(candidates,fleet,role='reviewer',
                    task_extra=dict(reviewer_min_tier='K',max_model_tool_calls={'reviewer':24}))
                self.assertEqual(result['status'],'REVIEWED')
                self.assertEqual([c['agent_id'] for c in fleet.execute_calls],['idle'])
                self.assertEqual(fleet.execute_calls[0]['max_model_tool_calls'],24)
                self.assertTrue(any('not enforced by mimo' in row['reason'] for row in result['preflight']))

    def test_pin_does_not_expand_an_unsupported_explicit_cap(self):
        fleet=self.providers()
        with patch.object(fixtures.model_tiers,'note_pick') as picks:
            result,_=self.run_node([self.candidate('busy','k-busy')],fleet,
                task_extra=dict(candidate_policy='pin',min_tier_execute='K',max_model_tool_calls={'maker':24}))
        self.assertEqual(result['status'],'BLOCKED')
        self.assertIn('Pinned candidate',result['reason'])
        self.assertTrue(any('not enforced by mimo' in r['reason'] for r in result['preflight']))
        self.assertEqual(fleet.execute_calls,[])
        picks.assert_not_called()

    def test_legacy_routing_filters_caps_before_score_choice(self):
        import os
        os.environ['AGENT_GRAPH_TIER_ROUTING']='0'
        fleet=self.providers()
        result,_=self.run_node([self.candidate('busy','k-busy',95),self.candidate('idle','k-idle',85)],fleet,
            task_extra=dict(max_model_tool_calls={'maker':24}))
        self.assertEqual(result['status'],'CANDIDATE')
        self.assertEqual([c['agent_id'] for c in fleet.execute_calls],['idle'])
        self.assertEqual(fleet.execute_calls[0]['max_model_tool_calls'],24)

    def test_enriched_peer_must_satisfy_the_cap(self):
        fleet=self.providers()
        result,_=self.run_node([self.candidate('idle','k-idle')],fleet,
            task_extra=dict(candidate_policy='open',min_tier_execute='K',max_model_tool_calls={'maker':24}))
        self.assertEqual(result['status'],'CANDIDATE')
        self.assertEqual([c['agent_id'] for c in fleet.execute_calls],['idle'])
        self.assertTrue(any('not enforced by mimo' in r['reason'] for r in result['preflight']))

    def test_other_role_cap_keeps_maker_native_envelope(self):
        fleet=self.providers()
        fleet.DEFAULT_PROFILES.pop('idle')
        fleet.DEFAULT_PROFILES.pop('worker')
        result,_=self.run_node([self.candidate('busy','k-busy')],fleet,
            task_extra=dict(min_tier_execute='K',max_model_tool_calls={'reviewer':24}))
        self.assertEqual(result['status'],'CANDIDATE')
        self.assertIsNone(fleet.execute_calls[0]['max_model_tool_calls'])
