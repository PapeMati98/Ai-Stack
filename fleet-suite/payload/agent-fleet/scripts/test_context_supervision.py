"""Tests for observed-context stop supervision candidate."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest

# Ensure candidate directory is on sys.path for direct imports
CANDIDATE_DIR = Path(__file__).resolve().parent
if str(CANDIDATE_DIR) not in sys.path:
    sys.path.insert(0, str(CANDIDATE_DIR))

from process_supervision import ContextEventUnreadable, ToolEventUnreadable, run


def _default_context_tokens(line):
    line = line.strip()
    if not line:
        return None
    if line.startswith('CTX:'):
        val = line[4:].strip()
        try:
            return int(val)
        except ValueError:
            raise ContextEventUnreadable(f'unreadable int: {val}')
    if line.startswith('UNREADABLE:'):
        raise ContextEventUnreadable('explicit unreadable marker')
    if line.startswith('FAULT:'):
        raise RuntimeError('simulated callback fault')
    if line.startswith('INVALID_RETURN:'):
        return 'not_an_int'
    return None


class TestContextSupervision(unittest.TestCase):
    def _create_retained_work_dir(self):
        # Explicitly use mkdtemp without TemporaryDirectory or deletion/cleanup
        return Path(tempfile.mkdtemp(prefix='context_guard_retained_'))

    def _create_script(self, work_dir, name, code):
        script_path = work_dir / name
        script_path.write_text(code, encoding='utf-8')
        return script_path

    def test_real_process_known_exceed_with_output_and_artifact_retained(self):
        work_dir = self._create_retained_work_dir()
        script = self._create_script(
            work_dir,
            'child_exceed.py',
            (
                'import sys, time\n'
                'from pathlib import Path\n'
                'artifact = Path("artifact.txt")\n'
                'artifact.write_text("retained artifact content", encoding="utf-8")\n'
                'print("step 1: initial output", flush=True)\n'
                'print("CTX: 50", flush=True)\n'
                'time.sleep(0.05)\n'
                'print("step 2: about to exceed", flush=True)\n'
                'print("CTX: 150", flush=True)\n'
                'time.sleep(10.0)\n'
                'print("step 3: should not reach here", flush=True)\n'
            ),
        )
        res = run(
            [sys.executable, '-u', str(script)],
            cwd=str(work_dir),
            env=dict(os.environ),
            stdin_text=None,
            timeout=5.0,
            context_limit=100,
            context_tokens=_default_context_tokens,
        )
        self.assertEqual(res['exit_code'], 127)
        self.assertTrue(res['context_budget_exceeded'])
        self.assertEqual(res['context_limit_tokens'], 100)
        self.assertEqual(res['context_peak_tokens'], 150)
        self.assertFalse(res['supervision_failed'])
        self.assertFalse(res['timed_out'])
        self.assertIn('step 1: initial output', res['stdout'])
        self.assertIn('step 2: about to exceed', res['stdout'])
        self.assertNotIn('step 3: should not reach here', res['stdout'])

        # Verify on-disk artifact is preserved and intact
        artifact_path = work_dir / 'artifact.txt'
        self.assertTrue(artifact_path.is_file())
        self.assertEqual(artifact_path.read_text(encoding='utf-8'), 'retained artifact content')

    def test_limit_boundary_equal_or_smaller_does_not_stop(self):
        work_dir = self._create_retained_work_dir()
        script = self._create_script(
            work_dir,
            'child_boundary.py',
            (
                'import sys\n'
                'print("CTX: 50", flush=True)\n'
                'print("CTX: 100", flush=True)\n'
                'print("CTX: 99", flush=True)\n'
                'print("boundary completed successfully", flush=True)\n'
            ),
        )
        res = run(
            [sys.executable, '-u', str(script)],
            cwd=str(work_dir),
            env=dict(os.environ),
            stdin_text=None,
            timeout=5.0,
            context_limit=100,
            context_tokens=_default_context_tokens,
        )
        self.assertEqual(res['exit_code'], 0)
        self.assertFalse(res['context_budget_exceeded'])
        self.assertEqual(res['context_limit_tokens'], 100)
        self.assertEqual(res['context_peak_tokens'], 100)
        self.assertFalse(res['supervision_failed'])
        self.assertIn('boundary completed successfully', res['stdout'])

    def test_normal_no_context_config(self):
        work_dir = self._create_retained_work_dir()
        script = self._create_script(
            work_dir,
            'child_normal.py',
            'print("normal execution output", flush=True)\n',
        )
        res = run(
            [sys.executable, '-u', str(script)],
            cwd=str(work_dir),
            env=dict(os.environ),
            stdin_text=None,
            timeout=5.0,
            context_limit=None,
            context_tokens=None,
        )
        self.assertEqual(res['exit_code'], 0)
        self.assertIsNone(res['context_limit_tokens'])
        self.assertIsNone(res['context_peak_tokens'])
        self.assertFalse(res['context_scan_incomplete'])
        self.assertFalse(res['context_budget_exceeded'])
        self.assertIn('normal execution output', res['stdout'])

    def test_invalid_config_pre_launch_marker_absence(self):
        work_dir = self._create_retained_work_dir()
        marker_path = work_dir / 'pre_launch_marker.txt'

        invalid_configs = [
            (100, None, ValueError),
            (None, _default_context_tokens, ValueError),
            (0, _default_context_tokens, ValueError),
            (-1, _default_context_tokens, ValueError),
            (True, _default_context_tokens, ValueError),
            ('100', _default_context_tokens, ValueError),
            (100, 'not_callable', TypeError),
        ]

        command = [
            sys.executable,
            '-c',
            f'from pathlib import Path; Path(r"{marker_path}").touch()',
        ]

        for limit, tokens_cb, exc_type in invalid_configs:
            with self.subTest(limit=limit, tokens_cb=tokens_cb):
                with self.assertRaises(exc_type):
                    run(
                        command,
                        cwd=str(work_dir),
                        env=dict(os.environ),
                        stdin_text=None,
                        timeout=5.0,
                        context_limit=limit,
                        context_tokens=tokens_cb,
                    )
                # Verify that no child process was launched: marker file must NOT exist
                self.assertFalse(marker_path.exists())

    def test_no_measurement_unknown(self):
        work_dir = self._create_retained_work_dir()
        script = self._create_script(
            work_dir,
            'child_no_meas.py',
            (
                'import sys\n'
                'print("log line 1", flush=True)\n'
                'print("log line 2", flush=True)\n'
            ),
        )
        res = run(
            [sys.executable, '-u', str(script)],
            cwd=str(work_dir),
            env=dict(os.environ),
            stdin_text=None,
            timeout=5.0,
            context_limit=100,
            context_tokens=lambda line: None,
        )
        self.assertEqual(res['exit_code'], 0)
        self.assertIsNone(res['context_peak_tokens'])
        self.assertEqual(res['context_limit_tokens'], 100)
        self.assertFalse(res['context_scan_incomplete'])
        self.assertFalse(res['context_budget_exceeded'])

    def test_context_event_unreadable(self):
        work_dir = self._create_retained_work_dir()
        script = self._create_script(
            work_dir,
            'child_unreadable.py',
            (
                'import sys\n'
                'print("CTX: 40", flush=True)\n'
                'print("UNREADABLE: corrupted token event", flush=True)\n'
                'print("CTX: 75", flush=True)\n'
                'print("all lines finished", flush=True)\n'
            ),
        )
        res = run(
            [sys.executable, '-u', str(script)],
            cwd=str(work_dir),
            env=dict(os.environ),
            stdin_text=None,
            timeout=5.0,
            context_limit=100,
            context_tokens=_default_context_tokens,
        )
        self.assertEqual(res['exit_code'], 0)
        self.assertFalse(res['supervision_failed'])
        self.assertTrue(res['context_scan_incomplete'])
        self.assertEqual(res['context_peak_tokens'], 75)
        self.assertFalse(res['context_budget_exceeded'])
        self.assertIn('UNREADABLE: corrupted token event', res['stdout'])
        self.assertIn('all lines finished', res['stdout'])

    def test_callback_fault_failure(self):
        work_dir = self._create_retained_work_dir()

        # Case 1: Callback raises an unexpected exception
        script1 = self._create_script(
            work_dir,
            'child_fault.py',
            (
                'import sys\n'
                'print("FAULT: parser exploded", flush=True)\n'
            ),
        )
        res1 = run(
            [sys.executable, '-u', str(script1)],
            cwd=str(work_dir),
            env=dict(os.environ),
            stdin_text=None,
            timeout=5.0,
            context_limit=100,
            context_tokens=_default_context_tokens,
        )
        self.assertEqual(res1['exit_code'], 126)
        self.assertTrue(res1['supervision_failed'])

        # Case 2: Callback returns an invalid non-int type
        script2 = self._create_script(
            work_dir,
            'child_invalid_return.py',
            (
                'import sys\n'
                'print("INVALID_RETURN: not_int", flush=True)\n'
            ),
        )
        res2 = run(
            [sys.executable, '-u', str(script2)],
            cwd=str(work_dir),
            env=dict(os.environ),
            stdin_text=None,
            timeout=5.0,
            context_limit=100,
            context_tokens=_default_context_tokens,
        )
        self.assertEqual(res2['exit_code'], 126)
        self.assertTrue(res2['supervision_failed'])

    def test_fragmented_utf8_json_lines(self):
        work_dir = self._create_retained_work_dir()
        script = self._create_script(
            work_dir,
            'child_fragmented.py',
            (
                'import sys, time\n'
                'prefix = b\'{"tokens": \' \n'
                'sys.stdout.buffer.write(prefix)\n'
                'sys.stdout.buffer.flush()\n'
                'time.sleep(0.02)\n'
                '# Multi-byte UTF8: Polish "zażółć gęślą jaźń"\n'
                'utf8_bytes = "zażółć gęślą jaźń".encode("utf-8")\n'
                'sys.stdout.buffer.write(b\'88, "text": "\' + utf8_bytes[:5])\n'
                'sys.stdout.buffer.flush()\n'
                'time.sleep(0.02)\n'
                'sys.stdout.buffer.write(utf8_bytes[5:] + b\'"}\\n\')\n'
                'sys.stdout.buffer.flush()\n'
            ),
        )

        def json_tokens_cb(line):
            line = line.strip()
            if not line.startswith('{'):
                return None
            try:
                data = json.loads(line)
                return data.get('tokens')
            except Exception:
                raise ContextEventUnreadable('invalid json')

        res = run(
            [sys.executable, '-u', str(script)],
            cwd=str(work_dir),
            env=dict(os.environ),
            stdin_text=None,
            timeout=5.0,
            context_limit=100,
            context_tokens=json_tokens_cb,
        )
        self.assertEqual(res['exit_code'], 0)
        self.assertEqual(res['context_peak_tokens'], 88)
        self.assertFalse(res['context_scan_incomplete'])
        self.assertIn('zażółć gęślą jaźń', res['stdout'])

    def test_oversized_line_then_valid_measurement(self):
        work_dir = self._create_retained_work_dir()
        script = self._create_script(
            work_dir,
            'child_oversized.py',
            (
                'import sys\n'
                '# Output over 1MiB without newline\n'
                'sys.stdout.write("X" * (1100 * 1024) + "\\n")\n'
                'sys.stdout.flush()\n'
                '# Followed by valid measurement\n'
                'sys.stdout.write("CTX: 65\\n")\n'
                'sys.stdout.flush()\n'
            ),
        )
        res = run(
            [sys.executable, '-u', str(script)],
            cwd=str(work_dir),
            env=dict(os.environ),
            stdin_text=None,
            timeout=5.0,
            context_limit=100,
            context_tokens=_default_context_tokens,
        )
        self.assertEqual(res['exit_code'], 0)
        self.assertTrue(res['context_scan_incomplete'])
        self.assertEqual(res['context_peak_tokens'], 65)
        self.assertFalse(res['context_budget_exceeded'])
        # Captured stdout must not be truncated
        self.assertGreater(len(res['stdout']), 1100 * 1024)
        self.assertIn('CTX: 65', res['stdout'])

    def test_timeout_interaction(self):
        work_dir = self._create_retained_work_dir()
        script = self._create_script(
            work_dir,
            'child_timeout.py',
            (
                'import sys, time\n'
                'print("CTX: 40", flush=True)\n'
                'time.sleep(5.0)\n'
            ),
        )
        res = run(
            [sys.executable, '-u', str(script)],
            cwd=str(work_dir),
            env=dict(os.environ),
            stdin_text=None,
            timeout=0.3,
            context_limit=100,
            context_tokens=_default_context_tokens,
        )
        self.assertEqual(res['exit_code'], 124)
        self.assertTrue(res['timed_out'])
        self.assertEqual(res['context_peak_tokens'], 40)
        self.assertFalse(res['context_budget_exceeded'])

    def test_existing_tool_budget_coexistence(self):
        work_dir = self._create_retained_work_dir()

        def tool_events_cb(line):
            line = line.strip()
            if line.startswith('TOOL:'):
                return {line}
            return set()

        # Subtest A: Tool limit exceeded before context limit
        script_a = self._create_script(
            work_dir,
            'child_tool_first.py',
            (
                'import sys, time\n'
                'print("CTX: 30", flush=True)\n'
                'print("TOOL: call_1", flush=True)\n'
                'print("TOOL: call_2", flush=True)\n'
                'time.sleep(5.0)\n'
            ),
        )
        res_a = run(
            [sys.executable, '-u', str(script_a)],
            cwd=str(work_dir),
            env=dict(os.environ),
            stdin_text=None,
            timeout=5.0,
            tool_limit=1,
            tool_events=tool_events_cb,
            context_limit=100,
            context_tokens=_default_context_tokens,
        )
        self.assertEqual(res_a['exit_code'], 125)
        self.assertTrue(res_a['tool_budget_exceeded'])
        self.assertFalse(res_a['context_budget_exceeded'])
        self.assertEqual(res_a['context_peak_tokens'], 30)

        # Subtest B: Context limit exceeded before tool limit
        script_b = self._create_script(
            work_dir,
            'child_context_first.py',
            (
                'import sys, time\n'
                'print("TOOL: call_1", flush=True)\n'
                'print("CTX: 120", flush=True)\n'
                'time.sleep(5.0)\n'
            ),
        )
        res_b = run(
            [sys.executable, '-u', str(script_b)],
            cwd=str(work_dir),
            env=dict(os.environ),
            stdin_text=None,
            timeout=5.0,
            tool_limit=5,
            tool_events=tool_events_cb,
            context_limit=100,
            context_tokens=_default_context_tokens,
        )
        self.assertEqual(res_b['exit_code'], 127)
        self.assertTrue(res_b['context_budget_exceeded'])
        self.assertFalse(res_b['tool_budget_exceeded'])
        self.assertEqual(res_b['context_peak_tokens'], 120)

    def test_unrelated_process_survives(self):
        work_dir = self._create_retained_work_dir()
        script = self._create_script(
            work_dir,
            'child_exceed_unrelated.py',
            (
                'import sys, time\n'
                'print("CTX: 250", flush=True)\n'
                'time.sleep(5.0)\n'
            ),
        )

        unrelated = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(15)'])
        try:
            self.assertIsNone(unrelated.poll())
            res = run(
                [sys.executable, '-u', str(script)],
                cwd=str(work_dir),
                env=dict(os.environ),
                stdin_text=None,
                timeout=5.0,
                context_limit=100,
                context_tokens=_default_context_tokens,
            )
            self.assertEqual(res['exit_code'], 127)
            self.assertTrue(res['context_budget_exceeded'])
            # Verify unrelated process was not killed and survived supervision cleanup
            self.assertIsNone(unrelated.poll())
        finally:
            unrelated.terminate()
            try:
                unrelated.wait(timeout=2.0)
            except subprocess.TimeoutExpired:
                unrelated.kill()
                unrelated.wait(timeout=2.0)


if __name__ == '__main__':
    unittest.main()
