# Agent Catalog

Verified locally through 2026-09-01. Model availability is dynamic; run `doctor
--deep` before consequential routing. Agent Fleet also requires a `FRESH`
Model Radar audit no older than seven days; see the companion
the external Model Radar state reported by `model_radar.py paths`. Bundled
runtime copies are immutable initialization seeds, not the live snapshot.

## Claude Code

- Local CLI: `2.1.257`.
- Headless: `-p`; structured output: `--json-schema`; permission modes include
  plan, auto, and accept-edits; effort can be set independently.
- Exact Fable 5.1 runtime/API ID: `claude-fable-5-1`. A local native canary on
  2026-09-01 returned that exact identity with a 1,000,000-token context.
- Keep Sonnet on routine low-complexity work. Use Fable 5.1 `medium` for normal
  advisory/review, `high` for demanding review/implementation, and `xhigh`
  only for the hardest advisory or planning work. The `low` Fable 5.1 profile
  is explicit A/B evidence only. Never auto-route `max`.
- Fable 5 and the `fable` family alias are retired from executable Fleet
  profiles. Fable 5.1 is pinned by exact identity before and after launch.
- While the owner credit declaration is active, every Claude profile remains
  selectable but is ordered after all other eligible lineages. Explicit Claude
  selection is allowed and labelled as a conscious billing override.
- Anthropic positions Fable 5.1 for demanding reasoning and long-horizon work
  where Opus 5 at high effort is insufficient. CursorBench 3.2 reports a large
  quality and token-efficiency improvement over Fable 5, but benchmark deltas
  are supporting evidence, not a production acceptance oracle.
- Fable 5.1 is a Covered Model for Anthropic's Zero Data Retention program, but
  ordinary API retention remains 30 days unless the account has an explicit
  ZDR agreement. Public/internal data may route normally. Confidential or
  restricted data fails closed unless the assessment includes current,
  account-scoped ZDR authorization covering exact model `claude-fable-5-1`.
- Anthropic also documents that Sonnet 5's updated tokenizer can map the same
  input to roughly 1.0-1.35x tokens depending on content. Treat that only as a
  baseline: retry loops, poor scope control, and repair cost require local
  task telemetry.
- Sources: https://platform.claude.com/docs/en/models/fable-5-1/overview,
  https://platform.claude.com/docs/en/build-with-claude/prompt-engineering/prompting-claude-fable-5-1,
  https://privacy.claude.com/en/articles/8956058-i-have-a-zero-data-retention-agreement-with-anthropic-what-products-does-it-apply-to,
  and https://cursor.com/cursorbench.

## Kimi Code

- Local CLI: `0.28.1`, matching the latest official Kimi Code changelog checked
  on 2026-07-21.
- The default and only routed model is local alias `k3`, displayed as K3 Max
  with a 1,048,576-token context. The managed catalog still exposes legacy
  `kimi-code/kimi-for-coding` as K2.7 Code, but Agent Fleet never selects it.
- Kimi supports image/video input, skills, hooks, and provider-native subagents.
- Route to frontend/UI consultation, broad repository review, large context,
  and visual/video-informed analysis. The strong frontend rating is Mateusz's
  local empirical preference.
- In this local version, `--prompt` implies auto approval and conflicts with
  `--plan`. Advisory and review use a temporary deny-by-default profile.
  Following Mateusz's explicit 2026-07-21 risk acceptance, implementation is
  permitted through `--allow-write` and a detached worktree even though Kimi
  has no native filesystem sandbox and is not technically contained by it.
- Sources: https://moonshotai.github.io/kimi-code/en/reference/kimi-command.html,
  https://moonshotai.github.io/kimi-code/en/configuration/config-files.html,
  https://moonshotai.github.io/kimi-code/en/guides/interaction.html, and
  https://moonshotai.github.io/kimi-code/en/release-notes/changelog.html

## Muse Code

- Local CLI: `1.4.6-R6000.1`, using the pinned native Windows executable
  `%LOCALAPPDATA%\Programs\muse\muse-bin-1.4.6-R6000.1.exe`.
  Fleet requires CLI >= 1.4.6, validates the versioned name and SHA-256,
  and keeps the existing Windows sandbox deep probe.
- Provider `meta` allows exactly `muse-spark-1.3` (K), `muse-wonder-1.0` (M),
  and `muse-wonder-full-1.0` (D), per owner decision 2026-10-12. Wonder and
  Wonder Full require explicit `--model` or execution-profile selection.
  Default Muse maps remain Spark 1.3; all share `meta-muse-code-plan`.
- Every model invocation, including availability canaries, passes
  `--contributor-mode off`. This run-only override defeats the account's
  persisted Contributor setting and is never persisted by Fleet.
  Any model ID containing `contributor` is categorically forbidden, including
  an ID reported only in native `run.model.configured` output.
- Aliases (`muse-wonder`, `muse-wonder-full`, `muse-wonder-1`), legacy
  `muse-spark-1.2`, fallback, substitution and snapshot suffixes are refused.
  The requested exact model must match the native provider/model identity.
  Missing, mixed, foreign or Contributor identity invalidates the handoff.
- Read/advisory disables write, shell and web. Implementation uses the
  Fleet-owned detached worktree. Literal argv, prompt files, removed API-key
  variables, bounded context/tool output and enabled sandbox remain enforced.
- All new execution profiles remain `LIMITED`, with conservative Spark priors.
  Meta's claimed Artificial Analysis Intelligence Index on 2026-10-12:
  Wonder Full 56, Wonder 52, Spark 1.3 48. This is provider information supplied
  by the parent, not a local measurement. Wonder context capacity has no
  supplied source: profiles retain Spark's provisional 1,007,997-token value,
  without claiming the unverified 1,048,576-token value.
- Parent evidence 2026-10-12: CLI 1.4.6 sandbox readiness and exact-ID native
  canaries; this contractor uses synthetic tests only. OpenClaw retains its
  separate Spark-only pin. Chrome relay and quota-pool configuration are unchanged.

## Codex CLI

- Local separately launchable CLI: `0.144.6`, resolved through the npm wrapper
  under `%APPDATA%\\npm\\codex.cmd`.
- Official noninteractive entrypoint: `codex exec`; default sandbox is
  read-only; structured output uses `--output-schema`; ephemeral runs use
  `--ephemeral`.
- Sol is the flagship for complex coding/research, Terra is the everyday
  workhorse, and Luna is the fastest option for clear repeatable tasks.
- The Desktop-bundled WindowsApps binary refused direct nested launch, but the
  independent npm CLI is launchable and is the adapter target.
- Source: https://developers.openai.com/codex/codex-manual.md

## Antigravity CLI

- Local CLI: `1.1.25`.
- Authenticated native inventory on 2026-09-03 exposes exact
  `gemini-3.8-flash-low`, `gemini-3.8-flash-medium`, and
  `gemini-3.8-flash-high`; all three are the automatic effort routes.
- Gemini 3.1 Pro High remains available only as an explicit specialist route.
  Legacy Gemini 3.6 IDs and Gemini 3.8 Flash Cyber are not selectable. Cyber
  access is limited to Google's Fairwind trusted-defender program and is not
  present in this local inventory.
- Google documents Gemini 3.8 Flash with a 1,048,576-token input limit and
  65,536-token output limit. Its speed, benchmark, and introductory-price
  statements are vendor claims; the local 2026-09-03 medium canary is recorded
  separately as one runtime measurement, not a general benchmark.
- Gemini 4 Argon (0.9.74, owner decision 2026-10-13): agy 1.3.4 inventory
  exposes `gemini-4-argon-low`, `gemini-4-argon-medium` and
  `gemini-4-argon-high`. Only `gemini-4-argon-high` is a Fleet profile
  (`antigravity-gemini-4-argon-high`, tier D in the Graph catalog). It is an
  explicit route (`--model` or Graph tier routing); the automatic effort map
  stays on Gemini 3.8 Flash High. Role: long context, large-repo review,
  knowledge work, multimodal. Vendor tables put it behind Opus/Astra in
  terminal work; capabilities are conservative vendor-derived priors with no
  local benchmark. Owner states model training is off on the Ultra account.
- Prefer Google models in this adapter so the Antigravity subscription carries
  its own share of work.
- Sources: https://blog.google/innovation-and-ai/models-and-research/gemini-models/3-8-flash-and-3-8-flash-cyber/
  and https://deepmind.google/models/gemini/flash/

## Grok CLI

- Local CLI: `1.0.5`; the authenticated `grok models` inventory on 2026-08-20
  returned `grok-4.7` as the default, with `grok-4.7-build-fast`, `grok-4.6` and `grok-4.5` also available (sonda 2026-09-21, grok CLI 1.0.34).
- Headless Fleet transport: long-lived `grok agent stdio` ACP session with
  `--json-schema`, explicit permissions, strict sandbox, disabled ambient MCP,
  memory, subagents, and web search. Fleet does not use `--single` or `--resume`.
- The local subscription exposes Grok 4.6 at the same entitlement boundary as
  4.5. Fleet routes only 4.6; 4.5 remains historical evidence, not a fallback.
- Route to engineering implementation, debugging, review, and current research.
- Local source: authenticated `grok models` output from Grok CLI 1.0.5 and a
  successful isolated ACP canary on 2026-08-20.
- Predecessor documentation: https://x.ai/news/grok-4-5 and
  https://docs.x.ai/developers/models/grok-4.5

## Qwen Code

- Local standalone CLI: `0.20.0`, resolved without a session restart through
  `%LOCALAPPDATA%\\qwen-code\\bin\\qwen.cmd`.
- The CLI supports headless prompts, structured output through `--json-schema`,
  plan mode, safe mode, and explicit wall-clock/tool/turn budgets.
- Authenticated native inventory verifies exact request IDs `qwen3.8-flash`,
  `qwen3.8-max`, `deepseek-v4.1-flash` (low lane since 0.9.71, owner 2026-10-13) and
  `deepseek-v4-flash-0731` (deprecated, retire after 2026-10-27). Qwen3.8-Flash-Next is
  the product lineage served through `qwen3.8-flash`; never send
  `qwen3.8-flash-next` as a runtime ID. They are distinct `LIMITED` execution profiles
  backed by one shared Coding Plan quota pool.
- Advisory/review uses bounded plan mode. Medium implementation uses
  `qwen3.8-flash`; high-complexity implementation uses the canonical
  `qwen3.8-max` writer in sandboxed `auto-edit` mode with a
  provider-reported dated snapshot retained only as provenance and a
  versioned tool denylist and detached Fleet worktree.
- Sources: https://qwen.ai/blog?id=qwen3.8-flash-next,
  https://www.qwencloud.com/models/qwen3.8-flash,
  https://github.com/QwenLM/qwen-code,
  https://qwenlm.github.io/qwen-code-docs/en/users/features/headless/, and
  https://qwenlm.github.io/qwen-code-docs/en/users/configuration/auth/

## Local Qwen aliases

- `qwen-local` and `qwen-local-5070ti` share one externally managed Ollama
  runtime: `http://127.0.0.1:11439/v1`, native tag inventory
  `http://127.0.0.1:11439/api/tags`, one serialized slot, and a 16,384-token
  context.
- The exact served model is `huihui_ai/Qwen3.8-abliterated`. Fleet accepts the
  bare name or `:latest` from the OpenAI-compatible inventory only when the
  corresponding native tag has manifest digest
  `bdc5e13cd1645bc409e95a7d9e864dc3ce3e5fe5beeb24a43c193e8041edd3e0`.
  A tag name alone is insufficient.
- The runtime owner is external to Fleet. Before execution, Fleet verifies the
  loopback listener owner plus executable
  `%LOCALAPPDATA%\MateuszAI\ollama\0.33.2\ollama.exe`, version `0.33.2`, and
  SHA-256 `c79df1e0c1bfa10ed813c7030ac4c3ba38bb0e350bd7322d9bb58320343235c6`.
  Mismatch is a hard failure; Fleet never starts, stops, or repairs Ollama.
- The aliases are bounded `LIMITED` implementation-capable, no-native-subagent
  profiles. They are eligible for ordinary routing only after the exact
  listener, executable, version and manifest checks pass. They share one
  serialized slot and carry no provider turn quota; Fleet still enforces hard
  tool and wall-time ceilings.
- Provider cost is `0 zł`; electricity and hardware depreciation are outside
  Fleet telemetry. The shared runtime is not part of the Bailian pool.

## OpenCode with Z.AI

- Minimum CLI: `1.18.18`; tested CLI: `1.18.21`; default exact model:
  `zai-coding-plan/glm-5.3-flash`. Exact `zai-coding-plan/glm-5.3` remains
  explicit-only for compatibility.
- OpenCode is the native terminal transport for Mateusz's paid Z.AI Coding
  Plan. It is not routed as a generic metered API fallback.
- The three default Flash execution profiles map low/medium/high complexity to
  OpenCode variants low/high/max. The high profile is `ACTIVE` as the pinned
  security auditor; lower profiles remain `LIMITED` review-only routes.
- Primary role: incremental security review of coherent implementation batches.
  The active high profile meets the frozen measurable floors and exposes the
  exact read-only auditor tool set. Every finding must include CWE, exact
  location, exploit path, confidence, evidence, and remediation.
- Fleet supplies an inline deny-by-default OpenCode configuration and allows
  only read, glob, grep, list, and LSP. Edit, shell, web, skills, MCP, native
  subagents, external directories, and interactive questions are denied.
- The Z.AI key remains encrypted with Windows DPAPI and is decrypted only into
  the bounded child process environment. Model-token telemetry does not reveal
  Coding Plan point consumption, so weekly points remain `unknown` unless read
  from a provider-native source.
- Sources: https://z.ai/blog/glm-5.3-flash,
  https://huggingface.co/zai-org/GLM-5.3-Flash,
  https://opencode.ai/docs/cli/, https://opencode.ai/docs/config/, and
  https://opencode.ai/docs/permissions/.

## Usage and quota evidence

Plan utilization is provider-account evidence, not a token counter. Use the
native interactive command, then append a source-attributed Fleet snapshot:

| Profile | Native plan command | Per-run token channel |
| --- | --- | --- |
| Claude | `/usage` | headless JSON |
| Kimi | `/usage` | stream JSON |
| Codex | `/usage` | `exec --json` events |
| Antigravity | `/usage` | unavailable/unknown in print mode |
| Grok | `/usage` | headless JSON |
| Qwen | `/stats` (`/usage`) | headless JSON |
| Qwen local | no provider quota | headless JSON; provider cost `0 zł` |
| OpenCode/Z.AI | provider dashboard | JSONL model-token events; plan points unknown |

Do not automatically scrape terminal UI. If a native command cannot be read
reliably, store `unknown` or make an explicit `tui-assisted`/manual snapshot.
Never convert plan percentages or credits into tokens.

## Execution-profile lifecycle

Do not route a marketing model name directly. Register a concrete Execution
Profile and move it through `DISCOVERED -> PROFILED -> CANARY -> LIMITED ->
ACTIVE`; use `DEPRECATED` when removed. Automatic routing accepts only
`LIMITED` and `ACTIVE`, still constrained by each profile's risk ceiling.
Every route and run repeats runtime authentication checks, because a weekly
catalog audit cannot prove the current session remains logged in.
