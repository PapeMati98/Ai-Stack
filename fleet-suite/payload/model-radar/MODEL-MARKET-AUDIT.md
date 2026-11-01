---
schema_version: 1
last_verified_at: 2026-09-03T08:22:26Z
registry_sha256: 99b0f2b5da291ab06fd291f85addc7d22d756bb7991dcc6262090c2adf97a7f2
registry_verified_at: 2026-09-06T21:01:26Z
valid_for_days: 7
status: reviewed
---

# Model Market Audit

This snapshot controls model characterization for the local Agent Fleet. It is
valid through 2026-09-10T08:22:26Z unless a provider changes the local catalog
or authentication state earlier.

## Historical routing snapshot (as of 2026-09-03)

| Contractor CLI | Verified models | Best current use | Access state |
| --- | --- | --- | --- |
| Claude Code 2.1.257 | exact `claude-fable-5-1`; Sonnet 5; legacy Fable 5 removed | Fable 5.1 medium for advisory/review, high for demanding review/implementation, xhigh for hardest advisory only; low remains explicit A/B; no automatic max; entire Claude lineage is automatic last resort while owner credit mode is active | verified subscription and exact-model native canary |
| Kimi Code 0.28.1 | local alias `k3` (K3/K3 Max); legacy K2.7 excluded | frontend/UI, visual inputs, large-context review | verified subscription |
| Codex CLI 0.153.4 | GPT-6 Astra; GPT-5.6 Sol, Terra, Luna | Astra automatically eligible for high architecture/advisory/review; Sol for high implementation, Terra for medium, Luna for low | native CLI version checked; Astra remains LIMITED; role-map regression tests pass; release canary pending |
| Antigravity CLI 1.1.25 | exact `gemini-3.8-flash-low`, `gemini-3.8-flash-medium`, `gemini-3.8-flash-high`; Gemini 3.1 Pro High explicit-only | Gemini 3.8 Flash low/medium/high for automatic fast multimodal, coding and agent work; 3.1 Pro High only for deliberate specialist selection | verified subscription, authenticated native inventory and bounded exact-model medium canary; Flash Cyber unavailable |
| Grok CLI 1.0.4 | Grok 4.6; Grok 4.5 available but excluded from routing | fast engineering, debugging, adversarial review, and current research | verified subscription and authenticated native model inventory |
| Qwen Code 0.20.0 | `qwen3.8-flash` (Qwen3.8-Flash-Next); canonical Max runtime ID `qwen3.8-max`; `deepseek-v4-flash-0731` | DeepSeek for fast volume, Flash-Next for medium work, Qwen3.8-Max through `qwen3.8-max` for high architecture/review/implementation; provider snapshot such as `qwen3.8-max-0902` is provenance only; GLM 5.2 is not an automatic Qwen Code route | existing shared plan; provider upgrade notice effective 2026-09-05; snapshot remains subject to native identity gate |
| OpenCode 1.18.21 / Z.AI | `glm-5.3-flash` low/high/max; legacy `glm-5.3` explicit-only | incremental read-only security review of coherent implementation batches | verified Coding Plan key, native inventory, and bounded exact-model canary; versions below 1.18.18 blocked; three default Flash profiles plus three explicit compatibility profiles |
| Muse Code 1.0.1 | exact `muse-spark-1.3`; legacy 1.2 and every Contributor variant prohibited | bounded implementation, large-context review, and an additional independent Meta lineage | verified local binary, fresh catalog, official 1.3 release, and exact-identity native canary; `LIMITED` pending accepted 1.3 outcomes |

## Codex CLI exact-ID update — 2026-09-23

A fresh canary report records successful runs on Codex CLI 0.156.1 for exact
runtime IDs gpt-6-luna and gpt-6-sol. In Agent Fleet Suite 0.9.20, Luna
routes low-complexity work; Sol routes medium-complexity and high
implementation work; Astra stays mapped to high advisory/review. The Luna/Sol
profiles remain LIMITED with conservative unbenchmarked priors. Dedicated
security review remains on ACTIVE GPT-5.6 Sol because GPT-6 Sol has zero
security-review capability. This updates the dated Codex availability status
only; the expired market table above remains historical, and no benchmark,
quota, or billing conclusion is drawn from the canaries.

All remote Qwen models consume one shared Coding Plan pool. Unknown remaining
headroom is neutral; a confirmed exhaustion or provider 429 blocks the whole
pool until reset without changing stable capability scores.

## Evidence and uncertainty

- Anthropic released Fable 5.1 on 2026-09-01 under exact ID
  `claude-fable-5-1`, with adaptive effort from low through max. A local native
  canary on Claude Code 2.1.257 returned the exact model and expected output.
  Sonnet remains the routine low-complexity option. No executable Fable 5 alias
  remains in Fleet.
- CursorBench 3.2 reports substantially better score/token efficiency than
  Fable 5, but external benchmark deltas remain supporting evidence. Local A/B
  evidence plus exact native identity support `LIMITED` medium/high/xhigh
  routing; low remains `CANARY` and max remains unavailable to auto-routing.
- Anthropic documents Fable 5.1 as ZDR-eligible, not ZDR-by-default. Ordinary
  retention remains 30 days unless the account has an explicit ZDR agreement,
  so sensitive routing requires current account-scoped evidence.
- Anthropic documents that Sonnet 5 uses an updated tokenizer and that the same
  input can map to roughly 1.0-1.35x more tokens depending on content type.
  This is a provider-documented baseline, not a complete explanation for local
  retry loops, excess tool use, or poor assignment efficiency.
- OpenAI documents Sol as flagship, Terra as balanced, and Luna as the
  cost-sensitive high-volume tier.
- Google announced Gemini 3.8 Flash on 2026-09-02 and documents exact API ID
  `gemini-3.8-flash`, a 1,048,576-token input limit, 65,536-token output limit,
  low/medium/high thinking, agentic coding gains, HLE 54.9, and an introductory
  price of USD 0.75/M input and USD 3.75/M output tokens through 2026-12-31.
  Those speed, benchmark and price statements are provider evidence, not local
  measurements or proof of subscription economics.
- Authenticated Antigravity CLI 1.1.25 inventory on 2026-09-03 exposed exact
  effort IDs `gemini-3.8-flash-low`, `gemini-3.8-flash-medium`, and
  `gemini-3.8-flash-high`. A bounded read-only medium canary completed with
  provider duration 17.995 seconds, 22.364 seconds local wall time, 79,343
  input tokens, 2,827 output tokens, 2,557 thinking tokens and 61,210 cache-read
  tokens. This is one local runtime sample, not a comparative latency benchmark.
- Gemini 3.8 Flash Cyber is a separate capability available through Google's
  Fairwind trusted-defender program. It is absent from the local Antigravity
  inventory, so every Cyber spelling remains unavailable and fails before
  launch. Gemini 3.1 Pro High remains explicit-only pending comparable local
  acceptance evidence; all Gemini 3.6 profiles are retired from routing.
- The authenticated Grok CLI 1.0.4 inventory on 2026-08-17 returned
  `grok-4.6` as default and `grok-4.5` as an older available model. The local
  subscription boundary does not justify spending work on the older model, so
  all Grok execution profiles now use 4.6 and an explicit 4.5 override fails.
  Authentication remains volatile, so Agent Fleet must repeat the deny-pattern
  readiness gate before every route and immediately before execution.
- Kimi's official configuration docs use `kimi-code/k3`; the local managed
  profile exposes alias `k3`. K2.7 remains a legacy entry and is never routed.
- QwenCloud identifies the new product as Qwen3.8-Flash-Next while its official
  API and Qwen Code runtime ID is exactly `qwen3.8-flash`. The string
  `qwen3.8-flash-next` is not a runtime ID. A provider notice dated 2026-09-05
  moves the rolling Max endpoint behind the canonical runtime ID `qwen3.8-max`
  to snapshot `qwen3.8-max-0902` with unchanged billing and the same 1M context.
  Fleet invokes the alias, records the snapshot as provenance and requires
  provider identity before
  execution; it does not route Qwen Code to GLM 5.2. Earlier pre-upgrade 404
  observations are retained as historical evidence, not as the current route.
- Antigravity read and writer canaries proved that `--add-dir` plus an
  absolute target path keeps native `view_file`/`write_to_file` operations in
  the delegated workspace. Headless `command` remains unavailable and is not
  advertised as a profile tool.
- OpenCode 1.18.21 listed exact model `zai-coding-plan/glm-5.3-flash` after Z.AI
  rolled GLM-5.3-Flash out to Coding Plan users. The prior bounded `glm-5.3`
  canary completed on 2026-08-22 after 1.17.15 repeatedly failed at transport.
  A bounded `glm-5.3-flash` canary completed on 2026-08-29 after the isolated
  OpenCode config pinned the new model instead of relying on a stale bundled
  catalog. Fleet constrains both generations to
  review-only operation and treats security capability as a local routing prior
  pending accepted outcome samples. Z.AI Coding Plan points are not inferred
  from OpenCode model-token telemetry.
- Meta announced Muse Spark 1.3 on 2026-09-02 and reports about 20% fewer tool
  calls and 25% fewer tokens than 1.2, along with faster execution, fewer
  unnecessary turns, stronger long-horizon instruction following, and better
  prompt-injection resistance. These are vendor claims, not local benchmark
  results; max reasoning is not yet available.
- Muse Code 1.0.1 is installed in WSL Ubuntu. Its provider catalog refreshed on
  2026-09-03 exposes exact model `muse-spark-1.3` with a 1,007,997-token context
  window. A native no-write/no-shell/no-web JSONL canary returned provider
  `meta` plus exact model `muse-spark-1.3`. Capability priors therefore remain
  conservative and `LIMITED` until accepted 1.3 task outcomes accumulate.
- The separate `muse-spark-1.3-contributor` catalog entry permits user content
  to be used for product improvement, and the refreshed catalog currently marks
  that unsafe variant as its default. It also still marks non-Contributor 1.2
  as `is_current`, so Fleet deliberately trusts neither catalog hint. Every
  Contributor spelling, including the legacy 1.2 Contributor model, is
  categorically denied across profile loading, overrides, invocation,
  fallback, retry, resume, and post-run identity verification. Non-Contributor
  `muse-spark-1.2` is also legacy and rejected before process launch. The
  2026-09-02 review and detached-worktree implementation canaries used 1.2 and
  remain transport/sandbox evidence only; they are not presented as 1.3
  capability evidence.
- Vendor capability claims are not local benchmark results. Agent Fleet fit
  weights remain empirical routing preferences and require repeated task data
  before adjustment.

## Primary sources

- https://research.meta.ai/blog/introducing-muse-spark-1-3
- Local Muse Code 1.0.1 `--version`, `exec --help`, provider catalog, and native JSONL canary evidence captured through 2026-09-03
- https://github.com/meta-models/meta-model-cookbook/tree/main/04_muse_code/03_staged_approvals
- https://github.com/meta-models/meta-model-cookbook/tree/main/04_muse_code/04_contained_execution

- https://platform.claude.com/docs/en/models/fable-5-1/overview
- https://platform.claude.com/docs/en/build-with-claude/prompt-engineering/prompting-claude-fable-5-1
- https://privacy.claude.com/en/articles/8956058-i-have-a-zero-data-retention-agreement-with-anthropic-what-products-does-it-apply-to
- https://cursor.com/cursorbench
- https://www.anthropic.com/news/claude-sonnet-5
- https://developers.openai.com/api/docs/guides/latest-model
- https://developers.openai.com/api/docs/models
- https://moonshotai.github.io/kimi-code/en/configuration/config-files.html
- https://moonshotai.github.io/kimi-code/en/release-notes/changelog.html
- https://ai.google.dev/gemini-api/docs/models
- https://ai.google.dev/gemini-api/docs/changelog
- https://blog.google/innovation-and-ai/models-and-research/gemini-models/3-8-flash-and-3-8-flash-cyber/
- https://deepmind.google/models/gemini/flash/
- https://x.ai/news/grok-4-5
- https://docs.x.ai/developers/models/grok-4.5
- https://github.com/QwenLM/qwen-code
- https://qwen.ai/blog?id=qwen3.8-flash-next
- https://www.qwencloud.com/models/qwen3.8-flash
- https://qwenlm.github.io/qwen-code-docs/en/users/features/headless/
- https://qwenlm.github.io/qwen-code-docs/en/users/configuration/auth/
- https://z.ai/blog/glm-5.3-flash
- https://huggingface.co/zai-org/GLM-5.3-Flash
- https://opencode.ai/docs/cli/
- https://opencode.ai/docs/config/
- https://opencode.ai/docs/permissions/

## Update log

- 2026-09-03: Replaced automatic Antigravity Gemini 3.6/3.1 routing with exact
  Gemini 3.8 Flash low/medium/high IDs after authenticated inventory and a
  bounded medium canary. Kept Gemini 3.1 Pro High explicit-only, retired all
  Gemini 3.6 routes, and recorded Flash Cyber as unavailable pending separate
  Fairwind entitlement and native-inventory proof.
- 2026-09-03: Replaced the only routable Muse model with exact
  `muse-spark-1.3`; legacy `muse-spark-1.2` now fails before process launch.
  Preserved the non-waivable Contributor deny across both generations and
  verified the fresh local catalog plus an exact-identity native 1.3 canary.
- 2026-09-05: Qwen provider notice moved the rolling Max endpoint behind the
  canonical runtime ID `qwen3.8-max` to snapshot `qwen3.8-max-0902` without a
  pricing change. Fleet invokes the alias, records the snapshot as provenance
  and rejects automatic Qwen Code GLM 5.2.
- 2026-09-01: Added Muse Code 1.0.1 and exact safe model
  `muse-spark-1.2` as a conservative `LIMITED` profile. Added a categorical,
  non-waivable deny for every Contributor spelling and transport path; local
  binary/catalog readiness is verified while the native canary remains a
  release gate.
- 2026-08-29: Added Qwen3.8-Flash-Next under its exact QwenCloud/Qwen Code
  runtime ID `qwen3.8-flash`; rejected the invented ID
  `qwen3.8-flash-next`; retained `qwen3.8-max` for high-complexity work. Added
  exact OpenCode runtime `zai-coding-plan/glm-5.3-flash` as the default
  security auditor and retained GLM-5.3 as explicit-only compatibility. A
  bounded native canary verified both runtime log identity sources and the
  read-only transport.
- 2026-08-22: Raised the OpenCode floor to 1.18.18 after repeated 1.17.15
  transport failures. Verified 1.18.21 with an exact `glm-5.3` native canary;
  added fail-fast version and fatal-probe gates plus mandatory Security Gate
  acceptance for implementation runs.
- 2026-08-18: Added native OpenCode 1.17.15 with exact Z.AI Coding Plan model
  `glm-5.3` as a review-only incremental security auditor. Recorded DPAPI
  credential readiness, native inventory, one bounded canary, and separate
  model-token versus plan-point semantics.
- 2026-08-17: Authenticated Grok CLI 1.0.4 reported Grok 4.6 as its default.
  Removed Grok 4.5 from every selectable execution profile while retaining old
  benchmark records as historical evidence only.
- 2026-08-16: Verified the reset Qwen shared plan with fresh native canaries for
  all three exact runtime IDs and a sandboxed Qwen Max writer canary. Enabled
  three `LIMITED` profiles with mode-specific routing. Certified Antigravity
  native file reads/writes with explicit workspace binding and removed shell
  from its routable tools.
- 2026-07-28: Rechecked the local fleet after the Router v2 work. Antigravity
  is now 1.1.8; Grok authentication recovered between two bounded probes.
  Recorded the state transition and made runtime deny-pattern detection a
  per-route and pre-execution hard gate rather than a weekly static assumption.
- 2026-07-28: Refreshed local CLI versions and official provider evidence.
  Recorded Gemini 3.6 Flash variants, Sonnet 5 tokenizer expansion, and an
  earlier Grok authentication failure.
- 2026-07-21: Initial six-CLI snapshot. Added installed Qwen Code 0.20.0 as
  subscription-pending and routing-disabled; recorded Qwen 3.8 Max as an
  unverified user-observed target rather than inventing a runtime model ID.
