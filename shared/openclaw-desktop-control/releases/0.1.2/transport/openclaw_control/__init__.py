"""Stable library API for the parent runtime.

Everything the parent needs without touching internals:

- ControlStore  : durable cc_* state in the shared authority SQLite file
- models        : states, bounds, envelopes
- delivery      : enqueue/claim/deliver/start/finish/recover/alarm/
                  reconcile/cancel/takeover + redacted summaries
- telegram      : OwnerConfig, intake_update, intake_callback
- pairing       : issue/redeem one-use nonces
- status_text   : render_status_pl, StatusTracker
- botapi        : BotApi (token from caller-injected env, injected opener)
- adapters      : ControlAdapter protocol, capability gates,
                  FakeControlAdapter (synthetic), DisabledAdapter,
                  NonLiteralAdapter (refusal-path double)
"""

from openclaw_control import models
from openclaw_control.adapters import (
    AdapterBusyError,
    AdapterDisabledError,
    AdapterRefusalError,
    AdapterUnavailableError,
    ControlAdapter,
    DeliverOutcome,
    DisabledAdapter,
    FakeControlAdapter,
    NonLiteralAdapter,
    StaleFenceError,
    deliver_checked,
    is_capable,
    looks_command_like,
    require_capable,
)
from openclaw_control import botapi, delivery, pairing, status_text, telegram
from openclaw_control.botapi import BotApi, BotApiError
from openclaw_control.delivery import (
    DeliveryError,
    StaleStatusError,
    UnboundTargetError,
)
from openclaw_control.store import (
    CallbackError,
    ClaimMismatchError,
    ControlError,
    ControlStore,
    ExpiredCallbackError,
    GenerationMismatchError,
    PairingError,
    ReconcileError,
    ReplayedCallbackError,
    StoppedError,
    UnknownCallbackError,
    UnknownDispatchError,
    sha256_text,
    target_binding_id,
)
from openclaw_control.telegram import (
    CrashInjected,
    OwnerConfig,
    recover_pending,
)

__version__ = "0.1.0"

__all__ = [
    "models",
    "delivery",
    "telegram",
    "pairing",
    "status_text",
    "botapi",
    "OwnerConfig",
    "CrashInjected",
    "recover_pending",
    "BotApi",
    "BotApiError",
    "ControlAdapter",
    "DeliverOutcome",
    "DisabledAdapter",
    "FakeControlAdapter",
    "NonLiteralAdapter",
    "AdapterBusyError",
    "AdapterDisabledError",
    "AdapterRefusalError",
    "AdapterUnavailableError",
    "StaleFenceError",
    "StaleStatusError",
    "DeliveryError",
    "UnboundTargetError",
    "CallbackError",
    "ClaimMismatchError",
    "ControlError",
    "ControlStore",
    "ExpiredCallbackError",
    "GenerationMismatchError",
    "PairingError",
    "ReconcileError",
    "ReplayedCallbackError",
    "StoppedError",
    "UnknownCallbackError",
    "UnknownDispatchError",
    "deliver_checked",
    "is_capable",
    "looks_command_like",
    "require_capable",
    "sha256_text",
    "target_binding_id",
]
