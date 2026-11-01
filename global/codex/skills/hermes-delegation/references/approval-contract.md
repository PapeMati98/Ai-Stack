# Approval contract

Sensitive Hermes work uses a one-time approval receipt tied to the SHA-256 of
the complete task contract. Approval never transfers from one contract to
another.

## Trust boundary

The bridge mechanically rejects a missing, expired, consumed or contract-mismatched
receipt. It also revalidates and consumes the receipt immediately before the
queued Hermes process starts. This prevents contract mutation, delayed use and
ordinary replay through the bridge.

The bridge is not an operating-system sandbox around Robert's tools. After an
approved run starts, compliance with recipient, command and action-count details
is enforced by the bounded prompt, selected toolset, Hermes skills and parent
verification. A principal with direct shell access as `operator` could bypass
this bridge entirely. Therefore SSH access to that account remains a trusted
administrative boundary, and high-impact actions still require post-effect
verification. Do not describe the receipt as cryptographic proof of human
presence or as protection against a compromised server account.

## Sensitive modes

Approval is mandatory for:

- `PRIVATE_READ`
- `INFRA_OBSERVE`
- `PREPARE_PATCH`
- `INFRA_CHANGE`
- `EXTERNAL_ACTION`
- `SCHEDULED_AUTONOMY`

Only `REPORT_ONLY` and `RESEARCH_ONLY` are non-sensitive, and their worker
toolsets are mechanically narrowed. A request that crosses either boundary
must be replanned in a sensitive mode.

## Action manifest

Every sensitive plan requires a JSON action manifest:

```json
{
  "action_type": "send-email",
  "scope": "Send one approved completion message",
  "max_actions": 1,
  "sender": "approved-account-alias",
  "recipients": ["recipient@example.com"],
  "cc": [],
  "bcc": [],
  "subject": "Approved subject",
  "body_sha256": "<64 lowercase hex characters>",
  "attachment_sha256": []
}
```

Do not put secrets in the manifest. Account aliases and recipient addresses are
scope metadata, not credentials.

Required fields by action type:

| `action_type` | Required binding |
|---|---|
| `send-email` | sender, recipients, subject, body SHA, attachment SHAs, max actions |
| `private-read` | account alias, data kind, exact query/scope, maximum items |
| `infra-observe` | target, purpose and read scope |
| `prepare-patch` | repository, isolated worktree, allowed paths and test commands |
| `calendar-write` | calendar/account, title, start, end, timezone, max actions |
| `production-change` | target, operation, expected effect, rollback, max actions |
| `vm-control` | VM name, operation, preconditions, rollback/recovery, max actions |
| `scheduled-job` | target, schedule, action, expiry, maximum runs |
| `account-change` | account alias, exact setting/action, rollback, max actions |
| `public-message` | channel, audience/recipients, content SHA, max actions |

For another sensitive action, use `other-sensitive` and include `target`,
`operation`, `expected_effect`, `rollback` and `max_actions`.

Every sensitive contract uses `max_actions: 1`. Repeated effects require a new
contract and a new approval. A remote append-only claim keyed by the complete
contract hash prevents reuse even if a task directory is later removed.

## Human approval sequence

1. Generate the final contract with `plan`.
2. Show Mateusz a short summary of the exact external or infrastructure effect.
3. Show the `approval_phrase` returned by the client.
4. Stop until Mateusz supplies that exact phrase in the current conversation.
5. Run `approve` with the exact phrase.
6. Submit once. The local receipt is consumed after remote acceptance; its
   remote copy is revalidated and consumed immediately before execution.

Do not treat these as approval:

- general statements such as "dzialaj" made before the final contract existed;
- approval for a draft, research or earlier version;
- an approval phrase copied from an old task;
- approval inferred from the user's normal high-autonomy preference;
- Hermes asking another agent to approve its own action.

## Verification after effects

- Email: confirm sender alias, recipients, subject and provider message ID or
  Sent-folder record; do not quote the body back unnecessarily.
- Calendar: read the created/updated event and verify timezone and idempotency.
- Infrastructure: run independent health/state checks and verify rollback.
- VM: verify `virsh domstate` or guest reachability, not only command exit code.
- Schedule: list the installed job, cadence, next run, expiry and disable path.
