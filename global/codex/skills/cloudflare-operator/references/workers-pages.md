# Workers and Pages

Read pinned tooling, wrangler.jsonc/toml, build configuration and CI owner without
dumping secrets. Bind account, project/script, environment, branch and artifact
SHA before any command. Inspect current installed command help; never assume a
flag or CLI default selects staging. Avoid global installs or login switches.

DEV means local/sandbox bindings and synthetic side effects. Inspect each binding:
D1, KV, R2, Durable Objects, Queues, services and secrets. A local development
server can still access remote resources if configured that way. Use the data
governance workflow before migrations or changes to persistent data/retention.

For Workers distinguish build/dry-run, version upload and deployment/traffic
activation. Revalidate exact routes/custom domains and bindings with the artifact.
Uploading a version is not proof users receive it. For Pages distinguish preview
and production branch deployment; do not change a project's branch to force a
release. Preview URLs may be public and may reference real resources: check both
access protection and downstream isolation before user tests.

Acceptance: local tests and build plus deployed version ID/traffic, route health,
browser user journey, unauthorized access, binding behavior, errors, and relevant
latency/usage compared with baseline. Use bounded sanitized tail/log windows;
never print production payloads, credentials or environment dumps.

Rollback must specify the previous known-good version/deployment and its compatible
bindings/config. Worker code rollback is NOT database, secret, KV/R2 or external
side-effect rollback. Check Cloudflare's current rollback limitations, especially
Durable Object migrations and resource compatibility. Do not promise rollback
until the previous version and needed resources are available. Pages production
rollback also needs a verified prior deployment; preview success is insufficient.

Sources (checked 2026-09-06):
- https://developers.cloudflare.com/workers/wrangler/commands/
- https://developers.cloudflare.com/workers/versions-and-deployments/rollbacks/
- https://developers.cloudflare.com/pages/configuration/preview-deployments/
