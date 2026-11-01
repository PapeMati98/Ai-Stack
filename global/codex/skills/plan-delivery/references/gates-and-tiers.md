# Gates and tiers

## Direct

Use only when every condition is true: narrow local scope, reversible work,
known context and LOW risk, explicit acceptance, and no hard-risk. Bounded
delegation, independent product review and disjoint parallel fixes are allowed;
none of them alone creates a planning contract. Duration or file count alone
does not select the tier. Broad scope,
unknown risk or an unresolved acceptance criterion cannot qualify as direct.
Inline checklist still includes goal, negative requirements,
acceptance and scope.

Classify the change, not the entire application. Confirm disjoint writes,
resources and prerequisite inputs before parallel execution; otherwise serialize
the affected slices. A real shared contract/cross-system dependency remains
coordinated work. Product regression tests and parent acceptance of delegated
work remain mandatory. An independent reviewer remains independent, without
creating a second review of the plan. Existing signed contracts and approvals
remain binding; this policy does not rewrite historical runs or waive gates.

## Compact

Use for one material execution stream with QA or MEDIUM risk and no hard-risk. It requires the
full ledger, fidelity gate and sufficiency gate. The CLI accepts one
`gate=combined` review for compact: one auditor call and one evidence file must
cover all clauses, requirements and tasks. `record-review` deterministically
projects that result into the legacy `fidelity` and `sufficiency` gate fields
and appends both compatible history rows. The combined auditor must be
independent from the planner; compact never carries hard-risk flags.

## Full

Use for coordinated work with shared contracts, cross-system work or any hard-risk.
The number of parallel workers alone does not require full. Fidelity and sufficiency
auditors must differ from planner; sufficiency lineage must also differ from the
fidelity lineage. Agent Fleet chooses current models. A weaker configuration is
BLOCKED. There is no agent-authored lineage waiver.

New contracts or revisions may explicitly set `planning.review_policy` to
`full-combined-v1`. This opt-in requires separate PASS projections for fidelity
and sufficiency, identical prompt/plan hashes, complete ledger coverage, and
the correct hard-risk projection. Shared evidence must be a hash-bound,
COMPLETED reviewer receipt whose terminal handoff proves both PASS results and
matches the auditor session, model and lineage. A qualified, hash-bound strong
planner witness remains required at approval and validation. Missing policy
preserves legacy behavior and never migrates historical reviews.

## Prepared Planning Review

For `parallel-planning-v1` prepared drafts, the existing fidelity/sufficiency
review checks caller-supplied edge reasons/inputs, owner-cap versus host-estimate
evidence, and complete dispatch/manual declarations. Reject placeholder prose;
the validator checks structure, not evidence truth. Interface-contract readiness
is distinct from implemented behavior: require the precise schema/API input or
completed artifact that the downstream task actually needs. A title never
authorizes deleting or reordering an edge. `report-parallelism` provides review
hints for missing/invalid reasons and transitive redundancy, not a new gate or
execution authority. Preserve legitimate data/interface and business gate edges.

## Hard-risk triggers

- production or public runtime;
- auth, roles, identity or authorization;
- secrets, credentials or restricted data;
- customer/personal data or destructive storage changes;
- public API/domain/integration contract;
- external messages, calendar, payments or remote mutations;
- irreversible decisions, material cost or conflicting instructions.

The deterministic validator scans the bound prompt for explicit trigger phrases
and rejects detected flags missing from `hard_risk_flags`. Fidelity review still
independently scans prompt and context, records the complete structured
`attested_hard_risk_flags` list and returns `repair` when any semantic trigger is
missing or the production boundary is understated.

## Repair

Fidelity, sufficiency, independent reviews and council runs debit only
`owner_ceiling.planning_and_review`. They never borrow from execution.

Fidelity and sufficiency share one pre-approval repair counter. Both reviews are
recorded once per round in append-only history. A compact combined review records
both projections atomically and reuses its single evidence path. At least one `repair` permits
`advance-repair`; `blocked` is terminal. After round 2 returns another repair,
the contract is BLOCKED. Do not soften negatives or acceptance to obtain PASS.

Amendments after approval have their own monotonic revision sequence. They keep
the prior review history byte-for-byte and require fresh planner/auditor sessions
and fresh evidence for the new revision; they never reset or restamp prior gates.

## Council and user decisions

Use Rada Modeli when a material conflict remains, multiple architecture choices
have comparable evidence and different blast radius, a public cross-system
contract changes, or two repairs make no progress.

Changed material intent, production boundary or independence policy requires a
new intake explicitly supplied by Mateusz and a new contract. Parent may choose
a reversible local assumption only when it does not alter business behavior,
security, authorization or scope.
