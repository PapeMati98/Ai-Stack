# Adaptive lane policy (shadow, policy v1)

The lane evaluator lives in `scripts/lane_policy.py` and answers one question
only: which governance lane does this work run in. It never grants authority,
never writes state, never calls a provider, and never replaces the task
assessment, the model-fit/quota router, the quality gate, the security gate,
the council, or Agent Loop scheduling. Graph owns structure; Fleet owns
routing; Loop owns scheduling; gates audit; the council resolves material
conflicts. During the shadow period the evaluator is inactive in the
installed runtime; decisions are recorded as shadow telemetry.

```powershell
python "$suite\agent-fleet\scripts\fleet.py" lane-policy --request-file request.json
python "$suite\agent-fleet\scripts\lane_policy.py" --request-file request.json
```

## Lanes

Fixed ordinals: `direct=0`, `compact=1`, `compact-recon=2`, `full=3`.

- `direct`: narrow local work with fresh continuity and no delegation gain.
- `compact`: bounded worker/review; requires parent-verified D0 evidence.
- `compact-recon`: exactly one separate, allowlisted, terminal read-only
  reconnaissance pass for missing continuity; evidence stays ephemeral.
- `full`: coordinated delivery, hard risk, durable continuity writes, or any
  fail-closed condition.

## Precedence (deterministic)

1. Model identity blocks first: the only public Qwen alias is `qwen3.8-max`.
   An unresolved alias or a silent substitution returns `BLOCKED` with
   `MODEL_ALIAS_UNRESOLVED` and resolves nothing. A dated provider snapshot
   (for example `qwen3.8-max-0902`) is never publicly requestable; it may
   appear in `resolved_model` only after provider/runtime verification, as
   provenance (`DATED_SNAPSHOT_PROVENANCE_ONLY`). Caller-supplied booleans
   are not verification: a trusted provider verifier callback must attest the
   served identity before either the canonical alias or a dated snapshot is
   accepted.
2. The baseline kill switch forces `full` (`KILL_SWITCH_BASELINE`).
3. Hard risk wins before continuity and scope: production or external
   effects, secrets, restricted data, security or role changes, persistent
   storage or migration work, material decisions, and required independent
   review each force `full`. A `PRODUCTION` environment forces `full`; an
   undeclared external effect fails closed (`HARD_RISK_MISDECLARED`).
4. Stale continuity signals, provenance drift, and gate regressions fail
   closed to `full` (`STALE_CONTINUITY_FAIL_CLOSED`,
   `PROVENANCE_DRIFT_FAIL_CLOSED`, `GATE_REGRESSION_FAIL_CLOSED`).
5. Durable continuity writes resolve to `full`
   (`DURABLE_CONTINUITY_WRITE_RESOLVES_FULL`).
6. `MISSING`, `STALE`, or `UNKNOWN` continuity without hard risk requires
   exactly one `compact-recon` pass (`LOCAL_CONTEXT_RECON_REQUIRED`): read
   only the frozen allowlist, keep evidence ephemeral, bind the result to a
   parent-issued SHA-256 receipt and safe revision identifier, never run it as
   `direct` or `compact`, never write durable continuity, and recompute the
   lane afterwards. A second pass, an allowlist violation, durable evidence,
   unresolved continuity, or an unverifiable receipt all fail closed to
   `full`. A trusted receipt verifier callback is required; record fields and
   `passes_used=1` alone are not proof.
7. Coordinated scope forces `full` (`COORDINATED_DELIVERY`). A delegation
   benefit resolves to `compact` only with parent-verified D0 evidence
   carrying path, SHA-256, and run identifier; missing or unverifiable
   evidence resolves to `full` with `D0_MISSING`. A trusted file verifier
   callback must attest the evidence; `parent_verified=true` alone is never
   sufficient. Otherwise `direct`.
8. `requested_lane` may escalate only. De-escalation is rejected
   (`REQUESTED_LANE_DEESCALATION_REJECTED`) and never applied. Escalation
   cannot bypass the recon or D0 gates. There is no `lane_exception`, no
   waiver, and no other bypass; such keys are refused structurally.
9. Per-lane kill switches (`AGENT_FLEET_LANE_KILL_DIRECT`,
   `AGENT_FLEET_LANE_KILL_COMPACT`, `AGENT_FLEET_LANE_KILL_COMPACT_RECON`,
   or the request's `kill_switches.lanes`) force the killed lane to `full`
   last (`KILL_SWITCH_LANE`).

## Request contract (policy_version 1, exact key set)

`requested_lane`, `requested_model`, `environment` (DEV/PRODUCTION),
`external_effects`, `hard_risk` (seven booleans), `coordinated_scope`,
`delegation_benefit`, `continuity_state` (FRESH/MISSING/STALE/UNKNOWN),
`continuity_durable_write`, `d0_evidence` (path, sha256, run_id,
parent_verified) or null, `recon` (passes_used, lane_used, reads,
evidence_persistence, continuity_state_after, receipt_sha256, revision) or
null, `signals`
(stale_continuity, provenance_drift, gate_regression), `kill_switches` or
null, `provider_verification` (verified, served_model) or null.

## Output and telemetry hygiene

The request is bounded before evaluation: the CLI accepts at most 256 KiB,
with bounded nesting, strings, collections and object keys. Refusal output
uses fixed error codes and never echoes caller-controlled field names, values
or paths. The decision exposes `requested_lane`/`resolved_lane`,
`requested_model`/`resolved_model`, stable `reason_codes`, the recon
directive when applicable, and shadow telemetry marked
`runtime_activation=false`. Prompt contents, secrets, quota claims, and
synthetic (`fallback`) assessments are refused as route input and can never
enter telemetry; a blocked alias is echoed only as a SHA-256 fingerprint
token. Node acceptance (including a `compact` worker) never equals system
acceptance: the projection stays `PENDING` until the parent's independent
gates close. Retest evidence is valid only when the exact quality, security,
tests, parity and rollback gate set is present once, all events are `PASS`,
all events match the expected revision, and a trusted parent verifier accepts
each receipt. Any gate `FAIL` or `BLOCKED` invalidates prior `PASS` evidence
and requires a full retest of the current revision.

The CLI exits 0 for a structured decision (including `BLOCKED`, mirroring
Fleet's completed-verdict convention) and 2 for a structural refusal.
