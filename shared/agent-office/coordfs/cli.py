"""Mirrored coord CLI. JSON is the default; --json is the stable explicit flag."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from .constants import DEFAULT_TTL_S, VERSION
from .doctor import doctor
from .errors import CoordError
from .identity import agent_identity
from .leases import CoordBoard
from .office import detect_secret


def _json(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def _text(payload: dict[str, Any]) -> str:
    status = payload.get("status")
    code = payload.get("code")
    if code == "held":
        return (
            f"WARNING held by {payload.get('holder_agent')} "
            f"ttl_hint_s={payload.get('ttl_hint_s')} expires={payload.get('expires_at')}\n"
        )
    if status == "ok":
        resource = payload.get("resource_id")
        holder = payload.get("holder_agent")
        return f"{code} resource={resource} holder={holder}\n"
    return f"{status} {code} {payload.get('message', '')}\n".rstrip() + "\n"


def _exit_code(payload: dict[str, Any]) -> int:
    status = payload.get("status")
    if status in {"ok", "warning"}:
        return 0
    if status == "conflict":
        return 2
    return 1


def _scan_meta_secret(value: Any) -> None:
    """Recursively refuse secret-shaped strings in meta keys and values.

    Nested dicts and lists are walked so a secret under a second-level key or
    inside a list is refused exactly like a first-level string.  The refusal
    message never echoes the key or value: both are unverified caller input and
    may themselves carry the secret shape.
    """
    if isinstance(value, str):
        secret = detect_secret(value)
        if secret is not None:
            raise CoordError(
                f"--meta matches a high-confidence secret pattern ({secret}); refused before write",
                code="secret_detected",
            )
    elif isinstance(value, dict):
        for key, item in value.items():
            _scan_meta_secret(key)
            _scan_meta_secret(item)
    elif isinstance(value, list):
        for item in value:
            _scan_meta_secret(item)


def _meta_from_args(args: argparse.Namespace) -> dict[str, Any]:
    """Parse the ``--meta`` JSON object and refuse secret-shaped values at write time.

    The lease meta lands on disk exactly like body, status, or workspace on the
    presence lease, and the same Office detector (``detect_secret``) covers it
    so patterns do not have to be duplicated here.  The scan runs on the raw
    meta text, recursively on every decoded key and value, and on the decoded
    object re-serialized with ``ensure_ascii=False``.  JSON escaping alone is
    enough to hide a high-confidence shape from the raw text, and a Unicode
    escape in a key or a value hides the assignment that exists only once the
    two are written together again, so no scan alone is sufficient.  Read paths
    stay untouched: this is a write-side rule and a lease already on disk is
    never rejudged.
    """
    meta: dict[str, Any] = {}
    if args.meta:
        secret = detect_secret(args.meta)
        if secret is not None:
            raise CoordError(
                f"--meta matches a high-confidence secret pattern ({secret}); refused before write",
                code="secret_detected",
            )
        loaded = json.loads(args.meta)
        if not isinstance(loaded, dict):
            raise CoordError("--meta must be a JSON object", code="invalid_meta")
        for key in loaded:
            if not isinstance(key, str):
                raise CoordError("--meta keys must be strings", code="invalid_meta")
        _scan_meta_secret(loaded)
        secret = detect_secret(json.dumps(loaded, ensure_ascii=False))
        if secret is not None:
            raise CoordError(
                f"--meta matches a high-confidence secret pattern ({secret}); refused before write",
                code="secret_detected",
            )
        meta.update(loaded)
    return meta


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="coord",
        description="Filesystem-backed coordination board. Warnings only; no deny hook.",
    )
    parser.add_argument("--json", action="store_true", default=True, help="Machine-readable JSON (default, stable flag).")
    parser.add_argument("--text", action="store_true", help="Human-readable text instead of JSON.")
    parser.add_argument("--home", type=Path, help="Override runtime home (default %%USERPROFILE%%/.agents/coord).")
    parser.add_argument("--cwd", type=Path, help="Working directory used to resolve filesystem paths.")
    parser.add_argument("--version", action="version", version=f"coordfs {VERSION}")
    sub = parser.add_subparsers(dest="command", required=True)

    def add_resource(target: argparse.ArgumentParser, with_agent: bool) -> None:
        target.add_argument("resource_pos", nargs="?", help="Resource identifier (path, write-scope prefix, or data:<id>).")
        target.add_argument("--resource", help="Resource identifier (stable flag; overrides positional).")
        target.add_argument("--kind", choices=("path", "scope", "data"), help="Force resource identity kind.")
        if with_agent:
            target.add_argument(
                "--harness",
                "--agent-id",
                dest="harness",
                required=True,
                help="Harness name. --agent-id is retained as a compatibility alias.",
            )
            target.add_argument(
                "--session-id",
                help="Runtime session id. Prefer COORDFS_SESSION_ID set once by the adapter.",
            )

    claim = sub.add_parser("claim", help="Atomically create a lease or emit a WARNING if held.")
    add_resource(claim, True)
    claim.add_argument("--ttl", type=int, default=DEFAULT_TTL_S, help="Lease TTL in seconds.")
    claim.add_argument("--task-id", help="Optional task lineage stored in meta.")
    claim.add_argument("--node-id", help="Optional node lineage stored in meta.")
    claim.add_argument("--graph-id", help="Optional graph lineage stored in meta.")
    claim.add_argument("--run-id", help="Optional run lineage stored in meta.")
    claim.add_argument("--meta", help="JSON object merged into lease meta.")

    release = sub.add_parser("release", help="Release a held lease if the caller is the holder.")
    add_resource(release, True)

    renew = sub.add_parser("renew", help="Refresh TTL for a held lease.")
    add_resource(renew, True)
    renew.add_argument("--ttl", type=int, help="Replacement TTL in seconds.")

    sub.add_parser("ls", help="List active leases after a lazy sweep.")

    who = sub.add_parser("who", help="Show the current holder of a resource.")
    add_resource(who, False)

    sub.add_parser("sweep", help="Reclaim expired leases.")
    sub.add_parser("doctor", help="Report runtime health. Advisory only.")
    return parser


def _resource(args: argparse.Namespace) -> str:
    value = getattr(args, "resource", None) or getattr(args, "resource_pos", None)
    if not value:
        raise CoordError("resource is required", code="invalid_resource")
    return str(value)


def _identity(args: argparse.Namespace) -> dict[str, str]:
    return agent_identity(args.harness, session_id=args.session_id)


def dispatch(args: argparse.Namespace) -> dict[str, Any]:
    board = CoordBoard(home=args.home, cwd=args.cwd)
    command = args.command
    if command == "claim":
        identity = _identity(args)
        meta = board.lineage_meta(
            _meta_from_args(args),
            task_id=args.task_id,
            node_id=args.node_id,
            graph_id=args.graph_id,
            run_id=args.run_id,
        )
        meta["agent_identity"] = {
            key: identity[key]
            for key in ("harness", "host", "session", "strength", "source")
        }
        return board.claim(_resource(args), identity["agent_id"], ttl_s=args.ttl, meta=meta, kind=args.kind)
    if command == "release":
        identity = _identity(args)
        return board.release(
            _resource(args),
            identity["agent_id"],
            kind=args.kind,
            caller_strength=identity.get("strength"),
        )
    if command == "renew":
        identity = _identity(args)
        return board.renew(
            _resource(args),
            identity["agent_id"],
            ttl_s=args.ttl,
            kind=args.kind,
            caller_strength=identity.get("strength"),
        )
    if command == "ls":
        return board.ls()
    if command == "who":
        return board.who(_resource(args), kind=args.kind)
    if command == "sweep":
        return board.sweep()
    if command == "doctor":
        return doctor(board)
    raise CoordError(f"unknown command: {command}", code="invalid_command")


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        payload = dispatch(args)
    except CoordError as exc:
        payload = {"status": "error", "code": exc.code, "message": str(exc), **exc.payload}
    except json.JSONDecodeError as exc:
        payload = {"status": "error", "code": "invalid_meta", "message": f"invalid JSON: {exc}"}
    text_mode = bool(getattr(args, "text", False))
    stream = sys.stdout
    if text_mode:
        stream.write(_text(payload))
    else:
        stream.write(_json(payload))
    return _exit_code(payload)
