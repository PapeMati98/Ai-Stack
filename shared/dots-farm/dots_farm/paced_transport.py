"""Process-local pacing gate for bounded model HTTP calls.

The gate serializes calls across adapter instances in this Python process. It
spaces starts, caps calls per run, and holds all calls after a typed HTTP 429.
It never retries a request itself. The engine owns run boundaries and should
wrap each tick in run_scope(stable_run_id).
"""

from contextlib import contextmanager
from contextvars import ContextVar
import math
import re
import threading
import time


MIN_SPACING_SECONDS = 60
MAX_CALLS_PER_RUN = 8
RUN_WINDOW_SECONDS = 14 * 60
DEFAULT_COOLDOWN_SECONDS = 60
MAX_COOLDOWN_SECONDS = 3600
_MAX_TRACKED_RUNS = 512
_RUN_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
_CURRENT_RUN_ID = ContextVar("dots_farm_paced_run_id", default=None)


def parse_retry_after(value):
    """Return a bounded integer delay from an HTTP delta-seconds header.

    HTTP-date forms and malformed values use the conservative default. The
    adapter applies the same minimum even if the upstream sends a smaller value.
    """
    if not isinstance(value, str) or not value.isascii():
        return DEFAULT_COOLDOWN_SECONDS
    if not value.isdigit():
        return DEFAULT_COOLDOWN_SECONDS
    normalized = value.lstrip("0") or "0"
    if len(normalized) > 4:
        return MAX_COOLDOWN_SECONDS
    try:
        seconds = int(normalized, 10)
    except (ValueError, OverflowError):
        return DEFAULT_COOLDOWN_SECONDS
    return min(MAX_COOLDOWN_SECONDS, max(DEFAULT_COOLDOWN_SECONDS, seconds))


def _bounded_cooldown(seconds):
    if type(seconds) is not int:
        return DEFAULT_COOLDOWN_SECONDS
    return min(MAX_COOLDOWN_SECONDS, max(DEFAULT_COOLDOWN_SECONDS, seconds))


class Http429Signal(Exception):
    """Safe signal carrying only a verified HTTP 429 and bounded delay."""

    __slots__ = ("http_status", "retry_after_s")
    code = "HTTP_429_RATE_LIMITED"

    def __init__(self, retry_after_s=DEFAULT_COOLDOWN_SECONDS):
        self.http_status = 429
        self.retry_after_s = _bounded_cooldown(retry_after_s)
        super().__init__(self.code)


class PacedCooldownActive(RuntimeError):
    """A prior HTTP 429 still blocks this process from making HTTP calls."""

    __slots__ = ("retry_after_s",)
    code = "PACED_COOLDOWN_ACTIVE"

    def __init__(self, retry_after_s):
        value = retry_after_s
        if type(value) is not int or value < 1:
            value = 1
        self.retry_after_s = min(MAX_COOLDOWN_SECONDS, value)
        super().__init__(self.code)


class PacedCallLimitExceeded(RuntimeError):
    """The current run already used its eight permitted HTTP attempts."""

    code = "PACED_RUN_CALL_LIMIT"

    def __init__(self):
        super().__init__(self.code)


class PacedTransportUnavailable(RuntimeError):
    """The pacing clock or sleeper failed; the request was not issued."""

    code = "PACED_TRANSPORT_UNAVAILABLE"

    def __init__(self):
        super().__init__(self.code)


@contextmanager
def run_scope(run_id):
    """Bind a stable run identifier to model calls made in this context."""
    if not isinstance(run_id, str) or not _RUN_ID_RE.fullmatch(run_id):
        raise ValueError("PACED_RUN_ID_INVALID")
    token = _CURRENT_RUN_ID.set(run_id)
    try:
        yield
    finally:
        _CURRENT_RUN_ID.reset(token)


def current_run_id():
    return _CURRENT_RUN_ID.get()


class ProcessSharedPacer:
    """One in-process limiter shared by all model adapter instances."""

    def __init__(
        self,
        *,
        clock=None,
        sleeper=None,
        spacing_seconds=MIN_SPACING_SECONDS,
        minimum_spacing_seconds=MIN_SPACING_SECONDS,
        max_calls_per_run=MAX_CALLS_PER_RUN,
        run_window_seconds=RUN_WINDOW_SECONDS,
        max_tracked_runs=_MAX_TRACKED_RUNS,
    ):
        if (type(minimum_spacing_seconds) is not int or not 10 <= minimum_spacing_seconds <= MIN_SPACING_SECONDS):
            raise ValueError("PACED_SPACING_FLOOR_INVALID")
        if (type(spacing_seconds) is not int or spacing_seconds < minimum_spacing_seconds
                or spacing_seconds > MAX_COOLDOWN_SECONDS):
            raise ValueError("PACED_SPACING_INVALID")
        if (type(max_calls_per_run) is not int
                or not 1 <= max_calls_per_run <= MAX_CALLS_PER_RUN):
            raise ValueError("PACED_CALL_LIMIT_INVALID")
        if (type(run_window_seconds) is not int
                or not 1 <= run_window_seconds <= 24 * 60 * 60):
            raise ValueError("PACED_RUN_WINDOW_INVALID")
        if (type(max_tracked_runs) is not int
                or not 1 <= max_tracked_runs <= 4096):
            raise ValueError("PACED_RUN_STATE_LIMIT_INVALID")
        self._clock = clock or time.monotonic
        self._sleeper = sleeper or time.sleep
        self._spacing_seconds = spacing_seconds
        self._max_calls_per_run = max_calls_per_run
        self._run_window_seconds = run_window_seconds
        self._max_tracked_runs = max_tracked_runs
        self._lock = threading.RLock()
        self._next_call_at = 0.0
        self._global_blocked_until = 0.0
        self._run_states = {}

    def _now(self):
        try:
            value = float(self._clock())
        except Exception:
            raise PacedTransportUnavailable() from None
        if not math.isfinite(value):
            raise PacedTransportUnavailable() from None
        return value

    def _run_key(self, run_id):
        selected = current_run_id() if run_id is None else run_id
        if selected is None:
            return "unscoped"
        if not isinstance(selected, str) or not _RUN_ID_RE.fullmatch(selected):
            raise ValueError("PACED_RUN_ID_INVALID")
        return selected

    def _expire_run_states(self, now):
        expired = [
            key for key, state in self._run_states.items()
            if state["started_at"] is not None
            and now - state["started_at"] >= self._run_window_seconds
        ]
        for key in expired:
            self._run_states.pop(key, None)

    def _get_run_state(self, key, now):
        state = self._run_states.get(key)
        if (state is not None and state["started_at"] is not None
                and now - state["started_at"] >= self._run_window_seconds):
            self._run_states.pop(key, None)
            state = None
        if state is None:
            self._expire_run_states(now)
            if len(self._run_states) >= self._max_tracked_runs:
                raise PacedCallLimitExceeded()
            state = {"started_at": None, "calls": 0}
            self._run_states[key] = state
        return state

    def call(self, transport, *args, run_id=None, **kwargs):
        """Wait for the process slot, then issue exactly one transport call."""
        if not callable(transport):
            raise ValueError("PACED_TRANSPORT_INVALID")
        key = self._run_key(run_id)
        with self._lock:
            now = self._now()
            if self._global_blocked_until > now:
                remaining = int(math.ceil(self._global_blocked_until - now))
                raise PacedCooldownActive(remaining)
            state = self._get_run_state(key, now)
            if state["calls"] >= self._max_calls_per_run:
                raise PacedCallLimitExceeded()

            delay = self._next_call_at - now
            if delay > 0:
                try:
                    self._sleeper(delay)
                except Exception:
                    raise PacedTransportUnavailable() from None
                now = self._now()
                if now < self._next_call_at:
                    raise PacedTransportUnavailable()

            if state["started_at"] is None:
                state["started_at"] = now
            state["calls"] += 1
            self._next_call_at = now + self._spacing_seconds
            try:
                return transport(*args, **kwargs)
            except Http429Signal as error:
                after = self._now()
                cooldown = max(self._spacing_seconds, error.retry_after_s)
                self._global_blocked_until = max(
                    self._global_blocked_until, after + cooldown
                )
                self._next_call_at = max(
                    self._next_call_at, self._global_blocked_until
                )
                raise


# Native controller has a four-minute hard lifetime. Three bounded HTTP calls
# fit its 60-second spacing and 20-second request timeouts; eight do not.
_DEFAULT_PACER = ProcessSharedPacer(max_calls_per_run=3)


def call(transport, *args, run_id=None, **kwargs):
    """Issue one call through the process-wide limiter."""
    return _DEFAULT_PACER.call(
        transport, *args, run_id=run_id, **kwargs
    )
