"""Owner CLI for a persisted, synthetic-only DOTS pilot."""

import argparse
import json
import os
from pathlib import Path
import sqlite3
import sys
import time

from .adapters import SyntheticSource, SyntheticModel, SyntheticTarget, SyntheticOffice
from .engine import Engine, identity, template_config, timestamp
from .hermes import profile_plan, supervisor_plan
from .store import Store


def parser():
    p = argparse.ArgumentParser(description="DOTS staged candidate: synthetic adapters only; integer microUSD.")
    p.add_argument("--state", required=True, help="Absolute private runtime directory (outside source code).")
    p.add_argument("--source", help="Synthetic JSON fixture; override source for this invocation.")
    p.add_argument("--target-state", help="Absolute synthetic target directory; keep outside controller backups.")
    sub = p.add_subparsers(dest="command", required=True)
    init = sub.add_parser("init", help="Explicitly configure the synthetic rolling budget; preserves holds/spend.")
    init.add_argument("--global-cap", required=True, type=int)
    init.add_argument("--emergency-reserve", type=int, default=0)
    init.add_argument("--now", type=float)
    create = sub.add_parser("create", help="Create an observer/collector template; existing identities never replaced.")
    create.add_argument("dot_id")
    create.add_argument("--kind", choices=("observer", "collector"), required=True)
    create.add_argument("--org", required=True)
    create.add_argument("--project", required=True)
    create.add_argument("--budget", type=int, default=2000000, help="Synthetic per-DOTS rolling cap, microUSD.")
    create.add_argument("--interval", type=float, default=900)
    create.add_argument("--max-call", type=int, default=10)
    create.add_argument("--max-queue", type=int, default=100)
    create.add_argument("--max-payload", type=int, default=16384)
    create.add_argument("--max-records", type=int, default=1000)
    create.add_argument("--timezone", default="UTC", help="Only UTC interval schedules supported; daily/DST unqualified.")
    create.add_argument("--model", choices=("synthetic",), default="synthetic")
    for name in ("tick", "status", "reconcile"):
        cmd = sub.add_parser(name, help={"tick": "Poll once per due DOTS; missed intervals coalesce.",
                                        "status": "Neutral metadata only; no private input/memory/effect payloads.",
                                        "reconcile": "Lookup uncertain drafts; never reissue writes."}[name])
        cmd.add_argument("--dot")
        if name != "status":
            cmd.add_argument("--now", type=float, help="Explicit synthetic UTC clock for reproducible UAT.")
        if name == "tick":
            cmd.add_argument("--force", action="store_true", help="Explicitly bypass cadence once; STOP/lease/caps still apply.")
        if name == "reconcile":
            cmd.add_argument("--reservation")
            cmd.add_argument("--actual-cost", type=int, help="Explicit measured synthetic cost; requires --dot and --reservation.")
    for name in ("pause", "resume"):
        cmd = sub.add_parser(name, help="Persist STOP independently of models." if name == "pause" else "Explicit owner resume; old writer remains fenced.")
        cmd.add_argument("dot_id")
    for name in ("memory", "correct"):
        cmd = sub.add_parser(name, help="Owner private memory; output shows metadata only.")
        cmd.add_argument("dot_id")
        cmd.add_argument("--memory-id")
        cmd.add_argument("--text-file", help="UTF-8 private text file; no content in stdout.")
        cmd.add_argument("--source-ref", default="owner")
        cmd.add_argument("--now", type=float)
        if name == "memory":
            cmd.add_argument("--query", help="Retrieve matching record metadata only.")
    plan = sub.add_parser("hermes-plan", help="Print declarative NEW profile candidate; never runs Hermes.")
    plan.add_argument("--dot")
    plan.add_argument("--base", help="Absolute proposed new profile root.")
    sub.add_parser("demo", help="Create and tick two synthetic DOTS once; fixtures and targets retained.")
    return p


def absolute(value):
    path = Path(value)
    if not path.is_absolute():
        raise ValueError("absolute_path_required")
    return path.resolve()


def runtime(store, state, args):
    sources = {}
    for dot in store.list_dots():
        source = args.source or dot["config"].get("source_path") or str(state / "sources" / (dot["id"] + ".json"))
        sources[dot["id"]] = SyntheticSource(absolute(source))
    targets = absolute(args.target_state) if args.target_state else state / "targets"
    now = getattr(args, "now", None)
    clock = time.time if now is None else lambda: timestamp(now)
    return Engine(store, sources, SyntheticModel(), SyntheticTarget(targets / "drafts.sqlite3"),
                  SyntheticOffice(targets / "office.sqlite3"), clock=clock)


def private_text(path, maximum):
    source = absolute(path)
    if source.stat().st_size > maximum:
        raise ValueError("payload_limit")
    return source.read_text(encoding="utf-8")


def dispatch(args):
    state = absolute(args.state)
    # Python 3.13's Windows 0700 ACL excludes restricted execution tokens.
    # Windows ACL isolation is an external qualification, not chmod semantics.
    state.mkdir(mode=0o700 if os.name != "nt" else 0o777, parents=True, exist_ok=True)
    store = Store(state / "controller.sqlite3")
    cmd = args.command
    if cmd == "init":
        store.configure_budget(args.global_cap, args.emergency_reserve, args.now)
        return {"candidate": True, "budget": store.budget_status()}
    if cmd == "create":
        config = template_config(args.kind, args.org, args.project, args.budget,
                                 interval_seconds=args.interval, max_call_micro_usd=args.max_call,
                                 max_queue=args.max_queue, max_payload_bytes=args.max_payload,
                                 max_state_records=args.max_records, timezone=args.timezone)
        if args.source:
            config["source_path"] = str(absolute(args.source))
        dot = store.create_dot(args.dot_id, config)
        return {"dot_id": dot["id"], "kind": args.kind, "model": "synthetic", "candidate": True}
    if cmd in ("pause", "resume"):
        store.pause(args.dot_id, cmd == "pause")
        return {"dot_id": args.dot_id, "paused": cmd == "pause"}
    if cmd in ("memory", "correct"):
        config = store.get_dot(args.dot_id)["config"]
        if cmd == "memory" and args.query is not None:
            if args.text_file or args.memory_id:
                raise ValueError("ambiguous_memory_command")
            records = store.retrieve(args.dot_id, args.query)
            return {"dot_id": args.dot_id, "records": [{"id": row["id"], "revision": row["revision"]} for row in records]}
        if not args.memory_id or not args.text_file:
            raise ValueError("memory_id_and_text_file_required")
        text = private_text(args.text_file, config["max_payload_bytes"])
        if cmd == "correct":
            row = store.correct(args.dot_id, args.memory_id, text, args.source_ref)
        else:
            row = store.remember(args.dot_id, args.memory_id, text, args.source_ref,
                                 time.time() if args.now is None else timestamp(args.now))
        return {"dot_id": row["dot_id"], "memory_id": row["id"], "revision": row["revision"]}
    if cmd == "hermes-plan":
        base = str(absolute(args.base)) if args.base else str(state / "hermes-candidates")
        if args.dot:
            dot = store.get_dot(args.dot)
            return profile_plan({**dot["config"], "id": dot["id"]}, base)
        return supervisor_plan(base)
    if cmd == "demo":
        if store.list_dots():
            raise ValueError("demo_requires_new_state")
        store.configure_budget(5000000, 500000)
        for kind in ("observer", "collector"):
            path = state / "sources" / (kind + ".json")
            path.parent.mkdir(parents=True, exist_ok=True)
            inv = {"org": "pilot-org", "number": "SYN-1", "date": "2026-10-08", "amount": "42.00", "currency": "PLN"}
            decision = {"action": "alert" if kind == "observer" else "collect", "reason_code": "relevant" if kind == "observer" else "invoice"}
            item = {"id": "synthetic-one", "org": "pilot-org", "content": "Synthetic pilot evidence.", "model_decision": decision}
            if kind == "collector":
                item["invoice"] = inv
            # 'x' avoids replacing retained fixtures from an interrupted demo.
            with path.open("x", encoding="utf-8") as stream:
                json.dump([item], stream)
            config = template_config(kind, "pilot-org", "pilot", source_path=str(path))
            store.create_dot(kind, config)
            store.remember(kind, "purpose", "Synthetic pilot evidence only.", "owner", time.time())
        engine = runtime(store, state, args)
        return {"ticks": engine.tick(), "status": engine.status()}
    engine = runtime(store, state, args)
    if cmd == "tick":
        return {"ticks": engine.tick(args.dot, force=args.force)}
    if cmd == "status":
        return engine.status(args.dot)
    if cmd == "reconcile":
        if args.reservation is not None or args.actual_cost is not None:
            if not args.dot or not args.reservation or args.actual_cost is None:
                raise ValueError("cost_reconciliation_arguments_required")
            return engine.reconcile_cost(args.dot, args.reservation, args.actual_cost)
        ids = [args.dot] if args.dot else [dot["id"] for dot in store.list_dots()]
        return {"results": [engine.reconcile(identity(key)) for key in ids]}
    raise ValueError("unknown_command")


def main(argv=None):
    try:
        args = parser().parse_args(argv)
        result = dispatch(args)
        print(json.dumps(result, sort_keys=True, ensure_ascii=True, allow_nan=False))
        return 0
    except Exception:
        # Fixed code; paths, payloads, provider errors and tracebacks stay private.
        print('{"error":"COMMAND_REJECTED","candidate":true}', file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
