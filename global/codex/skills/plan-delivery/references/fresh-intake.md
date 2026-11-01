> HISTORYCZNE WYLACZNIE: ten dokument obsluguje przypiete dawne runy/kontrakty i ich recovery. Dla NOWEJ pracy obowiazuje TODO z `../SKILL.md` i globalnego AGENTS.md; nie uruchamiaj opisanych tu fidelity/sufficiency, witness, pieczeci ani bramek planu. Zachowaj historyczne dowody i rzeczywiste granice zgody.

# Fresh intake and applicable authority

Use this procedure inside the existing intake and fidelity review. It is not a
new approval gate, schema, receipt, or reason to replan unaffected work.

## Choose the operation

| Operation | Preserve | Budget and authority |
| --- | --- | --- |
| New goal | Relevant requirements, current product state, reusable evidence, historical costs | Estimate unless an explicit owner limit applies to this new scope. Prior permissions are context, not a fresh grant. |
| Continue | Approved contract, unfinished scope, decisions, cumulative usage | Preserve existing hard limits and permissions within their original scope. A new session or date does not reset them. |
| Amendment | Immutable base plus supported append-only changes | Preserve the approved budget/policies and production boundary. Follow the existing amendment validator. |

Derive the operation from the user's intent, not the agent's choice of folder,
contract ID, or Goal label. A stop/resume or technical recovery is normally a
continuation. Explicit instructions for a new goal without inherited estimates
must not be replaced by an old budget grant. If a genuine hard limit's scope is
unclear and affects execution, ask one focused question; do not invent authority
or silently drop a binding restriction. Technical estimates need no such consent.

## Sections in the existing intake

1. **Current request:** literal user text, source turn/date if available, and
   material clarifications. Redact secrets; do not rewrite this as a planner
   summary. Missing original text means unverified provenance, not permission
   to fabricate a quote.
2. **Historical context:** links/hashes of old plans and handoffs, relevant
   unfinished requirements, accepted evidence, costs already incurred, and
   original scope of any old grant. Clearly label old instructions as history.
3. **Planner interpretation:** operation, requirement delta, applicable limits
   and permissions. For each `hard`, cite the exact owner instruction and
   explain why its scope still applies. If none applies to a new goal, state
   `token_policy=estimate`, `time_policy=estimate`; figures are forecasts.

Do not use an old intake or approved JSON as the new authority source. Reuse
verified tasks/evidence selectively. Keep prior contracts and accounting intact;
historical expenditure remains visible even when it is outside a new scope.
Recovery of a draft does not require redesigning its whole architecture.

## Existing fidelity review checklist

Give the reviewer the literal current request and relevant clarifications as
well as the candidate intake/contract. A reconstructed summary alone is not
sufficient. In the same review, check:

- Current instructions, exclusions and deferred work are represented correctly.
- Historical quotes are not presented as newly issued permissions or limits.
- Every hard policy has applicable authority; every estimate is labeled as such.
- Continuation has not reset costs/limits; a genuine new goal has not inherited
  an unrelated old allowance. Production approvals remain separately scoped.

If these checks fail, repair the affected intake/draft in the existing workflow.
Do not commission another reviewer just to review this checklist. Hashes and
`prepare-draft` cannot prove the temporal applicability of user consent; this
procedure is a semantic review obligation, not an automated authorization check.

## Regression scenarios

- Old tranche: 50M hard. User explicitly starts a separate new goal without a
  token allowance. Expected: estimate; old 50M and spend remain history only.
- Old tranche: 50M hard. User says "continue" or changes session. Expected:
  preserve hard and all prior spending, not a fresh 50M allowance.
- New goal explicitly limited by the user to 10M. Expected: hard with the exact
  current quote and scope; do not relabel it as an estimate.
- Current request contradicts an inherited constraint. Expected: apply the
  newer instruction within its authority, preserve the old snapshot, and use
  the existing new-contract path for changed intent rather than editing approval.
- Missing telemetry. Expected: unknown, neither zero nor a fabricated max-token
  debit. A forecast overrun is not a failed product test or a request for more GO.
