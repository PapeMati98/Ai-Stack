import hashlib,json,subprocess,tempfile,unittest
from unittest.mock import patch
from pathlib import Path
from caretaker.checkpoint import checkpoint_publication
from caretaker.publication import PublicationError

class CheckpointTests(unittest.TestCase):
    def test_exact_publication_only_and_changed_output_refused(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);vault=root/'vault';output=vault/'wiki/projects/p.md';output.parent.mkdir(parents=True);output.write_text('reviewed content')
            foreign=root/'foreign.txt';foreign.write_text('unrelated pending work')
            receipt=root/'receipts';receipt.mkdir();journal=receipt/'tx.json'
            for args in [('init','-b','main'),('config','user.name','Synthetic Test'),('config','user.email','test@example.invalid')]:
                subprocess.run(['git','-C',str(root),*args],check=True,capture_output=True)
            row={'path':'wiki/projects/p.md','after':hashlib.sha256(output.read_bytes()).hexdigest()}
            journal.write_text(json.dumps({'status':'committed','event_id':'evt-test','vault':str(vault),'files':[row]}))
            publication={'status':'PUBLISHED','transaction':str(journal),'event_id':'evt-test'}
            # Seed HEAD without including the untracked foreign file.
            seed=root/'seed';seed.write_text('seed')
            subprocess.run(['git','-C',str(root),'add','seed'],check=True,capture_output=True)
            subprocess.run(['git','-C',str(root),'commit','-m','seed'],check=True,capture_output=True)
            real_run=subprocess.run
            def concurrent_edit(args,**kwargs):
                if 'commit-tree' in args:
                    output.write_text('later concurrent edit')
                return real_run(args,**kwargs)
            with patch('caretaker.checkpoint.subprocess.run',side_effect=concurrent_edit):
                result=checkpoint_publication(publication,vault,receipt)
            self.assertEqual(result['status'],'CHECKPOINTED')
            self.assertEqual(subprocess.check_output(['git','-C',str(root),'show','HEAD:vault/wiki/projects/p.md'],text=True),'reviewed content')
            self.assertEqual(output.read_text(),'later concurrent edit')
            tracked=subprocess.check_output(['git','-C',str(root),'ls-files'],text=True)
            self.assertNotIn('foreign.txt',tracked)
            self.assertNotIn('receipts/',tracked)
            output.write_text('concurrent edit')
            with self.assertRaisesRegex(PublicationError,'changed_before_checkpoint'):
                checkpoint_publication(publication,vault,receipt)



# --- 0.1.17: serialization with intake checkpoints, plumbing commit, exact retry proof ---

import os  # noqa: E402
import shutil  # noqa: E402

HUB_SCRIPTS = Path(__file__).resolve().parents[4] / 'knowledge-hub' / 'skills' / 'llm-wiki' / 'scripts'


def _git(root, *args):
    return subprocess.run(['git', '-c', 'core.autocrlf=false', '-C', str(root), *args],
                          check=True, capture_output=True)


def _repo(root):
    vault = root / 'vault'
    (vault / 'wiki' / 'projects').mkdir(parents=True)
    for args in [('init', '-b', 'main'), ('config', 'user.name', 'Synthetic Test'),
                 ('config', 'user.email', 'test@example.invalid')]:
        _git(root, *args)
    return vault


def _write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data.encode('utf-8') if isinstance(data, str) else data)


def _commit(root, message, *paths):
    _git(root, 'add', *(paths or ('-A',)))
    _git(root, 'commit', '-m', message)


def _row(vault, name, before=None):
    row = {'path': name, 'after': hashlib.sha256((vault / name).read_bytes()).hexdigest()}
    if before is not None:
        row['before'] = before
    return row


def _publication(root, vault, rows, event='evt-test'):
    receipt = root / 'receipts'
    receipt.mkdir(exist_ok=True)
    journal = receipt / 'tx.json'
    journal.write_text(json.dumps({'status': 'committed', 'event_id': event, 'vault': str(vault), 'files': rows}))
    return {'status': 'PUBLISHED', 'transaction': str(journal), 'event_id': event}, receipt


def _staged(root):
    return _git(root, 'diff', '--cached', '--name-only').stdout.decode().strip()


def _head(root):
    return _git(root, 'rev-parse', 'HEAD').stdout.decode().strip()


def _lock(root):
    return root / '.git' / 'llm-wiki-intake-checkpoint.lock'


def _simple(root, pages=('wiki/projects/p.md',)):
    vault = _repo(root)
    _write(vault / 'log.md', 'seed\n'); _commit(root, 'seed')
    for page in pages:
        _write(vault / page, 'reviewed ' + page)
    return vault, _publication(root, vault, [_row(vault, page) for page in pages])


def _on(trigger, action):
    """subprocess.run that performs `action(args)` right before any git call containing `trigger`."""
    real_run = subprocess.run

    def run(args, **kwargs):
        if trigger in args:
            replaced = action(args)
            if replaced is not None:
                return replaced
        return real_run(args, **kwargs)
    return run


def _index_bytes(root):
    return (root / '.git' / 'index').read_bytes()


class CheckpointSerializationTests(unittest.TestCase):
    def test_busy_intake_lock_refuses_without_touching_the_index(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); vault, (publication, receipt) = _simple(root)
            _lock(root).write_text(json.dumps({'lock_id': 'intake-writer', 'pid': 1, 'machine': 'X',
                                               'created_at': '2026-09-23T06:12:58.000Z'}) + '\n')
            with patch('caretaker.checkpoint.LOCK_WAIT_SECONDS', 0.3):
                with self.assertRaisesRegex(PublicationError, 'checkpoint_lock_busy:intake'):
                    checkpoint_publication(publication, vault, receipt)
            self.assertEqual(_staged(root), '')
            self.assertEqual(json.loads(_lock(root).read_text())['lock_id'], 'intake-writer')

    def test_busy_git_index_is_waited_for_and_never_taken(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); vault, (publication, receipt) = _simple(root)
            (root / '.git' / 'index.lock').write_bytes(b'another git process')
            with patch('caretaker.checkpoint.LOCK_WAIT_SECONDS', 0.3):
                with self.assertRaisesRegex(PublicationError, 'git_index_busy'):
                    checkpoint_publication(publication, vault, receipt)
            self.assertEqual((root / '.git' / 'index.lock').read_bytes(), b'another git process')
            self.assertFalse(_lock(root).exists())

    def test_lock_payload_matches_the_knowledge_gateway_protocol(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); vault, (publication, receipt) = _simple(root)
            self.assertEqual(checkpoint_publication(publication, vault, receipt)['status'], 'CHECKPOINTED')
            [released] = list((root / '.git').glob('llm-wiki-intake-checkpoint.lock.release-*'))
            payload = json.loads(released.read_text())
            self.assertEqual(set(payload), {'lock_id', 'pid', 'machine', 'created_at', 'agent'})
            self.assertRegex(payload['created_at'], r'^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z$')
            self.assertFalse(_lock(root).exists())
            self.assertFalse((root / '.git' / 'index.lock').exists())
            self.assertEqual(list(receipt.glob('checkpoint-index-*')), [], 'the private index became the index')
            self.assertEqual(_git(root, 'status', '--porcelain', '--', 'vault').stdout.decode().strip(), '')

    def test_lock_that_could_not_be_written_is_not_left_behind(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); vault, (publication, receipt) = _simple(root)
            with patch('caretaker.checkpoint.os.write', side_effect=OSError('disk full')):
                with self.assertRaises(OSError):
                    checkpoint_publication(publication, vault, receipt)
            self.assertFalse(_lock(root).exists())
            self.assertTrue(list((root / '.git').glob('llm-wiki-intake-checkpoint.lock.failed-*')))
            self.assertEqual(checkpoint_publication(publication, vault, receipt)['status'], 'CHECKPOINTED')

    def test_failure_before_the_install_never_touches_the_shared_index(self):
        # 2026-09-23: a checkpoint that staged and could not finish blocked every writer.
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); vault, (publication, receipt) = _simple(root)
            index_before, head_before = _index_bytes(root), _head(root)
            failing = _on('commit-tree', lambda args: subprocess.CompletedProcess(args, 1, b'', b'simulated'))
            with patch('caretaker.checkpoint.subprocess.run', side_effect=failing):
                with self.assertRaisesRegex(PublicationError, 'publication_git_checkpoint_failed'):
                    checkpoint_publication(publication, vault, receipt)
            self.assertEqual(_index_bytes(root), index_before)
            self.assertEqual(_head(root), head_before)
            self.assertFalse((root / '.git' / 'index.lock').exists())
            self.assertTrue(list((root / '.git').glob('index.lock.caretaker-aborted-*')))
            self.assertFalse(_lock(root).exists())
            self.assertEqual(checkpoint_publication(publication, vault, receipt)['status'], 'CHECKPOINTED')

    def test_install_failure_after_the_commit_is_reported_for_the_operator(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); vault, (publication, receipt) = _simple(root)
            index_before = _index_bytes(root)
            with patch('caretaker.checkpoint._install_index', side_effect=OSError('sharing violation')):
                with self.assertRaisesRegex(PublicationError, 'checkpoint_index_install_failed_needs_operator'):
                    checkpoint_publication(publication, vault, receipt)
            self.assertEqual(_git(root, 'show', 'HEAD:vault/wiki/projects/p.md').stdout.decode(),
                             'reviewed wiki/projects/p.md', 'the verified commit exists')
            self.assertEqual(_index_bytes(root), index_before)
            self.assertFalse((root / '.git' / 'index.lock').exists())
            self.assertFalse((receipt / 'checkpoint.json').exists(), 'no clean receipt was written')

    def test_failed_lock_release_is_never_silent(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); vault, (publication, receipt) = _simple(root)
            with patch('caretaker.checkpoint._release_intake_lock', return_value=False):
                with self.assertRaisesRegex(PublicationError, 'checkpoint_lock_release_failed'):
                    checkpoint_publication(publication, vault, receipt)

    def test_git_writers_cannot_touch_the_index_during_the_transaction(self):
        # A Git writer that ignores the intake lock is stopped by Git's own index lock:
        # its stage of a foreign file or of an own path fails and nothing of it is lost.
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            vault, (publication, receipt) = _simple(root, pages=('wiki/projects/p.md', 'wiki/projects/q.md'))
            _write(root / 'foreign.txt', 'someone else\n')
            attempts = []

            def other_writer(args):
                for cmd in (['add', 'foreign.txt'], ['restore', '--staged', '--', 'vault/wiki/projects/q.md'],
                            ['add', 'vault/wiki/projects/p.md']):
                    attempts.append(subprocess.run(['git', '-C', str(root), *cmd], capture_output=True).returncode)
            with patch('caretaker.checkpoint.subprocess.run', side_effect=_on('write-tree', other_writer)):
                result = checkpoint_publication(publication, vault, receipt)
            self.assertEqual(result['status'], 'CHECKPOINTED')
            self.assertTrue(all(code != 0 for code in attempts), attempts)
            self.assertEqual(sorted(_git(root, 'show', '--format=', '--name-only', 'HEAD').stdout.decode().split()),
                             ['vault/wiki/projects/p.md', 'vault/wiki/projects/q.md'])
            self.assertEqual(_staged(root), '')
            self.assertEqual(_git(root, 'add', 'foreign.txt').returncode, 0, 'the other writer can retry afterwards')

    def test_index_changed_outside_git_commits_nothing(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); vault, (publication, receipt) = _simple(root)
            head_before = _head(root)

            def raw_write(args):
                index = root / '.git' / 'index'        # a writer that bypasses Git's lock
                data = bytearray(index.read_bytes()); data[-1] ^= 1
                index.write_bytes(bytes(data))
            with patch('caretaker.checkpoint.subprocess.run', side_effect=_on('commit-tree', raw_write)):
                with self.assertRaisesRegex(PublicationError, 'git_index_changed_outside_git'):
                    checkpoint_publication(publication, vault, receipt)
            self.assertEqual(_head(root), head_before)
            self.assertFalse((root / '.git' / 'index.lock').exists())

    def test_foreign_commit_during_the_checkpoint_is_never_rolled_back(self):
        # The branch moved after `before` was read: compare-and-swap refuses, nothing is undone.
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); vault, (publication, receipt) = _simple(root)
            index_before = _index_bytes(root)

            real_run = subprocess.run      # the patched run would re-enter this trigger

            def foreign_commit(args):
                def raw(*cmd):
                    return real_run(['git', '-C', str(root), *cmd], check=True, capture_output=True).stdout.decode().strip()
                commit = raw('commit-tree', raw('rev-parse', 'HEAD^{tree}'), '-p', 'HEAD', '-m', 'foreign')
                raw('update-ref', 'HEAD', commit)
            with patch('caretaker.checkpoint.subprocess.run', side_effect=_on('commit-tree', foreign_commit)):
                with self.assertRaisesRegex(PublicationError, 'checkpoint_branch_moved_retry'):
                    checkpoint_publication(publication, vault, receipt)
            self.assertEqual(_git(root, 'log', '-1', '--format=%s').stdout.decode().strip(), 'foreign')
            self.assertEqual(_index_bytes(root), index_before)
            result = checkpoint_publication(publication, vault, receipt)
            self.assertEqual(result['status'], 'CHECKPOINTED')
            self.assertEqual(_git(root, 'log', '-2', '--format=%s').stdout.decode().split('\n')[1], 'foreign')

    def test_inherited_git_variables_cannot_redirect_the_checkpoint(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); vault, (publication, receipt) = _simple(root)
            bogus = {'GIT_INDEX_FILE': str(root / 'other-index'), 'GIT_WORK_TREE': str(root / 'elsewhere'),
                     'GIT_DIR': str(root / 'nowhere')}
            with patch.dict(os.environ, bogus):
                result = checkpoint_publication(publication, vault, receipt)
            self.assertEqual(result['status'], 'CHECKPOINTED')
            self.assertEqual(_git(root, 'show', 'HEAD:vault/wiki/projects/p.md').stdout.decode(), 'reviewed wiki/projects/p.md')
            self.assertEqual(_git(root, 'status', '--porcelain', '--', 'vault').stdout.decode().strip(), '')
            self.assertFalse((root / 'other-index').exists())

    def test_foreign_blob_on_an_own_path_is_never_discarded(self):
        # Index-only bytes of another writer while the worktree holds the verified publication.
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); vault, (publication, receipt) = _simple(root)
            _write(vault / 'wiki/projects/p.md', 'someone else')
            _git(root, 'add', 'vault/wiki/projects/p.md')
            _write(vault / 'wiki/projects/p.md', 'reviewed wiki/projects/p.md')
            index_before = _index_bytes(root)
            with self.assertRaisesRegex(PublicationError, 'foreign_staged_changes_keep_pending'):
                checkpoint_publication(publication, vault, receipt)
            self.assertEqual(_index_bytes(root), index_before)
            self.assertEqual(_git(root, 'show', ':vault/wiki/projects/p.md').stdout.decode(), 'someone else')

    def test_retry_with_nothing_to_commit_still_guards_the_branch_and_the_index(self):
        # A retry after a completed commit (e.g. the receipt could not be written) has an
        # empty `must`; the compare-and-swap and the index guard must still hold.
        for disturbance, reason in (('soft-reset', 'checkpoint_branch_moved_retry'),
                                    ('raw-index-write', 'git_index_changed_outside_git')):
            with self.subTest(disturbance=disturbance), tempfile.TemporaryDirectory() as temp:
                root = Path(temp); vault, (publication, receipt) = _simple(root)
                self.assertEqual(checkpoint_publication(publication, vault, receipt)['status'], 'CHECKPOINTED')
                _write(root / 'extra.txt', 'x'); _commit(root, 'another commit', 'extra.txt')
                head_before = _head(root)
                real_run = subprocess.run

                def disturb(args):
                    if disturbance == 'soft-reset':
                        real_run(['git', '-C', str(root), 'reset', '--soft', 'HEAD~1'], check=True, capture_output=True)
                    else:
                        index = root / '.git' / 'index'
                        data = bytearray(index.read_bytes()); data[-1] ^= 1
                        index.write_bytes(bytes(data))
                with patch('caretaker.checkpoint.subprocess.run', side_effect=_on('write-tree', disturb)):
                    with self.assertRaisesRegex(PublicationError, reason):
                        checkpoint_publication(publication, vault, receipt)
                self.assertFalse((root / '.git' / 'index.lock').exists())
                if disturbance == 'soft-reset':
                    self.assertNotEqual(_head(root), head_before, 'the other writer\'s reset is not undone')

    def test_split_index_is_refused_in_every_boolean_spelling(self):
        for value, refused in (('true', True), ('yes', True), ('on', True), ('1', True), ('maybe', True),
                               ('false', False), ('no', False), ('off', False), ('0', False)):
            with self.subTest(value=value), tempfile.TemporaryDirectory() as temp:
                root = Path(temp); vault, (publication, receipt) = _simple(root)
                (root / '.git' / 'config').write_text(
                    (root / '.git' / 'config').read_text() + '[core]\n\tsplitIndex = %s\n' % value)
                if refused:
                    with self.assertRaisesRegex(PublicationError, 'checkpoint_split_index_unsupported'):
                        checkpoint_publication(publication, vault, receipt)
                else:
                    self.assertEqual(checkpoint_publication(publication, vault, receipt)['status'], 'CHECKPOINTED')

    def test_foreign_mode_change_with_the_right_blob_is_refused(self):
        # Ownership is the complete entry: the verified blob AND a regular-file mode.
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); vault, (publication, receipt) = _simple(root)
            _git(root, 'add', 'vault/wiki/projects/p.md')
            _git(root, 'update-index', '--chmod=+x', 'vault/wiki/projects/p.md')
            index_before = _index_bytes(root)
            with self.assertRaisesRegex(PublicationError, 'foreign_staged_changes_keep_pending'):
                checkpoint_publication(publication, vault, receipt)
            self.assertEqual(_index_bytes(root), index_before)

    def test_own_staging_left_by_an_interrupted_run_is_accepted(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); vault, (publication, receipt) = _simple(root)
            _git(root, 'add', 'vault/wiki/projects/p.md')        # a run that died after `git add`
            self.assertEqual(checkpoint_publication(publication, vault, receipt)['status'], 'CHECKPOINTED')
            self.assertEqual(_staged(root), '')

    def test_planted_hook_file_is_refused(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); vault, (publication, receipt) = _simple(root)
            _write(receipt / 'empty-hooks' / 'post-commit', '#!/bin/sh\nexit 0\n')
            with self.assertRaisesRegex(PublicationError, 'checkpoint_hooks_dir_not_empty'):
                checkpoint_publication(publication, vault, receipt)


@unittest.skipUnless((HUB_SCRIPTS / 'curation.py').is_file(), 'needs the Knowledge Hub generator')
class SharedFileRetryProofTests(unittest.TestCase):
    """log.md and wiki/index.md are regenerated from the whole vault on every publication."""

    def _setup(self, root):
        vault = _repo(root)
        scripts = root / 'skills' / 'llm-wiki' / 'scripts'
        scripts.mkdir(parents=True)
        for name in ('curation.py', 'knowledge.py'):
            shutil.copyfile(HUB_SCRIPTS / name, scripts / name)
        from caretaker.checkpoint import _generator
        gen = _generator(vault)
        page = ('---\ntitle: First ledger\ntype: evidence-ledger\nstatus: reviewed\n'
                'verification_scope: producer-reports\nlast_reviewed: 2026-09-23\nsummary: First.\n---\n\nBody\n')
        event = {'event_id': 'evt-first', 'occurred_at': '2026-09-23T06:15:09.212Z', 'agent': 'openclaw-caretaker',
                 'action': 'record-curation', 'summary': 'First publication'}
        seed = {**event, 'event_id': 'evt-seed', 'summary': 'seed'}
        _write(vault / 'events/2026/09/evt-seed.json', json.dumps(seed))
        _write(vault / 'log.md', gen._merge_log({}, seed))
        _write(vault / 'wiki/index.md', gen._merge_index({}, []))
        _commit(root, 'seed')
        befores = {n: hashlib.sha256((vault / n).read_bytes()).hexdigest() for n in ('log.md', 'wiki/index.md')}
        _write(vault / 'wiki/history/h1.md', page)
        _write(vault / 'events/2026/09/evt-first.json', json.dumps(event))
        _write(vault / 'log.md', gen._merge_log({'events/2026/09/evt-seed.json': json.dumps(seed).encode()}, event))
        _write(vault / 'wiki/index.md', gen._merge_index({}, [{'relative': 'wiki/history/h1.md', 'content': page.encode()}]))
        rows = [_row(vault, 'wiki/history/h1.md'), _row(vault, 'events/2026/09/evt-first.json'),
                _row(vault, 'log.md', befores['log.md']), _row(vault, 'wiki/index.md', befores['wiki/index.md'])]
        return vault, gen, event, page, _publication(root, vault, rows, 'evt-first')

    def _later(self, root, vault, gen, event, page):
        later = {**event, 'event_id': 'evt-second', 'occurred_at': '2026-09-23T06:30:46.136Z', 'summary': 'Second'}
        page2 = page.replace('First', 'Second')
        _write(vault / 'log.md', gen._merge_log({'events/2026/09/evt-first.json': json.dumps(event).encode()}, later))
        _write(vault / 'wiki/index.md', gen._merge_index(
            {'wiki/history/h1.md': page.encode()}, [{'relative': 'wiki/history/h2.md', 'content': page2.encode()}]))
        _commit(root, 'later', 'vault/log.md', 'vault/wiki/index.md')

    def test_rows_committed_by_a_later_publication_prove_this_one(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            vault, gen, event, page, (publication, receipt) = self._setup(root)
            self._later(root, vault, gen, event, page)
            result = checkpoint_publication(publication, vault, receipt)
            self.assertEqual(result['status'], 'CHECKPOINTED')
            self.assertEqual(result['already_in_head'], ['log.md', 'wiki/index.md'])
            self.assertEqual(_git(root, 'show', 'HEAD:vault/wiki/history/h1.md').stdout.decode(), page)
            self.assertEqual(_git(root, 'status', '--porcelain', '--', 'vault').stdout.decode().strip(), '')

    def test_branch_moved_during_the_locked_proof_commits_nothing(self):
        # The proof runs under Git's index lock against `before`; a branch move in that
        # window (a soft reset needs no index lock) is caught by the compare-and-swap.
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            vault, gen, event, page, (publication, receipt) = self._setup(root)
            self._later(root, vault, gen, event, page)
            later_head = _head(root)
            real_run = subprocess.run
            state = {'done': False}

            def soft_reset(args):
                if not state['done']:
                    state['done'] = True
                    real_run(['git', '-C', str(root), 'reset', '--soft', 'HEAD~1'], check=True, capture_output=True)
            with patch('caretaker.checkpoint.subprocess.run', side_effect=_on('show', soft_reset)):
                with self.assertRaisesRegex(PublicationError, 'checkpoint_branch_moved_retry'):
                    checkpoint_publication(publication, vault, receipt)
            self.assertNotEqual(_head(root), later_head)
            self.assertFalse((root / '.git' / 'index.lock').exists())

    def test_event_id_mentioned_elsewhere_is_not_proof(self):
        # A correction's summary may name this event; that is not this event's row.
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            vault, gen, event, page, (publication, receipt) = self._setup(root)
            other = {**event, 'event_id': 'evt-correction', 'summary': 'Correction of evt-first'}
            _write(vault / 'log.md', gen._merge_log({}, other))
            _commit(root, 'correction without the first event', 'vault/log.md')
            with self.assertRaisesRegex(PublicationError, 'changed_before_checkpoint'):
                checkpoint_publication(publication, vault, receipt)
            self.assertEqual(_staged(root), '')
            self.assertFalse(_lock(root).exists())

    def test_twin_event_with_the_same_visible_row_is_not_proof(self):
        # The log row carries no event id; an identical row may belong to another event.
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            vault, gen, event, page, (publication, receipt) = self._setup(root)
            _write(vault / 'events/2026/09/evt-twin.json', json.dumps({**event, 'event_id': 'evt-twin'}))
            self._later(root, vault, gen, event, page)
            with self.assertRaisesRegex(PublicationError, 'changed_before_checkpoint'):
                checkpoint_publication(publication, vault, receipt)
            self.assertEqual(_staged(root), '')
