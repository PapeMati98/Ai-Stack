# Domains, DNS, proxy and TLS

Snapshot only relevant records/settings with their exact IDs and modified state.
Compare type, name, content, priority, TTL and proxy flag; preserve unrelated
fields. Prefer a scoped patch to whole-record replacement. Check competing
A/AAAA/CNAME records, wildcard effects and flattening before proposing a change.

Do not proxy mail-service hosts blindly. Preserve MX, SPF, DKIM, DMARC and
verification records unless explicitly in scope; a website migration does not
authorize a mail migration. Nameserver, registrar, DNSSEC DS and account-transfer
changes are separate high-impact operations, not ordinary record edits.

Before cutover validate the actual upstream and certificate/hostname. Decide
DNS-only versus proxied deliberately: removing the proxy can expose the origin
and bypass edge protection. A proxied public answer need not equal the origin IP.
Full (strict) is the preferred TLS target when supported by a valid origin
certificate; do not switch a live zone blindly or downgrade to Flexible/disable
verification to hide an origin error. Origin CA trust differs from browser trust.

Accept only after checking authoritative DNS and representative recursive/client
resolution, expected TTL/cache behavior, HTTPS hostname/certificate, redirects,
the correct environment and real app behavior. Test IPv4/IPv6 when both published.
Do not call a partial propagation observation globally complete. HSTS and DNSSEC
changes have their own rollback constraints and need explicit planning.

Rollback restores the exact previous values for the changed records/settings,
after checking no concurrent edit occurred. Document propagation delay and
residual cached answers; do not promise immediate global reversal. Never purge
an entire zone or replace a whole zone-file as a record-level rollback shortcut.

Sources (checked 2026-09-06):
- https://developers.cloudflare.com/dns/manage-dns-records/how-to/create-dns-records/
- https://developers.cloudflare.com/ssl/origin-configuration/ssl-modes/full-strict/
