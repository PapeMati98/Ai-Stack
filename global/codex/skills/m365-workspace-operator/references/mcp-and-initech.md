# Managed MCP capabilities

## Use the installed connector

Discover tools on the actual executing client, once per changed connection.
Match names semantically if the host adds a namespace; use the returned schema,
not guessed Graph parameter names. Tool discovery, an authenticated account and
successful execution are separate observations. A configured client may require
reload; do not restart another active session.

Prefer the pinned, managed runtime to `npx latest`. Do not read or copy token
caches, `.env` values or other clients' credentials. Login repairs belong to the
same isolated client profile and known account. Follow its installed login
procedure; never treat an auth helper hidden by the guard as callable.

## Generic managed connector

The reusable source lives at `shared/mcp/m365-workspace` in this edition.
It requires an explicit `M365_WORKSPACE_CONFIG`; inactive example files do
not select a tenant, sign in, grant scopes or prove a working installation.
Discover the configured MCP registration name and namespaced tool schema on
the actual executing client. A connector installed for one client does not
prove readiness of another client or subscription.

The source pins Softeria **0.150.2** behind a fixed GET guard. No account
identifiers, client-specific deployment evidence or token cache is distributed
through this skill. The following is the bounded structural tool surface,
not evidence of authenticated live calls.

The guarded structural profile exposes exactly these eleven underlying tool names:

| Area | Tools |
| --- | --- |
| Signed-in user | `get-current-user` |
| Teams structure | `list-joined-teams`, `list-team-channels`, `get-team-channel` |
| SharePoint sites | `search-sharepoint-sites`, `get-sharepoint-site`, `get-sharepoint-site-by-path` |
| List metadata | `list-sharepoint-site-lists`, `get-sharepoint-site-list` |
| Column metadata | `list-sharepoint-list-columns`, `get-sharepoint-list-column` |

These are structural reads. They do not include list rows, files, chat messages,
mail, Planner tasks, Entra administration, app installation or any mutation.
Use the narrow site/list/column path when IDs are known; listing joined teams
does not discover all tenant teams. Returned user identity alone does not verify
the complete tenant/site binding required by `cloud-org-operator`.

Four requested structural scopes are `User.Read`, `Team.ReadBasic.All`,
`Channel.ReadBasic.All`, `Sites.Read.All`. An account may already hold wider previously consented permissions, including
writes. GET enforcement is the guard; a narrow requested scope list does not
revoke prior consent or prove that an existing token is least privilege.
Do not loosen filters, call `graph-batch` or use a wider client to work around it.

When a needed operation is unavailable, finish the independent local work and
report one concrete capability gap with the endpoint and permission to review.
Do not turn an ordinary metadata read into a proposal for broad admin access.

## Upstream patterns we reuse

[Softeria v0.150.2](https://github.com/Softeria/ms-365-mcp-server/blob/v0.150.2/README.md)
separates enabled tools, allowed scopes and authentication. Reuse that separation
and small tool surfaces. Its broader catalog is not our enabled capability list.
Read-only settings and scope narrowing do not revoke prior tenant consent.

[PnP CLI for Microsoft 365](https://github.com/pnp/cli-microsoft365) is useful
as an endpoint/command reference for a separately authorized CLI lane. It needs
its own confirmed Entra identity and connection; adding or authenticating that
lane is not an automatic fallback when MCP denies an operation.
