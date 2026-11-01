import json,tempfile,unittest
from pathlib import Path
from caretaker.backlog import select_pending

class BacklogTests(unittest.TestCase):
    def test_other_agents_eligible_but_accounted_restricted_other_project_refused(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);p=root/'proposals/other';p.mkdir(parents=True);ledger=root/'governance/curations';ledger.mkdir(parents=True)
            for ident,project,classification in [('1','p','internal'),('2','p','internal'),('3','q','internal'),('4','p','restricted')]:
                (p/('prop-'+ident+'.md')).write_text('---\nid: "prop-'+ident+'"\nclassification: "'+classification+'"\ntarget_pages: ["wiki/projects/'+project+'.md"]\n---\nbody')
            (ledger/'event.json').write_text(json.dumps({'proposals':[{'proposal_id':'prop-1','disposition':'unresolved'}]}))
            self.assertEqual(select_pending(root,'p'),['proposals/other/prop-2.md'])
