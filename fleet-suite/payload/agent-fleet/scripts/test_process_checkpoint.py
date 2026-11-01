"""Streaming metadata checkpoints for synthetic child processes only."""
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

import process_supervision

SCRIPTS_DIR = Path(__file__).resolve().parent
CHECKPOINT_FIELDS = {
    'schema',
    'run_correlation',
    'pid',
    'status',
    'exit_code',
    'stdout_bytes',
    'stdout_sha256',
    'stderr_bytes',
    'stderr_sha256',
    'started_at',
    'updated_at',
    'finished_at',
    'elapsed_seconds',
}


class ProcessCheckpointTests(unittest.TestCase):
    def test_checkpoint_is_opt_in(self):
        result = process_supervision.run(
            [sys.executable, '-u', '-c', "print('ordinary-run')"],
            cwd=SCRIPTS_DIR,
            env=dict(os.environ),
            stdin_text=None,
            timeout=5,
        )

        self.assertEqual(result['exit_code'], 0, result['stderr'])
        self.assertEqual(result['stdout'], 'ordinary-run\n')
        self.assertNotIn('process_checkpoint', result)

    def test_checkpoint_is_visible_while_child_runs_and_has_final_metadata(self):
        with tempfile.TemporaryDirectory(prefix='process-checkpoint-', dir=SCRIPTS_DIR) as tmp:
            checkpoint_path = Path(tmp) / 'run-metadata.json'
            marker = 'SYNTHETIC_SECRET_TEXT_FOR_CHECKPOINT_TEST'
            child_code = (
                f"import sys,time; sys.stdout.write({marker!r} + '\\n' + ('x' * 70000) + '\\n'); "
                'sys.stdout.flush(); time.sleep(2.0)'
            )
            outcome = []
            failures = []

            def invoke():
                try:
                    outcome.append(process_supervision.run(
                        [sys.executable, '-u', '-c', child_code],
                        cwd=Path(tmp),
                        env=dict(os.environ),
                        stdin_text=None,
                        timeout=8,
                        checkpoint_path=checkpoint_path,
                    ))
                except BaseException as exc:  # surface worker-thread failures in the test thread
                    failures.append(exc)

            runner = threading.Thread(target=invoke, daemon=True)
            runner.start()
            deadline = time.monotonic() + 5
            in_progress = None
            while time.monotonic() < deadline and runner.is_alive():
                if checkpoint_path.exists():
                    try:
                        candidate = json.loads(checkpoint_path.read_text(encoding='utf-8'))
                    except (OSError, json.JSONDecodeError):
                        candidate = None
                    if (candidate and candidate.get('status') == 'running'
                            and candidate.get('stdout_bytes', 0) >= process_supervision.CHECKPOINT_INTERVAL_BYTES):
                        in_progress = candidate
                        break
                time.sleep(0.025)

            self.assertIsNotNone(in_progress, 'checkpoint with streamed bytes was not visible during run()')
            self.assertTrue(runner.is_alive(), 'run() returned before the synthetic child finished waiting')
            self.assertEqual(set(in_progress), CHECKPOINT_FIELDS)
            self.assertNotIn(marker, json.dumps(in_progress))
            self.assertEqual(in_progress['schema'], 'agent-fleet.process-checkpoint.v1')
            self.assertRegex(in_progress['run_correlation'], r'^[0-9a-f]{32}$')
            self.assertGreater(in_progress['pid'], 0)
            self.assertIsNone(in_progress['exit_code'])
            self.assertIsNone(in_progress['finished_at'])

            runner.join(timeout=8)
            self.assertFalse(runner.is_alive(), 'run() did not finish within the test deadline')
            self.assertEqual(failures, [])
            self.assertEqual(len(outcome), 1)
            result = outcome[0]
            self.assertEqual(result['exit_code'], 0, result['stderr'])
            self.assertTrue(result['stdout'].startswith(marker + '\n'))

            final_text = checkpoint_path.read_text(encoding='utf-8')
            final = json.loads(final_text)
            self.assertNotIn(marker, final_text)
            self.assertEqual(set(final), CHECKPOINT_FIELDS)
            self.assertEqual(final['status'], 'completed')
            self.assertEqual(final['exit_code'], 0)
            stdout_wire = result['stdout'].encode('utf-8')
            stderr_wire = result['stderr'].encode('utf-8')
            if os.name == 'nt':
                stdout_wire = stdout_wire.replace(b'\n', b'\r\n')
                stderr_wire = stderr_wire.replace(b'\n', b'\r\n')
            self.assertEqual(final['stdout_bytes'], len(stdout_wire))
            self.assertEqual(final['stdout_sha256'], hashlib.sha256(stdout_wire).hexdigest())
            self.assertEqual(final['stderr_bytes'], len(stderr_wire))
            self.assertEqual(final['stderr_sha256'], hashlib.sha256(stderr_wire).hexdigest())
            telemetry = result['process_checkpoint']
            self.assertEqual(telemetry['status'], 'ok')
            self.assertTrue(telemetry['final_persisted'])
            self.assertEqual(telemetry['run_correlation'], final['run_correlation'])

    def test_writer_failure_does_not_change_child_result_and_is_reported(self):
        with tempfile.TemporaryDirectory(prefix='process-checkpoint-failure-', dir=SCRIPTS_DIR) as tmp:
            checkpoint_path = Path(tmp) / 'run-metadata.json'
            before = time.monotonic()
            with mock.patch.object(
                process_supervision,
                '_write_checkpoint_atomically',
                side_effect=OSError('synthetic writer failure with private path'),
            ):
                result = process_supervision.run(
                    [sys.executable, '-u', '-c', "import sys,time; print('retained-output', flush=True); time.sleep(.1)"],
                    cwd=Path(tmp),
                    env=dict(os.environ),
                    stdin_text=None,
                    timeout=5,
                    checkpoint_path=checkpoint_path,
                )

            self.assertLess(time.monotonic() - before, 4)
            self.assertEqual(result['exit_code'], 0, result['stderr'])
            self.assertEqual(result['stdout'], 'retained-output\n')
            self.assertFalse(result['supervision_failed'])
            telemetry = result['process_checkpoint']
            self.assertEqual(telemetry['status'], 'warning')
            self.assertGreaterEqual(telemetry['write_failures'], 1)
            self.assertFalse(telemetry['final_persisted'])
            self.assertIn('write_failed', telemetry['warnings'])
            self.assertFalse(checkpoint_path.exists())
            self.assertNotIn('private path', json.dumps(telemetry))


if __name__ == '__main__':
    unittest.main()
