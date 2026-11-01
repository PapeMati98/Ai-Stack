# WAF, rate limits, cache and redirects

Inventory the active ruleset phase, rule IDs, ordering, plan support and matching
expression for the exact hostname/path. Preserve unrelated rules. Trace a real
failure by sanitized Ray ID/time, edge status and origin status; distinguish WAF,
Access, rate limiting, TLS and application failure before changing protection.

WAF/rate-limit rollout: narrow rule, appropriate managed action and a measured
observation/test phase where supported. Verify legitimate browser/API/webhook
traffic as well as blocked synthetic cases. A service webhook may not solve an
interactive challenge. Test the rate-limit aggregation key (user/token/IP), proxy
trust and shared-NAT effect; never disable the entire WAF or add a broad skip as
a convenience fix. Active attack simulation needs its separately approved scope.

Cache acceptance must include anonymous and at least two synthetic user roles.
Sensitive/authenticated responses must not leak between users, cookies, tenants
or authorization states. Inspect cache keys, bypass rules, Cache-Control,
Set-Cookie, status and invalidation after an update; never blindly Cache Everything
on an authenticated application. Headers alone do not prove absence of leakage.

Prefer a targeted purge for the changed object using supported URL/tag/prefix
scope. Purge Everything is a separate high-impact choice; it can overload origin.
Purge cannot restore previously cached content as a transactional rollback:
revert the underlying rule/object, then verify refetch, correctness and load.

Redirect tests: hostname/path/query semantics, intended status, method preservation,
login callbacks, loops, HTTP-to-HTTPS and canonical URL behavior. Check rule
precedence against Workers and other redirect products. Use a bounded redirect
count, not an unbounded follow. Test mobile/browser user journey if user-visible.

Rollback restores only the changed rule IDs/config after checking concurrent
changes. Repeat authorized/unauthorized and cache-isolation tests. Report propagation,
cache refill and delayed analytics rather than assert instantaneous recovery.

Sources: https://developers.cloudflare.com/cache/how-to/purge-cache/
and current WAF/rules documentation at https://developers.cloudflare.com/waf/
and https://developers.cloudflare.com/rules/ (refresh feature and plan details).
