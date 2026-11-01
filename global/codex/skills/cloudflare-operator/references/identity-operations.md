# Identity, credentials and operations

## Account selection and tooling

Locate explicit account/zone IDs in the project configuration and Hub; reconcile
with authorized live inventory. Use list pagination and exact ID matching, not
the first account returned. If the project is IaC-managed, preserve its source
of truth and avoid a dashboard change that the next apply will undo.

Inspect only executable identity/version and non-secret project fields:
`Get-Command cloudflared,wrangler -ErrorAction SilentlyContinue` on Windows;
`cloudflared --version` and the project's pinned Wrangler version/help when present.
Do not use unpinned `npx` as a read-only preflight: it may install code. Missing
CLI is a prerequisite, not permission to install globally or claim service failure.

Cloudflare REST API base is `https://api.cloudflare.com/client/v4`.
Use documented SDK/API requests with process-scoped credential injection; validate
HTTP status AND Cloudflare success/error payload. Follow documented pagination,
timeouts and rate-limit signals. Retry bounded idempotent reads; reconcile unknown
write outcomes before retrying. No raw headers, cookie dumps or full error bodies
in reports. Use sanitized error code, resource ID and time instead.

## Credentials are not interchangeable

- Prefer narrowly scoped API tokens over the Global API Key. Bind resource scope
  to the needed zones/accounts and separate read-only reconnaissance from writes.
- A user API token, account API token, Wrangler OAuth login, tunnel connector
  token/credential, origin certificate and Access service token have different
  authority. Never pass one as a substitute for another or copy whole stores.
- Verify user tokens with the documented user-token verify endpoint. For account
  tokens use the account-specific documented verification flow, not the user path.
  Then perform an allowed read: token active does not prove operation permissions.
- When checking denial, use absent credentials or synthetic test identity; do not
  probe another customer's resources to prove a scope restriction.
- Take input in a hidden local prompt; store through approved secret facilities.
  Never put secrets in chat, command arguments, repo, Hub, examples or logs. Keep
  only credential references in project notes. Changes/rotation need scoped consent.
- Check telemetry and data-use controls separately from authentication; report
  unsupported/unclear privacy controls as UNVERIFIED and minimize uploaded content.
  Do not submit project-bearing feedback. Logs may contain personal data/secrets.

## Costs and diagnostics

Check the subscribed product and current limits for the intended action. Workers,
storage, requests, logs and other products have separate meters. A usage dashboard
or alert is not an enforced spending cap. Bound tests; do not enable paid products
or route Fleet onto Workers AI/AI Gateway without an explicit separate task.
Unknown usage stays unknown, not zero. Avoid full log exports; use short scoped
time windows, metadata and synthetic correlation IDs with approved retention.

## Operation receipt

Record project/account/zone/environment, resource IDs, config owner, approval,
redacted pre-state, intended diff, request result and follow-up live read,
positive/negative tests, business/UAT outcome, costs measured or unknown,
rollback plan/result, residual risks and Hub receipt. Claim VERIFIED only for the
scope actually exercised. Do not claim authenticated from a successful file copy.

Primary sources (checked 2026-09-06):
- https://developers.cloudflare.com/fundamentals/api/get-started/create-token/
- https://developers.cloudflare.com/fundamentals/api/get-started/account-owned-tokens/
- https://developers.cloudflare.com/workers/wrangler/commands/

Follow the API-token page's links for account tokens, permissions and rate limits.
