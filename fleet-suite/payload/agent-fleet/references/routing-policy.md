# Adaptive Routing Policy

## Objective

Maximize verified task quality and useful throughput across paid CLI
subscriptions. Consumption is not a goal by itself. A model below the quality
floor never wins merely because its plan is unused.

## Stable and momentary scores

Capability Score describes a versioned Execution Profile independent of the
current queue or quota. Assignment Score is an event produced for one Task
Profile against current readiness, quota, and local evidence. Never write a
momentary quota or busy-state value back into the stable capability profile.

An Execution Profile is identified by CLI, exact model, reasoning level, mode,
and required tools. The concrete fingerprint is deterministic.

## Gates

Require a current model audit, executable readiness, authenticated entitlement,
exact configured model, active lifecycle, mode and tool support, context
capacity, risk ceiling, minimum capabilities, explicit non-exhausted fresh
quota, write isolation, and applicable safety rules. Unknown quota is neutral;
unknown authentication is a hard stop. Qwen is eligible through verified
`LIMITED` native CLI profiles; only confirmed shared-pool exhaustion or a
provider denial blocks them.
Qwen3.8-Flash-Next is represented by exact native ID `qwen3.8-flash`; the
invented ID `qwen3.8-flash-next` is ineligible. OpenCode/Z.AI GLM-5.3-Flash is
eligible through review-only `LIMITED` profiles after
both the native model probe and DPAPI credential-readiness gate pass.
`qwen-local` and `qwen-local-5070ti` are two aliases of one bounded,
externally managed Ollama runtime. They support advisory, review and
implementation mode after readiness, with one shared serialized slot and the
exact `huihui_ai/Qwen3.8-abliterated` model at
`http://127.0.0.1:11439/v1`. Fleet fails closed until the listener owner,
pinned executable/version/SHA-256, and native `/api/tags` manifest digest all
match. Neither alias may start, stop, repair, or spawn native subagents; a
missing listener or mismatched digest blocks routing. Fleet observes but never
starts or stops Ollama.
It has no provider quota and receives headroom 1.0 with confidence 1.0; this is
an availability signal, not a claim that electricity or hardware is free.

Flat-rate subscriptions use quota as an availability/capacity gate, never as a
currency estimate. Execution profiles backed by one provider entitlement must
declare one shared pool identity. A confirmed exhausted window or HTTP 429
blocks every member of that pool until reset while keeping all members visible
for fit analysis. Do not fabricate separate headroom for Qwen3.8-Flash-Next,
Qwen 3.8 Max, GLM 5.2, and DeepSeek V4 Flash when they consume the same Coding
Plan window. Qwen Max is requested through the canonical runtime alias
`qwen3.8-max`. The current provider snapshot observed behind that alias is
`qwen3.8-max-0902`; it is provenance only and must not be used as a CLI
argument. GLM 5.3 Flash is routed through OpenCode/Z.AI rather than Qwen Code.
Never merge either local Qwen profile into that pool. Their provider cost is
`0 zł`, and their execution locks provide bounded queuing rather than paid-plan
capacity. Electricity and hardware remain outside telemetry.

## Task fit

For substantive work the parent supplies a validated assessment that scores
every profile in increments of five and gives workload weights summing to 100.

Terminal-dependent work must use Task Profile v2 and declare `shell` in
`required_tools`. Legacy v1 assessments cannot represent a required-tool
contract and are allowed only for backward-compatible, non-substantive calls;
they must never route work whose acceptance depends on commands, tests, builds,
or project scripts.

`fit = 0.75 * weighted_static_fit + 0.25 * orchestrator_fit + arena_adjustment`

Arena adjustment is optional, requires an exact verified alias and fresh
snapshot, and is clamped to `[-2.5, +2.5]`. Missing Arena data contributes zero
adjustment, not a penalty.

The blend above is the capability-fit component, not the final decision.

## Near-tie selection

Find the best eligible fit. Only candidates no more than six points behind it
enter the resource set. A sole candidate in that set wins immediately.

For low/medium risk:

`assignment = .40 capability + .12 quota + .12 speed + .12 first_pass + .10 verifiability + .06 concurrency + .08 subscription - penalties`

For high/critical risk:

`assignment = .65 capability + .04 quota + .03 speed + .15 first_pass + .08 verifiability + .02 concurrency + .03 subscription - penalties`

Penalties are delegation overhead, uncertainty multiplied by task impact, and
expected repair cost. Do not count the same repair risk in both first-pass and
another reliability component. A candidate more than six capability points
behind the best eligible profile does not beat a clear quality leader through
quota alone.

Do not estimate task token cost from a quota percentage. Quota is a tie-break,
not a promise that a task will fit into a provider window.

## Quality-auditor routing

Quality Gate first routes the task as `review`, then applies independent audit
gates. The auditor cannot share a CLI or `model_provider_id` with any worker,
must expose `filesystem-read` and `independent-review`, score at least 80 on
independent review, and satisfy the frozen contract risk ceiling.

Among eligible auditors:

`auditor_score = .70 review_assignment_score + .30 independent_review`

The Assignment Score already carries task fit, current subscription headroom,
speed, verifiability, and accepted local evidence. Provider independence is
therefore not counted again as a soft reward; it is a hard eligibility gate.
One auditor may cover up to 16 related root tasks. If the worker set consumes
all eligible model providers, split the group or use parent review; never waive
independence silently.

For `security-gate`, the same independence gates apply. An eligible ACTIVE
auditor must meet the frozen floors: independent review `>=95`, instruction
following `>=91`, security review `>=95`, tool use `>=95`, verifiability
`>=83`, and evidence confidence `>=0.7`. Its declared tools must equal exactly
`filesystem-read`, `independent-review`, `security-review`, and `tool-use`;
any write, shell, filesystem-write, or undeclared tool disqualifies it.
OpenCode/GLM-5.3-Flash is the deterministic primary. Codex and Grok backups
are considered only when they satisfy the same contract. The gate tries a
bounded deterministic fallback sequence, and an incomplete handoff is a failed
attempt. Split a batch that consumes the primary provider or use an independent
backup plus parent security review; never waive provider independence.

Run one security gate per coherent implementation batch, not per micro-edit.
This preserves review quality while avoiding repeated native-session context
overhead. Security findings are actionable only when the structured contract
contains exact code location, CWE, exploit path, confidence, evidence, and a
bounded remediation.

## Quota honesty gates (0.9.37)

- `availability_blocked` (a recorded provider refusal such as CodeBuddy
  `plan-credits-exhausted` or Grok HTTP 402, or a failed Kimi recheck) makes
  the profile not ready with reason `availability-blocked=<reason>` and fails
  `quota-not-exhausted` with the refusal named in its evidence. Neither the
  baseline `--allow-no-eligible` fallback nor an explicit override can launch
  it; an explicit request names the refusal. Only a fresh availability check
  clears it; for a pool without a native check (CodeBuddy) a `credits`
  refusal is cleared by a later fresh authoritative snapshot that shows
  allowance left (`credits_remaining > 0` when recorded, otherwise a positive
  headroom). A pool with a native check still needs that check. A fresh
  non-advisory snapshot with `credits_remaining <= 0` fails
  `quota-not-exhausted` on its own (evidence `credits_remaining=<n>`), even
  when a plan fraction still shows headroom and no refusal was stored. An
  explicit request for a refused account names the refusal also when the
  model audit is DEGRADED.
- `context-window` uses the larger of the assessment's
  `context_load.estimated_tokens` and `ceil(UTF-8 task bytes / 4)`
  (`source=utf8-bytes-div-4`); an understated assessment never shrinks it. The task text is a
  floor of the prompt, so routing never refuses a task that could fit. Before
  launch the same comparison runs on the real prompt bytes: more than 90% of a
  known window refuses the run. An unknown window never blocks.
- Run records carry the limits Fleet enforced: `max_prompt_bytes` is the
  smallest of the requested limit, the context-window bound and the OpenClaw
  envelope; `max_model_tool_calls` is Qwen's `--max-tool-calls`, Grok ACP's
  budget, the MiniMax `--max-steps` / Muse `--max-model-steps` cap (the
  requested bound or the per-complexity default) or the requested bound (local Qwen's `-1`, no limit, is `null`); `model_tool_calls` is also counted from
  Codex/Qwen JSON streams. `null` means no such limit or no visible count;
  a stream with a cut or unreadable event line, or with no event at all
  (empty stdout), gives `null`, never `0` or a lower count.
- Kimi `/usages` needs the CLI's 15-minute OAuth token. The native fallback
  refreshes it, so Fleet re-reads `/usages` once afterwards to keep the
  weekly/5 h percentage windows. Both answer shapes are parsed (`usage` plus
  `limits[]`, and `usages.limit_5h/limit_7d/limit_month_total`); an absent
  proto3 counter is zero, an unreadable value keeps the answer unknown.
  When the native check succeeded, the result keeps `source=native-cli-check`
  (it alone clears an exhaustion whose scope is not a `/usages` window, such
  as `unknown` from an authoritative snapshot or `credits`) and carries the
  re-read windows as pace evidence.

## Owner decisions: pace brake and unknown quota (default off)

Evidence: Qwen 3.8 Max consultation 2026-09-26
(`research-runs/tokenomics-20260926/consult-qwen.md`, Fleet run
`run-20260926T214101Z-6cabafa4bc`): 13 of 14 pools had
`pace_brake.reason=disabled`, 9 of 14 pools had no machine-readable quota, and
`quota-observed`/`quota-not-exhausted` passed with `status=unknown`,
`headroom=None`, `confidence=0.0`. Qwen proposed tempo WARN above 1.2 and
throttle above 1.5 for pools with fresh readings, and fail-capped gates for
unknown quota. Both are mechanisms only; enabling them is an owner decision
per pool in `profiles.json` `quota_collection`, set identically on every
profile of the pool.

- `pace_brake.tempo_warn` / `tempo_throttle` / `tempo_min_confidence`
  (optional, with `enabled: true`): the pool tempo is consumed fraction over
  elapsed fraction of the limiting window (1.0 = on pace). At or above
  `tempo_warn` the brake view reports `tempo_level=warn` and never blocks. At
  or above `tempo_throttle` the reversible brake activates with reason
  `tempo-exceeds-throttle`; `until` is the earliest release if the pool stays
  idle. Levels compare `tempo_now`, the tempo at evaluation time assuming no
  use since the reading (a measured `tempo` is frozen at its reading), so an
  idle pool is released at `until`. A reading older than the brake's
  `max_reading_age_hours` is stale: it never brakes, never counts as clear
  and is listed in `tempo_stale_windows`; `tempo_level=stale` only when no
  window is fresh. Staleness is decided per window before comparing, so a
  stale window with a higher tempo cannot hide a fresh window over the
  throttle; every fresh window is checked, `tempo_window` names the highest
  fresh one and `until` is when the last throttled window lets go. A
  tempo below `tempo_min_confidence` (default 0.5) stays unknown and never
  brakes. Without these keys the brake behaves exactly as before. The brake
  is computed once per pool, so every member of a pool must carry the same
  enabled `pace_brake` (or none/disabled on all); otherwise profile loading
  fails.
- `unknown_quota_mode`: `fail-open` (default, historic behaviour) or
  `fail-capped`. With `fail-capped`, a pool without a fresh numeric quota
  observation (a plan fraction or, when no fraction is reported, a credit
  balance) admits advisory and review runs only; implement runs fail the
  `quota-unknown-capped` gate in adaptive and baseline routing and at send
  time. Members of one pool must agree, otherwise profile loading fails.
- The baseline `--allow-no-eligible` fallback relaxes only the quality floor:
  it never selects a row that fails a quota gate (`quota-observed`,
  `quota-not-exhausted`, `quota-unknown-capped`, `pace-brake-clear`). An
  explicit request refused by one of these gates names that gate.

## Baseline and rollback

Every route logs the previous static-fit-plus-underuse baseline and whether the
adaptive decision differs. Set `AGENT_FLEET_ROUTING_MODE=baseline` to disable
adaptive selection for the current process or session. Do not delete adaptive
evidence when using the kill switch.

## Local learning

The parent records one immutable accepted/rejected evaluation after independent
tests and review. Workers never evaluate themselves. Group retries, repairs,
and council members by `root_task_id`; one root task is one statistical sample.
Evidence is scoped to task kind and exact execution-profile fingerprint. Local
signals activate at five samples, use shrinkage `n/(n+20)`, and remain capped
until 20 samples. Tokens are a cost and diagnostic per accepted result, never a
reward for consumption.

## Failure behavior

- stale, missing, or malformed market audit: continue visibly degraded with
  certified static profiles, disable benchmark scoring, and preserve all v2
  eligibility, identity, quota, and security gates;
- stale/missing Arena: static capability fallback;
- stale/missing quota: neutral resource signal;
- missing per-run tokens: preserve `unknown`;
- missing Z.AI plan points: preserve `unknown`; never convert OpenCode model
  tokens into Coding Plan points;
- exhausted shared flat-rate pool: keep members visible, mark all ineligible;
- no eligible model: refuse and explain hard gates;
- worker failure or timeout: preserve evidence; Quality Gate may request a
  bounded repair only under the frozen 0..3 repair budget;
- security gate with neither a non-empty Git patch nor changed-file snapshots:
  refuse before launching the auditor; an empty bundle is not code evidence;
- explicit profile/model override: preserve hard safety and readiness gates.
- task assessment says `SKIP`: refuse execution unless the parent explicitly
  uses `--force-delegation`; hard gates remain non-bypassable.
