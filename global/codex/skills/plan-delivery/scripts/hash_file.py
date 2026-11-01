#!/usr/bin/env python3
"""Print SHA-256 of a file. Used to refresh Agent Graph planner pins."""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path


def main() -> int:
    if len(sys.argv) != 2:
        print(json.dumps({"status": "ERROR", "error": "usage: hash_file.py <path>"}))
        return 1
    path = Path(sys.argv[1])
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    print(json.dumps({"status": "HASHED", "path": str(path), "sha256": digest.hexdigest()}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
