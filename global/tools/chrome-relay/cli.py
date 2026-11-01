"""Bounded CLI fallback for clients without native MCP. No shell execution."""

import argparse
import json
import os
import re
import sys
from collections import deque
from pathlib import Path

from broker import MAX_BODY, Reject, exact, json_bytes, parse_json
from download_finalizer import finalize_download
from mcp import Client


def line_count(value):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9]{1,4}", value) or not 1 <= int(value) <= 1000:
        raise argparse.ArgumentTypeError("tail count must be an integer from 1 to 1000")
    return int(value)


def logs(path, count):
    if type(count) is not int or not 1 <= count <= 1000:
        raise Reject("bound")
    # Cap disk IO, even for a giant or attacker-selected file. Only audit schema
    # fields are emitted; paths, URLs and arbitrary raw lines are not echoed.
    with Path(path).open("rb") as stream:
        stream.seek(0, 2)
        start = max(0, stream.tell() - 1024 * 1024)
        stream.seek(start)
        raw = stream.read(1024 * 1024)
    lines = raw.splitlines()
    if start and lines:
        lines = lines[1:]
    result = []
    for line in deque(lines, maxlen=count):
        if len(line) > 1024:
            continue
        try:
            entry = parse_json(line)
            exact(entry, ("event", "status", "op", "count"), ("request_id", "session_id", "tab_id"))
            if entry["event"] not in ("queued", "dispatched", "result", "extension_attach", "extension_stop", "extension_disconnect", "connection_error") or entry["status"] not in ("pending", "ok", "denied", "unknown") or entry["op"] not in ("", "chrome_attachment", "chrome_open", "chrome_read", "chrome_click", "chrome_type", "chrome_wait", "chrome_close"):
                continue
            if type(entry["count"]) is not int or not 0 <= entry["count"] <= 10000:
                continue
            if any(not isinstance(entry[key], str) or not re.fullmatch(r"[a-f0-9]{16}", entry[key]) for key in ("request_id", "session_id") if key in entry):
                continue
            if "tab_id" in entry and (type(entry["tab_id"]) is not int or not 0 <= entry["tab_id"] <= 2147483647):
                continue
            result.append(entry)
        except (Reject, TypeError, ValueError):
            continue
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description="Scoped Chrome MCP fallback and metadata diagnostics")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("call", help="Read {name,arguments} JSON from stdin")
    sub.add_parser("status", help="Observe attachment for the assigned native session")
    tail = sub.add_parser("logs", help="Read bounded sanitized audit metadata")
    tail.add_argument("count", type=line_count, nargs="?", default=20)
    tail.add_argument("--audit", default=os.environ.get("CHROME_RELAY_AUDIT_FILE"))
    dl = sub.add_parser("finalize-download", help="Finalize a verified Chrome GUID.tmp or named export without deleting files")
    dl.add_argument("--download-dir", required=True)
    dl.add_argument("--expected-name", required=True)
    dl.add_argument("--output-dir", default=None)
    dl.add_argument("--timeout", type=float, default=15.0)
    args = parser.parse_args(argv)
    try:
        if args.command == "logs":
            if not args.audit:
                raise Reject("audit_config")
            result = {"events": logs(args.audit, args.count)}
        elif args.command == "finalize-download":
            result = finalize_download(
                args.download_dir,
                args.expected_name,
                timeout_s=args.timeout,
                output_dir=args.output_dir,
            )
        else:
            client = Client.from_environment()
            if args.command == "status":
                name, arguments = "chrome_attachment", {}
            else:
                raw = sys.stdin.buffer.read(MAX_BODY + 1)
                value = parse_json(raw)
                exact(value, ("name",), ("arguments",))
                name, arguments = value["name"], value.get("arguments", {})
            result = client.call(name, arguments)
        sys.stdout.buffer.write(json_bytes(result) + b"\n")
        return 0 if result.get("status", "ok") == "ok" else 1
    except (Reject, ValueError, OSError, TypeError):
        sys.stdout.buffer.write(b'{"status":"denied","data":{"reason":"invalid_input"}}\n')
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
