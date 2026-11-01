# JEV escalation: guarded technical advice

`escalation_policy.categorize()` remains the deterministic offline baseline.
The production `jev.ps1 -CaseFile ...` wrapper selects `guarded` advice by
default; `-PolicyMode shadow` retains observation-only behavior. Direct Python
callers keep the historical default `shadow` and opt in with
`--policy-mode guarded`. Neither mode dispatches models, sends Office messages
or authorizes an action.

Guarded advice may promote a clearly technical question to an architect.
It cannot downgrade declared business/process/required-field/access/strategy
decisions, unknown business scope or any hard action gate. A model `self`
answer cannot create proof of a reproduced, approved baseline restoration.
Known technical uncertainty cannot be downgraded by a `lead` answer. Unknown,
low-confidence, malformed or unavailable advice retains the local baseline;
conflicts require the lead to reconcile the evidence. A confidence threshold
is an engineering guard, not measured Helpdesk accuracy or permission.

1. Build a case per `jev-escalation-example.json`: `schema_version: 1`, `id`,
   `summary`, `category`, `baseline_reference`, `business_change`,
   `scope_change`, `restores_baseline`, `reproduced`, `hard_gates`,
   `approved_gates`, `technical_uncertainty`. Shape is exactly
   `escalation_policy.validate_case`. `approved_gates` is parent-reviewed
   metadata, never proof of owner consent by itself.
2. Offline, no network, no API key:
   `python escalation_advisor.py baseline --case <case.json> --output <report.json>`.
   Use this whenever a shadow signal is not needed or JEV is unavailable.
3. With a bounded guarded consultation:
   `python escalation_advisor.py advise --case <case.json> --policy-mode guarded --max-cost-usd 0.004 --output <report.json>`
   or `jev.ps1 -CaseFile <case.json> -EscalationMaxCostUsd 0.004 -OutputFile <report.json>`
   (add `-Baseline` to `jev.ps1` for the fully offline path instead). The key
   comes from the same in-memory retrieval `jev.ps1` already used for routing;
   nothing here reads or writes a different credential/config path.
4. Exactly one Decisions request is sent, asking a single `route` choice
   (`self` / `lead` / `architect` / `owner` / `unknown`) over minimized case
   metadata only: category, business/scope-change flags, restores_baseline,
   reproduced, technical_uncertainty, whether a baseline_reference was
   declared (not its raw value) and the hard-gate names/count. `summary` is
   excluded by default; its explicit minimized opt-in is described below.
   `id`, the raw `baseline_reference` and `approved_gates` never reach the
   network -- local paths/ticket refs and owner-authorization evidence stay
   local. Bad input, an over-budget request or a malformed/
   mismatched response is rejected before anything is sent or trusted.
5. The report retains the deterministic baseline and its shadow annotation,
   and in guarded mode identifies the effective route and why the signal was
   applied or held for parent review. It also has a top-level
   `jev_consultation` block: `{"status": "consulted", ...}` with the
   decision id/model/provider/usage, or `{"status": "unavailable", "reason":
   "<fixed_code>"}` on any failure. A failure never blocks the report: the
   local baseline route remains available, and there is no model fallback or
   retry. Output files are append-only -- an existing report is never
   replaced; run again with a new path.

```powershell
python <skill>/scripts/escalation_advisor.py baseline --case <case.json> --output <report.json>
& <skill>/scripts/jev.ps1 -CaseFile <case.json> -EscalationMaxCostUsd 0.004 -OutputFile <report.json>
```

Existing routing calls (`-TaskFile`/`-SnapshotFile`/`-MaxCostUsd`) are
untouched: `-CaseFile` selects a separate PowerShell parameter set, so the
two modes cannot be mixed in one invocation.

To make the advice useful beyond repeating preclassified flags, the parent can
explicitly add `--include-redacted-summary` (wrapper `-IncludeRedactedSummary`).
First rewrite `summary` into an abstract problem description of at most 1200
characters, without customer details, raw tickets, code, contact details or secrets.
This sends only that summary as untrusted data in addition to the fixed fields.
Obvious paths, URLs, emails, long number strings, code fences and token patterns
are refused; these checks do not prove anonymization. Default behavior still
excludes the summary. No model may assume the flag authorizes sharing raw data.

## Evidence and business boundaries

`escalation_policy.calibration_report()` compares labelled rows offline; it
never activates routing at any sample size or confidence, and synthetic rows
are always reported as `SYNTHETIC_NOT_HELPDESK`, never real Helpdesk
validation. Real-case evaluation against real Helpdesk outcomes remains a
separate exercise. Production guarded advice uses parent-reviewed minimized
cases, not raw tickets and not automatic customer requests. Its report is
bound locally to the full case; this hash detects changes but cannot prove
the metadata true. A false technical label in the input needs reconciliation,
not an invented claim that a classifier verified the owner's business rules.

Use `baseline` for an obvious known bug or owner decision; do not spend an API
call to repeat a settled choice. For a genuine dilemma consult once, keep the
result with the task and send only its bounded conclusion through Office.
An Office Wake must not repeat the consultation. `consumed` and `ACK` do not
mean the work passed acceptance; the existing scheduler owns task completion.
Selecting an architect uses the existing `architect_advisor.py` and fresh
Fleet readiness. With catalog tiers (default): eligible Manager (M) first, the
least recently used one; a Director (D) only when no Manager qualifies; DENY
never. Without tier data the legacy order applies: Mythos, then ranked Opus.
The caller must independently check real approval evidence before any action,
even when a case includes `approved_gates`: that field never grants authority.

## Cost and data

Same fixed model, endpoint and pricing basis as routing (see
`jev-routing.md`): `typesafe/jev-1.13`, Decisions API, TypeSafe only,
`data_collection=deny`, no fallback, no redirects, no retries. The single
`route` question keeps the request small (well under the routing request's
byte budget); an absent `usage.cost` in the response stays `null`, never
coerced to zero.
