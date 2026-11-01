"""Shared constants and JSON envelope version for the TEST candidate."""

from __future__ import annotations

SCHEMA_VERSION = "1.0"

# Lease / freshness / checkpoint windows (seconds, per accepted protocol).
LEASE_TTL = 120
LEASE_RENEW_EVERY = 30
# A transport claim that has not been completed after this bounded interval
# is treated as an uncertain dispatch on the next worker pass.  It is never
# silently requeued.
NOTIFY_CLAIM_TTL = 120
STATUS_FRESHNESS = 10
CHECKPOINT_QUIET = 30
CLOCK_SKEW_LIMIT = 30

# Retry policy: backoff delays per attempt index, hard cap per grant.
BACKOFFS = (60, 300, 1800)
MAX_ATTEMPTS = 3

# Provider states reported by native adapters.
STATE_RUNNING = "running"
STATE_BUSY = "busy"
STATE_TERMINAL_COMPLETED = "terminal_completed"
STATE_TERMINAL_FAILED = "terminal_failed"
STATE_WAITING_USER = "waiting_user"
STATE_APPROVAL_WAIT = "approval_wait"
STATE_CANCELLED = "cancelled"
STATE_UNKNOWN = "unknown"

# Ledger task states.
TASK_RUNNING = "running"
TASK_SUSPECTED = "suspected-stall"
TASK_COMPLETED = "completed"
TASK_CANCELLED = "cancelled"
TASK_NEEDS_OWNER = "needs-owner"

# Notification delivery states.
NOTIFY_PENDING = "pending"
NOTIFY_SENT = "sent"
NOTIFY_UNKNOWN = "unknown"
# Internal durable state while a worker owns the right to invoke transport.
# It is never eligible for automatic resend; restart recovery turns it into
# NOTIFY_UNKNOWN and requires explicit owner reconciliation.
NOTIFY_CLAIMED = "claimed"

# Helpdesk recurring schedule.
HELPDESK_HOUR_WARSAW = 17
HELPDESK_PROJECTS = ("example-crm", "example-workspace")

# Repair-request scopes. Approval authorises candidate-local work ONLY.
# Deployment, IAM, deletion and customer-data mutation need a separate
# explicit scope and are forbidden by default.
CANDIDATE_SCOPES = ("candidate-code", "candidate-tests", "candidate-review")
FORBIDDEN_SCOPES = ("deploy", "iam", "delete", "customer-mutation")


def envelope(kind: str, payload: dict) -> dict:
    """Wrap a CLI/decision payload in the versioned JSON envelope."""
    return {"schema": SCHEMA_VERSION, "kind": kind, "payload": payload}
