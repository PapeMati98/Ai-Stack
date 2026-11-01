"""Synthetic owner opt-in and new-event publication isolation."""
import datetime as dt
import json
import sqlite3
import unittest
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import maintenance
from caretaker.audit import active_cycle, maybe_start_cycle, policy_from, summarize
from caretaker.store import Store
from caretaker.connectors import NormalizedEvent, SourceSpec, collect_sources
from caretaker.config import load_config
from caretaker.hub import FakeKnowledgeGateway, RecordHandoffResult
from caretaker.pipeline import PipelineDeps, run_pipeline
from caretaker.publication import freeze, sha
from caretaker.transports import Envelope, FakeTransport
from .test_pipeline import _office_connector
from .test_accounted_elsewhere import _summary, _control, _cfg
from .test_autonomous_loop import memory_db


def fixture_root():
    # Path.mkdir inherits the workspace ACL; tempfile's private Windows ACL is
    # inaccessible in the isolated contractor worktree. These ignored synthetic
    # fixtures are deliberately retained for inspection.
    root = Path(__file__).resolve().parents[1] / '.test-tmp' / ('new-epoch-' + uuid.uuid4().hex)
    root.mkdir(parents=True)
    return root


def event(cursor, completed_at):
    return {'tool': 'codex', 'native_session_id': 'session-one', 'cursor': cursor,
            'project': 'one', 'observed_at': '2026-09-23T12:00:00Z',
            'completed_at': completed_at, 'title': 'Synthetic turn',
            'excerpt': 'Synthetic bounded evidence', 'classification': 'internal'}


def proposal(vault, ident, source_id, author='codex'):
    folder = vault / 'proposals' / author
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / (ident + '.md')
    path.write_text('---\nid: "' + ident + '"\nclassification: "internal"\n'
                    'agent: "' + author + '"\nproject_slug: "one"\n'
                    'target_pages: ["wiki/projects/one.md"]\n'
                    'source_ids: ["' + source_id + '"]\n---\nSynthetic body\n', encoding='utf-8')
    return path


def agent_proposal(vault, ident, source_id, *, project=None, title=None,
                   author='codex', targets=None):
    folder = vault / 'proposals' / author
    folder.mkdir(parents=True, exist_ok=True)
    fields = [f'id: "{ident}"', 'classification: "internal"',
              f'agent: "{author}"', f'source_ids: ["{source_id}"]']
    if project is not None:
        fields.append(f'project_slug: "{project}"')
    if title is not None:
        fields.append(f'title: "{title}"')
    if targets is not None:
        fields.append('target_pages: ' + json.dumps(targets))
    path = folder / (ident + '.md')
    path.write_text('---\n' + '\n'.join(fields) + '\n---\nSynthetic body\n', encoding='utf-8')
    return path


def queue(db, value):
    ident = maintenance.event_id(value)
    db.execute('INSERT OR IGNORE INTO queue(id,project,payload) VALUES(?,?,?)',
               (ident, 'one', json.dumps(value)))
    db.commit()
    return ident


def intake_db(path):
    db = sqlite3.connect(path)
    db.execute('CREATE TABLE queue(id TEXT PRIMARY KEY,project TEXT,payload TEXT NOT NULL,forwarded INTEGER NOT NULL DEFAULT 0)')
    return db


class NewEventEpochTests(unittest.TestCase):
    def test_registry_alias_proposals_count_canonical_permission_and_target_only(self):
        root = fixture_root()
        vault = root / 'vault'
        db = intake_db(root / 'intake.sqlite')
        maintenance.activate_new_event_epoch(db, vault,
            dt.datetime(2026, 9, 23, 12, tzinfo=dt.timezone.utc))
        registry = vault / 'wiki/registry.md'
        registry.parent.mkdir(parents=True)
        registry.write_text('```json\n' + json.dumps({
            'schema_version': 1,
            'projects': {'one': {'main_page': 'wiki/projects/one.md'},
                         'two': {'main_page': 'wiki/projects/two.md'}},
            'aliases': {'old-one': 'one', 'old-two': 'two'},
        }) + '\n```\n', encoding='utf-8')
        agent_proposal(vault, 'prop-20260923T120200000Z-alias', 'src-alias',
                       project='old-one', targets=['wiki/projects/old-one.md'])
        agent_proposal(vault, 'prop-20260923T120300000Z-target', 'src-target',
                       targets=['wiki/projects/old-one.md'])
        agent_proposal(vault, 'prop-20260923T120400000Z-denied', 'src-denied',
                       project='old-two', targets=['wiki/projects/old-two.md'])
        result = maintenance._agent_proposal_inventory(
            db, vault, {'permitted_projects': ['one']})
        self.assertEqual(result['counts']['eligible'], 2)
        self.assertEqual(result['counts']['pending'], 2)
        self.assertEqual(result['counts']['unroutable'], 1)
        self.assertNotIn('registry_warning', result)
        registry.write_text('```json\n{"schema_version":1,"projects":{},'
                            '"aliases":{"old-one":"missing"}}\n```\n',
                            encoding='utf-8')
        fallback = maintenance._agent_proposal_inventory(
            db, vault, {'permitted_projects': ['one']})
        self.assertEqual(fallback['registry_warning'], 'registry_invalid')
        self.assertEqual(fallback['counts']['unroutable'], 3)
        db.close()

    def test_agent_proposals_route_only_after_activation_and_monitor_accounting(self):
        root = fixture_root()
        state = root / 'state'
        state.mkdir()
        vault = root / 'vault'
        baseline = agent_proposal(vault, 'prop-20260923T120100000Z-baseline',
                                  'src-baseline', project='one', targets=['wiki/projects/one.md'])
        db = intake_db(state / 'intake.sqlite')
        maintenance.activate_new_event_epoch(db, vault,
            dt.datetime(2026, 9, 23, 12, tzinfo=dt.timezone.utc))
        explicit = agent_proposal(vault, 'prop-20260923T120200000Z-explicit',
                                  'src-explicit', project='one', targets=['wiki/projects/one.md'])
        journal = agent_proposal(vault, 'prop-20260923T120300000Z-journal',
                                 'src-journal', title='Session one NO_DURABLE_CHANGE abc123')
        accounted = agent_proposal(vault, 'prop-20260923T120400000Z-accounted',
                                   'src-accounted', project='one', targets=['wiki/projects/one.md'])
        outside = agent_proposal(vault, 'prop-20260923T120500000Z-outside',
                                 'src-outside', project='two', targets=['wiki/projects/two.md'])
        missing = agent_proposal(vault, 'prop-20260923T120600000Z-missing',
                                 'src-missing', title='Session without project')
        older = agent_proposal(vault, 'prop-20260923T115900000Z-older',
                               'src-older', project='one', targets=['wiki/projects/one.md'])
        caretaker = agent_proposal(vault, 'prop-20260923T120700000Z-caretaker',
                                   'src-caretaker', project='one', author='openclaw-caretaker',
                                   targets=['wiki/projects/one.md'])
        ledger = vault / 'governance' / 'curations'
        ledger.mkdir(parents=True)
        ledger.joinpath('accounted.json').write_text(json.dumps({'proposals': [
            {'proposal_id': accounted.stem, 'disposition': 'synthesized'}]}), encoding='utf-8')
        control = {'permitted_projects': ['one']}
        inventory = maintenance._agent_proposal_inventory(db, vault, control)
        self.assertEqual(inventory['counts'],
                         {'eligible': 3, 'accounted': 1, 'pending': 2,
                          'unroutable': 2, 'unsupported': 0})
        filt = maintenance._new_epoch_proposal_filter(db, root, control, vault)
        def meta(path, project):
            return {'path': path.relative_to(vault).as_posix(),
                    'proposal_id': path.stem, 'project': project,
                    'producer': path.parent.name, 'source_ids': ['src-test']}
        self.assertTrue(filt(meta(explicit, 'one')))
        self.assertTrue(filt(meta(journal, 'one')))
        for path, project in ((baseline, 'one'), (accounted, 'one'),
                              (outside, 'two'), (missing, '_unscoped'),
                              (older, 'one'), (caretaker, 'one')):
            self.assertFalse(filt(meta(path, project)), path.name)
        from caretaker.audit import eligible_proposals
        found, _ = eligible_proposals(vault)
        self.assertIn(journal.stem, {item['proposal_id'] for item in found})
        db.close()
        base_path = root / 'base.json'
        base_path.write_text(json.dumps({'hub': {'vault': str(vault)}, 'office': {},
                                         'allowlist': {}, 'limits': {}}), encoding='utf-8')
        tick_control = {'base_config': str(base_path), 'sources': [], 'known_projects': [],
                        'permitted_projects': ['one'], 'max_projects_per_tick': 0,
                        'publication': {'enabled': False}, 'new_event_epoch': {'enabled': True}}
        report = maintenance._locked_tick(tick_control, state)
        self.assertEqual(report['new_lane']['agent_proposals'], inventory['counts'])
        self.assertNotIn('Synthetic body', json.dumps(report['new_lane']))

    def test_opted_in_documents_use_observation_time_and_preserve_baseline(self):
        root = fixture_root()
        vault = root / 'vault'
        vault.mkdir()
        db = intake_db(root / 'intake.sqlite')
        sha = 'a' * 64

        def document(cursor, observed, source_id='owner-files-v2', rejected=False):
            value = event(cursor, '2020-01-01T00:00:00Z')
            value.update(tool='files', native_session_id=f'{source_id}:notes',
                         event_type='owner_document.v1', completion_kind='owner_document',
                         observed_at=observed,
                         source_ref={'source_id': source_id, 'relative_path': 'notes.md',
                                     'sha256': sha})
            if rejected:
                value['source_ref']['rejection_reason'] = 'secret_detected'
            return value

        baseline = queue(db, document('baseline', '2026-09-23T12:02:00Z'))
        maintenance.activate_new_event_epoch(db, vault,
            dt.datetime(2026, 9, 23, 12, tzinfo=dt.timezone.utc))
        ids = {
            'admitted': queue(db, document('admitted', '2026-09-23T12:01:00Z')),
            'before': queue(db, document('before', '2026-09-23T11:59:00Z')),
            'other': queue(db, document('other', '2026-09-23T12:01:00Z', 'unknown')),
            'rejected': queue(db, document('rejected', '2026-09-23T12:01:00Z', rejected=True)),
        }
        control = {'document_epoch': {'enabled': True, 'source_ids': ['owner-files-v2']},
                   'sources': [{'id': 'owner-files-v2', 'kind': 'files', 'scheduled': True,
                                'status': 'supported', 'file_observation_mode': 'scan_time',
                                'roots': [{'path': str(root)}]}]}
        allowed = maintenance._document_epoch_source_ids(control)
        maintenance._admit_new_epoch_events(db, allowed)
        rows = dict(db.execute('SELECT event_id,status FROM new_epoch_events'))
        self.assertNotIn(baseline, rows)
        self.assertEqual(rows[ids['admitted']], 'admitted')
        self.assertEqual([rows[ids[name]] for name in ('before', 'other', 'rejected')],
                         ['deferred'] * 3)
        maintenance._admit_new_epoch_events(db, allowed)
        self.assertEqual(db.execute('SELECT count(*) FROM new_epoch_events').fetchone()[0], 4)
        control['sources'][0]['scheduled'] = False
        with self.assertRaisesRegex(ValueError, 'document_epoch_source_not_eligible'):
            maintenance._document_epoch_source_ids(control)
        control['sources'][0]['scheduled'] = True
        control['sources'][0]['file_observation_mode'] = 'mtime'
        with self.assertRaisesRegex(ValueError, 'document_epoch_source_not_eligible'):
            maintenance._document_epoch_source_ids(control)
        control['sources'][0]['file_observation_mode'] = 'scan_time'
        control['sources'].append(dict(control['sources'][0]))
        with self.assertRaisesRegex(ValueError, 'duplicate_file_source_id'):
            maintenance._document_epoch_source_ids(control)
        db.close()

    def test_agent_proposal_uses_existing_new_publication_path(self):
        root = fixture_root()
        vault = root / 'vault'
        vault.mkdir()
        db = intake_db(root / 'intake.sqlite')
        now = dt.datetime(2026, 9, 23, 12, tzinfo=dt.timezone.utc)
        maintenance.activate_new_event_epoch(db, vault, now)
        db.execute('CREATE TABLE publications(batch TEXT PRIMARY KEY,status TEXT,failures INTEGER,result TEXT)')
        path = agent_proposal(vault, 'prop-20260923T120100000Z-pending',
                              'src-pending', project='one', targets=['wiki/projects/one.md'])
        base = {'hub': {'vault': str(vault)}, 'office': {}, 'allowlist': {}, 'limits': {}}
        control = {'known_projects': ['one'], 'permitted_projects': ['one'],
                   'publication': {'enabled': True, 'min_ready_groups': 1}}
        held = maintenance._held_legacy_publication(base, control, db, root, now)
        self.assertEqual(held['raw_eligible'], 0)
        with patch('maintenance._invoke_curate', return_value={'status': 'PUBLISHED'}) as invoke:
            result = maintenance.publication_drain(control, base, db, root, now=now, lane='new')
        self.assertEqual(result['batches'][0]['status'], 'PUBLISHED')
        self.assertEqual(invoke.call_args.args[1], [path.relative_to(vault).as_posix()])
        db.close()

    def test_no_durable_change_journal_can_freeze_for_its_project(self):
        root = fixture_root()
        vault = root / 'vault'
        journal = agent_proposal(vault, 'prop-20260923T120300000Z-journal',
                                 'src-journal', title='Session one NO_DURABLE_CHANGE abc123')
        source = vault / 'raw/sources/2026/09/src-journal'
        source.mkdir(parents=True)
        data = b'Synthetic session closeout; no durable change.'
        (source / 'original.md').write_bytes(data)
        (source / 'source.json').write_text(json.dumps({
            'source_id': 'src-journal', 'classification': 'internal', 'local_only': False,
            'original_file': 'original.md', 'sha256': sha(data), 'byte_length': len(data)}),
            encoding='utf-8')
        page = vault / 'wiki/projects/one.md'
        page.parent.mkdir(parents=True)
        page.write_text('---\nstatus: reviewed\nproject_slug: one\n---\nExisting page\n',
                        encoding='utf-8')
        frozen = freeze(vault, [journal.relative_to(vault).as_posix()],
                        'wiki/projects/one.md')
        self.assertEqual(frozen['proposals'][0]['id'], journal.stem)

    def test_caretaker_fleet_receipts_are_deferred_once_without_degrading(self):
        root = fixture_root()
        state = root / 'state'
        state.mkdir()
        vault = root / 'vault'
        vault.mkdir()
        base_path = root / 'base.json'
        base_path.write_text(json.dumps({'hub': {'vault': str(vault)}, 'office': {},
                                         'allowlist': {}, 'limits': {}}), encoding='utf-8')
        db = intake_db(state / 'intake.sqlite')
        maintenance.activate_new_event_epoch(db, vault,
            dt.datetime(2026, 9, 23, 12, tzinfo=dt.timezone.utc))
        ids = []
        for name in ('stored', 'stored-deferred', 'new'):
            value = event(name, '2026-09-23T12:01:00Z')
            value.update(tool='mimo', native_session_id='logical:fleet-receipt:' + name,
                         completion_kind='verified_fleet_handoff',
                         cwd=str(state / 'new-event-epoch' / 'projects' / 'one' / 'state' / 'curation'))
            ident = maintenance.event_id(value)
            ids.append(ident)
            db.execute('INSERT INTO queue(id,project,payload) VALUES(?,NULL,?)',
                       (ident, json.dumps(value)))
        db.execute("INSERT INTO new_epoch_events(event_id,status,reason,completed_at) VALUES(?,'admitted','','2026-09-23T12:01:00+00:00')",
                   (ids[0],))
        db.execute("INSERT INTO new_epoch_events(event_id,status,reason,completed_at,forwarded) VALUES(?,'deferred','missing_completed_at',NULL,-1)",
                   (ids[1],))
        db.execute('UPDATE queue SET forwarded=-1 WHERE id=?', (ids[1],))
        db.commit()
        before = dict(db.execute('SELECT id,payload FROM queue'))
        db.close()
        control = {'base_config': str(base_path), 'sources': [], 'known_projects': [],
                   'permitted_projects': ['one'], 'max_projects_per_tick': 0,
                   'publication': {'enabled': False}, 'new_event_epoch': {'enabled': True}}
        first = maintenance._locked_tick(control, state)
        self.assertEqual(first['status'], 'OK')
        self.assertEqual(first['unassigned_events'], 0)
        self.assertEqual(first['rejected_events'], 0)
        self.assertEqual(first['new_lane']['unassigned_events'], 0)
        self.assertEqual(first['new_lane']['deferred_reasons']['caretaker_self_activity'], 3)
        db = sqlite3.connect(state / 'intake.sqlite')
        rows = db.execute('SELECT event_id,status,reason,completed_at,forwarded '
                          'FROM new_epoch_events ORDER BY event_id').fetchall()
        self.assertEqual({row[0] for row in rows}, set(ids))
        self.assertTrue(all(row[1:3] == ('deferred', 'caretaker_self_activity') for row in rows))
        self.assertEqual(dict(db.execute('SELECT id,payload FROM queue')), before)
        self.assertFalse(maintenance._is_caretaker_self_receipt(
            {'completion_kind': 'verified_fleet_handoff',
             'native_session_id': 'logical:fleet-receipt:other',
             'cwd': str(state) + '-other'}, state))
        db.close()
        second = maintenance._locked_tick(control, state)
        db = sqlite3.connect(state / 'intake.sqlite')
        self.assertEqual(db.execute('SELECT event_id,status,reason,completed_at,forwarded '
                                    'FROM new_epoch_events ORDER BY event_id').fetchall(), rows)
        self.assertEqual(second['new_lane']['deferred_reasons']['caretaker_self_activity'], 3)
        db.close()

    def test_claude_native_stop_timestamp_only_completes_claude(self):
        root = fixture_root()
        rows = [
            {'type': 'user', 'sessionId': 'session-one', 'uuid': 'user-one',
             'timestamp': '2026-09-23T12:00:00Z',
             'message': {'role': 'user', 'content': 'Inspect synthetic fixture.'}},
            {'type': 'assistant', 'sessionId': 'session-one', 'uuid': 'assistant-one',
             'timestamp': '2026-09-23T12:01:00.123Z',
             'message': {'role': 'assistant', 'stop_reason': 'end_turn',
                         'content': [{'type': 'text', 'text': 'Fixture inspected.'}]}},
        ]
        path = root / 'claude.jsonl'
        path.write_text(''.join(json.dumps(row) + '\n' for row in rows), encoding='utf-8')
        completed = collect_sources([SourceSpec('claude', path)]).events[0]
        other = collect_sources([SourceSpec('qwen', path)]).events[0]
        self.assertEqual(completed.completed_at, '2026-09-23T12:01:00.123Z')
        self.assertEqual(completed.observed_at, completed.completed_at)
        self.assertIn(other.completed_at, (None, ''))

        rows[1].pop('timestamp')
        rows[1]['observed_at'] = '2026-09-23T12:01:00Z'
        path.write_text(''.join(json.dumps(row) + '\n' for row in rows), encoding='utf-8')
        without_native = collect_sources([SourceSpec('claude', path)]).events[0]
        self.assertIn(without_native.completed_at, (None, ''))
        rows[1]['timestamp'] = 'invalid-native-time'
        path.write_text(''.join(json.dumps(row) + '\n' for row in rows), encoding='utf-8')
        invalid_native = collect_sources([SourceSpec('claude', path)]).events[0]
        self.assertEqual(invalid_native.observed_at, '2026-09-23T12:01:00.000Z')
        self.assertIn(invalid_native.completed_at, (None, ''))
        vault = root / 'vault'
        vault.mkdir()
        db = intake_db(root / 'intake.sqlite')
        maintenance.activate_new_event_epoch(db, vault,
            dt.datetime(2026, 9, 23, 12, tzinfo=dt.timezone.utc))
        ident = queue(db, invalid_native.to_dict())
        maintenance._admit_new_epoch_events(db)
        maintenance._admit_new_epoch_events(db)
        self.assertEqual(db.execute('SELECT status,reason FROM new_epoch_events WHERE event_id=?',
                                    (ident,)).fetchone(),
                         ('deferred', 'missing_completed_at'))
        db.close()

    def test_stored_claude_recovery_is_once_and_preserves_identity_and_forwarding(self):
        root = fixture_root()
        vault = root / 'vault'
        vault.mkdir()
        db = intake_db(root / 'intake.sqlite')
        maintenance.activate_new_event_epoch(db, vault,
            dt.datetime(2026, 9, 23, 12, tzinfo=dt.timezone.utc))
        ids = {}
        for name, observed in (
                ('post', '2026-09-23T12:01:00Z'),
                ('pre', '2026-09-23T11:59:00Z'),
                ('invalid', 'not-a-timestamp')):
            value = event(name, '')
            value.update(tool='claude', event_type='completed_turn.v1',
                         completion_kind='stop_reason:end_turn', observed_at=observed,
                         excerpt=f'Original {name} excerpt')
            ident = queue(db, value)
            ids[name] = ident
            forwarded = 0 if name == 'post' else 1
            db.execute('UPDATE queue SET forwarded=? WHERE id=?', (forwarded, ident))
            db.execute('INSERT INTO new_epoch_events(event_id,status,reason,completed_at,forwarded) '
                       "VALUES(?,'deferred','missing_completed_at',NULL,?)", (ident, forwarded))
        db.commit()
        maintenance._admit_new_epoch_events(db)
        first = db.execute('SELECT event_id,status,reason,completed_at,forwarded '
                           'FROM new_epoch_events ORDER BY event_id').fetchall()
        self.assertEqual(db.execute('SELECT status FROM new_epoch_events WHERE event_id=?',
                                    (ids['post'],)).fetchone()[0], 'admitted')
        self.assertEqual(db.execute('SELECT reason FROM new_epoch_events WHERE event_id=?',
                                    (ids['pre'],)).fetchone()[0],
                         'completed_before_activation')
        self.assertEqual(db.execute('SELECT reason FROM new_epoch_events WHERE event_id=?',
                                    (ids['invalid'],)).fetchone()[0],
                         'missing_completed_at')
        for name in ('post', 'pre', 'invalid'):
            payload, forwarded = db.execute('SELECT payload,forwarded FROM queue WHERE id=?',
                                             (ids[name],)).fetchone()
            self.assertEqual(forwarded, 0 if name == 'post' else 1)
            self.assertEqual(json.loads(payload)['excerpt'], f'Original {name} excerpt')
        self.assertEqual(maintenance._new_epoch_counts(db)['pending_events'], 1)
        self.assertEqual(json.loads(db.execute('SELECT payload FROM queue WHERE id=?',
                                               (ids['post'],)).fetchone()[0])['completed_at'],
                         '2026-09-23T12:01:00Z')

        new_missing = event('new-missing-native-timestamp', '')
        new_missing.update(tool='claude', event_type='completed_turn.v1',
                           completion_kind='stop_reason:end_turn',
                           observed_at='2026-09-23T12:02:00Z')
        new_id = queue(db, new_missing)
        maintenance._admit_new_epoch_events(db)
        maintenance._admit_new_epoch_events(db)
        self.assertEqual(db.execute('SELECT event_id,status,reason,completed_at,forwarded '
                                    'FROM new_epoch_events WHERE event_id!=? ORDER BY event_id',
                                    (new_id,)).fetchall(), first)
        self.assertEqual(db.execute('SELECT status,reason FROM new_epoch_events WHERE event_id=?',
                                    (new_id,)).fetchone(),
                         ('deferred', 'missing_completed_at'))
        db.close()

    def test_codex_mid_turn_requires_same_linked_user_and_later_terminal(self):
        root = fixture_root()
        vault = root / 'vault'
        vault.mkdir()
        state = root / 'state'
        state.mkdir()
        db = intake_db(state / 'intake.sqlite')
        maintenance.activate_new_event_epoch(db, vault,
            dt.datetime(2026, 9, 23, 12, tzinfo=dt.timezone.utc))
        mid = event('item-one', None)
        mid.update(event_type='completed_turn.v1', completion_kind='event:item.completed',
                   linked_user_cursor='user-one', observed_at='2026-09-23T12:01:00Z')
        mid_id = queue(db, mid)
        maintenance._admit_new_epoch_events(db)
        self.assertEqual(db.execute('SELECT reason FROM new_epoch_events WHERE event_id=?',
                                    (mid_id,)).fetchone()[0], 'missing_completed_at')
        terminal = event('turn-one', '2026-09-23T12:02:00Z')
        terminal.update(event_type='completed_turn.v1', completion_kind='event:task.complete',
                        linked_user_cursor='user-one', observed_at='2026-09-23T12:02:00Z')
        queue(db, terminal)
        maintenance._admit_new_epoch_events(db)
        self.assertEqual(db.execute('SELECT reason FROM new_epoch_events WHERE event_id=?',
                                    (mid_id,)).fetchone()[0],
                         'superseded_by_turn_completion')
        maintenance._admit_new_epoch_events(db)
        db.close()

        base_path = root / 'base.json'
        base_path.write_text(json.dumps({'hub': {'vault': str(vault)}, 'office': {},
                                         'allowlist': {}, 'limits': {}}), encoding='utf-8')
        control = {'base_config': str(base_path), 'sources': [], 'known_projects': ['one'],
                   'max_projects_per_tick': 0, 'publication': {'enabled': False},
                   'new_event_epoch': {'enabled': True}}
        report = maintenance._locked_tick(control, state)
        self.assertEqual(report['new_lane']['status'], 'DISABLED')

        db = sqlite3.connect(state / 'intake.sqlite')
        unrelated = mid | {'cursor': 'item-two', 'linked_user_cursor': 'user-two'}
        unrelated_id = queue(db, unrelated)
        maintenance._admit_new_epoch_events(db)
        self.assertEqual(db.execute('SELECT reason FROM new_epoch_events WHERE event_id=?',
                                    (unrelated_id,)).fetchone()[0],
                         'missing_completed_at')
        db.close()

    def test_accounted_new_cycle_does_not_switch_to_pending_legacy_cycle(self):
        db = memory_db()
        legacy = maybe_start_cycle(db, _summary('prop-legacy'))
        fresh = maybe_start_cycle(db, _summary('prop-new'), lane='new')
        self.assertNotEqual(legacy['id'], fresh['id'])
        with patch('maintenance._audit_summary', return_value=_summary('prop-new')), \
                patch('caretaker.audit.accounted_proposal_ids', return_value={'prop-new'}), \
                patch('maintenance._invoke_curate') as invoke:
            result = maintenance.publication_tick('config', _cfg(), _control(), db,
                                                  lane='new')
        invoke.assert_not_called()
        self.assertEqual(result['status'], 'NO_PENDING')
        self.assertIsNone(active_cycle(db, 'new'))
        self.assertEqual(active_cycle(db)['id'], legacy['id'])
        db.close()

    def test_actual_pipeline_receipt_admits_only_its_new_source(self):
        root = fixture_root()
        vault = root / 'vault'
        vault.mkdir()
        db = intake_db(root / 'intake.sqlite')
        activated = dt.datetime(2026, 9, 23, 12, tzinfo=dt.timezone.utc)
        maintenance.activate_new_event_epoch(db, vault, activated)
        item = NormalizedEvent(
            tool='codex', native_session_id='session-one', cursor='new',
            observed_at='2026-09-23T12:01:00Z', completed_at='2026-09-23T12:01:00Z',
            completion_kind='terminal', title='Synthetic completed turn',
            excerpt='A synthetic project fact with no customer data.',
            source_ref={'path_or_store': 'synthetic', 'table_or_stream': 'test'},
            linked_user_cursor='request-one', project='one')
        queue(db, item.to_dict())
        maintenance._admit_new_epoch_events(db)
        project_root = root / 'new-event-epoch' / 'projects' / 'one'
        project_root.mkdir(parents=True)
        source = project_root / 'codex.jsonl'
        source.write_text(json.dumps(item.to_dict()) + '\n', encoding='utf-8')
        raw = json.loads((Path(__file__).resolve().parents[1] / 'config.example.json').read_text(encoding='utf-8'))
        raw.update(project_slug='one', state_dir=str(project_root / 'state'),
                   inputs=[{'path': str(source), 'kind': 'normalized_events', 'tool': 'codex'}])
        raw['allowlist']['tools'] = ['codex']
        raw['hub']['vault'] = str(vault)
        raw['hub']['context_receipt'] = str(project_root / 'context.json')
        config_path = project_root / 'config.json'
        config_path.write_text(json.dumps(raw), encoding='utf-8')
        config = load_config(str(config_path))

        class ReceiptGateway(FakeKnowledgeGateway):
            def record_handoff(self, **kwargs):
                ident = Path(kwargs['path']).stem
                source_id = 'src-' + ident
                proposal(vault, ident, source_id, author='openclaw-caretaker')
                return RecordHandoffResult(
                    status='recorded', receipt_id=ident, receipt_status='COMPLETE',
                    source_id=source_id, proposal_id=ident,
                    event_ids=('synthetic-gateway-event',), checkpoint_status='COMPLETE')

        answer = json.dumps({'decision': 'new', 'synthesis': 'Synthetic fact.',
                             'source_refs': ['codex:session-one:new'], 'rationale': 'Source cited.'})
        primary = FakeTransport(name='primary', responses=[Envelope(
            ok=True, status='ok', final=answer, provider='vllm', model='Qwen3.8-27B')],
            expected_identity=('vllm', 'Qwen3.8-27B'))
        deps = PipelineDeps(primary=primary, fallback=None, gateway=ReceiptGateway(),
                            office=_office_connector(), worktree_root='')
        store = Store(str(project_root / 'state' / 'caretaker.sqlite'))
        try:
            result = run_pipeline(config, deps, store=store, dry_run=False)
        finally:
            store.close()
        self.assertEqual(result.proposals_new, 1)
        self.assertFalse(result.incidents)
        created = list((vault / 'proposals' / 'openclaw-caretaker').glob('*.md'))
        self.assertEqual(len(created), 1)
        ident = created[0].stem
        candidate = {'proposal_id': ident,
                     'path': created[0].relative_to(vault).as_posix(),
                     'project': 'one', 'source_ids': ['src-' + ident]}
        self.assertTrue(maintenance._new_epoch_proposal_filter(db, root)(candidate))
        self.assertFalse(maintenance._new_epoch_proposal_filter(db, root)(
            candidate | {'source_ids': ['src-unrelated']}))
        db.close()

    def test_new_lane_respects_global_publication_pause(self):
        root = fixture_root()
        vault = root / 'vault'
        vault.mkdir()
        db = intake_db(root / 'intake.sqlite')
        maintenance.activate_new_event_epoch(db, vault,
            dt.datetime(2026, 9, 23, 12, tzinfo=dt.timezone.utc))
        base = {'hub': {'vault': str(vault)}, 'office': {}, 'allowlist': {}, 'limits': {}}
        control = {'publication': {'enabled': False}}
        with patch('maintenance._invoke_curate') as curator:
            result = maintenance.publication_drain(control, base, db, root, lane='new')
        self.assertEqual(result['status'], 'DISABLED')
        curator.assert_not_called()
        db.close()

    def test_unreconciled_legacy_publication_blocks_new_lane(self):
        root = fixture_root()
        vault = root / 'vault'
        vault.mkdir()
        db = intake_db(root / 'intake.sqlite')
        maintenance.activate_new_event_epoch(db, vault,
            dt.datetime(2026, 9, 23, 12, tzinfo=dt.timezone.utc))
        db.execute('CREATE TABLE publications(batch TEXT PRIMARY KEY,status TEXT,failures INTEGER,result TEXT)')
        db.execute('INSERT INTO publications VALUES(?,?,?,?)',
                   ('legacy-batch', 'RUNNING', 0, json.dumps({'cycle_id': 'audit-legacy'})))
        db.commit()
        base = {'hub': {'vault': str(vault)}, 'office': {}, 'allowlist': {}, 'limits': {}}
        with patch('maintenance._invoke_curate') as curator:
            result = maintenance.publication_drain({'publication': {'enabled': True}}, base, db, root, lane='new')
        curator.assert_not_called()
        self.assertEqual(result['reason'], 'legacy_publication_unreconciled')
        self.assertEqual(db.execute('SELECT status FROM publications WHERE batch=?',
                                    ('legacy-batch',)).fetchone()[0], 'RUNNING')
        db.close()

    def test_legacy_tick_does_not_create_epoch(self):
        root = fixture_root()
        state = root / 'state'
        state.mkdir()
        base_path = root / 'base.json'
        base_path.write_text(json.dumps({'hub': {'vault': str(root / 'vault')},
                                         'office': {}, 'allowlist': {}, 'limits': {}}), encoding='utf-8')
        control = {'base_config': str(base_path), 'sources': [], 'known_projects': [],
                   'publication': {'enabled': False}}
        report = maintenance._locked_tick(control, state)
        self.assertEqual(report['new_lane']['status'], 'DISABLED')
        db = sqlite3.connect(state / 'intake.sqlite')
        self.assertFalse(maintenance._new_epoch_table_exists(db))
        db.close()

    def test_activation_baseline_timestamp_defer_restart_and_duplicate(self):
        root = fixture_root()
        vault = root / 'vault'
        proposal(vault, 'prop-old', 'src-old')
        db_path = root / 'intake.sqlite'
        db = intake_db(db_path)
        baseline = queue(db, event('baseline', '2026-09-23T13:00:00Z'))
        activated = dt.datetime(2026, 9, 23, 12, tzinfo=dt.timezone.utc)
        self.assertEqual(maintenance.activate_new_event_epoch(db, vault, activated), activated.isoformat())
        self.assertEqual(db.execute('SELECT event_id FROM new_epoch_baseline_events').fetchone()[0], baseline)
        self.assertEqual(db.execute('SELECT proposal_id FROM new_epoch_baseline_proposals').fetchone()[0], 'prop-old')

        ids = {name: queue(db, event(name, stamp)) for name, stamp in (
            ('old', '2026-09-23T11:59:59Z'), ('new', '2026-09-23T14:00:00+02:00'),
            ('missing', None), ('invalid', 'end_turn'))}
        old_without_completion = event('old-without-completion', None)
        old_without_completion['observed_at'] = '2026-09-23T11:30:00Z'
        ids['old_without_completion'] = queue(db, old_without_completion)
        document = event('historical-document-discovered-later', '2026-09-23T14:00:00Z')
        document.update(tool='files', event_type='owner_document.v1',
                        completion_kind='owner_document')
        ids['document'] = queue(db, document)
        user_message = event('user-message', None)
        user_message.update(event_type='user_message.v1',
                            completion_kind='user_statement',
                            observed_at='2026-09-23T12:01:00Z')
        ids['user_message'] = queue(db, user_message)
        report = {'new_events': 0, 'source_issues': []}
        maintenance._enqueue_source_events(db, [event('new', '2026-09-23T14:00:00+02:00')],
                                           report, 'synthetic', {'known_projects': ['one']})
        self.assertEqual(report['new_events'], 0)
        maintenance._admit_new_epoch_events(db)
        rows = dict(db.execute('SELECT event_id,status FROM new_epoch_events'))
        self.assertEqual(rows[ids['new']], 'admitted')
        self.assertTrue(all(rows[ids[name]] == 'deferred' for name in
                            ('old', 'missing', 'invalid', 'document', 'user_message',
                             'old_without_completion')))
        self.assertEqual(maintenance._new_epoch_counts(db)['deferred_reasons'],
                         {'completed_before_activation': 1, 'missing_completed_at': 1,
                          'invalid_completed_at': 1, 'not_completed_turn': 2,
                          'observed_before_activation_without_completion': 1})
        db.execute("UPDATE new_epoch_events SET reason='missing_completed_at' WHERE event_id=?",
                   (ids['user_message'],))
        db.commit()
        maintenance._admit_new_epoch_events(db)
        self.assertEqual(db.execute('SELECT reason FROM new_epoch_events WHERE event_id=?',
                                    (ids['user_message'],)).fetchone()[0],
                         'not_completed_turn')
        db.execute("UPDATE new_epoch_events SET reason='missing_completed_at' WHERE event_id=?",
                   (ids['old_without_completion'],))
        db.commit()
        maintenance._admit_new_epoch_events(db)
        self.assertEqual(db.execute('SELECT reason FROM new_epoch_events WHERE event_id=?',
                                    (ids['old_without_completion'],)).fetchone()[0],
                         'observed_before_activation_without_completion')
        db.close()

        db = sqlite3.connect(db_path)
        self.assertEqual(maintenance.activate_new_event_epoch(
            db, vault, activated + dt.timedelta(days=1)), activated.isoformat())
        maintenance._admit_new_epoch_events(db)
        self.assertEqual(maintenance._new_epoch_counts(db)['admitted_events'], 1)
        self.assertEqual(maintenance._new_epoch_counts(db)['baseline_events'], 1)
        self.assertEqual(db.execute('SELECT forwarded FROM queue WHERE id=?', (baseline,)).fetchone()[0], 0)
        db.close()

    def test_new_proposal_requires_admitted_receipt_and_ignores_old_active_cycle(self):
        root = fixture_root()
        vault = root / 'vault'
        proposal(vault, 'prop-old', 'src-old')
        db = intake_db(root / 'intake.sqlite')
        queue(db, event('old-queued', '2026-09-23T11:00:00Z'))
        policy = policy_from({'min_ready_groups': 1, 'max_batch': 8})
        now = dt.datetime(2026, 9, 23, 12, tzinfo=dt.timezone.utc)
        legacy = maybe_start_cycle(db, summarize(vault, policy, now=now.timestamp()), now_iso=now.isoformat())
        self.assertEqual(legacy['members'][0]['proposal_id'], 'prop-old')
        maintenance.activate_new_event_epoch(db, vault, now)
        queue(db, event('new', now.isoformat()))
        maintenance._admit_new_epoch_events(db)
        new_path = proposal(vault, 'prop-new', 'src-new', author='openclaw-caretaker')
        candidate = {'proposal_id': 'prop-new', 'path': new_path.relative_to(vault).as_posix(),
                     'project': 'one', 'source_ids': ['src-new']}
        self.assertFalse(maintenance._new_epoch_proposal_filter(db, root)(candidate))

        store_path = root / 'new-event-epoch' / 'projects' / 'one' / 'state' / 'caretaker.sqlite'
        store = Store(store_path)
        store.outbox_put(dedup_id='handoff-new', payload_hash='hash-new', status='recorded',
                         recorded_at=now.isoformat(), request={
                             'kind': 'handoff', 'project_slug': 'one',
                             'source_refs': ['codex:session-one:new'],
                             'receipt': {'receipt_status': 'COMPLETE',
                                         'proposal_id': 'prop-new', 'source_id': 'src-new'}})
        store.close()
        self.assertTrue(maintenance._new_epoch_proposal_filter(db, root)(candidate))
        self.assertFalse(maintenance._new_epoch_proposal_filter(db, root)(
            candidate | {'source_ids': ['src-new', 'src-old']}))
        base = {'hub': {'vault': str(vault)}, 'office': {}, 'allowlist': {}, 'limits': {}}
        control = {'publication': {'enabled': True, 'min_ready_groups': 1, 'max_batch': 8}}
        with patch('maintenance._invoke_curate', return_value={'status': 'PUBLISHED'}) as invoke:
            first = maintenance.publication_drain(control, base, db, root, now=now, lane='new')
            second = maintenance.publication_drain(control, base, db, root,
                                                   now=now + dt.timedelta(minutes=1), lane='new')
        self.assertEqual(first['batches'][0]['status'], 'PUBLISHED')
        self.assertEqual(invoke.call_count, 1)
        self.assertEqual(invoke.call_args.args[1], [candidate['path']])
        self.assertEqual(second['model_calls'], 0)
        self.assertEqual(active_cycle(db)['id'], legacy['id'])
        self.assertEqual(active_cycle(db)['members'][0]['status'], 'pending')
        self.assertTrue(new_path.is_file())
        held = maintenance._held_legacy_publication(base, control, db, root, now)
        self.assertEqual(held['status'], 'DEGRADED')
        self.assertEqual(held['active_cycle_id'], legacy['id'])
        db.close()

    def test_tick_routes_only_post_activation_event_to_isolated_pipeline(self):
        root = fixture_root()
        state = root / 'state'
        state.mkdir()
        vault = root / 'vault'
        proposal(vault, 'prop-old', 'src-old')
        db = intake_db(state / 'intake.sqlite')
        old_id = queue(db, event('old-queued', '2026-09-23T11:00:00Z'))
        db.close()
        base_path = root / 'base.json'
        base_path.write_text(json.dumps({'hub': {'vault': str(vault)}, 'office': {},
                                         'allowlist': {}, 'limits': {}}), encoding='utf-8')
        control = {'base_config': str(base_path), 'sources': [], 'known_projects': ['one'],
                   'max_projects_per_tick': 1, 'publication': {'enabled': False},
                   'new_event_epoch': {'enabled': True, 'activate': True}, 'python': 'python'}
        with patch('maintenance.subprocess.run') as pipeline, patch('maintenance._invoke_curate') as curator:
            first = maintenance._locked_tick(control, state)
        pipeline.assert_not_called()
        curator.assert_not_called()
        self.assertEqual(first['new_lane']['baseline_events'], 1)

        db = sqlite3.connect(state / 'intake.sqlite')
        new_id = queue(db, event('new', '2099-01-01T00:00:00Z'))
        old_discovered_later = queue(db, event('old-discovered-later', '2026-09-23T11:00:00Z'))
        db.close()
        control['new_event_epoch'] = {'enabled': True}
        with (patch('maintenance.subprocess.run', return_value=SimpleNamespace(
                stdout='{"status":"OK","incidents":[]}', returncode=0)) as pipeline,
              patch('maintenance._invoke_curate') as curator):
            second = maintenance._locked_tick(control, state)
        self.assertEqual(pipeline.call_count, 1)
        curator.assert_not_called()
        submitted = state / 'new-event-epoch' / 'projects' / 'one' / 'codex.jsonl'
        self.assertEqual([json.loads(line)['cursor'] for line in submitted.read_text().splitlines()], ['new'])
        self.assertEqual(second['new_lane']['admitted_events'], 1)
        self.assertEqual(second['new_lane']['deferred_reasons']['completed_before_activation'], 1)
        self.assertEqual(second['new_lane']['status'], 'DISABLED')
        db = sqlite3.connect(state / 'intake.sqlite')
        self.assertEqual(db.execute('SELECT forwarded FROM queue WHERE id=?', (old_id,)).fetchone()[0], 0)
        self.assertEqual(db.execute('SELECT forwarded FROM queue WHERE id=?', (old_discovered_later,)).fetchone()[0], 0)
        self.assertEqual(db.execute('SELECT forwarded FROM queue WHERE id=?', (new_id,)).fetchone()[0], 0)
        self.assertIsNone(active_cycle(db))
        db.close()
        control['new_event_epoch'] = {'enabled': False}
        with patch('maintenance.subprocess.run') as pipeline, patch('maintenance._invoke_curate') as curator:
            paused = maintenance._locked_tick(control, state)
        pipeline.assert_not_called()
        curator.assert_not_called()
        self.assertEqual(paused['new_lane']['status'], 'PAUSED')
        self.assertEqual(paused['publication']['status'], 'DEGRADED')

    def test_enabled_without_explicit_activation_holds_legacy_work(self):
        root = fixture_root()
        state = root / 'state'
        state.mkdir()
        vault = root / 'vault'
        proposal(vault, 'prop-old', 'src-old')
        db = intake_db(state / 'intake.sqlite')
        old_id = queue(db, event('old-queued', '2026-09-23T11:00:00Z'))
        db.close()
        base_path = root / 'base.json'
        base_path.write_text(json.dumps({'hub': {'vault': str(vault)}, 'office': {},
                                         'allowlist': {}, 'limits': {}}), encoding='utf-8')
        control = {'base_config': str(base_path), 'sources': [], 'known_projects': ['one'],
                   'max_projects_per_tick': 1, 'publication': {'enabled': True},
                   'new_event_epoch': {'enabled': True}}
        with patch('maintenance.subprocess.run') as pipeline, patch('maintenance._invoke_curate') as curator:
            report = maintenance._locked_tick(control, state)
        pipeline.assert_not_called()
        curator.assert_not_called()
        self.assertEqual(report['new_lane']['status'], 'NOT_ACTIVATED')
        self.assertEqual(report['publication']['status'], 'DEGRADED')
        self.assertEqual(report['status'], 'DEGRADED')
        db = sqlite3.connect(state / 'intake.sqlite')
        self.assertEqual(db.execute('SELECT count(*) FROM new_event_epoch').fetchone()[0], 0)
        self.assertEqual(db.execute('SELECT forwarded FROM queue WHERE id=?', (old_id,)).fetchone()[0], 0)
        db.close()


if __name__ == '__main__':
    unittest.main()
