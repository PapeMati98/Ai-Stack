# OpenClaw local lane, 2026-09-17

Owner workstation has official Qwen3.8-27B BF16 at localhost11440 through vLLM.
Configured context524288; actual499269-token input passed at500W, peak77C.
Text, reasoning, streaming and native OpenClaw read-tool/gateway canaries passed.

The OpenClaw local profile is explicitly enabled. Before each dispatch the
external runtime is checked for exact model identity and adequate context.
An unavailable or mismatched backend fails visibly; no automatic server repair
or cloud fallback. Local-Qwen legacy profiles remain present but are not routed.
Muse native CLI and ZAI profiles from0.9.5 are preserved. OpenClaw-Muse remains
disabled until full tool-transport acceptance; inference-only is insufficient.

Implementation permits confined file read/write/edit, while reviews cannot write.
Shell/network/ambient skills/plugins/MCP and subagents remain disabled for Fleet
isolated jobs. These are tool restrictions, not an OS sandbox. Caretaker Office
coordination is a separate explicit integration; no ambient access is implied.

Model lineage is qwen (underlying model), CLI provider is openclaw, response
provider is vllm. Separate harness names do not imply independent review.
Missing token telemetry remains unknown. Provider inference charge is0, with
electricity and hardware outside this measure.

2026-10-14 candidate: the local-Qwen implement profile opts into one fleet_sandbox stdio broker. Exactly exec/write_file/read_file/status are bound to a generated per-run session and retained volume. The Pythonw child uses isolated stdio, verifies the approved core/broker fingerprints and local image digest, and pins the local Docker daemon. Native host exec, network, other sessions, destroy, ambient MCP and plugins remain denied. Read-only modes and callers without the explicit opt-in keep the previous policy. Rollback: use the preceding installed Fleet release or disable the profile opt-in in a new reviewed release; no container/volume deletion. Native proof belongs to the parent UAT report, not this candidate documentation.
