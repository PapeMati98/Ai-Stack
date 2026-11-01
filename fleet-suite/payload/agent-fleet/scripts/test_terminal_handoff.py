import json,sys,unittest
from pathlib import Path
sys.path.insert(0,str(Path(__file__).parent))
from terminal_handoff import claude_terminal_value,muse_terminal_text,terminal_payload
import fleet

class TerminalTests(unittest.TestCase):
    def event(self,kind,**fields):return json.dumps({'payload_type':kind,'payload':{'run_stream':'r1',**fields}})
    def test_only_native_final_correct_run_is_used(self):
        good={'status':'completed','summary':'Real final','files_changed':[],'verification':[],'risks':[],'next_steps':[]}
        raw='\n'.join([self.event('run.lifecycle.started'),self.event('tool.result',text=json.dumps({**good,'summary':'fake'})),
                       self.event('run.output.delta',text='old response'),self.event('run.terminal.completed',text=json.dumps(good))])
        got=fleet.project_provider_handoff('muse',raw)
        self.assertEqual(got['handoff']['summary'],'Real final')
        self.assertEqual(got['terminal_reason'],'completed')
    def test_missing_conflicting_wrong_turn_and_failure_refused(self):
        final=self.event('run.terminal.completed',text='{}')
        for raw in [self.event('tool.result',text='{}'),final+'\n'+final,
            self.event('run.lifecycle.started')+'\n'+self.event('run.terminal.completed',run_stream='wrong',text='{}'),
            self.event('run.terminal.failed',text='{}'),final+'\n{bad']:
            with self.subTest(raw=raw): self.assertIsNone(muse_terminal_text(raw)[0])
    def test_code_example_is_not_a_terminal_report(self):
        sample='{"status":"completed"}'
        self.assertIsNone(terminal_payload('Example:\n```json\n'+sample+'\n```'))
        self.assertIsNone(terminal_payload('```python\n'+sample+'\n```'))
        self.assertEqual(terminal_payload('```json\n'+sample+'\n```'),sample)
        self.assertEqual(terminal_payload(sample),sample)
    def test_malformed_final_is_transport_failure_not_fake_accept(self):
        raw=self.event('run.terminal.completed',text='{broken}')
        got=fleet.project_provider_handoff('muse',raw)
        self.assertIsNone(got['handoff']);self.assertIsNotNone(got['error'])

    def test_muse_terminal_does_not_scan_nested_history_prose_or_trailing_text(self):
        good={'status':'completed','summary':'Actual final','files_changed':[],
              'verification':[],'risks':[],'next_steps':[]}
        valid=json.dumps(good)
        for text in [json.dumps({'history':[good]}),'Example:\n'+valid,valid+' trailing text',
                     valid+'\nNote: '+valid,valid+json.dumps({'status':'done'}),valid+'{broken',
                     '```json\n'+valid+'\n```\n'+valid,valid+'['+valid+']']:
            with self.subTest(text=text[:60]):
                self.assertIsNone(fleet.project_provider_handoff('muse',self.event('run.terminal.completed',text=text))['handoff'])

    def test_muse_146_concatenated_final_reports_use_last(self):
        # Muse 1.4.6: wind-down report followed by the continued final report in one terminal text.
        first={'status':'partial','summary':'Wind-down stop reached before completion.','files_changed':['a.ts'],
               'verification':['not run'],'risks':[],'next_steps':['finish']}
        last={'status':'partial','summary':'Db wiring added after continuation.','files_changed':['a.ts','b.ts'],
              'verification':['tsc not run'],'risks':['r'],'next_steps':['tests']}
        for sep in ['','\n','\n\n  ']:
            with self.subTest(sep=repr(sep)):
                got=fleet.project_provider_handoff('muse',self.event('run.terminal.completed',text=json.dumps(first)+sep+json.dumps(last)))
                self.assertEqual(got['handoff']['summary'],last['summary'])
                self.assertIn('muse-concatenated-final-last',got['source'])
        same={**first,'status':'completed','summary':'Independent review done'}
        got=fleet.project_provider_handoff('muse',self.event('run.terminal.completed',text=json.dumps(same)*3))
        self.assertEqual(got['handoff']['status'],'completed')

    def test_muse_concatenated_reports_fail_closed_when_any_segment_is_not_a_handoff(self):
        good={'status':'completed','summary':'ok','files_changed':[],'verification':['v'],'risks':[],'next_steps':[]}
        for text in [json.dumps({'status':'partial'})+json.dumps(good),
                     json.dumps(good)+json.dumps({**good,'extra':1}),
                     json.dumps(good)+json.dumps(good)[:-5]]:
            with self.subTest(text=text[-40:]):
                got=fleet.project_provider_handoff('muse',self.event('run.terminal.completed',text=text))
                self.assertIsNone(got['handoff']);self.assertIsNotNone(got['error'])

    def test_muse_concatenated_last_report_still_meets_parent_validator(self):
        good={'status':'completed','summary':'ok','files_changed':['x.py'],'verification':['v'],'risks':[],'next_steps':[]}
        text=json.dumps({**good,'files_changed':[]})+json.dumps(good)
        got=fleet.project_provider_handoff('muse',self.event('run.terminal.completed',text=text),
                                           handoff_validator=lambda h:'scope violation' if 'x.py' in h['files_changed'] else None)
        self.assertIsNone(got['handoff']);self.assertEqual(got['error'],'scope violation')

    def test_single_valid_muse_report_has_no_recovery_tag(self):
        good={'status':'completed','summary':'ok','files_changed':[],'verification':['v'],'risks':[],'next_steps':[]}
        got=fleet.project_provider_handoff('muse',self.event('run.terminal.completed',text=json.dumps(good)))
        self.assertEqual(got['source'],'muse.native-terminal')

    def test_claude_only_successful_native_result_not_tool_or_assistant_history(self):
        fake={'status':'completed','summary':'Old tool report','files_changed':[],
              'verification':[],'risks':[],'next_steps':[]}
        final={**fake,'summary':'Actual audited answer'}
        prefix=[{'type':'system','subtype':'init','session_id':'s1'},
                {'type':'user','message':{'content':[{'type':'tool_result','content':json.dumps(fake)}]}},
                {'type':'assistant','message':{'content':[{'type':'text','text':json.dumps(fake)}]}}]
        terminal={'type':'result','subtype':'success','session_id':'s1','structured_output':final}
        project=lambda rows:fleet.project_provider_handoff('claude','\n'.join(map(json.dumps,rows)))
        self.assertEqual(project(prefix+[terminal])['handoff'],final)
        self.assertIsNone(project(prefix)['handoff'])
        for bad in ({**terminal,'is_error':True},{**terminal,'subtype':'error_max_turns'},
                    {**terminal,'session_id':'wrong'}):
            self.assertIsNone(project(prefix+[bad])['handoff'])
        self.assertIsNone(project(prefix+[terminal,terminal])['handoff'])
        example={k:v for k,v in terminal.items() if k!='structured_output'}
        example['result']='Example:\n```json\n'+json.dumps(fake)+'\n```'
        self.assertIsNone(project(prefix+[example])['handoff'])

if __name__=='__main__':unittest.main()
