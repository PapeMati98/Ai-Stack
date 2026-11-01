"""Read-only frontmatter inventory and lexical shortlist for local skills."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import unicodedata
from pathlib import Path
from typing import Iterable, Iterator

SCHEMA_VERSION = 1
CATALOG_PARSER_VERSION = 2
CACHE_VERSION = 1
CANDIDATE_LIMIT = 8
MAX_FINAL_SKILLS = 4
FRONTMATTER_FIELDS = {"name", "description", "aliases", "triggers", "domains", "role", "tags"}
TOKEN_RE = re.compile(r"[^\w-]+", re.UNICODE)

# Bilingual function words and routing-generic verbs/nouns. They carry no
# domain signal, so they are dropped from both the task and the skill side
# before scoring. This is a lexicon of generic words, not a table of skills.
STOPWORDS = frozenset({
    # Polish function words.
    "a", "albo", "ani", "az", "bardzo", "bez", "byc", "co", "czy", "dla", "do", "go",
    "gdy", "i", "ja", "jak", "jest", "juz", "ktora", "ktore", "ktory", "lub", "mi",
    "miedzy", "mnie", "na", "nad", "nie", "o", "od", "oraz", "po", "pod", "prosze",
    "przed", "przez", "przy", "sa", "sie", "ta", "tak", "te", "tego", "ten", "to",
    "tylko", "u", "w", "we", "z", "za", "ze",
    # English function words.
    "an", "and", "are", "as", "at", "be", "before", "after", "by", "for", "from", "in", "is", "it",
    "its", "me", "my", "no", "not", "of", "on", "or", "our", "please", "so", "that",
    "the", "their", "this", "to", "we", "with", "you", "your",
    "current", "how", "what", "when", "which", "who", "why",
    # Generic task verbs and nouns shared by nearly every request.
    "napisz", "ocen", "oszacuj", "pokaz", "przygotuj", "sprawdz", "wykonaj", "zrob", "znajdz",
    "assess", "check", "find", "make", "prepare", "show", "write",
    # Other frequent words that carry no domain signal in a routing request.
    "ile", "new", "teraz", "tej", "zadan",
    "task", "tasks", "zadanie", "zadania",
    # Generic scope and status words appear in almost any request about a
    # project; on their own they must not surface orchestration skills.
    "projekt", "projektu", "projekcie", "projektem", "projekty", "projektow",
    "project", "projects", "status", "statusu",
})

# Compact bilingual concept groups for query expansion. Each group collects
# Polish and English surface forms of one domain concept so a natural request
# in either language can reach skills described in the other. Synonym hits
# score below exact token hits. These are generic domain terms, deliberately
# not a per-skill mapping.
SYNONYM_GROUPS = (
    frozenset({
        "ui", "ux", "interfejs", "interface", "frontend", "ekran", "screen", "widok", "view",
        "makieta", "mockup", "layout", "strona", "page", "komponent", "component", "dashboard",
        "formularz", "form", "graficzny", "graficznego", "graphic", "graphics",
        "responsywny", "responsywnosc", "responsive", "wizualny", "visual",
    }),
    frozenset({
        "incydent", "incident", "awaria", "awarie", "outage", "crash", "padl", "padla", "padlo",
        "pada", "down", "broke", "broken", "fail", "fails", "failing", "failure",
        "wysypala", "wysypuje", "sypie", "przyczyna", "przyczyne", "cause",
        "triage", "diagnoza", "diagnozowac", "zdiagnozuj", "blad", "bledy", "bledu", "bledem",
        "error", "errors", "bug", "napraw", "naprawic", "naprawiaj", "fix", "debug", "debugging",
        "reprodukuj", "reproduce",
    }),
    frozenset({
        "deploy", "wdrozenie", "wdrozenia", "wdrozeniu", "wdrozeniem", "release", "wydanie",
        "wydaniem", "produkcja", "produkcje", "production", "rollout",
    }),
    frozenset({
        "wplyw", "wplywu", "impact", "zmiana", "zmiany", "zmian", "change", "konsekwencje",
        "consequences", "skutki", "effects", "migracja", "migracji", "migration", "przebudowa",
        "rename", "break", "breaks", "breaking", "zepsuje", "zepsuc", "zmienie", "zmienic",
    }),
    frozenset({
        "test", "tests", "testy", "testow", "testu", "testing", "regresja", "regresji",
        "regression", "pokrycie", "coverage",
    }),
    frozenset({
        "schemat", "schema", "schematu", "schematow", "schematy", "tabela", "table", "tabeli",
        "tabele", "kolumna", "column", "kolumny", "baza", "database",
    }),
)
SYNONYM_INDEX: dict[str, frozenset[str]] = {}
for _group in SYNONYM_GROUPS:
    for _token in _group:
        SYNONYM_INDEX[_token] = _group


def _synonym_group_for(token: str) -> frozenset[str] | None:
    """Resolve a task token to its concept group.

    Exact index hits win. Otherwise a cautious inflection fallback maps the
    token to the group of the index key sharing the longest common prefix
    (at least five characters), so forms like "wydaniu" or "zmienie" reach
    the same group as "wydanie" and "zmiana". Ties break lexicographically
    over sorted keys, keeping the result deterministic.
    """
    group = SYNONYM_INDEX.get(token)
    if group is not None:
        return group
    if len(token) < 5:
        return None
    best_key: str | None = None
    best_prefix = 0
    for key in sorted(SYNONYM_INDEX):
        if len(key) < 5:
            continue
        prefix = len(os.path.commonprefix((token, key)))
        if prefix >= 5 and (prefix > best_prefix or (prefix == best_prefix and (best_key is None or key < best_key))):
            best_key = key
            best_prefix = prefix
    return SYNONYM_INDEX[best_key] if best_key is not None else None


def all_runtime_roots() -> list[Path]:
    home = Path.home()
    roots = [home / ".agents" / "skills", home / ".codex" / "skills", home / ".codex" / "skills" / ".system", home / ".claude" / "skills"]
    roots.extend([home / ".gemini" / "config" / "skills", home / ".grok" / "skills"])
    appdata = Path(os.environ.get("APPDATA", home / "AppData" / "Roaming"))
    roots.append(appdata / "kimi-desktop" / "daimon-share" / "daimon" / "skills")
    return roots


def active_runtime_roots() -> list[Path]:
    installed_root = Path(__file__).resolve().parents[2]
    active = Path(os.environ.get("SKILL_ACTIVE_ROOT", installed_root))
    roots = [active]
    system = active / ".system"
    if system.is_dir():
        roots.append(system)
    return roots


def _root_skill_files(root: Path) -> Iterator[Path]:
    if not root.is_dir():
        return
    direct = root / "SKILL.md"
    if direct.is_file():
        yield direct
        return
    skills_dir = root / "skills"
    if skills_dir.is_dir():
        root = skills_dir
    try:
        children = sorted(root.iterdir(), key=lambda p: p.as_posix().casefold())
    except OSError:
        return
    for child in children:
        if child.is_dir():
            candidate = child / "SKILL.md"
            if candidate.is_file():
                yield candidate


def discover_skill_files(roots: Iterable[Path]) -> tuple[list[Path], list[dict[str, str]]]:
    files: list[Path] = []
    errors: list[dict[str, str]] = []
    seen: set[str] = set()
    for raw_root in roots:
        root = Path(raw_root).expanduser()
        if not root.exists():
            errors.append({"path": str(root), "error": "root_not_found"})
            continue
        if not root.is_dir():
            errors.append({"path": str(root), "error": "root_not_directory"})
            continue
        try:
            candidates = _root_skill_files(root)
            for path in candidates:
                key = os.path.normcase(str(path.resolve()))
                if key not in seen:
                    seen.add(key)
                    files.append(path)
        except OSError as exc:
            errors.append({"path": str(root), "error": f"root_unreadable:{exc.__class__.__name__}"})
    return sorted(files, key=lambda p: p.as_posix().casefold()), errors


BLOCK_SCALAR_HEADER_RE = re.compile(r"^[|>]([+-]?\d+|\d+[+-]?|[+-])?\s*(#.*)?$")


def _strip_inline_comment(value: str) -> str:
    """Strip a trailing YAML comment from an unquoted scalar."""
    stripped = value.strip()
    if not stripped or stripped[0] in "\"'":
        return value
    if stripped.startswith("["):
        return value
    marker = stripped.find(" #")
    if marker != -1:
        return stripped[:marker].rstrip()
    if stripped.startswith("#"):
        return ""
    return value


def _parse_scalar(value: str) -> object:
    value = _strip_inline_comment(value.strip())
    if not value:
        return ""
    if value.startswith("[") and value.endswith("]"):
        return [_parse_scalar(item) for item in value[1:-1].split(",") if item.strip()]
    if (value.startswith("\"") and value.endswith("\"")) or (value.startswith("'") and value.endswith("'")):
        return value[1:-1]
    return value


def read_frontmatter(path: Path, max_bytes: int | None = None) -> tuple[dict[str, object], str]:
    """Read only frontmatter bytes; the file body is never requested."""
    raw_lines: list[bytes] = []
    with path.open("rb") as stream:
        first = stream.readline(max_bytes + 1) if max_bytes is not None else stream.readline()
        if first.startswith(b"\xef\xbb\xbf"):
            first = first[3:]
        size = len(first)
        if max_bytes is not None and size > max_bytes:
            raise ValueError("frontmatter_too_large")
        if first.rstrip(b"\r\n") != b"---":
            raise ValueError("frontmatter_missing")
        raw_lines.append(first)
        while True:
            remaining = max_bytes - size if max_bytes is not None else None
            line = stream.readline(remaining + 1) if remaining is not None else stream.readline()
            if not line:
                raise ValueError("frontmatter_unclosed")
            size += len(line)
            if max_bytes is not None and size > max_bytes:
                raise ValueError("frontmatter_too_large")
            raw_lines.append(line)
            if line.rstrip(b"\r\n") == b"---":
                break
    text = b"".join(raw_lines).decode("utf-8-sig")
    data: dict[str, object] = {}
    current_key: str | None = None
    lines = text.splitlines()[1:-1]
    index = 0
    while index < len(lines):
        line = lines[index]
        if not line.strip() or line.lstrip().startswith("#"):
            index += 1
            continue
        if line[:1].isspace() and current_key and isinstance(data.get(current_key), list):
            item = line.strip()
            if item.startswith("-"):
                data[current_key].append(_parse_scalar(item[1:]))  # type: ignore[union-attr]
            index += 1
            continue
        if ":" not in line:
            index += 1
            continue
        key, value = line.split(":", 1)
        key = key.strip()
        if key not in FRONTMATTER_FIELDS:
            current_key = None
            index += 1
            continue
        value = value.strip()
        if BLOCK_SCALAR_HEADER_RE.match(value):
            style = value[0]
            block: list[str] = []
            index += 1
            while index < len(lines) and (lines[index].startswith(" ") or lines[index].startswith("\t")):
                block.append(lines[index].strip())
                index += 1
            # Drop trailing blank lines the same way chomping indicators
            # ("-", "+", none) affect only the final newline, which is
            # stripped from the catalog description in every case.
            while block and not block[-1]:
                block.pop()
            if style == "|":
                data[key] = "\n".join(block).strip()
            else:
                # Folded scalar: single line breaks become spaces, blank
                # lines become paragraph breaks.
                paragraphs: list[str] = []
                current: list[str] = []
                for entry in block:
                    if entry:
                        current.append(entry)
                    elif current:
                        paragraphs.append(" ".join(current))
                        current = []
                if current:
                    paragraphs.append(" ".join(current))
                data[key] = "\n\n".join(paragraphs).strip()
            current_key = key
            continue
        if not value:
            data[key] = []
            current_key = key
        else:
            quote = value[0] if value[0] in "\"'" else ""
            if quote and not (len(value) >= 2 and value.endswith(quote)):
                # Quoted scalar spanning several lines: fold continuation
                # lines with spaces until the closing quote is found.
                parts = [value]
                index += 1
                while index < len(lines):
                    parts.append(lines[index].strip())
                    closing = lines[index].strip().endswith(quote)
                    index += 1
                    if closing:
                        break
                data[key] = _parse_scalar(" ".join(parts))
                current_key = key
                continue
            data[key] = _parse_scalar(value)
            current_key = key
        index += 1
    return data, hashlib.sha256(b"".join(raw_lines)).hexdigest()


def _as_strings(value: object) -> list[str]:
    if isinstance(value, list):
        return [str(item) for item in value]
    return [str(value)] if value else []


def _tokens(value: str) -> set[str]:
    normalized = unicodedata.normalize("NFKD", value.casefold())
    normalized = "".join(character for character in normalized if not unicodedata.combining(character))
    # NFKD does not decompose the Polish ł, so fold it explicitly; the same
    # normalization applies to both the task and the skill side.
    normalized = normalized.replace("ł", "l")
    # Single-character fragments (for example English contraction leftovers
    # like the "s" in "night's") carry no routing signal.
    return {token for token in TOKEN_RE.split(normalized) if len(token) > 1}


def _resolved_root_strings(roots: Iterable[Path]) -> list[str]:
    return [str(Path(root).expanduser().resolve()) for root in roots]


def _cache_entries_for(files: list[Path], cached: dict[str, dict[str, object]]) -> tuple[dict[str, dict[str, object]], bool]:
    """Split entries whose cached metadata matches the current bounded header."""
    hits: dict[str, dict[str, object]] = {}
    usable = isinstance(cached, dict)
    if not usable:
        return {}, False
    for path in files:
        key = str(path.resolve())
        entry = cached.get(key)
        if not isinstance(entry, dict):
            continue
        try:
            stat = path.stat()
        except OSError:
            continue
        if entry.get("size") == stat.st_size and entry.get("mtime_ns") == stat.st_mtime_ns:
            # Restored timestamps and same-size edits must not leave stale metadata.
            # Verify both the header hash and parsed fields. A valid JSON cache is
            # still untrusted and must not be able to hide skills or conflicts.
            try:
                frontmatter, digest = read_frontmatter(path, max_bytes=65536)
                current_fields = {key: frontmatter[key] for key in FRONTMATTER_FIELDS if key in frontmatter}
                if (digest == entry.get("frontmatter_hash")
                        and current_fields == entry.get("frontmatter")):
                    hits[key] = entry
            except (OSError, UnicodeError, ValueError):
                continue
    return hits, True


def load_cache_file(cache_path: Path, roots: Iterable[Path], all_runtimes: bool) -> dict[str, dict[str, object]] | None:
    """Load cached frontmatter records or return None to scan from disk.

    Any corrupt, unreadable, or stale cache falls back to a full scan
    without stopping work. The cache never stores task text.
    """
    try:
        payload = json.loads(cache_path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, ValueError):
        return None
    if not isinstance(payload, dict):
        return None
    if payload.get("cache_version") != CACHE_VERSION:
        return None
    if payload.get("parser_version") != CATALOG_PARSER_VERSION:
        return None
    if payload.get("all_runtimes") is not all_runtimes:
        return None
    expected_roots = _resolved_root_strings(roots)
    if not isinstance(payload.get("roots"), list) or [str(item) for item in payload["roots"]] != expected_roots:  # type: ignore[union-attr]
        return None
    entries = payload.get("entries")
    if not isinstance(entries, dict):
        return None
    cleaned: dict[str, dict[str, object]] = {}
    for key, entry in entries.items():
        if isinstance(entry, dict) and isinstance(entry.get("frontmatter"), dict):
            cleaned[str(key)] = entry
    return cleaned


def save_cache_file(cache_path: Path, roots: Iterable[Path], all_runtimes: bool,
                     entries: dict[str, dict[str, object]]) -> None:
    """Persist metadata-only catalog records; task text is never stored."""
    try:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "cache_version": CACHE_VERSION,
            "parser_version": CATALOG_PARSER_VERSION,
            "schema_version": SCHEMA_VERSION,
            "all_runtimes": all_runtimes,
            "roots": _resolved_root_strings(roots),
            "entries": entries,
        }
        cache_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    except (OSError, UnicodeError, ValueError):
        pass


def build_inventory(roots: Iterable[Path], task: str | None = None,
                    cache_file: Path | str | None = None, all_runtimes: bool = False) -> dict[str, object]:
    roots = list(roots)
    files, errors = discover_skill_files(roots)
    cached: dict[str, dict[str, object]] = {}
    if cache_file is not None:
        loaded = load_cache_file(Path(cache_file), roots, all_runtimes)
        if loaded is not None:
            cached, _ = _cache_entries_for(files, loaded)
            cached_full = loaded
        else:
            cached_full = {}
    else:
        cached_full = {}
    records: dict[tuple[str, str], dict[str, object]] = {}
    by_name: dict[str, list[dict[str, object]]] = {}
    fresh_entries: dict[str, dict[str, object]] = dict(cached_full)
    for path in files:
        path_key = str(path.resolve())
        try:
            hit = cached.get(path_key)
            if hit is not None:
                frontmatter = hit["frontmatter"]
                if not isinstance(frontmatter, dict):
                    raise ValueError("cache_entry_invalid")
                digest = str(hit.get("frontmatter_hash", ""))
                if not digest:
                    raise ValueError("cache_entry_invalid")
                name = str(frontmatter.get("name", "")).strip()
                if not name:
                    raise ValueError("name_missing")
            else:
                frontmatter, digest = read_frontmatter(path)
                name = str(frontmatter.get("name", "")).strip()
                if not name:
                    raise ValueError("name_missing")
                if cache_file is not None:
                    try:
                        stat = path.stat()
                        fresh_entries[path_key] = {
                            "frontmatter": {key: frontmatter[key] for key in FRONTMATTER_FIELDS if key in frontmatter},
                            "frontmatter_hash": digest,
                            "size": stat.st_size,
                            "mtime_ns": stat.st_mtime_ns,
                        }
                    except OSError:
                        pass
            record = {"name": name, "description": str(frontmatter.get("description", "")),
                      "metadata": {key: frontmatter[key] for key in FRONTMATTER_FIELDS if key in frontmatter},
                      "path": str(path.resolve()), "frontmatter_hash": digest}
            key = (name, digest)
            records.setdefault(key, record)
            by_name.setdefault(name, []).append(record)
        except (OSError, UnicodeError, ValueError) as exc:
            errors.append({"path": str(path), "error": str(exc)})
            if cache_file is not None and path_key in fresh_entries:
                # A modified file that no longer parses must not linger.
                fresh_entries.pop(path_key, None)
    if cache_file is not None:
        # Drop entries for removed skills so additions/removals invalidate.
        live = {str(path.resolve()) for path in files}
        for stale in [key for key in fresh_entries if key not in live]:
            fresh_entries.pop(stale, None)
        save_cache_file(Path(cache_file), roots, all_runtimes, fresh_entries)
    skills = sorted(records.values(), key=lambda r: (str(r["name"]).casefold(), str(r["path"]).casefold()))
    conflicts = []
    for name, named in sorted(by_name.items(), key=lambda item: item[0].casefold()):
        hashes = sorted({str(item["frontmatter_hash"]) for item in named})
        if len(hashes) > 1:
            conflicts.append({"name": name, "hashes": hashes, "paths": sorted(str(item["path"]) for item in named)})
    result: dict[str, object] = {"schema_version": SCHEMA_VERSION, "roots": [str(Path(root).expanduser().resolve()) for root in roots],
                                 "skills": skills, "conflicts": conflicts, "errors": sorted(errors, key=lambda e: (e["path"], e["error"]))}
    if task is not None:
        result["shortlist"] = shortlist(skills, task, conflicts)
    return result


def shortlist(skills: list[dict[str, object]], task: str, conflicts: list[dict[str, object]] | None = None) -> list[dict[str, object]]:
    blocked = {str(item["name"]) for item in (conflicts or [])}
    task_tokens = {token for token in _tokens(task) if token not in STOPWORDS}
    indexed: list[tuple[dict[str, object], set[str], set[str]]] = []
    for skill in skills:
        name = str(skill["name"])
        if name in blocked:
            continue
        metadata = skill.get("metadata", {})
        # Hyphenated names are also indexed as their parts, so a task word
        # like "debugging" can reach the skill named "systematic-debugging".
        haystack = [name, name.replace("-", " "), str(skill.get("description", ""))]
        if isinstance(metadata, dict):
            for key in ("aliases", "triggers", "domains", "role", "tags"):
                haystack.extend(_as_strings(metadata.get(key)))
        skill_tokens = {token for text in haystack for token in _tokens(text) if token not in STOPWORDS}
        name_tokens = {token for token in _tokens(name) | _tokens(name.replace("-", " ")) if token not in STOPWORDS}
        indexed.append((skill, name_tokens, skill_tokens))

    document_frequency: dict[str, int] = {}
    for _, _, skill_tokens in indexed:
        for token in skill_tokens:
            document_frequency[token] = document_frequency.get(token, 0) + 1

    document_count = max(len(indexed), 1)

    def idf(token: str) -> float:
        return math.log((document_count + 1) / (document_frequency.get(token, 0) + 1)) + 1.0

    scored: list[tuple[float, dict[str, object], list[str]]] = []
    for skill, name_tokens, skill_tokens in indexed:
        exact = task_tokens & skill_tokens
        # One task word yields at most one synonym hit per skill: the rarest
        # matched token. Distinct task words still contribute one hit each.
        # Without the per-word cap a skill whose description repeats one
        # concept in many forms (for example UI vocabulary) would outscore
        # the genuinely relevant skill on a single vague task word.
        synonym_hits: list[str] = []
        for task_token in sorted(task_tokens - exact):
            group = _synonym_group_for(task_token)
            if not group:
                continue
            hits = (group & skill_tokens) - exact
            if hits:
                # Rarest token wins; on a tie prefer a name token, because the
                # name is the most deliberate part of skill metadata.
                synonym_hits.append(
                    min(hits, key=lambda hit: (document_frequency.get(hit, 0), hit not in name_tokens, hit))
                )
        prefix: set[str] = set()
        for task_token in task_tokens - exact:
            if len(task_token) < 5:
                continue
            if any(
                len(skill_token) >= 5
                and len(os.path.commonprefix((task_token, skill_token))) >= 5
                for skill_token in skill_tokens
            ):
                prefix.add(task_token)

        # Rare terms carry more information than generic words such as
        # "audit". A name hit is additionally rewarded because it is the most
        # deliberate part of skill metadata. Cross-language synonym hits score
        # below exact hits but above plain prefix matches.
        score = sum(3.0 * idf(token) for token in exact)
        score += sum(1.5 * idf(token) for token in synonym_hits)
        score += sum(0.75 * idf(token) for token in prefix)
        score += sum(2.5 * idf(token) for token in exact & name_tokens)
        score += sum(1.0 * idf(token) for token in set(synonym_hits) & name_tokens)
        if score:
            scored.append((score, skill, sorted(exact | set(synonym_hits) | prefix)))
    scored.sort(key=lambda item: (-item[0], str(item[1]["name"]).casefold(), str(item[1]["path"]).casefold()))
    return [
        {
            "name": item[1]["name"],
            "path": item[1]["path"],
            "score": round(item[0], 3),
            "matched_tokens": item[2],
        }
        for item in scored[:CANDIDATE_LIMIT]
    ]


def compact_full_result(result: dict[str, object]) -> dict[str, object]:
    """Small per-prompt catalog: every skill stays available for semantics.

    Unlike the legacy shortlist-only view, this never reduces the inventory
    to the lexical top-8: all names, descriptions, and paths are retained
    alongside the full conflict/error details and the lexical shortlist.
    The shortlist is a generator; semantic selection stays mandatory.
    """
    skills = result.get("skills", [])
    slim: list[dict[str, object]] = []
    if isinstance(skills, list):
        for item in skills:
            if isinstance(item, dict):
                slim.append({
                    "name": item.get("name", ""),
                    "description": item.get("description", ""),
                    "path": item.get("path", ""),
                })
    conflicts = result.get("conflicts", [])
    errors = result.get("errors", [])
    return {
        "schema_version": result["schema_version"],
        "compact": True,
        "roots": result.get("roots", []),
        "skill_count": len(slim),
        "skills": slim,
        "shortlist": result.get("shortlist", []),
        "conflicts": conflicts,
        "conflicted_names": [item["name"] for item in conflicts],  # type: ignore[index,union-attr]
        "errors": errors,
        "semantic_selection_required": True,
        "max_final_skills": MAX_FINAL_SKILLS,
    }


def compact_result(result: dict[str, object]) -> dict[str, object]:
    conflicts = result.get("conflicts", [])
    errors = result.get("errors", [])
    return {
        "schema_version": result["schema_version"],
        "shortlist": result.get("shortlist", []),
        "semantic_selection_required": True,
        "max_final_skills": MAX_FINAL_SKILLS,
        "conflict_count": len(conflicts),  # type: ignore[arg-type]
        "conflicted_names": [item["name"] for item in conflicts],  # type: ignore[index,union-attr]
        "error_count": len(errors),  # type: ignore[arg-type]
        "errors": errors,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", action="append", default=[], help="Dodatkowy root skilli; można podać wielokrotnie.")
    parser.add_argument("--task", help="Zwróć także leksykalny shortlist dla tekstu zadania.")
    parser.add_argument("--inventory", action="store_true", help="Wypisz pełny inventory JSON (domyślne zachowanie).")
    parser.add_argument("--shortlist-only", action="store_true", help="Wypisz tylko kandydatów, konflikty i błędy.")
    parser.add_argument("--compact", action="store_true", help="Maly katalog na prompt: wszystkie skille, shortlist, konflikty i bledy.")
    parser.add_argument("--cache-file", default=None, help="Opcjonalny wersjonowany cache katalogu (tylko metadane, bez tresci zadania).")
    parser.add_argument("--all-runtimes", action="store_true", help="Tryb diagnostyczny: porównaj wszystkie lokalne drzewa narzędzi.")
    args = parser.parse_args(argv)
    roots = [*(all_runtime_roots() if args.all_runtimes else active_runtime_roots()), *(Path(item) for item in args.root)]
    result = build_inventory(roots, args.task, cache_file=args.cache_file, all_runtimes=args.all_runtimes)
    if args.compact:
        result = compact_full_result(result)
    elif args.shortlist_only:
        result = compact_result(result)
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
