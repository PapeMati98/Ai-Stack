"""Public-path regressions; all provider evidence is synthetic and local."""
import argparse
import copy
import json
import unittest
from pathlib import Path

import test_plan_contract as fixtures

plan = fixtures.plan_contract


class FullCombinedPolicyTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.PlanContractTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.root = self.fixture.root
        self.contract = copy.deepcopy(self.fixture.draft)
        self.contract['planning']['review_policy'] = 'full-combined-v1'
        self.sequence = 0

    def path(self, name):
        self.sequence += 1
        return self.root / f'{self.sequence}-{name}'

    def write(self, path, value):
        fixtures.write_json(path, value)
        return path

    def bind_review(self):
        c = self.contract
        ph = plan.planning_payload_hash(c)
        receipt_path = self.path('reviewer.json')
        auditor = {'identity': 'qwen', 'model': 'qwen3.8-max', 'lineage': 'alibaba',
                   'session_id': f'reviewer-{self.sequence}', 'independent_from': ['planner-main']}
        receipt = {
            'schema_version': 5, 'agent': 'qwen', 'adapter': 'qwen',
            'model': auditor['model'], 'run_id': auditor['session_id'],
            'run_status': 'COMPLETED', 'exit_code': 0, 'timed_out': False,
            'runtime_attestation': {'status': 'VERIFIED'},
            'model_identity': {'status': 'verified', 'expected_model': auditor['model'],
                               'observed_models': ['qwen3.8-max-0902']},
            'handoff_valid': True, 'handoff_status': 'completed',
            'handoff': {'status': 'completed', 'summary': '\n'.join([
                'FIDELITY PASS', 'SUFFICIENCY PASS',
                f'FULL_COMBINED_REVIEW_PASS {c["prompt"]["sha256"]} {ph}']),
                'files_changed': [], 'verification': ['All clauses, requirements and tasks reviewed.'],
                'risks': [], 'next_steps': []},
        }
        self.write(receipt_path, receipt)
        for name, gate in c['gates'].items():
            gate.update(auditor=copy.deepcopy(auditor), reviewed_plan_sha256=ph,
                        reviewed_prompt_sha256=c['prompt']['sha256'],
                        reviewed_clause_ids=[i['clause_id'] for i in c['clauses']],
                        reviewed_requirement_ids=[i['requirement_id'] for i in c['requirements']],
                        reviewed_task_ids=[] if name == 'fidelity' else [i['task_id'] for i in c['tasks']],
                        attested_hard_risk_flags=c['classification']['hard_risk_flags'] if name == 'fidelity' else [],
                        evidence={'path': receipt_path.name, 'sha256': plan.file_hash(receipt_path)})
        self.sync_history()
        self.receipt_path, self.receipt = receipt_path, receipt
        return receipt

    def sync_history(self):
        c = self.contract
        history = [r for r in c['planning']['review_history'] if r['revision'] < c['revision']]
        c['planning']['review_history'] = history + [
            {'revision': c['revision'], 'gate': name, **copy.deepcopy(gate)}
            for name, gate in c['gates'].items()]

    def planner(self, kind='codex-native'):
        marker = f'PLAN_REVIEWED {self.contract["prompt"]["sha256"]} {plan.planning_payload_hash(self.contract)}'
        session = self.contract['planning']['planner_session_id']
        if kind == 'codex-native':
            rows = [{'type': 'session_meta', 'payload': {'id': session}},
                    {'type': 'turn_context', 'payload': {'model': 'gpt-6-astra'}},
                    {'type': 'response_item', 'payload': {'type': 'message', 'role': 'assistant',
                     'content': [{'type': 'output_text', 'text': marker}]}}]
        elif kind == 'claude-native':
            rows = [{'type': 'assistant', 'sessionId': session, 'message': {
                'role': 'assistant', 'model': 'claude-fable-5.1',
                'content': [{'type': 'text', 'text': marker}]}}]
        else:
            rows = [{'model': 'gpt-6-astra', 'run_id': session, 'run_status': 'COMPLETED',
                     'adapter': 'codex', 'execution_profile_id': 'codex-gpt-6-astra',
                     'provider_attempts': [{'command': ['codex.cmd', 'exec', '--model', 'gpt-6-astra']}],
                     'runtime_attestation': {'status': 'VERIFIED'}, 'handoff': {'summary': marker}}]
        source = self.path('planner.jsonl')
        source.write_text('\n'.join(map(json.dumps, rows)), encoding='utf-8')
        return self.write(self.path('witness.json'), {'kind': kind, 'source': str(source),
                                                     'source_sha256': plan.file_hash(source)})

    def approve(self, kind='codex-native', base=None):
        args = argparse.Namespace(contract=str(self.write(self.path('draft.json'), self.contract)),
            output=str(self.path('approved.json')), planner_evidence=str(self.planner(kind)),
            approved_by='parent-architect', base_contract=str(base) if base else None)
        plan.command_approve(args)
        return plan.load_json(Path(args.output), 'approved')

    def ingest(self, review):
        pending = copy.deepcopy(self.contract)
        for gate in pending['gates'].values():
            gate.update(status='pending', auditor=None, reviewed_prompt_sha256=None,
                        reviewed_plan_sha256=None, reviewed_clause_ids=[], reviewed_requirement_ids=[],
                        reviewed_task_ids=[], attested_hard_risk_flags=[], evidence=None)
        pending['planning']['review_history'] = [r for r in pending['planning']['review_history']
                                               if r['revision'] < pending['revision']]
        args = argparse.Namespace(contract=str(self.write(self.path('pending.json'), pending)),
            review=str(self.write(self.path('review.json'), review)), output=str(self.path('recorded.json')))
        plan.command_record_review(args)
        return plan.load_json(Path(args.output), 'recorded')

    def test_native_approve_validate(self):
        for kind in ('codex-native', 'claude-native'):
            with self.subTest(kind=kind):
                self.contract['planning']['planner_lineage'] = 'openai' if kind == 'codex-native' else 'anthropic'
                self.bind_review()
                self.contract = self.ingest({'gate': 'combined', 'result': self.contract['gates']})
                approved = self.approve(kind)
                plan.validate_approved_contract(approved)
                source = approved['approval']['planner_witness']['source']
                Path(source).write_text('{}', encoding='utf-8')
                with self.assertRaises(plan.ContractError):
                    plan.validate_approved_contract(approved)

    def test_receipt_refusals_at_direct_approve_and_validation(self):
        mutations = [lambda r: r.update(run_status='PARTIAL'),
                     lambda r: r.update(model_identity={'status': 'unverified'}),
                     lambda r: r['model_identity'].update(observed_models=['gpt-6-astra']),
                     lambda r: r.update(agent='forged-agent'),
                     lambda r: r.update(run_id='wrong-session'),
                     lambda r: r['handoff'].update(status='partial'),
                     lambda r: r['handoff'].update(summary=r['handoff']['summary'].replace('SUFFICIENCY PASS', '')),
                     lambda r: r['handoff'].update(summary=r['handoff']['summary'].replace('FULL_COMBINED_REVIEW_PASS', 'forged')),
                     lambda r: r.update(runtime_attestation={'status': 'UNKNOWN'})]
        for index, mutate in enumerate(mutations):
            with self.subTest(case=index):
                self.bind_review()
                approved = self.approve('fleet')
                mutate(self.receipt)
                self.write(self.receipt_path, self.receipt)
                # Even a caller that rehashes both the gate and approved JSON cannot bypass provenance.
                for c in (self.contract, approved):
                    for gate in c['gates'].values():
                        gate['evidence']['sha256'] = plan.file_hash(self.receipt_path)
                    for row in c['planning']['review_history']:
                        row['evidence']['sha256'] = plan.file_hash(self.receipt_path)
                approved['approval']['contract_hash'] = plan.canonical_contract_hash(approved)
                with self.assertRaises(plan.ContractError):
                    self.approve('fleet')
                with self.assertRaises(plan.ContractError):
                    plan.validate_approved_contract(approved)

    def test_changed_evidence_after_ingestion_refused(self):
        self.bind_review()
        self.contract = self.ingest({'gate': 'combined', 'result': self.contract['gates']})
        approved = self.approve('fleet')
        self.receipt_path.write_text('{}', encoding='utf-8')
        with self.assertRaises(plan.ContractError):
            self.approve('fleet')
        with self.assertRaises(plan.ContractError):
            plan.validate_approved_contract(approved)

    def test_rounds_and_malformed_review_are_contract_errors(self):
        self.bind_review()
        bad = [None, [], {}, {'fidelity': {}}, copy.deepcopy(self.contract['gates'])]
        bad[-1]['sufficiency']['round'] = 1
        for result in bad:
            with self.subTest(result=result), self.assertRaises(plan.ContractError):
                self.ingest({'gate': 'combined', 'result': result})

    def test_policy_hash_and_legacy_compatibility(self):
        legacy = self.fixture.draft
        self.assertNotEqual(plan.planning_payload_hash(legacy), plan.planning_payload_hash(self.contract))
        plan.verify_gate_approval(legacy)
        legacy = copy.deepcopy(legacy)
        legacy['gates']['sufficiency']['auditor'] = legacy['gates']['fidelity']['auditor']
        with self.assertRaises(plan.ContractError):
            plan.verify_gate_approval(legacy)

    def test_opencode_real_identity_shape_without_lineage(self):
        self.bind_review()
        self.receipt.update(agent='opencode', adapter='opencode', model='glm-5.3-flash',
            execution_profile_id='opencode-glm-5-3-flash-max', model_provider_id=None,
            model_identity={'status': 'verified', 'expected_runtime_model': 'zai-coding-plan/glm-5.3-flash',
                            'observed_runtime_models': ['zai-coding-plan/glm-5.3-flash']})
        self.write(self.receipt_path, self.receipt)
        for gate in self.contract['gates'].values():
            gate['auditor'].update(identity='opencode', model='glm-5.3-flash', lineage='zai')
            gate['evidence']['sha256'] = plan.file_hash(self.receipt_path)
        self.sync_history()
        before = self.receipt_path.read_bytes()
        self.contract = self.ingest({'gate': 'combined', 'result': self.contract['gates']})
        approved = self.approve()
        plan.validate_approved_contract(approved)
        self.assertEqual(self.receipt_path.read_bytes(), before)
        for field, bad in [('status', 'unverified'), ('observed_runtime_models', ['openai/gpt-6-astra']),
                           ('expected_runtime_model', 'other/glm-5.3-flash')]:
            with self.subTest(field=field):
                original = self.receipt['model_identity'][field]
                self.receipt['model_identity'][field] = bad
                self.write(self.receipt_path, self.receipt)
                for gate in self.contract['gates'].values():
                    gate['evidence']['sha256'] = plan.file_hash(self.receipt_path)
                self.sync_history()
                with self.assertRaises(plan.ContractError):
                    self.approve()
                self.receipt['model_identity'][field] = original

    def test_explicit_new_revision_migration_preserves_base(self):
        base, base_path = self.fixture.approve()
        base_bytes = base_path.read_bytes()
        c = copy.deepcopy(base)
        c.update(revision=2, previous_contract_hash=base['approval']['contract_hash'], status='draft')
        c['approval'] = {'approved_by': None, 'approved_at': None, 'contract_hash': None}
        c['planning'].update(review_policy='full-combined-v1', planner_session_id='planner-revision-2')
        c['graph'].update(previous_projection_sha256=base['graph']['projection_sha256'], projection_sha256=None)
        old = c['tasks'][-1]
        old['status'] = 'blocked'
        new = copy.deepcopy(old)
        new.update(task_id='TASK-006', graph_node_id='node-final-r2', status='pending')
        new['acceptance_criteria'][0]['criterion_id'] = 'crit-final-r2'
        c['tasks'].append(new)
        for requirement in c['requirements']:
            if 'TASK-005' in requirement['task_refs']:
                requirement['task_refs'].append('TASK-006')
        c['amendment'] = {'reason': 'New revision uses authorized combined policy.',
                          'affected_task_ids': ['TASK-005'], 'introduced_task_ids': ['TASK-006'],
                          'authority': 'parent', 'approval_ref': 'coordinator-review'}
        self.contract = c
        self.bind_review()
        with self.assertRaises(plan.ContractError):
            plan.validate_amendment(base, c)
        evidence = self.write(self.path('authority.json'), {
            'authority': 'coordinator', 'reference': 'thread:test/message:authorized-correction',
            'scope': 'planning.review_policy:new-revision-opt-in', 'contract_id': c['contract_id'],
            'base_contract_hash': c['previous_contract_hash'], 'target_revision': 2,
            'review_policy': 'full-combined-v1', 'candidate_sha256': plan.planning_payload_hash(c)})
        c['amendment']['review_policy_migration'] = {
            'version': 'full-combined-migration-v1', 'authority': 'coordinator',
            'base_contract_hash': c['previous_contract_hash'],
            'evidence': {'path': str(evidence), 'sha256': plan.file_hash(evidence)}}
        old_hash = plan.planning_payload_hash(c)
        self.bind_review()
        plan.validate_amendment(base, c)
        self.contract = self.ingest({'gate': 'combined', 'result': c['gates']})
        approved = self.approve('fleet', base_path)
        plan.validate_approved_contract(approved)
        self.assertEqual(base_path.read_bytes(), base_bytes)
        self.assertEqual(approved['planning']['review_history'][:2], base['planning']['review_history'])
        c['amendment']['review_policy_migration']['authority'] = 'self'
        self.assertNotEqual(old_hash, plan.planning_payload_hash(c))
        with self.assertRaises(plan.ContractError):
            plan.validate_amendment(base, c)


if __name__ == '__main__':
    unittest.main()
