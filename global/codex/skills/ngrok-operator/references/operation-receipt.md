# Operation receipt

Use this as a small project-local record, not a new global database.

```text
project / environment / host:
surface: ai-gateway | http | tcp | tls | webhook
objective and exact authorized change:
pre-state evidence (redacted) / owned resource IDs:
credential references only (never values):
upstream / allowed consumers / expiry:
billing path / max test spend / retries / concurrency:
data classification / processors / privacy evidence or UNVERIFIED:
side-effect isolation evidence:
positive test expected / actual / evidence:
negative access or signature test expected / actual / evidence:
business or UAT result:
served model / usage / actual cost: unknown until measured
rollback or cleanup action / verification / resources intentionally retained:
status: VERIFIED | PARTIAL | BLOCKED
remaining prerequisites:
knowledge handoff receipt when material:
```

Never mark VERIFIED solely from installation, a configuration validation, process
start or HTTP status. Report unsupported tests explicitly; never invent PASS.
