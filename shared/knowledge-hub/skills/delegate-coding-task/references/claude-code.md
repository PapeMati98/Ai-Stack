# Claude Code CLI — delegation recipe

> Scope: operational gotchas only. Routing and model selection are owned by
> Agent Fleet (`profiles.json`) and Model Radar (`MODEL-MARKET-AUDIT.md`).

Binary: `/c/Users/Mateusz/AppData/Local/Microsoft/WinGet/Packages/Anthropic.ClaudeCode_Microsoft.Winget.Source_8wekyb3d8bbwe/claude.exe` (2.1.215).
WARNING: `~/.local/bin/claude.exe` is a STALE orphan shim (2.1.181) — never use it.
Models seen in owner config: `claude-opus-4-8`, `claude-fable-5`, Sonnet 5 tier available.

## Headless patterns

```bash
# Read-only analysis (safe)
cat brief.md | claude -p --output-format json --allowedTools Read Grep Glob

# Executor with edits (work)
cat brief.md | claude -p --output-format json --permission-mode acceptEdits

# Consult on a stronger/weaker model
cat consult.md | claude -p --model claude-fable-5 --output-format json

# Cap agentic turns, add fallback
cat brief.md | claude -p --permission-mode acceptEdits --max-turns 25 \
  --fallback-model claude-opus-4-8 --output-format json
```

Useful flags: `-p/--print` (headless), `--output-format json|stream-json|text`,
`--model`, `--fallback-model`, `--permission-mode default|acceptEdits|plan|bypassPermissions`,
`--allowedTools` / `--disallowedTools`, `--max-turns`, `--add-dir`,
`--append-system-prompt`, `--agents` (inline custom subagents JSON),
`-c/--continue`, `-r/--resume <id>` for session resume.

## Gotchas

- JSON output wraps the answer (`result`, `total_cost_usd`, `session_id`); parse
  `result` for the deliverable, keep `session_id` for follow-ups via `--resume`.
- Prompt via stdin (`cat brief | claude -p`) — avoids arg-length limits.
- Permission ladder in delegate.sh: `safe` = Read/Grep/Glob only, `work` =
  acceptEdits, `full` = `--dangerously-skip-permissions` (no gates — trusted
  dirs only).
- Quota: 5-hour rolling window + weekly cap. Fable burns it fastest — reserve
  for consults; Sonnet for bulk executor work.
- **Auth gotcha (verified 2026-07-21):** `claude auth status` reporting
  `loggedIn: true` does NOT guarantee headless works — it reads identity fields,
  not token validity. Symptom of dead headless auth: `-p` runs return
  `api_error_status: 401` while interactive sessions self-heal silently.
  Check `~/.claude/.credentials.json` (`expiresAt` past / empty `refreshToken`).
  Fix: owner runs `claude auth login`, then verify with a cheap probe
  (`claude -p "Say OK"`) before dispatching real work.
- Owner's global rules load automatically (CLAUDE.md bridge in agent-stack) —
  briefs still restate hard constraints; never rely on memory alone.
