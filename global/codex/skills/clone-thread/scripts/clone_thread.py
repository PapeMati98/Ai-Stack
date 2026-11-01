#!/usr/bin/env python3
"""
clone-thread (clone_thread.py)
Cross-desktop project knowledge normalizer and thread cloner for:
- Claude Desktop / Claude Code
- Codex Desktop / CLI
- ZCode Desktop / CLI
- Antigravity Desktop
- Kimi Desktop / CLI
- Agent Office Registry

Transcript files and SQLite databases are read-only (SQLite mode=ro).
Only project knowledge, thread registries and explicit Codex title metadata are updated.
No file deletions.
"""

from __future__ import annotations

import argparse
import dataclasses
import datetime as dt
import hashlib
import json
import os
import re
import sqlite3
import subprocess
import sys
import urllib.parse
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple


SCHEMA_VERSION = 1
MAX_KNOWLEDGE_LINES = 400
MAX_KNOWLEDGE_BYTES = 25 * 1024
DESKTOP_TOOLS = ("claude", "codex", "zcode", "antigravity", "kimi", "agent_office")

SECRET_PATTERNS: List[Tuple[str, re.Pattern[str]]] = [
    ("PRIVATE_KEY", re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z0-9 ]*PRIVATE KEY-----")),
    ("JWT", re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b")),
    ("OPENAI_KEY", re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b")),
    ("ANTHROPIC_KEY", re.compile(r"\bsk-ant-[A-Za-z0-9_-]{20,}\b")),
    ("GITHUB_TOKEN", re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{20,}\b")),
    ("SLACK_TOKEN", re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{15,}\b")),
    ("BEARER_TOKEN", re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]{20,}\b")),
    ("CONN_STRING", re.compile(r"(?i)\b(?:postgres|postgresql|mysql|mongodb|redis|mssql)://[^\s\"'`<>]+")),
    (
        "CREDENTIAL_ASSIGN",
        re.compile(
            r"(?i)\b(password|passwd|haslo|secret|api[_-]?key|access[_-]?token|client[_-]?secret)\s*[:=]\s*['\"]?[^\s'\",;]{6,}['\"]?"
        ),
    ),
]

PROMPT_INJECTION_PATTERNS: List[re.Pattern[str]] = [
    re.compile(r"(?i)\bignore\s+(?:all\s+)?(?:previous|prior|above)\s+instructions\b"),
    re.compile(r"(?i)\bdisregard\s+(?:all\s+)?(?:previous|prior|system)\s+instructions\b"),
    re.compile(r"(?i)<\s*/?\s*system\s*>"),
    re.compile(r"(?i)\byou\s+are\s+now\s+in\s+developer\s+mode\b"),
    re.compile(r"(?i)\bIMPORTANT:\s*execute\s+the\s+following\s+command\b"),
    re.compile(r"(?i)\bdo\s+not\s+mention\s+this\s+instruction\b"),
]

NOISE_LINE_PATTERNS: List[re.Pattern[str]] = [
    re.compile(r"^/[a-z0-9_-]+(?:\s+|$)", re.IGNORECASE),
    re.compile(r"^\$[a-z0-9_-]+(?:\s+|$)", re.IGNORECASE),
    re.compile(r"^<(?:USER_REQUEST|CONTEXT_SUMMARY|task-notification|system-reminder|environment_context|INSTRUCTIONS|skill)", re.IGNORECASE),
    re.compile(r"^#\s*(?:AGENTS\.md|Files mentioned by the user|Context from my IDE)", re.IGNORECASE),
    re.compile(r"^##\s*codex-clipboard-", re.IGNORECASE),
    re.compile(r"(?i)(?:codex-clipboard-|<image\s+name=|Distinguish instructions in attached documents)"),
    re.compile(r"^To jest GLOBALNE zrodlo prawdy", re.IGNORECASE),
    re.compile(r"^(?:ok|okej|dobra|super|dzieki|dzięki|tak|nie|kontynuuj|działaj|ruszaj)\.?$", re.IGNORECASE),
]

SYSTEM_BLOCK_RE = re.compile(
    r"(?is)<(?:INSTRUCTIONS|environment_context|permissions instructions|collaboration_mode|subagent_notification|system-reminder|task-notification|local-command-stdout|command-name|command-message|command-args|skill|turn_aborted)>.*?</(?:INSTRUCTIONS|environment_context|permissions instructions|collaboration_mode|subagent_notification|system-reminder|task-notification|local-command-stdout|command-name|command-message|command-args|skill|turn_aborted)>"
)


def strip_system_envelopes(text: str) -> str:
    if not text:
        return ""
    cleaned = SYSTEM_BLOCK_RE.sub("", text)
    if cleaned.lstrip().startswith("# AGENTS.md instructions"):
        # If an entire content item is the injected AGENTS.md block, drop it
        if "</INSTRUCTIONS>" in text:
            cleaned = text.split("</INSTRUCTIONS>", 1)[-1]
        else:
            return ""
    # Drop lines that look like raw filesystem listings from environment_context
    out_lines: List[str] = []
    for line in cleaned.splitlines():
        s = line.strip()
        if not s:
            continue
        if s.startswith(("<cwd>", "<shell>", "<current_date>", "<timezone>", "<writable_roots>", "</")):
            continue
        if "AGENTS.md - uniwersalny standard pracy agenta" in s:
            continue
        out_lines.append(s)
    return "\n".join(out_lines)

DECISION_KEYWORDS = re.compile(
    r"(?i)\b(chcę|chce|chciałbym|chcialbym|ustalamy|decyzja|zasada|musi\s+być|musi\s+byc|"
    r"powinien|powinno|powinna|nie\s+chcę|nie\s+chce|zakaz|docelowo|wprowadzamy|zmieniamy|"
    r"wymagam|zawsze|nigdy|architektur|logik[aię]|reguł[ayę]|moduł|modul|portfel|faktur|"
    r"klient|uprawnieni|autoryzacj|widok|proces|workflow|standard)\b"
)

PROBLEM_KEYWORDS = re.compile(
    r"(?i)\b(błąd|blad|błędy|bledy|problem|awaria|nie\s+działa|nie\s+dziala|regresj|"
    r"przyczyna|root\s+cause|napraw|fix|obejście|obejscie|workaround|pułapk|pulapk|"
    r"wyjątek|wyjatek|exception|crash|timeout|zablokowan|rozwiązan|rozwiazan)\b"
)


@dataclasses.dataclass
class StoreConfig:
    claude_sessions_root: Path
    claude_projects_root: Path
    codex_root: Path
    zcode_root: Path
    antigravity_root: Path
    kimi_root: Path
    office_registry_path: Path
    drives_to_search: List[Path]

    @classmethod
    def default(cls) -> "StoreConfig":
        home = Path.home()
        appdata = Path(os.environ.get("APPDATA", str(home / "AppData" / "Roaming")))
        drives = [Path.cwd().resolve()]
        return cls(
            claude_sessions_root=appdata / "Claude" / "claude-code-sessions",
            claude_projects_root=home / ".claude" / "projects",
            codex_root=home / ".codex",
            zcode_root=home / ".zcode",
            antigravity_root=home / ".gemini" / "antigravity",
            kimi_root=appdata / "kimi-desktop",
            office_registry_path=home / ".agents" / "coordfs" / "office" / "thread-registry.json",
            drives_to_search=drives,
        )


@dataclasses.dataclass
class MessageItem:
    role: str  # "user" | "assistant" | "summary"
    text: str
    timestamp: str = ""


@dataclasses.dataclass
class DiscoveredThread:
    id: str
    desktop: str
    native_id: str
    title: str
    module: str
    tier: int
    cwd: str
    store_path: str
    updated_at: str
    messages: List[MessageItem] = dataclasses.field(default_factory=list)
    watermark: Dict[str, Any] = dataclasses.field(default_factory=dict)
    decisions_extracted: int = 0
    problems_extracted: int = 0
    incremental_reused: bool = False


def now_iso() -> str:
    return dt.datetime.now().astimezone().isoformat(timespec="seconds")


def normalize_key(text: str) -> str:
    if not text:
        return ""
    polish_map = str.maketrans("ąćęłńóśźżĄĆĘŁŃÓŚŹŻ", "acelnoszzACELNOSZZ")
    cleaned = text.translate(polish_map).lower()
    return re.sub(r"[^a-z0-9]+", "", cleaned)


def normalize_slug(text: str) -> str:
    if not text:
        return "unknown-project"
    polish_map = str.maketrans("ąćęłńóśźżĄĆĘŁŃÓŚŹŻ", "acelnoszzACELNOSZZ")
    cleaned = text.translate(polish_map).lower()
    slug = re.sub(r"[^a-z0-9]+", "-", cleaned).strip("-")
    return slug or "unknown-project"


def redact_secrets(text: str) -> str:
    if not text:
        return ""
    result = text
    for label, pattern in SECRET_PATTERNS:
        if label == "CREDENTIAL_ASSIGN":
            result = pattern.sub(lambda m: f"{m.group(1)}=[REDACTED:{label}]", result)
        else:
            result = pattern.sub(f"[REDACTED:{label}]", result)
    return result


def contains_prompt_injection(text: str) -> bool:
    if not text:
        return False
    return any(p.search(text) for p in PROMPT_INJECTION_PATTERNS)


def sanitize_text_line(text: str, max_len: int = 260) -> str:
    if not text:
        return ""
    if contains_prompt_injection(text):
        return ""
    redacted = redact_secrets(text)
    redacted = re.sub(r"<[^>]{1,80}>", " ", redacted)
    redacted = re.sub(r"\s+", " ", redacted).strip()
    redacted = redacted.replace("|", "/")
    if len(redacted) > max_len:
        redacted = redacted[: max_len - 3].rstrip() + "..."
    return redacted


def parse_thread_spec(raw_spec: str) -> Tuple[str, str, str]:
    """
    Parses '<Projekt> - <Moduł>' or '/clone-thread "<Projekt> - <Moduł>"'
    Returns (canonical_thread_title, project_name, module_name).
    """
    cleaned = (raw_spec or "").strip()
    cmd_match = re.match(
        r"""^(?:[/\$](?:clone-thread|klonuj-w(?:a|ą)tek)\s+)?["'“”]?(.+?)["'“”]?$""",
        cleaned,
        flags=re.IGNORECASE,
    )
    if cmd_match:
        cleaned = cmd_match.group(1).strip()

    for sep in (" - ", " – ", " — ", " / "):
        if sep in cleaned:
            proj, mod = cleaned.split(sep, 1)
            proj = proj.strip()
            mod = mod.strip() or "Ogólny"
            return f"{proj} - {mod}", proj, mod

    proj = cleaned.strip() or "Projekt"
    return f"{proj} - Ogólny", proj, "Ogólny"


def compute_file_watermark(path: Path) -> Dict[str, Any]:
    try:
        stat = path.stat()
        with path.open("rb") as fh:
            head = fh.read(4096)
        return {
            "last_scanned_mtime": round(stat.st_mtime, 3),
            "last_byte_offset": stat.st_size,
            "head_sha256": hashlib.sha256(head).hexdigest(),
        }
    except OSError:
        return {"last_scanned_mtime": 0.0, "last_byte_offset": 0, "head_sha256": ""}


def is_temporary_worktree_path(path_str: str) -> bool:
    norm = path_str.replace("/", "\\").lower()
    return (
        "\\al lab\\workspaces\\" in norm
        or "\\.gemini\\antigravity\\brain\\" in norm
        or "\\appdata\\local\\temp\\" in norm
        or "\\_do-usuniecia-" in norm
    )


def resolve_project_info(
    project_name: str,
    explicit_root: Optional[str] = None,
    config: Optional[StoreConfig] = None,
    discovered_cwds: Optional[List[str]] = None,
) -> Dict[str, str]:
    cfg = config or StoreConfig.default()
    proj_key = normalize_key(project_name)
    proj_slug = normalize_slug(project_name)

    if explicit_root:
        root_path = Path(explicit_root).resolve()
        if not root_path.is_dir():
            raise ValueError(f"--project-root must name an existing directory: {root_path}")
        return _build_project_info_dict(project_name, proj_slug, root_path)

    # 1. Check Agent Office thread-registry.json if present
    if cfg.office_registry_path.exists():
        try:
            reg = json.loads(cfg.office_registry_path.read_text(encoding="utf-8"))
            for rec in reg.get("records", []):
                rec_slug = normalize_key(str(rec.get("project_slug") or ""))
                raw_title = str(rec.get("display_name") or rec.get("title") or "")
                cwd = str(rec.get("cwd") or rec.get("project_root") or "")
                if cwd and (rec_slug == proj_key or title_matches_project(raw_title, project_name)):
                    p = Path(cwd)
                    if p.exists() and not is_temporary_worktree_path(str(p)):
                        return _build_project_info_dict(project_name, str(rec.get("project_slug") or proj_slug), p)
        except Exception:
            pass

    # 2. Check discovered thread cwds from native stores
    if discovered_cwds:
        valid_candidates: List[Path] = []
        for raw_cwd in discovered_cwds:
            if not raw_cwd or is_temporary_worktree_path(raw_cwd):
                continue
            p = Path(raw_cwd)
            if p.exists() and p.is_dir():
                if normalize_key(p.name) == proj_key:
                    valid_candidates.append(p)
        if valid_candidates:
            # Prefer shortest canonical path on D:\
            valid_candidates.sort(key=lambda x: (0 if str(x).upper().startswith("D:") else 1, len(str(x))))
            return _build_project_info_dict(project_name, proj_slug, valid_candidates[0])

    # 3. Bounded directory scan on configured drives (e.g. D:\<Client>\Programy\<Project> or D:\AL Lab\Aplikacje\<Project>)
    for drive in cfg.drives_to_search:
        if not drive.exists():
            continue
        try:
            for level1 in drive.iterdir():
                if not level1.is_dir() or level1.name.startswith(("$", ".", "_")):
                    continue
                if normalize_key(level1.name) == proj_key:
                    return _build_project_info_dict(project_name, proj_slug, level1)
                for level2 in _safe_iterdir(level1):
                    if not level2.is_dir() or level2.name.startswith((".", "_")):
                        continue
                    if normalize_key(level2.name) == proj_key and not is_temporary_worktree_path(str(level2)):
                        return _build_project_info_dict(project_name, proj_slug, level2)
                    if level2.name.lower() in ("programy", "aplikacje", "projekty", "projects", "apps", "repos"):
                        for level3 in _safe_iterdir(level2):
                            if level3.is_dir() and normalize_key(level3.name) == proj_key:
                                return _build_project_info_dict(project_name, proj_slug, level3)
        except OSError:
            continue

    raise ValueError(f"Project not found: {project_name}. Provide --project-root with its existing directory.")


def _safe_iterdir(path: Path) -> List[Path]:
    try:
        return list(path.iterdir())
    except OSError:
        return []


def _build_project_info_dict(project_name: str, project_slug: str, root_path: Path) -> Dict[str, str]:
    parts = root_path.parts
    client_name = "Wewnętrzny / AI Lab"
    if len(parts) >= 2 and parts[0].upper().startswith("D:"):
        top_folder = parts[1]
        if top_folder.lower() not in ("al lab", "dokumenty", "archiwum"):
            client_name = top_folder
    return {
        "project_name": project_name,
        "project_slug": normalize_key(project_slug) or normalize_slug(project_name),
        "project_root": str(root_path),
        "client_name": client_name,
    }


def extract_module_from_title(title: str, project_name: str) -> str:
    if not title:
        return "Ogólny"
    cleaned = title.strip()
    for sep in (" - ", " – ", " — ", ": "):
        if sep in cleaned:
            left, right = cleaned.split(sep, 1)
            if normalize_key(left) == normalize_key(project_name):
                return right.strip() or "Ogólny"
    return "Ogólny"


def title_matches_project(title: str, project_name: str) -> bool:
    if not title or not project_name:
        return False
    cleaned = title.strip()
    norm_proj = normalize_key(project_name)
    if not norm_proj:
        return False
    for sep in (" - ", " – ", " — ", ": ", " / "):
        if sep in cleaned:
            left, _ = cleaned.split(sep, 1)
            return normalize_key(left) == norm_proj
    return normalize_key(cleaned) == norm_proj


def _collapse_dot_segments(path_str: str) -> str:
    """Lexical normalization only. Does not touch the filesystem or resolve symlinks."""
    if not path_str:
        return ""
    raw = path_str.replace("/", "\\").strip().rstrip("\\")
    drive = ""
    rest = raw
    unc = raw.startswith("\\\\")
    if len(raw) >= 2 and raw[1] == ":":
        drive = raw[:2]
        rest = raw[2:]
    stack: List[str] = []
    for part in rest.split("\\"):
        if not part or part == ".":
            continue
        if part == "..":
            if stack:
                stack.pop()
            continue
        stack.append(part)
    body = "\\".join(stack)
    if drive:
        return f"{drive}\\{body}" if body else drive
    if unc:
        return "\\\\" + body if body else "\\\\"
    return body


def _path_key(path_str: str) -> str:
    return _collapse_dot_segments(path_str).lower()


def cwd_matches_project(cwd: str, project_root: str, project_name: str) -> bool:
    if not cwd:
        return False
    norm_cwd = _path_key(cwd)
    norm_root = _path_key(project_root or "")
    if norm_root:
        return norm_cwd == norm_root or norm_cwd.startswith(norm_root + "\\")
    proj_key = normalize_key(project_name)
    leaf_key = normalize_key(Path(norm_cwd).name)
    return bool(proj_key and leaf_key == proj_key)


def _linked_worktree_matches_project(cwd: str, project_name: str) -> bool:
    if not cwd or not is_temporary_worktree_path(cwd):
        return False
    proj_key = normalize_key(project_name)
    if not proj_key:
        return False
    leaf_raw = Path(_collapse_dot_segments(cwd)).name
    leaf_key = normalize_key(leaf_raw)
    if leaf_key == proj_key:
        return True
    # Allow hyphen/underscore suffixed worktrees like ExampleCRM-Project-20261023,
    # but never prefix collisions like AlphaBeta.
    for prefix_part in re.split(r"[-_]+", leaf_raw):
        if prefix_part:
            return normalize_key(prefix_part) == proj_key
    return False


def _cwd_is_compatible_for_titled_thread(cwd: str, project_root: str, project_name: str) -> bool:
    if not cwd or not project_root:
        return True
    if cwd_matches_project(cwd, project_root, project_name):
        return True
    if _linked_worktree_matches_project(cwd, project_name):
        return True
    if is_temporary_worktree_path(cwd):
        return False
    norm_cwd = _path_key(cwd)
    norm_root = _path_key(project_root)
    # Parent directory of project_root (e.g., D:\Example Client or D:\)
    if norm_root.startswith(norm_cwd.rstrip("\\") + "\\"):
        return True
    # Shared client organization name (e.g., Example Client in OneDrive/SharePoint sync dir)
    root_parts = [p for p in Path(norm_root).parts if len(normalize_key(p)) >= 5 and normalize_key(p) not in ("programy", "projekty", "aplikacje", "clients", "klienci")]
    if root_parts:
        client_token = re.sub(r"(?:spzoo|spolka|sa|sp)$", "", normalize_key(root_parts[0]))
        if len(client_token) >= 5 and client_token in normalize_key(norm_cwd):
            return True
    return False


def thread_belongs_to_project(title: str, cwd: str, project_root: str, project_name: str) -> bool:
    if cwd_matches_project(cwd, project_root, project_name):
        return True
    if _linked_worktree_matches_project(cwd, project_name):
        return True
    if not title_matches_project(title, project_name):
        return False
    return _cwd_is_compatible_for_titled_thread(cwd, project_root, project_name)


def classify_tier(thread_module: str, target_module: str, thread_title: str, project_name: str) -> int:
    norm_mod = normalize_key(thread_module)
    norm_target = normalize_key(target_module)
    if norm_target and norm_mod and (norm_mod == norm_target or norm_target in norm_mod or norm_mod in norm_target):
        return 1
    if norm_target and norm_target in normalize_key(thread_title):
        return 1
    return 2


def _extract_claude_content_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: List[str] = []
        for item in content:
            if isinstance(item, dict):
                if item.get("type") == "text" and isinstance(item.get("text"), str):
                    parts.append(item["text"])
            elif isinstance(item, str):
                parts.append(item)
        return "\n".join(parts)
    return ""


def collect_fast_cwd_hints(project_name: str, config: StoreConfig) -> List[str]:
    """Fast (<100ms) metadata-only CWD discovery from Claude local_*.json and Codex global state."""
    hints: List[str] = []
    if config.claude_sessions_root.exists():
        for meta_file in config.claude_sessions_root.rglob("local_*.json"):
            try:
                data = json.loads(meta_file.read_text(encoding="utf-8", errors="replace"))
                title = str(data.get("title") or "").strip()
                cwd = str(data.get("cwd") or data.get("originCwd") or "").strip()
                if cwd and title_matches_project(title, project_name):
                    hints.append(cwd)
            except Exception:
                continue
    global_state_path = config.codex_root / ".codex-global-state.json"
    if global_state_path.exists():
        try:
            gstate = json.loads(global_state_path.read_text(encoding="utf-8", errors="replace"))
            tmap = (gstate.get("thread-titles") or {}).get("titles") or {}
            whints = gstate.get("thread-workspace-root-hints") or {}
            if isinstance(tmap, dict) and isinstance(whints, dict):
                for sid, tname in tmap.items():
                    if title_matches_project(str(tname), project_name) and whints.get(sid):
                        hints.append(str(whints[sid]))
        except Exception:
            pass
    return hints


def scan_claude_threads(
    project_name: str,
    project_root: str,
    target_module: str,
    config: StoreConfig,
    prior_watermarks: Dict[str, Dict[str, Any]],
    force_rescan: bool = False,
) -> List[DiscoveredThread]:
    results: Dict[str, DiscoveredThread] = {}
    cli_to_meta: Dict[str, Dict[str, Any]] = {}
    matched_cli_ids: set[str] = set()

    if config.claude_sessions_root.exists():
        for meta_file in config.claude_sessions_root.rglob("local_*.json"):
            try:
                data = json.loads(meta_file.read_text(encoding="utf-8", errors="replace"))
            except Exception:
                continue
            title = str(data.get("title") or "").strip()
            cwd = str(data.get("cwd") or data.get("originCwd") or "").strip()
            cli_id = str(data.get("cliSessionId") or data.get("sessionId") or meta_file.stem).strip()
            cli_to_meta[cli_id] = {
                "title": title,
                "cwd": cwd,
                "meta_path": str(meta_file),
                "last_activity": data.get("lastActivityAt"),
                "post_turn_summary": str(data.get("postTurnSummary") or "").strip(),
                "session_id": str(data.get("sessionId") or cli_id),
            }
            if thread_belongs_to_project(title, cwd, project_root, project_name):
                matched_cli_ids.add(cli_id)

    jsonl_map: Dict[str, Path] = {}
    proj_key = normalize_key(project_name)
    root_leaf_key = normalize_key(Path(project_root).name) if project_root else ""

    if config.claude_projects_root.exists():
        for proj_subdir in _safe_iterdir(config.claude_projects_root):
            if not proj_subdir.is_dir():
                continue
            subdir_key = normalize_key(proj_subdir.name)
            subdir_matches = bool(
                (proj_key and subdir_key.endswith(proj_key))
                or (root_leaf_key and len(root_leaf_key) >= 5 and subdir_key.endswith(root_leaf_key))
            )
            # Only scan top-level *.jsonl in each project directory (skip subagents/)
            for jf in _safe_iterdir(proj_subdir):
                if jf.is_file() and jf.suffix == ".jsonl":
                    stem = jf.stem
                    if stem in matched_cli_ids or (subdir_matches and stem not in cli_to_meta):
                        jsonl_map[stem] = jf
                        if subdir_matches and stem not in cli_to_meta:
                            matched_cli_ids.add(stem)

    for cid in sorted(matched_cli_ids):
        meta = cli_to_meta.get(cid, {})
        title = str(meta.get("title") or "")
        cwd = str(meta.get("cwd") or "")
        jpath = jsonl_map.get(cid)

        display_title = title or f"{project_name} - Sesja Claude ({cid[:8]})"
        module = extract_module_from_title(display_title, project_name)
        tier = classify_tier(module, target_module, display_title, project_name)
        store_file = jpath if jpath is not None else Path(str(meta.get("meta_path") or ""))
        wm = compute_file_watermark(store_file) if store_file and store_file.exists() else {}
        thread_id = f"claude:{cid}"

        messages: List[MessageItem] = []
        reused = False
        prior_wm = prior_watermarks.get(thread_id)
        if (
            not force_rescan
            and prior_wm
            and wm
            and prior_wm.get("head_sha256") == wm.get("head_sha256")
            and prior_wm.get("last_byte_offset") == wm.get("last_byte_offset")
        ):
            reused = True

        if meta.get("post_turn_summary"):
            messages.append(MessageItem(role="summary", text=str(meta["post_turn_summary"]), timestamp=""))

        if jpath and jpath.exists():
            try:
                with jpath.open("r", encoding="utf-8", errors="replace") as fh:
                    for raw_line in fh:
                        raw_line = raw_line.strip()
                        if not raw_line:
                            continue
                        try:
                            obj = json.loads(raw_line)
                        except Exception:
                            continue
                        ltype = obj.get("type")
                        if obj.get("cwd") and not cwd:
                            cwd = str(obj["cwd"])
                        if ltype == "custom-title" and obj.get("customTitle"):
                            display_title = str(obj["customTitle"])
                            module = extract_module_from_title(display_title, project_name)
                            tier = classify_tier(module, target_module, display_title, project_name)
                        elif ltype in ("user", "assistant"):
                            msg = obj.get("message") or {}
                            text = strip_system_envelopes(_extract_claude_content_text(msg.get("content")))
                            if text:
                                messages.append(
                                    MessageItem(
                                        role=str(ltype),
                                        text=text,
                                        timestamp=str(obj.get("timestamp") or "")[:10],
                                    )
                                )
            except Exception:
                pass

        if not thread_belongs_to_project(display_title, cwd, project_root, project_name):
            continue

        updated_iso = _format_epoch_or_iso(meta.get("last_activity"), store_file)
        results[thread_id] = DiscoveredThread(
            id=thread_id,
            desktop="claude",
            native_id=cid,
            title=display_title,
            module=module,
            tier=tier,
            cwd=cwd or project_root,
            store_path=str(store_file),
            updated_at=updated_iso,
            messages=messages,
            watermark=wm,
            incremental_reused=reused,
        )

    return list(results.values())


def scan_codex_threads(
    project_name: str,
    project_root: str,
    target_module: str,
    config: StoreConfig,
    prior_watermarks: Dict[str, Dict[str, Any]],
    force_rescan: bool = False,
) -> List[DiscoveredThread]:
    results: Dict[str, DiscoveredThread] = {}
    if not config.codex_root.exists():
        return []

    titles_by_id: Dict[str, str] = {}
    updated_by_id: Dict[str, str] = {}
    cwd_hints_by_id: Dict[str, str] = {}

    idx_path = config.codex_root / "session_index.jsonl"
    if idx_path.exists():
        try:
            with idx_path.open("r", encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        obj = json.loads(line)
                    except Exception:
                        continue
                    sid = str(obj.get("id") or "").strip()
                    tname = str(obj.get("thread_name") or "").strip()
                    if sid and tname:
                        titles_by_id[sid] = tname
                    if sid and obj.get("updated_at"):
                        updated_by_id[sid] = str(obj["updated_at"])
        except Exception:
            pass

    global_state_path = config.codex_root / ".codex-global-state.json"
    if global_state_path.exists():
        try:
            gstate = json.loads(global_state_path.read_text(encoding="utf-8", errors="replace"))
            tmap = (gstate.get("thread-titles") or {}).get("titles") or {}
            if isinstance(tmap, dict):
                for sid, tname in tmap.items():
                    if sid and tname and sid not in titles_by_id:
                        titles_by_id[str(sid)] = str(tname)
            hints = gstate.get("thread-workspace-root-hints") or {}
            if isinstance(hints, dict):
                for sid, cwd_hint in hints.items():
                    if sid and isinstance(cwd_hint, str):
                        cwd_hints_by_id[str(sid)] = cwd_hint
        except Exception:
            pass

    matched_sids: set[str] = set()
    for sid, title in titles_by_id.items():
        cwd_hint = cwd_hints_by_id.get(sid, "")
        if thread_belongs_to_project(title, cwd_hint, project_root, project_name):
            matched_sids.add(sid)
    for sid, cwd_hint in cwd_hints_by_id.items():
        if cwd_matches_project(cwd_hint, project_root, project_name):
            matched_sids.add(sid)

    rollout_by_id: Dict[str, Path] = {}
    sessions_dir = config.codex_root / "sessions"
    if sessions_dir.exists():
        all_rollouts = list(sessions_dir.rglob("rollout-*.jsonl"))
        for rf in all_rollouts:
            m = re.search(r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})\.jsonl$", rf.name, re.I)
            if m:
                rollout_by_id[m.group(1)] = rf

        for sid, rf in rollout_by_id.items():
            try:
                with rf.open("r", encoding="utf-8", errors="replace") as fh:
                    first_line = fh.readline().strip()
                    if first_line:
                        meta_obj = json.loads(first_line)
                        if meta_obj.get("type") == "session_meta":
                            cwd_val = str((meta_obj.get("payload") or {}).get("cwd") or "")
                            if cwd_val:
                                cwd_hints_by_id[sid] = cwd_val
                            if thread_belongs_to_project(titles_by_id.get(sid, ""), cwd_val, project_root, project_name):
                                matched_sids.add(sid)
                            elif sid in matched_sids:
                                matched_sids.discard(sid)
            except Exception:
                pass

    for sid in sorted(matched_sids):
        title = titles_by_id.get(sid, "")
        cwd = cwd_hints_by_id.get(sid, "")
        rpath = rollout_by_id.get(sid)

        display_title = title or f"{project_name} - Sesja Codex ({sid[:8]})"
        module = extract_module_from_title(display_title, project_name)
        tier = classify_tier(module, target_module, display_title, project_name)
        wm = compute_file_watermark(rpath) if rpath and rpath.exists() else {}
        thread_id = f"codex:{sid}"
        reused = bool(
            not force_rescan
            and prior_watermarks.get(thread_id)
            and wm
            and prior_watermarks[thread_id].get("head_sha256") == wm.get("head_sha256")
            and prior_watermarks[thread_id].get("last_byte_offset") == wm.get("last_byte_offset")
        )

        messages: List[MessageItem] = []
        if rpath and rpath.exists():
            try:
                with rpath.open("r", encoding="utf-8", errors="replace") as fh:
                    for raw_line in fh:
                        raw_line = raw_line.strip()
                        if not raw_line:
                            continue
                        try:
                            obj = json.loads(raw_line)
                        except Exception:
                            continue
                        otype = obj.get("type")
                        payload = obj.get("payload") or {}
                        ts = str(obj.get("timestamp") or "")[:10]
                        if otype == "session_meta" and payload.get("cwd"):
                            cwd = str(payload["cwd"])
                        elif otype == "event_msg":
                            ev_type = payload.get("type")
                            if ev_type == "user_message" and payload.get("message"):
                                cleaned_u = strip_system_envelopes(str(payload["message"]))
                                if cleaned_u:
                                    messages.append(MessageItem(role="user", text=cleaned_u, timestamp=ts))
                            elif ev_type == "agent_message" and payload.get("message"):
                                cleaned_a = strip_system_envelopes(str(payload["message"]))
                                if cleaned_a:
                                    messages.append(MessageItem(role="assistant", text=cleaned_a, timestamp=ts))
                        elif otype == "response_item" and payload.get("type") == "message":
                            role = str(payload.get("role") or "")
                            if role in ("user", "assistant"):
                                content_list = payload.get("content") or []
                                texts = [
                                    strip_system_envelopes(str(c.get("text") or ""))
                                    for c in content_list
                                    if isinstance(c, dict) and c.get("type") in ("input_text", "output_text", "text")
                                ]
                                joined = "\n".join(t for t in texts if t)
                                if joined:
                                    messages.append(MessageItem(role=role, text=joined, timestamp=ts))
            except Exception:
                pass

        if not thread_belongs_to_project(title, cwd, project_root, project_name):
            continue

        updated_iso = updated_by_id.get(sid) or _format_epoch_or_iso(None, rpath)
        results[thread_id] = DiscoveredThread(
            id=thread_id,
            desktop="codex",
            native_id=sid,
            title=display_title,
            module=module,
            tier=tier,
            cwd=cwd or project_root,
            store_path=str(rpath or idx_path),
            updated_at=updated_iso,
            messages=messages,
            watermark=wm,
            incremental_reused=reused,
        )

    return list(results.values())


def _connect_sqlite_ro(db_path: Path) -> sqlite3.Connection:
    uri = f"file:{ urllib.parse.quote(db_path.resolve().as_posix()) }?mode=ro"
    return sqlite3.connect(uri, uri=True, timeout=5.0)


def scan_zcode_threads(
    project_name: str,
    project_root: str,
    target_module: str,
    config: StoreConfig,
    prior_watermarks: Dict[str, Dict[str, Any]],
    force_rescan: bool = False,
) -> List[DiscoveredThread]:
    results: Dict[str, DiscoveredThread] = {}
    if not config.zcode_root.exists():
        return []

    cli_db = config.zcode_root / "cli" / "db" / "db.sqlite"
    if cli_db.exists():
        wm = compute_file_watermark(cli_db)
        try:
            conn = _connect_sqlite_ro(cli_db)
            try:
                cur = conn.cursor()
                cur.execute("SELECT id, title, directory, time_updated FROM session")
                for sid, title, directory, time_updated in cur.fetchall():
                    title_s = str(title or "").strip()
                    dir_s = str(directory or "").strip()
                    if not thread_belongs_to_project(title_s, dir_s, project_root, project_name):
                        continue
                    display_title = title_s or f"{project_name} - Sesja ZCode ({sid})"
                    module = extract_module_from_title(display_title, project_name)
                    tier = classify_tier(module, target_module, display_title, project_name)
                    thread_id = f"zcode:{sid}"

                    messages: List[MessageItem] = []
                    cur.execute(
                        "SELECT m.id, m.data, p.data FROM message m LEFT JOIN part p ON p.message_id = m.id WHERE m.session_id = ? ORDER BY m.time_created ASC",
                        (sid,),
                    )
                    for _mid, m_data_raw, p_data_raw in cur.fetchall():
                        try:
                            m_data = json.loads(m_data_raw) if m_data_raw else {}
                            p_data = json.loads(p_data_raw) if p_data_raw else {}
                        except Exception:
                            continue
                        role = str(m_data.get("role") or "user")
                        if p_data.get("type") == "text" and p_data.get("text"):
                            messages.append(
                                MessageItem(
                                    role=role if role in ("user", "assistant") else "assistant",
                                    text=str(p_data["text"]),
                                    timestamp=_format_epoch_or_iso(time_updated, None)[:10],
                                )
                            )
                    results[thread_id] = DiscoveredThread(
                        id=thread_id,
                        desktop="zcode",
                        native_id=str(sid),
                        title=display_title,
                        module=module,
                        tier=tier,
                        cwd=dir_s or project_root,
                        store_path=str(cli_db),
                        updated_at=_format_epoch_or_iso(time_updated, cli_db),
                        messages=messages,
                        watermark=wm,
                    )
            finally:
                conn.close()
        except Exception:
            pass

    tasks_db = config.zcode_root / "v2" / "tasks-index.sqlite"
    if tasks_db.exists():
        wm = compute_file_watermark(tasks_db)
        try:
            conn = _connect_sqlite_ro(tasks_db)
            try:
                cur = conn.cursor()
                cur.execute("SELECT task_id, title, workspace_path, updated_at, searchable_text FROM tasks")
                for tid, title, ws_path, updated_at, searchable_text in cur.fetchall():
                    thread_id = f"zcode:{tid}"
                    if thread_id in results:
                        continue
                    title_s = str(title or "").strip()
                    ws_s = str(ws_path or "").strip()
                    if not thread_belongs_to_project(title_s, ws_s, project_root, project_name):
                        continue
                    display_title = title_s or f"{project_name} - Sesja ZCode ({tid})"
                    module = extract_module_from_title(display_title, project_name)
                    tier = classify_tier(module, target_module, display_title, project_name)
                    msgs: List[MessageItem] = []
                    if searchable_text:
                        msgs.append(MessageItem(role="summary", text=str(searchable_text), timestamp=""))
                    results[thread_id] = DiscoveredThread(
                        id=thread_id,
                        desktop="zcode",
                        native_id=str(tid),
                        title=display_title,
                        module=module,
                        tier=tier,
                        cwd=ws_s or project_root,
                        store_path=str(tasks_db),
                        updated_at=_format_epoch_or_iso(updated_at, tasks_db),
                        messages=msgs,
                        watermark=wm,
                    )
            finally:
                conn.close()
        except Exception:
            pass

    return list(results.values())


def _workspace_paths_from_field(raw: str) -> List[str]:
    """Split workspace URI text before percent-decoding so spaces stay inside the URI."""
    text = raw or ""
    uris: List[str] = []
    seen_uri = set()

    def add(uri: str) -> None:
        uri = uri.strip()
        if not uri.lower().startswith("file:///"):
            return
        key = uri.lower()
        if key in seen_uri:
            return
        seen_uri.add(key)
        uris.append(uri)

    for match in re.finditer(r"file:///[^\s\"'\\,]+", text, flags=re.IGNORECASE):
        add(match.group(0))
    for match in re.finditer(r'"(file:///[^"]*)"', text, flags=re.IGNORECASE):
        add(match.group(1))

    paths: List[str] = []
    seen_path = set()
    for uri in uris:
        decoded = urllib.parse.unquote(uri)
        path = _collapse_dot_segments(decoded[len("file:///") :].replace("/", "\\"))
        key = path.lower()
        if not key or key in seen_path:
            continue
        seen_path.add(key)
        paths.append(path)
    return paths


def scan_antigravity_threads(
    project_name: str,
    project_root: str,
    target_module: str,
    config: StoreConfig,
    prior_watermarks: Dict[str, Dict[str, Any]],
    force_rescan: bool = False,
) -> List[DiscoveredThread]:
    results: Dict[str, DiscoveredThread] = {}
    if not config.antigravity_root.exists():
        return []

    db_path = config.antigravity_root / "conversation_summaries.db"
    if not db_path.exists():
        return []

    try:
        conn = _connect_sqlite_ro(db_path)
        try:
            cur = conn.cursor()
            fts_by_id: Dict[str, Tuple[str, str, str, str]] = {}
            try:
                cur.execute("SELECT conversation_id, title, workspace, user_body, agent_body FROM conversations_fts")
                for cid, ftitle, fws, ubody, abody in cur.fetchall():
                    fts_by_id[str(cid)] = (
                        str(ftitle or ""),
                        str(fws or ""),
                        str(ubody or ""),
                        str(abody or ""),
                    )
            except Exception:
                pass

            cur.execute("SELECT conversation_id, title, workspace_uris, last_modified_time FROM conversation_summaries")
            rows = cur.fetchall()
        finally:
            conn.close()
    except Exception:
        return []

    for cid_raw, title_raw, ws_uris_raw, last_mod in rows:
        cid = str(cid_raw or "").strip()
        if not cid:
            continue
        fts_title, fts_ws, ubody, abody = fts_by_id.get(cid, ("", "", "", ""))
        title = str(title_raw or fts_title or "").strip()
        ws_paths = _workspace_paths_from_field(str(ws_uris_raw or fts_ws or ""))
        matched_ws = [
            wp
            for wp in ws_paths
            if cwd_matches_project(wp, project_root, project_name)
            or _linked_worktree_matches_project(wp, project_name)
            or thread_belongs_to_project(title, wp, project_root, project_name)
        ]
        if matched_ws:
            origin_ws = matched_ws[0]
        elif not ws_paths and thread_belongs_to_project(title, "", project_root, project_name):
            origin_ws = ""
        else:
            continue

        display_title = title if title and not title.startswith("Conversation ") else f"{project_name} - Sesja Antigravity ({cid[:8]})"
        module = extract_module_from_title(display_title, project_name)
        tier = classify_tier(module, target_module, display_title, project_name)

        transcript_path = config.antigravity_root / "brain" / cid / ".system_generated" / "logs" / "transcript.jsonl"
        store_target = transcript_path if transcript_path.exists() else db_path
        wm = compute_file_watermark(store_target)

        messages: List[MessageItem] = []
        if transcript_path.exists():
            try:
                with transcript_path.open("r", encoding="utf-8", errors="replace") as fh:
                    for raw_line in fh:
                        raw_line = raw_line.strip()
                        if not raw_line:
                            continue
                        try:
                            obj = json.loads(raw_line)
                        except Exception:
                            continue
                        stype = obj.get("type")
                        content = str(obj.get("content") or "").strip()
                        ts = str(obj.get("created_at") or "")[:10]
                        if stype == "USER_INPUT" and content:
                            messages.append(MessageItem(role="user", text=content, timestamp=ts))
                        elif stype == "PLANNER_RESPONSE" and content:
                            messages.append(MessageItem(role="assistant", text=content, timestamp=ts))
            except Exception:
                pass
        else:
            if ubody:
                messages.append(MessageItem(role="user", text=ubody, timestamp=str(last_mod or "")[:10]))
            if abody:
                messages.append(MessageItem(role="assistant", text=abody, timestamp=str(last_mod or "")[:10]))

        thread_id = f"antigravity:{cid}"
        results[thread_id] = DiscoveredThread(
            id=thread_id,
            desktop="antigravity",
            native_id=cid,
            title=display_title,
            module=module,
            tier=tier,
            cwd=origin_ws or project_root,
            store_path=str(store_target),
            updated_at=_format_epoch_or_iso(last_mod, store_target),
            messages=messages,
            watermark=wm,
        )

    return list(results.values())


def scan_office_threads(
    project_name: str,
    project_slug: str,
    project_root: str,
    target_module: str,
    config: StoreConfig,
) -> List[DiscoveredThread]:
    results: List[DiscoveredThread] = []
    if not config.office_registry_path.exists():
        return results
    try:
        data = json.loads(config.office_registry_path.read_text(encoding="utf-8", errors="replace"))
        wm = compute_file_watermark(config.office_registry_path)
        for rec in data.get("records", []):
            rec_slug = normalize_key(str(rec.get("project_slug") or ""))
            title = str(rec.get("title") or rec.get("display_name") or "").strip()
            rec_cwd = str(rec.get("cwd") or "").strip()
            if rec_slug != normalize_key(project_slug) and not thread_belongs_to_project(title, rec_cwd, project_root, project_name):
                continue
            if rec_cwd and project_root and not cwd_matches_project(rec_cwd, project_root, project_name) and not is_temporary_worktree_path(rec_cwd):
                continue
            sid = str(rec.get("session_id") or rec.get("thread_id") or "office")
            harness = str(rec.get("harness") or "agent_office")
            display_title = title or f"{project_name} - {target_module}"
            module = extract_module_from_title(display_title, project_name)
            tier = classify_tier(module, target_module, display_title, project_name)
            results.append(
                DiscoveredThread(
                    id=f"agent_office:{harness}:{sid}",
                    desktop="agent_office",
                    native_id=f"{harness}:{sid}",
                    title=display_title,
                    module=module,
                    tier=tier,
                    cwd=str(rec.get("cwd") or project_root),
                    store_path=str(config.office_registry_path),
                    updated_at=str(rec.get("updated_at") or now_iso()),
                    messages=[],
                    watermark=wm,
                )
            )
    except Exception:
        pass
    return results


def _format_epoch_or_iso(val: Any, fallback_path: Optional[Path]) -> str:
    if isinstance(val, (int, float)) and val > 0:
        ts = val / 1000.0 if val > 1e11 else float(val)
        try:
            return dt.datetime.fromtimestamp(ts, tz=dt.timezone.utc).astimezone().isoformat(timespec="seconds")
        except Exception:
            pass
    if isinstance(val, str) and val.strip():
        if val.isdigit():
            return _format_epoch_or_iso(int(val), fallback_path)
        return val.strip()
    if fallback_path and fallback_path.exists():
        try:
            return (
                dt.datetime.fromtimestamp(fallback_path.stat().st_mtime, tz=dt.timezone.utc)
                .astimezone()
                .isoformat(timespec="seconds")
            )
        except Exception:
            pass
    return now_iso()


def extract_knowledge_from_threads(
    threads: List[DiscoveredThread],
    target_module: str,
) -> Tuple[List[Dict[str, str]], List[Dict[str, str]], Dict[str, Dict[str, Any]]]:
    """
    Extracts:
    - decisions: list of {date, module, source, decision, context, status}
    - problems: list of {date, module, source, symptom, resolution}
    - modules_summary: dict[module_name] -> {last_thread, updated_at, points, open_topics, thread_count}
    """
    decisions: List[Dict[str, str]] = []
    problems: List[Dict[str, str]] = []
    seen_decisions: set[str] = set()
    seen_problems: set[str] = set()
    modules_summary: Dict[str, Dict[str, Any]] = {}

    # Keep Tier 1 first, then prefer recent evidence within each tier.
    sorted_threads = sorted(threads, key=lambda t: t.updated_at or "", reverse=True)
    sorted_threads.sort(key=lambda t: t.tier)

    for th in sorted_threads:
        mod_name = th.module or "Ogólny"
        ms = modules_summary.setdefault(
            mod_name,
            {
                "module": mod_name,
                "last_thread_title": th.title,
                "last_thread_id": th.id,
                "updated_at": th.updated_at,
                "thread_count": 0,
                "desktops": set(),
                "points": [],
                "open_topics": [],
            },
        )
        ms["thread_count"] += 1
        ms["desktops"].add(th.desktop)
        if (th.updated_at or "") >= (ms.get("updated_at") or ""):
            ms["updated_at"] = th.updated_at
            ms["last_thread_title"] = th.title
            ms["last_thread_id"] = th.id

        max_per_thread = 10 if th.tier == 1 else 4
        thread_decisions = 0
        thread_problems = 0

        for msg in reversed(th.messages):
            raw_text = redact_secrets(msg.text or "")
            if contains_prompt_injection(raw_text):
                continue

            date_str = (msg.timestamp or th.updated_at or now_iso())[:10]
            source_ref = f"{mod_name} (`{th.desktop}:{th.native_id[:8]}`)"

            for para in re.split(r"[\r\n]+", raw_text):
                line = sanitize_text_line(para, max_len=240)
                if not line or len(line) < 24:
                    continue
                if any(np.search(line) for np in NOISE_LINE_PATTERNS):
                    continue

                norm_sig = normalize_key(line[:90])
                if not norm_sig or len(norm_sig) < 12:
                    continue

                if msg.role == "user" and DECISION_KEYWORDS.search(line) and thread_decisions < max_per_thread:
                    if norm_sig not in seen_decisions:
                        seen_decisions.add(norm_sig)
                        decisions.append(
                            {
                                "date": date_str,
                                "module_source": source_ref,
                                "decision": line,
                                "context": f"Wątek `{th.title}` (Tier {th.tier})",
                                "status": "ACTIVE",
                            }
                        )
                        thread_decisions += 1
                        if len(ms["points"]) < 6:
                            ms["points"].append(line)

                elif PROBLEM_KEYWORDS.search(line) and thread_problems < max_per_thread:
                    if norm_sig not in seen_problems:
                        seen_problems.add(norm_sig)
                        problems.append(
                            {
                                "date": date_str,
                                "module_source": source_ref,
                                "symptom": line[:140],
                                "resolution": (
                                    f"Zidentyfikowano w `{th.title}` ({th.desktop}:{th.native_id[:8]}) — "
                                    + (line[140:] if len(line) > 140 else "zweryfikuj ścieżkę przed zmianą.")
                                ),
                            }
                        )
                        thread_problems += 1

                elif msg.role in ("summary", "user") and len(ms["points"]) < 6:
                    if norm_sig not in seen_decisions:
                        seen_decisions.add(norm_sig)
                        ms["points"].append(line)

        # Capture the most recent user message in this thread as open/recent topic for the module
        user_msgs = [
            sanitize_text_line(m.text, max_len=220)
            for m in reversed(th.messages)
            if m.role == "user" and len(sanitize_text_line(m.text, max_len=220)) >= 20
        ]
        for uline in user_msgs[:2]:
            if uline and not any(np.search(uline) for np in NOISE_LINE_PATTERNS):
                if uline not in ms["open_topics"] and len(ms["open_topics"]) < 4:
                    ms["open_topics"].append(uline)

        th.decisions_extracted = thread_decisions
        th.problems_extracted = thread_problems

    # Ensure target_module exists in modules_summary even if this is the first thread for it
    if target_module and target_module not in modules_summary:
        modules_summary[target_module] = {
            "module": target_module,
            "last_thread_title": f"{target_module} (nowy wątek)",
            "last_thread_id": "current",
            "updated_at": now_iso(),
            "thread_count": 0,
            "desktops": set(),
            "points": ["Moduł zainicjalizowany przez /clone-thread na bazie wiedzy przekrojowej projektu (Tier 2)."],
            "open_topics": ["Kontynuacja prac w module na podstawie ustaleń projektowych."],
        }

    return decisions[:25], problems[:20], modules_summary


def parse_existing_manual_rows(knowledge_path: Path) -> Tuple[List[str], List[str]]:
    """Preserve dated knowledge rows, including direct edits without a MANUAL marker."""
    if not knowledge_path.exists():
        return [], []
    return _parse_knowledge_rows(knowledge_path.read_text(encoding="utf-8"))


def _parse_knowledge_rows(text: str) -> Tuple[List[str], List[str]]:
    manual_decisions: List[str] = []
    manual_problems: List[str] = []
    section = 0
    for line in text.splitlines():
        heading = re.match(r"^## ([1-5])\.", line)
        if heading:
            section = int(heading.group(1))
            continue
        if not re.match(r"^\|\s*\d{4}-\d{2}-\d{2}\s*\|", line):
            continue
        # Generated empty-state rows are not historical knowledge.
        if "Normalizacja `/clone-thread`" in line or "Dopisuj kolejne pułapki przez" in line:
            continue
        if contains_prompt_injection(line):
            continue
        if section == 2:
            manual_decisions.append(redact_secrets(line))
        elif section == 4:
            manual_problems.append(redact_secrets(line))
    return manual_decisions, manual_problems


def _knowledge_row_key(row: str) -> Tuple[str, str]:
    cells = row.strip().strip("|").split("|")
    if len(cells) >= 3:
        return normalize_key(cells[1]), normalize_key(cells[2])
    return "", normalize_key(row)


def _merge_knowledge_rows(existing: List[str], generated: Iterable[str]) -> List[str]:
    rows: List[str] = []
    seen: set[Tuple[str, str]] = set()
    for row in [*existing, *generated]:
        key = _knowledge_row_key(row)
        if key not in seen:
            rows.append(row)
            seen.add(key)
    return rows


def _rows_as_records(rows: List[str], columns: Tuple[str, ...]) -> List[Dict[str, str]]:
    records: List[Dict[str, str]] = []
    for row in rows:
        cells = [cell.strip() for cell in row.strip().strip("|").split("|")]
        if len(cells) == len(columns):
            records.append(dict(zip(columns, cells)))
    return sorted(records, key=lambda record: record["date"], reverse=True)


def render_project_knowledge_md(
    project_info: Dict[str, str],
    target_module: str,
    tools_with_threads: List[str],
    threads: List[DiscoveredThread],
    decisions: List[Dict[str, str]],
    problems: List[Dict[str, str]],
    modules_summary: Dict[str, Dict[str, Any]],
    manual_decisions: Optional[List[str]] = None,
    manual_problems: Optional[List[str]] = None,
) -> str:
    manual_decisions = manual_decisions or []
    manual_problems = manual_problems or []
    proj_name = project_info["project_name"]
    proj_root = project_info["project_root"]
    proj_slug = project_info["project_slug"]
    client_name = project_info["client_name"]
    ts = now_iso()

    tools_str = ", ".join(tools_with_threads) if tools_with_threads else "brak (pierwszy wątek)"
    module_names = sorted(modules_summary.keys(), key=lambda m: (0 if normalize_key(m) == normalize_key(target_module) else 1, m.lower()))

    lines: List[str] = [
        f"# PROJECT_KNOWLEDGE - {proj_name}",
        "",
        f"> Kanoniczna pamięć projektowa dla narzędzi Desktop (`Claude`, `Codex`, `ZCode`, `Antigravity`, `Kimi`).",
        f"> Projekt: `{proj_root}` | Klient: `{client_name}` | Slug: `{proj_slug}` | Aktualizacja: `{ts}`",
        "",
        "## 1. Jak czytać (granice źródeł)",
        "- `APP_PROFILE.md` — karta techniczna, środowisko uruchomieniowe, porty, serwer i procedura wdrożenia.",
        "- `PROJECT_KNOWLEDGE.md` (ten plik) — decyzje właściciela projektu, kontekst biznesowy, historyczne problemy/pułapki oraz stan poszczególnych grup modułów (`<Projekt> - <Moduł>`).",
        f"- `.agents/desktop-threads.json` — rejestr narzędzi Desktop posiadających minimum 1 wątek w projekcie (`tools_with_threads`: `{tools_str}`).",
        "",
        "## 2. Decyzje właściciela projektu i reguły biznesowe",
        "| Data | Moduł / Wątek | Decyzja / Reguła biznesowa | Kontekst / Źródło | Status |",
        "|---|---|---|---|---|",
    ]

    decision_rows = _merge_knowledge_rows(manual_decisions, (
        f"| {d['date']} | {d['module_source']} | {d['decision']} | {d['context']} | {d['status']} |"
        for d in decisions
    ))
    lines.extend(decision_rows)
    if not decision_rows:
        lines.append(
            f"| {ts[:10]} | {target_module} | Ustalone nazewnictwo wątków `{proj_name} - <Moduł>` i wspólna pamięć w `PROJECT_KNOWLEDGE.md` | Normalizacja `/clone-thread` | ACTIVE |"
        )

    lines.extend(
        [
            "",
            "## 3. Kontekst biznesowy i domenowy",
            f"- **Klient / Organizacja**: {client_name}",
            f"- **Projekt**: {proj_name} (`{proj_root}`)",
            f"- **Narzędzia Desktop z aktywnymi wątkami ({len(tools_with_threads)})**: `{tools_str}` (łącznie {len(threads)} wątków)",
            f"- **Rozpoznane grupy modułów (`{proj_name} - <Moduł>`)**: {', '.join(f'`{m}`' for m in module_names)}",
            "",
            "## 4. Historyczne problemy i pułapki",
            "| Data | Moduł / Wątek | Objaw / Problem | Przyczyna / Rozwiązanie (czego unikać) |",
            "|---|---|---|---|",
        ]
    )

    problem_rows = _merge_knowledge_rows(manual_problems, (
        f"| {p['date']} | {p['module_source']} | {p['symptom']} | {p['resolution']} |"
        for p in problems
    ))
    lines.extend(problem_rows)
    if not problem_rows:
        lines.append(
            f"| {ts[:10]} | Ogólny | Brak zarejestrowanych krytycznych regresji w dotychczasowych transkrypcjach | Dopisuj kolejne pułapki przez `clone_thread.py record` |"
        )

    lines.extend(
        [
            "",
            "## 5. Stan modułów (<Moduł>)",
        ]
    )

    for mod in module_names:
        ms = modules_summary[mod]
        desktops_str = ", ".join(sorted(ms["desktops"])) if ms["desktops"] else "bieżąca sesja"
        lines.append(f"### {mod}")
        lines.append(
            f"- **Ostatni aktywny wątek**: `{ms['last_thread_title']}` (`{ms['last_thread_id']}`, narzędzia: `{desktops_str}`, wątków: {ms['thread_count']}, ost. akt.: `{str(ms['updated_at'])[:19]}`)"
        )
        lines.append("- **Kluczowe ustalenia i kontekst modułu**:")
        if ms["points"]:
            for pt in ms["points"][:5]:
                lines.append(f"  - {pt}")
        else:
            lines.append("  - Brak wyodrębnionych szczegółowych notatek; sprawdź decyzje przekrojowe w Sekcji 2.")
        lines.append("- **Ostatnie prompty / otwarte tematy**:")
        if ms["open_topics"]:
            for ot in ms["open_topics"][:3]:
                lines.append(f"  - {ot}")
        else:
            lines.append("  - Gotowy do kontynuacji w bieżącym wątku.")
        lines.append("")

    return compact_knowledge_markdown("\n".join(lines).rstrip() + "\n")


def compact_knowledge_markdown(content: str) -> str:
    """Bound UTF-8 output while preserving headings and protected decision rows."""
    lines = content.splitlines()
    while True:
        rendered = "\n".join(lines).rstrip() + "\n"
        if len(rendered.splitlines()) <= MAX_KNOWLEDGE_LINES and len(rendered.encode("utf-8")) <= MAX_KNOWLEDGE_BYTES:
            return rendered
        section = 0
        candidates: List[Tuple[int, str, int]] = []
        for idx, row in enumerate(lines):
            heading = re.match(r"^## ([1-5])\.", row)
            if heading:
                section = int(heading.group(1))
            dated_row = re.match(r"^\|\s*(\d{4}-\d{2}-\d{2})\s*\|", row)
            if dated_row and "[MANUAL]" not in row:
                if "SUPERSEDED" in row:
                    candidates.append((0, dated_row.group(1), idx))
                elif section == 4:
                    candidates.append((1, dated_row.group(1), idx))
        if candidates:
            del lines[min(candidates)[2]]
            continue
        # Module summaries are reproducible from transcripts; keep their headings.
        notes = [idx for idx, line in enumerate(lines) if line.startswith("  - ")]
        if notes:
            del lines[max(notes, key=lambda idx: len(lines[idx].encode("utf-8")))]
            continue
        raise ValueError("PROJECT_KNOWLEDGE.md exceeds 400 lines / 25 KiB; shorten protected rows before retrying.")


def build_desktop_threads_registry(
    project_info: Dict[str, str],
    threads: List[DiscoveredThread],
    current_tool: Optional[str] = None,
    current_session_id: Optional[str] = None,
    canonical_title: Optional[str] = None,
    target_module: Optional[str] = None,
) -> Dict[str, Any]:
    serialized_threads: List[Dict[str, Any]] = []
    for t in sorted(threads, key=lambda x: (x.tier, x.updated_at or ""), reverse=False):
        serialized_threads.append(
            {
                "id": t.id,
                "desktop": t.desktop,
                "native_id": t.native_id,
                "title": t.title,
                "module": t.module,
                "tier": t.tier,
                "cwd": t.cwd,
                "store_path": t.store_path,
                "updated_at": t.updated_at,
                "message_count": len(t.messages),
                "decisions_extracted": t.decisions_extracted,
                "problems_extracted": t.problems_extracted,
                "watermark": t.watermark,
            }
        )

    if current_tool and canonical_title:
        sid = current_session_id or "active-session"
        cur_id = f"{current_tool}:{sid}"
        if not any(x["id"] == cur_id for x in serialized_threads):
            serialized_threads.append(
                {
                    "id": cur_id,
                    "desktop": current_tool,
                    "native_id": sid,
                    "title": canonical_title,
                    "module": target_module or "Ogólny",
                    "tier": 1,
                    "cwd": project_info["project_root"],
                    "store_path": "active-session",
                    "updated_at": now_iso(),
                    "message_count": 1,
                    "decisions_extracted": 0,
                    "problems_extracted": 0,
                    "watermark": {},
                }
            )
        else:
            current = next(item for item in serialized_threads if item["id"] == cur_id)
            current.update(title=canonical_title, module=target_module or "Ogólny", tier=1)

    desktops_map: Dict[str, Dict[str, Any]] = {}
    for tool in DESKTOP_TOOLS:
        tool_threads = [item for item in serialized_threads if item["desktop"] == tool]
        desktops_map[tool] = {
            "present": bool(tool_threads),
            "thread_count": len(tool_threads),
            "last_thread_at": max((item["updated_at"] for item in tool_threads), default=None),
        }
    tools_with_threads = sorted(
        tool for tool, meta in desktops_map.items() if meta["present"] and tool != "agent_office"
    )

    return {
        "schema_version": SCHEMA_VERSION,
        "project_name": project_info["project_name"],
        "project_slug": project_info["project_slug"],
        "project_root": project_info["project_root"],
        "client_name": project_info["client_name"],
        "thread_prefix": project_info["project_name"],
        "updated_at": now_iso(),
        "tools_with_threads": tools_with_threads,
        "desktops": desktops_map,
        "threads": serialized_threads,
    }


def register_current_thread_title(
    canonical_title: str,
    project_info: Dict[str, str],
    current_tool: Optional[str],
    session_id: Optional[str],
    config: StoreConfig,
    register_office: bool = True,
) -> Dict[str, Any]:
    status: Dict[str, Any] = {
        "thread_title": canonical_title,
        "current_tool": current_tool or "auto",
        "session_id": session_id or "",
        "office_registered": False,
        "native_title_updated": False,
    }

    # 1. Update Codex session_index.jsonl if running inside Codex with a known session_id
    if current_tool == "codex" and session_id:
        idx_path = config.codex_root / "session_index.jsonl"
        if idx_path.exists():
            try:
                entry = {
                    "id": session_id,
                    "thread_name": canonical_title,
                    "updated_at": now_iso(),
                }
                with idx_path.open("a", encoding="utf-8") as fh:
                    fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
                status["native_title_updated"] = True
            except Exception:
                pass

    # 2. Register in Agent Office if office.cmd exists and register_office=True
    if register_office:
        office_cmd = Path.home() / ".agents" / "runtime" / "agent-office" / "bin" / "office.cmd"
        if office_cmd.exists() and current_tool and session_id:
            creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
            try:
                cmd = [
                    str(office_cmd),
                    "thread-register",
                    "--harness",
                    current_tool,
                    "--session-id",
                    session_id,
                    "--project-slug",
                    project_info["project_slug"],
                    "--title",
                    canonical_title,
                    "--cwd",
                    project_info["project_root"],
                ]
                proc = subprocess.run(
                    cmd,
                    capture_output=True,
                    text=True,
                    timeout=8,
                    creationflags=creationflags,
                )
                status["office_registered"] = proc.returncode == 0
            except Exception:
                status["office_registered"] = False

    return status


def execute_clone(
    thread_spec: str,
    explicit_project_root: Optional[str] = None,
    current_tool: Optional[str] = None,
    session_id: Optional[str] = None,
    force_rescan: bool = False,
    dry_run: bool = False,
    register_office: bool = True,
    config: Optional[StoreConfig] = None,
) -> Dict[str, Any]:
    cfg = config or StoreConfig.default()
    canonical_title, project_name, target_module = parse_thread_spec(thread_spec)

    discovered_cwds = collect_fast_cwd_hints(project_name, cfg)

    project_info = resolve_project_info(
        project_name=project_name,
        explicit_root=explicit_project_root,
        config=cfg,
        discovered_cwds=discovered_cwds,
    )
    project_root_path = Path(project_info["project_root"])
    registry_path = project_root_path / ".agents" / "desktop-threads.json"
    knowledge_path = project_root_path / "PROJECT_KNOWLEDGE.md"

    prior_watermarks: Dict[str, Dict[str, Any]] = {}
    if registry_path.exists():
        try:
            old_reg = json.loads(registry_path.read_text(encoding="utf-8", errors="replace"))
            for old_th in old_reg.get("threads", []):
                if old_th.get("id") and isinstance(old_th.get("watermark"), dict):
                    prior_watermarks[str(old_th["id"])] = old_th["watermark"]
        except Exception:
            pass

    # Full scan with resolved project_root
    all_threads: List[DiscoveredThread] = []
    all_threads.extend(
        scan_claude_threads(project_name, project_info["project_root"], target_module, cfg, prior_watermarks, force_rescan)
    )
    all_threads.extend(
        scan_codex_threads(project_name, project_info["project_root"], target_module, cfg, prior_watermarks, force_rescan)
    )
    all_threads.extend(
        scan_zcode_threads(project_name, project_info["project_root"], target_module, cfg, prior_watermarks, force_rescan)
    )
    all_threads.extend(
        scan_antigravity_threads(project_name, project_info["project_root"], target_module, cfg, prior_watermarks, force_rescan)
    )
    all_threads.extend(
        scan_office_threads(project_name, project_info["project_slug"], project_info["project_root"], target_module, cfg)
    )

    decisions, problems, modules_summary = extract_knowledge_from_threads(all_threads, target_module)
    manual_decisions, manual_problems = parse_existing_manual_rows(knowledge_path)

    registry_payload = build_desktop_threads_registry(
        project_info=project_info,
        threads=all_threads,
        current_tool=current_tool,
        current_session_id=session_id,
        canonical_title=canonical_title,
        target_module=target_module,
    )

    knowledge_md = render_project_knowledge_md(
        project_info=project_info,
        target_module=target_module,
        tools_with_threads=registry_payload["tools_with_threads"],
        threads=all_threads,
        decisions=decisions,
        problems=problems,
        modules_summary=modules_summary,
        manual_decisions=manual_decisions,
        manual_problems=manual_problems,
    )
    retained_decision_rows, retained_problem_rows = _parse_knowledge_rows(knowledge_md)
    retained_decisions = _rows_as_records(
        retained_decision_rows, ("date", "module_source", "decision", "context", "status"),
    )
    retained_problems = _rows_as_records(
        retained_problem_rows, ("date", "module_source", "symptom", "resolution"),
    )

    had_knowledge_before = knowledge_path.exists()
    had_registry_before = registry_path.exists()

    if not dry_run:
        registry_path.parent.mkdir(parents=True, exist_ok=True)
        registry_path.write_text(json.dumps(registry_payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        knowledge_path.write_text(knowledge_md, encoding="utf-8")

    reg_status = {
        "thread_title": canonical_title,
        "current_tool": current_tool or "auto",
        "session_id": session_id or "",
        "office_registered": False,
        "native_title_updated": False,
        "skipped_read_only": dry_run,
    }
    if not dry_run:
        reg_status = register_current_thread_title(
            canonical_title=canonical_title,
            project_info=project_info,
            current_tool=current_tool,
            session_id=session_id,
            config=cfg,
            register_office=register_office,
        )

    tier1_threads = [t for t in all_threads if t.tier == 1]
    tier2_threads = [t for t in all_threads if t.tier == 2]

    return {
        "status": "OK",
        "dry_run": dry_run,
        "thread_title": canonical_title,
        "project_info": project_info,
        "target_module": target_module,
        "knowledge_path": str(knowledge_path),
        "registry_path": str(registry_path),
        "created_knowledge": not had_knowledge_before and not dry_run,
        "created_registry": not had_registry_before and not dry_run,
        "tools_with_threads": registry_payload["tools_with_threads"],
        "desktops": registry_payload["desktops"],
        "tier1_count": len(tier1_threads),
        "tier2_count": len(tier2_threads),
        "total_threads": len(all_threads),
        "decisions_count": len(retained_decisions),
        "problems_count": len(retained_problems),
        "target_module_state": {
            "module": target_module,
            "last_thread_title": modules_summary[target_module]["last_thread_title"],
            "last_thread_id": modules_summary[target_module]["last_thread_id"],
            "updated_at": modules_summary[target_module]["updated_at"],
            "points": modules_summary[target_module]["points"],
            "open_topics": modules_summary[target_module]["open_topics"],
        },
        "top_decisions": [record for record in retained_decisions if record["status"] != "SUPERSEDED"][:8],
        "top_problems": retained_problems[:6],
        "registration": reg_status,
    }


def record_entry(
    project_root: str,
    module: str,
    decision: Optional[str] = None,
    context: Optional[str] = None,
    problem: Optional[str] = None,
    resolution: Optional[str] = None,
    source: Optional[str] = None,
) -> Dict[str, Any]:
    root_path = Path(project_root).resolve()
    if not root_path.is_dir():
        raise ValueError(f"--project-root must name an existing directory: {root_path}")
    knowledge_path = root_path / "PROJECT_KNOWLEDGE.md"
    if not knowledge_path.exists():
        # Initialize baseline structure first
        execute_clone(
            thread_spec=f"{root_path.name} - {module or 'Ogólny'}",
            explicit_project_root=str(root_path),
            register_office=False,
        )

    text = knowledge_path.read_text(encoding="utf-8", errors="replace")
    lines = text.splitlines()
    today = now_iso()[:10]
    src_label = f"{module} (`{source or 'local'}`) [MANUAL]"

    if decision:
        clean_dec = sanitize_text_line(decision, max_len=240)
        clean_ctx = sanitize_text_line(context or "Decyzja właściciela w trakcie pracy", max_len=160)
        new_row = f"| {today} | {src_label} | {clean_dec} | {clean_ctx} | ACTIVE |"
        inserted = False
        for i, line in enumerate(lines):
            if line.startswith("## 2."):
                # Find table separator |---|
                for j in range(i + 1, min(i + 8, len(lines))):
                    if lines[j].startswith("|---"):
                        lines.insert(j + 1, new_row)
                        inserted = True
                        break
                break
        if not inserted:
            lines.append(new_row)

    if problem:
        clean_prob = sanitize_text_line(problem, max_len=180)
        clean_res = sanitize_text_line(resolution or "Rozwiązano w bieżącym wątku", max_len=220)
        new_row = f"| {today} | {src_label} | {clean_prob} | {clean_res} |"
        inserted = False
        for i, line in enumerate(lines):
            if line.startswith("## 4."):
                for j in range(i + 1, min(i + 8, len(lines))):
                    if lines[j].startswith("|---"):
                        lines.insert(j + 1, new_row)
                        inserted = True
                        break
                break
        if not inserted:
            lines.append(new_row)

    compacted = compact_knowledge_markdown("\n".join(lines))
    knowledge_path.write_text(compacted, encoding="utf-8")
    return {
        "status": "RECORDED",
        "knowledge_path": str(knowledge_path),
        "module": module,
        "decision_recorded": bool(decision),
        "problem_recorded": bool(problem),
    }


def format_hydration_brief(result: Dict[str, Any]) -> str:
    pinfo = result["project_info"]
    mstate = result["target_module_state"]
    tools_str = ", ".join(result["tools_with_threads"]) if result["tools_with_threads"] else "brak wcześniejszych wątków"
    action = "PODGLAD BEZ ZAPISU" if result.get("dry_run") else ("UTWORZONO" if result["created_knowledge"] else "ZAKTUALIZOWANO")

    out: List[str] = [
        f"=== BRIEF KONTYNUACJI WĄTKU: {result['thread_title']} ===",
        f"Projekt: {pinfo['project_name']} ({pinfo['project_root']}) | Klient: {pinfo['client_name']} | Slug: {pinfo['project_slug']}",
        f"Docelowa grupa modułów: {result['target_module']}",
        f"Narzędzia Desktop z wątkami w projekcie: {tools_str} (Tier 1 [{result['target_module']}]: {result['tier1_count']}, Tier 2 [pozostałe moduły]: {result['tier2_count']}, łącznie: {result['total_threads']})",
        f"Pliki wiedzy: {result['knowledge_path']} ({action}) | {result['registry_path']}",
        "",
        f"--- STAN MODUŁU: {mstate['module']} ---",
        f"Ostatni wątek: {mstate['last_thread_title']} ({mstate['last_thread_id']}, akt.: {mstate['updated_at']})",
    ]
    if mstate.get("points"):
        out.append("Kluczowe ustalenia modułu:")
        for pt in mstate["points"][:5]:
            out.append(f"  - {pt}")
    if mstate.get("open_topics"):
        out.append("Ostatnie prompty / otwarte tematy:")
        for ot in mstate["open_topics"][:3]:
            out.append(f"  - {ot}")

    if result.get("top_decisions"):
        out.append("")
        out.append("--- KLUCZOWE DECYZJE WŁAŚCICIELA (PRZEKROJOWO Z PROJEKTU) ---")
        for d in result["top_decisions"][:6]:
            out.append(f"  - [{d['date']}] ({d['module_source']}): {d['decision']}")

    if result.get("top_problems"):
        out.append("")
        out.append("--- HISTORYCZNE PROBLEMY I PUŁAPKI ---")
        for p in result["top_problems"][:4]:
            out.append(f"  - [{p['date']}] ({p['module_source']}): {p['symptom']}")

    return "\n".join(out)


def main(argv: Optional[List[str]] = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8")
            sys.stderr.reconfigure(encoding="utf-8")
        except Exception:
            pass

    parser = argparse.ArgumentParser(description="Clone a thread and normalize project knowledge across Desktop tools.")
    subparsers = parser.add_subparsers(dest="subcommand")

    # clone
    p_clone = subparsers.add_parser("clone", help="Rozpoznaj projekt, zeskanuj wątki Desktop, znormalizuj wiedzę i wypisz brief.")
    p_clone.add_argument("--thread", required=True, help='Nazwa wątku w formacie "<Projekt> - <Moduł>", np. "Example CRM - Module"')
    p_clone.add_argument("--project-root", default=None, help="Jawna ścieżka katalogu projektu (opcjonalnie)")
    p_clone.add_argument("--current-tool", default=None, choices=["claude", "codex", "zcode", "antigravity", "kimi"], help="Bieżące narzędzie Desktop")
    p_clone.add_argument("--session-id", default=None, help="ID bieżącej sesji do rejestracji tytułu wątku")
    p_clone.add_argument("--force-rescan", action="store_true", help="Pomiń watermarki i przeskanuj wszystkie transkrypcje od zera")
    p_clone.add_argument("--no-office", action="store_true", help="Nie wywołuj office.cmd thread-register")
    p_clone.add_argument("--json", action="store_true", help="Wypisz wynik w formacie JSON")

    # status
    p_status = subparsers.add_parser("status", help="Read-only podgląd wątków Desktop i stanu projektu bez zapisu plików.")
    p_status.add_argument("--thread", required=True, help='Nazwa wątku w formacie "<Projekt> - <Moduł>"')
    p_status.add_argument("--project-root", default=None, help="Jawna ścieżka katalogu projektu")
    p_status.add_argument("--json", action="store_true", help="Wypisz wynik w formacie JSON")

    # record
    p_rec = subparsers.add_parser("record", help="Dopisz decyzję właściciela lub historyczną pułapkę do PROJECT_KNOWLEDGE.md.")
    p_rec.add_argument("--project-root", required=True, help="Ścieżka do katalogu projektu")
    p_rec.add_argument("--module", default="Ogólny", help="Nazwa modułu, np. Portfel")
    p_rec.add_argument("--decision", default=None, help="Treść podjętej decyzji właściciela")
    p_rec.add_argument("--context", default=None, help="Kontekst / powód decyzji")
    p_rec.add_argument("--problem", default=None, help="Opis problemu / objawu")
    p_rec.add_argument("--resolution", default=None, help="Przyczyna i rozwiązanie / czego unikać")
    p_rec.add_argument("--source", default=None, help="Źródło, np. codex:01a0f3be")
    p_rec.add_argument("--json", action="store_true", help="Wypisz wynik w formacie JSON")

    args = parser.parse_args(argv)
    if not args.subcommand:
        parser.print_help()
        return 1

    if args.subcommand in ("clone", "status"):
        dry_run = args.subcommand == "status"
        res = execute_clone(
            thread_spec=args.thread,
            explicit_project_root=args.project_root,
            current_tool=getattr(args, "current_tool", None),
            session_id=getattr(args, "session_id", None),
            force_rescan=getattr(args, "force_rescan", False),
            dry_run=dry_run,
            register_office=(not getattr(args, "no_office", False) and not dry_run),
        )
        if args.json:
            print(json.dumps(res, indent=2, ensure_ascii=False))
        else:
            print(format_hydration_brief(res))
        return 0

    if args.subcommand == "record":
        if not args.decision and not args.problem:
            print("Podaj --decision lub --problem do zapisania.", file=sys.stderr)
            return 2
        res = record_entry(
            project_root=args.project_root,
            module=args.module,
            decision=args.decision,
            context=args.context,
            problem=args.problem,
            resolution=args.resolution,
            source=args.source,
        )
        if args.json:
            print(json.dumps(res, indent=2, ensure_ascii=False))
        else:
            print(f"Zapisano wpis w {res['knowledge_path']} (moduł: {res['module']}).")
        return 0

    return 1


if __name__ == "__main__":
    raise SystemExit(main())
