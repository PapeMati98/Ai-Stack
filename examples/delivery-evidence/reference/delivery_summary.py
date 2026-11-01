"""Original synthetic delivery-summary CLI.

Reads one UTF-8 JSON file containing an ARRAY of synthetic run records,
summarizes them with delivery_evidence.summarize_runs, and prints
deterministic JSON (sorted keys) to stdout. Synthetic-only; no network,
provider, Office, Hub, subprocess, or account access.
"""
import json
import sys
from pathlib import Path

USAGE = (
    "usage: delivery_summary.py INPUT_FILE\n"
    "\n"
    "Print a deterministic JSON summary of synthetic delivery runs.\n"
    "\n"
    "arguments:\n"
    "  INPUT_FILE  UTF-8 JSON file containing an array of run records\n"
    "\n"
    "options:\n"
    "  -h, --help  show this help message and exit\n"
)


def _fail(message):
    sys.stderr.write("error: %s\n" % message)
    raise SystemExit(2)


def _reject_constant(value):
    raise ValueError("non-standard JSON constant: %s" % value)


def main(argv):
    args = list(argv)
    if len(args) == 1 and args[0] in ("-h", "--help"):
        sys.stdout.write(USAGE)
        return 0
    if len(args) != 1:
        _fail("expected exactly one input file (array of records)")
    input_path = Path(args[0])
    try:
        text = input_path.read_text(encoding="utf-8")
    except (OSError, ValueError, UnicodeError):
        _fail("cannot read input file as UTF-8 text")
    try:
        payload = json.loads(text, parse_constant=_reject_constant)
    except (ValueError, TypeError, RecursionError):
        _fail("input file is not valid JSON")
    if not isinstance(payload, list):
        _fail("input JSON must be an array of records")
    try:
        from delivery_evidence import summarize_runs

        summary = summarize_runs(payload)
    except (ValueError, TypeError):
        _fail("input records are invalid")
    try:
        output = json.dumps(summary, sort_keys=True, ensure_ascii=False, allow_nan=False) + "\n"
    except (ValueError, TypeError, RecursionError):
        _fail("summary cannot be represented as JSON")
    try:
        sys.stdout.write(output)
    except (OSError, ValueError):
        _fail("cannot write summary to stdout")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
