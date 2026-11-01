# HISTORICAL ONLY - legacy SKILL.md for accept-system

> Ten plik jest archiwum. Nie jest domyslnie ladowany przez `select-skills`
> ani przez aktywny SKILL.md skilla. Sluzy wylacznie do odczytu dawnych
> przebiegow acceptance-contract (READY, init/approve, run-graph-*,
> evidence-only-rebind, formalny closeout) oraz do rekonstrukcji historycznych
> receiptow.
>
> Aktywna sciezka to TODO 1.0 opisana w `../SKILL.md`. Nie uruchamiaj
> `scripts/acceptance.py` dla nowego TODO, z wyjatkiem jawnego recovery
> historycznego runu (patrz nizej). Nie powtarzaj tresci tego pliku w nowych
> promptach ani w nowych taskach. Same bramki ochronne (no-delete, production
> consent, privacy, backup, mandatory skill selection, Office, session
> closeout, UI scenarios/roles/environment) sa zachowane w aktywnym SKILL.md.
>
> Istniejacy helper `scripts/acceptance.py` zostaje w repo. Jego komendy
> (ponizej) sluza wylacznie recovery historycznych runow; nie sa domyslna
> bramka nowej pracy.
>
> Pierwsze wejscie dla historycznego runu (acceptance):
> 1. Zlokalizuj acceptance JSON, planning-contract, graph JSON i `run-graph-*`.
> 2. Sprawdz, ze pliki i receipt sa niezmienione (hash, sciezka, manifest).
> 3. Uruchom wylacznie `doctor`, `validate`, `verify-receipt` (read-only).
>    Nie wywoluj `init`/`start`/`result`/`close`/`repair` ani
>    `evidence-only-rebind` bez jawnego polecenia rodzica.
> 4. Nowy odbior produktu dla tej samej funkcjonalnosci wymaga nowego TODO,
>    nie reaktywacji starego receipt.

---

Ponizej znajduje sie dokladna, niezmieniona tresc poprzedniego aktywnego
SKILL.md (sprzed 2026-09-14 TODO 1.0). Zachowana w calosci, bez skrocen,
lacznie z pelnym podrecznikiem komend `scripts/acceptance.py`.

```markdown
---
name: accept-system
description: Planuje i egzekwuje pelny odbior aplikacji lub systemu po implementacji - logika biznesowa, backend, dane, integracje, prawdziwe role UAT, UI/UX, osiagalnosc okien i powierzchni, widoczna reakcja na akcje, jawne bledy i odzyskanie, responsywnosc, accessibility, wydajnosc, niezawodnosc, security i bezpieczenstwo efektow zewnetrznych. Uzyj przy istotnej zmianie systemowej przed uznaniem zadania za zakonczone; nie uzywaj jako zamiennika jednostkowego testu podczas drobnej lokalnej poprawki.
---
## Nowa sciezka TODO (2026-09-14)

Dla nowej pracy stosuj krotkie TODO i produktowy odbior opisany w globalnym
AGENTS.md oraz plan-delivery. Ponizsze wymagania dotyczace osobnych
kontraktow, init/approve, fidelity/sufficiency i formalnego closeoutu sluza
wylacznie odzyskiwaniu starych runow. Nie uruchamiaj ich dla nowego TODO.
Zachowaj merytoryczne kryteria tej specjalizacji jako taski/testy w tym samym
TODO: bez oslabiania ochrony danych, zgod produkcyjnych i rzeczywistych testow.



# Odbierz system

Wersja 0.2.0 obsluguje dwa jawne typy user journey: `surface=graphical`
(domyslny dla starszych kontraktow) oraz `surface=cli`. CLI zamraza liste
`cli.argv` i `cli.expected_exit_code`; evidence uzywa `deterministic-check`,
`journey.command`, exit codes oraz hashowanych artefaktow `stdout.*` i
`stderr.*`. Nie tworzy viewportu i nie moze sluzyc jako dowod GUI. Dla GUI
pozostaja bez zmian viewport, widoczna reakcja i visible failure recovery.

Dowiez system, nie liste zielonych komend. Odbior zaczyna sie przed implementacja:
zamroz wymagania, stosowalnosc i oracles, a po pracy zbierz aktualne dowody z
warstw technicznych oraz z perspektywy rzeczywistej roli uzytkownika.

Helper `scripts/acceptance.py` jest deterministyczna bramka dowodow. Nie uruchamia
modeli, testow, przegladarki, Fleeta, Graphu, Loop ani napraw. Te czynnosci
wykonuja istniejace skille i narzedzia; helper jedynie odrzuca niekompletny lub
nieaktualny odbior.

Przed pierwszym kontraktem przeczytaj
[acceptance-contract.md](references/acceptance-contract.md). Przy DEV/STAGING,
integracjach i UAT przeczytaj
[environment-safety.md](references/environment-safety.md). Przy dowodach,
usterkach i naprawie przeczytaj
[evidence-and-repair.md](references/evidence-and-repair.md). Dla kazdego
user-facing workflow przeczytaj i zastosuj
[user-perception-and-feedback.md](references/user-perception-and-feedback.md).

## Przeplyw

1. Wez zatwierdzony kontrakt `plan-delivery` i skompilowany `agent-graph`.
   Przed mutacja utworz kontrakt odbioru `READY`. Kazdy materialny `REQ-*` musi
   byc pokryty kryterium i jednoznacznym oracle. Gdy `data-governance`
   sklasyfikowal zmiane jako `D1-D3`, kontrakt odbioru musi pokrywac wszystkie
   jego bramki i wskazywac hash zapieczetowanego kontraktu oraz fragmentu.
2. Rozdziel srodowisko: DEV/TEST ma dane syntetyczne i zablokowane efekty
   zewnetrzne; STAGING ma jawna allowliste; PROD wymaga osobnej zgody ownera.
   Brak dowodu egzekwowania blokady nie jest PASS.
3. Zainicjalizuj run z identyfikatorem konkretnego `run-graph-*` i przypnij
   binding kodu, srodowiska, wykonawcy, helpera oraz manifestu skilla. Receipt
   z jednego runu grafu nie moze zamknac innego. Kontrakt nie zmienia sie po
   `READY`; kolejne wykonania sa dopisywane do ledgeru.
4. Uruchom testy poza helperem. Dla UI wykonaj role-based UAT z trasa,
   viewportem, warunkami ekranu, akcjami i expected/actual. Kazdy stosowalny
   workflow user-facing MUSI miec osobne, obowiazkowe kryteria PASS/FAIL dla:
   osiagalnosci otwieranej powierzchni, postrzegalnej reakcji na akcje oraz
   widocznego bledu z bezpiecznym odzyskaniem. Wymagany dowod opisuje
   `references/user-perception-and-feedback.md`. `remote-computer-use` jest
   preferowanym izolowanym wykonawca browser UAT, ale taki mechanizm dowodzi wylacznie
   `browser-context`; nie dowodzi `application`, `network-egress`,
   `external-service` ani `host`.
5. Niezalezny reviewer zapisuje wynik kazdego kryterium. Maker nie akceptuje
   swojej pracy. `N/A` jest legalne tylko z trusted discovery albo jawna zgoda
   ownera zapisana juz w kontrakcie.
   Dla danych odbierz co najmniej: formule/jednostke/okres/strefe czasu,
   source-to-target reconciliation, allow/deny dla realnych rol, propagacje
   retencji/usuniecia, cache/backup/export i kompatybilnosc konsumentow. Row
   count, screenshot albo sam hash kontraktu nie wystarcza.
6. `close` zwraca niekoncowy `NOT_ACCEPTED`, gdy brakuje PASS i zostal budzet
   napraw. Zwykla naprawa wymaga nowego bindingu, jawnego rozwiazania wszystkich
   FAIL i uniewaznia WSZYSTKIE dotychczasowe PASS. Dla czysto transportowego lub
   formalnego packetu mozna jawnie uzyc `evidence-only-rebind`, ale tylko przy
   identycznym bindingu, niezmiennych hashach kryteriow/wymagan/graphu,
   identycznych aktualnych manifestach i niezaleznym reviewerze. Zmiana,
   niepewnosc lub brak dowodu wraca do pelnego retestu.
7. Finalny receipt powstaje raz: `ACCEPTED`, gdy kazde stosowalne obowiazkowe
   kryterium ma aktualny PASS, albo `NOT_ACCEPTED`, gdy petla zostala swiadomie
   zatrzymana lub wyczerpala budzet. Receipt nie ma sredniej ani wyniku punktowego.
8. Agent Graph moze zamknac istotna dostawe tylko z poprawnym receipt
   `ACCEPTED`. Dla `D1-D3` wymaga rowniez zweryfikowanego receipt
   `data-governance`; `PENDING` privacy/retention albo cutover blokuje PASS.
   Produkcyjny deploy i efekty zewnetrzne pozostaja osobnymi zgodami.

Dla dostawy mutujacej repo kontrakt odbioru musi zawierac kryterium repository
closeout wykonywane na docelowej galezi i w docelowym worktree po integracji.
Dowod obejmuje generyczny walidator projektu, `bootstrap.ps1 -ValidateSkills`
po zmianie wspolnych skilli, ownership albo release oraz `git status --short`.
Drzewo moze pozostac brudne tylko z kompletnym ledgerem kazdej sciezki,
wlascicielem i uzasadnieniem; taki ledger nie jest dowodem czystosci. Dowod z
izolowanego worktree wykonawcy nie zamyka tego kryterium.

## Minimalne polecenia

```powershell
python "<skill-dir>\scripts\acceptance.py" doctor
python "<skill-dir>\scripts\acceptance.py" validate --contract <acceptance.json> --planning-contract <approved.json> --graph <graph.json>
python "<skill-dir>\scripts\acceptance.py" init --contract <acceptance.json> --planning-contract <approved.json> --graph <graph.json> --graph-run-id <run-graph-id>
python "<skill-dir>\scripts\acceptance.py" start --run-id <id> --operation-id <op-id> --binding <binding.json>
python "<skill-dir>\scripts\acceptance.py" result --run-id <id> --operation-id <op-id> --criterion-id <id> --evidence <evidence.json>
python "<skill-dir>\scripts\acceptance.py" close --run-id <id> --operation-id <op-id>
python "<skill-dir>\scripts\acceptance.py" repair --run-id <id> --operation-id <op-id> --binding <new-binding.json> --resolution <resolution.json>
python "<skill-dir>\scripts\acceptance.py" evidence-only-rebind --run-id <id> --operation-id <op-id> --binding <same-binding.json> --resolution <resolution.json>
python "<skill-dir>\scripts\acceptance.py" verify-receipt --receipt <final-receipt.json> --graph-run-id <run-graph-id>
```

## Twarde granice

- Nie tworz wymagan ani wskaznikow "bo tak". Oracle wskazuje source i sens
  biznesowy; brak oracle blokuje odbior.
- Unit/API/build PASS nie zastepuje UAT widocznego z roli uzytkownika, gdy
  zmienilo sie zachowanie user-facing.
- Screenshot bez action trace, roli, route, viewportu i expected/actual nie jest
  dowodem UAT.
- Powierzchnia otwarta poza widocznym obszarem roboczym, nieosiagalna akcja
  podstawowa albo brak bezpiecznej drogi zamkniecia jest FAIL, nawet gdy stan
  backendu jest poprawny.
- Akcja bez natychmiastowego, postrzegalnego sygnalu przyjecia i bez prawdziwego
  stanu terminalnego jest FAIL. Prog czasu wynika z kontraktu produktu; skill
  nie narzuca jednej liczby wszystkim systemom.
- Blad widoczny tylko w konsoli lub logu, falszywy sukces, znikajacy dialog,
  wieczny spinner albo utrata danych formularza bez komunikatu i odzyskania jest
  FAIL.
- Storage state, cookies, credentials, tokeny i sekrety nie sa artefaktami
  odbioru i sa odrzucane.
- PASS sprzed naprawy nie przechodzi do nowej generacji. Nie wolno dobierac
  najwygodniejszego receipt ani powtarzac kryterium w tej samej generacji.
  Wyjatek stanowi wylacznie jawny, deterministyczny `evidence-only-rebind` z
  identycznym bindingiem i zweryfikowanymi hashowanymi dowodami; kazdy PASS poza
  jawna lista pozostaje PENDING.
- Finalny odbior jest per scope/run. Kazdy materialny criterion ma wlasny oracle
  i niezaleznego reviewera; automatyczny reviewer-of-review nie zastepuje tego
  dowodu.
- Tokeny sa domyslnie `estimate`; jawny `hard` pozostaje twardy. `unknown` nie
  jest kosztem, a sama estymata telemetryczna nie blokuje odbioru.
- Wynik modelu lub wykonawcy jest nieufnym twierdzeniem do niezaleznego odbioru.
- Backup bez restore testu, polityka RLS bez testu odmowy, migracja bez
  reconciliation oraz raport bez business oracle nie sa dowodem odbioru danych.
- Nie wysylaj realnych maili, platnosci, wpisow CRM/kalendarza ani mutacji
  publicznej integracji w DEV/TEST. Nie testuj PROD bez osobnej jawnej zgody.
- `NOT_ACCEPTED` jest uczciwym wynikiem, nie awaria do zamaskowania.

## Raport

Podaj: kontrakt i graph hash, srodowisko, generacje, pokrycie kryteriow,
historyczne FAIL i ich rozwiazania, wynik finalny, stop reason, receipt path,
dowod UAT, zakres egzekwowania side-effect safety oraz nierozwiazane blokery.
```
