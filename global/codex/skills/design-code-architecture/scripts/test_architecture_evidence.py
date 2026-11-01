#!/usr/bin/env python3
"""
test_architecture_evidence.py - Synthetic Git repository acceptance test suite
for architecture_evidence.py.

All tests accept --artifact-root and retain synthetic fixtures there.
No TemporaryDirectory cleanup / rmtree / unlink is performed.
All subprocesses run with hidden Windows consoles and bytecode generation disabled.
"""

import sys
sys.dont_write_bytecode = True

import argparse
import json
import os
import subprocess
import time
from pathlib import Path
from typing import Any, Dict, List, Tuple

# Import architecture_evidence module directly
SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))
import architecture_evidence as ae


def _get_creationflags() -> int:
    if sys.platform == "win32":
        return getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
    return 0


def run_cmd(args: List[str], cwd: Path) -> Tuple[int, str, str]:
    proc = subprocess.run(
        args,
        cwd=str(cwd),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        creationflags=_get_creationflags(),
        check=False,
    )
    return proc.returncode, proc.stdout, proc.stderr


def init_git_repo(repo_dir: Path) -> str:
    repo_dir.mkdir(parents=True, exist_ok=True)
    rc, _, err = run_cmd(["git", "init"], cwd=repo_dir)
    if rc != 0:
        raise RuntimeError(f"git init failed: {err}")
    run_cmd(["git", "config", "user.name", "Test Committer"], cwd=repo_dir)
    run_cmd(["git", "config", "user.email", "test@example.com"], cwd=repo_dir)
    run_cmd(["git", "config", "commit.gpgSign", "false"], cwd=repo_dir)
    run_cmd(["git", "config", "core.autocrlf", "false"], cwd=repo_dir)
    run_cmd(["git", "config", "core.quotepath", "false"], cwd=repo_dir)
    run_cmd(["git", "config", "core.longpaths", "true"], cwd=repo_dir)

    # Initial dummy file
    readme = repo_dir / "README.md"
    readme.write_text("# Synthetic Test Repository\n", encoding="utf-8")
    rc_add, _, err_add = run_cmd(["git", "add", "README.md"], cwd=repo_dir)
    if rc_add != 0:
        raise RuntimeError(f"git add README.md failed: {err_add}")
    rc_ci, _, err_ci = run_cmd(["git", "commit", "-m", "Initial commit"], cwd=repo_dir)
    if rc_ci != 0:
        raise RuntimeError(f"git commit failed: {err_ci}")

    rc, out, err = run_cmd(["git", "rev-parse", "HEAD"], cwd=repo_dir)
    if rc != 0:
        raise RuntimeError(f"git rev-parse HEAD failed: {err}")
    return out.strip()


def commit_file(repo_dir: Path, rel_path: str, content: str, msg: str) -> str:
    target = repo_dir / rel_path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    rc_add, _, err_add = run_cmd(["git", "add", rel_path], cwd=repo_dir)
    if rc_add != 0:
        raise RuntimeError(f"git add {rel_path} failed: {err_add}")
    rc_ci, _, err_ci = run_cmd(["git", "commit", "-m", msg], cwd=repo_dir)
    if rc_ci != 0:
        raise RuntimeError(f"git commit failed: {err_ci}")
    rc, out, err = run_cmd(["git", "rev-parse", "HEAD"], cwd=repo_dir)
    if rc != 0:
        raise RuntimeError(f"git rev-parse HEAD failed: {err}")
    return out.strip()


# ==============================================================================
# Test Cases
# ==============================================================================

def test_valid_citation(fixture_dir: Path) -> None:
    repo_dir = fixture_dir / "repo_valid_citation"
    init_git_repo(repo_dir)

    code = (
        "def authenticate(user, password):\n"
        "    if not user or not password:\n"
        "        return False\n"
        "    return verify_credentials(user, password)\n"
    )
    commit = commit_file(repo_dir, "auth/service.py", code, "Add auth service")

    manifest = {
        "commit": commit,
        "components": [
            {
                "id": "auth_service",
                "name": "Authentication Service",
                "type": "service",
                "description": "Handles user credentials",
                "evidence": [
                    {
                        "path": "auth/service.py",
                        "start_line": 1,
                        "end_line": 4,
                        "excerpt": code.strip()
                    }
                ]
            }
        ],
        "relations": []
    }

    manifest_file = fixture_dir / "manifest_valid.json"
    manifest_file.write_text(json.dumps(manifest, indent=2), encoding="utf-8")

    out_mmd = fixture_dir / "diagram_valid.mmd"

    # Test via CLI subprocess
    rc, stdout, stderr = run_cmd(
        [
            sys.executable, "-B", str(SCRIPT_DIR / "architecture_evidence.py"),
            "render", str(manifest_file),
            "--repo", str(repo_dir),
            "--output", str(out_mmd)
        ],
        cwd=fixture_dir
    )

    assert rc == 0, f"Expected CLI success, got rc={rc}, stderr={stderr}"
    assert out_mmd.is_file(), "Expected Mermaid output diagram to be published"
    mmd_content = out_mmd.read_text(encoding="utf-8")
    assert "c_auth_service" in mmd_content, "Expected node id in Mermaid diagram"
    assert "Authentication Service" in mmd_content, "Expected component name in Mermaid diagram"
    assert "Assurance Boundary" in mmd_content, "Expected Assurance Boundary in Mermaid diagram"
    print("  [PASS] test_valid_citation")


def test_wrong_quote(fixture_dir: Path) -> None:
    repo_dir = fixture_dir / "repo_wrong_quote"
    init_git_repo(repo_dir)

    real_code = "def process_order(order_id):\n    validate(order_id)\n    save(order_id)\n"
    commit = commit_file(repo_dir, "orders/processor.py", real_code, "Add processor")

    tampered_excerpt = "def process_order(order_id):\n    # Tampered code\n    return True\n"

    manifest = {
        "commit": commit,
        "components": [
            {
                "id": "order_proc",
                "name": "Order Processor",
                "evidence": [
                    {
                        "path": "orders/processor.py",
                        "start_line": 1,
                        "end_line": 3,
                        "excerpt": tampered_excerpt.strip()
                    }
                ]
            }
        ],
        "relations": []
    }

    manifest_file = fixture_dir / "manifest_wrong_quote.json"
    manifest_file.write_text(json.dumps(manifest, indent=2), encoding="utf-8")

    out_mmd = fixture_dir / "diagram_wrong_quote.mmd"

    rc, stdout, stderr = run_cmd(
        [
            sys.executable, "-B", str(SCRIPT_DIR / "architecture_evidence.py"),
            "render", str(manifest_file),
            "--repo", str(repo_dir),
            "--output", str(out_mmd)
        ],
        cwd=fixture_dir
    )

    assert rc != 0, "Expected validation failure for wrong quote"
    assert not out_mmd.exists(), "Mermaid diagram must not be published on validation failure"
    assert "Excerpt mismatch" in stdout or "Excerpt mismatch" in stderr, "Expected Excerpt mismatch error"
    print("  [PASS] test_wrong_quote")


def test_missing_path(fixture_dir: Path) -> None:
    repo_dir = fixture_dir / "repo_missing_path"
    commit = init_git_repo(repo_dir)

    manifest = {
        "commit": commit,
        "components": [
            {
                "id": "missing_comp",
                "name": "Missing Component",
                "evidence": [
                    {
                        "path": "nonexistent/module.py",
                        "start_line": 1,
                        "end_line": 5,
                        "excerpt": "some code"
                    }
                ]
            }
        ],
        "relations": []
    }

    manifest_file = fixture_dir / "manifest_missing_path.json"
    manifest_file.write_text(json.dumps(manifest, indent=2), encoding="utf-8")

    out_mmd = fixture_dir / "diagram_missing_path.mmd"

    rc, stdout, stderr = run_cmd(
        [
            sys.executable, "-B", str(SCRIPT_DIR / "architecture_evidence.py"),
            "render", str(manifest_file),
            "--repo", str(repo_dir),
            "--output", str(out_mmd)
        ],
        cwd=fixture_dir
    )

    assert rc != 0, "Expected validation failure for missing path"
    assert not out_mmd.exists(), "Mermaid diagram must not be published on missing path"
    assert "does not exist at pinned commit" in stdout or "does not exist at pinned commit" in stderr
    print("  [PASS] test_missing_path")


def test_stale_or_mutated_revision(fixture_dir: Path) -> None:
    repo_dir = fixture_dir / "repo_stale_revision"
    init_git_repo(repo_dir)

    original_code = "def compute():\n    return 42\n"
    c1 = commit_file(repo_dir, "calc.py", original_code, "Commit 1")

    # 1. Mutate worktree without committing
    calc_file = repo_dir / "calc.py"
    calc_file.write_text("def compute():\n    return 999  # mutated in worktree\n", encoding="utf-8")

    # The manifest pins commit C1. The validator must inspect git show C1:path,
    # NOT the mutable worktree!
    manifest_c1 = {
        "commit": c1,
        "components": [
            {
                "id": "calc",
                "name": "Calculator",
                "evidence": [
                    {
                        "path": "calc.py",
                        "start_line": 1,
                        "end_line": 2,
                        "excerpt": original_code.strip()
                    }
                ]
            }
        ]
    }
    res = ae.validate_manifest(manifest_c1, str(repo_dir))
    assert res["valid"] is True, "Validation against pinned commit must ignore uncommitted worktree changes"

    # 2. Non-existent commit hash
    fake_commit = "0" * 40
    manifest_fake = {
        "commit": fake_commit,
        "components": [
            {
                "id": "calc",
                "name": "Calculator",
                "evidence": [
                    {
                        "path": "calc.py",
                        "start_line": 1,
                        "end_line": 2,
                        "excerpt": original_code.strip()
                    }
                ]
            }
        ]
    }
    res_fake = ae.validate_manifest(manifest_fake, str(repo_dir))
    assert res_fake["valid"] is False, "Validation must fail for non-existent commit"
    assert any("not a valid commit object" in e for e in res_fake["errors"])

    # 3. Commit C2 adds new_file.py. Manifest citing C1 for new_file.py must fail (stale revision).
    c2 = commit_file(repo_dir, "new_file.py", "x = 1\n", "Commit 2")
    manifest_stale = {
        "commit": c1,
        "components": [
            {
                "id": "new_comp",
                "name": "New Component",
                "evidence": [
                    {
                        "path": "new_file.py",
                        "start_line": 1,
                        "end_line": 1,
                        "excerpt": "x = 1"
                    }
                ]
            }
        ]
    }
    res_stale = ae.validate_manifest(manifest_stale, str(repo_dir))
    assert res_stale["valid"] is False, "Manifest citing C1 for a file added in C2 must fail"

    print("  [PASS] test_stale_or_mutated_revision")


def test_rename(fixture_dir: Path) -> None:
    repo_dir = fixture_dir / "repo_rename"
    init_git_repo(repo_dir)

    code = "def handle_event():\n    pass\n"
    c1 = commit_file(repo_dir, "handlers/old_event.py", code, "Old handler")

    # Rename file in C2
    rc_mv, _, err_mv = run_cmd(["git", "mv", "handlers/old_event.py", "handlers/new_event.py"], cwd=repo_dir)
    assert rc_mv == 0, f"git mv failed: {err_mv}"
    rc_ci, _, err_ci = run_cmd(["git", "commit", "-m", "Rename handler"], cwd=repo_dir)
    assert rc_ci == 0, f"git commit failed: {err_ci}"
    rc, out, err = run_cmd(["git", "rev-parse", "HEAD"], cwd=repo_dir)
    assert rc == 0, f"git rev-parse HEAD failed: {err}"
    c2 = out.strip()

    # Manifest citing old path at C1 -> PASS
    m_c1 = {
        "commit": c1,
        "components": [
            {
                "id": "handler",
                "name": "Handler",
                "evidence": [
                    {
                        "path": "handlers/old_event.py",
                        "start_line": 1,
                        "end_line": 2,
                        "excerpt": code.strip()
                    }
                ]
            }
        ]
    }
    assert ae.validate_manifest(m_c1, str(repo_dir))["valid"] is True

    # Manifest citing old path at C2 -> FAIL (renamed)
    m_c2_old = {
        "commit": c2,
        "components": [
            {
                "id": "handler",
                "name": "Handler",
                "evidence": [
                    {
                        "path": "handlers/old_event.py",
                        "start_line": 1,
                        "end_line": 2,
                        "excerpt": code.strip()
                    }
                ]
            }
        ]
    }
    assert ae.validate_manifest(m_c2_old, str(repo_dir))["valid"] is False

    # Manifest citing new path at C2 -> PASS
    m_c2_new = {
        "commit": c2,
        "components": [
            {
                "id": "handler",
                "name": "Handler",
                "evidence": [
                    {
                        "path": "handlers/new_event.py",
                        "start_line": 1,
                        "end_line": 2,
                        "excerpt": code.strip()
                    }
                ]
            }
        ]
    }
    assert ae.validate_manifest(m_c2_new, str(repo_dir))["valid"] is True
    print("  [PASS] test_rename")


def test_invalid_span(fixture_dir: Path) -> None:
    repo_dir = fixture_dir / "repo_invalid_span"
    init_git_repo(repo_dir)

    code = "line1\nline2\nline3\nline4\nline5\n"
    commit = commit_file(repo_dir, "sample.txt", code, "Add sample")

    # a) start > end
    m1 = {
        "commit": commit,
        "components": [
            {
                "id": "c1",
                "name": "C1",
                "evidence": [{"path": "sample.txt", "start_line": 4, "end_line": 2, "excerpt": "foo"}]
            }
        ]
    }
    res1 = ae.validate_manifest(m1, str(repo_dir))
    assert res1["valid"] is False
    assert any("cannot be less than start_line" in e for e in res1["errors"])

    # b) start < 1
    m2 = {
        "commit": commit,
        "components": [
            {
                "id": "c2",
                "name": "C2",
                "evidence": [{"path": "sample.txt", "start_line": 0, "end_line": 3, "excerpt": "foo"}]
            }
        ]
    }
    res2 = ae.validate_manifest(m2, str(repo_dir))
    assert res2["valid"] is False
    assert any("start_line must be >= 1" in e for e in res2["errors"])

    # c) end > total lines
    m3 = {
        "commit": commit,
        "components": [
            {
                "id": "c3",
                "name": "C3",
                "evidence": [{"path": "sample.txt", "start_line": 1, "end_line": 999, "excerpt": "foo"}]
            }
        ]
    }
    res3 = ae.validate_manifest(m3, str(repo_dir))
    assert res3["valid"] is False
    assert any("exceeds file line count" in e for e in res3["errors"])

    # d) span exceeds max lines (500)
    m4 = {
        "commit": commit,
        "components": [
            {
                "id": "c4",
                "name": "C4",
                "evidence": [{"path": "sample.txt", "start_line": 1, "end_line": 600, "excerpt": "foo"}]
            }
        ]
    }
    res4 = ae.validate_manifest(m4, str(repo_dir))
    assert res4["valid"] is False
    assert any("exceeds maximum allowed" in e for e in res4["errors"])

    print("  [PASS] test_invalid_span")


def test_false_relation_marked_review_required(fixture_dir: Path) -> None:
    repo_dir = fixture_dir / "repo_false_relation"
    init_git_repo(repo_dir)

    code_fe = "function submitPayment() { return callApi(); }\n"
    code_db = "CREATE TABLE billing (id INT PRIMARY KEY);\n"

    commit_file(repo_dir, "frontend.js", code_fe, "Frontend")
    commit = commit_file(repo_dir, "db.sql", code_db, "Database")

    # A false relation is claimed: frontend writes directly to billing db
    manifest = {
        "commit": commit,
        "components": [
            {
                "id": "frontend",
                "name": "Web Frontend",
                "evidence": [{"path": "frontend.js", "start_line": 1, "end_line": 1, "excerpt": code_fe.strip()}]
            },
            {
                "id": "billing_db",
                "name": "Billing Database",
                "evidence": [{"path": "db.sql", "start_line": 1, "end_line": 1, "excerpt": code_db.strip()}]
            }
        ],
        "relations": [
            {
                "from": "frontend",
                "to": "billing_db",
                "type": "direct_sql_bypass",
                "label": "Direct SQL Access"
            }
        ]
    }

    res = ae.validate_manifest(manifest, str(repo_dir))
    assert res["valid"] is True, "Source locations are valid"

    # Assurance boundary check: semantic assertions are NOT certified
    boundary = res.get("assurance_boundary", {})
    assert boundary.get("semantic_proof") is False, "Validator must never certify semantic truth"
    assert boundary.get("semantic_verification") == "REVIEW_REQUIRED"

    rel = res["relations"][0]
    assert rel["review_required"] is True, "Semantic relations must be explicitly review_required"
    assert rel["semantic_verification"] == "REVIEW_REQUIRED"

    # Mermaid diagram check
    mmd = ae.generate_mermaid_diagram(res)
    assert "[review req]" in mmd, "Mermaid edge must contain review required annotation"
    assert "Assurance Boundary" in mmd, "Mermaid diagram must contain Assurance Boundary legend"

    print("  [PASS] test_false_relation_marked_review_required")


def test_preserve_previous_valid_output(fixture_dir: Path) -> None:
    repo_dir = fixture_dir / "repo_preserve_output"
    init_git_repo(repo_dir)

    code = "val = 12345\n"
    commit = commit_file(repo_dir, "data.py", code, "Add data")

    valid_manifest = {
        "commit": commit,
        "components": [
            {
                "id": "data_comp",
                "name": "Data Component",
                "evidence": [{"path": "data.py", "start_line": 1, "end_line": 1, "excerpt": "val = 12345"}]
            }
        ]
    }
    manifest_valid_file = fixture_dir / "manifest_preserve_v1.json"
    manifest_valid_file.write_text(json.dumps(valid_manifest, indent=2), encoding="utf-8")

    out_file = fixture_dir / "published_diagram.mmd"

    # 1. Run valid render
    rc1, _, _ = run_cmd(
        [
            sys.executable, "-B", str(SCRIPT_DIR / "architecture_evidence.py"),
            "render", str(manifest_valid_file),
            "--repo", str(repo_dir),
            "--output", str(out_file)
        ],
        cwd=fixture_dir
    )
    assert rc1 == 0
    assert out_file.is_file()
    initial_content = out_file.read_text(encoding="utf-8")
    assert "c_data_comp" in initial_content

    # 2. Run invalid render targeting the exact same output path
    invalid_manifest = {
        "commit": commit,
        "components": [
            {
                "id": "data_comp",
                "name": "Data Component",
                "evidence": [{"path": "data.py", "start_line": 1, "end_line": 1, "excerpt": "WRONG_EXCERPT"}]
            }
        ]
    }
    manifest_invalid_file = fixture_dir / "manifest_preserve_v2_invalid.json"
    manifest_invalid_file.write_text(json.dumps(invalid_manifest, indent=2), encoding="utf-8")

    rc2, _, _ = run_cmd(
        [
            sys.executable, "-B", str(SCRIPT_DIR / "architecture_evidence.py"),
            "render", str(manifest_invalid_file),
            "--repo", str(repo_dir),
            "--output", str(out_file)
        ],
        cwd=fixture_dir
    )
    assert rc2 != 0, "Expected second run to fail validation"

    # 3. Assert previous valid file is untouched
    assert out_file.is_file(), "Previous valid file must still exist"
    preserved_content = out_file.read_text(encoding="utf-8")
    assert preserved_content == initial_content, "Previous valid file must be byte-for-byte identical"

    print("  [PASS] test_preserve_previous_valid_output")


def test_adversarial_inputs(fixture_dir: Path) -> None:
    repo_dir = fixture_dir / "repo_adversarial"
    commit = init_git_repo(repo_dir)

    # 1. Path traversal
    m_trav = {
        "commit": commit,
        "components": [
            {
                "id": "c1",
                "name": "Traversal",
                "evidence": [{"path": "../../etc/shadow", "start_line": 1, "end_line": 1, "excerpt": "x"}]
            }
        ]
    }
    res_trav = ae.validate_manifest(m_trav, str(repo_dir))
    assert res_trav["valid"] is False
    assert any("Path traversal detected" in e for e in res_trav["errors"])

    # 2. Absolute path
    m_abs = {
        "commit": commit,
        "components": [
            {
                "id": "c2",
                "name": "AbsPath",
                "evidence": [{"path": "/etc/passwd", "start_line": 1, "end_line": 1, "excerpt": "x"}]
            }
        ]
    }
    res_abs = ae.validate_manifest(m_abs, str(repo_dir))
    assert res_abs["valid"] is False
    assert any("Absolute paths are not allowed" in e for e in res_abs["errors"])

    # 3. Control character in path
    m_ctrl = {
        "commit": commit,
        "components": [
            {
                "id": "c3",
                "name": "CtrlChar",
                "evidence": [{"path": "src/\x00evil.py", "start_line": 1, "end_line": 1, "excerpt": "x"}]
            }
        ]
    }
    res_ctrl = ae.validate_manifest(m_ctrl, str(repo_dir))
    assert res_ctrl["valid"] is False
    assert any("illegal control character" in e for e in res_ctrl["errors"])

    # 5. Relative segment traversal
    m_seg = {
        "commit": commit,
        "components": [
            {
                "id": "c4",
                "name": "SegTraversal",
                "evidence": [{"path": "src/../secret.py", "start_line": 1, "end_line": 1, "excerpt": "x"}]
            }
        ]
    }
    res_seg = ae.validate_manifest(m_seg, str(repo_dir))
    assert res_seg["valid"] is False
    assert any("Path traversal detected" in e for e in res_seg["errors"])

    # 6. Oversized excerpt
    m_big = {
        "commit": commit,
        "components": [
            {
                "id": "c5",
                "name": "BigExcerpt",
                "evidence": [{"path": "hello.py", "start_line": 1, "end_line": 1, "excerpt": "x" * (500 * 1024 + 1)}]
            }
        ]
    }
    res_big = ae.validate_manifest(m_big, str(repo_dir))
    assert res_big["valid"] is False
    assert any("exceeds maximum allowed size" in e for e in res_big["errors"])

    # 7. Mermaid syntax injection in component name
    code = "print('hello')\n"
    c_safe = commit_file(repo_dir, "hello.py", code, "Hello")
    m_inject = {
        "commit": c_safe,
        "components": [
            {
                "id": "comp_a",
                "name": 'Injection"; style comp_a fill:#f00; --',
                "evidence": [{"path": "hello.py", "start_line": 1, "end_line": 1, "excerpt": "print('hello')"}]
            }
        ]
    }
    res_inject = ae.validate_manifest(m_inject, str(repo_dir))
    assert res_inject["valid"] is True
    mmd_inject = ae.generate_mermaid_diagram(res_inject)
    assert "#quot;" in mmd_inject, "Quotes must be entity-escaped to prevent Mermaid injection"
    assert 'Injection"; style' not in mmd_inject, "Raw unescaped quotes must not appear in Mermaid node definition"

    print("  [PASS] test_adversarial_inputs")


# ==============================================================================
# Main Runner
# ==============================================================================

def main() -> int:
    parser = argparse.ArgumentParser(description="Synthetic Git repository acceptance test suite")
    parser.add_argument(
        "--artifact-root",
        default=str(SCRIPT_DIR / '.test-artifacts' / 'architecture'),
        help="Root directory where synthetic fixture repositories and outputs are retained"
    )
    args = parser.parse_args()

    artifact_root = Path(args.artifact_root).resolve()
    artifact_root.mkdir(parents=True, exist_ok=True)

    run_timestamp = f"run_{time.strftime('%Y%m%d_%H%M%S')}_{os.getpid()}_{int(time.time() * 1000) % 1000:03d}"

    print("=" * 70)
    print("Running Architecture Evidence Acceptance Test Suite")
    print(f"Artifact Root: {artifact_root}")
    print(f"Run Session: {run_timestamp}")
    print("Note: Fixtures are permanently retained under artifact root (no rmtree/unlink).")
    print("=" * 70)

    tests = [
        ("Valid Citation", test_valid_citation),
        ("Wrong Quote", test_wrong_quote),
        ("Missing Path", test_missing_path),
        ("Stale/Mutated Revision", test_stale_or_mutated_revision),
        ("Rename Handling", test_rename),
        ("Invalid Span", test_invalid_span),
        ("False Relation Marked Review-Required", test_false_relation_marked_review_required),
        ("Preserve Previous Valid Output", test_preserve_previous_valid_output),
        ("Adversarial Inputs", test_adversarial_inputs),
    ]

    failed = 0
    passed = 0

    for name, test_func in tests:
        case_dir = artifact_root / f"case_{test_func.__name__}"
        case_dir.mkdir(parents=True, exist_ok=True)
        test_dir = case_dir / run_timestamp
        test_dir.mkdir(parents=True, exist_ok=True)
        try:
            print(f"\n[RUN] {name} (directory: {case_dir.name}/{run_timestamp})...")
            test_func(test_dir)
            passed += 1
        except Exception as e:
            print(f"  [FAIL] {name}: {e}")
            import traceback
            traceback.print_exc()
            failed += 1

    print("\n" + "=" * 70)
    print(f"Summary: {passed} passed, {failed} failed out of {len(tests)} tests.")
    print("=" * 70)

    return 1 if failed > 0 else 0


if __name__ == "__main__":
    sys.exit(main())
