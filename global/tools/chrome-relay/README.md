# Fleet Chrome Relay for Windows

This owned runtime adds a profile-scoped semantic browser backend for assigned
Fleet processes. Python standard library implements the loopback broker, MCP
stdio and a CLI transport. The MV3 extension executes fixed DOM functions.
Agents cannot supply JavaScript, CDP, selectors, profile overrides, capability
tokens or another session's tabs. Existing DOTS registration, stable IDs and
label/value schemas remain unchanged.

The default registered browser UI path is the persistent managed Chrome
profile served by `managed.py` (schema `chrome-relay-managed/1`), with
per-harness native config overlay written by `registration.py`. The legacy
`broker.py --policy` and `launch-session.py` flow stays as an inert
fallback; it is not part of the documented default.

## Default agent browser and fail-closed

Fleet Chrome Relay is the default agent browser for logged-in services in
Codex, Claude and Antigravity. Managed registration targets are Codex
`~/.codex/config.toml` (`codex-toml`), Claude `~/.claude.json` under
`mcpServers` (user scope), and Antigravity
`~/.gemini/config/mcp_config.json` (`mcpServers-json`), all through
`registration.py`. Antigravity has a persistent native entry; MiniMax/Muse
registration is unchanged.

Codex enrollment sets `"required": true` for `fleet-chrome-relay`. `codex exec`
builds the turn's tool list before MCP servers finish starting; the required
flag makes it wait for the relay so its tools are available in that turn.

Managed MCP handles `initialize`, `ping` and `tools/list` locally. It registers
only on the first valid `tools/call`, and releases the browser slot after 20
minutes with no calls and no pending responses. A later call gets a fresh
session ID; tabs owned by the released session no longer belong to that agent.
The limit of 16 clients applies to active browser sessions, not idle MCP
processes. If configuration or harness validation fails at startup, the normal
eight Chrome tools remain visible in an unavailable MCP server. A registration
failure returns the same local blocker without starting a child. Calls after
validation retain an MCP tool result with `isError: true`; its text is JSON
shaped like:

```json
{"content":[{"type":"text","text":"{\"request_id\":\"...\",\"status\":\"denied\",\"data\":{\"reason\":\"relay_unavailable\",\"blocker\":\"BLOCKED_MANAGED_BROWSER_UNAVAILABLE\",\"detail\":\"config\",\"instruction\":\"Agent browser (Fleet Chrome Relay) is not available in this session. Report this exact blocker to the owner and stop the browser part of the task. Do not switch to the owner's private Chrome (claude-in-chrome, Codex Chrome plugin, CDP) or any other browser tool to work around it. After the owner starts the relay, a new session (or MCP reconnect) picks it up.\"}}"}],"isError":true}
```

`detail` is the normalized failure reason (for example `config`, `harness`,
`transport`, `expired`, `bound`, `state_io` or
`broker_unreachable_or_register`). Invalid tool arguments remain JSON-RPC
`-32602` errors. The semantic CLI fallback emits one denied JSON
result with the same `reason`, `blocker` and `detail` for each input line, then
returns code 3 at EOF.

The owner policy allows at most 20 exact HTTPS origins, without wildcards.
Identity-provider hosts are always denied; the owner completes login, SSO and
MFA himself in the agent profile. Changing the allowed origins is a new grant:
STOP is set and the owner must explicitly attach the profile again. If a
origin is denied or a login/MFA page appears, report the exact blocker and stop
the browser part of the task.

## Inert installation and rollback

Run the installer only in the approved local scope, with absolute paths:

```powershell
& 'D:\path\chrome-relay\install.ps1' `
  -SourceRoot 'D:\path\chrome-relay' `
  -InstallRoot '<owned-install-root>' `
  -Version '1.0.0' `
  -PythonExe '<python-exe>'
```

The result is `STAGED_INERT`. The installer creates no browser profile, service,
scheduled task, auto-start, server or global MCP registration. It does not edit
policy files or secrets and never deletes a file. A different existing version
is retained; differing bytes under the requested version fail closed. Rollback
selects a retained previous version in the assigned process's configuration.

Add `-DryRun` to validate source, assignment and any existing version without
creating the destination or writing package files.

An approved process assignment optionally supplies `-ClientCapabilityPath`,
`-NativeSessionId`, `-ProfileId` and `-BrokerBase` together. NativeSessionId is
the actual session identity supplied by the native host. The generated
`assigned-process-<ID>.json` is an inactive registration snippet for that
process only. The private capability JSON has exactly `session_id`, `profile_id`,
`broker_base` and `token` for that one assignment. The controller's complete
policy/session-token map stays controller-only and is never a launcher input.
`launch-session.py` reads the single capability at launch, validates the exact
session/profile/broker assignment and passes the token
through the child environment. No token goes in argv, stdout, audit, URLs,
screenshots or snippets. The native host launches it without a console, with
stdio pipes; automation consoles also use the private task desktop. Extension
interaction with an approved owner browser retains its actual desktop.
This boundary does not prevent same-user native shell access to other files;
it makes no OS isolation claim.

The same launcher supports `--transport cli`; the caller provides the exact
semantic JSON request on stdin. This fallback covers Fleet clients without
native MCP and retains the same broker capability checks. Native MCP processes
use `initialize`, `tools/list` and `tools/call` over JSON-RPC stdio. Registration
is per assigned process. This package does not prove global registration in
every provider. When neither assigned transport is available, fail closed.
Do not replace native global MCP configuration or add another Office/Hub
server section, use raw CDP or switch to another browser tool after denial.

## One assignment, bounded ordinary actions

The owner supplies one explicit policy: `profile_id`, HTTPS `allowed_origins`,
`allowed_actions`, exact native sessions and separate capability tokens,
`extension_token`, exact `extension_id`, `expires_at`, and `synthetic:false`.
Only the isolated fixture policy accepts loopback HTTP with `synthetic:true`.
The broker binds to `127.0.0.1`. HTTP bearer credentials identify the session;
`X-Session-Id` must match it, and the extension has its own bearer credential
and `X-Profile-Id`. Credentials live in an owner-private file/environment.

An explicit owner toolbar click on the active allowed tab enables that exact
profile/origin. It does not transfer ownership of the manual tab to an agent.
`chrome_open` creates inactive session-owned tabs. Read, click, type, wait and
close use owned tab IDs; refs are fresh opaque capabilities from that session's
current read. Manual/foreign tabs, stale/foreign refs, mismatched sessions,
disallowed origins and unknown fields fail closed. Page content is untrusted.
`chrome_read` returns at most 4096 bytes with input values and credential-like
text redacted.

In the Windows Fleet backend, ordinary actions authorized by the task proceed without repeated
questions, doctors, catalog calls, Office/Hub checkpoints or model calls.
Legitimate submit/send actions do not require approval solely because of their
verb. Credential, MFA/passkey entry and new OAuth consent/scope remain an
explicit owner escalation. Owner STOP is persisted and only an explicit owner
reattach can clear it. Technical reconnect does not replay a mutation; use
`chrome_attachment` to reconcile the same assigned profile and saved owned
tabs. An uncertain mutation keeps its stable `request_id`; retries return the
retained result instead of executing the mutation again.

The broker's internal wire ID is separate from that stable model-facing ID:
`generation:uuid4hex`. Generation is the first 32 lowercase hex characters of
SHA256 over compact UTF-8 JSON `[profile_id, extension_token, floor(expiry_ms)]`;
Python policy expiry is seconds and extension configuration expiry is milliseconds.
It supplements the existing bearer authentication. The extension retains every
terminal mutation ID in that generation across STOP, restart and unknown outcomes;
terminal plus reserved in-flight mutation IDs are bounded to 10000. A full journal
denies further mutations while STOP remains available. Invalid journals or storage
failures stop execution rather than discard active-generation IDs.
A fresh trusted task-policy assignment after expiry retires old wire IDs, journal
and ownership; STOP remains until the existing explicit owner attach. Late old
completions cannot populate the new journal or acquire ownership. This is part of
the one-time assignment, with no new tool arguments or per-operation steps.

Tools are `chrome_attachment`, `chrome_open(url)`, `chrome_read(tab_id)`,
`chrome_click(tab_id,ref)`, `chrome_type(tab_id,ref,text)`,
`chrome_wait(tab_id,role?,name?,text?)` and `chrome_close(tab_id)`. Every tool
accepts optional `request_id`. At least one observable wait field is required.
No tool accepts profile/session/token overrides or arbitrary code. DOTS uses
its already registered attachment/read/click/type/navigate tools, IDs and
label/value arguments and connector guards; installing this package never replaces them.

## Executable synthetic evidence

No packages are installed. The verifier uses explicit paths to existing Python,
Node, PowerShell 7, bundled Playwright and pinned Chrome-for-Testing; see `--help`. A browser
hash mismatch fails. It creates a fresh task-owned persistent profile under a
protected private evidence directory, never reads normal Chrome profiles,
accounts, cookies, VM state or owner data, and retains every file.

```text
python -B uat.py --offline-checks --evidence C:\Example\ChromeRelayEvidence\offline
python -B uat.py --synthetic --evidence C:\Example\ChromeRelayEvidence\synthetic-uat
python -B uat.py --start-staging --evidence C:\Example\ChromeRelayEvidence\two-agents
python -B tests\client.py --agent a --verify --evidence C:\Example\ChromeRelayEvidence\two-agents
python -B tests\client.py --agent b --verify --evidence C:\Example\ChromeRelayEvidence\two-agents
python -B uat.py --verify-agents --evidence C:\Example\ChromeRelayEvidence\two-agents
python -B uat.py --stop-staging --evidence C:\Example\ChromeRelayEvidence\two-agents
```

Windows staging requires the reviewed task background helper passed through
`--background-helper` or `CHROME_RELAY_BACKGROUND_HELPER`. It creates the
synthetic broker/browser on `AgentStackBackground` using `CREATE_NO_WINDOW`.
The current task default helper and dependency paths are explicit in `uat.py`;
future tasks pass their reviewed equivalents. UAT works from this source or an
installed package and never runs the installer.

`--offline-checks` actually runs broker/MCP adversarial unit tests and the
extension tests. `--synthetic` also runs the real loaded extension and actual
MCP stdio open/read/type/click workflow, two overlapping deterministic
transport clients, ownership denials, STOP, restart/reconnect, a real held-modal
in-flight timeout, stable-ID no-replay and log canaries. Staging stays alive
until the exact task-owned process is stopped; no directory is cleaned up.
Offline installer checks use inert placeholder capabilities against no live
service, and exercise dry-run, retained versions, binding denials and the real
generated MCP stdio launcher. Live staging requires a private Windows DACL;
if the harness cannot set it, staging fails before invoking the background helper.
The staging controller emits separate `private/client-a.json` and `client-b.json`
capabilities. Actor clients read only their assigned file, never controller policy.

Two actual model actors run sequentially against **one** retained staging handle
with separate assigned synthetic capability sessions. Actor A keeps its own
tab, tests a third synthetic client's foreign tab/ref, and actor B tests actor
A's actual tab/ref. No concurrent model barrier is required. Model identity and
native CLI session identity are independently bound by the parent's Fleet
receipts. Fixture JSON does not attest to either identity. The deterministic
overlap check is separate evidence and never claims to be a model execution.

For an actor to choose individual steps, `client.py --agent a --call --evidence
C:\Example\ChromeRelayEvidence\two-agents` reads `{"name":"chrome_read","arguments":{"tab_id":123}}`
or a `tools/call` envelope from stdin and performs a real MCP exchange. `--tools`
lists the registered schemas. It stores redacted actual tool transcripts;
typed values and URL arguments are omitted from evidence. `--verify` is a full
scripted scenario through actual stdio transport. Final verification compares
both records, shared instance, assigned sessions, distinct owned tabs, actual
denials and exact source hashes. Independent product acceptance and parent
model-receipt binding remain required.
Headless evidence proves only synthetic active-tab behavior. First owner HTTPS
permission grant, a physical toolbar click and physical focus remain unverified;
this release remains inert without an explicit owner account/site assignment.

## Supported vs tested clients

The managed runtime ships a single harness registry in `KNOWN_HARNESSES` and
maps each harness to a transport and a native layout. The split below is
the documented contract; runtime evidence and changes must keep this
section honest.

Tested harnesses (covered by `tests/test_managed.py` and
`tests/test_registration.py`):

- `codex` — `transport: mcp`, native layout `codex-toml`
  (`mcp_servers.fleet-chrome`); optional `native_session_env`
  (`CODEX_THREAD_ID`).
- `claude` — `transport: mcp`, native layout `mcpServers-json`
  (`mcpServers.fleet-chrome`).
- `kimi` — `transport: cli`, separate `chrome-relay-cli-assignment/1`
  assignments JSON; no native MCP edit.

Supported via explicit owner-verified declaration (`native_format=
mcpServers-json` is required because their native MCP layout has not
been recorded in `_NATIVE_FORMATS`):

- `glm` (alias: `zcode`, `glm/zcode`) — `zcode-json` is the known
  layout; an explicit declaration only applies when the harness is
  enrolled through `mcpServers-json`.
- `qwen`, `grok`, `minimax`, `mistral`, `muse`, `codebuddy`,
  `antigravity`, `mimo`.

Registration without a known layout and without an explicit declaration
fails honestly (`unknown_native_format`); no file is written and no
silently-inferred scope is applied. The package does not prove universal
global registration across providers.

## Sign-in and credential actions

The registered tool schema is exactly `ACTIONS = {chrome_attachment,
chrome_open, chrome_read, chrome_click, chrome_type, chrome_wait,
chrome_close}`. There is no `chrome_sign_in`, `chrome_login`,
`chrome_autofill`, `chrome_oauth` or `chrome_credential` tool. Saved
autofill sign-in is not provided: Chrome withholds autofilled values
from page script until a trusted user gesture and the fixed DOM backend
can only issue untrusted script events; trusted input would require
`chrome.debugger`/CDP, which this relay refuses. Owner must sign in
manually outside the agent's session; this is an explicit unmet
runtime requirement, not a step an agent can perform.

## Platform built-in tools boundaries

The relay is bounded by the OS, Chrome and the assigned harness. It
intentionally does not:

- Open a remote debugging port; the broker binds only to `127.0.0.1`.
- Replace, edit or remove any existing native MCP server entry,
  blanket `{mcpServers}` configuration, or Office/Hub registration.
- Edit environment variables, scheduled tasks, services, or the
  desktop session of another user.
- Inject caller-supplied JavaScript, CDP, selectors, profile overrides,
  capability tokens, or another session's tabs.
- Read owner passwords, cookies, autofilled values, owner profile
  contents, or any VM state.
- Run an installer, register a service, or auto-start a process.

The installer (`install.ps1`) only stages versioned inert files into a
chosen install root, refuses to overwrite a different existing version,
and never deletes a file. `-DryRun` validates without writing. The
release remains inert until an explicit owner grants account and site
scope; the runtime's own UAT proves only synthetic active-tab
behaviour.

## Provenance

This implementation was informed by `aindeev/agent-chrome-relay` at commit
`cf0f458901ea8aa0b505dfc4461246f47cc9c5dd` (MIT). No upstream installer or source
is copied, executed or activated. The runtime has no remote debugging port,
external package dependencies or paid model/API calls.


Build `1.2.0-billingo1` keeps the v1.2.0 MCP API and Chrome manifest version, with the independently reviewed legacy-inline saved-login guard. Exact top-level Billingo login/POST binding and Chrome autofill are required; a synthetic PASS does not prove Billingo authentication. Previous installed builds are retained.


Build `1.2.0-billingo2` declares the fixed saved-login debugger API as a required Chrome permission: Chrome does not support optional debugger. Toolbar asks only for assigned HTTPS origins in an actual owner gesture. Browser-level debugger permission is broad; supported operations remain fixed and profile-grant-bound. Owner consent and real browser UAT are required before activation. Previous builds are retained.

Build `1.2.0-billingo3` repairs two native scheduling races: the fixed isolated saved-login probe injects immediately, and new owned tabs wait within the existing deadline for an approved committed URL. It adds no permissions or credential access. A historical UNKNOWN login result is never rewritten or replayed; authenticated postconditions are separate evidence. Previous builds remain available for rollback.

## Owner start i autostart

`owner_start.py` starts the owner-approved browser profile on the interactive
owner desktop and starts the broker through HiddenLaunch. It never changes a
grant, reads credentials, attaches the profile automatically, kills processes,
or removes episodes, logs, or profile files. The owner supplies an
`owner-start.json` using schema `chrome-relay-owner-start/1`. It contains only
absolute paths and the owner-approved `grant_id`, `expected_origins`,
`profile_id`, `saved_login` route scope, and `start_url`; it must not contain
secrets. The launcher SHA-256 is pinned in `hidden_launch_sha256`. `broker_runtime`
must contain `managed.py` and `uat.py`; the extension files are checked against
that runtime before launch.

Start or inspect the process state with:

```text
python -B owner_start.py --start --config C:\path\owner-start.json
python -B owner_start.py --start --autostart --config C:\path\owner-start.json
python -B owner_start.py --status --config C:\path\owner-start.json
```

Status returns JSON with `state` equal to `broker`, `window`, `both`, or
`none`; uncertain profile or Python-process probes return `state: unknown`, a
detail of `profile_unknown` or `process_unknown`, and exit code 1. A start pass reports `STARTED`, `ALREADY_RUNNING`,
`ALREADY_STARTING`, `BLOCKED`, or `FAILED`. Exit codes are 0 for started or
already running/starting, 2 for a failed owner safety gate, and 1 for a
runtime or prerequisite failure. Autostart waits up to 180 seconds for runtime
files, mounted paths, and an unavailable or unreadable `--config` file. The
`BLOCKED` details include `extension_config`, `profile_unknown`, and
`process_unknown`. Each JSONL row includes `breakaway` booleans for the window
and broker processes, showing whether `CREATE_BREAKAWAY_FROM_JOB` was applied.
Each pass appends one redacted JSONL row to
`<private_dir>\owner-start.log`; the window process output is kept separately
in `<private_dir>\browser-process.log`. Episodes are retained under
`<private_dir>\owner-episodes`.

Before launch, the extension-config gate requires
`<private_dir>\extension-config.json` to exist and match the managed grant's
extension configuration exactly. Owner start never creates or rewrites that
owner-activated file. The browser setup carries the grant's expected extension
ID; Chrome's service-worker ID must match before the agent reads that file or
writes extension storage. An identity mismatch closes the browser context and
records a failed browser launch. A fresh installation therefore needs the
owner-approved activation first; autostart only restarts an activated grant.

Broker detection matches `managed.py broker --config <managed_config>` argv
tokens. Command lines the owner token cannot read (elevated logon tasks) are
skipped, because owner start never launches an elevated broker. If the broker
port still accepts connections without an identified broker, the pass is
`BLOCKED broker_unknown`: it neither starts a second broker nor sends the
controller key to the unidentified listener.

The scheduled logon task is planned without side effects, then applied in a
disabled, verified, enabled sequence:

```text
python -B owner_autostart.py plan --config C:\path\owner-start.json --runtime-dir C:\path\runtime --task-name "Owner Chrome Relay" --user DOMAIN\user
python -B owner_autostart.py apply --config C:\path\owner-start.json --runtime-dir C:\path\runtime --task-name "Owner Chrome Relay" --user DOMAIN\user
python -B owner_autostart.py status --config C:\path\owner-start.json --runtime-dir C:\path\runtime --task-name "Owner Chrome Relay" --user DOMAIN\user
```

`plan` prints the Task Scheduler XML and HiddenLaunch action XML. `apply`
refuses an existing task, writes both XML files under `private_dir`, registers
the task disabled, reads back and verifies its trigger, principal, settings,
and action, checks the HiddenLaunch SHA-256 pin before registration, and
enables the task only after verification succeeds. The broker's
`controller-action.xml` uses `isolate-desktop="true"` to run on the private
`AgentStackBackground` desktop. The logon task's `owner-start-action.xml`
deliberately has no `isolate-desktop` attribute: its Chrome window must appear
on the owner's interactive desktop. The task uses the interactive user's
least-privilege logon and ignores a second instance. Rollback disables the
task with `schtasks /Change /TN "Owner Chrome Relay" /DISABLE`; retained files
and process episodes are not deleted.
