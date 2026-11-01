import json,unittest
from pathlib import Path
from tempfile import TemporaryDirectory
import zcode_runtime as z
import fleet
from unittest.mock import patch
class DispatchTests(unittest.TestCase):
    def test_unknown_zcode_allowance_stays_bound_to_shared_pool(self):
        profiles={'zcode':{'adapter':'zcode','quota_pool_id':'zai-coding-plan-weekly'}}
        with TemporaryDirectory() as d, patch.object(fleet,'read_usage',return_value=[]), patch.object(fleet.quota_collectors,'collect_profile_quotas',return_value=({},{})):
            views,_=fleet.current_quota_views(Path(d),profiles)
        self.assertEqual(views['zcode']['quota_pool_id'],'zai-coding-plan-weekly')
        self.assertIsNone(views['zcode'].get('weekly_remaining_fraction'))
        self.assertFalse(views['zcode'].get('availability_blocked',False))
    def test_terminal_preserves_evidence_on_missing_response(self):
        events=[{'kind':'boundary_ready','transport_schema_version':1},{'kind':'model_request'},{'kind':'served_model','model':'GLM-5.3'}]
        value=z.terminal_export({'usage':{'source':'provider','inputTokens':3,'outputTokens':2,'totalTokens':5}},events,126)
        self.assertFalse(value['boundary_verified'])
        self.assertEqual(value['observed_models'],['GLM-5.3'])
        self.assertEqual(z.token_usage(value)['total_tokens'],5)
        self.assertEqual(value['native_output_status'],'missing-response')
    def test_terminal_counts_only_successful_reads(self):
        events=[{'kind':'boundary_ready','transport_schema_version':1},
                {'kind':'tool_call','number':1,'name':'fleet_list_files'},
                {'kind':'tool_result','number':1,'name':'fleet_list_files','refused':False},
                {'kind':'tool_call','number':2,'name':'fleet_read_file'},
                {'kind':'tool_result','number':2,'name':'fleet_read_file','refused':True},
                {'kind':'tool_call','number':3,'name':'fleet_read_file'},
                {'kind':'tool_result','number':3,'name':'fleet_read_file','refused':False}]
        self.assertEqual(z.terminal_export(None,events,126)['successful_file_reads'],1)

    def test_missing_later_result_blocks_despite_prior_success(self):
        events=[{'kind':'boundary_ready','transport_schema_version':1},
                {'kind':'model_request','number':1},{'kind':'served_model','model':'GLM-5.3','request':1},
                {'kind':'tool_call','number':1,'name':'fleet_read_file'},
                {'kind':'tool_result','number':1,'name':'fleet_read_file','refused':False},
                {'kind':'tool_call','number':2,'name':'fleet_list_files'},
                {'kind':'transport_error','code':'broker_closed_with_pending_calls','pending_tool_calls':[{'number':2,'name':'fleet_list_files'}]}]
        value=z.terminal_export({'response':'Synthetic'},events,0)
        self.assertEqual(value['successful_file_reads'],1)
        self.assertEqual(value['unresolved_tool_calls'],[{'number':2,'name':'fleet_list_files'}])
        self.assertIn('broker_closed_with_pending_calls',value['transport_errors'])
        self.assertFalse(value['boundary_verified'])
        self.assertIn('incomplete',z.read_evidence_error(value,{'max_tool_calls':4}))

    def test_duplicate_result_cannot_hide_another_unresolved_call(self):
        events=[{'kind':'boundary_ready','transport_schema_version':1},
                {'kind':'tool_call','number':1,'name':'fleet_read_file'},
                {'kind':'tool_result','number':1,'name':'fleet_read_file','refused':False},
                {'kind':'tool_result','number':1,'name':'fleet_read_file','refused':False},
                {'kind':'tool_call','number':2,'name':'fleet_list_files'}]
        value=z.terminal_export(None,events,126)
        self.assertEqual(value['unresolved_tool_calls'],[{'number':2,'name':'fleet_list_files'}])
        self.assertIn('duplicate_tool_result',value['transport_errors'])

    def test_complete_success_and_paired_refusal_have_no_transport_failure(self):
        events=[{'kind':'boundary_ready','transport_schema_version':1},
                {'kind':'model_request','number':1},{'kind':'served_model','model':'GLM-5.3','request':1},
                {'kind':'tool_call','number':1,'name':'fleet_read_file'},
                {'kind':'tool_result','number':1,'name':'fleet_read_file','refused':False},
                {'kind':'tool_call','number':2,'name':'fleet_list_files'},
                {'kind':'tool_result','number':2,'name':'fleet_list_files','refused':True}]
        value=z.terminal_export({'response':'Synthetic'},events,0)
        self.assertTrue(value['boundary_verified'])
        self.assertEqual(value['unresolved_tool_calls'],[])
        self.assertEqual(value['transport_errors'],[])
        self.assertIsNone(z.read_evidence_error(value,{'max_tool_calls':4}))

    def test_request_budget_failure_is_distinct_and_propagated(self):
        events=[{'kind':'boundary_ready','transport_schema_version':1},
                {'kind':'model_request','number':1},{'kind':'served_model','model':'GLM-5.3','request':1},
                {'kind':'model_request','number':2},
                {'kind':'request_budget_exceeded','number':2,'max_requests':1}]
        value=z.terminal_export({'response':'Synthetic'},events,0)
        self.assertTrue(value['request_budget_exceeded'])
        self.assertFalse(value['boundary_verified'])
        error=z.read_evidence_error(value,None)
        self.assertIn('request budget exceeded',error)
        self.assertNotIn('quota',error.lower())
    def test_review_without_trusted_dispatch_refused(self):
        with self.assertRaises(z.ZCodeRuntimeError):
            z.bind_tool_policy('Synthetic',workspace=Path('.'),mode='review',max_tool_calls=4)
    def test_prompt_json_cannot_grant_paths(self):
        prompt=json.dumps({'scope':['some-secret.txt'],'checks':[]})
        self.assertIsNone(z.bind_tool_policy(prompt,workspace=Path('.'),mode='advisory',max_tool_calls=4))
    def test_explicit_dispatch_binds_exact_file_and_ignores_prompt_json(self):
        with TemporaryDirectory() as d:
            p=Path(d);(p/'source.txt').write_text('synthetic',encoding='utf-8')
            bound=z.bind_tool_policy(json.dumps({'scope':['other.txt'],'checks':[]}),workspace=p,
                mode='review',max_tool_calls=4,dispatch={'scope':['source.txt'],'inputs':[],'checks':[]})
            self.assertEqual(bound['files'],['source.txt'])
    def test_read_evidence_requires_real_success_not_list_or_denial(self):
        policy={'files':['source.txt'],'max_tool_calls':4}
        for export in [{},{'transport_schema_version':1,'boundary_verified':True,'successful_file_reads':0},
                       {'transport_schema_version':1,'boundary_verified':False,'successful_file_reads':1},
                       {'transport_schema_version':1,'boundary_verified':True,'successful_file_reads':True}]:
            self.assertIsNotNone(z.read_evidence_error(export,policy))
        self.assertIsNone(z.read_evidence_error({'transport_schema_version':1,'boundary_verified':True,'successful_file_reads':1},policy))
        self.assertIsNone(z.read_evidence_error({},None))
    def test_checks_and_unknown_dispatch_keys_refused(self):
        for dispatch in [{'scope':[],'inputs':[],'checks':[['python','foo']]},
                         {'scope':[],'inputs':[],'checks':[],'write':True}]:
            with self.assertRaises(z.ZCodeRuntimeError):
                z.bind_tool_policy('Synthetic',workspace=Path('.'),mode='review',max_tool_calls=4,dispatch=dispatch)
if __name__=='__main__':unittest.main()
