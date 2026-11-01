from __future__ import annotations

import json
import stat
import subprocess
import sys
from pathlib import Path
from typing import Any


MAX_FILES = 10_000
MAX_FILE_BYTES = 5_000_000
MAX_TOTAL_BYTES = 100_000_000


def emit(status: str, checks: list[dict[str, Any]], error: str | None = None) -> int:
    payload = {"status": status, "checks": checks}
    if error:
        payload["error"] = error[:2000]
    print(json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":")))
    return 0 if status == "PASS" else 1


def is_link(path: Path) -> bool:
    metadata = path.lstat()
    if path.is_symlink():
        return True
    is_junction = getattr(path, "is_junction", None)
    if callable(is_junction) and is_junction():
        return True
    attributes = getattr(metadata, "st_file_attributes", 0)
    return bool(attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0))


def tracked_python_files(root: Path) -> list[str]:
    result = subprocess.run(
        ["git", "ls-files", "-z", "--", "*.py"],
        cwd=root,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.decode("utf-8", errors="replace")[:1000])
    return [item.decode("utf-8") for item in result.stdout.split(b"\0") if item]


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    checks: list[dict[str, Any]] = []
    if len(args) != 1:
        return emit("FAIL", checks, "Expected exactly one candidate worktree path")
    root = Path(args[0]).expanduser().resolve()
    if not root.is_dir() or is_link(root):
        return emit("FAIL", checks, "Candidate worktree must be a plain directory")
    try:
        relative_paths = tracked_python_files(root)
    except (OSError, RuntimeError, UnicodeError) as exc:
        return emit("FAIL", checks, f"Cannot enumerate tracked Python files: {exc}")
    if not relative_paths:
        return emit("FAIL", checks, "No tracked Python files found")
    if len(relative_paths) > MAX_FILES:
        return emit("FAIL", checks, f"Python file count exceeds {MAX_FILES}")

    total_bytes = 0
    failures: list[dict[str, str]] = []
    for relative in relative_paths:
        candidate = (root / relative).resolve()
        try:
            candidate.relative_to(root)
            if not candidate.is_file() or is_link(candidate):
                raise ValueError("not a plain file")
            size = candidate.stat().st_size
            if size > MAX_FILE_BYTES:
                raise ValueError(f"file exceeds {MAX_FILE_BYTES} bytes")
            total_bytes += size
            if total_bytes > MAX_TOTAL_BYTES:
                raise ValueError(f"total source exceeds {MAX_TOTAL_BYTES} bytes")
            compile(candidate.read_bytes(), relative, "exec", dont_inherit=True)
        except (OSError, SyntaxError, UnicodeError, ValueError) as exc:
            failures.append({"path": relative, "error": str(exc)[:500]})
            if len(failures) >= 50:
                break

    checks.append(
        {
            "name": "tracked-python-syntax",
            "status": "FAIL" if failures else "PASS",
            "file_count": len(relative_paths),
            "total_bytes": total_bytes,
            "failures": failures,
        }
    )
    return emit("FAIL" if failures else "PASS", checks)


if __name__ == "__main__":
    raise SystemExit(main())
