#!/usr/bin/env python3
"""Phase 3 measurement: is the polling `wait` (50 ms -> 500 ms -> 2 s backoff) good enough?

Measures, with real waiter processes (one `python -I` per agent, like real harnesses):

  A. wake latency: N agents waiting at once on one channel (one send wakes all) and on their
     own inboxes (one send each); the message lands after 0.3 s, 3 s and 12 s of waiting so
     every backoff tier is exercised;
  B. CPU of a waiting process over an idle window (default 60 s) with no traffic, for 1 and
     10 concurrent waiters;
  C. backlog: a log filled past the 1 MiB cap into rotated generations; latency of one poll,
     of a wake-up and of draining the backlog.

Latency is `perf_counter` in the waiter minus `perf_counter` in the sender right before the
send; the clock is system-wide on Windows (QueryPerformanceCounter), verified on this machine.
Prints a Markdown report plus one JSON document; `--json <path>` also writes the JSON.
`--quick` shortens everything for a smoke run.

Not a unit test: run it by hand, read the numbers, decide. Nothing here changes the office.
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
if str(PACKAGE_ROOT) not in sys.path:
    sys.path.insert(0, str(PACKAGE_ROOT))

from coordfs.office import Office, inbox_path  # noqa: E402

WAIT_TIMEOUT_S = 40.0
FILL_BODY = "b" * 16_000


# -- waiter process -----------------------------------------------------------------------


def waiter_main(args: argparse.Namespace) -> int:
    office = Office(agent_id=args.agent, home=Path(args.home))
    channel = args.channel or None
    # Put the cursor at the end so `wait` needs a genuinely new line.
    while True:
        result = office.read(limit=500, channel=channel)
        if result.get("status") != "ok" or result.get("remaining", 0) == 0:
            break
    sys.stdout.write("READY\n")
    sys.stdout.flush()
    cpu0 = time.process_time()
    started = time.perf_counter()
    result = office.wait(timeout_s=args.timeout, channel=channel)
    woke = time.perf_counter()
    cpu1 = time.process_time()
    sys.stdout.write(
        json.dumps(
            {
                "agent": args.agent,
                "code": result.get("code"),
                "started": started,
                "woke": woke,
                "cpu_s": cpu1 - cpu0,
                "polls": result.get("polls"),
                "waited_s": result.get("waited_s"),
            }
        )
        + "\n"
    )
    sys.stdout.flush()
    return 0


# -- driver ---------------------------------------------------------------------------------


class Waiters:
    def __init__(self, home: Path, agents: list[str], channel: str | None, timeout_s: float) -> None:
        self.processes: list[subprocess.Popen[str]] = []
        for agent in agents:
            command = [
                sys.executable, "-I", str(Path(__file__).resolve()), "--waiter",
                "--home", str(home), "--agent", agent, "--timeout", str(timeout_s),
            ]
            if channel:
                command += ["--channel", channel]
            self.processes.append(
                subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8")
            )

    def ready(self) -> None:
        for process in self.processes:
            assert process.stdout is not None
            line = process.stdout.readline().strip()
            if line != "READY":
                raise RuntimeError(f"waiter did not become ready: {line!r} / {process.stderr.read() if process.stderr else ''}")

    def collect(self) -> list[dict[str, Any]]:
        rows = []
        for process in self.processes:
            out, err = process.communicate(timeout=WAIT_TIMEOUT_S + 30)
            line = out.strip().splitlines()[-1] if out.strip() else ""
            if not line:
                raise RuntimeError(f"waiter produced no result; stderr={err!r}")
            rows.append(json.loads(line))
        return rows


def _stats(values: list[float]) -> dict[str, float]:
    return {
        "min_ms": round(min(values) * 1000, 1),
        "mean_ms": round(statistics.fmean(values) * 1000, 1),
        "max_ms": round(max(values) * 1000, 1),
    }


def scenario_wake(home: Path, counts: list[int], delays: list[float]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    sender = Office(agent_id="bench-sender", home=home, rate_limit=1_000_000)
    for target in ("channel", "inbox"):
        for count in counts:
            for delay in delays:
                agents = [f"w{target[0]}{count}-{index}" for index in range(count)]
                channel = f"bench-{count}-{int(delay * 10)}" if target == "channel" else None
                if channel:
                    sender.send(None, "prime", channel=channel)
                if not channel:
                    # A direct send needs an addressable recipient; waiters only read.
                    for agent in agents:
                        Office(agent_id=agent, home=home).presence(ttl_s=3600)
                waiters = Waiters(home, agents, channel, WAIT_TIMEOUT_S)
                waiters.ready()
                time.sleep(delay)
                send_times: dict[str, float] = {}
                if channel:
                    t0 = time.perf_counter()
                    sender.send(None, "wake", channel=channel)
                    send_times = {agent: t0 for agent in agents}
                else:
                    for agent in agents:
                        send_times[agent] = time.perf_counter()
                        sender.send(agent, "wake")
                results = waiters.collect()
                latencies = [item["woke"] - send_times[item["agent"]] for item in results]
                elapsed_at_send = [send_times[item["agent"]] - item["started"] for item in results]
                rows.append(
                    {
                        "target": target,
                        "agents": count,
                        "delay_s": delay,
                        "elapsed_at_send_s": [round(value, 2) for value in (min(elapsed_at_send), max(elapsed_at_send))],
                        "codes": sorted({item["code"] for item in results}),
                        "latency": _stats(latencies),
                        "cpu_ms_per_waiter": round(statistics.fmean(item["cpu_s"] for item in results) * 1000, 1),
                        "polls_per_waiter": round(statistics.fmean(item["polls"] for item in results), 1),
                    }
                )
    return rows


def scenario_idle_cpu(home: Path, counts: list[int], idle_s: float) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for count in counts:
        agents = [f"idle{count}-{index}" for index in range(count)]
        waiters = Waiters(home, agents, None, idle_s)
        waiters.ready()
        results = waiters.collect()
        cpu = [item["cpu_s"] for item in results]
        rows.append(
            {
                "agents": count,
                "idle_s": idle_s,
                "codes": sorted({item["code"] for item in results}),
                "cpu_ms_per_waiter": _stats(cpu),
                "cpu_percent_per_waiter": round(statistics.fmean(cpu) / idle_s * 100, 3),
                "polls_per_waiter": round(statistics.fmean(item["polls"] for item in results), 1),
                "waited_s": round(statistics.fmean(item["waited_s"] for item in results), 2),
            }
        )
    return rows


def scenario_backlog(home: Path, generations: int) -> dict[str, Any]:
    sender = Office(agent_id="bench-filler", home=home, rate_limit=1_000_000)
    # A direct send needs an addressable recipient; the backlog reader only reads here.
    Office(agent_id="backlog-reader", home=home).presence(ttl_s=3600)
    # Rotated generations are a little under the cap (rotation triggers before the cap is
    # crossed), so aim at half a generation less than the nominal total.
    target_bytes = int((generations - 0.5) * sender.inbox_max_bytes)
    sends = 0
    fill_started = time.perf_counter()
    while True:
        sender.send("backlog-reader", FILL_BODY)
        sends += 1
        live = inbox_path(home, "backlog-reader")
        retained = sum(
            path.stat().st_size for path in live.parent.glob("backlog-reader.jsonl*") if path.suffix != ".lock"
        )
        if retained >= target_bytes:
            break
    fill_s = time.perf_counter() - fill_started
    reader = Office(agent_id="backlog-reader", home=home)
    # First-time reader with a backlog: one poll must answer at once.
    probe_started = time.perf_counter()
    first_probe = reader.wait(timeout_s=0)
    first_probe_s = time.perf_counter() - probe_started
    # Drain everything (bounded 500 per read), then measure one poll on the full live file.
    drain_started = time.perf_counter()
    drained = 0
    while True:
        result = reader.read(limit=500)
        drained += result["count"]
        if result["remaining"] == 0:
            break
    drain_s = time.perf_counter() - drain_started
    probe_started = time.perf_counter()
    empty_probe = reader.wait(timeout_s=0)
    empty_probe_s = time.perf_counter() - probe_started
    # Wake-up with the cursor at the end of a ~1 MiB live file.
    waiters = Waiters(home, ["backlog-reader"], None, WAIT_TIMEOUT_S)
    waiters.ready()
    time.sleep(0.3)
    t0 = time.perf_counter()
    sender.send("backlog-reader", "wake")
    result = waiters.collect()[0]
    return {
        "generations": generations,
        "messages_written": sends,
        "retained_bytes": retained,
        "fill_s": round(fill_s, 2),
        "first_probe_code": first_probe["code"],
        "first_probe_ms": round(first_probe_s * 1000, 1),
        "drain_messages": drained,
        "drain_s": round(drain_s, 3),
        "poll_on_full_live_file_code": empty_probe["code"],
        "poll_on_full_live_file_ms": round(empty_probe_s * 1000, 2),
        "wake_latency_ms": round((result["woke"] - t0) * 1000, 1),
        "wake_code": result["code"],
    }


def render(report: dict[str, Any]) -> str:
    lines = ["# office wait benchmark", "", f"machine: {report['machine']}", f"python: {report['python']}", ""]
    lines += ["## A. wake latency", "", "| target | agents | sent after | elapsed at send | min | mean | max | cpu/waiter | polls |", "|---|---|---|---|---|---|---|---|---|"]
    for row in report["wake"]:
        lat = row["latency"]
        lines.append(
            f"| {row['target']} | {row['agents']} | {row['delay_s']} s | {row['elapsed_at_send_s'][0]}-{row['elapsed_at_send_s'][1]} s "
            f"| {lat['min_ms']} ms | {lat['mean_ms']} ms | {lat['max_ms']} ms | {row['cpu_ms_per_waiter']} ms | {row['polls_per_waiter']} |"
        )
    lines += ["", "## B. idle CPU", "", "| agents | idle | cpu/waiter min | mean | max | cpu % | polls |", "|---|---|---|---|---|---|---|"]
    for row in report["idle_cpu"]:
        cpu = row["cpu_ms_per_waiter"]
        lines.append(
            f"| {row['agents']} | {row['idle_s']} s | {cpu['min_ms']} ms | {cpu['mean_ms']} ms | {cpu['max_ms']} ms "
            f"| {row['cpu_percent_per_waiter']} % | {row['polls_per_waiter']} |"
        )
    back = report["backlog"]
    lines += [
        "", "## C. backlog", "",
        f"- {back['messages_written']} messages of 16 KB written in {back['fill_s']} s; {back['retained_bytes']} bytes retained over {back['generations']} generations",
        f"- first probe with unread backlog: {back['first_probe_code']} in {back['first_probe_ms']} ms",
        f"- drain of {back['drain_messages']} messages: {back['drain_s']} s",
        f"- one poll with the cursor at the end of the full live file: {back['poll_on_full_live_file_code']} in {back['poll_on_full_live_file_ms']} ms",
        f"- wake latency with that backlog: {back['wake_latency_ms']} ms ({back['wake_code']})",
    ]
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--waiter", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--home", help=argparse.SUPPRESS)
    parser.add_argument("--agent", help=argparse.SUPPRESS)
    parser.add_argument("--channel", help=argparse.SUPPRESS)
    parser.add_argument("--timeout", type=float, default=WAIT_TIMEOUT_S, help=argparse.SUPPRESS)
    parser.add_argument("--agents", default="2,5,10", help="Waiter counts for scenario A (default 2,5,10).")
    parser.add_argument("--delays", default="0.3,3,12", help="Seconds of waiting before the send (default 0.3,3,12).")
    parser.add_argument("--idle-seconds", type=float, default=60.0, help="Idle window for scenario B.")
    parser.add_argument("--idle-agents", default="1,10", help="Waiter counts for scenario B.")
    parser.add_argument("--generations", type=int, default=3, help="Log generations to fill for scenario C.")
    parser.add_argument("--quick", action="store_true", help="Short smoke run.")
    parser.add_argument("--json", type=Path, help="Also write the JSON report here.")
    args = parser.parse_args(argv)
    if args.waiter:
        return waiter_main(args)
    counts = [int(item) for item in args.agents.split(",") if item]
    delays = [float(item) for item in args.delays.split(",") if item]
    idle_counts = [int(item) for item in args.idle_agents.split(",") if item]
    idle_s = args.idle_seconds
    if args.quick:
        counts, delays, idle_counts, idle_s = [2], [0.3, 1.5], [1], 3.0
    with tempfile.TemporaryDirectory(prefix="office-bench-") as tmp:
        home = Path(tmp) / "coord-home"
        report = {
            "machine": f"{sys.platform} cpu_count={os.cpu_count()}",
            "python": sys.version.split()[0],
            "wait_tiers": "50 ms (<1 s), 500 ms (<10 s), 2 s (>=10 s)",
            "wake": scenario_wake(home, counts, delays),
            "idle_cpu": scenario_idle_cpu(home, idle_counts, idle_s),
            "backlog": scenario_backlog(home, args.generations),
        }
    text = render(report)
    sys.stdout.write(text)
    sys.stdout.write("\n```json\n" + json.dumps(report, indent=2, sort_keys=True) + "\n```\n")
    if args.json:
        args.json.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
