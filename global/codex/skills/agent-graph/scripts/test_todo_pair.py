import sys
import tempfile
import unittest
from pathlib import Path
sys.path.insert(0,str(Path(__file__).parent))
from todo_pair import run_pair
from todo_graph import digest, snapshot, write

class Adapter:
    def __init__(self): self.calls=[]
    def invoke(self,plan,task,role,workspace,folder,feedback,maker=None):
        self.calls.append(role)
        if role=='maker':
            (workspace/'answer.txt').write_text('42')
            return {'status':'PARTIAL','lineage':'maker','transport_status':'handoff-unparsed'}
        return {'status':'REVIEWED','verdict':'ACCEPT','checked':task['done_when'],
                'findings':[],'lineage':'reviewer'}

class PairTests(unittest.TestCase):
    def test_real_file_check_then_independent_review_and_idempotent_completion(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp); work=root/'work'; work.mkdir()
            candidate={'agent':'x','model':'y','reason':'fit','score':90}
            task={'id':'answer','text':'write42','covers':['answer'],'scope':['answer.txt'],
                  'done_when':['answer is 42'],'depends_on':[],'maker':[candidate],'reviewer':[candidate],
                  'checks':[[sys.executable,'-c',"from pathlib import Path; assert Path('answer.txt').read_text()=='42'"]]}
            plan={'version':1,'goal':'answer','planner':{'model':'parent','source':'task'},
                  'workspace':str(work),'max_parallel':1,'requirements':['answer'],'tasks':[task]}
            adapter=Adapter(); result=run_pair(plan,root/'evidence',adapter)
            self.assertEqual(result['status'],'ACCEPTED')
            self.assertEqual(result['transport_status'],'handoff-unparsed')
            self.assertEqual(result['knowledge_closeout'],'PARENT_PENDING')
            self.assertEqual(adapter.calls,['maker','reviewer'])
            run_pair(plan,root/'evidence',adapter)
            self.assertEqual(len(adapter.calls),2)
            (work/'answer.txt').write_text('wrong')
            with self.assertRaises(ValueError): run_pair(plan,root/'evidence',adapter)
            with self.assertRaises(ValueError): run_pair({**plan,'tasks':[task,task]},root/'other',adapter)
    def test_blocked_pair_does_not_silently_repeat_maker(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp); work=root/'work'; work.mkdir()
            c={'agent':'a','model':'m','reason':'fit','score':90}
            t={'id':'x','text':'x','covers':['x'],'scope':['x.txt'],'done_when':['x'],'depends_on':[],
               'maker':[c],'reviewer':[c],'checks':[[sys.executable,'-c','pass']]}
            p={'version':1,'goal':'x','planner':{'model':'parent','source':'task'},'workspace':str(work),
               'max_parallel':1,'requirements':['x'],'tasks':[t]}
            class Blocked(Adapter):
                def invoke(self,*args,**kwargs): self.calls.append('maker'); return {'status':'BLOCKED','reason':'transport'}
            a=Blocked(); self.assertEqual(run_pair(p,root/'e',a)['status'],'BLOCKED')
            run_pair(p,root/'e',a); self.assertEqual(a.calls,['maker'])

    def test_accepted_pair_pins_inputs_and_checks_without_restarting_maker(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp); work=root/'work'; work.mkdir()
            (work/'check.py').write_text(
                "from pathlib import Path\nassert Path('answer.txt').read_text() == '42'\n")
            c={'agent':'x','model':'y','reason':'fit','score':90}
            task={'id':'answer','text':'write42','covers':['answer'],'scope':['answer.txt'],
                  'inputs':['check.py'],'done_when':['answer is 42'],'depends_on':[],
                  'maker':[c],'reviewer':[c],'checks':[[sys.executable,'check.py']]}
            plan={'version':1,'goal':'answer','planner':{'model':'parent','source':'task'},
                  'workspace':str(work),'max_parallel':1,'requirements':['answer'],'tasks':[task]}
            adapter=Adapter(); evidence=root/'evidence'
            accepted=run_pair(plan,evidence,adapter)
            self.assertEqual(accepted['status'],'ACCEPTED')
            self.assertEqual(accepted['inputs_digest'],snapshot(work,['check.py']))
            self.assertEqual(accepted['checks_digest'],digest(task['checks']))
            self.assertEqual(adapter.calls,['maker','reviewer'])

            (work/'check.py').write_text(
                "from pathlib import Path\nassert Path('answer.txt').read_text() == 'not 42'\n")
            with self.assertRaisesRegex(ValueError,'Accepted inputs changed'):
                run_pair(plan,evidence,adapter)
            self.assertEqual(adapter.calls,['maker','reviewer'])

            task['checks']=[[sys.executable,'-c','pass']]
            with self.assertRaisesRegex(ValueError,'Accepted checks changed'):
                run_pair(plan,evidence,adapter)
            self.assertEqual(adapter.calls,['maker','reviewer'])

if __name__=='__main__': unittest.main()
