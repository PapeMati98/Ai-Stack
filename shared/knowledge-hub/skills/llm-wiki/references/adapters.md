# Tool adapters

The canonical skill lives in `<knowledge-hub>/skills/llm-wiki`. The Windows and
Linux installers deploy managed copies without touching credentials.

| Tool | Skill destination | Rules/context |
|---|---|---|
| Codex | `~/.codex/skills/llm-wiki` | `~/.codex/AGENTS.md` |
| Claude Code | `~/.claude/skills/llm-wiki` | `~/.claude/CLAUDE.md` imports the common rules |
| Kimi Code | `~/.agents/skills/llm-wiki` | `~/.agents/AGENTS.md` |
| Kimi Desktop | `%APPDATA%/kimi-desktop/daimon-share/daimon/skills/shared-agent-knowledge` | managed block in the main agent `IDENTITY.md` |
| Antigravity | `~/.gemini/config/skills/llm-wiki` | `~/.gemini/GEMINI.md` |
| Grok CLI | `~/.grok/skills/llm-wiki` | `~/.grok/AGENTS.md` |
| Qwen Code | `~/.qwen/skills/llm-wiki` | common Agent Stack rules |
| OpenCode / ZCode | shared `~/.agents/skills/llm-wiki` | native shared-skill scan; no duplicate managed copy |
| MiniMax Code 0.2.4 | compatibility scan of `~/.claude/skills/llm-wiki` | fresh native discovery verified this path; no duplicate Hub copy below `~/.minimax/skills` |
| Hermes on Windows | `<HERMES_HOME>/skills/shared-agent-knowledge` | managed block in `<HERMES_HOME>/SOUL.md`; contributor name must be explicit |
| Robert, main Hermes on Linux | resolved shared skills root `/srv/ai-lab/hermes-standalone/home/.hermes/skills/shared-agent-knowledge` | managed block in profile `owner-main/SOUL.md`; contributor `robert-hermes` |

All tools read `~/.agents/knowledge-config.json` for the machine-local vault path. The file contains paths and installation metadata only; never put credentials in it.

Kimi Desktop and Hermes use the alias `shared-agent-knowledge` because either runtime
may already provide a different skill named `llm-wiki`. The alias delegates to the
canonical gateway through `knowledge-config.json`; it does not create another vault.

The canonical `knowledge.py` gateway is contributor-only on every non-owner host.
Its bounded local checkpoint is enabled only when committed policy names the current
machine as `curator_machine`; Robert's Linux host is not that owner. Pass the resolved
skills root explicitly to `install-linux.sh`; do not infer the target through a
profile symlink. The active main Hermes is Robert in profile
`owner-main` on `lab-server`. Krzysztof on the Windows VM is a separate agent and
must never be selected merely because his profile is on a VM.

After installing or updating a skill, start a new session. For Hermes, run
`/reload-skills` or start a new session. Restart Kimi Desktop only after the user
approves closing active sessions.

The vault itself is portable. A remote machine clones the whole knowledge hub, runs the installer with its local vault path, and uses Git only after a private remote and credential policy are approved.
