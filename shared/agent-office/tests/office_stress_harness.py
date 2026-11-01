"""Criterion 9 harness: N independent processes append to one inbox; zero corruption."""

from __future__ import annotations

import json
import multiprocessing
import os
import queue
import sys
import time
from pathlib import Path
from typing import Any

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
if str(PACKAGE_ROOT) not in sys.path:
    sys.path.insert(0, str(PACKAGE_ROOT))

from coordfs.office import Office, inbox_path, parse_envelope  # noqa: E402

INBOX_AGENT = "stress-testhost-inbox"


def _sender_worker(home: str, index: int, per_sender: int, ready: Any, start: Any, results: Any) -> None:
    ready.put(index)
    if not start.wait(120):
        results.put({"index": index, "pid": os.getpid(), "error": "start-timeout"})
        return
    sent: list[str] = []
    failures: list[dict[str, Any]] = []
    started = time.monotonic()
    try:
        office = Office(
            agent_id=f"stress-sender-{index:03d}",
            home=Path(home),
            rate_limit=max(per_sender, 1),
            mutex_timeout_s=60.0,
        )
        for count in range(per_sender):
            body = f"sender {index:03d} message {count:03d} pid {os.getpid()} " + ("x" * 64)
            result = office.send(INBOX_AGENT, body)
            if result.get("status") == "ok":
                sent.append(result["id"])
            else:
                failures.append({"count": count, "code": result.get("code"), "error": result.get("error")})
    except BaseException as exc:  # pragma: no cover - returned to parent as evidence
        failures.append({"exception": f"{type(exc).__name__}: {exc}"})
    results.put(
        {
            "index": index,
            "pid": os.getpid(),
            "sent": sent,
            "failures": failures,
            "duration_s": round(time.monotonic() - started, 3),
        }
    )


def _drain(source: Any, count: int, timeout_s: float) -> list[Any]:
    deadline = time.monotonic() + timeout_s
    values: list[Any] = []
    while len(values) < count:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        try:
            values.append(source.get(timeout=remaining))
        except queue.Empty:
            break
    return values


def verify_inbox(home: Path, expected_ids: set[str], expected_per_sender: dict[str, int]) -> dict[str, Any]:
    path = inbox_path(home, INBOX_AGENT)
    raw = path.read_bytes() if path.is_file() else b""
    lines = raw.split(b"\n")
    trailing = lines.pop() if lines else b""
    parsed = 0
    malformed = 0
    ids: list[str] = []
    per_sender: dict[str, int] = {}
    for line in lines:
        envelope = parse_envelope(line)
        if envelope is None:
            malformed += 1
            continue
        parsed += 1
        ids.append(envelope["id"])
        per_sender[envelope["from"]] = per_sender.get(envelope["from"], 0) + 1
    reader = Office(agent_id=INBOX_AGENT, home=home)
    consumed = 0
    while True:
        page = reader.read(limit=500)
        if page.get("status") != "ok" or not page.get("count"):
            break
        consumed += page["count"]
    return {
        "file_bytes": len(raw),
        "trailing_partial_bytes": len(trailing),
        "parsed": parsed,
        "malformed": malformed,
        "unique_ids": len(set(ids)),
        "ids_match_senders": set(ids) == expected_ids,
        "per_sender_ok": per_sender == expected_per_sender,
        "cursor_read_total": consumed,
    }


def run_inbox_stress(home: Path, *, senders: int = 100, per_sender: int = 10, timeout_s: float = 600.0) -> dict[str, Any]:
    context = multiprocessing.get_context("spawn")
    ready = context.Queue()
    start = context.Event()
    results = context.Queue()
    processes = [
        context.Process(
            target=_sender_worker,
            args=(str(home), index, per_sender, ready, start, results),
            name=f"office-sender-{index:03d}",
        )
        for index in range(senders)
    ]
    # A direct send needs an addressable recipient: the inbox owner announces itself once,
    # long enough to outlive the whole run, so no sender races to create that lease.
    Office(agent_id=INBOX_AGENT, home=home).presence(ttl_s=3600)
    started = time.monotonic()
    for process in processes:
        process.start()
    ready_values = _drain(ready, senders, timeout_s / 2)
    start.set()
    values = [dict(item) for item in _drain(results, senders, timeout_s)]
    timed_out: list[int] = []
    for process in processes:
        process.join(timeout=30)
        if process.is_alive():
            timed_out.append(process.pid or -1)
            process.terminate()
            process.join(timeout=5)
    expected_ids = {message_id for item in values for message_id in item.get("sent", [])}
    expected_per_sender = {f"stress-sender-{item['index']:03d}": len(item.get("sent", [])) for item in values}
    failures = [item for item in values if item.get("failures") or "error" in item]
    inbox = verify_inbox(home, expected_ids, expected_per_sender)
    expected_total = senders * per_sender
    passed = (
        len(ready_values) == senders
        and len(values) == senders
        and not failures
        and not timed_out
        and inbox["parsed"] == expected_total
        and inbox["malformed"] == 0
        and inbox["trailing_partial_bytes"] == 0
        and inbox["unique_ids"] == expected_total
        and inbox["ids_match_senders"]
        and inbox["per_sender_ok"]
        and inbox["cursor_read_total"] == expected_total
    )
    return {
        "senders": senders,
        "per_sender": per_sender,
        "expected_total": expected_total,
        "ready": len(ready_values),
        "result_count": len(values),
        "unique_pids": len({item.get("pid") for item in values if item.get("pid")}),
        "failures": failures[:20],
        "process_timeouts": timed_out,
        "inbox": inbox,
        "duration_s": round(time.monotonic() - started, 3),
        "passed": passed,
    }


if __name__ == "__main__":
    print(json.dumps(run_inbox_stress(Path(sys.argv[1])), indent=2, sort_keys=True))
