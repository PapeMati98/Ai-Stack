"""Exercise the actual CLI ordering, without any network or production reads."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


class EntrypointTests(unittest.TestCase):
    def test_pre_17_existing_database_can_start_from_cli(self):
        with tempfile.TemporaryDirectory(prefix='openclaw-entrypoint-') as directory:
            root = Path(directory)
            database = root / 'fixture.db'
            database.touch()
            evidence = root / 'evidence.json'
            script = Path(__file__).with_name('helpdesk_tick.py')
            code = (
                'import runpy,sys,time; '
                'time.time=lambda:1789700400.0; '
                'sys.path.insert(0,sys.argv[1]); '
                'import helpdesk_read; '
                'fixture=lambda p:{"status":"OK","environment":"prod","project":p,"read_only":True,"items":[]}; '
                'helpdesk_read.crm=lambda:fixture("example-crm"); '
                'helpdesk_read.workspace=lambda:fixture("example-workspace"); '
                'script=sys.argv[2]; sys.argv=sys.argv[2:]; '
                'runpy.run_path(script,run_name="__main__")'
            )
            result = subprocess.run(
                [sys.executable, '-X', 'utf8', '-B', '-c', code,
                 str(script.parent), str(script), '--db', str(database),
                 '--evidence', str(evidence)],
                capture_output=True, text=True, encoding='utf-8', timeout=20)
            self.assertEqual(result.returncode, 0, result.stderr)
            report = json.loads(evidence.read_text(encoding='utf-8'))
            self.assertEqual(report['status'], 'CATCHUP_COLLECTED')
            self.assertEqual(report['pending_requests'], 0)


if __name__ == '__main__':
    unittest.main()
