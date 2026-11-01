# Operations and recovery

## Runtime topology

The client runs on Mateusz's Windows workstation. It invokes the native Hermes
CLI on `lab-server` over SSH. No Hermes HTTP API is used.

Remote root:

```text
/srv/ai-lab/hermes-standalone/home/.hermes/delegation-bridge/
  current -> releases/<transaction-id>/
  releases/<transaction-id>/hermes_remote_worker.py
  deployments/<transaction-id>.json
  approval-claims/<contract-sha256>.json
  tasks/<task-id>/
```

Each task directory contains contract, status, redacted result, stdout/stderr
artifacts and hashes. Files are user-private. The worker launches one transient
user systemd unit per task and serializes execution with a lock to avoid
concurrent pressure on Hermes state and the shared subscription.

## Readiness

Run:

```powershell
python scripts/hermes_delegate.py doctor --live
```

Static checks verify SSH identity, worker fingerprint, systemd user session and
Hermes version. The live probe starts one minimal native Hermes CLI turn. Treat
these separately:

- static pass + live pass: `READY`
- static pass + HTTP 401/token expired: `BLOCKED_AUTH`
- static pass + missing tool backend: the bridge works, but that capability is
  unavailable
- worker absent: install or recover the remote worker

Do not run `hermes doctor --fix`, modify `.env`, migrate config or replace
credentials automatically. Reauthentication requires Mateusz at the native
Hermes login/model flow on AI Lab Server.

Static readiness also requires Hermes `approvals.single_query_mode=deny`.
Single-query automation must never inherit an auto-approve setting for dangerous
terminal commands.

## Transactional remote install

`install-remote` uploads a versioned worker release, verifies SHA-256, records
the previous `current` target and atomically switches the symlink. A failed
post-activation doctor, identity check, fingerprint check or deployment-record
write automatically restores the previous symlink target or exact absence. It
does not restart the Hermes gateway, dashboard, Docker, VMs or applications.

`verify-remote` compares the live worker fingerprint to the bundled file.

`rollback-remote --receipt ...` restores the exact previous symlink target or
exact absence. Releases are retained for evidence; removing old releases is a
separate maintenance action.

Both manual deployment and manual rollback require an exact current phrase
shown by the client and supplied by Mateusz after seeing the effect. Automatic
recovery from a failed approved deployment restores only the recorded previous
target and needs no second prompt.

## Failure recovery

- `BLOCKED_AUTH`: reauthenticate Hermes; do not resubmit repeatedly.
- `FAILED`: inspect the redacted result and stderr SHA. Do not fetch raw output
  if it may contain mailbox or customer data.
- `TIMED_OUT`: inspect status, then cancel the exact task ID if still active.
- `QUEUED`: another bridge task may hold the single-worker lock.
- no result but unit ended: run `status`, then `doctor`; treat as bridge failure.
- identity mismatch: stop. Do not bypass expected host/user checks.

Cancel and timeout stop future bridge work; they cannot unsend mail or reverse
an effect already completed. Every approved scheduled/infra manifest therefore
needs an explicit disable or rollback path, followed by independent verification.

## Current capability caveat

An enabled toolset in `hermes tools list` is configuration intent, not a live
backend guarantee. Use `hermes doctor` or a bounded canary for the capability
needed by the task. In particular, mail/calendar access can be skill-backed
while generic web/browser/image generation remains unavailable.
