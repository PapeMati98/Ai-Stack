import copy,json,os,sys,time,unittest
from pathlib import Path
import types
t=types.SimpleNamespace(PY=sys.executable)
C=Path(__file__).resolve().parent
import fleet,process_supervision as supervisor
handoff={'status':'completed','summary':'Synthetic terminal contract checked','files_changed':[],'verification':['exact terminal fixture'],'risks':[],'next_steps':[]}
envelope={'schemaVersion':1,'type':'exec.result','runId':'exec-test','sessionId':'mvs-test','turnId':'turn-test','status':'succeeded','output':json.dumps(handoff),'model':{'providerId':'minimax','modelId':'MiniMax-M3','providerKind':'minimax-managed','providerSource':'provider'}}
class TerminalCleanup(unittest.TestCase):
 def test_native_result_recognized_but_nested_model_prose_is_not(self):
  self.assertTrue(fleet.minimax_terminal_completion(json.dumps(envelope)))
  self.assertFalse(fleet.minimax_terminal_completion(json.dumps({'type':'assistant','text':json.dumps(envelope)})))
 def test_identity_status_schema_duplicate_and_interim_denied(self):
  for field,value in [('status','failed'),('schemaVersion',True),('type','result'),('runId','')]:
   item=copy.deepcopy(envelope);item[field]=value;self.assertFalse(fleet.minimax_terminal_completion(json.dumps(item)))
  item=copy.deepcopy(envelope);item['model']['modelId']='foreign';self.assertFalse(fleet.minimax_terminal_completion(json.dumps(item)))
  item=copy.deepcopy(envelope);item['output']='{"status":"completed","summary":"working"}';self.assertFalse(fleet.minimax_terminal_completion(json.dumps(item)))
  raw=json.dumps(envelope);self.assertFalse(fleet.minimax_terminal_completion(raw[:-1]+',"status":"succeeded"}'))
 def test_valid_terminal_stops_owned_tree_without_waiting_for_stdio(self):
  code='import json,subprocess,sys,time,os;subprocess.Popen([sys.executable,"-c","import time;time.sleep(60)"],creationflags=subprocess.CREATE_NO_WINDOW if os.name=="nt" else 0);print('+repr(json.dumps(envelope))+',flush=True);time.sleep(60)'
  r=supervisor.run([t.PY,'-u','-c',code],cwd=C,env=dict(os.environ),stdin_text=None,timeout=6,terminal_completion=fleet.minimax_terminal_completion)
  self.assertEqual(r['exit_code'],0);self.assertTrue(r['protocol_terminal_completed']);self.assertFalse(r['timed_out']);self.assertFalse(r['supervision_failed']);self.assertLess(r['execution_seconds'],5);self.assertIn('native_process_exit_code',r)
 def test_without_native_terminal_ordinary_timeout_stays_failure(self):
  r=supervisor.run([t.PY,'-u','-c','import time;print("ordinary progress",flush=True);time.sleep(60)'],cwd=C,env=dict(os.environ),stdin_text=None,timeout=1,terminal_completion=fleet.minimax_terminal_completion)
  self.assertEqual(r['exit_code'],124);self.assertTrue(r['timed_out']);self.assertFalse(r['protocol_terminal_completed'])
 def test_oversized_progress_does_not_make_a_native_run_fail(self):
  code='print("x"*'+str(supervisor.MAX_CONTEXT_LINE_CHARS+1)+',flush=True)'
  r=supervisor.run([t.PY,'-u','-c',code],cwd=C,env=dict(os.environ),stdin_text=None,timeout=6,terminal_completion=fleet.minimax_terminal_completion)
  self.assertEqual(r['exit_code'],0);self.assertTrue(r['terminal_scan_incomplete']);self.assertFalse(r['supervision_failed'])
 def test_oversized_progress_then_valid_terminal_can_complete(self):
  code='import time;print("x"*'+str(supervisor.MAX_CONTEXT_LINE_CHARS+1)+',flush=True);print('+repr(json.dumps(envelope))+',flush=True);time.sleep(60)'
  r=supervisor.run([t.PY,'-u','-c',code],cwd=C,env=dict(os.environ),stdin_text=None,timeout=6,terminal_completion=fleet.minimax_terminal_completion)
  self.assertEqual(r['exit_code'],0);self.assertTrue(r['terminal_scan_incomplete']);self.assertTrue(r['protocol_terminal_completed']);self.assertLess(r['execution_seconds'],5)
 def test_native_failure_after_terminal_is_not_masked(self):
  code='import sys;print('+repr(json.dumps(envelope))+',flush=True);sys.exit(3)'
  r=supervisor.run([t.PY,'-u','-c',code],cwd=C,env=dict(os.environ),stdin_text=None,timeout=6,terminal_completion=fleet.minimax_terminal_completion)
  self.assertEqual(r['exit_code'],3);self.assertEqual(r['native_exit_before_cleanup'],3);self.assertTrue(r['protocol_terminal_completed'])
 def test_tool_guard_failure_dominates_valid_terminal(self):
  code='import time;print('+repr(json.dumps(envelope))+',flush=True);time.sleep(60)'
  r=supervisor.run([t.PY,'-u','-c',code],cwd=C,env=dict(os.environ),stdin_text=None,timeout=6,terminal_completion=fleet.minimax_terminal_completion,tool_limit=1,tool_events=lambda line:['one','two'])
  self.assertEqual(r['exit_code'],125);self.assertTrue(r['tool_budget_exceeded']);self.assertFalse(r['timed_out'])
if __name__=='__main__':unittest.main()
