> HISTORYCZNE WYLACZNIE: ten dokument obsluguje przypiete dawne runy/kontrakty i ich recovery. Dla NOWEJ pracy obowiazuje TODO z `../SKILL.md` i globalnego AGENTS.md; nie uruchamiaj opisanych tu fidelity/sufficiency, witness, pieczeci ani bramek planu. Zachowaj historyczne dowody i rzeczywiste granice zgody.

# Strong planner participation

The chat model may be cheap. It is a dispatcher, not automatically an architect.
For `full`, public `approve` requires `--planner-evidence witness.json`.
Compact does not require another architect call; use its combined review.

Use an actual qualified planner: Astra, Fable 5.x, Qwen 3.8 Max (runtime alias,
dated response provenance allowed), or Kimi K3/Max. Check Fleet readiness/quota
and route one bounded architecture task. The qualified native parent can review
directly without a second call. Never keep a costly architect polling workers.

After reading the actual intake and candidate plan and accepting its completeness,
the planner emits one exact line in its terminal handoff summary:

`PLAN_REVIEWED <normalized_prompt_sha256> <planning_payload_hash>`

This is provenance, not a substitute for fidelity/sufficiency or quality tests.
A changed plan invalidates it; a valid witness is reused, not purchased again.

Witness file (local, never uploaded):

```json
{"kind":"fleet","source":"C:/.../.agents/agent-fleet/runs/run-....json","source_sha256":"<actual file SHA256>"}
```

Fleet evidence requires COMPLETED, verified observed model identity and runtime,
and the exact bound line in the provider's handoff summary. For a native Codex
strong parent use `kind: codex-native` with its actual JSONL transcript. The
verifier reads session metadata, turn model and assistant output; user text
cannot attest a model. Never copy transcripts into the repository or Hub.
For the existing qualified Claude Code parent use `kind: claude-native`; the
verifier reads actual assistant-message model/session metadata and its bound
review line, not a caller-supplied Fable label. Other native transcript formats
need an explicitly tested adapter; their Fleet receipts already work.
Only the extracted model/session/hash receipt enters the approved plan.

A caller-supplied model label is not proof. Local receipts are not signatures
and cannot defend against a malicious same-user process. Legacy contracts remain
readable. Trusted internal Python API consumers must pass
`require_planner_witness=True`, as the public CLI always does; the compatibility
API is not a public waiver for newly authored full plans.
