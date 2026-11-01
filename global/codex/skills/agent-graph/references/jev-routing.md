# JEV: tani doradca wyboru, nie drugi scheduler

Uruchamiaj JEV, gdy trzeba porownac wykonawcow dla rzeczywistego TODO,
podzielic wieksza prace lub rozstrzygnac sens konsultacji. Nie wywoluj API
dla powitania, statusu, prostej odpowiedzi ani zadania z oczywistym wykonawca.
JEV zwraca wartosci typowane, nie uzasadnienie tekstowe ani plan kodowania.

1. Rodzic tworzy minimalny opis wedlug `jev-task-example.json`: cel, domena,
   wymagane narzedzia, ryzyko, szacowany kontekst i granice pracy. Bez kodu,
   danych klientow, sekretow, absolutnych sciezek ani pelnej rozmowy.
2. `routing_advisor.py snapshot` pobiera profile i aktualne dane quota puli
   kandydatow. Bez `--agents` (albo z `--agents all`) pula to kazdy profil z
   `routing_enabled`; reczny podzbior jest dozwolony, ale kazdy pominiety
   wlaczony profil trafia do `local_diagnostics` jako `NOT_SELECTED_BY_PARENT`
   (tylko lokalnie, nie jest wysylany do JEV). Nieznana quota nie wyklucza
   profilu. `--check-readiness` dodatkowo sprawdza wybrane CLI.
   Brakujace okno kontekstu Codexa moze uzupelnic natywny `models_cache.json`
   nie starszy niz 24 h: tylko domyslne okno po ograniczeniu effective percent,
   nigdy maksymalne okno wymagajace opt-in. Brak lub sprzecznosc danych nadal
   oznacza unknown; tozsamosc konta i pozostala tresc cache zostaja lokalne.
   Bez tej flagi gotowosc narzedzia pozostaje niezweryfikowana. Deklaracja
   `jev-funding.json` opisuje kanal rozliczenia, nie dostepny limit.
3. `jev.ps1` wykonuje jeden request z jawnym `MaxCostUsd`. Klucz z istniejacego
   Process/User albo wskazanego pliku referencji DPAPI pozostaje w pamieci
   procesu. Brak retry, fallbacku modelu i automatycznego uruchamiania agentow.
   Lokalny `~/.agents/jev-router/config.json` moze wskazywac istniejacy plik
   referencji przez `secret_reference_file`; nie zawiera wartosci klucza.
4. Rodzic czyta decyzje wraz z deterministycznym filtrowaniem. Niska pewnosc,
   duze ryzyko, brak recenzenta lub nieaktualny snapshot wymagaja oceny rodzica.
   Prog pewnosci 0.70 jest roboczy, nie skalibrowany na naszych zadaniach.
5. Porade przeznaczona do TODO utworz z `--todo` i `--task-id` (wrapper:
   `-TodoFile`, `-TaskId`). Raport ma byc zwiazany z pelnym zminimalizowanym
   zadaniem, biezacym snapshotem i intencja wybranego wezla TODO. Przy
   `draft-todo` przekaz te same `--task` i `--snapshot`. Zmiana tresci, ryzyka,
   narzedzi, testow lub planu wymaga nowej oceny. Stary raport bez tego
   powiazania oraz raport po 300 sekundach nie sa planem do zastosowania.
   Sama zgodnosc hasha nie jest uwierzytelnieniem ani zgoda wlasciciela.
6. `draft-todo` tworzy NOWY plik z listami maker/reviewer. Nie zmienia celu,
   plikow, zgody, testow, zaleznosci ani max_parallel. Fleet ponownie sprawdza
   aktualna gotowosc, tozsamosc, quote, uprawnienia i rodowod modelu przy starcie.
   Przy niepewnej poradzie rodzic moze podac `--parent-review-note` z wlasnym
   uzasadnieniem wyboru. To akceptacja porady przez planiste, nie zgoda wlasciciela
   na efekty zadania. Notatka rodzica nie obchodzi niezgodnosci powiazania
   ani wygasniecia raportu. Rekomendacja jest ponownie liczona ze zwiazanych
   danych; reczne podmienienie listy wykonawcow w raporcie jest odrzucane.
7. Gdy nic istotnego sie nie zmienilo, `advise --reuse-report <raport>`
   (wrapper `-ReuseReport`) odtwarza swieza porade bez nowego API. Nie wymaga
   klucza, ale wymaga zgodnego zadania, snapshotu i TODO. Nie uzywaj ponownie
   wygaslej porady ani nie uznawaj nowych tokenow z cache za nowe zuzycie API.

```powershell
python <skill>/scripts/routing_advisor.py snapshot --task <minimized-task.json> --funding <skill>/references/jev-funding.json --workspace <checkout> --capacity 3 --check-readiness --output <snapshot.json>
& <skill>/scripts/jev.ps1 -TaskFile <minimized-task.json> -SnapshotFile <snapshot.json> -TodoFile <todo.json> -TaskId <id> -MaxCostUsd 0.004 -OutputFile <report.json>
python <skill>/scripts/routing_advisor.py draft-todo --todo <todo.json> --task-id <id> --task <minimized-task.json> --snapshot <snapshot.json> --report <report.json> --output <todo-routed.json>
```

Wykonanie wezla (`todo_fleet`) domyslnie wybiera wedlug klas modeli
(SKILL.md, sekcja "Tier Routing"): ocena planisty minus kara do 10 pkt za
ostatnie uzycie w tej samej klasie, oszczedzanie Claude/Codex ponizej Dyrektora
i klase wyzsza niz wymagana; DENY nigdy. Procent quoty nie jest rankingiem.
Tylko w trybie rollback (`AGENT_GRAPH_TIER_ROUTING=0`) dziala stary tie-break:
w pasmie 5 pkt wiekszy zmierzony zapas quota, pula bez pomiaru = neutralne 0,5.
Snapshot JEV niesie `tier`, `recent_share` i `conserve` z katalogu; awaria
katalogu daje wiersz `MODEL_CATALOG_UNAVAILABLE` w `local_diagnostics`.

Przy kolejnych niezaleznych zadaniach przekazuj `--assignments`: aktualnie zajete
modele, rodowody, zakresy i zasoby. Nie powtarzaj tego samego pustego snapshotu
dla kazdego wezla. Rodzic zna lokalna pojemnosc; liczba abonamentow nie jest
liczba bezpiecznych jednoczesnych procesow. Office dostarcza ustalenia i sygnaly
kolizji, a scheduler uruchamia zaleznosci po odbiorze, nie po samym ACK.

Awaria JEV nie blokuje pracy: zachowaj blad i przejdz do zwyklego wyboru TODO
przez rodzica. Nie wysylaj automatycznie zadania do innego platnego modelu.

## Koszt i dane

Model przypiety: `typesafe/jev-1.13`, endpoint Decisions API. Cena zweryfikowana
2026-09-25: 0.042 USD / mln tokenow wejscia, wyjscie 0 USD. Lokalny szacunek
przed wyslaniem jest konserwatywny; zmiana taryfy wymaga aktualizacji tej podstawy.
Raport zapisuje koszt zwrocony przez API; brak kosztu to unknown, nie zero.
Nie nazywaj szacunku gwarancja rozliczenia przez dostawce.

TypeSafe deklaruje brak trenowania na Input. Ustawienia konta OpenRouter nie
sa przez ten skrypt automatycznie potwierdzane. Uzywaj zminimalizowanych danych;
request zawiera `data_collection=deny`, jeden provider i brak fallbacku.
Odrzucenie tych parametrow nie uprawnia do ich usuniecia.

Zrodla pierwotne: https://openrouter.ai/docs/guides/community/jev-tutorial,
https://openrouter.ai/api/v1/models/typesafe/jev-1.13/endpoints,
https://typesafe.ai/legal/privacy-policy.
