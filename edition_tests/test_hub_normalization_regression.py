"""Real gateway API regressions; synthetic stores owned only by this task."""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / 'shared/knowledge-hub/skills/llm-wiki/scripts/knowledge.py'
spec = importlib.util.spec_from_file_location('release_gateway_product_checks', SOURCE)
kg = importlib.util.module_from_spec(spec)
spec.loader.exec_module(kg)


class NormalizationProductChecks(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=Path(os.environ.get('TEAM_TEST_ROOT', str(ROOT))), prefix='hub-product-')
        self.root = Path(self.tmp.name)
        self.env = patch.dict(os.environ)
        self.env.start()
        for key in ('AGENT_KNOWLEDGE_CONFIG', 'AGENT_KNOWLEDGE_VAULT',
                    'KNOWLEDGE_CARETAKER_STATUS_PATH', 'KNOWLEDGE_READ_TELEMETRY_ROOT'):
            os.environ.pop(key, None)
        home = self.root / 'home'
        (home / '.agents').mkdir(parents=True)
        self.home = patch.object(Path, 'home', return_value=home)
        self.home.start()
        self.addCleanup(self.home.stop)
        self.addCleanup(self.env.stop)
        self.addCleanup(self.tmp.cleanup)

    def config(self, **fields):
        p = self.root / 'config.json'
        p.write_text(json.dumps({'managed_by': 'shared-agent-knowledge-hub', **fields}), encoding='utf-8')
        os.environ['AGENT_KNOWLEDGE_CONFIG'] = str(p)
        return p

    def vault(self):
        p = self.root / 'a/b/c/vault'
        p.mkdir(parents=True)
        (p / 'knowledge-vault.json').write_text('{}', encoding='utf-8')
        return p

    def test_actual_unexpandable_config_path_is_controlled(self):
        os.environ['AGENT_KNOWLEDGE_CONFIG'] = '~'
        with patch('os.path.expanduser', side_effect=lambda p: p):
            with self.assertRaises(kg.GatewayError):
                kg.resolve_vault(None)

    def test_actual_unexpandable_selected_vault_is_controlled(self):
        self.config(vault_path='~')
        with patch('os.path.expanduser', side_effect=lambda p: p):
            with self.assertRaises(kg.GatewayError):
                kg.resolve_vault(None)

    def test_unresolvable_default_home_is_controlled(self):
        with patch.object(Path, 'home', side_effect=RuntimeError('synthetic unavailable home')):
            with self.assertRaises(kg.GatewayError):
                kg.resolve_vault(None)

    def test_missing_caretaker_field_does_not_use_real_ancestor_trap(self):
        vault = self.vault()
        trap = vault.parents[3] / 'openclaw-data/hub-caretaker/latest.json'
        trap.parent.mkdir(parents=True)
        trap.write_text('{}', encoding='utf-8')
        self.config(vault_path=str(vault))
        self.assertIsNone(kg.resolve_caretaker_status_path(vault))

    def test_unexpandable_selected_caretaker_is_unknown(self):
        vault = self.vault()
        self.config(vault_path=str(vault), caretaker_status_path='~')
        with patch('os.path.expanduser', side_effect=lambda p: p):
            self.assertIsNone(kg.resolve_caretaker_status_path(vault))

    def test_unexpandable_environment_caretaker_is_unknown(self):
        vault = self.vault()
        self.config(vault_path=str(vault))
        os.environ['KNOWLEDGE_CARETAKER_STATUS_PATH'] = '~'
        with patch('os.path.expanduser', side_effect=lambda p: p):
            self.assertIsNone(kg.resolve_caretaker_status_path(vault))

    def test_invalid_environment_caretaker_does_not_use_selected_status(self):
        vault = self.vault()
        trap = self.root / 'other-status.json'
        trap.write_text('{}', encoding='utf-8')
        self.config(vault_path=str(vault), caretaker_status_path=str(trap))
        for value in ('', 'relative.json', str(self.root / 'missing.json')):
            with self.subTest(value=value):
                os.environ['KNOWLEDGE_CARETAKER_STATUS_PATH'] = value
                self.assertIsNone(kg.resolve_caretaker_status_path(vault))

    def test_unexpandable_telemetry_root_is_controlled(self):
        vault = self.vault()
        os.environ['KNOWLEDGE_READ_TELEMETRY_ROOT'] = '~'
        with patch('os.path.expanduser', side_effect=lambda p: p):
            with self.assertRaises(kg.GatewayError):
                kg.read_telemetry_path(vault)

    def test_unexpandable_receipt_root_is_controlled(self):
        vault = self.vault()
        with patch('os.path.expanduser', side_effect=lambda p: p):
            with self.assertRaises(kg.GatewayError):
                kg.receipt_root(vault, '~')

    def test_unresolvable_telemetry_and_receipt_default_homes_are_controlled(self):
        vault = self.vault()
        with patch.object(Path, 'home', side_effect=RuntimeError('synthetic unavailable home')):
            for call in (lambda: kg.read_telemetry_path(vault), lambda: kg.receipt_root(vault, None)):
                with self.assertRaises(kg.GatewayError):
                    call()


if __name__ == '__main__':
    print('gateway_sha256=' + hashlib.sha256(SOURCE.read_bytes()).hexdigest(), flush=True)
    unittest.main()
