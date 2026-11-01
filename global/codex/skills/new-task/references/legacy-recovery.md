# HISTORICAL ONLY - legacy SKILL.md for new-task

> Ten plik jest archiwum. Nie jest domyslnie ladowany przez `select-skills`
> ani przez aktywny SKILL.md skilla. Sluzy wylacznie do odczytu dawnych
> kontraktow, runow i historycznych przebiegow `plan-delivery` / Agent
> Graph / Agent Fleet, ktore wymagaly osobnego contract-ceremony.
>
> Aktywna sciezka to TODO 1.0 opisana w `../SKILL.md`. Nie uruchamiaj ponizszych
> krokow dla nowego TODO. Nie powtarzaj ich tresci w nowych promptach ani w
> nowych taskach. Same bramki ochronne (no-delete, production consent, privacy,
> backup, mandatory skill selection, Office, session closeout) sa zachowane w
> aktywnym SKILL.md.
>
> Istniejace narzedzia i helpery (np. `acceptance.py`, `data_governance.py`,
> `plan_contract.py`) pozostaja w repo dla odczytu historycznych receiptow.
> Nie uruchamiaj ich dla nowej pracy.
>
> Pierwsze wejscie dla historycznego runu:
> 1. Zidentyfikuj run_id, scope, model i binding z oryginalnego JSON-a
>    (planning-contract, acceptance-contract, run-graph-*) lub z manifestu.
> 2. Sprawdz, ze plik / receipt / dowody sa niezmienione (hash i sciezka).
> 3. Uruchom wlasciwy helper tylko w trybie read-only/validate, bez nowego
>    init/approve/closeout.
> 4. Eskalacja lub naprawa historycznego runu idzie do rodzica; nie tworz
>    nowego planu na podstawie tego pliku.

---

Ponizej znajduje sie dokladna, niezmieniona tresc poprzedniego aktywnego
SKILL.md (sprzed 2026-09-14 TODO 1.0). Zachowana w calosci, bez skrocen.

```markdown
---
name: new-task
description: Uzyj na poczatku KAZDEGO wiekszego zadania w aplikacji - orientuje projekt, dobiera skille i przekazuje zadanie M/L do plan-delivery, aby utrwalic wymagania, role, ryzyka, TODO i kryteria akceptacji ZANIM agent dotknie kodu.
---
## Nowa sciezka TODO (2026-09-14)

Dla nowej pracy stosuj krotkie TODO i produktowy odbior opisany w globalnym
AGENTS.md oraz plan-delivery. Ponizsze wymagania dotyczace osobnych
kontraktow, init/approve, fidelity/sufficiency i formalnego closeoutu sluza
wylacznie odzyskiwaniu starych runow. Nie uruchamiaj ich dla nowego TODO.
Zachowaj merytoryczne kryteria tej specjalizacji jako taski/testy w tym samym
TODO: bez oslabiania ochrony danych, zgod produkcyjnych i rzeczywistych testow.



Zaczynamy nowe zadanie w tej aplikacji. Najpierw przeczytaj AGENTS.md i
APP_PROFILE.md projektu. Nie zgaduj technologii ani stanu wdrozenia.

Najpierw uruchom `select-skills`: z aktualnego inventory wybierz minimalny zestaw
0-4 skilli specjalistycznych i przypisz im role governance, domain, execution lub QA.
Nie opieraj wyboru na stalej liscie nazw i nie szukaj skilla w sieci, dopoki lokalny
inventory nie potwierdzi luki.

Sklasyfikuj prace:

- `direct`: ponizej ok. 10 minut, jeden lokalny i odwracalny scope, bez
  delegacji, rownoleglosci, niezaleznego gate'u i hard-risk. Zapisz inline cel,
  zakazy, kryterium odbioru i scope, a potem wykonaj.
- `M/L`: uruchom `plan-delivery`. Przed jego approval wolno tylko czytac
  i wykonac bounded discovery potrzebne do decision-complete TODO. Nie uruchamiaj
  mutujacego Fleet/Loop/Graph i nie zmieniaj kodu docelowego.

Kontrakt M/L ma objac:

Zadanie i zamierzony wynik:
Requirement ledger wraz z negatywami i out-of-scope:
Otwarte pytania, decyzje i odwracalne zalozenia:
Aktywne role i skille z powodem:
Taski, zaleznosci, fale i bezpieczna rownoleglosc:
Wykonawca, niezalezny reviewer, scope i budzet kazdego tasku:
Kryteria akceptacji i wymagany evidence:
Triggery Agent Loop, Rady Modeli i decyzji Mateusza:
Ryzyka / produkcja / auth / dane / zewnetrzne mutacje:

Fidelity auditor sprawdza TODO przeciwko oryginalnemu promptowi. Sufficiency
auditor sprawdza, czy lista wystarcza do dowiezienia i odbioru calosci. Dopiero
po obu PASS zatwierdz immutable snapshot i skompiluj Agent Graph.

Dla aplikacji lub systemu przed pierwsza mutacja uruchom `accept-system`:
utworz kontrakt `READY`, ktory mapuje kazdy materialny requirement na jawny
business oracle, user journey albo side-effect safety. Zamroz role, srodowisko,
syntetyczne dane/odbiorcow, freshness i niezaleznosc. DEV/TEST nie wysyla
realnych maili, platnosci, CRM ani kalendarza. Agent Graph inicjalizuj dopiero z
`--planning-contract` oraz `--acceptance-contract`.

Po implementacji nie koncz na unit/API/build. Wykonaj metodyczny odbior,
naprawiaj FAIL w ograniczonej petli i po kazdej naprawie powtorz WSZYSTKIE
stosowalne kryteria. Zadanie systemowe jest zakonczone dopiero z finalnym
receipt `ACCEPTED` albo raportowane uczciwie jako `NOT_ACCEPTED`/BLOCKED.

Matryca rol:
- UI               -> ui-ux, product-business, qa
- Logika biznesowa -> product-business, backend-api, qa
- API/integracje   -> backend-api, security, qa
- Dane/migracje    -> data-storage, security, qa
- Deploy/prod      -> devops-deploy, security, qa
Zawsze: produkcja -> security+devops+qa; dane klienta -> data-storage+security.
```
