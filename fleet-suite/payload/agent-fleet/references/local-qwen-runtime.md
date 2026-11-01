# Shared Local Qwen Runtime

`qwen-local` and `qwen-local-5070ti` are aliases for one externally managed,
serialized Ollama runtime. They are bounded zero-provider-cost implementation
profiles. They may enter ordinary routing only after runtime readiness passes;
the local endpoint remains one serialized slot and never gets automatic
start/stop/repair behavior.

## Frozen contract

- OpenAI-compatible endpoint: `http://127.0.0.1:11439/v1`
- Native Ollama tags endpoint: `http://127.0.0.1:11439/api/tags`
- Model: `huihui_ai/Qwen3.8-abliterated`
- Native manifest digest:
  `bdc5e13cd1645bc409e95a7d9e864dc3ce3e5fe5beeb24a43c193e8041edd3e0`
- Executable:
  `%LOCALAPPDATA%\MateuszAI\ollama\0.33.2\ollama.exe`
- Executable version: `0.33.2`
- Executable SHA-256:
  `c79df1e0c1bfa10ed813c7030ac4c3ba38bb0e350bd7322d9bb58320343235c6`
- Context: exactly 16,384 tokens
- Capacity: exactly one slot shared by both aliases
- Provider cost: `0 zł`; electricity and hardware costs are outside Fleet
  telemetry

Fleet validates the listener owner, the executable hash before and after the
version probe, the exact version, the OpenAI model inventory, and the digest
served in `/api/tags`. A matching tag name without the pinned manifest digest
is not valid. `:latest` is admitted only when its native digest equals the
frozen digest.

## Operational boundary

The runtime is externally managed. Fleet only inspects it before a permitted
run. Fleet does not start, stop, restart, repair, download, retag, or modify
Ollama or the model. The lane has no provider turn quota, but it retains a hard
tool ceiling, wall-time ceiling, exact model-digest gate, and one shared
capacity lock. If the listener or any pinned identity check fails, the router
excludes the local lane and uses the next eligible provider.
