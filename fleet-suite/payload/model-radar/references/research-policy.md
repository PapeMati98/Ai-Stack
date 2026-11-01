# Research Policy

## Evidence order

1. Local CLI `--version`, read-only model inventory, and bounded live readiness.
2. Official provider model catalog, release notes, system card, and CLI docs.
3. Official Arena dataset or another independent reproducible benchmark.
4. Repeated parent-accepted local task evidence.
5. User observation, clearly labeled and awaiting confirmation.

Never promote a rumor, screenshot, search snippet, or marketing label to a
runtime model ID. Record publication date and verification date for every
time-sensitive conclusion.

## Characterization dimensions

For each model record:

- exact runtime ID and human-facing name;
- access path and whether the local subscription actually exposes it;
- reasoning/coding depth, speed, context, multimodality, and tool-use fit;
- best task classes and meaningful weaknesses;
- vendor claim versus local observation;
- safety constraints of the CLI adapter;
- replacement/deprecation status.

Do not collapse capability, latency, price, and subscription quota into one
"best model" score. Agent Fleet fit weights are task-specific.

## Arena policy

- Source only `lmarena-ai/leaderboard-dataset` for the weekly Arena snapshot.
- Use `latest` splits and preserve leaderboard publication dates.
- Match exact names from `arena-model-map.json`; candidate aliases contribute no
  routing signal until verified.
- Keep rating/IPS score, confidence interval, rank, votes or observations,
  sessions, category, and date together.
- Treat missing subsets, HTTP 429, or a changed schema as warnings. Preserve the
  previous valid snapshot and static capability fallback.
- Never use an older model's benchmark as a proxy for a newer local alias.

## Routing change gate

Change a profile only when:

1. the exact model ID is supported by the local CLI;
2. the paid entitlement or saved subscription authentication is verified;
3. a read-only or bounded live probe succeeds;
4. the adapter has a safe permission/sandbox mode for the intended work;
5. tests assert the new route and old deprecated ID cannot return silently.

If any item is missing, keep `routing_enabled: false` and describe the blocker.

## Lifecycle and local calibration

- `DISCOVERED`: exact candidate recorded, no routing.
- `PROFILED`: primary-source and local inventory evidence recorded.
- `CANARY`: bounded local benchmark tasks only.
- `LIMITED`: selected low/medium-risk production-like tasks with mandatory
  review.
- `ACTIVE`: repeated accepted evidence supports normal risk-bounded routing.
- `DEPRECATED`: no new routing; preserve historical telemetry under the old
  fingerprint.

External benchmarks set priors only. Local calibration must use accepted
results, first-pass completion, repair count, wall time, token telemetry,
instruction compliance, scope control, code or visual quality, and regressions.
Deduplicate by root task and shrink small samples.
