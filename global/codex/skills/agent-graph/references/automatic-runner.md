# Automatic bounded schedule

```powershell
python "<agent-graph>/scripts/agent_graph.py" run --run-id <run-id> --dispatch <dispatch.json>
```

Ordinary `run` still refuses a dispatch whose hash differs from the stored runner journal. After an accepted planning amendment, adopt the new map explicitly:

```powershell
python "<agent-graph>/scripts/agent_graph.py" adopt-dispatch --run-id <run-id> --expected-old-dispatch-hash <sha256> --dispatch <new-dispatch.json>
```

Required arguments are `--run-id`, `--expected-old-dispatch-hash` (SHA-256 of the currently stored dispatch), and `--dispatch` (the new map bound to the current approved plan). Optional `--state-root` matches `run`. The operation holds the runner lock then the Graph state lock, verifies the stored old dispatch, prior plan snapshot and accepted amendment chain, validates the new dispatch, snapshots the old map, and appends an adoption record. Retained node adapters cannot change; running or unresolved attempts are refused. The same request is recovered after a crash; a conflicting request is refused without resetting the journal.

A nonzero supervisor receipt stays a failure. When canonical terminal Graph evidence already binds the exact node and attempt, annotate the journal without manufacturing acceptance:

```powershell
python "<agent-graph>/scripts/agent_graph.py" reconcile-failure --run-id <run-id> --node-id <node-id> --attempt-id <attempt-id>
```

The same operations are also exposed on `graph_runner.py` as `adopt-dispatch` and `reconcile-failure` subcommands; the legacy `graph_runner.py --run-id --dispatch` drive entrypoint is unchanged.

No scheduler service, model-driven polling, new router or provider configuration
is needed. Keep this process running in the tool/background terminal, without
repeated LLM status checks. The runner starts subprocess supervisors, schedules
all ready independent nodes up to `max_parallel`, collects receipts and advances
dependencies. Separate worktrees permit real overlap; conflicting live read/write
scopes and resource locks serialize. Estimate tokens never become a new cutoff.

## Dispatch map

```json
{
  "schema_version": 2,
  "run_id": "run-graph-...",
  "graph_sha256": "<sha256 of canonical active graph JSON>",
  "max_parallel": 3,
  "parallelism": {
    "policy": "estimate",
    "reason": "fresh conservative host-capacity preflight",
    "evidence": {"source": "approved planning contract"}
  },
  "nodes": {
    "node-backend": {
      "argv": ["C:/Python/python.exe", "-B", "C:/skills/agent-graph/scripts/command_adapter.py", "C:/task/backend-recipe.json"],
      "workspace_root": "C:/worktrees/backend",
      "pinned_files": {
        "C:/Python/python.exe": "<sha256>",
        "C:/skills/agent-graph/scripts/command_adapter.py": "<sha256>",
        "C:/task/backend-recipe.json": "<sha256>"
      }
    }
  }
}
```

Version 1 maps remain valid and `max_parallel` remains a hard upper bound. In a
version 2 map, `policy=hard` is also an upper bound; `policy=estimate` lets the
runner refresh logical CPU and available-memory capacity up or down, while the
technical ceiling remains 16. An estimate requires a reason and evidence. Token
or time estimates never change this process capacity.

For a new `parallel-planning-v1` contract, call the planner's
`dispatch_requirements(approved_contract)` and provide every outstanding
`required_dispatch_node_ids` entry before starting the schedule. A declaration
is intent, not an executable adapter: resolve and pin the real argv, workspace
and inputs as above. Manual declarations must not appear in the executable map.
Use `max_parallel=capacity_basis.value`, policy `hard` for `owner-hard-cap` or
`estimate` for `host-capacity-estimate`, the exact approved reason, and
`parallelism.evidence.planning_capacity_basis` equal to the full approved basis.
The scheduler verifies this binding before process launch. Missing planned
adapters are an explicit error. Legacy approved contracts retain their partial-map
WAITING behavior; they are not silently upgraded.

Pins include executable, adapter, recipe and immutable inputs. Prepare them from
the approved plan, never from Office messages or raw web content. This manifest
is the executable schedule, not another planning/approval ceremony. For a graph
amendment reconcile its prior processes first; never edit a running manifest.

## Existing Fleet adapter

`command_adapter.py` accepts a pinned JSON recipe: `argv` (the existing Fleet
run/quality-gate/security-gate command), `pinned_files`, `oracles` (criterion ID
to deterministic argv), `artifact_paths` (declared output files). Each criterion
needs its real business/test oracle; never insert `exit 0` to manufacture PASS.
Use `{worker_run_ids}` as one argv element to expand covered upstream Fleet IDs
for a gate. Fleet continues to choose models, refresh quota and validate its
independent gates. A provider's successful exit is not sufficient for acceptance.

Implementation must use the SAME pre-bound detached worktree on every attempt.
Prepare it once, then use `fleet_bound.py --fleet <current/fleet.py>
--base-commit <exact-SHA> -- run ... --mode implement --allow-write` as recipe argv.
This calls Fleet's normal CLI and existing workspace-reuse API, checks detached
HEAD/common repository/base ancestry, and avoids Fleet creating a different
worktree after Graph has already bound its workspace. Read-only Fleet/gate calls
use the normal fleet.py directly. Pin fleet_bound.py and the Fleet entrypoint too.

For an executor needing custom collection, its pinned adapter writes the output
file named by `AGENT_GRAPH_EVIDENCE_PATH`:

```json
{"attempt_id":"<AGENT_GRAPH_ATTEMPT_ID>","evidence":{"...":"full existing Graph evidence schema"}}
```

`AGENT_GRAPH_CONTEXT_PATH` contains current graph/node/upstream references and
dispatch bounds. `AGENT_GRAPH_ATTEMPT` enables already-authorized bounded repair.
Use certified Agent Loop through its own adapter; never pretend a generic command
has Loop certification. Missing adapters are visible WAITING nodes, not success.

## Recovery and limits

### Negative quota evidence (0.5.3)

`command_adapter.py` now serializes object-valued quota telemetry as canonical
JSON strings; scalar/null values and all other metrics remain unchanged.
For an older exact `EXITED/0` attempt affected by this schema mismatch:

```text
python agent_graph.py correct-negative-evidence --state-root <state-root> --run-id <run-id> --node-id <node-id> --attempt-id <32-hex-id> --evidence <corrected-negative.json>
```

The input is a bare Graph evidence object. Preserve every field except
`evidence_id`, `summary`, and object `quota_before`/`quota_after`, converted to
equivalent JSON strings. Quota key order/whitespace are not significant; duplicate
keys, nonfinite numbers, scalar substitutions and all outcome/metric changes are
refused. Original evidence must become valid by quota normalization alone.

The command checks the original start operation, attempt, job, context, plan,
workspace, finished receipt and byte hashes. It creates an immutable correction
intent and corrected evidence, calls public Graph result once, then settles the
journal. Repeating it after interruption resumes settlement without a second
attempt or charge. Normal `run` reconciliation consumes this correction before
its settled-attempt shortcut. Original files are never rewritten. Links/reparse
paths, tampering (including after settlement), foreign/later/ambiguous attempts
and unfinished children are refused. A Graph node still awaiting its result is
eligible when the child really has the finished rc0 receipt.

Output `NEGATIVE_EVIDENCE_CORRECTED` communicates successful bookkeeping, not
product acceptance: inspect `verdict`, which remains `rejected` or `blocked`.
This command grants no production or live-project mutation authority. File pins
detect drift; they do not protect against an administrator coherently replacing
the entire state, intent and evidence. Keep the existing runner single-writer
discipline and OS access controls. Never nest `graph.run_lock` around this call.

- Supervisors retain OS resource locks while the command runs, even if the
  scheduling parent disappears. They write a durable completion receipt.
- Supervisors also hold one shared kernel capacity lease under the state root for
  the real child lifetime. Stale slot metadata is observation only; the kernel
  lock is authoritative across graph runs.
- Runner output separates queue wait from execution time, reports measured
  execution overlap, and names observed dependency, capacity, resource/workspace,
  missing-adapter and transport waits. Quota and sessions outside Graph remain
  explicit visibility gaps until their owning adapter emits stable evidence.
- Restarting the same schedule records a finished receipt once. An uncertain
  active attempt returns RECONCILE_REQUIRED; it is not relaunched automatically.
- `EXITED` nonzero is still failure. If an independently stored canonical
  blocked/rejected Graph result binds that exact attempt, the journal records
  `failure-reconciled` without calling Graph result to manufacture acceptance.
- Exit 0 with no fresh attempt-bound valid evidence is NEEDS_ATTENTION, never PASS.
- Already-authorized `retry-same-scope` repairs run within existing attempt bounds.
  Business changes or invalidating upstream work still need a plan amendment.
- Human, production/deep-security approval and final acceptance are not automated.
  EXECUTION_COMPLETE means execution, not CLOSED or permission to deploy/push.
- Office events contain only run/node/status metadata, fail open on unavailable
  Office, and never grant another agent authority. Hub remains durable knowledge.
- This is not an OS sandbox. Command authorization, scope checks, side-effect
  isolation and independent quality/security gates remain the parent's contract.
