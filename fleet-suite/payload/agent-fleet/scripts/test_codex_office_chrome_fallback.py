import ast, json, sys, unittest
from pathlib import Path
import fleet

class OfficeChromeFallback(unittest.TestCase):
 def branch(self, enabled, attestation='exact', chrome=True):
  tree=ast.parse(Path(fleet.__file__).read_text(encoding='utf8'))
  branches=[node for node in ast.walk(tree) if isinstance(node,ast.If) and
            isinstance(node.test,ast.Compare) and ast.unparse(node.test)=='office_mcp_config is not None' and
            any(isinstance(n,ast.Constant) and n.value=='mcp-preflight-mismatch' for n in ast.walk(node))]
  self.assertEqual(len(branches),1)
  calls=[]
  env={'office_mcp_config':{'configured':True},'office_telemetry':{},'listed':{},'effective':{'status':'mismatch'},
       'ambient':{'servers':['foreign']},'chrome_assignment':{} if chrome else None,
       'executable':'codex','execution_workspace':Path.cwd(),'codex_probe_env':{},
       'CHROME_MCP_SERVER_NAME':fleet.CHROME_MCP_SERVER_NAME,'FleetError':fleet.FleetError,
       'codex_mcp_isolation_args':lambda *args:['isolated'],
       'codex_mcp_servers':lambda *args:{'status':'ok','enabled':enabled},
       'codex_chrome_mcp_effective':lambda *args:calls.append(args) or {'status':attestation}}
  exec(compile(ast.Module(body=branches[0].body,type_ignores=[]),'<actual-fallback>','exec'),env)
  return env,calls
 def test_chrome_survives_office_checkpoint_relay_fallback(self):
  env,calls=self.branch([fleet.CHROME_MCP_SERVER_NAME]);self.assertIsNone(env['office_mcp_config']);self.assertEqual(len(calls),1);self.assertEqual(env['office_communication_mode']['mode'],'checkpoint-relay')
 def test_no_chrome_preserves_original_empty_isolation(self):
  env,calls=self.branch([],chrome=False);self.assertEqual(calls,[]);self.assertIsNone(env['office_mcp_config'])
 def test_ambient_leak_and_chrome_mismatch_still_fail_closed(self):
  with self.assertRaisesRegex(fleet.FleetError,'ambient MCP'):self.branch([fleet.CHROME_MCP_SERVER_NAME,'foreign'])
  with self.assertRaisesRegex(fleet.FleetError,'effective-config'):self.branch([fleet.CHROME_MCP_SERVER_NAME],attestation='mismatch')
if __name__=='__main__':unittest.main()
