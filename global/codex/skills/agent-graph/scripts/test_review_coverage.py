#!/usr/bin/env python3
"""test_review_coverage.py - Acceptance test suite for review coverage verification.

Tests:
1. Actual Git fixture with rename detection.
2. Generated file structured exclusion.
3. Missing changed file detection (unexplained omitted changes fail).
4. Invalid finding and stale anchor detection (stale anchors fail).
5. Cross-file dependency defect reporting and requirement to examine dependent behavior.
6. Full valid review manifest passing verification.

Accepts --artifact-root and retains all synthetic fixtures without cleanup/rmtree/unlink.
"""
from __future__ import annotations

import sys
sys.dont_write_bytecode = True

import argparse
import json
import os
from pathlib import Path
import subprocess
import unittest

from review_coverage import (
    get_git_diff_inventory,
    get_file_content_at_commit,
    verify_review_coverage,
    run_git,
    _get_creationflags,
)


def _run_git_checked(
    args: list[str],
    cwd: Path | str,
    env: dict[str, str] | None = None,
    input: str | None = None,
) -> str:
    code, out, err = run_git(args, cwd=cwd, env=env, input=input)
    if code != 0:
        raise RuntimeError(
            f"git {' '.join(args)} failed in '{cwd}' (exit code {code}): {err.strip()}"
        )
    return out


def init_git_fixture(root: Path) -> dict[str, str]:
    """Create or resume synthetic Git repository fixture with commits, renames, and dependencies.

    Deterministic and idempotent across repeated runs at the same --artifact-root without deleting fixtures.
    """
    repo = root / "git_review_repo"
    repo.mkdir(parents=True, exist_ok=True)
    meta_file = repo / "fixture_meta.json"

    # Fast path: if verified valid fixture already exists at this root, reuse verified commit SHAs
    if (repo / ".git").exists() and meta_file.exists():
        try:
            meta = json.loads(meta_file.read_text(encoding="utf-8"))
            base_sha = meta.get("base_sha", "")
            head_sha = meta.get("head_sha", "")
            if base_sha and head_sha and base_sha != head_sha:
                c1, _, _ = run_git(["rev-parse", "--verify", f"{base_sha}^{{commit}}"], cwd=repo)
                c2, _, _ = run_git(["rev-parse", "--verify", f"{head_sha}^{{commit}}"], cwd=repo)
                if c1 == 0 and c2 == 0:
                    inventory, err = get_git_diff_inventory(repo, base_sha, head_sha)
                    if err is None and any(cf.path == "calc_utils.py" and cf.status == "R" for cf in inventory):
                        return {
                            "repo": str(repo),
                            "base_sha": base_sha,
                            "head_sha": head_sha,
                        }
        except Exception:
            pass

    # Ensure repository is initialized with checked return codes
    if not (repo / ".git").exists():
        _run_git_checked(["init"], cwd=repo)
    _run_git_checked(["config", "user.name", "Review Fixture"], cwd=repo)
    _run_git_checked(["config", "user.email", "review-fixture@example.com"], cwd=repo)

    # 1. Base commit files
    math_file = repo / "math_utils.py"
    math_file.write_text(
        '"""Math utilities for service calculations."""\n'
        "\n"
        "def add(a: int, b: int) -> int:\n"
        "    return a + b\n"
        "\n"
        "def multiply(a: int, b: int) -> int:\n"
        "    return a * b\n",
        encoding="utf-8",
    )

    service_file = repo / "service.py"
    service_file.write_text(
        '"""Service consuming math_utils."""\n'
        "from math_utils import add\n"
        "\n"
        "def compute_total(items: list[int]) -> int:\n"
        "    total = 0\n"
        "    for item in items:\n"
        "        total = add(total, item)\n"
        "    return total\n",
        encoding="utf-8",
    )

    config_file = repo / "config.json"
    config_file.write_text('{"version": "1.0.0", "active": true}\n', encoding="utf-8")

    # Build base commit via isolated index file (avoids conflict with pre-existing working tree files)
    idx_base = repo / ".git" / "fixture_base_index"
    env_base = os.environ.copy()
    env_base["GIT_INDEX_FILE"] = str(idx_base)
    _run_git_checked(["add", "math_utils.py", "service.py", "config.json"], cwd=repo, env=env_base)
    tree_base = _run_git_checked(["write-tree"], cwd=repo, env=env_base).strip()
    base_sha = _run_git_checked(
        ["commit-tree", tree_base, "-m", "Base commit with initial math and service"],
        cwd=repo,
    ).strip()

    # 2. Head commit files
    calc_file = repo / "calc_utils.py"
    calc_file.write_text(
        '"""Calculator utilities with improved bounds."""\n'
        "\n"
        "def add(a: int, b: int) -> int:\n"
        "    # Safe integer addition\n"
        "    return a + b\n"
        "\n"
        "def multiply(a: int, b: int) -> int:\n"
        "    return a * b\n",
        encoding="utf-8",
    )

    service_file.write_text(
        '"""Service consuming calc_utils."""\n'
        "from calc_utils import add\n"
        "\n"
        "def compute_total(items: list[int]) -> int:\n"
        "    total = 0\n"
        "    for item in items:\n"
        "        total = add(total, item)\n"
        "    return total\n",
        encoding="utf-8",
    )

    dist_dir = repo / "dist"
    dist_dir.mkdir(exist_ok=True)
    bundle_file = dist_dir / "bundle.js"
    bundle_file.write_text(
        "// Machine-generated bundle v1.0.1\nconsole.log('ready');\n",
        encoding="utf-8",
    )

    unreviewed = repo / "unreviewed_file.py"
    unreviewed.write_text(
        "# This change was omitted from review manifest\nx = 42\n",
        encoding="utf-8",
    )

    # Build head commit via isolated index file (math_utils.py omitted, calc_utils.py added)
    idx_head = repo / ".git" / "fixture_head_index"
    env_head = os.environ.copy()
    env_head["GIT_INDEX_FILE"] = str(idx_head)
    _run_git_checked(
        ["add", "calc_utils.py", "service.py", "config.json", "dist/bundle.js", "unreviewed_file.py"],
        cwd=repo,
        env=env_head,
    )
    tree_head = _run_git_checked(["write-tree"], cwd=repo, env=env_head).strip()
    head_sha = _run_git_checked(
        [
            "commit-tree",
            tree_head,
            "-p",
            base_sha,
            "-m",
            "Head commit with rename, generated bundle, service edit, and unreviewed file",
        ],
        cwd=repo,
    ).strip()

    # Update main branch ref
    _run_git_checked(["update-ref", "refs/heads/main", head_sha], cwd=repo)
    _run_git_checked(["symbolic-ref", "HEAD", "refs/heads/main"], cwd=repo)

    # Save fixture metadata for verified idempotent reuse
    meta_file.write_text(
        json.dumps({"base_sha": base_sha, "head_sha": head_sha}, indent=2),
        encoding="utf-8",
    )

    return {
        "repo": str(repo),
        "base_sha": base_sha,
        "head_sha": head_sha,
    }


class ReviewCoverageTests(unittest.TestCase):
    artifact_root: Path
    fixture: dict[str, str]

    @classmethod
    def setUpClass(cls):
        cls.fixture = init_git_fixture(cls.artifact_root)

    def test_git_inventory_with_rename(self):
        repo = Path(self.fixture["repo"])
        inventory, err = get_git_diff_inventory(repo, self.fixture["base_sha"], self.fixture["head_sha"])
        self.assertIsNone(err)
        paths = {cf.path: cf for cf in inventory}

        # Verify rename detection
        self.assertIn("calc_utils.py", paths)
        calc_cf = paths["calc_utils.py"]
        self.assertEqual(calc_cf.status, "R")
        self.assertEqual(calc_cf.old_path, "math_utils.py")

        # Verify other changed files
        self.assertIn("service.py", paths)
        self.assertIn("dist/bundle.js", paths)
        self.assertIn("unreviewed_file.py", paths)

    def test_generated_exclusion(self):
        repo = Path(self.fixture["repo"])
        manifest = {
            "schema_version": 1,
            "base": self.fixture["base_sha"],
            "head": self.fixture["head_sha"],
            "reviewed_files": [
                {"path": "calc_utils.py", "status": "reviewed", "decision": "ACCEPT"},
                {"path": "service.py", "status": "reviewed", "decision": "ACCEPT"},
                {"path": "unreviewed_file.py", "status": "reviewed", "decision": "ACCEPT"},
            ],
            "excluded_files": [
                {
                    "path": "dist/bundle.js",
                    "reason": "generated",
                    "justification": "Machine-generated bundle build output artifact",
                }
            ],
            "findings": [],
            "dependency_pointers": [
                {
                    "source_path": "calc_utils.py",
                    "target_path": "service.py",
                    "relation": "imported_by",
                    "examined": True,
                    "status": "clean",
                    "notes": "Verified import and caller compatibility",
                }
            ],
        }
        res = verify_review_coverage(repo, self.fixture["base_sha"], self.fixture["head_sha"], manifest)
        self.assertEqual(res["status"], "PASS")
        self.assertEqual(res["excluded_files_count"], 1)
        self.assertEqual(len(res["omitted_files"]), 0)

        # Invalid exclusion reason must fail
        manifest_invalid_reason = dict(manifest)
        manifest_invalid_reason["excluded_files"] = [
            {"path": "dist/bundle.js", "reason": "unauthorized_reason", "justification": "arbitrary"}
        ]
        res_invalid = verify_review_coverage(repo, self.fixture["base_sha"], self.fixture["head_sha"], manifest_invalid_reason)
        self.assertEqual(res_invalid["status"], "FAIL")
        self.assertTrue(any("Invalid exclusion reason" in err for err in res_invalid["errors"]))

    def test_missing_changed_file_fails(self):
        repo = Path(self.fixture["repo"])
        # Manifest omits unreviewed_file.py
        manifest = {
            "schema_version": 1,
            "base": self.fixture["base_sha"],
            "head": self.fixture["head_sha"],
            "reviewed_files": [
                {"path": "calc_utils.py", "status": "reviewed", "decision": "ACCEPT"},
                {"path": "service.py", "status": "reviewed", "decision": "ACCEPT"},
            ],
            "excluded_files": [
                {
                    "path": "dist/bundle.js",
                    "reason": "generated",
                    "justification": "Machine-generated build bundle",
                }
            ],
            "findings": [],
            "dependency_pointers": [],
        }
        res = verify_review_coverage(repo, self.fixture["base_sha"], self.fixture["head_sha"], manifest)
        self.assertEqual(res["status"], "FAIL")
        self.assertIn("unreviewed_file.py", res["omitted_files"])
        self.assertTrue(any("Unexplained omitted changed file: 'unreviewed_file.py'" in err for err in res["errors"]))

    def test_invalid_finding_stale_anchor_fails(self):
        repo = Path(self.fixture["repo"])
        # Finding anchor text does not match head content (stale anchor)
        manifest = {
            "schema_version": 1,
            "base": self.fixture["base_sha"],
            "head": self.fixture["head_sha"],
            "reviewed_files": [
                {"path": "calc_utils.py", "status": "reviewed", "decision": "REPAIR"},
                {"path": "service.py", "status": "reviewed", "decision": "ACCEPT"},
                {"path": "unreviewed_file.py", "status": "reviewed", "decision": "ACCEPT"},
            ],
            "excluded_files": [
                {"path": "dist/bundle.js", "reason": "generated", "justification": "Generated bundle"}
            ],
            "findings": [
                {
                    "finding_id": "FINDING-STALE-01",
                    "path": "calc_utils.py",
                    "start_line": 3,
                    "end_line": 4,
                    "anchor_text": "def calculate_ancient_formula():\n    pass",  # Does NOT match calc_utils.py
                    "severity": "critical",
                    "message": "Outdated anchor citation from old draft",
                }
            ],
            "dependency_pointers": [],
        }
        res = verify_review_coverage(repo, self.fixture["base_sha"], self.fixture["head_sha"], manifest)
        self.assertEqual(res["status"], "FAIL")
        self.assertTrue(len(res["stale_anchors"]) > 0)
        self.assertEqual(res["stale_anchors"][0]["finding_id"], "FINDING-STALE-01")

        # Also verify invalid span exceeding total lines fails
        manifest_bad_span = dict(manifest)
        manifest_bad_span["findings"] = [
            {
                "finding_id": "FINDING-SPAN-02",
                "path": "calc_utils.py",
                "start_line": 100,
                "end_line": 200,
                "anchor_text": "def foo(): pass",
                "severity": "critical",
                "message": "Span out of bounds",
            }
        ]
        res_bad_span = verify_review_coverage(repo, self.fixture["base_sha"], self.fixture["head_sha"], manifest_bad_span)
        self.assertEqual(res_bad_span["status"], "FAIL")
        self.assertTrue(len(res_bad_span["stale_anchors"]) > 0)

    def test_cross_file_dependency_defect(self):
        repo = Path(self.fixture["repo"])

        # Case 1: Dependency pointer was not examined
        manifest_unexamined = {
            "schema_version": 1,
            "base": self.fixture["base_sha"],
            "head": self.fixture["head_sha"],
            "reviewed_files": [
                {"path": "calc_utils.py", "status": "reviewed", "decision": "ACCEPT"},
                {"path": "service.py", "status": "reviewed", "decision": "ACCEPT"},
                {"path": "unreviewed_file.py", "status": "reviewed", "decision": "ACCEPT"},
            ],
            "excluded_files": [
                {"path": "dist/bundle.js", "reason": "generated", "justification": "Generated bundle"}
            ],
            "findings": [],
            "dependency_pointers": [
                {
                    "source_path": "calc_utils.py",
                    "target_path": "service.py",
                    "relation": "imported_by",
                    "examined": False,  # Not examined!
                    "notes": "Did not check callers in service",
                }
            ],
        }
        res_unexamined = verify_review_coverage(repo, self.fixture["base_sha"], self.fixture["head_sha"], manifest_unexamined)
        self.assertEqual(res_unexamined["status"], "FAIL")
        self.assertTrue(any("Unexamined cross-file dependency" in err for err in res_unexamined["errors"]))
        self.assertIn("Reviewers must examine dependent behavior", res_unexamined["manifest_completeness_notice"])

        # Case 2: Cross-file dependency defect detected
        manifest_defect = {
            "schema_version": 1,
            "base": self.fixture["base_sha"],
            "head": self.fixture["head_sha"],
            "reviewed_files": [
                {"path": "calc_utils.py", "status": "reviewed", "decision": "REPAIR"},
                {"path": "service.py", "status": "reviewed", "decision": "REPAIR"},
                {"path": "unreviewed_file.py", "status": "reviewed", "decision": "ACCEPT"},
            ],
            "excluded_files": [
                {"path": "dist/bundle.js", "reason": "generated", "justification": "Generated bundle"}
            ],
            "findings": [],
            "dependency_pointers": [
                {
                    "source_path": "calc_utils.py",
                    "target_path": "service.py",
                    "relation": "imported_by",
                    "examined": True,
                    "status": "defect_detected",  # Defect present
                    "notes": "Signature change broke service.py caller contracts",
                }
            ],
        }
        res_defect = verify_review_coverage(repo, self.fixture["base_sha"], self.fixture["head_sha"], manifest_defect)
        self.assertEqual(res_defect["status"], "FAIL")
        self.assertTrue(any("Cross-file dependency defect detected" in err for err in res_defect["errors"]))
        self.assertTrue(len(res_defect["dependency_defects"]) > 0)

    def test_re_runnability_idempotence(self):
        """Verify init_git_fixture can be re-run at the same artifact root without errors or diff loss."""
        rerun_fixture = init_git_fixture(self.artifact_root)
        self.assertEqual(rerun_fixture["repo"], self.fixture["repo"])
        self.assertNotEqual(rerun_fixture["base_sha"], rerun_fixture["head_sha"])

        repo = Path(rerun_fixture["repo"])
        inventory, err = get_git_diff_inventory(repo, rerun_fixture["base_sha"], rerun_fixture["head_sha"])
        self.assertIsNone(err)
        paths = {cf.path: cf for cf in inventory}
        self.assertIn("calc_utils.py", paths)
        self.assertEqual(paths["calc_utils.py"].status, "R")
        self.assertEqual(paths["calc_utils.py"].old_path, "math_utils.py")


def main():
    parser = argparse.ArgumentParser(description="Run review coverage acceptance tests.")
    parser.add_argument(
        "--artifact-root",
        default=str(Path.cwd() / "fixtures" / "review"),
        help="Root directory where synthetic test fixtures are retained.",
    )
    args, remaining_argv = parser.parse_known_args()

    artifact_root = Path(args.artifact_root).resolve()
    artifact_root.mkdir(parents=True, exist_ok=True)
    ReviewCoverageTests.artifact_root = artifact_root

    # Run unittest with sanitized argv
    runner = unittest.TextTestRunner(verbosity=2)
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(ReviewCoverageTests)
    result = runner.run(suite)
    sys.exit(0 if result.wasSuccessful() else 1)


if __name__ == "__main__":
    main()
