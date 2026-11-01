# openclaw-control (T2/T3 TEST candidate)

Stdlib-only Python package: exact-Unicode owner transport, durable
delivery, owner-gated Telegram intake, and a JSON CLI. This is a
**candidate**: synthetic transports only until native Desktop capability
evidence lands. No live messages, no credentials, no model calls.

Run from this directory:

```
python -m unittest discover -s tests
python -m openclaw_control.cli --db /tmp/ctl.db status
```

No third-party dependencies. No network during tests.

## Reuse boundary

The package opens the **same SQLite file** as `openclaw_ops.Ledger`
(the authority host) and adds only `cc_*` tables
(`cc_dispatches`, `cc_updates`, `cc_pairing`, `cc_callbacks`,
`cc_stop`, `cc_status`, `cc_takeover`, `cc_receipts`, `cc_messages`,
`cc_selection`). It never creates, migrates, or writes the Ledger
`tasks`/`intents`/`events` tables: generations, grants, and goal
progress stay authoritative in `openclaw_ops`. No second goal
scheduler lives here.

## What it does

- **Exact prompt** (`store.py`, `delivery.py`): the owner text is
  stored verbatim -- no trim, no NFC/NFD normalisation, no CRLF
  changes, no Markdown rendering, no model rewrite. SHA256 is computed
  over the exact UTF-8 bytes. Entities travel separately. The stable
  target binding id is
  `SHA256(provider || 0x00 || native_thread)`.
- **Delivery** (`delivery.py`): `queued → dispatching → delivered →
  running → completed / blocked / failed`, plus `delivery_unknown`,
  `cancel_pending`, `cancelled`. ACK, turn start, and result are three
  distinct facts. Claims are transactional (`BEGIN IMMEDIATE`), so
  concurrent workers cannot double-claim. A crash/orphaned claim
  becomes `delivery_unknown` via `recover` -- never an automatic
  resend. Exactly one alarm is due 120 s after uncertainty
  (`pop_unknown_alarm`); the owner reconciles explicitly
  (`reconcile --delivered true|false`).
- **STOP / cancel**: persisted `cc_stop` (`all` or `task:<id>`)
  blocks new dispatches and turn starts, and survives reboot.
  Cancel is `request → cancel_pending → cancelled` ONLY on adapter
  confirmation; silence stays `cancel_pending`, never "stopped".
- **Takeover**: `takeover` records the new host; the next step must
  re-read fresh native status (older than 10 s is rejected).
- **Adapters** (`adapters.py`): explicit `Protocol` with five
  capability flags. `require_capable` fails closed. Payloads starting
  with `/`, `!`, `@` stay literal; an adapter without proven literal
  delivery gets an `AdapterRefusalError` (the dispatch is `blocked`,
  the text untouched -- never escaped or reinterpreted).
  `FakeControlAdapter` is synthetic/test-only (idempotent deliver,
  exact readback, fencing, one-shot ACK-loss and stop-refusal
  scripts). `DisabledAdapter` is every unknown Desktop adapter:
  visible, permanently disabled until native evidence lands.
- **Telegram intake** (`telegram.py`): private owner chat + sender
  allowlist only; groups, bots, `via_bot`/`sender_chat`, and any
  forwarded content are rejected. Text stays literal (even `STOP`);
  STOP happens only via `stop` callback or the control API. Target
  selection is a separate callback action BEFORE/AFTER the text, never
  parsed out of it. Edits are new `correction` dispatches linked via
  `correction_of`. Duplicate `update_id`s are durable no-ops;
  out-of-order updates are accepted. Voice/oversize/attachments yield
  a persisted explicit unsupported receipt -- never truncation.
- **Pairing** (`pairing.py`): local expiring one-use nonce, redeemed
  for a verified private `(sender, chat)`; reuse/expiry/non-private
  rejected. No tokens or credentials are stored.
- **Status** (`status_text.py`): one short Polish message per
  operation, hard-capped at 600 chars, edited in place (`send`/`edit`
  decisions), identical text skipped, uncertain sends wait instead of
  blind-retrying.
- **Bot API** (`botapi.py`): HTTPS client with an injected opener
  (default: no-redirect urllib), allowlisted methods only
  (`sendMessage`, `editMessageText`, `answerCallbackQuery`), bounded
  responses, numeric error codes only, token read from the
  caller-injected environment (`TELEGRAM_BOT_TOKEN`) -- never argv --
  and fully redacted exceptions.
- **CLI** (`cli.py`): every command prints one versioned JSON
  envelope (`{"schema": "1.0", "kind": ..., "payload": ...}`).
  Diagnostics carry hashes/references only; raw text is available
  ONLY via the explicit `readback` receiver path.

## Supported vs unconnected

| Adapter | Status |
|---|---|
| `FakeControlAdapter` | Synthetic/test only. |
| `NonLiteralAdapter` | Refusal-path test double. |
| `DisabledAdapter` (`unknown-desktop`, any Desktop name) | Disabled: never dispatches, never resends. Real Desktop evidence is proven by the parent, never claimed here. |

## CLI examples

```
DB=/tmp/ctl.db
python -m openclaw_control.cli --db $DB enqueue --task t1 --generation 1 \
  --provider desktop-code --thread thread-7 --text "Zażółć 🙂 /clear"
python -m openclaw_control.cli --db $DB claim --worker w1
python -m openclaw_control.cli --db $DB deliver-one --worker w1
python -m openclaw_control.cli --db $DB readback --dispatch <id>
python -m openclaw_control.cli --db $DB stop --scope all --reason owner
python -m openclaw_control.cli --db $DB status
```

## Library (parent runtime)

```python
from openclaw_control import (
    ControlStore, OwnerConfig, delivery, telegram, pairing,
    status_text, BotApi, FakeControlAdapter, DisabledAdapter,
)
store = ControlStore("/path/to/authority.db")  # same file as Ledger
cfg = OwnerConfig(owner_user_ids=(111,), owner_chat_id=222)
result = telegram.intake_update(store, update_dict, cfg, wall_now)
```

## What this candidate does NOT claim

No live native Desktop capability, no real Telegram delivery, no
exactly-once without receiver support. Green synthetic tests are not
acceptance; acceptance is a real phone run with independent readback.
