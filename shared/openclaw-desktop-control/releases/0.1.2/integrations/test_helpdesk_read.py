import io,json,unittest
from types import SimpleNamespace
from unittest.mock import Mock
from helpdesk_read import (
    WORKSPACE_COVERAGE, CRM_COVERAGE, WORKSPACE_QUERY, NoRedirect,
    content_revision, crm, workspace,
)

class ReadOnlyCollectors(unittest.TestCase):
    def test_workspace_fixed_readonly_query_and_minimization(self):
        run=Mock(return_value=SimpleNamespace(returncode=0,stdout=json.dumps({'status':'OK','items':[{'id':56,'status':'reported','updated_at':'2026-09-16T12:00:00','description':'restricted details','reporter_name':'PRIVATE'}]})))
        got=workspace(run)
        self.assertEqual(got['items'][0]['issue_id'],'56')
        self.assertNotIn('PRIVATE',json.dumps(got));self.assertNotIn('restricted',json.dumps(got))
        argv=run.call_args.args[0]
        self.assertIn('StrictHostKeyChecking=yes',argv);self.assertIn('example-workspace-prod',argv)
        self.assertIn('mode=ro',WORKSPACE_QUERY);self.assertIn('query_only=ON',WORKSPACE_QUERY)
        self.assertIn('title',WORKSPACE_QUERY);self.assertIn('description',WORKSPACE_QUERY)
        self.assertEqual(got['coverage'], WORKSPACE_COVERAGE)
        self.assertEqual(got['comments_coverage'], 'unavailable')
        expected=content_revision('2026-09-16T12:00:00', title='', body='restricted details',
                                  status='reported', severity='/')
        self.assertEqual(got['items'][0]['revision'], expected)
        self.assertIn('#c=', got['items'][0]['revision'])
    def test_invalid_source_and_error_do_not_leak(self):
        for response in [SimpleNamespace(returncode=1,stdout='SECRET'),SimpleNamespace(returncode=0,stdout='SECRET'),SimpleNamespace(returncode=0,stdout=json.dumps({'status':'OK','items':[{'id':'bad','status':'reported'}]}))]:
            result=workspace(Mock(return_value=response));self.assertEqual(result['status'],'BLOCKED');self.assertNotIn('SECRET',str(result))
    def test_crm_no_auth_never_calls_network(self):
        opener=Mock();self.assertEqual(crm(opener=opener)['reason'],'read_only_auth_not_configured');opener.open.assert_not_called()
    def test_crm_filters_without_losing_reopened_closed(self):
        data=[{'id':'abc-123','kind':'BUG','status':'CLOSED','needsReview':True,'updatedAt':'2026-09-16T12:00:00Z','description':'PRIVATE','reportedBy':{'email':'private@example.invalid'}},{'id':'abc-124','kind':'BUG','status':'CLOSED','needsReview':False},{'id':'abc-125','kind':'CHANGE_REQUEST','status':'NEW'}]
        opener=Mock();opener.open.return_value=io.BytesIO(json.dumps(data).encode())
        got=crm('synthetic-test-only',opener)
        self.assertEqual(got['status'],'OK');self.assertEqual(len(got['items']),1);self.assertNotIn('PRIVATE',json.dumps(got));self.assertNotIn('email',json.dumps(got))
        request=opener.open.call_args.args[0];self.assertEqual(request.get_method(),'GET');self.assertEqual(request.host,'crm.example.invalid')
        self.assertEqual(got['coverage'], CRM_COVERAGE)
        self.assertEqual(got['comments_coverage'], 'unavailable')
        expected=content_revision('2026-09-16T12:00:00Z', title='', body='PRIVATE',
                                  status='CLOSED', severity='')
        self.assertEqual(got['items'][0]['revision'], expected)
    def test_workspace_content_edit_same_updated_at_changes_revision(self):
        def payload(description):
            return json.dumps({'status':'OK','items':[{'id':56,'status':'reported',
                'title':'Login fails','description':description,'urgency':'high',
                'importance':'low','updated_at':'2026-09-16T12:00:00'}]})
        first=workspace(Mock(return_value=SimpleNamespace(returncode=0,stdout=payload('500 on submit'))))
        second=workspace(Mock(return_value=SimpleNamespace(returncode=0,stdout=payload('500 on submit, retry helps'))))
        self.assertEqual(first['status'],'OK');self.assertEqual(second['status'],'OK')
        self.assertNotEqual(first['items'][0]['revision'], second['items'][0]['revision'])
        dumped=json.dumps(first)+json.dumps(second)
        self.assertNotIn('500 on submit', dumped)
        self.assertNotIn('retry helps', dumped)
        self.assertNotIn('Login fails', dumped)
        self.assertEqual(first['coverage'], WORKSPACE_COVERAGE)
    def test_crm_content_edit_same_updated_at_changes_revision(self):
        def payload(description):
            return [{
                'id':'abc-123','kind':'BUG','status':'NEW','severity':'HIGH',
                'title':'Login fails','description':description,
                'updatedAt':'2026-09-16T12:00:00Z','needsReview':False,
            }]
        opener=Mock();opener.open.return_value=io.BytesIO(json.dumps(payload('500 on submit')).encode())
        first=crm('synthetic-test-only',opener)
        opener.open.return_value=io.BytesIO(json.dumps(payload('500 on submit, retry helps')).encode())
        second=crm('synthetic-test-only',opener)
        self.assertEqual(first['status'],'OK');self.assertEqual(second['status'],'OK')
        self.assertNotEqual(first['items'][0]['revision'], second['items'][0]['revision'])
        dumped=json.dumps(first)+json.dumps(second)
        self.assertNotIn('500 on submit', dumped)
        self.assertNotIn('Login fails', dumped)
        self.assertEqual(first['coverage'], CRM_COVERAGE)
    def test_redirect_denied(self):
        self.assertIsNone(NoRedirect().redirect_request(None,None,None,None,None,None))

if __name__=='__main__':unittest.main()
