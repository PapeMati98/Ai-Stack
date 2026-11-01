## 0.9.88 Grok diagnostic candidate provenance

Grok projection now supplies its selected structuredOutput or assistant text to handoff diagnosis. Missing assistant content no longer falls back to the whole native envelope and ancillary Office/tool-event id/kind/title/status. Empty terminal text does not hide an invalid structured answer. Absent handoff is named; cancelled native cause is not inferred. Cancellation, no-auto-resume, identity, permissions, limits, transport and provider-failure priority are unchanged. No handoff is reconstructed from metadata.

## 0.9.86 explicit-only Reflection Beam profile

OpenCode provider configuration, provider allowlisting, and model inventory are
derived from the selected profile. The existing Z.AI profile retains the
0.9.85 runtime config byte for byte. The only added provider is Reflection,
mapped to `REFLECTION_API_KEY` through the same secret filtering as the Z.AI
credential.

`opencode-beam` is disabled for routing and has no Office access. Fleet accepts
it only when explicitly selected with a workspace whose resolved real path is
inside the explicitly configured synthetic-only workspace root; path resolution follows junctions
before applying the case-insensitive, separator-bounded prefix check. The
profile remains limited to synthetic content because Reflection marks submitted
content as training enabled. Its CLI config contains only the Reflection
provider, denies edit and shell permissions, and injects no Office MCP tools.

Both Beam execution profiles declare `max_risk: low`. A call without a task
assessment uses Fleet's deterministic fallback risk of `medium`, so the risk
ceiling refuses that call; a validated assessment with `risk: low` passes this
gate. The shared execution-profile map is not a mode override: Beam does not
resolve for implementation mode, which its profile does not support.

## 0.9.71 exact Muse Wonder models and Contributor opt-out

Native Muse allows exactly Spark 1.3, Wonder 1.0 and Wonder Full 1.0 under
provider meta. Owner decision 2026-10-12 assigns tiers K, M and D respectively.
Spark remains the default; explicit model selection resolves the matching
read/implementation profile and reasoning effort without model fallback.
Wonder profiles stay LIMITED and inherit conservative priors and provisional
Spark context capacity; Meta's AA index claims are identified as provider claims.

Every model run, including content-free availability probes, supplies
`--contributor-mode off`. CLI versions below 1.4.6 are not ready because they
lack this run-only override. The native pin is 1.4.6-R6000.1 (SHA-256
`0b33e58c27ac157e6372626e02d03f22c360dca2878b2c78cfea8805e9b24cc4`);
its Authenticode signature was locally verified as Meta Platforms, Inc.
The filename/path/hash and Windows sandbox deep-probe gates remain intact.

Contributor is denied before launch and in native identity evidence. A native
`run.model.configured` must match the exact requested model; missing, mixed,
foreign, padded or Contributor model identity invalidates the handoff.
Identity-bearing events preserve literal model/provider IDs: whitespace and
case variants cannot normalize into the requested model or provider meta.
Existing narrowly authorized security-review contracts still pin Spark.
OpenClaw, the static Muse Chrome relay slot and shared subscription quota pool
are unchanged. This candidate does not deploy or modify account settings.

## 0.9.68 durable state and corrupt-cache recovery

A hard reset left availability and pace JSON files filled with zero bytes:
atomic replacement published metadata before buffered payloads reached disk.
`adaptive.atomic_write_bytes` now writes an exclusive temporary file in the target
directory, flushes it and calls `os.fsync` before `os.replace`. JSON state,
Fleet run/quality/campaign records, incident indexes, process checkpoints and
local Qwen state use this helper. File fsync errors keep the old target intact;
Windows directory fsync is not attempted. Creation modes are preserved,
including private 0600 incident/checkpoint files. Failed writes can leave owned
temporary files; this change adds no automatic deletion or quarantine.

Bounded state reads ignore zero-filled/truncated JSON, invalid encoding/schema,
links and oversized files. Availability becomes `unknown` with
`reason=corrupt-cache-ignored`, as with missing evidence. A subsequent native
success, refusal or probe replaces the file and retains `cache_read_reason` for
diagnostics. Independent valid exhaustion and native-probe requirements still
apply. Import write failures are isolated per pool (`state-write-unavailable`).
Bad quota/run records are skipped with aggregate `state_files` diagnostics;
bad pace exports regenerate with `previous_state_reason=corrupt-cache-ignored`.
Validation includes nested native-attempt fields, finite token counts and cached
native quota windows before they feed availability, routing or pace calculations.
Valid pace entries still merge per pool. Pace calculations have no state reads,
so `quota_pace.py` requires no change. No owner state or production is modified.

## 0.9.67 automatic Antigravity availability recovery

The native 429 refusal lost its `Resets in` duration, while passive checks
without an availability probe renewed the same block every minute. Routing
could consequently never send the native run needed to clear the refusal.

The existing Antigravity terminal projection now reads only native failure
fields and anchored stderr `Error:` / `AGY_ERROR:` messages. A refusal marker
and a complete, ordered d/h/m/s duration must occur in the same message.
Conflicting or malformed durations and missing resets retain the generic
failure behavior. Provider attempts retain only safe quota metadata. A verified
failed native run with known zero tokens maps it to subscription `exhausted`,
`reason=usage-limit`, `source=native-provider-attempt`, and `reset_at` equal to
run completion plus that duration, without `clear_requires=native-cli-check`.

For pools without a probe, a passed reset persists `unknown` with
`reason=reset-passed-no-probe`. Without a reset, the original `observed_at`
plus the existing 15 min / 1 h / 6 h / 24 h backoff determines half-open
recovery (`unknown`, `reason=half-open-after-backoff`). Passive checks never
move that deadline or the original observation. Legacy sliding deadlines are
ignored, so a six-day-old step-3 Antigravity block opens on its first check.
Reimporting the same receipt cannot reblock or escalate it. A new refusal
escalates the retained step (capped at 3); native success clears it as before.
Unknown permits routing but does not assert remaining tokens or proven capacity.

Pools with native probes preserve their evidence requirements. Muse and
CodeBuddy have an explicit `availability-recheck` lane even though passive
preflight supplies no probe: their native refusal remains blocked pending
fresh proof, with a fixed deadline rather than minute-by-minute renewal.
Legacy refusals are recognized by their existing reason; new ones also retain
the recheck adapter. Kimi and Grok probe behavior is unchanged. No new probe,
provider call, production state edit, deployment or reset-file repair is needed
for Antigravity recovery. This isolated candidate still requires parent review.

# Agent Fleet Suite 0.9.30 reproducible release candidate

## 0.9.30 stateless pool pace brake

Zbudowane na wdrozonym 0.9.29 sesji 88900f5b (odpowiedzi przekaznika Office) - scalenie trojstronne fleet.py, routing_v2.py, quota_pace.py, profiles.json i dokumentacji wzgledem 0.9.28 bez konfliktow.

`quota_collection.pace_brake` enables a hard routing and send gate per profile;
only MiMo enables it initially (`burst_factor=1.5`, `window_hours=24`,
`max_reading_age_hours=48`). Missing configuration leaves routing unchanged.
Every `current_quota_views` call recomputes the pool brake from the current
quota readings and Fleet run ledger. It uses the limiting active plan window,
the latest remaining fraction projected by run tokens after that reading,
and the last two readings in the same window with a measured decline and
matching run tokens for fraction-per-token calibration. The allowed rolling
window fraction is `remaining / hours_to_reset * window_hours * burst_factor`;
the consumed fraction is the sum of pool run `token_usage.total_tokens` in
the rolling window times that calibration. The brake is active only when
consumed exceeds allowed. `until` is the first time the allowance catches up
or an old run leaves the rolling window, assuming no new run. The next quota
view automatically clears it; no brake cache, state file or brake lock exists.

Missing calibration, no ledger tokens, a reading older than the configured
age, or a passed plan reset all fail open with an explicit reason. The view
publishes `pace_brake` and the existing `pace/latest.json` pool entry publishes
the same fields as `brake` for the AI panel. The export is informational and
is never read for routing. Its existing `latest.lock` protects only the
panel export. Automatic routing excludes an active pool through
`pace-brake-clear`; `execute_single` rechecks the gate before run, knowledge,
prompt or provider-settings materialization. An explicit `run --agent mimo
--override-pace-brake "reason"` bypasses only this gate for that run and
records the reason in the run ledger. Other quota and safety gates still apply.

## 0.9.28 per-pool pace/latest.json merge

Zbudowane na wdrozonym 0.9.27 innej sesji (0.9.26: przekaznik Office dla kontraktorow; 0.9.27: hotfix runow Codex) - scalenie trojstronne fleet.py wzgledem 0.9.25 bez konfliktow.

`current_quota_views` exports whenever it computes at least one pool, including
calls with `agent_ids` and calls with a partial `profiles` map. The export
holds a short cross-process lock on `pace/latest.lock` while it reads,
merges and atomically replaces `pace/latest.json`. A valid schema_version 1
file contributes pools absent from the current calculation; malformed or
other-schema JSON starts a fresh pool map. Existing pool entries without
`computed_at` inherit the old file's `generated_at`. Recomputed
entries receive `computed_at` from the calculation time, in UTC ISO Z form;
the top-level `generated_at` records the new file write time. Existing
top-level and per-pool fields remain available to panel consumers. A failed
lock, unreadable file or write is reported through
`diagnostics["pace_latest"]` and does not interrupt routing. The lock file
remains in `pace/` so replacement of
`latest.json` cannot change the lock identity.


## 0.9.25 native Codex and Kimi limit windows in consumption pace

Owner decision 2026-09-24: routing must consume subscriptions in proportion
to their plan windows, so the pace of Codex and Kimi can no longer stay
unknown just because their collectors were TUI-assisted. Built on the 0.9.24
copy with the version bumped; no older section changed.

- quota_collectors.py: new machine-readable collector_mode
  codex-rollout-rate-limits (registered in MACHINE_READABLE_COLLECTOR_MODES
  and dispatched in collect_profile_quotas). The session-file scan is
  bounded in both directions: directory entries are streamed out of
  os.scandir (a listing is never materialized or sorted as a whole), a
  size-capped heap keeps only max_files newest rollout-*.jsonl candidates
  by mtime (constant memory) and max_scan_entries caps how many directory
  entries are visited overall. The entry budget is checked per entry,
  including inside a single directory, so one oversized directory is never
  scanned whole on a routing decision; within it the heap selects by mtime
  among the visited entries only. Sibling directories are explored
  newest-name-first - only their names are sorted, their count per
  directory is bounded by the same entry budget, and the ISO-timestamped
  Codex partitions (YYYY/MM/DD) are few by construction - so the budget
  truncates the oldest partitions, not the newest files. Each candidate is
  tail-read under max_bytes_per_file. Only lines containing "rate_limits"
  are parsed, only token_count events are accepted as quota samples (a
  foreign event with a lookalike block is ignored) and only the newest
  sample by event timestamp is kept. Windows map by window_minutes:
  <= 24 h -> short (with the window length in hours on the row),
  6-8 days -> weekly, 28-31 days -> monthly; others are skipped.
  remaining = clamp(1 - used_percent/100, 0, 1) (used_percent above 100 is
  accepted and clamps to 0); resets_at (epoch seconds) or
  resets_in_seconds (relative to the event timestamp) give reset_at, and a
  corrupted resets_in_seconds that would overflow timedelta is skipped
  like any other invalid window instead of raising into the routing
  decision (collect_profile_quotas also catches OverflowError as a
  collector error). Status fresh requires sample age <=
  max_sample_age_seconds and a reset in the future, else stale; a stale
  row keeps timestamps and reported_headroom for diagnostics but publishes
  no window fractions or resets, so quota_pace and routing penalties never
  run on evidence the age ceiling already rejected. A missing directory,
  no files or no samples return an "unknown" row with a reason instead of
  raising. Privacy: only counters, timestamps and plan_type leave the
  session files - never line content, session file paths or credit
  balances. The codex profile in references/profiles.json now uses this
  collector (sessions_dir default %USERPROFILE%\.codex\sessions, max_files
  8, 256 KiB per file, max_sample_age_seconds 3600).
- Kimi: kimi_usage.parse_usage now reports window_hours per window from the
  /usages window descriptor (timeUnit x duration). In
  fleet.current_quota_views the windows of a successful kimi-native-usage
  availability answer (never the native CLI fallback probe, which proves
  availability without windows) are mapped onto the Kimi view as
  weekly/short_window/monthly remaining_fraction + reset_at plus
  short_window_hours before pace is computed, reusing the cached availability
  state - no extra network calls or inference. Applied windows own the view
  source (kimi-native-usage), so pace never reports an earlier stored
  provider-dashboard label for numbers measured by /usages; conversely the
  window fields this refresh did not write (including a leftover
  credits_remaining, which converts to a monthly fraction when the profile
  declares a plan capacity) are dropped from the relabelled view, so the
  kimi-native-usage label never covers provider-dashboard numbers - the
  stored record keeps feeding pace separately through
  adaptive.read_quota_records with its own source. View status, headroom
  and the fail-closed exhaustion semantics are untouched; the fields are
  numeric pace evidence only.
- quota_pace.calculate_pool_pace: the short-window length is taken from the
  snapshot row first (short_window_hours, matched by reset), then from the
  profile-level fallback; when rows disagree on the length of the same
  window (same reset_at), the newest observation wins, so an older row
  cannot move the window start away from the latest sample. Weekly stays
  7 days. Other pools are unchanged.
- fleet.current_quota_views: a fresh provider-dashboard record with a
  confidence at least as high as the live collector row still wins the
  status view, but the displaced fresh live row is no longer dropped from
  evidence - it joins the pace snapshots next to the stored records and
  annotated views, so a manual dashboard entry without window resets can
  no longer hide a native Codex window from quota_pace (before this fix
  the pool reported tempo=None, source=none and routing lost the
  pace_overburn penalty). The view keeps the dashboard source and status;
  diagnostics gain pace_evidence_from naming the displaced live source.
- fleet.current_quota_views now exports pace for consumers that cannot
  import fleet.py: <state_dir>/pace/latest.json, atomically via
  adaptive.atomic_write, schema_version 1 with generated_at, fleet_version
  and per-pool {agents, **calculate_pool_pace result}. No secrets or paths
  beyond the state dir itself. Only a full-fleet view (no agent_ids
  filter) writes the file; the single-agent send gates compute pace for
  their agent but never overwrite full coverage with a one-pool panel. The
  contract requires the export whenever pace was computed, so the atomic
  write creates the state directory when it does not exist yet. A failed
  write is noted in quota_collection["pace_latest"] and never interrupts
  routing. quota-status reports the export truthfully: a successful
  pace/latest.json write counts as one writes_performed even though no
  quota record changed. The file deliberately lives in pace/, not quota/,
  where adaptive reads quota-*.json records.


## 0.9.24 plan-pace routing, Muse sandbox TEMP, Kimi agent loop

Built on the deployed 0.9.23 (three-way merge of fleet.py against 0.9.22, no
conflicts). Owner decision 2026-09-24: subscriptions must be consumed in
proportion to their windows; no "explicit-only" mode.

- quota_pace.py (new, stdlib only): per-pool pace = used fraction / elapsed
  window fraction at the time of measurement. Monthly window starts one
  calendar month before monthly_reset_at, weekly seven days before the weekly
  reset. A reading with only credits_remaining uses
  quota_collection.plan_capacity_credits (mimo: 11 000 000 000). Between two
  readings of one window Fleet calibrates fraction per Fleet token and projects
  the current fraction from its own run ledger, so pacing keeps working without
  new console readings. Confidence: projection 0.8, reading 1.0 up to 12 h and
  linearly 0 at 72 h, damped in the first 5 % of a window. No data = neutral.
- One canonical pool identity: rows and runs of a known profile belong to that
  profile's quota_pool_id; quota-record now stamps it. Legacy readings recorded
  under the agent name still map correctly without file migration.
- routing_v2: subscription_utility blends run-share underuse with
  pace_utility = 50 + 50 * clamp(1 - tempo, -1, 1) by confidence, and
  penalties['pace_overburn'] = c * min(35, 20 * log2(tempo)) above tempo 1.25.
  Pace never hard-blocks; monthly_remaining_fraction now counts in headroom.
- Muse native transport (implement): TEMP, TMP and TMPDIR point to
  <workspace>/.muse-tmp, excluded through `git rev-parse --git-common-dir`
  /info/exclude. Measured: Muse runs commands as muse-sbx-r1 with a restricted
  token that can write only inside --workspace.
- Kimi guarded home: max_attempts_per_step 5 replaces the ignored
  max_retries_per_step; max_steps_per_turn 512 for implement, 128 otherwise.
  The availability probe keeps its own 1 attempt / 2 steps.

## 0.9.23 adapter delta (other session)

See its tests: test_codex_stream_redaction.py, test_codex_terminal_handoff.py,
test_antigravity_empty_response.py, test_minimax_terminal_handoff.py.

## 0.9.22 Kimi native availability probe delta

The native Kimi probe is the only path that may clear a monthly lockout
(`source == native-cli-check`), and since 2026-09-16 it could not succeed.
Three stacked defects were measured live on Kimi CLI 0.43.1 on 2026-09-24:

1. `loop_control.max_steps_per_turn = 1` left the reasoning Kimi k3 no step
   for its reply (`max_steps_exceeded`). Raised to 2 in 0.9.21.
2. `max_output_size = 16` was spent entirely on reasoning
   (`APIEmptyResponseError`, `finishReason=length`). 64 was the smallest
   working value measured; the probe uses 128. The deprecated
   `max_retries_per_step` key was silently ignored, so the CLI retried up to
   ten times; the probe now writes `max_attempts_per_step = 1`.
3. Kimi CLI 0.43.1 emits no usage event in stream-json, so a token-only
   success test could never pass. A clean exit with non-empty assistant TEXT
   now also proves availability (`probe_evidence: assistant-text`); thinking
   parts, other roles, a non-zero exit, a timeout or a provider rejection in
   stderr never do.

The only tool control in this probe is the native `deny "*"` rule:
`run_bounded_child` enforces its tool limit for codex and claude only.
`quota_availability.check` is byte-identical to 0.9.20.

## 0.9.21 monthly quota window delta

`quota-record` accepts `--monthly-remaining-pct` and `--monthly-reset-at`,
stored as `monthly_remaining_fraction` and `monthly_reset_at`. Routing
headroom still reads short-window and weekly fractions only.

## 0.9.20 GPT-6 canary route delta

Restore the 0.9.18 Codex routing map after successful fresh canaries on
Codex CLI 0.156.1 for exact GPT-6 Luna/Sol runtime IDs. GPT-6 Luna and Sol
stay LIMITED with conservative priors. High advisory/review remains mapped
to GPT-6 Astra, and dedicated security review remains on ACTIVE GPT-5.6 Sol
because GPT-6 Sol has no security-review capability.

## 0.8.68 two-gap continuation

This candidate repairs two post-0.8.65 defects only. Graph failure
reconciliation may bind a unique canonical start/result/finished/job/context
chain when `adapter-result.json` is absent; a present malformed or conflicting
adapter result still refuses. Quality-gate OpenCode auditors admit only the
current-gate revalidated captured public-source snapshot file into
`permission.read` after the existing `**/secrets/**` deny. Ordinary OpenCode
runs stay default-deny. GLM output validation is unchanged.

## 0.8.47 explicit-only GPT-6 Astra delta

This immutable delta adds one Codex execution profile, `codex-gpt-6-astra`, reachable only by
an explicit `--model gpt-6-astra` or `--execution-profile codex-gpt-6-astra`. The automatic
`low/medium/high -> Luna/Terra/Sol` map is unchanged, so no routing decision moves without an
explicit request. The profile is `LIMITED` with conservative priors until accepted outcomes
replace them. Version 0.8.45 is reserved by an in-flight, unmerged maker checkpoint
(quality DLP fixtures) and is intentionally skipped here.

## 0.8.42 Claude Fable 5.1 and credit-aware routing delta

This immutable delta removes every executable legacy Fable 5 profile and pins
general and security-review work to exact model `claude-fable-5-1`. Medium,
high, and xhigh profiles are `LIMITED`, low remains an explicit A/B canary,
security review is active and read-only, and max is never automatic.

Fleet reads a sanitized Claude entry from the owner billing declaration. While
credit mode is active, Claude stays available but is placed after all other
eligible lineages in adaptive routing, baseline routing, quality-gate choice,
and security fallback choice. GLM remains the primary security auditor. The
end date is inclusive, an absent declaration is neutral, a malformed Claude
entry fails safe, and an explicit Claude request remains allowed with a visible
billing-override note. Owner prose never enters routing evidence.

## 0.8.41 Gemini routing delta

This immutable delta preserves every 0.8.40 security-evidence control and
replaces automatic Antigravity routing with exact
`gemini-3.8-flash-low`, `gemini-3.8-flash-medium`, and
`gemini-3.8-flash-high` profiles. Gemini 3.1 Pro High remains available only
through explicit model or execution-profile selection. Legacy Gemini 3.6 and
unavailable Gemini 3.8 Flash Cyber IDs have no execution profile and therefore
fail resolution before process launch.

The local model inventory and a bounded medium canary prove availability and
transport, not a universal speed ranking. Provider benchmark, context, price,
and speed statements stay separately attributed in Model Radar. Flash Cyber
requires a future owner-approved Fairwind entitlement plus native-inventory
proof; this release does not expose it.

## 0.8.40 trust-boundary delta

This immutable delta replaces Muse Spark 1.2 with exact model
`muse-spark-1.3` in every routable profile, prompt envelope, adapter, and
identity gate. Legacy 1.2 and every Contributor spelling are rejected before
process launch; the native JSONL identity check still fails closed after each
run. Muse remains `LIMITED` until accepted 1.3 outcomes replace conservative
priors.

The 0.8.40 evidence-only delta adds no Fleet behavior. Its regression suite
proves that `fleet_runtime_attestation()` rejects a payload changed after the
runtime marker fingerprint was created, and that a frozen quality bundle
preserves disjoint changed paths and snapshots from two completed worker runs.

## 0.8.38 trust-boundary delta

This immutable delta adds a dedicated Claude Fable 5 security-review profile
that satisfies the same measurable ACTIVE/read-only qualification policy as
GLM, Codex, and Grok. A Codex maker therefore retains an Anthropic fallback
when GLM times out and Grok transport is unavailable. The ordinary Fable
profile remains unchanged; only the dedicated profile can enter a security
gate and it exposes no write, shell, web, or subagent capability.

## 0.8.33 Windows evidence-path delta

Quality and security evidence snapshots use the Windows extended-length path
form for filesystem writes after the existing containment and link checks.
This preserves canonical manifest paths while allowing valid repository paths
longer than the legacy 260-character Win32 limit.

## 0.8.32 security and local-runtime delta

Security-gate auditors are eligible only when an ACTIVE execution profile meets
the frozen independent-review, instruction-following, security-review,
tool-use, verifiability, and evidence-confidence floors and exposes exactly the
declared read-only auditor tools. GLM-5.3-Flash remains the deterministic
primary. Dedicated Codex and Grok review-only executions are bounded backups
only after satisfying the same floors. A security gate tries at most four
independent eligible auditors in deterministic order; an incomplete transport
or handoff is a failed attempt and cannot create success.

Task assessments carry required tools and a typed `red_team_intent`. The
abliterated local Qwen aliases require that explicit intent, remain CANARY,
offer advisory/review only, and do not participate in normal automatic routing.
They serialize one externally managed Ollama runtime at port 11439 with one
slot, a 16,384-token context, exact executable/version/hash, and a native
manifest digest check. Fleet never starts, stops, or repairs that runtime.

Distribution 0.8.31 added Muse Code 1.0.1 through the exact Windows System32
`wsl.exe` and pinned provider `meta` plus model `muse-spark-1.2`. Distribution
0.8.40 replaces that model with exact `muse-spark-1.3`. Prompt transport
uses a bounded file rather than the local `bash -lc` bridge. Profile loading,
explicit overrides, command construction, and the mandatory native
`run.model.configured` JSONL envelope all fail closed on aliases, fallback,
foreign providers, missing identity, mixed identity, and every Contributor
spelling. Review/advisory disables write, shell, and web; implementation stays
inside Fleet's detached worktree and Muse's enforcing sandbox. Fleet strips
`WSLENV`, converts only the temporary worktree `.git` pointer for WSL, and
restores its exact original bytes before collecting Git evidence.

Distribution 0.8.30 adds exact Claude Fable 5.1 canary routing without
promoting it blindly over every Claude route. The router passes the selected
profile effort to Claude Code, pins exact model ID `claude-fable-5-1`, keeps
Sonnet for routine work, reserves xhigh for high-complexity advisory tasks,
and never auto-routes max. After each Fable 5.1 child run, provider-reported
assistant or canonical usage identity is verified and a missing or different
model invalidates the handoff. Release 0.8.42 retires the then-explicit Fable 5
fallback from executable profiles.

The task assessment gains an optional but validated `data_handling` contract.
Fable 5.1 fails closed when classification is absent or unknown. Public and
internal data may pass the retention gate; confidential and restricted data
require current, account-scoped ZDR authorization that explicitly covers the
exact model. ZDR eligibility alone is never treated as authorization.

Distribution 0.8.30 retains the provider, quota, local-Qwen, Grok ACP,
quality-gate, MiniMax, and Agent Loop 0.3.0 controls of 0.8.27. It preserves two
exact-model routes. Qwen3.8-Flash-Next is the product lineage, while QwenCloud
and Qwen Code expose it through runtime/API ID `qwen3.8-flash`; the invented ID
`qwen3.8-flash-next` is rejected. It becomes the medium-complexity Qwen route,
while the canonical runtime ID `qwen3.8-max` remains the high-complexity architecture and implementation route. The provider may report snapshot `qwen3.8-max-0902` as provenance; it is never passed as the CLI model ID. Native
OpenCode/Z.AI security review moves to exact runtime ID
`zai-coding-plan/glm-5.3-flash`; legacy `glm-5.3` profiles remain explicit-only
compatibility routes. The guarded OpenCode home keeps remote model-catalog
fetching disabled and pins exactly those two Z.AI model IDs in its resolved
configuration, preventing a fresh isolated home from falling back to the
binary's older bundled catalog. Qwen output now always uses newline-delimited
`stream-json`; the fail-closed parser accepts legacy aggregate JSON and the
native event stream without the Qwen Code 64 KiB aggregate-output truncation.

The narrow 0.8.28 delta makes the existing MiniMax Coding Plan route explicitly
global-region. `MAVIS_REGION=en` is injected into version probes, deep readiness
probes, and every child run. An authentication-specific readiness failure names
only `mcode login --region global` as manual remediation. Fleet never executes
login or logout itself, and unrelated failures do not receive login guidance.

The historical 0.8.27 second-backend topology is superseded in 0.8.32. Both
local-Qwen aliases now point to the same externally managed Windows Ollama
runtime and one shared slot. They require exact native manifest-digest proof,
have no write mode or automatic routing, and give Fleet no start/stop authority.

Historical 0.8.24 added an expiring maintenance lease for the former local-vLLM
topology. That topology, its stop/start authority, and its recovery actions are
superseded and unreachable from the two 0.8.32 local-Qwen aliases, which only
observe the pinned external Ollama runtime.

Distribution 0.8.22 added native MiniMax Code 0.2.4 with the paid managed-login transport,
pins `minimax/MiniMax-M3`, parses the terminal `exec.result`, verifies exact
provider/model identity, and records native token telemetry. A per-run config
disables browser, web, shell, memory, external skills, plugins, MCP, and native
subagents; implementation adds only file write/edit tools in Fleet's detached
worktree.

Distribution 0.8.21 added Agent Loop 0.3.0 `L2-search`. The controller compares several frozen
implementation hypotheses as independent Fleet worktrees from one clean
baseline. A trusted verifier runs before a different-CLI Fleet Quality Gate;
the complete trajectory, score vector, dead ends, ties, stagnation, and optional
read-only supervisor redirects are preserved. The parent must explicitly accept
the champion gate and close every other auditor-accepted gate. The controller
never integrates a candidate.

Distribution 0.8.20 advanced the Agent Fleet runtime to 0.8.20. Provider
diagnostics are redacted for sensitive response-header values before parsing,
persistence, and run-ledger projection. Manual and
TUI-assisted quota snapshots are now advisory and cannot create a hard routing
block. A fresh provider-dashboard or native-JSON snapshot remains authoritative,
outranks a later manual note while valid, and a zero-headroom snapshot without an
explicit reset expires after six hours. This prevents a historical or
misclassified provider error from silently disabling a recovered subscription.
The historical local Qwen3.8 27B vLLM topology exposed two cross-process
advisory/review slots and a large YaRN context. It is superseded by the shared,
single-slot external Ollama contract in 0.8.32 and is not an eligible route for
either local alias. OpenCode/Z.AI
GLM-5.3 now requires exact runtime provider/model observations from sanitized
native INFO logs. Missing or switched identity fails closed. Its terminal handoff
parser conservatively repairs invalid JSON backslash escapes in Windows paths, and
guarded-home cleanup ignores child files that disappear during `rmtree` while
retaining bounded retries for real permission failures. Grok's declared
`shell` capability is now implemented by an explicit command allowlist under
`dontAsk`, a parent-side one-shot permission broker, and strict sandbox
containment, while destructive and remote-Git commands remain denied. Claude
readiness uses `auth status`, and an empty code
evidence bundle cannot reach the GLM-5.3 security auditor. Guarded OpenCode
home cleanup validates the isolated path first, then uses the Windows extended-length
path namespace with bounded retry. Deep temporary `node_modules` trees can therefore
be removed without converting a completed provider run to exit code 70.
When a semantic
gate validator is active, OpenCode event projection now selects the latest
handoff satisfying that validator instead of a later generic duplicate without
`quality_gate`. Distribution 0.8.12's durable Git-evidence fix remains active,
so quality/security bundles contain the bounded patch and changed-file snapshots.
OpenCode/Z.AI
GLM-5.3 now fails fast below OpenCode 1.18.18, records sanitized INFO transport
diagnostics, and retains the exact provider/model inventory gate. Every
implementation run is marked as requiring a `security-gate`; legacy acceptance
and a generic quality gate cannot bypass that requirement.

## Boundary

The Knowledge Hub capability path remains instruction-only. Executable bytes
live under one verified user-local runtime; tool scan roots receive only thin
`SKILL.md` adapters.

Release 0.8.42 extends the released 0.8.41 runtime and atomically couples
four components:

- Agent Fleet 0.8.42 with credit-aware Claude last-resort routing, exact Fable
  5.1-only execution, and exact Gemini 3.8 Flash automatic effort routing,
  explicit-only Gemini 3.1 Pro High, legacy Gemini 3.6 and unavailable Cyber
  denial, exact Muse Spark 1.3 enforcement, legacy Muse 1.2 denial,
  exact Claude Fable 5.1 canary routing, profile-bound
  effort, retention/ZDR hard gates, and exact remote Qwen3.8-Flash-Next routing through
  runtime ID `qwen3.8-flash`, exact OpenCode/Z.AI GLM-5.3-Flash security
  review, and one loopback Ollama/Qwen Code red-team runtime shared by both
  local aliases at 16,384 text tokens and provider cost `0 zł`, plus Fleet
  Quality Gate and native OpenCode/Z.AI
  incremental Security Gate, persisted worker handoffs, evidence-based Grok
  handoff validation, safe Antigravity workspace tools, and live shared-pool
  accounting for Qwen/Bailian and native MiniMax M3 execution. Scientific campaigns add a parent-owned Gate0,
  immutable experiment-card bindings, bounded reservations, and auditor-backed
  group acceptance without introducing a second scheduler. Grok runs use the
  native `grok agent stdio` ACP transport. One isolated process owns one native
  session, and every bounded repair prompt stays inside that session until
  completion, parent timeout, a hard transport stop, or the complexity ceiling;
- Model Radar 0.3.5;
- Agent CLI compatibility facade 0.2.4;
- Agent Loop 0.3.0 with L1 report-only, explicit L2-thin, and explicit
  multi-candidate L2-search controllers.

## Local topology

Runtime:

`%USERPROFILE%\.agents\runtime\agent-fleet-suite\current`

Instruction-only adapter roots:

- Codex and Kimi Code: `%USERPROFILE%\.agents\skills`;
- Claude Code: `%USERPROFILE%\.claude\skills`;
- Antigravity: `%USERPROFILE%\.gemini\config\skills`;
- Grok CLI: `%USERPROFILE%\.grok\skills`.

Kimi Desktop receives the same four adapters through its native managed plugin.
Qwen Code is a live contractor CLI target; it does not receive a Fleet skill
adapter because the parent agents invoke it through the shared runtime.
OpenCode is likewise a contractor target. Its Z.AI Coding Plan credential stays
DPAPI-protected and is exposed only to the bounded child process.
MiniMax Code is a contractor target authenticated through its existing
global-region managed login. Fleet forces `MAVIS_REGION=en` for version probes,
deep readiness probes, and child environments; renewal uses exactly
`mcode login --region global`. The Fleet child receives no ambient API keys and
no browser or external extension surface.

## CodeBuddy hy4-preview pin

CodeBuddy Code is a contractor target whose CLI is a router across many
vendors (`gpt-5.6-*`, `glm-5.3`, `kimi-k3`, `gemini-3.5-flash`,
`deepseek-v4.1-flash`, `hy3`, and others). Fleet therefore pins the only
Tencent-owned lineage, `hy4-preview`, as the sole execution profile. Without
that pin, a default alias or fallback would count a foreign vendor model as a
new Fleet executor. Child runs isolate `CODEBUDDY_CONFIG_DIR` to the run
directory, disable memory and telemetry, refuse serve/sandbox/remote-control
and permission-bypass flags, and verify assistant `providerData.model` plus
`requestModelId` from the JSON envelope before accepting identity.
Robert/Hermes is a separate owner-run rollout.

## DeepSeek V4 Pro control boundary

Qwen Code remains the sole provider transport and credential boundary for the
Qwen-plan model alias `deepseek-v4-pro`. Two explicit-only profiles expose
`high` reasoning for bounded advisory, review, and implementation, and `max`
reasoning for read-only advisory or review. They are deliberately absent from
automatic complexity maps until enough accepted outcomes support promotion.

Each Qwen run receives an isolated settings file and exact requested-model
verification. Fleet removes ambient retry and timeout overrides, disables model
fallbacks, bounds retry/tool/context behavior, preserves loop detection, and
fails closed when provider output does not identify the requested model. The
separate DeepSeek Harness developer-preview runtime is neither installed nor
nested: it would create a second agent/tool/session boundary and requires its
own provider configuration rather than inheriting the Qwen subscription.

The four prior Codex payload directories below `.codex\skills` are retired in
the same local transaction to prevent duplicate discovery. Rollback restores
their exact prior state.

## L2-thin boundary

L1 remains the default and is report-only. L2 must be requested consciously and
requires an exact frozen contract hash, clean baseline, detached Fleet worktree,
allowlisted changed paths, different certified maker and reviewer, a
fingerprinted trusted verifier, hard context/tool/token/time/diff budgets,
explicit parent acceptance, and explicit cleanup.

Only Codex and Claude are certified L2 actors in this release. Grok,
Antigravity, Kimi, and Qwen remain fail-closed as L2 actors until their headless
context, write isolation, streaming tool evidence, and handoff behavior pass a
separate bounded certification. No loop may merge, commit, push, deploy,
register a scheduler, or edit the base checkout.

## L2-search boundary

L2-search is separate from L2-thin actor certification. It admits only a maker
that Agent Fleet can execute in `implement` mode with a bounded detached
worktree and a valid structured handoff. Unsupported makers fail one isolated
attempt and cannot weaken the search contract. The auditor must use a different
CLI from every maker, and the optional supervisor must use a third independent
CLI. All actor assessments, quality criteria, hypotheses, runtime identities,
scope, baseline score, improvement threshold, tie budget, stagnation policy,
and resource limits are frozen under one operator-approved hash.

Every hypothesis starts from the same exact HEAD. The search is not cumulative
evolution and does not repair a previous candidate. Provider or verifier failure
records a dead end; baseline/runtime drift, a kill switch, or a hard budget
breach stops the entire search. A verifier-passing candidate receives one Fleet
Quality Gate. Only an accepted candidate that meets the frozen score threshold
may become champion. Equal-score alternates are bounded, and a supervisor note
is advisory, read-only, and consumed once.

Before controller acceptance, the champion Fleet gate must be
`PARENT_ACCEPTED`. Every other auditor-accepted gate, including an accepted
candidate below the baseline, must be `PARENT_REJECTED`; auditor-rejected gates
may remain `NEEDS_REPAIR` or `BLOCKED`. Acceptance records evidence only. It
does not copy, commit, merge, push, deploy, or schedule the champion.

## Integrity and recovery

`package-lock.json` seals the distribution except for the lock itself and
generated cache files. Every adapter embeds the exact runtime payload
fingerprint and verifies it before forwarding. Deployment stages and verifies
all targets before activation, stores backups outside scan roots, refuses
unmanaged collisions without explicit adoption, and writes an append-only
transaction receipt. Active drift blocks rollback. A separate Kimi Desktop
transaction snapshots and restores plugin registry, plugin bytes, and device
identity.

Release 0.6.9 retains marker identity as active state. Planning,
post-activation checks, receipt verification, and rollback guards validate the
manager, package, adapter, tool, and payload fields. A byte-identical payload
with false provenance is repaired instead of being reported as current.

Release 0.6.2 repairs the thin adapters shipped by 0.6.1: their embedded
runtime fingerprint now matches the sealed payload. Package sealing also
validates this invariant so a stale adapter fingerprint cannot be released
again.

Release 0.6.4 made Grok output provider-aware: only terminal
`structuredOutput` is eligible as a handoff, `stopReason` participates in the
run state, internal thought and tool output are never parsed as completion, and
full acceptance is blocked for incomplete transport. Read-only Grok runs use a
`dontAsk` allowlist with memory, MCP, subagents, and web disabled by default.
Verified incomplete evidence is recorded separately as `USEFUL_PARTIAL`.
Release 0.7.2 makes Grok 4.6 the only selectable Grok model for low, medium,
and high complexity. Grok 4.5 remains visible only in historical evidence and
benchmark snapshots; it cannot be selected by the router or an explicit model
override. Reasoning effort, rather than an older model, differentiates the
fast, balanced, and deep profiles.

Release 0.7.3 closes the Quality Gate handoff gap. A Grok terminal response
that losslessly places `integration` and `unresolved_risks` beside
`quality_gate` is normalized only into the exact supplied schema and then
fully validated. Any other missing, malformed, or schema-invalid Grok handoff
receives one format-only continuation in the same native session; the model is
told not to repeat the audit or use tools. This prevents a completed audit from
silently collapsing into `BLOCKED` and dropping the worker's repair iteration.

Release 0.8.0 added OpenCode as a native CLI transport for the paid Z.AI
Coding Plan and exact model `glm-5.3`. The adapter starts `opencode run --pure`
with a deny-by-default inline configuration: repository reads and LSP are
allowed, while writes, shell, web, skills, MCP, subagents, and external paths
are denied. A separate `security-gate` command audits 1..16 coherent worker
runs and requires evidence-bearing CWE findings before bounded repair. The
auditor never edits code or performs parent acceptance. OpenCode model-token
telemetry is retained, but weekly plan points remain unknown rather than being
fabricated from tokens.

Release 0.8.10 requires OpenCode 1.18.18 or newer after repeated native
transport failures on 1.17.15. A bounded `glm-5.3` canary passed on OpenCode
1.18.21 on 2026-08-22. Readiness now rejects stale clients and fatal provider
probe output before spending a worker run, while sanitized INFO logs preserve
future transport diagnostics. Implementation runs persist
`required_gate_kind=security`; only a successful Security Gate can advance them
to parent acceptance.

Candidate 0.8.1 adds explicit-only DeepSeek V4 Pro high/max profiles through
the native Qwen transport, per-run Qwen system settings, bounded retries and
tool/context behavior, disabled fallback, and fail-closed exact provider-model
verification. It adopts useful control-plane ideas without depending on or
executing the separate DeepSeek Harness runtime.

Release 0.6.5 closes the canary findings: Grok native subagents now require
explicit per-run opt-in, the legacy medium/high model map also resolves to 4.6,
and a legacy run without explicit complete transport evidence cannot receive a
full acceptance.

Release 0.6.6 historically removed Grok's hard `--json-schema` constraint after native CLI
evidence showed that the model could terminate with a start/read message while
returning exit code zero and `end_turn`. Grok now runs as a fresh local CLI
session in the delegated workspace. That one-shot/restart transport is retained
here only as release history and is superseded by the 0.8.7 ACP contract below.
Provider `cost_usd` remains reference telemetry rather than an API bill.

Release 0.6.7 removes two stale routing failures. Qwen/Bailian now exposes
verified `LIMITED` profiles for `deepseek-v4-flash-0731` and
`qwen3.8-max` through the native Qwen CLI and one shared quota pool; its dated
provider snapshot is recorded separately as provenance. GLM
5.3 Flash is routed through OpenCode/Z.AI. Its writer
uses sandboxed `auto-edit`, a versioned native-tool denylist, and hard turn,
tool-call, and wall-time ceilings. Antigravity now receives the exact workspace
through `--add-dir`, uses structured streaming output and native file tools,
and no longer advertises unsupported headless shell access. Absolute-path
prompts prevent writes from being redirected into the Antigravity scratch tree.

Release 0.6.8 removes the arbitrary 40-character acceptance threshold for
advisory and review handoffs. A concise Grok result is accepted when its
terminal handoff is complete and includes concrete verification; a first-turn
progress-only `partial` result still receives one native-session continuation.
Antigravity terminal projection unwraps exactly one schema-generated nested
handoff from `summary`, after validating both outer and inner structures.
The router now reads the local Qwen JSONL token ledger without executing a TUI,
deduplicates usage IDs, rolls a timezone-aware seven-day window, and exposes
fresh `quota_before` and `quota_after` evidence for the shared Qwen/GLM/DeepSeek
pool. Qwen alone fails closed when this required pool evidence is missing,
stale, exhausted, or below the declared confidence floor.

Release 0.6.9 fixes native Grok session recovery. Grok's `num_turns` counts
internal turns inside one CLI invocation, not Fleet continuation attempts. A
progress-only first invocation ending with `cancelled`, `max_tokens`, or
`max_turns` now receives exactly one same-session resume. Parent timeout,
non-zero process exit, and tool-budget stops remain non-resumable.

Release 0.7.0 adds Fleet Quality Gate as an outer acceptance layer for
substantive worker runs and groups. A frozen group contract binds criteria,
risk, and a 0..3 repair budget. One read-only auditor may inspect 1..16 current
root tasks, but must use a different CLI and model provider from every worker.
The auditor receives an atomically published bounded evidence bundle with handoffs, patches, and
changed-file snapshots; it cannot write, browse, spawn agents, repair its own
findings, or perform parent acceptance.

Each gate returns a separate item verdict plus one integration verdict.
`REPAIR` dispatches only rejected scope back to the original maker while
preserving `root_task_id` and the existing detached worktree. Incremental
re-audit freezes accepted runs and requires a new run for rejected items.
`accept-group` is the only acceptance path for gated workers, is serialized and
transaction-journaled for recoverable idempotency, and records the parent
decision and model/quota/token evidence.
Standalone Agent Loop L2 keeps its certified internal reviewer; the outer gate
is used when that candidate participates in a wider Fleet group.

Release 0.8.3 closes two native-transport defects observed in production-like
local runs. Grok CLI 1.0.5 maps its `search_replace` tool to the permission name
`Edit`; `acceptEdits` alone cancelled the first write in headless mode. Fleet now
adds only `Edit(./**)` inside the detached worktree and restores exact structured
output after a native canary proved `--json-schema` completes correctly on 1.0.5.
Local Qwen no longer inherits the remote 24-turn/40-tool high ceiling. It uses
high anti-loop guards, honors explicit budgets up to 512 tool calls, and holds a
per-model execution lock around Qwen Code so two callers queue before Docker
instead of colliding on its fixed sandbox-container name.

Release 0.8.4 closes the remaining local implementation transport defect.
Qwen Code re-executes inside Docker for `--sandbox`, where the host-only system
settings path and `127.0.0.1` endpoint were no longer valid. Fleet now mounts a
separate read-only settings file into the container, derives the Docker host
gateway URL from the verified loopback port, and passes only a literal local
dummy key. The vLLM transient unit also uses `Restart=on-failure` with a
five-second delay; deployment itself does not restart an already active unit.

Release 0.8.6 adds Scientific Campaign Mode as a runtime-enforced contract
layer. One parent-owned campaign contract binds Gate 0, experiment-card IDs,
independent run reservations, hardware and data-quality budgets, stop rules,
and the no-merge/no-deploy/no-promotion boundary. `campaign-validate`, `run`,
`quality-gate`, `quality-repair`, and `accept-group` remain the Fleet execution
and acceptance paths; interrupted recurrence uses Agent Loop's existing
operator-confirmed recovery. A low-cost independent reviewer may cover a run group;
repairs are limited to rejected scope, and a costly parent model runs only
after the first gate. Parent platform copies and the package lock are resealed
from the reviewed payload before transactional deployment.

Release 0.8.7 replaces Grok's restarted one-shot transport with one native ACP
process and one session. Fleet references the existing cached subscription auth
from an isolated `GROK_HOME`, disables ambient skills, rules, hooks, memory,
subagents, web and MCP sources, denies unknown ACP reverse requests, and enforces strict
workspace tools, absolute tool and repair ceilings, a parent deadline, process
tree termination, and exact model identity across the full session. Only
terminal text or structured output becomes a handoff; thought content and raw
stderr are never persisted. Bounded repair prompts remain in the same session.
On Windows, a sealed launcher waits behind a private file gate until its process
is assigned to a kill-on-close Job Object; closing that job terminates every
descendant even if the direct Grok parent exited first.

## Deliberate exclusions

- no provider-account, approval-mode, model-entitlement, or secret-value changes;
- no production deployment, Git remote operation, forced live-service restart,
  or merge;
- no automatic loop scheduling or L3 behavior;
- no Qwen application, Robert, or Hermes installation in this Windows rollout;
- no DeepSeek Harness installation, plugin persistence, nested runtime, or
  separate DeepSeek API credential;
- no pilot fixture, project-specific verifier, provider config, session, or
  runtime memory inside the package.

## 0.9.71 - DeepSeek V4.1 Flash

- Owner decision 2026-10-13: DeepSeek V4.1 Flash is active and replaces `deepseek-v4-flash-0731` in the Qwen low lane (all modes).
- Native canary 2026-10-13: Qwen Code 0.25.1, `-m deepseek-v4.1-flash`, answer OK, provider stats report exactly `deepseek-v4.1-flash`.
- The 0731 profile stays LIMITED, so explicit runs keep working during the 14-day retirement window. In the catalog it is `deprecated` with `replacement_id` and `retire_after` 2026-10-27.
- Same channel and quota pool as before: Qwen Code / Alibaba Coding Plan.

## 0.9.72 - bounded state reader (2026-10-13)

`adaptive.read_state_json` reads at most `min(max_bytes, st_size) + 1` bytes and rejects data longer than the
lstat size (file grown after stat) as `corrupt-cache-ignored`. The previous `read(max_bytes + 1)` allocated the
full cap for every small state file; with ~11k usage records `read_usage` exceeded 30 s and every Fleet preflight
lost minutes. Candidate from the Codex Chrome-native session (patch a9dfbb12), independent Grok 4.7 ACCEPT
run-20261012T233134Z-7e15e8e151 on the identical baseline 944d356e (unchanged 0.9.69-0.9.71).

## 0.9.73 - Muse 1.4.6 concatenated final reports (2026-10-13)

Muse 1.4.6 writes every final report of a run into the single `run.terminal.completed` text. After a wind-down
(step cap reached) the text is two complete handoff objects back to back. 0.9.70-0.9.72 rejected that text as a
whole, so 10 of 12 failed Muse implement runs on 2026-10-13 (wonder-1.0 and spark-1.3) were FAILED although the
model returned valid reports. `muse_last_of_concatenated_reports` accepts only a text made exclusively of two or more
whole top-level handoff objects separated by whitespace and takes the last one (latest state); the parent
`handoff_validator` still applies to it. Prose, fences, trailing text, nested history and any non-handoff segment
keep failing closed. The run records `handoff_projection_source = muse.native-terminal.recovered-muse-concatenated-final-last`.
Replay on the 12 failed runs of 2026-10-13: 9 recovered (8 partial, 1 blocked); 3 still refused correctly
(truncated JSON, non-schema key `design_review`, provider exit 1).

## 0.9.74 - Gemini 4 Argon as Director (2026-10-13)

Owner decision 2026-10-13: Gemini 4 Argon (Antigravity agy 1.3.4: `gemini-4-argon-low/medium/high`) joins Fleet as
tier D. Profile `antigravity-gemini-4-argon-high` is LIMITED: reachable through an explicit `--model` route and the
Graph tier routing; the automatic complexity map is unchanged (Flash High). Conservative priors, no local benchmark.
The bundled `model-catalog.json` lists Argon as `candidate`; the live agent-graph catalog has it `active`.

## 0.9.75 - Qwen native budget stop is not a provider failure (2026-10-13)

Qwen Code 0.20.0 ends a run with native exit 55 when the tool-call/wall budget is exhausted and 53 at the turn
limit. Without a handoff Fleet reported this as `PROVIDER FAILURE` (RSI Helpdesk ticket HD-20261013-9b4217:
26 runs in 18 workspaces), which sent callers to switch models instead of narrowing the task or raising an
existing allowance. `fleet.py` now recognises the budget stop in the diagnosis, `transport_outcome` and
`completion_reason`, and propagates it to the run record and the attempt metadata. Semantics are unchanged: a run
without a valid handoff stays FAILED, a valid report after the stop stays PARTIAL; parent timeout keeps priority;
real errors and cancellations are not budget stops; identity checks, limits, fallback and retry are not relaxed.

## 0.9.76 - Grok tool-budget stop is not a provider failure (2026-10-13)

The Grok ACP adapter stops a run with its own exit 125 when the per-run tool-call budget is exhausted
(`grok_acp.GrokAcpError(..., exit_code=125)`, `tool_budget_exceeded=True` in the child record). Without a handoff
Fleet reported this as `PROVIDER FAILURE (not a handoff problem): the provider process exited with code 125` (RSI
Helpdesk ticket HD-20261013-dbb9a5: 9 runs in 6 workspaces on 0.9.69-0.9.74, every record `tool_budget_exceeded=true`
and tools = max+1), so callers switched models instead of narrowing the task or raising `--max-model-tool-calls`.
`fleet.py` now recognises the attested budget stop (adapter grok, exit 125 and `tool_budget_exceeded is True`) in the
diagnosis (`GROK TOOL BUDGET EXHAUSTED`), `transport_outcome` (`grok-tool-budget-exhausted`) and `completion_reason`,
and propagates it to the run record and every attempt. Semantics are unchanged: a run without a valid handoff stays
FAILED, a valid report after the stop stays PARTIAL; parent timeout keeps priority; a bare exit 125 without the
adapter flag, other adapters and other exit codes keep the generic diagnosis; the exact-model identity gate, the tool
limits, same-session repairs, fallback and retry are not relaxed. Built on 0.9.75 (e1d9076f) plus the two-file
candidate from the RSI worktree rsi-grok-dbb9a5-20261013 (fleet.py 7999a6c5, test_grok_budget_diagnosis.py 82f6325d).

## 0.9.77 - Antigravity headless permission denial: named target and one same-conversation resume (2026-10-14)

Headless `agy` (print mode) cannot answer a permission prompt. When the model's native file tool (`view_file`,
`grep_search`, `list_dir` or a write) targets a path outside the `--add-dir` workspace, agy auto-denies it, ends the
turn at once and the terminal result is `status SUCCESS`, `response ""`, `denied_actions [read_file (ViewFile)]` with
process exit 0 (agy 1.3.4 stderr: "no output produced - a tool required the read_file permission that headless mode
cannot prompt for, so it was auto-denied"). Fleet 0.9.69-0.9.76 reported `EMPTY_RESPONSE ... denied_actions:
read_file (ViewFile)` and FAILED the run without naming the path, which lives only in the `step_update` ERROR event;
every step before the denial (up to 31 steps with diffs in `git_evidence`) was lost (RSI Helpdesk HD-20261013-f2a859:
6 FAILED runs in 4 workspaces in 24 h, exit 0, terminal SUCCESS; reproduced synthetically in the stack worktree).

`fleet.py` now (1) parses the permission-denial ERROR events (`antigravity_denied_tool_paths`) and appends
`headless agy auto-denied the permission prompt for: view_file(<path>)` to the EMPTY_RESPONSE diagnosis, exposing
`denied_paths` in the projection; (2) resumes the same native conversation exactly once
(`antigravity_denied_read_continuation_decision`, `antigravity_continuation_command`: identical argv plus
`--conversation <id>`, a repair prompt that forbids retrying the denied target and asks for the final handoff with
`partial` when the input was essential, and a `--print-timeout` bounded by the remaining provider budget with a 120 s
floor); the first attempt stays in evidence as `initial`, the resume is `native-session-continuation`,
`continuation_trigger` is `antigravity-permission-denied`, `provider_session_id` records the conversation id;
(3) sets `completion_reason` to `completed-after-same-session-repair`, `antigravity-permission-denied-after-same-
session-repair` (resume still empty) or `antigravity-permission-denied` (resume skipped: missing conversation id or
insufficient budget, recorded in `continuation_skip_reason`); (4) adds one rule to the Antigravity worker preamble
that out-of-workspace reads are auto-denied and end the turn. Permissions are never widened: no
`--dangerously-skip-permissions`, no allow-rules, same `--mode` and `--sandbox`; no second resume; FAILED without a
handoff, PARTIAL with a partial handoff, the exact-model identity gate, parent timeout priority, other adapters and the
Grok continuation path are unchanged. Built on 0.9.76 (4f4ad882) plus `fleet.py` and the new
`test_antigravity_denied_read_continuation.py`; the separate Antigravity stdin candidate (HD-20261013-794f63) is not
merged here. Independent review repairs (Codex Sol, rounds 1-2 REJECT): the exact-model identity gate evaluates the native
`init` of every attempt separately (`antigravity_attempts_model_identity`): the run is verified only when each attempt
is verified, a mismatched or missing identity in any attempt fails the run and the failing attempt is named; the run-level token usage of a
resumed run is the last attempt alone because agy reports conversation-cumulative usage in the resumed result
(5675 + 6406 = 12081 measured); the resume requires a truly empty terminal response (None, blank string or empty dict),
not an unsupported non-empty type; denied labels are bounded to 240 characters; a non-zero exit of the resume keeps
`process-exit-<n>`; the remaining budget is re-measured right before the resume starts and the first attempt is
recorded only when the resume actually launches.

## 0.9.78 - Mistral native token budget stop is not a provider failure (2026-10-14)

Native Vibe 2.26.1 (`mistral-large-4`) stops a run at its cumulative token budget (`--max-tokens 262144`) with a
controlled exit 3 and writes a native export with `schema_version 1`, `outcome token_limit`, `exit_code 3` and
`error null`. Fleet 0.9.69-0.9.77 diagnosed the bare exit code as `PROVIDER FAILURE (not a handoff problem): the
provider process exited with code 3` with `transport_outcome exit-code-3` and `completion_reason process-exit-3`, so
callers switched agent or model instead of narrowing the task (RSI Helpdesk HD-20261013-3eef98: 12 FAILED runs in
24 h across several workspaces; three permitted Helpdesk exports confirmed the native `token_limit` outcome).

`mistral_runtime.mistral_token_budget_exhausted(export, exit_code)` classifies only the pinned native export: exact
integer exit 3, `schema_version` integer 1, `vibe_version` equal to `MISTRAL_NATIVE_VERSION`, export `exit_code`
equal to the process exit code, `outcome == "token_limit"` and an explicit `error: null`. Model text, stdout/stderr,
a missing or malformed export, a foreign version, a different outcome or a non-null error keep the generic provider
diagnosis; other adapters and other exit codes never gain the label. `provider_failure_reason` and
`provider_transport_outcome` accept `native_export`; `execute_single` passes the run-level `mistral_export` and each
attempt its own export. A matching stop reports `MISTRAL TOKEN BUDGET EXHAUSTED ...` as the diagnosis,
`transport_outcome {status failed|partial, reason mistral-token-budget-exhausted}` (partial only with a valid
handoff) and `completion_reason mistral-token-budget-exhausted` on the run record and every attempt. Parent timeout
wins over the budget cause. The native token limit (262144), the projector, exact-model identity gate, run status
(FAILED without a valid completion), retry/continuation behaviour and quota handling are unchanged; no work is
recovered automatically.

Built on 0.9.77 (72beb3a4) in the RSI worktree rsi-mistral-3eef98-release-20261014; the source change is the
candidate accepted on 0.9.76 (Codex maker, Grok 4.7 review ACCEPT run-20261014T001425Z-cf8e9289a3) ported
unchanged (identical `mistral_runtime.py` and test bytes, the same seven `fleet.py` hunks).

## 0.9.105 - Mistral native session token budget follows the step bound (2026-10-18)

Vibe's `--max-tokens` is a cumulative session ceiling over `session_prompt_tokens + session_completion_tokens`
of every request (vibe/cli/headless_run.py `observe_usage`, vibe/app_server/models.py `session_total_llm_tokens`).
Fleet disables native compaction, so each agentic step resends the full context (10-22k tokens observed) and the
fixed 262144 of 0.9.78 ended bounded reviews and implementations after 13-27 tool calls, far below the 48-96
tool-call budget and the 64-step turn limit of the same run (RSI Helpdesk HD-20261015-a9a428 and four sibling
tickets; 0.9.93 and 0.9.103 runs with `completion_reason mistral-token-budget-exhausted`, `tool_budget_exceeded false`).

`mistral_runtime.session_token_budget(max_tool_calls)` maps the run's step bound to the budget:
`max(MISTRAL_MIN_SESSION_TOKENS, min(MISTRAL_MAX_TOKENS, min(MISTRAL_MAX_TURNS, max_tool_calls + 1) *
MISTRAL_SESSION_TOKENS_PER_STEP))` with floor 262144 (the former fixed value), 16384 tokens per step and ceiling
1048576 (64 steps). `build_command` derives `--max-tokens` from the bound tool policy's `limits.max_tool_calls`
when the caller passes none; advisory runs without a policy keep the floor; an explicit `max_tokens` is still an
int in `1..MISTRAL_MAX_TOKENS`; malformed policy limits raise `MistralRuntimeError`. The token_limit diagnosis,
identity gate, tool caps, max_turns, time limit, retries, `fleet.py` and `mistral_tools.py` are unchanged, so a run
that still exhausts the derived budget keeps `MISTRAL TOKEN BUDGET EXHAUSTED` and FAILED/PARTIAL semantics.
The per-run ceiling rises at most 4x on the existing Vibe Code Pro allowance; real quota remains unknown and the
availability/recheck gates are unchanged.

## 0.9.107 - MiMo shutdown abort is not a provider failure; rejections are diagnosed from the assistant text (2026-10-18)

MiMo 0.1.15 logs `ERROR service=session.processor ... error=Aborted process` when the
instance is disposed while its internal `checkpoint-writer` subagent is still
streaming. That happens after the model's turn ended with an answer on stdout and
the process exits 0. `PROVIDER_FAILURE_STDERR_MARKERS = ("aborted",)`, introduced for
an aborted CodeBuddy run with no stdout, matched that line and reported every exit-0
MiMo run with a rejected handoff as "PROVIDER FAILURE (not a handoff problem): stderr
reported 'aborted'". 7 of 19 COMPLETED stack runs on 2026-10-17/18 carry the same line.
`mimo_runtime.mimo_shutdown_abort_only(stdout, stderr)` recognises the pattern only
when the native stream reports a completed terminal and every abort line is that
exact shutdown line; then `_provider_failure_reason_core` drops the marker for MiMo.
Other adapters, an abort before a completed answer, a native error and any other
abort wording keep the marker.

The MiMo projection now returns `candidate_text` (the joined assistant text the parser
judged). Without it `handoff_diagnosis_text` fell back to the raw transcript, where
tool results dominate: the diagnosis reported "MALFORMED JSON ... context: '{ 29: ...'"
over the line-number prefixes of a file the model read, and skipped the schema-not-data
category because raw transcripts always echo the prompt schema. The real cause of the
evidence runs - the model answering with the handoff schema itself - is now named as
SCHEMA RETURNED INSTEAD OF DATA. Rejections, statuses, identity, limits and retries are
unchanged (HD-20261018-1abe4d).

## 0.9.108 - Grok ACP broker trusts only its own per-session proxy alias (2026-10-18)

`grok_acp.run_session` launches the grok CLI with `cwd=<proxy_root>` (default
`C:/ProgramData/AgentFleet/grok-acp/<run_id>`), where `<proxy_root>/project` is the
junction `_prepare_runtime` creates to the assigned workspace. The CLI therefore
requests SearchReplace/Write with the absolute alias path, not `project/<...>` and
not the physical workspace path that 0.9.107 mapped; the first attested 0.9.107 run
(run-20261018T220521Z-fa8370eecd) was cancelled with `path_drive_absolute` and 0.9.107
was rolled back. `run_session` now passes `proxy_project_root=proxy_root / "project"`
to `_AcpConnection`, which forwards it to `_permission_allow_once_option` and
`classify_permission_denial`. `_resolve_project_segments_with_source` maps a
drive-absolute path first against the physical workspace root and only then against
the alias, with identical lexical rules, and reports which root matched.
`_project_local_write_allowed` accepts alias-derived segments only after
`_alias_trusted` confirms the alias is an existing directory, is a filesystem
link/junction, and resolves (or `samefile`s) to exactly the resolved physical
workspace root; every existing guard then runs on the physical root. Nothing else
under `C:/ProgramData`, other run ids, plain directories, foreign links, the alias
root alone, traversal, UNC/device, other drives or non-Windows hosts is accepted,
and `proxy_project_root=None` keeps 0.9.107 behaviour. Diagnostics expose alias
paths only as the redacted `project/<...>` form. The fake ACP server gains the
`permission-write-allow-alias` and `permission-write-deny-alias-sibling` scenarios
that emit the real absolute alias from the server cwd (HD-20261013-037b0e).

## 0.9.109 - Grok ACP: one bounded same-session resume after a broker-refused permission (2026-10-19)

grok CLI 1.0.41 ends the prompt turn with `stopReason=cancelled` whenever the Fleet
broker refuses a `session/request_permission`. Live controls on the attested 0.9.108
broker (run-20261019T033309Z-32197c2aad, run-20261019T033344Z-ed663f7ce3) and on a
copy that selected the agent's own `reject_once` option instead of answering
`cancelled` (run-20261019T033458Z-34f712aab8, permission mode `default`) all ended
the turn as cancelled after the single refused Bash request: the protocol answer is
not the lever, the provider behaviour is. Since 2026-10-13 more than 80 implement and
review runs ended PARTIAL/cancelled without a handoff this way, each with exactly one
refused permission (`shell_command_not_allowed` or a `path_*` shape), the largest
spending 4.5M tokens (RSI HD-20261013-bb5973 and the related open Grok tickets).

The same native session does accept a further `session/prompt`: a control that
resumed once with a short refusal notice (run-20261019T033653Z-73ce6e9774) finished
the task with `end_turn`, the edit applied and a valid terminal handoff.
`fleet.py` already carried `GROK_PERMISSION_DENIED_RETRY_PROMPT` and mapped the
reason `permission-denied-retry`, but `grok_continuation_reason` returned `None` for
every cancelled envelope: the cumulative `permission_requests_denied` cannot tell
whether the refusal happened in this prompt, and an owner STOP must stay terminal.

`grok_acp._AcpState` now keeps `prompt_permission_requests_denied`, reset in
`begin_prompt()` and incremented together with the cumulative counter; the native
envelope exposes it as `_fleet.permission_requests_denied_in_prompt`. The protocol
answer, the classifier and the allow rules are unchanged. `grok_continuation_reason`
returns `permission-denied-retry` for a cancelled envelope only when that per-prompt
counter is a positive integer and fewer than `GROK_PERMISSION_DENIED_RESUME_LIMIT`
(2) earlier `permission-denied-retry` continuations are recorded; every other
cancelled envelope (no `_fleet`, cumulative count only, limit reached, owner STOP)
still returns `None`, and the existing `max_additional_prompts` and tool budgets keep
bounding the session. The retry prompt now names shell or file-write denials and
forbids retrying the refused request. The Grok projection names a refusal-caused
cancellation with the count and the redacted reason tokens instead of "native
cancellation source is unknown"; without the per-prompt counter the old text stays.
The fake ACP server gains `permission-deny-then-complete` (prompt 1 refused ->
cancelled, prompt 2 completes) and `test_grok_permission_denied_resume.py` (14 tests)
is RED 9/14 on pristine 0.9.108 and GREEN on 0.9.109.

## 0.9.110 - one bounded same-session MiniMax resume when the native validator refused a final answer that carried no handoff (2026-10-19)

mcode 0.2.4 `exec --output-schema` ends the turn with `STRUCTURED_OUTPUT_INVALID`
(`retryable: true`) whenever the model's final answer is not a valid instance. Since
0.9.93 Fleet re-receives the final `agent_message` from the stream, which recovers the
fenced or prose-wrapped objects (9 of 14 COMPLETED MiniMax runs on 0.9.109 on
2026-10-19). The residual failure of HD-20261015-afe374 is a final message that holds
no object at all: the model writes the files, says "Returning the handoff now." and
stops. `mcode exec --session <sessionId>` resumes that session in a new process and
keeps its context (canary 2026-10-19: a code word stored in turn 1 came back in a
schema-validated turn 2).

`minimax_missing_handoff_continuation_decision` triggers one resume only for the
exact managed MiniMax-M3 `failed`/`STRUCTURED_OUTPUT_INVALID` envelope (process exit
4, not timed out) whose projection yields no handoff; it is skipped with a reason
when the envelope has no `sessionId` or fewer than `MINIMAX_CONTINUATION_MIN_SECONDS`
remain. `minimax_continuation_command` keeps the exact argv (config, cwd, model,
permission, stream-json, output-schema) and only adds `--session`, lowers
`--max-steps` to `MINIMAX_CONTINUATION_MAX_STEPS` and sets the remaining `--timeout`;
`--continue` is refused. The resume prompt (`MINIMAX_HANDOFF_REPAIR_PROMPT` plus the
Fleet validation diagnosis) asks for exactly one handoff object and forbids new work.
The first attempt stays in the evidence as attempt 1 (`kind: initial`,
`continuation_reason`), the resume is attempt 2 (`native-session-continuation`),
`continuation_count` is 1, `provider_session_id` is the mcode session and a successful
resume ends `completed-after-same-session-repair`. Timeouts, step budgets, other error
codes, foreign models and a resume that fails again keep their existing verdicts; no
permission or tool is widened.
