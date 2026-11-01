"""Shared fixtures for Agent Office tests."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from typing import Any

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
if str(PACKAGE_ROOT) not in sys.path:
    sys.path.insert(0, str(PACKAGE_ROOT))

from coordfs.office import Office  # noqa: E402

OFFICE_PY = PACKAGE_ROOT / "office.py"
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


class OfficeTestCase(unittest.TestCase):
    ALICE_ID = "codex-testhost-alice"
    BOB_ID = "claude-testhost-bob"
    CAROL_ID = "qwen-testhost-carol"
    MALLORY_ID = "gemini-testhost-mallory"
    OBSERVER_ID = "codex-testhost-observer"
    SENDER_ID = "codex-testhost-sender"
    RECEIVER_ID = "claude-testhost-receiver"
    ROOT_ID = "codex-testhost-root"
    SINK_ID = "codex-testhost-sink"
    ARCH_ID = "codex-testhost-arch"
    SOMEONE_ID = "codex-testhost-someone"
    LONELY_ID = "codex-testhost-lonely"
    CRASH_ID = "codex-testhost-crash"
    ALIVE_ID = "codex-testhost-alive"
    POLITE_ID = "codex-testhost-polite"

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="office-test-")
        self.home = Path(self.tmp.name) / "coord-home"
        self.clock = [1_700_000_000.0]
        self.alice = self.office(self.ALICE_ID)
        self.bob = self.office(self.BOB_ID)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def office(self, agent_id: str, **overrides: Any) -> Office:
        options: dict[str, Any] = {
            "agent_id": agent_id,
            "home": self.home,
            "now": lambda: self.clock[0],
            "mutex_timeout_s": 1.0,
        }
        options.update(overrides)
        return Office(**options)

    def advance(self, seconds: float) -> None:
        self.clock[0] += seconds

    def announce(self, *agent_ids: str, ttl_s: int = 86_400) -> None:
        """Put agents on the board so they are addressable recipients.

        A direct send refuses an address nobody ever answered from, so a fixture that
        sends to an inbox has to announce that agent first. The lease is written on the
        wall clock, not the suite's fake one, so a CLI or MCP subprocess -- which runs on
        real time -- sees the agent as live too. One day of TTL covers every clock step in
        this suite (the largest is 61 s) without a heartbeat.
        """
        for agent_id in agent_ids:
            self.office(agent_id, now=time.time).presence(ttl_s=ttl_s)

    def cli_env(self, **extra: str) -> dict[str, str]:
        env = os.environ.copy()
        env["COORDFS_HOME"] = str(self.home)
        env.update(extra)
        return env

    def run_cli(self, args: list[str], **extra_env: str) -> tuple[int, dict[str, Any]]:
        completed = subprocess.run(
            [sys.executable, "-I", str(OFFICE_PY), *args],
            cwd=str(self.tmp.name),
            env=self.cli_env(**extra_env),
            text=True,
            encoding="utf-8",
            capture_output=True,
            check=False,
            creationflags=NO_WINDOW,
        )
        self.assertTrue(completed.stdout.strip(), completed.stderr)
        return completed.returncode, json.loads(completed.stdout)


class McpClient:
    """Minimal newline-delimited JSON-RPC client over a subprocess pipe."""

    def __init__(self, agent_id: str, env: dict[str, str], ttl_s: int = 5, heartbeat: bool = True) -> None:
        args = [sys.executable, "-I", str(OFFICE_PY), "--agent", agent_id, "mcp", "--ttl", str(ttl_s)]
        if not heartbeat:
            args.append("--no-heartbeat")
        self.process = subprocess.Popen(
            args,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            creationflags=NO_WINDOW,
        )
        self.next_id = 0

    @property
    def pid(self) -> int:
        return self.process.pid

    @property
    def returncode(self) -> int | None:
        return self.process.poll()

    def request(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        self.send_request(method, params)
        return self.receive()

    def send_request(self, method: str, params: dict[str, Any] | None = None) -> int:
        self.next_id += 1
        payload = {"jsonrpc": "2.0", "id": self.next_id, "method": method, "params": params or {}}
        assert self.process.stdin is not None
        self.process.stdin.write((json.dumps(payload) + "\n").encode("utf-8"))
        self.process.stdin.flush()
        return self.next_id

    def notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        payload = {"jsonrpc": "2.0", "method": method, "params": params or {}}
        assert self.process.stdin is not None
        self.process.stdin.write((json.dumps(payload) + "\n").encode("utf-8"))
        self.process.stdin.flush()

    def receive(self) -> dict[str, Any]:
        assert self.process.stdout is not None
        line = self.process.stdout.readline()
        if not line:
            stderr = self.process.stderr.read().decode("utf-8", "replace") if self.process.stderr else ""
            raise AssertionError(f"MCP server closed stdout; stderr={stderr!r}")
        return json.loads(line.decode("utf-8"))

    def call(self, name: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
        response = self.request("tools/call", {"name": name, "arguments": arguments or {}})
        if "error" in response:
            raise AssertionError(f"tool call failed: {response['error']}")
        return response["result"]

    def handshake(self) -> dict[str, Any]:
        result = self.request(
            "initialize",
            {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "office-test", "version": "0"}},
        )
        self.notify("notifications/initialized")
        return result

    def close(self) -> int:
        if self.process.stdin is not None:
            try:
                self.process.stdin.close()
            except OSError:
                pass
        try:
            return self.process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.process.kill()
            return self.process.wait(timeout=5)

    def kill(self) -> None:
        self.process.kill()
        self.process.wait(timeout=5)
