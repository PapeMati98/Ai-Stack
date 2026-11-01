import unittest
from unittest import mock
import urllib.error

from dots_farm import paced_transport
from dots_farm.adapters import (
    ModelRateLimited,
    OpenRouterModel,
    _call_transport,
    _default_transport,
)


class _FakeClock:
    def __init__(self):
        self.value = 0.0
        self.sleeps = []

    def monotonic(self):
        return self.value

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.value += seconds


class _Response:
    def __init__(self, status, headers=None, body=b"{}"): 
        self.status = status
        self.headers = headers or {}
        self.body = body
        self.read_calls = 0

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self, _size=-1):
        self.read_calls += 1
        return self.body


class _FakeOpener:
    def __init__(self, result):
        self.result = result
        self.calls = 0

    def open(self, _request, timeout=None):
        self.calls += 1
        if isinstance(self.result, BaseException):
            raise self.result
        return self.result


class RetryAfterTests(unittest.TestCase):
    def test_delta_seconds_is_bounded_and_malformed_defaults(self):
        self.assertEqual(paced_transport.parse_retry_after("120"), 120)
        self.assertEqual(paced_transport.parse_retry_after("1"), 60)
        self.assertEqual(paced_transport.parse_retry_after("9999999999"), 3600)
        self.assertEqual(paced_transport.parse_retry_after("99999999999"), 3600)
        self.assertEqual(paced_transport.parse_retry_after("Wed, 21 Oct 2030"), 60)
        self.assertEqual(paced_transport.parse_retry_after("secret-value"), 60)
        self.assertEqual(paced_transport.parse_retry_after(None), 60)

    def test_http_error_429_is_typed_and_does_not_read_body(self):
        body = mock.Mock()
        error = urllib.error.HTTPError(
            "https://private.invalid/", 429, "SECRET-ERROR-TEXT",
            {"Retry-After": "125"}, body,
        )
        opener = _FakeOpener(error)
        with mock.patch("urllib.request.build_opener", return_value=opener):
            with self.assertRaises(ModelRateLimited) as caught:
                _default_transport("https://example.invalid/", {}, {}, 1, 10)
        self.assertEqual(opener.calls, 1)
        self.assertEqual(caught.exception.http_status, 429)
        self.assertEqual(caught.exception.retry_after_s, 125)
        self.assertEqual(str(caught.exception), "MODEL_RATE_LIMITED")
        self.assertNotIn("SECRET", repr(caught.exception))
        self.assertIsNone(caught.exception.__context__)
        body.read.assert_not_called()

    def test_response_status_429_is_typed_without_reading_body(self):
        response = _Response(
            429,
            {"Retry-After": "42", "Location": "SECRET-REDIRECT"},
            b"SECRET-RESPONSE",
        )
        opener = _FakeOpener(response)
        with mock.patch("urllib.request.build_opener", return_value=opener):
            with self.assertRaises(ModelRateLimited) as caught:
                _default_transport("https://example.invalid/", {}, {}, 1, 10)
        self.assertEqual(opener.calls, 1)
        self.assertEqual(response.read_calls, 0)
        self.assertEqual(caught.exception.retry_after_s, 60)
        self.assertNotIn("SECRET", str(caught.exception))

    def test_injected_http_429_status_is_typed_before_redirect_metadata(self):
        with self.assertRaises(ModelRateLimited) as caught:
            _call_transport(
                lambda *_args: {
                    "_http_status": 429,
                    "Location": "SECRET-REDIRECT",
                    "body": "SECRET-BODY",
                },
                "https://example.invalid/", {}, {},
            )
        self.assertEqual(caught.exception.http_status, 429)
        self.assertEqual(caught.exception.retry_after_s, 60)
        self.assertNotIn("SECRET", repr(caught.exception))

    def test_402_is_still_unknown_and_not_rate_limited(self):
        error = urllib.error.HTTPError(
            "https://private.invalid/", 402, "SECRET-CREDIT-TEXT", {}, mock.Mock()
        )
        opener = _FakeOpener(error)
        with mock.patch("urllib.request.build_opener", return_value=opener):
            with self.assertRaises(ConnectionError) as caught:
                _default_transport("https://example.invalid/", {}, {}, 1, 10)
        self.assertNotIsInstance(caught.exception, ModelRateLimited)
        self.assertEqual(str(caught.exception), "TRANSPORT_FAILED_UNKNOWN")
        self.assertNotIn("SECRET", repr(caught.exception))

    def test_call_transport_preserves_only_sanitized_429_fields(self):
        class Untrusted429(paced_transport.Http429Signal):
            def __str__(self):
                return "SECRET-EXCEPTION-TEXT"

        original = Untrusted429(95)

        def transport(*_args):
            raise original

        with self.assertRaises(ModelRateLimited) as caught:
            _call_transport(transport, "https://example.invalid/", {}, {})
        self.assertIsNot(caught.exception, original)
        self.assertEqual(caught.exception.http_status, 429)
        self.assertEqual(caught.exception.retry_after_s, 95)
        self.assertEqual(str(caught.exception), "MODEL_RATE_LIMITED")
        self.assertNotIn("SECRET", repr(caught.exception))
        self.assertIsNone(caught.exception.__context__)


class ProcessSharedPacerTests(unittest.TestCase):
    def _pacer(self, clock, **overrides):
        return paced_transport.ProcessSharedPacer(
            clock=clock.monotonic,
            sleeper=clock.sleep,
            **overrides,
        )

    def test_instances_share_start_spacing(self):
        clock = _FakeClock()
        pacer = self._pacer(clock)
        started = []

        def transport():
            started.append(clock.monotonic())
            return len(started)

        self.assertEqual(pacer.call(transport, run_id="run-a"), 1)
        self.assertEqual(pacer.call(transport, run_id="run-b"), 2)
        self.assertEqual(started, [0.0, 60.0])
        self.assertEqual(clock.sleeps, [60.0])

    def test_eighth_attempt_is_allowed_but_ninth_is_blocked(self):
        clock = _FakeClock()
        pacer = self._pacer(clock)
        calls = []

        def transport():
            calls.append(clock.monotonic())
            return True

        for _ in range(8):
            self.assertTrue(pacer.call(transport, run_id="one-run"))
        with self.assertRaises(paced_transport.PacedCallLimitExceeded):
            pacer.call(transport, run_id="one-run")
        self.assertEqual(len(calls), 8)
        self.assertEqual(calls, [float(value) for value in range(0, 480, 60)])

    def test_one_429_blocks_all_runs_until_bounded_cooldown_expires(self):
        clock = _FakeClock()
        pacer = self._pacer(clock)
        calls = []

        def limited():
            calls.append(clock.monotonic())
            raise ModelRateLimited(120)

        with self.assertRaises(ModelRateLimited):
            pacer.call(limited, run_id="run-one")
        with self.assertRaises(paced_transport.PacedCooldownActive) as caught:
            pacer.call(lambda: calls.append(clock.monotonic()), run_id="run-two")
        self.assertEqual(caught.exception.retry_after_s, 120)
        self.assertEqual(calls, [0.0])
        clock.value = 120.0
        self.assertEqual(
            pacer.call(lambda: calls.append(clock.monotonic()) or "ok", run_id="run-two"),
            "ok",
        )
        self.assertEqual(calls, [0.0, 120.0])

    def test_invalid_run_scope_rejected_and_context_restored(self):
        self.assertIsNone(paced_transport.current_run_id())
        with self.assertRaisesRegex(ValueError, "PACED_RUN_ID_INVALID"):
            with paced_transport.run_scope("bad run id"):
                pass
        with paced_transport.run_scope("stable-run-1"):
            self.assertEqual(paced_transport.current_run_id(), "stable-run-1")
        self.assertIsNone(paced_transport.current_run_id())

    def test_default_model_uses_paced_transport_only_when_not_injected(self):
        model = OpenRouterModel({}, lambda _ref: "unused")
        self.assertEqual(model.transport.__name__, "_paced_default_transport")
        injected = lambda *_args: {}
        self.assertIs(OpenRouterModel({}, lambda _ref: "unused", transport=injected).transport,
                      injected)


if __name__ == "__main__":
    unittest.main()
