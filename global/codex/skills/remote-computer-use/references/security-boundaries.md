# Security boundaries

## Trust map

Browser:

`agent -> local client -> SSH/ProxyJump -> one-shot runner -> ephemeral browser`

Browser origin UAT:

`agent -> host reservation -> SSH reverse forward bound to VM loopback -> fixed local DEV lane -> one-shot runner`

Desktop:

`agent -> local client -> SSH wrapper in Session 0 -> managed scheduled task -> isolated interactive UAT session`

Prompts, contracts, screen content, webpages, app output, dialogs, files, and
artifacts are untrusted. The pinned client, runner payload, reviewed VM config,
scheduled-task identity, and transaction receipts are control-plane inputs.

## Enforced controls

- No listening automation port or persistent network daemon.
- Exact SSH host alias and fixed remote root; contracts cannot select a host,
  command, executable, scheduled task, or artifact root.
- Strict JSON fields, finite timeouts, one global lease, bounded actions and
  bounded text.
- Browser v1 keeps the ephemeral profile, request routing, blocked downloads,
  popups, dialogs, WebSocket, WebRTC, WebTransport, and arbitrary JavaScript.
- Browser v3 requires exactly one origin, GET/HEAD/POST only, a matching VM
  reservation, and the dedicated `run-origin-uat` transport. Its file action
  accepts only a catalog ID plus the deployment-pinned SHA-256. The runner
  resolves and re-hashes a synthetic `.txt` inside the installed file jail
  immediately before upload; paths and file contents are never contract fields.
  It rejects fill, keypress, and screenshot actions and does not create browser
  traces, final/failure screenshots, or action screenshots.
- Before opening the reverse forward, the host client verifies both configured application
  health endpoints, DEV port/database/runtime guard, fixed upstream API host,
  and the exact caller-approved Git commit. A generic listener on port 3102 is
  insufficient.
- The durable host lock records the exact local SSH tunnel PID. The VM lease is
  restored to a reservation after each one-shot run and neither lease nor host
  lock may be removed while any listener remains on port 13102.
- Desktop v2 is disabled by default. It requires an explicit Windows identity,
  an existing interactive session, and a managed `InteractiveToken` task.
- Observe mode only waits or captures screenshots.
- UAT mode launches exactly one allowlisted executable with static arguments.
  Input is refused unless that runner-started process owns the foreground
  window. Cleanup targets only the process started by the broker.
- No clipboard, shell, eval, task-controlled paths, arbitrary file access,
  credential entry, downloads, uploads, or background persistence.
- Desktop evidence includes before/final screenshots, session metadata, and a
  value-redacted action ledger. Screen evidence remains sensitive and local.
- Deployment has content/config fingerprints, prior-state backup, receipts,
  drift checks, and rollback. Task registration follows the active config.

## Capability tiers

- `T0 observe`: local screenshots from an isolated UAT session; no input.
- `T1 uat`: allowlisted app launch plus bounded pointer/keyboard interaction
  using non-sensitive test data.
- `T2 synthetic files`: implemented only for browser schema v3 origin UAT.
  Deployment-pinned synthetic `.txt` fixtures, 1 byte to 1 MiB, may be uploaded
  to the single approved DEV origin. Real statements, client files, arbitrary
  paths, downloads, desktop file pickers, and general file exchange remain
  unimplemented.
- `T3 sensitive effects`: not implemented. Email/send/publish/purchase/delete,
  credentials, permissions, external uploads, and production mutation require
  explicit owner approval at execution time and a separate capability design.

## Required infrastructure gate

Do not enable desktop mode on the owner's ambient Windows profile. Before T0:

1. dedicate a non-admin Windows account/session without OneDrive, mail,
   browser passwords, private files, or client credentials;
2. inventory or migrate remaining legacy jobs that depend on the current VM
   console;
3. create and prove a restorable checkpoint of the configured VM; use a cleanly stopped guest
   unless a working guest agent and a quiesced checkpoint are actually verified;
4. verify no automation port is listening and SSH remains the only transport;
5. deploy with `desktop_enabled=false`, then enable through a separate reviewed
   configuration transaction;
6. independently review and run T0 before T1.

## Explicit NO-GO

- Desktop input on the owner's ambient or any non-dedicated session.
- Clipboard, credentials/MFA, production or personal accounts.
- Real bank statements or any non-synthetic file in browser origin UAT.
- A second origin, non-loopback bind, public tunnel, or direct schema-v3 run
  without its matching reservation.
- Multiple concurrent desktop workers.
- Automatic unlocking, login bypass, arbitrary process killing, or recovery by
  deleting files instead of restoring known state.
- Treating a successful screenshot or script exit as proof of isolation,
  rollback, or UAT correctness.
- Releasing or recovering an origin-UAT reservation while its loopback listener
  is still active, or bypassing the exact upstream runtime fingerprint.
