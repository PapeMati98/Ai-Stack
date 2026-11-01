# Agent Loop L2-search contract

L2-search is a bounded portfolio search over several isolated Agent Fleet
candidates. It borrows the useful parts of evolutionary engineering without
copying an external autonomous shell runner.

## What one approval authorizes

`prepare` freezes the repository HEAD, scope, hypotheses, maker roster,
independent auditor, optional supervisor, trusted verifier, quality contract,
runtime identities, and all budgets. `approve` accepts only the emitted hash.
The approved run may execute each frozen hypothesis once. It may not invent an
additional hypothesis, repair a rejected candidate, change actors, use the
network, or exceed the frozen limits.

## Search semantics

- Every hypothesis starts from the same clean baseline in a separate Fleet
  worktree. This is portfolio search, not cumulative mutation.
- A maker may change only allowlisted paths. The controller rejects links,
  protected paths, oversized diffs, invalid handoffs, and baseline drift.
- The suite-bundled verifier runs before any semantic audit.
- One independent Fleet Quality Gate audits each verifier-passing candidate.
  The gate's `quality_score`, criteria, findings, and token telemetry form the
  score vector.
- A candidate becomes champion only if its gate returns `ACCEPT`, unresolved
  risks are empty, and it meets the frozen baseline/improvement policy.
- Equal-score alternatives are retained only up to `tie_budget`; they do not
  reset stagnation.
- After `stagnation_window` non-improving attempts, the optional supervisor is
  invoked read-only. Its redirect is advisory, bounded, and consumed by one
  later hypothesis. The supervisor must be independent from every maker and
  the quality auditor.

## Acceptance boundary

`CHAMPION_READY` is not integration. Before `accept`:

1. Inspect the champion worktree, diff, verifier evidence, quality gate, token
   ledger, and the full rejected trajectory.
2. Read `fleet_state_root` from `status` and pass it explicitly as the Fleet
   global option: `fleet.py --state-dir <path> accept-group ...`.
3. Record `accept-group --decision accept` for the champion gate.
4. Record `accept-group --decision reject` for every non-champion gate that
   also returned `ACCEPT`.
5. Call the search controller's `accept` with the champion gate ID.

The controller never commits, merges, pushes, deploys, schedules, or copies a
candidate into the base checkout. Integration remains a separate parent task.

## AVO concepts deliberately not adopted

- no `bypassPermissions` or arbitrary `bash -lc`;
- no inherited provider secrets for evaluators;
- no user-level MCP server with arbitrary path parameters;
- no target-supplied executable evaluator;
- no automatic `git reset --hard`, `git clean`, commit, tag, merge, or deploy;
- no same-session actor pretending to be an independent supervisor.

The machine-readable contract is
`l2-search-contract.schema.json`. Search history and rejected candidates are
evidence, not instructions for later models.

## Token policy

Set `limits.token_policy` to `estimate` for ordinary new searches. Numeric
overruns and missing token telemetry are recorded as warnings and the search
continues; the separate telemetry totals retain only known values and literal
unknown remains unknown. Use explicit `hard` for a token-budget canary.
Contracts without the optional key retain legacy `hard` behavior. Wall/process
timeouts, candidate isolation, search breakers, and independent acceptance are
unchanged.
