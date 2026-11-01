"""Synthetic new-lane publication owner and provenance retry coverage."""
from __future__ import annotations

import datetime as dt
import json
import re
import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import curate_once
import maintenance
from caretaker import tick_budget
from caretaker.audit import ensure_tables, group_key, policy_from, summarize
from caretaker.config import load_config
from caretaker.publication import PublicationError, fm, freeze, sha
from caretaker.pubstate import batch_key, code_version, ensure_pub_tables, fingerprint
from caretaker.repaired_retry import (
    REPAIRED_PUBLICATION_SHA256, SUMMARY_REPAIR_REASON,
    TARGET_PROVENANCE_RETRY_REASON, ensure_retry_table, retry_token,
)
from caretaker.transports import TransportError


ROOT = Path(__file__).resolve().parents[1]
TEST_TMP = ROOT / '.test-tmp'
PROJECT = 'test'
MAIN = 'wiki/projects/test.md'
NOW = dt.datetime(2026, 10, 24, 12, 0, tzinfo=dt.timezone.utc)
POLICY = {
    'enabled': True, 'include_existing_hub_backlog': False,
    'projects': [PROJECT], 'held_projects': [], 'min_ready_groups': 1,
    'max_wait_hours': 24, 'max_batch': 8,
    'maker_timeout_seconds': 500, 'reviewer_timeout_seconds': 500,
    'reviewer': {'mode': 'muse'},
}


def _workspace(test):
    TEST_TMP.mkdir(parents=True, exist_ok=True)
    temp = tempfile.TemporaryDirectory(prefix='publication-owner-', dir=str(TEST_TMP))
    test.addCleanup(temp.cleanup)
    root = Path(temp.name)
    vault = root / 'vault'
    vault.mkdir()
    source = vault / 'raw/sources/2026/10/src-one'
    source.mkdir(parents=True)
    original = b'Synthetic source for a bounded publication test.'
    (source / 'original.md').write_bytes(original)
    (source / 'source.json').write_text(json.dumps({
        'source_id': 'src-one', 'classification': 'internal', 'local_only': False,
        'original_file': 'original.md', 'sha256': sha(original),
        'byte_length': len(original),
    }), encoding='utf-8')
    return root, vault


def _write_page(vault: Path, *, draft=False):
    page = vault / MAIN
    page.parent.mkdir(parents=True, exist_ok=True)
    if draft:
        text = (
            '---\ntitle: Test\ntype: project\nstatus: draft\n'
            'summary: Producer reports.\nlast_verified: unverified\n'
            'verification_scope: producer-reports\nsources: [src-one]\n'
            'project_slug: test\n---\n\n'
            '## Caretaker publication 2026-10-20\nSynthetic prior report (src-one).\n'
        )
    else:
        text = (
            '---\ntitle: Test\nstatus: reviewed\nproject_slug: test\n'
            'sources: [src-one]\n---\n\n# Test\n'
        )
    page.write_text(text, encoding='utf-8')
    return page


def _attest_draft(vault: Path, state: Path, page: Path):
    run = state / 'curation' / 'prior-run'
    candidate = run / 'candidate'
    candidate.mkdir(parents=True)
    manifest = candidate / 'publication.json'
    manifest.write_text(json.dumps({
        'outputs': [{'path': MAIN, 'sha256': sha(page.read_bytes())}],
    }), encoding='utf-8')
    digest = sha(manifest.read_bytes())
    event_id = 'evt-prior-test'
    (run / 'result.json').write_text(json.dumps({
        'status': 'PUBLISHED', 'manifest': str(manifest),
        'manifest_sha256': digest,
        'publication': {'event_id': event_id, 'manifest_sha256': digest},
    }), encoding='utf-8')
    ledger = vault / 'governance/curations' / (event_id + '.json')
    ledger.parent.mkdir(parents=True)
    ledger.write_text(json.dumps({
        'event_id': event_id, 'manifest_sha256': digest, 'proposals': [],
        'review': {
            'verdict': 'PASS', 'maker': 'openclaw-caretaker',
            'manifest_sha256': digest,
            'reviewer': {'id': 'independent-reviewer'},
        },
    }), encoding='utf-8')


def _write_proposal(vault: Path, ident='prop-20261024T120100000Z-fresh'):
    proposal = vault / 'proposals/agent' / (ident + '.md')
    proposal.parent.mkdir(parents=True, exist_ok=True)
    proposal.write_text(
        f'---\nid: {ident}\nclassification: internal\nproject_slug: test\n'
        'source_ids:\n  - src-one\ntarget_pages:\n  - wiki/projects/test.md\n'
        '---\nSynthetic proposal tied to src-one.\n',
        encoding='utf-8',
    )
    return proposal


def _base(root: Path, vault: Path):
    base = json.loads((ROOT / 'config.example.json').read_text(encoding='utf-8'))
    base.update(project_slug=PROJECT, state_dir=str(root / 'state'))
    base['hub']['vault'] = str(vault)
    base['hub']['script'] = str(root / 'synthetic-knowledge.py')
    base['hub']['context_receipt'] = str(root / 'context-receipt.json')
    return base


def _control():
    return {
        'known_projects': [PROJECT], 'permitted_projects': [PROJECT],
        'max_projects_per_tick': 4, 'max_batches_per_tick': 4,
        'publication': dict(POLICY),
    }


def _db(vault):
    db = sqlite3.connect(':memory:')
    ensure_tables(db)
    ensure_pub_tables(db)
    db.execute(
        'CREATE TABLE queue(id TEXT PRIMARY KEY,project TEXT,payload TEXT NOT NULL,'
        'forwarded INTEGER NOT NULL DEFAULT 0)'
    )
    maintenance.activate_new_event_epoch(db, vault, NOW)
    return db


def _seed_target_quarantine(db, vault, state, proposal):
    summary = summarize(vault, policy_from(POLICY), now=NOW.timestamp())
    meta = next(item for item in summary['eligible'] if item['path'] == proposal.relative_to(vault).as_posix())
    paths = [meta['path']]
    pids = [meta['proposal_id']]
    hashes = [meta['sha256']]
    old_cycle = 'audit-new-target-provenance-old'
    key = batch_key(PROJECT, paths)
    policy = policy_from(POLICY)
    current_fp = fingerprint(PROJECT, paths, hashes + [sha((vault / MAIN).read_bytes())],
                             policy, code_version())
    result = {
        'status': 'QUARANTINED', 'reason': TARGET_PROVENANCE_RETRY_REASON,
        'original_reason': TARGET_PROVENANCE_RETRY_REASON, 'batch': key,
        'cycle_id': old_cycle, 'proposal_ids': pids, 'paths': paths,
        'hashes': hashes, 'project': PROJECT,
        'state_dir': str(state / 'new-event-epoch' / 'projects' / PROJECT / 'state'),
    }
    db.execute(
        'INSERT INTO audit_cycles(id,trigger,created_at,status,counts,lane) VALUES(?,?,?,?,?,?)',
        (old_cycle, 'threshold', NOW.isoformat(), 'DRAINED', '{}', 'new'),
    )
    db.execute(
        'INSERT INTO audit_members(cycle_id,proposal_id,path,sha256,project,group_key,status,reason) '
        'VALUES(?,?,?,?,?,?,?,?)',
        (old_cycle, pids[0], paths[0], hashes[0], PROJECT, group_key(meta),
         'quarantined', 'QUARANTINED'),
    )
    db.execute(
        'INSERT INTO publications(batch,status,failures,result,attempt,next_due,category,fingerprint,updated_at) '
        'VALUES(?,?,?,?,?,?,?,?,?)',
        (key, 'QUARANTINED', 2, json.dumps(result), 2, '', 'deterministic',
         current_fp, NOW.isoformat()),
    )
    db.commit()
    return key, current_fp


class _Clock:
    def __init__(self):
        self.value = 0.0

    def __call__(self):
        return self.value

    def advance(self, seconds):
        self.value += seconds


class _Maker:
    def __init__(self, proposal_id, *, clock=None, advance=0):
        self.calls = 0
        self.clock = clock
        self.advance = advance
        self.proposal_id = proposal_id

    def invoke(self, **_kwargs):
        self.calls += 1
        if self.clock:
            self.clock.advance(self.advance)
        return SimpleNamespace(
            ok=True, status='ok', provider='vllm', model='Qwen3.8-27B',
            session_id='synthetic-maker',
            final=json.dumps({
                'body': '# Synthetic report\nDated source note (src-one).\n',
                'summary': 'Synthetic report.',
                'dispositions': {self.proposal_id: {
                    'disposition': 'synthesized', 'reason': 'Cited synthetic source.',
                }},
            }),
        )


class _SummaryOnlyMaker:
    def __init__(self, proposal_id, *, fleet=False):
        self.calls = 0
        self.prompts = []
        self.proposal_id = proposal_id
        self.fleet = fleet
        self.body = '# Synthetic report\nDated source note (src-one).\n'
        self.dispositions = {proposal_id: {
            'disposition': 'synthesized', 'reason': 'Cited synthetic source.',
        }}

    def _encode(self, value):
        if not self.fleet:
            return json.dumps(value)
        return json.dumps({
            'status': 'completed', 'summary': json.dumps(value),
            'files_changed': [], 'verification': [], 'risks': [], 'next_steps': [],
        })

    def invoke(self, *, prompt, **_kwargs):
        self.calls += 1
        self.prompts.append(prompt)
        if self.calls == 1:
            final = self._encode({
                'body': self.body,
                'summary': {'invalid': 'summary type'},
                'dispositions': self.dispositions,
            })
        elif self.calls == 2:
            final = self._encode({'summary': 'Synthetic concise report.'})
        else:
            raise AssertionError('summary repair must stay bounded')
        return SimpleNamespace(
            ok=True, status='ok', provider='vllm', model='Qwen3.8-27B',
            session_id=f'synthetic-maker-{self.calls}', final=final,
        )


class _SummaryRetrySequenceMaker:
    def __init__(self, proposal_id):
        self.calls = 0
        self.prompts = []
        self.body = '# Synthetic report\nDated source note (src-one).\n'
        self.dispositions = {proposal_id: {
            'disposition': 'synthesized', 'reason': 'Cited synthetic source.',
        }}
        self.responses = [
            {
                'body': self.body,
                'summary': {'invalid': 'initial summary type'},
                'dispositions': self.dispositions,
            },
            {'summary': 123},
            {'summary': 'Synthetic concise report.'},
        ]

    def invoke(self, *, prompt, **_kwargs):
        self.calls += 1
        self.prompts.append(prompt)
        return SimpleNamespace(
            ok=True, status='ok', provider='vllm', model='Qwen3.8-27B',
            session_id=f'synthetic-summary-retry-{self.calls}',
            final=json.dumps(self.responses[self.calls - 1]),
        )


class _Reviewer:
    def __init__(self, actions=(), *, clock=None, advance=0):
        self.calls = 0
        self.actions = list(actions)
        self.clock = clock
        self.advance = advance

    def invoke(self, *, prompt, **_kwargs):
        self.calls += 1
        if self.clock:
            self.clock.advance(self.advance)
        action = self.actions.pop(0) if self.actions else 'PASS'
        if action == 'timeout':
            raise TransportError('synthetic reviewer timeout', reason_code='process_timeout')
        match = re.search(r'manifest_sha256":"([a-f0-9]{64})"', prompt)
        if not match:
            raise AssertionError('review input did not bind a manifest digest')
        verdict = 'REPAIR' if action == 'REPAIR' else 'PASS'
        findings = ([{'class': 'unsupported_claim', 'weight': 'blocking',
                      'text': 'synthetic unsupported claim'}]
                    if verdict == 'REPAIR' else [])
        return SimpleNamespace(
            ok=True, status='ok', provider='muse-code', model='muse-spark-1.3',
            session_id=f'synthetic-review-{self.calls}',
            final=json.dumps({
                'verdict': verdict, 'manifest_sha256': match.group(1),
                'findings': findings,
            }),
        )


def _curator_patches(root):
    gateway = SimpleNamespace(
        health=lambda: SimpleNamespace(status='pass'),
        lint=lambda: SimpleNamespace(status='pass'),
    )

    def publish(_cfg, _manifest, _review, _run_root, result):
        result.update(
            status='PUBLISHED', publication={'status': 'PUBLISHED'},
            checkpoint={'status': 'CHECKPOINTED'},
        )

    return (
        patch.object(curate_once, 'CanonicalKnowledgeGateway', return_value=gateway),
        patch.object(curate_once, '_write_run_config', return_value=str(root / 'run-config.json')),
        patch.object(curate_once, '_publish_under_lock', side_effect=publish),
    )


def _runtime_cfg(root, vault, state, db):
    return maintenance._project_runtime_cfg(
        _base(root, vault), PROJECT, state / 'new-event-epoch',
        lane='new', database=db,
    )


def _phase_pointer(state, proposal_path, phase):
    from caretaker.pubstate import batch_key

    pointer = state / 'curation' / (
        'phase-' + batch_key(PROJECT, [proposal_path]) + '.json'
    )
    pointer.parent.mkdir(parents=True, exist_ok=True)
    pointer.write_text(json.dumps({'phase': phase, 'synthetic': True}), encoding='utf-8')
    return pointer


class PublicationStateOwnerTests(unittest.TestCase):
    def test_normal_source_misattribution_retry_repairs_and_reviews_fresh(self):
        from caretaker.publication import (
            BOOTSTRAP_RECEIPT_PARAGRAPH, build_candidate, knowledge_basis,
            safe_file,
        )

        root, vault = _workspace(self)
        (vault / 'wiki').mkdir(parents=True, exist_ok=True)
        (vault / 'wiki/registry.md').write_text(
            '```json\n' + json.dumps({
                'schema_version': 1, 'projects': {PROJECT: {'main_page': MAIN}},
                'aliases': {},
            }) + '\n```\n', encoding='utf-8',
        )
        state = root / 'state'
        state.mkdir()
        owner_state = state / 'projects' / PROJECT / 'state'
        curation = owner_state / 'curation'
        curation.mkdir(parents=True)

        def add_source(source_id, content):
            source_dir = vault / 'raw/sources/2026/10' / source_id
            source_dir.mkdir(parents=True, exist_ok=True)
            original = source_dir / 'original.md'
            original.write_bytes(content.encode('utf-8'))
            data = original.read_bytes()
            (source_dir / 'source.json').write_text(json.dumps({
                'source_id': source_id, 'classification': 'internal',
                'local_only': False, 'original_file': 'original.md',
                'sha256': sha(data), 'byte_length': len(data),
            }), encoding='utf-8')

        receipt = (
            '# Automatically discovered local project root\n\n'
            + BOOTSTRAP_RECEIPT_PARAGRAPH
            + '\n\nProject slug: test\nDirectory identity SHA-256: '
            + 'a' * 64 + '\n'
        )
        add_source('src-one', receipt)
        add_source('src-old-two', receipt.replace('Directory identity SHA-256: ' + 'a' * 64,
                                                   'Directory identity SHA-256: ' + 'b' * 64))
        old_proposal = vault / 'proposals/agent/prop-old-bootstrap.md'
        old_proposal.parent.mkdir(parents=True, exist_ok=True)
        old_proposal.write_text(
            '---\nid: prop-old-bootstrap\nclassification: internal\nproject_slug: test\n'
            'source_ids:\n  - src-one\ntarget_pages:\n  - wiki/projects/test.md\n---\n'
            'Metadata receipt only.\n', encoding='utf-8',
        )
        old_proposal_two = vault / 'proposals/agent/prop-old-bootstrap-two.md'
        old_proposal_two.write_text(
            '---\nid: prop-old-bootstrap-two\nclassification: internal\nproject_slug: test\n'
            'source_ids:\n  - src-old-two\ntarget_pages:\n  - wiki/projects/test.md\n---\n'
            'Second metadata receipt only.\n', encoding='utf-8',
        )
        old_paths = sorted([
            old_proposal.relative_to(vault).as_posix(),
            old_proposal_two.relative_to(vault).as_posix(),
        ])
        old_frozen = freeze(vault, old_paths, MAIN, attestation_state_dir=owner_state)
        old_decision = {
            'body': 'The discovery receipts recorded two D-drive directory identities and no verified app contents (src-one, src-old-two).',
            'summary': 'Two metadata-only directory receipts.',
            'dispositions': {old_proposal.stem: {
                'disposition': 'synthesized', 'reason': 'Exact bootstrap receipt.',
            }, old_proposal_two.stem: {
                'disposition': 'synthesized', 'reason': 'Exact bootstrap receipt.',
            }},
        }
        self.assertEqual(knowledge_basis(
            old_frozen, old_decision['body'], old_decision['dispositions'],
        )['status'], 'unconfirmed')
        old_run = curation / 'prior-inventory-run'
        old_candidate = old_run / 'candidate'
        old_manifest = build_candidate(
            old_frozen, old_decision, old_candidate, '2026-10-20T08:00:00Z',
        )
        old_doc = json.loads(old_manifest.read_text(encoding='utf-8'))
        old_target = next(item for item in old_doc['outputs'] if item['path'] == MAIN)
        old_payload = safe_file(old_candidate, old_target['payload_path'])
        page = vault / MAIN
        page.parent.mkdir(parents=True, exist_ok=True)
        page.write_bytes(old_payload.read_bytes())
        old_digest = sha(old_manifest.read_bytes())
        event_id = 'evt-prior-inventory-test'
        old_result = {
            'status': 'PUBLISHED', 'manifest': str(old_manifest),
            'manifest_sha256': old_digest,
            'publication': {'status': 'PUBLISHED', 'event_id': event_id,
                            'manifest_sha256': old_digest},
            'proposal_ids': [old_proposal.stem, old_proposal_two.stem],
        }
        old_run.mkdir(parents=True, exist_ok=True)
        (old_run / 'result.json').write_text(json.dumps(old_result), encoding='utf-8')
        ledger_dir = vault / 'governance/curations'
        ledger_dir.mkdir(parents=True, exist_ok=True)
        (ledger_dir / (event_id + '.json')).write_text(json.dumps({
            'event_id': event_id, 'manifest_sha256': old_digest,
            'review': {'verdict': 'PASS', 'manifest_sha256': old_digest,
                       'maker': 'openclaw-caretaker',
                       'reviewer': {'id': 'muse-independent-reviewer'}},
            'proposals': [
                {'proposal_id': old_proposal.stem,
                 'path': old_proposal.relative_to(vault).as_posix(),
                 'sha256': sha(old_proposal.read_bytes()), 'disposition': 'synthesized'},
                {'proposal_id': old_proposal_two.stem,
                 'path': old_proposal_two.relative_to(vault).as_posix(),
                 'sha256': sha(old_proposal_two.read_bytes()), 'disposition': 'synthesized'},
            ],
        }), encoding='utf-8')

        add_source('src-two', 'Names, addresses, and contact details should not be added to the Wiki.')
        proposal = vault / 'proposals/agent/prop-20261024T130000000Z-app-correction.md'
        proposal.write_text(
            f'---\nid: {proposal.stem}\nclassification: internal\nproject_slug: test\n'
            'source_ids:\n  - src-two\ntarget_pages:\n  - wiki/projects/test.md\n---\n'
            'Keep the Wiki privacy statement within the source scope.\n',
            encoding='utf-8',
        )
        paths = [proposal.relative_to(vault).as_posix()]
        frozen = freeze(vault, paths, MAIN, attestation_state_dir=owner_state)
        expected_summary = 'Correction: source limits names, addresses, and contact details in the Wiki (src-two).'
        decision = {
            'body': 'All personal data remained outside the entire Hub (src-two).',
            'summary': expected_summary,
            'dispositions': {proposal.stem: {
                'disposition': 'synthesized',
                'reason': 'The matching move row supports the correction.',
                'knowledge_class': 'durable-fact',
            }},
        }
        config_path, runtime_cfg = maintenance._project_runtime_cfg(
            _base(root, vault), PROJECT, state, lane='legacy', database=None,
        )
        cfg = load_config(config_path)
        from caretaker.draft_summary import corrected_draft_decision, patch_draft_summary_candidate
        selected = corrected_draft_decision(frozen, decision)
        self.assertIsNotNone(selected)
        run_root = curation / 'mail-current-run'
        run_root.mkdir(parents=True)
        candidate = run_root / 'candidate'
        initial_manifest = build_candidate(frozen, selected, candidate, '2026-10-24T17:49:00Z')
        self.assertTrue(patch_draft_summary_candidate(frozen, selected, candidate, initial_manifest))
        initial_digest = sha(initial_manifest.read_bytes())
        from caretaker.draft_summary import (
            archive_and_relocate_inventory_history,
            prepared_inventory_history_plan,
        )
        prepared_run_root = curation / 'mail-prepared-run'
        prepared_run_root.mkdir()
        prepared_candidate = prepared_run_root / 'candidate'
        prepared_manifest = build_candidate(
            frozen, selected, prepared_candidate, '2026-10-24T17:49:00Z',
        )
        self.assertTrue(patch_draft_summary_candidate(
            frozen, selected, prepared_candidate, prepared_manifest,
        ))
        prepared_progress = {
            'phase': 'PREPARED', 'maker_calls': 1, 'review_calls': 0,
            'maker_provider': 'kimi', 'maker_model': 'k3', 'maker_route': 'fleet_append',
            'observed': '2026-10-24T17:49:00Z', 'review_rounds': [],
        }
        curate_once._save_phase_progress(
            cfg, paths, frozen, prepared_run_root, prepared_progress['observed'],
            'PREPARED', prepared_candidate, prepared_manifest, prepared_progress,
        )
        loaded_prepared = curate_once._load_phase_progress(
            cfg, paths, curation, frozen,
        )
        prepared_plan = prepared_inventory_history_plan(
            cfg, paths, frozen, loaded_prepared,
        )
        self.assertIsNotNone(prepared_plan)
        nonbootstrap_prior = dict(prepared_plan['prior_inventory'])
        nonbootstrap_prior['canonical_bootstrap_metadata'] = False
        with patch('caretaker.draft_summary._prior_inventory_body_proof',
                   return_value=nonbootstrap_prior):
            nonbootstrap_plan = prepared_inventory_history_plan(
                cfg, paths, frozen, loaded_prepared,
            )
        self.assertIsNotNone(nonbootstrap_plan)
        drifted_prepared = dict(frozen, snapshot_sha256='0' * 64)
        self.assertIsNone(prepared_inventory_history_plan(
            cfg, paths, drifted_prepared, loaded_prepared,
        ))
        unknown_prepared = dict(loaded_prepared)
        unknown_prepared['progress'] = dict(loaded_prepared['progress'], status='UNKNOWN')
        self.assertIsNone(prepared_inventory_history_plan(
            cfg, paths, frozen, unknown_prepared,
        ))
        transformed = archive_and_relocate_inventory_history(
            prepared_plan, frozen, cfg, paths, build_candidate,
            curate_once._save_phase_progress,
        )
        self.assertIsNotNone(transformed)
        transformed_loaded = curate_once._load_phase_progress(
            cfg, paths, curation, frozen,
        )
        self.assertEqual(transformed_loaded['phase'], 'PREPARED')
        self.assertEqual(transformed_loaded['maker_calls'], 1)
        self.assertEqual(transformed_loaded['review_calls'], 0)
        self.assertTrue(transformed_loaded['progress']['deterministic_inventory_history_relocation'])
        transformed_manifest = Path(transformed_loaded['manifest_path'])
        transformed_doc = json.loads(transformed_manifest.read_text(encoding='utf-8'))
        transformed_output = next(item for item in transformed_doc['outputs'] if item['path'] == MAIN)
        transformed_page = (transformed_manifest.parent / transformed_output['payload_path']).read_text(encoding='utf-8')
        from caretaker.publication import existing_page_body
        prior_body = existing_page_body(old_payload.read_text(encoding='utf-8'))
        self.assertIn('## Historical evidence', transformed_page)
        self.assertTrue(transformed_page.endswith(prior_body))
        self.assertNotEqual(transformed_loaded['manifest_sha256'], initial_digest)

        hook_run_root = curation / 'mail-prepared-hook-run'
        hook_run_root.mkdir()
        hook_candidate = hook_run_root / 'candidate'
        hook_manifest = build_candidate(
            frozen, selected, hook_candidate, '2026-10-24T17:49:00Z',
        )
        self.assertTrue(patch_draft_summary_candidate(
            frozen, selected, hook_candidate, hook_manifest,
        ))
        curate_once._save_phase_progress(
            cfg, paths, frozen, hook_run_root, prepared_progress['observed'],
            'PREPARED', hook_candidate, hook_manifest, prepared_progress,
        )
        hook_loaded = []

        def prepared_only_run(run_config, run_paths, publish_flag, **_kwargs):
            self.assertEqual(run_paths, paths)
            self.assertTrue(publish_flag)
            loaded = curate_once._load_phase_progress(
                cfg, run_paths, curation, frozen,
            )
            hook_loaded.append(loaded)
            return {'status': 'PENDING', 'reason': 'prepared_test_complete'}

        with patch.object(curate_once, 'run', side_effect=prepared_only_run):
            prepared_result = maintenance._invoke_curate(config_path, paths)
        self.assertEqual(prepared_result['reason'], 'prepared_test_complete')
        self.assertEqual(len(hook_loaded), 1)
        self.assertTrue(hook_loaded[0]['progress']['deterministic_inventory_history_relocation'])
        self.assertEqual(hook_loaded[0]['maker_calls'], 1)
        self.assertEqual(hook_loaded[0]['review_calls'], 0)
        self.assertIsNone(hook_loaded[0].get('assessment'))

        assessment = {
            'verdict': 'REPAIR', 'manifest_sha256': initial_digest,
            'findings': [
                {'class': 'source_misattribution', 'weight': 'blocking',
                 'text': 'The current source supports restricting names, addresses, and contacts in Wiki, not all personal data outside the entire Hub.'},
            ],
        }
        progress = {
            'phase': 'REPAIR_FAILED', 'maker_calls': 2, 'review_calls': 1,
            'maker_provider': 'kimi', 'maker_model': 'k3', 'maker_route': 'fleet_append',
            'observed': '2026-10-24T17:49:00Z',
            'assessment': assessment,
            'review_rounds': [{'round': 1, 'verdict': 'REPAIR'}],
        }
        curate_once._save_phase_progress(
            cfg, paths, frozen, run_root, progress['observed'],
            'REPAIR_FAILED', None, None, progress,
        )
        initial_result = {
            'status': 'PENDING', 'reason': 'citation_outside_publication',
            'error_code': 'citation_outside_publication',
            'review_repair_exhausted': True, 'format_repair_exhausted': False,
            'candidate_path': None, 'run_path': str(run_root),
            'maker_calls': 2, 'review_calls': 1,
            'proposal_ids': [proposal.stem], 'paths': paths,
            'hashes': [sha(proposal.read_bytes())], 'project': PROJECT,
            'snapshot_sha256': frozen['snapshot_sha256'], 'target': MAIN,
            'publication': None, 'checkpoint': None,
        }
        (run_root / 'result.json').write_text(json.dumps(initial_result), encoding='utf-8')

        db = sqlite3.connect(state / 'intake.sqlite')
        ensure_tables(db)
        ensure_pub_tables(db)
        self.addCleanup(db.close)
        current_cycle = 'audit-legacy-mail-current-failed'
        old_batch = batch_key(PROJECT, paths)
        meta = summarize(vault, policy_from(POLICY), now=NOW.timestamp())['eligible'][0]
        from caretaker.audit import group_key
        db.execute(
            'INSERT INTO audit_cycles(id,trigger,created_at,status,counts,lane) VALUES(?,?,?,?,?,?)',
            (current_cycle, 'threshold', NOW.isoformat(), 'DRAINED', '{}', 'legacy'),
        )
        db.execute(
            'INSERT INTO audit_members(cycle_id,proposal_id,path,sha256,project,group_key,status,reason) '
            'VALUES(?,?,?,?,?,?,?,?)',
            (current_cycle, proposal.stem, paths[0], initial_result['hashes'][0],
             PROJECT, group_key(meta), 'quarantined', 'QUARANTINED'),
        )
        failed_row = dict(initial_result, status='QUARANTINED', batch=old_batch,
                          cycle_id=current_cycle, state_dir=str(owner_state),
                          original_reason='citation_outside_publication')
        stored_fingerprint = fingerprint(
            PROJECT, paths, [sha(proposal.read_bytes()), sha((vault / MAIN).read_bytes())],
            policy_from(dict(POLICY, include_existing_hub_backlog=True)), code_version(),
        )
        db.execute(
            'INSERT INTO publications(batch,status,failures,result,attempt,next_due,category,fingerprint,updated_at) '
            'VALUES(?,?,?,?,?,?,?,?,?)',
            (old_batch, 'QUARANTINED', 2, json.dumps(failed_row), 2, '',
             'deterministic', stored_fingerprint, NOW.isoformat()),
        )
        db.commit()

        from caretaker.repaired_retry import _valid_inventory_history_row
        self.assertIsNotNone(_valid_inventory_history_row(
            old_batch, 'QUARANTINED', 2, 2, 'deterministic', 'f' * 64,
            json.dumps(failed_row),
        ))
        self.assertIsNone(_valid_inventory_history_row(
            old_batch, 'QUARANTINED', 3, 3, 'deterministic', 'f' * 64,
            json.dumps(failed_row),
        ))

        current_summary = summarize(vault, policy_from(POLICY), now=NOW.timestamp())
        self.assertEqual([item['path'] for item in current_summary['eligible']], paths)
        from caretaker.draft_summary import _prior_inventory_body_proof, inventory_history_repair_plan
        from caretaker.publication import _is_caretaker_draft
        self.assertEqual(fm(frozen['before'], 'status'), 'draft')
        self.assertTrue(_is_caretaker_draft(
            frozen['before'], MAIN, vault=vault, state_dir=owner_state,
            page_sha256=frozen['preimage_sha256'], expected_project=PROJECT,
        ))
        plan = inventory_history_repair_plan(cfg, paths, frozen, failed_row)
        self.assertIsNotNone(_prior_inventory_body_proof(vault, owner_state, frozen))
        self.assertIsNotNone(plan)
        self.assertTrue(plan['source_misattribution_repair'])
        self.assertIsNone(inventory_history_repair_plan(
            cfg, paths, frozen, dict(failed_row, publication={'status': 'PUBLISHED'}),
        ))
        drifted_frozen = dict(frozen)
        drifted_frozen['snapshot_sha256'] = '0' * 64
        self.assertIsNone(inventory_history_repair_plan(cfg, paths, drifted_frozen, failed_row))

        pointer = plan['pointer']
        original_phase = pointer.read_bytes()
        changed_phase = json.loads(original_phase)
        changed_phase['progress']['assessment']['findings'][0]['class'] = 'unrelated_review_finding'
        changed_phase['cache_sha256'] = curate_once._phase_digest(changed_phase)
        try:
            pointer.write_text(json.dumps(changed_phase), encoding='utf-8')
            self.assertIsNone(inventory_history_repair_plan(cfg, paths, frozen, failed_row))
        finally:
            pointer.write_bytes(original_phase)

        changed_phase = json.loads(original_phase)
        changed_phase['progress']['status'] = 'UNKNOWN_REQUIRES_RECONCILIATION'
        changed_phase['cache_sha256'] = curate_once._phase_digest(changed_phase)
        try:
            pointer.write_text(json.dumps(changed_phase), encoding='utf-8')
            self.assertIsNone(inventory_history_repair_plan(cfg, paths, frozen, failed_row))
        finally:
            pointer.write_bytes(original_phase)

        journal_dir = plan['candidate'] / '.curation-transactions'
        journal_dir.mkdir()
        journal = journal_dir / 'tx-inventory-history-test.json'
        try:
            journal.write_text(json.dumps({'status': 'applying'}), encoding='utf-8')
            self.assertIsNone(inventory_history_repair_plan(cfg, paths, frozen, failed_row))
        finally:
            journal.unlink()
            journal_dir.rmdir()

        source_two = vault / 'raw/sources/2026/10/src-two/original.md'
        original_source = source_two.read_bytes()
        try:
            source_two.write_bytes(original_source + b' drift')
            with self.assertRaises(PublicationError):
                freeze(vault, paths, MAIN, attestation_state_dir=owner_state)
        finally:
            source_two.write_bytes(original_source)
        from caretaker import repaired_retry
        retry_probe = repaired_retry.repaired_retry_candidates(
            db, current_summary, 'legacy', lambda name: name == PROJECT, vault,
            source_preflight=lambda _meta: True,
            inventory_history_preflight=lambda old: inventory_history_repair_plan(
                cfg, paths, frozen, old['result'],
            ) is not None,
            inventory_history_path=ROOT / 'caretaker' / 'draft_summary.py',
        )
        self.assertEqual(len(retry_probe), 1, retry_probe)
        def inventory_retry_allowed(retry_project, value, _stored_fp):
            return (retry_project == PROJECT and value.get('project') == PROJECT
                    and inventory_history_repair_plan(
                        cfg, paths, frozen, value,
                    ) is not None)

        summary_with_exact_plan = maintenance._audit_summary(
            vault, policy_from(dict(POLICY, include_existing_hub_backlog=True)),
            db, NOW, inventory_history_preflight=inventory_retry_allowed,
        )
        self.assertIn(proposal.stem, {
            item.get('proposal_id') for item in summary_with_exact_plan['eligible']
        })
        summary_without_plan = maintenance._audit_summary(
            vault, policy_from(dict(POLICY, include_existing_hub_backlog=True)),
            db, NOW, inventory_history_preflight=lambda *_args: False,
        )
        self.assertNotIn(proposal.stem, {
            item.get('proposal_id') for item in summary_without_plan['eligible']
        })
        calls = []
        published = []
        class SourceRepairMaker:
            def __init__(self):
                self.calls = 0
                self.prompts = []

            def invoke(self, *, prompt, **_kwargs):
                self.calls += 1
                self.prompts.append(prompt)
                return SimpleNamespace(
                    ok=True, status='ok', provider='kimi', model='k3',
                    session_id='source-misattribution-repair',
                    final=json.dumps({
                        'body': ('The source says names, addresses, and contact details '
                                 'should not be added to the Wiki (src-two).\n'),
                        'summary': expected_summary,
                        'dispositions': {proposal.stem: {
                            'disposition': 'synthesized',
                            'reason': 'The current source supports only its stated Wiki scope.',
                            'knowledge_class': 'durable-fact',
                        }},
                    }),
                )

        class FreshReviewer:
            def __init__(self):
                self.actions = ['REPAIR', 'PASS']
                self.calls = 0
                self.manifests = []

            def invoke(self, *, prompt, **_kwargs):
                self.calls += 1
                match = re.search(r'manifest_sha256":"([a-f0-9]{64})"', prompt)
                if not match:
                    raise AssertionError('review input did not bind a manifest digest')
                self.manifests.append(match.group(1))
                action = self.actions.pop(0)
                verdict = 'REPAIR' if action == 'REPAIR' else 'PASS'
                findings = ([{
                    'class': 'source_misattribution', 'weight': 'blocking',
                    'text': 'The current source supports the Wiki restriction only.',
                }] if verdict == 'REPAIR' else [])
                return SimpleNamespace(
                    ok=True, status='ok', provider='muse-code', model='muse-spark-1.3',
                    session_id=f'source-review-{self.calls}',
                    final=json.dumps({
                        'verdict': verdict, 'manifest_sha256': match.group(1),
                        'findings': findings,
                    }),
                )

        maker = SourceRepairMaker()
        reviewer = FreshReviewer()
        gateway = SimpleNamespace(
            health=lambda: SimpleNamespace(status='pass'),
            lint=lambda: SimpleNamespace(status='pass'),
        )

        original_run_with_config = curate_once._run_with_config
        marker_values = []

        def run_with_config(cfg, selected_paths, publish_flag, *_args, **_kwargs):
            marker_values.append(maintenance._validated_summary_retry_marker(cfg, selected_paths))
            return original_run_with_config(
                cfg, selected_paths, publish_flag, maker, reviewer,
            )

        def publish(_cfg, manifest, _review, _run_root, result):
            doc = json.loads(Path(manifest).read_text(encoding='utf-8'))
            output = next(item for item in doc['outputs'] if item['path'] == MAIN)
            payload = Path(manifest).parent / output['payload_path']
            published.append(payload.read_text(encoding='utf-8'))
            result.update(status='PUBLISHED', publication={'status': 'PUBLISHED'},
                          checkpoint={'status': 'CHECKPOINTED'})

        control = {'publication': dict(POLICY, include_existing_hub_backlog=True)}
        with patch.object(curate_once, '_run_with_config', side_effect=run_with_config), \
             patch.object(curate_once, 'CanonicalKnowledgeGateway', return_value=gateway), \
             patch.object(curate_once, '_write_run_config', return_value=str(root / 'run-config.json')), \
             patch.object(curate_once, '_publish_under_lock', side_effect=publish):
            result = maintenance.publication_tick(
                str(config_path), runtime_cfg, control, db, now=NOW, lane='legacy',
            )

        self.assertEqual(result['status'], 'PUBLISHED', result)
        self.assertEqual(len(marker_values), 1)
        self.assertIsNotNone(marker_values[0])
        self.assertEqual(maker.calls, 1)
        self.assertEqual(reviewer.calls, 2)
        self.assertEqual(result['maker_calls'], 3)
        self.assertEqual(result['review_calls'], 3)
        self.assertEqual(len(reviewer.manifests), 2)
        self.assertNotEqual(reviewer.manifests[0], initial_digest)
        self.assertNotEqual(reviewer.manifests[0], reviewer.manifests[1])
        self.assertIn('SOURCE-MISATTRIBUTION CORRECTION', maker.prompts[0])
        self.assertIn('Do not import citation IDs from the frozen before text', maker.prompts[0])
        self.assertIn('does not establish that all personal data remained outside', maker.prompts[0])
        self.assertEqual(len(published), 1)
        self.assertIn('## Historical evidence', published[0])
        self.assertIn('not asserted as current project knowledge', published[0])
        self.assertIn('The source says names, addresses, and contact details', published[0])
        self.assertNotIn('All personal data remained outside the entire Hub', published[0])
        from caretaker.publication import existing_page_body
        old_body = existing_page_body(old_payload.read_text(encoding='utf-8'))
        self.assertTrue(published[0].endswith(old_body))
        self.assertLess(published[0].find('## Caretaker publication 2026-10-24T17:49:00Z'),
                        published[0].find('## Historical evidence'))
        self.assertEqual(db.execute(
            'SELECT status FROM publications WHERE batch=?', (old_batch,),
        ).fetchone()[0], 'QUARANTINED')
        self.assertEqual(db.execute(
            'SELECT COUNT(*) FROM repaired_retry_tokens',
        ).fetchone()[0], 1)
        archive = run_root / ('inventory-history-archive-' + initial_digest[:16])
        self.assertTrue(archive.is_dir())
        archived_result = json.loads((archive / 'result.json').read_text(encoding='utf-8'))
        self.assertEqual(archived_result['status'], 'PENDING')
        archived_phase = json.loads((archive / 'phase.json').read_text(encoding='utf-8'))
        self.assertEqual(
            archived_phase['progress']['assessment']['findings'][0]['class'],
            'source_misattribution',
        )

    def test_new_drain_uses_legacy_draft_attestation_and_one_time_retry_token(self):
        root, vault = _workspace(self)
        state = root / 'state'
        state.mkdir()
        page = _write_page(vault, draft=True)
        db = _db(vault)
        self.addCleanup(db.close)
        maintenance.activate_new_event_epoch(db, vault, NOW)
        legacy_project_state = state / 'projects' / PROJECT / 'state'
        _attest_draft(vault, legacy_project_state, page)
        proposal = _write_proposal(vault)
        old_key, old_fp = _seed_target_quarantine(db, vault, state, proposal)
        calls = []

        def invoke(config_path, paths):
            cfg = load_config(config_path)
            frozen = freeze(
                vault, paths, MAIN, attestation_state_dir=Path(cfg.state_dir),
            )
            calls.append((cfg.state_dir, paths, frozen['proposals'][0]))
            return {'status': 'PUBLISHED'}

        with patch.object(maintenance, '_invoke_curate', side_effect=invoke):
            result = maintenance.publication_drain(
                _control(), _base(root, vault), db, state, now=NOW,
                lane='new', recovery={'status': 'NO_LOCK'},
            )

        self.assertEqual(result['batches'][0]['status'], 'PUBLISHED')
        self.assertEqual(len(calls), 1)
        self.assertEqual(Path(calls[0][0]), legacy_project_state)
        self.assertEqual(calls[0][1], [proposal.relative_to(vault).as_posix()])
        self.assertEqual(calls[0][2]['id'], proposal.stem)
        self.assertEqual(calls[0][2]['sha256'], sha(proposal.read_bytes()))
        self.assertFalse((state / 'new-event-epoch' / 'projects' / PROJECT
                          / 'state' / 'curation').exists())
        self.assertEqual(db.execute(
            'SELECT status,fingerprint FROM publications WHERE batch=?', (old_key,),
        ).fetchone(), ('QUARANTINED', old_fp))
        retry_row = db.execute(
            "SELECT status FROM publications WHERE batch LIKE 'repaired-%'",
        ).fetchone()
        self.assertEqual(retry_row[0], 'PUBLISHED')
        self.assertEqual(db.execute(
            'SELECT COUNT(*) FROM repaired_retry_tokens',
        ).fetchone()[0], 1)
        retry_cycle = db.execute(
            "SELECT id FROM audit_cycles WHERE id<>? ORDER BY created_at LIMIT 1",
            ('audit-new-target-provenance-old',),
        ).fetchone()
        self.assertTrue(retry_cycle[0].startswith('audit-new-'))

    def test_fake_draft_does_not_qualify_for_target_provenance_retry(self):
        root, vault = _workspace(self)
        state = root / 'state'
        state.mkdir()
        page = _write_page(vault, draft=True)
        db = _db(vault)
        self.addCleanup(db.close)
        maintenance.activate_new_event_epoch(db, vault, NOW)
        proposal = _write_proposal(vault)
        old_key, old_fp = _seed_target_quarantine(db, vault, state, proposal)
        with self.assertRaisesRegex(PublicationError, 'target is not reviewed'):
            freeze(vault, [proposal.relative_to(vault).as_posix()], MAIN,
                   attestation_state_dir=state / 'projects' / PROJECT / 'state')
        invoked = []

        with patch.object(maintenance, '_invoke_curate', side_effect=lambda *args: invoked.append(args)):
            result = maintenance.publication_drain(
                _control(), _base(root, vault), db, state, now=NOW,
                lane='new', recovery={'status': 'NO_LOCK'},
            )

        self.assertFalse(invoked)
        self.assertEqual(result['model_calls'], 0)
        self.assertEqual(db.execute(
            'SELECT status,fingerprint FROM publications WHERE batch=?', (old_key,),
        ).fetchone(), ('QUARANTINED', old_fp))
        self.assertIsNone(db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='repaired_retry_tokens'",
        ).fetchone())


    def _decision(self, proposal_id, summary='Synthetic corrected report.'):
        return {
            'body': '# Synthetic report\nDated source note (src-one).\n',
            'summary': summary,
            'dispositions': {proposal_id: {
                'disposition': 'synthesized', 'reason': 'Cited synthetic source.',
            }},
        }

    def test_candidate_updates_only_summary_for_attested_draft(self):
        from caretaker.draft_summary import patch_draft_summary_candidate
        from caretaker.publication import build_candidate, fm

        root, vault = _workspace(self)
        state = root / 'state'
        owner_state = state / 'projects' / PROJECT / 'state'
        page = _write_page(vault, draft=True)
        _attest_draft(vault, owner_state, page)
        proposal = _write_proposal(vault)
        paths = [proposal.relative_to(vault).as_posix()]
        frozen = freeze(vault, paths, MAIN, attestation_state_dir=owner_state)
        decision = self._decision(proposal.stem)
        candidate = owner_state / 'curation' / 'summary-patch' / 'candidate'
        manifest = build_candidate(frozen, decision, candidate, NOW.isoformat())
        doc = json.loads(manifest.read_text(encoding='utf-8'))
        output = next(item for item in doc['outputs'] if item['path'] == MAIN)
        payload = candidate / output['payload_path']
        before = payload.read_text(encoding='utf-8')
        old_summary = fm(before, 'summary')
        self.assertNotEqual(old_summary, decision['summary'])

        self.assertTrue(patch_draft_summary_candidate(frozen, decision, candidate, manifest))

        after = payload.read_text(encoding='utf-8')
        self.assertEqual(fm(after, 'summary'), decision['summary'])
        self.assertEqual(fm(after, 'title'), fm(before, 'title'))
        self.assertEqual(fm(after, 'sources'), fm(before, 'sources'))
        self.assertEqual(fm(after, 'last_verified'), fm(before, 'last_verified'))
        self.assertEqual(fm(after, 'status'), fm(before, 'status'))
        self.assertEqual(
            after.replace('summary: ' + json.dumps(decision['summary'], ensure_ascii=False),
                          'summary: ' + json.dumps(old_summary, ensure_ascii=False), 1),
            before,
        )
        updated = json.loads(manifest.read_text(encoding='utf-8'))
        output = next(item for item in updated['outputs'] if item['path'] == MAIN)
        self.assertEqual(output['sha256'], sha(payload.read_bytes()))
        self.assertEqual(updated['summary'], decision['summary'])

    def test_windows_path_and_quotes_round_trip_in_draft_summary_patch(self):
        from caretaker.draft_summary import patch_draft_summary_candidate
        from caretaker.publication import build_candidate, fm

        root, vault = _workspace(self)
        state = root / 'state'
        owner_state = state / 'projects' / PROJECT / 'state'
        page = _write_page(vault, draft=True)
        _attest_draft(vault, owner_state, page)
        proposal = _write_proposal(vault)
        paths = [proposal.relative_to(vault).as_posix()]
        frozen = freeze(vault, paths, MAIN, attestation_state_dir=owner_state)
        expected = r'D:\app "quoted"'
        decision = self._decision(proposal.stem, summary=expected)
        candidate = owner_state / 'curation' / 'summary-windows-path' / 'candidate'
        manifest = build_candidate(frozen, decision, candidate, NOW.isoformat())
        doc = json.loads(manifest.read_text(encoding='utf-8'))
        output = next(item for item in doc['outputs'] if item['path'] == MAIN)
        payload = candidate / output['payload_path']

        self.assertTrue(patch_draft_summary_candidate(frozen, decision, candidate, manifest))
        self.assertEqual(fm(payload.read_text(encoding='utf-8'), 'summary'), expected)
        self.assertEqual(json.loads(manifest.read_text(encoding='utf-8'))['summary'], expected)

    def test_prepared_phase_rerenders_windows_summary_without_new_maker(self):
        from caretaker.config import load_config
        from caretaker.draft_summary import prepared_summary_repair_plan

        expected = r'D:\app "quoted"'

        class WindowsSummaryMaker(_Maker):
            def invoke(self, **kwargs):
                response = super().invoke(**kwargs)
                value = json.loads(response.final)
                value['summary'] = expected
                response.final = json.dumps(value)
                return response

        class RecordingReviewer(_Reviewer):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                self.manifests = []

            def invoke(self, *, prompt, **kwargs):
                match = re.search(r'manifest_sha256":"([a-f0-9]{64})"', prompt)
                if not match:
                    raise AssertionError('review prompt did not bind a manifest digest')
                self.manifests.append(match.group(1))
                return super().invoke(prompt=prompt, **kwargs)

        root, vault = _workspace(self)
        state = root / 'state'
        state.mkdir()
        page = _write_page(vault, draft=True)
        owner_state = state / 'projects' / PROJECT / 'state'
        _attest_draft(vault, owner_state, page)
        proposal = _write_proposal(vault)
        paths = [proposal.relative_to(vault).as_posix()]
        config_path, _runtime = maintenance._project_runtime_cfg(
            _base(root, vault), PROJECT, state, lane='legacy', database=None,
        )
        cfg = load_config(config_path)
        clock = _Clock()
        maker = WindowsSummaryMaker(proposal.stem, clock=clock, advance=350)
        reviewer = RecordingReviewer(clock=clock)
        gateway = SimpleNamespace(
            health=lambda: SimpleNamespace(status='pass'),
            lint=lambda: SimpleNamespace(status='pass'),
        )
        with patch.object(curate_once, 'CanonicalKnowledgeGateway', return_value=gateway), \
             patch.object(curate_once, '_write_run_config', return_value=str(root / 'run-config.json')):
            with tick_budget.TickBudget(seconds=500, clock=clock).activate():
                first = curate_once._run_with_config(cfg, paths, True, maker, reviewer)
        self.assertEqual(first['status'], 'DEFERRED_PREPARED')
        self.assertEqual((maker.calls, reviewer.calls), (1, 0))

        phase_state = owner_state / 'curation'
        pointer = curate_once._phase_pointer(phase_state, PROJECT, paths)
        record = json.loads(pointer.read_text(encoding='utf-8'))
        self.assertEqual(record['phase'], 'PREPARED')
        frozen = freeze(vault, paths, MAIN, attestation_state_dir=owner_state)
        loaded = curate_once._load_phase_progress(cfg, paths, phase_state, frozen)
        plan = prepared_summary_repair_plan(cfg, paths, frozen, loaded)
        self.assertIsNotNone(plan)
        old_manifest_sha = record['manifest_sha256']

        published = []

        def publish(_cfg, manifest, _review, _run_root, result):
            doc = json.loads(Path(manifest).read_text(encoding='utf-8'))
            output = next(item for item in doc['outputs'] if item['path'] == MAIN)
            published.append((Path(manifest).parent / output['payload_path']).read_text(encoding='utf-8'))
            result.update(status='PUBLISHED', publication={'status': 'PUBLISHED'},
                          checkpoint={'status': 'CHECKPOINTED'})

        def run(path, selected, publish_flag, **_kwargs):
            return curate_once._run_with_config(
                load_config(path), selected, publish_flag, maker, reviewer,
            )

        with patch.object(curate_once, 'run', side_effect=run), \
             patch.object(curate_once, 'CanonicalKnowledgeGateway', return_value=gateway), \
             patch.object(curate_once, '_write_run_config', return_value=str(root / 'run-config.json')), \
             patch.object(curate_once, '_publish_under_lock', side_effect=publish):
            resumed = maintenance._invoke_curate(config_path, paths)

        self.assertEqual(resumed['status'], 'PUBLISHED', resumed)
        self.assertEqual((maker.calls, reviewer.calls), (1, 1))
        self.assertEqual(len(reviewer.manifests), 1)
        self.assertNotEqual(reviewer.manifests[0], old_manifest_sha)
        self.assertEqual(fm(published[0], 'summary'), expected)

    def test_repair_prepared_phase_rerenders_windows_summary_without_new_maker(self):
        from caretaker.config import load_config
        from caretaker.draft_summary import prepared_summary_repair_plan

        expected = r'D:\app "quoted"'

        class WindowsSummaryMaker(_Maker):
            def invoke(self, **kwargs):
                response = super().invoke(**kwargs)
                value = json.loads(response.final)
                value['summary'] = expected
                response.final = json.dumps(value)
                return response

        class RecordingReviewer(_Reviewer):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                self.manifests = []

            def invoke(self, *, prompt, **kwargs):
                match = re.search(r'manifest_sha256":"([a-f0-9]{64})"', prompt)
                if not match:
                    raise AssertionError('review prompt did not bind a manifest digest')
                self.manifests.append(match.group(1))
                return super().invoke(prompt=prompt, **kwargs)

        root, vault = _workspace(self)
        state = root / 'state'
        state.mkdir()
        page = _write_page(vault, draft=True)
        owner_state = state / 'projects' / PROJECT / 'state'
        _attest_draft(vault, owner_state, page)
        proposal = _write_proposal(vault)
        paths = [proposal.relative_to(vault).as_posix()]
        config_path, _runtime = maintenance._project_runtime_cfg(
            _base(root, vault), PROJECT, state, lane='legacy', database=None,
        )
        cfg = load_config(config_path)
        clock = _Clock()
        maker = WindowsSummaryMaker(proposal.stem, clock=clock, advance=350)
        reviewer = RecordingReviewer(clock=clock)
        gateway = SimpleNamespace(
            health=lambda: SimpleNamespace(status='pass'),
            lint=lambda: SimpleNamespace(status='pass'),
        )
        with patch.object(curate_once, 'CanonicalKnowledgeGateway', return_value=gateway), \
             patch.object(curate_once, '_write_run_config', return_value=str(root / 'run-config.json')):
            with tick_budget.TickBudget(seconds=500, clock=clock).activate():
                first = curate_once._run_with_config(cfg, paths, True, maker, reviewer)
        self.assertEqual(first['status'], 'DEFERRED_PREPARED')
        self.assertEqual((maker.calls, reviewer.calls), (1, 0))

        phase_state = owner_state / 'curation'
        pointer = curate_once._phase_pointer(phase_state, PROJECT, paths)
        record = json.loads(pointer.read_text(encoding='utf-8'))
        self.assertEqual(record['phase'], 'PREPARED')
        # Model the accepted, pinned REPAIR_PREPARED phase shape while retaining
        # the pre-review candidate whose summary still equals the old page.
        record['phase'] = 'REPAIR_PREPARED'
        record['progress']['phase'] = 'REPAIR_PREPARED'
        record['progress']['assessment'] = {
            'verdict': 'REPAIR',
            'findings': [{'class': 'unsupported_claim', 'weight': 'blocking',
                          'text': 'synthetic bounded repair finding'}],
        }
        record['progress']['repair_attempt'] = 1
        record['progress']['review_calls'] = 1
        record['cache_sha256'] = curate_once._phase_digest(record)
        pointer.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding='utf-8')

        frozen = freeze(vault, paths, MAIN, attestation_state_dir=owner_state)
        loaded = curate_once._load_phase_progress(cfg, paths, phase_state, frozen)
        plan = prepared_summary_repair_plan(cfg, paths, frozen, loaded)
        self.assertIsNotNone(plan)
        old_manifest_sha = record['manifest_sha256']

        published = []

        def publish(_cfg, manifest, _review, _run_root, result):
            doc = json.loads(Path(manifest).read_text(encoding='utf-8'))
            output = next(item for item in doc['outputs'] if item['path'] == MAIN)
            published.append((Path(manifest).parent / output['payload_path']).read_text(encoding='utf-8'))
            result.update(status='PUBLISHED', publication={'status': 'PUBLISHED'},
                          checkpoint={'status': 'CHECKPOINTED'})

        def run(path, selected, publish_flag, **_kwargs):
            return curate_once._run_with_config(
                load_config(path), selected, publish_flag, maker, reviewer,
            )

        with patch.object(curate_once, 'run', side_effect=run), \
             patch.object(curate_once, 'CanonicalKnowledgeGateway', return_value=gateway), \
             patch.object(curate_once, '_write_run_config', return_value=str(root / 'run-config.json')), \
             patch.object(curate_once, '_publish_under_lock', side_effect=publish):
            resumed = maintenance._invoke_curate(config_path, paths)

        self.assertEqual(resumed['status'], 'PUBLISHED', resumed)
        self.assertEqual((maker.calls, reviewer.calls), (1, 1))
        self.assertEqual(len(reviewer.manifests), 1)
        self.assertNotEqual(reviewer.manifests[0], old_manifest_sha)
        self.assertEqual(fm(published[0], 'summary'), expected)

    def test_reviewed_and_unattested_draft_summaries_are_preserved(self):
        from caretaker.draft_summary import (
            corrected_draft_decision, patch_draft_summary_candidate,
        )
        from caretaker.publication import build_candidate, fm

        root, vault = _workspace(self)
        state = root / 'state'
        owner_state = state / 'projects' / PROJECT / 'state'
        page = _write_page(vault, draft=True)
        _attest_draft(vault, owner_state, page)
        proposal = _write_proposal(vault)
        paths = [proposal.relative_to(vault).as_posix()]
        frozen = freeze(vault, paths, MAIN, attestation_state_dir=owner_state)
        decision = self._decision(proposal.stem)

        fake = dict(frozen)
        fake['attestation_state_dir'] = str(root / 'missing-state')
        self.assertIsNone(corrected_draft_decision(fake, decision))
        fake_candidate = owner_state / 'curation' / 'fake' / 'candidate'
        fake_manifest = build_candidate(fake, decision, fake_candidate, NOW.isoformat())
        fake_doc = json.loads(fake_manifest.read_text(encoding='utf-8'))
        fake_output = next(item for item in fake_doc['outputs'] if item['path'] == MAIN)
        fake_payload = fake_candidate / fake_output['payload_path']
        self.assertFalse(patch_draft_summary_candidate(fake, decision, fake_candidate, fake_manifest))
        self.assertEqual(fm(fake_payload.read_text(encoding='utf-8'), 'summary'), 'Producer reports.')

        reviewed = dict(frozen)
        reviewed['before'] = (
            '---\ntitle: Test\ntype: project\nstatus: reviewed\n'
            'summary: Reviewed summary.\nlast_verified: unverified\n'
            'verification_scope: producer-reports\nsources: [src-one]\n'
            'project_slug: test\n---\n\n# Test\n'
        )
        self.assertIsNone(corrected_draft_decision(reviewed, decision))
        reviewed_candidate = owner_state / 'curation' / 'reviewed' / 'candidate'
        reviewed_manifest = build_candidate(
            reviewed, decision, reviewed_candidate, NOW.isoformat(),
        )
        reviewed_doc = json.loads(reviewed_manifest.read_text(encoding='utf-8'))
        reviewed_output = next(item for item in reviewed_doc['outputs'] if item['path'] == MAIN)
        reviewed_payload = reviewed_candidate / reviewed_output['payload_path']
        self.assertFalse(patch_draft_summary_candidate(
            reviewed, decision, reviewed_candidate, reviewed_manifest,
        ))
        self.assertEqual(fm(reviewed_payload.read_text(encoding='utf-8'), 'summary'),
                         'Reviewed summary.')

    def test_normal_invoke_builds_corrected_summary_before_fresh_review(self):
        from caretaker.config import load_config

        root, vault = _workspace(self)
        state = root / 'state'
        state.mkdir()
        page = _write_page(vault, draft=True)
        owner_state = state / 'projects' / PROJECT / 'state'
        _attest_draft(vault, owner_state, page)
        proposal = _write_proposal(vault)
        paths = [proposal.relative_to(vault).as_posix()]
        config_path, _runtime = maintenance._project_runtime_cfg(
            _base(root, vault), PROJECT, state, lane='legacy', database=None,
        )
        maker = _Maker(proposal.stem)
        reviewer = _Reviewer()
        published = []

        def publish(_cfg, manifest, _review, _run_root, result):
            doc = json.loads(Path(manifest).read_text(encoding='utf-8'))
            output = next(item for item in doc['outputs'] if item['path'] == MAIN)
            payload = Path(manifest).parent / output['payload_path']
            published.append(payload.read_text(encoding='utf-8'))
            result.update(status='PUBLISHED', publication={'status': 'PUBLISHED'},
                          checkpoint={'status': 'CHECKPOINTED'})

        def run(path, selected, publish_flag, **_kwargs):
            return curate_once._run_with_config(
                load_config(path), selected, publish_flag, maker, reviewer,
            )

        gateway = SimpleNamespace(
            health=lambda: SimpleNamespace(status='pass'),
            lint=lambda: SimpleNamespace(status='pass'),
        )
        with patch.object(curate_once, 'run', side_effect=run), \
             patch.object(curate_once, 'CanonicalKnowledgeGateway', return_value=gateway), \
             patch.object(curate_once, '_write_run_config', return_value=str(root / 'run-config.json')), \
             patch.object(curate_once, '_publish_under_lock', side_effect=publish):
            result = maintenance._invoke_curate(config_path, paths)

        self.assertEqual(result['status'], 'PUBLISHED')
        self.assertEqual(maker.calls, 1)
        self.assertEqual(reviewer.calls, 1)
        self.assertEqual(fm(published[0], 'summary'), 'Synthetic report.')
        prior_body = page.read_text(encoding='utf-8').split('\n---\n', 1)[1]
        self.assertIn(prior_body, published[0])

    def test_normal_invoke_repairs_only_invalid_summary_and_retains_parsed_decision(self):
        root, vault = _workspace(self)
        state = root / 'state'
        state.mkdir()
        proposal = _write_proposal(vault)
        paths = [proposal.relative_to(vault).as_posix()]
        config_path, _runtime = maintenance._project_runtime_cfg(
            _base(root, vault), PROJECT, state, lane='legacy', database=None,
        )
        maker = _SummaryOnlyMaker(proposal.stem)
        reviewer = _Reviewer()
        published = []

        def publish(_cfg, manifest, _review, _run_root, result):
            doc = json.loads(Path(manifest).read_text(encoding='utf-8'))
            output = next(item for item in doc['outputs'] if item['path'] == MAIN)
            payload = Path(manifest).parent / output['payload_path']
            published.append(payload.read_text(encoding='utf-8'))
            result.update(status='PUBLISHED', publication={'status': 'PUBLISHED'},
                          checkpoint={'status': 'CHECKPOINTED'})

        def run(path, selected, publish_flag, **_kwargs):
            return curate_once._run_with_config(
                load_config(path), selected, publish_flag, maker, reviewer,
            )

        gateway = SimpleNamespace(
            health=lambda: SimpleNamespace(status='pass'),
            lint=lambda: SimpleNamespace(status='pass'),
        )
        with patch.object(curate_once, 'run', side_effect=run), \
             patch.object(curate_once, 'CanonicalKnowledgeGateway', return_value=gateway), \
             patch.object(curate_once, '_write_run_config', return_value=str(root / 'run-config.json')), \
             patch.object(curate_once, '_publish_under_lock', side_effect=publish):
            result = maintenance._invoke_curate(config_path, paths)

        self.assertEqual(result['status'], 'PUBLISHED')
        self.assertEqual(maker.calls, 2)  # synthesis + bounded summary-only repair
        self.assertEqual(reviewer.calls, 1)
        self.assertIn('SUMMARY-ONLY FORMAT REPAIR', maker.prompts[1])
        self.assertIn('src-one', maker.prompts[1])
        self.assertIn('Synthetic report\nDated source note (src-one).', published[0])
        self.assertEqual(fm(published[0], 'summary'), 'Synthetic concise report.')
        retained = list((state / 'projects' / PROJECT / 'state' / 'curation')
                        .rglob('summary-repair-decision.json'))
        self.assertEqual(len(retained), 1)
        decision = json.loads(retained[0].read_text(encoding='utf-8'))['decision']
        self.assertEqual(decision['body'], maker.body)
        self.assertEqual(decision['dispositions'], maker.dispositions)
        self.assertEqual(decision['summary'], {'invalid': 'summary type'})

    def test_fleet_handoff_summary_repair_preserves_original_public_decision(self):
        root, vault = _workspace(self)
        state = root / 'state'
        state.mkdir()
        _write_page(vault)
        proposal = _write_proposal(vault)
        paths = [proposal.relative_to(vault).as_posix()]
        config_path, _runtime = maintenance._project_runtime_cfg(
            _base(root, vault), PROJECT, state, lane='legacy', database=None,
        )
        maker = _SummaryOnlyMaker(proposal.stem, fleet=True)
        reviewer = _Reviewer()
        published = []

        def publish(_cfg, manifest, _review, _run_root, result):
            doc = json.loads(Path(manifest).read_text(encoding='utf-8'))
            output = next(item for item in doc['outputs'] if item['path'] == MAIN)
            published.append((Path(manifest).parent / output['payload_path']).read_text(encoding='utf-8'))
            result.update(status='PUBLISHED', publication={'status': 'PUBLISHED'},
                          checkpoint={'status': 'CHECKPOINTED'})

        def run(path, selected, publish_flag, **_kwargs):
            cfg = load_config(path)
            return curate_once._run_with_config(
                cfg, selected, publish_flag, maker, reviewer,
            )

        gateway = SimpleNamespace(
            health=lambda: SimpleNamespace(status='pass'),
            lint=lambda: SimpleNamespace(status='pass'),
        )
        spec = SimpleNamespace(argv=['--agent', 'kimi', '--model', 'k3'])
        with patch.object(
                 curate_once, '_build_fleet_append_makers',
                 return_value=[{'spec': spec, 'transport': maker, 'lineage': 'kimi'}]), \
             patch.object(curate_once, 'run', side_effect=run), \
             patch.object(curate_once, 'CanonicalKnowledgeGateway', return_value=gateway), \
             patch.object(curate_once, '_write_run_config', return_value=str(root / 'run-config.json')), \
             patch.object(curate_once, '_publish_under_lock', side_effect=publish):
            result = maintenance._invoke_curate(config_path, paths)

        self.assertEqual(result['status'], 'PUBLISHED', result)
        self.assertEqual((maker.calls, reviewer.calls), (2, 1))
        self.assertIn('handoff summary must be exactly one JSON object', maker.prompts[1])
        self.assertIn('Synthetic report\nDated source note (src-one).', published[0])
        retained = list((state / 'projects' / PROJECT / 'state' / 'curation')
                        .rglob('summary-repair-decision.json'))
        self.assertEqual(len(retained), 1)
        decision = json.loads(retained[0].read_text(encoding='utf-8'))['decision']
        self.assertEqual(decision['body'], maker.body)
        self.assertEqual(decision['dispositions'], maker.dispositions)

    def test_summary_only_state_survives_an_invalid_repair_response(self):
        root, vault = _workspace(self)
        state = root / 'state'
        state.mkdir()
        proposal = _write_proposal(vault)
        paths = [proposal.relative_to(vault).as_posix()]
        config_path, _runtime = maintenance._project_runtime_cfg(
            _base(root, vault), PROJECT, state, lane='legacy', database=None,
        )
        maker = _SummaryRetrySequenceMaker(proposal.stem)
        built = []

        def run(path, selected, publish_flag, **_kwargs):
            cfg = load_config(path)
            Path(cfg.state_dir).mkdir(parents=True, exist_ok=True)
            frozen = freeze(
                vault, selected, MAIN, attestation_state_dir=Path(cfg.state_dir),
            )
            original = curate_once._decision_from_maker(
                maker.invoke(prompt='synthetic initial decision'),
            )
            with self.assertRaisesRegex(PublicationError, 'invalid summary'):
                curate_once.build_candidate(
                    frozen, original, Path(cfg.state_dir) / 'initial-candidate',
                    NOW.isoformat(),
                )

            repaired_decision = None
            for attempt in range(2):
                prompt = curate_once.repair_prompt(frozen, 'invalid summary')
                self.assertIn('SUMMARY-ONLY FORMAT REPAIR', prompt)
                self.assertIn('src-one', prompt)
                env = maker.invoke(prompt=prompt)
                if attempt == 0:
                    with self.assertRaisesRegex(PublicationError, 'invalid summary'):
                        curate_once._decision_from_maker(env)
                    continue
                repaired_decision = curate_once._decision_from_maker(env)

            manifest = curate_once.build_candidate(
                frozen, repaired_decision,
                Path(cfg.state_dir) / 'final-candidate', NOW.isoformat(),
            )
            built.append(json.loads(manifest.read_text(encoding='utf-8')))
            self.assertEqual(repaired_decision['body'], maker.body)
            self.assertEqual(repaired_decision['dispositions'], maker.dispositions)
            return {'status': 'PUBLISHED'}

        with patch.object(curate_once, 'run', side_effect=run):
            result = maintenance._invoke_curate(config_path, paths)

        self.assertEqual(result['status'], 'PUBLISHED', result)
        self.assertEqual(maker.calls, 3)
        self.assertEqual(len(maker.prompts), 3)
        self.assertIn('SUMMARY-ONLY FORMAT REPAIR', maker.prompts[1])
        self.assertIn('SUMMARY-ONLY FORMAT REPAIR', maker.prompts[2])
        self.assertEqual(built[0]['source_ids'], ['src-one'])
        self.assertEqual(built[0]['summary'], 'Synthetic concise report.')

    def test_prepared_resume_rerenders_and_reviews_without_new_maker(self):
        from caretaker.config import load_config

        root, vault = _workspace(self)
        state = root / 'state'
        state.mkdir()
        page = _write_page(vault, draft=True)
        owner_state = state / 'projects' / PROJECT / 'state'
        _attest_draft(vault, owner_state, page)
        proposal = _write_proposal(vault)
        paths = [proposal.relative_to(vault).as_posix()]
        config_path, _runtime = maintenance._project_runtime_cfg(
            _base(root, vault), PROJECT, state, lane='legacy', database=None,
        )
        cfg = load_config(config_path)
        clock = _Clock()
        maker = _Maker(proposal.stem, clock=clock, advance=350)
        reviewer = _Reviewer()
        gateway = SimpleNamespace(
            health=lambda: SimpleNamespace(status='pass'),
            lint=lambda: SimpleNamespace(status='pass'),
        )
        with patch.object(curate_once, 'CanonicalKnowledgeGateway', return_value=gateway), \
             patch.object(curate_once, '_write_run_config', return_value=str(root / 'run-config.json')):
            with tick_budget.TickBudget(seconds=500, clock=clock).activate():
                first = curate_once._run_with_config(cfg, paths, True, maker, reviewer)
        self.assertEqual(first['status'], 'DEFERRED_PREPARED')
        self.assertEqual(maker.calls, 1)
        self.assertEqual(reviewer.calls, 0)

        published = []

        def publish(_cfg, manifest, _review, _run_root, result):
            doc = json.loads(Path(manifest).read_text(encoding='utf-8'))
            output = next(item for item in doc['outputs'] if item['path'] == MAIN)
            published.append((Path(manifest).parent / output['payload_path']).read_text(encoding='utf-8'))
            result.update(status='PUBLISHED', publication={'status': 'PUBLISHED'},
                          checkpoint={'status': 'CHECKPOINTED'})

        def run(path, selected, publish_flag, **_kwargs):
            return curate_once._run_with_config(
                load_config(path), selected, publish_flag, maker, reviewer,
            )

        with patch.object(curate_once, 'run', side_effect=run), \
             patch.object(curate_once, 'CanonicalKnowledgeGateway', return_value=gateway), \
             patch.object(curate_once, '_write_run_config', return_value=str(root / 'run-config.json')), \
             patch.object(curate_once, '_publish_under_lock', side_effect=publish):
            second = maintenance._invoke_curate(config_path, paths)

        self.assertEqual(second['status'], 'PUBLISHED')
        self.assertEqual(maker.calls, 1)
        self.assertEqual(reviewer.calls, 1)
        self.assertEqual(fm(published[0], 'summary'), 'Synthetic report.')

    def test_normal_new_drain_repairs_summary_only_terminal_review_without_maker(self):
        from caretaker.config import load_config
        from caretaker.audit import summarize

        class SummaryReviewer:
            def __init__(self):
                self.actions = ['REPAIR', 'REPAIR', 'PASS']
                self.calls = 0
                self.manifests = []

            def invoke(self, *, prompt, **_kwargs):
                self.calls += 1
                action = self.actions.pop(0)
                match = re.search(r'manifest_sha256":"([a-f0-9]{64})"', prompt)
                if not match:
                    raise AssertionError('review prompt did not bind a manifest digest')
                digest = match.group(1)
                self.manifests.append(digest)
                findings = ([{
                    'class': 'inventory_as_project_knowledge', 'weight': 'blocking',
                    'text': 'The page summary still describes a metadata-only directory as project knowledge.',
                }] if action == 'REPAIR' else [])
                return SimpleNamespace(
                    ok=True, status='ok', provider='muse-code', model='muse-spark-1.3',
                    session_id=f'summary-review-{self.calls}',
                    final=json.dumps({
                        'verdict': action, 'manifest_sha256': digest,
                        'findings': findings,
                    }),
                )

        root, vault = _workspace(self)
        state = root / 'state'
        state.mkdir()
        page = _write_page(vault, draft=True)
        legacy_state = state / 'projects' / PROJECT / 'state'
        _attest_draft(vault, legacy_state, page)
        registry_path = vault / 'wiki' / 'registry.md'
        registry_path.parent.mkdir(parents=True, exist_ok=True)
        registry_path.write_text(
            '```json\n' + json.dumps({
                'schema_version': 1,
                'projects': {PROJECT: {'main_page': MAIN}},
                'aliases': {},
            }) + '\n```\n',
            encoding='utf-8',
        )
        database = sqlite3.connect(str(state / 'intake.sqlite'))
        self.addCleanup(database.close)
        ensure_tables(database)
        ensure_pub_tables(database)
        database.execute(
            'CREATE TABLE queue(id TEXT PRIMARY KEY,project TEXT,payload TEXT NOT NULL,'
            'forwarded INTEGER NOT NULL DEFAULT 0)'
        )
        maintenance.activate_new_event_epoch(database, vault, NOW)
        proposal = _write_proposal(vault)
        paths = [proposal.relative_to(vault).as_posix()]
        config_path, _runtime = maintenance._project_runtime_cfg(
            _base(root, vault), PROJECT, state, lane='legacy', database=None,
        )
        cfg = load_config(config_path)
        maker = _Maker(proposal.stem)
        reviewer = SummaryReviewer()
        gateway = SimpleNamespace(
            health=lambda: SimpleNamespace(status='pass'),
            lint=lambda: SimpleNamespace(status='pass'),
        )
        curator_patches = _curator_patches(root)
        for patcher in curator_patches:
            patcher.start()
        try:
            terminal = curate_once._run_with_config(cfg, paths, True, maker, reviewer)
        finally:
            for patcher in reversed(curator_patches):
                patcher.stop()
        self.assertEqual(terminal['status'], 'PENDING')
        self.assertEqual(terminal['error_code'], 'independent_review_not_accepted')
        self.assertEqual((maker.calls, reviewer.calls), (2, 2))

        policy = policy_from(POLICY)
        meta = next(item for item in summarize(
            vault, policy, now=NOW.timestamp(),
        )['eligible'] if item['path'] == paths[0])
        old_cycle = 'audit-new-summary-review-old'
        old_batch = batch_key(PROJECT, paths)
        current_fp = fingerprint(
            PROJECT, paths,
            [sha(proposal.read_bytes()), sha(page.read_bytes())],
            policy, code_version(),
        )
        old_result = dict(terminal)
        old_result.update(
            status='QUARANTINED', reason='independent_review_not_accepted',
            original_reason='independent_review_not_accepted', batch=old_batch,
            cycle_id=old_cycle, project=PROJECT, proposal_ids=[proposal.stem],
            paths=paths, hashes=[sha(proposal.read_bytes())],
        )
        from caretaker.draft_summary import terminal_summary_retry_plan
        frozen_terminal = freeze(vault, paths, MAIN, attestation_state_dir=legacy_state)
        terminal_plan = terminal_summary_retry_plan(
            vault, legacy_state, frozen_terminal, old_result,
            project=PROJECT, paths=paths,
        )
        self.assertIsNotNone(terminal_plan)
        self.assertIsNone(terminal_summary_retry_plan(
            vault, legacy_state, frozen_terminal,
            {**old_result, 'publication': {'status': 'PUBLISHED'}},
            project=PROJECT, paths=paths,
        ))
        original_proposal = proposal.read_bytes()
        try:
            proposal.write_bytes(original_proposal + b'\nsynthetic drift\n')
            drifted = freeze(vault, paths, MAIN, attestation_state_dir=legacy_state)
            self.assertIsNone(terminal_summary_retry_plan(
                vault, legacy_state, drifted, old_result,
                project=PROJECT, paths=paths,
            ))
        finally:
            proposal.write_bytes(original_proposal)

        phase_pointer = next((legacy_state / 'curation').glob('phase-*.json'))
        original_phase = phase_pointer.read_bytes()
        try:
            phase_record = json.loads(original_phase.decode('utf-8'))
            phase_record['progress']['assessment']['findings'] = [{
                'class': 'unsupported_claim', 'weight': 'blocking',
                'text': 'Unrelated synthetic rejection.',
            }]
            phase_record['cache_sha256'] = curate_once._phase_digest(phase_record)
            phase_pointer.write_text(json.dumps(phase_record), encoding='utf-8')
            self.assertIsNone(terminal_summary_retry_plan(
                vault, legacy_state, frozen_terminal, old_result,
                project=PROJECT, paths=paths,
            ))
        finally:
            phase_pointer.write_bytes(original_phase)

        phase_record = json.loads(original_phase.decode('utf-8'))
        candidate = legacy_state / 'curation' / phase_record['candidate']
        tx_dir = candidate / '.curation-transactions'
        tx_dir.mkdir()
        tx_file = tx_dir / 'tx-summary-test.json'
        tx_bytes = b'{"status":"applying"}\n'
        try:
            tx_file.write_bytes(tx_bytes)
            phase_record['pins'][tx_file.relative_to(legacy_state / 'curation').as_posix()] = sha(tx_bytes)
            phase_record['cache_sha256'] = curate_once._phase_digest(phase_record)
            phase_pointer.write_text(json.dumps(phase_record), encoding='utf-8')
            self.assertIsNone(terminal_summary_retry_plan(
                vault, legacy_state, frozen_terminal, old_result,
                project=PROJECT, paths=paths,
            ))
        finally:
            phase_pointer.write_bytes(original_phase)
            tx_file.unlink()
            tx_dir.rmdir()
        database.execute(
            'INSERT INTO audit_cycles(id,trigger,created_at,status,counts,lane) VALUES(?,?,?,?,?,?)',
            (old_cycle, 'threshold', NOW.isoformat(), 'DRAINED', '{}', 'new'),
        )
        database.execute(
            'INSERT INTO audit_members(cycle_id,proposal_id,path,sha256,project,group_key,status,reason) '
            'VALUES(?,?,?,?,?,?,?,?)',
            (old_cycle, proposal.stem, paths[0], sha(proposal.read_bytes()), PROJECT,
             group_key(meta), 'quarantined', 'QUARANTINED'),
        )
        database.execute(
            'INSERT INTO publications(batch,status,failures,result,attempt,next_due,category,fingerprint,updated_at) '
            'VALUES(?,?,?,?,?,?,?,?,?)',
            (old_batch, 'QUARANTINED', 2,
             json.dumps(old_result, ensure_ascii=False), 2, '', 'deterministic',
             current_fp, NOW.isoformat()),
        )
        database.commit()

        published = []

        def publish(_cfg, manifest, _review, _run_root, result):
            doc = json.loads(Path(manifest).read_text(encoding='utf-8'))
            output = next(item for item in doc['outputs'] if item['path'] == MAIN)
            published.append((Path(manifest).parent / output['payload_path']).read_text(encoding='utf-8'))
            result.update(status='PUBLISHED', publication={'status': 'PUBLISHED'},
                          checkpoint={'status': 'CHECKPOINTED'})

        def run(path, selected, publish_flag, **_kwargs):
            return curate_once._run_with_config(
                load_config(path), selected, publish_flag, maker, reviewer,
            )

        runtime_patches = [
            patch.object(maintenance, '_new_epoch_proposal_filter',
                         return_value=lambda _meta: True),
            patch.object(curate_once, 'run', side_effect=run),
            patch.object(curate_once, 'CanonicalKnowledgeGateway', return_value=gateway),
            patch.object(curate_once, '_write_run_config', return_value=str(root / 'run-config.json')),
            patch.object(curate_once, '_publish_under_lock', side_effect=publish),
        ]
        for patcher in runtime_patches:
            patcher.start()
        try:
            result = maintenance.publication_drain(
                _control(), _base(root, vault), database, state, now=NOW,
                lane='new', recovery={'status': 'NO_LOCK'},
            )
        finally:
            for patcher in reversed(runtime_patches):
                patcher.stop()

        self.assertEqual(result['batches'][0]['status'], 'PUBLISHED', result)
        self.assertEqual((maker.calls, reviewer.calls), (2, 3))
        self.assertEqual(fm(published[-1], 'summary'), 'Synthetic report.')
        self.assertEqual(database.execute(
            'SELECT status FROM publications WHERE batch=?', (old_batch,),
        ).fetchone()[0], 'QUARANTINED')
        retry = database.execute(
            "SELECT batch,status,result FROM publications WHERE batch LIKE 'repaired-%'",
        ).fetchone()
        self.assertIsNotNone(retry)
        self.assertEqual(retry[1], 'PUBLISHED')
        self.assertEqual(database.execute(
            'SELECT COUNT(*) FROM repaired_retry_tokens',
        ).fetchone()[0], 1)
        latest = json.loads(next((legacy_state / 'curation').glob('phase-*.json')).read_text(
            encoding='utf-8'))
        self.assertNotEqual(latest['manifest_sha256'], terminal['manifest_sha256'])
        self.assertEqual(reviewer.manifests[-1], latest['manifest_sha256'])
        self.assertNotEqual(reviewer.manifests[-1], terminal['manifest_sha256'])
        archive = legacy_state / 'curation' / latest['run_root'] / (
            'summary-repair-archive-' + terminal['manifest_sha256'][:16]
        )
        self.assertTrue((archive / 'phase.json').is_file())
        self.assertTrue((archive / 'result.json').is_file())
        with patch.object(maintenance, '_new_epoch_proposal_filter',
                          return_value=lambda _meta: True):
            second = maintenance.publication_drain(
                _control(), _base(root, vault), database, state,
                now=NOW + dt.timedelta(minutes=1), lane='new',
                recovery={'status': 'NO_LOCK'},
            )
        self.assertEqual(second['model_calls'], 0)
        self.assertEqual(database.execute(
            'SELECT COUNT(*) FROM repaired_retry_tokens',
        ).fetchone()[0], 1)

    def test_legacy_drain_retries_old_summary_review_with_fresh_review_and_no_maker(self):
        from caretaker.draft_summary import helper_sha256
        from caretaker.pubstate import ensure_pub_tables
        from caretaker.repaired_retry import (
            SUMMARY_REPAIR_REASON, ensure_retry_table, retry_batch_key, retry_token,
        )

        class SummaryReviewer:
            def __init__(self):
                self.actions = ['REPAIR', 'REPAIR', 'PASS']
                self.calls = 0
                self.manifests = []

            def invoke(self, *, prompt, **_kwargs):
                self.calls += 1
                action = self.actions.pop(0)
                match = re.search(r'manifest_sha256":"([a-f0-9]{64})"', prompt)
                if not match:
                    raise AssertionError('review prompt did not bind a manifest digest')
                digest = match.group(1)
                self.manifests.append(digest)
                findings = ([{
                    'class': 'inventory_as_project_knowledge', 'weight': 'blocking',
                    'text': 'The project summary still describes an upload directory as project knowledge.',
                }] if action == 'REPAIR' else [])
                return SimpleNamespace(
                    ok=True, status='ok', provider='muse-code', model='muse-spark-1.3',
                    session_id=f'legacy-summary-review-{self.calls}',
                    final=json.dumps({
                        'verdict': action, 'manifest_sha256': digest,
                        'findings': findings,
                    }),
                )

        root, vault = _workspace(self)
        state = root / 'state'
        state.mkdir()
        page = _write_page(vault, draft=True)
        legacy_state = state / 'projects' / PROJECT / 'state'
        _attest_draft(vault, legacy_state, page)
        registry_path = vault / 'wiki' / 'registry.md'
        registry_path.parent.mkdir(parents=True, exist_ok=True)
        registry_path.write_text(
            '```json\n' + json.dumps({
                'schema_version': 1,
                'projects': {PROJECT: {'main_page': MAIN}},
                'aliases': {},
            }) + '\n```\n', encoding='utf-8',
        )
        database = sqlite3.connect(str(state / 'intake.sqlite'))
        self.addCleanup(database.close)
        ensure_tables(database)
        ensure_pub_tables(database)
        database.execute(
            'CREATE TABLE queue(id TEXT PRIMARY KEY,project TEXT,payload TEXT NOT NULL,'
            'forwarded INTEGER NOT NULL DEFAULT 0)'
        )
        maintenance.activate_new_event_epoch(database, vault, NOW)
        proposal = _write_proposal(vault)
        paths = [proposal.relative_to(vault).as_posix()]
        config_path, _runtime = maintenance._project_runtime_cfg(
            _base(root, vault), PROJECT, state, lane='legacy', database=None,
        )
        cfg = load_config(config_path)
        maker = _Maker(proposal.stem)
        reviewer = SummaryReviewer()
        curator_patches = _curator_patches(root)
        for patcher in curator_patches:
            patcher.start()
        try:
            terminal = curate_once._run_with_config(cfg, paths, True, maker, reviewer)
        finally:
            for patcher in reversed(curator_patches):
                patcher.stop()
        self.assertEqual(terminal['status'], 'PENDING')
        self.assertEqual(terminal['error_code'], 'independent_review_not_accepted')
        self.assertEqual((maker.calls, reviewer.calls), (2, 2))

        policy = policy_from(POLICY)
        meta = next(item for item in summarize(
            vault, policy, now=NOW.timestamp(),
        )['eligible'] if item['path'] == paths[0])
        old_cycle = 'audit-26legacy-summary-review-old'
        old_batch = batch_key(PROJECT, paths)
        current_fp = fingerprint(
            PROJECT, paths,
            [sha(proposal.read_bytes()), sha(page.read_bytes())],
            policy, code_version(),
        )
        old_result = dict(terminal)
        old_result.update(
            status='QUARANTINED', reason='independent_review_not_accepted',
            original_reason='independent_review_not_accepted', batch=old_batch,
            cycle_id=old_cycle, project=PROJECT, proposal_ids=[proposal.stem],
            paths=paths, hashes=[sha(proposal.read_bytes())],
        )
        database.execute(
            'INSERT INTO audit_cycles(id,trigger,created_at,status,counts,lane) VALUES(?,?,?,?,?,?)',
            (old_cycle, 'threshold', NOW.isoformat(), 'DRAINED', '{}', 'legacy'),
        )
        database.execute(
            'INSERT INTO audit_members(cycle_id,proposal_id,path,sha256,project,group_key,status,reason) '
            'VALUES(?,?,?,?,?,?,?,?)',
            (old_cycle, proposal.stem, paths[0], sha(proposal.read_bytes()), PROJECT,
             group_key(meta), 'quarantined', 'QUARANTINED'),
        )
        database.execute(
            'INSERT INTO publications(batch,status,failures,result,attempt,next_due,category,fingerprint,updated_at) '
            'VALUES(?,?,?,?,?,?,?,?,?)',
            (old_batch, 'QUARANTINED', 2,
             json.dumps(old_result, ensure_ascii=False), 2, '', 'deterministic',
             current_fp, NOW.isoformat()),
        )

        # Simulate the exact old helper token being consumed in 0.1.64. The
        # changed lane validator in 0.1.65 must mint its distinct helper-bound token.
        previous_helper = sha((
            ROOT.parent / 'candidate-draft-summary64' / 'caretaker' / 'draft_summary.py'
        ).read_bytes())
        current_helper = helper_sha256()
        self.assertNotEqual(previous_helper, current_helper)
        ensure_retry_table(database)
        stale_cycle = 'audit-0aeee52a4d2dc7e640b7'
        previous_retry = {
            'proposal_id': proposal.stem, 'path': paths[0],
            'sha256': sha(proposal.read_bytes()), 'project': PROJECT,
            'reason': SUMMARY_REPAIR_REASON,
            'publication_sha256': REPAIRED_PUBLICATION_SHA256,
            'code_version': code_version(), 'previous_batch': old_batch,
            'helper_sha256': previous_helper,
        }
        previous_token = retry_token(
            REPAIRED_PUBLICATION_SHA256, PROJECT, proposal.stem, paths[0],
            sha(proposal.read_bytes()), reason=SUMMARY_REPAIR_REASON,
            helper_sha256=previous_helper,
        )
        previous_retry['token'] = previous_token
        database.execute(
            'INSERT INTO audit_cycles(id,trigger,created_at,status,counts,lane) VALUES(?,?,?,?,?,?)',
            (stale_cycle, 'scoped', NOW.isoformat(), 'ACTIVE',
             json.dumps({'repaired_retry': [previous_retry]}), 'legacy'),
        )
        database.execute(
            'INSERT INTO audit_members(cycle_id,proposal_id,path,sha256,project,group_key,status,reason) '
            'VALUES(?,?,?,?,?,?,?,?)',
            (stale_cycle, proposal.stem, paths[0], sha(proposal.read_bytes()),
             PROJECT, group_key(meta), 'pending', ''),
        )
        database.execute(
            'INSERT INTO repaired_retry_tokens(token,cycle_id,publication_sha256,code_version,'
            'proposal_id,path,sha256,previous_batch) VALUES(?,?,?,?,?,?,?,?)',
            (previous_token, stale_cycle, REPAIRED_PUBLICATION_SHA256,
             code_version(), proposal.stem, paths[0], sha(proposal.read_bytes()), old_batch),
        )
        database.commit()
        old_row = database.execute(
            'SELECT status,failures,result,attempt,fingerprint FROM publications WHERE batch=?',
            (old_batch,),
        ).fetchone()

        published = []

        def publish(_cfg, manifest, _review, _run_root, result):
            doc = json.loads(Path(manifest).read_text(encoding='utf-8'))
            output = next(item for item in doc['outputs'] if item['path'] == MAIN)
            published.append((Path(manifest).parent / output['payload_path']).read_text(encoding='utf-8'))
            result.update(status='PUBLISHED', publication={'status': 'PUBLISHED'},
                          checkpoint={'status': 'CHECKPOINTED'})

        def run(path, selected, publish_flag, **_kwargs):
            return curate_once._run_with_config(
                load_config(path), selected, publish_flag, maker, reviewer,
            )

        control = _control()
        control['publication']['include_existing_hub_backlog'] = True
        runtime_patches = [
            patch.object(curate_once, 'run', side_effect=run),
            patch.object(curate_once, 'CanonicalKnowledgeGateway',
                         return_value=SimpleNamespace(
                             health=lambda: SimpleNamespace(status='pass'),
                             lint=lambda: SimpleNamespace(status='pass'),
                         )),
            patch.object(curate_once, '_write_run_config', return_value=str(root / 'run-config.json')),
            patch.object(curate_once, '_publish_under_lock', side_effect=publish),
        ]
        for patcher in runtime_patches:
            patcher.start()
        try:
            result = maintenance.publication_drain(
                control, _base(root, vault), database, state, now=NOW,
                lane='legacy', recovery={'status': 'NO_LOCK'},
            )
        finally:
            for patcher in reversed(runtime_patches):
                patcher.stop()

        self.assertEqual(result['batches'][0]['status'], 'PUBLISHED', result)
        self.assertEqual((maker.calls, reviewer.calls), (2, 3))
        self.assertEqual(fm(published[0], 'summary'), 'Synthetic report.')
        self.assertEqual(database.execute(
            'SELECT status,failures,result,attempt,fingerprint FROM publications WHERE batch=?',
            (old_batch,),
        ).fetchone(), old_row)
        current_token = retry_token(
            REPAIRED_PUBLICATION_SHA256, PROJECT, proposal.stem, paths[0],
            sha(proposal.read_bytes()), reason=SUMMARY_REPAIR_REASON,
            helper_sha256=current_helper,
        )
        self.assertIsNotNone(database.execute(
            'SELECT 1 FROM repaired_retry_tokens WHERE token=?', (current_token,),
        ).fetchone())
        self.assertEqual(database.execute(
            'SELECT status FROM audit_cycles WHERE id=?', (stale_cycle,),
        ).fetchone()[0], 'DRAINED')
        self.assertEqual(database.execute(
            'SELECT status,reason FROM audit_members WHERE cycle_id=?', (stale_cycle,),
        ).fetchone(), ('failed', 'stale_unstarted_summary_retry_superseded'))
        self.assertIsNotNone(database.execute(
            'SELECT 1 FROM repaired_retry_tokens WHERE token=?', (previous_token,),
        ).fetchone())
        self.assertEqual(database.execute(
            "SELECT COUNT(*) FROM publications WHERE batch LIKE 'repaired-%' AND status='PUBLISHED'",
        ).fetchone()[0], 1)

    def test_owner_fallback_requires_empty_new_state_and_real_legacy_state(self):
        root, vault = _workspace(self)
        state = root / 'state'
        state.mkdir()
        db = sqlite3.connect(':memory:')
        self.addCleanup(db.close)
        base = _base(root, vault)
        proposal_path = 'proposals/agent/prop-owner-test.md'
        new_project_state = state / 'new-event-epoch' / 'projects' / PROJECT / 'state'
        legacy_project_state = state / 'projects' / PROJECT / 'state'

        _config_path, runtime = _runtime_cfg(root, vault, state, db)
        self.assertEqual(Path(runtime['state_dir']), new_project_state)

        empty_new_curation = new_project_state / 'curation'
        empty_new_curation.mkdir(parents=True)
        legacy_phase = _phase_pointer(legacy_project_state, proposal_path, 'REPAIR_PREPARED')
        _config_path, runtime = _runtime_cfg(root, vault, state, db)
        self.assertEqual(Path(runtime['state_dir']), legacy_project_state)
        legacy_bytes = legacy_phase.read_bytes()
        self.assertEqual(legacy_phase.read_bytes(), legacy_bytes)

        new_phase = _phase_pointer(new_project_state, proposal_path, 'PREPARED')
        new_bytes = new_phase.read_bytes()
        _config_path, runtime = _runtime_cfg(root, vault, state, db)
        self.assertEqual(Path(runtime['state_dir']), new_project_state)
        self.assertEqual(new_phase.read_bytes(), new_bytes)
        self.assertEqual(legacy_phase.read_bytes(), legacy_bytes)

    def test_new_reviewed_and_unknown_state_remain_the_owner(self):
        root, vault = _workspace(self)
        state = root / 'state'
        state.mkdir()
        db = sqlite3.connect(':memory:')
        self.addCleanup(db.close)
        ensure_pub_tables(db)
        new_project_state = state / 'new-event-epoch' / 'projects' / PROJECT / 'state'
        legacy_project_state = state / 'projects' / PROJECT / 'state'
        legacy_phase = _phase_pointer(legacy_project_state, 'proposals/agent/one.md',
                                      'REPAIR_PREPARED')

        reviewed = new_project_state / 'curation' / 'reviewed-review-cache.json'
        reviewed.parent.mkdir(parents=True, exist_ok=True)
        reviewed.write_text('{"status":"REVIEWED_CANDIDATE"}', encoding='utf-8')
        reviewed_bytes = reviewed.read_bytes()
        _config_path, runtime = _runtime_cfg(root, vault, state, db)
        self.assertEqual(Path(runtime['state_dir']), new_project_state)
        self.assertEqual(reviewed.read_bytes(), reviewed_bytes)

        reviewed.unlink()
        (new_project_state / 'curation').rmdir()
        unknown_previous = {
            'project': PROJECT, 'cycle_id': 'audit-new-unknown',
            'run_path': str(new_project_state / 'curation' / 'missing-run'),
        }
        db.execute(
            "INSERT INTO publications(batch,status,failures,result,attempt,next_due,category,fingerprint,updated_at) "
            "VALUES(?,?,?,?,?,?,?,?,?)",
            ('unknown-batch', 'UNKNOWN_REQUIRES_RECONCILIATION', 1,
             json.dumps({'status': 'UNKNOWN_REQUIRES_RECONCILIATION',
                         'previous': json.dumps(unknown_previous)}),
             1, '', 'uncertain', 'a' * 64, NOW.isoformat()),
        )
        db.commit()
        legacy_bytes = legacy_phase.read_bytes()
        _config_path, runtime = _runtime_cfg(root, vault, state, db)
        self.assertEqual(Path(runtime['state_dir']), new_project_state)
        self.assertEqual(legacy_phase.read_bytes(), legacy_bytes)

    def test_cycle_id_does_not_override_explicit_legacy_owner_and_unknown_is_fail_closed(self):
        root, vault = _workspace(self)
        state = root / 'state'
        state.mkdir()
        db = sqlite3.connect(':memory:')
        self.addCleanup(db.close)
        ensure_pub_tables(db)
        new_project_state = state / 'new-event-epoch' / 'projects' / PROJECT / 'state'
        legacy_project_state = state / 'projects' / PROJECT / 'state'
        _phase_pointer(legacy_project_state, 'proposals/agent/legacy-owner.md',
                       'REPAIR_PREPARED')

        legacy_previous = {
            'project': PROJECT, 'cycle_id': 'audit-new-current-cycle',
            'run_path': str(legacy_project_state / 'curation' / 'legacy-run'),
        }
        db.execute(
            "INSERT INTO publications(batch,status,failures,result,attempt,next_due,category,fingerprint,updated_at) "
            "VALUES(?,?,?,?,?,?,?,?,?)",
            ('legacy-unknown', 'UNKNOWN_REQUIRES_RECONCILIATION', 1,
             json.dumps({'status': 'UNKNOWN_REQUIRES_RECONCILIATION',
                         'project': PROJECT, 'cycle_id': 'audit-new-current-cycle',
                         'previous': json.dumps(legacy_previous)}),
             1, '', 'uncertain', 'b' * 64, NOW.isoformat()),
        )
        db.commit()
        _config_path, runtime = _runtime_cfg(root, vault, state, db)
        self.assertEqual(Path(runtime['state_dir']), legacy_project_state)

        for owner in (None, 17, str(root / 'foreign' / 'curation' / 'run')):
            with self.subTest(owner=owner):
                db.execute('DELETE FROM publications')
                unknown = {
                    'status': 'UNKNOWN_REQUIRES_RECONCILIATION',
                    'project': PROJECT, 'cycle_id': 'audit-new-current-cycle',
                }
                if owner is not None:
                    unknown['run_path'] = owner
                db.execute(
                    "INSERT INTO publications(batch,status,failures,result,attempt,next_due,category,fingerprint,updated_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?)",
                    ('ambiguous-unknown', 'UNKNOWN_REQUIRES_RECONCILIATION', 1,
                     json.dumps(unknown), 1, '', 'uncertain', 'c' * 64, NOW.isoformat()),
                )
                db.commit()
                _config_path, runtime = _runtime_cfg(root, vault, state, db)
                self.assertEqual(Path(runtime['state_dir']), new_project_state)

    def test_new_drain_deferral_resumes_fresh_review_in_legacy_state(self):
        root, vault = _workspace(self)
        state = root / 'state'
        state.mkdir()
        page = _write_page(vault, draft=True)
        db = _db(vault)
        self.addCleanup(db.close)
        maintenance.activate_new_event_epoch(db, vault, NOW)
        legacy_project_state = state / 'projects' / PROJECT / 'state'
        _attest_draft(vault, legacy_project_state, page)
        proposal = _write_proposal(vault, 'prop-20261024T120100000Z-deferred')
        control = _control()
        base = _base(root, vault)
        clock = _Clock()
        maker = _Maker(proposal.stem, clock=clock, advance=350)
        reviewer = _Reviewer(clock=clock)
        seen_state = []

        def run(config_path, paths, publish, maintenance_lock_held=False):
            cfg = load_config(config_path)
            seen_state.append(Path(cfg.state_dir))
            if len(seen_state) == 1:
                with tick_budget.TickBudget(seconds=500, clock=clock).activate():
                    return curate_once._run_with_config(cfg, paths, publish, maker, reviewer)
            return curate_once._run_with_config(cfg, paths, publish, maker, reviewer)

        patches = _curator_patches(root)
        for patcher in patches:
            patcher.start()
        run_patch = patch.object(curate_once, 'run', side_effect=run)
        filter_patch = patch.object(
            maintenance, '_new_epoch_proposal_filter', return_value=lambda _meta: True,
        )
        run_patch.start()
        filter_patch.start()
        try:
            first = maintenance.publication_drain(
                control, base, db, state, now=NOW, lane='new',
                recovery={'status': 'NO_LOCK'},
            )
            prepared = db.execute(
                "SELECT status,result FROM publications WHERE status='PREPARED_DEFERRED'",
            ).fetchone()
            self.assertIsNotNone(prepared)
            self.assertEqual(prepared[0], 'PREPARED_DEFERRED')
            stored = json.loads(prepared[1])
            self.assertTrue(stored['cycle_id'].startswith('audit-new-'))
            self.assertEqual(Path(stored['run_path']).parent, legacy_project_state / 'curation')
            self.assertEqual(first['batches'][0]['status'], 'DEFERRED_PREPARED')
            self.assertEqual((maker.calls, reviewer.calls), (1, 0))

            second = maintenance.publication_drain(
                control, base, db, state, now=NOW + dt.timedelta(minutes=1), lane='new',
                recovery={'status': 'NO_LOCK'},
            )
        finally:
            filter_patch.stop()
            run_patch.stop()
            for patcher in reversed(patches):
                patcher.stop()

        self.assertEqual(second['batches'][0]['status'], 'PUBLISHED')
        self.assertEqual(seen_state, [legacy_project_state, legacy_project_state])
        self.assertEqual((maker.calls, reviewer.calls), (1, 1))
        self.assertFalse((state / 'new-event-epoch' / 'projects' / PROJECT
                          / 'state' / 'curation').exists())

    def test_legacy_repair_prepared_resumes_review_without_repeating_maker(self):
        root, vault = _workspace(self)
        state = root / 'state'
        state.mkdir()
        page = _write_page(vault)
        db = _db(vault)
        self.addCleanup(db.close)
        maintenance.activate_new_event_epoch(db, vault, NOW)
        proposal = _write_proposal(vault, 'prop-20261024T120100000Z-repair')
        base = _base(root, vault)
        control = _control()
        runtime_base = maintenance._runtime_base_with_control_publication(base, control)
        config_path, _runtime = maintenance._project_runtime_cfg(
            runtime_base, PROJECT, state,
        )
        cfg = load_config(config_path)
        maker = _Maker(proposal.stem, clock=_Clock(), advance=150)
        clock = maker.clock
        reviewer = _Reviewer(['REPAIR', 'timeout', 'PASS'], clock=clock, advance=400)
        patches = _curator_patches(root)
        for patcher in patches:
            patcher.start()
        try:
            with tick_budget.TickBudget(seconds=700, clock=clock).activate():
                first = curate_once._run_with_config(
                    cfg, [proposal.relative_to(vault).as_posix()], True, maker, reviewer,
                )
            second = curate_once._run_with_config(
                cfg, [proposal.relative_to(vault).as_posix()], True, maker, reviewer,
            )
        finally:
            for patcher in reversed(patches):
                patcher.stop()
        self.assertEqual(first['phase'], 'REPAIR_REQUIRED')
        self.assertEqual(second['phase'], 'REPAIR_PREPARED')
        self.assertEqual((maker.calls, reviewer.calls), (2, 2))

        from caretaker.pubstate import batch_key
        phase = (state / 'projects' / PROJECT / 'state' / 'curation'
                 / ('phase-' + batch_key(PROJECT, [proposal.relative_to(vault).as_posix()]) + '.json'))
        phase_bytes = phase.read_bytes()
        seen_state = []

        def run(config_path, paths, publish, maintenance_lock_held=False):
            loaded = load_config(config_path)
            seen_state.append(Path(loaded.state_dir))
            return curate_once._run_with_config(loaded, paths, publish, maker, reviewer)

        patches = _curator_patches(root)
        for patcher in patches:
            patcher.start()
        run_patch = patch.object(curate_once, 'run', side_effect=run)
        run_patch.start()
        try:
            result = maintenance.publication_drain(
                control, base, db, state, now=NOW + dt.timedelta(minutes=2),
                lane='new', recovery={'status': 'NO_LOCK'},
            )
        finally:
            run_patch.stop()
            for patcher in reversed(patches):
                patcher.stop()

        self.assertEqual(result['batches'][0]['status'], 'PUBLISHED')
        self.assertEqual(seen_state, [state / 'projects' / PROJECT / 'state'])
        self.assertEqual((maker.calls, reviewer.calls), (2, 3))
        self.assertTrue(phase.is_file())
        self.assertNotEqual(phase.read_bytes(), phase_bytes)

