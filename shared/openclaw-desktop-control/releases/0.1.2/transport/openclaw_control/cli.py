"""Executable JSON CLI for the openclaw_control candidate.

Every command prints exactly one versioned JSON envelope to stdout.
Diagnostics carry references/hashes only -- never raw owner text (use
the explicit ``readback`` receiver path) and never tokens. The bot token
is read ONLY from the caller-injected environment (TELEGRAM_BOT_TOKEN);
no CLI option accepts it.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time

from openclaw_control import models
from openclaw_control.adapters import FakeControlAdapter
from openclaw_control.store import ControlStore
from openclaw_control import delivery as delivery_mod
from openclaw_control import pairing as pairing_mod
from openclaw_control import status_text as status_mod
from openclaw_control import telegram as telegram_mod


def _now() -> float:
    return time.time()


def _store(db: str) -> ControlStore:
    return ControlStore(db)


def _emit(kind: str, payload: dict) -> int:
    json.dump(models.envelope(kind, payload), sys.stdout,
              ensure_ascii=False, indent=2)
    sys.stdout.write("\n")
    return 0


def _err(reason: str, detail: str = "") -> int:
    payload = {"reason": reason}
    if detail:
        payload["detail"] = detail
    json.dump(models.envelope("error", payload), sys.stdout,
              ensure_ascii=False, indent=2)
    sys.stdout.write("\n")
    return 2


def cmd_enqueue(args: object) -> int:
    text = args.text
    if args.text_file:
        with open(args.text_file, "rb") as fh:
            text = fh.read().decode("utf-8")  # exact bytes, no strip
    if text is None:
        return _err("missing-text")
    store = _store(args.db)
    try:
        row = delivery_mod.enqueue_prompt(
            store,
            task_id=args.task or "",
            generation=int(args.generation or 0),
            provider=args.provider or "",
            native_thread=args.thread or "",
            raw_text=text,
            entities=[],
            now=_now(),
            correction_of=args.correction_of or "",
        )
        target = row["target_binding"]
        return _emit("enqueue", {
            "dispatch_id": row["dispatch_id"],
            "text_sha256": row["text_sha256"],
            "bound": bool(row["task_id"] and target),
            "state": row["state"],
        })
    except Exception as exc:
        return _err(type(exc).__name__, str(exc)[:200])
    finally:
        store.close()


def cmd_bind(args: object) -> int:
    store = _store(args.db)
    try:
        row = store.bind_target(
            args.dispatch, args.task, int(args.generation),
            args.provider, args.thread, _now(),
        )
        return _emit("bind", {
            "dispatch_id": row["dispatch_id"],
            "target_binding": row["target_binding"],
            "state": row["state"],
        })
    except Exception as exc:
        return _err(type(exc).__name__, str(exc)[:200])
    finally:
        store.close()


def cmd_claim(args: object) -> int:
    store = _store(args.db)
    try:
        entry = store.claim_one(args.worker, _now())
        if entry is None:
            return _emit("claim", {"claimed": None})
        return _emit("claim", {
            "claimed": entry["dispatch_id"],
            "claim_token": entry["claim_token"],
        })
    finally:
        store.close()


def cmd_deliver_one(args: object) -> int:
    # Synthetic pump: per-process fake adapter (tests/synthetic only).
    store = _store(args.db)
    try:
        entry = store.claim_one(args.worker, _now())
        if entry is None:
            return _emit("deliver", {"claimed": None})
        if not entry["task_id"] or not entry["target_binding"]:
            store.unclaim(entry["dispatch_id"], _now())
            return _err("unbound-target", entry["dispatch_id"])
        adapter = FakeControlAdapter()
        row = delivery_mod.deliver(
            store, adapter, entry["dispatch_id"], _now()
        )
        return _emit("deliver", delivery_mod.redacted_summary(row))
    except Exception as exc:
        return _err(type(exc).__name__, str(exc)[:200])
    finally:
        store.close()


def cmd_ack(args: object) -> int:
    store = _store(args.db)
    try:
        row = store.record_ack(
            args.dispatch, args.ack, _now(), args.claim_token
        )
        return _emit("ack", delivery_mod.redacted_summary(row))
    except Exception as exc:
        return _err(type(exc).__name__, str(exc)[:200])
    finally:
        store.close()


def cmd_start(args: object) -> int:
    store = _store(args.db)
    try:
        row = delivery_mod.start_turn(
            store, FakeControlAdapter(), args.dispatch,
            int(args.fencing_token), _now(),
        )
        out = delivery_mod.redacted_summary(row)
        out["turn_receipt"] = row.get("turn_receipt")
        return _emit("start", out)
    except Exception as exc:
        return _err(type(exc).__name__, str(exc)[:200])
    finally:
        store.close()


def cmd_result(args: object) -> int:
    store = _store(args.db)
    try:
        row = store.record_result(
            args.dispatch, args.result, _now(), args.detail or ""
        )
        return _emit("result", delivery_mod.redacted_summary(row))
    except Exception as exc:
        return _err(type(exc).__name__, str(exc)[:200])
    finally:
        store.close()


def cmd_block(args: object) -> int:
    store = _store(args.db)
    try:
        row = store.mark_blocked(
            args.dispatch, _now(), args.detail or ""
        )
        return _emit("block", delivery_mod.redacted_summary(row))
    except Exception as exc:
        return _err(type(exc).__name__, str(exc)[:200])
    finally:
        store.close()


def cmd_recover(args: object) -> int:
    store = _store(args.db)
    try:
        rows = store.recover_crashed(_now())
        return _emit("recover", {
            "unknown": [delivery_mod.redacted_summary(r) for r in rows],
        })
    finally:
        store.close()


def cmd_alarm(args: object) -> int:
    store = _store(args.db)
    try:
        row = store.pop_unknown_alarm(_now())
        if row is None:
            return _emit("alarm", {"due": None})
        return _emit("alarm", {
            "due": delivery_mod.redacted_summary(row),
        })
    finally:
        store.close()


def cmd_reconcile(args: object) -> int:
    store = _store(args.db)
    try:
        delivered = args.delivered == "true"
        row = store.reconcile(args.dispatch, delivered, _now())
        return _emit("reconcile", delivery_mod.redacted_summary(row))
    except Exception as exc:
        return _err(type(exc).__name__, str(exc)[:200])
    finally:
        store.close()


def cmd_stop(args: object) -> int:
    store = _store(args.db)
    try:
        store.set_stop(args.scope, _now(), args.reason or "")
        return _emit("stop", {"scope": args.scope, "stopped": True})
    finally:
        store.close()


def cmd_unstop(args: object) -> int:
    store = _store(args.db)
    try:
        store.clear_stop(args.scope)
        return _emit("unstop", {"scope": args.scope, "stopped": False})
    finally:
        store.close()


def cmd_stops(args: object) -> int:
    store = _store(args.db)
    try:
        return _emit("stops", {"stops": store.list_stops()})
    finally:
        store.close()


def cmd_cancel(args: object) -> int:
    store = _store(args.db)
    try:
        adapter = FakeControlAdapter()
        if args.refuse_stop:
            adapter.refuse_stop_once = True
        row = delivery_mod.cancel(store, adapter, args.dispatch, _now())
        return _emit("cancel", delivery_mod.redacted_summary(row))
    except Exception as exc:
        return _err(type(exc).__name__, str(exc)[:200])
    finally:
        store.close()


def cmd_cancel_confirm(args: object) -> int:
    store = _store(args.db)
    try:
        confirmed = args.confirmed == "true"
        row = store.confirm_cancel(args.dispatch, confirmed, _now())
        return _emit("cancel-confirm", delivery_mod.redacted_summary(row))
    except Exception as exc:
        return _err(type(exc).__name__, str(exc)[:200])
    finally:
        store.close()


def cmd_intake(args: object) -> int:
    with open(args.update_file, "rb") as fh:
        update = json.loads(fh.read().decode("utf-8"))
    cfg = telegram_mod.OwnerConfig(
        owner_user_ids=tuple(args.owner_user or []),
        owner_chat_id=int(args.owner_chat),
    )
    store = _store(args.db)
    try:
        result = telegram_mod.intake_update(store, update, cfg, _now())
        return _emit("intake", result)
    except Exception as exc:
        return _err(type(exc).__name__, str(exc)[:200])
    finally:
        store.close()


def cmd_pair_issue(args: object) -> int:
    store = _store(args.db)
    try:
        nonce = pairing_mod.issue(store, _now(), float(args.ttl))
        return _emit("pair-issue", {"nonce": nonce})
    finally:
        store.close()


def cmd_pair_redeem(args: object) -> int:
    store = _store(args.db)
    try:
        entry = pairing_mod.redeem(
            store, args.nonce, int(args.sender), int(args.chat),
            args.chat_type, _now(),
        )
        return _emit("pair-redeem", {
            "sender_id": entry["sender_id"],
            "chat_id": entry["chat_id"],
        })
    except Exception as exc:
        return _err(type(exc).__name__, str(exc)[:200])
    finally:
        store.close()


def cmd_callback_issue(args: object) -> int:
    store = _store(args.db)
    try:
        action_id = store.issue_callback(
            kind=args.kind,
            task_id=args.task or "",
            provider=args.provider or "",
            native_thread=args.thread or "",
            generation=int(args.generation or 0),
            scope=args.scope or models.STOP_SCOPE_ALL,
            now=_now(),
            ttl=float(args.ttl),
        )
        return _emit("callback-issue", {"action_id": action_id})
    finally:
        store.close()


def cmd_status(args: object) -> int:
    store = _store(args.db)
    try:
        rows = store.list_dispatches()
        return _emit("status", {
            "dispatches": [delivery_mod.redacted_summary(r) for r in rows],
            "stops": store.list_stops(),
        })
    finally:
        store.close()


def cmd_readback(args: object) -> int:
    # Explicit receiver path: the ONLY diagnostic carrying raw text.
    store = _store(args.db)
    try:
        row = store.require_dispatch(args.dispatch)
        return _emit("readback", {
            "dispatch_id": row["dispatch_id"],
            "raw_text": row["raw_text"],
            "entities": json.loads(row["entities_json"] or "[]"),
            "text_sha256": row["text_sha256"],
            "target_binding": row["target_binding"],
            "state": row["state"],
        })
    except Exception as exc:
        return _err(type(exc).__name__, str(exc)[:200])
    finally:
        store.close()


def cmd_takeover(args: object) -> int:
    store = _store(args.db)
    try:
        store.record_takeover(
            args.task, args.host, _now(), args.note or ""
        )
        return _emit("takeover", {
            "task_id": args.task,
            "host_session": args.host,
        })
    finally:
        store.close()


def cmd_status_render(args: object) -> int:
    store = _store(args.db)
    try:
        row = store.require_dispatch(args.dispatch)
        text = status_mod.render_status_pl(
            args.operation, row["state"],
            label=args.dispatch[:8], detail=args.detail or "",
        )
        tracker = status_mod.StatusTracker(store)
        decision = tracker.decide(args.dispatch, text)
        return _emit("status-render", {
            "text": text,
            "chars": len(text),
            "decision": decision,
        })
    except Exception as exc:
        return _err(type(exc).__name__, str(exc)[:200])
    finally:
        store.close()


def cmd_bot_send(args: object) -> int:
    # Parent canary path only. Token from environment, never argv.
    from openclaw_control.botapi import (
        BotApi,
        urllib_no_redirect_opener,
    )

    try:
        api = BotApi(os.environ, urllib_no_redirect_opener)
        result = api.send_text(int(args.chat), args.text)
        return _emit("bot-send", {"result": result})
    except Exception as exc:
        return _err(type(exc).__name__, str(exc)[:200])


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="openclaw-control",
        description="OpenClaw owner-control transport candidate (T2/T3)",
    )
    sub = p.add_subparsers(dest="command", required=True)

    def _db(sp: argparse.ArgumentParser) -> argparse.ArgumentParser:
        sp.add_argument("--db", required=True)
        return sp

    e = _db(sub.add_parser("enqueue"))
    e.add_argument("--task", default="")
    e.add_argument("--generation", default="0")
    e.add_argument("--provider", default="")
    e.add_argument("--thread", default="")
    e.add_argument("--text", default=None)
    e.add_argument("--text-file", default=None)
    e.add_argument("--correction-of", default="")

    b = _db(sub.add_parser("bind"))
    b.add_argument("--dispatch", required=True)
    b.add_argument("--task", required=True)
    b.add_argument("--generation", required=True)
    b.add_argument("--provider", required=True)
    b.add_argument("--thread", required=True)

    c = _db(sub.add_parser("claim"))
    c.add_argument("--worker", required=True)

    d = _db(sub.add_parser("deliver-one"))
    d.add_argument("--worker", required=True)

    a = _db(sub.add_parser("ack"))
    a.add_argument("--dispatch", required=True)
    a.add_argument("--ack", required=True, choices=["acked", "unknown"])
    a.add_argument("--claim-token", default=None)

    s = _db(sub.add_parser("start"))
    s.add_argument("--dispatch", required=True)
    s.add_argument("--fencing-token", required=True)

    r = _db(sub.add_parser("result"))
    r.add_argument("--dispatch", required=True)
    r.add_argument("--result", required=True,
                   choices=["completed", "blocked", "failed"])
    r.add_argument("--detail", default="")

    bl = _db(sub.add_parser("block"))
    bl.add_argument("--dispatch", required=True)
    bl.add_argument("--detail", default="")

    _db(sub.add_parser("recover"))
    _db(sub.add_parser("alarm"))

    rc = _db(sub.add_parser("reconcile"))
    rc.add_argument("--dispatch", required=True)
    rc.add_argument("--delivered", required=True,
                    choices=["true", "false"])

    st = _db(sub.add_parser("stop"))
    st.add_argument("--scope", required=True)
    st.add_argument("--reason", default="")

    us = _db(sub.add_parser("unstop"))
    us.add_argument("--scope", required=True)

    _db(sub.add_parser("stops"))

    cc = _db(sub.add_parser("cancel"))
    cc.add_argument("--dispatch", required=True)
    cc.add_argument("--refuse-stop", action="store_true")

    cf = _db(sub.add_parser("cancel-confirm"))
    cf.add_argument("--dispatch", required=True)
    cf.add_argument("--confirmed", required=True,
                    choices=["true", "false"])

    it = _db(sub.add_parser("intake"))
    it.add_argument("--update-file", required=True)
    it.add_argument("--owner-user", action="append", type=int, default=[])
    it.add_argument("--owner-chat", required=True)

    pi = _db(sub.add_parser("pair-issue"))
    pi.add_argument("--ttl", default="600")

    pr = _db(sub.add_parser("pair-redeem"))
    pr.add_argument("--nonce", required=True)
    pr.add_argument("--sender", required=True)
    pr.add_argument("--chat", required=True)
    pr.add_argument("--chat-type", required=True)

    ci = _db(sub.add_parser("callback-issue"))
    ci.add_argument("--kind", required=True,
                    choices=["target_select", "stop"])
    ci.add_argument("--task", default="")
    ci.add_argument("--generation", default="0")
    ci.add_argument("--scope", default="all")
    ci.add_argument("--provider", default="")
    ci.add_argument("--thread", default="")
    ci.add_argument("--ttl", default="600")

    _db(sub.add_parser("status"))

    rb = _db(sub.add_parser("readback"))
    rb.add_argument("--dispatch", required=True)

    to = _db(sub.add_parser("takeover"))
    to.add_argument("--task", required=True)
    to.add_argument("--host", required=True)
    to.add_argument("--note", default="")

    sr = _db(sub.add_parser("status-render"))
    sr.add_argument("--dispatch", required=True)
    sr.add_argument("--operation", default="dispatch")
    sr.add_argument("--detail", default="")

    bs = _db(sub.add_parser("bot-send"))
    bs.add_argument("--chat", required=True)
    bs.add_argument("--text", required=True)

    return p


def main(argv: "list[str] | None" = None) -> int:
    args = build_parser().parse_args(argv)
    handlers = {
        "enqueue": cmd_enqueue,
        "bind": cmd_bind,
        "claim": cmd_claim,
        "deliver-one": cmd_deliver_one,
        "ack": cmd_ack,
        "start": cmd_start,
        "result": cmd_result,
        "block": cmd_block,
        "recover": cmd_recover,
        "alarm": cmd_alarm,
        "reconcile": cmd_reconcile,
        "stop": cmd_stop,
        "unstop": cmd_unstop,
        "stops": cmd_stops,
        "cancel": cmd_cancel,
        "cancel-confirm": cmd_cancel_confirm,
        "intake": cmd_intake,
        "pair-issue": cmd_pair_issue,
        "pair-redeem": cmd_pair_redeem,
        "callback-issue": cmd_callback_issue,
        "status": cmd_status,
        "readback": cmd_readback,
        "takeover": cmd_takeover,
        "status-render": cmd_status_render,
        "bot-send": cmd_bot_send,
    }
    handler = handlers.get(args.command)
    if handler is None:  # pragma: no cover - argparse guards
        return 2
    return handler(args)


if __name__ == "__main__":
    raise SystemExit(main())
