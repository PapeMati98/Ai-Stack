# Delegation policy

## Rule for M/D parents

A D or M parent plans once. For every well-scoped, eligible piece of work that
can be assigned safely, it uses a qualified K or W model through an actual
native CLI run. The parent keeps integration, acceptance, and work that really
requires its credentials, owner-level model, irreversible judgment, or
resolution of hard uncertainty. If a task has separable eligible work, that
work remains delegable even when the parent retains a difficult central
decision.

Do not add another planner call, ask for a council, or require a fixed number
of child agents. Zero children is valid when no eligible subtask exists or a
recorded exception covers the work. A worker node owns its assigned leaf and
does not recursively delegate it.

## Harness coverage

The rule is harness-agnostic: it binds an M/D parent in any packet harness,
including Antigravity and ZCode. What proves delegation is the child's verified
native CLI receipt, never the parent's harness, a profile path, or an agent
name. Coordinate with harnesses that share no CLI channel through Office and
the shared metadata policy only. Never physically prompt a ZCode Desktop
session (the owner alone prompts ZCode), and never present a Desktop prompt or
a synthetic scheduler run as delegation evidence.

## Evidence and assessment

The public packet uses `delegation` with exactly these fields:

```json
{
  "parent_tier": "D|M|K|W|unknown",
  "eligible": true,
  "exception": "none|trivial|credentials|irreversible|capability-unavailable|quota-exhausted|explicit-owner-model|worker-node",
  "planned_tasks": 1
}
```

`eligible` and `planned_tasks` may be `null` when unknown. The helper
`scripts/delegation_policy.py` exposes
`assess_delegation(packet) -> DelegationAssessment`. It independently checks
the exact types and values of the packet members it reads: `delegation`,
`metrics`, and `runs`. The packet producer still validates the complete packet
with the core packet validator before enqueue.

Assessment states:

- `delegated`: parent tier is D/M, eligible is true, exception is `none`,
  evidence is complete, and at least one K/W `maker` run has a verified native
  receipt with status `completed`.
- `underused`: the same conditions hold, but no K/W child maker was attempted.
  This can produce `missing-delegation` only when evidence
  coverage is explicitly complete.
- `exception`: a typed exception is present. The reason remains visible; it is
  not counted as delegation or underuse.
- `unknown`: tier, eligibility, or evidence is unknown or incomplete, or a
  child maker was attempted without a verified completion. A failed or
  unverified child is not successful delegation and is not proof of underuse.
- `not-applicable`: the parent is not D/M, or `eligible` is false.

`planned_tasks` is diagnostic only. Its value, including zero or null, does not
prove or disprove execution. `evidence_complete=true` means the packet has
coverage for deciding eligibility, applicable exceptions (including known
quota/auth availability), and child run receipts. If that coverage is absent,
use false or null so the result stays unknown. A confirmed exhausted quota is
`quota-exhausted`; missing quota data is unknown. Authentication required by a
subtask is `credentials`; unavailable child capability is
`capability-unavailable`; owner-only judgment is `explicit-owner-model`.

The child evidence comes from a `runs` receipt with its actual `run_id`,
`actor_id`, `model_id`, `provider`, tier, role, status, and verified flag.
Count only `role=maker`, `tier=K|W`, `status=completed`, `verified=true`.
Declarations, profile paths, model suggestions, a parent's own run, and
unverified or failed runs do not establish successful delegated execution.
Review independence is checked separately: the reviewer's provider must differ
from the maker's provider; a CLI-name difference is insufficient.

The helper raises `DelegationValidationError` on malformed values or unknown
fields inside the structures it reads. It has no import-time dependency on
`audit_core` and never treats free prose as policy evidence.
