# Grok CLI — delegation recipe

> Scope: operational gotchas only. Routing and model selection are owned by
> Agent Fleet (`profiles.json`) and Model Radar (`MODEL-MARKET-AUDIT.md`).

Binary: `/c/Users/Mateusz/.grok/bin/grok.exe` (0.2.103 stable).
Default model: `grok-build` (Grok 4.5 family) — cheap, fast, automation-oriented.

## Headless patterns

```bash
# Read-only / analysis pass (verified 2026-07-20: headless runs CANCEL on any mode
# that can prompt — plan and dontAsk both die after 1 turn; use auto)
grok --prompt-file brief.md --permission-mode auto --output-format json

# Executor with edits
grok --prompt-file brief.md --permission-mode acceptEdits \
  --max-turns 30 --output-format json

# Structured output chores
grok --prompt-file brief.md --json-schema '{"type":"object","properties":{...}}'

# Parallel candidates, pick best (headless only)
grok --prompt-file brief.md --best-of-n 3 --max-turns 30 --output-format json
```

Useful flags: `-p/--single` (inline prompt), `--prompt-file`, `--prompt-json`,
`--output-format plain|json|streaming-json`, `--json-schema`,
`--permission-mode default|acceptEdits|auto|dontAsk|bypassPermissions|plan`,
`-m/--model`, `--max-turns`, `--best-of-n`, `--check` (self-verification loop),
`--cwd`, `--sandbox <profile>`, `-w/--worktree` (git worktree isolation),
`--tools`/`--disallowed-tools`, `-r/--resume`, `--reasoning-effort`.

## Gotchas

- Grok can tool-loop on vague briefs — cap turns via `MAX_TURNS` env when you
  see looping; no cap by default (owner: effectiveness first).
- `--prompt-file` is the cleanest brief channel (no arg-length issues).
- `--worktree` isolates risky experiments in a throwaway git worktree.
- JSON output: parse the final message; keep `session_id` for `--resume`.
- Permission modes: `auto` = the only non-prompting mode that completes headless
  read-only runs (`plan` and `dontAsk` CANCEL after 1 turn — verified);
  `acceptEdits` = file edits; `bypassPermissions` = full power (`full` mode).
- Plan limits are unprobed — start with small dispatches and watch behavior
  before routing heavy weekly volume.
