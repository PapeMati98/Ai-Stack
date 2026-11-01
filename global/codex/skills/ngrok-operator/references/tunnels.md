# Tunnels and endpoint lifecycle

## Read-only preflight

On the intended host, resolve the actual executable and inspect:

```powershell
Get-Command ngrok -ErrorAction SilentlyContinue
ngrok version
ngrok config check
ngrok http --help
ngrok tcp --help
ngrok tls --help
```

On Linux use `command -v ngrok`. Do not dump the configuration file; it can
contain credentials. `config check` success does not prove auth or connectivity.
Check the selected local upstream health and port owner before creating a tunnel.
If installation is needed, inspect the official package source/version and use
an approved installer, never pipe an unreviewed download into a shell.

## Prepare before exposure

Record exact host, scheme/port, project, DEV/STAGING/PROD, intended consumers,
endpoint/domain, duration and teardown owner. Obtain approval for this public
exposure, including a DEV tunnel. A random URL is not authentication.

Choose an access policy appropriate to the consumer: authenticated users for
interactive UI; authenticated machine calls or verified provider signatures for
webhooks. Verify current Traffic Policy syntax and plan support before start.
Protect the app itself as well. Do not expose a database, RDP, SSH, debugging
console, local inspector or model server merely to make a browser test work.
Public access to any such service needs separate explicit scope and review.

HTTP command shape after approval and after policy validation:

```powershell
ngrok http http://127.0.0.1:8080 --traffic-policy-file .\ngrok-policy.yml
```

The port is illustrative, not a stack default. Use the actual approved address.
Custom `--url`, named `start`, `tcp`, `tls`, service installation and admin API
operations require current command help, a known resource and scoped approval.
Do not use `start --all`, install persistence or reserve/change domains casually.
Do not disable upstream certificate verification to hide TLS failures.

For approved TCP exposure the basic shape is `ngrok tcp <approved-port>`;
for TLS use `ngrok tls <approved-upstream>` only after checking the installed
TLS command's certificate, hostname and policy requirements. HTTP auth policy
must not be assumed to protect raw TCP/TLS traffic.

Use a supervised job; record PID plus start time/executable and endpoint ID.
On Windows background helpers use hidden windows unless the user requests an
interactive terminal. Keep the consumer URL separate from the local upstream.
For AI Lab VM tests, bind reachability to that VM; do not assume its localhost
addresses your PC. Avoid public tunnels when existing private transport suffices.

## Verify and recover

1. Local health succeeds; public authorized probe reaches the expected app and
   environment. Unauthorized probe is denied, not just redirected to the app.
2. Test relevant redirects, cookies, forwarded-host trust, CORS, WebSockets and
   mixed content. UI tests must inspect actual browser state and user actions.
3. DEV/TEST downstream mail, payments and external mutations are technically
   blocked before tests. STAGING uses the approved recipient/account allowlist.
4. Diagnose hop-by-hop: client -> ngrok policy -> agent -> upstream. Record error
   code, time and sanitized request ID; do not export captured request bodies.
   Agent debug logs and HTTP capture can also contain sensitive material: disable
   unnecessary capture, restrict log access and review redacted metadata only.
5. On expiry/failure stop the exact owned job or endpoint, restore the scoped
   pre-state, verify this URL no longer reaches the test app and unrelated
   sessions remain alive. A persistent cloud endpoint can outlive a local process;
   inspect it separately, do not assume process exit deletes every resource.

Sources: https://ngrok.com/docs/gateway/agent/cli and current Traffic Policy
documentation linked from https://ngrok.com/docs (checked 2026-09-06).
