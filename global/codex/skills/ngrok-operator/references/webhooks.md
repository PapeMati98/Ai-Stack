# Webhook development and diagnosis

1. Identify provider, event type, environment, route, signature scheme, secret
   reference, timestamp tolerance and idempotency key. Use provider documentation,
   not a generic guessed HMAC formula. Secrets remain in approved storage.
2. Isolate downstream effects before exposure: synthetic fixtures, test accounts,
   sandbox mail/payment adapters and a bounded test dataset. A label saying DEV
   is not a technical block. Never replay captured customer events as fixtures.
3. Verify supported ngrok `verify-webhook` policy against the actual provider.
   App-level verification must remain correct if bypassing the edge is possible.
   Validate raw signed bytes before JSON reserialization; reject malformed,
   missing, invalid or stale signatures according to the provider protocol.
4. Acceptance matrix: valid event; invalid signature; tampered body; expired
   timestamp where applicable; duplicate; out-of-order event; downstream timeout;
   provider retry. Check exact business effect count and state, not just HTTP 200.
5. Replay only a named synthetic event into an approved isolated receiver. Confirm
   idempotency prevents a second effect. Avoid global inspector replay, production
   deliveries, unrestricted redirects or logging full headers/payloads.

If ngrok accepts the event but the app does not process it, trace the route,
policy, body handling, queue and worker independently. Record sanitized event ID,
expected/actual state, retries and correlation ID. Follow provider ACK/retry rules;
do not return fake success to suppress retries while dropping work silently.

Close with endpoint cleanup, test-data disposition approved in the project plan,
and a redacted handoff. Never delete real records while cleaning a test.

Primary source (checked 2026-09-06):
https://ngrok.com/docs/gateway/traffic-policy/actions/verify-webhook
Provider-specific verification and retry documentation must also be read.
