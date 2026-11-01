"""Parent integration regressions at the scheduler/council boundary."""
import unittest
from datetime import timedelta
from test_jev_policy import NOW, iso, make_task, make_snapshot, make_candidate, make_decision
from jev_policy import recommend, consultation_policy, PolicyError


class Boundaries(unittest.TestCase):
    def setUp(self):
        self.task = make_task(scope=['src/parser.py'])
        self.candidates = [make_candidate('a','first'), make_candidate('b','second')]
        self.decision = make_decision({'a': {'score': 4,'confidence': .95}, 'b': {'score': 3.8,'confidence': .9}},
                                     parallelism={'value':'parallel','confidence':.99})
        self.active = [{'candidate_id':'z','lineage':'third','scope':['other/task.py'],'resources':[]}]

    def result(self, **kwargs):
        return recommend(self.task, make_snapshot(self.candidates, **kwargs), self.decision, now=NOW)

    def test_directory_scope_conflicts_with_descendant(self):
        self.active[0]['scope']=['src']
        self.assertEqual(self.result(active_assignments=self.active)['parallelism']['max_parallel'],1)

    def test_no_parallel_claim_for_empty_scope(self):
        self.task['scope']=[]
        self.assertEqual(self.result()['parallelism']['max_parallel'],1)

    def test_no_parallel_claim_without_known_other_work(self):
        self.assertEqual(self.result()['parallelism']['max_parallel'],1)

    def test_stale_snapshot_cannot_assert_parallel(self):
        self.assertEqual(self.result(active_assignments=self.active,captured_at=iso(NOW-timedelta(hours=1)))['parallelism']['max_parallel'],1)

    def test_capacity_is_not_hardcoded_to_two(self):
        self.assertEqual(self.result(active_assignments=self.active,capacity=4)['parallelism']['max_parallel'],4)

    def test_reviewer_context_is_checked(self):
        self.candidates[1]['context_window_tokens']=100
        self.assertEqual(self.result()['reviewer'],[])

    def test_reviewer_needs_read_tool(self):
        self.candidates[1]['tools']=[]
        self.assertEqual(self.result()['reviewer'],[])

    def test_reviewer_low_confidence_is_explicit(self):
        self.decision['fit']['b']['confidence']=.2
        self.assertTrue(self.result()['requires_parent_review'])

    def test_write_only_model_is_not_consultant(self):
        self.task['high_risk']=True
        self.candidates[1]['modes']=['implement']
        self.candidates[1]['tools']=['write']
        r=consultation_policy(self.task,make_snapshot(self.candidates),self.decision)
        self.assertNotIn('b',[c['id'] for c in r['consultants']])

    def test_subscription_model_with_openrouter_prefix_not_bought_again(self):
        self.task['high_risk']=True
        self.candidates[0]['model']='gpt-6-astra'
        r=consultation_policy(self.task,make_snapshot(self.candidates),self.decision,api_budget_usd=1,
                              justification='Explore a genuinely additional hypothesis',explicit_api_models=['openai/gpt-6-astra'])
        self.assertEqual(r['api_consultants'],[])

    def test_real_fleet_tool_name_allows_review_and_consultation(self):
        self.task.update(required_tools=['filesystem-read','filesystem-write'],high_risk=True)
        for candidate in self.candidates:
            candidate['tools']=['filesystem-read','filesystem-write']
        self.assertEqual(len(self.result()['reviewer']),1)
        self.assertEqual(len(consultation_policy(self.task,make_snapshot(self.candidates),self.decision)['consultants']),2)

    def test_consultant_context_and_specialization(self):
        self.task['high_risk']=True
        self.candidates[0]['context_window_tokens']=100
        r=consultation_policy(self.task,make_snapshot(self.candidates),self.decision)
        self.assertNotIn('a',[c['id'] for c in r['consultants']])
        self.candidates[0]['context_window_tokens']=100000
        self.decision['fit']['b']['score']=4
        self.decision['fit']['a']['score']=1
        r=consultation_policy(self.task,make_snapshot(self.candidates),self.decision)
        self.assertEqual(r['consultants'][0]['id'],'b')

    def test_budget_is_total_not_multiplied_by_models(self):
        self.task['high_risk']=True
        r=consultation_policy(self.task,make_snapshot(self.candidates),self.decision,api_budget_usd=1,
                              justification='Two distinct additional perspectives',explicit_api_models=['vendor/unique1','vendor/unique2'])
        self.assertEqual(r['api_budget_total_usd'],1)
        self.assertTrue(all('budget_usd' not in c for c in r['api_consultants']))

    def test_unknown_active_scope_and_drive_relative_path(self):
        self.active[0]['scope']=[]
        self.assertEqual(self.result(active_assignments=self.active)['parallelism']['max_parallel'],1)
        self.task['scope']=['C:relative']
        self.assertEqual(self.result(active_assignments=self.active)['parallelism']['max_parallel'],1)

    def test_opus_cross_transport_spelling_is_not_bought_again(self):
        self.task['high_risk']=True
        self.candidates[0]['model']='claude-opus-5-5'
        r=consultation_policy(self.task,make_snapshot(self.candidates),self.decision,api_budget_usd=1,
                              justification='Extra perspective',explicit_api_models=['anthropic/claude-opus-5.5'])
        self.assertEqual(r['api_consultants'],[])

    def test_consultation_uses_same_clock_as_executor_eligibility(self):
        self.task['high_risk']=True
        self.candidates[0].update(quota_remaining=0,quota_observed_at=iso(NOW-timedelta(seconds=10)))
        result=self.result()
        self.assertNotIn('a',[c['id'] for c in result['maker']])
        self.assertNotIn('a',[c['id'] for c in result['consultation']['consultants']])

    def test_api_allowlist_is_a_bounded_explicit_sequence(self):
        self.task['high_risk']=True
        for invalid in ('vendor/model', {'vendor/model': True}, ['x']*3, [''], ['has space'], [None]):
            with self.subTest(invalid=invalid), self.assertRaises(PolicyError):
                consultation_policy(self.task,make_snapshot(self.candidates),self.decision,
                                    api_budget_usd=1,justification='Additional perspective',explicit_api_models=invalid)

    def test_reviewer_exclusions_explain_unavailable_independent_model(self):
        self.candidates[1]['context_window_tokens']=100
        result=self.result()
        exclusions={c['id']: c['reasons'] for c in result['reviewer_excluded']}
        self.assertIn('CONTEXT_INSUFFICIENT',exclusions['b'])
        self.assertIn('SAME_MODEL_LINEAGE',exclusions['a'])

    def test_duplicate_normalized_api_models_are_rejected(self):
        self.task['high_risk']=True
        with self.assertRaisesRegex(PolicyError,'^API_MODEL_ALLOWLIST_DUPLICATE$'):
            consultation_policy(self.task,make_snapshot(self.candidates),self.decision,
                                api_budget_usd=1,justification='Additional perspective',
                                explicit_api_models=['gpt-6-astra','openai/gpt-6-astra'])

    def test_consultant_count_and_quota_is_not_a_tie_break(self):
        # Owner 2026-09-29: consultants are ordered by class-adjusted fit, not quota headroom.
        self.task['high_risk']=True
        self.candidates.append(make_candidate('c','third'))
        for c in self.candidates:
            c.update(quota_remaining={'a':.1,'b':.9,'c':.5}[c['id']],quota_observed_at=iso(NOW))
            self.decision['fit'][c['id']]={'score':4,'confidence':.95}
        result=self.result()['consultation']
        self.assertEqual([c['id'] for c in result['consultants']],['a','b'])
        self.assertEqual(result['execution'],'NOT_DISPATCHED')


if __name__=='__main__': unittest.main()
