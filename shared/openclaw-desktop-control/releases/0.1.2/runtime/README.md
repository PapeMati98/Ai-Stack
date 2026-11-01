# OpenClaw runtime (T4/T5/T7 local controller)

Stdlib Python, one long-lived Windows process. Telegram long-poll + Codex
Desktop adapter. **Disabled by default.** This slice does not activate a live
bot, open a public port, start Desktop, or call a model.

## Run

Token is read only from the process environment key `TELEGRAM_BOT_TOKEN`.
Owner IDs and paths come from the parent-supplied JSON. Never put the token
in the config file, argv, logs, or scheduled-task XML.

```
python project/runtime/run.py --config PATH --status
python project/runtime/run.py --config PATH --once
python project/runtime/run.py --config PATH --serve
```

`--status` prints a versioned JSON envelope (hashes/ids only). `--once`
does one recover/poll/dispatch pass. `--serve` is a quiet single-instance
loop. A second process exits with `already-running`.

Copy `config.example.json` and keep every `enabled` flag false until the
parent wires a live session.

## Integrated native path

The normal entrypoint constructs the pinned installed Codex AppTools client,
checks the exact task read-only, and wraps `CodexDesktop`. Set `server_image`
to the verified absolute Codex executable path. Discovery requires the same
Windows user and pinned executable; different server processes are ambiguous
and refused. Several pipes of the same process are probed read-only, because
some expose only browser controls. Every prompt still names an enrolled exact
thread and a read-only rollout file below `sessions_root`.

`telegram.initial_update_offset` skips pre-pairing updates only when no durable
cursor exists. STOP persists across restarts; the owner can explicitly resume
future sends using a fresh button. Menu/status sends persist their intent before
the network call. Uncertain sends are not retried. Native observations continue
after ACK and after controller restart, using a durable delivery-attempt time.
One unresolved dispatch prevents later sends to the same native thread.

The protected launcher accepts a path to an existing DPAPI CurrentUser blob;
the token is passed to Python only through its process environment and is
removed from the child MCP client environment. Config has no token. The
PowerShell launcher's location is `windows/start-protected.ps1`.

CLI audit/repair dispatch, the WSL coordinator channel, automatic goal
continuation and authenticated legacy CRM-collector reads remain unwired. Their presence as
injected hooks is not a claim that the full owner workflow is deployed.

## Capabilities that are NOT claimed

- Native cancel is absent. STOP blocks **new sends**. A running turn stays
  `cancel_pending` until a native confirmation exists.
- Receiver CAS is absent. Broker fencing is not last-turn CAS.
- Status API omits latest turn items. Exact match / start / result use a
  bounded read-only rollout tail.
- UNKNOWN is reconciled by readback only. Never resent.
- Claude Desktop is unverified. Generic registration stays disabled. No
  CLI fallback.
- Idle is not semantic goal completion.
- PID or named pipe unavailable ⇒ queued/blocked alarm. No second app-server.
- No webhook takeover (`setWebhook` / `deleteWebhook` are not used).
- No public listener and no WSL listener in this slice.

## Rollback

1. Press STOP in Telegram, or run the transport CLI `stop --scope all`.
2. STOP is persisted in the authority SQLite file and survives reboot.
3. Do **not** delete the authority database. Queued prompts, inbox, and
   outbox stay put.
4. `runtime/windows/rollback.ps1` documents STOP + preserve-state only.

See `windows/` for start/stop/install templates (metadata only, no secrets).
The DPAPI launcher is a **template** — it must not be filled with a live
token in this repository.
