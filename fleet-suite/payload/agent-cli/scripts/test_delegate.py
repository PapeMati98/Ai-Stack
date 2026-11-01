"""Exercise the PowerShell facade with a fake Python function, never a provider."""
import base64
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


class FacadeTests(unittest.TestCase):
    def invoke(self, extra=""):
        shell = shutil.which("pwsh") or shutil.which("powershell")
        if not shell:
            self.skipTest("PowerShell unavailable")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fleet = root / "agent-fleet/scripts/fleet.py"
            fleet.parent.mkdir(parents=True)
            fleet.write_text("# never executed", encoding="utf-8")
            script = str(Path(__file__).with_name("delegate.ps1")).replace("'", "''")
            cwd = str(root).replace("'", "''")
            command = (
                "function global:python { $global:LASTEXITCODE=0; "
                "ConvertTo-Json -InputObject @($args) -Compress }; "
                f"& '{script}' -Agent grok -Prompt 'test only' -Cwd '{cwd}' {extra}"
            )
            done = subprocess.run(
                [shell, "-NoProfile", "-NonInteractive", "-EncodedCommand",
                 base64.b64encode(command.encode("utf-16-le")).decode("ascii")],
                env=dict(os.environ, AGENT_FLEET_SUITE_ROOT=str(root)),
                capture_output=True, encoding="utf-8", errors="replace", timeout=30,
            )
            return done

    def test_required_inputs_forwarded_with_single_fleet_call(self):
        done = self.invoke("-RequiredInputsFile 'inputs manifest.json' -DryRun")
        self.assertEqual(done.returncode, 0, done.stderr)
        args = json.loads(done.stdout)
        self.assertEqual(args[1], "run")
        index = args.index("--required-inputs-file")
        self.assertTrue(args[index + 1].endswith("inputs manifest.json"))
        self.assertIn("--dry-run", args)
        self.assertNotIn("check", args)

    def test_implementation_still_requires_explicit_write_permission(self):
        done = self.invoke("-Mode implement")
        self.assertNotEqual(done.returncode, 0)
        self.assertIn("requires -AllowWrite", done.stderr)


if __name__ == "__main__":
    unittest.main()
