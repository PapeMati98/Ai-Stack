import hashlib
import json
from pathlib import Path
import tempfile
import time
import unittest
from codex_desktop import CodexDesktop, Unavailable, DeliveryUnknown

THREAD='01a0ac02-c538-7033-81be-58a15d8eb075'
CALLER='01a0aaaa-5394-7d93-a00d-8663131f6837'
class App:
    caller_thread=CALLER
    def __init__(self):self.sent=[];self.state='idle';self.turn='before';self.lose_ack=False;self.turns_empty=False
    def call(self,name,args):
        if name=='read_thread':
            turns=[] if self.turns_empty else [{'id':self.turn,'status':'completed'}]
            return {'thread':{'id':THREAD,'status':{'type':self.state}},'turns':turns}
        self.sent.append(args)
        if self.lose_ack:raise Unavailable('connection closed')
        return {'threadId':THREAD}

class NativeTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);self.path=self.root/('rollout-'+THREAD+'.jsonl');self.path.write_text('',encoding='utf-8')
        self.app=App();self.adapter=CodexDesktop(self.app,{THREAD:{'rollout_path':str(self.path)}},self.root)
    def test_raw_unicode_send_unchanged(self):
        text='  Żółć e\u0301 🦞\r\n`x` </input>  '
        result=self.adapter.send(THREAD,text,'before',observed_at=time.time())
        self.assertEqual(self.app.sent,[{'threadId':THREAD,'prompt':text}])
        self.assertFalse(result['turn_started']);self.assertEqual(result['role'],'agent-delegation')
    def test_wrong_target_never_calls(self):
        with self.assertRaises(ValueError):self.adapter.send(CALLER,'x','before',observed_at=time.time())
        self.assertEqual(self.app.sent,[])
    def test_active_or_changed_never_sends(self):
        for state,turn in [('running','before'),('idle','changed')]:
            self.app.state=state;self.app.turn=turn
            with self.assertRaises(Unavailable):self.adapter.send(THREAD,'x','before',observed_at=time.time())
        self.assertEqual(self.app.sent,[])
    def test_stale_and_future_observation(self):
        for value in [time.time()-11,time.time()+60]:
            with self.assertRaises(Unavailable):self.adapter.send(THREAD,'x','before',observed_at=value)
        self.assertEqual(self.app.sent,[])
    def test_prefix_refuses_without_rewriting(self):
        for text in ['/clear','!echo hi','@file','  /clear']:
            with self.assertRaises(ValueError):self.adapter.send(THREAD,text,'before',observed_at=time.time())
        self.assertEqual(self.app.sent,[])
    def test_unknown_ack_does_not_resend(self):
        self.app.lose_ack=True
        with self.assertRaises(DeliveryUnknown):self.adapter.send(THREAD,'x','before',observed_at=time.time())
        self.assertEqual(len(self.app.sent),1)
    def test_readback_exact_and_restart(self):
        text='x\r\ne\u0301 </input>'
        value='<codex_delegation>\n  <source_thread_id>'+CALLER+'</source_thread_id>\n  <input>'+text+'</input>\n</codex_delegation>'
        event={'type':'response_item','payload':{'type':'function_call_output','name':'send_message_to_thread','output':value,'internal_chat_message_metadata_passthrough':{'create_time':100,'turn_id':'new'}}}
        complete={'type':'event_msg','payload':{'type':'task_complete','turn_id':'new','last_agent_message':'OK'}}
        self.path.write_text(json.dumps(event)+'\n'+json.dumps(complete)+'\n',encoding='utf-8')
        result=self.adapter.readback(THREAD,text,not_before=99)
        self.assertEqual(result['state'],'completed');self.assertTrue(result['exact_unicode_match'])
        self.assertEqual(self.adapter.readback(THREAD,text,not_before=101)['state'],'delivery_unknown')
        self.assertEqual(self.adapter.readback(THREAD,text+'!',not_before=99)['state'],'delivery_unknown')
    def test_ambiguous_native_match_never_accepts(self):
        text='x';records=[]
        for turn in ['one','two']:
            records.append({'type':'response_item','payload':{'type':'function_call_output','name':'send_message_to_thread','output':'<codex_delegation>\n  <source_thread_id>'+CALLER+'</source_thread_id>\n  <input>x</input>\n</codex_delegation>','internal_chat_message_metadata_passthrough':{'create_time':100,'turn_id':turn}}})
        self.path.write_text('\n'.join(json.dumps(e) for e in records),encoding='utf-8')
        self.assertEqual(self.adapter.readback(THREAD,text,not_before=90)['matching_turns'],2)
    def test_no_cancel_or_receiver_cas_claim(self):
        self.assertFalse(self.adapter.supports_cancel);self.assertFalse(self.adapter.supports_atomic_last_turn_cas)
        self.assertFalse(self.adapter.supports_latest_turn_items_in_status)
        self.assertFalse(self.adapter.capabilities['supports_cancel'])
        self.assertFalse(self.adapter.capabilities['supports_atomic_last_turn_cas'])
        self.assertFalse(self.adapter.capabilities['cli_fallback'])
    def test_supervisor_label_required(self):
        with self.assertRaises(ValueError):self.adapter.send(THREAD,'continue','before',observed_at=time.time(),supervisor=True)
        self.assertEqual(self.app.sent,[])
    def test_missing_status_turns_use_rollout_last_turn(self):
        self.app.turns_empty=True
        event={'type':'event_msg','payload':{'type':'task_complete','turn_id':'before','last_agent_message':'ok'}}
        self.path.write_text(json.dumps(event)+'\n',encoding='utf-8')
        result=self.adapter.send(THREAD,'hello','before',observed_at=time.time())
        self.assertEqual(self.app.sent,[{'threadId':THREAD,'prompt':'hello'}])
        self.assertEqual(result['role'],'agent-delegation')
    def test_pipe_missing_never_starts_server(self):
        from codex_desktop import AppTools, Unavailable, pipe_available
        self.assertFalse(pipe_available('not-a-pipe'))
        node=self.root/'node.exe'; node.write_bytes(b'x')
        plugin=self.root/'server.js'; plugin.write_bytes(b'plugin')
        digest=hashlib.sha256(b'plugin').hexdigest()
        with self.assertRaises(Unavailable):
            AppTools(str(node.resolve()), str(plugin.resolve()), digest, CALLER,
                     '\\\\.\\pipe\\openclaw-missing')

if __name__=='__main__':unittest.main()
