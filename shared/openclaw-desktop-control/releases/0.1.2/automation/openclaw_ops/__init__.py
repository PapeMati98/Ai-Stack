"""OpenClaw operational heartbeat + recurring helpdesk (TEST candidate).

One authority-host SQLite ledger, explicit adapters, no production wiring.
Parent Codex owns real transports, Telegram integration and activation.
"""

from openclaw_ops.clock import Clock, SystemClock, FakeClock
from openclaw_ops.ledger import Ledger
from openclaw_ops.adapters import (
    Adapter,
    FakeNativeAdapter,
    ObserveOnlyAdapter,
    ProviderStatus,
    ResumeResult,
    UnsupportedResumeError,
    StaleFenceError,
)
from openclaw_ops.service import REGISTRY, register_adapter, main

__all__ = [
    "Clock",
    "SystemClock",
    "FakeClock",
    "Ledger",
    "Adapter",
    "FakeNativeAdapter",
    "ObserveOnlyAdapter",
    "ProviderStatus",
    "ResumeResult",
    "UnsupportedResumeError",
    "StaleFenceError",
    "REGISTRY",
    "register_adapter",
    "main",
]
