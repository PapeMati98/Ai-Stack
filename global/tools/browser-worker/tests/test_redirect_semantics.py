"""Document redirects must change Chromium's URL and origin, not just its body."""

import asyncio
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import threading
import time
from types import SimpleNamespace

import pytest

from test_browser_worker import bw


class RedirectPage(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        self.server.hits.append(self.path)
        if self.path in {"/redirect", "/changed-final"}:
            self.send_response(302)
            self.send_header("Location", self.server.target)
            self.end_headers()
            return
        if self.path == "/loop":
            self.send_response(302)
            self.send_header("Location", "/loop")
            self.end_headers()
            return
        pages = {
            "/source": "<script>localStorage.identity='SOURCE'</script><a id='go' href='/redirect'>Go</a>",
            "/target": "<script>localStorage.identity='TARGET'</script><p>Target seeded</p>",
            "/final/index": """<!doctype html><p id='state'></p><form action='submit'></form>
                <img src='relative.png'><script>
                document.querySelector('#state').textContent=localStorage.identity || 'EMPTY';
                </script>""",
        }
        content = pages.get(self.path, "<p>Synthetic</p>").encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        self.wfile.write(content)


@pytest.fixture
def redirect_origins():
    servers, threads = [], []
    for _ in range(2):
        server = ThreadingHTTPServer(("127.0.0.1", 0), RedirectPage)
        server.hits = []
        server.target = f"http://127.0.0.1:{server.server_port}/final/index"
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        servers.append(server)
        threads.append(thread)
    servers[0].target = servers[1].target
    try:
        yield servers
    finally:
        for server, thread in zip(servers, threads):
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)


@pytest.fixture
def browser(tmp_path, redirect_origins):
    worker = bw.BrowserWorker(tmp_path, allow_hosts={"127.0.0.1", "synthetic.test"},
                              allow_private_hosts=[f"127.0.0.1:{server.server_port}" for server in redirect_origins])
    try:
        worker.call("open", {"profile": "test"})
        yield worker
    finally:
        worker.close()


@pytest.mark.parametrize("via_click", [False, True])
def test_redirect_preserves_final_url_origin_storage_and_relative_urls(browser, redirect_origins, via_click):
    source, target = [f"http://127.0.0.1:{server.server_port}" for server in redirect_origins]
    browser.call("navigate", {"profile": "test", "url": target + "/target"})
    browser.call("navigate", {"profile": "test", "url": source + "/source"})
    if via_click:
        browser.call("click", {"profile": "test", "selector": "#go"})
    else:
        result = browser.call("navigate", {"profile": "test", "url": source + "/redirect"})
        assert result["url"] == target + "/final/index"
    assert browser.call("text", {"profile": "test"})["untrusted_content"]["text"] == "TARGET"
    async def inspect():
        return await browser.sessions["test"].page.evaluate("""() => ({
            url: location.href, origin: location.origin,
            form: document.querySelector('form').action
        })""")
    assert browser.submit_coroutine(inspect()) == {"url": target + "/final/index", "origin": target, "form": target + "/final/submit"}
    assert "/final/relative.png" in redirect_origins[1].hits
    assert "/final/relative.png" not in redirect_origins[0].hits


def test_http_https_redirect_keeps_chromium_secure_context_without_tls_or_public_network(browser, redirect_origins, monkeypatch):
    from playwright.async_api import Route
    local = f"http://127.0.0.1:{redirect_origins[1].server_port}"
    redirect_origins[0].target = "https://synthetic.test/final/index"
    original_fetch = Route.fetch
    async def synthetic_fetch(route, **kwargs):
        # Test-only response transport: every actual socket still uses 127.0.0.1.
        # Chromium sees the genuine https URL through its normal route handling;
        # no certificate, TLS opt-out, public DNS or external website is used.
        if kwargs["url"].startswith("https://synthetic.test/"):
            kwargs["url"] = local + kwargs["url"].removeprefix("https://synthetic.test")
        return await original_fetch(route, **kwargs)
    monkeypatch.setattr(Route, "fetch", synthetic_fetch)
    source = f"http://127.0.0.1:{redirect_origins[0].server_port}"
    assert browser.call("navigate", {"profile": "test", "url": source + "/redirect"})["url"] == "https://synthetic.test/final/index"
    async def inspect():
        return await browser.sessions["test"].page.evaluate("""() => ({
            origin: location.origin, secure: isSecureContext,
            crypto: typeof crypto.subtle, form: document.querySelector('form').action
        })""")
    assert browser.submit_coroutine(inspect()) == {"origin": "https://synthetic.test", "secure": True, "crypto": "object", "form": "https://synthetic.test/final/submit"}


@pytest.mark.parametrize("blocked", [None, "http://127.0.0.1:11440/private", "file:///private", "http://service.localhost/private"])
def test_main_document_checks_each_redirect_and_preserves_method_without_network(blocked):
    from playwright.async_api import Error
    async def exercise():
        worker = object.__new__(bw.BrowserWorker)
        worker.allow_hosts = {"example.com"}
        worker.allow_private_hosts = set()
        worker.navigations = {}
        worker.redirect_tasks = {}
        fetched, fulfilled, aborted = [], [], []

        class Frame:
            async def goto(self, url, **kwargs):
                assert kwargs["wait_until"] == "load"
                route = Route(url)
                await worker._route(route)
                if route.aborted:
                    raise Error("synthetic aborted navigation")

        frame = Frame()
        class Response:
            status = 307
            headers = {"location": "https://example.com/final"}
            async def dispose(self):
                pass
            async def body(self):
                return b"synthetic"

        class Route:
            def __init__(self, url):
                self.request = SimpleNamespace(url=url, method="POST" if url.startswith("http:") else "GET",
                    headers={"authorization": "synthetic", "cookie": "synthetic=1"}, post_data_buffer=b"synthetic",
                    frame=frame, is_navigation_request=lambda: True)
                self.aborted = False
            async def fetch(self, **kwargs):
                fetched.append(kwargs)
                response = Response()
                if kwargs["url"].startswith("https:"):
                    response.status, response.headers = (302, {"location": blocked}) if blocked else (200, {})
                return response
            async def fulfill(self, **kwargs):
                fulfilled.append(self.request.url)
            async def abort(self, reason):
                self.aborted = True
                aborted.append(self.request.url)

        if blocked:
            with pytest.raises(Error):
                await worker._navigate(frame, "http://example.com/start")
            assert fulfilled == []
            assert aborted == ["http://example.com/start", "https://example.com/final"]
        else:
            await worker._navigate(frame, "http://example.com/start")
            assert fulfilled == ["https://example.com/final"]
            assert aborted == ["http://example.com/start"]
        assert len(fetched) == 2
        assert fetched[1]["method"] == "POST" and fetched[1]["post_data"] == b"synthetic"
        assert not {"authorization", "cookie"} & fetched[1]["headers"].keys()
        assert worker.navigations == {}
    asyncio.run(exercise())


def test_main_document_redirect_limit_and_same_document_shortcut_fail_closed():
    from playwright.async_api import Error
    async def exercise(shortcut):
        worker = object.__new__(bw.BrowserWorker)
        worker.allow_hosts = {"example.com"}
        worker.allow_private_hosts = set()
        worker.navigations, worker.redirect_tasks = {}, {}
        hits = []
        class Frame:
            async def goto(self, url, **kwargs):
                if shortcut and hits:
                    return None
                route = Route(url)
                await worker._route(route)
                raise Error("synthetic aborted")
        frame = Frame()
        class Response:
            status = 302
            headers = {"location": "/loop"}
            async def dispose(self):
                pass
        class Route:
            def __init__(self, url):
                self.request = SimpleNamespace(url=url, method="GET", headers={}, post_data_buffer=None,
                                               frame=frame, is_navigation_request=lambda: True)
            async def fetch(self, **kwargs):
                hits.append(kwargs["url"])
                return Response()
            async def abort(self, reason):
                assert reason == "aborted"
            async def fulfill(self, **kwargs):
                pytest.fail("Redirect document fulfilled at original URL")
        with pytest.raises((Error, bw.WorkerError)):
            await worker._navigate(frame, "https://example.com/loop")
        assert len(hits) == (1 if shortcut else bw.MAX_REDIRECTS + 1)
        assert not worker.navigations
    asyncio.run(exercise(False))
    asyncio.run(exercise(True))


def test_unrelated_navigation_cannot_replay_redirect_credentials_to_another_allowed_origin():
    async def exercise():
        worker = object.__new__(bw.BrowserWorker)
        worker.allow_hosts = {"example.com", "other.example"}
        worker.allow_private_hosts = set()
        frame = object()
        worker.navigations = {frame: bw.Navigation(deadline=time.monotonic() + 30, request={
            "url": "https://example.com/final", "method": "POST", "post_data": b"synthetic",
            "headers": {"authorization": "synthetic"}})}
        class Route:
            request = SimpleNamespace(url="https://other.example/unrelated", method="GET", headers={},
                post_data_buffer=None, frame=frame, is_navigation_request=lambda: True)
            aborted = False
            async def fetch(self, **kwargs):
                pytest.fail("Redirect credentials replayed to unrelated origin")
            async def abort(self, reason):
                self.aborted = True
        route = Route()
        await worker._route(route)
        assert route.aborted
    asyncio.run(exercise())
