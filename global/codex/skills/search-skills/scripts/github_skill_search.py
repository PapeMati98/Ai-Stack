#!/usr/bin/env python3
"""Search GitHub repositories for AI-agent skill candidates.

This is a discovery helper only. It does not download or trust anything.
"""

from __future__ import annotations

import argparse
import json
import sys
from urllib.parse import quote_plus
from urllib.request import Request, urlopen


API = "https://api.github.com/search/repositories"


def fetch_json(url: str) -> dict:
    req = Request(
        url,
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": "Codex skill discovery helper",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    with urlopen(req, timeout=30) as response:
        return json.loads(response.read().decode("utf-8"))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("query", help="capability to search for, e.g. 'frontend design'")
    parser.add_argument("--limit", type=int, default=10)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    q = f"{args.query} claude skill SKILL.md"
    url = f"{API}?q={quote_plus(q)}&sort=stars&order=desc&per_page={min(max(args.limit, 1), 30)}"
    try:
        data = fetch_json(url)
    except Exception as exc:  # noqa: BLE001 - CLI should report simple errors
        print(f"GitHub search failed: {exc}", file=sys.stderr)
        return 2

    candidates = []
    for item in data.get("items", [])[: args.limit]:
        candidates.append(
            {
                "full_name": item.get("full_name"),
                "html_url": item.get("html_url"),
                "clone_url": item.get("clone_url"),
                "description": item.get("description"),
                "stars": item.get("stargazers_count"),
                "forks": item.get("forks_count"),
                "updated_at": item.get("updated_at"),
                "pushed_at": item.get("pushed_at"),
                "language": item.get("language"),
                "license": (item.get("license") or {}).get("spdx_id"),
            }
        )

    if args.json:
        print(json.dumps(candidates, ensure_ascii=False, indent=2))
    else:
        for i, c in enumerate(candidates, start=1):
            print(f"{i}. {c['full_name']} ({c['stars']} stars, updated {c['updated_at']})")
            print(f"   {c['html_url']}")
            if c.get("description"):
                print(f"   {c['description']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
