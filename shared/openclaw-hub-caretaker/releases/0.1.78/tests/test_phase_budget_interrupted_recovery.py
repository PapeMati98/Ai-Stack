"""Actual-loader recovery over generic, task-owned synthetic fixtures only."""
import json
import unittest
from pathlib import Path
from unittest import mock
import curate_once as curator
from caretaker.publication import PublicationError
from test_phase_resume import PhaseResumeTests, Clock, Maker


class BudgetInterruptedRecoveryTests(unittest.TestCase):
    def fixture(self):
        f=PhaseResumeTests(); f.setUp()
        clock=Clock();f.maker=Maker(clock,advance=350)
        first=f._run(budget=f._budget(clock,500))
        self.assertEqual(first['status'],'DEFERRED_PREPARED')
        pointer=f._phase_path();record=json.loads(pointer.read_text())
        record.update(phase='REPAIR_STARTED',candidate=None,manifest=None,
                      manifest_sha256=None,pins={},code_version='6d74ee300e9032e35c27ced6fd2f274e')
        record['progress'].update(phase='REPAIR_STARTED',review_calls=1,
                                  assessment={'verdict':'REPAIR','findings':[]},repair_attempt=1)
        self.save(pointer,record)
        result=Path(first['run_path'])/'result.json'
        return f,pointer,record,result

    def save(self,pointer,record):
        record['cache_sha256']=curator._phase_digest(record)
        pointer.write_text(json.dumps(record),encoding='utf-8')

    def test_budget_interruption_reuses_maker_but_requires_fresh_review(self):
        f,pointer,record,result=self.fixture()
        before=result.read_bytes()
        second=f._run()
        self.assertEqual(second['status'],'PUBLISHED')
        self.assertEqual(f.maker.calls,1)
        self.assertEqual(f.reviewer.calls,1)
        self.assertIn(b'DEFERRED_PREPARED',before)

    def test_rejected_result_and_binding_cases_never_invoke_models(self):
        cases=['native_timeout','UNKNOWN','RUNNING','PUBLISHED','wrong_manifest',
               'wrong_snapshot','wrong_target','wrong_proposals','external_candidate',
               'published_effect','missing_result','terminal_rejection','wrong_version',
               'tampered_pointer','changed_source','changed_target','transaction_pending',
               'changed_payload','changed_ledger']
        for case in cases:
            with self.subTest(case=case):
                f,pointer,record,result=self.fixture()
                body=json.loads(result.read_text())
                if case=='native_timeout':body['reason']='native_timeout'
                elif case in {'UNKNOWN','RUNNING','PUBLISHED'}:body['status']=case
                elif case=='wrong_manifest':body['manifest_sha256']='0'*64
                elif case=='wrong_snapshot':body['snapshot_sha256']='0'*64
                elif case=='wrong_target':body['target']='wiki/projects/wrong.md'
                elif case=='wrong_proposals':body['proposal_ids']=['prop-other']
                elif case=='external_candidate':body['candidate_path']=str(f.root/'other')
                elif case=='published_effect':body['publication']={'event_id':'evt-synthetic'}
                elif case=='terminal_rejection':record['phase']='REPAIR_REJECTED';self.save(pointer,record)
                elif case=='wrong_version':record['code_version']='unqualified';self.save(pointer,record)
                elif case=='tampered_pointer':record['cache_sha256']='0'*64;pointer.write_text(json.dumps(record))
                elif case=='changed_source':(f.vault/'raw/sources/2026/09/src-one/original.md').write_text('tampered')
                elif case=='changed_target':
                    page=f.vault/'wiki/projects/new-slug.md';page.parent.mkdir(parents=True,exist_ok=True);page.write_text('changed')
                elif case=='transaction_pending':
                    journal=Path(body['candidate_path'])/'.curation-transactions/tx-synthetic.json';journal.parent.mkdir();journal.write_text(json.dumps({'status':'prepared'}))
                elif case in {'changed_payload','changed_ledger'}:
                    candidate=Path(body['candidate_path']);manifest=json.loads((candidate/'publication.json').read_text())
                    relative=manifest['ledger_path'] if case=='changed_ledger' else manifest['outputs'][0]['payload_path']
                    (candidate/relative).write_text('tampered retained artifact',encoding='utf-8')
                if case=='missing_result':result.rename(result.with_name('retained-original-result.json'))
                else:result.write_text(json.dumps(body),encoding='utf-8')
                with self.assertRaises(PublicationError):f._run()
                self.assertEqual(f.maker.calls,1)
                self.assertEqual(f.reviewer.calls,0)


if __name__=='__main__':unittest.main()
