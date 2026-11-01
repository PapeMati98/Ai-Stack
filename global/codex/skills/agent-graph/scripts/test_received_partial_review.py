"""Offline public-review retention regressions; no provider replay or host writes."""
import ast
import copy
import importlib.util
import json
import os
from pathlib import Path
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import patch
import uuid

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.dont_write_bytecode = True
os.environ['AGENT_GRAPH_WATCH_EVENTS'] = '0'

SUPPORT_FILENAME = 'test_todo_fleet.py'


def resolve_support_path(scripts_dir=HERE):
    """Require adjacent repo-relative Graph test support; no installed fallback."""
    candidate = Path(scripts_dir) / SUPPORT_FILENAME
    if not candidate.is_file():
        raise RuntimeError(
            'Missing repo-relative Graph test support: %s not found adjacent to %s; '
            'no installed fallback is used' % (SUPPORT_FILENAME, Path(scripts_dir)))
    return candidate


SUPPORT_PATH = resolve_support_path()

def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    value = importlib.util.module_from_spec(spec)
    sys.modules[name] = value
    spec.loader.exec_module(value)
    return value

bridge = load('received_partial_candidate', HERE / 'todo_fleet.py')
SUPPORT_EXPORTS = ('FakeFleet',)


def load_support_isolated(path=SUPPORT_PATH):
    """Load only FakeFleet from adjacent Graph test support, never its module body.

    The adjacent module executes host-dependent admission at import
    (real_zcode_runtime() probes the operator profile), so executing the full
    module would couple this offline regression to host state. Only the
    FakeFleet class body is parsed out and executed; missing support fails
    clearly here, before any fallback lookup runs.
    """
    source = Path(path).read_text(encoding='utf-8')
    tree = ast.parse(source, filename=str(path))
    found = [node for node in tree.body
             if isinstance(node, ast.ClassDef) and node.name in SUPPORT_EXPORTS]
    missing = [name for name in SUPPORT_EXPORTS
               if all(node.name != name for node in found)]
    if missing:
        raise RuntimeError(
            'Missing repo-relative Graph test support: %s does not define %s; '
            'no installed fallback is used' % (path, ', '.join(missing)))
    module = ast.Module(body=found, type_ignores=[])
    ast.fix_missing_locations(module)
    namespace = {'SimpleNamespace': SimpleNamespace, 'Path': Path}
    exec(compile(module, str(path), 'exec'), namespace)
    return SimpleNamespace(**{name: namespace[name] for name in SUPPORT_EXPORTS})


support = load_support_isolated()

class ReceivedReviewTests(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict(os.environ, {'AGENT_GRAPH_TIER_ROUTING': '0'})
        self.env.start()
        self.addCleanup(self.env.stop)
        fixture_root = Path(os.environ['TEAM_TEST_ROOT']).resolve()
        fixtures = fixture_root / 'received-review'
        fixtures.mkdir(exist_ok=True)
        if fixtures.is_symlink() or not fixtures.resolve().is_relative_to(fixture_root):
            raise RuntimeError('Linked/outside fixture directory refused')
        self.root = fixtures / uuid.uuid4().hex
        self.root.mkdir()
        self.review = {'verdict': 'ACCEPT', 'findings': [], 'checked': ['required behavior'], 'summary': 'Synthetic public decision.'}
        self.record = {'completion_reason': 'native-step-cap-observed-needs-parent-review',
            'provider_terminal_reason': 'completed', 'exit_code': 0, 'handoff_valid': True,
            'handoff_status': 'completed', 'model_identity': {'status': 'verified'},
            'runtime_attestation': {'status': 'VERIFIED'}, 'max_model_tool_calls': 24,
            'model_tool_calls': 24, 'context_guard': {'exceeded': False}}
        self.result = {'status': 'PARTIAL', 'model': 'm-model', 'run_id': 'synthetic-review',
            'exit_code': 0, 'handoff': {'status': 'completed', 'quality_gate': self.review}}

    def execute(self, result=None, record=None):
        result = copy.deepcopy(result or self.result)
        record = copy.deepcopy(record or self.record)
        out = self.root / ('round-' + uuid.uuid4().hex) / 'reviewer'
        out.mkdir(parents=True)
        usage = out / 'synthetic-usage.json'
        usage.write_text(json.dumps(record), encoding='utf-8')
        result['usage_record'] = str(usage)
        fleet = support.FakeFleet(result=result)
        task = {'id': 'review-retention', 'text': 'Synthetic review evidence only.', 'mode': 'advisory',
            'scope': ['todo_fleet.py'], 'done_when': ['required behavior'], 'checks': [],
            'maker': [{'agent': 'maker', 'model': 'm-model', 'score': 90, 'reason': 'synthetic'}],
            'reviewer': [{'agent': 'reviewer', 'model': 'm-model', 'score': 90, 'reason': 'synthetic'}]}
        request = {'goal': 'offline regression', 'role': 'reviewer', 'workspace': str(HERE), 'task': task}
        response = bridge.execute(request, out, fleet=fleet)
        self.assertEqual(len(fleet.execute_calls), 1)
        self.assertEqual(json.loads(usage.read_text(encoding='utf-8')), record)
        return response

    def assert_parent_required(self, response):
        self.assertEqual(response['status'], 'BLOCKED')
        self.assertEqual(response['cause'], 'received-review-parent-required')
        self.assertEqual(response['received_review'], self.review)
        self.assertEqual(response['transport_status'], 'PARTIAL')
        self.assertEqual(response['acceptance_status'], 'PARENT_REQUIRED')
        self.assertNotIn('verdict', response)
        self.assertNotIn('checked', response)
        self.assertEqual(len(response['review_recovery']['public_review_sha256']), 64)

    def test_actual_redacted_case_shapes_retain_public_review(self):
        for case in json.loads((HERE / 'redacted-cases.json').read_text(encoding='utf-8')):
            with self.subTest(case=case['run_id']):
                record = copy.deepcopy(self.record)
                for key in ('completion_reason', 'provider_terminal_reason', 'exit_code', 'handoff_valid', 'handoff_status'):
                    record[key] = case[key]
                result = copy.deepcopy(self.result)
                result['exit_code'] = case['exit_code']
                self.assert_parent_required(self.execute(result, record))

    def test_repair_public_decision_is_retained_without_scheduler_verdict(self):
        result = copy.deepcopy(self.result)
        review = result['handoff']['quality_gate']
        review.update(verdict='REPAIR', findings=['Synthetic concrete defect.'])
        response = self.execute(result)
        self.assertEqual(response['received_review']['verdict'], 'REPAIR')
        self.assertEqual(response['status'], 'BLOCKED')
        self.assertNotIn('verdict', response)

    def test_unattested_wrong_identity_quarantined_and_guarded_never_accept(self):
        invalid = [
            ('attestation', {'runtime_attestation': {'status': 'UNVERIFIED'}}, {}),
            ('identity', {'model_identity': {'status': 'wrong-model'}}, {}),
            ('quarantine', {}, {'artifact_disposition': 'quarantined'}),
            ('record-quarantine', {'artifact_disposition': 'quarantined'}, {}),
            ('tool-budget', {'model_tool_calls': 25}, {}),
            ('context', {'context_guard': {'exceeded': True}}, {}),
            ('context-status', {'context_guard': {'status': 'exceeded'}}, {}),
            ('native-cancelled', {'provider_terminal_reason': 'cancelled'}, {}),
            ('handoff-unvalidated', {'handoff_valid': False}, {}),
            ('handoff-uncompleted', {'handoff_status': 'partial'}, {}),
            ('handoff-error', {'handoff_parse_error': 'synthetic invalid'}, {}),
            ('exit-pair', {'exit_code': 124}, {}),
            ('exit-missing', {'exit_code': None}, {}),
            ('exit-bool', {'exit_code': False}, {}),
            ('other-partial', {'completion_reason': 'context-guard'}, {}),
            ('failed-result', {}, {'status': 'FAILED'}),
        ]
        for name, changes, result_changes in invalid:
            with self.subTest(case=name):
                record = copy.deepcopy(self.record)
                result = copy.deepcopy(self.result)
                record.update(changes)
                result.update(result_changes)
                response = self.execute(result, record)
                self.assertEqual(response['status'], 'BLOCKED')
                self.assertNotIn('received_review', response)
                self.assertNotEqual(response.get('verdict'), 'ACCEPT')

    def test_missing_malformed_inconsistent_oversized_review_refused(self):
        reviews = [None, {}, {'verdict': 'ACCEPT'},
            {**self.review, 'findings': ['contradiction']},
            {**self.review, 'verdict': 'REPAIR'},
            {**self.review, 'private_tool_text': 'unexpected'},
            {**self.review, 'summary': 'x' * (bridge.MAX_EVIDENCE_BYTES + 1)}]
        for review in reviews:
            with self.subTest(shape=type(review).__name__, size=len(str(review))):
                result = copy.deepcopy(self.result)
                result['handoff'] = {'quality_gate': review}
                response = self.execute(result)
                self.assertEqual(response['status'], 'BLOCKED')
                self.assertNotIn('received_review', response)

    def test_partial_never_uses_completed_codex_fallback(self):
        with patch.object(bridge, 'recovered_review', side_effect=AssertionError('partial fallback forbidden')):
            self.assert_parent_required(self.execute())
            record = {**self.record, 'provider_terminal_reason': 'cancelled'}
            self.assertEqual(self.execute(record=record)['status'], 'BLOCKED')

    def test_completed_valid_review_unchanged(self):
        result = {**self.result, 'status': 'COMPLETED'}
        response = self.execute(result)
        self.assertEqual(response['status'], 'REVIEWED')
        self.assertEqual(response['verdict'], 'ACCEPT')
        self.assertNotIn('received_review', response)

    def test_helper_does_not_mutate_inputs_or_unwrap_raw_stdout(self):
        result = copy.deepcopy(self.result)
        record = copy.deepcopy(self.record)
        before = copy.deepcopy((result, record))
        bridge.received_partial_review(result, record, attested=True, identity_ok=True)
        self.assertEqual((result, record), before)
        result['handoff'] = None
        result['stdout_path'] = str(self.root / 'should-never-be-read')
        self.assertEqual(bridge.received_partial_review(result, record, attested=True, identity_ok=True), (None, None))

    def test_missing_support_fails_clearly_without_installed_fallback(self):
        missing_dir = self.root / 'no-support-scripts'
        missing_dir.mkdir()
        with self.assertRaisesRegex(RuntimeError, 'Missing repo-relative Graph test support'):
            resolve_support_path(missing_dir)
        with self.assertRaisesRegex(RuntimeError, 'no installed fallback'):
            resolve_support_path(missing_dir)
        self.assertEqual(resolve_support_path(), HERE / SUPPORT_FILENAME)

    def test_support_load_never_executes_host_dependent_module_body(self):
        probe = self.root / 'host-probe-support.py'
        probe.write_text(
            'import os\n'
            'os.environ["RECEIVED_PARTIAL_HOST_PROBE"] = "executed"\n'
            'REAL_ZCODE = object()\n'
            'class FakeFleet:\n'
            '    pass\n', encoding='utf-8')
        with patch.dict(os.environ):
            os.environ.pop('RECEIVED_PARTIAL_HOST_PROBE', None)
            loaded = load_support_isolated(probe)
            self.assertNotIn('RECEIVED_PARTIAL_HOST_PROBE', os.environ)
            self.assertIsNotNone(loaded.FakeFleet)

    def test_support_load_missing_helper_fails_clearly_without_installed_fallback(self):
        probe = self.root / 'helperless-support.py'
        probe.write_text('REAL_ZCODE = None\n', encoding='utf-8')
        with self.assertRaisesRegex(RuntimeError, 'Missing repo-relative Graph test support'):
            load_support_isolated(probe)
        with self.assertRaisesRegex(RuntimeError, 'no installed fallback'):
            load_support_isolated(probe)

    def test_isolated_fakefleet_matches_adjacent_definition(self):
        self.assertEqual(support.FakeFleet.SYNTHETIC_ONLY_PROFILE_ID, 'opencode-beam')
        fleet = support.FakeFleet(result=dict(self.result))
        self.assertEqual(fleet.execute_single(), self.result)
if __name__ == '__main__':
    unittest.main()
