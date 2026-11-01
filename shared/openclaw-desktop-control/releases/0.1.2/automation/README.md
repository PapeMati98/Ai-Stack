# openclaw-ops (TEST candidate)

Small stdlib-only Python package for the OpenClaw operational heartbeat and
the recurring production helpdesk, per `HEARTBEAT-DESIGN.md` (accepted
recovery protocol). This is a **TEST candidate**: the parent Codex wires
real transports, Telegram integration, inventory and release activation.

Run from this directory:

```
python -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
python -m unittest discover -s tests
```

No cron install, no network, no models, no customer data. No third-party
dependencies.

## What it does

- **Authority ledger** (`openclaw_ops/ledger.py`): one SQLite file is the
  single authority host. Leases are compare-and-swap on
  (task, generation) inside `BEGIN IMMEDIATE` (`TTL 120s`, renewal
  increments a monotonic fencing token). Dispatch intent is persisted
  **before** sending under the idempotency key
  `task:generation:attempt`. The events table is append-only.
- **Watchdog** (`openclaw_ops/watchdog.py`): one bounded pass compares
  ledger intent with fresh native provider status (max 10s old).
  Recovery needs a live owner grant, a capable adapter, a terminal
  provider failure with a transient cause marked resolved, attempts and
  backoff budget (`60s / 5min / 30min`, max 3), an acquired lease, and a
  post-lease cancellation/revision recheck. Anything else is observe-only.
- **Helpdesk** (`openclaw_ops/helpdesk.py`): daily run per
  `project + Europe/Warsaw local date + schedule generation`, due at
  17:00 Warsaw time (EU DST rules; `zoneinfo` when present, embedded
  fallback otherwise). Read-only findings from the legacy `umbrella-crm` and
  `initech-app` collector bindings (technical compatibility names, not grants); bounded sanitized summaries only
  (1600 UTF-8 bytes excerpt / 6000 chars synthesis, reject-don't-truncate).
  Dedup on `(stable_id, updated revision)`; quiet when nothing is new.
  New findings create a *proposed* bounded repair request + owner
  notification. Nothing dispatches without exact owner approval.
- **Approvals** (`openclaw_ops/approvals.py`): a Telegram message
  authorises only the exact bound tuple -- allowlisted
  `(senderID, chatID)` + pending request id + exact revision/scope/hash +
  unexpired window + canonical `APPROVE ...` text. Replays (persisted
  seen-cache via `serialise`/`restore`), stale/forwarded/wrong-sender/
  group messages and plain text never authorise. Message text is data.
- **Outbox** (`openclaw_ops/notify.py`): delivery states
  `pending/sent/unknown`, with an internal durable `claimed` state. A worker
  claims a row in SQLite before invoking transport; concurrent workers cannot
  claim it, and a claim older than the bounded handoff window becomes
  `UNKNOWN` without an automatic resend. After an uncertain ACK the entry
  stays `UNKNOWN` until the owner explicitly reconciles it.
- **CLI** (`openclaw_ops/service.py`): `tick --db PATH`,
  `status --db PATH`, `pending --db PATH`, all versioned JSON
  (`{"schema": "1.0", "kind": ..., "payload": ...}`). Diagnostics carry
  references/hashes only, never raw bodies or tokens.

## Supported vs unconnected

| Provider | Status |
|---|---|
| `FakeNativeAdapter` | Synthetic/test only: idempotent lookup + fencing. Used by the suite and synthetic resume tests. |
| `ObserveOnlyAdapter` | Explicitly unsupported providers: visible as `observe-only` / `UNKNOWN_DISPATCH`, never resumes, never resends after a crash. |
| Any real native resume API (Office/Telegram/production providers) | **Unconnected.** No universal native resume is claimed. Parent owns exact-source verification and integration via `register_adapter()`. |

Until the Office-integration acceptance lands, the active posture is
maintenance/observe-only: tasks without an explicit owner grant are never
auto-recovered, and there is no replay of arbitrary old conversations.

## Approval scope

An approval unlocks **local isolated candidate work only**
(`candidate-code`, `candidate-tests`, `candidate-review`). Deployment,
IAM, deletion and customer-data mutation require a separate explicit
scope and are recorded as `forbidden-needs-explicit-scope` by default.
This package implements no shell execution and holds no cloud/API
credentials.

## T6b scheduling hardening (candidate)

- **Bounded catch-up** (`helpdesk.process_catchup`): pre-17:00 ticks recover
  at most the latest missed Warsaw business day, exactly once per
  `project:date:generation` through the same durable run marker as the daily
  pass. A multi-day outage yields one catch-up, never a historical storm.
  DST resolves through the same Warsaw clock as the daily run.
- **Meaningful revisions** (`helpdesk.derive_revision`): collector-supplied
  title/body/status/severity/permitted comments are hashed **in memory** at
  the collection boundary; only the digest suffix enters the revision, and
  only fixed-schema metadata persists. A material change with an unchanged
  source timestamp still proposes; raw bodies never touch the database.
- **Bounded retry + one alarm** (`helpdesk.read_with_retry`,
  `helpdesk.record_collector_outcome`): only raising collectors are retried
  (max 3); BLOCKED results are final. One durable alarm row per
  project+reason fires on state change only -- repeated identical failures
  and steady OK stay quiet, across reboots. Auth failures never persist an
  empty successful run.
- **Durable approvals** (`approvals.DurableApprovalStore`,
  `repairs.authorize_repair`): decisions persist bound to the exact
  project/issue/revision/scope/scope-hash/candidate-hash/expiry, with a
  durable replay cache. Repairs run only after an exact owner decision, only
  for candidate-local scopes, and only through the injectable Graph hook
  (default: unconnected, no call). No model or remote call exists here.

## Unsupported live dependencies (truthful status)

| Dependency | Status |
|---|---|
| Legacy CRM collector read-only auth | **Not configured.** No token is manufactured, harvested, or extracted here; the collector exposes an injectable authorized reader (`token` param) and reports `read_only_auth_not_configured` without network. Parent owns the legal path. |
| Live production collector fetch | **Not performed by tests.** All contractor tests are synthetic; the legacy structural collector pins the exact read-only production binding (`mode=ro`, `query_only=ON`, fixed query) but those deployment-specific bindings are not portable account setup; live runs require the owner's actual scope and reviewed configuration. |
| Telegram send / owner notify | **Unconnected.** Proposals enqueue owner outbox rows; actual transport is the parent's wiring. |
| Graph repair dispatch / models | **Disabled by default.** `authorize_repair` takes an injectable hook; with none, work authorises but never dispatches. No model call exists in this package. |
| Cron 17:00 install / autostart | **Not installed.** The schedule logic (due/catch-up) is implemented and tested with fake clocks; timer installation is the parent's activation decision. |
