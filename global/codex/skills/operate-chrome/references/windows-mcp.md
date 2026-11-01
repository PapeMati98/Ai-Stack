# Assigned Windows MCP runtime

The shared skill also supports an explicitly assigned Windows Fleet process.
Provisioning belongs to the runtime maintainer, once per profile/task policy.
Ordinary sessions reuse that assignment without setup, catalog, doctor,
Office/Hub or extra model calls.

## Default registered profile

The default browser UI path is the persistent managed Chrome profile in
`global/tools/chrome-relay/managed.py` (schema `chrome-relay-managed/1`).
One owner grant fixes the profile, allowed HTTPS origins/actions, the
extension, an optional expiry, and the exact list of enrolled harnesses.
The harness map in the grant decides whether a process connects via native
MCP or the CLI fallback; `registration.py` writes the matching overlay
entry into the harness's native MCP file. The managed registration targets
are Codex `~/.codex/config.toml` (`codex-toml`), Claude
`~/.claude.json` under `mcpServers` (user scope), and Antigravity
`~/.gemini/config/mcp_config.json` (`mcpServers-json`). Unknown harnesses need an explicit
`native_format=mcpServers-json` declaration in the enrollment record, or the
registration fails honestly.

The launcher serves initialization and `tools/list` locally, then registers a
fresh SID and child capability on the first valid `tools/call`; it releases
the slot after 20 minutes without calls or pending responses, and the 16-client
limit counts active browser sessions, so tabs from a released SID stop belonging
to that agent. The controller replies with a short-lived client capability
(`session_id`, `token`, `profile_id`, `broker_base`, `expires_in_s`) that the
launcher passes only through the child environment. Capability tokens never
appear in argv, URLs, logs, screenshots or model context. A harness enrollment change is a new setup decision:
STOP is set, tombstones are dropped and every client capability is
revoked. `native_session_env`, when configured for a harness, is read
from the inherited environment and recorded alongside the SID; it is never
labelled verified and no PID/UUID is substituted for it.

The candidate roster contains Codex, Claude, Qwen, GLM/ZCode, Mistral,
Kimi, Grok, CodeBuddy, MiniMax, Muse, Antigravity, MiMo and OpenClaw.
`zcode` is an alias for `glm`. The roster is an implementation inventory,
not evidence that every pinned native client has passed UAT. Current native
acceptance and installed release must be checked in the release evidence.
Antigravity has a persistent native managed MCP entry in
`~/.gemini/config/mcp_config.json`, written by `registration.py` with
`native_format=mcpServers-json`; each process still receives its bounded
assignment only from Fleet's child environment. MiniMax/Muse registrations
remain unchanged. If managed MCP starts without a usable assignment, its
Chrome tools remain visible and each call returns an error result carrying
`BLOCKED_MANAGED_BROWSER_UNAVAILABLE`. These Chrome adapters are implement-only;
their reviewer web access remains unsupported. Existing other MCP settings
are preserved; this does not sandbox their capabilities.

The owner toolbar click authorises the exact active tab/profile/origin;
that manual tab remains outside agent ownership. Opened tabs are
inactive and owned by the current session; close only those tabs.
STOP persists across restarts and requires explicit owner reattach.
Technical disconnect requires attachment reconciliation of the same
profile and saved tab/origin state; reconnect never replays a mutation.

## Transport fallback

If a Fleet client has no native MCP, the same enrolled harness uses
`managed.py cli`: the caller provides the identical semantic JSON request
over stdin (`{"name": ..., "arguments": ...}`). A harness whose grant
lists `transport: cli` is CLI-only; one with `transport: mcp` is MCP-only.
An explicit `transports: ["cli","mcp"]` enrollment supports both paths
for the same harness. It neither enrolls another harness nor grants another
origin; the broker verifies the requested transport on every registration.
Registration under `chrome-relay-cli-assignment/1` writes a separate
assignments JSON artifact rather than touching any native MCP config.
When neither transport is available, fail closed. Do not replace the
global native MCP configuration or Office/Hub registrations.

## Legacy assignment

The legacy `launch-session.py` flow (read one per-session capability JSON
created from `-ClientCapabilityPath`, `-NativeSessionId`, `-ProfileId`
and `-BrokerBase`) remains as an inert fallback installed by
`install.ps1` for environments that have not yet migrated to a managed
grant. It is not the default registered browser UI path; the managed
mode above is. The package does not replace global `mcp`/`mcpServers`
configuration or Office/Hub registrations.

## Process assignment

Use only the semantic tools already registered for this process. The
owner fixes one profile, allowed HTTPS origins/actions, expiry and the
actual native session ID in a controller-private policy. The assigned
process receives only one private capability file containing `session_id`,
`profile_id`, `broker_base` and `token`. Native hosts launch stdio with
CREATE_NO_WINDOW. Noninteractive automation also uses its task-private
desktop; approved browser UI remains on its assigned desktop without a
console. The file boundary does not sandbox native shell access by the
same OS user. Synthetic UAT uses expiring, fixture-only actor copies
under `native-actors/<actor>/client.json`, outside the owner-only
controller directory. This test setup grants no access to real accounts
or profiles.

Tools are `chrome_attachment`, `chrome_open(url)`, `chrome_read(tab_id)`,
`chrome_click(tab_id,ref)`, `chrome_type(tab_id,ref,text)`,
`chrome_wait(tab_id,role?,name?,text?)`, `chrome_close(tab_id)` and the
optional `chrome_saved_login(tab_id)`, with
an optional stable `request_id`. Refs come from a fresh read. Never
choose another profile/session, expose a token or use caller
JavaScript/CDP to bypass denial.

The stable model-facing `request_id` is unchanged. Internally,
broker-to-extension wire IDs carry the trusted task-policy generation
derived from the grant scope; the same generation retains terminal
mutation IDs across STOP, restart and unknown outcomes, with a bound of
10000 terminal plus reserved in-flight mutation IDs; reaching it denies
mutation while STOP stays available. Journal corruption or storage
failure stops execution. A fresh trusted assignment after expiry retires
old wire IDs, journal and ownership, preserves STOP and uses the
existing explicit owner attach. Late old results cannot change new
ownership. This adds no reset tool, ordinary-session reads, questions or
model calls.

## Saved-password sign-in

An owner binding fixes exact HTTPS login pages and form POST paths. Only an
owned tab on that page can call `chrome_saved_login`; no credential, selector,
destination, JavaScript or CDP arguments are accepted. Chrome must already
have autofilled the saved password. MFA, passkeys, OAuth consent and missing
autofill remain owner actions; ordinary credential reads/types/clicks stay
denied. The result confirms submission only; use the existing read/wait to
verify authentication.

The owner grants the extension's optional debugger permission in its existing
toolbar attach gesture once. The fixed worker suppresses page scripts only
around the trusted submit click, restores scripting and detaches in `finally`.
It does not expose debugger, Network, Fetch, cookie or password-reading tools
to models. The per-tab attempted-login tombstone survives STOP/restart and
denies a second attempt even with a different request ID. An uncertain result
requires observation, never replay. Persistent permission does not clear STOP;
an explicit owner attach is still needed after STOP or broker restart.

## Evidence and rollout

`global/tools/chrome-relay/install.ps1` stages a versioned, inert runtime.
It starts no server/profile/service, edits no secrets/global MCP
configuration, preserves previous versions and deletes no files.
Rollback selects the retained previous runtime in the assigned process
configuration. Activation follows its explicit approved scope and
independent acceptance. `-DryRun` validates without writing files.

`uat.py --offline-checks` runs actual unit/MCP/extension tests.
`--synthetic` loads the real extension in existing pinned
Chrome-for-Testing on a fresh, task-owned persistent profile and uses
actual stdio MCP operations. No package installation or normal owner
profile is needed. Two model actors reuse one `--start-staging` handle
sequentially through `tests/client.py --agent a|b --verify` or
individual `--call` stdin steps. JSON labels identify only assigned
synthetic capability sessions. The parent independently binds actual
model/native identity from Fleet receipts. A separate deterministic
transport check proves overlapping clients without claiming two model
executions. Headless synthetic checks do not verify first owner HTTPS
permission grant, physical toolbar interaction or physical focus. These
remain owner-side UAT; the release is inert until the owner assigns
its account and site scope.

These extended ref/tab schemas apply only to an assigned Fleet Windows
runtime. Existing DOTS tools, stable IDs and label/value schemas
described in [runtime.md](runtime.md) stay unchanged.
