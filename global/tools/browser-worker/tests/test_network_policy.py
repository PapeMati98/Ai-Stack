"""SSRF regressions: network I/O only against synthetic 127.0.0.1 servers."""

import asyncio
from http.server import ThreadingHTTPServer
import threading
from types import SimpleNamespace
from urllib.error import HTTPError
from urllib.request import ProxyHandler, build_opener

import pytest

from test_browser_worker import bw, call, origin, request, SyntheticPage
from test_protocol import api, send


@pytest.mark.parametrize("host", [
    "127.0.0.1", "localhost", "[::1]", "2130706433", "0x7f000001",
    "127.1", "0177.0.0.1", "0x7f.1", "127.000.000.001",
    "[::ffff:127.0.0.1]", "[::ffff:7f00:1]", "%31%32%37.0.0.1",
    "LOCALHOST.", "service.localhost", "service.localhost.",
    "10.0.0.1", "172.16.0.1", "192.168.0.1", "100.64.0.1",
    "100.127.255.254", "169.254.169.254", "0.1.2.3", "0",
    "[fc00::1]", "[fd00::1]", "[fe80::1]", "[::]", "[::127.0.0.1]",
])
def test_private_targets_default_deny_and_explicit_grant(host):
    url = f"http://{host}:8000/"
    with pytest.raises(bw.WorkerError, match="url_not_allowed"):
        bw.allowed_url(url, None)
    # Policy only: no DNS lookup or connection to IPv6/private networks.
    grant = {bw.private_host_entry(host + ":8000")}
    assert bw.allowed_url(url, None, grant) == url
    with pytest.raises(bw.WorkerError, match="url_not_allowed"):
        bw.allowed_url(f"http://{host}:8001/", None, grant)


@pytest.mark.parametrize("host", ["127.1", "2130706433", "0x7f000001", "0177.0.0.1", "[::ffff:127.0.0.1]", "%31%32%37.0.0.1"])
def test_numeric_aliases_share_canonical_private_grant(host):
    assert bw.private_host_entry(host) == ("127.0.0.1", None)
    assert bw.allowed_url(f"http://{host}/", None, {("127.0.0.1", 80)})


@pytest.mark.parametrize("url", ["http://8.8.8.8/", "https://example.com/", "http://100.128.0.1/", "http://172.32.0.1/", "http://[2606:4700:4700::1111]/"])
def test_public_url_policy_without_network(url):
    assert bw.allowed_url(url, None) == url


def test_private_grants_do_not_override_normal_host_allowlist():
    with pytest.raises(bw.WorkerError, match="url_not_allowed"):
        bw.allowed_url("http://127.0.0.1/", {"example.com"}, {("127.0.0.1", None)})
    with pytest.raises(bw.WorkerError, match="url_not_allowed"):
        bw.allowed_url("http://127.0.0.1/", {"127.0.0.1"})
    assert bw.allowed_url("https://localhost/", None, {("localhost", 443)})
    with pytest.raises(bw.WorkerError, match="url_not_allowed"):
        bw.allowed_url("http://localhost/", None, {("localhost", 443)})


def test_private_grants_are_startup_configuration_only(api, monkeypatch, capsys):
    captured = []

    class Server:
        home = "synthetic"
        server_port = 12345
        closed = False

        def serve_forever(self):
            raise KeyboardInterrupt

        def server_close(self):
            self.closed = True

    server = Server()
    def create(*args):
        captured.append(args)
        return server
    monkeypatch.setattr(bw, "create_server", create)
    assert bw.main(["serve", "--allow-private-host", "127.0.0.1:8000", "--allow-private-host", "[::1]:8001"]) == 0
    assert captured[0][-1] == ["127.0.0.1:8000", "[::1]:8001"]
    assert server.closed
    assert "12345" in capsys.readouterr().out
    status, result = send(api, {"operation": "navigate", "arguments": {
        "profile": "test", "url": "http://127.0.0.1/", "allow_private_hosts": ["127.0.0.1"]}}, token=api.token)
    assert (status, result["error"]["code"]) == (400, "invalid_arguments")


@pytest.mark.parametrize("value", ["localhost:0", "localhost:65536", "localhost:", "http://localhost", "user@localhost", "localhost/path", "[fe80::1%25eth0]"])
def test_invalid_private_grants_fail_closed(value):
    with pytest.raises(bw.WorkerError, match="invalid_allow_private_host"):
        bw.private_host_entry(value)


@pytest.fixture
def unrestricted_service(tmp_path, origin):
    # No public host allowlist. Only one exact private host/port is approved.
    authority = origin.removeprefix("http://")
    server = bw.create_server(home=tmp_path / "state", allow_private_hosts=[authority])
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server
    server.shutdown()
    server.server_close()
    thread.join(timeout=10)


def test_api_blocks_private_aliases_before_browser_launch(api, origin):
    # Real HTTP API with an in-memory synthetic token, no private profile/ACL.
    api.worker.allow_hosts = None
    for host in ("127.0.0.1", "localhost", "[::1]", "2130706433", "0x7f000001"):
        url = origin.replace("127.0.0.1", host) + "/"
        payload = {"operation": "navigate", "arguments": {"profile": "test", "url": url}}
        status, result = send(api, payload, token=api.token)
        assert (status, result["error"]["code"]) == (400, "url_not_allowed")
        # Owner start-time grants, unavailable in HTTP/MCP tool arguments.
        api.worker.allow_private_hosts = {bw.private_host_entry(host + ":" + origin.rsplit(":", 1)[1])}
        status, result = send(api, payload, token=api.token)
        assert (status, result["error"]["code"]) == (404, "profile_not_open")
        api.worker.allow_private_hosts.clear()
    assert api.worker.playwright is None


def test_chromium_numeric_aliases_with_private_grant(unrestricted_service, origin):
    call(unrestricted_service, "open", profile="test")
    for host in ("127.0.0.1", "2130706433", "0x7f000001", "0177.0.0.1", "127.1"):
        call(unrestricted_service, "navigate", profile="test", url=origin.replace("127.0.0.1", host) + "/")
        assert "UNTRUSTED" in call(unrestricted_service, "text", profile="test")["untrusted_content"]["text"]


def test_no_host_allowlist_checks_subrequests_and_every_redirect(unrestricted_service, origin):
    denied = ThreadingHTTPServer(("127.0.0.1", 0), SyntheticPage)
    thread = threading.Thread(target=denied.serve_forever, daemon=True)
    thread.start()
    # Set Location on the already running synthetic origin, not the worker.
    port = int(origin.rsplit(":", 1)[1])
    # The handler for the approved origin reads this fixture-owned mapping.
    previous = getattr(SyntheticPage, "denied_origin", None)
    SyntheticPage.denied_origin = f"http://127.0.0.1:{denied.server_port}"
    try:
        call(unrestricted_service, "open", profile="test")
        before = {path: SyntheticPage.hits.count(path) for path in ("/blocked", "/denied-port", "/denied-resource")}
        call(unrestricted_service, "navigate", profile="test", url=origin + "/private-resource")
        assert "Private resource blocked" in call(unrestricted_service, "text", profile="test")["untrusted_content"]["text"]
        for path in ("/redirect-private-port", "/redirect-chain"):
            status, result = request(unrestricted_service, {"operation": "navigate", "arguments": {"profile": "test", "url": origin + path}})
            assert status == 400, result
            call(unrestricted_service, "navigate", profile="test", url=origin + "/redirect-relative")
            assert "Allowed redirect destination" in call(unrestricted_service, "text", profile="test")["untrusted_content"]["text"]
        call(unrestricted_service, "navigate", profile="test", url=origin + "/redirected-resource")
        assert "Redirected resource blocked" in call(unrestricted_service, "text", profile="test")["untrusted_content"]["text"]
        assert {path: SyntheticPage.hits.count(path) for path in before} == before
        assert denied.server_port != port
    finally:
        SyntheticPage.denied_origin = previous
        denied.shutdown()
        denied.server_close()
        thread.join(timeout=5)


def test_public_redirect_hops_scheme_change_and_fail_closed_without_network():
    async def exercise(locations, allow_hosts=None):
        worker = object.__new__(bw.BrowserWorker)
        worker.allow_hosts = allow_hosts
        worker.allow_private_hosts = set()
        responses = []

        class Response:
            def __init__(self, status, headers):
                self.status, self.headers = status, headers
                self.disposed = False

            async def dispose(self):
                self.disposed = True

            async def body(self):
                return b"synthetic"

        class Route:
            request = SimpleNamespace(url="http://example.com/start", method="POST", headers={"authorization": "synthetic", "cookie": "synthetic=1", "content-type": "text/plain"}, post_data_buffer=b"synthetic", is_navigation_request=lambda: False)
            fetched = []
            action = None

            async def fetch(self, **kwargs):
                assert kwargs["max_redirects"] == 0
                self.fetched.append(kwargs)
                index = len(self.fetched) - 1
                response = Response(302, {"location": locations[index]}) if index < len(locations) else Response(200, {})
                responses.append(response)
                return response

            async def fulfill(self, response):
                self.action = "fulfilled"
                assert response.status == 200

            async def abort(self, reason):
                assert reason == "aborted"
                self.action = "aborted"

            async def continue_(self):
                pytest.fail("Unchecked browser redirect chain")

        route = Route()
        await worker._route(route)
        assert all(response.disposed for response in responses)
        return route

    permitted = asyncio.run(exercise(["https://example.com/next", "/final"], {"example.com"}))
    assert permitted.action == "fulfilled"
    assert [hop["url"] for hop in permitted.fetched] == ["http://example.com/start", "https://example.com/next", "https://example.com/final"]
    assert permitted.fetched[1]["method"] == "GET"
    assert permitted.fetched[1]["post_data"] == b""
    assert not {"authorization", "cookie", "content-type"} & permitted.fetched[1]["headers"].keys()
    for blocked in ("http://2130706433:11440/", "file:///x", "http://[::ffff:127.0.0.1]/", "http://service.localhost/"):
        denied = asyncio.run(exercise(["/allowed-hop", blocked]))
        assert denied.action == "aborted"
        assert len(denied.fetched) == 2
    loop = asyncio.run(exercise(["/loop"] * (bw.MAX_REDIRECTS + 1)))
    assert loop.action == "aborted"
    assert len(loop.fetched) == bw.MAX_REDIRECTS + 1


def test_private_routed_request_aborts_before_fetch_without_allowlist():
    worker = object.__new__(bw.BrowserWorker)
    worker.allow_hosts = None
    worker.allow_private_hosts = set()

    class Route:
        request = SimpleNamespace(url="http://127.0.0.1:11440/", method="GET", headers={}, post_data_buffer=None,
                                  is_navigation_request=lambda: False)
        action = None

        async def fetch(self, **kwargs):
            pytest.fail("Forbidden private subrequest was fetched")

        async def abort(self, reason):
            self.action = reason

    route = Route()
    asyncio.run(worker._route(route))
    assert route.action == "aborted"


def test_real_http_redirect_hops_refuse_unapproved_private_port(origin):
    denied = ThreadingHTTPServer(("127.0.0.1", 0), SyntheticPage)
    thread = threading.Thread(target=denied.serve_forever, daemon=True)
    thread.start()
    previous = getattr(SyntheticPage, "denied_origin", None)
    SyntheticPage.denied_origin = f"http://127.0.0.1:{denied.server_port}"
    worker = object.__new__(bw.BrowserWorker)
    worker.allow_hosts = None
    worker.allow_private_hosts = {bw.private_host_entry(origin.removeprefix("http://"))}

    class Response:
        def __init__(self, raw):
            self.raw = raw
            self.status = raw.status
            self.headers = {name.lower(): value for name, value in raw.headers.items()}
            self.disposed = False

        async def dispose(self):
            self.raw.close()
            self.disposed = True

        async def body(self):
            return self.raw.read()

    class Route:
        def __init__(self, path):
            self.request = SimpleNamespace(url=origin + path, method="GET", headers={}, post_data_buffer=None,
                                           is_navigation_request=lambda: False)
            self.fetched, self.responses = [], []
            self.action = None

        async def fetch(self, *, url, max_redirects, timeout, **kwargs):
            assert max_redirects == 0
            self.fetched.append(url)
            opener = build_opener(ProxyHandler({}), bw.NoRedirect())
            try:
                raw = opener.open(url, timeout=timeout / 1000)
            except HTTPError as error:
                raw = error
            response = Response(raw)
            self.responses.append(response)
            return response

        async def fulfill(self, response):
            self.action = "fulfilled"

        async def abort(self, reason):
            self.action = reason

        async def continue_(self):
            pytest.fail("Unchecked redirect chain")

    try:
        before = SyntheticPage.hits.count("/denied-port")
        for path, expected in (("/redirect-relative", "fulfilled"), ("/redirect-private-port", "aborted"), ("/redirect-chain", "aborted")):
            route = Route(path)
            asyncio.run(worker._route(route))
            assert route.action == expected
            assert all(response.disposed for response in route.responses)
            assert all(url.startswith(origin + "/") for url in route.fetched)
        assert SyntheticPage.hits.count("/denied-port") == before
    finally:
        SyntheticPage.denied_origin = previous
        denied.shutdown()
        denied.server_close()
        thread.join(timeout=5)
