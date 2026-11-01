# HISTORICAL ONLY - legacy SKILL.md for data-governance

> Ten plik jest archiwum. Nie jest domyslnie ladowany przez `select-skills`
> ani przez aktywny SKILL.md skilla. Sluzy wylacznie do odczytu dawnych
> przebiegow data-governance (DRAFT/READY, seal, compile-graph-fragment,
> closeout, verify-receipt) oraz do rekonstrukcji historycznych contractow i
> fragmentow.
>
> Aktywna sciezka to TODO 1.0 opisana w `../SKILL.md`. Nie uruchamiaj
> `scripts/data_governance.py` dla nowego TODO, z wyjatkiem jawnego recovery
> historycznego runu (patrz nizej). Nie powtarzaj tresci tego pliku w nowych
> promptach ani w nowych taskach. Same bramki ochronne (owner, system of
> record, lineage, access, quality, recovery, privacy/retention, PROD consent,
> backup test) sa zachowane w aktywnym SKILL.md.
>
> Istniejacy helper `scripts/data_governance.py` zostaje w repo. Jego komendy
> (ponizej) sluza wylacznie recovery historycznych runow; nie sa domyslna
> bramka nowej pracy.
>
> Pierwsze wejscie dla historycznego runu (data-governance):
> 1. Zlokalizuj contract JSON, fragment JSON, classification i `run-graph-*`.
> 2. Sprawdz, ze pliki i receipt sa niezmienione (hash, sciezka, manifest).
> 3. Uruchom wylacznie `doctor`, `classify`, `validate`, `verify-graph-fragment`,
>    `verify-receipt` (read-only). Nie wywoluj `seal`/`compile-graph-fragment`/
>    `closeout` bez jawnego polecenia rodzica.
> 4. Nowa zmiana danych dla tej samej domeny wymaga nowego TODO z inline
>    wpisami, nie reaktywacji starego contract.

---

Ponizej znajduje sie dokladna, niezmieniona tresc poprzedniego aktywnego
SKILL.md (sprzed 2026-09-14 TODO 1.0). Zachowana w calosci, bez skrocen,
lacznie z pelnym podrecznikiem komend `scripts/data_governance.py`.

```markdown
---
name: data-governance
description: Zamraza i egzekwuje kontrakt governance dla trwalych danych, schematow, ETL/ELT, metryk, migracji, backfilli, danych osobowych/restricted, retencji i przeplywow cross-system. Uzyj przed mutacja danych lub publikacja, aby przypisac ownera/stewarda, purpose, klasyfikacje, lineage, access, quality/reconciliation, privacy decisions, recovery i niezalezny odbior; nie uzywaj jako klienta bazy, generatora SQL ani porady prawnej.
---
## Nowa sciezka TODO (2026-09-14)

Dla nowej pracy stosuj krotkie TODO i produktowy odbior opisany w globalnym
AGENTS.md oraz plan-delivery. Ponizsze wymagania dotyczace osobnych
kontraktow, init/approve, fidelity/sufficiency i formalnego closeoutu sluza
wylacznie odzyskiwaniu starych runow. Nie uruchamiaj ich dla nowego TODO.
Zachowaj merytoryczne kryteria tej specjalizacji jako taski/testy w tym samym
TODO: bez oslabiania ochrony danych, zgod produkcyjnych i rzeczywistych testow.



# Data Governance

Nie pozwol, by poprawny kod produkowal niekontrolowane dane. Skill tworzy
immutable kontrakt decyzji, kompiluje wymagane bramki do istniejacego Agent
Graph i odrzuca closeout bez aktualnych, niezaleznych dowodow. Nie wykonuje SQL,
migracji, skanow danych, modeli, Fleeta, Graphu ani polaczen sieciowych.

Przed pierwszym kontraktem przeczytaj
[contract-format.md](references/contract-format.md) i
[risk-and-gates.md](references/risk-and-gates.md). Przy migracji lub backfillu
przeczytaj [migration-and-backfill.md](references/migration-and-backfill.md).
Przy danych osobowych, retencji albo usuwaniu przeczytaj
[privacy-and-retention.md](references/privacy-and-retention.md). Pochodzenie
wzorcow i granice materialu zewnetrznego sa w
[sources-and-provenance.md](references/sources-and-provenance.md).

## Kiedy aktywowac

Uruchom przed pierwsza mutacja, gdy zadanie dotyka co najmniej jednego z:

- trwalego storage, schematu, API danych, ETL/ELT, cache, eksportu lub topicu;
- publikowanej metryki, raportu, feature albo danych bedacych source of truth;
- danych osobowych, poufnych, restricted, finansowych lub regulowanych;
- migracji, backfillu, replay, deduplikacji, merge albo masowego delete/update;
- przeplywu cross-system, zmiany dostepu, retencji, usuwania lub backupu.

Najpierw uruchom `classify` na jawnym change assessment. Brak sygnalu jest
bledem, nie `false`. `D0` dotyczy wylacznie danych syntetycznych/nietrwalych bez
publikacji i konsumentow; zapisz wynik `SKIP` i nie tworz kontraktu. `D1` uzywa
profilu `compact`. `D2-D3` uzywaja `full`. Dane osobowe, special-category albo
`restricted` zawsze wymuszaja `D3`.

## Przeplyw

1. Po orientacji projektu i odczycie `llm-wiki` zidentyfikuj aktywa, system of
   record, ownera, stewarda, konsumentow, purpose i zakazane uzycia. Unknown nie
   staje sie zalozeniem.
2. W `plan-delivery` dodaj materialne `REQ-*` dla data contract, lineage,
   access, quality, recovery, privacy/retention i odbioru. Skill governance nie
   zastapuje fidelity ani sufficiency review promptu.
3. Utworz `DRAFT` zgodny z reference. Kontrakt zawiera metadane i reguly, nigdy
   rekordy klientow, sample rows, sekrety, cookies, tokeny ani credentials.
4. Ustal klasyfikacje i purpose. Decyzje legal-DPO/ownera maja source, date
   weryfikacji i status. Brak decyzji pozostaje `PENDING` i generuje human gate;
   agent nie wybiera sam lawful basis, retencji, DPIA ani transfer mechanism.
5. Zaprojektuj kontrakt danych i compatibility, lineage wejsc-transformacji-
   wyjsc, least privilege, masking/minimisation oraz blokujace quality checks.
6. Dla migracji/backfillu zamroz checkpoint, idempotent replay, reprezentatywna
   probe, backup i restore test, reconciliation, rollback i roll-forward.
   Destrukcyjny krok lub PROD cutover nadal wymaga osobnej zgody Mateusza.
7. Zweryfikuj DRAFT i zapieczetuj `READY`. Hash jest liczony z kanonicznego JSON
   (UTF-8, sortowane klucze, bez whitespace). Dowodzi niezmiennosci deklaracji,
   nie prawdziwosci stanu bazy. Po `seal` nie nadpisuj snapshotu; nastepca ma
   `previous_contract.relation=supersedes`, poprzedni hash i change summary.
8. Skompiluj fragment. Parent mapuje `node_key` na swieze ID taskow/wezelow,
   zachowuje dependencies, `data:<asset-id>` resource locks, gate refs i oracles.
   Przed mutacja Graph nabywa wszystkie locki, wiaze swiezy pre-state evidence,
   ponownie sprawdza kontrakt i fragment, a dopiero potem uruchamia wykonawce.
   Agent Fleet dobiera wykonawcow; maker i reviewer pozostaja niezalezni.
9. `accept-system` musi zawierac oracles dla danych widocznych biznesowo:
   formula/metryka, source-to-target reconciliation, role access, deletion,
   cache/backup/export, consumer compatibility oraz side effects.
10. Po testach utworz closeout. Kazdy gate dostaje content-hashed evidence i
    reviewer lineage. `BLOCKED` oraz `NOT_ACCEPTED` sa prawidlowym wynikiem; nie
    wolno ich zamienic w sredni score ani pominac brakujacego gate'a.

## Polecenia

```powershell
python "<skill-dir>\scripts\data_governance.py" doctor
python "<skill-dir>\scripts\data_governance.py" classify --change <change.json>
python "<skill-dir>\scripts\data_governance.py" validate --contract <draft.json> --phase draft
python "<skill-dir>\scripts\data_governance.py" seal --contract <draft.json> --output <ready.json>
python "<skill-dir>\scripts\data_governance.py" compile-graph-fragment --contract <ready.json> --output <fragment.json>
python "<skill-dir>\scripts\data_governance.py" verify-graph-fragment --contract <ready.json> --fragment <fragment.json>
python "<skill-dir>\scripts\data_governance.py" closeout --contract <ready.json> --evidence <closeout.json> --output <receipt.json>
python "<skill-dir>\scripts\data_governance.py" verify-receipt --contract <ready.json> --receipt <receipt.json>
```

## Twarde granice

- Kontrakt governance nie nadaje authority do PROD, destrukcyjnej migracji,
  odczytu danych klienta, zmiany IAM, sekretow ani publicznej integracji.
- Nie eksportuj danych restricted do modeli. Do planowania wystarcza schema,
  klasyfikacja, metryki zagregowane i zredagowane dowody.
- Nie uznawaj backupu za recovery bez restore testu. Nie uznawaj row count za
  pelna reconciliation bez kluczowych agregatow, constraints i konsumentow.
- Down migration nie jest automatycznie bezpiecznym rollbackiem. Utrata danych
  czesto wymaga roll-forward, checkpointu albo restore.
- RLS, RBAC, masking i polityka w dokumencie nie sa dowodem egzekwowania.
  Potrzebny jest test roli i odmowy dostepu.
- Screenshot dashboardu nie dowodzi poprawnosci metryki. Oracle wskazuje
  formule, source, jednostke, okres, strefe czasu i tolerancje.
- Prawo i standardy sa zmienne. High-stakes decyzje odswiezaj w zrodle
  pierwotnym i eskaluj do legal/DPO; skill nie udziela porady prawnej.
- `ACCEPTED` oznacza integralnosc manifestu dowodow i niezalezny review. Nie
  oznacza, ze helper sam zobaczyl DB. Stan live dowodza zewnetrzne testy, query,
  restore/UAT i ich subject hash.

## Raport

Podaj: tier i triggers, aktywa oraz owner/steward, system of record i lineage,
purpose/classification, kontrakt i compatibility, access, quality/reconciliation,
privacy/retention decisions, plan migration/recovery, gate status, contract i
fragment hash, receipt oraz nierozwiazane decyzje lub blokery.
```
