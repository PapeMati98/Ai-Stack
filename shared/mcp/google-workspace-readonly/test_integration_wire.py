"""Real-shaped wire fixtures: no Google calls, auth secrets or local data."""
import importlib.util
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest

BASE=Path(__file__).resolve().parent.parent
def module(name,path):
    spec=importlib.util.spec_from_file_location(name,path);obj=importlib.util.module_from_spec(spec)
    sys.modules[name]=obj;spec.loader.exec_module(obj);return obj
ws=module('workspace_wire_candidate',BASE/'google-workspace-readonly/server.py')
gcp=module('gcp_wire_candidate',BASE/'gcp-readonly/server.py')

class Response:
    def __init__(self,body,status=200):
        self.body=body;self.status_code=status;self.history=[];self.is_redirect=False
    def iter_content(self,chunk_size):yield self.body
    def close(self):pass

class Transport:
    def request(self,method,url,**kwargs):
        assert method=='GET'
        params=kwargs['params']
        if url.endswith('/userinfo'):data={'email':'owner@example.com','verified_email':True}
        elif url.endswith('/about'):
            if 'user(storageQuota' in params.get('fields',''):return Response(b'{"error":"Invalid field selection storageQuota"}',400)
            data={'user':{'emailAddress':'owner@example.com','displayName':'Owner'},'storageQuota':{'limit':'2000000000000','usage':'5000000000'}}
        elif params.get('alt')=='media':return Response(b'Actual Drive plaintext\nsecond line')
        elif url.endswith('/export'):return Response(b'Actual Google Docs text export')
        else:data={'id':'abc123','mimeType':'application/vnd.google-apps.document' if EXPORT else 'text/plain','name':'fixture','trashed':False}
        return Response(json.dumps(data).encode())

EXPORT=False
class WireRegressions(unittest.TestCase):
    def setUp(self):
        global EXPORT
        EXPORT=False
        p=ws.Policy('owner@example.com',str(Path(tempfile.gettempdir()) / 'google-workspace-wire-fixture'),'C:/trusted/sdk/lib/gcloud.py')
        b=ws.GoogleBackend(p,str(Path(p.config_root)/'synthetic-oauth.json'),session_factory=lambda *_:Transport())
        self.session=ws.Session(p,b)
    def test_drive_plaintext_real_http_body(self):
        data=ws.drive_read_text(self.session,{'file_id':'abc123'})
        self.assertEqual(data['text'],'Actual Drive plaintext\nsecond line')
    def test_docs_export_real_http_body(self):
        global EXPORT
        EXPORT=True
        data=ws.drive_read_text(self.session,{'file_id':'abc123'})
        self.assertEqual(data['text'],'Actual Google Docs text export')
    def test_storage_uses_valid_user_fields(self):
        data=ws.drive_storage(self.session,{})
        self.assertEqual(data['storageQuota']['limit'],'2000000000000')
    def test_workspace_standard_call_meta_is_not_a_tool_argument(self):
        self.session.initialized=True
        result=ws.handle_message({'jsonrpc':'2.0','id':1,'method':'tools/call','params':{'name':'workspace_context','arguments':{},'_meta':{'progressToken':'native-client'}}},self.session)
        self.assertNotIn('error',result)
        self.assertFalse(result['result']['isError'])
    def test_gcp_standard_call_meta_reaches_strict_arguments(self):
        s=gcp.Session(gcp.Policy({}),None);s.initialized=True
        result=gcp.handle_message({'jsonrpc':'2.0','id':1,'method':'tools/call','params':{'name':'project_info','arguments':{'profile':'unknown'},'_meta':{'progressToken':'native-client'}}},s)
        self.assertNotIn('error',result)
        self.assertTrue(result['result']['isError'])
    def test_invalid_standard_meta_fails_before_auth(self):
        for mod,session,name,args in ((ws,ws.Session(self.session.policy,object()),'workspace_context',{}),
                                     (gcp,gcp.Session(gcp.Policy({}),None),'project_info',{'profile':'unknown'})):
            session.initialized=True
            for meta in (None,[],5,'text',{'large':'x'*9000}):
                with self.subTest(module=mod.__name__,meta_type=type(meta).__name__):
                    result=mod.handle_message({'jsonrpc':'2.0','id':1,'method':'tools/call',
                        'params':{'name':name,'arguments':args,'_meta':meta}},session)
                    self.assertEqual(result['error']['code'],-32602)
    def test_metadata_never_relaxes_tool_arguments(self):
        session=ws.Session(self.session.policy,object());session.initialized=True
        result=ws.handle_message({'jsonrpc':'2.0','id':1,'method':'tools/call',
            'params':{'name':'workspace_context','arguments':{'url':'https://example.com'},
                      '_meta':{'progressToken':'native-client'}}},session)
        self.assertTrue(result['result']['isError'])
    def test_unknown_envelope_field_remains_rejected(self):
        session=ws.Session(self.session.policy,object());session.initialized=True
        result=ws.handle_message({'jsonrpc':'2.0','id':1,'method':'tools/call',
            'params':{'name':'workspace_context','arguments':{},'_meta':{},'url':'https://example.com'}},session)
        self.assertEqual(result['error']['code'],-32602)

if __name__=='__main__':unittest.main()
