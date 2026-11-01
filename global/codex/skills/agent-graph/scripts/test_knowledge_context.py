import unittest
from knowledge_context import compact

class ContextTests(unittest.TestCase):
    def test_preserves_pending_integrity_and_all_returned_sources_without_freshness_claim(self):
        src={'status':'COMPLETE','receipt_path':'receipt','health':{'lint':'PASS','status':'PASS'},
             'context':{'project_slug':'p','requires_live_verification':True,'stable_age_freshness':'UNVERIFIED_BASELINE',
               'has_more':True,'pending_state':[{'proposal_id':'new','handoff_integrity':'COMPLETE',
                 'path':'proposals/new','source_ids':['source-new'],'state_summary':'x'*1000}],
               'stable_state':[{'path':'wiki/p','summary':'stable','verification_scope':'producer-reports'}]}}
        got=compact(src)
        self.assertTrue(got['requires_live_verification']);self.assertTrue(got['has_more'])
        self.assertEqual(got['pending_state'][0]['source_ids'],['source-new'])
        self.assertTrue(got['pending_state'][0]['state_summary_truncated'])
        self.assertEqual(got['stable_state'][0]['verification_scope'],'producer-reports')
    def test_failed_health_not_hidden_by_small_output(self):
        got=compact({'status':'FAILED','failure_code':'LINT','health':{'lint':'FAIL','dirty_tree_write_blocked':True}})
        self.assertEqual(got['failure_code'],'LINT');self.assertTrue(got['health']['dirty_tree_write_blocked'])

if __name__=='__main__':unittest.main()
