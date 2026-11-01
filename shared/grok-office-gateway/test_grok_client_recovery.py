#!/usr/bin/env python3
"""Behavioral recovery tests for the Grok Office client (ask path).

Uses a mock transport and a fake clock — no live network, no real sleeping.
Tests assert observable behavior (exit codes, emitted JSON, request counts,
clock budgets), not implementation details.
"""

from __future__ import annotations

import hashlib
import io
import json
from pathlib import Path
import sys
import types
import unittest
from unittest import mock
import urllib.error
from urllib.parse import urlsplit

_MODULE_DIR = Path(__file__).parent.resolve()
if str(_MODULE_DIR) not in sys.path:
    sys.path.insert(0, str(_MODULE_DIR))

import grok_office_client as goc


class FakeClock:
    """Deterministic monotonic clock; sleep() advances time instantly."""

    def __init__(self, start: float = 1_000.0):
        self.now = start

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += max(0.0, seconds)


def _url_path(url: str) -> str:
    """Path component of a URL, without query string.

    The real client builds inbox URLs with a query string
    (``/v1/inbox?reply_to=<id>&peek=1``), so suffix matching on the raw URL
    would never hit the inbox handler. Match on the path only.
    """
    return urlsplit(url).path


class FakeTransport:
    """Scriptable replacement for goc._request_json.

    Records every call and answers from per-path handlers. Handlers receive
    the recorded call dict and return a (status_code, body) tuple.
    """

    def __init__(self):
        self.calls: list[dict] = []
        self.handlers: list[tuple[str, object]] = []

    def set(self, path_suffix: str, handler) -> None:
        # Later registrations win, so tests can override a default.
        self.handlers = [(s, h) for (s, h) in self.handlers if s != path_suffix]
        self.handlers.append((path_suffix, handler))

    def __call__(self, url, method="GET", headers=None, payload=None, timeout=30.0):
        call = {
            "url": url,
            "method": method,
            "headers": dict(headers or {}),
            "payload": payload,
            "timeout": timeout,
        }
        self.calls.append(call)
        path = _url_path(url)
        for suffix, handler in reversed(self.handlers):
            if path.endswith(suffix):
                return handler(call)
        return 200, {"status": "ok"}

    def count(self, method: str | None = None, path_suffix: str | None = None) -> int:
        n = 0
        for c in self.calls:
            if method is not None and c["method"] != method:
                continue
            if path_suffix is not None and not _url_path(c["url"]).endswith(path_suffix):
                continue
            n += 1
        return n


SENT_ID = "b4a49c697c30451890266914da4fcbe4"
EXPECTED_AGENT = "claude-ailab-main"
TEST_TOKEN = "test-token-value-xyz"


def make_env() -> dict:
    return {
        "GROK_OFFICE_URL": "https://gateway.example/grok-office",
        "GROK_OFFICE_TOKEN": TEST_TOKEN,
    }


def ok_health(call):
    return 200, {
        "status": "ok",
        "agent": "grokbot-cloud-agent-office",
        "targets": ["agent-office-thread"],
        "target_agents": {"agent-office-thread": EXPECTED_AGENT},
    }


def ok_send(call):
    return 200, {"id": SENT_ID, "ts": 1700000000, "status": "sent"}


def empty_inbox(call):
    return 200, {"messages": [], "remaining": 0, "cursor_advanced": False}


class AskRecoveryTestBase(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.transport = FakeTransport()
        self._orig_request_json = goc._request_json
        self._orig_time = goc.time
        goc._request_json = self.transport
        # The client module does `import time` and calls time.monotonic /
        # time.sleep; swap in a shim exposing only the fake clock.
        goc.time = types.SimpleNamespace(
            monotonic=self.clock.monotonic, sleep=self.clock.sleep
        )
        self.addCleanup(self._restore)

    def _restore(self):
        goc._request_json = self._orig_request_json
        goc.time = self._orig_time

    def run_ask(self, extra_args: list[str], body: str = "Recovery question body"):
        buf = io.StringIO()
        argv = ["ask", "--target", "agent-office-thread", "--body", body] + extra_args
        code = goc.run_client(argv, env=make_env(), stdout=buf, stdin=io.StringIO(""))
        out = json.loads(buf.getvalue().strip())
        return code, out, buf.getvalue()


class TestAskTimeoutBounds(AskRecoveryTestBase):
    """The requested 240 s budget is validated before network I/O."""

    def test_default_budget_is_240_not_600(self):
        self.transport.set("/v1/health", ok_health)
        self.transport.set("/v1/send", ok_send)
        self.transport.set("/v1/inbox", empty_inbox)

        code, out, _ = self.run_ask(["--poll-s", "20"])

        self.assertEqual(code, 3)
        self.assertEqual(out.get("code"), "timeout")
        # With a 240 s budget and 20 s polls the clock advances exactly 240 s.
        # A 600 s default would advance 600 s and poll 30 times.
        self.assertAlmostEqual(self.clock.now - 1_000.0, 240.0, places=6)
        self.assertEqual(self.transport.count(method="GET", path_suffix="/v1/inbox"), 12)

    def test_timeout_above_240_rejected_before_network(self):
        for bad in ["241", "600", "1000000"]:
            with self.subTest(bad=bad):
                self.transport.calls.clear()
                code, out, _ = self.run_ask(["--timeout-s", bad])
                self.assertEqual(code, 2)
                self.assertEqual(out.get("status"), "error")
                self.assertEqual(out.get("code"), "invalid_timeout")
                self.assertEqual(
                    self.transport.calls, [],
                    "no network request may be made for an out-of-bounds timeout",
                )

    def test_nonfinite_and_nonpositive_timeouts_rejected_before_network(self):
        for bad in ["nan", "inf", "0"]:
            with self.subTest(bad=bad):
                self.transport.calls.clear()
                code, out, _ = self.run_ask(["--timeout-s", bad])
                self.assertEqual(code, 2)
                self.assertEqual(out.get("code"), "invalid_timeout")
                self.assertEqual(self.transport.calls, [])

    def test_negative_timeout_rejected_before_network(self):
        # argparse treats a leading '-' as an option prefix; pass it via '='.
        self.transport.calls.clear()
        buf = io.StringIO()
        code = goc.run_client(
            ["ask", "--target", "agent-office-thread", "--body", "x",
             "--timeout-s=-5"],
            env=make_env(), stdout=buf, stdin=io.StringIO(""),
        )
        out = json.loads(buf.getvalue().strip())
        self.assertEqual(code, 2)
        self.assertEqual(out.get("code"), "invalid_timeout")
        self.assertEqual(self.transport.calls, [])

    def test_boundary_240_accepted(self):
        self.transport.set("/v1/health", ok_health)
        self.transport.set("/v1/send", ok_send)
        self.transport.set("/v1/inbox", empty_inbox)

        code, out, _ = self.run_ask(["--timeout-s", "240", "--poll-s", "240"])

        self.assertEqual(code, 3)
        self.assertEqual(out.get("code"), "timeout")
        self.assertGreater(self.transport.count(), 0, "a valid timeout must reach the network")

    def test_invalid_poll_interval_rejected_before_network(self):
        for bad in ["0", "nan", "inf"]:
            with self.subTest(bad=bad):
                self.transport.calls.clear()
                code, out, _ = self.run_ask(["--timeout-s", "10", "--poll-s", bad])
                self.assertEqual(code, 2)
                self.assertEqual(out.get("code"), "invalid_poll_interval")
                self.assertEqual(self.transport.calls, [])


class TestTimeoutCorrelation(AskRecoveryTestBase):
    """Finding 2: timeout output preserves sent_id and the original dedupe key."""

    def test_timeout_after_send_preserves_sent_id_and_dedupe_key(self):
        self.transport.set("/v1/health", ok_health)
        self.transport.set("/v1/send", ok_send)
        self.transport.set("/v1/inbox", empty_inbox)

        body = "Correlation probe question"
        expected_key = hashlib.sha256(
            f"agent-office-thread\nquestion\n{body}".encode("utf-8")
        ).hexdigest()[:40]

        code, out, raw = self.run_ask(
            ["--timeout-s", "30", "--poll-s", "10"], body=body
        )

        self.assertEqual(code, 3)
        self.assertEqual(out.get("status"), "error")
        self.assertEqual(out.get("code"), "timeout")
        self.assertEqual(out.get("sent_id"), SENT_ID)
        self.assertEqual(out.get("dedupe_key"), expected_key)
        # No secrets in the timeout report.
        self.assertNotIn(TEST_TOKEN, raw)
        self.assertNotIn("Authorization", raw)
        # Exactly one send: a timeout must not trigger a resend.
        self.assertEqual(self.transport.count(method="POST", path_suffix="/v1/send"), 1)
        # Reads are peeks: no cursor advance is requested.
        for c in self.transport.calls:
            if _url_path(c["url"]).endswith("/v1/inbox"):
                self.assertIn("peek=1", c["url"])
        self.assertEqual(self.transport.count(method="POST", path_suffix="/v1/advance"), 0)

    def test_timeout_before_send_preserves_dedupe_key_without_sent_id(self):
        def health_timeout(call):
            return 0, {"status": "error", "code": "timeout", "message": "request timed out"}

        self.transport.set("/v1/health", health_timeout)

        body = "Early timeout probe"
        expected_key = hashlib.sha256(
            f"agent-office-thread\nquestion\n{body}".encode("utf-8")
        ).hexdigest()[:40]

        code, out, _ = self.run_ask(["--timeout-s", "30", "--poll-s", "10"], body=body)

        self.assertEqual(code, 3)
        self.assertEqual(out.get("code"), "timeout")
        self.assertEqual(out.get("dedupe_key"), expected_key)
        self.assertNotIn("sent_id", out, "no id exists before a successful send")
        self.assertEqual(self.transport.count(method="POST", path_suffix="/v1/send"), 0)

    def test_scan_incomplete_preserves_sent_id_and_dedupe_key(self):
        def backlog(call):
            return 200, {
                "messages": [],
                "remaining": 7,
                "scan_incomplete": True,
                "cursor_advanced": False,
            }

        self.transport.set("/v1/health", ok_health)
        self.transport.set("/v1/send", ok_send)
        self.transport.set("/v1/inbox", backlog)

        body = "Backlog probe question"
        expected_key = hashlib.sha256(
            f"agent-office-thread\nquestion\n{body}".encode("utf-8")
        ).hexdigest()[:40]

        code, out, _ = self.run_ask(
            ["--timeout-s", "30", "--poll-s", "10"], body=body
        )

        self.assertEqual(code, 4)
        self.assertEqual(out.get("code"), "backlog_exceeds_scan")
        self.assertEqual(out.get("sent_id"), SENT_ID)
        self.assertEqual(out.get("dedupe_key"), expected_key)
        self.assertEqual(out.get("remaining"), 7)
        # No resend and no cursor advance on an incomplete scan.
        self.assertEqual(self.transport.count(method="POST", path_suffix="/v1/send"), 1)
        self.assertEqual(self.transport.count(method="POST", path_suffix="/v1/advance"), 0)


class TestAskHealthFailureCorrelation(AskRecoveryTestBase):
    """Health failures retain the caller's operation handle."""

    def test_health_http_and_connection_errors_preserve_supplied_dedupe_key(self):
        dedupe_key = "caller-supplied-operation-key"
        cases = [
            ("http", 503, {"status": "error", "code": "http_error", "http_status": 503}),
            ("connection", 0, {"status": "error", "code": "connection_error"}),
        ]

        for label, status, response in cases:
            with self.subTest(kind=label):
                self.transport.calls.clear()
                self.transport.set("/v1/health", lambda call: (status, response))

                code, out, _ = self.run_ask(
                    ["--dedupe-key", dedupe_key, "--timeout-s", "30", "--poll-s", "10"]
                )

                self.assertEqual(code, 2)
                self.assertEqual(out.get("dedupe_key"), dedupe_key)
                self.assertEqual(out.get("code"), response["code"])
                self.assertEqual(self.transport.count(method="POST", path_suffix="/v1/send"), 0)


class TestDeadlineBoundary(AskRecoveryTestBase):
    """Boundary regression: a successful send that lands exactly at the
    deadline must still report the known id in the timeout result."""

    def test_send_success_exactly_at_deadline_preserves_sent_id(self):
        # The send handler answers successfully but advances the fake clock
        # to exactly the deadline, so the post-send deadline check fires.
        def send_at_deadline(call):
            self.clock.now = 1_000.0 + 30.0
            return 200, {"id": SENT_ID, "ts": 1700000000, "status": "sent"}

        self.transport.set("/v1/health", ok_health)
        self.transport.set("/v1/send", send_at_deadline)
        self.transport.set("/v1/inbox", empty_inbox)

        body = "Deadline boundary probe"
        expected_key = hashlib.sha256(
            f"agent-office-thread\nquestion\n{body}".encode("utf-8")
        ).hexdigest()[:40]

        code, out, _ = self.run_ask(
            ["--timeout-s", "30", "--poll-s", "10"], body=body
        )

        self.assertEqual(code, 3)
        self.assertEqual(out.get("code"), "timeout")
        # The send succeeded and returned an id before the deadline check;
        # that id must be preserved so the caller can correlate a retry.
        self.assertEqual(out.get("sent_id"), SENT_ID)
        self.assertEqual(out.get("dedupe_key"), expected_key)
        # No resend, no polling after the deadline, no cursor advance.
        self.assertEqual(self.transport.count(method="POST", path_suffix="/v1/send"), 1)
        self.assertEqual(self.transport.count(method="GET", path_suffix="/v1/inbox"), 0)
        self.assertEqual(self.transport.count(method="POST", path_suffix="/v1/advance"), 0)

    def test_polling_http_error_after_send_preserves_sent_id_and_dedupe_key(self):
        # The send succeeds; the first inbox poll then fails with an HTTP
        # error. The correlation material must survive that failure.
        def inbox_http_error(call):
            return 500, {"status": "error", "code": "http_error", "message": "boom"}

        self.transport.set("/v1/health", ok_health)
        self.transport.set("/v1/send", ok_send)
        self.transport.set("/v1/inbox", inbox_http_error)

        body = "Polling failure probe"
        expected_key = hashlib.sha256(
            f"agent-office-thread\nquestion\n{body}".encode("utf-8")
        ).hexdigest()[:40]

        code, out, _ = self.run_ask(
            ["--timeout-s", "30", "--poll-s", "10"], body=body
        )

        self.assertEqual(code, 2)
        self.assertEqual(out.get("status"), "error")
        self.assertEqual(out.get("code"), "http_error")
        self.assertEqual(out.get("sent_id"), SENT_ID)
        self.assertEqual(out.get("dedupe_key"), expected_key)
        # Exactly one send: a polling failure must not trigger a resend.
        self.assertEqual(self.transport.count(method="POST", path_suffix="/v1/send"), 1)
        self.assertEqual(self.transport.count(method="POST", path_suffix="/v1/advance"), 0)


class TestLateSendErrorCorrelation(AskRecoveryTestBase):
    """A late non-2xx send remains explicitly unconfirmed on timeout."""

    def test_late_http_error_preserves_unconfirmed_outcome_and_dedupe_key(self):
        self.transport.set("/v1/health", ok_health)

        def late_http_error(call):
            self.clock.now = 1_000.0 + 30.0
            return 500, {
                "status": "error",
                "code": "http_error",
                "http_status": 500,
                "message": "gateway error",
            }

        self.transport.set("/v1/send", late_http_error)
        body = "Late send error correlation probe"
        expected_key = hashlib.sha256(
            f"agent-office-thread\nquestion\n{body}".encode("utf-8")
        ).hexdigest()[:40]

        code, out, _ = self.run_ask(
            ["--timeout-s", "30", "--poll-s", "10"], body=body
        )

        self.assertEqual(code, 3)
        self.assertEqual(out.get("code"), "timeout")
        self.assertEqual(out.get("dedupe_key"), expected_key)
        self.assertEqual(out.get("send_outcome"), "unconfirmed")
        self.assertNotIn("sent_id", out)
        self.assertEqual(self.transport.count(method="POST", path_suffix="/v1/send"), 1)
        self.assertEqual(self.transport.count(method="GET", path_suffix="/v1/inbox"), 0)


class TestPerCallTimeoutBudget(AskRecoveryTestBase):
    """Each HTTP call's timeout is capped by the remaining polling budget.

    The fake clock checks the client's budget arithmetic. It does not model
    urllib's behavior for streamed responses or prove a hard wall-clock cap.
    """

    def test_final_poll_timeout_capped_by_remaining_budget(self):
        # Repro: --timeout-s 240 --poll-s 239.75. The final poll starts with
        # 0.25 s remaining; its per-call timeout must be 0.25 s, not 1 s.
        inbox_timeouts = []

        def record_inbox(call):
            inbox_timeouts.append(call["timeout"])
            return 200, {"messages": [], "remaining": 0, "cursor_advanced": False}

        self.transport.set("/v1/health", ok_health)
        self.transport.set("/v1/send", ok_send)
        self.transport.set("/v1/inbox", record_inbox)

        code, out, _ = self.run_ask(["--timeout-s", "240", "--poll-s", "239.75"])

        self.assertEqual(code, 3)
        self.assertEqual(out.get("code"), "timeout")
        self.assertEqual(len(inbox_timeouts), 2)
        self.assertAlmostEqual(inbox_timeouts[0], 30.0, places=6)
        self.assertAlmostEqual(inbox_timeouts[-1], 0.25, places=6)
        # The fake clock reaches the requested budget after bounded calls.
        self.assertAlmostEqual(self.clock.now, 1_000.0 + 240.0, places=6)

    def test_no_call_timeout_exceeds_total_budget(self):
        # With a budget smaller than the 30 s per-call cap, every call must
        # receive exactly the remaining budget.
        self.transport.set("/v1/health", ok_health)
        self.transport.set("/v1/send", ok_send)
        self.transport.set("/v1/inbox", empty_inbox)

        code, out, _ = self.run_ask(["--timeout-s", "0.5", "--poll-s", "0.5"])

        self.assertEqual(code, 3)
        self.assertEqual(out.get("code"), "timeout")
        self.assertGreater(len(self.transport.calls), 0)
        for c in self.transport.calls:
            self.assertLessEqual(c["timeout"], 0.5)
        self.assertAlmostEqual(self.clock.now, 1_000.0 + 0.5, places=6)

    def test_ack_timeout_capped_by_remaining_budget(self):
        # A reply arriving just before the deadline: the ack must not be
        # issued with a timeout beyond the remaining budget.
        reply_id = "aaaabbbbccccddddeeeeffff00001111"
        ack_timeouts = []

        def inbox_with_reply(call):
            self.clock.now = 1_000.0 + 30.0 - 0.25
            return 200, {
                "messages": [
                    {
                        "id": reply_id,
                        "from": EXPECTED_AGENT,
                        "reply_to": SENT_ID,
                        "body": "The answer",
                    }
                ],
                "remaining": 0,
                "cursor_advanced": False,
            }

        def record_ack(call):
            ack_timeouts.append(call["timeout"])
            return 200, {"status": "ok"}

        self.transport.set("/v1/health", ok_health)
        self.transport.set("/v1/send", ok_send)
        self.transport.set("/v1/inbox", inbox_with_reply)
        self.transport.set("/v1/ack", record_ack)

        code, out, _ = self.run_ask(["--timeout-s", "30", "--poll-s", "10"])

        self.assertEqual(code, 0)
        self.assertEqual(out.get("id"), reply_id)
        self.assertEqual(len(ack_timeouts), 1)
        self.assertAlmostEqual(ack_timeouts[0], 0.25, places=6)


class TestRequestJsonErrorNormalization(unittest.TestCase):
    """Finding: exceptions while reading or decoding an HTTPError body must
    not escape error normalization."""

    @staticmethod
    def _http_error(fp) -> urllib.error.HTTPError:
        return urllib.error.HTTPError(
            "https://gateway.example/grok-office/v1/inbox",
            500,
            "Internal Server Error",
            {},
            fp,
        )

    def test_undecodable_error_body_is_normalized(self):
        err = self._http_error(io.BytesIO(b"\xff"))
        with mock.patch.object(goc._CLIENT_OPENER, "open", side_effect=err):
            code, resp = goc._request_json("https://gateway.example/grok-office/v1/inbox")
        self.assertEqual(code, 500)
        self.assertEqual(resp.get("status"), "error")
        self.assertEqual(resp.get("code"), "http_error")
        self.assertEqual(resp.get("http_status"), 500)

    def test_unreadable_error_body_is_normalized(self):
        class UnreadableBody:
            def read(self, *args, **kwargs):
                raise OSError("connection reset")

        err = self._http_error(UnreadableBody())
        with mock.patch.object(goc._CLIENT_OPENER, "open", side_effect=err):
            code, resp = goc._request_json("https://gateway.example/grok-office/v1/inbox")
        self.assertEqual(code, 500)
        self.assertEqual(resp.get("status"), "error")
        self.assertEqual(resp.get("code"), "http_error")
        self.assertEqual(resp.get("http_status"), 500)


class _FakeResponse:
    """Minimal context-manager response for the fake opener."""

    def __init__(self, status: int, body: bytes):
        self.status = status
        self._body = body

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class TestAskPollingErrorBodyNormalization(AskRecoveryTestBase):
    """End-to-end: a successful send followed by an HTTP 500 whose body
    cannot be decoded must still preserve sent_id/dedupe_key. The real
    _request_json runs against a fake opener."""

    def test_undecodable_polling_error_preserves_correlation(self):
        # Restore the real transport function; drive it through a fake
        # opener instead of the scripted FakeTransport.
        goc._request_json = self._orig_request_json

        requests = []

        def fake_open(req, timeout=30.0):
            path = urlsplit(req.full_url).path
            requests.append(path)
            if path.endswith("/v1/health"):
                return _FakeResponse(200, json.dumps({
                    "status": "ok",
                    "agent": "grokbot-cloud-agent-office",
                    "targets": ["agent-office-thread"],
                    "target_agents": {"agent-office-thread": EXPECTED_AGENT},
                }).encode("utf-8"))
            if path.endswith("/v1/send"):
                return _FakeResponse(200, json.dumps({
                    "id": SENT_ID, "ts": 1700000000, "status": "sent",
                }).encode("utf-8"))
            if path.endswith("/v1/inbox"):
                raise urllib.error.HTTPError(
                    req.full_url, 500, "Internal Server Error", {},
                    io.BytesIO(b"\xff"),
                )
            raise AssertionError(f"unexpected request path: {path}")

        body = "Undecodable polling error probe"
        expected_key = hashlib.sha256(
            f"agent-office-thread\nquestion\n{body}".encode("utf-8")
        ).hexdigest()[:40]

        with mock.patch.object(goc._CLIENT_OPENER, "open", side_effect=fake_open):
            code, out, raw = self.run_ask(
                ["--timeout-s", "30", "--poll-s", "10"], body=body
            )

        self.assertEqual(code, 2)
        self.assertEqual(out.get("status"), "error")
        self.assertEqual(out.get("code"), "http_error")
        self.assertEqual(out.get("sent_id"), SENT_ID)
        self.assertEqual(out.get("dedupe_key"), expected_key)
        self.assertNotIn(TEST_TOKEN, raw)
        sends = [p for p in requests if p.endswith("/v1/send")]
        self.assertEqual(len(sends), 1, "a polling failure must not trigger a resend")


class TestAskSendErrorRecovery(AskRecoveryTestBase):
    """A failed send response must retain its idempotency handle."""

    def test_send_http_error_and_timeout_preserve_supplied_key_and_unconfirmed_outcome(self):
        dedupe_key = "caller-supplied-send-key"
        cases = [
            (
                "http",
                500,
                {"status": "error", "code": "http_error", "http_status": 500},
                2,
                "http_error",
            ),
            (
                "timeout",
                0,
                {"status": "error", "code": "timeout"},
                3,
                "timeout",
            ),
        ]

        for label, status, response, expected_exit, expected_code in cases:
            with self.subTest(kind=label):
                self.transport.calls.clear()
                self.transport.set("/v1/health", ok_health)
                self.transport.set("/v1/send", lambda call: (status, response))

                code, out, _ = self.run_ask(
                    ["--dedupe-key", dedupe_key, "--timeout-s", "30", "--poll-s", "10"]
                )

                self.assertEqual(code, expected_exit)
                self.assertEqual(out.get("code"), expected_code)
                self.assertEqual(out.get("dedupe_key"), dedupe_key)
                self.assertEqual(out.get("send_outcome"), "unconfirmed")
                self.assertNotIn("sent_id", out)
                self.assertEqual(self.transport.count(method="POST", path_suffix="/v1/send"), 1)
                self.assertEqual(self.transport.count(method="GET", path_suffix="/v1/inbox"), 0)

    def test_undecodable_send_error_preserves_dedupe_key_and_unconfirmed_status(self):
        goc._request_json = self._orig_request_json

        requests = []

        def fake_open(req, timeout=30.0):
            path = urlsplit(req.full_url).path
            requests.append(path)
            if path.endswith("/v1/health"):
                return _FakeResponse(200, json.dumps({
                    "status": "ok",
                    "agent": "grokbot-cloud-agent-office",
                    "targets": ["agent-office-thread"],
                    "target_agents": {"agent-office-thread": EXPECTED_AGENT},
                }).encode("utf-8"))
            if path.endswith("/v1/send"):
                raise urllib.error.HTTPError(
                    req.full_url, 500, "Internal Server Error", {},
                    io.BytesIO(b"\xff"),
                )
            raise AssertionError(f"unexpected request path: {path}")

        body = "Undecodable send error probe"
        expected_key = hashlib.sha256(
            f"agent-office-thread\nquestion\n{body}".encode("utf-8")
        ).hexdigest()[:40]

        with mock.patch.object(goc._CLIENT_OPENER, "open", side_effect=fake_open):
            code, out, raw = self.run_ask(
                ["--timeout-s", "30", "--poll-s", "10"], body=body
            )

        self.assertEqual(code, 2)
        self.assertEqual(out.get("code"), "http_error")
        self.assertEqual(out.get("http_status"), 500)
        self.assertEqual(out.get("dedupe_key"), expected_key)
        self.assertEqual(out.get("send_outcome"), "unconfirmed")
        self.assertNotIn("sent_id", out)
        self.assertNotIn(TEST_TOKEN, raw)
        self.assertEqual(requests, [
            "/grok-office/v1/health",
            "/grok-office/v1/send",
        ])


class TestIdempotencyTruth(AskRecoveryTestBase):
    """Finding 3: same key + same payload is idempotent; a new key is a new operation."""

    def _send_payload_for(self, body: str, extra: list[str]):
        self.transport.calls.clear()
        self.transport.set("/v1/health", ok_health)
        self.transport.set("/v1/send", ok_send)
        self.transport.set("/v1/inbox", empty_inbox)
        code, out, _ = self.run_ask(
            ["--timeout-s", "1", "--poll-s", "1"] + extra, body=body
        )
        self.assertEqual(code, 3)
        sends = [c for c in self.transport.calls if _url_path(c["url"]).endswith("/v1/send")]
        self.assertEqual(len(sends), 1)
        return sends[0]["payload"]

    def test_same_target_and_body_yield_same_dedupe_key(self):
        p1 = self._send_payload_for("Stable idempotent body", [])
        p2 = self._send_payload_for("Stable idempotent body", [])
        self.assertEqual(p1["dedupe_key"], p2["dedupe_key"])
        self.assertEqual(p1["body"], p2["body"])
        self.assertEqual(p1["target"], p2["target"])
        self.assertEqual(p1["kind"], "question")

    def test_different_body_yields_different_dedupe_key(self):
        p1 = self._send_payload_for("Body variant A", [])
        p2 = self._send_payload_for("Body variant B", [])
        self.assertNotEqual(p1["dedupe_key"], p2["dedupe_key"])

    def test_explicit_dedupe_key_is_used_verbatim(self):
        payload = self._send_payload_for("Any body", ["--dedupe-key", "my-stable-key-123"])
        self.assertEqual(payload["dedupe_key"], "my-stable-key-123")


class TestAskSuccessStillWorks(AskRecoveryTestBase):
    """Guard: the recovery changes must not break the happy path."""

    def test_matching_reply_is_returned_and_acked(self):
        reply_id = "aaaabbbbccccddddeeeeffff00001111"

        def inbox_with_reply(call):
            return 200, {
                "messages": [
                    {
                        "id": reply_id,
                        "from": EXPECTED_AGENT,
                        "reply_to": SENT_ID,
                        "body": "The answer",
                    }
                ],
                "remaining": 0,
                "cursor_advanced": False,
            }

        acked = []

        def record_ack(call):
            acked.append(call["payload"])
            return 200, {"status": "ok"}

        self.transport.set("/v1/health", ok_health)
        self.transport.set("/v1/send", ok_send)
        self.transport.set("/v1/inbox", inbox_with_reply)
        self.transport.set("/v1/ack", record_ack)

        code, out, _ = self.run_ask(["--timeout-s", "30", "--poll-s", "10"])

        self.assertEqual(code, 0)
        self.assertEqual(out.get("id"), reply_id)
        self.assertEqual(out.get("body"), "The answer")
        self.assertEqual(len(acked), 1)
        self.assertEqual(acked[0]["message_id"], reply_id)
        # The ask flow never advances the cursor itself.
        self.assertEqual(self.transport.count(method="POST", path_suffix="/v1/advance"), 0)


if __name__ == "__main__":
    unittest.main()
