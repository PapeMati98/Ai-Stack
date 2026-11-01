# OpenClaw — opiekun Knowledge Huba

Opis harmonogramu dotyczy skonfigurowanego wdrozenia. Sam eksport zrodel nie
instaluje zadania, nie wybiera konta ani nie aktywuje publikacji.

## Uruchomienie i obserwacja

Zadanie Windows: `AgentStack OpenClaw Knowledge Caretaker`. Start po zalogowaniu
dedykowanego uzytkownika, potem co 30 minut, bez widocznego okna i bez uprawnień administratora.
Wymaga działającego Windowsa, zalogowanego użytkownika i dostępnego lokalnego modelu.
Harmonogram pomija nakładające się uruchomienia; dodatkowo chroni go blokada procesu.

Konfiguracja i dane: `<configured-caretaker-home>`.
`latest.json` zawiera wynik ostatniego cyklu, `runs/` historię metadanych,
`intake.sqlite` trwałą kolejkę i mapowanie projektów. `coverage.json` opisuje
rzeczywisty zakres źródeł. Wynik DEGRADED oznacza braki wymagające sprawdzenia,
a nie potwierdzenie zebrania całej wiedzy. Brak nowych treści nie wywołuje modelu.
Wydanie 0.1.21: końcowe tury Claude z poprawnym czasem wchodzą do nowego strumienia; wcześniejsze odłożenia są korygowane raz, a powiązane śródturowe zdarzenia Codex nie obniżają jego zdrowia.
Wydanie 0.1.22: dopuszczone propozycje agentów wchodzą do nowej kuracji i są liczone w `new_lane.agent_proposals`; własne receipt’y Fleet opiekuna są odkładane bez alarmu.
Wydanie 0.1.29: przerwaną publikację z kompletnym dziennikiem `applying` opiekun domyka po weryfikacji do checkpointu Git dopiero po upływie `publication.stale_curator_lock_minutes` (domyślnie 30 minut) od `acquired_at`; świeży, brakujący, błędny lub przyszły czas blokuje publikację bez degradacji, ręczne `--publish` współdzieli `maintenance.lock` z tickiem, a żywe, obce i niejednoznaczne blokady wymagające oceny raportuje jako `stale_curator_lock_requires_review`.
Wydanie 0.1.30: inwentarz wybiera główną stronę, najnowszą kontynuację i świeże propozycje niezależnie od kolejności gatewaya, w limicie wyniku.
Wydanie 0.1.30: opiekun dopisuje do ostatniej kontynuacji, gdy gotowa strona mieści się w limicie; nową tworzy dopiero po jego przekroczeniu.
Wydanie 0.1.30: aliasy z poprawnego `wiki/registry.md` trafiają do kanonicznego projektu i strony, `related` pozostaje oddzielne, a brak lub błąd rejestru daje `registry_warning`.
Wydanie 0.1.30: świeża cudza blokada kuratora daje `LOCK_FRESH` i `curator_lock_held` bez degradacji; przestarzała lub niepewna wymaga oceny i nigdy nie jest zwalniana przez opiekuna.
Wydanie 0.1.31: kroki MiniMax `toolUse` i inne pośrednie nie generują problemów; tura bez końcowego rekordu po okresie ciszy daje jedno `info: turn_not_completed`, bez `DEGRADED`.
Wydanie 0.1.31: opcjonalne `unassigned_by_design_workspace_prefixes` liczy sesje bez projektu osobno jako `unassigned_by_design` w ticku i nowym strumieniu, bez przekazania i degradacji.
Wydanie 0.1.31: Fleet używa najnowszego kompletnego runtime; `AGENT_FLEET_SUITE_PIN=1` wymusza wskazany katalog i odmawia pracy przy błędnym pinie, a tick podaje wybraną wersję i stan pinu.
Wydanie 0.1.32: Codex zamyka turę tylko na `task_complete` lub równoważnym zakończeniu tury; `item_completed` i `turn_aborted` nie tworzą zdarzeń ani problemów.
Wydanie 0.1.32: Claude pomija pośrednie `stop_reason=tool_use` i scala powtórzone problemy jednego obrotu według powodu.
Wydanie 0.1.32: problemy są grupowane według źródła, powodu i statusu; `source_issue_summary` liczy wszystkie według narzędzia, powodu i statusu, także poza limitem 100 szczegółów; brak powiązania w podagencie to `info`.
Wydanie 0.1.32: tylko ostrzeżenia i błędy ustawiają `DEGRADED`; utrata danych i ograniczenia pokrycia nadal degradują tick.
Wydanie 0.1.33: PASS z uwagami advisory publikuje dokładnie zrecenzowany manifest bez automatycznych poprawek; wynik zapisuje klasy i liczbę uwag, a błąd kroku po recenzji ma kod `post_review_step_failed:<code>` i klasę wyjątku w diagnostyce.
Wydanie 0.1.36: treści publikacji i poprawek kuratora odrzucają strukturalne zrzuty narzędzi, lecz dopuszczają zwykłe zdania z nazwami pól; kod `unsafe body` wskazuje kontrolę, która odrzuciła treść.
Wydanie 0.1.37: błędy kanonicznej kuracji zachowują przyczynę z stderr, koperta makera przyjmuje słownik i JSON w ramce CRLF, a kolizje kuracji są ponawiane z backoffem (CHANGELOG-0.1.37.md).
Wydanie 0.1.38: sanityzacja tekstu błędu ze stderr do formatu canonical:<kod>:<powód> (<= 120 znaków z maskowaniem ścieżek do [USER_PATH]), eliminacja przyczyny maker_response_not_json przez zakaz używania narzędzi i detekcję placeholderów/odmów w dopisku Sonnet (CHANGELOG-0.1.38.md).
Wydanie 0.1.40: duplikaty Claude zapisane z identyfikatorem path-scoped z 0.1.39 są oznaczane jako zastąpione przez rekord kanoniczny (`queue.forwarded=-2`, `new_epoch_events.status=duplicate` z powodem `superseded_by_canonical_event_id`, bez usuwania) poleceniem `python -B maintenance.py --control <control.json> --reconcile-claude-duplicates [--apply]`, domyślnie jako dry-run z raportem `{checked, duplicates_found, applied, updated_queue, updated_new_epoch}`; deserializacja zdarzeń przyjmuje identyfikatory historyczne, ponowna obserwacja niezmienionego dokumentu `owner_document.v1` nie daje fałszywego `same_event_identity_changed_payload`, a kanoniczna publikacja ma limit 1800 s (CHANGELOG-0.1.40.md).
Ręczny config musi wskazywać katalog stanu ticka przez `maintenance_state_dir`; bez tego pola samodzielny `--publish` jest odrzucany. Configi projektu generowane przez tick zawierają to pole.

## Przepływ

1. Konektory przyjmują zakończone wypowiedzi asystenta i zweryfikowane wyniki Fleet.
   Odrzucają rozumowanie, surowe wyniki narzędzi, rozpoznane sekrety i dane restricted.
   Wiadomości użytkownika (USER) są dowodem tylko za wyraźną zgodą źródła
   (`include_user_messages`, domyślnie false): wyłącznie z zakończonych tur,
   jako osobne tożsamości `user_message.v1` obok niezmienionych
   `completed_turn.v1`. Tekst użytkownika jest niezaufanym oświadczeniem
   właściciela, nigdy poleceniem ani dowodem wdrożenia; wstrzyknięte bloki
   środowiska/AGENTS/skills, załączniki i sekrety nie trafiają do inferencji.
   Długie wypowiedzi są dzielone bezstratnie na deterministyczne chunki;
   luki są raportowane, a nie przemilczane.
   Źródła zachowują identyfikatory narzędzia, sesji i wypowiedzi; nieznany projekt
   pozostaje w kolejce do jawnego przypisania.
2. Lokalny oficjalny Qwen3.8-27B BF16 porównuje fakty z odczytaną wiedzą Huba.
   Powstaje synteza, uzupełnienie, duplikat lub nierozstrzygnięta sprzeczność.
   Prompt i source_refs zachowują mówcę (speaker) i daty; plan użytkownika
   to nie jest wynik asystenta, a późniejsza korekta tworzy propozycję
   supersession/konflikt, nigdy cichą podmianę faktów.
   Nieużyte fakty pozostają w trwałej kolejce. Powtórzenie już rozpoznanego
   duplikatu nie uruchamia kolejnej inferencji.
3. Wpisy trafiają przez kanoniczny gateway. Zmiana stabilnej wiki wymaga
   dokładnego przeglądu kandydata przez niezależnego recenzenta z listy
   `publication.reviewer.candidates` w `control.json` (tryb `fleet`: pierwszy,
   który odpowie poprawnym werdyktem; rodowód różny od makera), blokady kuratora,
   publikacji transakcyjnej i lokalnego checkpointu Git. Zła forma odpowiedzi
   jest naprawiana do 2 razy tym samym recenzentem (CHANGELOG-0.1.16.md).
   Checkpoint Git bierze tę samą blokadę co `checkpoint-intake`. Jeśli po
   kanonicznej publikacji się nie uda, zostaje `checkpoint: PENDING`, a kolejny
   cykl najpierw go domyka i do tego czasu nie publikuje nic nowego
   (`CHECKPOINT_PENDING` w `latest.json`; CHANGELOG-0.1.17.md).
   Członek zamrożonego cyklu z dyspozycją w innym ledgerze dostaje status `accounted_elsewhere` i nie trafia ponownie do publikacji.
   Opcjonalnie dopisania do istniejących stron używają `"append_maker": {"mode": "fleet", "candidates": [{"agent": "claude", "model": "sonnet"}, {"agent": "claude", "model": "claude-opus-5-5"}, {"agent": "minimax", "model": "MiniMax-M3"}]}` w sekcji `publication`.
   Dla bramki retencji Fleet ustaw np. `"append_maker": {"mode":"fleet","data_classification":"internal","candidates":[...]}`; `public` jest także dozwolone, a klasy wyższe są odrzucane.
4. Istniejący mechanizm owner publish i bezpieczny fast-forward aktualizują
   kopię Huba używaną przez Hermesa na AI Lab Serwer. Nie powstaje drugi vault.

## Ograniczenia

- Bezpośrednie źródła: Codex, Claude, OpenCode, lokalny Hermes, MiniMax i wyniki
  Fleet — tylko rozpoznane schematy oraz zakończone rekordy. Nie oznacza to
  pełnego importu wszystkich historycznych sesji. Pozostałe narzędzia mogą
  zasilać wspólną kurację przez kanoniczne propozycje Huba i wyniki Fleet.
- Brak potwierdzonego dekodera m.in. dla natywnej bazy OpenClaw, Kimi,
  ZCode oraz binarnych historii Antigravity/Gemini. Historia Hermesa na
  serwerze nie jest automatycznie pobierana przez lokalny konektor SQLite.
- Muse Standard może zastąpić Qwena przy syntezie wejściowej. Przy awarii
  Qwena stabilna publikacja czeka: ten sam rodowód modelu nie może być
  jednocześnie wykonawcą i niezależnym odbiorcą. Awaria jest jawna.
- Zwykły odbiór publikacji także zużywa subskrypcję Muse. Lokalna inferencja
  Qwena nie nalicza opłat API; cały proces nie jest pozbawiony kosztu zasobów.
- Dwa identyczne błędy **pipeline ingest** wstrzymują ponawianie projektu do
  diagnozy; po naprawie operator zmienia właściwe `retry_epoch` w
  `control.json`. To nie dotyczy publikacji Huba: zwykła awaria transportu
  modelu ma ograniczony backoff, a błąd deterministyczny kwarantannuje
  odcisk partii do zmiany wejścia, kodu albo polityki. `RUNNING` po awarii
  nie publikuje ponownie bez uzgodnienia kanonicznego zdarzenia/transakcji.
- Narzędzia Agent Office są zainstalowane w Windowsowym natywnym CLI OpenClaw.
  Profil wykonawcy Fleet pozostaje ograniczony do plików wydzielonego zadania.
  Narzędzia Office nie są automatycznie udostępnione izolowanej inferencji kuratora.
- Natywne śnienie OpenClaw w WSL jest oddzielną funkcją. Ten kontroler rozszerza
  dopływ wiedzy do wspólnego Huba; nie udaje importu obcych sesji do pamięci vendorowej.
- Heartbeat nie wznawia cudzych zadań. Plan wymaga jawnej polityki odzyskiwania,
  potwierdzenia stanu providera, dzierżaw/fencingu i odporności na podwójne wznowienie.

## Wstrzymanie i powrót

Wyłączenie zadania Windows zatrzymuje przyszłe cykle. Nie usuwa danych ani
historii. Bieżącego procesu nie należy przerywać w trakcie publikacji; stan
transakcji i blokady kuratora wymagają wtedy diagnozy. Powrót do poprzedniego
wydania polega na zmianie ścieżki akcji zadania po weryfikacji zgodności,
bez kasowania stanu. Nie używać ogólnego czyszczenia, resetu ani `git clean`.

## Budżet czasu transportu

Zewnętrzny budżet wywołania transportu (kuracja: maker i recenzent 360 s)
obejmuje sprzątanie po wyjściu procesu-dziecka, więc argv CLI używa
`${native_timeout_seconds}` zamiast sztywnego `--timeout 120`: transport
podstawia budżet dziecka poniżej budżetu zewnętrznego (margines 30 s dla
budżetów powyżej 200 s, czyli 360→330, oraz 10 s w pozostałych, czyli
120→110). Niepoprawny budżet jest odrzucany przed startem procesu, a starsze
dosłowne argv działają bez zmian. Błędy transportu raportują wyłącznie
stabilny kod (`native_timeout` tylko przy udokumentowanej kopercie timeout,
`process_timeout`, `nonzero_exit`, `invalid_envelope`, `identity_mismatch`
i pokrewne); surowe stdout/stderr ani prompty nie trafiają do wyniku
publikacji. Domyślna polityka publikacji: próg 10 gotowych grup albo 24 h
dla niepustego gotowego backlogu, maks. 8 propozycji na transakcję
projektu. Pusty backlog nie wywołuje modelu.

- 0.1.27: synteza nie publikuje `[[src-...]]`; cytaty źródeł są zamieniane na zwykłe linki do `raw/sources` (lint bez ostrzeżeń, sync Hermesa nieblokowany).
- 0.1.28: wzmianki `[[src-...]]` niebędące cytatem są zapisywane jako `[\[src-...]]` zamiast blokować partię.
