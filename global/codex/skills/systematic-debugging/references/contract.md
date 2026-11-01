# Debug contract

## Input

- `symptom`: obserwowany blad i oczekiwany kontrakt
- `scope`: dozwolone moduly, srodowisko i ograniczenia
- `reproduction`: komenda lub kroki aktywnego odtworzenia oraz wynik `REPRODUCED` albo `NOT_REPRODUCED`; same historyczne logi sa dowodem incydentu, nie reprodukcja
- `evidence`: timestamp, wersja/commit, konfiguracja bez sekretow i artefakty
- `incident` (tylko produkcja): krotka os czasu symptomu, deployow i konfiguracji; korelacja, konkurencyjne hipotezy, test je odrozniajacy i nierozstrzygniete luki. Bez zgody tylko odczyt produkcji. Reprodukcja w non-prod wymaga danych syntetycznych lub zredagowanych i blokady realnych skutkow ubocznych.

## Hypothesis ledger

| id | hipoteza | test rozstrzygajacy | wynik | dowod |
|---|---|---|---|---|
| H1 | ... | ... | open/confirmed/rejected | sciezka lub test |

## Output

```text
status: FIXED | NOT_REPRODUCED | BLOCKED
root_cause: ...
change: pliki i zakres
regression_test: nazwa + wynik
adjacent_tests: ...
rollback: ...
evidence_gap: none albo opis
```

`FIXED` wymaga reprodukcji przed poprawka i zielonego testu po poprawce. `NOT_REPRODUCED` nie jest potwierdzeniem braku bledu. Nierozstrzygnieta przyczyna przy odtworzonym objawie nie zmienia statusu reprodukcji.
