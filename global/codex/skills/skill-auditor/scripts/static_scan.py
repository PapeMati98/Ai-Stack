#!/usr/bin/env python3
"""
static_scan.py - statyczny skaner bezpieczenstwa skilla/pluginu/agenta Claude Code.

Bezpieczny do uruchomienia: uzywa TYLKO stdlib (argparse, json, os, re, sys, base64,
unicodedata, pathlib). Czyta pliki READ-ONLY. Nigdy nie wykonuje, nie importuje ani nie
ewaluuje skanowanej tresci, nie laczy sie z siecia. Wzorzec: snyk/agent-scan +
simplybychris/skill-audit. Taksonomia regul zgodna z badaniem Snyk "ToxicSkills".

Uzycie:
    python static_scan.py <sciezka_do_katalogu_skilla> [--json] [--max-bytes N]

Wyjscie:
    Czytelny raport + (z --json) maszynowy JSON z findings i werdyktem.
    Exit code: 0 = ALLOW, 1 = MANUAL_REVIEW, 2 = BLOCK, 3 = blad uzycia.

Werdykt:
    BLOCK         - znaleziono >=1 finding severity critical lub >=2 high
    MANUAL_REVIEW - znaleziono >=1 high lub >=3 medium (wymaga oceny czlowieka/subagenta)
    ALLOW         - tylko low/medium ponizej progu (i tak rekomendowany przeglad semantyczny)
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import re
import stat
import sys
import unicodedata
from pathlib import Path

REPARSE_ATTR = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
WINDOWS_RESERVED_NAMES = {
    "con", "prn", "aux", "nul", "clock$",
    *{f"com{i}" for i in range(1, 10)},
    *{f"lpt{i}" for i in range(1, 10)},
    "com\u00b9", "com\u00b2", "com\u00b3",
    "lpt\u00b9", "lpt\u00b2", "lpt\u00b3",
}

# Pliki ktore w ogole skanujemy jako tekst
TEXT_EXTS = {".md", ".py", ".sh", ".bash", ".zsh", ".js", ".mjs", ".cjs", ".ts",
             ".ps1", ".psm1", ".bat", ".cmd", ".rb", ".pl", ".php", ".yaml", ".yml",
             ".json", ".toml", ".cfg", ".ini", ".txt", ".env", ".tpl", ".tmpl", ".html"}
# Rozszerzenia ktore JUZ samym istnieniem sa podejrzane w skillu
EXECUTABLE_EXTS = {".exe", ".dll", ".so", ".dylib", ".bin", ".com", ".scr",
                   ".msi", ".apk", ".jar", ".o", ".a"}
SCRIPT_EXTS = {".py", ".sh", ".bash", ".zsh", ".js", ".mjs", ".cjs", ".ts",
               ".ps1", ".psm1", ".bat", ".cmd", ".rb", ".pl", ".php"}

DEFAULT_MAX_BYTES = 2_000_000  # nie czytaj pojedynczych plikow wiekszych niz 2 MB


# (rule_id, severity, opis_pl, wzorzec_regex, flagi)
# severity: critical > high > medium > low
RULES = [
    # ---- E-klasa: krytyczne wzorce zlosliwego kodu / iniekcji ----
    ("E005-download", "critical",
     "Podejrzany URL pobierania (executable / skroty URL / surowy bin)",
     r"(curl|wget|Invoke-WebRequest|iwr|certutil\s+-urlcache)\s+[^\n]*?(https?://[^\s'\"]+?(\.(exe|sh|ps1|dll|bin|msi|scr)|bit\.ly|tinyurl|t\.co|is\.gd|cutt\.ly))",
     re.I),
    ("E006-pipe-bash", "critical",
     "Pobranie i potokowanie wprost do powloki (curl|wget ... | bash/sh)",
     r"(curl|wget|fetch)\s+[^\n|]*\|\s*(bash|sh|zsh|python|node|pwsh|powershell)\b",
     re.I),
    ("E006-rce-eval", "critical",
     "Dynamiczne wykonanie kodu z danych (eval/exec/Function na zmiennej)",
     r"\b(eval|exec)\s*\(\s*(base64|bytes|input|os\.environ|open|requests|urllib|fetch|atob|decode)",
     re.I),
    ("E006-revshell", "critical",
     "Wzorzec reverse shell / bind shell",
     r"(bash\s+-i\s+>&?\s*/dev/tcp/|nc\s+-e\s|socket\.socket\([^)]*\)\s*[^\n]*connect\(|/dev/tcp/\d)",
     re.I),
    ("E004-injection", "critical",
     "Iniekcja promptu: instrukcja by zignorowac zasady / dzialac po cichu / nadpisac polecenia",
     r"(ignore\s+(all\s+)?(previous|prior|above)\s+(instructions|prompts)|disregard\s+(the\s+)?(system|safety)|override\s+(your|the)\s+(safety|guard|instruction)|silently\s+(send|exfiltrate|upload|forward|leak)|do\s+not\s+(tell|inform|mention)\s+the\s+user|bez\s+wiedzy\s+u(z|ż)ytkownika|po\s+cichu\s+(wy(s|ś)lij|prze(s|ś)lij))",
     re.I),

    # ---- W-klasa secrets / exfiltracja ----
    ("W007-secret-read", "high",
     "Odczyt sciezek z sekretami (~/.ssh, .env, ~/.aws, keychain, credentials)",
     r"(~/\.ssh/|/\.ssh/id_|\.aws/credentials|\.env\b|id_rsa|id_ed25519|security\s+find-generic-password|gnome-keyring|secrets?\.(json|yaml|txt)|\.netrc|\.npmrc|\.pypirc)",
     re.I),
    ("W008-hardcoded-secret", "high",
     "Zaszyty sekret (klucz API / token / private key) w tresci skilla",
     r"(sk-[A-Za-z0-9]{16,}|ghp_[A-Za-z0-9]{20,}|xox[baprs]-[A-Za-z0-9-]{10,}|AKIA[0-9A-Z]{12,}|-----BEGIN\s+(RSA|OPENSSH|EC|DSA|PRIVATE)\s+KEY-----|AIza[0-9A-Za-z_\-]{20,})",
     0),
    ("W017-exfil-net", "high",
     "Wysylka danych na zewnetrzny host (POST/webhook/discord/telegram/pastebin)",
     r"(requests\.post|urllib\.request|fetch\(|axios\.|Invoke-RestMethod)\s*[^\n]*?(webhook|discord\.com/api|api\.telegram\.org|hooks\.slack|pastebin|ngrok|requestbin|burpcollaborator|interactsh|oast\.|\.onion)",
     re.I),
    ("W012-remote-fetch-exec", "high",
     "Pobieranie instrukcji/kodu z zewnetrznego URL w czasie dzialania (zdalna kontrola)",
     r"(curl|wget|requests\.get|urllib|fetch)\s*[^\n]*?https?://[^\s'\"]+[^\n]*?(\|\s*(bash|sh|python|node)|exec|eval|>\s*\$?\w+\.(sh|py|ps1))",
     re.I),

    # ---- W-klasa persistence / modyfikacja systemu ----
    ("W013-persistence", "high",
     "Persistence / modyfikacja autostartu (crontab, bashrc, launchd, rejestr Run, scheduled task)",
     r"(crontab\s+-|>>\s*~?/?\.(bashrc|zshrc|profile|bash_profile)|launchctl\s+load|/Library/LaunchAgents|reg\s+add\s+[^\n]*\\Run|schtasks\s+/create|systemctl\s+enable)",
     re.I),
    ("W013-skill-tamper", "high",
     "Modyfikacja innych skilli / configu Claude (samopropagacja, wylaczanie ochron)",
     r"(\.claude/(skills|settings|plugins)|~/\.claude/|\.claude\.json|settings\.local\.json)[^\n]*?(rm\s|>\s|cp\s|mv\s|Write|echo\s+[^\n]*>)",
     re.I),
    ("W013-config-override", "high",
     "Nadpisanie endpointu modelu (ANTHROPIC_BASE_URL / OPENAI_BASE_URL) lub wlaczenie enableAllProjectMcpServers",
     r"(?:(?:export\s+|\$env:)?\b(?:ANTHROPIC_BASE_URL|OPENAI_BASE_URL)\b\s*(?<![!=])[:=](?!=)\s*[\"']?https?://|[\"'](?:ANTHROPIC_BASE_URL|OPENAI_BASE_URL)[\"']\s*:\s*[\"']https?://|os\.environ\s*(?:\[\s*[\"'](?:ANTHROPIC_BASE_URL|OPENAI_BASE_URL)[\"']\s*\]\s*=(?!=)|\.setdefault\(\s*[\"'](?:ANTHROPIC_BASE_URL|OPENAI_BASE_URL)[\"'])|(?:[\"']enableAllProjectMcpServers[\"']\s*:\s*true\b|(?:^|[{,\n])\s*enableAllProjectMcpServers\s*(?<![!=])[:=](?!=)\s*true\b))",
     re.I | re.M),
    ("W019-destructive", "high",
     "Destrukcyjne polecenia (rm -rf /, format, del /s, dd, mkfs)",
     r"(rm\s+-rf\s+[\"']?(/|~|\$HOME)\s|rm\s+-rf\s+--no-preserve-root|\bmkfs\b|\bdd\s+if=|format\s+[a-z]:|del\s+/[sq]\s)",
     re.I),

    # ---- Obfuskacja ----
    ("W006-obfuscation", "medium",
     "Obfuskacja: duzy blok base64 / hex / \\xNN do dekodowania i wykonania",
     r"(base64\s+-d|b64decode|atob\(|FromBase64String|[A-Za-z0-9+/]{120,}={0,2}|(\\x[0-9a-fA-F]{2}){10,})",
     0),
    ("W006-char-build", "medium",
     "Skladanie polecen ze znakow (chr()/fromCharCode/String.fromCharCode) - typowa obfuskacja",
     r"(chr\(\d+\)\s*\+\s*chr\(|String\.fromCharCode\(|\bjoin\(\s*\[?\s*chr\()",
     re.I),

    # ---- Untrusted content / financial ----
    ("W011-untrusted-browse", "medium",
     "Przetwarzanie niezaufanej tresci z dowolnych URL (ryzyko posredniej iniekcji)",
     r"(browse|scrape|fetch|read)\s+[^\n]*?(arbitrary|any|user-?provided|unknown)\s+url",
     re.I),
    ("W009-financial", "medium",
     "Bezposrednie operacje finansowe (przelew, platnosc, wyplata)",
     r"\b(transfer\s+(funds|money)|send\s+payment|withdraw|wire\s+transfer|execute\s+(trade|order)|/payments?/|stripe\.(charges|transfers)|przele[wz]|wyp(l|ł)at)",
     re.I),
]

# Frontmatter allowed-tools: ktore uprawnienia sa "szerokie"/ryzykowne
BROAD_TOOL_PATTERNS = [
    (r"Bash\s*$", "high", "allowed-tools: Bash bez ograniczenia komend (pelny dostep do powloki)"),
    (r"Bash\(\s*\*\s*\)", "high", "allowed-tools: Bash(*) - dowolna komenda"),
    (r"Bash\((curl|wget|nc|ncat|bash|sh|python|node|powershell|pwsh|eval)", "high",
     "allowed-tools: Bash z komenda sieciowa/wykonawcza"),
    (r"\bWrite\b", "low", "allowed-tools: Write (zapis plikow) - sprawdz gdzie pisze"),
    (r"WebFetch|WebSearch", "low", "allowed-tools: dostep do sieci (WebFetch/WebSearch)"),
]

SEV_ORDER = {"critical": 4, "high": 3, "medium": 2, "low": 1}


def check_windows_path_component(part: str) -> str | None:
    """Sprawdza pojedynczy komponent sciezki wzgledem pulapek Windows/NTFS przed odczytem."""
    if not part or part == ".":
        return None
    if part == "..":
        return "Komponent nadrzedny '..' w sciezce skilla"
    if "\x00" in part or any(ord(ch) < 32 for ch in part):
        return "Znak sterujacy lub NUL w nazwie pliku/katalogu"
    if ":" in part:
        return f"Strumien alternatywny NTFS ADS lub dwukropek w nazwie komponentu ({part!r})"
    if part.endswith((".", " ")):
        return f"Komponent sciezki konczy sie kropka lub spacja ({part!r}) - ryzyko ominiecia kanonizacji Windows"
    trimmed = part.rstrip(". ")
    stem = trimmed.split(".", 1)[0].casefold()
    if stem in WINDOWS_RESERVED_NAMES:
        return f"Zarezerwowana nazwa urzadzenia Windows ({part!r})"
    return None


def _is_reparse_or_symlink(st: os.stat_result) -> bool:
    return bool(stat.S_ISLNK(st.st_mode) or (getattr(st, "st_file_attributes", 0) & REPARSE_ATTR))


def _raw_root_parts(raw_root: str) -> list[str]:
    text = raw_root.replace("/", "\\")
    if text.startswith("\\\\"):
        return [p for p in text.split("\\") if p]
    if len(text) >= 2 and text[1] == ":":
        text = text[2:]
    return [p for p in text.split("\\") if p and p != "."]


def _component_prefixes(path: Path) -> list[Path]:
    """Prefiksy od pierwszego komponentu do pelnej sciezki, bez normalizacji przez filesystem."""
    parts = path.parts
    if len(parts) <= 1:
        return [path] if parts else []
    prefixes = []
    acc = Path(parts[0])
    prefixes.append(acc)
    for part in parts[1:]:
        acc = acc / part
        prefixes.append(acc)
    return prefixes


def _intermediate_reparse(path: Path) -> bool:
    """Czy posredni komponent jest symlinkiem albo punktem reparse.

    os.lstat na pelnej sciezce nie podaza za ostatnim komponentem, ale podaza
    za wczesniejszymi. Dlatego kazdy prefiks poza ostatnim jest sprawdzany
    wlasnym lstat zanim ktokolwiek dotknie konca sciezki.
    """
    prefixes = _component_prefixes(path)
    for prefix in prefixes[:-1]:
        if check_windows_path_component(prefix.name):
            return False
        try:
            st = os.lstat(prefix)
        except OSError:
            return False
        if _is_reparse_or_symlink(st):
            return True
    return False


def read_text(p: Path, max_bytes: int, known_size: int | None = None) -> str | None:
    try:
        size = known_size if known_size is not None else p.stat().st_size
        if size > max_bytes:
            return None
        return p.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return None


def line_of(text: str, idx: int) -> int:
    return text.count("\n", 0, idx) + 1


def scan_hidden_unicode(text: str, relpath: str):
    """W021 - niewidzialne znaki Unicode (Format/Cf lub Control/Cc poza zwyklymi)."""
    findings = []
    allowed_cc = {"\t", "\n", "\r"}
    hidden_types = {}
    decoded_tag = None
    tag_chars = []
    for i, ch in enumerate(text):
        cat = unicodedata.category(ch)
        if cat == "Cf" or (cat == "Cc" and ch not in allowed_cc):
            name = unicodedata.name(ch, f"U+{ord(ch):04X}")
            hidden_types.setdefault(name, 0)
            hidden_types[name] += 1
        # Unicode Tags block (E0000-E007F) - kanal ukrytych wiadomosci
        if 0xE0000 <= ord(ch) <= 0xE007F:
            tag_chars.append(chr(ord(ch) - 0xE0000) if 0x20 <= ord(ch) - 0xE0000 < 0x7F else "")
    if tag_chars:
        decoded_tag = "".join(tag_chars).strip()
    if hidden_types:
        distinct = len(hidden_types)
        total = sum(hidden_types.values())
        sev = "high" if (distinct >= 3 or decoded_tag) else "medium"
        msg = f"Ukryte znaki Unicode ({total} szt., {distinct} typow: {', '.join(list(hidden_types)[:4])})"
        if decoded_tag:
            msg += f" | zdekodowana ukryta wiadomosc TAG: {decoded_tag[:80]!r}"
        findings.append(("W021-hidden-unicode", sev, msg, relpath, 0))
    return findings


def parse_frontmatter(text: str) -> str | None:
    """Zwraca surowy blok frontmatter (miedzy --- ... ---) jezeli istnieje."""
    m = re.match(r"^﻿?---\s*\n(.*?)\n---\s*\n", text, re.S)
    return m.group(1) if m else None


def scan_skill_md(p: Path, text: str):
    findings = []
    fm = parse_frontmatter(text)
    if fm is None:
        return findings  # brak frontmatter zglaszany osobno (W014)
    # allowed-tools - szukaj szerokich uprawnien
    if re.search(r"allowed-tools", fm, re.I):
        tools_block = fm
        for pat, sev, msg in BROAD_TOOL_PATTERNS:
            if re.search(pat, tools_block, re.M):
                findings.append(("W-allowed-tools", sev, msg, p.name, 0))
    return findings


def _walk_safe(root: Path, raw_root: str | None = None):
    """Bezpieczne przejscie po drzewie bez podazania za symlinkami/reparse i bez otwierania urzadzen Windows."""
    findings = []
    files = []
    raw_text = raw_root if raw_root is not None else str(root)
    for part in _raw_root_parts(raw_text):
        hazard = check_windows_path_component(part)
        if hazard:
            findings.append(("W016-windows-path-hazard", "high", hazard, raw_text, 0))
            return files, findings

    if _intermediate_reparse(root):
        findings.append((
            "W015-symlink-reparse", "high",
            "Posredni komponent sciezki skilla jest dowiazaniem symbolicznym lub punktem reparse - pominieto podazanie poza drzewo",
            str(root), 0
        ))
        return files, findings

    try:
        root_st = os.lstat(root)
    except OSError:
        findings.append(("W-unreadable", "low", "Katalog skilla nieczytelny - przejrzyj recznie", str(root), 0))
        return files, findings

    if _is_reparse_or_symlink(root_st):
        findings.append((
            "W015-symlink-reparse", "high",
            "Katalog glowny skilla jest dowiazaniem symbolicznym lub punktem reparse - pominieto podazanie poza drzewo",
            str(root), 0
        ))
        return files, findings

    stack = [(root, "")]
    while stack:
        current_dir, rel_prefix = stack.pop()
        try:
            entries = sorted(os.scandir(current_dir), key=lambda e: e.name.casefold())
        except OSError:
            findings.append((
                "W-unreadable", "low",
                "Podkatalog nieczytelny - przejrzyj recznie",
                rel_prefix or str(current_dir), 0
            ))
            continue

        dirs_to_push = []
        for entry in entries:
            name = entry.name
            if name == ".git":
                continue
            rel = f"{rel_prefix}{os.sep}{name}" if rel_prefix else name

            # 1. Walidacja leksykalna surowego komponentu PRZED lstat/stat/open
            hazard = check_windows_path_component(name)
            if hazard:
                findings.append(("W016-windows-path-hazard", "high", hazard, rel, 0))
                continue

            # 2. Sprawdzenie lstat bez podazania za dowiazaniem/junction
            try:
                st = os.lstat(entry.path)
            except OSError:
                findings.append(("W-unreadable", "low", "Element nieczytelny - przejrzyj recznie", rel, 0))
                continue

            if _is_reparse_or_symlink(st):
                findings.append((
                    "W015-symlink-reparse", "high",
                    "Dowiazanie symboliczne lub punkt reparse (symlink/junction) w paczce skilla - pominieto podazanie poza drzewo",
                    rel, 0
                ))
                continue

            if stat.S_ISDIR(st.st_mode):
                dirs_to_push.append((Path(entry.path), rel))
            elif stat.S_ISREG(st.st_mode):
                files.append((Path(entry.path), rel, st.st_size))
            else:
                findings.append((
                    "W016-windows-path-hazard", "high",
                    "Nieregularny plik systemowy/urzadzenie w paczce skilla",
                    rel, 0
                ))

        for item in reversed(dirs_to_push):
            stack.append(item)

    return files, findings


def scan(root: Path, max_bytes: int, raw_root: str | None = None):
    files, findings = _walk_safe(root, raw_root=raw_root)

    skill_md_found = any(p.name.lower() == "skill.md" for p, _, _ in files)
    if not skill_md_found:
        findings.append(("W014-no-skillmd", "low",
                         "Brak pliku SKILL.md - utrudniona analiza, traktuj nieufnie", str(root), 0))

    file_inventory = []
    seen_rule_hits = set()
    for p, rel, size in files:
        ext = p.suffix.lower()
        file_inventory.append(rel)

        # E007 - binaria/wykonywalne w skillu = czerwona flaga sama w sobie
        if ext in EXECUTABLE_EXTS:
            findings.append(("E007-binary", "critical",
                             f"Plik wykonywalny/binarny w skillu ({ext}) - skill nie powinien dostarczac binariow",
                             rel, 0))
            continue

        if ext not in TEXT_EXTS:
            continue
        text = read_text(p, max_bytes, known_size=size)
        if text is None:
            findings.append(("W-unreadable", "low",
                             "Plik nieczytelny jako tekst lub > limitu - przejrzyj recznie", rel, 0))
            continue

        # ukryte unicode
        findings.extend(scan_hidden_unicode(text, rel))

        # reguly tresci
        for rid, sev, desc, pat, flags in RULES:
            for m in re.finditer(pat, text, flags):
                ln = line_of(text, m.start())
                hit_key = (rid, rel, ln)
                if hit_key not in seen_rule_hits:
                    seen_rule_hits.add(hit_key)
                    findings.append((rid, sev, desc, rel, ln))
                break  # jeden trafienie na regule/plik wystarczy do flagi

        # frontmatter allowed-tools tylko dla SKILL.md
        if p.name.lower() == "skill.md":
            findings.extend(scan_skill_md(p, text))

    return findings, file_inventory


def verdict_of(findings):
    crit = sum(1 for f in findings if f[1] == "critical")
    high = sum(1 for f in findings if f[1] == "high")
    med = sum(1 for f in findings if f[1] == "medium")
    if crit >= 1 or high >= 2:
        return "BLOCK", 2
    if high >= 1 or med >= 3:
        return "MANUAL_REVIEW", 1
    return "ALLOW", 0


def main():
    ap = argparse.ArgumentParser(description="Statyczny audyt skilla Claude Code (read-only, stdlib).")
    ap.add_argument("path", help="Katalog ze skillem (zawiera SKILL.md)")
    ap.add_argument("--json", action="store_true", help="Wyjscie JSON")
    ap.add_argument("--max-bytes", type=int, default=DEFAULT_MAX_BYTES)
    args = ap.parse_args()

    raw_arg = args.path
    root = Path(raw_arg)
    has_root_hazard = any(check_windows_path_component(part) for part in _raw_root_parts(raw_arg))
    if not has_root_hazard and not _intermediate_reparse(root):
        try:
            st = os.lstat(root)
        except OSError:
            print(f"BLAD: sciezka nie istnieje: {root}", file=sys.stderr)
            sys.exit(3)
        if not _is_reparse_or_symlink(st) and stat.S_ISREG(st.st_mode):
            root = root.parent
            raw_arg = str(root)

    findings, inventory = scan(root, args.max_bytes, raw_root=raw_arg)
    findings.sort(key=lambda f: (-SEV_ORDER.get(f[1], 0), f[0]))
    verdict, code = verdict_of(findings)

    counts = {s: sum(1 for f in findings if f[1] == s) for s in ("critical", "high", "medium", "low")}

    if args.json:
        print(json.dumps({
            "path": str(root),
            "verdict": verdict,
            "counts": counts,
            "file_count": len(inventory),
            "findings": [
                {"rule": r, "severity": s, "desc": d, "file": f, "line": ln}
                for (r, s, d, f, ln) in findings
            ],
            "inventory": inventory,
        }, ensure_ascii=False, indent=2))
    else:
        print("=" * 70)
        print(f"AUDYT STATYCZNY: {root}")
        print(f"Plikow: {len(inventory)} | krytyczne={counts['critical']} "
              f"wysokie={counts['high']} srednie={counts['medium']} niskie={counts['low']}")
        print("=" * 70)
        if not findings:
            print("Brak trafien heurystyk. (Nadal zalecany przeglad semantyczny subagentem.)")
        for (r, s, d, f, ln) in findings:
            loc = f"{f}:{ln}" if ln else f
            print(f"[{s.upper():8}] {r:22} {d}")
            print(f"            -> {loc}")
        print("-" * 70)
        print(f"WERDYKT: {verdict}")
        if verdict == "BLOCK":
            print("  Twarda blokada - NIE instaluj. Wymaga jawnej decyzji czlowieka po analizie.")
        elif verdict == "MANUAL_REVIEW":
            print("  Wymaga przegladu semantycznego (subagent read-only) + decyzji czlowieka.")
        else:
            print("  Brak twardych flag. Przejdz do warstwy semantycznej, potem decyzja.")

    sys.exit(code)


if __name__ == "__main__":
    main()
