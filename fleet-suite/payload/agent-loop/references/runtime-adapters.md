# Runtime adapters

## Distribution model

Agent Loop 0.3.0 is the fourth component of Agent Fleet Suite 0.8.31. One sealed
executable payload is installed at:

`%USERPROFILE%\.agents\runtime\agent-fleet-suite\current\agent-loop`

Native skill roots receive only an instruction adapter that verifies the exact
runtime fingerprint before forwarding to the canonical `SKILL.md` and
controller. Adapters must not fork validation, budgets, locks, evidence,
verification, or recovery logic.

| Runtime | Instruction adapter target |
| --- | --- |
| Codex and Kimi Code | `%USERPROFILE%\.agents\skills\agent-loop` |
| Claude Code | `%USERPROFILE%\.claude\skills\agent-loop` |
| Antigravity | `%USERPROFILE%\.gemini\config\skills\agent-loop` |
| Grok CLI | `%USERPROFILE%\.grok\skills\agent-loop` |
| Kimi Desktop | native `agent-fleet-suite` plugin |

The transactional installer stages and byte-verifies every target, stores
receipts and backups outside scan roots, and retires a prior direct
`%USERPROFILE%\.codex\skills\agent-loop` copy in the same transaction. Exact
rollback restores the prior state. Active drift blocks rollback.

## Host behavior

Every host may discover and orchestrate L1. L2-thin actor certification is
separate from discovery: only Codex and Claude may be selected as implementer
or validator by that controller. L2-search may use any maker that Fleet can
execute with a bounded implementation worktree, but every candidate requires a
different-CLI Quality Gate and unsupported makers fail closed as rejected
attempts. The host parent still frames the contract, verifies Fleet evidence,
records acceptance, and integrates any accepted candidate separately.

Antigravity helper scripts remain canonical payload utilities. Discovery is
read-only. The scheduler helper only constructs a reviewable command contract;
it never registers a Windows task.

## Excluded targets

Qwen is a contractor target, not a Suite adapter target. It is invoked only
through the verified Fleet runtime and never receives a copied Agent Loop skill.

Robert is the primary Hermes agent on another Windows VM. Hermes needs a
separate owner-run target mapping, transport, identity, fingerprint, discovery,
and rollback approval. Never infer its paths from this workstation or deploy it
through the Knowledge Hub.

## Discovery proof

Deployment verification proves exact bytes and markers. Activation additionally
requires native fresh-process discovery for each local runtime. Do not confuse
the current long-lived session's cached skill inventory with a failed install.
