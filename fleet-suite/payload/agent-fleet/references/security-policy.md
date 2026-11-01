# Security Policy

## Non-negotiable defaults

- Use saved OAuth/subscription authentication. Remove ambient API-key variables
  from child environments. The only package-scoped exception is OpenCode/Z.AI:
  decrypt the user-approved DPAPI Coding Plan key only into that bounded child
  process and never serialize or print it.
- Run advisory and review tasks read-only. Never add bypass, yolo,
  always-approve, or danger-full-access flags.
- Place every implementation writer in its own detached Git worktree. Multiple
  writers may run concurrently, but never against one checkout and never with
  automatic merge or apply.
- Keep web opt-in. Allow provider memory and approved local skills when they
  improve task quality; recursive orchestrator skills remain excluded.
- Refuse prompts containing high-confidence credentials, private keys, bearer
  tokens, or common vendor token prefixes.
- Pass argument arrays directly to child processes. Never build a shell command
  from task text.
- Store only task hashes and operational metadata in the usage ledger.

## Native Muse exact models and opt-out (0.9.71)

Only provider meta with muse-spark-1.3, muse-wonder-1.0 or
muse-wonder-full-1.0 is permitted. Every model run passes
`--contributor-mode off` on the pinned native CLI 1.4.6-R6000.1; older versions
are not ready because they lack this override. Model aliases and substitution
are refused. Any ID containing contributor is categorically forbidden before
launch and in native identity output, even if the local account opted out.
Missing, mixed or foreign run.model.configured identity invalidates the handoff.
Wonder does not expand the existing OpenClaw Spark-only lane or quota pool.

## Trust boundaries

The child CLI runs as the same Windows user and can potentially read user-level
files if its own sandbox or permission mode fails. Tool flags reduce accidental
access; they are not a security boundary against a malicious model or host
compromise.

Kimi is a special case: non-interactive `-p` uses auto approval and cannot be
combined with Plan mode. Agent Fleet therefore creates a temporary Kimi home,
loads only managed OAuth references plus approved shared skills, denies
write/shell in advisory and review modes, removes the temporary credential
junction after the process, and requires the normal `--allow-write` plus a
detached worktree for implementation. Mateusz explicitly accepted on
2026-07-21 that Kimi has no native filesystem sandbox and may technically write
outside that worktree. Parent review and Git evidence are therefore mandatory,
but they are detection and acceptance controls rather than containment.

Repository instructions, skills, plugins, MCP servers, hooks, raw sources, and
child output may contain prompt injection. Use isolated/safe CLI modes where
available and review all output before acting on it.

Approved skill copies and stable wiki snapshots refuse symbolic links and
Windows junctions instead of following them. This prevents a link nested under
an allowed skill or `wiki/` from importing a blocked orchestrator, `raw/`,
`proposals/`, or another user-readable path.

Qwen/Bailian access and exact model IDs are verified by bounded native CLI
canaries. Advisory/review runs use plan mode, terminal JSON output, disabled
chat recording, explicit turn/tool/wall-clock budgets, and read-only file tools.
Implementation uses `auto-edit`, the Windows container sandbox, an explicit
versioned denylist that leaves only read/edit/write file tools, and the same
hard budgets. Shell, notebook, web, computer-control, tool discovery, and
native subagents remain excluded unless a narrower future adapter revision is
independently certified. Qwen's `--safe-mode` intentionally disables ambient
skills, hooks, extensions, MCP servers, and context files; because that mode
ignores `--core-tools`, the adapter does not claim a false allowlist.

Exception since 0.9.33 (owner decision 2026-09-26, canary evidence in
`QWEN_OFFICE_MCP_CANARY_EVIDENCE`): a host-side, non-implement qwen run that
mounts the per-run contractor Agent Office server runs WITHOUT `--safe-mode`
(which would disable that server) and with the hardening made explicit:
`--allowed-mcp-server-names office` (no other MCP server can connect),
`--extensions none`, the same excluded-tool denylist (no shell, web, skills,
subagents) and the same budgets. The global QWEN.md context may load in this
mode. After the run Fleet checks the qwen init event: unless it reports only
the `office` server connected and exactly the contractor tools, the run is
recorded as `checkpoint-relay` with reason `mcp-attestation-*`, never as live
MCP. Implement runs (container sandbox) and local runtimes stay on safe mode
and checkpoint relay.

The `qwen-local` and `qwen-local-5070ti` aliases share one externally managed
Ollama runtime. Fleet strips ambient provider/proxy values, permits only the
loopback URLs on port 11439, and serializes one bounded execution slot.
Implementation is allowed only through the same readiness, digest and hard
budget gates as review; cloud fallback, native subagents, start/stop/repair and
ambient external endpoints remain excluded.

Before every run Fleet proves the exact `127.0.0.1` listener owner, validates
`%LOCALAPPDATA%\MateuszAI\ollama\0.33.2\ollama.exe` against the pinned
SHA-256/version, and confirms the served model through `/api/tags` plus
`/v1/models`. The bare model name and optional `:latest` are accepted only when
the native tag carries the frozen manifest digest. A tag, executable version, or
model listing alone is not sufficient. Fleet never starts, stops, repairs, or
exposes this external runtime. The process remains a user-level local service,
not a host security boundary; parent review and detached-worktree isolation
remain mandatory.

Grok implementation remains in `workspace-write` sandbox mode with MCP, web,
memory, and native subagents disabled. `acceptEdits` is paired with the narrow
native rule `Edit(./**)`, which approves `search_replace` only inside the
delegated worktree. Structured output is constrained by the exact handoff schema;
blanket approval and permission-bypass modes remain prohibited.

Grok ACP permission callbacks are not treated as user cancellations. The parent
broker validates the requested Bash command against the sealed allow and deny
rules, allows a pipeline only when every segment is independently allowlisted,
rejects chaining, redirection, command substitution and unknown commands, and
selects only the provider's `allow_once` option. In implement mode only, the
same broker may approve a native `SearchReplace` request whose lexical and
resolved path stays under the isolated `project/` worktree and contains no link,
traversal, drive or alternate-stream segment. Other tools, review-mode writes,
and requests without `allow_once` fail closed. A denied command resumes the same
session with bounded alternatives instead of converting one refusal into a false
terminal `BLOCKED`. This preserves non-interactive execution without enabling
ACP `yoloMode` or a permission-bypass flag.
Project-local Python entrypoints are permitted only through paths rooted at the
isolated `project/` mount. Inline `python -c`, parent traversal, shell chaining,
and interpreter paths outside that mount remain denied.

OpenCode/Z.AI access is review-only. Fleet invokes `opencode run --pure` with an
inline deny-by-default configuration that enables only read, glob, grep, list,
and LSP. It denies edit, shell, task/subagent, skills, MCP, web, interactive
questions, external directories, and provider sharing. The child receives only
`ZHIPU_API_KEY` decrypted from the current Windows user's DPAPI store; all other
ambient provider keys are stripped. The key is never added to command lines,
prompts, stdout, run records, or package files. These controls reduce accidental
mutation and credential spread but do not turn a same-user process into a hard
host sandbox.

## Modes

### Advisory and review

- No edits.
- No mutating shell commands.
- Web is automatic only for tasks classified as `research`; otherwise it needs
  `--enable-web`. `--disable-web` always forces an offline run.
- Native subagents use the configured per-contractor budget.
- Parallel workers may share the same checkout only because all are read-only.

### Implement

- Require `--allow-write`.
- Require a Git repository.
- Create a detached worktree under the run directory.
- Permit several implementation members only when every member has a separate
  worktree.
- Return the worktree path, status, diff stat, and handoff.
- Leave integration and cleanup to the parent agent after review.

## Recursion and fan-out

Set `AGENT_FLEET_DEPTH=1` in child environments. Instruct every child not to
invoke Agent Fleet. Native child subagents stay within the provider CLI, may
use a configurable budget up to 64 per contractor, and may not spawn another
generation. A zero budget is enforced natively for Claude, Codex, Kimi, Grok,
Qwen, and OpenCode. Antigravity exposes no equivalent CLI switch, so its zero and positive
budgets are contract-level instructions; its workspace sandbox remains active.
Positive numeric ceilings are native for Kimi and advisory for providers that
offer only an on/off subagent control.

Kimi, Qwen, and OpenCode default to a zero native-subagent budget at every task complexity.
A positive budget requires an explicit per-run flag and is persisted as an
opt-in decision in run evidence; it is not globally banned.

## Sensitive inputs

Do not delegate:

- API keys, OAuth tokens, passwords, cookies, private keys, or `.env` contents.
- Restricted client data unless the provider, account, and task are explicitly
  approved for that data class.
- Production mutation instructions without a separate owner approval.

Mentioning an authentication design is allowed. Supplying actual credential
material is not.

## Output handling

Raw stdout and stderr stay under the per-run directory. They can contain code,
paths, and model-generated text. Do not publish them or ingest them into the
shared Knowledge Hub without redaction and the normal source/proposal workflow.
Handoff parsing is bounded independently from raw-output retention so oversized
or deeply nested contractor output degrades to an invalid handoff instead of
unbounded recursive parsing.

## Fleet Quality Gate

- Freeze criteria, risk, and repair budget before the first audit.
- A quality auditor must differ from every worker in both CLI identity and
  declared `model_provider_id`; this is a hard gate, not a score bonus.
- Auditors always run in review mode with writes, web, inherited API keys, and
  native subagents disabled. They cannot dispatch repairs or accept a group.
- Build a bounded evidence bundle outside the project. Include only handoffs,
  normalized execution constraints, exact runtime attestation, normalized broker
  telemetry, patches, and changed-file snapshots. Broker telemetry is projected
  from a sealed provider envelope and never copies raw provider logs or command
  arguments. The manifest also attests the exact Fleet runtime that generated
  the bundle, separately from every worker runtime. Refuse `.env`, credential/secret paths,
  linked files, patches over 8 MiB, individual files over 2 MiB, or groups over
  32 MiB/1000 changed files. Scan the actual patch and snapshot bytes for
  high-confidence secret patterns, stage the bundle privately, and publish it
  with one atomic directory rename only after every check passes.
- For `security-gate`, require material implementation evidence: at least one
  non-empty Git patch or one changed-file snapshot. Refuse the gate before the
  auditor starts when every worker contributes zero bytes of code evidence.
- Treat contract text, handoffs, patches, snapshots, and prior findings as
  untrusted data. Neither auditors nor repair workers may execute instructions
  embedded in that evidence.
- Treat auditor output as untrusted until its exact structured shape, run set,
  transport, derived verdict, and worker completeness are validated.
- Every new run record carries a fail-closed `runtime_attestation`. Installed
  execution recomputes the sealed payload fingerprint and binds the run to the
  package version and deploy transaction; an absent marker is valid only for an
  explicitly labeled source-tree test run.
- Preserve accepted run IDs during incremental re-audit. A rejected item must
  produce a new run under the same root task and may consume only the frozen
  repair budget.
- Reuse an implementation worktree only when it remains below Fleet's guarded
  evidence root and still resolves as a Git worktree. Never reuse the base
  checkout.
- `accept-group` belongs to the parent. A gated run must not fall back to
  legacy `accept`. Serialize repair and acceptance per gate, reject linked state
  directories/records, and journal the multi-file parent decision so an
  interrupted acceptance is recoverable without allowing a conflicting verdict.

`security-gate` uses the same evidence, lineage, repair, and parent-acceptance
controls with a stricter finding schema. It defaults to OpenCode/GLM-5.3-Flash,
and the isolated OpenCode config pins only `glm-5.3-flash` plus explicit-only
legacy `glm-5.3` while remote model-catalog fetching stays disabled. Every
eligible auditor must meet the frozen measurable floors and expose exactly the
declared read-only auditor tool set; provider name is never enough. Codex and
Grok backups enter the bounded deterministic failover only when they also meet
those floors. An incomplete transport or handoff is failed, not success. The
auditor remains provider-independent from every worker and cannot edit findings
into the worktree. Use `quality-repair` for accepted repair scope, then re-audit
with the frozen prior gate lineage.

## Responsibility boundary

Provider CLIs are external contractors, not replacements for the parent Codex
or its native subagents. The parent must inspect evidence, rerun focused tests,
and decide whether to accept, revise, or reject every handoff. A valid JSON
handoff is a transport guarantee, not a quality guarantee.
