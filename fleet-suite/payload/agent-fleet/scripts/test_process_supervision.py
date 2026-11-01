"""Synthetic CLI process trees; no model or provider calls."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

import fleet
import openclaw_runtime


class ProcessSupervisionTests(unittest.TestCase):
    @staticmethod
    def opencode_step(identity, fresh, cached):
        return {"type": "step_finish", "part": {"type": "step-finish",
                "id": identity, "tokens": {"input": fresh, "output": 1,
                "cache": {"read": cached, "write": 0}}}}

    def test_opencode_cumulative_above_limit_with_individual_steps_below_completes(self):
        first = json.dumps(self.opencode_step("a", 1000, 1500))
        second = json.dumps(self.opencode_step("b", 100, 2600))
        code = (f"import time; print({first!r}, flush=True); "
                f"print({first!r}, flush=True); print({second!r}, flush=True); "
                "time.sleep(0.2); print('fixture-completed', flush=True)")
        result = fleet.run_bounded_child(
            [sys.executable, '-u', '-c', code], cwd=Path.cwd(),
            env=dict(os.environ), stdin_text=None, timeout=8,
            adapter_id='opencode', max_model_tool_calls=None,
            max_context_tokens=4000)
        self.assertEqual(result['exit_code'], 0, result['stderr'])
        self.assertIn('fixture-completed', result['stdout'])
        self.assertEqual(result['context_peak_tokens'], 2700)
        self.assertFalse(result['context_budget_exceeded'])
        self.assertFalse(result['context_scan_incomplete'])

    def test_opencode_unreadable_usage_stops_instead_of_running_unbounded(self):
        code = ("import time; print('{\"type\":\"step_finish\"}', flush=True); "
                "time.sleep(5)")
        result = fleet.run_bounded_child(
            [sys.executable, '-u', '-c', code], cwd=Path.cwd(),
            env=dict(os.environ), stdin_text=None, timeout=8,
            adapter_id='opencode', max_model_tool_calls=None,
            max_context_tokens=4000)
        self.assertEqual(result['exit_code'], 127, result['stderr'])
        self.assertTrue(result['context_scan_stopped'])
        self.assertTrue(result['context_scan_incomplete'])
        self.assertFalse(result['context_budget_exceeded'])

    def test_opencode_oversized_stream_line_stops_safely(self):
        import process_supervision
        code = ("import time; print('x' * "
                f"{process_supervision.MAX_CONTEXT_LINE_CHARS + 1}, flush=True); "
                "time.sleep(5)")
        result = fleet.run_bounded_child(
            [sys.executable, '-u', '-c', code], cwd=Path.cwd(),
            env=dict(os.environ), stdin_text=None, timeout=8,
            adapter_id='opencode', max_model_tool_calls=None,
            max_context_tokens=4000)
        self.assertEqual(result['exit_code'], 127, result['stderr'])
        self.assertTrue(result['context_scan_stopped'])

    def test_opencode_large_tool_result_is_not_misclassified_as_usage(self):
        import process_supervision
        step = json.dumps(self.opencode_step('a', 1000, 500))
        code = ("import json; print(json.dumps({'type':'tool_use','part':"
                "{'state':{'output':'x' * "
                f"{process_supervision.MAX_CONTEXT_LINE_CHARS + 1}" 
                "}}}), flush=True); "
                f"print({step!r}, flush=True)")
        result = fleet.run_bounded_child(
            [sys.executable, '-u', '-c', code], cwd=Path.cwd(),
            env=dict(os.environ), stdin_text=None, timeout=8,
            adapter_id='opencode', max_model_tool_calls=None,
            max_context_tokens=4000)
        self.assertEqual(result['exit_code'], 0, result['stderr'])
        self.assertFalse(result['context_scan_stopped'])
        self.assertFalse(result['context_scan_incomplete'])
        self.assertEqual(result['context_peak_tokens'], 1500)

    def test_openclaw_probe_stops_run_at_persisted_tool_budget(self):
        with tempfile.TemporaryDirectory() as tmp:
            state=openclaw_runtime.acquire_state_dir(Path(tmp),label='tool-budget')
            child_code='''
import json, pathlib, sqlite3, sys, time
db=pathlib.Path(sys.argv[-1])/'agents'/'main'/'agent'/'openclaw-agent.sqlite'
db.parent.mkdir(parents=True)
conn=sqlite3.connect(db)
conn.execute('CREATE TABLE schema_meta(role TEXT, schema_version INTEGER, app_version TEXT)')
conn.execute("INSERT INTO schema_meta VALUES('agent',19,'2026.9.4')")
conn.execute('CREATE TABLE transcript_events(event_json TEXT)')
conn.commit()
for i in range(40):
    event={'type':'message','message':{'role':'assistant','content':[{'type':'toolCall','name':'read'}]}}
    conn.execute('INSERT INTO transcript_events VALUES(?)',(json.dumps(event),))
    conn.commit()
    time.sleep(0.12)
conn.close()
'''
            result=fleet.run_bounded_child(
                [sys.executable,'-u','-c',child_code,'--state-dir',str(state)],
                cwd=Path(tmp),env=dict(os.environ),stdin_text=None,timeout=8,
                adapter_id='openclaw',max_model_tool_calls=3)
            self.assertEqual(result['exit_code'],125, result['stderr'])
            self.assertTrue(result['tool_budget_exceeded'])
            self.assertFalse(result['supervision_failed'])
            self.assertGreater(result['model_tool_calls'],3)

    def test_openclaw_probe_missing_transcript_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            state=openclaw_runtime.acquire_state_dir(Path(tmp),label='missing-db')
            result=fleet.run_bounded_child(
                [sys.executable,'-u','-c',"print('no transcript')",'--state-dir',str(state)],
                cwd=Path(tmp),env=dict(os.environ),stdin_text=None,timeout=5,
                adapter_id='openclaw',max_model_tool_calls=3)
            self.assertEqual(result['exit_code'],126)
            self.assertTrue(result['supervision_failed'])
            self.assertIsNone(result['model_tool_calls'])

    def test_confirmed_over_budget_survives_final_probe_error(self):
        import process_supervision

        calls=0
        def probe():
            nonlocal calls
            calls+=1
            if calls==1:
                return 2
            raise OSError('synthetic transcript lock during cleanup')

        result=process_supervision.run(
            [sys.executable,'-u','-c','import time; time.sleep(5)'],
            cwd=Path.cwd(),env=dict(os.environ),stdin_text=None,timeout=5,
            tool_limit=1,tool_count_probe=probe)
        self.assertTrue(result['tool_budget_exceeded'])
        self.assertEqual(result['exit_code'],125, result['stderr'])
        self.assertEqual(result['model_tool_calls'],2)
        self.assertFalse(result['supervision_failed'])

    def test_launch_observer_and_deadline_work_together(self):
        seen=[]
        def observe(process):
            seen.append(process.pid)
            return {'status':'confirmed'}
        result=fleet.run_bounded_child([sys.executable,'-u','-c',"print('ok')"],
            cwd=Path.cwd(),env=dict(os.environ),stdin_text=None,timeout=5,
            adapter_id='qwen',max_model_tool_calls=None,launch_observer=observe)
        self.assertEqual(len(seen),1)
        self.assertEqual(result['exit_code'],0)
        self.assertEqual(result['launch_protection'],{'status':'confirmed'})
        self.assertGreater(result['execution_seconds'],0)

    def test_failed_launch_observer_does_not_return_success(self):
        def observe(process):
            raise RuntimeError('synthetic observer error')
        result=fleet.run_bounded_child([sys.executable,'-u','-c','import time; time.sleep(5)'],
            cwd=Path.cwd(),env=dict(os.environ),stdin_text=None,timeout=2,
            adapter_id='qwen',max_model_tool_calls=None,launch_observer=observe)
        self.assertEqual(result['exit_code'],126)
        self.assertTrue(result['supervision_failed'])

    def run_child(self, code, timeout=1, stdin_text=None):
        return fleet.run_bounded_child([sys.executable,'-u','-c',code],
            cwd=Path.cwd(),env=dict(os.environ),stdin_text=stdin_text,
            timeout=timeout,adapter_id='minimax',max_model_tool_calls=None)

    def test_normal_success_preserves_stdout_stderr_and_stdin(self):
        result=self.run_child("import sys; print(sys.stdin.read()); print('err',file=sys.stderr)",
                              timeout=5,stdin_text='synthetic-input')
        self.assertEqual(result['exit_code'],0)
        self.assertFalse(result['timed_out'])
        self.assertEqual(result['stdout'],'synthetic-input\n')
        self.assertEqual(result['stderr'],'err\n')

    def test_early_nonzero_exit_is_not_timeout(self):
        result=self.run_child("import sys; print('partial',flush=True); sys.exit(7)",timeout=5)
        self.assertEqual(result['exit_code'],7)
        self.assertFalse(result['timed_out'])
        self.assertIn('partial',result['stdout'])

    def test_timeout_preserves_unterminated_output_and_artifact(self):
        with tempfile.TemporaryDirectory() as tmp:
            artifact=Path(tmp)/'artifact.txt'
            code=f"import pathlib,sys,time; pathlib.Path({str(artifact)!r}).write_text('retained'); sys.stdout.write('partial-no-newline'); sys.stdout.flush(); time.sleep(5)"
            result=self.run_child(code)
            self.assertEqual(result['exit_code'],124)
            self.assertTrue(result['timed_out'])
            self.assertIn('partial-no-newline',result['stdout'])
            self.assertEqual(artifact.read_text(),'retained')

    def test_wrapper_exit_with_grandchild_holding_pipe_is_bounded(self):
        grandchild="import time; print('grandchild',flush=True); time.sleep(5)"
        code=f"import subprocess,sys; subprocess.Popen([sys.executable,'-u','-c',{grandchild!r}]); print('wrapper-exited',flush=True)"
        before=time.monotonic()
        result=self.run_child(code)
        self.assertLess(time.monotonic()-before,3.5)
        self.assertEqual(result['exit_code'],124)
        self.assertTrue(result['timed_out'])
        self.assertIn('wrapper-exited',result['stdout'])
        self.assertIn('grandchild',result['stdout'])

    @unittest.skipUnless(os.name=='nt','Windows wrapper regression')
    def test_windows_cmd_wrapper_timeout_preserves_unrelated_process(self):
        with tempfile.TemporaryDirectory() as tmp:
            script=Path(tmp)/'fixture.py'
            child_pid=Path(tmp)/'grandchild.json'
            leaf=f"import os,time,json,pathlib; pathlib.Path({str(child_pid)!r}).write_text(json.dumps(os.getpid())); print('leaf',flush=True); time.sleep(5)"
            script.write_text(f"import subprocess,sys,time\nsubprocess.Popen([sys.executable,'-u','-c',{leaf!r}])\nprint('parent',flush=True)\ntime.sleep(5)\n")
            unrelated=subprocess.Popen([sys.executable,'-c','import time; time.sleep(15)'],
                stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,
                creationflags=subprocess.CREATE_NO_WINDOW)
            try:
                wrapper=Path(tmp)/'fixture.cmd'
                wrapper.write_text('@"'+sys.executable+'" -u "%~dp0fixture.py"\n')
                command=[str(wrapper)]
                before=time.monotonic()
                result=fleet.run_bounded_child(command,cwd=Path(tmp),env=dict(os.environ),
                    stdin_text=None,timeout=1,adapter_id='minimax',max_model_tool_calls=None)
                self.assertLess(time.monotonic()-before,3.5)
                self.assertEqual(result['exit_code'],124)
                self.assertTrue(result['timed_out'])
                self.assertIsNone(unrelated.poll())
                self.assertIn('leaf',result['stdout'])
                import ctypes
                self.assertTrue(child_pid.exists())
                pid = json.loads(child_pid.read_text())
                handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)
                if handle:
                    try:
                        exit_code = ctypes.c_ulong()
                        self.assertTrue(ctypes.windll.kernel32.GetExitCodeProcess(
                            handle, ctypes.byref(exit_code)))
                        self.assertNotEqual(exit_code.value, 259)  # STILL_ACTIVE
                    finally:
                        ctypes.windll.kernel32.CloseHandle(handle)
            finally:
                unrelated.terminate()
                unrelated.wait(timeout=5)

    def test_stdin_backpressure_is_also_deadline_bounded(self):
        before=time.monotonic()
        result=self.run_child('import time; time.sleep(5)',stdin_text='x'*1000000)
        self.assertLess(time.monotonic()-before,3.5)
        self.assertEqual(result['exit_code'],124)

    @unittest.skipUnless(os.name=='nt','Windows job containment')
    def test_failed_containment_never_starts_cli(self):
        import grok_acp
        with tempfile.TemporaryDirectory() as tmp:
            marker=Path(tmp)/'must-not-exist'
            with mock.patch.object(grok_acp._WindowsJob,'assign',side_effect=RuntimeError('synthetic containment failure')):
                with self.assertRaisesRegex(RuntimeError,'synthetic containment failure'):
                    self.run_child(f"import pathlib; pathlib.Path({str(marker)!r}).touch()")
            self.assertFalse(marker.exists())

    @unittest.skipUnless(os.name=='nt','Windows job cleanup failure')
    def test_close_failure_still_terminates_private_job_and_returns_failure(self):
        import grok_acp
        before=time.monotonic()
        with mock.patch.object(grok_acp._WindowsJob,'close',side_effect=RuntimeError('synthetic CloseHandle failure')):
            result=self.run_child("import time; print('partial',flush=True); time.sleep(5)")
        self.assertLess(time.monotonic()-before,3.5)
        self.assertTrue(result['supervision_failed'])
        self.assertNotEqual(result['exit_code'],0)
        self.assertIn('partial',result['stdout'])

    def test_tool_callback_exception_cannot_report_success(self):
        import process_supervision
        before=time.monotonic()
        def broken_event_parser(line):
            raise ValueError('synthetic parser failure')
        result=process_supervision.run([sys.executable,'-u','-c',
            "import time; print('event',flush=True); time.sleep(5)"],
            cwd=Path.cwd(),env=dict(os.environ),stdin_text=None,timeout=10,
            tool_limit=2,tool_events=broken_event_parser)
        self.assertLess(time.monotonic()-before,3.5)
        self.assertTrue(result['supervision_failed'])
        self.assertEqual(result['exit_code'],126)
        self.assertIn('event',result['stdout'])


if __name__=='__main__':
    unittest.main()
