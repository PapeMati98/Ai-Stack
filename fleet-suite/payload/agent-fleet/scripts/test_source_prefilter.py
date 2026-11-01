"""Regression and adversarial cases. Fixtures are synthetic, never real secrets."""
import hashlib
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import fleet
import source_prefilter as sp
import supplemental_evidence as supplemental


API_VAR = 'api' + 'Key'
ENV_NAME = 'CLAWOPS_' + 'API_' + 'KEY'
ENV_REFERENCE = 'process.env.' + ENV_NAME
IDENTITY_REFERENCE = 'this.opts.identity.' + API_VAR
PRIVATE_KEY_HEADER = '-----BEGIN ' + 'PRIVATE' + ' KEY-----'

ENV = 'const ' + API_VAR + ' = ' + ENV_REFERENCE + ';'
PROPERTY = 'const ' + API_VAR + ' = ' + IDENTITY_REFERENCE + ';'
LITERAL = 'const ' + API_VAR + ' = "' + 'synthetic_fixture_value_0123456789' + '";'


class SourcePrefilterTests(unittest.TestCase):
    def test_references_and_generic_boundary(self):
        for source in (ENV, PROPERTY, '\ufeff' + ENV, '// emoji \U0001f600\n' + ENV,
                       'const ' + API_VAR + '=\n ' + ENV_REFERENCE + ';',
                       'this.' + API_VAR + ' = ' + IDENTITY_REFERENCE + ';',
                       'const x = {' + API_VAR + ': ' + ENV_REFERENCE + '};'):
            with self.subTest(source=source):
                self.assertEqual(fleet.scan_sensitive_source(source.encode(), 'file.ts'), [])
                self.assertIn('assigned-secret', fleet.scan_sensitive_task(source))

    def test_adversarial_sources(self):
        cases = [LITERAL,
            ENV.replace(ENV_REFERENCE, 'getSecretValueFromStore()'),
            ENV.replace(ENV_REFERENCE, 'longSourceIdentifier[\'' + ENV_NAME + '\']'),
            ENV.replace(ENV_REFERENCE, ENV_REFERENCE + ' || "fallback"'),
            ENV.replace(ENV_REFERENCE, ENV_REFERENCE + ' ?? fallbackValue'),
            ENV.replace(ENV_REFERENCE, '(' + ENV_REFERENCE + ')'),
            ENV.replace(ENV_REFERENCE, 'process?.env.' + ENV_NAME),
            ENV.replace(ENV_REFERENCE, ENV_REFERENCE + ' as string'),
            '// ' + ENV, '/*\n' + ENV + '\n*/',
            'const text = `' + ENV + '`;',
            'const text = "' + ENV + '";',
            'const text = `${' + API_VAR + ' = ' + ENV_REFERENCE + '}`;',
            'function f(' + API_VAR + ' = ' + ENV_REFERENCE + ') {}',
            ENV + '\nconst malformed = ;',
            ENV + '\n' + LITERAL.replace('const ' + API_VAR, 'const access' + 'Token'),
            ENV + ' ' + LITERAL.replace('const ' + API_VAR, 'const access' + 'Token'),
            ENV + '\n// Bearer ' + 'a' * 30,
            ENV + '\n// sk-' + 'a' * 30,
            ENV + '\n// ' + PRIVATE_KEY_HEADER,  # backup-secret-scan: allow-fixture (header only; no key)
        ]
        for source in cases:
            with self.subTest(source=source):
                self.assertTrue(fleet.scan_sensitive_source(source.encode(), 'file.ts'))

    def test_unknown_language_and_invalid_encoding(self):
        for name in ('file.json', 'file.md', 'file.js', 'file.py', '.env'):
            self.assertTrue(fleet.scan_sensitive_source(ENV.encode(), name))
        self.assertTrue(fleet.scan_sensitive_source(b'\xff' + ENV.encode(), 'file.ts'))

    def test_missing_parser_fails_closed(self):
        def clear_parser_caches():
            sp.reference_ranges.cache_clear()
            fleet._content_policy._prefilter_module().reference_ranges.cache_clear()

        clear_parser_caches()
        try:
            with patch.object(sp.shutil, 'which', return_value=None):
                self.assertTrue(fleet.scan_sensitive_source(ENV.encode(), 'file.ts'))
        finally:
            clear_parser_caches()

    def test_diff_requires_exact_full_source(self):
        diff = 'diff --git a/x.ts b/x.ts\n--- a/x.ts\n+++ b/x.ts\n@@ -1 +1 @@\n-const x = 1;\n+' + ENV + '\n'
        with self.assertRaises(fleet.FleetError):
            fleet.serialize_quality_patch(diff)
        loader = lambda side, path: (b'const x = 1;\n' if side == 'old' else (ENV + '\n').encode())
        result, metadata = fleet.serialize_quality_patch(diff, sources=sp.DiffSources(loader, fleet.SENSITIVE_PATTERNS))
        self.assertEqual(result, diff.encode())
        self.assertTrue(metadata['patch_values_complete'])
        for data in ('/*\n' + ENV + '\n*/', 'const text = `\n' + ENV + '\n`;', LITERAL):
            with self.subTest(data=data), self.assertRaises(fleet.FleetError):
                fleet.serialize_quality_patch(diff, sources=sp.DiffSources(lambda *args: data.encode(), fleet.SENSITIVE_PATTERNS))

    def test_comment_outside_hunk_cannot_fake_source(self):
        diff = 'diff --git a/x.ts b/x.ts\n--- a/x.ts\n+++ b/x.ts\n@@ -2 +2 @@\n-const x = 1;\n+' + ENV + '\n'
        loader = lambda side, path: ('/*\n' + ('const x = 1;' if side == 'old' else ENV) + '\n*/').encode()
        with self.assertRaises(fleet.FleetError):
            fleet.serialize_quality_patch(diff, sources=sp.DiffSources(loader, fleet.SENSITIVE_PATTERNS))

    def test_removed_literal_still_blocked(self):
        diff = 'diff --git a/x.ts b/x.ts\n--- a/x.ts\n+++ b/x.ts\n@@ -1 +1 @@\n-' + LITERAL + '\n+' + ENV + '\n'
        loader = lambda side, path: (LITERAL if side == 'old' else ENV).encode()
        result, metadata = fleet.serialize_quality_patch(diff, sources=sp.DiffSources(loader, fleet.SENSITIVE_PATTERNS))
        self.assertFalse(metadata['patch_values_complete'])
        self.assertEqual(metadata['patch_review_disposition'], 'BLOCKED')
        self.assertNotIn(b'synthetic_fixture_value', result)
        self.assertIn(ENV.encode(), result)

    def test_git_pre_and_post_images(self):
        with tempfile.TemporaryDirectory(prefix='fleet-prefilter-') as temp:
            root = Path(temp)
            def git(*args):
                return subprocess.run(['git', *args], cwd=root, capture_output=True, check=True).stdout
            git('init', '-q')
            (root / 'x.ts').write_text(PROPERTY + '\n', encoding='utf-8')
            git('add', 'x.ts')
            git('-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.invalid', 'commit', '-qm', 'fixture')
            (root / 'x.ts').write_text(ENV + '\n', encoding='utf-8')
            diff = git('diff', '--no-ext-diff', 'HEAD', '--').decode()
            result, metadata = fleet.serialize_quality_patch(diff, sources=sp.git_sources(root, 'HEAD', None, fleet.SENSITIVE_PATTERNS))
            self.assertEqual(result, diff.encode())
            self.assertTrue(metadata['patch_values_complete'])
            data = (root / 'x.ts').read_bytes()
            entries = [{'path': 'x.ts', 'sha256': hashlib.sha256(data).hexdigest()}]
            self.assertEqual(len(fleet.preflight_required_inputs(root, entries)), 1)
            self.assertEqual(supplemental._read(fleet, root / 'x.ts', 2048, source_path='x.ts'), data)
            with self.assertRaises(ValueError):
                supplemental._read(fleet, root / 'x.ts', 2048)
            (root / 'x.ts').write_text(LITERAL, encoding='utf-8')
            with self.assertRaises(fleet.FleetError):
                fleet.preflight_required_inputs(root, entries)

    def test_complete_outgoing_quality_bundle(self):
        with tempfile.TemporaryDirectory(prefix='fleet-source-bundle-') as temp:
            root = Path(temp)
            worktree = root / 'evidence/worker-evidence/worktree'
            worktree.mkdir(parents=True)
            def git(*args):
                return subprocess.run(['git', *args], cwd=worktree, capture_output=True, check=True).stdout
            def commit():
                git('add', 'module.ts')
                git('-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.invalid', 'commit', '-qm', 'fixture')
                return git('rev-parse', 'HEAD').decode().strip()
            git('init', '-q')
            source = worktree / 'module.ts'
            source.write_text('const x = 1;\n', encoding='utf-8')
            base = commit()
            source.write_text(PROPERTY + '\n', encoding='utf-8')
            commit()
            source.write_text(ENV + '\n', encoding='utf-8')
            worker = dict(run_id='run-20260909T010000Z-aaaaaaaaaa', root_task_id='task-worker-0001',
                agent='qwen', model='deepseek-v4-flash-0731', execution_profile_id='qwen-deepseek-v4-flash-0731',
                mode='implement', run_status='COMPLETED', exit_code=0, handoff=None,
                git_evidence=dict(worktree=str(worktree), base_commit_sha=base, base_provenance='recorded'))
            contract = dict(schema_version=1, group_id='group-quality-0001', title='Synthetic source review',
                acceptance_criteria=['References retain exact bytes'], integration_criteria=[],
                risk='medium', max_repair_rounds=2)
            bundle, manifest = fleet.prepare_quality_bundle(root, gate_id='qgate-20260909T020000Z-bbbbbbbbbb',
                contract=contract, worker_records=[worker], profiles=fleet.load_profiles(fleet.DEFAULT_PROFILES),
                previous=None, dry_run=False)
            item = manifest['workers'][0]
            self.assertTrue(item['patch_values_complete'])
            self.assertEqual(item['patch_review_disposition'], 'REVIEWABLE')
            self.assertEqual((bundle / 'snapshots/task-worker-0001/module.ts').read_bytes(), source.read_bytes())
            patch_bytes = (bundle / item['patch']).read_bytes()
            self.assertIn(PROPERTY.encode(), patch_bytes)
            self.assertIn(ENV.encode(), patch_bytes)
            self.assertEqual(item['patch_sha256'], hashlib.sha256(patch_bytes).hexdigest())
            source.write_text(LITERAL, encoding='utf-8')
            with self.assertRaises(fleet.FleetError):
                fleet.prepare_quality_bundle(root, gate_id='qgate-20260909T020000Z-cccccccccc',
                    contract=contract, worker_records=[worker], profiles=fleet.load_profiles(fleet.DEFAULT_PROFILES),
                    previous=None, dry_run=False)


if __name__ == '__main__':
    unittest.main()
