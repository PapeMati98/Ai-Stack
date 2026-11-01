# DOTS farm: staged synthetic candidate

Stdlib Python 3.13 pilot with two routines, private memory, rolling budgets,
deterministic cadence, fenced leases and durable effect intents. No service,
Hermes profile, real model, key, mail or application integration was activated.
`deploy/` is a proposal. Independent Qwen acceptance belongs to the parent.

## Run locally

From the task checkout, use the installed Python executable or `python -B`.
Choose a NEW absolute private runtime directory outside source. These commands
use synthetic budgets and targets only; no credentials or network are consulted.

```powershell
$runtime = Join-Path (Get-Location) 'shared/dots-farm/dots.py'
$state = Join-Path (Get-Location) 'shared/dots-farm/.test-runs/manual-pilot-unique'
python -B "$runtime" --state "$state" demo
python -B "$runtime" --state "$state" status
python -B "$runtime" --state "$state" pause observer
python -B "$runtime" --state "$state" tick
python -B "$runtime" --state "$state" resume observer
python -B "$runtime" --state "$state" reconcile
python -B "$runtime" --state "$state" hermes-plan
```

`demo` creates observer and collector, separate purpose memories and sources,
5,000,000 microUSD synthetic global cap, 500,000 reserve inside that cap, and
2,000,000 per routine. It retains all artifacts and refuses an existing DOTS
state. Supervisor has a declarative 1,000,000 candidate allocation, no inference.

For a custom synthetic source, create a UTF-8 JSON list as below (observer):

```json
[{"id":"change-one","org":"pilot-org","content":"Delivery moved to next month.","model_decision":{"action":"alert","reason_code":"relevant"},"cost_micro_usd":1}]
```

```powershell
python -B "$runtime" --state "$state" init --global-cap 5000000 --emergency-reserve 500000
python -B "$runtime" --state "$state" --source "$source" create watcher --kind observer --org pilot-org --project pilot --budget 2000000 --interval 900 --max-call 10
python -B "$runtime" --state "$state" tick --dot watcher
python -B "$runtime" --state "$state" status --dot watcher
```

Start custom creation with a separate state from `demo`. `--source` precedes the
command; creation saves its absolute reference. Without a saved source, the
runtime reads `<state>/sources/<dot_id>.json`. Missing/failed input produces
visible failed/inconclusive coverage. Empty list is quiet and successful.
Global cap defaults to zero until explicit `init`. Reconfiguring it preserves
spend and unknown holds. Integer microUSD only. Ordinary calls cannot consume
the emergency partition. CLI never auto-reserves emergency money.

Collector sources use action `collect`, plus an `invoice` object with org,
number, ISO date, positive finite amount and currency. Supported synthetic
currencies: PLN/EUR/USD/GBP/CHF/CZK; ASCII invoice identity, maximum two decimal
places and amount 1,000,000,000. Unsupported/ambiguous values go to needs_owner.
Target records are drafts, without any tax or payment approval.

## Memory and correction

Private UTF-8 text files avoid embedding memory in command lines. Output returns
IDs/revisions only. Read contents through the authorized private Store interface,
not logs or Office.

```powershell
python -B "$runtime" --state "$state" memory watcher --memory-id contract --text-file "$privateText" --source-ref owner
python -B "$runtime" --state "$state" memory watcher --query milestone
python -B "$runtime" --state "$state" correct watcher --memory-id contract --text-file "$correctedText"
```

Retrieval is dot-bound, at most five records/4096 bytes, selected against new
content. Corrections increment revision and replace active text across restarts.
Memory is untrusted evidence; it never changes grants. No whole-history prompt
cache or stable wiki promotion exists in the pilot.

## Failure, STOP and recovery

1. `pause <id>` persists STOP using an independent short Store connection, even
   while inference is blocked. Resume never revives an old lease. Effects recheck
   STOP, fencing token and the full config/version immediately before adapters.
   An already-started external operation cannot be recalled: real remote fencing
   and that race still require adapter/VM qualification.
   Cancellation after reserve but before inference settles that unused hold at
   zero. A later tick creates a fresh reservation and rechecks current caps;
   cancelled starts do not consume the maximum three inference attempts.
   Process loss after the journal commit but before reserve leaves no Store hold:
   after lease expiry, recovery keeps the input and attempt count, rechecks current
   caps and acquires a fresh reservation. An unresolved real hold retains unknown cost.
   Loss after cancellation settles zero but before `ready` commits follows the
   same fresh-hold recovery, preserving attempts; a nonzero settlement is never
   treated as a free retry.
   A legacy blocked entry without a Store reservation or saved reply can be
   recovered explicitly with `reconcile --dot <id> --reservation <id> --actual-cost 0`.
   Other costs are rejected for that absent hold; reconciliation makes no model
   call, and the next tick still needs a new reservation under current caps.
2. Due UTC intervals use last attempt. Missed slots coalesce into one poll;
   `--force` is an explicit single cadence override, never a grant/cap override.
   Daily/local-time/DST schedules are unsupported and rejected, not mapped to UTC.
3. `status` shows last success/attempt, coverage, pending reasons, reservations,
   unconfirmed effects and exact budget totals. Tick JSON outcomes include empty,
   ok, pending, failed, inconclusive, not_due, leased, paused, stopped_or_fenced.
   A successful process exit means the CLI ran, not that coverage is complete.
4. Unknown inference disposition/cost retains its full max-call hold, persists
   input and blocks automatic re-inference/effects. Model failures are not free
   calls. Explicit synthetic measured settlement:
   `reconcile --dot <id> --reservation <opaque-id> --actual-cost <integer>`.
   A stored decision can then finish on the next tick. Without a decision, owner
   settlement permits a bounded retry (maximum three total calls). Live cost
   reconciliation is not available/qualified through this CLI.
   Reopening a saved decision rechecks cost policy: unknown cost stays held and
   blocks effects; a known cost above max-call records the cost and pauses DOTS,
   including an interruption between saving the reply and settling its cost.
5. Intents commit before dispatch; attempts become unknown before adapter calls.
   Unknown draft effects use lookup, never blind writes. `reconcile --dot <id>`
   only looks up persistent targets. A missing receipt stays unresolved. Up to
   three automatic delivery checks per job, one put, no fallback. Synthetic
   Office has no lookup API: interrupted Office sends stay explicitly unresolved
   rather than being reissued. STOP/version changes may leave an intent pending.
   Target reconciliation requires an active lease: paused or already leased DOTS
   return `paused_or_leased` without a lookup. For recovery while scheduling stays
   disabled, explicitly `resume <id>`, run lookup-only `reconcile --dot <id>`, then
   `pause <id>` again; do not run a tick in that window. Cost settlement is separate.

Controller Store and journal are private SQLite WAL files. Synthetic target and
Office are separate SQLite sinks, default `<state>/targets`, or explicit absolute
`--target-state`. Target invoice identity is global by org/number/date across
collectors, restarts and controller restore. Before any invoice intent/effect,
the private journal atomically claims org/number/date with the normalized invoice
payload. Changed amounts/currencies produce needs_owner, including concurrent
collectors. Current Store effects also protect existing controller history.
Opaque target lookup cannot verify historical payload conflicts
after a snapshot restore. That capability is BLOCKED for live acceptance.

Each queue is bounded by max_queue, total serialized item by max_payload_bytes,
file source by min(1 MiB, queue*payload+4096), and retained journal by
max_state_records (default 1000; at most 10000). Lifetime record exhaustion is
visible inconclusive/pending; no implicit deletion or rotation. Store memory is
also bounded. OS RAM/disk, total fleet capacity, real source streaming/OCR and
production retention remain unqualified. Status and Office use fixed metadata
reason codes; model reasons cannot become arbitrary shared text. Peer messages
are optional typed advice, mutual peer/action grants, same org/project, hop<=2,
TTL<=3600, dedupe; no recipient tool execution. CLI exposes no peer dispatch.

## Candidate isolation and runbook

The implemented single controller has logical per-DOTS namespaces. It is NOT an
OS sandbox. Windows state inherits task ACLs; Unix creation requests mode 0700
(existing directories require independent checks). No cross-principal or disk
quota claim follows from local tests. See `deploy/isolation-plan.json` for the
separate worker principals and secret broker that are NOT implemented yet.
The synthetic systemd candidate denies network and has deterministic timer wake,
no model/supervisor dependency. Units were not parsed by a live systemd host.

`hermes-plan` calls actual frozen profile factories for NEW per-DOTS or supervisor
homes: fresh memory, one writer, no credentials/global fallback/Robert changes,
script-only no_agent wake, suggestions only. No installed Hermes compatibility
was checked. No separate paid supervisor is running.

Before live activation, the parent must qualify exact model/provider quality and
data policy, measured input/output ceiling and fresh tariffs, broker/key caps,
bounded source/target adapters with remote fencing and reconciliation, least
privilege workers, egress/VM resources, service and Hermes compatibility, UAT and
authorized effects. Accepted OpenRouter adapters have local fake-transport tests;
CLI/Engine do not wire them into live execution. No implied model fallback exists.

For synthetic backup/restore: pause all DOTS and stop scheduling in the chosen
isolated test environment; keep the controller/journal pair from the same quiet
checkpoint using SQLite online backup, with code/config version and hashes.
Restore both together; retain target/Office as their independent system of record.
Restoring a paid ledger would roll costs back: reconcile with provider/targets
before any resume; live recovery is BLOCKED. Avoid copying only a main DB file
while WAL has active writes. No backup/restore commands delete anything.

Rollback: pause candidate routines/timers, retain all state/unknown holds, select
the previous code/config and inspect status. Keep timers and other tick workers
stopped. If target confirmation is needed, explicitly resume one DOTS, run
lookup-only reconcile, and pause it again before restoring any scheduling.
Check that its final status is paused; unresolved receipts/holds remain visible.
Paused target reconciliation is a no-op (`paused_or_leased`). Do not remove
profiles, keys, DBs or fixtures. No repository commit,
deployment, skill sync or shared-skill private backup is performed by this maker.

## Verification

Journal startup reads the current SQLite mode and sets WAL only when needed.
Competing initialization retries only SQLITE_BUSY/SQLITE_LOCKED for at most ten
seconds, closing each connection before retrying. Other errors propagate;
inference, invoice claims and effect dispatch have no new retries. Tests include
fresh concurrent constructor+tick workers, eight fresh constructors, an existing
writer, and a retained lock that exceeds the initialization deadline.

```powershell
$env:DOTS_TEST_ROOT = Join-Path (Get-Location) 'shared/dots-farm/.test-runs'
$absoluteEvidenceRoot = Join-Path $env:DOTS_TEST_ROOT 'acceptance'
python -B -m unittest discover -s shared/dots-farm/tests -p 'test_*.py' -v
python -B shared/dots-farm/tests/pilot_acceptance.py --output "$absoluteEvidenceRoot"
```

DOTS_TEST_ROOT must remain inside this task workspace. Acceptance `--output`
accepts any explicitly supplied absolute evidence directory with write access,
including a parent-owned directory outside the checkout.
Default fixture root is `shared/dots-farm/.test-runs`; every run uses fresh UUIDs
and retains all fixture directories. Subprocesses use CREATE_NO_WINDOW on Windows.
Reports use a fresh UUID directory with exclusive creation on every run; existing
reports and fixtures are never overwritten or deleted. A separate report root
does not move the synthetic fixtures out of DOTS_TEST_ROOT. Retain older evidence
as history, not current acceptance.
`pilot_acceptance.py` records executed tests, neutral CLI command evidence, all
31 expected/actual cases and current source SHA-256. Consult ACCEPTANCE.md for
honest local/external qualification boundaries. The maker report is evidence for
independent review, not independent ACCEPT or a live deployment verdict.
