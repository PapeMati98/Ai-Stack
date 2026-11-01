# Targeted UAT and debugging

Use the current task's maker/reviewer and test environment; do not create a
second acceptance contract. Scenarios depend on the changed surface and the
supported clients, not a universal checklist for every edit.

For a new or materially changed tab test the actual installed candidate in
Teams web and desktop, and mobile when part of the required journey. Roles:
allowed user and forbidden user; owner/admin only when an admin action is in
scope. Use synthetic data in isolated DEV/STAGING. Record artifact version/hash
and expected versus actual behavior, plus any untested client.

| Scenario | Useful evidence |
| --- | --- |
| Open personal/channel tab, reload, switch away/back | Page loads without hanging; intended routing and state |
| First login, renewal, consent denial/fallback | No iframe redirect loop; actionable failure; token stays private |
| Wrong tenant/role or cross-resource ID | Backend denies access, not merely hidden UI |
| Theme, narrow view, keyboard, slow network | Readable/usable UI; progress/error state; no duplicate action |
| Authorized data operation and `403`/`429`/`412` | Correct result or honest partial/error state; no silent overwrite |
| Existing web app outside Teams | Existing login, navigation and API behavior preserved |

Agents Playground can test supported bot logic locally. It cannot establish
Teams iframe, NAA client compatibility, tenant policy, installation or tab UX.
When a real host is unavailable, report the missing UAT; do not label the app
ready because local build and an emulator passed.

## Diagnose the smallest boundary

- Blank tab: HTTPS/certificate, actual framing headers, content URL/redirect,
  manifest domain and TeamsJS initialization. Do not open a new public tunnel
  or remove CSP everywhere to test a hypothesis.
- Login loop/401: host capability, correct NAA versus SSO flow, registered
  redirect URI, exact token audience, issuer/tenant and backend validation.
  Inspect sanitized error codes; never dump token caches or secret env files.
- 403: business authorization versus Graph permission/resource policy. Use
  `m365-workspace-operator` for that cloud slice; no broad consent as a shortcut.
- Valid ZIP but app missing: app catalog/availability, install scope, policies
  and supported manifest version; packaging is distinct from publication.
- Duplicate/stale task or list write: stable keys, uncertain retries,
  ETag/reconciliation; do not delete user data as a reset.

Publication, app installation, catalog/policy updates, Entra consent, Azure
provisioning, backend deploy and external notifications require their actual
existing authorization. Prepare the reviewable package/diff first. Recovery:
retain prior package version and backend artifact, preserve data/schemas and
identify authorized rollback operations; do not assume deleting a registration
or uninstalling an app is permitted.
