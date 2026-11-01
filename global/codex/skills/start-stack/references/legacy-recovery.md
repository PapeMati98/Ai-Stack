# HISTORICAL ONLY - legacy SKILL.md for start-stack

> Ten plik jest archiwum. Nie jest domyslnie ladowany przez `select-skills`
> ani przez aktywny SKILL.md skilla. Sluzy wylacznie do odczytu dawnych
> przebiegow wieloagentowego stacku kontraktowego (model stewardow,
> fidelity/sufficiency, planner-witness, formalny closeout), ktory wymuszal
> osobny manifest grafu i ceremonialny approval.
>
> Aktywna sciezka to TODO 1.0 opisana w `../SKILL.md`. Nie uruchamiaj ponizszych
> krokow dla nowego TODO. Nie powtarzaj ich tresci w nowych promptach ani w
> nowych taskach. Same bramki ochronne (no-delete, production consent, privacy,
> backup, mandatory skill selection, Office, session closeout, intent guard)
> sa zachowane w aktywnym SKILL.md.
>
> Istniejace helpery i pliki runow (np. plan_contract.py, run-graph-*) zostaja
> w repo dla odczytu historycznych receiptow. Nie uruchamiaj ich dla nowej
> pracy.
>
> Pierwsze wejscie dla historycznego runu:
> 1. Zidentyfikuj run_id, scope, model stewardow i powiazany planning-contract
>    lub run-graph-* z oryginalnego JSON-a albo z manifestu.
> 2. Sprawdz, ze plik / receipt / manifest sa niezmienione (hash i sciezka).
> 3. Uruchom wlasciwy helper tylko w trybie read-only/validate.
> 4. Eskalacja lub naprawa historycznego runu idzie do rodzica; nie tworz
>    nowego planu wieloagentowego na podstawie tego pliku.

---

Ponizej znajduje sie dokladna, niezmieniona tresc poprzedniego aktywnego
SKILL.md (sprzed 2026-09-14 TODO 1.0). Zachowana w calosci, bez skrocen.

```markdown
---
name: start-stack
description: "Tryb wieloagentowy / orchestrator dla powazniejszej pracy nad projektem. Uzyj na starcie zadan produkcyjnych, ML lub tradingowych: orientacja wg AGENTS.md i ACTIVE_MANIFEST, ustalenie realnego celu (straznik intencji), role stewardow, twarde reguly bezpieczenstwa, format handoff. Najwyzszy prog rzetelnosci."
---
## Nowa sciezka TODO (2026-09-14)

Dla nowej pracy stosuj krotkie TODO i produktowy odbior opisany w globalnym
AGENTS.md oraz plan-delivery. Ponizsze wymagania dotyczace osobnych
kontraktow, init/approve, fidelity/sufficiency i formalnego closeoutu sluza
wylacznie odzyskiwaniu starych runow. Nie uruchamiaj ich dla nowego TODO.
Zachowaj merytoryczne kryteria tej specjalizacji jako taski/testy w tym samym
TODO: bez oslabiania ochrony danych, zgod produkcyjnych i rzeczywistych testow.



Tryb: wieloagentowy stack (jestes orchestratorem). Najwyzszy prog rzetelnosci.

KROK 0 - Dynamiczny dobor skilli:
- Uruchom `select-skills` na podstawie aktualnie dostepnego inventory i celu zadania.
- Wybierz minimalny zestaw specjalistow; polityki globalne i twarde bramki nadal obowiazuja.
- Powtorz dobor, gdy zakres, domena, ryzyko albo oczekiwany sposob odbioru istotnie sie zmieni.

KROK 1 - Orientacja (nie zgaduj):
- Przeczytaj AGENTS.md tego projektu i WSZYSTKIE pliki z jego sekcji "Required First Read"
  (np. PROJECT_FOLDER_POLICY, PROJECT_INDEX, 00_project_control/ACTIVE_MANIFEST.json,
  SOURCE_OF_TRUTH, HARDWARE_PROFILE, COMPUTE_POLICY, KNOWN_ISSUES, DECISION_LOG) - jesli istnieja.
- Zasada nadrzedna: zaden artefakt nie jest "aktualny" dlatego, ze nazwa tak wyglada. Decyduje ACTIVE_MANIFEST.

KROK 2 - Cel (straznik intencji), ZANIM cokolwiek zrobisz:
- W 1-2 zdaniach: jaki REALNY problem rozwiazujemy i po czym poznamy sukces (mierzalnie). Co jest POZA zakresem.
- Dla modeli predykcyjnych/tradingowych: czy metryka = realny cel, czy proxy latwe do oszukania?
  Backtest alpha != realny PnL. Pilnuj: lookahead/leakage, dostepnosc cechy w momencie decyzji,
  realistyczne koszty/poslizg, out-of-sample, walk-forward, zmiana rezimu, ogony (nie tylko srednia).
- Dla strategii rynkowych uruchom `market-strategy-research`; pozostan w
  research-only i nie traktuj dobrego backtestu jako zgody na handel lub promocje.
- Jesli cel jest niejasny lub sprzeczny - ZATRZYMAJ i zapytaj Mateusza.

KROK 3 - Role:
- Uzyj rol z AGENTS.md projektu (np. model stewardow). Jeden agent moze grac kilka rol,
  ale w raporcie JAWNIE oznacz, ktora rola mowi. Wybierz tylko role istotne dla zadania.

KROK 4 - Wykonanie (przejdz role po kolei, kazda osobno):
- Data/Feature: dostepnosc cech w momencie decyzji, leakage/lookahead, lineage, braki, reprezentatywnosc.
- Architecture: czy podejscie pasuje do problemu; baseline; bez przekombinowania.
- Compute/Training: GPU wg HARDWARE_PROFILE, seedy, wznawialnosc; binaria/checkpointy POZA firmowym OneDrive.
- Evaluation: uczciwy benchmark (OOS, koszty, rezim), porownanie do baseline, realne vs proxy.
- Production Gatekeeper: promocja tylko przez manifest/checklist; wersjonowany kandydat - nie nadpisuj biezacego.
- Security: sekrety/.env - nie wystawiaj; trzymaj poza OneDrive jesli mozliwe.

KROK 5 - Bezpieczenstwo (TWARDE):
- Bez jawnej zgody Mateusza: nie deployuj, nie kasuj danych, nie ruszaj zaplanowanych jobow,
  paper-tradingu ani produkcji, nie nadpisuj aktywnych artefaktow.

KROK 6 - Output (format handoff z AGENTS.md):
Task / Current status / Last successful artifact / Last command / Open risks / Next safe action / Files changed / Files not to trust.

Zacznij od KROKU 1. Po orientacji podaj cel (KROK 2) i poczekaj na potwierdzenie, jesli cokolwiek jest niejasne.
```
