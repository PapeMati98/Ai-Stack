# Resource mapping and Graph reliability

## Find the existing object

Use stable IDs, not a renamed display label, folder guess or email-domain guess.
Record only the mappings needed in the existing task: team/group -> channel ->
site -> list/drive -> item, or plan -> bucket -> task. Preserve `id`, `webUrl`,
column internal `name` versus display name, type and lookup target when relevant.
The pilot can inspect schema; schema access does not imply permission to rows.

Standard channels share the team's parent SharePoint site. Private and shared
channels have their own sites. Resolve the actual resource rather than assuming
all channel files live in one library or reconstructing paths from titles.
Graph `GET /teams/{teamId}/channels/{channelId}/filesFolder` can identify file
storage only when that endpoint is available and authorized on the chosen lane.
Source: [Teams/SharePoint relationship](https://learn.microsoft.com/en-us/sharepoint/teams-connected-sites).

Before changing a list or moving an app screen, identify readers and writers,
automation owners and downstream dependencies. Reuse existing lists/flows when
they fit. Hiding a screen does not authorize disabling its data synchronization.
SharePoint can be the source for a payroll calculation in a separate app; a UI
migration must preserve that feed. Migration and retirement choices are business
decisions, not implied by this skill or a received document.

## Bounded reads and error handling

- Use API-supported `$select`, filters and page size for the needed metadata.
  Follow the complete `@odata.nextLink` only on the verified Graph cloud host;
  keep its opaque paging state. A time/page cap means **partial**, not complete.
  If the connector hides continuation, disclose that limitation rather than
  claiming a tenant-wide inventory. [Graph paging](https://learn.microsoft.com/en-us/graph/paging).
- `429`: respect `Retry-After`; when missing, bounded exponential backoff with
  jitter. Do not immediately retry or model-poll. Batched requests can fail
  individually; inspect subresponses. Do not blindly replay writes after an
  uncertain result. [Throttling](https://learn.microsoft.com/en-us/graph/throttling).
- `401`: distinguish expired/session/auth configuration from wrong audience.
  `403`: inspect endpoint permission, consent, user/resource access and policy.
  `404`: verify ID and visibility; it is not proof of deletion. Keep sanitized
  HTTP status and request ID, never authorization headers or response contents.
- Use `v1.0` when the needed endpoint exists. Beta is a deliberate compatibility
  choice with version-specific tests, not a fix for denied permissions.

## Planner and synchronization, only when in scope

Determine whether the actual plan type and operation are supported by Graph;
do not assume every Planner UI feature has a matching endpoint. Inspect the
chosen endpoint's delegated/application permission support. Pilot MCP has no
Planner tool; local preparation remains possible.

For concurrency-sensitive updates use the exact endpoint's ETag/`If-Match`
contract. On `412`, re-read and reconcile the intended diff; do not overwrite
another user's change. Task and task-details ETags are distinct.
[Planner API](https://learn.microsoft.com/en-us/graph/api/resources/planner-overview?view=graph-rest-1.0).

For an actual sync integration, extend the existing task with system of record,
owner, permitted fields, stable keys, reconciliation and recovery. Prefer delta
or notifications only where the endpoint supports them; handle cursor expiry,
deletions and subscription renewal. Test duplicates, partial pages, throttling,
concurrent changes and access denial on synthetic data. No real notifications,
customer-data extraction or new background automation to test a skill.
