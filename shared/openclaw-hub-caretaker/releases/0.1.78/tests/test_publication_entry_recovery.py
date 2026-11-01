"""Normal drain admission must use the same recovery proofs as project ticks."""
import contextlib
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import maintenance
from caretaker.audit import ensure_tables, policy_from
from caretaker.pubstate import code_version, ensure_pub_tables, fingerprint
from tests import test_repaired_batch_retry as retry_fixture


TEMP = Path(os.environ.get("TEAM_TEST_ROOT") or tempfile.gettempdir())


class AdmissionCaptured(Exception):
    pass


class PublicationEntryRecoveryTests(unittest.TestCase):
    def setUp(self):
        TEMP.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(prefix='entry71-', dir=TEMP)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.vault = self.root / 'vault'
        self.state = self.root / 'state'
        self.state.mkdir()
        self.vault.mkdir()
        self.db = sqlite3.connect(':memory:')
        self.addCleanup(self.db.close)
        ensure_tables(self.db)
        ensure_pub_tables(self.db)
        self.projects = ['summary-fixture', 'inventory-fixture', 'terminal-fixture']
        self.policy = dict(enabled=True, include_existing_hub_backlog=True,
                           min_ready_groups=1, max_wait_hours=1, max_batch=8,
                           projects=self.projects)
        self.control = {'publication': self.policy}
        self.base = {'hub': {'vault': str(self.vault)}}
        self.identities = {}
        reasons = ['independent_review_not_accepted', 'citation_outside_publication',
                   'phase_resume_terminal_no_replay']
        for number, (project, reason) in enumerate(zip(self.projects, reasons), 1):
            name = 'proposals/fixture/' + project + '.md'
            path = self.vault / name
            path.parent.mkdir(parents=True, exist_ok=True)
            body = ('---\nid: prop-' + project + '\nclassification: internal\nproject_slug: '
                    + project + '\ntarget_pages: ["wiki/projects/' + project + '.md"]'
                    + '\nsource_ids: ["src-fixture-' + project + '"]\n---\nSynthetic.\n').encode()
            path.write_bytes(body)
            digest = hashlib.sha256(body).hexdigest()
            pid = 'prop-' + project
            batch = str(number) * 64
            cycle = 'audit-new-' + project
            value = dict(status='QUARANTINED', reason=reason, original_reason=reason,
                         project=project, paths=[name], proposal_ids=[pid], hashes=[digest],
                         batch=batch, cycle_id=cycle)
            fp = fingerprint(project, [name], [digest, 'missing_target'], policy_from(self.policy), code_version())
            self.db.execute('INSERT INTO publications(batch,status,failures,result,fingerprint) VALUES(?,?,?,?,?)',
                            (batch, 'QUARANTINED', 0, json.dumps(value), fp))
            self.db.execute('INSERT INTO audit_cycles(id,lane,trigger,created_at,status,counts) VALUES(?,?,?,?,?,?)',
                            (cycle, 'new', 'threshold', '2026-10-24T00:00:00Z', 'DRAINED', '{}'))
            self.db.execute('INSERT INTO audit_members(cycle_id,proposal_id,path,sha256,project,group_key,status) VALUES(?,?,?,?,?,?,?)',
                            (cycle, pid, name, digest, project, project, 'quarantined'))
            self.identities[project] = dict(id=pid, path=name, sha256=digest)
            for owner in (self.state, self.state / 'new-event-epoch'):
                config = owner / 'projects' / project / 'config.json'
                config.parent.mkdir(parents=True, exist_ok=True)
                config.write_text('{}', encoding='utf-8')
        self.db.commit()

    def snapshot(self):
        return {path.relative_to(self.root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
                for path in self.root.rglob('*') if path.is_file()}

    @contextlib.contextmanager
    def run_drain(self, *, lane='legacy', reject_inventory=False, reject_terminal=False,
                  bad_state=False):
        captured = {}
        loaded = []
        proof_calls = []
        def load(path):
            path = Path(path)
            project = path.parent.name
            loaded.append(path)
            return SimpleNamespace(project_slug=project,
                state_dir=path.parent / ('wrong-state' if bad_state else 'state'),
                hub=SimpleNamespace(vault=self.vault))
        def freeze(_vault, paths, target, *, attestation_state_dir):
            project = next(p for p, item in self.identities.items() if item['path'] == paths[0])
            expected = maintenance._canonical_project_runtime_state(self.state, project, lane, self.db)
            self.assertEqual(Path(attestation_state_dir), expected / 'projects' / project / 'state')
            return {'proposals': [self.identities[project]], 'vault_path': str(self.vault)}
        def summary_plan(_vault, state, frozen, value, *, project, paths):
            proof_calls.append(('summary', project))
            self.assertEqual(project, self.projects[0])
            return {}
        def inventory_plan(cfg, paths, frozen, value):
            proof_calls.append(('inventory', cfg.project_slug))
            self.assertEqual(cfg.project_slug, self.projects[1])
            return None if reject_inventory else {}
        def terminal_plan(cfg, paths, frozen, value):
            proof_calls.append(('terminal', cfg.project_slug))
            self.assertEqual(cfg.project_slug, self.projects[2])
            return None if reject_terminal else {}
        def capture(_db, summary, **kwargs):
            captured.update(summary)
            raise AdmissionCaptured
        before = self.snapshot()
        with contextlib.ExitStack() as stack:
            for target, kwargs in (
                ('caretaker.registry.read_registry', {'return_value': ({}, None)}),
                ('caretaker.registry.main_page', {'side_effect': lambda project, registry: 'wiki/projects/' + project + '.md'}),
                ('caretaker.config.load_config', {'side_effect': load}),
                ('caretaker.publication.freeze', {'side_effect': freeze}),
                ('caretaker.draft_summary.terminal_summary_retry_plan', {'side_effect': summary_plan}),
                ('caretaker.draft_summary.inventory_history_repair_plan', {'side_effect': inventory_plan}),
                ('caretaker.terminal_generation.terminal_generation_plan', {'side_effect': terminal_plan}),
                ('caretaker.audit.maybe_start_cycle', {'side_effect': capture}),
                ('maintenance._new_epoch_proposal_filter', {'return_value': None}),
                ('maintenance._prepared_priority_candidate', {'return_value': None}),
                ('maintenance._project_runtime_cfg', {'side_effect': AssertionError('audit must not create runtime/context')}),
                ('maintenance._invoke_curate', {'side_effect': AssertionError('no model work in admission fixture')}),
            ):
                stack.enter_context(patch(target, **kwargs))
            with self.assertRaises(AdmissionCaptured):
                maintenance.publication_drain(self.control, self.base, self.db, self.state,
                    lane=lane, recovery={'status': 'NO_LOCK'},
                    _hub_preflight_state={'result': {'status': 'PASS', 'health_status': 'pass', 'lint_status': 'pass'}})
            self.assertEqual(self.snapshot(), before)
            self.assertEqual(self.db.execute('SELECT count(*) FROM publications WHERE status="QUARANTINED"').fetchone()[0], 3)
            yield captured, loaded, proof_calls

    def test_normal_drain_recovers_all_three_proof_types(self):
        with self.run_drain() as (summary, loaded, calls):
            self.assertEqual(summary['trigger'], 'threshold')
            self.assertEqual(summary['quarantined_proposals'], 0)
            self.assertEqual(summary['ready_group_count'], 3)
            self.assertEqual({member['project'] for member in summary['eligible']}, set(self.projects))
            self.assertEqual(set(calls), {('summary', self.projects[0]), ('inventory', self.projects[1]),
                                          ('terminal', self.projects[2])})
            self.assertTrue(all(path.is_relative_to(self.state / 'projects') for path in loaded))

    def test_failed_inventory_proof_keeps_only_that_identity_quarantined(self):
        with self.run_drain(reject_inventory=True) as (summary, loaded, calls):
            self.assertEqual(summary['quarantined_proposals'], 1)
            self.assertNotIn(self.projects[1], {member['project'] for member in summary['eligible']})

    def test_failed_terminal_proof_keeps_only_that_identity_quarantined(self):
        with self.run_drain(reject_terminal=True) as (summary, loaded, calls):
            self.assertEqual(summary['quarantined_proposals'], 1)
            self.assertNotIn(self.projects[2], {member['project'] for member in summary['eligible']})

    def test_new_lane_uses_new_canonical_owner_and_retains_terminal_legacy_guard(self):
        with self.run_drain(lane='new') as (summary, loaded, calls):
            self.assertEqual(summary['quarantined_proposals'], 1)
            self.assertEqual({member['project'] for member in summary['eligible']}, set(self.projects[:2]))
            self.assertTrue(all(path.is_relative_to(self.state / 'new-event-epoch' / 'projects') for path in loaded))
            self.assertNotIn(('terminal', self.projects[2]), calls)

    def test_canonical_config_owner_mismatch_fails_closed(self):
        with self.run_drain(bad_state=True) as (summary, loaded, calls):
            self.assertEqual(summary['quarantined_proposals'], 2)
            self.assertEqual({member['project'] for member in summary['eligible']}, {self.projects[0]})
            self.assertNotIn(('inventory', self.projects[1]), calls)
            self.assertNotIn(('terminal', self.projects[2]), calls)


class PublicationEntryRoutingTests(unittest.TestCase):
    def setUp(self):
        TEMP.mkdir(parents=True, exist_ok=True)
        with patch.object(retry_fixture, 'TASK_TMP', TEMP):
            self.root, self.vault, self.state = retry_fixture._workspace(self)
        self.db = retry_fixture._db()
        self.addCleanup(self.db.close)
        self.summary = retry_fixture._summary(self.vault)
        self.old_key, _ = retry_fixture._seed_failure(self.db, self.summary, self.state)
        # Both terminal member states must retain their spent identity.
        self.db.execute("UPDATE audit_members SET status='failed'")
        self.db.commit()
        self.base = json.loads((retry_fixture.ROOT / 'config.example.json').read_text(encoding='utf-8'))
        self.base.pop('project_slug', None)
        self.base.update(state_dir=str(self.state))
        self.base['hub'].update(vault=str(self.vault), script=str(self.root / 'synthetic-knowledge.py'))
        self.control = {'max_projects_per_tick': 2, 'known_projects': [], 'publication': {
            'enabled': True, 'include_existing_hub_backlog': True,
            'projects': ['test'], 'held_projects': [], 'max_batch': 8,
            'max_batches_per_tick': 2, 'min_ready_groups': 1, 'max_wait_hours': 24}}
        self.preflight = {'result': {'status': 'PASS', 'health_status': 'pass', 'lint_status': 'pass'}}

    def drain(self):
        return maintenance.publication_drain(self.control, self.base, self.db, self.state,
            now=retry_fixture.NOW, recovery={'status': 'NO_LOCK'},
            _hub_preflight_state=self.preflight)

    def test_spent_identity_reaches_real_project_planner_and_model_boundary(self):
        from caretaker.audit import active_cycle, maybe_start_cycle
        # The normal global fresh-cycle gate really refuses this identity.
        self.assertIsNone(maybe_start_cycle(self.db, self.summary,
            now_iso=retry_fixture.NOW.isoformat(), lane='legacy',
            project_allowed=maintenance._publication_scope_predicate(self.control['publication'])))
        self.assertIsNone(active_cycle(self.db, 'legacy'))
        old_row = self.db.execute('SELECT * FROM publications WHERE batch=?', (self.old_key,)).fetchone()
        old_members = self.db.execute("SELECT * FROM audit_members WHERE cycle_id='cycle-old'").fetchall()
        old_cycles = self.db.execute("SELECT * FROM audit_cycles WHERE id='cycle-old'").fetchall()
        called = []
        def invoke(config, paths):
            cycle = active_cycle(self.db, 'legacy')
            self.assertIsNotNone(cycle)
            counts = json.loads(self.db.execute('SELECT counts FROM audit_cycles WHERE id=?',
                                                (cycle['id'],)).fetchone()[0])
            retries = counts['repaired_retry']
            self.assertEqual(len(retries), 1)
            self.assertEqual(retries[0]['previous_batch'], self.old_key)
            self.assertEqual(self.db.execute('SELECT count(*) FROM repaired_retry_tokens').fetchone()[0], 1)
            called.append(list(paths))
            return {'status': 'PUBLISHED', 'model_calls': 0}
        # No mock summary, project tick, retry planner, source freeze, or token code.
        with patch.object(maintenance, '_invoke_curate', side_effect=invoke):
            result = self.drain()
        self.assertEqual(called, [[self.summary['eligible'][0]['path']]])
        self.assertEqual(result['batches'][0]['status'], 'PUBLISHED')
        self.assertTrue(result['batches'][0]['batch'].startswith('repaired-'))
        self.assertEqual(self.db.execute('SELECT * FROM publications WHERE batch=?', (self.old_key,)).fetchone(), old_row)
        self.assertEqual(self.db.execute("SELECT * FROM audit_members WHERE cycle_id='cycle-old'").fetchall(), old_members)
        self.assertEqual(self.db.execute("SELECT * FROM audit_cycles WHERE id='cycle-old'").fetchall(), old_cycles)

    def test_eligible_routing_rotates_with_limits_and_does_not_stop_at_idle_seed(self):
        import copy
        summary = copy.deepcopy(self.summary)
        original = summary['eligible'][0]
        summary['eligible'] = [dict(original, project=project, proposal_id='prop-' + project,
                                    path='proposals/fixture/' + project + '.md')
                               for project in ('alpha', 'beta', 'gamma', 'held')]
        self.control['publication'].update(projects=['alpha', 'beta', 'gamma', 'held'], held_projects=['held'])
        for member in summary['eligible']:
            self.db.execute('INSERT INTO audit_members(cycle_id,proposal_id,path,sha256,project,group_key,status) VALUES(?,?,?,?,?,?,?)',
                ('cycle-old', member['proposal_id'], member['path'], member['sha256'], member['project'], member['project'], 'failed'))
        self.db.commit()
        visited = []
        def tick(config, cfg, *args, **kwargs):
            visited.append(cfg['project_slug'])
            return {'status': 'IDLE', 'model_calls': 0}
        with patch.object(maintenance, '_audit_summary', return_value=summary), \
                patch.object(maintenance, '_project_runtime_cfg', side_effect=lambda base, project, state, **kw: ('fixture', {'project_slug': project})), \
                patch.object(maintenance, '_prepared_priority_candidate', return_value=None), \
                patch.object(maintenance, 'publication_tick', side_effect=tick):
            first = self.drain()
            second = self.drain()
        self.assertEqual(visited, ['alpha', 'beta', 'beta', 'gamma'])
        self.assertEqual(len(first['batches']), 2)
        self.assertEqual(len(second['batches']), 2)
        self.assertEqual(first['model_calls'] + second['model_calls'], 0)
        self.assertEqual(self.db.execute('SELECT value FROM publication_rotation WHERE id=1').fetchone()[0], 2)

    def test_spent_identity_with_changed_source_never_reaches_model_or_token(self):
        original = self.vault / 'raw/sources/2026/09/src-one/original.md'
        original.write_bytes(b'tampered synthetic source')
        with patch.object(maintenance, '_invoke_curate') as invoke:
            result = self.drain()
        self.assertEqual(result['model_calls'], 0)
        invoke.assert_not_called()
        token_table = self.db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='repaired_retry_tokens'").fetchone()
        if token_table:
            self.assertEqual(self.db.execute('SELECT count(*) FROM repaired_retry_tokens').fetchone()[0], 0)
        self.assertEqual(self.db.execute('SELECT status FROM publications WHERE batch=?', (self.old_key,)).fetchone()[0], 'QUARANTINED')

    def test_empty_eligible_known_fallback_performs_no_model_work(self):
        summary = dict(self.summary, eligible=[], groups=[], raw_eligible=0,
                       group_count=0, ready_group_count=0, trigger=None)
        self.control['known_projects'] = ['test']
        with patch.object(maintenance, '_audit_summary', return_value=summary), \
                patch.object(maintenance, '_invoke_curate') as invoke:
            result = self.drain()
        self.assertEqual(result['model_calls'], 0)
        invoke.assert_not_called()

    def test_active_alpha_does_not_hide_qualified_inventory_beta(self):
        from caretaker.pubstate import batch_key
        from caretaker.audit import group_key, summarize
        from caretaker.publication import sha
        old = json.loads(self.db.execute('SELECT result FROM publications WHERE batch=?', (self.old_key,)).fetchone()[0])
        old.update(reason='citation_outside_publication', original_reason='citation_outside_publication',
                   error_code='citation_outside_publication', review_repair_exhausted=True,
                   format_repair_exhausted=False, candidate_path=None, maker_calls=2, review_calls=1)
        self.db.execute('UPDATE publications SET result=? WHERE batch=?', (json.dumps(old), self.old_key))
        alpha = self.vault / 'proposals/agent/alpha.md'
        alpha.write_text('---\nid: prop-alpha\nclassification: internal\nproject_slug: alpha\n'
                         'source_ids: ["src-one"]\ntarget_pages: ["wiki/projects/alpha.md"]\n---\nSynthetic alpha.\n', encoding='utf-8')
        page = self.vault / 'wiki/projects/alpha.md'
        page.write_text('---\nstatus: reviewed\nproject_slug: alpha\n---\n# Alpha\n', encoding='utf-8')
        self.control['publication']['projects'] = ['alpha', 'test']
        raw_summary = summarize(self.vault, policy_from(self.control['publication']))
        meta = next(item for item in raw_summary['eligible'] if item['project'] == 'alpha')
        self.db.execute('INSERT INTO audit_cycles(id,lane,trigger,created_at,status,counts) VALUES(?,?,?,?,?,?)',
                        ('active-alpha', 'legacy', 'threshold', retry_fixture.NOW.isoformat(), 'ACTIVE', '{}'))
        self.db.execute('INSERT INTO audit_members(cycle_id,proposal_id,path,sha256,project,group_key,status) VALUES(?,?,?,?,?,?,?)',
                        ('active-alpha', meta['proposal_id'], meta['path'], meta['sha256'], 'alpha', group_key(meta), 'pending'))
        key = batch_key('alpha', [meta['path']])
        fp = fingerprint('alpha', [meta['path']], [meta['sha256'], sha(page.read_bytes())], policy_from(self.control['publication']), code_version())
        self.db.execute('INSERT INTO publications(batch,status,failures,result,attempt,next_due,category,fingerprint) VALUES(?,?,?,?,?,?,?,?)',
                        (key, 'BLOCKED', 1, '{}', 1, '2099-01-01T00:00:00+00:00', 'transient', fp))
        self.db.commit()
        old_row = self.db.execute('SELECT * FROM publications WHERE batch=?', (self.old_key,)).fetchone()
        reached = []
        def invoke(config, paths):
            from caretaker.config import load_config
            self.assertEqual(load_config(config).project_slug, 'test')
            reached.append(list(paths))
            self.assertEqual(self.db.execute('SELECT count(*) FROM repaired_retry_tokens').fetchone()[0], 1)
            return {'status': 'PUBLISHED', 'model_calls': 0}
        with patch('caretaker.draft_summary.inventory_history_repair_plan', return_value={}), \
                patch.object(maintenance, '_invoke_curate', side_effect=invoke):
            result = self.drain()
        self.assertEqual(reached, [[old['paths'][0]]])
        self.assertEqual([item['project'] for item in result['batches']], ['alpha', 'test'])
        self.assertEqual(self.db.execute('SELECT status FROM audit_members WHERE cycle_id="active-alpha"').fetchone()[0], 'pending')
        self.assertEqual(self.db.execute('SELECT * FROM publications WHERE batch=?', (self.old_key,)).fetchone(), old_row)


class TerminalOwnedRootEvidenceTests(unittest.TestCase):
    def setUp(self):
        import copy
        from caretaker.pubstate import batch_key
        TEMP.mkdir(parents=True, exist_ok=True)
        temporary = tempfile.TemporaryDirectory(prefix='owned72-', dir=TEMP)
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.owner = self.root / 'owner'
        self.state = self.owner / 'projects/test/state'
        self.phase = self.state / 'curation'
        self.phase.mkdir(parents=True)
        self.vault = self.root / 'vault'
        self.vault.mkdir()
        config = json.loads((retry_fixture.ROOT / 'config.example.json').read_text(encoding='utf-8'))
        config.update(project_slug='test', state_dir=str(self.state), maintenance_state_dir=str(self.owner))
        config['hub']['vault'] = str(self.vault)
        self.config = self.state.parent / 'config.json'
        self.config.write_text(json.dumps(config), encoding='utf-8')
        path = 'proposals/fixture/one.md'
        digest = 'a' * 64
        batch = batch_key('test', [path])
        value = dict(status='QUARANTINED', reason='phase_resume_terminal_no_replay',
                     original_reason='phase_resume_terminal_no_replay', project='test',
                     paths=[path], proposal_ids=['prop-one'], hashes=[digest],
                     batch=batch, cycle_id='audit-new-source')
        self.old = dict(result=value, reason=value['reason'], terminal_recovery=True,
                        project='test', paths=[path], proposal_ids=['prop-one'],
                        hashes=[digest], batch=batch, cycle_id=value['cycle_id'])
        progress = {'phase': 'REPAIR_STARTED', 'status': 'DEFERRED_BUDGET'}
        record = {'project': 'test', 'paths': [path], 'phase': 'REPAIR_STARTED',
                  'progress': progress, 'code_version': code_version()}
        pointer = self.phase / 'pointer.json'
        pointer.write_text(json.dumps(record), encoding='utf-8')
        self.plan = dict(phase_state=self.phase, pointer=pointer, record=record,
                        progress=progress, run_root=self.phase / 'run', pins={},
                        manifest_sha256='b' * 64, terminal_report_sha256='c' * 64,
                        batch=batch, cycle_id=value['cycle_id'], frozen={
                            'vault_path': str(self.vault), 'attestation_state_dir': str(self.state),
                            'proposals': [{'id': 'prop-one', 'path': path, 'sha256': digest}]})
        self.fresh = copy.deepcopy(self.plan)

    def check(self, old=None, plan=None, *, fresh=True):
        from caretaker.repaired_retry import _terminal_owned_absence_result
        before = {p.relative_to(self.root).as_posix(): p.read_bytes() for p in self.root.rglob('*') if p.is_file()}
        serialized = json.dumps(self.old, sort_keys=True)
        # This leaf represents the already-tested closed terminal proof. The
        # adapter must repeat that proof and reject missing or changed evidence.
        with patch('caretaker.terminal_generation.terminal_generation_plan',
                   return_value=self.fresh if fresh else None) as proof:
            result = _terminal_owned_absence_result(self.vault, old or self.old, plan or self.plan)
        self.assertEqual(json.dumps(self.old, sort_keys=True), serialized)
        self.assertEqual({p.relative_to(self.root).as_posix(): p.read_bytes() for p in self.root.rglob('*') if p.is_file()}, before)
        return result, proof.call_count

    def test_missing_pointer_gets_only_revalidated_owned_absence_evidence(self):
        from caretaker.pubstate import canonical_verdict
        self.assertEqual(canonical_verdict(self.vault, {'result': json.dumps(self.old['result'])})[1]['reason'],
                         'missing_owned_state_root')
        result, calls = self.check()
        self.assertEqual(calls, 1)
        self.assertEqual(result['state_dir'], str(self.state.resolve()))
        self.assertNotIn('state_dir', self.old['result'])
        self.assertEqual(canonical_verdict(self.vault, {'result': json.dumps(result)})[0], 'absent')

    def test_wrong_project_vault_identity_hash_and_unknown_root_fail_closed(self):
        import copy
        variants = []
        plan = copy.deepcopy(self.plan); plan['frozen']['vault_path'] = str(self.root / 'other-vault'); variants.append(plan)
        plan = copy.deepcopy(self.plan); plan['frozen']['proposals'][0]['id'] = 'prop-other'; variants.append(plan)
        plan = copy.deepcopy(self.plan); plan['frozen']['proposals'][0]['sha256'] = 'd' * 64; variants.append(plan)
        plan = copy.deepcopy(self.plan); plan['frozen']['proposals'][0]['path'] = 'proposals/fixture/other.md'; variants.append(plan)
        plan = copy.deepcopy(self.plan); plan['frozen']['attestation_state_dir'] = str(self.root); variants.append(plan)
        plan = copy.deepcopy(self.plan); plan['phase_state'] = self.root / 'missing/curation'; variants.append(plan)
        for plan in variants:
            with self.subTest(plan_variant=len(variants)):
                self.assertIsNone(self.check(plan=plan)[0])
        wrong = copy.deepcopy(self.old); wrong['project'] = 'other'
        self.assertIsNone(self.check(old=wrong)[0])

    def test_active_unknown_mutated_and_unproven_generations_fail_closed(self):
        import copy
        for status in ('RUNNING', 'UNKNOWN', 'UNKNOWN_REQUIRES_RECONCILIATION'):
            plan = copy.deepcopy(self.plan); plan['progress']['status'] = status
            with self.subTest(status=status):
                self.assertIsNone(self.check(plan=plan)[0])
        plan = copy.deepcopy(self.plan); plan['record']['code_version'] = 'd' * 32
        self.assertIsNone(self.check(plan=plan)[0])
        self.assertIsNone(self.check(fresh=False)[0])
        old = copy.deepcopy(self.old); old['result']['state_dir'] = str(self.root / 'unverified')
        self.assertIsNone(self.check(old=old)[0])

    def test_reparse_owned_root_is_rejected(self):
        import stat
        real = Path.lstat
        def attributes(path, *args, **kwargs):
            if path == self.phase:
                return SimpleNamespace(st_mode=stat.S_IFDIR | 0o700, st_file_attributes=0x400)
            return real(path, *args, **kwargs)
        with patch.object(Path, 'lstat', attributes):
            self.assertIsNone(self.check()[0])

    def test_owned_proof_never_overrides_uncertain_canonical_evidence(self):
        import caretaker.repaired_retry as retry
        db = retry_fixture._db()
        self.addCleanup(db.close)
        retry.ensure_retry_table(db)
        old = self.old
        db.execute('INSERT INTO audit_cycles(id,lane,trigger,created_at,status,counts) VALUES(?,?,?,?,?,?)',
                   (old['cycle_id'], 'new', 'threshold', retry_fixture.NOW.isoformat(), 'DRAINED', '{}'))
        db.execute('INSERT INTO audit_members(cycle_id,proposal_id,path,sha256,project,group_key,status) VALUES(?,?,?,?,?,?,?)',
                   (old['cycle_id'], old['proposal_ids'][0], old['paths'][0], old['hashes'][0], 'test', 'test', 'quarantined'))
        db.execute('INSERT INTO publications(batch,status,failures,result,attempt,category,fingerprint) VALUES(?,?,?,?,?,?,?)',
                   (old['batch'], 'QUARANTINED', 4, json.dumps(old['result']), 4, 'deterministic', 'a' * 64))
        db.commit()
        summary = {'eligible': [{'proposal_id': old['proposal_ids'][0], 'path': old['paths'][0],
                                'sha256': old['hashes'][0], 'project': 'test'}], 'policy': {'max_batch': 8}}
        def candidate():
            return retry.repaired_retry_candidates(db, summary, 'legacy', lambda p: p == 'test', self.vault,
                source_preflight=lambda meta: True, publication_path=retry_fixture.ROOT / 'caretaker/publication.py',
                curate_path=retry_fixture.ROOT / 'curate_once.py', phase_cache_preflight=lambda *args: False,
                terminal_generation_preflight=lambda *args: self.plan,
                terminal_generation_path=retry_fixture.ROOT / 'caretaker/terminal_generation.py')
        before = db.execute('SELECT * FROM publications').fetchall()
        with patch('caretaker.terminal_generation.terminal_generation_plan', return_value=self.fresh):
            self.assertEqual(len(candidate()), 1)
            ledger = self.vault / 'governance/curations/broken.json'
            ledger.parent.mkdir(parents=True)
            ledger.write_text('{broken', encoding='utf-8')
            self.assertEqual(candidate(), [])
        self.assertEqual(db.execute('SELECT * FROM publications').fetchall(), before)
        self.assertEqual(db.execute('SELECT count(*) FROM repaired_retry_tokens').fetchone()[0], 0)


if __name__ == '__main__':
    unittest.main()
