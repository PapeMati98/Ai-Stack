from __future__ import annotations

import argparse
import ast
import hashlib
import io
import importlib.util
import json
import re
import subprocess
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock


MODULE_PATH = (
    Path(__file__).resolve().parents[1] / "scripts" / "codex_security_safe.py"
)
SPEC = importlib.util.spec_from_file_location("codex_security_safe", MODULE_PATH)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(MODULE)


class CodexSecuritySafeTests(unittest.TestCase):
    def make_repository(self) -> tuple[tempfile.TemporaryDirectory, Path]:
        temporary = tempfile.TemporaryDirectory(dir=Path.cwd())
        repository = Path(temporary.name) / "repo"
        repository.mkdir()
        (repository / "app.py").write_text("print('ok')\n", encoding="utf-8")
        return temporary, repository

    def test_runtime_is_exact_and_integral(self):
        runtime = MODULE.verify_runtime()
        self.assertEqual(runtime["manifest"]["version"], "0.1.1")
        self.assertEqual(runtime["fileCount"], 178)
        self.assertEqual(
            runtime["nodeSha256"],
            "3331e1ffe19874215472217c5e94f5a0c6d8e18c4ac7111d3937aa0ad5e9b4a5",
        )

    def test_package_tree_hash_matches_runtime_manifest_and_installer(self):
        skill_root = Path(__file__).resolve().parents[1]
        manifest = json.loads(
            (skill_root / "references" / "runtime-manifest.json").read_text(
                encoding="utf-8"
            )
        )
        package_root = (
            MODULE.runtime_root()
            / "node_modules"
            / "@openai"
            / "codex-security"
        )
        digest = hashlib.sha256()
        files = sorted(
            (path for path in package_root.rglob("*") if path.is_file()),
            key=lambda path: path.relative_to(package_root).as_posix(),
        )
        for path in files:
            relative = path.relative_to(package_root).as_posix()
            digest.update(relative.encode("utf-8") + b"\0")
            digest.update(path.read_bytes())
            digest.update(b"\0")
        actual = digest.hexdigest()

        installer_path = skill_root / "scripts" / "install_runtime.ps1"
        installer = installer_path.read_text(encoding="utf-8")
        match = re.search(
            r'\$ExpectedTreeHash\s*=\s*"([0-9a-f]{64})"',
            installer,
        )
        self.assertIsNotNone(match)
        self.assertEqual(actual, manifest["packageTreeSha256"])
        self.assertEqual(actual, match.group(1))

        powershell = f"""
$tokens = $null
$errors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile(
    '{installer_path.as_posix()}',
    [ref]$tokens,
    [ref]$errors
)
if ($errors) {{ throw 'Nie mozna sparsowac instalatora.' }}
$function = $ast.Find(
    {{ param($node) $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $node.Name -eq 'Get-TreeSha256' }},
    $true
)
Invoke-Expression $function.Extent.Text
Get-TreeSha256 -Root '{package_root.as_posix()}'
"""
        completed = subprocess.run(
            ["powershell", "-NoProfile", "-Command", powershell],
            check=True,
            capture_output=True,
            text=True,
        )
        self.assertEqual(
            completed.stdout.strip().splitlines()[-1],
            manifest["packageTreeSha256"],
        )

    def test_drive_root_is_rejected(self):
        root = Path(Path.cwd().anchor)
        with self.assertRaises(MODULE.PolicyError):
            MODULE.validate_repository(str(root))

    def test_scope_cannot_escape_repository(self):
        temporary, repository = self.make_repository()
        self.addCleanup(temporary.cleanup)
        with self.assertRaises(MODULE.PolicyError):
            MODULE.validate_scopes(repository, [".."])

    def test_absolute_scope_is_rejected(self):
        temporary, repository = self.make_repository()
        self.addCleanup(temporary.cleanup)
        with self.assertRaises(MODULE.PolicyError):
            MODULE.validate_scopes(repository, [str(repository / "app.py")])

    def test_direct_skipped_directories_are_rejected(self):
        temporary, repository = self.make_repository()
        self.addCleanup(temporary.cleanup)
        for directory in (".git", ".venv", "node_modules"):
            with self.subTest(directory=directory):
                with self.assertRaises(MODULE.PolicyError):
                    MODULE.validate_scopes(repository, [directory])

    def test_repository_below_skipped_directory_is_rejected(self):
        temporary, repository = self.make_repository()
        self.addCleanup(temporary.cleanup)
        nested = repository / "node_modules" / "package"
        nested.mkdir(parents=True)
        with self.assertRaises(MODULE.PolicyError):
            MODULE.validate_repository(str(nested))

    def test_repository_reparse_point_is_rejected(self):
        temporary, repository = self.make_repository()
        self.addCleanup(temporary.cleanup)
        with mock.patch.object(
            MODULE,
            "is_reparse_point",
            side_effect=lambda path: Path(path) == repository,
        ):
            with self.assertRaises(MODULE.PolicyError):
                MODULE.validate_repository(str(repository))

    def test_case_insensitive_skipped_directory_is_not_traversed(self):
        temporary, repository = self.make_repository()
        self.addCleanup(temporary.cleanup)
        skipped = repository / "Node_Modules"
        skipped.mkdir()
        (skipped / ".env").write_text("TOKEN=secret\n", encoding="utf-8")
        files = {
            path.relative_to(repository).as_posix()
            for path in MODULE.iter_scan_files(repository, [])
        }
        self.assertEqual(files, {"app.py"})

    def test_reparse_directory_is_not_traversed(self):
        temporary, repository = self.make_repository()
        self.addCleanup(temporary.cleanup)
        linked = repository / "linked"
        linked.mkdir()
        (linked / ".env").write_text("TOKEN=secret\n", encoding="utf-8")
        with mock.patch.object(
            MODULE,
            "is_reparse_point",
            side_effect=lambda path: Path(path) == linked,
        ):
            files = {
                path.relative_to(repository).as_posix()
                for path in MODULE.iter_scan_files(repository, [])
            }
        self.assertEqual(files, {"app.py"})

    def test_secret_file_blocks_scan(self):
        temporary, repository = self.make_repository()
        self.addCleanup(temporary.cleanup)
        (repository / ".env").write_text("EXAMPLE=not-a-real-secret\n", encoding="utf-8")
        with self.assertRaises(MODULE.PolicyError):
            MODULE.check_repository_inputs(repository, [])

    def test_env_example_is_allowed(self):
        temporary, repository = self.make_repository()
        self.addCleanup(temporary.cleanup)
        (repository / ".env.example").write_text("TOKEN=\n", encoding="utf-8")
        self.assertEqual(MODULE.find_blocked_files(repository, []), [])

    def test_high_confidence_secret_content_blocks_scan_without_echoing_value(self):
        temporary, repository = self.make_repository()
        self.addCleanup(temporary.cleanup)
        secret = "hf_" + "A" * 30
        (repository / "config.txt").write_text(secret, encoding="utf-8")
        with self.assertRaises(MODULE.PolicyError) as caught:
            MODULE.check_repository_inputs(repository, [])
        self.assertIn("config.txt [huggingface-token]", str(caught.exception))
        self.assertNotIn(secret, str(caught.exception))

    def test_placeholder_content_is_not_treated_as_secret(self):
        temporary, repository = self.make_repository()
        self.addCleanup(temporary.cleanup)
        (repository / "config.txt").write_text(
            "OPENAI_API_KEY=<YOUR_API_KEY>\nTOKEN=example\n",
            encoding="utf-8",
        )
        self.assertEqual(MODULE.find_blocked_content(repository, []), [])

    def test_restricted_cloud_scan_is_rejected(self):
        args = argparse.Namespace(
            allow_cloud_code_upload=True,
            data_classification="restricted",
            confirm_confidential=False,
            max_cost_usd=1.0,
        )
        with self.assertRaises(MODULE.PolicyError):
            MODULE.validate_cloud_gate(args)

    def test_cloud_scan_without_explicit_consent_is_rejected(self):
        args = argparse.Namespace(
            allow_cloud_code_upload=False,
            data_classification="internal",
            confirm_confidential=False,
            max_cost_usd=1.0,
        )
        with self.assertRaises(MODULE.PolicyError):
            MODULE.validate_cloud_gate(args)

    def test_confidential_requires_second_confirmation(self):
        args = argparse.Namespace(
            allow_cloud_code_upload=True,
            data_classification="confidential",
            confirm_confidential=False,
            max_cost_usd=1.0,
        )
        with self.assertRaises(MODULE.PolicyError):
            MODULE.validate_cloud_gate(args)

    def test_runtime_root_is_protected(self):
        with self.assertRaises(MODULE.PolicyError):
            MODULE.validate_repository(str(MODULE.runtime_root()))

    def test_cost_ceiling_is_enforced(self):
        args = argparse.Namespace(
            allow_cloud_code_upload=True,
            data_classification="internal",
            confirm_confidential=False,
            max_cost_usd=25.01,
        )
        with self.assertRaises(MODULE.PolicyError):
            MODULE.validate_cloud_gate(args)

    def test_non_finite_costs_are_rejected(self):
        for value in (float("nan"), float("inf"), float("-inf")):
            with self.subTest(value=value):
                args = argparse.Namespace(
                    allow_cloud_code_upload=True,
                    data_classification="internal",
                    confirm_confidential=False,
                    max_cost_usd=value,
                )
                with self.assertRaises(MODULE.PolicyError):
                    MODULE.validate_cloud_gate(args)

    def test_diff_argument_is_not_exposed(self):
        parser = MODULE.build_parser()
        with self.assertRaises(SystemExit):
            parser.parse_args(
                [
                    "preflight",
                    "--repository",
                    ".",
                    "--diff",
                    "HEAD~1",
                ]
            )

    def test_preflight_is_fully_local_and_does_not_invoke_upstream(self):
        temporary, repository = self.make_repository()
        self.addCleanup(temporary.cleanup)
        args = argparse.Namespace(
            repository=str(repository),
            path=[],
            working_tree=False,
            model=None,
        )
        output = io.StringIO()
        with (
            mock.patch.object(
                MODULE,
                "verify_runtime",
                return_value={
                    "manifest": {
                        "package": "@openai/codex-security",
                        "version": "0.1.1",
                    },
                    "nodeSha256": "pinned",
                },
            ),
            mock.patch.object(MODULE.subprocess, "run") as subprocess_runner,
            mock.patch.object(MODULE, "run_cli") as cli_runner,
            mock.patch.object(MODULE, "append_audit"),
            redirect_stdout(output),
        ):
            self.assertEqual(MODULE.command_preflight(args), 0)
        cli_runner.assert_not_called()
        subprocess_runner.assert_not_called()
        payload = json.loads(output.getvalue())
        self.assertEqual(payload["preflight"], "local-only")
        self.assertIs(payload["upstreamCliInvoked"], False)
        self.assertIs(payload["cloudUpload"], False)
        self.assertEqual(payload["modelCostUsd"], 0)
        self.assertEqual(payload["filesInspected"], 1)

    def test_scan_forces_managed_output_and_standard_mode(self):
        temporary, repository = self.make_repository()
        self.addCleanup(temporary.cleanup)
        args = argparse.Namespace(
            repository=str(repository),
            path=[],
            working_tree=False,
            model=None,
            data_classification="internal",
            allow_cloud_code_upload=True,
            confirm_confidential=False,
            max_cost_usd=3.0,
            auth="chatgpt",
            fail_on_severity="high",
        )
        managed = Path(temporary.name) / "managed-scan"
        managed.mkdir()
        with (
            mock.patch.object(MODULE, "run_cli", return_value=0) as runner,
            mock.patch.object(MODULE, "make_scan_dir", return_value=managed),
            mock.patch.object(MODULE, "append_audit"),
        ):
            self.assertEqual(MODULE.command_scan(args), 0)
        command = runner.call_args.args[0]
        self.assertEqual(command[command.index("--mode") + 1], "standard")
        self.assertEqual(command[command.index("--max-cost") + 1], "3.0")
        self.assertEqual(command[command.index("--output-dir") + 1], str(managed))
        self.assertNotIn("--plugin-path", command)
        self.assertNotIn("--codex", command)

    def test_child_environment_is_allowlisted(self):
        runtime = {
            "node": r"C:\Program Files\nodejs\node.exe",
            "python": r"C:\Python311\python.exe",
        }
        with (
            mock.patch.dict(
                MODULE.os.environ,
                {
                    "GITHUB_TOKEN": "do-not-forward",
                    "DATABASE_PASSWORD": "do-not-forward",
                    "DATABASE_URL": "do-not-forward",
                    "SENTRY_DSN": "do-not-forward",
                    "MY_KEY": "do-not-forward",
                    "CODEX_HOME": "do-not-forward",
                    "OPENAI_API_KEY": "forward-only-this",
                    "TEMP": r"Z:\spoofed-temp",
                    "USERPROFILE": r"Z:\spoofed-profile",
                    "SystemRoot": r"Z:\spoofed-windows",
                },
                clear=False,
            ),
            mock.patch.object(
                MODULE, "user_profile_root", return_value=Path(r"C:\Users\Trusted")
            ),
            mock.patch.object(
                MODULE,
                "local_app_data_root",
                return_value=Path(r"C:\Users\Trusted\AppData\Local"),
            ),
            mock.patch.object(
                MODULE,
                "roaming_app_data_root",
                return_value=Path(r"C:\Users\Trusted\AppData\Roaming"),
            ),
            mock.patch.object(
                MODULE, "program_data_root", return_value=Path(r"C:\ProgramData")
            ),
            mock.patch.object(
                MODULE, "windows_root", return_value=Path(r"C:\Windows")
            ),
            mock.patch.object(Path, "mkdir"),
        ):
            environment = MODULE.build_child_env("api-key", runtime)
        self.assertNotIn("GITHUB_TOKEN", environment)
        self.assertNotIn("DATABASE_PASSWORD", environment)
        self.assertNotIn("DATABASE_URL", environment)
        self.assertNotIn("SENTRY_DSN", environment)
        self.assertNotIn("MY_KEY", environment)
        self.assertNotIn("CODEX_HOME", environment)
        self.assertEqual(environment["OPENAI_API_KEY"], "forward-only-this")
        self.assertEqual(
            environment["TEMP"], r"C:\Users\Trusted\AppData\Local\Temp"
        )
        self.assertEqual(environment["USERPROFILE"], r"C:\Users\Trusted")
        self.assertNotIn("Z:\\spoofed", " ".join(environment.values()))
        self.assertIn(r"C:\Program Files\nodejs", environment["PATH"])

    def test_chatgpt_auth_does_not_receive_api_key(self):
        runtime = {"node": "node.exe", "python": "python.exe"}
        with mock.patch.dict(
            MODULE.os.environ,
            {
                "OPENAI_API_KEY": "do-not-forward",
                "USERPROFILE": r"C:\Users\Test",
            },
            clear=False,
        ):
            environment = MODULE.build_child_env("chatgpt", runtime)
        self.assertNotIn("OPENAI_API_KEY", environment)

    def test_cli_never_uses_shell(self):
        runtime = {
            "node": "node.exe",
            "binary": "codex-security.mjs",
        }
        completed = mock.Mock(returncode=0)
        with (
            mock.patch.object(MODULE, "verify_runtime", return_value=runtime),
            mock.patch.object(MODULE, "build_child_env", return_value={}) as environment,
            mock.patch.object(MODULE.subprocess, "run", return_value=completed) as runner,
        ):
            self.assertEqual(MODULE.run_cli(["info"]), 0)
        self.assertIs(runner.call_args.kwargs["shell"], False)
        environment.assert_called_once_with("none", runtime)

    def test_every_subprocess_run_has_explicit_environment_and_no_shell(self):
        tree = ast.parse(MODULE_PATH.read_text(encoding="utf-8"))
        calls = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "subprocess"
            and node.func.attr == "run"
        ]
        self.assertGreaterEqual(len(calls), 2)
        for call in calls:
            keywords = {keyword.arg: keyword.value for keyword in call.keywords}
            self.assertIn("env", keywords)
            self.assertIn("shell", keywords)
            self.assertIsInstance(keywords["shell"], ast.Constant)
            self.assertIs(keywords["shell"].value, False)

    def test_manifest_runtime_path_is_relative_and_canonical(self):
        manifest = MODULE.load_manifest()
        self.assertNotIn("runtimeRoot", manifest)
        relative = Path(manifest["runtimeRelativePath"])
        self.assertFalse(relative.is_absolute())
        self.assertNotIn("..", relative.parts)
        self.assertEqual(
            MODULE.runtime_root(manifest),
            (
                MODULE.user_profile_root()
                / ".agents"
                / "runtimes"
                / "codex-security"
                / "0.1.1"
            ).resolve(),
        )

    def test_installer_has_fixed_roots_lock_and_collision_safe_names(self):
        skill_root = Path(__file__).resolve().parents[1]
        installer = (
            skill_root / "scripts" / "install_runtime.ps1"
        ).read_text(encoding="utf-8")
        self.assertIn("[Environment+SpecialFolder]::UserProfile", installer)
        self.assertIn("[Environment+SpecialFolder]::ProgramFiles", installer)
        self.assertIn("[System.Threading.Mutex]", installer)
        self.assertIn("[guid]::NewGuid()", installer)
        self.assertIn("Assert-NoReparsePath", installer)
        self.assertNotIn("[string]$RuntimeRoot", installer)
        self.assertNotIn("$env:USERPROFILE", installer)

    def test_rollback_is_limited_to_versioned_backup_base(self):
        skill_root = Path(__file__).resolve().parents[1]
        rollback = (
            skill_root / "scripts" / "rollback_active_copies.ps1"
        )
        completed = subprocess.run(
            [
                "powershell",
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(rollback),
                "-BackupRoot",
                str(Path.cwd()),
                "-ConfirmRollback",
                "-WhatIf",
            ],
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn(
            "BackupRoot wykracza poza dozwolony katalog",
            completed.stderr + completed.stdout,
        )

    def test_rollback_has_fixed_targets_reparse_checks_and_mutex(self):
        skill_root = Path(__file__).resolve().parents[1]
        rollback = (
            skill_root / "scripts" / "rollback_active_copies.ps1"
        ).read_text(encoding="utf-8")
        self.assertIn("[Environment+SpecialFolder]::UserProfile", rollback)
        self.assertIn(
            "[Environment+SpecialFolder]::LocalApplicationData", rollback
        )
        self.assertIn("agent-stack-backups\\codex-security-cli", rollback)
        self.assertIn("Assert-NoReparsePath", rollback)
        self.assertIn("[System.Threading.Mutex]", rollback)
        self.assertIn("$plan = @()", rollback)
        self.assertNotIn("$env:USERPROFILE", rollback)


if __name__ == "__main__":
    unittest.main()
