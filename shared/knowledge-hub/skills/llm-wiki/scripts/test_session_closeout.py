"""Session-closeout user journeys in disposable vaults only."""
import argparse
import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import knowledge as k


class SessionCloseoutTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='hub-session-test-')
        self.home = Path(self.temp.name)
        self.vault = self.home / 'vault'
        self.vault.mkdir()
        k.write_json(self.vault/'knowledge-vault.json', {'curator_machine':'FIXTURE','curation_scope':'designated-machine-only'})
        self.home_patch = mock.patch.object(Path, 'home', return_value=self.home)
        self.home_patch.start()
        self.note = self.home/'note.md'
        self.note.write_text('## Result\nSynthetic parser repaired.\n## Evidence\nFixture test passed.\n## Next step\nReview candidate.\n',encoding='utf-8')

    def tearDown(self):
        self.home_patch.stop()
        self.temp.cleanup()

    def args(self, **changes):
        values = dict(agent='codex',session_id='sensitive-local-session-label',checkpoint_id='episode-1',
                      project_slug='alpha-project',outcome='COMPLETED',path=str(self.note),summary='Synthetic parser repaired.',
                      classification='internal',context_receipt=None)
        values.update(changes)
        return argparse.Namespace(**values)

    def snapshot(self):
        return {p.relative_to(self.vault).as_posix():hashlib.sha256(p.read_bytes()).hexdigest()
                for p in self.vault.rglob('*') if p.is_file()}

    def test_s01_exact_retry_reuses_complete_pair(self):
        first = k.record_session(self.vault,self.args())
        before = self.snapshot()
        second = k.record_session(self.vault,self.args())
        self.assertEqual(first['receipt_status'],'COMPLETE')
        self.assertEqual(first['source_id'],second['source_id'])
        self.assertEqual(first['proposal_id'],second['proposal_id'])
        self.assertTrue(second['duplicate'])
        self.assertEqual(before,self.snapshot())
        self.assertEqual(len(first['event_ids']),2)
        proposal = Path(first['proposal_path']).read_text(encoding='utf-8')
        self.assertNotIn('TODO',proposal)
        self.assertNotIn('critical-state',proposal)
        for path in self.vault.rglob('*'):
            if path.is_file():
                self.assertNotIn('sensitive-local-session-label',path.read_text(encoding='utf-8'))

    def test_closeout_where_deletion_is_forbidden_uses_the_git_directory(self):
        # 2026-09-23: a Codex hook that forbids deletion made every tempfile probe
        # fail ("No usable temporary directory found"); the closeout must still complete.
        import os
        subprocess.run(['git', '-C', str(self.home), 'init', '--quiet', '--initial-branch=main'], check=True)
        scratch = str(self.home / '.git' / 'llm-wiki-scratch')
        real_mkdtemp, real_unlink, real_rmdir = tempfile.mkdtemp, os.unlink, os.rmdir

        def no_default_temp(*a, **kw):
            if kw.get('dir', a[2] if len(a) > 2 else None) is None:
                raise FileNotFoundError(2, 'No usable temporary directory found')
            return real_mkdtemp(*a, **kw)

        def guarded(real):
            def call(path, *a, **kw):
                if os.path.abspath(str(path)).startswith(os.path.abspath(scratch)):
                    raise PermissionError(13, 'deletion blocked by audit hook')
                return real(path, *a, **kw)
            return call
        with mock.patch.object(k.tempfile, 'mkdtemp', side_effect=no_default_temp), \
                mock.patch('os.unlink', side_effect=guarded(real_unlink)), \
                mock.patch('os.rmdir', side_effect=guarded(real_rmdir)):
            result = k.record_session(self.vault, self.args())
        self.assertEqual(result['receipt_status'], 'COMPLETE')
        self.assertTrue(result['proposal_id'])

    def test_s02_resume_appends_but_same_key_drift_refuses(self):
        first = k.record_session(self.vault,self.args())
        before = self.snapshot()
        with self.assertRaisesRegex(k.GatewayError,'SESSION_CONFLICT'):
            k.record_session(self.vault,self.args(summary='Changed meaning.'))
        self.assertEqual(before,self.snapshot())
        second = k.record_session(self.vault,self.args(checkpoint_id='episode-2',summary='Changed meaning.'))
        self.assertNotEqual(first['source_id'],second['source_id'])
        self.assertNotEqual(first['checkpoint_key'],second['checkpoint_key'])

    def test_retry_rejects_changed_proposal_without_creating_another(self):
        first=k.record_session(self.vault,self.args())
        path=Path(first['proposal_path'])
        original=path.read_text(encoding='utf-8')
        variants=[original+'\nChanged content.\n', original.replace('status: proposed','status: rejected'),
                  original.replace('agent: codex','agent: grok'),
                  original.replace('source_ids:\n  - '+first['source_id'],'source_ids: []'),
                  original.replace('target_pages:\n  - wiki/projects/alpha-project.md','target_pages: []')]
        for changed in variants:
            with self.subTest(change=variants.index(changed)):
                path.write_text(changed,encoding='utf-8')
                before=self.snapshot()
                with self.assertRaises(k.GatewayError):
                    k.record_session(self.vault,self.args())
                self.assertEqual(self.snapshot(),before)
        path.write_text(original,encoding='utf-8')
        self.assertEqual(k.record_session(self.vault,self.args())['receipt_status'],'COMPLETE')

    def test_s03_no_change_is_minimal_not_a_page_request(self):
        result = k.record_session(self.vault,self.args(outcome='NO_DURABLE_CHANGE',path=None,summary='',project_slug=''))
        self.assertEqual(result['receipt_status'],'COMPLETE')
        proposal = Path(result['proposal_path']).read_text(encoding='utf-8')
        self.assertEqual(k.fm_list(proposal,'target_pages'),[])
        self.assertIn('No stable-page change requested',proposal)
        self.assertFalse((self.vault/'wiki').exists())
        with self.assertRaises(k.GatewayError):
            k.record_session(self.vault,self.args(outcome='NO_DURABLE_CHANGE'))

    def test_s04_blocked_interrupted_are_not_success(self):
        for outcome in ('BLOCKED','INTERRUPTED'):
            result = k.record_session(self.vault,self.args(outcome=outcome,checkpoint_id=outcome))
            self.assertEqual(result['outcome'],outcome)
            self.assertEqual(result['receipt_status'],'COMPLETE')
            self.assertFalse(result['work_success_attested'])

    def test_s05_sensitive_note_and_bad_identity_refused_before_write(self):
        before = self.snapshot()
        self.note.write_text('gh'+'p_'+'a'*30,encoding='utf-8')
        with self.assertRaises(k.GatewayError):
            k.record_session(self.vault,self.args())
        self.assertEqual(before,self.snapshot())
        with self.assertRaises(k.GatewayError):
            k.record_session(self.vault,self.args(agent='Codex ../Claude'))
        self.assertEqual(before,self.snapshot())

    def test_s05_failed_checkpoint_preserves_pair_for_retry(self):
        with mock.patch.object(k,'checkpoint_intake',return_value={'required':True,'status':'BLOCKED_DIRTY_INDEX'}):
            result = k.record_session(self.vault,self.args())
        self.assertEqual(result['receipt_status'],'CHECKPOINT_BLOCKED')
        before = self.snapshot()
        retried = k.record_session(self.vault,self.args())
        self.assertEqual(retried['receipt_status'],'COMPLETE')
        self.assertEqual(retried['proposal_id'],result['proposal_id'])
        self.assertEqual(before,self.snapshot())

    def test_s05_permission_denial_never_becomes_complete(self):
        with mock.patch.object(k,'stage_source',side_effect=PermissionError('synthetic ACL denial')):
            with self.assertRaises(PermissionError):
                k.record_session(self.vault,self.args())
        self.assertTrue(self.note.is_file())
        self.assertEqual(k.source_records(self.vault),[])

    def test_s05_wrong_context_caller_refuses(self):
        with mock.patch.object(k,'inspect_context_receipt',side_effect=k.ContextReceiptError('CALLER_MISMATCH','synthetic caller mismatch')):
            with self.assertRaisesRegex(k.GatewayError,'CALLER_MISMATCH'):
                k.record_session(self.vault,self.args(context_receipt='synthetic-receipt.json'))
        self.assertEqual(k.source_records(self.vault),[])

    def test_s05_stale_valid_context_does_not_block_closeout(self):
        with mock.patch.object(k,'inspect_context_receipt',return_value={'fresh':False,'freshness_reason':'PROJECT_CHANGED'}):
            result=k.record_session(self.vault,self.args(context_receipt='synthetic-receipt.json'))
        self.assertEqual(result['receipt_status'],'COMPLETE')
        self.assertTrue(result['prior_context_stale'])

    def test_s06_fresh_consumer_finds_pending_session_and_wiki_unchanged(self):
        page=self.vault/'wiki/projects/alpha-project.md'
        k.write_text(page,'---\ntitle: Alpha project\ntype: project\nstatus: reviewed\nsummary: Baseline\nlast_verified: 2026-09-12\nsources: []\n---\n\nBaseline.\n')
        before=page.read_bytes()
        result=k.record_session(self.vault,self.args())
        command=[sys.executable,'-I','-X','utf8','-B',str(Path(k.__file__)), 'project-brief','--vault',str(self.vault),'--project-slug','alpha-project','--query','Synthetic parser','--require-context']
        run=subprocess.run(command,capture_output=True,text=True,encoding='utf-8',timeout=30)
        self.assertEqual(run.returncode,0,run.stderr)
        self.assertIn(result['proposal_id'],run.stdout)
        self.assertNotIn('producer-attested-handoff',run.stdout)
        self.assertEqual(page.read_bytes(),before)

    def test_s07_critical_path_still_works_without_session_duplicate(self):
        result=k.record_handoff(self.vault,argparse.Namespace(
            agent='codex',project_slug='alpha-project',state_summary='Synthetic environment evidence.',
            title='Critical fixture',origin='fixture',path=str(self.note),classification='internal',
            target_page=['wiki/projects/alpha-project.md'],context_receipt=None,session_id=None))
        self.assertEqual(result['receipt_status'],'COMPLETE')
        self.assertEqual(len(k.source_records(self.vault)),1)
        self.assertEqual(len(k.proposal_records(self.vault)),1)

    def test_cli_has_one_portable_entrypoint(self):
        args=k.parser().parse_args(['record-session','--agent','grok','--session-id','s1','--checkpoint-id','c1','--outcome','NO_DURABLE_CHANGE'])
        self.assertEqual(args.action,'record-session')
        ps=Path(k.__file__).with_suffix('.ps1').read_text(encoding='utf-8')
        self.assertIn("$RequestedAction -eq 'record-session'",ps)
        self.assertIn("'--checkpoint-id', $CheckpointId",ps)

    def test_non_owner_cannot_accept_missing_event(self):
        result=k.record_session(self.vault,self.args())
        for event,path in k.readable_event_records(self.vault):
            if event.get('action') == 'new-proposal':
                path.unlink()
        retried=k.record_session(self.vault,self.args())
        self.assertNotEqual(retried['receipt_status'],'COMPLETE')
        self.assertFalse(retried['events_complete'])
        self.assertEqual(retried['proposal_id'],result['proposal_id'])

    def test_real_owner_checkpoint_and_duplicate_are_clean(self):
        def git(*args):
            return subprocess.run(['git','-C',str(self.home),*args],check=True,capture_output=True,text=True)
        git('init','--quiet','--initial-branch=main')
        git('config','user.name','Session Test')
        git('config','user.email','session-test@example.invalid')
        k.write_json(self.vault/'knowledge-vault.json',dict(curator_machine=k.local_machine_name(),
            curation_scope='designated-machine-only',auto_checkpoint_intake=True,checkpoint_mode=k.CHECKPOINT_MODE))
        git('add','vault')
        git('commit','--quiet','-m','Synthetic baseline')
        first=k.record_session(self.vault,self.args())
        self.assertEqual(first['receipt_status'],'COMPLETE')
        self.assertEqual(first['checkpoint']['status'],'CHECKPOINTED')
        second=k.record_session(self.vault,self.args())
        self.assertEqual(second['checkpoint']['status'],'CURRENT')
        self.assertEqual(git('status','--short','--','vault').stdout,'')


if __name__ == '__main__':
    unittest.main()
