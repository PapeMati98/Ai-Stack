#!/usr/bin/env python3
"""Read-only inventory of skill instructions and explicitly supplied rules."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import sys
from collections import defaultdict
from pathlib import Path

MAX_FILE_BYTES = 1024 * 1024
REPARSE = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
HIDDEN = getattr(stat, "FILE_ATTRIBUTE_HIDDEN", 0x2)
UNIVERSAL = re.compile(
    r"\b(?:every|each|all|always|must|shall|mandatory|required|obligatory|"
    r"obligations?|obowiązkow\w*|zawsze|musi|należy|nie wolno)\b", re.I)
RECEIPT = re.compile(
    r"\b(?:receipt|closeout|record-session|record-handoff|checkpoint|"
    r"handoff|evidence|receipt_status)\b", re.I)
OLD_CONTRACT = re.compile(
    r"\b(?:fidelity\s*/\s*sufficiency|planner[- ]witness|acceptance[- ]contract|"
    r"quality[- ]contract|amendment budget)\b", re.I)
NEGATION = re.compile(r"\b(?:no|not|without|don't|doesn't|never|unless|nie|bez|zakaz)\b", re.I)
LEGACY = re.compile(
    r"\b(?:legacy|deprecated|historical|superseded|removed|obsolete|"
    r"wycofan\w*|historyczn\w*|zastąpion\w*)\b", re.I)
OBLIGATION = re.compile(
    r"\b(?:must|shall|required|mandatory|obligatory|should|always|never|do not|"
    r"must not|nie wolno|musi|należy|wymagane|obowiązkow\w*|zawsze)\b", re.I)
FENCE = re.compile(r"^\s*(\x60{3}|~~~)")


class InventoryError(Exception):
    def __init__(self, code):
        self.code = code


def _guard(value, base=None):
    """Resolve components lexically and reject links/junctions before access."""
    raw = os.fspath(value)
    if raw.startswith((r"\\", "//")):
        raise InventoryError("network_path_rejected")
    path = Path(raw)
    if path.is_absolute():
        current, parts = Path(path.anchor), path.parts[1:]
    else:
        base = Path(base or Path.cwd())
        if not base.is_absolute():
            base = Path(os.path.abspath(base))
        current, parts = _guard(base), path.parts
    for part in parts:
        if part in ("", "."):
            continue
        if part == "..":
            current = current.parent
            continue
        current /= part
        try:
            st = os.lstat(current)
        except FileNotFoundError:
            continue
        except NotADirectoryError:
            raise InventoryError("not_a_directory") from None
        except OSError:
            raise InventoryError("path_unavailable") from None
        if stat.S_ISLNK(st.st_mode) or getattr(st, "st_file_attributes", 0) & REPARSE:
            raise InventoryError("reparse_point_rejected")
    return current


def _sensitive(value):
    for part in Path(value).parts:
        name = part.casefold()
        if name in {"secret", "secrets", "token", "tokens", "password", "passwords", "auth", "credentials"}:
            return True
        if name.startswith("credentials") or (name.startswith("auth") and name.endswith(".json")):
            return True
        if re.match(r"^(?:secret|token|password|auth)(?:[-_.].+)$", name):
            return True
    return False


def _canonical(path):
    return os.path.normcase(os.path.normpath(os.path.abspath(path)))


def _read(path):
    path = _guard(path)
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        raise InventoryError("missing") from None
    except OSError:
        raise InventoryError("unreadable") from None
    if stat.S_ISLNK(st.st_mode) or getattr(st, "st_file_attributes", 0) & REPARSE:
        raise InventoryError("reparse_point_rejected")
    if not stat.S_ISREG(st.st_mode):
        raise InventoryError("not_a_regular_file")
    if st.st_size > MAX_FILE_BYTES:
        raise InventoryError("oversize")
    try:
        with open(path, "rb") as stream:
            data = stream.read(MAX_FILE_BYTES + 1)
    except OSError:
        raise InventoryError("unreadable") from None
    if len(data) > MAX_FILE_BYTES:
        raise InventoryError("oversize")
    try:
        return data, data.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise InventoryError("invalid_utf8") from None


def _flags(text):
    result, fenced = [], False
    for number, line in enumerate(text.splitlines(), 1):
        if FENCE.match(line):
            fenced = not fenced
            continue
        if fenced:
            continue
        for regex, kind in ((UNIVERSAL, "universal_trigger_candidate"),
                            (RECEIPT, "formal_receipt_candidate"),
                            (OLD_CONTRACT, "old_contract_candidate")):
            if regex.search(line):
                result.append({
                    "type": kind, "line": number,
                    "negation_hint": bool(NEGATION.search(line)),
                    "legacy_hint": bool(LEGACY.search(line)),
                })
    return result


def _duplicate_key(line):
    line = re.sub(r"^(?:[-*+]\s+|\d+[.)]\s+|\[[ xX]\]\s*)", "", line.strip())
    line = re.sub(r"\s+", " ", line).strip().casefold().rstrip(" .;:")
    return line if len(line) >= 50 and len(line.split()) >= 8 and OBLIGATION.search(line) else None


FRONTMATTER_RE = re.compile(r"^---[ \t]*\n(.*?)\n---[ \t]*(?:\n|$)", re.S)
TOP_KEY_RE = re.compile(r"^([A-Za-z0-9_-]+)[ \t]*:[ \t]*(.*)$")


def _strip_unquoted_yaml_comment(val: str) -> str:
    """Ucina komentarz YAML (# poza cudzyslowem). Pusty wynik oznacza brak wartosci."""
    out = []
    in_single = False
    in_double = False
    escaped = False
    i = 0
    while i < len(val):
        ch = val[i]
        if in_double:
            out.append(ch)
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_double = False
        elif in_single:
            out.append(ch)
            if ch == "'" and i + 1 < len(val) and val[i + 1] == "'":
                out.append("'")
                i += 1
            elif ch == "'":
                in_single = False
        elif ch == '"':
            in_double = True
            out.append(ch)
        elif ch == "'":
            in_single = True
            out.append(ch)
        elif ch == "#" and (i == 0 or val[i - 1].isspace()):
            break
        else:
            out.append(ch)
        i += 1
    return "".join(out).strip()


def _unquote_yaml_scalar(val: str) -> str:
    val = val.strip()
    if len(val) >= 2 and ((val[0] == '"' and val[-1] == '"') or (val[0] == "'" and val[-1] == "'")):
        inner = val[1:-1]
        if val[0] == '"':
            inner = inner.replace(r'\"', '"').replace(r"\\", "\\")
        else:
            inner = inner.replace("''", "'")
        return inner.strip()
    return val


def _extract_frontmatter_fields(text: str):
    """Extract (frontmatter_utf8_bytes, name, description) from YAML frontmatter, or None if absent."""
    normalized = text.lstrip("\ufeff").replace("\r\n", "\n").replace("\r", "\n")
    match = FRONTMATTER_RE.match(normalized)
    if not match:
        return None
    fm_raw = match.group(1)
    fm_bytes = len(fm_raw.encode("utf-8"))
    lines = fm_raw.splitlines()
    fields = {}
    idx = 0
    while idx < len(lines):
        line = lines[idx]
        if not line.strip() or line.lstrip().startswith("#"):
            idx += 1
            continue
        key_match = TOP_KEY_RE.match(line)
        if not key_match:
            idx += 1
            continue
        key = key_match.group(1).strip().casefold()
        rest = _strip_unquoted_yaml_comment(key_match.group(2))
        idx += 1
        block_lines = []
        while idx < len(lines) and (lines[idx].startswith((" ", "\t")) or not lines[idx].strip()):
            block_lines.append(lines[idx])
            idx += 1
        if rest in (">", ">-", ">+", "|", "|-", "|+"):
            stripped_lines = [b.strip() for b in block_lines if b.strip()]
            joiner = " " if rest.startswith(">") else "\n"
            fields[key] = joiner.join(stripped_lines).strip()
        else:
            scalar = _unquote_yaml_scalar(rest)
            cont = [_strip_unquoted_yaml_comment(b) for b in block_lines if _strip_unquoted_yaml_comment(b) and not b.strip().startswith("-")]
            if cont and not scalar:
                scalar = " ".join(_unquote_yaml_scalar(c) for c in cont).strip()
            elif cont and scalar:
                scalar = (scalar + " " + " ".join(_unquote_yaml_scalar(c) for c in cont)).strip()
            fields[key] = scalar
    name = fields.get("name", "").strip() or None
    description = fields.get("description", "").strip() or None
    return fm_bytes, name, description


def _failure_record(path, role, skill, code):
    return {"path": str(path), "role": role, "skill": skill, "status": "failed",
            "sha256": None, "bytes": None, "lines": None, "excerpt_flags": [],
            "failure": code}


def _inside(root, path):
    try:
        return os.path.commonpath([_canonical(root), _canonical(path)]) == _canonical(root)
    except ValueError:
        return False


def build_report(catalog_value, rules=None):
    catalog_path = _guard(catalog_value)
    if _sensitive(catalog_path):
        raise InventoryError("sensitive_path_excluded")
    raw, _ = _read(catalog_path)
    try:
        catalog = json.loads(raw.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise InventoryError("catalog_invalid_json") from None
    if not isinstance(catalog, dict) or not isinstance(catalog.get("skills"), list):
        raise InventoryError("catalog_missing_skills_list")

    report = {
        "schema_version": 1,
        "summary": {
            "detected_files": 0, "read_files": 0, "failed_files": 0,
            "excluded_items": 0, "partial": False,
            "eager_discovery_overhead": {
                "measured_skills": 0,
                "incomplete_frontmatter_skills": 0,
                "frontmatter_bytes": 0,
                "name_description_bytes": 0,
                "estimated_tokens": 0,
                "estimation_formula": "utf8_bytes_div_4_approx",
                "scope": "supplied_catalog_deduplicated_skill_roots",
                "policy": "passive_telemetry_not_a_gate",
            },
        },
        "files": [], "issues": [], "catalog_name_conflicts": [],
        "duplicate_obligations": [], "mentioned_skill_candidates": [],
    }
    roots, names = {}, defaultdict(set)

    def issue(code, source):
        report["issues"].append({"source": source, "reason": code})

    def failed_file(path, role, name, code):
        report["summary"]["detected_files"] += 1
        report["summary"]["failed_files"] += 1
        report["files"].append(_failure_record(path, role, name, code))
        issue(code, str(path))
        report["summary"]["partial"] = True

    for index, item in enumerate(catalog["skills"], 1):
        if not isinstance(item, dict) or not isinstance(item.get("name"), str) or not isinstance(item.get("path"), str):
            issue("invalid_catalog_entry", f"catalog_entry_{index}")
            continue
        name, raw_path = item["name"].strip(), item["path"]
        if not name or _sensitive(name) or _sensitive(raw_path):
            report["summary"]["excluded_items"] += 1
            issue("sensitive_input_excluded", f"catalog_entry_{index}")
            continue
        try:
            path = _guard(raw_path, catalog_path.parent)
        except InventoryError as error:
            report["summary"]["excluded_items"] += 1
            issue(error.code, f"catalog_entry_{index}")
            continue
        root = path.parent if path.name.casefold() == "skill.md" else path
        key = _canonical(root)
        roots.setdefault(key, {"root": root, "names": set(), "file": root / "SKILL.md"})
        roots[key]["names"].add(name)
        names[name.casefold()].add(key)

    for folded, keys in sorted(names.items()):
        if len(keys) > 1:
            label = next(n for key in keys for n in roots[key]["names"] if n.casefold() == folded)
            report["catalog_name_conflicts"].append({
                "name": label, "candidate_paths": sorted(str(roots[k]["root"]) for k in keys)})

    contents, duplicate_lines = defaultdict(list), defaultdict(list)

    def read_file(path, role, skill_key=None, name=None):
        try:
            data, text = _read(path)
            record = {"path": str(path), "role": role, "skill": name, "status": "read",
                      "sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data),
                      "lines": len(text.splitlines()), "excerpt_flags": _flags(text)}
        except InventoryError as error:
            failed_file(path, role, name, error.code)
            if role == "skill":
                report["summary"]["eager_discovery_overhead"]["incomplete_frontmatter_skills"] += 1
            return
        report["summary"]["detected_files"] += 1
        report["summary"]["read_files"] += 1
        report["files"].append(record)
        if role == "skill":
            ego = report["summary"]["eager_discovery_overhead"]
            fm_info = _extract_frontmatter_fields(text)
            if fm_info is None:
                ego["incomplete_frontmatter_skills"] += 1
            else:
                fm_bytes, fm_name, fm_desc = fm_info
                ego["frontmatter_bytes"] += fm_bytes
                if fm_name and fm_desc:
                    ego["measured_skills"] += 1
                    ego["name_description_bytes"] += len(fm_name.encode("utf-8")) + len(fm_desc.encode("utf-8"))
                    ego["estimated_tokens"] = round(ego["name_description_bytes"] / 4)
                else:
                    ego["incomplete_frontmatter_skills"] += 1
        if skill_key is None:
            return
        contents[skill_key].append((str(path), text))
        fenced = False
        for number, line in enumerate(text.splitlines(), 1):
            if FENCE.match(line):
                fenced = not fenced
            elif not fenced and (key := _duplicate_key(line)):
                duplicate_lines[key].append({
                    "skill_path": str(roots[skill_key]["root"]),
                    "skill_names": sorted(roots[skill_key]["names"]),
                    "file": str(path), "line": number,
                })

    for key, entry in roots.items():
        root, name = entry["root"], sorted(entry["names"])[0]
        skill_file = entry["file"]
        read_file(skill_file, "skill", key, name)
        try:
            refs = _guard(root / "references")
            try:
                st = os.lstat(refs)
            except FileNotFoundError:
                continue
            if stat.S_ISLNK(st.st_mode) or getattr(st, "st_file_attributes", 0) & REPARSE:
                raise InventoryError("reparse_point_rejected")
            if not stat.S_ISDIR(st.st_mode):
                issue("references_not_directory", f"skill:{name}")
                report["summary"]["partial"] = True
                continue
        except InventoryError as error:
            issue(error.code, f"skill:{name}")
            report["summary"]["partial"] = True
            continue

        stack = [refs]
        while stack:
            directory = stack.pop()
            try:
                children = sorted(os.scandir(directory), key=lambda child: child.name.casefold())
            except OSError:
                issue("directory_unreadable", f"skill:{name}")
                report["summary"]["partial"] = True
                continue
            for child in children:
                path = Path(child.path)
                if _sensitive(child.name):
                    report["summary"]["excluded_items"] += 1
                    continue
                try:
                    path = _guard(path)
                    st = os.lstat(path)
                except InventoryError as error:
                    if path.suffix.casefold() == ".md":
                        failed_file(path, "reference", name, error.code)
                    else:
                        issue(error.code, f"skill:{name}")
                        report["summary"]["partial"] = True
                    continue
                if stat.S_ISLNK(st.st_mode) or getattr(st, "st_file_attributes", 0) & REPARSE:
                    if path.suffix.casefold() == ".md":
                        failed_file(path, "reference", name, "reparse_point_rejected")
                    else:
                        issue("reparse_point_rejected", f"skill:{name}")
                        report["summary"]["partial"] = True
                elif stat.S_ISDIR(st.st_mode):
                    if path.name.startswith(".") or getattr(st, "st_file_attributes", 0) & HIDDEN:
                        report["summary"]["excluded_items"] += 1
                        continue
                    if not _inside(root, path):
                        issue("outside_skill_root_rejected", f"skill:{name}")
                        report["summary"]["partial"] = True
                        continue
                    stack.append(path)
                elif stat.S_ISREG(st.st_mode) and path.suffix.casefold() == ".md":
                    if not _inside(root, path):
                        failed_file(path, "reference", name, "outside_skill_root_rejected")
                    else:
                        read_file(path, "reference", key, name)

    for index, raw_rule in enumerate(rules or [], 1):
        label = f"rule_input_{index}"
        if _sensitive(raw_rule):
            report["summary"]["excluded_items"] += 1
            issue("sensitive_path_excluded", label)
            report["summary"]["partial"] = True
            continue
        try:
            path = _guard(raw_rule, catalog_path.parent)
            if path.is_dir():
                raise InventoryError("rules_must_be_explicit_files")
        except InventoryError as error:
            report["summary"]["excluded_items"] += 1
            issue(error.code, label)
            report["summary"]["partial"] = True
            continue
        read_file(path, "rule")

    for line, locations in duplicate_lines.items():
        if len({item["skill_path"] for item in locations}) > 1:
            report["duplicate_obligations"].append({
                "normalized_line_sha256": hashlib.sha256(line.encode("utf-8")).hexdigest(),
                "character_count": len(line), "locations": locations,
                "interpretation": "exact_normalized_obligation_candidate",
            })

    relations = defaultdict(set)
    for source, docs in contents.items():
        for folded, targets in names.items():
            if len(folded) < 3:
                continue
            pattern = re.compile(r"(?<![\w])" + re.escape(folded) + r"(?![\w])", re.I)
            for file_path, text in docs:
                for number, line in enumerate(text.splitlines(), 1):
                    if pattern.search(line):
                        for target in targets - {source}:
                            relations[(source, folded)].add((file_path, number))
    for (source, folded), locations in relations.items():
        report["mentioned_skill_candidates"].append({
            "source_skill_path": str(roots[source]["root"]),
            "mentioned_name": folded,
            "candidate_paths": sorted(str(roots[k]["root"]) for k in names[folded]),
            "locations": [{"file": path, "line": line}
                          for path, line in sorted(locations)],
            "relationship": "textual_mention_candidate_only",
        })
    report["summary"]["partial"] |= bool(
        report["summary"]["failed_files"] or report["summary"]["excluded_items"] or report["issues"])
    return report


def _write_report(report, output_value, catalog_value):
    rendered = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    if output_value is None:
        sys.stdout.write(rendered)
        return
    output = _guard(output_value)
    if _sensitive(output):
        raise InventoryError("sensitive_output_path_rejected")
    output_key = _canonical(output)
    inputs = {_canonical(_guard(catalog_value))}
    inputs.update(_canonical(item["path"]) for item in report["files"] if item.get("path"))
    if output_key in inputs:
        raise InventoryError("output_overlaps_input")
    for item in report["files"]:
        if item.get("role") == "skill" and item.get("path"):
            root = _canonical(Path(item["path"]).parent)
            try:
                if os.path.commonpath([root, output_key]) == root:
                    raise InventoryError("output_inside_skill_root")
            except ValueError:
                pass
    if not output.parent.is_dir():
        raise InventoryError("output_parent_missing")
    try:
        with open(output, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(rendered)
    except OSError:
        raise InventoryError("output_write_failed") from None


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--catalog", required=True, help="JSON inventory with a skills array")
    parser.add_argument("--rules", action="append", default=[], help="Explicit markdown rules file; repeatable")
    parser.add_argument("--output", help="Write JSON report to this explicit path; default is stdout")
    args = parser.parse_args(argv)
    try:
        report = build_report(args.catalog, args.rules)
        _write_report(report, args.output, args.catalog)
    except InventoryError as error:
        sys.stderr.write(f"audit_inventory: {error.code}\n")
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

