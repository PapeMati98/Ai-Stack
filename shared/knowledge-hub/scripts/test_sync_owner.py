import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
SYNC_SCRIPT = HERE / "sync-owner.ps1"


def run_ps_assert_clean_main(repo_root: Path) -> subprocess.CompletedProcess:
    # Script snippet that defines functions from sync-owner.ps1 and calls Assert-CleanMain
    ps_code = f"""
    $hub = "{str(repo_root).replace('\\', '/')}"
    $ErrorActionPreference = 'Stop'
    
    function Invoke-Git {{
        param([string[]]$Arguments, [switch]$AllowFailure)
        $previousErrorAction = $ErrorActionPreference
        try {{
            $ErrorActionPreference = 'Continue'
            $output = @(& git -c "safe.directory=$hub" -C $hub @Arguments 2>&1)
            $exitCode = $LASTEXITCODE
        }}
        finally {{
            $ErrorActionPreference = $previousErrorAction
        }}
        if ($exitCode -ne 0 -and -not $AllowFailure) {{
            throw "git $($Arguments -join ' ') failed ($exitCode): $($output -join [Environment]::NewLine)"
        }}
        return [PSCustomObject]@{{ ExitCode = $exitCode; Output = $output }}
    }}
    
    function Test-FreshIntakePath {{
        param([string]$Path)
        $normalized = $Path.Trim('"').Replace('\\', '/')
        return (
            $normalized -like 'vault/events/*' -or
            $normalized -like 'vault/proposals/*' -or
            $normalized -like 'vault/raw/sources/*'
        )
    }}

    function Assert-CleanMain {{
        $branch = (Invoke-Git -Arguments @('symbolic-ref', '--quiet', '--short', 'HEAD')).Output[0]
        if ([string]$branch -ne 'main') {{ throw "Owner sync requires branch main, got '$branch'." }}
        $changes = @(Invoke-Git -Arguments @('status', '--porcelain=v1', '--untracked-files=all')).Output
        $blocking = @()
        foreach ($line in $changes) {{
            if ([string]::IsNullOrWhiteSpace($line)) {{ continue }}
            if ($line.Length -lt 4) {{ $blocking += $line; continue }}
            $statusCode = $line.Substring(0, 2)
            $relPath = $line.Substring(3).Trim()
            if ($statusCode -eq '??' -and (Test-FreshIntakePath -Path $relPath)) {{
                continue
            }}
            $blocking += $line
        }}
        if ($blocking.Count -gt 0) {{
            throw "Owner sync requires a clean worktree; found $($blocking.Count) change(s): $($blocking -join ', ')."
        }}
    }}
    
    Assert-CleanMain
    """
    return subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", ps_code],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace"
    )


class TestSyncOwnerCleanMain(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.repo = Path(self.temp_dir.name)
        # git init -b main
        subprocess.run(["git", "init", "-b", "main", str(self.repo)], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(self.repo), "config", "user.email", "test@test.com"], check=True)
        subprocess.run(["git", "-C", str(self.repo), "config", "user.name", "Tester"], check=True)
        
        # Initial commit
        (self.repo / "README.md").write_text("initial", encoding="utf-8")
        subprocess.run(["git", "-C", str(self.repo), "add", "."], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(self.repo), "commit", "-m", "init"], check=True, capture_output=True)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_clean_main_passes(self):
        proc = run_ps_assert_clean_main(self.repo)
        self.assertEqual(proc.returncode, 0, f"Expected success on clean repo, got: {proc.stderr}")

    def test_untracked_fresh_intake_is_ignored(self):
        # Create fresh intake paths
        (self.repo / "vault" / "events" / "2026" / "10").mkdir(parents=True, exist_ok=True)
        (self.repo / "vault" / "events" / "2026" / "10" / "evt-123.json").write_text("{}", encoding="utf-8")
        
        (self.repo / "vault" / "proposals" / "claude").mkdir(parents=True, exist_ok=True)
        (self.repo / "vault" / "proposals" / "claude" / "prop-123.md").write_text("# Proposal", encoding="utf-8")
        
        (self.repo / "vault" / "raw" / "sources" / "2026" / "10" / "src-123").mkdir(parents=True, exist_ok=True)
        (self.repo / "vault" / "raw" / "sources" / "2026" / "10" / "src-123" / "source.json").write_text("{}", encoding="utf-8")
        
        proc = run_ps_assert_clean_main(self.repo)
        self.assertEqual(proc.returncode, 0, f"Expected fresh intake to be ignored, but failed: {proc.stderr}")

    def test_untracked_non_intake_file_blocks(self):
        (self.repo / "vault" / "wiki" / "projects").mkdir(parents=True, exist_ok=True)
        (self.repo / "vault" / "wiki" / "projects" / "test.md").write_text("wiki content", encoding="utf-8")
        
        proc = run_ps_assert_clean_main(self.repo)
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("found 1 change(s)", proc.stderr)
        self.assertIn("vault/wiki/projects/test.md", proc.stderr)

    def test_modified_tracked_file_blocks_even_in_intake(self):
        # Commit a file in vault/events
        event_dir = self.repo / "vault" / "events" / "2026" / "10"
        event_dir.mkdir(parents=True, exist_ok=True)
        event_file = event_dir / "evt-tracked.json"
        event_file.write_text('{"version": 1}', encoding="utf-8")
        subprocess.run(["git", "-C", str(self.repo), "add", "."], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(self.repo), "commit", "-m", "add event"], check=True, capture_output=True)
        
        # Modify it without committing
        event_file.write_text('{"version": 2}', encoding="utf-8")
        
        proc = run_ps_assert_clean_main(self.repo)
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("found 1 change(s)", proc.stderr)
        self.assertIn("evt-tracked.json", proc.stderr)


if __name__ == "__main__":
    unittest.main()
