"""Adapter protocol and capability gates.

Only an adapter proving exact-text delivery, turn start, fencing,
idempotent lookup AND literal prefix delivery may dispatch. Anything
else -- including every unknown Desktop adapter -- stays disabled:
visible, never dispatching, never resending. The fake adapter below is
the only implementation shipped here; it is synthetic/test-only.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional, Protocol, runtime_checkable

from openclaw_control import models

REQUIRED_CAPS = (
    "supports_exact_text",
    "supports_turn_start",
    "supports_fencing",
    "supports_idempotent_lookup",
)
# supports_literal_prefixes is intentionally NOT a hard capability: an
# adapter lacking it stays enabled for ordinary payloads but must refuse
# any payload starting with /, ! or @ (refusal, never reinterpretation).

COMMAND_PREFIXES = ("/", "!", "@")


class AdapterDisabledError(RuntimeError):
    """Adapter lacks proven capability; dispatch refused (fail closed)."""


class AdapterRefusalError(RuntimeError):
    """Adapter cannot carry this payload literally; refused, not mangled."""


class AdapterBusyError(RuntimeError):
    """Target is busy or owner-changed; caller must queue, not mark unknown."""


class AdapterUnavailableError(RuntimeError):
    """PID/pipe missing. Queue/block; never start a second app-server."""


class StaleFenceError(RuntimeError):
    """Old generation or fencing token presented."""


def looks_command_like(text: str) -> bool:
    """True when the payload would be a client command after leading space.

    Does not mutate the stored text. Prefix refusal is per-payload: an
    adapter lacking ``supports_literal_prefixes`` stays enabled for
    ordinary exact text.
    """
    if not isinstance(text, str) or not text:
        return False
    return text.lstrip().startswith(COMMAND_PREFIXES)


@runtime_checkable
class ControlAdapter(Protocol):
    name: str
    supports_exact_text: bool
    supports_turn_start: bool
    supports_fencing: bool
    supports_idempotent_lookup: bool
    supports_literal_prefixes: bool

    def deliver(self, dispatch: dict) -> "DeliverOutcome":
        raise NotImplementedError

    def lookup(self, idempotency_key: str) -> Optional[str]:
        raise NotImplementedError

    def readback(self, idempotency_key: str) -> Optional[str]:
        raise NotImplementedError

    def start_turn(
        self, dispatch: dict, fencing_token: int
    ) -> dict:
        raise NotImplementedError

    def stop_turn(self, dispatch: dict) -> bool:
        raise NotImplementedError


@dataclass
class DeliverOutcome:
    run_ref: str
    ack: str  # models.ACKED | models.ACK_UNKNOWN


def missing_caps(adapter: object) -> list[str]:
    return [c for c in REQUIRED_CAPS if not bool(getattr(adapter, c, False))]


def is_capable(adapter: object) -> tuple[bool, list[str]]:
    missing = missing_caps(adapter)
    return (not missing, missing)


def require_capable(adapter: object) -> None:
    ok, missing = is_capable(adapter)
    if not ok:
        raise AdapterDisabledError(
            "adapter %r lacks proven capability (%s); dispatch refused"
            % (getattr(adapter, "name", "?"), ",".join(missing))
        )


def deliver_checked(adapter: ControlAdapter, dispatch: dict) -> DeliverOutcome:
    """Gate capability FIRST, then literal-carry, then deliver.

    A payload starting with /, ! or @ stays literal. When the adapter
    cannot prove literal delivery the payload is refused -- never
    escaped, prefixed, or reinterpreted as a bot command.
    """
    require_capable(adapter)
    text = dispatch.get("raw_text", "")
    if looks_command_like(text) and not adapter.supports_literal_prefixes:
        raise AdapterRefusalError(
            "adapter %r cannot carry %r literally; refused, not mangled"
            % (adapter.name, text[:1])
        )
    return adapter.deliver(dispatch)


class DisabledAdapter:
    """Unknown/unproven provider (e.g. an unverified Desktop adapter).

    Visible in listings, permanently disabled for dispatch until native
    capability evidence lands. Every action raises.
    """

    supports_exact_text = False
    supports_turn_start = False
    supports_fencing = False
    supports_idempotent_lookup = False
    supports_literal_prefixes = False

    def __init__(self, name: str = "unknown-desktop") -> None:
        self.name = name

    def _deny(self, what: str) -> AdapterDisabledError:
        return AdapterDisabledError(
            "provider %r is observe-only/disabled; owner reconciliation"
            " required (%s)" % (self.name, what)
        )

    def deliver(self, dispatch: dict) -> DeliverOutcome:
        raise self._deny("UNKNOWN_DISPATCH")

    def lookup(self, idempotency_key: str) -> Optional[str]:
        return None

    def readback(self, idempotency_key: str) -> Optional[str]:
        return None

    def start_turn(self, dispatch: dict, fencing_token: int) -> dict:
        raise self._deny("no turn start on disabled adapter")

    def stop_turn(self, dispatch: dict) -> bool:
        raise self._deny("no confirmed stop on disabled adapter")


class FakeControlAdapter:
    """Deterministic synthetic adapter (tests/synthetic only).

    Idempotent deliver on dispatch_id, exact readback of the delivered
    text, monotonic fencing tokens per (task, generation).
    ``script_ack_unknown_once`` simulates a crash between native dispatch
    and ACK for exactly one call: the run IS recorded (so lookup proves
    it) but the ACK is reported unknown. ``refuse_stop_once`` simulates
    a missing stop confirmation for exactly one call.
    """

    supports_exact_text = True
    supports_turn_start = True
    supports_fencing = True
    supports_idempotent_lookup = True
    supports_literal_prefixes = True

    def __init__(self, name: str = "fake-control") -> None:
        self.name = name
        self.runs: dict[str, str] = {}
        self.texts: dict[str, str] = {}
        self.max_token: dict[str, int] = {}
        self.max_generation: dict[str, int] = {}
        self.deliver_calls = 0
        self.start_calls = 0
        self.stop_calls = 0
        self.script_ack_unknown_once = False
        self._ack_unknown_spent = False
        self.refuse_stop_once = False
        self._stop_refused_spent = False

    def deliver(self, dispatch: dict) -> DeliverOutcome:
        key = str(dispatch["dispatch_id"])
        if key in self.runs:
            return DeliverOutcome(self.runs[key], models.ACKED)
        gen = int(dispatch.get("generation") or 0)
        task = str(dispatch.get("task_id") or "")
        if gen < self.max_generation.get(task, 0):
            raise StaleFenceError("old generation %d" % gen)
        self.max_generation[task] = gen
        self.deliver_calls += 1
        run_ref = "%s:run%d" % (key, self.deliver_calls)
        self.runs[key] = run_ref
        # Exact bytes in, exact bytes out: no model, no rewrite.
        self.texts[key] = str(dispatch.get("raw_text", ""))
        if self.script_ack_unknown_once and not self._ack_unknown_spent:
            self._ack_unknown_spent = True
            return DeliverOutcome(run_ref, models.ACK_UNKNOWN)
        return DeliverOutcome(run_ref, models.ACKED)

    def lookup(self, idempotency_key: str) -> Optional[str]:
        return self.runs.get(idempotency_key)

    def readback(self, idempotency_key: str) -> Optional[str]:
        return self.texts.get(idempotency_key)

    def start_turn(self, dispatch: dict, fencing_token: int) -> dict:
        scope = "%s:%s" % (
            dispatch.get("task_id", ""),
            dispatch.get("generation", 0),
        )
        seen = self.max_token.get(scope, 0)
        if int(fencing_token) <= seen:
            raise StaleFenceError("stale fencing token for %s" % scope)
        self.max_token[scope] = int(fencing_token)
        self.start_calls += 1
        return {"started": True, "fencing_token": int(fencing_token)}

    def stop_turn(self, dispatch: dict) -> bool:
        self.stop_calls += 1
        if self.refuse_stop_once and not self._stop_refused_spent:
            self._stop_refused_spent = True
            return False
        return True


class NonLiteralAdapter(FakeControlAdapter):
    """Test double proving the refusal path: cannot carry / ! @ literally."""

    supports_literal_prefixes = False

    def __init__(self, name: str = "non-literal") -> None:
        super().__init__(name)
