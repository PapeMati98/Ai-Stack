"""Regression coverage for the native command actually submitted to Muse."""
import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, os.environ.get('TEST_MUSE_FLEET_SCRIPTS', str(Path(__file__).resolve().parent)))
import fleet


MUSE_EXE = 'C:/synthetic-fixture/tools/muse/1.4.6/muse.exe'


class MuseContextControls(unittest.TestCase):
    def command(self, mode, complexity, override=None):
        command, stdin, final = fleet.build_command(
            'muse', MUSE_EXE,
            prompt='Synthetic fixture only.', workspace=Path(__file__).resolve().parent,
            mode=mode, model='muse-spark-1.3', complexity=complexity, timeout=30,
            run_dir=Path(__file__).resolve().parent / 'not-materialized',
            native_subagent_limit=0, enable_web=False, materialize=False,
            max_model_tool_calls=override)
        return command

    def value(self, command, flag):
        self.assertEqual(command.count(flag), 1, 'Each native option must occur exactly once')
        return command[command.index(flag)+1]

    def test_all_modes_have_bounded_native_context_and_tool_output(self):
        for mode in ('implement', 'advisory', 'review'):
            for complexity in ('low', 'medium', 'high'):
                with self.subTest(mode=mode, complexity=complexity):
                    command=self.command(mode, complexity)
                    self.assertEqual(self.value(command, '--context-compaction-strategy'), 'summary-preserved-suffix/v1')
                    soft=float(self.value(command, '--context-compaction-soft-threshold'))
                    hard=float(self.value(command, '--context-compaction-hard-threshold'))
                    self.assertGreater(soft, 0)
                    self.assertLess(soft, hard)
                    self.assertLessEqual(hard, 0.20, 'Do not wait for most of the million-token context to fill')
                    self.assertLessEqual(int(self.value(command, '--max-tool-output-bytes')), 65536)

    def test_legacy_dispatch_without_override_cannot_silently_expand_to_512_steps(self):
        for complexity in ('low', 'medium', 'high'):
            with self.subTest(complexity=complexity):
                self.assertLessEqual(int(self.value(self.command('implement', complexity), '--max-model-steps')), 72)

    def test_explicit_task_budget_stays_exact(self):
        for limit in (1, 6, 48, 96, 241):
            with self.subTest(limit=limit):
                self.assertEqual(self.value(self.command('implement', 'high', limit), '--max-model-steps'), str(limit))

    def test_compaction_does_not_change_identity_privacy_or_readonly_permissions(self):
        for mode in ('implement', 'advisory', 'review'):
            with self.subTest(mode=mode):
                command=self.command(mode,'medium')
                self.assertEqual(self.value(command,'--model'),'muse-spark-1.3')
                self.assertEqual(self.value(command,'--provider'),'meta')
                self.assertEqual(self.value(command,'--contributor-mode'),'off')
                self.assertEqual(self.value(command,'--sandbox-network'),'restricted')
                self.assertIn('--no-foreign-personal-context', command)
                self.assertIn('--disable-web-tools', command)
                self.assertNotIn('--yolo', command)
                self.assertNotIn('--api-key-stdin', command)
                if mode != 'implement':
                    self.assertIn('--disable-write', command)
                    self.assertIn('--disable-shell', command)


if __name__=='__main__':
    unittest.main()
