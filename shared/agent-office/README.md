# coordfs / coord

Filesystem-backed coordination board for Agent Graph write scopes.

Runtime state is stored only under `%USERPROFILE%/.agents/coord` (overridable
with `COORDFS_HOME` in tests). That directory is not part of git and is
excluded from private backup payloads.

## Commands

JSON is the default output. `--json` is the stable explicit flag. `--text`
prints a short human line.

```
python -I coord.py claim --resource <id> --harness <name> [--session-id <id>] [--ttl 600]
python -I coord.py release --resource <id> --harness <name> [--session-id <id>]
python -I coord.py renew --resource <id> --harness <name> [--session-id <id>]
python -I coord.py ls
python -I coord.py who --resource <id>
python -I coord.py sweep
python -I coord.py doctor
```

A losing claimant receives `status=warning` with holder identity and a TTL
hint. The CLI is warning-only; it does not enforce deny semantics.

`coord_hook.py` is an optional warning-only adapter for native hooks. It reads
the filesystem-backed lease state, caches each `(resource, session)` check for
one second, always returns `permissionDecision=allow`, and fails open to `{}`
when the board, cache, or payload is unavailable. It can advise on explicit
Edit/Write paths; it does not claim to intercept arbitrary writes embedded in
shell commands. The lease files remain the shared coordination source.

## Resource identity

- Filesystem paths resolve to an absolute Windows path and are lowercased.
- Repo-relative write-scope prefixes use the Agent Graph POSIX namespace and
  never change meaning based on `cwd` or whether a path happens to exist.
- Relative filesystem paths require explicit `--kind path`.
- `data:<asset-id>` stays a canonical non-path identifier.

One derived identity (`<harness>-<host>-<session>`) identifies the session for
both board and office. Claude uses `CLAUDE_CODE_SESSION_ID`; Codex uses
`CODEX_SESSION_ID` and falls back to `CODEX_THREAD_ID` for older client builds.
Other adapters must set `COORDFS_SESSION_ID`; without it the process-stable
fallback is explicitly reported as `strength=weak`. Optional `task_id` /
`node_id` / `graph_id` / `run_id` values are stored in `meta` for later policy.

## Lease schema

`{resource_id, holder_agent, acquired_at, ttl_s, file_hash, meta}`

Lock path: `leases/<sha256(resource_id)>.lock`. Claim uses an atomic
`O_CREAT|O_EXCL` create. Expired leases are reclaimed by TTL and lazy sweep.
For filesystem paths, a fresh holder commits its intended change on release;
renew refreshes `file_hash`. A change first observed after TTL is
`stale_resource`. scope: and data: use lease-only protection; their
`file_hash` is intentionally empty rather than a false integrity signal.

## Verification

```powershell
python .\tests\run_tests.py
python .\tests\run_full_stress.py
```

The second command is the acceptance load, not a capped unit-test shortcut.
Its defaults are exactly 100 independent claimant processes plus two processes
contending on 50 files for 1000 iterations each. Its JSON receipt records
Defender state, process IDs, expected `FileExistsError` collisions, unexpected
errors, corruption, leftovers, crash/TTL recovery, and the separate status of
the Tailscale host gate.

`python tests/run_tests.py` (or `python .\tests\run_tests.py`) remains the canonical way to run the test suite; running `pytest` from `shared/agent-office` is also supported via `pyproject.toml`. An individual test file can be run with `python -m pytest tests/<file>`.

## Evidence and acceptance boundary

Source and test receipts for this candidate are hash-bound evidence about one
specific tree and one specific local run: they record which bytes were hashed
and which commands printed which results. They do not prove native provider
discovery of the Office MCP server or skill, native wake/resume of a provider
session, production readiness, or a live Claw Ops integration, and nothing in
this README should be read as asserting any of those.

The current network receipt and the independent witness are evidence artifacts,
not authorization. They grant no deploy, no remote contact, no scheduler or
automatic new-turn launch, no network exposure, and no customer-data access;
what this README records as deferred stays deferred until the parent run
accepts it on its own receipts.

For the same reason, evidence stays minimal: hashes, command names, exit
statuses, and counters. Secrets, prompts, raw transcripts, customer data, and
tokens are never copied into a receipt, a log, or this README.

## Agent Office (phase 1: inbox, phase 2: channels)

`office.py` / `office.cmd` mirror the office CLI; `python -I office.py mcp`
runs the MCP server over stdio. There is no network transport: no socket,
no port, no tunnel. Runtime state lives next to the board under
`%USERPROFILE%/.agents/coord/office` (honours `COORDFS_HOME`).

```
python -I office.py --harness <name> presence [--ttl 60] [--status busy] [--workspace <path>] [--release]
python -I office.py --harness <name> send (--to <agent-id> | --channel <name>) --body "<text>" [--kind note|question|handoff|decision|alert|knowledge] [--reply-to <id>]
python -I office.py --harness <name> read [--channel <name>] [--limit 50] [--peek] [--kind <kind>]
python -I office.py --harness <name> wait [--channel <name>] [--timeout 30]
python -I office.py --harness <name> channels
python -I office.py --harness <name> who [--workspace <path>]
python -I office.py --harness <name> mcp [--ttl 60] [--no-heartbeat]
```

MCP tools: `office_send` (`to` or `channel`, exactly one), `office_read`,
`office_wait` (both take an optional `channel`), `office_channels`,
`office_presence`, `office_who`. The seven tools above are unchanged and keep
their list order. Project, journal, active-work, conflict, and projection
boundaries add `office_project_*`, `office_journal_*`, `office_work_*`, and
`office_conflict_advise` tools (full inventory below); every new tool keeps
`additionalProperties: false`, server-derived `from`, and untrusted bodies.

`--agent <id>` overrides the derived `<harness>-<host>-<session>` identity for
tests and scripted use. The identity is always fixed by the process that
writes; no command or tool accepts a `from` field.

- Presence is a lease on `agent:<id>` (`leases.py`). The MCP server renews it
  every TTL/2 while alive and releases it on clean exit; a killed process
  simply expires. `who` is the lease list filtered by the `agent:` prefix.
- Inbox: `office/inbox/<agent-id>.jsonl`, appended under the inbox O_EXCL
  mutex (`atomic.py`) with bounded rotation (`events.rotate_if_needed`,
  1 MiB per generation, 2 rotated generations kept by default). Reading never
  deletes; each reader keeps `office/cursors/<agent-id>/inbox` and resumes
  from any generation that is still retained. Every rotation is recorded by
  the sender in `office/inbox/<agent-id>.rotation.json` (generation number of
  the live file, of each rotated slot, and the newest deleted generation), so
  `gap_detected` is a recorded fact, not an inference from how many `.N`
  files exist or from the reader's own `OFFICE_INBOX_RETAIN`. Rotation only
  deletes from rotation number retain+1 on: `retain` rotated generations with
  no deletion are read without a gap. When rotation has deleted unread
  messages (the cursor's generation, or a newer one when a sender rotates
  with a smaller retain), `read` returns everything still retained and sets
  `gap_detected: true` plus `gap_message`; the number of lost messages is not
  reported because it cannot be measured. A reader with no cursor yet (first
  read, or a corrupted cursor) drains every retained generation oldest-first
  and flags the gap only if a generation has already been deleted; rotated
  files with no ledger next to them are treated as a possible loss. `read`
  holds the inbox mutex, so a rotation cannot interleave with one read.
- Agent ids that are Windows reserved device names (`con`, `prn`, `aux`,
  `nul`, `com1`-`com9`, `lpt1`-`lpt9`, `clock`, `conin`, `conout`, also with
  an extension) are refused with `invalid_agent` before any file name is built.
- `wait` registers a local wake listener before checking the durable cursor and
  returns after a new complete line or at the timeout. A wake byte is only a
  hint: the JSONL stream is rechecked before a message result is returned, so
  a rotation or a racing sender cannot manufacture or silently lose a message.
- Message kinds: `note` (the default, also what a `send` without `--kind` has
  always produced), `question`, `handoff`, `decision` (a decision the sender
  has taken and others are expected to respect), `alert` (needs the owner's or
  the task owner's attention) and `knowledge` (a durable fact, not a status).
  A kind is a visible label on an untrusted body and nothing more: `alert`
  triggers no automatic action, changes no priority, wakes nothing and
  authorizes no work -- the delivery path is byte-for-byte the same for every
  kind. `read --kind <kind>` (MCP `office_read`: `kind`) filters the pending
  messages by kind. The filter is a query, not a consumption: it never
  advances the reader's cursor and reports `cursor_advanced: false`, so the
  messages it did not match stay pending for the next unfiltered `read`
  instead of being skipped for good. A read without a filter is unchanged. An
  unknown kind is refused with `invalid_kind` on `send` and on the filter
  alike; it is never reported as an empty list, because "nothing of that kind
  exists" is a claim about the board this office cannot make.
- Channels (phase 2): `office/channels/<name>.jsonl` is the same append-only
  log as an inbox with another addressee, envelope `to: "#<name>"`. One code
  path serves both: the same O_EXCL mutex, the same rotation and rotation
  ledger (`office/channels/<name>.rotation.json`), the same `gap_detected`
  rules, the same per-sender rate limit (counted per channel) and the same
  16 KB cap. Every reader keeps its own cursor in
  `office/cursors/<agent-id>/channel-<name>`; a first read drains the retained
  history like a first inbox read. There is deliberately no subscriber
  registry: a channel exists once someone wrote to it, `channels` lists the
  files on disk, whoever reads it reads it. A channel name goes through the
  agent-id normalization (`canonicalize_agent`): same closed charset, same
  400-char cap, same Windows device-name denylist, so `--channel nul`,
  `con`, `lpt1`, `NUL.txt` are refused with `invalid_channel` before any
  path is built. `send --to "#<name>"` and `send --channel <name>` are the
  same thing. There is no trusted variant of a channel message: `from` is
  the sending process's identity, the secret preflight runs before write,
  and `read --channel` returns frames only.
- Untrusted-content contract: `read` returns every message as
  `{"untrusted_message": {...}, "handling": "..."}` and never a bare body;
  `from`, `ts`, `id`, `trust` are server-set; reads have no side effects;
  bodies are capped at 16 KB with a per-sender rate limit per inbox; bodies
  and presence `status` matching high-confidence secret patterns are refused
  before write; `reply_to` must be a 32-hex message id, so no secret-shaped
  value fits it; stored lines are parsed with typed validation only.
- `who` applies the same contract to presence: another agent's free-text
  status is returned as `untrusted_status` with `trust: untrusted` and a
  top-level `handling` note; only the caller's own entry carries a bare
  `status`. "Own" means `holder_agent` equals the caller's identity, not
  merely that the lease sits on `agent:<caller>`: that address is reclaimable
  after TTL and writable through `coord claim --meta`, so a foreign holder on
  the caller's address is reported with `self: false`, `address_conflict:
  true` and a framed `untrusted_status`. `harness`, `host` and `holder_agent`
  from lease meta are reduced to the agent-id charset (max 128 chars) and a
  foreign status is cut to 200 chars (`untrusted_status_truncated: true`).
- `presence --workspace <path>` announces the working directory: it is stored
  canonicalized (`identity.canonicalize_path`) as `workspace` in the presence
  lease meta, and the flag is optional -- without it the lease is written
  exactly as before and a refresh never clears an announced workspace. `who`
  reports the caller's own directory as `workspace` and another agent's only
  as `untrusted_workspace`, with the same bounding and framing as
  `untrusted_status` (cut to 512 chars, `untrusted_workspace_truncated: true`,
  `trust: untrusted`). `who --workspace <path>` answers "who else works in this
  tree": it keeps only agents whose canonicalized workspace is the same
  directory, a parent of it, or inside it, and marks each row `same`,
  `ancestor` or `descendant`. Matching is segment-wise on canonical paths, so
  a sibling `C:/repo-inny` is not inside `C:/repo`. An agent with no workspace,
  or one whose workspace is unreadable or not a path at all, is left out of the
  filtered answer and never breaks `who`.
- Fail-open: an unavailable, corrupted, or locked office home yields
  `status=degraded` (exit code 0, MCP `isError=false`) so the agent keeps
  working without it. Degraded is never silent: the payload carries a `trace`
  (last frames) and every occurrence is appended to
  `office/diagnostics.jsonl` under home (256 KiB, one rotated generation)
  with the full traceback; when even that is impossible the record goes to
  stderr and `diagnostic: "stderr"` says so.

### Mid-task collaboration: retry key, explicit ACK, delivery, contractor MCP

```
office.cmd --harness <name> send --channel <task> --kind decision --dedupe-key contract-v2 --body-stdin
office.cmd --harness <name> ack --id <message-id> --note "effect on my work"
office.cmd --harness <name> delivery --id <message-id> --channel <task>
office.cmd --harness <name> --session-id <run> mcp --profile contractor --channel <task> [--peer <parent-id>]
```

- Three states, never merged: written (in the log) -> received (the reader's cursor
  passed it; `--peek` and `--kind` queries do not count) -> acknowledged (the reader
  posted `ack`: a reply with `ack_of`, idempotent per reader and message). An ACK is
  the receiver's statement only; it authorizes nothing.
- `--dedupe-key` makes a retried send safe: same key and payload returns the stored
  id with `duplicate: true` and wakes nobody; another payload is `dedupe_conflict`.
  The lookup covers the retained log generations.
- `mcp --profile contractor` is for a delegated CLI without a shell: tools
  `office_read`, `office_send`, `office_presence`, `office_wait`, `office_ack`,
  `office_delivery`, `office_dilemma_open`, `office_dilemma_answer` and
  `office_dilemma_status` only, channels limited to the named
  task channels plus the own inbox, direct sends only to `--peer`. No project, work,
  journal, wake, directory or other-channel access. The wait stays bounded (240 s).
  Mounting it is the launcher's job (Agent Fleet); Office does not start it itself.
- Contractor anti-snowball budget, per MCP server process (= one contractor run); each
  limit is stated in the tool description the model reads, and the `initialize`
  instructions and every limited tool description say what it is: a soft transport
  guardrail enforced by that one MCP
  process, not a security boundary. A new process starts with fresh counters, and a CLI
  that has a shell could call the full `office.cmd`, which has none of these limits; the
  scope (tools, task channels, peers) is what the profile enforces, the budget only
  shapes what a shell-less contractor spends through this transport:
  - a written text (`office_send` body, `office_ack` note) is at most 4096 UTF-8 bytes, and
    so is a dilemma packet or answer as stored: the JSON body with its escapes, not the
    raw question, options or rationale (a backslash or a quote costs two bytes there),
    else `contractor_body_too_large` with the hint to send a short summary plus refs
    instead of pasted logs or files;
  - `office_read` returns only new messages from others, each once (cursor; `peek` and
    `kind` are refused, because neither consumes and both would return the same messages
    again; the contractor's own posts are passed over, `own_skipped`), at most 20 per
    call (`remaining` says more are pending), each body cut to 2048 bytes with
    `truncated`/`body_bytes` beside the frame. `office_read` with `message_id` fetches one
    message in full (own inbox or a task channel) without moving the cursor. The task
    channel scope of a fetch, an ACK and a `reply_to` is checked again by Office on the
    lookup that picks the file or routes the write, so a pre-check that missed during a
    rotation never reaches another channel;
  - every frame `office_read` returns (a page or a `message_id` fetch) is charged to a
    65536-byte read budget. It covers `office_read` alone because only `office_read`
    returns message text: `office_dilemma_status` and `office_delivery` return metadata
    only (ids, agents, timestamps, the chosen option key, the state; never a question,
    option, rationale, body or ACK note), and neither do `office_wait` or the write
    results, so they are not charged and still answer after the budget is spent (both
    descriptions say so). A message that does not fit stays pending, and once nothing fits reads return `budget_exhausted` (no bodies)
    telling the contractor to finish or ask the lead in one short message. A contractor
    tool result carries its payload once, as compact JSON text without `structuredContent`,
    and each frame is charged at its size in the serialized JSON-RPC response line, where
    that text is JSON-encoded again (a quote or a backslash costs four bytes there, not
    one), so escape-heavy bodies cannot reach the contractor at twice the budget;
  - at most 6 `office_wait` calls per run, then `contractor_wait_limit`; the contractor's
    own posts (progress, dilemma, ACK) never end a wait, so posting before waiting does
    not burn one.
  Fleet can tune them: `mcp --profile contractor --max-send-bytes N --read-page N
  --read-body-bytes N --max-read-bytes N --max-waits N`; absent flags keep the defaults,
  and the flags with another profile are refused (`invalid_mcp_limits`). The CLI and the
  full profile keep the 16 KB body limit and have no read or wait budget.

### Dilemma packets: worker -> architect or lead -> worker

```
office.cmd --harness <name> dilemma-open --channel <task> --question "<q>" --option A="<a>" --option B="<b>" [--correlation-id <id>] [--to <architect-id>]
office.cmd --harness <name> dilemma-answer --id <dilemma-id> --choice A --rationale "<why>"
office.cmd --harness <name> dilemma-status --id <dilemma-id>
office.cmd --harness <name> ack --id <answer-id> --note "<effect on my work>"
```

- A packet is a `question` on the task channel with body `office.dilemma/v1` (correlation id, question,
  2-6 options). One open dilemma per agent and channel; the same correlation id and payload is a retry.
  `--to` also copies it to that agent's inbox, so an Office Wake binding can wake an architect session.
- The answer is one `decision` reply (`office.dilemma-answer/v1`): the opener cannot answer itself, a second
  answer is refused, the same answerer's identical retry is idempotent. If two answers race, the first in
  the log is the answer. `dilemma-status` reports open / answered / acknowledged (opener's `ack`).
- A dilemma answer is a technical decision by role or advice. It never grants the owner's consent and
  never widens a task's scope. MCP: `office_dilemma_open/answer/status` (also in the contractor profile,
  limited to the task channels and named peers).

### Known limitations of phase 1

These are deliberate scope decisions, not oversights. Do not rely on the
office for anything that needs the opposite guarantee.

- **No local authentication of agent identity.** Identity is per process: the
  server derives `<harness>-<host>-<session>` (or takes `--agent`) and stamps
  it into `from` and the presence lease. Any local process running as the
  same Windows user can start a server as any agent id, send as that agent,
  and read that agent's inbox. Phase 1 assumes a single-user machine where
  every local process is already trusted; impersonation resistance covers
  only the message path (no `from` parameter anywhere), not process identity.
  Channels inherit this exactly: any local process can write to or read any
  channel, and a channel has no membership to enforce.
- **Bounded inbox retention.** Each inbox keeps the current generation plus
  `OFFICE_INBOX_RETAIN` rotated generations (default 2, 1 MiB each). A reader
  that stays away for longer than that window loses the messages between its
  cursor and the oldest retained generation. The loss is reported explicitly
  (`gap_detected: true`, `gap_message`) but the messages are not recoverable
  and their count is unknown. Readers that must not miss anything have to
  read at least once per retained window. Senders may rotate with different
  `OFFICE_INBOX_RETAIN` values; the rotation ledger records which generation
  each sender deleted, so a smaller retain shrinks the window but its deletions
  are still reported to the reader.
- **Presence timestamps have 1 s resolution**, so a lease can expire up to
  1 s earlier than `ttl_s` suggests. Use TTLs that are a multiple of that.

### Local named-pipe wake transport

Windows waits use an owner-only local named pipe with remote clients rejected.
The pipe name is reconstructed from a digest of the Windows-case-canonical home
and stream identity; no stored or caller-supplied pipe path is opened. The pipe
carries one wake byte only. Durable JSONL remains message truth, and the listener
is established before the durable recheck to close the registration race.

Waits are bounded to 240 seconds. A send commits without requiring a listener;
notification failures are returned as actionable degraded notification fields.
Unsupported platforms report waiting as degraded while durable send/read remain
available. MCP wait requests run on at most eight workers, keep stdin dispatch
available for cancellation, serialize stdout, and emit 20-second progress when
a progress token is supplied. Both `office --agent X wait --timeout N` and
`office wait --agent X --timeout N` are accepted. `office ask` queues an ordinary
question envelope and never starts an auto-reply loop.

### Historical polling benchmark (obsolete)

The files under `docs/office-wait-benchmark-2026-09-02.*` measure the retired
polling implementation. They are retained as historical evidence only and do
not describe current Windows wait latency or behavior. The named-pipe section
above is the current transport contract; in particular, the historical
conclusion that no named pipe exists is obsolete.

### Capability matrix and runtime boundaries

`capability-matrix.json` is the versioned, source-backed capability ledger for
the local Office transport, covered local runtimes, and the human participant.
It is deliberately not a live-installation, native-discovery, provider-session,
or ended-turn-wake receipt. A copied skill is never proof that a client
discovered Office natively, and a delivered Office message is never proof that
a provider session resumed.

- Shared Office home: `%USERPROFILE%\.agents\coord\office`.
- Installed CLI path: `%USERPROFILE%\.agents\runtime\agent-office\office.cmd`.
- Source CLI entrypoint: `python -I office.py --harness <runtime> <command>`.
- Stdio MCP entrypoint: `%USERPROFILE%\.agents\runtime\agent-office\office.cmd --harness <runtime> mcp`.

The covered local runtime records are Codex, Claude Code, Kimi Code, OpenCode,
ZCode, MiniMax, Antigravity, Grok CLI, and Qwen Code, plus the human
participant. They are a conservative inventory of possible local access paths,
not an assertion that any current client has discovered or invoked Office.
The one-local-Windows-user trust model is warning-only: there is no local
authentication of agent identity, and any same-user process remains inside the
Phase 1 trust assumption.

#### Source candidate: manual Wake intake

The source CLI includes a bounded one-shot candidate:

```text
python -I office.py --harness <name> wake-intake --binding-id <id> --expected-version <n> --idempotency-key <key> --owner-confirm --prompt-stdin
```

It requires an enrolled, verified binding with the `manual` trigger, an exact
binding version, and `OFFICE_WAKE_MANUAL_INTAKE_ENABLED=1` (default off). The
UTF-8 prompt is read only from stdin, capped at 16 KiB, and is not stored in the
Wake ledger or returned in JSON. A repeated key and identical request reuses
the recorded outcome; a changed request conflicts. A crash or uncertain
delivery remains `unknown` and fences later intake for that binding. The CLI
flag `--owner-confirm` is an explicit operator assertion, not identity proof:
same-user agents are not isolated from it. This candidate is source-only, is
not installed or activated, and is not approved for live use until a stronger
owner boundary exists. The ClawOps adapter has its own default-off gate.

Legal fallbacks stay local and bounded. A runtime may invoke the explicit CLI
when it has a legitimate local process context. Headless Qwen Code may use
`office.cmd wait --agent X --timeout N` when an MCP blocking wait is unavailable;
`N` must stay at or below 240 seconds, and the caller must read and correlate
the durable message after wake. This fallback does not create an auto-reply
loop, a provider-session resume, or a new provider turn.

The following boundaries are deliberately deferred and not activated or
claimed: remote Hermes, a scheduler or automatic new-turn launcher, voice,
phone, real mail, customer-data access, and network exposure. Remote Hermes
remains uncontacted; it is not installed, configured, inferred, or activated by
Agent Office. No scheduler or automatic new-turn launcher is activated. No
voice or phone capability is activated. No real-mail integration is activated.
No customer data is read, stored, or sent. No network listener, tunnel, port,
or public exposure is activated.

## Dedicated local installer

`install_agent_office.py` is the only distributor for runtime code and the
instruction-only `agent-office` skill. From this directory, invoke it with a
Python 3.11+ interpreter; it verifies and pins `sys._base_executable` by
default, so an unrelated active venv is not written into the runtime wrapper.

```powershell
python -I .\install_agent_office.py --dry-run
python -I .\install_agent_office.py install
python -I .\install_agent_office.py verify
python -I .\install_agent_office.py rollback --transaction-id <32-hex-id>
python -I .\install_agent_office.py recover --transaction-id <32-hex-id>
python -I .\install_agent_office.py skill-pin
```

`skill-pin` is read-only. It compares the skill fingerprint derived from
`skill/agent-office/SKILL.md` with the pin that `global/codex/skills-ownership.json`
holds in `marker_fingerprint` and `receipt_fingerprint`, and when that pin is
stale it names the stale fields and prints the exact value to write. It never
edits the ownership file: that layer repins it deliberately.

Use `--python-executable <absolute-python.exe>` before the command when an
explicit pin is required. Dry-run performs no writes. Install receipts and
exact config pre/postimages stay under
`%USERPROFILE%\.agents\agent-office-deploy\transactions\`; they are not part
of the source backup. Config images are encrypted for the current Windows user
with DPAPI; receipts contain hashes and provenance, not config payloads.
Runtime code and the safe wrapper are installed under
`%USERPROFILE%\.agents\runtime\agent-office\`. The durable coordination home
remains `%USERPROFILE%\.agents\coord`, is pinned explicitly in the wrapper and
both native server commands, and is never an installer target.

The installer adds only these native registrations:

- Claude: `agent-office` in `%USERPROFILE%\.claude.json`, plus only
  `env.MCP_TOOL_TIMEOUT` in `.claude\settings.json`. A valid existing numeric
  value at least 300000 ms is preserved; when absent the value is 100000000.
- Codex: one bounded `[mcp_servers.agent-office]` section with
  `startup_timeout_sec = 30`, `tool_timeout_sec = 300`, and `required = false`.

All unrelated JSON bytes and TOML text/comments are retained during install,
and exact preimages are restored by rollback only when every live postimage is
still unchanged. No hook, PATH entry, account, remote host, network listener,
or running client session is changed. Existing sessions are not restarted.

### The installer is source-side only

`install_agent_office.py` is the distributor. It is deliberately NOT part of
the distributed payload: it is absent from both `RUNTIME_SOURCE_MAP` and
`SKILL_SOURCE_MAP`, so it is never copied into
`%USERPROFILE%\.agents\runtime\agent-office\` nor into any skill root. It lives
only in this repository and runs from here. Package identity is unchanged by
verification work on it: `PACKAGE_VERSION` stays `0.1.1`, and `package.json`,
the runtime, the skills, the markers, and the ownership/config records are byte
for byte untouched. No new installer version, upgrade protocol, or reinstall
flow is introduced by the verify correction below.

### Semantic verify versus full-file mutating CAS

The three shared-config targets — `.claude.json`, `.claude\settings.json`, and
`.codex\config.toml` — are owned only in part: the installer owns the
`agent-office` MCP entry and the timeout keys, while the native tools own the
rest of the file. A native Claude CLI run, for example, appends a top-level
`skillUsage` key to `.claude.json` and leaves `mcpServers["agent-office"]`
exactly as installed.

* `verify` (standalone) is semantic for these three targets. Each still has to
  read the live file, pass the plain path/reparse checks, and validate its
  Agent-Office-owned entry exactly (Claude MCP entry, Claude
  `env.MCP_TOOL_TIMEOUT`, Codex `mcp_servers.agent-office`). If the owned
  semantics are current, `verify` stays `CURRENT` even when the whole-file
  bytes differ from the recorded postimage, because an unrelated foreign field
  changed the hash without touching what the installer owns. The report keeps
  the two facts separate and visible without printing any config value: the
  per-target `current`/`expected` hashes disagree, the target carries
  `whole_file_equal: false` with `unrelated_field_drift: true`, and a top-level
  `unrelated_config_drift` list names the affected targets. Runtime code, every
  skill tree, the ownership markers, the DPAPI receipt images, malformed or
  duplicate JSON/TOML, an owned-field change or removal, a deleted or
  non-plain config file, and any extra payload byte in a managed tree (a foreign
  `.py` file, for example) are NOT tolerated and still fail as `DRIFT`.
  `__pycache__` directories and `.pyc`/`.pyo` files are the one exception: they
  are interpreter output, not package payload, so they are ignored rather than
  deleted. Without that filter, using Office blocked upgrading it — a day of
  normal use left nine cached files in a 21-file deployment and `upgrade`
  refused with `Upgrade target drift blocks the transaction: runtime` until the
  owner moved the cache by hand. Every file the package owns is still compared
  exactly, so changing or removing one still fails as `DRIFT`.
* `install`, `rollback`, and `recover` remain a full-file, byte-exact
  compare-and-set. They compare the whole live target to the recorded preimage
  or postimage before touching anything, so the very same foreign `skillUsage`
  append blocks a repeated `install` — it refuses in preflight, writes nothing,
  and leaves the existing transaction and its `DEPLOYED` receipt in place — and
  blocks `rollback`, forcing each to preserve the file byte for byte rather than
  restore a stale preimage over someone else's edit. Mechanically,
  `verify_install` is strict by default, so every install and recover check (the
  preflight, the lock-held re-check, and the verification after the last write)
  and the whole rollback CAS fail closed on any byte difference; only the
  read-only surfaces, the standalone `verify` command and `dry-run`, pass the
  explicit non-strict flag. Tolerance for unrelated fields therefore never
  reaches a mutating transaction.

In short: semantic `verify` answers "is my owned registration still correct?",
while the full-file CAS answers "is the whole file still the exact byte image I
staged?". A concurrent foreign config edit satisfies the first and is rejected
by the second, by design.


The skill is installed once per actual scan root:

- `%USERPROFILE%\.agents\skills\agent-office`: Codex, Kimi Code, OpenCode,
  and ZCode;
- `%USERPROFILE%\.claude\skills\agent-office`: Claude and MiniMax;
- `%USERPROFILE%\.gemini\config\skills\agent-office`: Antigravity;
- `%USERPROFILE%\.grok\skills\agent-office`: Grok;
- `%USERPROFILE%\.qwen\skills\agent-office`: Qwen.

There is no duplicate `%USERPROFILE%\.codex\skills\agent-office` target because
Codex discovers the shared `.agents\skills` root. Kimi Desktop is intentionally
not given a duplicate loose copy. Remote Hermes is not installed, configured,
inferred, or contacted.

After install, use the explicit wrapper rather than changing global PATH:

```powershell
& "$env:USERPROFILE\.agents\runtime\agent-office\office.cmd" --harness <tool> presence --status busy
& "$env:USERPROFILE\.agents\runtime\agent-office\office.cmd" --harness <tool> read
& "$env:USERPROFILE\.agents\runtime\agent-office\office.cmd" wait --agent X --timeout 240
```

Claude can derive a strong identity from `CLAUDE_CODE_SESSION_ID`, and Codex
from `CODEX_SESSION_ID` or `CODEX_THREAD_ID`. Other local clients should
provide `--session-id` or `COORDFS_SESSION_ID`; otherwise Office reports the
process-stable fallback as weak. A pending wait can wake, but an
ended model turn cannot wake itself. Native Claude-to-Claude injection,
scheduling, and auto-reply are not implemented.

Source verification commands (the parent acceptance run owns execution):

```powershell
python .\tests\run_tests.py
python .\tests\run_office_stress.py
python ..\..\global\codex\backup\test_shared_skills_backup.py
```

### Accepted boundaries: projects, activity, journal, active work, conflicts, projection

The five accepted modules (`coordfs/projects.py`, `coordfs/journal_bridge.py`,
`coordfs/work.py`, `coordfs/conflicts.py`, `coordfs/projection.py`) are exposed
through the existing Office API, the stdio MCP server, and the CLI below. They
create no second store and no second authority: every helper is bound to the
same Office home (and the same inbox/rate/retention tuning), so the registry
keeps only routing metadata, the journal only schema-shaped receipts, the work
board only lifecycle records, the advisor only warning-only views, and the
projection only a pure read-only mapping.

#### Project-scoped transport

```
python -I office.py --harness <name> project-register --project-id <id> --checkout <abs-path> [--alias <a>]
python -I office.py --harness <name> project-resolve --project <id-or-alias>
python -I office.py --harness <name> project-stream --project <id> --task <t> --thread <th> --run <r>
python -I office.py --harness <name> project-activity --project-id <id> --task <t> --thread <th> --run <r> --activity-json '<typed activity object>'
office.cmd --harness <name> project-activity --project-id <id> --task <t> --thread <th> --run <r> --activity-file <path-to-typed-json>
python -I office.py --harness <name> project-activity-read --project-id <id> --task <t> --thread <th> --run <r> [--limit 50]
python -I office.py --harness <name> project-send --project-id <id> --task <t> --thread <th> --run <r> --recipient-json '{"harness":"codex","host":"h","session_id":"s"}' --body "<text>"
python -I office.py --harness <name> project-send-channel --project-id <id> --task <t> --thread <th> --run <r> --channel <name> --body "<text>"
python -I office.py --harness <name> project-read --project-id <id> --task <t> --thread <th> --run <r> [--peek]
python -I office.py --harness <name> project-wait --project-id <id> --task <t> --thread <th> --run <r> [--timeout 30]
python -I office.py --harness <name> project-acknowledge --project-id <id> --delivery <id>
python -I office.py --harness <name> project-delivery --project-id <id> --delivery <id>
python -I office.py --harness <name> project-cancel --project-id <id> --delivery <id>
python -I office.py --harness <name> project-audits --project <id>
```

MCP mirrors: `office_project_register`, `office_project_resolve`,
`office_project_stream`, `office_project_activity`,
`office_project_activity_read`, `office_project_audits`, `office_project_send`,
`office_project_send_channel`, `office_project_read`,
`office_project_read_channel`, `office_project_wait`,
`office_project_wait_channel`, `office_project_acknowledge`,
`office_project_delivery`, `office_project_cancel`.

- A project id binds to exactly one checkout; a checkout never rebinds. Routes
  (inbox address, channel name, cursor, pipe namespace) derive deterministically
  from project plus task/thread/run plus runtime identity, never from the
  working directory. Identical local names in two projects cannot share
  inboxes, cursors, recipients, or wake signals.
- The sender is the server identity by default. An explicit `--agent` identity
  carries no harness/host/session parts, so project send/read/wait/ack commands
  then require `--sender-harness/--sender-host/--sender-session` together
  (MCP: `harness`/`host`/`session_id`); partial triples are refused rather than
  silently addressed. A recipient is always an explicit
  `{harness, host, session_id}` mapping; no interface accepts `from`.
- `project-activity` is the provider-neutral lifecycle ingress for adapters that
  need to publish a project/task/thread/run event. Its object is intentionally
  closed: `source_event_id`, allowlisted `provider_id`/`kind`/`status`, bounded
  `summary`, `raw_method`, optional model/role, and numeric usage only. The
  server derives identity, timestamp, and sequence; it rejects prompt-like
  text, executable instructions, secrets, raw bodies, and unknown fields before
  writing. Repeating the same source id with the same payload is a safe
  deduplication; changing the payload is a visible conflict.
- Activity is stored with the canonical project registry under the existing
  Office home and is read through `project-activity-read`; it is not a second
  network service, a message body, or an automatic authorization signal. An
  unavailable Office remains fail-open for the caller, so adapters must treat a
  `degraded` result as telemetry loss and continue their actual work.
- Durable project delivery is not an application ACK: `project-acknowledge` is
  a separate, receiver-side record, retries stay manual-only, and delivery
  state is inspectable without side effects. Authority-shaped bodies are
  recorded as audit rejections (`project-audits`), never executed or
  forwarded. Project waits use the same local named-pipe wake and durable-log
  recheck; over MCP they run on the blocking worker pool, bounded by the same
  240 s ceiling, with cancellation taking effect at completion.

#### Work journal

```
python -I office.py --harness <name> journal-export --record-json '<office-work.v1 record>'
python -I office.py --harness <name> journal-checkpoint --record-json '<checkpoint-carrying record>'
python -I office.py --harness <name> journal-read-checkpoint --project-id <id> --task-id <t> [--checkpoint-id <c>]
python -I office.py --harness <name> journal-audit
python -I office.py --harness <name> journal-reconcile
```

MCP mirrors: `office_journal_export`, `office_journal_checkpoint`,
`office_journal_read_checkpoint`, `office_journal_audit`,
`office_journal_reconcile`.

- The journal stores small schema-shaped receipts under the existing home
  (shape: `docs/office-work.v1.schema.json`). It never sends messages, reserves
  work, or writes to Knowledge Hub. Bounds are visible refusals, not silent
  drops (64 KiB per record, 8 MiB / 4096 records per journal, 1024 checkpoints
  per lineage, 1 MiB audit).
- A checkpoint file is written once and bound to its successor by content hash;
  the bridge exposes no overwrite, partial rewrite, hash update, or delete
  operation. A failed durable write is `UNKNOWN` and is never retried
  automatically. `journal-reconcile` inspects consistency only and repairs
  nothing.

#### Active-work ledger

```
python -I office.py --harness <name> work-register --project <p> --task <t> --checkout <abs-dir> --scope <s> [--scope ...]
python -I office.py --harness <name> work-get --project <p> --task <t>
python -I office.py --harness <name> work-list [--project <p>] [--checkout <dir>]
python -I office.py --harness <name> work-update --project <p> --task <t> [--status ready] [...]
python -I office.py --harness <name> work-blocker-add --project <p> --task <t> --blocker <b> --reason "<text>"
python -I office.py --harness <name> work-blocker-transition --project <p> --task <t> --blocker <b> --status resolved --reason "<text>"
python -I office.py --harness <name> work-plan-record --project <p> --task <t> --from-agent <a> --to-agent <b> --scope <s> --summary "<text>"
python -I office.py --harness <name> work-plan-list --project <p> --task <t>
python -I office.py --harness <name> work-plan-acknowledge --project <p> --task <t> --plan <id> --ack-agent <a> --status accepted [--note "<text>"]
```

MCP mirrors: `office_work_register`, `office_work_get`, `office_work_list`,
`office_work_update`, `office_work_blocker_add`,
`office_work_blocker_transition`, `office_work_plan_record`,
`office_work_plan_list`, `office_work_plan_acknowledge`.

- The ledger records intent, blockers, and scoped handoffs. It is not an
  authority engine: it never runs Git, changes a checkout, claims or releases
  a lease, retries a conflict, or changes permissions. Task bindings are
  immutable once written; missing commit evidence needs explicit unknown
  reasons. `work-update` changes only supplied fields, and an explicit null
  never clears a field through the CLI/MCP facades (clearing stays a direct
  module operation). A plan ACK is coordination evidence only.

#### Conflict advice and work projection

```
python -I office.py --harness <name> conflict-advise --lease-json '<observed lease result>' [--lineage-json '<requester lineage>']
python -I office.py --harness <name> work-projection --project-id <id> --records-json '[<records>]'
```

MCP mirrors: `office_conflict_advise`, `office_work_projection`.

- `conflict-advise` consumes an already-observed lease result and returns
  warning-only advice (`status=warning`, CLI exit 0, MCP `isError=false`). It
  performs no board operation and its recommendation never authorizes VCS,
  checkout, permission, or production actions.
- `work-projection` is pure and performs no I/O. Its authority and storage
  markers are fixed to `none`: a boundary view, never a second source of
  truth. Conflicting origin ids are exposed as refusals, never silently
  resolved.

#### Exact limitations of the new boundaries

- A project delivery receipt, a journal checkpoint, or a work-plan ACK is
  coordination evidence between local agents. None of them resumes a provider
  session, starts a provider turn, or wakes an ended turn; there is still no
  scheduler or automatic new-turn launcher.
- Project waits over MCP report cancellation at completion; a cancelled legacy
  `office_wait`/`office_ask` still interrupts promptly.
- The installer now also distributes the five boundary modules
  (`coordfs/conflicts.py`, `coordfs/journal_bridge.py`,
  `coordfs/projection.py`, `coordfs/projects.py`, `coordfs/work.py`) with the
  runtime. It still writes no secrets and configures no remote host; verify
  with `python -I .\install_agent_office.py --dry-run` before installing.
