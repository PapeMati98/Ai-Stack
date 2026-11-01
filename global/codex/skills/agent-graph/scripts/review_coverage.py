#!/usr/bin/env python3
"""review_coverage.py - Review coverage and anchor verification against Git changes.

Enforces:
1. Git base/head changed-file inventory with rename detection.
2. Structured accounting: every changed file must be reviewed or excluded with valid reason.
3. Anchor freshness: findings must have valid line spans and exact matching excerpts at head.
4. Dependency context pointers: verifies target existence and flags unexamined or defective cross-file dependencies.
5. Explicit assurance boundary: manifest completeness is NOT code review and does not automatically find all semantic bugs; reviewers must examine dependent behavior.
"""
from __future__ import annotations

import sys
sys.dont_write_bytecode = True

import argparse
import json
import os
from pathlib import Path
import re
import subprocess
from typing import Any, Dict, List, Optional, Set, Tuple


VERSION = "0.1.0"
ALLOWED_EXCLUSION_REASONS = frozenset({
    "generated",
    "lockfile",
    "vendor",
    "fixture",
    "binary",
    "config",
    "docs",
})
SAFE_ID_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._-]{0,99}$")


def _get_creationflags() -> int:
    if sys.platform == "win32":
        return getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
    return 0


def run_git(
    args: List[str],
    cwd: Optional[Path | str] = None,
    input: Optional[str] = None,
    env: Optional[Dict[str, str]] = None,
) -> Tuple[int, str, str]:
    """Execute git command with CREATE_NO_WINDOW on Windows."""
    try:
        proc = subprocess.run(
            ["git"] + args,
            input=input,
            cwd=str(cwd) if cwd is not None else None,
            env=env,
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
    except Exception as exc:
        return 1, "", f"Failed to execute git: {exc}"


def is_safe_repo_relative_path(path: str) -> bool:
    """Validate path is safe, relative, and does not escape repository root."""
    if not isinstance(path, str):
        return False
    normalized = path.replace("\\", "/").strip()
    if not normalized or normalized.startswith("/") or ":" in normalized:
        return False
    parts = normalized.split("/")
    if any(p == ".." or p == "." or not p for p in parts):
        return False
    return True


def normalize_lines(text: str) -> List[str]:
    """Normalize text into lines with universal line endings and stripped trailing whitespace."""
    return [line.rstrip("\r\n") for line in text.splitlines()]


class ChangedFile:
    def __init__(self, status: str, path: str, old_path: Optional[str] = None, score: Optional[int] = None):
        self.status = status  # 'A', 'M', 'D', 'R', 'C', 'T'
        self.path = path.replace("\\", "/")
        self.old_path = old_path.replace("\\", "/") if old_path else None
        self.score = score

    def to_dict(self) -> Dict[str, Any]:
        result: Dict[str, Any] = {"status": self.status, "path": self.path}
        if self.old_path:
            result["old_path"] = self.old_path
        if self.score is not None:
            result["score"] = self.score
        return result


def get_git_diff_inventory(repo: Path, base: str, head: str) -> Tuple[List[ChangedFile], Optional[str]]:
    """Parse git diff --name-status -M <base> <head> into ChangedFile records."""
    code, out, err = run_git(["diff", "--name-status", "-M", base, head], cwd=repo)
    if code != 0:
        return [], f"git diff failed (code {code}): {err.strip()}"

    inventory: List[ChangedFile] = []
    lines = out.strip().splitlines()
    for line in lines:
        if not line.strip():
            continue
        parts = line.split("\t")
        if not parts:
            continue
        raw_status = parts[0].strip()
        status_letter = raw_status[0].upper()
        if status_letter in ("R", "C") and len(parts) >= 3:
            # Rename or copy: R100\told_path\tnew_path
            score_match = re.search(r"\d+", raw_status)
            score = int(score_match.group(0)) if score_match else None
            old_p = parts[1].strip()
            new_p = parts[2].strip()
            inventory.append(ChangedFile(status=status_letter, path=new_p, old_path=old_p, score=score))
        elif len(parts) >= 2:
            path = parts[1].strip()
            inventory.append(ChangedFile(status=status_letter, path=path))
        else:
            # Fallback if whitespace separation differs
            tokens = line.split()
            if len(tokens) >= 2:
                inventory.append(ChangedFile(status=tokens[0][0].upper(), path=tokens[1]))

    return inventory, None


def get_file_content_at_commit(repo: Path, commit_or_ref: str, file_path: str) -> Tuple[Optional[str], Optional[str]]:
    """Retrieve file content at specified commit/ref via git show."""
    git_path = file_path.replace("\\", "/")
    code, out, err = run_git(["show", f"{commit_or_ref}:{git_path}"], cwd=repo)
    if code != 0:
        return None, f"File '{file_path}' not found at {commit_or_ref}: {err.strip()}"
    return out, None


def verify_review_coverage(repo: Path, base: str, head: str, manifest: Dict[str, Any]) -> Dict[str, Any]:
    """Verify review manifest coverage and anchor integrity against Git diff."""
    errors: List[str] = []
    warnings: List[str] = []
    omitted_files: List[str] = []
    stale_anchors: List[Dict[str, Any]] = []
    invalid_findings: List[Dict[str, Any]] = []
    dependency_defects: List[Dict[str, Any]] = []
    exclusion_errors: List[Dict[str, Any]] = []

    # 1. Obtain Git changed files inventory
    changed_files, diff_err = get_git_diff_inventory(repo, base, head)
    if diff_err:
        return {
            "status": "FAIL",
            "base": base,
            "head": head,
            "repo": str(repo),
            "errors": [diff_err],
            "changed_files_count": 0,
            "reviewed_files_count": 0,
            "excluded_files_count": 0,
            "manifest_completeness_notice": (
                "Manifest completeness verifies changed-file accounting and anchor validity. "
                "It does not replace code review or guarantee semantic correctness. "
                "Reviewers must examine dependent behavior across module boundaries."
            ),
        }

    # 2. Index reviewed files and exclusions
    reviewed_list = manifest.get("reviewed_files", [])
    excluded_list = manifest.get("excluded_files", [])
    findings_list = manifest.get("findings", [])
    pointers_list = manifest.get("dependency_pointers", [])

    reviewed_map: Dict[str, Dict[str, Any]] = {}
    for item in reviewed_list:
        if isinstance(item, dict) and "path" in item:
            p = str(item["path"]).replace("\\", "/")
            reviewed_map[p] = item

    excluded_map: Dict[str, Dict[str, Any]] = {}
    for item in excluded_list:
        if isinstance(item, dict) and "path" in item:
            p = str(item["path"]).replace("\\", "/")
            reason = item.get("reason", "")
            justification = item.get("justification", "")
            if reason not in ALLOWED_EXCLUSION_REASONS:
                exclusion_errors.append({
                    "path": p,
                    "reason": reason,
                    "error": f"Invalid exclusion reason '{reason}'. Allowed: {sorted(ALLOWED_EXCLUSION_REASONS)}"
                })
            elif not justification or not isinstance(justification, str) or len(justification.strip()) < 4:
                exclusion_errors.append({
                    "path": p,
                    "reason": reason,
                    "error": "Exclusion lacks meaningful structured justification (minimum 4 non-whitespace chars required)"
                })
            else:
                excluded_map[p] = item

    # 3. Check every changed file is accounted for
    for cf in changed_files:
        path = cf.path
        old_path = cf.old_path
        accounted = False
        if path in reviewed_map or path in excluded_map:
            accounted = True
        elif old_path and (old_path in reviewed_map or old_path in excluded_map):
            accounted = True

        if not accounted:
            omitted_files.append(path)
            errors.append(f"Unexplained omitted changed file: '{path}' (status: {cf.status})")

    if exclusion_errors:
        for ee in exclusion_errors:
            errors.append(f"Exclusion error for '{ee['path']}': {ee['error']}")

    # 4. Verify finding anchors against head commit
    for idx, finding in enumerate(findings_list):
        fid = finding.get("finding_id", f"finding-{idx + 1}")
        fpath = str(finding.get("path", "")).replace("\\", "/")
        if not is_safe_repo_relative_path(fpath):
            invalid_findings.append({
                "finding_id": fid,
                "path": fpath,
                "error": "Unsafe or non-relative finding path"
            })
            errors.append(f"Finding '{fid}' has unsafe path '{fpath}'")
            continue

        start_line = finding.get("start_line")
        end_line = finding.get("end_line")
        anchor_text = finding.get("anchor_text")

        if not isinstance(start_line, int) or not isinstance(end_line, int) or start_line < 1 or end_line < start_line:
            invalid_findings.append({
                "finding_id": fid,
                "path": fpath,
                "error": f"Invalid line span: start_line={start_line}, end_line={end_line}"
            })
            errors.append(f"Finding '{fid}' has invalid line span: [{start_line}, {end_line}]")
            continue

        # Fetch actual file content at head
        content, read_err = get_file_content_at_commit(repo, head, fpath)
        if read_err or content is None:
            stale_anchors.append({
                "finding_id": fid,
                "path": fpath,
                "error": f"Target file does not exist at head commit: {read_err}"
            })
            errors.append(f"Finding '{fid}' references missing file at head: '{fpath}'")
            continue

        file_lines = normalize_lines(content)
        if end_line > len(file_lines):
            stale_anchors.append({
                "finding_id": fid,
                "path": fpath,
                "start_line": start_line,
                "end_line": end_line,
                "file_line_count": len(file_lines),
                "error": f"End line {end_line} exceeds total file lines ({len(file_lines)})"
            })
            errors.append(f"Finding '{fid}' span [{start_line}, {end_line}] exceeds file length {len(file_lines)}")
            continue

        # Check exact anchor match
        actual_span_lines = file_lines[start_line - 1:end_line]
        actual_span_text = "\n".join(actual_span_lines)
        expected_anchor_text = "\n".join(normalize_lines(str(anchor_text))) if anchor_text is not None else ""

        if actual_span_text.strip() != expected_anchor_text.strip():
            stale_anchors.append({
                "finding_id": fid,
                "path": fpath,
                "start_line": start_line,
                "end_line": end_line,
                "expected": expected_anchor_text,
                "actual": actual_span_text,
                "error": "Anchor text does not match actual file content at head commit"
            })
            errors.append(f"Finding '{fid}' has stale anchor at '{fpath}':lines {start_line}-{end_line}")

        # Check finding-level dependency context pointers
        dep_context = finding.get("dependency_context", [])
        if isinstance(dep_context, list):
            for dep in dep_context:
                if isinstance(dep, dict):
                    dep_path = str(dep.get("path", "")).replace("\\", "/")
                    if not is_safe_repo_relative_path(dep_path):
                        invalid_findings.append({
                            "finding_id": fid,
                            "dependency_path": dep_path,
                            "error": "Unsafe dependency context path"
                        })
                        errors.append(f"Finding '{fid}' has unsafe dependency path '{dep_path}'")
                    else:
                        dep_content, dep_err = get_file_content_at_commit(repo, head, dep_path)
                        if dep_err or dep_content is None:
                            invalid_findings.append({
                                "finding_id": fid,
                                "dependency_path": dep_path,
                                "error": f"Dependent file not found at head: {dep_err}"
                            })
                            errors.append(f"Finding '{fid}' dependency '{dep_path}' does not exist at head")

    # 5. Check manifest-level dependency pointers and defects
    for idx, pointer in enumerate(pointers_list):
        if not isinstance(pointer, dict):
            continue
        src = str(pointer.get("source_path", "")).replace("\\", "/")
        tgt = str(pointer.get("target_path", "")).replace("\\", "/")
        relation = pointer.get("relation", "dependent")
        examined = pointer.get("examined", False)
        status = pointer.get("status", "clean")
        notes = pointer.get("notes", "")

        if not is_safe_repo_relative_path(tgt):
            dependency_defects.append({
                "source_path": src,
                "target_path": tgt,
                "error": "Unsafe target path in dependency pointer"
            })
            errors.append(f"Dependency pointer {src} -> {tgt} has unsafe target path")
            continue

        # Check target exists at head
        tgt_content, tgt_err = get_file_content_at_commit(repo, head, tgt)
        if tgt_err or tgt_content is None:
            dependency_defects.append({
                "source_path": src,
                "target_path": tgt,
                "error": f"Dependent target file does not exist at head: {tgt_err}"
            })
            errors.append(f"Dependency target '{tgt}' does not exist at head")
            continue

        if not examined:
            dependency_defects.append({
                "source_path": src,
                "target_path": tgt,
                "relation": relation,
                "error": "Reviewer did not examine dependent behavior for cross-file caller/callee"
            })
            errors.append(f"Unexamined cross-file dependency: {src} -> {tgt}. Reviewer must examine dependent behavior.")

        if status == "defect_detected":
            dependency_defects.append({
                "source_path": src,
                "target_path": tgt,
                "relation": relation,
                "notes": notes,
                "error": "Unresolved cross-file dependency defect"
            })
            errors.append(f"Cross-file dependency defect detected: {src} -> {tgt} ({notes})")

    status = "PASS" if not errors else "FAIL"
    return {
        "status": status,
        "base": base,
        "head": head,
        "repo": str(repo),
        "changed_files_count": len(changed_files),
        "reviewed_files_count": len(reviewed_map),
        "excluded_files_count": len(excluded_map),
        "changed_files": [cf.to_dict() for cf in changed_files],
        "omitted_files": omitted_files,
        "stale_anchors": stale_anchors,
        "invalid_findings": invalid_findings,
        "dependency_defects": dependency_defects,
        "exclusion_errors": exclusion_errors,
        "errors": errors,
        "warnings": warnings,
        "manifest_completeness_notice": (
            "Manifest completeness verifies changed-file accounting and anchor validity. "
            "It does not replace code review or guarantee semantic correctness. "
            "Reviewers must examine dependent behavior across module boundaries."
        ),
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Verify review coverage, exclusions, anchor freshness, and dependency context against Git diff."
    )
    parser.add_argument("--repo", default=".", help="Path to Git repository root (default: current directory)")
    parser.add_argument("--base", default="HEAD~1", help="Base Git commit/ref (default: HEAD~1)")
    parser.add_argument("--head", default="HEAD", help="Head Git commit/ref (default: HEAD)")
    parser.add_argument("--review", help="Path to review manifest JSON file")
    parser.add_argument("--output", help="Optional path to write verification result JSON")
    parser.add_argument("--inventory-only", action="store_true", help="Only output changed files inventory")
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    repo_path = Path(args.repo).resolve()
    if not repo_path.is_dir():
        sys.stderr.write(f"Error: repo directory '{repo_path}' does not exist.\n")
        return 2

    if args.inventory_only:
        inventory, err = get_git_diff_inventory(repo_path, args.base, args.head)
        if err:
            sys.stderr.write(f"Error: {err}\n")
            return 1
        payload = {
            "status": "INVENTORY",
            "base": args.base,
            "head": args.head,
            "changed_files": [cf.to_dict() for cf in inventory],
        }
        output_json = json.dumps(payload, ensure_ascii=False, indent=2)
        if args.output:
            Path(args.output).write_text(output_json + "\n", encoding="utf-8")
        sys.stdout.write(output_json + "\n")
        return 0

    if not args.review:
        sys.stderr.write("Error: --review <manifest.json> is required unless --inventory-only is specified.\n")
        return 2

    review_path = Path(args.review).resolve()
    if not review_path.is_file():
        sys.stderr.write(f"Error: review manifest file '{review_path}' not found.\n")
        return 2

    try:
        manifest = json.loads(review_path.read_text(encoding="utf-8"))
    except Exception as exc:
        sys.stderr.write(f"Error parsing review manifest JSON: {exc}\n")
        return 2

    result = verify_review_coverage(repo_path, args.base, args.head, manifest)
    output_json = json.dumps(result, ensure_ascii=False, indent=2)

    if args.output:
        Path(args.output).write_text(output_json + "\n", encoding="utf-8")

    sys.stdout.write(output_json + "\n")
    return 0 if result["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
