# Tab hosting and packaging

## Host integration

A tab is an HTTPS web page in a host frame. Teams does not deploy that page's
backend or database. Initialize the installed TeamsJS before host APIs and
check feature support. Keep an ordinary browser path usable without pretending
it has a Teams identity; avoid a hanging initialization or endless auth loop.

Read theme changes/context only for presentation/navigation. Test dark/high
contrast, narrow tab widths, mobile keyboard, keyboard focus and external links.
For a configurable channel tab use the installed SDK's configuration save
contract and stable `entityId`/content URL; a personal static tab is different.

Response CSP `frame-ancestors` must permit the supported Microsoft hosts,
including `https://*.cloud.microsoft` and the applicable Teams origins. Preserve
the existing security policy and omit incompatible framing headers for the tab
route; do not remove security headers globally or allow every origin.
`validDomains` is a manifest host allowlist, not CSP, CORS or an auth policy.
Confirm actual response headers, redirects and asset paths inside Teams.
[Tab requirements](https://learn.microsoft.com/en-us/microsoftteams/platform/tabs/how-to/tab-requirements).

## Package and identities

Keep manifest schema and `manifestVersion` aligned; validate against that schema
using the available official tooling. Package `version` follows the release;
keep Teams app `id` stable for updates and separate it from Entra client IDs.
Add only the requested surface: `staticTabs`, `configurableTabs`, bot, message
extension or other capability. Domains are hostnames, not URL paths; include
only needed origins. Keep development placeholders out of a release package.

The ZIP root contains `manifest.json` and the referenced PNG icons. It is not
the full project directory or a parent folder. Color icon: **192x192**; outline
icon: **32x32**, transparent background/white outline as the design requires.
Custom activity icons are conditional. No `.env`, private key or runtime code
in the package. HTTPS content/website/configuration/privacy/terms URLs must
resolve for their actual use. [Package requirements](https://learn.microsoft.com/en-us/microsoftteams/platform/concepts/build-and-test/apps-package).

## Optional local helper

`python scripts/validate_teams_package.py <zip>` emits redacted JSON and exit
`0` for `PASS_OFFLINE`, `2` for `REPAIR`; malformed command usage exits `2`.
It reads a bounded local ZIP without extracting or writing. Checks include
root layout, unsafe/duplicate entries, size bounds, manifest JSON, unresolved
template fields, app ID/version, domains, tab HTTPS URLs, icon references and
PNG IHDR dimensions. It does not print the manifest or resource identities.

This is **not** a full Microsoft schema validator, image decoder, authorization
test, a check of runtime headers or proof that links are reachable. Run existing
official manifest/build checks and the relevant UAT as well. An upstream sample's
successful build does not validate the modified project.
