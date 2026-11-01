# codex-security-cli (TOMBSTONE)

Ten skill zostal wycofany `2026-09-02` na rzecz wspolnego skilla
`simulate-attack`. Nie uruchamiaj juz tego skilla, nie
synchronizuj jego plikow, nie instaluj jego runtime i nie uzywaj
`scripts/codex_security_safe.py` do skanowania.

## Dlaczego tombstone zamiast cichego usuniecia

Skill jest zadeklarowany w `global/codex/skills-ownership.json` jako
`bootstrap_owned`. Ma aktywne kopie w `~/.codex/skills`,
`~/.claude/skills`, `~/.agents/skills`, `~/.gemini/config/skills` i
`~/.grok/skills`. Usuniecie w originie bez jawnego tombstone'u
zostawiloby:

- wiszace referencje w `security-audit/SKILL.md`,
- wolany `preflight` w `security-audit-cycle/scripts/security_audit.py`,
- historyczny `scripts/rollback_active_copies.ps1` z odwolaniem do
  `codex-security-cli`,
- testy `tests/test_codex_security_safe.py`, ktore moga byc uruchamiane
  przez istniejacy pipeline.

Tombstone zachowuje te referencje, ale przekierowuje czytelnika na
`simulate-attack`.

## Co zrobic

1. Jesli Twoje zadanie uzywalo `codex-security-cli`, przenies je na
   `simulate-attack`. Patrz `../simulate-attack/SKILL.md`.
2. Jesli aktywna kopia skilla pozostaje w drzewie agenta, mozna ja
   zostawic: jest zarchiwizowana i nie jest uzywana przez helper
   `security-audit-cycle` (sprawdza `wrapper.exists()` i
   zwraca `{"available": false}` jesli brak).
3. Jesli chcesz cofnac aktywne kopie do stanu sprzed tego skilla,
   uruchom `scripts/rollback_active_copies.ps1` zgodnie z jego
   dokumentacja. Nie uruchamiaj tego skryptu bezposrednio z
   `bootstrap.ps1`.

## Manifest wlasnosci

`global/codex/skills-ownership.json` zostal zaktualizowany, aby w
`bootstrap_owned` utrzymac nazwe `codex-security-cli`, ale z
`status: deprecated` oraz `replaced_by: simulate-attack`.
`bootstrap.ps1 -ValidateSkills` bedzie wymagac jawnego potwierdzenia
obecnosci tombstone przed synchronizacja.
