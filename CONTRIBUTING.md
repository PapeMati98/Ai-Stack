# Contributing to AI Stack

Thanks for your interest. AI Stack is maintained by a small team, so focused
contributions are the easiest to review.

## Good first contributions

- **Translate a skill to English.** Some `SKILL.md` files under
  `global/codex/skills/` are written in Polish. Keep the meaning, structure,
  safety rules and examples; do not change the skill's `name`.
- **Report a portability problem.** The full stack is Windows-first. Issues
  that show exactly what fails on macOS or Linux (command, output, OS and
  Python version) are very useful.
- **Improve a standalone skill** such as `html-video`, `operations-playbook` or
  `search-visibility`, with a concrete example of the better behaviour.
- **Add tests** that reproduce a bug before it is fixed.

## Ground rules

- One topic per pull request. Describe what changed, why, and how you checked it.
- Run the tests next to the code you changed, for example
  `python -B -m pytest -q path/to/test_file.py`. Many tests need the
  maintainer's local runtimes; say which ones you could not run.
- Never add credentials, tokens, personal data or real customer names. Tests
  use synthetic data and fictional names.
- Keep `.gitattributes` line-ending rules intact: source files keep their
  existing line endings byte for byte.
- Third-party code needs its license and attribution in the same folder and a
  line in [NOTICE](NOTICE). Do not add code you are not allowed to relicense
  under Apache-2.0 (or a compatible license kept alongside it).
- Security problems go through private reporting, not issues: see
  [SECURITY.md](SECURITY.md).

By contributing you agree that your contribution is licensed under the
[Apache License 2.0](LICENSE) and that you follow the
[Code of Conduct](CODE_OF_CONDUCT.md).
