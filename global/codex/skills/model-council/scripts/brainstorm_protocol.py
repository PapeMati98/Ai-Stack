"""Deterministic, network-free helpers for the model-council brainstorm protocol."""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Iterable


class BrainstormProtocolError(ValueError):
    pass


def require_round2_capacity(successful_round1: int, minimum: int) -> None:
    if minimum < 1:
        raise BrainstormProtocolError("minimum must be positive")
    if successful_round1 < minimum:
        raise BrainstormProtocolError(
            f"BRAINSTORM_INSUFFICIENT_ROUND1: {successful_round1}/{minimum}"
        )


def anonymize_bundle(responses: Iterable[dict], redact: Iterable[str] = ()) -> str:
    rows = list(responses)
    if not rows:
        raise BrainstormProtocolError("BRAINSTORM_EMPTY_ROUND1")
    forbidden = [value for value in redact if value and value.strip()]
    pattern = None
    if forbidden:
        pattern = re.compile("|".join(re.escape(value) for value in sorted(forbidden, key=len, reverse=True)), re.I)
    parts = []
    for index, row in enumerate(rows, start=1):
        content = row.get("content") if isinstance(row, dict) else None
        if not isinstance(content, str) or not content.strip():
            raise BrainstormProtocolError(f"BRAINSTORM_EMPTY_RESPONSE: {index}")
        if pattern:
            content = pattern.sub("[USUNIETO]", content)
        parts.append(f"## POMYSL {index:02d}\n\n{content.strip()}")
    return "\n\n".join(parts)


def main() -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    anonymize = sub.add_parser("anonymize")
    anonymize.add_argument("--input", required=True)
    anonymize.add_argument("--output", required=True)
    anonymize.add_argument("--redact", action="append", default=[])
    args = parser.parse_args()
    if args.command == "anonymize":
        rows = json.loads(Path(args.input).read_text(encoding="utf-8-sig"))
        bundle = anonymize_bundle(rows, args.redact)
        Path(args.output).write_text(json.dumps({"schema_version": 1, "bundle": bundle}, ensure_ascii=False, indent=2), encoding="utf-8")
        return 0
    return 2


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (BrainstormProtocolError, json.JSONDecodeError, OSError) as exc:
        print(f"BRAINSTORM_HELPER_ERROR: {exc}")
        raise SystemExit(2)
