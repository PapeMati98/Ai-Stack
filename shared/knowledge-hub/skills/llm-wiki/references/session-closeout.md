# Every session leaves a trace

## Closing boundary

This applies to every coding, research, operating, review and ordinary agent
session, across all tools, including delegated sessions. A work episode ends
when the agent delivers a result, hands work over, stops on a blocker, knowingly
interrupts work, or waits for a user decision. Do not wait for the chat window to
be deleted or for nightly dreaming. Do not record each tool call, poll, progress
message or token chunk. The closeout operation itself is not a new work episode.

Keep the native session ID and a stable checkpoint ID in the existing task
evidence/outbox. Use the same IDs and bytes when retrying the same closeout. On
resumption after new work, select a new checkpoint ID; old entries are append-only.
The gateway stores only session/checkpoint digests, not raw session labels. IDs
are correlation, not authenticated identity. Never borrow another agent's ID.

## One command, no extra model

Resolve `canonical_skill_path` and `vault_path` from the normal configuration.
For a material result prepare one short, redacted Markdown note using
`assets/session-closeout-template.md`. Include result, important decisions and
constraints, evidence, blockers and next step. Capture unfinished work as such.
Do not copy a transcript, prompts, hidden reasoning or restricted content.
The command accepts at most 16 KiB; aim for 5-10 concrete lines.

```powershell
python "<canonical-skill>/scripts/knowledge.py" record-session --agent codex --session-id "<native-id>" --checkpoint-id "<episode-id>" --project-slug "<canonical-project>" --outcome COMPLETED --summary "<actual result>" --path "<redacted-note.md>" --context-receipt "<pre-task-receipt>"
```

Use `BLOCKED` or `INTERRUPTED` when appropriate. The returned
`receipt_status=COMPLETE` means that the **entry** was recorded, not that the work
was successful. The `outcome` stays separate. Keep the actual source/proposal/event
IDs and checkpoint result in task evidence; do not invent a receipt line.

When there was no durable change, no additional note or free-form summary is
accepted. The journal still exists, but requests no wiki page:

```powershell
python "<canonical-skill>/scripts/knowledge.py" record-session --agent grok --session-id "<native-id>" --checkpoint-id "<episode-id>" --outcome NO_DURABLE_CHANGE --project-slug "<project-if-known>"
```

Omit `--project-slug` only for genuinely non-project no-change sessions. Never
invent a project slug to satisfy the command. Pass `--context-receipt` whenever
there was a pre-task read, also for a no-change project session. Without it the
receipt explicitly says `lifecycle_binding=unverified`; writing does not certify
that the agent consulted existing knowledge. Expired but valid context is labeled
stale without preventing the final write.

Windows `.cmd`/PowerShell equivalent:

```powershell
& "<canonical-skill>/scripts/knowledge.cmd" -Action record-session -Agent kimi -SessionId "<native-id>" -CheckpointId "<episode-id>" -ProjectSlug "<project>" -Outcome BLOCKED -Summary "<blocker>" -Path "<redacted-note.md>" -ContextReceipt "<pre-task-receipt>"
```

### Optional: keep the bounded note on disk (retain mode) and confine session locks

By default `record-session` uses the platform's temporary directory and cleans
it up automatically. When an audit, postmortem or external witness needs the
exact bytes the producer handed the vault, pass an explicit absolute directory
with `--retain-temp-root` (or `-RetainTempRoot` on PowerShell). In that mode:

- The gateway creates one unique subdirectory under the supplied root, writes
  the bounded note there, and never probes system TEMP.
- `--retain-temp-root` also confines session-locks under `<retain_root>/session-locks/<vault_sha256>/<key>.lock` without touching `Path.home()` or `~/.agents`.
- Explicit `--receipt-root` or `--lock-root` (PowerShell: `-ReceiptRoot` or `-LockRoot`) can also be passed to `record-session` to confine session locks under an explicit root (with `--lock-root` taking precedence over `--receipt-root` and `--retain-temp-root`).
- No cleanup, finalizer, or watchdog deletes the scratch afterwards; the note
  stays until the operator moves or removes it explicitly.
- The successful receipt carries a concrete `retained_session_note` field with
  the absolute path of the unique scratch directory.
- The supplied root must already exist as a regular directory; relative paths,
  files, and symlink/junction/reparse roots are refused before any vault write
  and the gateway never silently falls back to system TEMP.
- This adds no scheduler, hook, timer, model call, or approval gate; the
  caller decides when the file is moved. Plan for disk space: the retained
  scratch persists until the operator reclaims it.

```powershell
python "<canonical-skill>/scripts/knowledge.py" record-session --agent codex --session-id "<native-id>" --checkpoint-id "<episode-id>" --project-slug "<canonical-project>" --outcome COMPLETED --summary "<actual result>" --path "<redacted-note.md>" --retain-temp-root "C:\ai-lab\auditor\notes"
```

## Critical handoff and noise

A deployment, host, access, storage, external integration or contradictory state
still requires the stronger `record-handoff` path immediately. Do not relabel it
as an ordinary journal entry. A final `COMPLETE` handoff already covering this
agent/episode, exact final state and evidence can serve as the one session entry;
inspect the actual receipt and bound note before reuse. A stale handoff, another
agent's result or an unbound unverifiable claim cannot be reused on assumption.

The noise gate applies to **promotion of claims**, never to whether an episode
leaves a trace. Ordinary session entries remain producer reports in raw/proposals.
`NO_DURABLE_CHANGE` has no stable page target and should be accounted as historical
journal data during authorized consolidation, not sent through a model review by
itself. The graph is not expected to have one wiki node per session.

## Retry, crash and connected tools

- Exact same checkpoint and bytes returns the same source/proposal IDs. Changed
  bytes under the same key return `SESSION_CONFLICT`; use a new checkpoint only
  for an actual new episode, not to hide an unresolved write failure.
- A failed checkpoint preserves the pair. Retry the same call or the existing
  `checkpoint-intake` recovery. Keep the original redacted note until success.
- If lint, storage, ACL or transport prevents capture, report
  `knowledge_closeout=BLOCKED`; preserve the note and IDs in existing local task
  evidence/outbox. Do not claim Hub completion or bypass the gateway.
- A hard crash cannot run an agent's final instruction. Its missing closeout is
  unrecorded, not a fabricated `INTERRUPTED` entry. On the next supervised resume
  or through an already approved connector, reconstruct only evidence-backed
  state and retry. This skill does not install a universal crash hook or timer.
- A parent may publish a contractor closeout only with the actual producer and
  episode attribution plus the received evidence; account for each contractor,
  not one vague group summary. Never label a parent's read receipt as the child's.
- Claw Ops dreaming may reconcile missed admitted sessions through its connectors,
  but is not a substitute for this immediate closeout. No new inputs means no
  extra model calls. Installing this skill does not prove connector coverage.
- Robert/Hermes uses the same command in his actual AI Lab Server environment
  and the already approved `sync-robert.sh` transport. A local Windows receipt
  does not prove the remote server received it.

Report three distinct levels: policy present, adapter bytes installed, and an
actual observed write/read in that runtime. Only the last is runtime evidence;
none proves that every future session will comply automatically.
