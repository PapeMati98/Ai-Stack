"""Provider adapters. Explicit capabilities, no simulated production success.

Only an adapter with BOTH idempotent lookup and fencing enforcement may
resume a task. Anything else is observe-only: the watchdog reports UNKNOWN
and never resends after a crash. Real native adapters are owned by the
parent; this package ships a fake (tests/synthetic) and an observe-only
adapter for unsupported providers.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Optional

from openclaw_ops import models


class UnsupportedResumeError(RuntimeError):
    """Raised when resume is attempted on an observe-only adapter."""


class StaleFenceError(RuntimeError):
    """Raised when a resume presents an old generation or fencing token."""


@dataclass
class ProviderStatus:
    state: str  # one of models.STATE_*
    observed_wall: float  # authority-host time when observed
    provider_wall: float = 0.0  # remote timestamp: evidence, never a clock
    native_run_id: Optional[str] = None
    revision: Optional[str] = None
    checkpoint: str = ""
    writer_active: bool = False
    failure_cause: Optional[str] = None
    cause_transient: bool = False


@dataclass
class ResumeResult:
    native_run_id: str
    ack: str  # "acked" | "unknown"


class Adapter:
    """Protocol for native provider adapters (duck-typed)."""

    name: str = "base"
    supports_idempotent_lookup: bool = False
    supports_fencing: bool = False

    def get_status(self, task: dict) -> Optional[ProviderStatus]:
        raise NotImplementedError

    def lookup(self, idempotency_key: str) -> Optional[str]:
        raise NotImplementedError

    def resume(self, task: dict, idempotency_key: str,
               fencing_token: int) -> ResumeResult:
        raise NotImplementedError


class ObserveOnlyAdapter(Adapter):
    """Unsupported provider: visible, never resumes, never resends."""

    def __init__(self, name: str = "unsupported") -> None:
        self.name = name
        self.supports_idempotent_lookup = False
        self.supports_fencing = False

    def get_status(self, task: dict) -> Optional[ProviderStatus]:
        return None  # unknown / unreachable -> watchdog must not dispatch

    def lookup(self, idempotency_key: str) -> Optional[str]:
        return None

    def resume(self, task: dict, idempotency_key: str,
               fencing_token: int) -> ResumeResult:
        raise UnsupportedResumeError(
            f"provider '{self.name}' is observe-only; owner reconciliation"
            " required (UNKNOWN_DISPATCH)"
        )


StatusScript = Callable[[dict], Optional[ProviderStatus]]


class FakeNativeAdapter(Adapter):
    """Deterministic test double with idempotent resume + fencing.

    Resume is idempotent on the idempotency key: repeating it replays the
    recorded native run id without creating another turn. Fencing rejects
    old generations and stale tokens. ``script_ack_unknown_once`` simulates
    a crash between dispatch and ACK for exactly one resume call.
    """

    def __init__(self, name: str = "fake-native",
                 script: Optional[StatusScript] = None) -> None:
        self.name = name
        self.supports_idempotent_lookup = True
        self.supports_fencing = True
        self._script = script
        self.runs: dict[str, str] = {}  # idempotency key -> native run id
        self.max_token: dict[str, int] = {}  # task:generation -> max token
        self.max_generation: dict[str, int] = {}  # task -> max generation
        self.resume_calls = 0
        self.status_calls = 0
        self.script_ack_unknown_once = False
        self._ack_unknown_spent = False

    def get_status(self, task: dict) -> Optional[ProviderStatus]:
        self.status_calls += 1
        if self._script is None:
            return None
        return self._script(task)

    def lookup(self, idempotency_key: str) -> Optional[str]:
        return self.runs.get(idempotency_key)

    def resume(self, task: dict, idempotency_key: str,
               fencing_token: int) -> ResumeResult:
        if idempotency_key in self.runs:
            return ResumeResult(self.runs[idempotency_key], "acked")
        gen = int(task["generation"])
        if gen < self.max_generation.get(task["task_id"], 0):
            raise StaleFenceError(
                f"old generation {gen} for task {task['task_id']}"
            )
        self.max_generation[task["task_id"]] = gen
        scope = f"{task['task_id']}:{gen}"
        seen = self.max_token.get(scope, 0)
        if fencing_token <= seen:
            raise StaleFenceError(
                f"stale fencing token {fencing_token} <= {seen} for {scope}"
            )
        self.resume_calls += 1
        self.max_token[scope] = fencing_token
        run_id = f"{idempotency_key}:run{self.resume_calls}"
        self.runs[idempotency_key] = run_id
        if self.script_ack_unknown_once and not self._ack_unknown_spent:
            self._ack_unknown_spent = True
            return ResumeResult(run_id, "unknown")
        return ResumeResult(run_id, "acked")
