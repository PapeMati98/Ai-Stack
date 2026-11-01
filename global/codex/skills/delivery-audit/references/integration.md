# Integration contract

## Asynchronous metadata hook for the Hub owner

The canonical Hub closeout owner calls one metadata-only hook after its
existing `record-session` closeout has the final outcome. This is a contract
for the Hub owner; this skill does not edit Hub, Office, Fleet, or RSI code.
The Hub owner installed and independently accepted one converged wrapper
for fresh and retried final `record-session` receipts. The 2026-10-14 Windows
canary verified the existing public closeout -> native audit -> Office path.
The original closeout body and receipt remain unchanged; failures are fail-open.

```python
# ROOT uses existing internal args; no new public gateway flag.
producer.after_hub_closeout(final_receipt, args.agent, args.session_id,
                           args.project_slug, checkpoint_id=args.checkpoint_id)
```

Graph/Pair production terminal paths emit one classified whole-TODO sidecar
under `~/.agents/telemetry/todo/task-closeouts/<binding-sha>/<plan-sha>.json`.
The existing plan contains `completion` with `task_kind`, `task_family`,
`episode_role`, `eligible`, `exception`, plus optional `uat_required`.
`task_family` is exactly one of `implementation`, `research`, `operations`,
`skill`, `other`; a custom family label is unclassified and does not emit.
Classification stays in the existing TODO, without another agent action.
Reuse the canonical parent's context receipt and native session/project binding;
no new ordinary-agent closeout command, model call or transcript scan.
The helper resolves only that bounded binding directory, checks current native
Fleet receipts against opaque workspace/output-directory hashes, ignores
incoming verification claims, and enqueues only after final COMPLETE Hub
`record-session` outcome COMPLETED. Packet session ID remains native.

Coverage is explicit: absent/unclassified plans, ambiguous multiple completions,
worker/audit/status episodes, nonfinal receipts and blocked tasks skip.
`record-handoff` alone has no typed session outcome and is not inferred as
successful delivery. The activated path covers classified whole-TODO
completions closed by final COMPLETE `record-session`; it does not infer a
completed delivery from a handoff, free text, or an absent sidecar.

Enqueue is asynchronous and idempotent by canonical packet/audit ID. Retry of
one checkpoint reuses its frozen event; uncertain enqueue does not claim audit,
Office or RSI success. Product closeout stays complete on audit failure.
Office uses the existing allowlisted `agent-graph` service harness with per-audit
job identity; channel audit-realizacji is the output route, not a new harness/IAM.

## Packet boundary

Packet v1 has exactly these top-level keys: `schema_version`, `task_id`,
`session_id`, `project_slug`, `harness`, `task_kind`, `task_family`, `outcome`,
`workspace_id`, `requirements`, `runs`, `metrics`, `delegation`,
`observations`, and `evidence_refs`. IDs are bounded safe ASCII (maximum 120
characters); `workspace_id` is a SHA-256 digest, never a raw path. Evidence
references are opaque IDs only. Reject unknown fields, invalid enums, unsafe
paths, links, reparse points, UNC paths, oversized input (64 KiB or 256
records), and excess nesting (depth 12).

Never include prompts, source text or code, command strings, raw stderr,
exception text, email, URLs, credentials, tokens, restricted customer details,
or raw paths. `null` means unknown, including absent UAT requirement; do not convert missing telemetry to zero.
Model output is untrusted: reports and dispatches contain only validated
finding enums, counts, run IDs, and grounded evidence references.

The producer sets `runs[].verified` only after checking the real native
receipt's run, actor, model, provider, role and terminal state. A model's
declaration or a boolean in an arbitrary incoming JSON packet is not native
proof. Unavailable or ambiguous source receipts remain unverified, and missing
telemetry cannot justify `evidence_complete: true`. This verification belongs
inside the existing closeout producer, without another manual agent step.
The pure metadata validator checks shape; it does not authenticate the source
of task facts. The runner separately verifies actual audit-model receipts.

## Fixed native CLI run

The runner invokes the installed, pinned Graph once per accepted audit input:

These are legacy runtime pins, not proof of a working installation from this
source edition. Resolve `%USERPROFILE%` for the executing user. Source export
does not activate or silently repin this runtime; installation and a real canary
must verify the installed bytes before claiming readiness.

- Graph: `%USERPROFILE%\.agents\skills\agent-graph\scripts\todo_graph.py`
- Expected Graph SHA-256: `082162b568d9605d75203389d2de7a813b381a823950792abb3d14a750d061b8`; version `1.0.1`, with the required inspect, interrupted-resolution and historical-scout capabilities.
- Fleet installation: `%USERPROFILE%\.agents\runtime\agent-fleet-suite\lean-0.9.82`
- Expected Fleet fingerprint: `696d5bb1f7719719de02739c328ac81d023654e7eb007dfc5c247dfb69e85d1b`
- Background launcher: `%USERPROFILE%\.agents\runtime\hidden-launch\1.0.3\HiddenLaunch.exe`
- Expected launcher SHA-256: `62fef0d9763aa674d167d508f68d49f8d69277f22ae1443baef6ff361dbebf48`

The fixed graph is history scout -> execution audit -> recurrence audit. The
scout uses the installed deterministic economic gate. Each audit node has one
native maker and one independent reviewer; review independence means a
different provider. Generate the TODO from verified installed profiles,
include non-empty checks, and use scope/evidence metadata only. Do not run a
second scheduler per node. Resume accepted graph nodes after a failure instead
of repeating them.

Completed audits retain their exact plan, controller source hash, native
receipt hashes and runtime pins. Read-only history verification uses those
frozen dependencies, so a catalog, Python, skill path or Fleet upgrade does
not silently erase accepted observations. Historical verification never
executes an old model or runtime. New work must pass the current Graph,
Fleet and launcher pins; a mismatch has an explicit diagnostic.

Enqueue launches in the background with no visible console. The pinned
HiddenLaunch 1.0.3 is started through the existing Windows WMI service;
the launcher starts the console action on its private background desktop.
No dedicated launcher service is installed. `CREATE_NO_WINDOW` alone is not the
detachment mechanism and is not claimed as one. Keep the runner's
state and outbox in its configured absolute state directory, outside the
project checkout. No new provider credentials or paid API are used. A quota
wait or failure stops only the audit; it never holds the completed product.

The launched worker enables the existing inspect/resolve recovery. Failed
jobs keep bounded retry eligibility while later queued deliveries progress;
there is no busy loop or new retry daemon. A launch intent without enough
evidence remains explicitly blocked and is never blindly re-dispatched.
STOP still prevents all new model work.

## Finding and RSI routing

The execution report distinguishes task-specific inefficiency from evidence
gaps. The recurrence report preserves denominators and unknown values. A
systemic pattern requires the same stable code/component across at least 3
different task IDs and 2 workspace IDs, plus at least 2 independently verified
actor/provider pairs. Exclude quota resets and transient provider failures.
An accepted audit report is still required; a model assertion alone is not
enough.

Only a newly confirmed systemic stack finding is sent to the existing RSI
Helpdesk. Deterministically map validated finding/component enums and opaque
evidence IDs to the Helpdesk interface; use code-owned static text if that
interface requires symptom or impact strings. Never forward model prose.
Deduplicate by audit/pattern ID. Historical findings alone do not reopen a
resolved ticket. Real Helpdesk dispatch is not part of candidate tests.

## What each kind of verification proves

- **Candidate tests** run against this checkout. Unit tests, schema checks,
  synthetic scheduler runs, `--no-launch`, and `--dry-run` prove candidate
  behavior only; they do not prove an installed graph or native model run.
- **Installed integration** verifies the pinned installed Graph, Fleet
  fingerprint, hidden launcher, and hook against the configured runtime. A
  local candidate test cannot stand in for this check.
- **Native execution** requires actual per-node CLI receipts with model,
  provider, role, run ID, status, and verification, plus the independent
  reviewer's provider difference. An enqueue receipt or mock is not native
  execution proof.

The parent integrates the candidate and records the actual verification class.
No test in this candidate should launch a real audit, post to Office, file an
RSI issue, use production data, or start OpenClaw. A prior OpenClaw launch
without model output is not evidence to diagnose or repeat from this slice.

## Existing process review: trigger and cost

Before this change, closeout had no automatic realization audit and M/D
delegation could remain a discretionary declaration without a child receipt.
After this change, each completed top-level delivery/repair gets one
non-blocking metadata enqueue. The fixed graph nominally adds one deterministic
scout and four native audit CLI runs (maker plus independent reviewer for each
of two audit nodes), all in the background; no dollar or token estimate is
claimed here. Quota may delay or prevent only the audit.

The trigger does not duplicate product acceptance/UAT, TODO planning, ordinary
subtask closeout, status reporting, or RSI review. Audit and audit-review
records cannot recurse. This cost and collision check belongs to the existing
anti-bureaucracy review of the skill change; it does not add another review,
planner, receipt, or user-facing gate.
