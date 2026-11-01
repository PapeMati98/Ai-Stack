#!/usr/bin/env python3
"""Run a full MCP office session in-process under a socket-denying audit hook.

Prints one JSON receipt. Any `socket.*` audit event (creation, bind, connect,
send, name resolution) is recorded; socket creation raises so a hidden network
call cannot succeed silently. Runs under `python -I` in a scratch home.
"""

from __future__ import annotations

import io
import json
import os
import sys
from hashlib import sha256
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PACKAGE_ROOT))

SOCKET_EVENTS: list[str] = []
DENIED = ("socket.__new__", "socket.bind", "socket.connect", "socket.sendto", "socket.sendmsg", "socket.getaddrinfo")


def _audit(event: str, args: tuple) -> None:
    if event.startswith("socket."):
        SOCKET_EVENTS.append(event)
        if event in DENIED:
            raise RuntimeError(f"network denied by probe: {event}")


def main() -> int:
    sys.addaudithook(_audit)
    home = Path(os.environ["COORDFS_HOME"])
    from coordfs.office import Office
    from coordfs.office_mcp import OfficeMcpServer

    alice = Office(agent_id="codex-testhost-probe-alice", home=home)
    bob = Office(agent_id="codex-testhost-probe-bob", home=home)
    # Direct delivery uses a complete synthetic Office ID and presence evidence.
    bob.presence(ttl_s=30)
    requests = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "office_presence", "arguments": {"ttl_s": 5}}},
        {"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "office_send", "arguments": {"to": "codex-testhost-probe-bob", "body": "no network here"}}},
        {"jsonrpc": "2.0", "id": 5, "method": "tools/call", "params": {"name": "office_who"}},
    ]
    stdout = io.BytesIO()
    server = OfficeMcpServer(alice, heartbeat_ttl_s=2)
    stats = server.serve(io.BytesIO(b"".join(json.dumps(item).encode("utf-8") + b"\n" for item in requests)), stdout)
    responses = [json.loads(line) for line in stdout.getvalue().decode("utf-8").splitlines() if line]
    bob_side = OfficeMcpServer(bob, heartbeat_ttl_s=None)
    waited = bob_side.call_tool("office_wait", {"timeout_s": 0.5})
    read = bob_side.call_tool("office_read", {})
    receipt = {
        "python_isolated": bool(sys.flags.isolated),
        "python_version": sys.version.split()[0],
        "probe_sha256": sha256(Path(__file__).read_bytes()).hexdigest(),
        "socket_events": SOCKET_EVENTS,
        "responses": len(responses),
        "server_stats": {key: value for key, value in stats.items() if key != "presence"},
        "codes": [item.get("result", {}).get("structuredContent", {}).get("code") for item in responses[2:]],
        "wait_code": waited.get("code"),
        "read_count": read.get("count"),
        "read_from": (read.get("messages") or [{}])[0].get("untrusted_message", {}).get("from"),
    }
    print(json.dumps(receipt, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
