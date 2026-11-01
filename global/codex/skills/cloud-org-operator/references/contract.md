# Contract

## Provider decision

Microsoft 365 and SharePoint use the additive provider `m365`. They are not
mandatory fields on the existing `graph` provider. This preserves schema-v1 and
schema-v2 Azure/Graph entries while giving SharePoint a complete target and its
own channel rules. A partial `m365` target is invalid even if an older `graph`
entry for the same organization is valid.

The M365 target is one tuple:

`organization_id + tenant_id + sharepoint_host + optional site_path + auth_mode + principal`

The host is lowercase DNS only, without scheme, port or path. A site path is an
absolute canonical URI path, without query, fragment, dot segments, repeated
slashes or a trailing slash (except `/`). A SharePoint host must not be inferred
from the company name, display name, email domain or tenant domain.
A decoded site segment may contain letters, digits, spaces and `-._~`; spaces and
Unicode are represented by canonical uppercase percent encoding. Shell
metacharacters, nested percent encoding and percent-encoded dot-segment tricks
are denied.

## Registry input

`cloud-organizations.json` contains `schema_version` and a non-empty
`organizations` array. Each organization has a unique `organization_id`, unique
aliases, and one or more provider entries. Azure, Graph, GCP and Google Workspace
retain their existing fields and behavior. Schema v1 remains readable for those
legacy providers.

Provider `m365` requires schema v2 and:

- `project_root`, `config_root`, `tenant_id`, `sharepoint_host`, `auth_mode`;
- optional `site_path` and `m365_connection_name`;
- for `auth_mode: delegated`, exactly one non-secret `account`;
- for `auth_mode: app-only`, exactly one non-secret `application_id`;
- optional `mcp_connectors`, each with a globally unique `connector_id` and an
  exact copy of its parent tenant, host, optional site, auth mode and principal.

The registry contains identifiers only. Tokens, client secrets, certificates,
passwords, cookies and credential paths do not belong in it. Secret-looking keys
are recursively rejected.

## Target discovery

For a confirmed Microsoft domain, read this public metadata without logging in:

`https://login.microsoftonline.com/<domain>/v2.0/.well-known/openid-configuration`

The tenant GUID in the returned tenant-scoped issuer is evidence for the domain's
Entra tenant. It does not derive or prove the SharePoint hostname. Confirm the
SharePoint host and optional site separately from an authoritative tenant response
or an owner-approved URL, then put those values only in the owner's local registry.

## Authentication modes

`delegated` means a signed-in user. The Graph PowerShell inspection may use
`Get-MgContext`, but must compare both `TenantId` and `Account`, then inspect the
exact SharePoint site.

`app-only` means client credentials or another application identity flow. It must
not use `Get-MgContext`. The non-secret observation is produced by an authenticated
read such as `GET https://graph.microsoft.com/v1.0/organization?$select=id`, whose
organization `id` must equal the registry `tenant_id`, plus an exact site lookup.
The observed file contains only tenant/site/application identifiers; a raw access
token is rejected rather than decoded or echoed by this helper.

## Execution channels

Provider `m365` requires both `execution_channel` and `intent` for preflight and
verification. Supported channels are `graph-powershell`, `m365-cli` and `mcp`;
intents are `read` and `mutate`. These arguments are rejected for legacy providers.

### CLI for Microsoft 365

The PnP CLI can retain multiple named connections. The helper never installs it,
logs in or executes it. Its plan requires an explicit registry
`m365_connection_name` and documents these steps:

- `m365 connection list --output json`
- `m365 connection use --name <registered-name>`
- `m365 tenant id get --output json`

The active connection name and returned tenant ID must match the registry before
an exact SharePoint site read. Selecting a connection changes local CLI context,
not cloud resources; it is still emitted only as a non-executing plan.
The machine-readable plan represents every CLI step as an `argv` array. A caller
must pass each item as one literal process argument and must not join it into a
shell command.

### MCP connectors

An MCP connector is not trusted because it is present or authenticated. A
registered connector must run a read-only identity probe equivalent to `get_me`
or an organization query and return a separately captured non-secret observation.
`verify-context` compares connector ID, tenant, host, optional site, auth mode and
principal with the parent organization mapping.

Connector IDs are globally indexed across organizations. If an ID exists but
belongs to another organization, preflight and verification are `DENY` for both
read and mutation; this is never downgraded to an unregistered connector.

An unregistered connector is `DENY` for `mutate`. For `read`, the maximum result
is `REGISTRY_ALLOW` with `UNVERIFIED`, `execution_allowed: false`,
`read_only_allowed: true` and an explicit warning that grants no execution
authorization. It never becomes `ALLOW` until the owner registers its exact identity.

The JSON Schema is a structural precheck. Cross-record connector uniqueness and
parent equality remain authoritative runtime checks in `validate-registry`; a
schema-only pass never authorizes execution.

## Decisions and output

`REGISTRY_ALLOW` means only that one registry target resolved. It is not execution
permission. `ALLOW` is possible only after exact live-context equality.
`DENY` is mandatory for missing input, ambiguity, provider mismatch, partial M365
target, unknown auth mode, invalid canonical host/site, connector mismatch,
unregistered mutation, malformed observed shape or secret-looking data.

Every command emits canonical JSON. Exit code `0` means pass/allow, `2` means an
expected fail-closed denial, and `1` means malformed CLI or an unexpected internal
error. Unexpected errors are not included verbatim in the receipt.

## Execution boundary

The helper performs no authentication, network request, cloud discovery, CLI or
MCP invocation, config write or resource mutation. The agent may execute a
documented inspection only as a separate phase. A cloud mutation still requires
the user's ordinary production/permission approval immediately after a fresh
context verification. An implicit fallback target is never allowed.
