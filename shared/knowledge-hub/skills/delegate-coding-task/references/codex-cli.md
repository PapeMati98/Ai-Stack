# Codex CLI — delegation recipe

> Scope: operational gotchas only. Routing and model selection are owned by
> Agent Fleet (`profiles.json`) and Model Radar (`MODEL-MARKET-AUDIT.md`).

Binary: `/c/Users/Mateusz/AppData/Roaming/npm/codex.cmd` (0.144.3).
Configured default model: `gpt-5.6-sol` (lanes: Sol=coding, Terra=mid, Luna=budget volume).

## Headless patterns

```bash
# Read-only analysis (safe) — prompt from stdin via '-'
codex exec -s read-only -C "<workdir>" --skip-git-repo-check - < brief.md

# Executor with workspace writes (work)
codex exec -s workspace-write -C "<workdir>" --skip-git-repo-check - < brief.md

# Override model per run
codex exec -m gpt-5.6-luna -s workspace-write -C "<workdir>" - < brief.md

# Native code review of the current repo
codex exec review -C "<workdir>"

# Resume last session for a follow-up
codex exec resume --last "follow-up instruction"
```

Useful flags: `-s/--sandbox read-only|workspace-write|danger-full-access`,
`-m/--model`, `-C/--cd`, `--add-dir`, `--skip-git-repo-check`, `--ephemeral`
(no session files), `-c key=value` config overrides (TOML), `-p/--profile`,
`-i/--image` (attach images to initial prompt), `--enable/--disable <feature>`.
Subcommands: `exec`, `exec resume`, `exec review`.

## Gotchas

- Real OS sandbox: `read-only` genuinely cannot write — perfect default for
  analysis; `workspace-write` scopes writes to the workdir. `danger-full-access`
  is never used by the delegation script.
- Windows sandbox configured as `elevated` in owner config — do not override.
- Sol can overthink vague briefs: keep steps explicit and cap scope;
  for mechanical volume prefer `-m gpt-5.6-luna`.
- Long prompts: stdin (`-` or piped) avoids arg limits; stdin + arg are merged
  (stdin appended as `<stdin>` block).
- Trusted projects are registered in `~/.codex/config.toml`; outside them keep
  `--skip-git-repo-check` and explicit `-C`.
