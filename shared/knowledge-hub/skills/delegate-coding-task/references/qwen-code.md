# Qwen Code CLI — delegation recipe

> Scope: operational gotchas only. Routing and model selection are owned by
> Agent Fleet (`profiles.json`) and Model Radar (`MODEL-MARKET-AUDIT.md`).

Binary: `/c/Users/Mateusz/AppData/Local/qwen-code/bin/qwen.cmd` (0.20.0 standalone).
WARNING: `%APPDATA%\npm\qwen.cmd` is a STALE npm shim (0.17.0) — never use it
(same trap as the old Claude shim).
Update channel: `qwen update`.

**Status: SEAT RESERVED — no active subscription yet (owner is acquiring one;
target model: Qwen 3.8 Max).** Do not dispatch real work until auth is verified
with a cheap probe (`qwen -p "Say OK" -o json`).

## Headless patterns

```bash
# Analysis / execution (same shape until approval flags are verified)
qwen -p "$(cat brief.md)" -o json

# Pin a model (+ fallbacks on capacity errors)
qwen -m <model-id> --fallback-model <id2,id3> -p "$(cat brief.md)" -o json

# Resume for follow-ups
qwen --continue -p "follow-up" -o json
```

Useful flags: `-p/--prompt` (headless; stdin appended), `-o/--output-format
text|json|stream-json`, `-m/--model`, `--fallback-model` (up to 3, for capacity
errors), `-s/--sandbox`, `-c/--continue`, `-r/--resume <id>`,
`--safe-mode` (disable customizations for troubleshooting), `qwen update`,
`qwen sessions`, `qwen serve` (experimental HTTP daemon).

## Gotchas

- Approval/permission flags for write mode are UNVERIFIED (no `--yolo`-style
  flag in 0.20.0 help). Until probed with a live plan, treat write delegation
  as untested: first work-mode dispatch = small throwaway task, watched closely.
- Exact model ID for "3.8 Max" unverified — check `qwen -m` accepted aliases
  after activating the subscription; record the working alias here.
- `-p` takes the prompt as an argument — keep briefs tight or point to files.
- Qwen Code is a Gemini-CLI fork: conventions (settings.json, sessions dir)
  live in `~/.qwen/`.
