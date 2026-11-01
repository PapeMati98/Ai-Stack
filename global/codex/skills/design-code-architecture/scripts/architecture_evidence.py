#!/usr/bin/env python3
"""
architecture_evidence.py - Canonical helper for validating architecture code evidence
against pinned Git commits and generating deterministic Mermaid diagrams.

Assurance boundary:
- Source location validation proves only that the specified path, line span, and quoted text
  exist at the pinned immutable Git commit object.
- It DOES NOT prove semantic causality, correctness of architectural relations, or business logic.
- All semantic relation assertions remain explicitly REVIEW REQUIRED.
"""

import sys
sys.dont_write_bytecode = True

import argparse
import json
import os
import posixpath
import re
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

MAX_MANIFEST_BYTES = 5 * 1024 * 1024  # 5 MB
MAX_SPAN_LINES = 500
MAX_EXCERPT_BYTES = 500 * 1024  # 500 KB
COMMIT_HASH_REGEX = re.compile(r"^[0-9a-fA-F]{40}$|^[0-9a-fA-F]{64}$")
SAFE_ID_REGEX = re.compile(r"^[a-zA-Z0-9_-]{1,100}$")


def _get_creationflags() -> int:
    if sys.platform == "win32":
        return getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
    return 0


def run_git(args: List[str], cwd: Optional[str] = None) -> Tuple[int, str, str]:
    try:
        proc = subprocess.run(
            ["git"] + args,
            cwd=str(cwd) if cwd is not None else None,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            creationflags=_get_creationflags(),
            check=False,
        )
        return proc.returncode, proc.stdout, proc.stderr
    except FileNotFoundError:
        return 127, "", "git executable not found in PATH"
    except Exception as e:
        return 1, "", f"Failed to execute git: {e}"


def validate_safe_repo_relative_path(path: str) -> Tuple[bool, str, Optional[str]]:
    """
    Validates that a path is a safe repo-relative path.
    Rejects:
    - empty paths
    - absolute paths (starting with /, \\, or drive letters like C:)
    - path traversal (.., components resolving outside repo root)
    - control characters (\\x00-\\x1f, \\x7f)
    Returns (is_valid, normalized_posix_path, error_message).
    """
    if not isinstance(path, str) or not path.strip():
        return False, "", "Path must be a non-empty string"

    for ch in path:
        code = ord(ch)
        if code < 32 or code == 127:
            return False, "", f"Path contains illegal control character (ASCII {code})"

    if path.startswith(("/", "\\")):
        return False, "", "Absolute paths are not allowed; path must be repo-relative"
    if len(path) >= 2 and path[1] == ":":
        return False, "", "Drive letters are not allowed; path must be repo-relative"
    if path.startswith("\\\\"):
        return False, "", "UNC paths are not allowed; path must be repo-relative"

    raw_norm = path.replace("\\", "/")
    raw_segments = raw_norm.split("/")
    if any(seg in (".", "..") for seg in raw_segments):
        return False, "", "Path traversal detected (relative segment '.' or '..')"

    normalized = posixpath.normpath(raw_norm)
    if normalized in (".", ""):
        return False, "", "Path cannot resolve to current directory or empty"
    if normalized.startswith("../") or normalized == "..":
        return False, "", "Path traversal detected (resolves above repo root)"

    parts = normalized.split("/")
    for part in parts:
        if part in (".", ".."):
            return False, "", "Path traversal detected in path segments"
        if not part:
            return False, "", "Empty path segment detected"

    return True, normalized, None


def check_symlink_escapes(commit: str, posix_path: str, repo_path: str) -> Optional[str]:
    """
    Inspects git tree at commit to ensure path or any parent directory is not a symlink (mode 120000).
    """
    parts = posix_path.split("/")
    accum = ""
    for part in parts:
        accum = f"{accum}/{part}" if accum else part
        rc, out, err = run_git(["ls-tree", "--full-tree", commit, accum], cwd=repo_path)
        if rc == 0 and out.strip():
            for line in out.strip().splitlines():
                if "\t" in line:
                    meta, item_path = line.split("\t", 1)
                    item_mode = meta.split()[0] if meta.split() else ""
                else:
                    tokens = line.split()
                    item_mode = tokens[0] if tokens else ""
                    item_path = tokens[-1] if len(tokens) >= 4 else ""
                if item_path == accum and item_mode == "120000":
                    return f"Path component '{accum}' is a symbolic link (mode 120000) in commit {commit}; symlinks are rejected."
    return None


def fetch_git_blob(commit: str, posix_path: str, repo_path: str) -> Tuple[int, str, str]:
    return run_git(["show", f"{commit}:{posix_path}"], cwd=repo_path)


def validate_evidence_item(
    evidence: Dict[str, Any],
    commit: str,
    repo_path: str,
    context: str = "evidence"
) -> List[str]:
    errors = []

    if not isinstance(evidence, dict):
        return [f"[{context}] Evidence item must be an object (dict), got {type(evidence).__name__}"]

    path = evidence.get("path")
    start_line = evidence.get("start_line")
    end_line = evidence.get("end_line")
    excerpt = evidence.get("excerpt")

    if not isinstance(path, str):
        errors.append(f"[{context}] 'path' must be a string, got {type(path).__name__}")
    if not isinstance(start_line, int) or isinstance(start_line, bool):
        errors.append(f"[{context}] 'start_line' must be an integer, got {type(start_line).__name__}")
    if not isinstance(end_line, int) or isinstance(end_line, bool):
        errors.append(f"[{context}] 'end_line' must be an integer, got {type(end_line).__name__}")
    if not isinstance(excerpt, str):
        errors.append(f"[{context}] 'excerpt' must be a string, got {type(excerpt).__name__}")

    if errors:
        return errors

    is_safe, posix_path, path_err = validate_safe_repo_relative_path(path)
    if not is_safe:
        errors.append(f"[{context}] Invalid path '{path}': {path_err}")
        return errors

    symlink_err = check_symlink_escapes(commit, posix_path, repo_path)
    if symlink_err:
        errors.append(f"[{context}] {symlink_err}")
        return errors

    if start_line < 1:
        errors.append(f"[{context}] Invalid span: start_line must be >= 1 (got {start_line})")
    if end_line < start_line:
        errors.append(f"[{context}] Invalid span: end_line ({end_line}) cannot be less than start_line ({start_line})")
    span_len = end_line - start_line + 1
    if span_len > MAX_SPAN_LINES:
        errors.append(f"[{context}] Invalid span: span length of {span_len} lines exceeds maximum allowed {MAX_SPAN_LINES}")

    if len(excerpt.encode("utf-8")) > MAX_EXCERPT_BYTES:
        errors.append(f"[{context}] Excerpt exceeds maximum allowed size of {MAX_EXCERPT_BYTES} bytes")

    if start_line < 1 or end_line < start_line:
        return errors

    rc_type, obj_type, err_type = run_git(["cat-file", "-t", f"{commit}:{posix_path}"], cwd=repo_path)
    if rc_type != 0 or obj_type.strip() != "blob":
        errors.append(f"[{context}] File '{posix_path}' does not exist at pinned commit {commit}: {err_type.strip()}")
        return errors

    rc, content, err = fetch_git_blob(commit, posix_path, repo_path)
    if rc != 0:
        errors.append(f"[{context}] File '{posix_path}' does not exist at pinned commit {commit}: {err.strip()}")
        return errors

    lines = content.splitlines()
    total_lines = len(lines)
    if end_line > total_lines:
        errors.append(f"[{context}] Invalid span: end_line ({end_line}) exceeds file line count ({total_lines}) in '{posix_path}' at commit {commit}")

    if errors:
        return errors

    actual_lines = lines[start_line - 1 : end_line]
    actual_slice = "\n".join(actual_lines)
    norm_excerpt = excerpt.replace("\r\n", "\n")

    if norm_excerpt != actual_slice and norm_excerpt.rstrip("\n") != actual_slice:
        if norm_excerpt.strip() != actual_slice.strip():
            errors.append(
                f"[{context}] Excerpt mismatch at {posix_path}:{start_line}-{end_line}. "
                f"Quoted excerpt does not match Git blob at commit {commit}."
            )
        else:
            errors.append(
                f"[{context}] Excerpt mismatch at {posix_path}:{start_line}-{end_line}: "
                f"Exact indentation or whitespace differs from Git blob at commit {commit}."
            )

    return errors


def validate_manifest(
    manifest_data: Dict[str, Any],
    repo_path: str,
    override_commit: Optional[str] = None
) -> Dict[str, Any]:
    repo_path = str(Path(repo_path).resolve())
    errors = []
    warnings = []

    if not isinstance(manifest_data, dict):
        return {
            "valid": False,
            "errors": ["Manifest root must be a JSON object (dict)"],
            "warnings": [],
            "assurance_boundary": {
                "source_location_verified": False,
                "semantic_proof": False,
                "semantic_verification": "FAILED"
            }
        }

    commit_is_valid = False
    commit = override_commit or manifest_data.get("commit")
    if not commit or not isinstance(commit, str):
        errors.append("Manifest must contain a valid 'commit' string (40 or 64 hex characters)")
    elif not COMMIT_HASH_REGEX.match(commit):
        errors.append(f"Commit hash '{commit}' is invalid; must be 40 or 64 hex characters")
    else:
        rc, out, err = run_git(["rev-parse", "--verify", f"{commit}^{{commit}}"], cwd=repo_path)
        if rc != 0:
            errors.append(f"Pinned commit '{commit}' is not a valid commit object in repository {repo_path}")
        else:
            commit = out.strip()
            commit_is_valid = True

    components = manifest_data.get("components", [])
    if not isinstance(components, list):
        errors.append("'components' must be a list of component objects")
        components = []

    comp_ids = set()
    validated_components = []
    for idx, comp in enumerate(components):
        c_ctx = f"component[{idx}]"
        if not isinstance(comp, dict):
            errors.append(f"{c_ctx} must be an object")
            continue
        c_id = comp.get("id")
        c_name = comp.get("name")
        if not c_id or not isinstance(c_id, str):
            errors.append(f"{c_ctx} must have a non-empty string 'id'")
        elif not SAFE_ID_REGEX.match(c_id):
            errors.append(f"{c_ctx} id '{c_id}' must match regex ^[a-zA-Z0-9_-]{{1,100}}$")
        elif c_id in comp_ids:
            errors.append(f"{c_ctx} duplicate component id '{c_id}'")
        else:
            comp_ids.add(c_id)

        if not c_name or not isinstance(c_name, str):
            errors.append(f"{c_ctx} must have a non-empty string 'name'")

        ev_list = comp.get("evidence")
        if isinstance(ev_list, list):
            for e_idx, ev in enumerate(ev_list):
                if commit_is_valid:
                    ev_errors = validate_evidence_item(ev, commit, repo_path, context=f"{c_ctx}.evidence[{e_idx}]")
                    errors.extend(ev_errors)
        elif ev_list is not None:
            errors.append(f"{c_ctx} 'evidence' must be a list if provided")

        validated_components.append({
            "id": c_id,
            "name": c_name,
            "type": comp.get("type", "module"),
            "description": comp.get("description", "")
        })

    relations = manifest_data.get("relations", [])
    if not isinstance(relations, list):
        errors.append("'relations' must be a list of relation objects")
        relations = []

    validated_relations = []
    for idx, rel in enumerate(relations):
        r_ctx = f"relation[{idx}]"
        if not isinstance(rel, dict):
            errors.append(f"{r_ctx} must be an object")
            continue
        r_from = rel.get("from")
        r_to = rel.get("to")
        r_type = rel.get("type", "depends_on")
        r_label = rel.get("label", r_type)

        if not r_from or not isinstance(r_from, str):
            errors.append(f"{r_ctx} 'from' must be a non-empty string")
        elif r_from not in comp_ids:
            errors.append(f"{r_ctx} 'from' component '{r_from}' does not exist in declared components")

        if not r_to or not isinstance(r_to, str):
            errors.append(f"{r_ctx} 'to' must be a non-empty string")
        elif r_to not in comp_ids:
            errors.append(f"{r_ctx} 'to' component '{r_to}' does not exist in declared components")

        r_ev_list = rel.get("evidence")
        if isinstance(r_ev_list, list):
            for e_idx, ev in enumerate(r_ev_list):
                if commit_is_valid:
                    ev_errors = validate_evidence_item(ev, commit, repo_path, context=f"{r_ctx}.evidence[{e_idx}]")
                    errors.extend(ev_errors)
        elif r_ev_list is not None:
            errors.append(f"{r_ctx} 'evidence' must be a list if provided")

        # Semantic relation assertions remain explicitly review-required
        validated_relations.append({
            "from": r_from,
            "to": r_to,
            "type": r_type,
            "label": r_label,
            "review_required": True,
            "semantic_verification": "REVIEW_REQUIRED",
            "assurance_note": "Source citation verified; semantic relation causality requires manual architectural review."
        })

    facts = manifest_data.get("facts", [])
    if isinstance(facts, list):
        for idx, fact in enumerate(facts):
            f_ctx = f"fact[{idx}]"
            if isinstance(fact, dict):
                comp_ref = fact.get("component")
                if comp_ref and comp_ref not in comp_ids:
                    errors.append(f"{f_ctx} references undeclared component '{comp_ref}'")
                f_ev_list = fact.get("evidence")
                if isinstance(f_ev_list, list):
                    for e_idx, ev in enumerate(f_ev_list):
                        if commit_is_valid:
                            ev_errors = validate_evidence_item(ev, commit, repo_path, context=f"{f_ctx}.evidence[{e_idx}]")
                            errors.extend(ev_errors)
                elif f_ev_list is not None:
                    errors.append(f"{f_ctx} 'evidence' must be a list if provided")
    elif facts is not None:
        errors.append("'facts' must be a list if provided")

    assumptions = manifest_data.get("assumptions", [])
    if isinstance(assumptions, list):
        for idx, assump in enumerate(assumptions):
            if isinstance(assump, dict):
                if "status" not in assump:
                    assump["status"] = "review_required"
    elif assumptions is not None:
        errors.append("'assumptions' must be a list if provided")

    is_valid = len(errors) == 0

    return {
        "valid": is_valid,
        "commit": commit,
        "repo_path": repo_path,
        "components_count": len(validated_components),
        "relations_count": len(validated_relations),
        "assurance_boundary": {
            "source_location_verified": is_valid,
            "semantic_proof": False,
            "semantic_verification": "REVIEW_REQUIRED",
            "statement": (
                "Validation proves only that cited file paths, bounded line spans, and exact excerpts "
                "match the immutable Git object tree at the pinned commit. It DOES NOT prove semantic causality "
                "or correctness of architectural relations. All semantic relation assertions remain explicitly REVIEW REQUIRED."
            )
        },
        "errors": errors,
        "warnings": warnings,
        "components": sorted(validated_components, key=lambda c: c.get("id", "")),
        "relations": sorted(validated_relations, key=lambda r: (r.get("from", ""), r.get("to", ""), r.get("type", "")))
    }


def escape_mermaid_label(text: str) -> str:
    """
    Escapes characters that interfere with Mermaid syntax:
    Quotes, brackets, parenthesis, braces, angle brackets, newlines.
    """
    if not isinstance(text, str):
        text = str(text)
    text = "".join(ch for ch in text if ord(ch) >= 32 or ch in ("\n", "\t"))
    text = text.replace("&", "&amp;")
    text = text.replace('"', "#quot;")
    text = text.replace("<", "#lt;").replace(">", "#gt;")
    text = text.replace("[", "#91;").replace("]", "#93;")
    text = text.replace("(", "#40;").replace(")", "#41;")
    text = text.replace("{", "#123;").replace("}", "#125;")
    text = text.replace("\n", "<br/>")
    return text


def generate_mermaid_diagram(validation_result: Dict[str, Any]) -> str:
    commit = validation_result.get("commit", "unknown")
    short_commit = commit[:8] if commit else "unknown"
    components = validation_result.get("components", [])
    relations = validation_result.get("relations", [])

    lines = [
        "flowchart TD",
        "  %% ----------------------------------------------------------------",
        f"  %% Pinned Git Commit: {commit}",
        "  %% Assurance Boundary: Citations verified against Git tree.",
        "  %% Semantic relations remain explicitly REVIEW REQUIRED.",
        "  %% ----------------------------------------------------------------",
        "",
        '  subgraph Assurance_Boundary ["Assurance Boundary: Source Verified | Semantic Review Required"]',
        f'    Legend_Notice["Commit: {short_commit}<br/>Source Locations: VERIFIED<br/>Semantic Relations: REVIEW REQUIRED"]',
        "  end",
        ""
    ]

    comps_by_type: Dict[str, List[Dict[str, Any]]] = {}
    for comp in sorted(components, key=lambda c: (c.get("type", ""), c.get("id", ""))):
        ctype = comp.get("type", "module")
        comps_by_type.setdefault(ctype, []).append(comp)

    for ctype in sorted(comps_by_type.keys()):
        safe_type_name = escape_mermaid_label(ctype.title())
        subgraph_id = f"sg_{re.sub(r'[^a-zA-Z0-9_]', '_', ctype)}"
        lines.append(f'  subgraph {subgraph_id} ["{safe_type_name}"]')
        for comp in comps_by_type[ctype]:
            cid = comp["id"]
            node_id = f"c_{re.sub(r'[^a-zA-Z0-9_]', '_', cid)}"
            cname = escape_mermaid_label(comp.get("name", cid))
            cdesc = escape_mermaid_label(comp.get("description", ""))
            if cdesc:
                label = f"{cname}<br/><sub>{cdesc}</sub>"
            else:
                label = cname
            lines.append(f'    {node_id}["{label}"]')
        lines.append("  end")
        lines.append("")

    sorted_relations = sorted(
        relations,
        key=lambda r: (r.get("from", ""), r.get("to", ""), r.get("label", ""))
    )
    for rel in sorted_relations:
        from_id = f"c_{re.sub(r'[^a-zA-Z0-9_]', '_', rel['from'])}"
        to_id = f"c_{re.sub(r'[^a-zA-Z0-9_]', '_', rel['to'])}"
        raw_label = rel.get("label") or rel.get("type") or "relates"
        if raw_label.endswith("[review req]"):
            base_label = raw_label[:-len("[review req]")].rstrip()
        else:
            base_label = raw_label
        escaped_label = f"{escape_mermaid_label(base_label)} [review req]"
        lines.append(f'  {from_id} -->|"{escaped_label}"| {to_id}')

    lines.append("")
    return "\n".join(lines)


def publish_atomic(content: str, output_path: str) -> None:
    """
    Publishes content to output_path atomically via a temporary file.
    Guarantees that a previous valid file survives if writing fails.
    """
    target = Path(output_path).resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    temp_path = target.with_name(f"{target.name}.tmp_{os.getpid()}")
    try:
        with open(temp_path, "w", encoding="utf-8") as f:
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp_path, target)
    except Exception as e:
        raise RuntimeError(f"Failed to atomically publish Mermaid file to {target}: {e}")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Validate code evidence against pinned Git commit and publish deterministic Mermaid architecture diagrams."
    )
    subparsers = parser.add_subparsers(dest="command")

    # validate subcommand
    p_val = subparsers.add_parser("validate", help="Validate manifest against git repo")
    p_val.add_argument("manifest", help="Path to architecture evidence manifest JSON")
    p_val.add_argument("--repo", default=".", help="Path to git repository (default: .)")
    p_val.add_argument("--commit", help="Override pinned git commit hash")
    p_val.add_argument("--diagnostics", help="Path to output JSON diagnostics")
    p_val.add_argument("--format", choices=["json", "text"], default="text", help="Output format for stdout")

    # render subcommand
    p_ren = subparsers.add_parser("render", help="Validate and render Mermaid diagram")
    p_ren.add_argument("manifest", help="Path to architecture evidence manifest JSON")
    p_ren.add_argument("--output", "-o", required=True, help="Target path for rendered Mermaid .mmd file")
    p_ren.add_argument("--repo", default=".", help="Path to git repository (default: .)")
    p_ren.add_argument("--commit", help="Override pinned git commit hash")
    p_ren.add_argument("--diagnostics", help="Path to output JSON diagnostics")
    p_ren.add_argument("--format", choices=["json", "text"], default="text", help="Output format for stdout")

    # Fallback/shorthand routing if no explicit subcommand was provided
    cli_args = sys.argv[1:]
    if cli_args and not cli_args[0].startswith("-") and cli_args[0] not in ("validate", "render", "-h", "--help"):
        if any(opt in cli_args for opt in ("--output", "-o")):
            cli_args = ["render"] + cli_args
        else:
            cli_args = ["validate"] + cli_args

    args = parser.parse_args(cli_args)

    if not args.command:
        parser.print_help(sys.stderr)
        return 2

    manifest_file = args.manifest
    repo_dir = getattr(args, "repo", ".")
    commit_override = getattr(args, "commit", None)
    diag_file = getattr(args, "diagnostics", None)
    fmt = getattr(args, "format", "text")
    output_file = getattr(args, "output", None) if args.command == "render" else None

    if not manifest_file:
        parser.print_help(sys.stderr)
        return 2

    manifest_path = Path(manifest_file).resolve()
    if not manifest_path.is_file():
        sys.stderr.write(f"Error: Manifest file '{manifest_path}' does not exist\n")
        return 2

    if manifest_path.stat().st_size > MAX_MANIFEST_BYTES:
        sys.stderr.write(f"Error: Manifest file exceeds maximum size of {MAX_MANIFEST_BYTES} bytes\n")
        return 1

    try:
        with open(manifest_path, "r", encoding="utf-8") as f:
            manifest_data = json.load(f)
    except Exception as e:
        sys.stderr.write(f"Error: Failed to parse manifest JSON: {e}\n")
        return 1

    repo_resolved = str(Path(repo_dir).resolve())

    # Perform validation
    res = validate_manifest(manifest_data, repo_resolved, override_commit=commit_override)

    # If output_file was requested, generate and publish Mermaid diagram only on validation success
    if res["valid"] and output_file:
        try:
            mermaid_text = generate_mermaid_diagram(res)
            publish_atomic(mermaid_text, output_file)
        except Exception as e:
            res["valid"] = False
            res["errors"].append(f"Failed to publish Mermaid diagram: {e}")

    # Write diagnostics if requested
    if diag_file:
        try:
            diag_path = Path(diag_file).resolve()
            diag_path.parent.mkdir(parents=True, exist_ok=True)
            with open(diag_path, "w", encoding="utf-8") as f:
                json.dump(res, f, indent=2)
        except Exception as e:
            sys.stderr.write(f"Warning: Failed to write diagnostics file '{diag_file}': {e}\n")

    # Format stdout output
    if fmt == "json":
        print(json.dumps(res, indent=2))
    else:
        status_label = "VALID" if res["valid"] else "INVALID"
        print(f"Architecture Evidence Verification: {status_label}")
        print(f"Pinned Commit: {res.get('commit')}")
        print(f"Components: {res.get('components_count')}, Relations: {res.get('relations_count')}")
        print("Assurance Boundary: Source locations verified; semantic relations remain REVIEW_REQUIRED.")
        if res["errors"]:
            print("\nValidation Errors:")
            for err in res["errors"]:
                print(f"  - [FAIL] {err}")
        if res["valid"] and output_file:
            print(f"\nSuccessfully published Mermaid diagram to: {output_file}")

    return 0 if res["valid"] else 1


if __name__ == "__main__":
    sys.exit(main())
