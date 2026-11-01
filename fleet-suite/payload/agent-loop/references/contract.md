# Agent Loop V0.1 Contract

## Scope

V0.1 operationalizes recurring L1 analysis without granting implementation
authority. The parent agent remains responsible for task framing, acceptance,
and any later implementation.

The controller is standard-library Python and invokes only Agent Fleet. It does
not depend on an npm package, MCP server, provider SDK, or shell parser.

## Project layout

```text
<project>/
  .agents/
    loops/
      .claims/
      .claims.lock
      <loop_id>/
        loop.json
        gate.json
        task.md
        assessment.json
        state.json
        runs.jsonl
        loop.lock
        reports/
```

`loop.json`, `gate.json`, `task.md`, and `assessment.json` are operator-owned
inputs. `state.json`, `runs.jsonl`, `loop.lock`, `.claims/`, `.claims.lock`,
and `reports/` are controller-owned operational evidence. The shared claim
registry is reserved controller metadata; a loop contract cannot redirect it.

## Input rules

- JSON must be UTF-8, contain no BOM or NUL, and contain no duplicate keys.
- `loop_id` must match `^[a-z0-9][a-z0-9-]{1,63}$`.
- Referenced task, assessment, and kill-switch paths must remain inside the
  loop directory and must not traverse a symlink or Windows reparse point.
- Watched paths and claims are relative to the project root.
- L1 `allowed_write_areas` must equal `.agents/loops/<loop_id>/**`.
- The task assessment must satisfy Agent Fleet's current assessment schema.
- `gate.json.verifier_default` must be `REJECT`.

The detailed machine-readable contracts are:

- `loop.schema.json`
- `gate.schema.json`
- `state.schema.json`
- `runs-entry.schema.json`

The Python validator is authoritative for V0.1 execution and applies additional
cross-field and path checks that JSON Schema alone cannot express.

## State machine

```text
IDLE -> DELEGATING -> IDLE
                   -> STOPPED
                   -> active state persisted after unexpected interruption

persisted active state --operator-confirmed recovery--> STOPPED
```

An open circuit breaker prevents another run. `STOPPED` is terminal in V0.1;
recovery closes interrupted evidence but does not clear the breaker. Breaker
reasons include:

- manual stop or kill switch;
- timeout or interrupted process;
- repeated Fleet failure;
- repeated no-progress marker;
- run or token budget exhaustion;
- missing token telemetry under fail-closed policy;
- watched-scope mutation.

## Exactly-once accounting

`runs.jsonl` is append-only. A unique loop `run_id` is charged once. A run event
never changes after append. Parent acceptance is a second event linked to both
the loop `run_id` and the Agent Fleet `fleet_run_id`.

Daily totals are reconciled from the ledger rather than trusted from mutable
state. Unknown tokens are not converted to zero, estimated, or invoiced.
New ordinary-work contracts set `limits.budget.token_policy` to `estimate`;
numeric overruns and unknown telemetry are then recorded as warnings while
the run continues. `hard` preserves fail-closed token enforcement. The
optional key is absent from older immutable contracts, which therefore retain
legacy hard behavior. `unknown_token_policy` remains accepted for
compatibility with those contracts.

## Locking and claims

Each loop has an OS-level file lock. Shared path claims are persisted under
`.agents/loops/.claims/` and serialized by `.claims.lock`. These are the only
controller writes outside the current loop directory. A second active loop
with an overlapping claim is refused.

Stale claims may be reclaimed only when their recorded process is no longer
alive on the same host. On Windows this check uses process APIs rather than
POSIX signal behavior.

## L1 mutation detection

The controller hashes watched file contents before and after Fleet execution.
It excludes `.git` and `.agents/loops`. Any content change opens the breaker,
marks the run rejected, and records changed paths.

The controller separately hashes `loop.json`, `gate.json`, the task,
assessment, state, and ledger around the Fleet call. A control-plane change
opens a dedicated breaker, produces a non-acceptable incident report, and does
not append a run event to a potentially compromised ledger.

`writes_performed` means detected unauthorized watched-project or control-plane
mutations; it does not count expected state, report, lock, ledger, or claim
metadata writes.

This is post-run detection, not preventive containment. A future L2 contract
must add an isolated worktree or sandbox, independent verification, mutation
approval, and a separate owner decision.

## Environment and subprocess rules

- Use structured `subprocess` argument arrays with `shell=False`.
- Send no interactive stdin.
- Scrub inherited environment variables by deny-by-default policy. In
  particular, do not inherit `PYTHONPATH`, `PYTHONHOME`, controller path
  overrides, model-radar path overrides, or arbitrary `AGENT_FLEET_*` values.
- Never inherit API keys, cookies, tokens, passwords, or secret-shaped values.
- Do not use `--allow-write` or `--inherit-api-keys`.
- Set Agent Fleet `--native-subagent-limit 0` to avoid nested fan-out.

## Zero-write commands

`doctor`, `validate`, and `plan` are read-only. `status` is read-only when
operational files are already present and reports `MISSING` otherwise.

`run`, `stop`, `recover`, and `record-result` are intentional writes restricted
to the loop operational directory.
