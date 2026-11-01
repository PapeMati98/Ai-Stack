"""Criterion 8: zero network. No socket is created and no listener appears for office processes."""

from __future__ import annotations

import json
import concurrent.futures
from hashlib import sha256
import os
import shutil
import subprocess
import sys
import time
import unittest
from pathlib import Path

from office_support import McpClient, OfficeTestCase

PROBE = Path(__file__).resolve().parent / "office_no_network_probe.py"


class NoNetworkTests(OfficeTestCase):
    def test_native_transport_imports_without_network_modules(self) -> None:
        import coordfs.office_pipe as office_pipe
        self.assertTrue(callable(office_pipe.notify))
        self.assertTrue(hasattr(concurrent.futures, "ThreadPoolExecutor"))

    def test_full_session_under_socket_denying_audit_hook(self) -> None:
        completed = subprocess.run(
            [sys.executable, "-I", str(PROBE)],
            env=self.cli_env(),
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        receipt = json.loads(completed.stdout)
        self.assertEqual(receipt["socket_events"], [], receipt)
        self.assertTrue(receipt["python_isolated"], receipt)
        self.assertEqual(receipt["probe_sha256"], sha256(PROBE.read_bytes()).hexdigest())
        self.assertEqual(receipt["responses"], 5)
        self.assertEqual(receipt["codes"], ["present", "sent", "who"])
        self.assertEqual(receipt["wait_code"], "message")
        self.assertEqual(receipt["read_count"], 1)
        self.assertEqual(receipt["read_from"], "codex-testhost-probe-alice")

    @unittest.skipUnless(os.name == "nt" and shutil.which("netstat"), "netstat listener check is Windows-only")
    def test_running_mcp_servers_own_no_tcp_or_udp_endpoints(self) -> None:
        alice = McpClient("codex-testhost-net-alice", self.cli_env(), ttl_s=5)
        bob = McpClient("codex-testhost-net-bob", self.cli_env(), ttl_s=5)
        try:
            alice.handshake()
            bob.handshake()
            sent = alice.call("office_send", {"to": "codex-testhost-net-bob", "body": "hello"})
            self.assertEqual(sent["structuredContent"]["code"], "sent")
            bob.call("office_read")
            self.assertIsNone(alice.returncode)
            self.assertIsNone(bob.returncode)
            time.sleep(0.2)
            listing = subprocess.run(
                ["netstat", "-ano"], capture_output=True, text=True, encoding="utf-8", errors="replace", check=False,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            ).stdout
            self.assertIsNone(alice.returncode)
            self.assertIsNone(bob.returncode)
            self.assertTrue(listing.splitlines(), "netstat must provide a non-empty sample")
            pids = {str(alice.pid), str(bob.pid)}
            offending = [line for line in listing.splitlines() if line.split() and line.split()[-1] in pids]
            self.assertEqual(offending, [], "office MCP processes must own no network endpoints")
        finally:
            alice.close()
            bob.close()
