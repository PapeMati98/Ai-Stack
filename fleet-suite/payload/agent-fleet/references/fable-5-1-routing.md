# Claude Fable 5.1 routing evidence

Evidence frozen on 2026-09-01. Refresh Model Radar before using these values as
current market evidence.

## Exact identity and capability

- Exact model ID: `claude-fable-5-1`.
- Anthropic release date: 2026-09-01.
- Advertised context: 1,000,000 input tokens; maximum output: 128,000 tokens.
- Pricing: $10 per million input tokens, $50 per million output tokens, and
  $0.25 per million cache-read tokens.
- Native local canary: Claude Code `2.1.257`, `--model claude-fable-5-1`, low
  effort, no tools, no session persistence. It returned the exact expected
  payload and reported canonical model `claude-fable-5-1`.
- Each Fleet child run additionally verifies provider-reported assistant model
  identity (or canonical model-usage evidence for JSON output). Missing or
  different Fable 5.1 identity invalidates the handoff.

## Routing policy

Fable 5.1 is the only executable Fable generation in Fleet. Medium, high, and
xhigh profiles are `LIMITED`; the low profile remains an explicit A/B canary.

| Work | Low complexity | Medium complexity | High complexity |
| --- | --- | --- | --- |
| Advisory | Sonnet | Fable 5.1 medium | Fable 5.1 xhigh |
| Review | Sonnet | Fable 5.1 medium | Fable 5.1 high |
| Implementation | Sonnet | Sonnet | Fable 5.1 high |

The Fable 5.1 low profile is explicit A/B only. No automatic route uses `max`.
Fable 5 and the `fable` family alias are not configured and are rejected before
process launch. Provider family aliases are not valid identity evidence.

## Temporary owner credit policy

Fleet reads only `tryb`, `od`, and `wazne_do` from the Claude entry in
`~/.agents/billing-mode.json`; owner prose and unrelated provider fields never
enter route evidence. While a valid `kredytowy` declaration is active, the
entire Claude lineage is retained as a real fallback but placed after every
other eligible provider in adaptive and baseline routing, quality-gate choice,
and security fallback choice. GLM remains the primary security auditor.

The end date is inclusive. The owner declaration through `2026-09-06` therefore
keeps Claude last through that date and expires automatically on `2026-09-07`.
An absent file or absent Claude entry is neutral. A malformed Claude declaration
fails safe by keeping Claude last. Explicit `--agent claude` remains possible
and is labelled `conscious billing last-resort override` in route evidence.

Anthropic recommends testing all effort levels because Fable 5.1 effort labels
are not directly comparable with Fable 5. Medium is the default evidence-
gathering level. High/xhigh must earn their extra runtime and token cost.

## External benchmark evidence

CursorBench 3.2 reported:

| Model / effort | Score | Cost per task | Tokens |
| --- | ---: | ---: | ---: |
| Fable 5.1 medium | 68.0 | $3.53 | 23,801 |
| Fable 5.1 high | 69.4 | $4.80 | 33,153 |
| Fable 5.1 xhigh | 72.8 | $6.96 | 51,349 |
| Fable 5.1 max | 73.4 | $9.64 | 72,060 |
| Fable 5 max | 70.5 | $17.32 | 103,525 |
| Opus 5 max | 70.0 | $8.23 | not used as a routing gate |
| Grok 4.6 xhigh | 70.8 | $2.81 | not used as a routing gate |

These figures support a canary and local A/B program. They do not prove that a
small score difference is statistically significant, nor do they override
task fit, subscription headroom, data policy, or accepted local telemetry.

## Retention boundary

Fable 5.1 is eligible for Anthropic's Zero Data Retention program, but normal
retention remains 30 days unless the relevant account has a current explicit
ZDR agreement. The router therefore requires `data_handling.classification`.

- `public` and `internal`: eligible subject to the ordinary task gates.
- `unknown`, missing, or invalid: fail closed for Fable 5.1.
- `confidential` or `restricted`: require current account-scoped authorization,
  exact model coverage, verification and expiry timestamps, and a redacted
  evidence reference. A model being ZDR-capable is not authorization.

The evidence reference must never contain credentials or contract text.

## Sources

- https://platform.claude.com/docs/en/models/fable-5-1/overview
- https://platform.claude.com/docs/en/build-with-claude/prompt-engineering/prompting-claude-fable-5-1
- https://privacy.claude.com/en/articles/8956058-i-have-a-zero-data-retention-agreement-with-anthropic-what-products-does-it-apply-to
- https://cursor.com/cursorbench
