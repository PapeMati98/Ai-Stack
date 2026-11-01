"""Offline OAuth adapter tests. Fixtures stay below explicitly chosen task root."""
import configparser
import json
import os
from pathlib import Path
import tempfile
import unittest
import uuid
from unittest import mock
import sys

import adc_entry as adc
import server


def _resolve_team_test_root():
    raw = os.environ.get("TEAM_TEST_ROOT")
    if raw is None or not raw.strip():
        raise RuntimeError("TEAM_TEST_ROOT must be set to an explicit caller-owned test root directory")
    return Path(raw.strip()).resolve() / ("oauth-portable-" + uuid.uuid4().hex)


FIXTURES = _resolve_team_test_root()


def _resolve_sdk():
    """Test-only resolver for the optional installed Cloud SDK root.

    GOOGLE_CLOUD_SDK_ROOT points at the SDK root; vendor libraries live at
    root/lib/third_party and the gcloud entry at root/lib/gcloud.py.
    Returns (vendor, entry) or None when the SDK is missing/unavailable,
    in which case the two optional SDK refresh tests skip explicitly.
    """
    raw = os.environ.get("GOOGLE_CLOUD_SDK_ROOT")
    if raw is None or not raw.strip():
        return None
    root = Path(raw.strip())
    vendor = root / "lib" / "third_party"
    entry = root / "lib" / "gcloud.py"
    if not vendor.is_dir():
        return None
    if not (vendor / "google" / "auth" / "__init__.py").is_file():
        return None
    return vendor, entry


class Response:
    def __init__(self, value, status=200):
        self.value, self.status_code = value, status
        self.closed = False
    def iter_content(self, chunk_size):
        yield self.value if isinstance(self.value, bytes) else json.dumps(self.value).encode()
    def close(self):
        self.closed = True


class Transport:
    def __init__(self):
        self.calls = []
        self.email = 'pilot@example.com'
        self.project = 'example-project'
        self.response = None
    def request(self, method, url, **kw):
        self.calls.append((method, url, kw))
        if self.response is not None:
            return self.response
        if url.endswith('/userinfo'):
            return Response({'email': self.email, 'verified_email': True})
        if url.endswith('/v1/projects/example-project'):
            return Response({'projectId': self.project, 'projectNumber': '12345', 'name': 'Example', 'lifecycleState': 'ACTIVE'})
        if 'serviceusage' in url:
            return Response({'services': [{'config': {'name': 'run.googleapis.com', 'title': 'Cloud Run'}}]})
        item = {'name': 'projects/example-project/locations/europe-west1/services/example-service',
                'terminalCondition': {'state': 'CONDITION_SUCCEEDED'}, 'latestReadyRevision': 'projects/p/locations/r/revisions/example-service-00001',
                'secret': 'must-not-return'}
        return Response(item if url.endswith('/example-service') else {'services': [item]})


class AdapterTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        FIXTURES.mkdir(parents=True, exist_ok=True)
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix='oauth-', dir=str(FIXTURES)))
        (self.root/'configurations').mkdir()
        (self.root/'configurations/config_example').write_text('[core]\naccount=pilot@example.com\nproject=example-project\n')
        raw = {'profiles': {'example': {'account': 'pilot@example.com', 'project_id': 'example-project',
            'gcloud_configuration': 'example', 'cloudsdk_config': str(self.root),
            'python_executable': sys.executable, 'gcloud_python_entrypoint': str(self.root/'sdk'/'lib'/'gcloud.py')}}}
        self.policy = server.load_policy(json.dumps(raw))
        self.p = self.policy.profiles['example']
        self.t = Transport()
        self.factories = 0
        def factory(*args):
            self.factories += 1
            return self.t
        self.b = adc.ReadonlyADCBackend(self.policy, self.root/'synthetic-oauth.json', factory)
        self.s = server.Session(self.policy, self.b, allowed_tools=("context_status", "project_info", "enabled_services"))
    def call(self, name, **args):
        return server.dispatch_tool(self.s, name, {'profile':'example', **args})
    def test_metadata_tools_and_get_only(self):
        self.assertTrue(self.call('context_status')['account_active'])
        self.assertEqual(self.call('project_info')['project_id'], 'example-project')
        self.assertEqual(self.call('enabled_services')['services'][0]['name'], 'run.googleapis.com')
        self.assertEqual(self.factories,1)
        for method,url,kw in self.t.calls:
            self.assertEqual(method,'GET')
            self.assertFalse(kw['allow_redirects'])
            self.assertTrue(kw['stream'])
            self.assertLessEqual(kw['timeout'],30)
            self.assertIn('fields',kw['params'])
    def test_rejected_input_never_loads_credentials(self):
        for tool,args in [('project_info',{'argv':['delete']}),('cloud_run_services',{'region':'../other'}),('cloud_run_service_status',{'region':'europe-west1','service':'--delete'})]:
            with self.assertRaises(server.SafeToolError): self.call(tool,**args)
        env = server.child_environment(self.p)
        with self.assertRaises(server.SafeToolError):
            self.b.run(server.command_project_describe(self.p)+['--impersonate-service-account=other'],env,5,1000)
        self.assertEqual(self.factories,0)
        self.assertEqual(self.t.calls,[])
    def test_wrong_account_denied_before_project(self):
        self.t.email='other@example.com'
        with self.assertRaises(server.SafeToolError): self.call('project_info')
        self.assertEqual(len(self.t.calls),1)
    def test_wrong_project_rejected(self):
        self.t.project='other-project'
        with self.assertRaises(server.SafeToolError): self.call('project_info')
    def test_local_mismatch_denied_before_auth(self):
        (self.root/'configurations/config_example').write_text('[core]\naccount=other@example.com\nproject=example-project\n')
        with self.assertRaises(server.SafeToolError): self.call('project_info')
        self.assertEqual(self.factories,0)
    def test_endpoint_cross_host_paths_denied(self):
        for url in ['http://www.googleapis.com/oauth2/v2/userinfo','https://evil.example/oauth2/v2/userinfo','https://run.googleapis.com/v1/projects/example-project',
                    'https://www.googleapis.com/oauth2/v2/userinfo?token=bad','https://run.googleapis.com/v2/projects/example-project/locations/europe-west1/services/../secrets',
                    'https://serviceusage.googleapis.com/v1/projects/12345/services/enable']:
            with self.assertRaises(server.SafeToolError): self.b._get(self.p,url,{},10,100)
        self.assertEqual(self.factories,0)
    def test_redirect_oversize_duplicates_http_rejected_and_closed(self):
        for response in [Response({},302),Response(b'x'*1025),Response(b'{"email":"a","email":"b"}'),Response({'secret':'private'},403)]:
            self.t.response=response
            with self.assertRaises(server.SafeToolError) as result:
                self.b._get(self.p,'https://www.googleapis.com/oauth2/v2/userinfo',{},10,1024)
            self.assertNotIn('private',str(result.exception))
            self.assertTrue(response.closed)
    def test_only_three_scoped_tools_are_advertised(self):
        self.s.initialized=True
        result=server.handle_message({'jsonrpc':'2.0','id':1,'method':'tools/list'},self.s)
        self.assertEqual({x['name'] for x in result['result']['tools']},{'context_status','project_info','enabled_services'})
        with self.assertRaises(server.SafeToolError):
            self.call('cloud_run_services',region='europe-west1')
        self.assertEqual(self.factories,0)
    def test_refresh_rejects_broader_scopes_without_network(self):
        resolved = _resolve_sdk()
        if resolved is None: self.skipTest('GOOGLE_CLOUD_SDK_ROOT unavailable for refresh test')
        vendor, entry = resolved
        sys.path.insert(0,str(vendor))
        from google.oauth2.credentials import Credentials
        from google.auth.transport.requests import AuthorizedSession
        from dataclasses import replace
        p=replace(self.p,gcloud_python_entrypoint=str(entry))
        path=self.root/'synthetic-oauth.json'
        path.write_text(json.dumps({'client_id':'synthetic-client','client_secret':'synthetic-only',
                                   'refresh_token':'synthetic-only','type':'authorized_user'}))
        def broad(creds,request):
            creds._granted_scopes=['https://www.googleapis.com/auth/cloud-platform']
        with mock.patch.object(Credentials,'refresh',broad),mock.patch.object(AuthorizedSession,'request',side_effect=AssertionError('Network forbidden')):
            with self.assertRaises(server.SafeToolError):adc.google_transport(p,path)
    def test_refresh_uses_persistent_grant_without_network(self):
        resolved = _resolve_sdk()
        if resolved is None: self.skipTest('GOOGLE_CLOUD_SDK_ROOT unavailable for refresh test')
        vendor, entry = resolved
        sys.path.insert(0,str(vendor))
        from google.oauth2.credentials import Credentials
        from dataclasses import replace
        p=replace(self.p,gcloud_python_entrypoint=str(entry))
        path=self.root/'synthetic-oauth.json'
        path.write_text(json.dumps({'client_id':'synthetic-client','client_secret':'synthetic-only',
                                   'refresh_token':'synthetic-only','type':'authorized_user'}))
        called=[]
        def readonly(creds,request):
            called.append(bool(creds.refresh_token))
            creds._granted_scopes=list(adc.SCOPES)
        with mock.patch.object(Credentials,'refresh',readonly):
            transport=adc.google_transport(p,path)
            self.assertEqual(called,[True])
            self.assertFalse(transport.trust_env)
            transport.close()


if __name__ == '__main__':
    unittest.main()
