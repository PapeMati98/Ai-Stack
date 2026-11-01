"""Exercise the real installed Node HTTP/decompression transport, on loopback."""

import gzip
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import subprocess
import threading
import time
import zlib

import pytest

from test_browser_worker import bw, ROOT


class LimitedPage(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    sent = {}
    brotli_body = None

    def log_message(self, *args):
        pass

    def do_GET(self):
        self.close_connection = True
        self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        if self.path == "/declared":
            self.send_header("Content-Length", str(bw.HTTP_BODY_LIMIT + 1))
        elif self.path == "/chunked":
            self.send_header("Transfer-Encoding", "chunked")
        elif self.path in {"/gzip", "/deflate", "/brotli"}:
            data = b"x" * (bw.HTTP_BODY_LIMIT + 1)
            data = (gzip.compress(data) if self.path == "/gzip" else
                    zlib.compress(data) if self.path == "/deflate" else self.brotli_body)
            self.send_header("Content-Encoding", "br" if self.path == "/brotli" else self.path[1:])
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            try:
                self.wfile.write(data)
            except (BrokenPipeError, ConnectionResetError):
                pass
            return
        elif self.path == "/attachment":
            self.send_header("Content-Disposition", 'attachment; filename="synthetic.bin"')
        self.end_headers()
        sent = 0
        try:
            total = bw.HTTP_BODY_LIMIT if self.path == "/fits" else bw.HTTP_BODY_LIMIT * 2
            while sent < total:
                chunk = b"x" * min(64 * 1024, total - sent)
                if self.path == "/chunked":
                    self.wfile.write(f"{len(chunk):x}\r\n".encode() + chunk + b"\r\n")
                else:
                    self.wfile.write(chunk)
                sent += len(chunk)
                # Bound the synthetic producer's speed so rejection is observable
                # without relying on the host's TCP buffer size or timing races.
                time.sleep(0.001)
            if self.path == "/chunked":
                self.wfile.write(b"0\r\n\r\n")
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            type(self).sent[self.path] = sent


@pytest.fixture
def limited_origin():
    import playwright
    node = Path(playwright.__file__).parent / "driver" / ("node.exe" if os.name == "nt" else "node")
    if LimitedPage.brotli_body is None:
        # Brotli creation uses the already installed Node stdlib, not a package.
        compressed = subprocess.run([str(node), "-e",
            "process.stdout.write(require('node:zlib').brotliCompressSync(Buffer.alloc(8*1024*1024+1, 'x')));"],
            capture_output=True, timeout=10, **bw.hidden_process_kwargs())
        assert compressed.returncode == 0
        LimitedPage.brotli_body = compressed.stdout
    LimitedPage.sent.clear()
    server = ThreadingHTTPServer(("127.0.0.1", 0), LimitedPage)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}"
    server.shutdown()
    server.server_close()
    thread.join(timeout=5)


@pytest.mark.parametrize("path", ["/declared", "/no-length", "/chunked", "/attachment", "/gzip", "/deflate", "/brotli"])
def test_driver_caps_responses_before_buffering(limited_origin, path):
    import playwright

    # No browser/profile/token is needed: route.fetch uses this same installed
    # APIRequestContext implementation and the same preloaded transport guard.
    driver = Path(playwright.__file__).parent / "driver"
    node = driver / ("node.exe" if os.name == "nt" else "node")
    script = """
        const { request } = require(process.argv[1]);
        (async () => {
            const context = await request.newContext();
            try {
                let blocked = false;
                try { await context.get(process.argv[2] + process.argv[3], {timeout:5000}); }
                catch (_) { blocked = true; }
                // Overflow must abort the stream, without crashing the driver.
                const response = await context.get(process.argv[2] + '/fits', {timeout:5000});
                const length = (await response.body()).length;
                await response.dispose();
                process.stdout.write(JSON.stringify({blocked, length}));
            } finally { await context.dispose(); }
        })().catch(error => { process.stderr.write(String(error)); process.exitCode = 1; });
    """
    completed = subprocess.run([str(node), "--require", str(ROOT / "hide_children.cjs"),
        "--require", str(ROOT / "bounded_network.cjs"), "-e", script, str(driver / "package"), limited_origin, path],
        text=True, capture_output=True, timeout=20, **bw.hidden_process_kwargs())
    assert completed.returncode == 0, completed.stderr
    assert json.loads(completed.stdout) == {"blocked": True, "length": bw.HTTP_BODY_LIMIT}
    if path in {"/declared", "/no-length", "/chunked", "/attachment"}:
        deadline = time.monotonic() + 2
        while path not in LimitedPage.sent and time.monotonic() < deadline:
            time.sleep(0.01)
        assert LimitedPage.sent[path] < bw.HTTP_BODY_LIMIT * 2
        if path == "/declared":
            assert LimitedPage.sent[path] < bw.HTTP_BODY_LIMIT
