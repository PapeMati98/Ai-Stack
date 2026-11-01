# Delegation activation, not another router

Start with a brief inline decision. No 10/15-minute cliff. A large coordinated
delivery needs a plan; a long but narrow reversible local operation need not
become a multi-agent project. Never classify an unknown or protected operation
as direct merely to save time.

Delegate when specialist quality, independence, or net delivery time justifies it.
Compare time through acceptance, including integration and expected rework.
Subscription headroom selects among useful contractors, not unnecessary tasks.
Reuse the current approved work wave; do not repeat whole-stack intake per file.

Optional calculator (no profiles, provider requests, state or receipts written):

```powershell
python "$suite\agent-fleet\scripts\fleet.py" delegation-policy --scope-file scope.json
```

```json
{
  "scope": "bounded",
  "risk": "low",
  "context_ready": true,
  "protected_change": false,
  "independent_review": false,
  "specialist_gain": false,
  "parallel_slices": 0,
  "economics": {"local_minutes": 120, "delegated_minutes": 115, "coordination_minutes": 20}
}
```

`scope` is bounded/coordinated; `risk` low/medium/high/critical/unknown.
`protected_change` includes production, auth/roles, secrets, customer/restricted
data, destructive storage, public API contracts, external effects, irreversible
decisions or material cost. A declared false value is not proof these are absent.
`economics` can be null if genuinely unknown; never invent savings. A required
independent reviewer or a demonstrable specialist advantage can justify delegation
without a time advantage. Otherwise unknown gain means SKIP, not a fabricated score.

Lanes: direct = narrow local work; compact = bounded worker/review; full =
coordinated work, useful parallel scopes or protected/high risk. Unknown context
returns compact-recon/DEFER (or full/DEFER for known hard risk); resolve locally
before dispatch. Full/SKIP is valid: no external worker advantage does not remove
planning, native independent review or safety duties.

`parallel_recommended` does not grant write access or create workers. `ONE` is a
baseline recommendation; choose COUNCIL or PARALLEL_IMPLEMENT only after establishing
appropriate scopes and mode. Returned value 0/50 is only legacy assessment-compatible
encoding, not a benchmark or model score. Do not create an assessment for local SKIP.
If routing is chosen, produce the existing validated assessment with concrete fit
reasons. Existing legacy direct CLI calls remain supported, never auto-approval.

A SKIPPED command exits 0 because no dispatch was requested; it has no accepted
artifact or worker run. `--force-delegation` is an explicit parent reconsideration,
not a way around provider, permission, quota, scope or independent acceptance gates.
