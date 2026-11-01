> HISTORYCZNE WYLACZNIE: format dawnego oddzielnego kontraktu. W nowej pracy ownership, dostep, jakosc, kompatybilnosc i recovery umiesc w istniejacym TODO i odbiorze produktu wedlug `../SKILL.md`; nie wymagaj osobnej pieczeci/JSON ani wymyslonych decyzji DPO/DPIA. Produkcja, dane i rzeczywiste decyzje wlasciciela zachowuja swoje bramki.

# Format kontraktu data governance

Kontrakt jest JSON-em z `schema_version: 1` i
`contract_schema_version: 1.0.0`. `DRAFT` nie ma `frozen_at` ani
`contract_hash`. `seal` tworzy `READY`, dodaje czas UTC i SHA-256 kanonicznej
reprezentacji (UTF-8, sortowane klucze, brak whitespace). Po zapieczetowaniu
pliku nie edytuj.

`D1` ma `profile: compact`; moze pominac `privacy` i `migration`, gdy
klasyfikator nie wykryl ich triggerow, oraz miec pusta liste detailed access
controls przy istniejacym lokalnym dostepie. `D2-D3` maja `profile: full`.

## Sekcje obowiazkowe

- `contract_id`, `project_slug`, `tier`, `profile`, `triggers`;
- `change`: summary, materiality, synthetic-only i changed surfaces;
- `actors`: realny business owner, data steward i status legal/DPO;
- `planning_contract`: ID, hash i materialne `REQ-*`;
- `sources`: owner decisions, primary standards, policy, schema, repo albo live
  observation z wersja/commitem i czasem obserwacji;
- `assets`: ID, kind, owner, steward, source-of-truth, classification,
  personal/special-category, environments i consumers;
- `purposes`: aktywa, opis, dozwolone i zakazane uzycia oraz decyzja ownera;
- `lineage`: wejscia, nazwane transformacje i wszystkie materialne wyjscia;
- `data_contract`: wersja, compatibility i pola z typem, nullability, opisem i
  klasyfikacja;
- `access_controls`: principal, privileges, scope, justification i reviewer;
- `quality_checks`: wymiar, metoda, prog, severity, owner i evidence kind;
- `retention`: decyzja, source, legal hold, store coverage i deletion oracle;
- `privacy`: lawful basis, DPIA i transfer jako jawne decyzje lub pending;
- `migration`: applicability, recovery i validation refs;
- `gates`: moment, applicability, mandatory i reviewer policy.

Gate zawiera tez `max_age_seconds`, `invalidate_on` i `evidence_subjects`.
Dowod closeout ma hash samego pliku oraz `subject_sha256`, ktory przypina go do
konkretnego schema/dataset/restore/access state. Sam opis `PASS` nie wystarcza.

## Ewolucja

Nowy snapshot moze wskazac `previous_contract` z ID, hashem, relacja
`supersedes` i change summary. Stary READY pozostaje weryfikowalny przez runtime
obslugujacy jego `contract_schema_version`; nie przepisuj ani nie re-sealuj go
tylko dlatego, ze pojawila sie nowsza wersja skilla.

## Zakazane tresci

Nie zapisuj sample records, rows, raw data, adresow osob z produkcji, cookies,
storage state, credentials, tokenow, hasel, private keys ani wartosci sekretow.
Schema i nazwy pol sa dozwolone; wartosci rekordow nie sa potrzebne.

## Compatibility

- `backward`: nowy producent nie psuje obecnych konsumentow;
- `forward`: nowy konsument toleruje stary payload;
- `full`: oba kierunki;
- `breaking`: jawna migracja konsumentow, okno kompatybilnosci i cutover gate.

Opis `breaking` nie daje zgody na usuniecie pola lub danych.
