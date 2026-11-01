# hub-caretaker

Small Python standard-library caretaker integration under `hub-caretaker`. It
mirrors contracts visible in `evidence-input/clawops` without executing or
editing that repo. The existing Hub remains the sole system of record.

## Scope (this slice)

- Run-once CLI with explicit config. No scheduler install, no scans of real
  user sessions, no network/model/Hub/Office calls during implementation.
  The owner Windows scheduled task remains the live authority; this tree is
  a candidate only.
- Reads allowlisted session JSONL: Codex / OpenClaw / Claude native schemas or
  normalized events. Other schemas are reported separately and skipped.
- Ignores raw reasoning/tool dumps, secrets, and `restricted` payloads.
- Preserves `source`, `tool`, `native_session_id`, and `cursor` provenance.
- SQLite-backed event/outbox/checkpoint. Reserved handoffs can be retried after
  restart without re-reading their inputs. Pre-synthesis pending records still
  require the original input to be re-read; input rotation is not yet covered.
- Pinned local primary (`vllm/Qwen3.8-27B` per fleet/profile). On local outage,
  emits a durable incident and preserves the queue; the optional fallback is
  the paid Muse Code subscription only via `muse-code/muse-spark-1.3` with an
  explicit result identity. The metered Meta HTTP path is forbidden.
- All Hub writes go through the canonical knowledge gateway
  (`knowledge.py` inventory / begin-task / verify-context / record-handoff)
  with health, lint, and context-read prerequisites. A dirty worktree fails
  closed without consuming unpublished work.
- Office transport uses the canonical wrapper with scoped target/session, a
  rate bound, dedupe, and reply correlation. No autoplay reply loops.
- No-input means zero model calls. Empty inputs short-circuit cleanly.

## Autonomous publication loop (candidate)

Maintenance ticks can drain an existing Hub backlog without a manual per-batch
invocation. This does not install a scheduler and does not activate live
publication.

- Policy defaults: `min_ready_groups=10`, `max_wait_hours=24`, `max_batch=8`
  physical proposals per bounded project transaction.
- A nonempty ready backlog triggers a frozen audit cycle at 10 ready groups
  **or** when the oldest ready group is at least 24 hours old (even a single
  group). Counts come from proposal files and curation ledgers, never from
  raw intake events.
- A triggered cycle freezes admitted proposal IDs, paths and hashes. Bounded
  per-project batches drain over later ticks; new arrivals wait for the next
  cycle. Empty work never invokes a model (quiet idle).
- Grouping is conservative: project + producer/agent **and** episode/session
  when both are present, otherwise exact source IDs. Producer/agent alone
  never collapses distinct sources. Oversized groups are blocked with an
  explicit reason rather than dropped or split. Every original source ID in a
  selected group stays accounted.
- Each project batch gets its own maker pass (`vllm` / `Qwen3.8-27B`) and an
  independent Muse Spark 1.3 subscription review of the exact candidate
  bytes. A global cycle is not one model review. The reviewer remains the
  existing OpenClaw Muse plugin transport; that is not an Agent CLI audit.
  Reviewer unavailability defers; there is no self-review and no API fallback.
- Maker format failures (wrong type, too short/long, frontmatter, generic
  invalid bounded body) get one targeted repair with the same frozen packet.
  A review `REPAIR` gets at most one maker repair and then a **new**
  independent review of the changed manifest. A previous `PASS` is never
  reused. Long existing pages are kept pending rather than truncated.
- Transient model transport outages use bounded exponential backoff (cap 4h)
  instead of a permanent two-error freeze. Deterministic input/policy/semantic
  failures quarantine that fingerprint until the input, code or policy
  fingerprint changes.
- `RUNNING` after a crash is reconciled against a canonical committed
  event/transaction. Caller-supplied status strings are not proof. If
  evidence is missing the row stays `UNKNOWN_REQUIRES_RECONCILIATION` and
  is not republished.
- Publication is decoupled from an unrelated ingest pipeline failure: a
  healthy existing Hub backlog remains eligible. The kernel single-writer
  lock is unchanged. Only the canonical publisher writes stable wiki.

## Out of scope (acknowledged limitations)

- Live activation, project publication, scheduler install, credential
  changes and production rollout stay with the parent.
- Periodic refresh is not implemented here (no scheduler in this tree).
- No telemetry, no remote calls, no model CLI exec beyond the configured argv
  transport wrappers (which are injectable for tests).
- No deletion, no credentials/auth reads or mutation, no metered Meta API.

## Layout

```
hub-caretaker/
  run.py                # CLI entry point (run-once, --dry-run, --status)
  config.example.json   # documented config example
  caretaker/
    __init__.py
    config.py           # config load + validate
    ingest.py           # JSONL readers (Codex/OpenClaw/Claude/normalized)
    filter.py           # redactor: secrets, restricted, reasoning/tool dumps
    store.py            # SQLite event/outbox/checkpoint/incident
    transports.py       # injectable OpenClaw + Muse argv transports
    office.py           # Office scoped wrapper with dedupe/rate bound
    hub.py              # Canonical knowledge gateway wrapper
    model.py            # Synthesis prompt + structured model decision
    pipeline.py         # End-to-end orchestration
    status.py           # dry-run / status reporter
  tests/
    fixtures/*.jsonl    # synthetic Codex/OpenClaw/Claude/normalized inputs
    test_*.py
```

## Configuration

See `config.example.json` for the documented shape. The caretaker only
writes inside the configured `state_dir` and reads inside the allowlisted
`input_paths`. The `Hub` and `Office` commands are not executed during this
slice; the wrapper classes accept injectable transports and a `dry_run` flag.

## Run-once CLI

```text
python run.py --config config.example.json --dry-run
python run.py --config config.example.json --status
python run.py --config config.example.json --once
```

- `--dry-run` enumerates input files, schema detection, expected records, and
  the planned pipeline order without invoking any model or Hub command.
- `--status` prints the SQLite event/outbox/checkpoint/incident summary.
- `--once` runs the full pipeline once with the configured argv transports.

## Tests

Run from the `hub-caretaker/` directory so the `caretaker/` package is on
``sys.path``:

```text
cd hub-caretaker
python -X utf8 -m unittest discover -s tests -t . -v
```

The tests cover: crash/retry/offline, secret detection, id-conflict,
provenance preservation, partial JSONL, local outage, fallback activation,
deduplication, the no-input short-circuit, publication threshold/age
triggers, frozen-cycle restart, transient backoff, poison-batch isolation,
uncertain crash reconciliation, format repair, and diagnostic redaction.

Parent verification on 2026-09-17: 83 tests passed, including six new restart,
reservation, explicit-hash and deleted-record cases, and a regression proving
gateway error text does not enter durable outbox state. Restart tests use a
deterministic simulated canonical server. Separate live read-only gateway
checks and a synthetic native OpenClaw-to-Muse inference probe passed. These
checks do not prove live unattended Hub publication, full semantic dedup,
all-agent ingestion, curation, or local Qwen availability. Successful Muse
fallback preserves the local-Qwen outage status. No recurring job is enabled.

## Forbidden in this slice

- Real Hub, Office, model, or network calls during implementation.
- Metered Meta HTTP API; the Contributor path is forbidden.
- Direct vault writes; only the canonical knowledge gateway is allowed.
- Commit, push, deploy, or automatic curator privilege.
- Scanning actual user session stores (only synthetic fixtures are read).
