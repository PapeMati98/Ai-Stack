# GCP read-only MCP

Production pilot entrypoint: `adc_entry.py`. This local stdio server exposes
exactly `context_status(profile)`, `project_info(profile)`, and
`enabled_services(profile)`. It has no listener, public endpoint, shell tool,
write operation, or login tool. Inputs cannot supply command vectors, URLs,
credential paths, extra flags, accounts or projects.

## Identity and read boundary

The trusted launcher supplies an absolute private policy and credentials-file
path. Each named profile binds an exact Google account, project, local named
configuration, private configuration directory outside the repository and
installed Cloud SDK library location. The two pilot profiles share one owner
account, with distinct project bindings. A different account requires a separate
private OAuth root and server instance. Local configuration is checked before
credentials are loaded. Verified Google email and Google project identity are
checked before each metadata operation. All unknown tool arguments and profiles
are rejected before authentication or HTTP. Google's responses are reduced to a
fixed metadata projection; resource specs, logs, IAM policies and tokens are not
requested or returned. Remote text is untrusted data.

The dedicated Desktop OAuth client must be marked agentic. Its grant contains
only `cloud-platform.read-only`, `userinfo.email`, and `openid`, requested with
`access_type=offline` and explicit consent. Existing application OAuth clients
and their secrets are not reused for this pilot. Credential JSON is kept outside
Git in a private directory with an owner/SYSTEM/Administrators DACL. Do not put
its contents into a client configuration, log, message, or knowledge handoff.

OAuth scopes prevent Google write operations with this access token. The local
allowlist further restricts the MCP surface to the two configured projects and
three metadata tools. The owner's existing IAM roles are not modified: the
refresh token itself is not restricted to only those project IDs by Google IAM.
An operator who can change the private launch policy can change local project
bindings. A separate least-privilege IAM principal is required if a future pilot
needs a Google-enforced project boundary or Cloud Run access.

Cloud Run Admin API requires `cloud-platform`, including read methods. Therefore
Cloud Run tools from the reusable `server.py` gcloud backend are **not exposed**
by `adc_entry.py`; broadening OAuth to make them work is not part of this pilot.
The installed launch configurations must point to `adc_entry.py`, never to the
optional broader gcloud backend `server.py`.

## Refresh and Google dependencies

`adc_entry.py` uses the official Google authentication/requests libraries already
bundled with the installed Cloud SDK. The interpreter is launched as Windows
GUI `pythonw.exe -I`, without a console and without environment/cwd Python import
influence. No package installation is needed. Google requests do not use proxy
or credential overrides inherited from the caller. Credential and API request
failures return redacted errors, without raw Google response bodies.

The first permitted authenticated call in each process forces a refresh from
the persistent refresh token. The official AuthorizedSession subsequently
refreshes expiring/expired access tokens and retries eligible authentication
failures. Granted scopes are checked after every refresh. Refresh requests have
a 30-second timeout. The original persistent refresh token remains in the
private credential file; the server does not emit or copy tokens. Starting a new
MCP process does not require a browser when the refresh grant remains valid.
Google may revoke grants, enforce organization session policy or require owner
reauthentication. This does not remove those requirements or automate MFA.

The OAuth client's consumer project must have Cloud Resource Manager API
(`cloudresourcemanager.googleapis.com`) enabled for project metadata. Service
Usage API is used only for GET/list enabled service names. Neither an API-enable
operation nor IAM mutation is exposed through MCP.

## Protocol and installation

Standard MCP stdio is newline-delimited UTF-8 JSON-RPC, without Content-Length
headers. The supported methods are initialize, initialized notification, ping,
tools/list and tools/call. Inputs/output are bounded and errors are redacted.

Example trusted launch (use absolute paths, with credentials in a private root):

```json
{
  "command": "C:/Python313/pythonw.exe",
  "args": ["-I", "C:/agent-stack/shared/mcp/gcp-readonly/adc_entry.py",
           "--policy", "C:/PrivateGoogle/pilot-policy.json",
           "--credentials-file", "C:/PrivateGoogle/pilot-readonly-credentials.json"]
}
```

`register.py` adds this exact entry only to existing local client configs. It
preserves unrelated content, rejects conflicting entries and concurrent edits,
and writes private preimages before any config change. The parent verifies the
private backup root ACL. Applying registration requires the reviewed current
source hashes, identical installed source bytes, and an absolute GUI interpreter.
Registration does not restart apps or claim that an already running chat has
reloaded the connection. Restart/reconnect the MCP client to load a new entry.
Rollback restores only this task's changed entry/preimage after checking that no
concurrent edits would be lost; leave new inert files in place, do not delete.

## Validation

Offline tests use synthetic identities and injected HTTP/subprocess backends.
They cover input isolation, wrong identity/project, exact GET endpoints, response
limits and redaction, standard stdio, hidden subprocess execution, and config
preservation/conflict detection. Test scratch must be set to an explicitly
chosen directory inside the task workspace, never an environment-derived
cleanup target. Live acceptance must additionally demonstrate OAuth refresh in
fresh processes, all three tools for both projects, and rejected arbitrary
commands/other-project calls. An ACCEPT of offline code alone is not live UAT.

Primary protocol/auth sources:
- https://modelcontextprotocol.io/specification/2025-06-18/basic/transports
- https://developers.google.com/identity/protocols/oauth2/native-app
- https://docs.cloud.google.com/run/docs/reference/rest/v2/projects.locations.services/list
