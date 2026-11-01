# Tunnel and Access

Identify the host where cloudflared runs, tunnel ID, connector instances,
management mode (remote or local), routing and upstream service. The local PC,
AI Lab Server and test VM are not interchangeable. Reuse the existing owner's
transport where suitable; do not create a new Hermes or duplicate a live tunnel.

Tunnel connectivity does not by itself enforce user identity. For a public
hostname requiring restricted access, prepare/verify the Access application and
policy before exposing the route. Check alternative hostnames, workers.dev or
preview URLs and direct-origin paths for bypasses. Choose interactive identity
versus machine service authentication deliberately. Never add Everyone/Bypass
just to make a failing UAT pass.

For named tunnel changes inspect the current installed cloudflared help and
configuration owner. A remote-managed token is a connector secret, not an API
administration token. Do not print service command lines that may contain it.
Use supported secret-file/environment facilities after inspecting version support.
Local ingress changes need rule-order and catch-all checks before restart.
Quick tunnels are temporary public exposure, not a private test environment;
they require approved hostname/access scope too.

For private-network access verify client/WARP/Access path and route collisions.
TCP, RDP and SSH differ from browser HTTPS: a published address does not prove
protocol reachability or safe authentication. Do not expose VM administration
or model ports as an incidental fix for browser tests.

Verification: connector health, local upstream health, authorized end-user path,
denied unauthenticated/wrong-role request and absence of alternative bypass.
Inspect actual login/callback flow in browser where relevant. Negative tests use
synthetic roles, not another customer's account. Keep live sessions undisturbed.

Restart/stop only the approved connector on the correct host, accounting for
replicas and active users. Restore the scoped route/policy/config pre-state and
verify both desired access and denial after rollback. Stopping a local process
does not delete DNS, an Access application or remotely managed tunnel resources.

Source (checked 2026-09-06):
https://developers.cloudflare.com/cloudflare-one/networks/connectors/cloudflare-tunnel/
Follow current links for management mode, Access policy and private-network setup.
