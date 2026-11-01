"""Clock abstractions. Authority-host wall time is the only expiry reference.

A remote timestamp is evidence, never a clock. Local elapsed durations use a
monotonic source that is invalidated by restart (a new FakeClock/SystemClock
instance starts fresh; callers must not persist monotonic readings).
"""

from __future__ import annotations

import time


class Clock:
    def wall(self) -> float:
        """Authority-host wall time, seconds since epoch."""
        raise NotImplementedError

    def mono(self) -> float:
        """Monotonic seconds for local elapsed durations. Restart-invalid."""
        raise NotImplementedError


class SystemClock(Clock):
    def wall(self) -> float:
        return time.time()

    def mono(self) -> float:
        return time.monotonic()


class FakeClock(Clock):
    """Deterministic clock for tests. Both axes advance only via advance()."""

    def __init__(self, wall: float = 0.0, mono: float = 0.0) -> None:
        self._wall = float(wall)
        self._mono = float(mono)

    def wall(self) -> float:
        return self._wall

    def mono(self) -> float:
        return self._mono

    def advance(self, wall_delta: float = 0.0, mono_delta: float = 0.0) -> None:
        self._wall += float(wall_delta)
        self._mono += float(mono_delta)

    def set_wall(self, value: float) -> None:
        self._wall = float(value)
