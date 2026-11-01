---
name: agent-cli
description: Execute lean TODO tasks through native CLI models with independent product review and targeted repairs; no procedural contract chain for new work.
---

# Agent CLI: native execution

Use the same short TODO -> maker/reviewer -> product checks path as Agent Fleet.
This skill is the entrypoint for delegation, not a trigger for a large contract.

Choose real supported CLI/model candidates from the available pool.
For one small delegated task, run `agent-graph/scripts/todo_pair.py --todo
<todo.json> --run-dir <absolute-evidence-dir>`: one maker, checks and independent
reviewer, with targeted repair. This path does not start a graph scheduler.
For larger work with real independent branches/dependencies, invoke
`todo_graph.py run` once. Do not add a second orchestrator or model polling.

For read-only one-off planning/research call agent-graph/scripts/todo_fleet.py
directly with --request and --output; see references/planning-request.json in
agent-graph. No graph is needed to ask the strong planner for the first TODO.
Give the smallest necessary context and explicit deliverable.
For code use the prepared isolated worktree; actual product acceptance belongs
to an independent reviewer, not to the command's exit code.
Never invent CLI flags, silently substitute models or bypass provider permissions.

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
