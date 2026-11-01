"""The isolated Windows contributor process must opt into UTF-8 explicitly."""
import os
from pathlib import Path
import re
import subprocess
import tempfile
import sys
import unittest


class IsolatedEncodingTests(unittest.TestCase):
    def test_launcher_keeps_isolation_and_explicit_utf8(self):
        script = Path(__file__).resolve().parents[1] / 'scripts' / 'knowledge.ps1'
        text = script.read_text(encoding='utf8')
        match = re.search(r"\$arguments = @\(([^\n]+)\)", text)
        self.assertIsNotNone(match)
        self.assertIn("'-I', '-X', 'utf8', '-B'", match.group(1))

    def test_direct_python_gateway_survives_a_legacy_console_code_page(self):
        """SKILL.md documents `python knowledge.py ...`, not only the .ps1 launcher.

        Without an explicit UTF-8 opt-in that path died with exit 2 and
        `'charmap' codec can't encode character '\\u2260'` whenever vault content
        carried a character outside the host code page - which on this machine
        broke `project-brief --project-slug agent-stack` outright.
        """
        gateway = Path(__file__).resolve().parents[1] / 'scripts' / 'knowledge.py'
        env = {**os.environ, 'PYTHONIOENCODING': 'cp1250', 'PYTHONUTF8': '0'}
        # Always against a throwaway vault: without --vault the gateway resolved
        # the operator's live vault through ~/.agents/knowledge-config.json, so the
        # test both depended on that machine and read real content.
        with tempfile.TemporaryDirectory() as temp:
            vault = Path(temp) / 'vault'
            for name in ('wiki', 'proposals', 'raw/sources', 'events'):
                (vault / name).mkdir(parents=True)
            (vault / 'knowledge-vault.json').write_text('{"schema_version": 1}', encoding='utf8')
            result = subprocess.run(
                [sys.executable, '-B', str(gateway), 'inventory', '--query', '≠',
                 '--limit', '1', '--vault', str(vault)],
                env=env, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('≠', result.stdout.decode('utf8'))

    def test_stderr_is_utf8_too_so_error_paths_do_not_die_encoding_the_error(self):
        """force_utf8_streams must cover stderr; errors travel there, not stdout."""
        gateway = Path(__file__).resolve().parents[1] / 'scripts' / 'knowledge.py'
        env = {**os.environ, 'PYTHONIOENCODING': 'cp1250', 'PYTHONUTF8': '0'}
        result = subprocess.run(
            [sys.executable, '-B', str(gateway), 'project-brief',
             '--project-slug', 'niemasz≠takiego', '--vault', 'C:/nie/ma/vaultu≠tutaj'],
            env=env, capture_output=True)
        self.assertNotEqual(result.returncode, 0)
        combined = (result.stdout + result.stderr).decode('utf8')
        self.assertNotIn('charmap', combined)
        self.assertIn('≠', combined)

    def test_non_ascii_output_with_conflicting_environment(self):
        env = {**os.environ, 'PYTHONIOENCODING': 'cp1250', 'PYTHONUTF8': '0'}
        result = subprocess.run([sys.executable, '-I', '-X', 'utf8', '-B', '-c',
                                 "import sys; sys.stdout.write(chr(0x2011) + chr(0x1f680))"],
                                env=env, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.decode('utf8'), '\u2011\U0001f680')


if __name__ == '__main__':
    unittest.main()
