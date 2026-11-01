# Entra identities, consent and least privilege

## Keep the identities distinct

| Object | Meaning |
| --- | --- |
| Teams manifest `id` | ID of the packaged Teams app |
| Entra application `appId` | Client ID used for token requests |
| Application object `id` | Object ID for managing the home-tenant registration |
| Service principal `id` | Enterprise-app instance in the target tenant |
| User `oid` + `tid` | Tenant-bound user identity; email/display name can change |

Do not interchange these IDs or reuse the administrative MCP registration as
the identity for a business tab, bot or background service. Reuse an existing
business app registration only when its purpose, owner and trust boundary match.
Entra registration alone does not install or publish a Teams app. Graph-based
registration creation and service-principal creation can be separate operations.
[Application/service-principal model](https://learn.microsoft.com/en-us/entra/identity-platform/app-objects-and-service-principals).

## Reason from the exact operation

For a requested API operation consult its current Microsoft Graph endpoint
permission table. Choose the least permission supporting that operation and
the intended principal; read/write in a scope's name alone is insufficient.

Delegated permission acts with a signed-in user and remains limited by their
resource access. Application permission acts as the app and often needs admin
consent. Requested permissions (`requiredResourceAccess`) are not the same as
effective delegated consent (`oauth2PermissionGrants`) or application grants
(`appRoleAssignments`). A powerful signed-in user does not add a missing token
permission; a scope does not override a resource's access policy.

App-only runtime design may use managed identity or a certificate when supported;
do not create credentials, grant consent or install an admin tool for planning.
Never put a confidential-client secret in a tab or SPA.
[Updating requested permissions](https://learn.microsoft.com/en-us/entra/identity-platform/howto-update-permissions).

## Prefer a narrower resource boundary when feasible

Teams resource-specific consent (RSC) can scope supported permissions to a team
or chat; verify the exact RSC permission and tenant policy. Installing an app
with RSC is a consent/access effect, not an offline packaging step.
[RSC](https://learn.microsoft.com/en-us/microsoftteams/platform/graph-api/rsc/resource-specific-consent).

For SharePoint selected permissions, scope consent, an explicit resource grant
and a matching token are all needed. Consent alone gives no site access.
Tenant-wide discovery/search may still require broader scopes. Compare supported
operations before replacing `Sites.Read.All` with `Sites.Selected`; never silently
change the pilot or claim selected access works without the site grant.
[Selected permissions](https://learn.microsoft.com/en-us/graph/permissions-selected-overview).

## Diagnose before proposing a grant

For a `403`, compare the registered tenant/principal, auth mode, endpoint,
effective permission, resource access and relevant Conditional Access/policy.
Use an available authorized inspection lane; the structural read-only connector
cannot inspect Entra grants. Prepare a specific missing capability instead of
requesting `Directory.ReadWrite.All` or broad application access by default.

Before an authorized IAM change: show the exact application/service-principal,
current and desired permission, affected resource, user/app-only impact, test and
recovery. Preserve unrelated grants, redirect URIs and secrets. Existing owner
production/IAM approval gates apply; this reference creates no standing grant.
