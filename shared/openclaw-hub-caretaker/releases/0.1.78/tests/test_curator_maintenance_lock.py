"""The manual curator and scheduled tick share one caretaker maintenance lock."""
from contextlib import contextmanager, redirect_stdout
import io
import json
import msvcrt
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch

import curate_once
import maintenance
from caretaker.publication import PublicationError


def _config(root):
    state = root / 's'
    return SimpleNamespace(
        state_dir=str(state / 'projects' / 'example' / 'state'),
        raw={'maintenance_state_dir': str(state)},
        project_slug='example',
        hub=SimpleNamespace(vault=str(root / 'vault'),
                            script=str(root / 'knowledge.py'),
                            python_executable='python'),
    )


def _byte_lock(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    lock = open(path, 'a+b')
    lock.seek(0)
    if lock.read(1) == b'':
        lock.write(b'0')
        lock.flush()
    lock.seek(0)
    msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
    return lock


class CuratorMaintenanceLockTests(unittest.TestCase):
    def test_manual_config_without_shared_state_refuses_before_hub(self):
        with tempfile.TemporaryDirectory() as temp:
            cfg = _config(Path(temp))
            cfg.raw = {}
            cfg.state_dir = str(Path(temp) / 'independent-state')
            with patch('curate_once.load_config', return_value=cfg), \
                    patch('curate_once.CanonicalKnowledgeGateway') as gateway:
                with self.assertRaisesRegex(PublicationError, 'maintenance_state_dir_required'):
                    curate_once.run('synthetic.json', ['synthetic.md'], publish=True)
            gateway.assert_not_called()

    def test_older_generated_project_config_resolves_tick_root(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            cfg = _config(root)
            cfg.raw = {}
            self.assertEqual(curate_once._maintenance_state_dir(cfg), root / 's')
            cfg.state_dir = str(root / 's' / 'new-event-epoch' / 'projects' /
                                'example' / 'state')
            self.assertEqual(curate_once._maintenance_state_dir(cfg), root / 's')

    def test_manual_publish_refuses_busy_tick_before_touching_hub(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            cfg = _config(root)
            lock = _byte_lock(root / 's' / 'maintenance.lock')
            try:
                output = io.StringIO()
                with patch('curate_once.load_config', return_value=cfg), \
                        patch('curate_once.CanonicalKnowledgeGateway') as gateway, \
                        patch('sys.argv', ['curate_once.py', '--config', 'synthetic.json',
                                           '--proposal', 'synthetic.md', '--publish']), \
                        redirect_stdout(output):
                    exit_code = curate_once.main()
                self.assertEqual(exit_code, 1)
                self.assertEqual(json.loads(output.getvalue())['status'], 'BLOCKED')
                self.assertEqual(json.loads(output.getvalue())['reason'], 'caretaker_tick_running',
                                 output.getvalue())
                gateway.assert_not_called()
                self.assertFalse((root / 'vault').exists())
            finally:
                lock.seek(0)
                msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
                lock.close()

    def test_manual_publish_holds_maintenance_through_curator_release(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            cfg = _config(root)
            lock_path = root / 's' / 'maintenance.lock'

            @contextmanager
            def curator_lock(*args):
                self._assert_maintenance_held(lock_path)
                yield 'synthetic-lock'
                self._assert_maintenance_held(lock_path)

            with patch('curate_once.load_config', return_value=cfg), \
                    patch('curate_once.CanonicalKnowledgeGateway') as gateway, \
                    patch('curate_once.freeze', return_value={}), \
                    patch('curate_once._wire_fleet_publication', return_value={
                        'status': 'REVIEWED_CANDIDATE', 'manifest': 'manifest.json',
                        'review': 'review.json', 'proposal_ids': ['synthetic-id']}), \
                    patch('curate_once.curator_lock', side_effect=curator_lock), \
                    patch('curate_once.accounted_proposal_ids', return_value=set()), \
                    patch('curate_once.command', return_value={'status': 'PUBLISHED'}), \
                    patch('curate_once.checkpoint_or_pending',
                          return_value={'status': 'CHECKPOINTED'}):
                gateway.return_value.health.return_value.status = 'pass'
                gateway.return_value.lint.return_value.status = 'pass'
                result = curate_once.run('synthetic.json', ['synthetic.md'], publish=True)
            self.assertEqual(result['status'], 'PUBLISHED')
            lock = _byte_lock(lock_path)
            lock.seek(0)
            msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
            lock.close()

    def test_tick_path_publishes_while_owning_maintenance_lock(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            cfg = _config(root)
            control_path = root / 'control.json'
            control_path.write_text(json.dumps({'state_dir': str(root / 's')}), encoding='utf-8')

            @contextmanager
            def curator_lock(*args):
                self._assert_maintenance_held(root / 's' / 'maintenance.lock')
                yield 'synthetic-lock'
                self._assert_maintenance_held(root / 's' / 'maintenance.lock')

            def reviewed_candidate(**kwargs):
                return {'status': 'REVIEWED_CANDIDATE', 'manifest': 'manifest.json',
                        'review': 'review.json', 'proposal_ids': ['synthetic-id']}

            def gateway_status():
                return SimpleNamespace(status='pass')

            with patch('maintenance._locked_tick',
                       side_effect=lambda control, state: maintenance._invoke_curate(
                           'synthetic.json', ['synthetic.md'])), \
                    patch('curate_once.load_config', return_value=cfg), \
                    patch('curate_once.CanonicalKnowledgeGateway') as gateway, \
                    patch('curate_once.freeze', return_value={}), \
                    patch('curate_once._wire_fleet_publication', side_effect=reviewed_candidate), \
                    patch('curate_once.curator_lock', side_effect=curator_lock), \
                    patch('curate_once.accounted_proposal_ids', return_value=set()), \
                    patch('curate_once.command', return_value={'status': 'PUBLISHED'}), \
                    patch('curate_once.checkpoint_or_pending',
                          return_value={'status': 'CHECKPOINTED'}):
                gateway.return_value.health.side_effect = gateway_status
                gateway.return_value.lint.side_effect = gateway_status
                result = maintenance.tick(control_path)
            self.assertEqual(result['status'], 'PUBLISHED')
            self.assertEqual(result['publication']['status'], 'PUBLISHED')

    def _assert_maintenance_held(self, path):
        with open(path, 'r+b') as probe:
            probe.seek(0)
            try:
                msvcrt.locking(probe.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError:
                return
            probe.seek(0)
            msvcrt.locking(probe.fileno(), msvcrt.LK_UNLCK, 1)
        self.fail('curator section ran without the caretaker maintenance lock')


if __name__ == '__main__':
    unittest.main()
