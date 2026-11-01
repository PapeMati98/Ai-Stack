#!/usr/bin/env python3
"""Clone a GitHub repository into quarantine without submodules.

The clone is for static inspection only. Do not run code from the output.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path


GITHUB_RE = re.compile(r"^https://github\.com/([^/]+)/([^/#?]+)")


def safe_name(url: str) -> str:
    match = GITHUB_RE.match(url)
    if not match:
        raise ValueError("Only https://github.com/owner/repo URLs are supported")
    owner, repo = match.groups()
    repo = repo.removesuffix(".git")
    return re.sub(r"[^A-Za-z0-9_.-]+", "-", f"{owner}-{repo}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("url")
    parser.add_argument("--out", type=Path, default=Path("work") / "skill-quarantine")
    args = parser.parse_args()

    dest_root = args.out.resolve()
    dest_root.mkdir(parents=True, exist_ok=True)
    dest = dest_root / f"{safe_name(args.url)}-{datetime.now().strftime('%Y%m%d-%H%M%S')}"

    cmd = [
        "git",
        "clone",
        "--depth",
        "1",
        "--no-tags",
        "--filter=blob:none",
        args.url,
        str(dest),
    ]
    try:
        subprocess.run(cmd, check=True)
        commit = subprocess.check_output(["git", "-C", str(dest), "rev-parse", "HEAD"], text=True).strip()
    except subprocess.CalledProcessError as exc:
        print(f"clone failed: {exc}", file=sys.stderr)
        return 2

    print(str(dest))
    print(f"commit {commit}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
