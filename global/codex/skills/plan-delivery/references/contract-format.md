> HISTORYCZNE WYLACZNIE: ten dokument obsluguje przypiete dawne runy/kontrakty i ich recovery. Dla NOWEJ pracy obowiazuje TODO z `../SKILL.md` i globalnego AGENTS.md; nie uruchamiaj opisanych tu fidelity/sufficiency, witness, pieczeci ani bramek planu. Zachowaj historyczne dowody i rzeczywiste granice zgody.

# Contract format V1

The validator in `scripts/plan_contract.py` is normative. This reference explains
the fields an agent must author; it is not a second schema.

## Identity and prompt

- `schema_version`: `1`.
- `contract_id`: stable `plan-*` identifier.
- `revision`: starts at 1 and only increases.
- `previous_contract_hash`: null for revision 1; exact prior approved hash later.
- `status`: `draft`, `approved`, `amended`, `blocked` or `closed`.
- `prompt.source`: relative path to the redacted intake file.
- `prompt.summary`: bounded statement of user intent.
- `prompt.sha256`: hash from `hash-prompt`.
- `prompt.context_sources`: redacted attachment/context references with SHA-256.

Prompt canonicalization is UTF-8, Unicode NFC, CRLF/CR to LF and exactly one
final newline. Prompt hash detects changed input; it does not establish the
authority or continued applicability of a quoted instruction. Follow
[fresh-intake.md](fresh-intake.md): keep the literal current request separate
from historical context and planner interpretation. The existing fidelity
review checks both coverage and whether inherited limits/permissions still
apply. These intake sections do not add fields to the JSON schema.

## Classification

`classification` contains `tier`, `hard_risk_flags`, `data_classification` and
`production_boundary`. A persistent contract supports `compact` and `full`.
Direct work remains an inline checklist and never enters Agent Graph.

The mechanical validator scans the bound prompt for conservative, explicit
hard-risk phrases and rejects undeclared matches. The fidelity gate separately
records `attested_hard_risk_flags`; fidelity PASS requires that attestation to
equal the complete declared list. Semantic risks outside the bounded detector
remain the independent fidelity auditor's responsibility.

## Clause and requirement ledgers

Before requirements, extract an ordered `clauses` ledger. Every clause has a
stable `CLAUSE-###`, kind, exact bounded text, source reference, material flag
and reciprocal requirement refs. Material clauses cannot be uncovered. The
fidelity auditor must review the complete ordered clause list.

Each requirement has:

- stable `requirement_id` (`REQ-001`, never renumbered);
- `kind`: goal, constraint, negative, conditional, deferred, owner_decision,
  acceptance, output, reporting, risk, out_of_scope, question or decision;
- precise `text` and `source_ref` into intake;
- reciprocal `clause_refs`, `material`, `task_refs` and lifecycle `status`.

Every active material requirement must reference at least one task before
sufficiency PASS. This release has no automated waiver path: changed material
owner intent creates a new prompt contract.

Every item in `decisions` cites one matching clause by `evidence_ref`. Parent or
Mateusz decisions require an `owner_decision` clause; a council decision may cite
an `owner_decision` or `derived` clause. Text must match exactly.

## Tasks

Every task has:

- stable `task_id` and `graph_node_id`;
- `kind`, `title`, requirement refs, dependencies, evidence dependencies, wave;
- Agent Graph `capability`, executor primitive and workspace mode;
- maker and reviewer provider lineages for Fleet-backed mutations, which must
  differ;
- independent reviewer identity/capability;
- normalized relative read/write scopes and resource locks;
- acceptance criteria with graph-unique `crit-*` IDs and evidence kinds;
- bounded budget, repair policy, Loop policy and Council trigger;
- lifecycle status.

Tasks must form an acyclic graph. Unordered mutating tasks cannot overlap write
scopes or resource locks. The compiler rejects both cases.

Every repository-mutating plan includes one material repository-closeout task
after integration. Its acceptance criteria bind evidence from the target branch
and target worktree, run the project's generic validator, require
`bootstrap.ps1 -ValidateSkills` when shared skills, ownership, or release roots
changed, and capture `git status --short`. A dirty tree is acceptable only as a
complete path ledger with an owner and reason for every remainder; it cannot be
reported as a clean or fully closed delivery. Evidence from a contractor's
isolated worktree does not satisfy this task.

## Gates

`gates.fidelity` and `gates.sufficiency` contain round, status, auditor identity,
reviewed prompt/plan hashes, exact reviewed clause/requirement/task IDs,
`attested_hard_risk_flags` and an immutable evidence path+SHA-256. Fidelity
attests the declared hard-risk set; sufficiency attests an empty set. Gate PASS
is valid only for the exact planning payload hash in the approved snapshot.

`planning.repair_round` is global across both gates. It is not the amendment
sequence. `planning.review_history` is append-only and keyed by revision, round
and gate. A later round is legal only after two reviews and at least one
`repair`; `blocked` is terminal. An amendment preserves all historical rows and
uses fresh planner/auditor sessions plus fresh evidence files. Planner
identity/session/lineage is recorded without chain-of-thought.

## Approval and graph

Approval is generated by `approve`, not hand-written. The command validates both
gates, independent lineages, prompt hash, task coverage and the deterministic
graph hash before writing a new file with exclusive-create semantics. Revision
greater than one additionally requires `--base-contract`; approval validates
the complete append-only amendment against that exact approved snapshot.

The graph section declares project root, graph ID, integration task, Agent Graph
V1 global budget, previous projection hash and current projection hash.
`previous_projection_sha256` is null in revision 1 and must equal the base
projection for an amendment. `compile-graph` regenerates ordinary Agent Graph v1.
For a new unreviewed revision-1 plan, `prepare-draft` stamps explicit `estimate`
defaults for global token/time and node token/tool policies BEFORE review.
It rejects reviewed drafts; `approve` preserves the reviewed payload and never
rewrites reviewer hashes. Amendments preserve the approved fields; an old
approved contract with missing policy fields keeps the runtime `hard` defaults.

## Owner ceiling

`owner_ceiling` is a historical field name for the planning/execution token
partition. New plans use agent-authored forecasts (`token_policy=estimate`)
unless an applicable explicit owner limit requires `hard`. A numeric field or
an old signed contract is not by itself evidence of a new owner declaration.
The partition lives on the planning contract, not as a hardcoded constant.

- `planning_and_review`: fidelity/sufficiency gates, independent reviews, Model
  Council, and contract audits. These Fleet runs happen before or beside the
  graph and never debit `graph.global_budget`.
- `execution`: approved graph node runs. It must equal
  `graph.global_budget.max_total_tokens` (an estimate unless explicitly hard).
- Pools remain separate telemetry. Only an explicit hard ceiling blocks the
  corresponding pool; an estimate overrun is a warning, never a new GO request.
- `unknown_usage_policy` preserves unknown telemetry. A run whose adapter
  reports `token_usage.status=unknown` is recorded as unknown, without a
  fabricated `max_tokens` charge or cost. The legacy
  `pessimistic_max_tokens` label remains accepted for compatibility; silent zero
  is invalid.
- Routing: planning uses quality (best capability fit). Execution uses cheapest
  profile still above `capability_floor` sourced from
  `profiles.capabilities`, plus `efficiency_priors.first_pass` and
  `evidence_confidence`. The floor exists so a zero-cost local model cannot win
  by price after returning unusable structured output.
- `quota_snapshot_max_age_hours` marks provider quota snapshots stale. Stale or
  unknown quota is not a neutral routing signal.

Both values are required integers; the planner can estimate them without asking
the owner to approve a token allowance. For `hard`, cite the applicable owner
instruction and its scope in the intake; for a continuation, preserve the old
limit and cumulative usage rather than restarting the allowance. Do not clone
an approved JSON into a new revision-1 draft and retain its policies by accident.
`prepare-draft` fills missing policies but intentionally preserves explicit
`hard`; semantic applicability is checked in the existing fidelity review.
`reconcile-budget` emits BUDGET-RECONCILIATION JSON/Markdown from run records.

## Prepared Planning Metadata

`prepare-draft` opts a new unreviewed revision-1 draft into
`planning.preparation_policy: "parallel-planning-v1"`. With this policy,
validation (including approval) requires all of the following caller-authored
metadata. It supplies no placeholder reasons, adapters or capacity defaults.
Existing legacy drafts/approved contracts without this policy remain readable;
their payload hashes and review history are unchanged.

- Each task has `dependency_justifications`, an object keyed by exactly its
  `depends_on` IDs (including `{}` for roots). Each entry contains `kind`
  (`data/interface`, `write-conflict`, or `gate`), a concrete `reason` and
  concrete consumed `input`. Reasons require at least 8 trimmed characters;
  inputs at least 3. Structural validation cannot prove the truth of prose;
  existing fidelity/sufficiency review must reject placeholders and unsupported
  dependencies. Dependency order and business edges are never inferred from titles.
- `graph.capacity_basis` contains `kind` (`owner-hard-cap` or
  `host-capacity-estimate`), `value` (integer 1..16), `reason` (8+ trimmed
  characters) and `evidence` (3+ trimmed characters). Evidence cites the
  applicable owner instruction or a host observation, as appropriate. The
  planner supplies the value; no provider-count heuristic or cap2 default is
  used. The word "provider" in legitimate owner evidence is permitted.
- `planning.adapter_declarations` covers exactly the pending task IDs.
  Dispatch entries contain `mode: "dispatch"` and a nonempty `adapter` name.
  Eligible `parent-local` tasks may dispatch. Human, production-approval,
  deep-security-approval and final-acceptance tasks require `{ "mode": "manual" }`.
  A technical manual override requires `mode: "manual"` plus a concrete
  `reason` (8+ trimmed characters); omission never silently selects manual mode.

The preparation policy and adapter declarations are conditionally included in
the review payload when present. Task dependency/interface metadata and graph
capacity are already covered by the task/graph payload. Any edit requires new
review bindings; no historical review is restamped. Absence of new metadata
does not add fields to a legacy hash or its compiled Graph projection.

Optional task `interface_contract` contains `status` (`not-applicable`,
`declared`, `ready`, `implemented`) and `evidence`. It is a planning declaration,
not an execution status. `ready` describes the interface contract only;
completed implementation requires its own artifact and acceptance evidence.

`report-parallelism --contract <draft.json>` checks a bounded DAG (1..128 tasks,
at most 32 direct dependencies each), reports missing/invalid justifications
and redundant transitive edges as review hints, and leaves the DAG unchanged.
It can inspect invalid justification metadata; it does not certify readiness.
Valid data/interface, gate and write-conflict edges receive no missing-reason
warning. A redundant edge may still express business intent and must not be
automatically removed.

`prepare-dispatch --contract <contract.json> --adapters <declarations.json>
--output <report.json>` validates the contract and adapter declarations. When
declarations are bound in the plan, the supplied file must match them exactly.
The output is a non-executable report, not an Agent Graph dispatch map; existing
v1 runtime maps and their limits are untouched. Graph owns executable/input
pin verification, capacity enforcement, scheduling and authority gates.

Integration helper: `plan_contract.dispatch_requirements(approved_contract)`
first validates the approved contract (including review/witness and projection
bindings), then returns:

- `contract_hash` and `graph_sha256` for integration binding;
- `required_dispatch_node_ids`, in contract task order;
- `dispatch_declarations` and `manual_declarations`, keyed by Graph node ID;
- `capacity_basis`, copied exactly from the plan;
- `undeclared_node_ids` and `status: LEGACY_UNDECLARED` for missing legacy
  intent/capacity, rather than invented declarations or defaults;
- `executable: false`.

The helper returns independent copies and changes neither the contract nor the
Graph projection. Adapter strings declare intent only. Parent integration must
check coverage even when Graph `validate_dispatch` accepts a partial map, bind
the contract/graph hashes, and verify the actual executable map through Graph
`validate_dispatch` (argv, workspace and pins). Graph schema v2 carries capacity
via `max_parallel` and `parallelism: {policy, reason, evidence: {...}}`; integration
maps `owner-hard-cap` to `hard` and `host-capacity-estimate` to `estimate`, retaining
the declared value and binding structured evidence. This helper does not emit
that runtime map or modify frozen runs. Legacy undeclared intent requires explicit
integration handling; an empty required-node list is not proof of coverage.

## Amendment

`planning.review_policy` is optional for backward compatibility. The only
supported value is `full-combined-v1`; it is bound to the new review hashes and
cannot be added while preserving legacy review history.

Revision >1 adds `amendment`: reason, affected task IDs, parent authority and
approval reference. Prompt, classification, clause/decision ledgers, questions,
assumptions, exclusions, requirements, project binding and global budget are
immutable. Every affected baseline task changes only from `pending` to `blocked`.
Replacements append new task/node IDs; no task is rewritten in place. An
amendment that appends tasks should also declare the optional
`introduced_task_ids`; the validator requires it to equal the exact
candidate-minus-base task diff, so appended work is machine-auditable rather
than hidden in prose. Historical amendments may omit this field and remain
readable. New owner requirements require a new prompt contract, not an
amendment.

## Closeout receipt

The closeout input binds contract ID/hash and gives one final record per material
requirement. Every cited criterion ID has an explicit evidence item carrying the
same criterion ID, relative path and content hash. `satisfied` needs criteria and
evidence; `blocked` needs criteria, evidence and a blocker. Agent Graph requires
the resulting `COMPLETE` receipt before it can close a contracted run. The
receipt must stay beside its evidence files. Graph revalidates requirement
coverage, linked criteria and every evidence SHA-256 from the receipt location;
a self-consistent receipt hash alone is insufficient.
