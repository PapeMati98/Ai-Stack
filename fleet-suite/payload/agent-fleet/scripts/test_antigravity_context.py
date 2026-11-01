"""Tests for the bounded Antigravity numeric context parser candidate."""
import os
from pathlib import Path
import sys
import tempfile
import unittest

CANDIDATE_DIR = Path(__file__).resolve().parent
if str(CANDIDATE_DIR) not in sys.path:
    sys.path.insert(0, str(CANDIDATE_DIR))

from antigravity_context import measure_steps, stream_context_tokens
from process_supervision import ContextEventUnreadable, run


def _done(conversation_id, step_index, input_tokens, cache_read_tokens):
    return {
        'event': 'step_update',
        'step_update': {
            'conversation_id': conversation_id,
            'step_index': step_index,
            'step_type': 'agent_response',
            'state': 'DONE',
            'usage': {'input_tokens': input_tokens, 'cache_read_tokens': cache_read_tokens},
        },
    }


def _progress(conversation_id, step_index, usage=None):
    step_update = {
        'conversation_id': conversation_id,
        'step_index': step_index,
        'step_type': 'agent_response',
        'state': 'IN_PROGRESS',
    }
    if usage is not None:
        step_update['usage'] = usage
    return {'event': 'step_update', 'step_update': step_update}


class TestMeasureSteps(unittest.TestCase):
    def test_first_last_max_two_steps(self):
        events = [_done('c1', 0, 100, 10), _done('c1', 1, 500, 50)]
        result = measure_steps(events)
        self.assertEqual(result['steps'], [110, 550])
        self.assertFalse(result['incomplete'])
        self.assertIsNone(result['tool_result_bytes'])
        self.assertIsNone(result['compaction_events'])

    def test_duplicate_progress_and_done_retain_max(self):
        events = [
            _progress('c1', 0, {'input_tokens': 50, 'cache_read_tokens': 0}),
            _progress('c1', 0, {'input_tokens': 200, 'cache_read_tokens': 0}),
            _done('c1', 0, 120, 0),
        ]
        result = measure_steps(events)
        self.assertEqual(result['steps'], [200])
        self.assertFalse(result['incomplete'])

    def test_same_step_index_two_conversations(self):
        events = [_done('c1', 0, 100, 0), _done('c2', 0, 300, 0)]
        result = measure_steps(events)
        self.assertEqual(result['steps'], [100, 300])

    def test_missing_counter_taints_but_keeps_known(self):
        events = [
            _done('c1', 0, 100, 0),
            {
                'event': 'step_update',
                'step_update': {
                    'conversation_id': 'c1',
                    'step_index': 1,
                    'step_type': 'agent_response',
                    'state': 'DONE',
                    'usage': {'input_tokens': 100},
                },
            },
        ]
        result = measure_steps(events)
        self.assertEqual(result['steps'], [100])
        self.assertTrue(result['incomplete'])

    def test_bool_counter_invalid(self):
        events = [_done('c1', 0, True, 5)]
        result = measure_steps(events)
        self.assertEqual(result['steps'], [])
        self.assertTrue(result['incomplete'])

    def test_negative_counter_invalid(self):
        events = [_done('c1', 0, -1, 5)]
        result = measure_steps(events)
        self.assertTrue(result['incomplete'])

    def test_string_counter_invalid(self):
        events = [_done('c1', 0, '100', 5)]
        result = measure_steps(events)
        self.assertTrue(result['incomplete'])

    def test_oversized_counter_invalid(self):
        events = [_done('c1', 0, 10 ** 12 + 1, 0)]
        result = measure_steps(events)
        self.assertTrue(result['incomplete'])

    def test_sum_overflow_invalid(self):
        events = [_done('c1', 0, 10 ** 12, 10 ** 12)]
        result = measure_steps(events)
        self.assertTrue(result['incomplete'])

    def test_bad_identity_missing_conversation_id(self):
        events = [{
            'event': 'step_update',
            'step_update': {
                'step_index': 0,
                'step_type': 'agent_response',
                'state': 'DONE',
                'usage': {'input_tokens': 10, 'cache_read_tokens': 0},
            },
        }]
        result = measure_steps(events)
        self.assertEqual(result['steps'], [])
        self.assertTrue(result['incomplete'])

    def test_bad_step_index_negative(self):
        events = [_done('c1', -1, 10, 0)]
        result = measure_steps(events)
        self.assertTrue(result['incomplete'])

    def test_unrelated_tool_init_aggregate_ignored(self):
        events = [
            {'event': 'init'},
            {'event': 'step_update', 'step_update': {'step_type': 'tool_call'}},
            {'event': 'result', 'usage': {'input_tokens': 999999, 'cache_read_tokens': 0}},
        ]
        result = measure_steps(events)
        self.assertEqual(result['steps'], [])
        self.assertFalse(result['incomplete'])

    def test_progress_missing_usage_ignored_not_incomplete(self):
        events = [_progress('c1', 0)]
        result = measure_steps(events)
        self.assertEqual(result['steps'], [])
        self.assertFalse(result['incomplete'])


class TestStreamContextTokens(unittest.TestCase):
    def test_done_step_returns_total(self):
        import json
        line = json.dumps(_done('c1', 0, 100, 20))
        self.assertEqual(stream_context_tokens(line), 120)

    def test_progress_with_usage_validated(self):
        import json
        line = json.dumps(_progress('c1', 0, {'input_tokens': 30, 'cache_read_tokens': 0}))
        self.assertEqual(stream_context_tokens(line), 30)

    def test_progress_missing_usage_ignored(self):
        import json
        line = json.dumps(_progress('c1', 0))
        self.assertIsNone(stream_context_tokens(line))

    def test_aggregate_result_never_a_step(self):
        import json
        line = json.dumps({'event': 'result', 'usage': {'input_tokens': 1, 'cache_read_tokens': 1}})
        self.assertIsNone(stream_context_tokens(line))

    def test_relevant_malformed_terminal_raises(self):
        import json
        line = json.dumps(_done('c1', 0, 'not_an_int', 0))
        with self.assertRaises(ContextEventUnreadable):
            stream_context_tokens(line)

    def test_torn_json_raises(self):
        line = '{"event": "step_update", "step_update": {"state": "DONE"'
        with self.assertRaises(ContextEventUnreadable):
            stream_context_tokens(line)

    def test_non_json_log_ignored(self):
        line = 'this is a plain log line, not json at all'
        self.assertIsNone(stream_context_tokens(line))

    def test_blank_line_ignored(self):
        self.assertIsNone(stream_context_tokens(''))

    def test_init_ignored(self):
        import json
        self.assertIsNone(stream_context_tokens(json.dumps({'event': 'init'})))


class TestRealPrivateSyntheticChild(unittest.TestCase):
    def _fixtures_root(self):
        fixtures_root = CANDIDATE_DIR / 'fixtures'
        fixtures_root.mkdir(exist_ok=True)
        return Path(tempfile.mkdtemp(dir=str(fixtures_root), prefix='antigravity_context_'))

    def _write_script(self, work_dir, name, code):
        script_path = work_dir / name
        script_path.write_text(code, encoding='utf-8')
        return script_path

    def test_real_child_stops_after_observed_excess(self):
        work_dir = self._fixtures_root()
        script = self._write_script(
            work_dir,
            'child_antigravity_exceed.py',
            (
                'import json, sys, time\n'
                'from pathlib import Path\n'
                'Path("work.txt").write_text("retained work", encoding="utf-8")\n'
                'print(json.dumps({"event": "init"}), flush=True)\n'
                'print(json.dumps({"event": "step_update", "step_update": {'
                '"conversation_id": "c1", "step_index": 0, "step_type": "agent_response",'
                ' "state": "DONE", "usage": {"input_tokens": 1000, "cache_read_tokens": 0}}}),'
                ' flush=True)\n'
                'print(json.dumps({"event": "step_update", "step_update": {'
                '"conversation_id": "c1", "step_index": 1, "step_type": "agent_response",'
                ' "state": "DONE", "usage": {"input_tokens": 214831, "cache_read_tokens": 0}}}),'
                ' flush=True)\n'
                'time.sleep(1.0)\n'
                'Path("late.txt").write_text("late marker", encoding="utf-8")\n'
            ),
        )
        res = run(
            [sys.executable, '-u', str(script)],
            cwd=str(work_dir),
            env=dict(os.environ),
            stdin_text=None,
            timeout=5.0,
            context_limit=200000,
            context_tokens=stream_context_tokens,
        )
        self.assertEqual(res['exit_code'], 127)
        self.assertTrue(res['context_budget_exceeded'])
        self.assertEqual(res['context_peak_tokens'], 214831)
        self.assertFalse(res['context_scan_incomplete'])
        self.assertFalse((work_dir / 'late.txt').exists())
        self.assertTrue((work_dir / 'work.txt').is_file())
        self.assertEqual((work_dir / 'work.txt').read_text(encoding='utf-8'), 'retained work')
        self.assertIn('step_update', res['stdout'])

    def test_exactly_at_limit_not_stopped(self):
        work_dir = self._fixtures_root()
        script = self._write_script(
            work_dir,
            'child_antigravity_boundary.py',
            (
                'import json, sys\n'
                'print(json.dumps({"event": "step_update", "step_update": {'
                '"conversation_id": "c1", "step_index": 0, "step_type": "agent_response",'
                ' "state": "DONE", "usage": {"input_tokens": 200000, "cache_read_tokens": 0}}}),'
                ' flush=True)\n'
                'print("boundary completed", flush=True)\n'
            ),
        )
        res = run(
            [sys.executable, '-u', str(script)],
            cwd=str(work_dir),
            env=dict(os.environ),
            stdin_text=None,
            timeout=5.0,
            context_limit=200000,
            context_tokens=stream_context_tokens,
        )
        self.assertEqual(res['exit_code'], 0)
        self.assertFalse(res['context_budget_exceeded'])
        self.assertEqual(res['context_peak_tokens'], 200000)
        self.assertIn('boundary completed', res['stdout'])

    def test_unrelated_child_survives(self):
        import subprocess
        work_dir = self._fixtures_root()
        script = self._write_script(
            work_dir,
            'child_antigravity_exceed_unrelated.py',
            (
                'import json, sys, time\n'
                'print(json.dumps({"event": "step_update", "step_update": {'
                '"conversation_id": "c1", "step_index": 0, "step_type": "agent_response",'
                ' "state": "DONE", "usage": {"input_tokens": 250000, "cache_read_tokens": 0}}}),'
                ' flush=True)\n'
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
                context_limit=200000,
                context_tokens=stream_context_tokens,
            )
            self.assertEqual(res['exit_code'], 127)
            self.assertTrue(res['context_budget_exceeded'])
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
