# routing-ledger

Read-only rejestr decyzji routingu -> wynik, zbudowany z istniejacych artefaktow
runow. Zrodlo danych do porownania JEV z modelem lokalnym (tryb cienia) i do
ewentualnego dotrenowania. Nie zmienia routingu ani zrodel.

```bash
python routing_ledger.py build
python shadow_replay.py            # etap 2a: replay offline na lokalnym Qwen (127.0.0.1:11440)
python -m unittest test_routing_ledger test_shadow_replay
```

Wynik: nowe pliki `ledger-<UTC>.jsonl` i `summary-<UTC>.json` w
`~/.agents/private-artifacts/routing-ledger/` (otwarcie `x`, bez nadpisywania).

## Zrodla

| typ rekordu | zrodlo | co niesie |
|---|---|---|
| `dispatch` | `~/.agents/agent-fleet/runs/run-*.json` | agent/model/rodowod, kind/mode/complexity, bramki, quota, status, kod porazki, tokeny, czas |
| `selection` | folder roli TODO (`request.json`, `assessment.json`, `tier-selection.json`, `preflight-skipped.json`, `result.json`) | cechy zadania (liczby, rozszerzenia plikow, narzedzia, ryzyko), kandydaci planisty, kolejnosc klas, pominiecia preflight, wybor, werdykt |
| `node` | `state.json` grafu (`plan_hash` + `nodes`) | kroki maker/reviewer/tests, statusy, REPAIR, tokeny |
| `jev` | raport z `consultation_status` + `binding` | difficulty/parallelism/consultation/fit, koszt |

Foldery rol i grafow sa znajdowane przez `output_dir` rekordow Fleet (wiele
korzeni projektow) oraz przez przejscie domyslnych korzeni: `~/.agents`,
`agent-stack/docs`, `agent-stack/outputs`, `Agenci AI/research-runs`
(`--root` zastepuje liste). Checkouty/worktree/staging sa pomijane.

## Prywatnosc

Pola z tekstem wolnym (cele, tresc zadan, powody, podsumowania, findings,
handoffy, sciezki) nie sa kopiowane: powody -> stale kody (`REASON_CODES`),
bramki -> `KNOWN_GATES` albo `other-gate`, rozszerzenia plikow -> `KNOWN_EXTENSIONS`
albo `(other)`, sciezki i id zadan -> sha256[:16], czasy -> tylko ISO-8601.
Pozostale stringi pochodza z pol maszynowych (agent, model, status, cause) i
przechodza przez `safe()`: identyfikator bez `/`, `\`, `:` i rozszerzenia pliku,
inaczej `REDACTED`. Ryzyko resztkowe: wartosc o ksztalcie identyfikatora wpisana
recznie w pole maszynowe (np. nazwa klienta jako `cause`) przejdzie filtr ksztaltu.
Wyjscie nie moze wskazywac katalogu runow Fleet ani korzenia zrodel.

## Metryka tokenow

`fresh` = wejscie bez cache + wyjscie. Adaptery roznie licza `total_tokens`
(czesc wlicza odczyty cache), wiec sumy porownawcze uzywaja `fresh`. Gdy brak
`fresh_input_tokens`, zakladamy `input_tokens` z cache i odejmujemy
`cache_read_tokens` (zgodne z probkami grok/zcode z 2026-10-11). Brak wejscia
albo wyjscia = unknown (nie zaniza sumy).
`wasted_*` = dispatch FAILED/PARTIAL/BLOCKED; PARTIAL moze zawierac prace do
odzyskania z worktree, wiec to gorna granica, nie strata pewna.
`rework_*` = tokeny makera z rundy odrzuconej przez REPAIR (koszt poprawek, nie strata).
Kroki wznowionych grafow sa liczone raz (po `run_id`).
