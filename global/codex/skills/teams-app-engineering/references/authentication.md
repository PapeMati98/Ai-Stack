# Choose the authentication flow deliberately

Preserve a working existing flow. Select NAA or traditional Teams SSO based on
app architecture, current SDK and supported host clients; do not combine both
recipes or promise universal seamless login from a package check.

## NAA for an Entra SPA tab

[Nested app authentication](https://learn.microsoft.com/en-us/microsoftteams/platform/concepts/authentication/nested-authentication)
uses MSAL.js with the host broker and supports delegated Graph calls from the
SPA. Confirm host support at runtime and the app's registered SPA/broker redirect
URI. Prefer silent acquisition, then a user-initiated interactive fallback.
NAA is not a recipe for non-Entra providers or a confidential backend.

The optional manifest prefetch configuration must match the runtime client ID,
scopes, redirect URI and claims. It is a performance enhancement with limited
client support, not a mandatory authentication dependency. A down-level client
needs a tested fallback. Use the chosen resource's token for that resource;
a Graph access token is not an access token for the application's own API.

## Traditional Teams SSO with a backend

[Teams tab SSO](https://learn.microsoft.com/en-us/microsoftteams/platform/tabs/how-to/authentication/tab-sso-overview)
uses TeamsJS v2 `app.initialize()` and `authentication.getAuthToken()`.
`webApplicationInfo.id/resource`, the Entra client ID/Application ID URI,
exposed scope and supported Teams host preauthorization must agree.

Validate the inbound access token for the app's API on the server. If that API
needs Graph on behalf of the user, perform the supported OBO exchange server-side
with the separately consented Graph scopes. Do not send `getAuthToken()` directly
to Graph or return the backend's OBO token to the browser. A token that happens
to decode is not a validated identity.

## Common trust boundary

Use maintained JWT/MSAL middleware and OIDC metadata for the expected issuer.
Validate signature, issuer, audience, expiry and tenant, then required scopes or
roles and resource-specific access. Map `(tid, oid)` to the existing application's
user/role model. Never map an admin role from email domain, display name, a URL
parameter or TeamsJS `getContext()`; context is not proof of authorization.

Do not place tokens in URLs, logs, Office, shared knowledge or ad-hoc persistent
storage. Use the selected library's supported cache. Keep secrets/certificates
server-side; any `NEXT_PUBLIC_*` value is public to the browser. Token acquisition
does not authorize all backend routes or grant all SharePoint/Planner access.

For popup/fallback login preserve OAuth state/nonce and PKCE as required by the
chosen library/flow; verify redirect origin and return path. Login redirects
inside an iframe and dependence on third-party cookies can fail on Teams clients.
Use the supported host authentication API and test that branch.
[Popup authentication](https://learn.microsoft.com/en-us/microsoftteams/platform/tabs/how-to/authentication/auth-tab-aad).

Test valid user, forbidden role, wrong tenant, wrong audience, expired token,
consent denial and a host without the selected capability. No real grants or
secret changes for a synthetic authentication test.
