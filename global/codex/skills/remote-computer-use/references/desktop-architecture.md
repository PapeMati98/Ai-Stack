# Desktop architecture 0.2

## Runtime

The SSH server launches commands in Windows Session 0. Native desktop input and
screen capture therefore run through one fixed Task Scheduler task configured
with `InteractiveToken` for the dedicated UAT user. The task has no trigger and
starts only after the Session 0 wrapper acquires the global lease and writes a
validated request under that lease.

The broker:

1. re-checks canonical paths and configured identity;
2. requires its session ID to equal the active console session;
3. captures `before.png` and session/process metadata;
4. executes the bounded contract;
5. records a redacted action ledger and final evidence;
6. closes only its own allowlisted target process and exits.

The wrapper polls the result, stops the managed task on timeout, and releases
the lease. No command or input is transported through a listening socket.

## Desktop contract

Schema version 2 adds:

- `capability`: `observe` or `uat`;
- `target`: the whole desktop for observe, or one configured `app_id` for UAT;
- actions: screenshot, wait, launch, move, click, text, safe key, close target.

`observe` requires `side_effects=none`. `uat` requires
`side_effects=test-only` and exactly one launch action matching the target app.
The VM policy is authoritative: the local validator cannot add an app.

## Provisioning status

Package 0.2 can be deployed while desktop mode stays disabled. Enabling requires
an isolated interactive account and a proven VM checkpoint/restore path. The inert example uses `EXAMPLE-VM\ExampleUser`; it is not an existing account
or permission grant. The account is not created by the
skill because account creation, session migration, and credentials are owner
infrastructure decisions.

The KVM host alias and guest domain are deployment-specific control-plane
inputs. Check the actual disk layout and guest-agent availability. Never claim
a live quiesced checkpoint without a working, verified guest agent; otherwise
stop the guest cleanly before creating a restorable recovery baseline.

Activation is a separate transaction through
`scripts/configure_vm_desktop.ps1`. It refuses the legacy ambient-account name as well as unsafe account state,
administrator membership, OneDrive or browser-login artifacts, a missing
checkpoint, a non-console identity, an active runner lease, and config drift.
