"""Retained candidate rebase: real loader, synthetic source/target, no models."""
import json
import unittest
from pathlib import Path
from unittest.mock import patch
import curate_once as curator
from caretaker import prepared_rebase
from caretaker.publication import PublicationError, freeze
import test_phase_resume as fx


class PreparedTargetRebaseTests(unittest.TestCase):
    def fixture(self, started=False):
        f = fx.PhaseResumeTests(); f.setUp()
        clock = fx.Clock(); f.maker = fx.Maker(clock, advance=350)
        first = f._run(budget=f._budget(clock, 500))
        self.assertEqual(first['status'], 'DEFERRED_PREPARED')
        pointer = f._phase_path()
        record = json.loads(pointer.read_text())
        if started:
            record.update(phase='REPAIR_STARTED', candidate=None, manifest=None,
                          manifest_sha256=None, pins={},
                          code_version=prepared_rebase.OLD60_CODE_VERSION)
            record['progress'].update(phase='REPAIR_STARTED', review_calls=1,
                                     assessment={'verdict': 'REPAIR', 'findings': []}, repair_attempt=1)
            self.save(pointer, record)
        page = f.vault/'wiki/projects/new-slug.md'
        page.parent.mkdir(parents=True, exist_ok=True)
        page.write_text('---\nid: new-slug\ntype: project\nstatus: reviewed\nsources: []\n---\n# New project\nEarlier accepted knowledge.\n', encoding='utf-8')
        return f, pointer, record, Path(first['run_path'])/'result.json', first

    def save(self, pointer, record):
        record['cache_sha256'] = curator._phase_digest(record)
        pointer.write_text(json.dumps(record), encoding='utf-8')

    def resume(self, f):
        original = curator._load_phase_progress
        def load(cfg, paths, state, frozen):
            return prepared_rebase.load_with_prepared_rebase(original, cfg, paths, state, frozen)
        with patch.object(curator, '_load_phase_progress', side_effect=load):
            return f._run()

    def test_changed_target_reuses_maker_and_requires_fresh_review(self):
        for started in (False, True):
            with self.subTest(started=started):
                f, pointer, record, result, first = self.fixture(started)
                previous = result.read_bytes()
                out = self.resume(f)
                self.assertEqual(out['status'], 'PUBLISHED')
                self.assertEqual(f.maker.calls, 1)
                self.assertEqual(f.reviewer.calls, 1)
                archive = json.loads((result.parent/'prepared-rebase-archive.json').read_text())
                self.assertEqual(archive['original_record'], record)
                self.assertEqual(archive['original_result'], json.loads(previous))
                candidate = Path(out['candidate_path'])
                manifest = json.loads((candidate/'publication.json').read_text())
                payload = (candidate/manifest['outputs'][0]['payload_path']).read_text()
                self.assertIn('Earlier accepted knowledge.', payload)
                self.assertIn('Dated tool canary report', payload)

    def test_unsafe_or_non_budget_result_never_reaches_review(self):
        cases = ['native_timeout','UNKNOWN','RUNNING','PUBLISHED','missing_result',
                 'manifest','snapshot','target','proposal_ids','run_path','candidate_path',
                 'publication','checkpoint']
        for started in (False, True):
            for case in cases:
                with self.subTest(started=started, case=case):
                    f, pointer, record, path, first = self.fixture(started)
                    body = json.loads(path.read_text())
                    if case == 'native_timeout': body['reason'] = case
                    elif case in {'UNKNOWN','RUNNING','PUBLISHED'}: body['status'] = case
                    elif case == 'manifest': body['manifest_sha256'] = '0'*64
                    elif case == 'snapshot': body['snapshot_sha256'] = '0'*64
                    elif case == 'target': body['target'] = 'wiki/projects/other.md'
                    elif case == 'proposal_ids': body['proposal_ids'] = ['prop-other']
                    elif case in {'run_path','candidate_path'}: body[case] = str(f.root/'other')
                    elif case in {'publication','checkpoint'}: body[case] = {'status':'PUBLISHED'}
                    if case == 'missing_result': path.rename(path.with_name('old-result-retained.json'))
                    else: path.write_text(json.dumps(body), encoding='utf-8')
                    with self.assertRaises(PublicationError): self.resume(f)
                    self.assertEqual(f.maker.calls, 1)
                    self.assertEqual(f.reviewer.calls, 0)

    def test_bad_retained_input_or_progress_cannot_be_laundered(self):
        for case in ['source','proposal','payload','ledger','journal','dirty_progress','wrong_version','bad_digest']:
            with self.subTest(case=case):
                f, pointer, record, result, first = self.fixture()
                candidate = Path(first['candidate_path'])
                manifest = json.loads((candidate/'publication.json').read_text())
                if case == 'source': (f.vault/'raw/sources/2026/09/src-one/original.md').write_text('changed')
                elif case == 'proposal': (f.vault/f.paths[0]).write_text((f.vault/f.paths[0]).read_text()+'\nChanged source claim.\n')
                elif case in {'payload','ledger'}:
                    rel = manifest['ledger_path'] if case == 'ledger' else manifest['outputs'][0]['payload_path']
                    (candidate/rel).write_text('changed')
                elif case == 'journal':
                    p = candidate/'.curation-transactions/tx-test.json'; p.parent.mkdir(); p.write_text('{"status":"prepared"}')
                elif case == 'dirty_progress': record['progress']['reviewer_format_repairs'] = {'round1':1}; self.save(pointer,record)
                elif case == 'wrong_version': record['code_version'] = 'unqualified'; self.save(pointer,record)
                elif case == 'bad_digest': record['cache_sha256'] = '0'*64; pointer.write_text(json.dumps(record))
                with self.assertRaises(PublicationError): self.resume(f)
                self.assertEqual(f.maker.calls,1)
                self.assertEqual(f.reviewer.calls,0)

    def test_unchanged_binding_is_not_a_rebase(self):
        f, pointer, record, result, first = self.fixture()
        page = f.vault/'wiki/projects/new-slug.md'
        page.rename(page.with_name('retained-later-page.md'))
        frozen = freeze(f.vault, f.paths, 'wiki/projects/new-slug.md')
        self.assertIsNone(prepared_rebase._rebase_plan(f.cfg,f.paths,f.state/'curation',frozen))


if __name__ == '__main__': unittest.main()
