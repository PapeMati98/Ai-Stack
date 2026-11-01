"""Synthetic source-capability tests; fixtures intentionally retained."""
import copy
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest
import uuid
from unittest.mock import patch


MODULE = Path(__file__).resolve().parents[1] / 'fleet-suite/payload/agent-fleet/scripts/mistral_tools.py'
spec = importlib.util.spec_from_file_location('guard_tools_test_module', MODULE)
tools = importlib.util.module_from_spec(spec)
spec.loader.exec_module(tools)


class SourceGrantTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.gettempdir()) / ('guard-synthetic-' + uuid.uuid4().hex)
        self.root.mkdir()
        self.ws = self.root / 'workspace'
        self.run = self.root / 'run'
        self.ws.mkdir()
        self.run.mkdir()
        (self.ws / 'tests').mkdir()
        (self.ws / 'src').mkdir()
        self.rel = 'tests/test_secret_guard.py'
        self.path = self.ws / self.rel
        self.path.write_bytes(b'def test_example():\n    assert True\n')
        self.args = dict(workspace=self.ws, run_dir=self.run, mode='implement',
                         scope=['src'], inputs=[self.rel])

    def records(self):
        data = self.path.read_bytes()
        return [{'path': self.rel, 'sha256': hashlib.sha256(data).hexdigest(), 'size_bytes': len(data)}]

    def policy(self):
        verified = tools.create_verified_read_inputs(**self.args, records=self.records())
        return tools.build_policy(**self.args, checks=[['python', self.rel]], verified_read_inputs=verified)

    def test_admitted_read_list_snapshot_and_script(self):
        policy = self.policy()
        self.assertIn('assert True', tools.read_text(policy, self.rel)['content'])
        self.assertIn(self.rel, tools.list_files(policy)['entries'])
        self.assertIn(self.rel, tools.convert_check_argv(policy, ['python', self.rel]))
        target = self.root / 'snapshot'
        tools.materialize_snapshot(policy, target)
        self.assertEqual((target / self.rel).read_bytes(), self.path.read_bytes())

    def test_strict_default_forgery_overlap_and_preview(self):
        with self.assertRaises(tools.ToolPolicyError): tools.normalize_relative(self.rel)
        with self.assertRaises(tools.ToolPolicyError): tools.build_policy(**self.args, checks=[], verified_read_inputs={})
        with self.assertRaises(tools.ToolPolicyError): tools.VerifiedReadInputs({}, (), [])
        bad = dict(self.args, scope=['tests'])
        with self.assertRaises(tools.ToolPolicyError): tools.create_verified_read_inputs(**bad, records=self.records())
        preview = tools.build_policy(**self.args, checks=[], preview=True)
        with self.assertRaises(tools.ToolPolicyError): tools.write_policy(preview)

    def test_digest_and_bound_run_workspace(self):
        grant = tools.create_verified_read_inputs(**self.args, records=self.records())
        other = self.root / 'other'
        other.mkdir()
        with self.assertRaises(tools.ToolPolicyError):
            tools.build_policy(**dict(self.args, run_dir=other), checks=[], verified_read_inputs=grant)
        with self.assertRaises(tools.ToolPolicyError):
            tools.build_policy(**dict(self.args, workspace=other), checks=[], verified_read_inputs=grant)
        policy = tools.build_policy(**self.args, checks=[], verified_read_inputs=grant)
        path = tools.write_policy(policy)
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        with self.assertRaises(tools.ToolPolicyError): tools.load_policy(path)
        with self.assertRaises(tools.ToolPolicyError): tools.load_policy(path, '0' * 64)
        self.assertEqual(tools.load_policy(path, digest)['read_sources'][0]['path'], self.rel)

    def test_preflight_digest_drift_scan_and_missing_snapshot(self):
        bad = self.records()
        bad[0]['sha256'] = '0' * 64
        with self.assertRaises(tools.ToolPolicyError): tools.create_verified_read_inputs(**self.args, records=[])
        with self.assertRaises(tools.ToolPolicyError): tools.create_verified_read_inputs(**self.args, records=bad)
        with patch.object(tools, '_source_scan', side_effect=RuntimeError('failure')):
            with self.assertRaises(Exception): tools.create_verified_read_inputs(**self.args, records=self.records())
        policy = self.policy()
        self.path.write_bytes(b'changed')
        with self.assertRaises(tools.ToolPolicyError): tools.read_text(policy, self.rel)
        with self.assertRaises(tools.ToolPolicyError): tools.materialize_snapshot(policy, self.root / 'snapshot')
        missing = copy.deepcopy(policy)
        missing['inputs'] = []
        with self.assertRaises(tools.ToolPolicyError): tools.materialize_snapshot(missing, self.root / 'snapshot2')

    def test_hard_names_and_sensitive_content(self):
        for value in ('tests/.env_secret.py', 'tests/auth_secret.py', 'tests/secret_key.py',
                      'tests/credentials_secret.py', 'tests/secret.py.', 'tests/../secret.py',
                      'tests/SECRET~1.py', '.git/secret.py'):
            with self.subTest(value=value), self.assertRaises(tools.ToolPolicyError):
                tools.validate_preflight_paths(self.ws, [value])
        self.path.write_bytes(('token = "' + 'gh' + 'p_' + 'A' * 25 + '"\n').encode())
        with self.assertRaises(tools.ToolPolicyError): tools.create_verified_read_inputs(**self.args, records=self.records())

    def test_hardlink_refused(self):
        try:
            os.link(self.path, self.ws / 'tests' / 'other.py')
        except OSError:
            self.skipTest('hard links unavailable')
        with self.assertRaises(tools.ToolPolicyError): tools.validate_preflight_paths(self.ws, [self.rel])

    def test_wrong_object_same_bytes_and_link(self):
        policy = self.policy()
        data = self.path.read_bytes()
        self.path.rename(self.ws / 'tests' / 'retained.py')
        self.path.write_bytes(data)
        with self.assertRaises(tools.ToolPolicyError): tools.read_text(policy, self.rel)
        link = self.ws / 'tests' / 'link_secret.py'
        try:
            link.symlink_to(self.path)
        except OSError:
            self.skipTest('symlinks unavailable')
        with self.assertRaises(tools.ToolPolicyError):
            tools.validate_preflight_paths(self.ws, ['tests/link_secret.py'])

    def test_json_grant_cannot_replace_capability(self):
        record = dict(self.records()[0], identity=[0, 0])
        forged = {'read_sources': [record]}
        with self.assertRaises(tools.ToolPolicyError):
            tools.build_policy(**self.args, checks=[], verified_read_inputs=forged)

