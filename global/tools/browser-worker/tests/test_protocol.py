"""Executable API/protocol checks that need neither profiles nor Chromium."""

import asyncio
import http.client
import importlib.util
import json
from pathlib import Path
import secrets
import os
import subprocess
import sys
import threading
from types import SimpleNamespace

import pytest


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("browser_worker_protocol", ROOT / "browser_worker.py")
bw = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = bw
SPEC.loader.exec_module(bw)


@pytest.fixture
def api():
    server = bw.LocalServer(("127.0.0.1", 0), bw.Handler)
    server.token = secrets.token_urlsafe(32)
    server.worker = bw.BrowserWorker(ROOT, {"127.0.0.1"}, max_pages=1)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server
    server.shutdown()
    server.server_close()
    thread.join(timeout=5)


def send(server, payload, method="POST", token=None, **extra_headers):
    connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=5)
    headers = {"Content-Type": "application/json", **extra_headers}
    if token is not None:
        headers["Authorization"] = "Bearer " + token
    connection.request(method, "/call", json.dumps(payload), headers)
    response = connection.getresponse()
    body = response.read()
    connection.close()
    return response.status, json.loads(body) if body else {}


def test_api_auth_and_bind_without_browser(api, capsys):
    assert api.server_address[0] == "127.0.0.1"
    for method in ("POST", "GET", "OPTIONS", "PATCH", "HEAD", "TRACE", "CONNECT", "UNKNOWN"):
        for token in (None, "wrong", "\u00e9"):
            status, _ = send(api, {}, method, token)
            assert status == 401
        assert send(api, {}, method, api.token)[0] == (400 if method == "POST" else 405)
    # Kernel binding to the literal loopback interface excludes every NIC;
    # inspect the real socket without sending any traffic beyond 127.0.0.1.
    assert api.socket.getsockname() == ("127.0.0.1", api.server_port)
    output = capsys.readouterr()
    assert api.token not in output.out + output.err


@pytest.mark.parametrize("url", ["file:///tmp/page", "data:text/plain,x", "chrome://settings", "javascript:1", "ftp://127.0.0.1/x", "http://localhost/", "http://127.0.0.1.attacker.invalid/", "http://u:p@127.0.0.1/", "http://127.0.0.1:99999/", "http://127.0.0.1\\@localhost/", "http://127.0.0.1/\nsecret"])
def test_api_rejects_url_before_launch(api, url):
    status, body = send(api, {"operation": "navigate", "arguments": {"profile": "test", "url": url}}, token=api.token)
    assert status == 400
    assert body["error"]["code"] == "url_not_allowed"
    assert api.worker.playwright is None


def test_api_request_limits_and_browser_origin(api):
    valid = {"operation": "navigate", "arguments": {"profile": "test", "url": "http://127.0.0.1/"}}
    assert send(api, valid, token=api.token, Origin="http://127.0.0.1")[0] == 403
    assert send(api, valid, token=api.token, Host="attacker.invalid")[0] == 403
    assert send(api, {"padding": "a" * (64 * 1024)}, token=api.token)[0] == 413
    for operation, arguments in (([], {}), ("open", {"profile": "test", "headed": True}), ("open", {"profile": "../test"}), ("type", {"profile": "test", "selector": "x", "text": "a" * 17000})):
        assert send(api, {"operation": operation, "arguments": arguments}, token=api.token)[0] == 400
    assert api.worker.playwright is None


def test_mcp_contract_and_notifications():
    init = bw.mcp_response({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2024-11-05"}})
    assert init["result"]["protocolVersion"] == "2024-11-05"
    assert init["result"]["capabilities"] == {"tools": {}}
    assert bw.mcp_response({"jsonrpc": "2.0", "method": "notifications/initialized"}) is None
    listed = bw.mcp_response({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
    assert {tool["name"] for tool in listed["result"]["tools"]} == {"open", "navigate", "text", "screenshot", "click", "type", "close"}
    assert all(tool["inputSchema"]["additionalProperties"] is False for tool in listed["result"]["tools"])
    error = bw.mcp_response({"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "navigate", "arguments": {"profile": "test", "url": "file:///x"}}})
    assert error["error"]["code"] == -32602


def test_untrusted_page_cannot_bypass_text_cap():
    result = bw.bounded_text({"text": "\U0001f642" * 40000, "truncated": False, "trusted": True})
    assert result["truncated"] is True
    assert len(result["text"].encode("utf-8")) == 64 * 1024
    assert set(result) == {"text", "truncated"}
    result = bw.bounded_text({"text": "x" + "\U0001f642" * 40000})
    assert len(result["text"].encode("utf-8")) == 64 * 1024 - 3
    assert "\ufffd" not in result["text"]


@pytest.mark.parametrize("operation", ["click", "type"])
@pytest.mark.parametrize("interaction_fails", [False, True])
def test_interaction_waits_for_popup_close(operation, interaction_fails):
    """Reproduce a page event whose asynchronous close outlives the action.

    Test doubles isolate scheduling; actual Chromium is checked separately.
    """
    from playwright.async_api import Error

    async def exercise():
        background_closes = []
        page = SimpleNamespace(url="http://127.0.0.1/", is_closed=lambda: False)
        page.main_frame = object()
        context = SimpleNamespace(pages=[page])

        class Popup:
            async def close(self):
                await asyncio.sleep(0)
                if self in context.pages:
                    context.pages.remove(self)

        class Locator:
            async def evaluate(self, expression, *, timeout):
                return {"file": False, "href": None}

            async def click(self, *, timeout):
                popup = Popup()
                context.pages.append(popup)
                # Match Playwright's fire-and-forget async event callback.
                background_closes.append(asyncio.create_task(popup.close()))
                if interaction_fails:
                    raise Error("synthetic interaction failure")

            async def fill(self, text, *, timeout):
                await self.click(timeout=timeout)

        page.locator = lambda selector: Locator()
        worker = object.__new__(bw.BrowserWorker)
        worker.operation_lock = asyncio.Lock()
        worker.redirect_tasks = {}
        worker.allow_hosts = {"127.0.0.1"}
        worker.allow_private_hosts = {("127.0.0.1", None)}
        worker.sessions = {"test": bw.Session(context, page)}
        arguments = {"profile": "test", "selector": "#popup"}
        if operation == "type":
            arguments["text"] = "synthetic"
        try:
            if interaction_fails:
                with pytest.raises(bw.WorkerError, match="browser_operation_failed"):
                    await worker._call(operation, arguments)
            else:
                assert (await worker._call(operation, arguments))["completed"] is True
            # No test-side wait: the operation must establish this invariant.
            assert context.pages == [page]
        finally:
            await asyncio.gather(*background_closes)

    asyncio.run(exercise())


def test_installed_node_child_launches_are_hidden():
    import playwright
    node = Path(playwright.__file__).parent / "driver" / ("node.exe" if os.name == "nt" else "node")
    script = r"""
        const cp = require('node:child_process');
        const calls=[];
        for(const name of ['spawn','spawnSync','execFileSync','fork','execFile','exec','execSync']) {
            cp[name] = function(...args) {
                const options=args.find(a => a && typeof a === 'object' && !Array.isArray(a));
                calls.push({name, options, callback:args.some(a => typeof a === 'function')});
                return 'stub';
            };
        }
        require(process.argv[1]);
        for(const name of ['spawn','spawnSync','execFileSync','fork']) {
            cp[name]('synthetic', ['argument'], {windowsHide:false, marker:123});
            cp[name]('synthetic', {windowsHide:false});
        }
        cp.execFile('synthetic', ['argument'], {windowsHide:false}, ()=>{});
        cp.execFile('synthetic', ()=>{});
        cp.execFile('synthetic', ['argument'], ()=>{});
        cp.exec('synthetic', {windowsHide:false}, ()=>{});
        cp.exec('synthetic', ()=>{});
        cp.execSync('synthetic', {windowsHide:false});
        process.stdout.write(JSON.stringify(calls));
    """
    completed = subprocess.run([str(node), "-e", script, str(ROOT / "hide_children.cjs")], text=True, capture_output=True, timeout=10, **bw.hidden_process_kwargs())
    assert completed.returncode == 0, completed.stderr
    calls = json.loads(completed.stdout)
    assert len(calls) == 14
    assert all(call["options"]["windowsHide"] is True for call in calls)
    assert all(call["callback"] for call in calls if call["name"] in {"execFile", "exec"})
    assert calls[0]["options"]["marker"] == 123


@pytest.mark.skipif(os.name != "nt", reason="Windows console/GUI launch policy")
def test_explicit_headed_only_exposes_chromium_gui():
    import playwright
    node = Path(playwright.__file__).parent / "driver" / "node.exe"
    script = r"""
        const cp=require('node:child_process'); const hidden=[];
        cp.spawn=(file,args,options)=>hidden.push(options.windowsHide);
        require(process.argv[1]);
        cp.spawn('chrome.exe',['--user-data-dir=synthetic'],{});
        cp.spawn('chrome.exe',['--user-data-dir=synthetic','--headless'],{});
        cp.spawn('node.exe',['--user-data-dir=synthetic'],{});
        cp.spawn('chrome.exe',[],{});
        process.stdout.write(JSON.stringify(hidden));
    """
    for explicit, expected in (("0", [True, True, True, True]), ("1", [False, True, True, True])):
        env = os.environ.copy()
        env["BROWSER_WORKER_HEADED"] = explicit
        completed = subprocess.run([str(node), "-e", script, str(ROOT / "hide_children.cjs")], text=True, capture_output=True, env=env, timeout=10, **bw.hidden_process_kwargs())
        assert completed.returncode == 0, completed.stderr
        assert json.loads(completed.stdout) == expected
    error = bw.mcp_response({"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": [], "arguments": {}}})
    assert error["error"]["code"] == -32602
