"""Owner acceptance counterexample: recent positive notes must not hide a failed repair."""
import json,os,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
import helpdesk,repair_history

class CounterevidenceAcceptance(unittest.TestCase):
    def test_older_negative_survives_recent_positive_notes(self):
        base=Path(os.environ.get('TEAM_TEST_ROOT',tempfile.gettempdir()))/'rsi-counterevidence'
        base.mkdir(parents=True,exist_ok=True)
        home=Path(tempfile.mkdtemp(prefix='counterevidence-',dir=base))
        with patch.dict(os.environ,{'RSI_HELPDESK_HOME':str(home),'RSI_HELPDESK_NO_OFFICE':'1','RSI_HELPDESK_NO_TRIGGER':'1'}):
            ticket,_=helpdesk.file_ticket(component='hub',symptom='synthetic context retrieval regression',evidence=[],impact='fixture',workaround='none',reporter='parent-acceptance')
            ticket['lessons']=[{'version':n,'ts':'2026-10-19T11:00:00Z','by':'fixture','status':'open','text':('Failed repair must remain visible' if n==1 else 'Later positive observation'),
                'context':{'target':'harness','outcome':('ineffective' if n==1 else 'effective'),'applicability':'synthetic runtime v1'}} for n in range(1,17)]
            helpdesk.save(ticket)
            result=repair_history.related_history({'component':'hub','signature':'new-case','symptom':'synthetic context retrieval regression'})
            self.assertLessEqual(len(repair_history._serialize(result)),4096)
            lessons=[lesson for entry in result['tickets'] for lesson in entry.get('lessons',[])]
            self.assertTrue(any(x.get('context',{}).get('outcome')=='ineffective' for x in lessons),'bounded retrieval hid the failed repair behind later positive notes')
            self.assertEqual(len(helpdesk.load(ticket['signature'])['lessons']),16)

if __name__=='__main__':unittest.main()
