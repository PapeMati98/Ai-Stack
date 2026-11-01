# Audyt pakietu

Data audytu: 2026-07-29

## Zrodlo

- Repozytorium: `https://github.com/openai/codex-security`
- Pakiet: `@openai/codex-security@0.1.1`
- Licencja: Apache-2.0
- Tag zrodla: `npm-v0.1.1`
- Commit: `e94d6bef9797a192febfde89a26ec7f831bc09b2`
- SHA1 tarballa: `2ed585c8d2dfd7b0ebccf94753921913b8402061`
- npm integrity:
  `sha512-sNxULf7IyicJRgYnycguaEzO2ZeANEv3oyrupjMoKVq5TjRrmIhORpaa7U2LVIpEHJP7//icGZV37MRIlp9X/A==`

Pakiet ma provenance npm, podpisy rejestru i nie zawiera skryptow
`install`/`postinstall`. Instalacja jest wykonywana z `--ignore-scripts` i
przypieta do wersji.

## Wynik

Werdykt: **zaakceptowany przez ograniczony wrapper po trzech niezaleznych
audytach**.

Do globalnego wlaczenia odrzucono szeroki plugin dolaczony do pakietu. Zawiera
on narzedzia zapisu i instrukcje pozyskiwania tokenu GitHub REST z pominieciem
jawnie wskazanego Connectora. To koliduje z lokalna polityka narzedzi i zasada
najmniejszych uprawnien.

Dozwolony jest tylko oficjalny skan w odizolowanym runtime oraz lokalny odczyt
i eksport wynikow. Wrapper nie udostepnia `bulk-scan`, hookow, patchowania,
rerunow, MCP ani synchronizacji pluginu.

## Weryfikacja runtime

- `npm audit --omit=dev`: 0 podatnosci,
- `npm audit signatures`: podpisy i attestations zweryfikowane,
- liczba plikow pakietu: 178,
- SHA256 drzewa pakietu:
  `87e8455013bf8672a43c46f5f2a0187f64628624551ca9e3de2cdc12e15594bc`,
- SHA256 `package-lock.json`:
  `e7257e397f279162635448c55d9fcfefcb980082e60da2a23619a2d5c997ce42`,
- SHA256 binarki:
  `8464effc0af5c05c2d58703de9f43c315b0c9e4e66299937c5cf8a304294e326`.

Statyczny skan kwarantanny:
`%LOCALAPPDATA%\agent-stack-quarantine\codex-security\0.1.1\static-scan-npm.md`.

## Audyt lokalnego wrappera

Deterministyczny skaner `audit-skills` oznaczyl wrapper jako `ODRZUCONY`,
poniewaz znalazl nazwy `.env`, `.npmrc`, `.pypirc`, `cookies.sqlite` oraz
wywolania `Get-Command`. Reczna kwalifikacja wykazala, ze sa to falszywe
alarmy:

- nazwy plikow poufnych tworza liste blokujaca wyslanie ich do chmury,
- `Get-Command` tylko sprawdza lokalne wymagania runtime,
- uruchomienia procesu przekazuja argumenty jako tablice i wymuszaja
  `shell=False`,
- wrapper przekazuje do procesu potomnego tylko jawna liste bezpiecznych
  zmiennych,
- testy potwierdzaja blokade braku jawnej zgody, danych `restricted`,
  katalogow chronionych i kosztu ponad lokalny limit.

Pierwszy niezalezny audyt Agent Fleet zakonczyl sie werdyktem `REVISE`.
Wykryl: wartosci `NaN`/Infinity omijajace limit kosztu, arbitralny `--diff`,
bezposrednie zakresy `.git`/`.venv`/`node_modules`, zbyt szerokie
dziedziczenie srodowiska, brak przypiecia `node.exe` i niedostatecznie
powtarzalny rollback.

Po audycie:

- odrzucono wszystkie niefinitywne limity kosztu,
- usunieto `--diff`,
- zablokowano katalogi specjalne bez wzgledu na wielkosc liter oraz reparse
  points,
- `preflight` przepisano jako operacje czysto lokalna,
- srodowisko procesu zmieniono na allowlist,
- przypieto `node.exe` i jego SHA256,
- instalator zmieniono na transakcyjny,
- dodano jawny tryb auth i powtarzalny skrypt rollbacku,
- ujednolicono porzadek sortowania sciezek w instalatorze do porzadku
  ordinalnego uzywanego przez wrapper i porownano runtime plik-po-pliku
  z tarballem w kwarantannie,
- rozszerzono testy regresyjne.

Drugi niezalezny audyt Agent Fleet rowniez zakonczyl sie werdyktem `REVISE`.
Wykryl: mozliwosc wskazania arbitralnego katalogu runtime w instalatorze,
dziedziczenie srodowiska przez pomocnicze wywolania `node`, zbyt szeroki
parametr katalogu kopii rollbacku oraz ryzyko kolizji nazw kopii.

Po drugim audycie:

- runtime jest rozwiazywany wylacznie jako przypieta sciezka wzgledna pod
  kanonicznym profilem uzytkownika,
- wszystkie wywolania procesow maja `shell=False` i jawne, minimalne
  srodowisko,
- katalogi profilu, AppData, ProgramData i Windows sa rozwiazywane bez zaufania
  do `USERPROFILE`, `TEMP`, `PATH` ani innych zmiennych procesu,
- instalator i rollback sprawdzaja reparse points, uzywaja blokady mutex oraz
  unikalnych nazw opartych o timestamp i GUID,
- rollback akceptuje tylko bezposrednie dziecko wersjonowanego katalogu kopii
  pod `%LOCALAPPDATA%\agent-stack-backups\codex-security-cli`,
- testy regresyjne rozszerzono do 31 przypadkow.

Trzeci niezalezny audyt Agent Fleet zakonczyl sie werdyktem `PASS`. Potwierdzil
usuniecie wszystkich findingow `HIGH` i `MEDIUM` z dwoch poprzednich audytow
oraz zgodnosc runtime, manifestu, instalatora i 31 testow regresyjnych.

Dowod:
`.delegations\codex-security-cli-review-3\run-20260729T140415Z-4892f05d9e`.

Pozostale ryzyka `LOW`:

- lokalny skaner tresci pomija pliki binarne i pliki wieksze niz 2 MB,
- upstream CLI dostaje korzen repozytorium i moze odczytac historie `.git`,
  ktorej lokalna bramka sekretow nie analizuje,
- `--model` jest przekazywany jako osobny argument, ale nie ma lokalnej
  allowlisty nazw modeli,
- limit kosztu jest szacunkiem, a nie twarda blokada rozliczeniowa.

Podczas walidacji nie uruchomiono rzeczywistego skanu chmurowego, nie wyslano
kodu do OpenAI, nie poniesiono kosztu modelu i nie zmieniono CI/CD. Akceptacja
dotyczy wylacznie ograniczonego wrappera. Szerszy plugin dostarczany w pakiecie
pozostaje odrzucony.
