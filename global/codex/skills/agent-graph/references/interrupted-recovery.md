# Interrupted Recovery

Explicit, inspected recovery for TODO graph nodes left in `RUNNING` state by a
native-less interruption. No automatic startup reconciliation, process
termination, stale-lock deletion or hand-editing of state is permitted.

## Commands

```powershell
python <skill-dir>/scripts/todo_graph.py inspect --todo <todo.json> --run-dir <absolute-evidence-dir>
python <skill-dir>/scripts/todo_graph.py resolve-interrupted --todo <todo.json> --run-dir <absolute-evidence-dir> --inspection <inspection.json>
```

Both commands require an absolute `--run-dir`. `resolve-interrupted` requires a
hash-bound inspection record produced by `inspect`.

## inspect

`inspect` is read-only. It gathers, for every `RUNNING` node, exact hashes and
process/lock evidence:

| Field | Source |
|---|---|
| `task_hash` | `digest(task definition)` |
| `checks_hash` | `digest(task.checks)` |
| `run_id` | evidence directory name |
| `plan_hash` | `digest(plan)` |
| `scope_hash` | `snapshot(workspace, scope)` |
| `artifact_hashes` | `artifact_recovery.manifest(workspace, scope)` |
| `model` / `model_hash` | maker receipt / authoritative usage model identity |
| `runtime` / `runtime_hash` | maker receipt / authoritative usage runtime attestation (never transport_status) |
| `checkout_base` / `base_hash` | live independent workspace Git HEAD only (never a usage `worktree_base_commit_sha` fallback) |
| `lineage` | maker receipt / authoritative usage lineage / model_provider_id |
| `run_id` | maker receipt / authoritative usage Fleet run_id (independent of Graph directory name) |
| `maker_receipt` | authoritative maker result from attempt folder on disk |
| `process_evidence` | PID / start-time across maker, reviewer, check processes, and native checkpoints (`*.process-checkpoint.json`) |
| `lock_evidence` | `runner.lock` and workspace-lock PID and liveness |

The record is bound by `inspection_hash = digest(record without inspection_hash)`.

## resolve-interrupted

`resolve-interrupted` changes state only after proving no current native
process or controller remains, acquiring exclusive workspace and runner locks,
and verifying authoritative maker provenance on disk. Any unknown or adverse
evidence fails closed and leaves the original `RUNNING` state, history,
transport failures and artifacts untouched. It never fabricates `CANDIDATE` or
`ACCEPT` and never blind-replays the maker.

### Gates (in order)

1. **Scheduler invariants** — plan must validate; evidence directory must
   reside outside execution workspace.
2. **Inspection hash** — `inspection_hash` must match the record body.
   Recomputed hashes on tampered inspections fail closed against live workspace
   and task integrity.
3. **Run id & plan hash** — `run_id` and `plan_hash` must match current run
   directory and validated plan.
4. **Lock serialization** — acquires `workspace_lock` and `runner.lock`
   atomically without deleting foreign locks. Exclusive locks are released by
   atomic same-directory rename to a unique retained `.released` proof after
   verifying file identity and owner bytes, never unlinked. Any existing lock
   held by another process or in unknown state blocks recovery.
5. **State re-check under lock** — node must still be `RUNNING` in `state.json`.
6. **Process tree & supervision evidence** — all recorded processes (maker,
   reviewer, test checks) and native supervision checkpoints
   (`attempt-1.process-checkpoint.json`) must be dead. Orphan children, unknown
   liveness or unknown start provenance (`started_at=None`) refuse recovery.
   On Windows a live PID may belong to a newer unrelated instance. Recovery
   admits this only with native GetProcessTimes creation evidence strictly
   newer than the recorded start plus a conservative one-second margin. Raw
   PID liveness, creation FILETIME and reuse facts remain in every entry; an
   earlier or close creation time never proves reuse. Every other live/unknown
   recorded instance still blocks. One fresh native sample per PID per scan
   replaces repeated tasklist/wmic subprocesses; no observation is cached
   across inspect and resolve. Locks retain their existing conservative rules.
7. **Task / checks integrity** — `task_hash` and `checks_hash` must match
   current plan task definition and checks.
8. **Required receipt & usage fields** — `model`, `runtime`, `base`, `lineage`,
   and `run_id` must all be non-empty and present. `runtime` cannot fall back to
   `transport_status` and must be `VERIFIED`.
9. **Authoritative on-disk receipt & mandatory local usage** — maker result must
   exist on disk in the attempt directory (`result.json`); progress-only receipts
   fail closed. Authoritative on-disk status must be `CANDIDATE`/`PARTIAL`.
   Mandatory authoritative local Fleet usage JSON is resolved only via the
   result `usage_record` path (never a loose `usage.json` fallback), matching
   Fleet run_id, model, lineage, base commit, execution workspace, exit
   code 0, and completed terminal reason. Result lineage/base must agree with
   the usage record when present.
10. **Checkout base** — usage base commit and workspace Git HEAD must match
    inspection.
11. **Artifact / scope integrity** — `scope_hash` and `artifact_hashes` must
    match current workspace (`artifact_recovery.manifest`).
12. **Trustworthy maker receipt** — status must be `CANDIDATE` or `PARTIAL`
    with verified `retained_candidate` proof. Raw unverified `PARTIAL` fails
    closed.

### Recovery path

When every gate passes and a trustworthy maker receipt exists the node
transitions through the existing `review_pending` mechanism: declared checks
run, then an independent reviewer evaluates the unchanged scope and checks. The
original `RUNNING` state is preserved in `progress.json` history as a
`recovery` event. The retained maker is never re-invoked.

When any gate fails the resolve result is `BLOCKED` with a diagnostic reason
and `original_status: RUNNING`. Process/lock/provenance refusals keep the node
`RUNNING` in `state.json` so the existing scheduler guard still blocks blind
replay. No `CANDIDATE` or `ACCEPT` status is fabricated.

## Synthetic fixtures

Tests retain fixture directories in the caller-owned directory selected with
`--artifact-root`. Historical private test evidence is not included in this source
edition; choose a new local evidence directory for a new run.
Real-user run IDs are never used for tests. Hidden Windows children use
`CREATE_NO_WINDOW`; bytecode writes are disabled.

## Related

- `scripts/test_todo_graph_recovery.py` — executable regression tests.
- `scripts/artifact_recovery.py` — `manifest` and `verify_usage_record` helpers.
