import json,sys,unittest
from pathlib import Path
import fleet_chrome_integration as c,mimo_runtime as m
class NativeNames(unittest.TestCase):
 def route(self,mode):
  r=c.fleet_mcp_assignment('mimo',{'schema':c.BINDING_SCHEMA,'python':sys.executable,'managed':r'D:\fixture\managed.py','config':r'D:\fixture\managed.json'},mode=mode)
  return {**r,'budget_state':r'D:\fixture\fleet-chrome-call-budget.v1','budget_nonce':'a'*32,'budget_limit':12}
 def test_native_generated_names_and_review_boundary(self):
  for mode,count in (('implement',8),('review',5)):
   r=self.route(mode);p=m.mimo_config_payload(mode,provider_model=m.MIMO_RUNTIME_PROVIDER_MODEL,endpoint=m.MIMO_DESIGNATED_AUTH_ENDPOINT,chrome_assignment=r)
   expected={c.SERVER+'_'+name for name in c.mcp_tool_names(r)}
   self.assertEqual({k for k,v in p['permission'].items() if k.startswith(c.SERVER+'_') and v=='allow'},expected);self.assertEqual(len(expected),count)
   self.assertEqual(p['permission']['bash'],'deny');self.assertEqual(p['permission']['webfetch'],'deny')
   env=m.harden_mimo_environment({},mode=mode,run_home=r'D:\fixture\mimo-home',auth_content='{}',chrome_assignment=r)
   self.assertEqual(json.loads(env['MIMOCODE_PERMISSION']),p['permission'])
   self.assertEqual(m.mimo_environment_attestation(env,mode=mode,chrome_assignment=r)['permission_payload'],p['permission'])
 def test_claude_prefix_and_foreign_names_are_denied(self):
  for name in ('mcp__fleet-chrome-relay__chrome_read','foreign_chrome_read','fleet-chrome-relay_chrome_eval'):
   with self.assertRaises(m.MiMoRuntimeError):m.build_permission_payload('implement',chrome_tool_names=(name,))
 def test_without_browser_preserves_empty_mcp_and_deny_defaults(self):
  p=m.mimo_config_payload('implement',provider_model=m.MIMO_RUNTIME_PROVIDER_MODEL,endpoint=m.MIMO_DESIGNATED_AUTH_ENDPOINT)
  self.assertEqual(p['mcp'],{});self.assertEqual(p['permission']['*'],'deny');self.assertFalse(any(k.startswith(c.SERVER+'_') for k in p['permission']))
 def test_review_mutations_and_unknown_mode_are_refused(self):
  for name in ('chrome_click','chrome_type','chrome_saved_login'):
   with self.assertRaises(m.MiMoRuntimeError):m.build_permission_payload('review',chrome_tool_names=(c.SERVER+'_'+name,))
  with self.assertRaises(m.MiMoRuntimeError):m.build_permission_payload('unknown')
  with self.assertRaises(m.MiMoRuntimeError):m.mimo_environment_attestation({},mode='unknown')
 def test_assignment_mode_mismatch_and_advisory_fail_closed(self):
  for mode in ('review','advisory'):
   route=self.route('implement')
   with self.assertRaises(m.MiMoRuntimeError):m.harden_mimo_environment({},mode=mode,chrome_assignment=route)
   a=m.mimo_environment_attestation({},mode=mode,chrome_assignment=route)
   self.assertEqual(a['status'],'UNVERIFIED');self.assertFalse(any(k.startswith(c.SERVER+'_') for k in a['permission_payload']))
if __name__=='__main__':unittest.main()
