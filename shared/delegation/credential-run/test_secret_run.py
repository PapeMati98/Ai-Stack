#!/usr/bin/env python3
"""
test_secret_run.py - Testy jednostkowe i integracyjne dla secret_run (V2).

Testy sprawdzaja:
1. Odmowe wykonania przed uruchomieniem jakiegokolwiek procesu przy referencji spoza allowlisty (REF_NOT_ALLOWED).
2. Odmowe wykonania przy komendzie lub skrypcie spoza listy commands (COMMAND_NOT_ALLOWED).
3. Bezwzgledny zakaz powlok ('cmd', 'powershell', 'pwsh', 'bash', 'sh') oraz kodu inline ('python -c', 'node -e')
   nawet jesli znajduja sie na allowliscie (SHELL_NOT_ALLOWED).
4. Odmowe wykonania przy starym formacie allowlisty z komunikatem o migracji (ALLOWLIST_FORMAT_OLD).
5. Poprawne wykonanie gdy zarowno interpreter jak i skrypt sa na liscie commands.
6. Brak wycieku wartosci sekretu z atrapy op w stdout, stderr, audycie i wyjatkach (wbudowane maskowanie op).
7. Zakaz sciezki reveal/read/get/inject oraz flagi --no-masking (FORBIDDEN_OPERATION).
8. Poprawnosc zapisu do audit.jsonl (rejestracja wylacznie referencji op://, nigdy wartosci sekretow).
9. Obsluge braku 1Password CLI 'op' (kod OP_CLI_MISSING i instrukcja dla Mateusza).
10. Przekazywanie kodow wyjscia komend potomnych.
11. Brak okien procesow potomnych (creationflags na Windows).
12. R1: Sanityzacje odziedziczonego srodowiska (op:// oraz niebezpieczne zmienne) i zakaz op:// w argumentach potomnych.
13. R2: Walidacje preloadow Node i zakaz opcji inspekcji / interaktywnych.
14. R3: Przypinanie skrotow SHA-256 skryptow (command_sha256, COMMAND_HASH_MISMATCH).
15. R4: Blokade nadpisywania allowlisty w srodowisku produkcyjnym (ALLOWLIST_OVERRIDE_FORBIDDEN).
16. R5: Rezerwowe wykrywanie binarnego op.exe na Windows.
17. R6: Poprawnosc ladowania i struktury allowlist.example.json.

Zasady:
- Tylko stdlib Pythona.
- Zadnych zaleznosci sieciowych ani modeli.
- Atrapa op w katalogu testowym przez SECRET_RUN_OP; prawdziwy 'op' nigdy nie jest wywolywany.
- Zakaz usuwania plikow (bez os.remove/unlink/rmtree). Pliki tymczasowe w AGENT_TEST_ROOT lub ~/.agents/test-runs/.
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import unittest
import unittest.mock as mock
import uuid
from pathlib import Path

# Dodanie katalogu z secret_run do sys.path
SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import secret_run as sr  # noqa: E402

FAKE_OP_SCRIPT = """#!/usr/bin/env python3
import json
import os
import subprocess
import sys
from pathlib import Path

def main():
    argv = sys.argv[1:]
    log_file = os.environ.get("FAKE_OP_CALL_LOG")
    if log_file:
        try:
            with open(log_file, "a", encoding="utf-8") as f:
                f.write(json.dumps(argv) + "\\n")
        except Exception:
            pass

    if not argv:
        sys.stderr.write("fake_op: brak argumentow\\n")
        sys.exit(1)

    subcmd = argv[0]
    if subcmd != "run":
        sys.stderr.write(f"fake_op: nieobslugiwana podkomenda {subcmd}\\n")
        sys.exit(1)

    masking = "--no-masking" not in argv

    if "--" not in argv:
        sys.stderr.write("fake_op: brak separatora '--'\\n")
        sys.exit(1)

    idx = argv.index("--")
    cmd = argv[idx + 1:]
    if not cmd:
        sys.stderr.write("fake_op: brak komendy po '--'\\n")
        sys.exit(1)

    # Wstrzykniecie syntetycznych wartosci sekretow w miejsce referencji op://
    child_env = os.environ.copy()
    secrets_to_mask = []
    for k, v in list(child_env.items()):
        if v.startswith("op://"):
            secret_val = f"MOCK_SECRET_VAL_FOR_{v}"
            child_env[k] = secret_val
            secrets_to_mask.append(secret_val.encode("utf-8"))

    creationflags = 0
    if sys.platform == "win32":
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)

    if cmd and Path(cmd[0]).stem.lower() in ("node", "nodejs"):
        out = b"mock node output\\n"
        err = b""
        retcode = 0
    else:
        proc = subprocess.run(
            cmd,
            env=child_env,
            capture_output=True,
            creationflags=creationflags,
        )
        out = proc.stdout
        err = proc.stderr
        retcode = proc.returncode

    if masking:
        marker = b"<concealed by 1Password>"
        for s in secrets_to_mask:
            out = out.replace(s, marker)
            err = err.replace(s, marker)

    sys.stdout.buffer.write(out)
    sys.stdout.buffer.flush()
    sys.stderr.buffer.write(err)
    sys.stderr.buffer.flush()
    sys.exit(retcode)

if __name__ == "__main__":
    main()
"""


def get_test_dir(prefix: str = "secret-run-test") -> Path:
    test_root = os.environ.get("AGENT_TEST_ROOT")
    if test_root:
        base = Path(test_root)
    else:
        base = Path.home() / ".agents" / "test-runs"
    d = base / f"{prefix}-{uuid.uuid4().hex[:10]}"
    d.mkdir(parents=True, exist_ok=True)
    return d


class TestSecretRun(unittest.TestCase):
    def setUp(self):
        self.test_dir = get_test_dir()
        self.fake_op_path = self.test_dir / "fake_op.py"
        self.fake_op_path.write_text(FAKE_OP_SCRIPT, encoding="utf-8")

        self.call_log_path = self.test_dir / "fake_op_calls.jsonl"
        self.allowlist_path = self.test_dir / "allowlist.json"
        self.audit_path = self.test_dir / "audit.jsonl"

        # Domyslny plik wykonywalny skryptu dla testow poprawnych wywolan
        self.dummy_script = self.test_dir / "dummy_script.py"
        self.dummy_script.write_text("print('hello')\n", encoding="utf-8")

        # Domyslna poprawna allowlista w nowym formacie (V2)
        self.allowlist_data = {
            "demo-project": {
                "refs": [
                    "op://Agents/Demo/api_key",
                    "op://Agents/Demo/db_pass",
                ],
                "commands": [
                    sys.executable,
                    "python",
                    "python3",
                    str(self.dummy_script),
                    "dummy_script.py",
                ],
            },
            "billing-app": {
                "refs": [
                    "op://Agents/Billing/token",
                ],
                "commands": [
                    sys.executable,
                    "python",
                    "python3",
                ],
            },
        }
        self.allowlist_path.write_text(json.dumps(self.allowlist_data), encoding="utf-8")

        # Zapamietanie i ustawienie srodowiska
        self._orig_env = os.environ.copy()
        os.environ["SECRET_RUN_OP"] = str(self.fake_op_path)
        os.environ["SECRET_RUN_ALLOWLIST_PATH"] = str(self.allowlist_path)
        os.environ["SECRET_RUN_AUDIT_PATH"] = str(self.audit_path)
        os.environ["FAKE_OP_CALL_LOG"] = str(self.call_log_path)

    def tearDown(self):
        # Przywrocenie srodowiska bez usuwania plikow (zgodnie z zasada braku usuwania plikow)
        os.environ.clear()
        os.environ.update(self._orig_env)

    def _get_fake_op_calls(self) -> list[list[str]]:
        if not self.call_log_path.exists():
            return []
        lines = self.call_log_path.read_text(encoding="utf-8").strip().splitlines()
        return [json.loads(line) for line in lines if line.strip()]

    def test_op_missing_returns_error_and_instructions(self):
        """Brak 1Password CLI zwraca czytelny blad OP_CLI_MISSING z instrukcja dla Mateusza."""
        non_existent_op = str(self.test_dir / "non_existent_op_binary")
        res = sr.secret_run(
            project="demo-project",
            env_pairs=[("API_KEY", "op://Agents/Demo/api_key")],
            command=[sys.executable, str(self.dummy_script)],
            allowlist_path=self.allowlist_path,
            audit_path=self.audit_path,
            op_binary=non_existent_op,
        )
        self.assertEqual(res["status"], "error")
        self.assertEqual(res["code"], "OP_CLI_MISSING")
        self.assertIn("OP_CLI_MISSING", res["error"])
        self.assertIn("Mateusz", res["message"])
        self.assertIn("1Password", res["message"])
        self.assertIn("Integrate with 1Password CLI", res["message"])
        # Upewnienie sie, ze fake op nie zostal uruchomiony
        self.assertEqual(len(self._get_fake_op_calls()), 0)

    def test_op_missing_via_env_var_returns_op_cli_missing(self):
        """Brak 1Password CLI wskazanego w SECRET_RUN_OP zwraca OP_CLI_MISSING."""
        os.environ["SECRET_RUN_OP"] = str(self.test_dir / "non_existent_op_env")
        res = sr.secret_run(
            project="demo-project",
            env_pairs=[("API_KEY", "op://Agents/Demo/api_key")],
            command=[sys.executable, str(self.dummy_script)],
            allowlist_path=self.allowlist_path,
            audit_path=self.audit_path,
        )
        self.assertEqual(res["status"], "error")
        self.assertEqual(res["code"], "OP_CLI_MISSING")
        self.assertIn("OP_CLI_MISSING", res["error"])
        self.assertIn("Mateusz", res["message"])
        self.assertEqual(len(self._get_fake_op_calls()), 0)

    def test_reference_outside_allowlist_refused_before_process_start(self):
        """Referencja spoza allowlisty jest odrzucana przed jakimkolwiek uruchomieniem procesu."""
        res = sr.secret_run(
            project="demo-project",
            env_pairs=[("API_KEY", "op://Agents/Demo/unauthorized_secret")],
            command=[sys.executable, str(self.dummy_script)],
            allowlist_path=self.allowlist_path,
            audit_path=self.audit_path,
            op_binary=str(self.fake_op_path),
        )
        self.assertEqual(res["status"], "error")
        self.assertEqual(res["code"], "REF_NOT_ALLOWED")
        self.assertIn("REF_NOT_ALLOWED", res["error"])
        self.assertIn("unauthorized_secret", res["message"])
        # Zaden proces potomny nie wystartowal
        self.assertEqual(len(self._get_fake_op_calls()), 0)
        # Audyt nie odnotowal uruchomienia
        self.assertFalse(self.audit_path.exists())

    def test_unknown_project_refused_before_process_start(self):
        """Nieznany projekt spoza allowlisty jest odrzucany z REF_NOT_ALLOWED."""
        res = sr.secret_run(
            project="nieistniejacy-projekt",
            env_pairs=[("API_KEY", "op://Agents/Demo/api_key")],
            command=[sys.executable, str(self.dummy_script)],
            allowlist_path=self.allowlist_path,
            audit_path=self.audit_path,
            op_binary=str(self.fake_op_path),
        )
        self.assertEqual(res["status"], "error")
        self.assertEqual(res["code"], "REF_NOT_ALLOWED")
        self.assertEqual(len(self._get_fake_op_calls()), 0)

    def test_missing_allowlist_file_refused(self):
        """Brak pliku allowlisty powoduje odmowe z kodem ALLOWLIST_MISSING."""
        brak_allowlisty = self.test_dir / "brak_allowlist.json"
        res = sr.secret_run(
            project="demo-project",
            env_pairs=[("API_KEY", "op://Agents/Demo/api_key")],
            command=[sys.executable, str(self.dummy_script)],
            allowlist_path=brak_allowlisty,
            audit_path=self.audit_path,
            op_binary=str(self.fake_op_path),
        )
        self.assertEqual(res["status"], "error")
        self.assertEqual(res["code"], "ALLOWLIST_MISSING")
        self.assertEqual(len(self._get_fake_op_calls()), 0)

    def test_forbidden_operations_rejected(self):
        """Podkomendy reveal, read, get, inject oraz --reveal sa bezwzglednie blokowane."""
        for forbidden in ["read", "get", "reveal", "inject"]:
            parse_res = sr.parse_cli([forbidden, "op://Agents/Demo/api_key"])
            self.assertIsNone(parse_res[0])
            err = parse_res[1]
            self.assertEqual(err["code"], "FORBIDDEN_OPERATION")

        # Flaga --reveal
        parse_reveal = sr.parse_cli(["run", "--project", "demo-project", "--reveal", "--", "cmd"])
        self.assertIsNone(parse_reveal[0])
        self.assertEqual(parse_reveal[1]["code"], "FORBIDDEN_OPERATION")

        # Flaga --no-masking
        parse_nomask = sr.parse_cli(["run", "--project", "demo-project", "--no-masking", "--", "cmd"])
        self.assertIsNone(parse_nomask[0])
        self.assertEqual(parse_nomask[1]["code"], "FORBIDDEN_OPERATION")

    def test_masking_prevents_secret_leak_in_stdout_and_stderr(self):
        """Maskowanie w procesie op run zamienia wartosc sekretu na <concealed by 1Password>."""
        leak_script = self.test_dir / "leak_script.py"
        leak_script.write_text(
            "import os, sys\n"
            "val = os.environ.get('MOJ_SEKRET', '')\n"
            "print('Stdout wyciek: ' + val)\n"
            "sys.stderr.write('Stderr wyciek: ' + val + '\\n')\n",
            encoding="utf-8",
        )
        self.allowlist_data["demo-project"]["commands"].append(str(leak_script))
        self.allowlist_path.write_text(json.dumps(self.allowlist_data), encoding="utf-8")

        cmd = [sys.executable, str(leak_script)]

        res = sr.secret_run(
            project="demo-project",
            env_pairs=[("MOJ_SEKRET", "op://Agents/Demo/api_key")],
            command=cmd,
            allowlist_path=self.allowlist_path,
            audit_path=self.audit_path,
            op_binary=str(self.fake_op_path),
        )

        self.assertEqual(res["status"], "success")
        self.assertEqual(res["exit_code"], 0)

        stdout_text = res["stdout"].decode("utf-8")
        stderr_text = res["stderr"].decode("utf-8")

        raw_secret_value = "MOCK_SECRET_VAL_FOR_op://Agents/Demo/api_key"

        # 1. Zaden sekret nie pojawia sie w stdout ani stderr
        self.assertNotIn(raw_secret_value, stdout_text)
        self.assertNotIn(raw_secret_value, stderr_text)

        # 2. Pojawia sie znacznik maskowania 1Password
        self.assertIn("<concealed by 1Password>", stdout_text)
        self.assertIn("<concealed by 1Password>", stderr_text)

    def test_audit_records_refs_without_values(self):
        """Plik audit.jsonl rejestruje referencje op://, komende[0], kod wyjscia, czas - bez wartosci sekretow."""
        audit_script = self.test_dir / "audit_script.py"
        audit_script.write_text("import os; print('Wartosc: ' + os.environ.get('K1', ''))\n", encoding="utf-8")
        self.allowlist_data["demo-project"]["commands"].append(str(audit_script))
        self.allowlist_path.write_text(json.dumps(self.allowlist_data), encoding="utf-8")

        cmd = [sys.executable, str(audit_script)]

        res = sr.secret_run(
            project="demo-project",
            env_pairs=[
                ("K1", "op://Agents/Demo/api_key"),
                ("K2", "op://Agents/Demo/db_pass"),
            ],
            command=cmd,
            allowlist_path=self.allowlist_path,
            audit_path=self.audit_path,
            op_binary=str(self.fake_op_path),
        )

        self.assertEqual(res["status"], "success")
        self.assertTrue(self.audit_path.exists())

        audit_lines = self.audit_path.read_text(encoding="utf-8").strip().splitlines()
        self.assertEqual(len(audit_lines), 1)

        record = json.loads(audit_lines[0])
        self.assertEqual(record["project"], "demo-project")
        self.assertEqual(record["references"], [
            "op://Agents/Demo/api_key",
            "op://Agents/Demo/db_pass",
        ])
        self.assertEqual(record["command"], sys.executable)
        self.assertEqual(record["exit_code"], 0)
        self.assertIn("timestamp", record)
        self.assertIn("duration_seconds", record)
        self.assertGreaterEqual(record["duration_seconds"], 0)

        # Sprawdzenie, ze ZADNA wartosc sekretu nie trafila do calego pliku audytu
        audit_file_content = self.audit_path.read_text(encoding="utf-8")
        self.assertNotIn("MOCK_SECRET_VAL", audit_file_content)

    def test_child_command_failure_propagates_exit_code(self):
        """Kod wyjscia komendy potomnej jest wiernie zwracany i rejestrowany w audycie."""
        fail_script = self.test_dir / "fail_script.py"
        fail_script.write_text("import sys; sys.exit(42)\n", encoding="utf-8")
        self.allowlist_data["billing-app"]["commands"].append(str(fail_script))
        self.allowlist_path.write_text(json.dumps(self.allowlist_data), encoding="utf-8")

        cmd = [sys.executable, str(fail_script)]

        res = sr.secret_run(
            project="billing-app",
            env_pairs=[("TOKEN", "op://Agents/Billing/token")],
            command=cmd,
            allowlist_path=self.allowlist_path,
            audit_path=self.audit_path,
            op_binary=str(self.fake_op_path),
        )

        self.assertEqual(res["status"], "success")
        self.assertEqual(res["exit_code"], 42)

        record = json.loads(self.audit_path.read_text(encoding="utf-8").strip())
        self.assertEqual(record["exit_code"], 42)

    def test_cli_execution_via_main(self):
        """Pelny obieg CLI przez funkcje main()."""
        cli_script = self.test_dir / "cli_script.py"
        cli_script.write_text("import os; print('Wynik CLI: ' + os.environ.get('DEMO_CRED', ''))\n", encoding="utf-8")
        self.allowlist_data["demo-project"]["commands"].append(str(cli_script))
        self.allowlist_path.write_text(json.dumps(self.allowlist_data), encoding="utf-8")

        cli_argv = [
            "run",
            "--project", "demo-project",
            "--env", "DEMO_CRED=op://Agents/Demo/api_key",
            "--",
            sys.executable, str(cli_script),
        ]

        import io
        from contextlib import redirect_stdout, redirect_stderr

        out_buf = io.StringIO()
        err_buf = io.StringIO()
        with redirect_stdout(out_buf), redirect_stderr(err_buf):
            exit_code = sr.main(cli_argv)

        self.assertEqual(exit_code, 0)
        out_text = out_buf.getvalue()
        self.assertIn("Wynik CLI: <concealed by 1Password>", out_text)
        self.assertNotIn("MOCK_SECRET_VAL", out_text)

        # Sprawdzenie wpisu audytu
        self.assertTrue(self.audit_path.exists())
        record = json.loads(self.audit_path.read_text(encoding="utf-8").strip())
        self.assertEqual(record["project"], "demo-project")
        self.assertEqual(record["references"], ["op://Agents/Demo/api_key"])

    def test_windows_creationflags_windowless(self):
        """Weryfikacja flagi CREATE_NO_WINDOW na platformie Windows."""
        flags = sr.get_creationflags()
        if sys.platform == "win32":
            self.assertEqual(flags, 0x08000000)
        else:
            self.assertEqual(flags, 0)

    # =========================================================================
    # NOWE TESTY V2: allowlista komend per projekt, zakaz powlok i kodu inline
    # =========================================================================

    def test_command_not_on_project_list_refused_before_start(self):
        """Komenda potomna spoza allowlisty projektu jest odrzucana z COMMAND_NOT_ALLOWED przed startem."""
        res = sr.secret_run(
            project="demo-project",
            env_pairs=[("API_KEY", "op://Agents/Demo/api_key")],
            command=["curl", "https://api.example.com"],
            allowlist_path=self.allowlist_path,
            audit_path=self.audit_path,
            op_binary=str(self.fake_op_path),
        )
        self.assertEqual(res["status"], "error")
        self.assertEqual(res["code"], "COMMAND_NOT_ALLOWED")
        self.assertIn("COMMAND_NOT_ALLOWED", res["error"])
        self.assertIn("curl", res["message"])
        # Zaden proces potomny nie wystartowal
        self.assertEqual(len(self._get_fake_op_calls()), 0)
        self.assertFalse(self.audit_path.exists())

    def test_script_not_on_project_list_refused_before_start(self):
        """Skrypt niewymieniony na liscie commands (mimo ze python jest na liscie) jest odrzucany przed startem."""
        unauthorized_script = self.test_dir / "unauthorized_script.py"
        unauthorized_script.write_text("print('should not run')\n", encoding="utf-8")

        res = sr.secret_run(
            project="demo-project",
            env_pairs=[("API_KEY", "op://Agents/Demo/api_key")],
            command=[sys.executable, str(unauthorized_script)],
            allowlist_path=self.allowlist_path,
            audit_path=self.audit_path,
            op_binary=str(self.fake_op_path),
        )
        self.assertEqual(res["status"], "error")
        self.assertEqual(res["code"], "COMMAND_NOT_ALLOWED")
        self.assertIn("COMMAND_NOT_ALLOWED", res["error"])
        self.assertIn(str(unauthorized_script), res["message"])
        self.assertEqual(len(self._get_fake_op_calls()), 0)
        self.assertFalse(self.audit_path.exists())

    def test_shells_refused_even_if_listed(self):
        """Powloki (cmd, powershell, pwsh, bash, sh) sa bezwzglednie zakazane z SHELL_NOT_ALLOWED, nawet na liscie."""
        # Dodajemy powloki do listy komend, aby przetestowac ze sa odrzucane NAWET gdy sa na liscie
        self.allowlist_data["demo-project"]["commands"].extend([
            "cmd", "powershell", "pwsh", "bash", "sh", "zsh"
        ])
        self.allowlist_path.write_text(json.dumps(self.allowlist_data), encoding="utf-8")

        shell_commands = [
            ["cmd", "/c", "dir"],
            ["powershell", "-Command", "Get-ChildItem"],
            ["pwsh", "-Command", "Get-ChildItem"],
            ["bash", "script.sh"],
            ["sh", "script.sh"],
        ]

        for shell_cmd in shell_commands:
            res = sr.secret_run(
                project="demo-project",
                env_pairs=[("API_KEY", "op://Agents/Demo/api_key")],
                command=shell_cmd,
                allowlist_path=self.allowlist_path,
                audit_path=self.audit_path,
                op_binary=str(self.fake_op_path),
            )
            self.assertEqual(res["status"], "error", f"Powloka {shell_cmd[0]} powinna zwrocic status error")
            self.assertEqual(res["code"], "SHELL_NOT_ALLOWED", f"Powloka {shell_cmd[0]} powinna zwrocic SHELL_NOT_ALLOWED")
            self.assertIn("SHELL_NOT_ALLOWED", res["error"])

        # Zaden proces potomny nie wystartowal
        self.assertEqual(len(self._get_fake_op_calls()), 0)
        self.assertFalse(self.audit_path.exists())

    def test_inline_code_refused_even_if_listed(self):
        """Kod inline ('python -c', 'node -e') oraz tryby REPL sa bezwzglednie zakazane z SHELL_NOT_ALLOWED."""
        self.allowlist_data["demo-project"]["commands"].extend([
            "python -c", "node", "node -e"
        ])
        self.allowlist_path.write_text(json.dumps(self.allowlist_data), encoding="utf-8")

        inline_commands = [
            [sys.executable, "-c", "import os; print(os.environ.get('API_KEY'))"],
            [sys.executable, "-u", "-c", "print('inline')"],
            ["node", "-e", "console.log(process.env.API_KEY)"],
            [sys.executable],  # REPL bez skryptu
        ]

        for inline_cmd in inline_commands:
            res = sr.secret_run(
                project="demo-project",
                env_pairs=[("API_KEY", "op://Agents/Demo/api_key")],
                command=inline_cmd,
                allowlist_path=self.allowlist_path,
                audit_path=self.audit_path,
                op_binary=str(self.fake_op_path),
            )
            self.assertEqual(res["status"], "error", f"Komenda {inline_cmd} powinna zwrocic status error")
            self.assertEqual(res["code"], "SHELL_NOT_ALLOWED", f"Komenda {inline_cmd} powinna zwrocic SHELL_NOT_ALLOWED")
            self.assertIn("SHELL_NOT_ALLOWED", res["error"])

        self.assertEqual(len(self._get_fake_op_calls()), 0)
        self.assertFalse(self.audit_path.exists())

    def test_old_allowlist_format_refused_with_migration_message(self):
        """Stary format allowlisty (mapowanie na liste referencji) jest odrzucany z kodem ALLOWLIST_FORMAT_OLD."""
        old_format_allowlist = {
            "demo-project": [
                "op://Agents/Demo/api_key",
                "op://Agents/Demo/db_pass",
            ],
            "billing-app": [
                "op://Agents/Billing/token",
            ],
        }
        old_allowlist_file = self.test_dir / "old_allowlist.json"
        old_allowlist_file.write_text(json.dumps(old_format_allowlist), encoding="utf-8")

        res = sr.secret_run(
            project="demo-project",
            env_pairs=[("API_KEY", "op://Agents/Demo/api_key")],
            command=[sys.executable, str(self.dummy_script)],
            allowlist_path=old_allowlist_file,
            audit_path=self.audit_path,
            op_binary=str(self.fake_op_path),
        )

        self.assertEqual(res["status"], "error")
        self.assertEqual(res["code"], "ALLOWLIST_FORMAT_OLD")
        self.assertIn("ALLOWLIST_FORMAT_OLD", res["error"])
        # Komunikat zawiera instrukcje migracji
        self.assertIn("migracj", res["message"].lower())
        self.assertIn("commands", res["message"])
        self.assertEqual(len(self._get_fake_op_calls()), 0)
        self.assertFalse(self.audit_path.exists())

    def test_malformed_allowlist_structure_rejected(self):
        """Allowlista bez pol 'commands' lub 'refs' jest odrzucana z ALLOWLIST_INVALID."""
        invalid_allowlist_file = self.test_dir / "invalid_struct_allowlist.json"

        # Brak pola commands
        invalid_allowlist_file.write_text(json.dumps({
            "demo-project": {
                "refs": ["op://Agents/Demo/api_key"],
            }
        }), encoding="utf-8")

        res = sr.secret_run(
            project="demo-project",
            env_pairs=[("API_KEY", "op://Agents/Demo/api_key")],
            command=[sys.executable, str(self.dummy_script)],
            allowlist_path=invalid_allowlist_file,
            audit_path=self.audit_path,
            op_binary=str(self.fake_op_path),
        )
        self.assertEqual(res["status"], "error")
        self.assertEqual(res["code"], "ALLOWLIST_INVALID")

        # Brak pola refs
        invalid_allowlist_file.write_text(json.dumps({
            "demo-project": {
                "commands": ["python"],
            }
        }), encoding="utf-8")

        res2 = sr.secret_run(
            project="demo-project",
            env_pairs=[("API_KEY", "op://Agents/Demo/api_key")],
            command=[sys.executable, str(self.dummy_script)],
            allowlist_path=invalid_allowlist_file,
            audit_path=self.audit_path,
            op_binary=str(self.fake_op_path),
        )
        self.assertEqual(res2["status"], "error")
        self.assertEqual(res2["code"], "ALLOWLIST_INVALID")

    def test_valid_script_execution_when_both_python_and_script_listed(self):
        """Skrypt z pliku wykonuje sie pomyslnie, gdy zarowno python jak i plik skryptu sa na liscie commands."""
        res = sr.secret_run(
            project="demo-project",
            env_pairs=[("API_KEY", "op://Agents/Demo/api_key")],
            command=[sys.executable, str(self.dummy_script)],
            allowlist_path=self.allowlist_path,
            audit_path=self.audit_path,
            op_binary=str(self.fake_op_path),
        )
        self.assertEqual(res["status"], "success")
        self.assertEqual(res["exit_code"], 0)
        self.assertEqual(len(self._get_fake_op_calls()), 1)
        self.assertTrue(self.audit_path.exists())

    def test_node_eval_inline_with_allowed_script_rejected_as_shell_not_allowed(self):
        """Odrzucenie kodu inline Node w dowolnej formie (--eval=..., -e=..., -p=...) nawet przy obecnosci skryptu na liscie."""
        self.allowlist_data["demo-project"]["commands"].extend([
            "node",
            str(self.dummy_script),
        ])
        self.allowlist_path.write_text(json.dumps(self.allowlist_data), encoding="utf-8")

        node_inline_variants = [
            ["node", "--eval=console.log(process.env.API_KEY)", str(self.dummy_script)],
            ["node", "-e=console.log(process.env.API_KEY)", str(self.dummy_script)],
            ["node", "-e", "console.log(process.env.API_KEY)", str(self.dummy_script)],
            ["node", "--eval", "console.log(process.env.API_KEY)", str(self.dummy_script)],
            ["node", "-p=process.env.API_KEY", str(self.dummy_script)],
            ["node", "--print=process.env.API_KEY", str(self.dummy_script)],
            ["node", "-pe", "process.env.API_KEY", str(self.dummy_script)],
        ]

        for cmd in node_inline_variants:
            res = sr.secret_run(
                project="demo-project",
                env_pairs=[("API_KEY", "op://Agents/Demo/api_key")],
                command=cmd,
                allowlist_path=self.allowlist_path,
                audit_path=self.audit_path,
                op_binary=str(self.fake_op_path),
            )
            self.assertEqual(res["status"], "error", f"Komenda {cmd} powinna zostac odrzucona")
            self.assertEqual(res["code"], "SHELL_NOT_ALLOWED", f"Komenda {cmd} powinna zwrocic SHELL_NOT_ALLOWED")
            self.assertIn("SHELL_NOT_ALLOWED", res["error"])

            # extract_script_and_args musi zwracac (None, None) dla kodu inline
            script_path, mod = sr.extract_script_and_args(cmd)
            self.assertIsNone(script_path, f"extract_script_and_args nie moze zwrocic skryptu dla {cmd}")

        self.assertEqual(len(self._get_fake_op_calls()), 0)
        self.assertFalse(self.audit_path.exists())

    def test_bare_script_token_does_not_match_full_path_allowlist_entry(self):
        """Token bedacy sama nazwa pliku nie dopasowuje pelnej sciezki w allowliscie z innego katalogu."""
        trusted_dir = self.test_dir / "trusted_bin"
        trusted_dir.mkdir(parents=True, exist_ok=True)
        trusted_script = trusted_dir / "dummy_script.py"
        trusted_script.write_text("print('trusted')\n", encoding="utf-8")

        self.allowlist_data["demo-project"]["commands"] = [
            sys.executable,
            str(trusted_script),
        ]
        self.allowlist_path.write_text(json.dumps(self.allowlist_data), encoding="utf-8")

        # Proba wykonania dummy_script.py z biezacego katalogu (nie z trusted_bin)
        res = sr.secret_run(
            project="demo-project",
            env_pairs=[("API_KEY", "op://Agents/Demo/api_key")],
            command=[sys.executable, "dummy_script.py"],
            allowlist_path=self.allowlist_path,
            audit_path=self.audit_path,
            op_binary=str(self.fake_op_path),
        )
        self.assertEqual(res["status"], "error")
        self.assertEqual(res["code"], "COMMAND_NOT_ALLOWED")
        self.assertIn("COMMAND_NOT_ALLOWED", res["error"])
        self.assertEqual(len(self._get_fake_op_calls()), 0)
        self.assertFalse(self.audit_path.exists())

    def test_full_path_script_token_does_not_match_bare_name_allowlist_entry(self):
        """Token bedacy sciezka z innego katalogu nie dopasowuje wpisu bedacego sama nazwa pliku."""
        untrusted_dir = self.test_dir / "untrusted_src"
        untrusted_dir.mkdir(parents=True, exist_ok=True)
        untrusted_script = untrusted_dir / "dummy_script.py"
        untrusted_script.write_text("print('untrusted')\n", encoding="utf-8")

        # Allowlista ma tylko sama nazwe "dummy_script.py"
        self.allowlist_data["demo-project"]["commands"] = [
            sys.executable,
            "dummy_script.py",
        ]
        self.allowlist_path.write_text(json.dumps(self.allowlist_data), encoding="utf-8")

        # Proba wykonania skryptu z pelna sciezka z innego katalogu
        res = sr.secret_run(
            project="demo-project",
            env_pairs=[("API_KEY", "op://Agents/Demo/api_key")],
            command=[sys.executable, str(untrusted_script)],
            allowlist_path=self.allowlist_path,
            audit_path=self.audit_path,
            op_binary=str(self.fake_op_path),
        )
        self.assertEqual(res["status"], "error")
        self.assertEqual(res["code"], "COMMAND_NOT_ALLOWED")
        self.assertIn("COMMAND_NOT_ALLOWED", res["error"])
        self.assertEqual(len(self._get_fake_op_calls()), 0)
        self.assertFalse(self.audit_path.exists())

    def test_python_inline_code_with_option_argument_masquerading_as_script_rejected_as_shell_not_allowed(self):
        """Odrzucenie prob ominiecia zakazu kodu inline przez maskowanie argumentu opcji Pythona jako skryptu."""
        self.allowlist_data["demo-project"]["commands"].extend([
            sys.executable,
            "python",
            str(self.dummy_script),
        ])
        self.allowlist_path.write_text(json.dumps(self.allowlist_data), encoding="utf-8")

        bypass_attempts = [
            [sys.executable, "--pycache-prefix", str(self.dummy_script), "-c", "import os; print(os.environ.get('API_KEY'))"],
            [sys.executable, "--check-hash-based-pycs", str(self.dummy_script), "-c", "print('inline')"],
            [sys.executable, "-W", "ignore", "--pycache-prefix", str(self.dummy_script), "-c", "print('inline')"],
            [sys.executable, "--pycache-prefix=" + str(self.dummy_script), "-c", "print('inline')"],
            [sys.executable, "-uc", "print('inline')"],
            [sys.executable, "--pycache-prefix", str(self.dummy_script)],  # Brak skryptu (REPL z opcja)
        ]

        for cmd in bypass_attempts:
            res = sr.secret_run(
                project="demo-project",
                env_pairs=[("API_KEY", "op://Agents/Demo/api_key")],
                command=cmd,
                allowlist_path=self.allowlist_path,
                audit_path=self.audit_path,
                op_binary=str(self.fake_op_path),
            )
            self.assertEqual(res["status"], "error", f"Komenda {cmd} powinna zostac odrzucona")
            self.assertEqual(res["code"], "SHELL_NOT_ALLOWED", f"Komenda {cmd} powinna zwrocic SHELL_NOT_ALLOWED")
            self.assertIn("SHELL_NOT_ALLOWED", res["error"])

            script_path, mod = sr.extract_script_and_args(cmd)
            self.assertIsNone(script_path, f"extract_script_and_args nie moze zwrocic skryptu dla {cmd}")

        self.assertEqual(len(self._get_fake_op_calls()), 0)
        self.assertFalse(self.audit_path.exists())

    def test_node_inline_code_with_option_argument_masquerading_as_script_rejected_as_shell_not_allowed(self):
        """Odrzucenie prob ominiecia zakazu kodu inline przez maskowanie argumentu opcji Node jako skryptu."""
        self.allowlist_data["demo-project"]["commands"].extend([
            "node",
            str(self.dummy_script),
        ])
        self.allowlist_path.write_text(json.dumps(self.allowlist_data), encoding="utf-8")

        bypass_attempts = [
            ["node", "--conditions", str(self.dummy_script), "-e", "console.log(process.env.API_KEY)"],
            ["node", "-C", str(self.dummy_script), "-e", "console.log(process.env.API_KEY)"],
            ["node", "--env-file", str(self.dummy_script), "-e", "console.log(process.env.API_KEY)"],
            ["node", "-r", "some-mod", "--conditions", str(self.dummy_script), "-e", "console.log(process.env.API_KEY)"],
            ["node", "--conditions=" + str(self.dummy_script), "-e", "console.log(process.env.API_KEY)"],
            ["node", "--conditions", str(self.dummy_script), "--eval=console.log(process.env.API_KEY)"],
            ["node", "--cpu-prof-dir", str(self.dummy_script), "-e", "console.log(process.env.API_KEY)"],
            ["node", "--heap-prof-dir", str(self.dummy_script), "-e", "console.log(process.env.API_KEY)"],
            ["node", "--allow-fs-read", str(self.dummy_script), "-e", "console.log(process.env.API_KEY)"],
            ["node", "--input-type", str(self.dummy_script), "-e", "console.log(process.env.API_KEY)"],
            ["node", "--redirect-warnings", str(self.dummy_script), "-e", "console.log(process.env.API_KEY)"],
            ["node", "--trace-event-categories", str(self.dummy_script), "-e", "console.log(process.env.API_KEY)"],
            ["node", "--cpu-prof-dir", str(self.dummy_script)],  # Brak skryptu (REPL z opcja)
        ]

        for cmd in bypass_attempts:
            res = sr.secret_run(
                project="demo-project",
                env_pairs=[("API_KEY", "op://Agents/Demo/api_key")],
                command=cmd,
                allowlist_path=self.allowlist_path,
                audit_path=self.audit_path,
                op_binary=str(self.fake_op_path),
            )
            self.assertEqual(res["status"], "error", f"Komenda {cmd} powinna zostac odrzucona")
            self.assertEqual(res["code"], "SHELL_NOT_ALLOWED", f"Komenda {cmd} powinna zwrocic SHELL_NOT_ALLOWED")
            self.assertIn("SHELL_NOT_ALLOWED", res["error"])

            script_path, mod = sr.extract_script_and_args(cmd)
            self.assertIsNone(script_path, f"extract_script_and_args nie moze zwrocic skryptu dla {cmd}")

        self.assertEqual(len(self._get_fake_op_calls()), 0)
        self.assertFalse(self.audit_path.exists())

    def test_options_with_values_allow_real_script_when_both_listed(self):
        """Poprawne opcje z wartosciami (np. --check-hash-based-pycs) przepuszczaja dozwolony skrypt."""
        cmd = [sys.executable, "--check-hash-based-pycs", "always", str(self.dummy_script)]

        res = sr.secret_run(
            project="demo-project",
            env_pairs=[("API_KEY", "op://Agents/Demo/api_key")],
            command=cmd,
            allowlist_path=self.allowlist_path,
            audit_path=self.audit_path,
            op_binary=str(self.fake_op_path),
        )
        self.assertEqual(res["status"], "success")
        self.assertEqual(res["exit_code"], 0)
        self.assertEqual(len(self._get_fake_op_calls()), 1)
        self.assertTrue(self.audit_path.exists())

    def test_node_options_with_values_allow_real_script_when_both_listed(self):
        """Poprawne opcje z wartosciami Node (np. --cpu-prof-dir) przepuszczaja dozwolony skrypt."""
        self.allowlist_data["demo-project"]["commands"].extend([
            "node",
            str(self.dummy_script),
        ])
        self.allowlist_path.write_text(json.dumps(self.allowlist_data), encoding="utf-8")

        cmd = ["node", "--cpu-prof-dir", "prof_dir", str(self.dummy_script)]

        res = sr.secret_run(
            project="demo-project",
            env_pairs=[("API_KEY", "op://Agents/Demo/api_key")],
            command=cmd,
            allowlist_path=self.allowlist_path,
            audit_path=self.audit_path,
            op_binary=str(self.fake_op_path),
        )
        self.assertEqual(res["status"], "success")
        self.assertEqual(res["exit_code"], 0)
        self.assertEqual(len(self._get_fake_op_calls()), 1)
        self.assertTrue(self.audit_path.exists())

    def test_node_option_consuming_dummy_path_does_not_allow_unauthorized_script(self):
        """Opcja Node konsumujaca dozwolona sciezke jako parametr nie pozwala uruchomic nieautoryzowanego skryptu."""
        unauthorized_script = self.test_dir / "unauthorized_node_script.js"
        unauthorized_script.write_text("console.log('unauthorized');\n", encoding="utf-8")

        self.allowlist_data["demo-project"]["commands"].extend([
            "node",
            str(self.dummy_script),
        ])
        self.allowlist_path.write_text(json.dumps(self.allowlist_data), encoding="utf-8")

        cmd = ["node", "--cpu-prof-dir", str(self.dummy_script), str(unauthorized_script)]

        res = sr.secret_run(
            project="demo-project",
            env_pairs=[("API_KEY", "op://Agents/Demo/api_key")],
            command=cmd,
            allowlist_path=self.allowlist_path,
            audit_path=self.audit_path,
            op_binary=str(self.fake_op_path),
        )
        self.assertEqual(res["status"], "error")
        self.assertEqual(res["code"], "COMMAND_NOT_ALLOWED")
        self.assertIn("COMMAND_NOT_ALLOWED", res["error"])
        self.assertEqual(len(self._get_fake_op_calls()), 0)
        self.assertFalse(self.audit_path.exists())

    # =========================================================================
    # NOWE TESTY HD-20261021-57ae31 (R1 - R6)
    # =========================================================================

    def test_r1_inherited_op_env_and_dangerous_vars_stripped(self):
        """R1: Odziedziczone zmienne zawierajace 'op://' oraz niebezpieczne zmienne sa usuwane ze srodowiska procesu potomnego."""
        dump_script = self.test_dir / "dump_env.py"
        dump_script.write_text(
            "import os, json\n"
            "keys = ['INHERITED_OP', 'INHERITED_OP_UPPER', 'NODE_OPTIONS', 'PYTHONPATH', 'PYTHONSTARTUP', 'API_KEY']\n"
            "print(json.dumps({k: os.environ.get(k) for k in keys}))\n",
            encoding="utf-8",
        )
        self.allowlist_data["demo-project"]["commands"].append(str(dump_script))
        self.allowlist_path.write_text(json.dumps(self.allowlist_data), encoding="utf-8")

        # Ustawienie odziedziczonych zmiennych
        os.environ["INHERITED_OP"] = "op://Agents/Leak/token"
        os.environ["INHERITED_OP_UPPER"] = "OP://Agents/Leak/upper_token"
        os.environ["NODE_OPTIONS"] = "--require=evil"
        os.environ["PYTHONPATH"] = "/fake/pythonpath"
        os.environ["PYTHONSTARTUP"] = "/fake/startup.py"

        cmd = [sys.executable, str(dump_script)]
        res = sr.secret_run(
            project="demo-project",
            env_pairs=[("API_KEY", "op://Agents/Demo/api_key")],
            command=cmd,
            allowlist_path=self.allowlist_path,
            audit_path=self.audit_path,
            op_binary=str(self.fake_op_path),
        )

        self.assertEqual(res["status"], "success")
        self.assertEqual(res["exit_code"], 0)

        stdout_text = res["stdout"].decode("utf-8").strip()
        env_dump = json.loads(stdout_text)

        # Odziedziczone zmienne op:// musza byc usuniete (None)
        self.assertIsNone(env_dump["INHERITED_OP"])
        self.assertIsNone(env_dump["INHERITED_OP_UPPER"])

        # Niebezpieczne zmienne musza byc usuniete (None)
        self.assertIsNone(env_dump["NODE_OPTIONS"])
        self.assertIsNone(env_dump["PYTHONPATH"])
        self.assertIsNone(env_dump["PYTHONSTARTUP"])

        # Zwalidowany sekret API_KEY musi byc obecny (z zamaskowana wartoscia)
        self.assertIsNotNone(env_dump["API_KEY"])

    def test_r1_child_argv_containing_op_ref_rejected(self):
        """R1: Argumenty procesu potomnego zawierajace token 'op://' sa odrzucane z FORBIDDEN_OPERATION."""
        cmd = [sys.executable, str(self.dummy_script), "op://Agents/Demo/leak"]
        res = sr.secret_run(
            project="demo-project",
            env_pairs=[("API_KEY", "op://Agents/Demo/api_key")],
            command=cmd,
            allowlist_path=self.allowlist_path,
            audit_path=self.audit_path,
            op_binary=str(self.fake_op_path),
        )
        self.assertEqual(res["status"], "error")
        self.assertEqual(res["code"], "FORBIDDEN_OPERATION")
        self.assertIn("FORBIDDEN_OPERATION", res["error"])
        self.assertIn("op://", res["message"])
        self.assertEqual(len(self._get_fake_op_calls()), 0)
        self.assertFalse(self.audit_path.exists())

        # Bezposredni test check_forbidden_args z is_child_cmd=True
        is_forbid, msg = sr.check_forbidden_args(["python", "app.py", "OP://SECRETS/KEY"], is_child_cmd=True)
        self.assertTrue(is_forbid)
        self.assertIn("FORBIDDEN_OPERATION", msg)

    def test_r2_node_preload_unallowlisted_rejected(self):
        """R2: Flagi preload Node (-r, --require, --import, --loader itp.) z nieautoryzowanym celem sa odrzucane z COMMAND_NOT_ALLOWED."""
        self.allowlist_data["demo-project"]["commands"].extend(["node", str(self.dummy_script)])
        self.allowlist_path.write_text(json.dumps(self.allowlist_data), encoding="utf-8")

        unallowlisted_variants = [
            ["node", "-r", "malicious-preload", str(self.dummy_script)],
            ["node", "--require", "malicious-preload", str(self.dummy_script)],
            ["node", "--import", "malicious-preload", str(self.dummy_script)],
            ["node", "--loader=malicious-loader", str(self.dummy_script)],
            ["node", "--experimental-loader", "malicious-loader", str(self.dummy_script)],
            ["node", "--env-file", "malicious.env", str(self.dummy_script)],
            ["node", "--env-file-if-exists=malicious.env", str(self.dummy_script)],
            ["node", "-r"],  # brak wartosci flagi
            ["node", "--require="],  # pusta wartosc
        ]

        for cmd in unallowlisted_variants:
            res = sr.secret_run(
                project="demo-project",
                env_pairs=[("API_KEY", "op://Agents/Demo/api_key")],
                command=cmd,
                allowlist_path=self.allowlist_path,
                audit_path=self.audit_path,
                op_binary=str(self.fake_op_path),
            )
            self.assertEqual(res["status"], "error", f"Komenda {cmd} powinna zostac odrzucona")
            self.assertEqual(res["code"], "COMMAND_NOT_ALLOWED", f"Komenda {cmd} powinna zwrocic COMMAND_NOT_ALLOWED")
            self.assertIn("Preload", res["message"])

        self.assertEqual(len(self._get_fake_op_calls()), 0)

    def test_r2_node_preload_allowlisted_accepted(self):
        """R2: Dozwolony preload Node (obecny na allowliscie commands) jest poprawnie akceptowany."""
        self.allowlist_data["demo-project"]["commands"].extend([
            "node",
            str(self.dummy_script),
            "ts-node/register",
            "./esm-hook.mjs",
        ])
        self.allowlist_path.write_text(json.dumps(self.allowlist_data), encoding="utf-8")

        cmd = ["node", "-r", "ts-node/register", "--import=./esm-hook.mjs", str(self.dummy_script)]
        res = sr.secret_run(
            project="demo-project",
            env_pairs=[("API_KEY", "op://Agents/Demo/api_key")],
            command=cmd,
            allowlist_path=self.allowlist_path,
            audit_path=self.audit_path,
            op_binary=str(self.fake_op_path),
        )
        self.assertEqual(res["status"], "success")
        self.assertEqual(res["exit_code"], 0)
        self.assertEqual(len(self._get_fake_op_calls()), 1)

    def test_r2_node_inspector_and_python_interactive_forbidden_as_shell_not_allowed(self):
        """R2: Flagi inspekcji Node (--inspect, --inspect-brk, --inspect-port) oraz interaktywne Pythona (-i) sa zakazane (SHELL_NOT_ALLOWED)."""
        self.allowlist_data["demo-project"]["commands"].extend(["node", str(self.dummy_script)])
        self.allowlist_path.write_text(json.dumps(self.allowlist_data), encoding="utf-8")

        forbidden_cmds = [
            ["node", "--inspect", str(self.dummy_script)],
            ["node", "--inspect-brk", str(self.dummy_script)],
            ["node", "--inspect-port=9229", str(self.dummy_script)],
            ["node", "--inspect-port", "9229", str(self.dummy_script)],
            [sys.executable, "-i", str(self.dummy_script)],
        ]

        for cmd in forbidden_cmds:
            res = sr.secret_run(
                project="demo-project",
                env_pairs=[("API_KEY", "op://Agents/Demo/api_key")],
                command=cmd,
                allowlist_path=self.allowlist_path,
                audit_path=self.audit_path,
                op_binary=str(self.fake_op_path),
            )
            self.assertEqual(res["status"], "error", f"Komenda {cmd} powinna zostac odrzucona")
            self.assertEqual(res["code"], "SHELL_NOT_ALLOWED", f"Komenda {cmd} powinna zwrocic SHELL_NOT_ALLOWED")

        self.assertEqual(len(self._get_fake_op_calls()), 0)

    def test_r2_python_legitimate_options_with_i_accepted(self):
        """R2: Prawidlowe flagi Pythona zawierajace litere 'i' (np. -Wignore, -Winvalid-mode, -Xint_max_str_digits) nie sa odrzucane."""
        allowed_cmds = [
            [sys.executable, "-Wignore::DeprecationWarning", str(self.dummy_script)],
            [sys.executable, "-Xint_max_str_digits=0", str(self.dummy_script)],
        ]
        for cmd in allowed_cmds:
            res = sr.secret_run(
                project="demo-project",
                env_pairs=[("API_KEY", "op://Agents/Demo/api_key")],
                command=cmd,
                allowlist_path=self.allowlist_path,
                audit_path=self.audit_path,
                op_binary=str(self.fake_op_path),
            )
            self.assertEqual(res["status"], "success", f"Komenda {cmd} powinna zostac zaakceptowana: {res}")
            self.assertEqual(res["exit_code"], 0)

    def test_r3_command_sha256_match_and_mismatch(self):
        """R3: Weryfikacja skrotow SHA-256 skryptow: prawidlowy przechodzi, niezgodny lub brakujacy zwraca COMMAND_HASH_MISMATCH."""
        pinned_script = self.test_dir / "pinned_script.py"
        script_content = b"print('pinned')\n"
        pinned_script.write_bytes(script_content)
        correct_hash = hashlib.sha256(script_content).hexdigest().lower()

        # 1. Prawidlowy hash
        self.allowlist_data["demo-project"]["commands"].append(str(pinned_script))
        self.allowlist_data["demo-project"]["command_sha256"] = {
            str(pinned_script): correct_hash,
        }
        self.allowlist_path.write_text(json.dumps(self.allowlist_data), encoding="utf-8")

        cmd = [sys.executable, str(pinned_script)]
        res_ok = sr.secret_run(
            project="demo-project",
            env_pairs=[("API_KEY", "op://Agents/Demo/api_key")],
            command=cmd,
            allowlist_path=self.allowlist_path,
            audit_path=self.audit_path,
            op_binary=str(self.fake_op_path),
        )
        self.assertEqual(res_ok["status"], "success")

        # 2. Niezgodny hash
        self.allowlist_data["demo-project"]["command_sha256"][str(pinned_script)] = "0" * 64
        self.allowlist_path.write_text(json.dumps(self.allowlist_data), encoding="utf-8")

        res_bad = sr.secret_run(
            project="demo-project",
            env_pairs=[("API_KEY", "op://Agents/Demo/api_key")],
            command=cmd,
            allowlist_path=self.allowlist_path,
            audit_path=self.audit_path,
            op_binary=str(self.fake_op_path),
        )
        self.assertEqual(res_bad["status"], "error")
        self.assertEqual(res_bad["code"], "COMMAND_HASH_MISMATCH")
        self.assertIn("COMMAND_HASH_MISMATCH", res_bad["error"])

        # 3. Brakujacy plik z przypietym hashem
        missing_script = self.test_dir / "missing_script.py"
        self.allowlist_data["demo-project"]["commands"].append(str(missing_script))
        self.allowlist_data["demo-project"]["command_sha256"][str(missing_script)] = correct_hash
        self.allowlist_path.write_text(json.dumps(self.allowlist_data), encoding="utf-8")

        res_missing = sr.secret_run(
            project="demo-project",
            env_pairs=[("API_KEY", "op://Agents/Demo/api_key")],
            command=[sys.executable, str(missing_script)],
            allowlist_path=self.allowlist_path,
            audit_path=self.audit_path,
            op_binary=str(self.fake_op_path),
        )
        self.assertEqual(res_missing["status"], "error")
        self.assertEqual(res_missing["code"], "COMMAND_HASH_MISMATCH")

        # 4. require_script_sha256=True dla skryptu bez wpisu w command_sha256
        unpinned_script = self.test_dir / "unpinned_script.py"
        unpinned_script.write_text("print('unpinned')\n", encoding="utf-8")
        self.allowlist_data["demo-project"]["commands"].append(str(unpinned_script))
        self.allowlist_data["demo-project"]["require_script_sha256"] = True
        self.allowlist_path.write_text(json.dumps(self.allowlist_data), encoding="utf-8")

        res_req = sr.secret_run(
            project="demo-project",
            env_pairs=[("API_KEY", "op://Agents/Demo/api_key")],
            command=[sys.executable, str(unpinned_script)],
            allowlist_path=self.allowlist_path,
            audit_path=self.audit_path,
            op_binary=str(self.fake_op_path),
        )
        self.assertEqual(res_req["status"], "error")
        self.assertEqual(res_req["code"], "COMMAND_HASH_MISMATCH")

        # 5. command_sha256 niebedace slownikiem zwraca ALLOWLIST_INVALID
        self.allowlist_data["demo-project"]["command_sha256"] = "invalid_string"
        self.allowlist_path.write_text(json.dumps(self.allowlist_data), encoding="utf-8")

        res_inv = sr.secret_run(
            project="demo-project",
            env_pairs=[("API_KEY", "op://Agents/Demo/api_key")],
            command=[sys.executable, str(self.dummy_script)],
            allowlist_path=self.allowlist_path,
            audit_path=self.audit_path,
            op_binary=str(self.fake_op_path),
        )
        self.assertEqual(res_inv["status"], "error")
        self.assertEqual(res_inv["code"], "ALLOWLIST_INVALID")

    def test_r4_allowlist_override_forbidden_with_non_py_op(self):
        """R4: Nadpisanie SECRET_RUN_ALLOWLIST_PATH z binarka op inna niz testowa atrapa .py zwraca ALLOWLIST_OVERRIDE_FORBIDDEN."""
        # Test 1: op_binary="op" (nie konczy sie na .py)
        res = sr.secret_run(
            project="demo-project",
            env_pairs=[("API_KEY", "op://Agents/Demo/api_key")],
            command=[sys.executable, str(self.dummy_script)],
            allowlist_path=None,  # brak jawnej sciezki
            op_binary="op",
        )
        self.assertEqual(res["status"], "error")
        self.assertEqual(res["code"], "ALLOWLIST_OVERRIDE_FORBIDDEN")
        self.assertEqual(res["error"], "ALLOWLIST_OVERRIDE_FORBIDDEN")
        self.assertIn("syntetyczna atrapa op (.py)", res["message"])
        self.assertEqual(res["exit_code"], 1)

        # Test 2: SECRET_RUN_OP="op.exe" w srodowisku
        os.environ["SECRET_RUN_OP"] = "op.exe"
        res2 = sr.secret_run(
            project="demo-project",
            env_pairs=[("API_KEY", "op://Agents/Demo/api_key")],
            command=[sys.executable, str(self.dummy_script)],
            allowlist_path=None,
        )
        self.assertEqual(res2["status"], "error")
        self.assertEqual(res2["code"], "ALLOWLIST_OVERRIDE_FORBIDDEN")

        # Test 3: syntetyczna atrapa .py jest dozwolona
        os.environ["SECRET_RUN_OP"] = str(self.fake_op_path)
        res3 = sr.secret_run(
            project="demo-project",
            env_pairs=[("API_KEY", "op://Agents/Demo/api_key")],
            command=[sys.executable, str(self.dummy_script)],
            allowlist_path=None,
        )
        self.assertEqual(res3["status"], "success")

    def test_r5_windows_winget_fallback_discovery(self):
        """R5: Weryfikacja rezerwowego przeszukiwania sciezki op.exe na Windows."""
        # Usun zmienna SECRET_RUN_OP
        if "SECRET_RUN_OP" in os.environ:
            del os.environ["SECRET_RUN_OP"]

        # Symulacja Windows z brakiem 'op' w PATH
        with mock.patch("sys.platform", "win32"), mock.patch("shutil.which", return_value=None):
            # Test 1: WinGet Links
            with mock.patch.dict(os.environ, {
                "LOCALAPPDATA": str(self.test_dir / "appdata"),
                "PROGRAMFILES": str(self.test_dir / "progfiles"),
            }):
                links_path = Path(self.test_dir / "appdata") / "Microsoft" / "WinGet" / "Links" / "op.exe"
                links_path.parent.mkdir(parents=True, exist_ok=True)
                links_path.write_text("dummy", encoding="utf-8")

                resolved = sr.resolve_op_binary()
                self.assertEqual(resolved, str(links_path))

            # Test 2: WinGet Packages
            with mock.patch.dict(os.environ, {
                "LOCALAPPDATA": str(self.test_dir / "pkg_appdata"),
                "PROGRAMFILES": str(self.test_dir / "progfiles"),
            }):
                pkg_path = Path(self.test_dir / "pkg_appdata") / "Microsoft" / "WinGet" / "Packages" / "AgileBits.1Password.CLI_123" / "op.exe"
                pkg_path.parent.mkdir(parents=True, exist_ok=True)
                pkg_path.write_text("dummy", encoding="utf-8")

                resolved_pkg = sr.resolve_op_binary()
                self.assertEqual(resolved_pkg, str(pkg_path))

            # Test 3: Program Files fallback gdy brak w WinGet
            with mock.patch.dict(os.environ, {
                "LOCALAPPDATA": str(self.test_dir / "empty_appdata"),
                "PROGRAMFILES": str(self.test_dir / "progfiles"),
            }):
                prog_path = Path(self.test_dir / "progfiles") / "1Password CLI" / "op.exe"
                prog_path.parent.mkdir(parents=True, exist_ok=True)
                prog_path.write_text("dummy", encoding="utf-8")

                resolved_prog = sr.resolve_op_binary()
                self.assertEqual(resolved_prog, str(prog_path))

    def test_r6_allowlist_example_loads_cleanly(self):
        """R6: Publiczny przyklad laduje sie ze skladniowymi referencjami i pozostaje bez aktywacji."""
        example_path = SCRIPT_DIR / "allowlist.example.json"
        self.assertTrue(example_path.exists(), "allowlist.example.json musi istniec")

        data, err = sr.load_allowlist(example_path)
        self.assertIsNone(err, f"Blad ladowania allowlist.example.json: {err}")
        self.assertIsNotNone(data)

        # Public, inert example; operator policies are not shipped.
        required_projects = ["example-project"]
        self.assertEqual(set(data), set(required_projects))

        for proj in required_projects:
            self.assertIn(proj, data, f"Projekt '{proj}' musi znajdowac sie w allowlist.example.json")
            proj_conf = data[proj]
            self.assertIn("refs", proj_conf)
            self.assertIsInstance(proj_conf["refs"], list)
            self.assertGreater(len(proj_conf["refs"]), 0)
            for ref in proj_conf["refs"]:
                self.assertTrue(ref.startswith("op://Agents/"), f"Referencja '{ref}' musi zaczynac sie od op://Agents/")

            self.assertIn("commands", proj_conf)
            self.assertIsInstance(proj_conf["commands"], list)
            self.assertGreater(len(proj_conf["commands"]), 0)

            self.assertIn("command_sha256", proj_conf)
            self.assertIsInstance(proj_conf["command_sha256"], dict)
            self.assertGreater(len(proj_conf["command_sha256"]), 0)
            self.assertIs(proj_conf["require_script_sha256"], True)
            for digest in proj_conf["command_sha256"].values():
                self.assertRegex(digest, r"^[0-9a-f]{64}$")

            # Test walidacji referencji
            first_ref = proj_conf["refs"][0]
            val_ok, val_err = sr.validate_project_and_refs(proj, [("TEST_VAR", first_ref)], data)
            self.assertTrue(val_ok, f"Walidacja referencji dla {proj} nie powiodla sie: {val_err}")


if __name__ == "__main__":
    unittest.main()
