# Test-first contract

```text
contract: co ma byc prawda dla uzytkownika/API/danych
red_test: test, ktory failuje przed zmiana + przyczyna fail
minimal_change: pliki i zachowanie
green_test: wynik po zmianie
blast_radius: testy sasiednie i ich wyniki
user_backend_check: PASS | FAIL | NOT_APPLICABLE + evidence
known_gaps: ...
acceptance: PASS | REVISE | BLOCKED
```

Czerwony test musi sprawdzac zachowanie, a nie obecnosc tekstu lub implementacyjny szczegol. `PASS` wymaga zielonego testu kontraktowego i odpowiednich testow sasiednich.

## Warunkowe punkty kontrolne regresji AI (stosuj wyłacznie, gdy zmiana dotyka danej warstwy)

- **API / integracje (`sandbox vs prod parity`)**: sprawdz, czy sciezka sandbox/mock i sciezka produkcyjna zwracaja ten sam ksztalt odpowiedzi, kody statusu i strukture bledow.
- **Zapytania i projekcje danych (`SELECT` / DTO / serializer)**: gdy logika lub widok korzysta z nowego pola, sprawdz testem, ze zapytanie (`SELECT`/ORM) i serializer faktycznie zwracaja to pole (brak cichego `None`/`undefined` przez pominieta kolumne).
- **UI — stan po bledzie (`error-state stale data cleanup`)**: po bledzie pobrania lub zmianie parametrow widoku upewnij sie, ze komponent czysci lub jawnie oznacza poprzednie dane, zamiast wyswietlac nieaktualny rekord jako biezacy.
- **UI — wycofanie aktualizacji optymistycznej (`optimistic update rollback`)**: gdy mutacja po stronie API zakonczy sie bledem, sprawdz, ze stan UI wraca do wartosci sprzed operacji i komunikuje niepowodzenie.
