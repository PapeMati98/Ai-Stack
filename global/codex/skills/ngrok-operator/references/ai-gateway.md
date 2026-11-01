# AI Gateway runbook

## Identify and inspect

AI Gateway uses `app.ngrok.ai`; classic endpoint administration uses
`dashboard.ngrok.com`. Do not confuse their balances or management APIs.
OpenAI SDK base: `https://gateway.ngrok.ai/v1`.
Anthropic SDK base: `https://gateway.ngrok.ai`.
These are inference bases, not permission to send project content.

Read documented account/model/provider/access-configuration metadata only.
Do not list or reveal key values. Determine the allowed model IDs live; do not
copy model examples from documentation as current Fleet recommendations.
Configuration changes use the AI Gateway API reference or authenticated UI,
not guessed `ngrok ai` commands or classic `ngrok api` endpoints.

## Choose the billing and privacy path

| Path | Verify before sending |
| --- | --- |
| ngrok.ai inference | Prepaid credits, processing and model charges, current catalog |
| BYOK | Provider permits this API use; provider charge plus gateway processing charge |
| Self-hosted | Actual server address, TLS/auth, processing charges, GPU queue and capacity |

Paid consumer/CLI subscriptions do not automatically fund generic API calls.
A local model behind a paid gateway is not a zero-cost transport. Check current
pricing, not the screenshot balance. `ngrok credits` prints software credits;
it does not retrieve AI Gateway funds.

Create per-project/environment client access scope rather than broad account
access. Reference provider credentials through approved secret facilities.
Before any authorized write, capture a redacted configuration pre-state and
stable IDs. Never revoke/rotate someone else's active key for a test.

Fleet owns the chosen model. Configure fallback only to named, permitted models
and providers within the same approved data scope and cost ceiling. Do not silently
fall back from local/subscription to paid inference or from opted-out to Contributor.
Define request timeout, retry count, concurrency and maximum output explicitly.
Confirm whether budget controls are enforced or only alerts; implement a bounded
caller budget where no hard cap exists. Do not present observations as hard limits.

## Verify a scoped configuration

1. With approval to spend the small test budget, send a synthetic, non-sensitive
   prompt using the selected SDK and injected inference access key.
2. Check complete result, served model/provider where reported, latency, token
   usage and billable usage. Missing telemetry stays unknown, never zero.
3. Verify missing/invalid key denial and disallowed-model rejection. Do not use
   production credentials in negative probes or log Authorization headers.
4. Test approved fallback with a controlled fixture or synthetic failure, not by
   disrupting an active provider. Check the actual provider and price path.
5. Restore only changed configuration; revoke only a test key created in scope
   if authorized. Revalidate the prior client's expected behavior.

For 401/403 inspect identity and policy; for 429 inspect provider versus gateway
limits; for 5xx/timeout correlate sanitized request ID and upstream health.
Record streaming interruptions as incomplete, not successful delivery.

## Primary sources (checked 2026-09-06)

- https://ngrok.com/docs/ai-gateway/overview
- https://ngrok.com/docs/ai-gateway/concepts/credits
- https://ngrok.com/docs/ai-gateway/guides/securing-endpoints
- https://ngrok.com/docs/ai-gateway/observability

Follow current links from the overview to API schemas, catalog and account
settings. Recheck mutable prices, supported models and logging controls live.
