---
name: agent-fleet
description: Execute lean TODO tasks through native CLI models with independent product review and targeted repairs; no procedural contract chain for new work.
---

# Agent Fleet: lean dispatch 1.0

For new work, use one short TODO from plan-delivery. A single small task
uses `agent-graph/scripts/todo_pair.py --todo <todo.json> --run-dir <absolute-dir>`;
larger work with real branches uses `todo_graph.py run` once. Each task has one maker and one independent
reviewer, its real dependencies and product checks. No planning, quality or
acceptance contracts, fidelity/sufficiency gates or audits of reports.

Fleet remains the native CLI transport and model/availability router. Preserve
exact-model identity, data-sharing denylist, credentials, quota and workspace
protections. Fit first; prefer cheaper competent makers and appropriately strong
reviewers. For comparable fit prefer unused paid capacity. Unknown quota is not
zero. Check before forwarding task content; do not try exhausted accounts in a loop.

Availability checks are shared per subscription pool and cached across CLI runs.
Snapshot expiry means recheck, not restored capacity. Kimi uses native usage data;
if it cannot resolve availability, one isolated minimal CLI ping uses no project
files or tools. A weekly counter cannot clear a monthly denial. A failed check
selects another eligible worker, not a full task as a probe. Never buy extra usage
or switch accounts automatically. Native quota errors refresh this small cache;
tokens remain telemetry, not a new contract or acceptance requirement.

TODO Graph starts ready disjoint tasks in parallel. Deterministic failing tests
go back to the maker before paid review. Product REPAIR returns concrete findings;
no fresh planning cycle. Preserve code on malformed handoff or transport failure.
Two identical failures without progress need diagnosis, not another blind retry.
Token estimates are telemetry, not acceptance gates. Explicit owner limits remain.
Transport, retained artifact, product acceptance and knowledge closeout are
separate states. A verified maker with only an unparsed terminal handoff can
continue to the declared checks and independent review on the retained artifact;
this is PARTIAL work, never ACCEPT. Wrong model, checkout, base, scope or missing
provenance stays blocked. Do not call the maker again to repair a report format.

The current strong parent plans and integrates; a cheap parent delegates one
planning pass, not every coordination decision. Actual model lineage must differ
between maker and reviewer even when models share the same CLI subscription.

Legacy fleet.py run/quality-gate/security-gate/accept-group retain their old
semantics for existing records. TODO results do not overwrite those ledgers.
Report actual worker/reviewer, model, product result, tokens/quota if known.
An accepted candidate is not a production deployment.

Incident metadata is appended without a global history scan. Run `fleet.py
incident-report` only when inspecting problems; it refreshes the derived index.
Do not poll the report per node. A stored event does not imply a refreshed index.
TODO stage telemetry is automatic; see agent-graph's Lightweight Telemetry.

## Runtime identity

```powershell
$suite = if ($env:AGENT_FLEET_SUITE_ROOT) { $env:AGENT_FLEET_SUITE_ROOT } else { "$env:USERPROFILE\.agents\runtime\agent-fleet-suite\lean-0.9.142" }
$expectedRuntime = "e1c59b4b427a96656bcaf1a3a14638cbcfdbf4bfa63cc63a1568130bed4acc58"
python "$suite\agent-fleet\scripts\verify_runtime.py" --runtime-root $suite --expected-fingerprint $expectedRuntime --package-version 0.9.142
```

Verify once per unchanged work wave. If verification fails, diagnose the exact files; do not silently switch to another runtime. Existing legacy current runtime is not modified by this release.

## OpenClaw local executor

Use agent `openclaw-local-qwen` with exact model `vllm/Qwen3.8-27B` in TODO candidates. The backend is the official local BF16 model, configured context524288. A missing backend is an outage and fails visibly. Direct legacy `qwen-local` routes are disabled. This local lane supports workspace-scoped file implementation and read-only review. Implementation also receives four fixed offline sandbox MCP tools (exec, write_file, read_file, status); shell execution remains inside the pinned, non-root container without host mounts or network. Review/advisory receive no sandbox MCP tools. Select a reviewer from a different model family. Native Muse subscription remains a separate eligible executor.

## Measured context stop (MiMo)

The MiMo profile retains native auto-compaction at 180000 tokens and adds a 200000-token observed per-step input stop. Fresh input plus cache read/write count toward that measurement; output and cumulative usage do not. Exceeding it stops only the private child tree and preserves work as PARTIAL when native model identity is verified. Missing or unreadable telemetry remains unknown. This is a post-observation stop, not a pre-inference request ceiling: the triggering request may already have been charged. Do not restart automatically or resume the same large history; the parent narrows scope and creates an explicit continuation after reviewing the retained artifacts. This profile does not configure Grok, Kimi or Desktop limits.

## Antigravity context measurement

Native step_update agent_response usage now reports per-step fresh input plus cache read input. Cumulative results, output and thinking are excluded. Deduplication uses validated native step identity without exposing it. Missing or malformed measurements remain incomplete/unknown. An explicit profile context_stop_tokens may use the same private-child measured stop as MiMo; no Antigravity profile opts in by default in this release. The stop is post-observation, so the triggering request may already be charged. It preserves PARTIAL only with verified native model identity and never retries or compacts automatically. This does not configure Desktop limits or prove monthly quota savings.

## Muse native context controls

Muse uses supported summary-preserved-suffix/v1 compaction with soft0.12/hard0.18 fractions of the effective native context window and 65536-byte maximum tool output. Fallback low/medium/high step limits are24/48/72; an explicit task step override remains exact. These are native context controls, not an absolute input-token ceiling or a money guarantee. Unknown per-step usage and remaining allowance remain unknown. Use fresh bounded tasks; never claim monthly quota savings without measurements or use Contributor models as a fallback.

## Native ZCode Coding Plan

GLM-5.3 uses pinned official ZCode CLI0.16.9 for advisory/review with exact file admission through two read MCP tools. No native shell, write, web, plugins, hooks, memory, skills or subagents. Existing account auth remains native; generic API fallback is refused. Native response proves model identity. OpenCode remains legacy with routing disabled; the existing Z.AI quota pool and history are preserved. Numerical remaining allowance stays unknown unless verified. This adds no ordinary-session step.
