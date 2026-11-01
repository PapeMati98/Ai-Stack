import json,tempfile,unittest
from pathlib import Path
from caretaker.fleet_receipts import read_receipt

class FleetReceiptTests(unittest.TestCase):
 def test_terminal_verified_only_no_prompt(self):
  with tempfile.TemporaryDirectory() as d:
   path=Path(d)/'run.json'
   good={'run_status':'COMPLETED','handoff_valid':True,'exit_code':0,'transport_outcome':{'status':'complete'},'model_identity':{'status':'verified'},'handoff':{'status':'completed','summary':'Local file validation passed.','risks':['Deployment not proven.']},'run_id':'run-123','adapter':'kimi','completed_at':'2026-09-17T20:00:00Z','prompt':'DO NOT EXPORT THIS'}
   path.write_text(json.dumps(good));result=read_receipt(path)
   self.assertEqual(result['tool'],'kimi');self.assertIn('Deployment not proven',result['excerpt']);self.assertNotIn('DO NOT EXPORT',json.dumps(result))
   for delta in [{'run_status':'FAILED'},{'handoff_valid':False},{'model_identity':{'status':'unknown'}},{'timed_out':True}]:
    path.write_text(json.dumps(good|delta));self.assertIsNone(read_receipt(path))

if __name__=='__main__':unittest.main()
