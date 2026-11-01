"""Real Windows PowerShell regression paths, with owned synthetic fixtures only.

The sync tests extract only Assert-LintClean: no remote, fetch, push, or Git
mutation is reachable. Fixtures are retained for inspection, never removed.
"""
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

HUB = Path(__file__).resolve().parents[1]
GATEWAY = HUB / 'skills/llm-wiki/scripts/knowledge.ps1'
SYNC = HUB / 'scripts/sync-owner.ps1'
PS = Path(os.environ.get('SystemRoot', r'C:\Windows')) / 'System32/WindowsPowerShell/v1.0/powershell.exe'

@unittest.skipUnless(os.name == 'nt' and PS.is_file(), 'Windows PowerShell required')
class WindowsGatewayTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root = Path(tempfile.mkdtemp(prefix='kh-wrapper-regression-'))
        cls.scripts = cls.root / 'trusted scripts'
        cls.scripts.mkdir()
        cls.vault = cls.root / 'synthetic vault'
        cls.vault.mkdir()
        (cls.vault/'knowledge-vault.json').write_text('{}', encoding='utf-8')
        shutil.copyfile(GATEWAY, cls.scripts/'knowledge.ps1')
        (cls.scripts/'registry_v2_lint.py').write_text("VALUE = 'trusted sibling'\n", encoding='utf-8')
        (cls.root/'registry_v2_lint.py').write_text("raise RuntimeError('CWD module loaded')\n", encoding='utf-8')
        (cls.scripts/'knowledge.py').write_text('''import json, sys
from registry_v2_lint import VALUE
print(json.dumps({'action':sys.argv[1], 'argv':sys.argv[1:], 'isolated':sys.flags.isolated, 'ignore_environment':sys.flags.ignore_environment, 'value':VALUE}))
raise SystemExit(7 if '--project-slug' in sys.argv and 'missing' in sys.argv else 0)
''', encoding='utf-8')
        cls.guard = cls.root/'guard-only.ps1'
        cls.guard.write_text('''param([string]$Source, [string]$Gateway, [string]$Vault)
$ErrorActionPreference = 'Stop'
$gateway = $Gateway
$vault = $Vault
$tokens = $null; $errors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile($Source, [ref]$tokens, [ref]$errors)
if ($errors.Count) { throw 'Invalid source syntax' }
$function = $ast.Find({param($node) $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $node.Name -eq 'Assert-LintClean'}, $true)
if (-not $function) { throw 'Missing guard' }
. ([ScriptBlock]::Create($function.Extent.Text))
function Invoke-Git { throw 'Git must never run in this test' }
Assert-LintClean
Write-Output 'PASS'
''', encoding='utf-8')

    def run_ps(self, *args):
        env = dict(os.environ, PYTHONPATH=str(self.root), PYTHONHOME=str(self.root/'untrusted-python-home'))
        return subprocess.run([str(PS), '-NoLogo', '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass', *map(str,args)], cwd=self.root, env=env, capture_output=True, text=True, encoding='utf-8', timeout=45, creationflags=subprocess.CREATE_NO_WINDOW)

    def test_isolated_gateway_imports_trusted_sibling(self):
        result = self.run_ps('-File', self.scripts/'knowledge.ps1', '-Action', 'lint', '-Vault', self.vault)
        self.assertEqual(result.returncode, 0, result.stderr)
        data = json.loads(result.stdout)
        self.assertEqual(data['value'], 'trusted sibling')
        self.assertEqual(data['isolated'], 1)
        self.assertEqual(data['ignore_environment'], 1)
        self.assertEqual(data['argv'], ['lint', '--vault', str(self.vault)])

    def test_gateway_preserves_nonzero_project_brief_exit(self):
        result = self.run_ps('-File', self.scripts/'knowledge.ps1', '-Action', 'project-brief', '-ProjectSlug', 'missing', '-Vault', self.vault)
        self.assertEqual(result.returncode, 7, result.stderr)
        self.assertEqual(json.loads(result.stdout)['action'], 'project-brief')

    def guard_result(self, text, exit_code=0):
        stub = self.root/f'lint-{len(list(self.root.glob("lint-*.ps1")))}.ps1'
        stub.write_text("[Console]::Write('"+text.replace("'", "''")+"')\nexit "+str(exit_code)+"\n", encoding='utf-8')
        return self.run_ps('-File', self.guard, '-Source', SYNC, '-Gateway', stub, '-Vault', self.vault)

    def test_guard_accepts_only_valid_clean_result(self):
        result = self.guard_result('{"error_count":0,"warning_count":0}')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('PASS', result.stdout)

    def test_guard_rejects_failures_and_bad_shapes(self):
        cases = [('nonzero', '{"error_count":0,"warning_count":0}', 7), ('empty', '', 0), ('invalid', 'not-json', 0), ('missing', '{}', 0), ('null', 'null', 0), ('array', '[{"error_count":0,"warning_count":0}]', 0), ('string', '{"error_count":"0","warning_count":0}', 0), ('boolean', '{"error_count":false,"warning_count":0}', 0), ('negative', '{"error_count":-1,"warning_count":0}', 0), ('errors', '{"error_count":1,"warning_count":0}', 0), ('warnings', '{"error_count":0,"warning_count":1}', 0)]
        for label, text, exit_code in cases:
            with self.subTest(label=label):
                result = self.guard_result(text, exit_code)
                self.assertNotEqual(result.returncode, 0, label+' accepted')
                self.assertNotIn('PASS', result.stdout)

if __name__ == '__main__':
    unittest.main(verbosity=2)
