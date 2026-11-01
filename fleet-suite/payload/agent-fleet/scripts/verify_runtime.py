from __future__ import annotations

import argparse
import hashlib
import json
import stat
import sys
from pathlib import Path
from typing import Any


RUNTIME_MARKER = ".agent-fleet-suite-runtime.json"
IGNORED_DIRS = {"__pycache__", ".ruff_cache", ".pytest_cache"}
EXECUTABLE_SOURCE_SUFFIXES = {
    ".bat",
    ".cmd",
    ".com",
    ".cjs",
    ".exe",
    ".js",
    ".mjs",
    ".ps1",
    ".py",
    ".sh",
}
IGNORED_SUFFIXES = {".pyc", ".pyo"}


class VerificationError(RuntimeError):
    pass


def canonical_json(value: Any) -> bytes:
    return (
        json.dumps(value, ensure_ascii=True, sort_keys=True, indent=2) + "\n"
    ).encode("ascii")


def is_filesystem_link(path: Path) -> bool:
    metadata = path.lstat()
    if path.is_symlink():
        return True
    is_junction = getattr(path, "is_junction", None)
    if callable(is_junction) and is_junction():
        return True
    attributes = getattr(metadata, "st_file_attributes", 0)
    return bool(attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0))


def runtime_manifest(root: Path) -> tuple[list[dict[str, Any]], str]:
    if not root.is_dir() or is_filesystem_link(root):
        raise VerificationError(f"Runtime root is not a plain directory: {root}")
    rows: list[dict[str, Any]] = []
    for path in sorted(root.rglob("*"), key=lambda item: item.as_posix().casefold()):
        relative = path.relative_to(root)
        if ".pytest_cache" in relative.parts:
            if is_filesystem_link(path):
                raise VerificationError(f"Linked runtime path is refused: {path}")
            if (
                path.is_file()
                and path.suffix.casefold() in EXECUTABLE_SOURCE_SUFFIXES
            ):
                raise VerificationError(
                    "Executable source is present in an ignored cache directory"
                )
            continue
        if any(part in IGNORED_DIRS for part in relative.parts):
            continue
        if path.name == RUNTIME_MARKER or path.suffix.casefold() in IGNORED_SUFFIXES:
            continue
        if is_filesystem_link(path):
            raise VerificationError(f"Linked runtime path is refused: {path}")
        if path.is_file():
            payload = path.read_bytes()
            rows.append(
                {
                    "path": relative.as_posix(),
                    "size": len(payload),
                    "sha256": hashlib.sha256(payload).hexdigest(),
                }
            )
    return rows, hashlib.sha256(canonical_json(rows)).hexdigest()


def verify(root: Path, expected_fingerprint: str, package_version: str) -> dict[str, Any]:
    marker_path = root / RUNTIME_MARKER
    try:
        marker = json.loads(marker_path.read_text(encoding="ascii"))
    except (OSError, json.JSONDecodeError) as exc:
        raise VerificationError(f"Cannot read runtime marker: {exc}") from exc
    if not isinstance(marker, dict):
        raise VerificationError("Runtime marker is not an object")
    rows, fingerprint = runtime_manifest(root)
    passed = (
        marker.get("managed_by") == "agent-fleet-suite-v2-installer"
        and marker.get("package_version") == package_version
        and marker.get("payload_fingerprint") == expected_fingerprint
        and marker.get("payload_file_count") == len(rows)
        and fingerprint == expected_fingerprint
    )
    if not passed:
        raise VerificationError("Runtime marker or payload fingerprint mismatch")
    return {
        "action": "verify-runtime",
        "status": "PASS",
        "runtime_root": str(root.resolve()),
        "package_version": package_version,
        "payload_file_count": len(rows),
        "payload_fingerprint": fingerprint,
        "writes_performed": 0,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Verify the installed Agent Fleet suite before execution."
    )
    parser.add_argument(
        "--runtime-root",
        type=Path,
        default=Path(__file__).resolve().parents[2],
    )
    parser.add_argument("--expected-fingerprint", required=True)
    parser.add_argument("--package-version", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        result = verify(
            args.runtime_root.expanduser().resolve(),
            args.expected_fingerprint,
            args.package_version,
        )
        sys.stdout.buffer.write(canonical_json(result))
        return 0
    except (OSError, VerificationError) as exc:
        sys.stderr.buffer.write(
            canonical_json(
                {
                    "action": "verify-runtime",
                    "status": "FAIL",
                    "error": str(exc),
                    "writes_performed": 0,
                }
            )
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
