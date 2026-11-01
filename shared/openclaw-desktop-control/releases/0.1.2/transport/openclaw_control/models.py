"""Shared constants for the openclaw_control T2/T3 candidate (stdlib only)."""

from __future__ import annotations

SCHEMA_VERSION = "1.0"

# Durable dispatch states. ACK/start/result are distinct facts recorded by
# separate store calls; delivery_unknown is terminal-for-resend until the
# owner explicitly reconciles it.
QUEUED = "queued"
DISPATCHING = "dispatching"
DELIVERED = "delivered"
RUNNING = "running"
COMPLETED = "completed"
BLOCKED = "blocked"
FAILED = "failed"
DELIVERY_UNKNOWN = "delivery_unknown"
USER_PAUSED = "user_paused"
CANCEL_PENDING = "cancel_pending"
CANCELLED = "cancelled"

TERMINAL_STATES = frozenset({COMPLETED, BLOCKED, FAILED, CANCELLED})
CANCELLABLE_STATES = frozenset(
    {QUEUED, DISPATCHING, DELIVERED, RUNNING, DELIVERY_UNKNOWN}
)

# Distinct ACK facts.
ACKED = "acked"
ACK_UNKNOWN = "unknown"

# One visible alarm for an uncertain dispatch; configurable per parent.
UNKNOWN_ALARM_AFTER = 120.0

# One short status message per operation.
STATUS_MAX_CHARS = 600

# Telegram transport bound: oversize input is an explicit unsupported
# receipt, never a truncation.
TELEGRAM_TEXT_LIMIT = 4096

# Owner-decision windows (seconds, authority-host wall clock).
CALLBACK_TTL = 600.0
PAIRING_TTL = 600.0

# Freshness bound for native provider status reads (seconds).
STATUS_FRESHNESS = 10.0
# Status timestamps more than this far in the future are rejected.
STATUS_FUTURE_SKEW = 1.0

STOP_SCOPE_ALL = "all"

# Poll cursor and inbox keys.
POLL_CURSOR_TELEGRAM = "telegram-getupdates"
RUNTIME_MENU_SHOWN = "menu_shown"


def envelope(kind: str, payload: dict) -> dict:
    """Wrap a CLI/library payload in the versioned JSON envelope."""
    return {"schema": SCHEMA_VERSION, "kind": kind, "payload": payload}


def task_scope(task_id: str) -> str:
    return "task:%s" % task_id
