# Agent Graph 0.2 execution contract

## Purpose and ownership

Agent Graph is an append-only coordination contract over existing executors.
The parent architect is the active coding agent. It may use its own reasoning,
project discovery, Model Council, and verified Knowledge Hub context to produce
the plan. The deterministic helper validates and records the plan but does not
replace the parent planner.

The user owns business intent, acceptable risk, sensitive actions, production
approval, and changes to global budget or data classification. Technical
replanning that preserves those boundaries belongs to the parent.

## Plan revisions

The approved revision used at run initialization is preserved exactly in state
and its matching `plans/revision-N.json` snapshot. A later revision is produced
only from an approved planning-contract amendment and contains:

- every historical node from the previous revision;
- invalidated nodes and descendants retained with `active: false`;
- newly appended active nodes with new IDs;
- `previous_revision_hash` equal to the canonical SHA-256 of the prior plan.

An amendment does not mutate evidence already recorded for an old node and
cannot edit a node definition in place. It appends replacement IDs and changes
which nodes are active for current final acceptance.

## Node state machine

```text
pending -> running -> accepted
                   -> rejected -> running (same-scope bounded retry)
                   -> blocked
         -> recovery-required -> running | blocked

accepted/rejected/blocked -> inactive only through an amendment
```

`ready` returns an active `pending` node when all active `depends_on` nodes are
accepted. A rejected node may be started again only within its attempt budget.
For active nodes, `evidence_from` may name only dependency ancestors. Historical
inactive evidence remains available to a forward repair node created by an
amendment; evidence never grants dependency readiness.

## Four node kinds

- `work`: discovery, contract definition, implementation, integration,
  user-journey execution, or another artifact-producing activity.
- `verify`: deterministic checks, visual/UX/accessibility review, or another
  bounded verifier.
- `gate`: independent quality, incremental security, or parent final acceptance.
- `human`: an explicit owner decision. The node records approval evidence but
  never performs the approved action.

`capability` gives the semantic role. `executor` maps the node to one existing
public primitive. Neither field pins a provider or model version.

## Isolation and scopes

Write scopes are repository-relative path prefixes:

- directory prefix: `frontend/src/`
- exact file: `frontend/package.json`

Absolute paths, parent traversal, dot segments, backslashes, and wildcard
characters are invalid. Two active mutating nodes that are unordered by graph
dependencies may not have overlapping write scopes or shared resource locks.
Read scopes may overlap. Reviewer, locks, Loop policy and Council trigger are
hash-bound fields of every node.

Every mutating node uses either:

- `detached-worktree`: Fleet/Loop owns an isolated worktree; or
- `integration-owner`: the one parent-owned integration node.

When two unordered mutation branches exist, `integration_node_id` is required.
That node must transitively depend on every active mutating branch. Agent Graph
does not create or merge worktrees.

## Evidence and acceptance

Every criterion has a stable criterion ID. An accepted result must contain one
`PASS` result for every criterion and no `FAIL`. Mutating nodes must report all
changed paths, and each path must be within the declared write scope.

Artifact entries use a relative path, byte size, and SHA-256. The helper reads
the workspace bound at `start` only to verify those values. A repeated start of
the same node cannot switch that absolute workspace root. External references
bind the node to Fleet/Loop/gate evidence without copying raw model traces.

Model-authored prose, verdicts, file paths, and external references are
untrusted claims. Schema validity is necessary but not sufficient: the parent
must independently inspect cited artifacts and deterministic checks before
acceptance.

Executor requirements:

| Executor | Required external reference |
|---|---|
| `fleet-run` | `fleet_run` |
| `agent-loop-l2` | `agent_loop` |
| `fleet-quality-gate` | `fleet_quality_gate` |
| `fleet-security-gate` | `fleet_security_gate` |
| `human` | `human_approval` |
| `parent-local` | none; deterministic checks still need evidence |

Fleet remains responsible for provider independence and gate schema. Agent
Graph validates that the expected Fleet reference exists, not its private
storage internals.

An implementation graph must contain both an independent quality gate and an
incremental security gate. Quality gates explicitly list all applicable
implementation, integration, deterministic-tests, user-journey, visual, UX, and
accessibility nodes in `evidence_from`. Security gates explicitly list every
implementation and integration batch. Their evidence must repeat the external
Fleet/Loop references of every covered source node, so an unrelated reviewer
run cannot satisfy the gate.

## Repair and cross-node defects

Use the existing same-scope Fleet repair or certified L2 loop while the node
remains the owner of the defect. If a downstream result invalidates another
node's output, create an amendment:

1. identify the triggering node and exact reason;
2. deactivate the triggering node or one of its upstream provenance ancestors,
   plus the affected transitive active descendants;
3. append a repair node that consumes old evidence through `evidence_from` and
   depends only on still-valid prerequisites;
4. append the minimum re-verification path;
5. keep the old evidence immutable.

The macro graph remains acyclic because repair is forward-only in a new plan
revision.

## Budgets and fast path

Budgets are hard ceilings, not estimates:

- `max_attempts`: starts of one node;
- `max_wall_seconds`: runtime bound supplied to the executor;
- `max_tool_calls`: executor tool bound;
- `max_tokens`: total model tokens; mandatory for every model-backed executor;
- graph-level `max_total_attempts` and `max_total_tokens`.

Unknown quota or cost never becomes zero. Unknown token telemetry is charged
against the execution pool as a pessimistic `budget.max_tokens` estimate and
never disappears as a silent zero. Planning-gate tokens live on
`owner_ceiling.planning_and_review` and do not debit this graph brake.
The parent checks Fleet quota before dispatch. The graph-level token ceiling is
checked before every start, including nodes whose executor does not consume
model tokens. A gate may be omitted only
before initialization and only when its risk is genuinely absent. Every active
node is mandatory; low quota does not authorize dropping or degrading it.

Use no graph for a small direct task, a compact three-stage graph for one
implementation stream, and a larger DAG only when distinct artifacts or
acceptance boundaries justify it.

## Recovery and human gates

Every mutating command has an `operation_id`. Repeating the same operation ID
with the same payload is idempotent; reusing it for another payload fails.
State is updated atomically under an OS file lock. `recover` marks stale running
nodes `recovery-required`; it does not decide whether an external process is
alive and never launches a retry.

Human nodes may define `expires_after_seconds`. Expiry blocks the node and
requires a new approval/amendment. There is no force-unblock: override is an
explicit new owner decision with a new operation and evidence hash.

## Final acceptance

`close` succeeds only when:

- the latest plan revision and state hashes agree;
- the approved contract snapshot, linkage fields, deterministic projection and
  current plan agree;
- every active node is accepted;
- no active node is running, rejected, blocked, stale, or recovery-required;
- the active final-acceptance gate is accepted;
- graph budgets were not exceeded;
- the current planning contract has a `COMPLETE` closeout receipt whose hash is
  snapshotted into the run; the runtime revalidates material requirement
  coverage, criterion linkage and relative evidence file hashes before close.

Closing a graph does not merge, commit, push, deploy, send, or publish anything.
