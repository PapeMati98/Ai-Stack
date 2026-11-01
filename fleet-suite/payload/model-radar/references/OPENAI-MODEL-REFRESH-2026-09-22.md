# OpenAI model refresh candidate — 2026-09-22

Historical Codex routing after the 2026-09-22 local ChatGPT-account CLI canary: GPT-5.6 Luna low, GPT-5.6 Terra medium, GPT-5.6 Sol high implementation, and GPT-6 Astra high advisory/review. At that time, the exact gpt-6-luna and gpt-6-sol profiles were PROFILED and failed Fleet lifecycle admission, including explicit selection. The local canary for each model returned HTTP 400, model is not supported when using Codex with a ChatGPT account. Their capability and efficiency values were unbenchmarked routing priors, not measured results.

Official OpenAI API model documentation lists Sol at $2 input / $10 output and Luna at $0.10 input / $0.50 output per million tokens: https://developers.openai.com/api/docs/models . These are API USD prices. Official Codex pricing lists Sol at 50 input / 5 cached input / 250 output credits and Luna at 2.5 / 0.25 / 12.5 credits per million tokens: https://learn.chatgpt.com/docs/pricing . The source of billing for a particular Codex subscription run and remaining quota require account-level evidence. Published prices do not establish billing source or remaining quota for a particular subscription run. Exact CLI availability is established here only by the dated canary below; recheck on subsequent CLI or account changes.

Arena IDs in `arena-model-map.json` are candidate exact-ID lookups only. The bundled `MODEL-BENCHMARK-SNAPSHOT.json` and historical `MODEL-MARKET-AUDIT.md` remain unchanged.

## 2026-09-23 exact-ID canary update

A fresh canary report records successful runs on Codex CLI 0.156.1 for the
exact runtime IDs gpt-6-luna and gpt-6-sol. Agent Fleet Suite 0.9.20
routes GPT-6 Luna to low complexity and GPT-6 Sol to medium complexity and
high implementation; GPT-6 Astra remains mapped to high advisory/review.
The Luna/Sol profiles remain LIMITED with conservative unbenchmarked priors.
Dedicated security review remains on ACTIVE GPT-5.6 Sol because GPT-6 Sol has
zero security-review capability. This availability result does not establish
comparative quality, quota headroom, subscription billing, or general
performance.

