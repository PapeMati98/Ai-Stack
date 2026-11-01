"""ControlAdapter wrapper around proven CodexDesktop.

Does not claim native cancel or receiver CAS. Prefix refusal is per-payload.
UNKNOWN is reconciled only via readback. Pipe/PID miss is Unavailable —
the registry/controller queues; this module never starts an app-server.
"""

from __future__ import annotations

from typing import Any, Optional

from openclaw_control import models
from openclaw_control.adapters import (
    AdapterBusyError,
    AdapterRefusalError,
    AdapterUnavailableError,
    DeliverOutcome,
    looks_command_like,
)
from openclaw_control.delivery import require_fresh_status

from codex_desktop import (
    CAPABILITIES,
    CodexDesktop,
    DeliveryUnknown,
    Unavailable,
    pipe_available,
    process_available,
)


TargetBusy = AdapterBusyError
PipeUnavailable = AdapterUnavailableError


class CodexControlAdapter:
    name = "codex-desktop"
    supports_exact_text = True
    supports_turn_start = True
    # Broker-side serialization only. NOT receiver CAS.
    supports_fencing = True
    supports_idempotent_lookup = True
    supports_literal_prefixes = False
    supports_cancel = False
    supports_atomic_last_turn_cas = False
    supports_native_idempotency = False
    capabilities = CAPABILITIES

    def __init__(
        self,
        desktop: Any,
        *,
        pipe_path: str = "",
        pid: int = 0,
        clock: Any = None,
    ) -> None:
        self.desktop = desktop
        self.pipe_path = pipe_path
        self.pid = pid
        self.clock = clock
        self.runs: dict[str, str] = {}
        self.texts: dict[str, str] = {}
        self.last_status: dict[str, dict] = {}
        self.blocked_reason = ""

    def _now(self) -> float:
        if self.clock is not None:
            return float(self.clock.wall())
        import time

        return time.time()

    def availability(self) -> dict:
        if self.pipe_path and not pipe_available(self.pipe_path):
            return {"ok": False, "reason": "pipe-unavailable"}
        if self.pid and not process_available(self.pid):
            return {"ok": False, "reason": "pid-unavailable"}
        return {"ok": True, "reason": ""}

    def _ensure_available(self) -> None:
        av = self.availability()
        if not av["ok"]:
            self.blocked_reason = av["reason"]
            raise PipeUnavailable(av["reason"])

    def status_of(self, thread: str) -> dict:
        self._ensure_available()
        st = self.desktop.status(thread)
        require_fresh_status(st["observed_at"], self._now())
        self.last_status[thread] = st
        return st

    def deliver(self, dispatch: dict) -> DeliverOutcome:
        self._ensure_available()
        text = dispatch.get("raw_text", "")
        if looks_command_like(text) and not self.supports_literal_prefixes:
            raise AdapterRefusalError(
                "adapter %r cannot carry prefix literally; refused, not mangled"
                % self.name
            )
        thread = str(dispatch.get("native_thread") or "")
        key = str(dispatch["dispatch_id"])
        if key in self.runs:
            return DeliverOutcome(self.runs[key], models.ACKED)
        st = self.status_of(thread)
        if st.get("state") != "idle":
            raise TargetBusy("target-busy")
        try:
            result = self.desktop.send(
                thread,
                text,
                st.get("last_turn_id"),
                observed_at=st["observed_at"],
                supervisor=bool(dispatch.get("supervisor")),
            )
        except DeliveryUnknown:
            self.texts[key] = text
            return DeliverOutcome("", models.ACK_UNKNOWN)
        except Unavailable as exc:
            raise TargetBusy(str(exc)) from None
        run_ref = str(result.get("text_sha256") or key)
        self.runs[key] = run_ref
        self.texts[key] = text
        return DeliverOutcome(run_ref, models.ACKED)

    def lookup(self, idempotency_key: str) -> Optional[str]:
        return self.runs.get(idempotency_key)

    def readback(self, idempotency_key: str) -> Optional[str]:
        return self.texts.get(idempotency_key)

    def native_readback(self, dispatch: dict) -> dict:
        """Reconcile UNKNOWN from the rollout tail only. Never resend."""
        thread = str(dispatch.get("native_thread") or "")
        text = dispatch.get("raw_text", "")
        not_before = float(dispatch.get('delivery_attempt_at') or dispatch.get("claim_started_at") or 0)
        if not_before <= 0:
            return {'state': 'delivery_unknown', 'reason': 'no-durable-attempt-time'}
        return self.desktop.readback(thread, text, not_before=not_before)

    def start_turn(self, dispatch: dict, fencing_token: int) -> dict:
        """Observe native start. Does not send. Broker token is not CAS."""
        _ = fencing_token
        rb = self.native_readback(dispatch)
        started = rb.get("state") in ("running", "completed")
        return {
            "started": started,
            "state": rb.get("state"),
            "turn_id": rb.get("turn_id"),
            "atomic_compare_and_swap": False,
        }

    def stop_turn(self, dispatch: dict) -> bool:
        """Native cancel is absent. Silence stays cancel_pending."""
        _ = dispatch
        return False
