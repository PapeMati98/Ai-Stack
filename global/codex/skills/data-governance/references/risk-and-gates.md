# Tier i bramki

## Tier

| Tier | Zakres | Minimalny proces |
|---|---|---|
| `D0` | syntetyczne/nietrwale dane robocze, bez publikacji i konsumentow | wynik klasyfikatora `SKIP`, inline anti-trigger |
| `D1` | trwale dane wewnetrzne, additive-compatible, bez nowego celu/konsumenta/PII | profil compact: owner/steward, purpose, contract, lineage, retention, quality |
| `D2` | shared/cross-system, metryka biznesowa, schema lub ETL | pelny lineage, consumers, access, reconciliation |
| `D3` | personal/restricted/regulowane albo materialny cutover | privacy/legal decisions, niezalezny security i owner gates |

Tier jest dolnym ograniczeniem, nie wynikiem negocjacji. `personal_data`,
`special_category_data` lub `restricted` wymusza D3.

Klasyfikator przyjmuje wszystkie sygnaly jako jawne booleany. `D2` aktywuja
m.in. nowy konsument, breaking schema, published metric, cross-system, access,
migration/backfill i retention/delete. Taki sam sygnal na PROD podnosi D3.
Unknown lub brak pola zatrzymuje klasyfikacje. Nie ma semantycznego fallbacku.

## Gate'y

- `classification-purpose`: przed pozyskaniem lub rozszerzeniem uzycia;
- `data-contract`: przed mutacja producenta lub storage;
- `privacy`: przed publish dla danych osobowych/restricted;
- `access-security`: przed udostepnieniem nowej roli lub systemu;
- `quality-reconciliation`: przed close i po kazdej generacji naprawy;
- `migration-readiness`: przed cutover;
- `retention-deletion`: przed automatyzacja usuwania;
- `data-acceptance`: finalny receipt dla materialnej zmiany.

`NOT_APPLICABLE` wymaga rationale zamrozonego w kontrakcie. Reviewer nie moze
zmienic applicability podczas closeout. Maker i reviewer maja rozne ID i
lineage. Dokument polityki nie jest dowodem enforcementu.

## Freshness i invalidation

Kontrakt zapisuje czas obserwacji zrodel. Przed decyzja high-stakes odswiez
prawo, policy, role, schema i stan live. Stary kontrakt moze byc poprawny
historycznie i niepoprawny operacyjnie; nowe uruchomienie wymaga aktualnego
evidence binding.

Kazdy gate ma TTL oraz liste zdarzen uniewazniajacych, np. schema, purpose,
owner, source-state, code revision, backup lub legal source. Po takim zdarzeniu
stary PASS nie przechodzi do nowego runu. Content hash zachowuje dowod; nie
przedluza jego waznosci.
