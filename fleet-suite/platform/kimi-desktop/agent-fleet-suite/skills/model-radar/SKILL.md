---
name: model-radar
description: Maintain model capability and quota evidence for lean task selection.
---

# Model Radar

Refresh stale model facts only when needed for selection. Reuse current evidence within a work wave. A market audit is not required before every TODO node. Unknown availability is not zero; retain privacy and exact-model checks.

## Runtime identity

```powershell
$suite = if ($env:AGENT_FLEET_SUITE_ROOT) { $env:AGENT_FLEET_SUITE_ROOT } else { "$env:USERPROFILE\.agents\runtime\agent-fleet-suite\lean-0.9.142" }
$expectedRuntime = "e1c59b4b427a96656bcaf1a3a14638cbcfdbf4bfa63cc63a1568130bed4acc58"
python "$suite\agent-fleet\scripts\verify_runtime.py" --runtime-root $suite --expected-fingerprint $expectedRuntime --package-version 0.9.142
```

Verify once per unchanged work wave. If verification fails, diagnose the exact files; do not silently switch to another runtime. Existing legacy current runtime is not modified by this release.
