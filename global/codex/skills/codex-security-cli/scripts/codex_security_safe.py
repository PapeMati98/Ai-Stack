#!/usr/bin/env python3
"""Fail-closed wrapper for the official OpenAI Codex Security CLI."""

from __future__ import annotations

import argparse
import ctypes
import hashlib
import json
import math
import os
import platform
import re
import subprocess
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Sequence


SKILL_ROOT = Path(__file__).resolve().parents[1]
MANIFEST_PATH = SKILL_ROOT / "references" / "runtime-manifest.json"
MAX_COST_USD = 25.0
MAX_SECRET_SCAN_BYTES = 2 * 1024 * 1024
SKIP_DIRECTORIES = {
    ".git",
    ".hg",
    ".svn",
    ".idea",
    ".vscode",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    ".tox",
    ".venv",
    "venv",
    "node_modules",
    "dist",
    "build",
    "coverage",
}
ALLOWED_ENV_EXAMPLES = {
    ".env.example",
    ".env.sample",
    ".env.template",
    ".env.dist",
}
BLOCKED_EXACT_NAMES = {
    ".npmrc",
    ".pypirc",
    ".netrc",
    ".dockercfg",
    "credentials",
    "credentials.json",
    "secrets.json",
    "service-account.json",
    "service_account.json",
    "id_rsa",
    "id_dsa",
    "id_ecdsa",
    "id_ed25519",
    "cookies.sqlite",
    "login data",
    "terraform.tfstate",
    "terraform.tfstate.backup",
}
BLOCKED_SUFFIXES = {
    ".pem",
    ".key",
    ".pfx",
    ".p12",
    ".kdbx",
    ".jks",
    ".keystore",
    ".tfstate",
    ".db",
    ".sqlite",
    ".sqlite3",
}
SECRET_CONTENT_PATTERNS = {
    "private-key": re.compile(
        r"-----BEGIN (?:RSA |EC |DSA |OPENSSH )?PRIVATE KEY-----"
    ),
    "openai-key": re.compile(r"\bsk-(?:proj-|svcacct-)?[A-Za-z0-9_-]{20,}\b"),
    "github-token": re.compile(
        r"\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,})\b"
    ),
    "huggingface-token": re.compile(r"\bhf_[A-Za-z0-9]{20,}\b"),
    "aws-access-key": re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"),
    "slack-token": re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{20,}\b"),
    "credential-uri": re.compile(
        r"\b[a-z][a-z0-9+.-]{2,12}://[^/\s:@]{2,64}:[^/\s@]{8,}@",
        re.IGNORECASE,
    ),
}
SCAN_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{2,127}$")
KNOWN_FOLDER_IDS = {
    0x001A: "3EB685DB-65F9-4CF6-A03A-E3EF65729F3D",  # RoamingAppData
    0x001C: "F1B32785-6FBA-4FCF-9D55-7B8E7F157091",  # LocalAppData
    0x0023: "62AB5D82-FDC1-4DC3-A9DD-070D1D495D97",  # ProgramData
    0x0028: "5E6C858F-0E22-4760-9AFE-EA3317B67173",  # Profile
}


class PolicyError(RuntimeError):
    """Raised when the local safety policy blocks an operation."""


class WindowsGuid(ctypes.Structure):
    _fields_ = [
        ("data1", ctypes.c_uint32),
        ("data2", ctypes.c_uint16),
        ("data3", ctypes.c_uint16),
        ("data4", ctypes.c_ubyte * 8),
    ]

    @classmethod
    def from_text(cls, value: str) -> "WindowsGuid":
        return cls.from_buffer_copy(uuid.UUID(value).bytes_le)


def windows_known_folder(csidl: int, fallback: Path) -> Path:
    """Resolve a Windows known folder without trusting process environment."""
    if os.name != "nt":
        return fallback.expanduser().resolve()

    folder_id = KNOWN_FOLDER_IDS.get(csidl)
    if not folder_id:
        raise PolicyError(f"Brak przypietego Known Folder dla CSIDL={csidl}.")

    shell32 = ctypes.windll.shell32
    ole32 = ctypes.windll.ole32
    shell32.SHGetKnownFolderPath.argtypes = [
        ctypes.POINTER(WindowsGuid),
        ctypes.c_uint32,
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_void_p),
    ]
    shell32.SHGetKnownFolderPath.restype = ctypes.c_long
    ole32.CoTaskMemFree.argtypes = [ctypes.c_void_p]

    guid = WindowsGuid.from_text(folder_id)
    raw_path = ctypes.c_void_p()
    result = shell32.SHGetKnownFolderPath(
        ctypes.byref(guid), 0, None, ctypes.byref(raw_path)
    )
    if result != 0 or not raw_path.value:
        raise PolicyError(f"Windows nie zwrocil znanego katalogu CSIDL={csidl}.")
    try:
        return Path(ctypes.wstring_at(raw_path.value)).resolve()
    finally:
        ole32.CoTaskMemFree(raw_path)


def windows_user_shell_folder(value_name: str, fallback: Path) -> Path:
    """Resolve a per-user shell folder without expanding process environment."""
    if os.name != "nt":
        return fallback.expanduser().resolve()

    import winreg

    key_path = (
        r"Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders"
    )
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path) as key:
            raw_value, _ = winreg.QueryValueEx(key, value_name)
    except FileNotFoundError:
        return fallback.resolve()

    if not isinstance(raw_value, str) or not raw_value.strip():
        raise PolicyError(f"Nieprawidlowa wartosc folderu powloki: {value_name}.")

    profile = user_profile_root()
    substitutions = {
        "%USERPROFILE%": str(profile),
        "%HOMEDRIVE%%HOMEPATH%": str(profile),
        "%SYSTEMDRIVE%": profile.anchor.rstrip("\\/"),
    }
    expanded = raw_value
    for variable, trusted_value in substitutions.items():
        expanded = re.sub(
            re.escape(variable),
            lambda _match, value=trusted_value: value,
            expanded,
            flags=re.IGNORECASE,
        )
    if re.search(r"%[^%]+%", expanded):
        raise PolicyError(
            f"Folder powloki {value_name} zawiera nierozpoznana zmienna."
        )

    resolved = Path(expanded).resolve()
    if not resolved.is_absolute():
        raise PolicyError(f"Folder powloki {value_name} nie jest sciezka absolutna.")
    return resolved


def user_profile_root() -> Path:
    return windows_known_folder(0x0028, Path.home())


def roaming_app_data_root() -> Path:
    return windows_user_shell_folder(
        "AppData", user_profile_root() / "AppData" / "Roaming"
    )


def local_app_data_root() -> Path:
    return windows_user_shell_folder(
        "Local AppData", user_profile_root() / "AppData" / "Local"
    )


def program_data_root() -> Path:
    return windows_known_folder(0x0023, Path("C:/ProgramData"))


def windows_root() -> Path:
    if os.name != "nt":
        return Path("/")
    buffer = ctypes.create_unicode_buffer(32768)
    length = ctypes.windll.kernel32.GetWindowsDirectoryW(buffer, len(buffer))
    if length <= 0 or length >= len(buffer):
        raise PolicyError("Windows nie zwrocil katalogu systemowego.")
    return Path(buffer.value).resolve()


def load_manifest() -> dict:
    return json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))


def local_data_root() -> Path:
    return local_app_data_root() / "MateuszAI" / "codex-security"


def runtime_root(manifest: dict | None = None) -> Path:
    data = manifest or load_manifest()
    relative = Path(data["runtimeRelativePath"])
    if relative.is_absolute() or ".." in relative.parts:
        raise PolicyError("Manifest zawiera niebezpieczna sciezke runtime.")
    return (user_profile_root() / relative).resolve()


def package_root(manifest: dict | None = None) -> Path:
    return runtime_root(manifest) / "node_modules" / "@openai" / "codex-security"


def binary_path(manifest: dict | None = None) -> Path:
    return package_root(manifest) / "bin" / "codex-security.mjs"


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def sha256_tree(root: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    files = sorted(
        (path for path in root.rglob("*") if path.is_file()),
        key=lambda path: path.relative_to(root).as_posix(),
    )
    for path in files:
        digest.update(path.relative_to(root).as_posix().encode("utf-8"))
        digest.update(b"\0")
        with path.open("rb") as handle:
            for block in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(block)
        digest.update(b"\0")
    return digest.hexdigest(), len(files)


def parse_version(text: str) -> tuple[int, ...]:
    match = re.search(r"(\d+)(?:\.(\d+))?(?:\.(\d+))?", text)
    if not match:
        return ()
    return tuple(int(part or 0) for part in match.groups())


def verify_runtime() -> dict:
    manifest = load_manifest()
    root = runtime_root(manifest)
    package = package_root(manifest)
    lock = root / "package-lock.json"
    binary = binary_path(manifest)
    metadata_path = package / "package.json"

    required = (root, package, lock, binary, metadata_path)
    missing = [str(path) for path in required if not path.exists()]
    if missing:
        raise PolicyError(
            "Brak zweryfikowanego runtime. Uruchom scripts/install_runtime.ps1. "
            f"Braki: {', '.join(missing)}"
        )

    node = Path(manifest["nodePath"]).expanduser()
    if not node.is_file():
        raise PolicyError(f"Brak przypietego Node.js: {node}")
    node = node.resolve()
    if sha256_file(node) != manifest["nodeSha256"]:
        raise PolicyError("Kontrola integralnosci przypietego Node.js nie powiodla sie.")

    python = Path(sys.executable).resolve()
    if not python.is_file():
        raise PolicyError("Nie mozna zweryfikowac interpretera Python wrappera.")

    node_version = subprocess.run(
        [str(node), "--version"],
        capture_output=True,
        text=True,
        check=True,
        shell=False,
        env=build_base_child_env(node=node, python=python),
    ).stdout.strip()
    python_version = platform.python_version()
    if parse_version(node_version) < parse_version(manifest["nodeMinimum"]):
        raise PolicyError(f"Node.js jest za stary: {node_version}.")
    if parse_version(python_version) < parse_version(manifest["pythonMinimum"]):
        raise PolicyError(f"Python jest za stary: {python_version}.")

    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    if (
        metadata.get("name") != manifest["package"]
        or metadata.get("version") != manifest["version"]
        or metadata.get("license") != manifest["license"]
    ):
        raise PolicyError("Metadane zainstalowanego pakietu nie sa zgodne z manifestem.")

    tree_hash, file_count = sha256_tree(package)
    checks = {
        "packageTreeSha256": tree_hash,
        "packageLockSha256": sha256_file(lock),
        "binarySha256": sha256_file(binary),
    }
    for key, actual in checks.items():
        if actual != manifest[key]:
            raise PolicyError(f"Kontrola integralnosci nie powiodla sie: {key}.")
    if file_count != manifest["packageFileCount"]:
        raise PolicyError(
            f"Niezgodna liczba plikow pakietu: {file_count} "
            f"(oczekiwano {manifest['packageFileCount']})."
        )

    return {
        "manifest": manifest,
        "runtimeRoot": str(root),
        "binary": str(binary),
        "node": str(node),
        "nodeVersion": node_version,
        "nodeSha256": manifest["nodeSha256"],
        "python": str(python),
        "pythonVersion": python_version,
        "fileCount": file_count,
    }


def is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def paths_overlap(first: Path, second: Path) -> bool:
    return is_relative_to(first, second) or is_relative_to(second, first)


def has_blocked_path_component(path: Path) -> bool:
    return any(part.lower() in SKIP_DIRECTORIES for part in path.parts)


def is_reparse_point(path: Path) -> bool:
    try:
        attributes = getattr(path.lstat(), "st_file_attributes", 0)
    except OSError:
        return False
    return path.is_symlink() or bool(attributes & 0x400)


def validate_no_reparse_components(path: Path, label: str) -> None:
    absolute = Path(os.path.abspath(path))
    current = Path(absolute.anchor)
    parts = absolute.parts[1:] if absolute.anchor else absolute.parts
    for part in parts:
        current = current / part
        if current.exists() and is_reparse_point(current):
            raise PolicyError(f"{label} przechodzi przez dowiazanie/reparse point: {current}")


def validate_scope_components(repository: Path, raw: Path) -> None:
    if has_blocked_path_component(raw):
        blocked = next(
            part for part in raw.parts if part.lower() in SKIP_DIRECTORIES
        )
        raise PolicyError(f"Zakres wskazuje pominiety katalog: {blocked}")

    current = repository
    for part in raw.parts:
        current = current / part
        if current.exists() and is_reparse_point(current):
            raise PolicyError(f"Zakres przechodzi przez dowiazanie/reparse point: {part}")


def protected_roots() -> list[Path]:
    home = user_profile_root()
    candidates = [
        home / ".ssh",
        home / ".aws",
        home / ".azure",
        home / ".codex",
        home / ".agents",
        home / ".claude",
        home / ".gemini",
        home / ".grok",
        windows_root(),
        program_data_root(),
        roaming_app_data_root(),
        local_app_data_root(),
    ]
    candidates.append(local_data_root())
    return [path.resolve(strict=False) for path in candidates]


def validate_repository(value: str) -> Path:
    requested = Path(value).expanduser()
    if has_blocked_path_component(requested):
        blocked = next(
            part for part in requested.parts if part.lower() in SKIP_DIRECTORIES
        )
        raise PolicyError(f"Repozytorium wskazuje pominiety katalog: {blocked}")
    validate_no_reparse_components(requested, "Repozytorium")
    repository = requested.resolve(strict=True)
    if not repository.is_dir():
        raise PolicyError(f"Repozytorium nie jest katalogiem: {repository}")

    anchor = Path(repository.anchor).resolve(strict=False)
    if repository == anchor:
        raise PolicyError("Nie wolno skanowac korzenia dysku.")

    home = user_profile_root()
    if repository == home:
        raise PolicyError("Nie wolno skanowac calego katalogu uzytkownika.")

    for protected in protected_roots():
        if repository == protected or (
            repository != home and paths_overlap(repository, protected)
        ):
            raise PolicyError(
                f"Zakres nachodzi na chroniony katalog: {protected}"
            )
    return repository


def validate_scopes(repository: Path, values: Sequence[str] | None) -> list[str]:
    if not values:
        return []
    scopes: list[str] = []
    for value in values:
        raw = Path(value)
        if raw.is_absolute():
            raise PolicyError(f"Zakres musi byc wzgledny: {value}")
        validate_scope_components(repository, raw)
        resolved = (repository / raw).resolve(strict=True)
        if not is_relative_to(resolved, repository):
            raise PolicyError(f"Zakres wychodzi poza repozytorium: {value}")
        relative = resolved.relative_to(repository).as_posix()
        if relative in ("", "."):
            raise PolicyError("Dla calego repozytorium nie podawaj --path.")
        scopes.append(relative)
    return sorted(set(scopes))


def is_blocked_file(path: Path) -> bool:
    name = path.name.lower()
    if name in ALLOWED_ENV_EXAMPLES:
        return False
    if name == ".env" or name.startswith(".env."):
        return True
    if name in BLOCKED_EXACT_NAMES:
        return True
    if path.suffix.lower() in BLOCKED_SUFFIXES:
        return True
    if name.endswith((".credentials.json", ".secrets.json")):
        return True
    return False


def iter_scan_files(repository: Path, scopes: Sequence[str]) -> Iterable[Path]:
    roots = [repository / scope for scope in scopes] if scopes else [repository]
    seen: set[Path] = set()
    for root in roots:
        if root.is_file():
            if root not in seen:
                seen.add(root)
                yield root
            continue
        for current, directories, files in os.walk(root, followlinks=False):
            directories[:] = [
                name
                for name in directories
                if name.casefold() not in SKIP_DIRECTORIES
                and not is_reparse_point(Path(current) / name)
            ]
            for name in files:
                path = Path(current) / name
                if is_reparse_point(path):
                    continue
                if path not in seen:
                    seen.add(path)
                    yield path


def find_blocked_files(repository: Path, scopes: Sequence[str]) -> list[str]:
    findings = []
    for path in iter_scan_files(repository, scopes):
        if is_blocked_file(path):
            findings.append(path.relative_to(repository).as_posix())
            if len(findings) >= 50:
                break
    return findings


def inspect_text_for_secrets(text: str) -> str | None:
    for label, pattern in SECRET_CONTENT_PATTERNS.items():
        if pattern.search(text):
            return label
    return None


def find_blocked_content(repository: Path, scopes: Sequence[str]) -> list[dict[str, str]]:
    findings: list[dict[str, str]] = []
    for path in iter_scan_files(repository, scopes):
        try:
            if path.stat().st_size > MAX_SECRET_SCAN_BYTES:
                continue
            data = path.read_bytes()
        except OSError:
            continue
        if b"\0" in data[:8192]:
            continue
        label = inspect_text_for_secrets(data.decode("utf-8", errors="ignore"))
        if label:
            findings.append(
                {
                    "path": path.relative_to(repository).as_posix(),
                    "kind": label,
                }
            )
            if len(findings) >= 50:
                break
    return findings


def check_repository_inputs(repository: Path, scopes: Sequence[str]) -> list[str]:
    blocked = find_blocked_files(repository, scopes)
    if blocked:
        preview = ", ".join(blocked[:10])
        suffix = " (lista skrocona)" if len(blocked) > 10 else ""
        raise PolicyError(
            "Skan chmurowy zablokowany: znaleziono typowe pliki z sekretami: "
            f"{preview}{suffix}."
        )
    blocked_content = find_blocked_content(repository, scopes)
    if blocked_content:
        preview = ", ".join(
            f"{item['path']} [{item['kind']}]" for item in blocked_content[:10]
        )
        suffix = " (lista skrocona)" if len(blocked_content) > 10 else ""
        raise PolicyError(
            "Skan chmurowy zablokowany: wykryto wzorce sekretow w tresci: "
            f"{preview}{suffix}. Wartosci nie zostaly zapisane w logu."
        )
    warnings = []
    if not (repository / ".git").exists():
        warnings.append("Katalog nie wyglada na repozytorium Git.")
    return warnings


def build_base_child_env(node: Path, python: Path) -> dict[str, str]:
    profile = user_profile_root()
    local_app_data = local_app_data_root()
    roaming_app_data = roaming_app_data_root()
    system_root = windows_root()
    system32 = system_root / "System32"
    temp = local_app_data / "Temp"
    temp.mkdir(parents=True, exist_ok=True)

    trusted_path = [str(node.parent), str(python.parent), str(system32)]
    environment = {
        "APPDATA": str(roaming_app_data),
        "COMSPEC": str(system32 / "cmd.exe"),
        "HOME": str(profile),
        "HOMEDRIVE": profile.drive,
        "HOMEPATH": str(profile)[len(profile.drive) :],
        "LOCALAPPDATA": str(local_app_data),
        "NUMBER_OF_PROCESSORS": str(os.cpu_count() or 1),
        "OS": "Windows_NT" if os.name == "nt" else os.name,
        "PATH": os.pathsep.join(dict.fromkeys(trusted_path)),
        "PATHEXT": ".COM;.EXE;.BAT;.CMD",
        "PROCESSOR_ARCHITECTURE": platform.machine(),
        "PROGRAMDATA": str(program_data_root()),
        "SystemRoot": str(system_root),
        "TEMP": str(temp),
        "TMP": str(temp),
        "USERPROFILE": str(profile),
        "WINDIR": str(system_root),
    }
    if os.environ.get("NO_COLOR"):
        environment["NO_COLOR"] = "1"
    return environment


def build_child_env(auth: str = "none", runtime: dict | None = None) -> dict[str, str]:
    verified = runtime or verify_runtime()
    environment = build_base_child_env(
        node=Path(verified["node"]),
        python=Path(verified["python"]),
    )

    if auth == "api-key":
        api_key = os.environ.get("OPENAI_API_KEY")
        if not api_key:
            raise PolicyError("Brak OPENAI_API_KEY dla jawnie wybranego auth=api-key.")
        environment["OPENAI_API_KEY"] = api_key

    environment["CODEX_SECURITY_STATE_DIR"] = str(local_data_root() / "state")
    return environment


def run_cli(arguments: Sequence[str], auth: str = "none") -> int:
    runtime = verify_runtime()
    (local_data_root() / "state").mkdir(parents=True, exist_ok=True)
    command = [runtime["node"], runtime["binary"], *arguments]
    result = subprocess.run(
        command,
        env=build_child_env(auth, runtime),
        check=False,
        shell=False,
    )
    return int(result.returncode)


def append_audit(event: dict) -> None:
    log_dir = local_data_root() / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    record = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        **event,
    }
    with (log_dir / "audit.jsonl").open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=True, sort_keys=True) + "\n")


def make_scan_dir(repository: Path) -> Path:
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", repository.name).strip("-") or "repo"
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    directory = local_data_root() / "scans" / slug / f"{stamp}-{uuid.uuid4().hex[:8]}"
    directory.mkdir(parents=True, exist_ok=False)
    return directory


def add_selection_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--repository", required=True)
    parser.add_argument("--path", action="append", default=[])
    parser.add_argument("--working-tree", action="store_true")
    parser.add_argument("--model")


def selection_cli_arguments(args: argparse.Namespace, scopes: Sequence[str]) -> list[str]:
    result: list[str] = []
    for scope in scopes:
        result.extend(["--path", scope])
    if args.working_tree:
        result.append("--working-tree")
    if args.model:
        result.extend(["--model", args.model])
    return result


def command_info(_: argparse.Namespace) -> int:
    runtime = verify_runtime()
    payload = {
        "wrapper": "codex-security-safe",
        "wrapperPolicy": "fail-closed",
        "package": runtime["manifest"]["package"],
        "version": runtime["manifest"]["version"],
        "license": runtime["manifest"]["license"],
        "runtimeRoot": runtime["runtimeRoot"],
        "node": runtime["nodeVersion"],
        "python": runtime["pythonVersion"],
        "cloudScanRequiresExplicitConsent": True,
        "maximumDeclaredCostUsd": MAX_COST_USD,
        "globalPluginEnabled": False,
    }
    print(json.dumps(payload, indent=2, ensure_ascii=False))
    return 0


def prepare_repository(args: argparse.Namespace) -> tuple[Path, list[str], list[str]]:
    repository = validate_repository(args.repository)
    scopes = validate_scopes(repository, args.path)
    warnings = check_repository_inputs(repository, scopes)
    return repository, scopes, warnings


def command_preflight(args: argparse.Namespace) -> int:
    runtime = verify_runtime()
    repository, scopes, warnings = prepare_repository(args)
    for warning in warnings:
        print(f"OSTRZEZENIE: {warning}", file=sys.stderr)

    file_count = sum(1 for _ in iter_scan_files(repository, scopes))
    payload = {
        "status": "OK",
        "preflight": "local-only",
        "upstreamCliInvoked": False,
        "cloudUpload": False,
        "modelCostUsd": 0,
        "repository": str(repository),
        "scopes": scopes,
        "filesInspected": file_count,
        "runtimePackage": runtime["manifest"]["package"],
        "runtimeVersion": runtime["manifest"]["version"],
        "nodeSha256": runtime["nodeSha256"],
        "warnings": warnings,
    }
    print(json.dumps(payload, indent=2, ensure_ascii=False))
    append_audit(
        {
            "command": "preflight",
            "repository": str(repository),
            "scopes": scopes,
            "cloudUpload": False,
            "costCapUsd": 0,
            "filesInspected": file_count,
            "upstreamCliInvoked": False,
            "exitCode": 0,
        }
    )
    return 0


def validate_cloud_gate(args: argparse.Namespace) -> None:
    if not args.allow_cloud_code_upload:
        raise PolicyError(
            "Brak jawnej zgody na wyslanie kodu: dodaj --allow-cloud-code-upload."
        )
    if args.data_classification == "restricted":
        raise PolicyError("Dane restricted nie moga zostac wyslane do skanu chmurowego.")
    if (
        args.data_classification == "confidential"
        and not args.confirm_confidential
    ):
        raise PolicyError(
            "Dane confidential wymagaja dodatkowego --confirm-confidential."
        )
    if (
        not math.isfinite(args.max_cost_usd)
        or args.max_cost_usd <= 0
        or args.max_cost_usd > MAX_COST_USD
    ):
        raise PolicyError(
            f"--max-cost-usd musi byc wieksze od 0 i nie wieksze niz {MAX_COST_USD}."
        )


def command_scan(args: argparse.Namespace) -> int:
    validate_cloud_gate(args)
    repository, scopes, warnings = prepare_repository(args)
    for warning in warnings:
        print(f"OSTRZEZENIE: {warning}", file=sys.stderr)

    output = make_scan_dir(repository)
    cli = [
        "scan",
        str(repository),
        "--auth",
        args.auth,
        "--mode",
        "standard",
        "--output-dir",
        str(output),
        "--max-cost",
        str(args.max_cost_usd),
        *selection_cli_arguments(args, scopes),
    ]
    if args.fail_on_severity:
        cli.extend(["--fail-on-severity", args.fail_on_severity])

    print(f"Artefakty skanu: {output}")
    code = run_cli(cli, auth=args.auth)
    append_audit(
        {
            "command": "scan",
            "repository": str(repository),
            "scopes": scopes,
            "classification": args.data_classification,
            "cloudUpload": True,
            "costCapUsd": args.max_cost_usd,
            "outputDir": str(output),
            "exitCode": code,
        }
    )
    return code


def command_list(args: argparse.Namespace) -> int:
    repository = validate_repository(args.repository)
    scan_root = local_data_root() / "scans"
    scan_root.mkdir(parents=True, exist_ok=True)
    return run_cli(
        ["scans", "list", str(repository), "--scan-root", str(scan_root)]
    )


def command_show(args: argparse.Namespace) -> int:
    if not SCAN_ID_RE.fullmatch(args.scan_id):
        raise PolicyError("Nieprawidlowy identyfikator skanu.")
    return run_cli(["scans", "show", args.scan_id])


def managed_scan_directory(value: str) -> Path:
    scan_root = (local_data_root() / "scans").resolve()
    candidate = Path(value).expanduser().resolve(strict=True)
    if not candidate.is_dir() or not is_relative_to(candidate, scan_root):
        raise PolicyError(f"Katalog skanu musi znajdowac sie pod: {scan_root}")
    return candidate


def command_export(args: argparse.Namespace) -> int:
    scan_dir = managed_scan_directory(args.scan_dir)
    export_dir = local_data_root() / "exports"
    export_dir.mkdir(parents=True, exist_ok=True)
    suffix = args.export_format
    output = export_dir / f"{scan_dir.name}.{suffix}"
    cli = [
        "export",
        str(scan_dir),
        "--export-format",
        args.export_format,
        "--output",
        str(output),
    ]
    if args.source_root:
        source = validate_repository(args.source_root)
        cli.extend(["--source-root", str(source)])
    code = run_cli(cli)
    append_audit(
        {
            "command": "export",
            "scanDir": str(scan_dir),
            "format": args.export_format,
            "output": str(output),
            "cloudUpload": False,
            "costCapUsd": 0,
            "exitCode": code,
        }
    )
    if code == 0:
        print(f"Eksport: {output}")
    return code


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Bezpieczna nakladka na OpenAI Codex Security CLI."
    )
    commands = parser.add_subparsers(dest="command", required=True)

    info = commands.add_parser("info", help="Zweryfikuj runtime i pokaz polityke.")
    info.set_defaults(handler=command_info)

    preflight = commands.add_parser(
        "preflight", help="Lokalnie zweryfikuj zakres bez wysylania kodu."
    )
    add_selection_arguments(preflight)
    preflight.set_defaults(handler=command_preflight)

    scan = commands.add_parser("scan", help="Uruchom jawnie zatwierdzony skan.")
    add_selection_arguments(scan)
    scan.add_argument(
        "--data-classification",
        required=True,
        choices=("public", "internal", "confidential", "restricted"),
    )
    scan.add_argument("--allow-cloud-code-upload", action="store_true")
    scan.add_argument("--confirm-confidential", action="store_true")
    scan.add_argument("--max-cost-usd", required=True, type=float)
    scan.add_argument("--auth", required=True, choices=("chatgpt", "api-key"))
    scan.add_argument(
        "--fail-on-severity",
        choices=("critical", "high", "medium", "low"),
    )
    scan.set_defaults(handler=command_scan)

    list_command = commands.add_parser("list", help="Pokaz zarzadzane skany.")
    list_command.add_argument("--repository", required=True)
    list_command.set_defaults(handler=command_list)

    show = commands.add_parser("show", help="Pokaz zapisany skan.")
    show.add_argument("--scan-id", required=True)
    show.set_defaults(handler=command_show)

    export = commands.add_parser("export", help="Eksportuj zarzadzany skan.")
    export.add_argument("--scan-dir", required=True)
    export.add_argument(
        "--export-format",
        choices=("sarif", "json", "csv"),
        default="sarif",
    )
    export.add_argument("--source-root")
    export.set_defaults(handler=command_export)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.handler(args))
    except PolicyError as error:
        print(f"BLOKADA POLITYKI: {error}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("Przerwano.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
