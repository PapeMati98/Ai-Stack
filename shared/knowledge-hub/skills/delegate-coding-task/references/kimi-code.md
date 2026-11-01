# Kimi Code CLI — delegation recipe

> Scope: operational gotchas only. Routing and model selection are owned by
> Agent Fleet (`profiles.json`) and Model Radar (`MODEL-MARKET-AUDIT.md`).

Binary: `/c/Users/Mateusz/.kimi-code/bin/kimi.exe` (0.28.1; upgrade:
`irm https://code.kimi.com/kimi-code/install.ps1 | iex`).
Default model: `kimi-for-coding` (K2.7 Code, 256k ctx, thinking). **K3 verified
working headless via `-m k3`** (2026-07-21, Allegro plan) — prefer K3 for
delegated frontend/UI and long-horizon tasks.

## Headless patterns

```bash
# Read-only analysis (bare -p; -p rejects --auto and --plan — bare is the baseline)
kimi -p "$(cat brief.md)" --output-format text

# Delegate to K3 (frontend, long-horizon) — preferred tier on Allegro
kimi -m k3 -p "$(cat brief.md)"

# Executor (yolo approves all actions — script's max exposure for kimi)
kimi -p "$(cat brief.md)" --yolo

# Machine-readable stream
kimi -p "$(cat brief.md)" --yolo --output-format stream-json

# Plan-then-report style consult
kimi -p "$(cat consult.md)" --plan --auto
```

Useful flags: `-p/--prompt` (single non-interactive run), `-m/--model <alias>`,
`--auto` (auto permission mode), `-y/--yolo` (approve all), `--plan`,
`--output-format text|stream-json`, `-S/--session [id]`, `-C/--continue`.
Other entries: `kimi acp` (ACP server), `kimi provider`, `kimi doctor`.

## Gotchas

- **Shared quota**: same Allegro subscription pool as the Kimi Work orchestrator
  (5-hour window + weekly). Delegate here for Kimi-specific strengths (frontend)
  or true parallelism — not as a default executor.
- `-p` takes the prompt as an argument — huge briefs can hit arg-length limits;
  keep briefs tight (or point to files the agent should Read).
- `--yolo` approves everything including shell commands — only inside the
  intended project dir, never on production checkouts.
- **Image deadlock (LLM Wiki KI-001)**: WebP/GIF or oversized images 400 the
  endpoint and the failed image poisons the whole session. Delegated kimi tasks:
  PNG/JPEG only, viewport-sized segments, no full-page screenshots — or avoid
  images entirely and analyze DOM/CSS.
- Owner's global rules load via the agent-stack bridge; restate hard constraints
  in the brief anyway.
