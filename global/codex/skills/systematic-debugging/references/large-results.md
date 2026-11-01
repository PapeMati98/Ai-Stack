# Duze wyniki i agregacja dowodow

Helper `summarize_evidence.py` jest **opcjonalny** i stosowany **wylacznie**,
gdy reczny odczyt surowego wyniku jest niepraktyczny: dlugie logi (setki/tysiace
linii), rozlegle raporty testow, duzo powtarzajacych sie wpisow, dlugie diffy.
Nie jest nowym preflightem przed kazda komenda ani nie zastępuje zwyklej sciezki
1-6.

## Co helper robi

- Wczytuje **wprost podany** plik (`--input <path>`); nigdy nie wykonuje
  przechwyconej komendy, nie wykrywa wejsc rekurencyjnie i nie pobiera nic
  z sieci. Brakujace sciezki i inline text sa odrzucane.
- Grupuje linie/rekordy JSONL w deterministyczne sygnatury, **zachowujac
  numeryczne kody bledow** (404 vs 500 pozostaja rozne sygnatury).
- Liczy `coverage.total_records, coverage.parsed_records,
  coverage.skipped_records, coverage.invalid_records` oraz
  `coverage.input_complete` (false gdy `--input-truncated`;
  ujecie w `diagnostics.input_truncated`).
- Redaguje w kazdym wyjsciu (stdout i `--output`): tokeny Bearer, JWT,
  klucze typu `password`/`authorization`/`cookie`/`token`/`api_key`/`secret` i
  dane uwierzytelniajace w URL (`https://user:pass@host/`). Surowe dane
  pozostaja tylko w oryginalnym pliku.
- Zachowuje `--command-exit-code` i `--exit-policy`, oblicza
  `command.command_status` (np. `NO_MATCH` dla `rg` z kodem 1,
  `DIFFERENT` dla `diff` z kodem 1, `SUCCESS`/`FAILED` dla `command`).

## Czego helper nie robi

- Nie wykonuje przechwyconej komendy.
- Nie wczytuje plików o nazwach z bazy znanych sekretów
  (`.env`, `secrets.json`, `credentials.json`, `id_rsa`, ...).
- Nie podaza za symlinkami.
- Nie nadpisuje istniejacego `--output` bez `--force`.
- Nie pozwala, by `--output` aliasowal plik wejsciowy.
- Nie laduje modulu subprocess ani nie wywoluje sieci.
- Nie ujawnia surowych tokenow argumentow w bledach parsera
  (nie-calkowity `--command-exit-code`, zly choice, nieznane flagi):
  nadal zwraca exit 2, ale bez wartosci w stdout/stderr.

## Wyjscie pomocnicze vs status przechwyconej komendy

`helper.exit_status == "SUCCEEDED"` opisuje dzialanie pomocnika, **nie**
wynik testu. Asercja FIXED wymaga reprodukcji przed poprawka i zielonego
testu po poprawce (krok 5 w SKILL.md). Pomocnik z `--command-exit-code 2`
i `--exit-policy command` moze zwrocic `helper.exit_status = "SUCCEEDED"` i
jednoczesnie `command.command_status = "FAILED"` — te dwa fakty nie sa
sprzeczne i oba powinny byc wplywane do raportu.

Kody wyjscia pomocnika:

| Kod | Znaczenie                                                    |
|-----|--------------------------------------------------------------|
| 0   | SUCCEEDED — pomocnik wykonal agregacje poprawnie             |
| 2   | invalid input / contract violation (np. zly format)          |
| 3   | blad wejscia/wyjscia                                         |
| 4   | blad wewnetrzny pomocnika                                    |

## Przyklad: agregacja dlugiego logu testu

```powershell
python -B (Join-Path $SkillRoot 'scripts\summarize_evidence.py') `
  --input "D:\path\to\captured.txt" `
  --format text `
  --command-exit-code $LASTEXITCODE `
  --exit-policy command `
  --top 10 `
  --max-examples 3 `
  --output "D:\path\to\summary.json"
```

Podobne sa niedocelowe: brak `--force` chroni istniejacy `summary.json`.

## Przyklad: agregacja JSONL z `--exit-policy rg`

```powershell
python -B (Join-Path $SkillRoot 'scripts\summarize_evidence.py') `
  --input "D:\path\to\rg-capture.jsonl" `
  --format jsonl `
  --command-exit-code $LASTEXITCODE `
  --exit-policy rg `
  --top 20
```

Dla `rg` kod 1 → `command_status = NO_MATCH`; helper dalej zwraca 0 i
`SUCCEEDED`. Status `NO_MATCH` nie oznacza bledu reprodukcji.

## Wymuszone pokrycie

Coverage zawsze zawiera `coverage.total_records, coverage.parsed_records,
coverage.skipped_records, coverage.invalid_records` i `coverage.input_complete`;
`diagnostics.input_truncated` oznacza material uciety przez zewnetrzny bufor.
Brak lub uciety material **nigdy** nie jest raportowany jako pelna populacja.

## Testy

`scripts/test_summarize_evidence.py` -- akceptuje `--artifact-root`, zatrzymuje
syntetyczne dane pod zadanym katalogiem bez czyszczenia. Pokrywa:
licznosci vs ograniczone wyswietlanie, powtarzajace sie sygnatury,
rozne kody bledow, ANSI/CRLF/Unicode, obcinanie materialu, niepoprawny
JSONL, redakcje w kazdej sciezce wyjscia, semantyke NO_MATCH/DIFFERENT
i ochrone wejscia/wyjscia.