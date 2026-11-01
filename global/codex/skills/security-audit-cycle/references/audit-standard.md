# Standard cyklicznego audytu bezpieczenstwa

## Cel

Wykrywac brak regularnej kontroli bezpieczenstwa w aktywnie rozwijanych
systemach i zostawiac jednoznaczny, datowany slad wykonania audytu.

## Bramka swiezosci

- Domyslna waznosc audytu: 14 dni.
- Projekt jest aktywny, gdy ma brudny worktree Git albo plik z kodem lub
  konfiguracja zmodyfikowany w ostatnich 7 dniach.
- Aktywny projekt bez ukonczonego audytu albo z audytem starszym niz 14 dni
  otrzymuje `AUDIT_REQUIRED`.
- Samo istnienie starego katalogu nie wymusza audytu.

## Klasyfikacja

- `public` - publiczny kod bez danych poufnych.
- `internal` - kod wewnetrzny bez danych klientow.
- `confidential` - kod lub konfiguracja systemu biznesowego.
- `restricted` - tajemnice, dane zdrowotne, finansowe, uwierzytelniajace albo
  dane klientow o podwyzszonej wrazliwosci.

Automat dziala lokalnie dla kazdej klasyfikacji. Kod `restricted` nigdy nie
moze trafic do skanu chmurowego. `confidential` wymaga dodatkowego potwierdzenia.

## Archiwum

Domyslny katalog:

```text
%USERPROFILE%\.agents\security-audits\
  <slug>-<hash-sciezki>\
    project.json
    latest.json
    ledger.jsonl
    audits\
      <timestamp>__<agent>\
        metadata.json
        baseline.json
        REPORT.md
        evidence\
        tool-output\
```

`ledger.jsonl` jest append-only. `latest.json` wskazuje ostatni ukonczony audyt.
Zapisy JSON sa atomowe. Blokada projektu zapobiega rownoleglym audytom.

## Minimalny raport

1. Identyfikator, data, agent i klasyfikacja.
2. Zakres oraz wykluczenia.
3. Potwierdzenie, czy kod opuscil komputer.
4. Inwentaryzacja technologii i stanu Git.
5. Findings z severity, plikiem, linia, regula i rekomendacja.
6. Reczna weryfikacja false positive.
7. Ograniczenia audytu.
8. Werdykt `PASS`, `REVISE` albo `NO-GO`.
9. Termin kolejnego audytu.

## Interpretacja werdyktu

- `NO-GO` - finding krytyczny albo brak bezpiecznej granicy zakresu.
- `REVISE` - finding wysoki/sredni lub istotny brak dowodu.
- `PASS` - brak potwierdzonych findingow blokujacych w zbadanym zakresie.

`PASS` nie jest gwarancja bezpieczenstwa i nie zastepuje testow penetracyjnych.

## Ograniczenia automatu

Lokalny baseline nie dowodzi poprawnosci:

- modelu uprawnien i logiki domenowej,
- izolacji tenantow,
- konfiguracji uruchomionej produkcji,
- podatnosci zaleznosci wedlug aktualnych baz CVE,
- bezpieczenstwa infrastruktury poza katalogiem projektu.

W tych obszarach wymagany jest reczny review lub dedykowane narzedzie.
