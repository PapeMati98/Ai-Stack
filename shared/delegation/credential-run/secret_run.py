#!/usr/bin/env python3
"""
secret_run.py - Uruchamianie komend z poswiadczeniami 1Password bez wgladu agenta.

Cel T5 (Fala 1 i Fala 2): agent uruchamia komende z poswiadczeniem bez poznania wartosci sekretu.
Prawdziwe sekrety sa wstrzykiwane wylacznie w procesie 'op run' 1Password CLI
z wlaczonym automatycznym maskowaniem.

Wymagania i zasady bezpieczenstwa (V2):
1. CLI: secret_run.py run --project <slug> --env NAZWA=op://<sejf>/<pozycja>/<pole> [--env ...] -- <komenda> [arg...]
2. Allowlista: ~/.agents/secrets/allowlist.json w nowym formacie per projekt:
   {"<projekt>": {"refs": ["op://..."], "commands": ["<sciezka-lub-nazwa-programu>", ...]}}
   Referencja spoza allowlisty -> odmowa przed startem (REF_NOT_ALLOWED).
   Stary format allowlisty (lista referencji) -> odmowa z komunikatem o migracji (ALLOWLIST_FORMAT_OLD).
   Brak allowlisty -> odmowa (ALLOWLIST_MISSING).
3. Komenda potomna (argv[0], po rozwiazaniu nazwy) oraz skrypt musza znajdowac sie na liscie 'commands'
   projektu, inaczej odmowa przed startem (COMMAND_NOT_ALLOWED). Dopasowanie sciezki skryptu jest scisle
   (sama nazwa nie dopasowuje pelnej sciezki z innego katalogu).
4. Zakazane interpretery-ogolniki i kod inline nawet jesli znajduja sie na allowliscie:
   'cmd', 'powershell', 'pwsh', 'bash', 'sh', 'python -c', 'node -e', 'node --eval' itp. (SHELL_NOT_ALLOWED).
   Skrypt z pliku ('python sciezka.py') jest dozwolony wylacznie wtedy, gdy 'python' i ta sciezka sa na liscie.
5. Zakazane operacje: podkomendy ujawniajace wartosc ('read', 'get', 'reveal', 'inject' do stdout)
   oraz flaga '--no-masking'. Skrypt nie ma trybu 'pokaz wartosc'.
6. Maskowanie: wbudowane maskowanie 'op run' zastepuje wystapienia sekretu znacznikiem
   <concealed by 1Password>; wartosc sekretu nigdy nie trafia do stdout/stderr skryptu, argv ani logow.
7. Audyt: ~/.agents/secrets/audit.jsonl rejestruje czas, projekt, referencje (bez wartosci),
   komenda[0], kod wyjscia oraz czas trwania.
8. Sciezka op: zmienna SECRET_RUN_OP (atrapa w testach) lub 'op' z PATH. Brak op -> czytelny blad OP_CLI_MISSING.
9. Procesy potomne uruchamiane wylacznie bez okien konsoli (CREATE_NO_WINDOW na Windows).
10. Tylko stdlib Pythona, bez usuwania plikow.
11. R1: Sanityzacja srodowiska (usuwanie odziedziczonych op:// oraz niebezpiecznych zmiennych)
    oraz zakaz op:// w argumentach potomnych (FORBIDDEN_OPERATION).
12. R2: Walidacja opcji preload Node oraz zakaz opcji diagnostycznych i trybu interaktywnego (SHELL_NOT_ALLOWED).
13. R3: Opcjonalne przypinanie skrotow SHA-256 skryptow (command_sha256, COMMAND_HASH_MISMATCH).
14. R4: Straznik nadpisywania SECRET_RUN_ALLOWLIST_PATH (ALLOWLIST_OVERRIDE_FORBIDDEN).
15. R5: Rezerwowe wykrywanie binarnego op.exe na Windows (WinGet / Program Files).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

# Sciezki domyslne (konfigurowalne przez zmienne srodowiskowe dla celow testowych)
DEFAULT_ALLOWLIST_PATH = Path.home() / ".agents" / "secrets" / "allowlist.json"
DEFAULT_AUDIT_PATH = Path.home() / ".agents" / "secrets" / "audit.jsonl"

CONCEALED_MARKER = "<concealed by 1Password>"
FORBIDDEN_OPERATIONS = {"read", "get", "reveal", "inject"}

# R1: Niebezpieczne zmienne srodowiskowe wstrzykiwania kodu
DANGEROUS_ENV_VARS = {
    "NODE_OPTIONS",
    "PYTHONSTARTUP",
    "PYTHONPATH",
    "PYTHONINSPECT",
    "BASH_ENV",
    "ENV",
    "PROMPT_COMMAND",
}

# Zakazane powloki-ogolniki
FORBIDDEN_SHELLS = {
    "cmd", "cmd.exe",
    "powershell", "powershell.exe",
    "pwsh", "pwsh.exe",
    "bash", "bash.exe",
    "sh", "sh.exe",
    "zsh", "zsh.exe",
    "csh", "csh.exe",
    "tcsh", "tcsh.exe",
    "ksh", "ksh.exe",
    "fish", "fish.exe",
    "wsl", "wsl.exe",
}

PYTHON_INTERPRETERS = {"python", "python3", "py"}
NODE_INTERPRETERS = {"node", "nodejs"}

# R2: Opcje preload i zakazane opcje Node / Python
NODE_PRELOAD_OPTIONS = {
    "-r",
    "--require",
    "--import",
    "--loader",
    "--experimental-loader",
    "--env-file",
    "--env-file-if-exists",
}
NODE_FORBIDDEN_OPTIONS = {
    "--inspect",
    "--inspect-brk",
    "--inspect-port",
}
PYTHON_FORBIDDEN_OPTIONS = {
    "-i",
}

# Opcje interpreterow, ktore przyjmuja wartosc jako nastepny token (lub z =)
PYTHON_OPTIONS_WITH_VALUE = {
    "-W",
    "-X",
    "--check-hash-based-pycs",
    "--pycache-prefix",
}

NODE_OPTIONS_WITH_VALUE = {
    "-r",
    "--require",
    "-C",
    "--conditions",
    "--loader",
    "--experimental-loader",
    "--import",
    "--env-file",
    "--env-file-if-exists",
    "--openssl-config",
    "--tls-cipher-list",
    "--diagnostic-dir",
    "--heapdump-target",
    "--icu-data-dir",
    "--max-old-space-size",
    "--max-semi-space-size",
    "--title",
    "--inspect",
    "--inspect-brk",
    "--inspect-port",
    "--inspect-publish-uid",
    "--test-name-pattern",
    "--test-reporter",
    "--test-reporter-destination",
    "--test-shard",
    "--test-coverage-include",
    "--test-coverage-exclude",
    "--snapshot-blob",
    "--disable-warning",
    "--experimental-default-type",
    "--input-type",
    "--unhandled-rejections",
    "--stack-trace-limit",
    "--stack-size",
    "--max-http-header-size",
    "--tls-keylog",
    "--heapsnapshot-near-heap-limit",
    "--heapsnapshot-signal",
    "--diagnostic-report-directory",
    "--diagnostic-report-filename",
    "--cpu-prof-dir",
    "--cpu-prof-name",
    "--cpu-prof-interval",
    "--heap-prof-dir",
    "--heap-prof-name",
    "--heap-prof-interval",
    "--redirect-warnings",
    "--trace-event-categories",
    "--trace-event-file-pattern",
    "--allow-fs-read",
    "--allow-fs-write",
    "--report-dir",
    "--report-directory",
    "--report-filename",
    "--report-signal",
    "--secure-heap",
    "--secure-heap-min",
    "--disable-proto",
    "--entry-url",
    "--experimental-policy",
    "--policy-integrity",
    "--network-family-autoselection-attempt-timeout",
}


def is_python_inline_code(arg: str) -> bool:
    """
    Wykrywa argumenty wykonania kodu inline dla Pythona (-c, -c=kod, -ckod, -uc itp.).
    """
    if arg == "-c" or arg.startswith("-c"):
        return True
    if not arg.startswith("--") and arg.startswith("-"):
        if len(arg) > 1 and arg[1] in ("W", "X"):
            return False
        if "c" in arg[1:]:
            return True
    return False


def is_node_inline_eval(arg: str) -> bool:
    """
    Wykrywa argumenty wykonania kodu inline dla Node.js (-e, --eval, -p, --print)
    w dowolnym formacie (np. -e, --eval=kod, -e=kod, -p, --print=kod, -pe itp.).
    """
    if arg in ("-e", "--eval", "-p", "--print"):
        return True
    if arg.startswith("--eval=") or arg.startswith("--print="):
        return True
    if arg.startswith("-e=") or arg.startswith("-p="):
        return True
    if not arg.startswith("--") and (arg.startswith("-e") or arg.startswith("-p")):
        return True
    return False


def extract_node_preload_targets(command: list[str]) -> list[str]:
    """
    Skanuje command[1:] do separatora '--' pod katem opcji preload Node (NODE_PRELOAD_OPTIONS)
    zarowno w postaci 'opt val' jak i 'opt=val'. Zwraca liste celow preload.
    Zwraca [''] jesli ktoras flaga nie ma podanej wartosci.
    """
    if len(command) <= 1:
        return []
    targets: list[str] = []
    args = command[1:]
    i = 0
    while i < len(args):
        arg = args[i]
        if arg == "--":
            break
        if "=" in arg:
            opt, sep, val = arg.partition("=")
            if opt in NODE_PRELOAD_OPTIONS:
                val_clean = val.strip()
                if not val_clean:
                    return [""]
                targets.append(val_clean)
            i += 1
            continue
        if arg in NODE_PRELOAD_OPTIONS:
            if i + 1 < len(args) and args[i + 1] != "--":
                val_clean = args[i + 1].strip()
                if not val_clean:
                    return [""]
                targets.append(val_clean)
                i += 2
                continue
            return [""]
        i += 1
    return targets


def get_allowlist_path() -> Path:
    env_path = os.environ.get("SECRET_RUN_ALLOWLIST_PATH")
    if env_path:
        return Path(env_path)
    return DEFAULT_ALLOWLIST_PATH


def get_audit_path() -> Path:
    env_path = os.environ.get("SECRET_RUN_AUDIT_PATH")
    if env_path:
        return Path(env_path)
    return DEFAULT_AUDIT_PATH


def get_creationflags() -> int:
    if sys.platform == "win32":
        return getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
    return 0


def resolve_op_binary(custom_op: str | None = None) -> str | None:
    candidate = custom_op if custom_op is not None else os.environ.get("SECRET_RUN_OP")
    if candidate:
        candidate = candidate.strip()
    if candidate:
        p = Path(candidate)
        if (p.exists() and not p.is_dir()) or shutil.which(candidate):
            return str(p if p.exists() else candidate)
        return None

    which_op = shutil.which("op")
    if which_op:
        return which_op

    # R5: Rezerwowe przeszukiwanie lokalizacji instalacji 1Password CLI na platformie Windows
    if sys.platform == "win32":
        local_app_data = os.environ.get("LOCALAPPDATA")
        if local_app_data:
            # 1. %LOCALAPPDATA%/Microsoft/WinGet/Links/op.exe
            p_links = Path(local_app_data) / "Microsoft" / "WinGet" / "Links" / "op.exe"
            if p_links.is_file():
                return str(p_links)

            # 2. sorted(%LOCALAPPDATA%/Microsoft/WinGet/Packages/AgileBits.1Password.CLI_*/op.exe)
            packages_dir = Path(local_app_data) / "Microsoft" / "WinGet" / "Packages"
            if packages_dir.is_dir():
                pkg_matches = sorted(packages_dir.glob("AgileBits.1Password.CLI_*/op.exe"))
                for p_pkg in pkg_matches:
                    if p_pkg.is_file():
                        return str(p_pkg)

        # 3. %PROGRAMFILES%/1Password CLI/op.exe
        prog_files = os.environ.get("PROGRAMFILES", "C:\\Program Files")
        p_prog = Path(prog_files) / "1Password CLI" / "op.exe"
        if p_prog.is_file():
            return str(p_prog)

    return None


def check_forbidden_args(
    argv: list[str],
    is_child_cmd: bool = False,
) -> tuple[bool, str | None]:
    for arg in argv:
        if arg == "--no-masking":
            return True, "Flaga '--no-masking' jest bezwzglednie zakazana"
        if arg == "--reveal":
            return True, "Flaga '--reveal' jest bezwzglednie zakazana (skrypt nie udostepnia trybu ujawniania)"

    if not is_child_cmd and argv:
        first = argv[0].lower().strip()
        if first in FORBIDDEN_OPERATIONS:
            return True, f"Podkomenda '{argv[0]}' jest zakazana (skrypt nie ma trybu ujawniania sekretow)"

    if is_child_cmd and argv:
        # R1: Odrzucanie tokenow op:// w argumentach procesu potomnego
        for arg in argv:
            if "op://" in arg.lower():
                return True, f"Przekazywanie referencji 'op://' w argumentach komendy ('{arg}') jest zakazane (FORBIDDEN_OPERATION)"
        first_token = Path(argv[0]).name.lower()
        if first_token in ("op", "op.exe"):
            for tok in argv[1:]:
                if tok.lower() in FORBIDDEN_OPERATIONS or tok.lower() in ("--reveal", "-r"):
                    return True, f"Wywolanie 'op {tok}' wewnatrz secret_run jest zakazane"

    return False, None


def load_allowlist(allowlist_path: Path) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    if not allowlist_path.exists():
        return None, {
            "status": "error",
            "error": "ALLOWLIST_MISSING",
            "code": "ALLOWLIST_MISSING",
            "message": f"Brak pliku allowlisty: {allowlist_path}",
        }
    try:
        content = allowlist_path.read_text(encoding="utf-8")
        data = json.loads(content)
        if not isinstance(data, dict):
            return None, {
                "status": "error",
                "error": "ALLOWLIST_INVALID",
                "code": "ALLOWLIST_INVALID",
                "message": "Allowlista musi byc obiektem JSON mapujacym slug projektu na konfiguracje allowlisty",
            }

        # Wykrywanie starego formatu (mapowanie projektu bezposrednio na liste referencji)
        if any(isinstance(v, list) for v in data.values()):
            return None, {
                "status": "error",
                "error": "ALLOWLIST_FORMAT_OLD",
                "code": "ALLOWLIST_FORMAT_OLD",
                "message": (
                    "Wykryto stary format allowlisty (lista referencji). "
                    "Wymagana migracja do nowego formatu ze sciezka komend: "
                    '{"<projekt>": {"refs": ["op://..."], "commands": ["<program-lub-sciezka>", ...]}}'
                ),
            }

        return data, None
    except Exception as e:
        return None, {
            "status": "error",
            "error": "ALLOWLIST_INVALID",
            "code": "ALLOWLIST_INVALID",
            "message": f"Blad parsowania pliku allowlisty: {e}",
        }


def normalize_path_str(p: str) -> str:
    norm = p.replace("\\", "/").strip().lower()
    if norm.startswith("./"):
        norm = norm[2:]
    return norm


def match_command_token(token: str, allowed_list: list[str]) -> bool:
    token_clean = token.strip()
    token_norm = normalize_path_str(token_clean)
    token_path = Path(token_clean)
    token_has_slash = "/" in token_norm or "\\" in token_clean

    resolved_token = None
    try:
        resolved_token = str(token_path.resolve()).lower()
    except Exception:
        pass

    which_token = None
    if not token_has_slash and ("." not in token_clean or token_clean.lower().endswith((".exe", ".bat", ".cmd"))):
        try:
            wt = shutil.which(token_clean)
            if wt:
                which_token = str(Path(wt).resolve()).lower()
        except Exception:
            pass

    for entry in allowed_list:
        if not isinstance(entry, str):
            continue
        entry_clean = entry.strip()
        entry_norm = normalize_path_str(entry_clean)
        entry_path = Path(entry_clean)
        entry_has_slash = "/" in entry_norm or "\\" in entry_clean

        # 1. Znormalizowany string (dokladne dopasowanie sciezek lub nazw)
        if token_norm == entry_norm:
            return True

        # 2. Nazwy binarne bez slashy rozniace sie wylacznie rozszerzeniem .exe (np. python i python.exe)
        if not token_has_slash and not entry_has_slash:
            if token_norm.endswith(".exe") and token_norm[:-4] == entry_norm and "." not in entry_norm:
                return True
            if entry_norm.endswith(".exe") and entry_norm[:-4] == token_norm and "." not in token_norm:
                return True

        # 3. Rozwiazana absolutna sciezka na dysku (obie musza wskazywac na ten sam plik)
        resolved_entry = None
        try:
            resolved_entry = str(entry_path.resolve()).lower()
        except Exception:
            pass

        if resolved_token and resolved_entry and resolved_token == resolved_entry:
            return True

        # 4. Rozwiazanie binarne z PATH (shutil.which) dla programow bez slashy
        if which_token and resolved_entry and which_token == resolved_entry:
            return True

        if not entry_has_slash and ("." not in entry_clean or entry_clean.lower().endswith((".exe", ".bat", ".cmd"))):
            try:
                we = shutil.which(entry_clean)
                if we:
                    resolved_we = str(Path(we).resolve()).lower()
                    if resolved_token and resolved_token == resolved_we:
                        return True
                    if which_token and which_token == resolved_we:
                        return True
            except Exception:
                pass

    return False


def extract_script_and_args(command: list[str]) -> tuple[str | None, str | None]:
    """
    Zwraca (script_path, module_name) dla interpreterow python/node.
    Jesli wykryto kod inline (-c / -e / --eval itp.), zwraca (None, None).
    """
    if len(command) <= 1:
        return None, None

    argv0_stem = Path(command[0]).stem.lower()
    args = command[1:]

    if argv0_stem in PYTHON_INTERPRETERS:
        # Skanowanie pod katem kodu inline przed ewentualnym '--'
        for arg in args:
            if arg == "--":
                break
            if is_python_inline_code(arg):
                return None, None

        i = 0
        while i < len(args):
            arg = args[i]
            if arg == "-m":
                if i + 1 < len(args):
                    return None, args[i + 1]
                return None, None
            if arg == "--":
                if i + 1 < len(args):
                    return args[i + 1], None
                return None, None
            if arg.startswith("-"):
                if arg in PYTHON_OPTIONS_WITH_VALUE and i + 1 < len(args):
                    i += 2
                    continue
                i += 1
                continue
            return arg, None
        return None, None

    if argv0_stem in NODE_INTERPRETERS:
        # Skanowanie pod katem kodu inline przed ewentualnym '--'
        for arg in args:
            if arg == "--":
                break
            if is_node_inline_eval(arg):
                return None, None

        i = 0
        while i < len(args):
            arg = args[i]
            if arg == "--":
                if i + 1 < len(args):
                    return args[i + 1], None
                return None, None
            if arg.startswith("-"):
                if arg in NODE_OPTIONS_WITH_VALUE and i + 1 < len(args):
                    i += 2
                    continue
                i += 1
                continue
            return arg, None
        return None, None

    return None, None


def verify_script_sha256(
    project: str,
    script_path: str,
    allowed_for_project: dict[str, Any],
) -> tuple[bool, dict[str, Any] | None]:
    """
    Weryfikuje skrot SHA-256 skryptu na podstawie wpisu command_sha256 oraz require_script_sha256.
    """
    command_sha256 = allowed_for_project.get("command_sha256")
    if command_sha256 is None:
        command_sha256 = {}
    require_script_sha256 = bool(allowed_for_project.get("require_script_sha256", False))

    sp_clean = script_path.strip()
    sp_norm = normalize_path_str(sp_clean)
    sp_resolved = None
    try:
        sp_resolved = str(Path(sp_clean).resolve()).lower()
    except Exception:
        pass

    expected_hash: str | None = None
    for k, v in command_sha256.items():
        if not isinstance(k, str):
            continue
        k_clean = k.strip()
        k_norm = normalize_path_str(k_clean)
        if sp_clean == k_clean or sp_norm == k_norm:
            expected_hash = str(v).strip().lower()
            break
        try:
            k_resolved = str(Path(k_clean).resolve()).lower()
            if sp_resolved and k_resolved and sp_resolved == k_resolved:
                expected_hash = str(v).strip().lower()
                break
        except Exception:
            pass

    if expected_hash is None:
        if require_script_sha256:
            return False, {
                "status": "error",
                "error": "COMMAND_HASH_MISMATCH",
                "code": "COMMAND_HASH_MISMATCH",
                "message": f"Wymagany skrot SHA-256 dla skryptu '{script_path}' nie zostal zdefiniowany w command_sha256 dla projektu '{project}'",
                "project": project,
                "command": script_path,
            }
        return True, None

    p = Path(sp_clean)
    try:
        if not p.is_file():
            return False, {
                "status": "error",
                "error": "COMMAND_HASH_MISMATCH",
                "code": "COMMAND_HASH_MISMATCH",
                "message": f"Plik skryptu '{script_path}' nie istnieje lub nie jest plikiem (COMMAND_HASH_MISMATCH)",
                "project": project,
                "command": script_path,
            }
        actual_hash = hashlib.sha256(p.read_bytes()).hexdigest().lower()
    except Exception as e:
        return False, {
            "status": "error",
            "error": "COMMAND_HASH_MISMATCH",
            "code": "COMMAND_HASH_MISMATCH",
            "message": f"Blad odczytu pliku skryptu '{script_path}' do obliczenia SHA-256: {e}",
            "project": project,
            "command": script_path,
        }

    if actual_hash != expected_hash:
        return False, {
            "status": "error",
            "error": "COMMAND_HASH_MISMATCH",
            "code": "COMMAND_HASH_MISMATCH",
            "message": f"Skrot SHA-256 pliku '{script_path}' ({actual_hash}) nie zgadza sie z oczekiwanym ({expected_hash})",
            "project": project,
            "command": script_path,
        }

    return True, None


def validate_project_and_refs(
    project: str,
    env_pairs: list[tuple[str, str]],
    allowlist: dict[str, Any],
) -> tuple[bool, dict[str, Any] | None]:
    allowed_for_project = allowlist.get(project)
    if allowed_for_project is None:
        return False, {
            "status": "error",
            "error": "REF_NOT_ALLOWED",
            "code": "REF_NOT_ALLOWED",
            "message": f"Projekt '{project}' nie znajduje sie na allowliscie",
            "project": project,
        }

    if isinstance(allowed_for_project, list):
        return False, {
            "status": "error",
            "error": "ALLOWLIST_FORMAT_OLD",
            "code": "ALLOWLIST_FORMAT_OLD",
            "message": (
                f"Wykryto stary format allowlisty dla projektu '{project}' (lista referencji). "
                "Wymagana migracja do nowego formatu ze sciezka komend: "
                '{"<projekt>": {"refs": ["op://..."], "commands": ["<program-lub-sciezka>", ...]}}'
            ),
            "project": project,
        }

    if not isinstance(allowed_for_project, dict):
        return False, {
            "status": "error",
            "error": "ALLOWLIST_INVALID",
            "code": "ALLOWLIST_INVALID",
            "message": f"Wpis allowlisty dla projektu '{project}' musi byc obiektem z polami 'refs' i 'commands'",
            "project": project,
        }

    refs = allowed_for_project.get("refs")
    if not isinstance(refs, list):
        return False, {
            "status": "error",
            "error": "ALLOWLIST_INVALID",
            "code": "ALLOWLIST_INVALID",
            "message": f"Pole 'refs' dla projektu '{project}' musi byc lista referencji",
            "project": project,
        }

    commands = allowed_for_project.get("commands")
    if not isinstance(commands, list):
        return False, {
            "status": "error",
            "error": "ALLOWLIST_INVALID",
            "code": "ALLOWLIST_INVALID",
            "message": f"Pole 'commands' dla projektu '{project}' musi byc lista dozwolonych komend",
            "project": project,
        }

    for var_name, ref in env_pairs:
        if ref not in refs:
            return False, {
                "status": "error",
                "error": "REF_NOT_ALLOWED",
                "code": "REF_NOT_ALLOWED",
                "message": f"Referencja '{ref}' nie znajduje sie na allowliscie dla projektu '{project}'",
                "project": project,
                "ref": ref,
                "var": var_name,
            }

    return True, None


def validate_project_command(
    project: str,
    command: list[str],
    allowlist: dict[str, Any],
) -> tuple[bool, dict[str, Any] | None]:
    if not command:
        return False, {
            "status": "error",
            "error": "COMMAND_MISSING",
            "code": "COMMAND_MISSING",
            "message": "Nie podano komendy do wykonania",
        }

    raw_argv0 = command[0].strip()
    p0 = Path(raw_argv0)
    name0 = p0.name.lower()
    stem0 = p0.stem.lower()

    # 1. Zakazane powloki-ogolniki (nawet jesli bylyby na liscie) -> SHELL_NOT_ALLOWED
    if name0 in FORBIDDEN_SHELLS or stem0 in FORBIDDEN_SHELLS:
        return False, {
            "status": "error",
            "error": "SHELL_NOT_ALLOWED",
            "code": "SHELL_NOT_ALLOWED",
            "message": f"Uruchamianie powloki ('{command[0]}') jest bezwzglednie zakazane ze wzgledow bezpieczenstwa (SHELL_NOT_ALLOWED)",
            "project": project,
            "command": command[0],
        }

    # 2. Zakazany kod inline (python -c, node -e / --eval itp.) -> SHELL_NOT_ALLOWED
    if stem0 in PYTHON_INTERPRETERS:
        for arg in command[1:]:
            if arg == "--":
                break
            if is_python_inline_code(arg):
                return False, {
                    "status": "error",
                    "error": "SHELL_NOT_ALLOWED",
                    "code": "SHELL_NOT_ALLOWED",
                    "message": "Wykonywanie kodu inline Pythona ('python -c') jest bezwzglednie zakazane (SHELL_NOT_ALLOWED)",
                    "project": project,
                    "command": command[0],
                }

    if stem0 in NODE_INTERPRETERS:
        for arg in command[1:]:
            if arg == "--":
                break
            if is_node_inline_eval(arg):
                return False, {
                    "status": "error",
                    "error": "SHELL_NOT_ALLOWED",
                    "code": "SHELL_NOT_ALLOWED",
                    "message": "Wykonywanie kodu inline Node ('node -e' / '--eval') jest bezwzglednie zakazane (SHELL_NOT_ALLOWED)",
                    "project": project,
                    "command": command[0],
                }

    # R2: Weryfikacja opcji preload dla Node (przed sprawdzeniem trybu REPL)
    if stem0 in NODE_INTERPRETERS:
        preload_targets = extract_node_preload_targets(command)
        if preload_targets:
            allowed_for_project_pre = allowlist.get(project)
            allowed_commands_pre = (
                allowed_for_project_pre.get("commands", [])
                if isinstance(allowed_for_project_pre, dict)
                else []
            )
            for target in preload_targets:
                if not target or not match_command_token(target, allowed_commands_pre):
                    return False, {
                        "status": "error",
                        "error": "COMMAND_NOT_ALLOWED",
                        "code": "COMMAND_NOT_ALLOWED",
                        "message": f"Preload '{target}' nie znajduje sie na allowliscie commands dla projektu '{project}'",
                        "project": project,
                        "command": target,
                    }

    # Sprawdzenie czy interpreter nie zostal wywolany w trybie interaktywnym (REPL)
    if stem0 in PYTHON_INTERPRETERS or stem0 in NODE_INTERPRETERS:
        script_path, module_name = extract_script_and_args(command)
        if script_path is None and module_name is None:
            return False, {
                "status": "error",
                "error": "SHELL_NOT_ALLOWED",
                "code": "SHELL_NOT_ALLOWED",
                "message": f"Uruchamianie interpretera '{command[0]}' bez skryptu (tryb interaktywny) jest zakazane (SHELL_NOT_ALLOWED)",
                "project": project,
                "command": command[0],
            }
    else:
        script_path, module_name = None, None

    # R2: Odrzucenie zakazanych opcji interpretera (inspektor Node, tryb interaktywny Pythona)
    if stem0 in PYTHON_INTERPRETERS:
        for arg in command[1:]:
            if arg == "--":
                break
            opt = arg.partition("=")[0].strip()
            if opt in PYTHON_FORBIDDEN_OPTIONS:
                return False, {
                    "status": "error",
                    "error": "SHELL_NOT_ALLOWED",
                    "code": "SHELL_NOT_ALLOWED",
                    "message": f"Opcja interaktywna Pythona ('{arg}') jest bezwzglednie zakazana (SHELL_NOT_ALLOWED)",
                    "project": project,
                    "command": command[0],
                }

    if stem0 in NODE_INTERPRETERS:
        for arg in command[1:]:
            if arg == "--":
                break
            opt = arg.partition("=")[0].strip()
            if opt in NODE_FORBIDDEN_OPTIONS:
                return False, {
                    "status": "error",
                    "error": "SHELL_NOT_ALLOWED",
                    "code": "SHELL_NOT_ALLOWED",
                    "message": f"Opcja inspektora Node ('{arg}') jest bezwzglednie zakazana (SHELL_NOT_ALLOWED)",
                    "project": project,
                    "command": command[0],
                }

    # 3. Weryfikacja allowlisty commands dla projektu
    allowed_for_project = allowlist.get(project)
    if not isinstance(allowed_for_project, dict):
        return False, {
            "status": "error",
            "error": "ALLOWLIST_INVALID",
            "code": "ALLOWLIST_INVALID",
            "message": f"Brak konfiguracji projektu '{project}' w allowliscie",
            "project": project,
        }

    allowed_commands = allowed_for_project.get("commands", [])
    if not isinstance(allowed_commands, list):
        return False, {
            "status": "error",
            "error": "ALLOWLIST_INVALID",
            "code": "ALLOWLIST_INVALID",
            "message": f"Pole 'commands' dla projektu '{project}' musi byc lista",
            "project": project,
        }

    # R3: Sprawdzenie poprawnosci typu command_sha256
    command_sha256 = allowed_for_project.get("command_sha256")
    if command_sha256 is not None and not isinstance(command_sha256, dict):
        return False, {
            "status": "error",
            "error": "ALLOWLIST_INVALID",
            "code": "ALLOWLIST_INVALID",
            "message": f"Pole 'command_sha256' dla projektu '{project}' musi byc obiektem (slownikiem)",
            "project": project,
        }

    # Sprawdzenie argv[0]
    if not match_command_token(raw_argv0, allowed_commands):
        return False, {
            "status": "error",
            "error": "COMMAND_NOT_ALLOWED",
            "code": "COMMAND_NOT_ALLOWED",
            "message": f"Komenda '{command[0]}' nie znajduje sie na allowliscie commands dla projektu '{project}'",
            "project": project,
            "command": command[0],
        }

    # R2 & R3: Sprawdzenie opcji preload dla Node
    if stem0 in NODE_INTERPRETERS:
        preload_targets = extract_node_preload_targets(command)
        for target in preload_targets:
            if not target or not match_command_token(target, allowed_commands):
                return False, {
                    "status": "error",
                    "error": "COMMAND_NOT_ALLOWED",
                    "code": "COMMAND_NOT_ALLOWED",
                    "message": f"Preload '{target}' nie znajduje sie na allowliscie commands dla projektu '{project}'",
                    "project": project,
                    "command": target,
                }
            valid_hash, hash_err = verify_script_sha256(project, target, allowed_for_project)
            if not valid_hash:
                return False, hash_err

    # Jesli interpreter wykonuje skrypt z pliku: zarowno interpreter jak i skrypt musza byc na liscie
    if script_path is not None:
        full_pair = f"{command[0]} {script_path}"
        if not match_command_token(script_path, allowed_commands) and not match_command_token(full_pair, allowed_commands):
            return False, {
                "status": "error",
                "error": "COMMAND_NOT_ALLOWED",
                "code": "COMMAND_NOT_ALLOWED",
                "message": f"Skrypt '{script_path}' nie znajduje sie na allowliscie commands dla projektu '{project}'",
                "project": project,
                "command": script_path,
            }
        # R3: Weryfikacja SHA-256 skryptu
        valid_hash, hash_err = verify_script_sha256(project, script_path, allowed_for_project)
        if not valid_hash:
            return False, hash_err

    if module_name is not None:
        full_pair = f"{command[0]} -m {module_name}"
        if not match_command_token(module_name, allowed_commands) and not match_command_token(full_pair, allowed_commands):
            return False, {
                "status": "error",
                "error": "COMMAND_NOT_ALLOWED",
                "code": "COMMAND_NOT_ALLOWED",
                "message": f"Modul '{module_name}' nie znajduje sie na allowliscie commands dla projektu '{project}'",
                "project": project,
                "command": module_name,
            }

    return True, None


def validate_references(
    project: str,
    env_pairs: list[tuple[str, str]],
    allowlist_path: Path,
) -> tuple[bool, dict[str, Any] | None]:
    allowlist, err = load_allowlist(allowlist_path)
    if err:
        return False, err
    assert allowlist is not None
    return validate_project_and_refs(project, env_pairs, allowlist)


def record_audit(
    audit_path: Path,
    project: str,
    references: list[str],
    command: list[str],
    exit_code: int,
    duration_seconds: float,
) -> None:
    try:
        audit_path.parent.mkdir(parents=True, exist_ok=True)
        record = {
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "project": project,
            "references": list(references),
            "command": command[0] if command else "",
            "exit_code": exit_code,
            "duration_seconds": round(duration_seconds, 4),
            "duration_ms": round(duration_seconds * 1000, 2),
        }
        with open(audit_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception as e:
        sys.stderr.write(f"[secret_run] Ostrzezenie: blad zapisu audytu ({e})\n")


def secret_run(
    project: str,
    env_pairs: list[tuple[str, str]] | dict[str, str],
    command: list[str],
    allowlist_path: Path | None = None,
    audit_path: Path | None = None,
    op_binary: str | None = None,
) -> dict[str, Any]:
    # 1. Walidacja obecnosci komendy i zakazanych operacji ogolnych
    if not command:
        return {
            "status": "error",
            "error": "COMMAND_MISSING",
            "code": "COMMAND_MISSING",
            "message": "Nie podano komendy do wykonania",
            "exit_code": 1,
        }

    is_forbidden, forbidden_msg = check_forbidden_args(command, is_child_cmd=True)
    if is_forbidden:
        return {
            "status": "error",
            "error": "FORBIDDEN_OPERATION",
            "code": "FORBIDDEN_OPERATION",
            "message": forbidden_msg,
            "exit_code": 1,
        }

    # R4: Straznik nadpisywania SECRET_RUN_ALLOWLIST_PATH
    if allowlist_path is None and os.environ.get("SECRET_RUN_ALLOWLIST_PATH"):
        candidate_op = op_binary if op_binary is not None else os.environ.get("SECRET_RUN_OP", "")
        if not str(candidate_op).strip().lower().endswith(".py"):
            return {
                "status": "error",
                "error": "ALLOWLIST_OVERRIDE_FORBIDDEN",
                "code": "ALLOWLIST_OVERRIDE_FORBIDDEN",
                "message": "Nadpisywanie SECRET_RUN_ALLOWLIST_PATH jest dozwolone wylacznie z syntetyczna atrapa op (.py)",
                "exit_code": 1,
            }

    pairs: list[tuple[str, str]]
    if isinstance(env_pairs, dict):
        pairs = list(env_pairs.items())
    else:
        pairs = list(env_pairs)

    # 2. Ladowanie allowlisty (wykrywa brak pliku oraz stary format)
    target_allowlist = allowlist_path or get_allowlist_path()
    allowlist, err = load_allowlist(target_allowlist)
    if err:
        err["exit_code"] = 1
        return err
    assert allowlist is not None

    # 3. Walidacja referencji przeciw allowliscie
    valid_refs, err_refs = validate_project_and_refs(project, pairs, allowlist)
    if not valid_refs:
        assert err_refs is not None
        err_refs["exit_code"] = 1
        return err_refs

    # 4. Walidacja komendy potomnej przeciw allowliscie commands i zakazowi powlok/kodu inline
    valid_cmd, err_cmd = validate_project_command(project, command, allowlist)
    if not valid_cmd:
        assert err_cmd is not None
        err_cmd["exit_code"] = 1
        return err_cmd

    # 5. Weryfikacja obecnosci 1Password CLI 'op'
    resolved_op = resolve_op_binary(op_binary)
    if not resolved_op:
        return {
            "status": "error",
            "error": "OP_CLI_MISSING",
            "code": "OP_CLI_MISSING",
            "message": (
                "Nie znaleziono 1Password CLI ('op'). "
                "Instrukcja dla Mateusza: Zainstaluj 1Password CLI "
                "(https://developer.1password.com/docs/cli/get-started/) "
                "oraz wlacz integracje w aplikacji desktop 1Password "
                "(Settings -> Developer -> Integrate with 1Password CLI)."
            ),
            "exit_code": 1,
        }

    # 6. Przygotowanie srodowiska dla procesu potomnego
    # R1: Sanityzacja srodowiska - usuwamy odziedziczone zmienne zawierajace op:// oraz niebezpieczne zmienne
    child_env = os.environ.copy()
    for k, v in list(child_env.items()):
        if "op://" in str(v).lower():
            del child_env[k]
        elif k in DANGEROUS_ENV_VARS:
            del child_env[k]

    for var_name, ref in pairs:
        child_env[var_name] = ref

    # 7. Budowa polecenia: op run -- <komenda> [arg...]
    if resolved_op.endswith(".py"):
        full_cmd = [sys.executable, resolved_op, "run", "--"] + command
    else:
        full_cmd = [resolved_op, "run", "--"] + command

    # 8. Wykonanie procesu bez okna konsoli
    creationflags = get_creationflags()
    start_time = time.time()
    try:
        proc = subprocess.run(
            full_cmd,
            env=child_env,
            creationflags=creationflags,
            capture_output=True,
        )
        duration = time.time() - start_time
    except FileNotFoundError:
        duration = time.time() - start_time
        return {
            "status": "error",
            "error": "OP_CLI_MISSING",
            "code": "OP_CLI_MISSING",
            "message": (
                "Nie znaleziono 1Password CLI ('op'). "
                "Instrukcja dla Mateusza: Zainstaluj 1Password CLI "
                "(https://developer.1password.com/docs/cli/get-started/) "
                "oraz wlacz integracje w aplikacji desktop 1Password "
                "(Settings -> Developer -> Integrate with 1Password CLI)."
            ),
            "exit_code": 1,
        }
    except Exception as e:
        duration = time.time() - start_time
        return {
            "status": "error",
            "error": "EXECUTION_ERROR",
            "code": "EXECUTION_ERROR",
            "message": f"Blad wykonania procesu op: {e}",
            "exit_code": 1,
        }

    # 9. Zapis audytu (wylacznie referencje, komenda[0], exit_code, czas trwania)
    target_audit = audit_path or get_audit_path()
    refs_only = [ref for _, ref in pairs]
    record_audit(
        audit_path=target_audit,
        project=project,
        references=refs_only,
        command=command,
        exit_code=proc.returncode,
        duration_seconds=duration,
    )

    return {
        "status": "success",
        "exit_code": proc.returncode,
        "stdout": proc.stdout,
        "stderr": proc.stderr,
        "duration_seconds": duration,
    }


def parse_cli(argv: list[str]) -> tuple[str, list[tuple[str, str]], list[str]] | tuple[None, dict[str, Any], None]:
    is_forbidden, forbidden_msg = check_forbidden_args(argv)
    if is_forbidden:
        return None, {
            "status": "error",
            "error": "FORBIDDEN_OPERATION",
            "code": "FORBIDDEN_OPERATION",
            "message": forbidden_msg,
            "exit_code": 1,
        }, None

    if not argv:
        return None, {
            "status": "error",
            "error": "USAGE_ERROR",
            "code": "USAGE_ERROR",
            "message": "Uzycie: secret_run.py run --project <slug> --env NAZWA=op://... -- <komenda> [arg...]",
            "exit_code": 1,
        }, None

    subcmd = argv[0]
    if subcmd != "run":
        is_forbid = subcmd in FORBIDDEN_OPERATIONS
        return None, {
            "status": "error",
            "error": "FORBIDDEN_OPERATION" if is_forbid else "UNKNOWN_SUBCOMMAND",
            "code": "FORBIDDEN_OPERATION" if is_forbid else "UNKNOWN_SUBCOMMAND",
            "message": f"Nieznana lub zakazana podkomenda '{subcmd}'. Dozwolona podkomenda to wylacznie 'run'.",
            "exit_code": 1,
        }, None

    run_args = argv[1:]
    if "--" in run_args:
        idx = run_args.index("--")
        opt_tokens = run_args[:idx]
        cmd_tokens = run_args[idx + 1:]
    else:
        opt_tokens = []
        cmd_tokens = []
        i = 0
        while i < len(run_args):
            token = run_args[i]
            if token in ("--project", "-p"):
                if i + 1 < len(run_args):
                    opt_tokens.extend([token, run_args[i + 1]])
                    i += 2
                else:
                    opt_tokens.append(token)
                    i += 1
            elif token in ("--env", "-e"):
                if i + 1 < len(run_args):
                    opt_tokens.extend([token, run_args[i + 1]])
                    i += 2
                else:
                    opt_tokens.append(token)
                    i += 1
            else:
                cmd_tokens = run_args[i:]
                break

    parser = argparse.ArgumentParser(prog="secret_run.py run", add_help=False)
    parser.add_argument("--project", "-p", required=True)
    parser.add_argument("--env", "-e", action="append", default=[])

    try:
        parsed, extra_opts = parser.parse_known_args(opt_tokens)
    except SystemExit:
        return None, {
            "status": "error",
            "error": "INVALID_ARGUMENTS",
            "code": "INVALID_ARGUMENTS",
            "message": "Brak wymaganych argumentow (wymagane: --project)",
            "exit_code": 1,
        }, None

    if extra_opts:
        return None, {
            "status": "error",
            "error": "INVALID_ARGUMENTS",
            "code": "INVALID_ARGUMENTS",
            "message": f"Nierozpoznane opcje: {' '.join(extra_opts)}",
            "exit_code": 1,
        }, None

    env_pairs: list[tuple[str, str]] = []
    for item in parsed.env:
        name, sep, ref = item.partition("=")
        if not sep or not name.strip():
            return None, {
                "status": "error",
                "error": "INVALID_ENV_ARG",
                "code": "INVALID_ENV_ARG",
                "message": f"Niepoprawny format --env '{item}'. Wymagany format: NAZWA=op://<sejf>/<pozycja>/<pole>",
                "exit_code": 1,
            }, None
        name = name.strip()
        ref = ref.strip()
        if not ref.startswith("op://"):
            return None, {
                "status": "error",
                "error": "REF_NOT_ALLOWED",
                "code": "REF_NOT_ALLOWED",
                "message": f"Referencja '{ref}' dla zmiennej '{name}' musi zaczynac sie od 'op://'",
                "exit_code": 1,
            }, None
        env_pairs.append((name, ref))

    if not cmd_tokens:
        return None, {
            "status": "error",
            "error": "COMMAND_MISSING",
            "code": "COMMAND_MISSING",
            "message": "Nie podano komendy do wykonania po '--'",
            "exit_code": 1,
        }, None

    return parsed.project, env_pairs, cmd_tokens


def main(argv: list[str] | None = None) -> int:
    if argv is None:
        argv = sys.argv[1:]

    parse_res = parse_cli(argv)
    if parse_res[0] is None:
        err = parse_res[1]
        assert isinstance(err, dict)
        sys.stderr.write(json.dumps(err, ensure_ascii=False) + "\n")
        sys.stderr.flush()
        return err.get("exit_code", 1)

    project, env_pairs, cmd_tokens = parse_res
    assert isinstance(project, str)
    assert isinstance(env_pairs, list)
    assert isinstance(cmd_tokens, list)

    res = secret_run(
        project=project,
        env_pairs=env_pairs,
        command=cmd_tokens,
    )

    if res.get("status") == "error":
        sys.stderr.write(json.dumps(res, ensure_ascii=False) + "\n")
        sys.stderr.flush()
        return res.get("exit_code", 1)

    if res.get("stdout"):
        out_bytes = res["stdout"]
        if hasattr(sys.stdout, "buffer"):
            sys.stdout.buffer.write(out_bytes)
            sys.stdout.buffer.flush()
        else:
            sys.stdout.write(out_bytes.decode("utf-8", errors="replace"))
            sys.stdout.flush()

    if res.get("stderr"):
        err_bytes = res["stderr"]
        if hasattr(sys.stderr, "buffer"):
            sys.stderr.buffer.write(err_bytes)
            sys.stderr.buffer.flush()
        else:
            sys.stderr.write(err_bytes.decode("utf-8", errors="replace"))
            sys.stderr.flush()

    return res.get("exit_code", 0)


if __name__ == "__main__":
    sys.exit(main())
