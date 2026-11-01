# Polityka bezpiecznego uzycia

## Klasyfikacja danych

- `public`: kod i dokumentacja przeznaczone do publikacji.
- `internal`: kod wewnetrzny bez danych klientow i sekretow.
- `confidential`: niepubliczny kod lub dane biznesowe. Wymaga dodatkowego,
  jawnego potwierdzenia przed wyslaniem.
- `restricted`: sekrety, dane uwierzytelniajace, dane medyczne, finansowe,
  klientow, osobowe albo inne dane o najwyzszej wrazliwosci. Skan chmurowy jest
  zabroniony.

## Granice wrappera

Wrapper:
- akceptuje tylko jedno, jawnie wskazane repozytorium,
- blokuje korzenie dyskow, katalogi systemowe i magazyny sekretow,
- blokuje przejscie `..`, symlinki, junctions i inne reparse points,
- blokuje bezposrednie skanowanie `.git`, `.venv`, `node_modules` i ich
  wariantow wielkosci liter,
- nie udostepnia arbitralnego `--diff`,
- wykrywa typowe pliki i wysokiej pewnosci wzorce sekretow przed wyslaniem,
- wymusza tryb `standard`,
- zapisuje wyniki i stan poza repozytorium,
- przekazuje do procesu potomnego tylko jawna liste bezpiecznych zmiennych
  srodowiskowych,
- uruchamia przypiety plik `node.exe` po weryfikacji SHA256,
- zapisuje lokalny dziennik metadanych bez tresci kodu.

Skaner sekretow jest dodatkowa warstwa bezpieczenstwa, a nie gwarancja
wykrycia kazdego sekretu. Przed skanem nadal trzeba recznie ocenic zakres i
usunac dane wrazliwe.

Lokalny `preflight` nie wywoluje binarki upstream, nie laczy sie z siecia i nie
tworzy katalogu stanu skanu. Sprawdza runtime, sciezki, pliki i zasady lokalnie.

## Uwierzytelnienie

- `--auth chatgpt`: oficjalny runtime moze odczytac istniejace dane logowania
  Codex z domyslnego `%USERPROFILE%\.codex\auth.json`. Wrapper nie udostepnia
  komendy `login`.
- `--auth api-key`: do procesu skanu przekazywany jest tylko jawnie ustawiony
  `OPENAI_API_KEY`. Klucz nie trafia do logu wrappera.
- Brak wybranego trybu uwierzytelnienia blokuje skan.

Jesli logowania Codex nie ma, wykonaj je poza tym skillem przez oficjalne
narzedzie Codex. Nie kopiuj tokenu do konfiguracji skilla.

## Dane i koszt

Wlasciwy `scan` korzysta z uslugi OpenAI i moze wysylac analizowany kod oraz
metadane potrzebne do oceny. Findings moga zawierac fragmenty kodu i opis
podatnosci. Nie publikuj artefaktow skanu bez redakcji.

`--max-cost` zatrzymuje prace po przekroczeniu szacunku, ale operacje juz
rozpoczete moga zakonczyc sie po przekroczeniu wskazanej kwoty. Nie jest to
twardy limit rozliczeniowy. Lokalna nakladka ogranicza deklarowany limit do
25 USD na pojedyncze uruchomienie.

Walidacja instalacji i lokalny `preflight` nie wykonuja skanu chmurowego.
Pierwszy rzeczywisty skan wymaga osobnej decyzji uzytkownika po podaniu
repozytorium, zakresu, klasyfikacji danych i deklarowanego limitu kosztu.

## Dozwolone operacje

- lokalny `preflight`,
- nowy skan po jawnej zgodzie,
- lista i odczyt zapisanych skanow,
- eksport SARIF, JSON albo CSV z zarzadzanych katalogow.

## Zablokowane operacje

- masowe skanowanie organizacji,
- automatyczne hooki Git i CI,
- modyfikowanie kodu przez `patch`,
- walidacja i rerun bez nowej zgody kosztowej,
- globalna instalacja dolaczonego pluginu lub MCP,
- dowolny katalog pluginu, baza wiedzy albo override konfiguracji Codex.

## Rollback

Aktywne kopie mozna wycofac powtarzalnym skryptem z kanonicznego zrodla:

```powershell
powershell -ExecutionPolicy Bypass -File `
  "<installed-skill-dir>\scripts\rollback_active_copies.ps1" `
  -BackupRoot "<pelna-sciezka-kopii-zapasowej>" `
  -ConfirmRollback
```

Skrypt usuwa tylko aktywne kopie `codex-security-cli` w pieciu obslugiwanych
runtime i przywraca zachowane pliki `security-audit/SKILL.md`. Nie usuwa
runtime ani wynikow. Przed wykonaniem mozna dopisac `-WhatIf`, aby sprawdzic
plan bez zmian na dysku.

Runtime jest instalowany transakcyjnie. Poprzednia wersja pozostaje obok niego
jako katalog `.0.1.1.backup-<timestamp>` i moze byc przywrocona przez zmiane
nazw katalogow po zatrzymaniu procesow korzystajacych z runtime.

Opcjonalnie mozna usunac runtime:
`%USERPROFILE%\.agents\runtimes\codex-security\0.1.1`.

Opcjonalnie mozna usunac lokalne wyniki:
   `%LOCALAPPDATA%\MateuszAI\codex-security`.

Usuniecie runtime lub wynikow jest destrukcyjne i wymaga osobnej, jawnej zgody.
