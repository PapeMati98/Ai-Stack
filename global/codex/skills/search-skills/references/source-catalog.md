# Source Catalog

Use this as a starting map, not as a trust guarantee. Every candidate still requires audit.

## Higher-Trust Starting Points

- Anthropic Claude Code docs on skills: `https://code.claude.com/docs/en/skills`
- Anthropic public skills examples: `https://github.com/anthropics/skills`
- Anthropic Claude Code plugin examples: `https://github.com/anthropics/claude-code`
- Curated list for discovery only: `https://github.com/ComposioHQ/awesome-claude-skills`

## Security Research And Audit Tools

- Snyk ToxicSkills article: `https://snyk.io/blog/toxicskills-malicious-ai-agent-skills-clawhub/`
- Snyk Agent Scan: `https://github.com/snyk/agent-scan`
- skill-audit reference implementation: `https://github.com/simplybychris/skill-audit`
- OWASP Agentic Skills Top 10, Malicious Skills: `https://owasp.org/www-project-agentic-skills-top-10/ast01`

## UI/Design Candidates Observed 2026-06-24

- `frontend-design` from `anthropics/skills`: best primary candidate for visual direction; prompt-only, narrow, no obvious install-time code in SKILL.md. Good inspiration for a local Codex skill.
- `web-artifacts-builder` from `anthropics/skills`: useful for Claude artifacts with React/Tailwind/shadcn and bundling scripts; heavier than needed for normal app UI work.
- `webapp-testing` from `anthropics/skills`: useful Playwright workflow; overlaps with Codex browser/frontend verification guidance.
- `brand-guidelines` from `anthropics/skills`: Anthropic-specific branding; not suitable unless the user explicitly wants Anthropic styling.
- `anydesign` by `uxKero`: interesting design extraction from images/sites/Figma into design docs; audit before install because it can involve external capture flows and more references.
- Large community UI bundles such as `ui-ux-pro-max`: review carefully; large script-heavy skill bodies increase audit and context risk.

Default recommendation for UI: install/adapt one focused design-direction skill, not a bundle.
