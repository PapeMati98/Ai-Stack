# Search visibility contract

Return a Markdown or JSON audit containing:

- `scope`: property, URL set, locale, engines, device and observation window.
- `evidence`: source, fetch date, revision and freshness status.
- `findings`: id, layer, URL or template, symptom, evidence, severity and confidence.
- `recommendations`: proposed change, expected effect, owner, effort and risk.
- `measurement`: baseline, metric, segment, sampling window and acceptance threshold.
- `volatile_claims`: fact, source, checked date and expiry or refresh rule.
- `decision`: prioritize, revise, blocked or ready-for-approved-implementation.

Check crawlability, canonical and index controls, metadata, content intent,
structured data, internal links, accessibility signals, answer extraction and
citation presence as applicable. Never fabricate rankings, traffic, citations or
schema validity. Read-only analysis and drafts are the default.
