# secret_run - Uruchamianie komend z poswiadczeniami 1Password bez wgladu agenta

Modul realizuje cel **T5** programu organizacji agentow (fala 1, wezel U2 oraz fala 2, wezel V2) oraz utwardzenia bezpieczenstwa (HD-20261021-57ae31): agenci moga uruchamiac procesy z kluczami API i poswiadczeniami pobieranymi z 1Password, bez mozliwosci poznania ich wartosci.

---

## 1. Glowne zasady bezpieczenstwa

1. **Zero-Knowledge dla agenta**:
   - Agent **nigdy** nie odczytuje, nie pobiera ani nie ujawnia wartosci sekretu.
   - Podkomendy ujawniajace wartosc (`read`, `get`, `reveal`, `--reveal`, `inject` do stdout) sa **bezwzglednie zakazane i zablokowane**.
   - Skrypt nie posiada zadnego trybu "pokaz wartosc". Jedyna dozwolona sciezka to `secret_run.py run`.
2. **Scisla allowlista referencji i komend (V2)**:
   - Kazda referencja `op://<sejf>/<pozycja>/<pole>` przekazana w `--env` musi znajdowac sie na liscie `refs` projektu w allowliscie (`~/.agents/secrets/allowlist.json`).
   - Komenda potomna (`argv[0]`, po rozwiazaniu nazwy) oraz skrypt musza znajdowac sie na liscie `commands` danego projektu.
   - Dopasowanie sciezki skryptu jest scisle: pelna sciezka wymaga dokladnego dopasowania sciezki (skrypt spoza dozwolonej sciezki nie spelnia wpisu po samej nazwie pliku, a wpis bedacy sama nazwa nie dopasowuje sciezki z innego katalogu).
   - Proba wykonania komendy lub skryptu spoza listy skutkuje odmowa (`COMMAND_NOT_ALLOWED`) przed uruchomieniem jakiegokolwiek procesu.
   - Stary format allowlisty (sama lista referencji) jest odrzucany z komunikatem o migracji (`ALLOWLIST_FORMAT_OLD`).
   - W przypadku braku allowlisty lub referencji spoza listy wykonanie zostaje **natychmiast odrzucone** (kod `ALLOWLIST_MISSING` lub `REF_NOT_ALLOWED`).
3. **Bezwzgledny zakaz powlok i kodu inline (SHELL_NOT_ALLOWED)**:
   - Interpretery-ogolniki i powloki: `cmd`, `powershell`, `pwsh`, `bash`, `sh`, `zsh`, `fish`, `wsl` sa bezwzglednie zakazane jako komenda potomna, nawet jesli zostalyby umieszczone na allowliscie.
   - Kod inline w dowolnej postaci (`python -c`, `node -e`, `node --eval`, `node --eval=<kod>`, `node -p`, `node --print`, `node -pe`) oraz uruchamianie interpreterow w trybie interaktywnym (REPL bez pliku skryptu) sa zablokowane.
   - Parsowanie opcji interpreterow jest utwardzone przed maskowaniem argumentow opcji jako skryptu: lista opcji przyjmujacych parametry jest rozszerzona o opcje diagnostyczne, profilowania i uprawnien Node, a skanowanie argumentow w poszukiwaniu kodu inline obejmuje pelny zakres przed separatorem `--`, co uniemozliwia pominiecie blokady `SHELL_NOT_ALLOWED`.
   - Skrypt z pliku (`python sciezka.py`) jest dozwolony wylacznie wtedy, gdy zarowno interpreter (`python`), jak i sciezka do skryptu znajduja sie na allowliscie `commands`.
4. **R1: Sanityzacja srodowiska i zakaz referencji op:// w argumentach**:
   - Przed wstrzyknieciem zwalidowanych zmiennych ze srodowiska procesu potomnego bezwzglednie usuwane sa wszelkie odziedziczone zmienne zawierajace `op://` (case-insensitive).
   - Usuwane sa niebezpieczne zmienne mogace posluzyc do wstrzykniecia kodu: `NODE_OPTIONS`, `PYTHONSTARTUP`, `PYTHONPATH`, `PYTHONINSPECT`, `BASH_ENV`, `ENV`, `PROMPT_COMMAND`.
   - Jakikolwiek token w argumentach potomnych (`argv`) zawierajacy `op://` jest natychmiast odrzucany z kodem `FORBIDDEN_OPERATION`.
5. **R2: Walidacja preload Node i blokada opcji inspekcji / interaktywnych**:
   - Flagi wstepnego ladowania modulu w Node (`-r`, `--require`, `--import`, `--loader`, `--experimental-loader`, `--env-file`, `--env-file-if-exists`) sa skanowane przed separator `--`. Kazdy modul docelowy (preload target) musi byc obecny na liscie `commands` allowlisty projektu (brak -> `COMMAND_NOT_ALLOWED`).
   - Flagi uruchamiajace inspektor Node (`--inspect`, `--inspect-brk`, `--inspect-port`) oraz flaga interaktywna Pythona (`-i`) sa bezwzglednie blokowane przed `--` z kodem `SHELL_NOT_ALLOWED`.
6. **R3: Opcjonalne przypinanie skrotow SHA-256 skryptow**:
   - Projekty moga zdefiniowac opcjonalny slownik `command_sha256` mapujacy sciezke skryptu/preloadu na oczekiwany skrot heksadecymalny SHA-256 oraz opcjonalna flage `require_script_sha256` (domyslnie `false`).
   - Jesli skrypt lub preload target znajduje sie w `command_sha256` (lub gdy wlaczono `require_script_sha256`), obliczany jest rzeczywisty skrot pliku; niezgodnosc lub brak pliku skutkuje odmowa z kodem `COMMAND_HASH_MISMATCH`.
7. **R4: Straznik nadpisywania SECRET_RUN_ALLOWLIST_PATH**:
   - Nadpisanie sciezki allowlisty przez zmienna `SECRET_RUN_ALLOWLIST_PATH` jest dozwolone wylacznie wtedy, gdy binarka `op` jest syntetyczna atrapa testowa w Pythonie (konczy sie rozszerzeniem `.py`). W przeciwnym razie wywolanie konczy sie kodem `ALLOWLIST_OVERRIDE_FORBIDDEN`.
8. **R5: Rezerwowe wykrywanie binarnego op.exe na Windows**:
   - Gdy `op` nie znajduje sie bezposrednio w `PATH`, `resolve_op_binary()` sprawdza typowe lokalizacje instalacji WinGet oraz Program Files:
     1. `%LOCALAPPDATA%/Microsoft/WinGet/Links/op.exe`
     2. `%LOCALAPPDATA%/Microsoft/WinGet/Packages/AgileBits.1Password.CLI_*/op.exe`
     3. `%PROGRAMFILES%/1Password CLI/op.exe`
9. **Wymuszone maskowanie**:
   - Uruchamianie nastepuje wylacznie przez `op run -- <komenda>` z referencjami w srodowisku procesu potomnego.
   - Flaga `--no-masking` jest zablokowana. Wbudowane maskowanie 1Password zastepuje wystapienia sekretu w stdout/stderr znacznikiem `<concealed by 1Password>`.
10. **Niezalezny audyt bez wycieku sekretow**:
    - Kazde wykonanie jest rejestrowane w `~/.agents/secrets/audit.jsonl`.
    - Zapisywane sa: timestamp, slug projektu, lista referencji URI `op://...`, nazwa komendy (`komenda[0]`), kod wyjscia oraz czas trwania.
    - Wartosci sekretow, pelny argv ani wyjscie komendy **nigdy** nie trafiaja do audytu.
11. **Uruchamianie bez okien**:
    - Wszystkie podprocesy na platformie Windows uruchamiane sa z flaga `creationflags=subprocess.CREATE_NO_WINDOW`.

---

## 2. Ryzyko resztkowe i zasada doboru komend (V2)

### Opis ryzyka resztkowego
Wbudowane maskowanie 1Password CLI (`op run`) przechwytuje i filtruje wylacznie strumienie `stdout` oraz `stderr` procesu potomnego.
Istnieje **ryzyko resztkowe**: program lub skrypt uruchomiony z poziomu allowlisty posiada bezposredni dostep do zmiennych srodowiskowych z odszyfrowanym sekretem w swojej pamieci. Zlosliwy lub zle skonfigurowany program moze samoczynnie:
- Zapisac wartosc sekretu do pliku tymczasowego, cache lub logu na dysku,
- Przekazac sekret w ciele zapytania sieciowego do nieuprawnionego serwisu,
- Zapisac sekret w bazie danych.

### Zasada doboru programow na allowliste commands
Na allowliscie `commands` wolno umieszczac **wylacznie programy, ktore uzywaja sekretu wylacznie do bezposredniego uwierzytelnienia** (np. driver bazy danych nawiazujacy polaczenie, klient API autoryzujacy zapytanie biznesowe, oficjalne narzedzie deployu).
**Bezwzgledny zakaz umieszczania na liscie**:
- Programow diagnostycznych, narzedzi zrzutu pamieci/srodowiska (np. `env`, `printenv`, `set`),
- Polecen kopiujacych lub wypisujacych pliki do logow,
- Narzedzi ogolnego przeznaczenia mogacych sluzyc jako powloka. Przyklad: `git` (opcje `-c core.pager=...`, aliasy, hooki repozytorium uruchamiaja dowolne polecenia), `env`, `make`, `npm run` - nie umieszczaj ich na liscie `commands`.

---

## 3. Kroki konfiguracyjne dla Mateusza

Aby wlaczyc pelna integracje ze stackiem na stacji roboczej, Mateusz wykonuje nastepujace kroki:

### Krok 1: Instalacja 1Password CLI (`op`)
Zainstaluj oficjalne narzedzie 1Password CLI na maszynie:
```powershell
winget install AgileBits.1Password.CLI
```
Lub pobierz instalator z oficjalnej strony dokumentacji 1Password:
`https://developer.1password.com/docs/cli/get-started/`

Dzieki mechanizmowi R5 narzedzie `secret_run` automatycznie odnajduje binarke w standardowych sciezka WinGet, jesli nie zostala dodana do systemowego `PATH`.

### Krok 2: Wlaczenie integracji z aplikacja desktopowa 1Password
1. Otworz aplikacje **1Password dla Windows**.
2. Przejdz do: **Ustawienia (Settings)** -> **Deweloper (Developer)**.
3. Zaznacz opcje: **Wlacz integracje z 1Password CLI (Integrate with 1Password CLI)**.
4. Upewnij sie, ze odblokowywanie biometryczne (Windows Hello) jest aktywne.
   Dzieki temu 1Password CLI autoryzuje operacje bez koniecznosci zapisywania tokenow w plikach tekstowych.

### Krok 3: Utworzenie dedykowanego sejfu `Agents`
1. W 1Password utworz osobny sejf o nazwie **`Agents`**.
2. W sejfie `Agents` umieszczaj wylacznie pozycje (Items) i klucze API, do ktorych dostep maja miec agenci stacku (np. `OpenAI`, `Anthropic`, `ExampleService-DB`).
3. Prywatne sejfy osobiste Mateusza pozostaja niedostepne dla referencji agentow.

### Krok 4: Konfiguracja allowlisty (`~/.agents/secrets/allowlist.json`)
Utworz katalog i plik allowlisty:
Sciezka: `~/.agents/secrets/allowlist.json` (czyli `%USERPROFILE%\.agents\secrets\allowlist.json`).
Nieaktywny, generyczny przyklad formatu znajduje sie w `shared/delegation/credential-run/allowlist.example.json`.

Format JSON mapuje identyfikator projektu (`slug`) na obiekt zawierajacy liste dozwolonych referencji URI (`refs`), liste dozwolonych komend i skryptow (`commands`) oraz opcjonalne przypiecie skrotow (`command_sha256`):
```json
{
  "example-project": {
    "refs": [
      "op://Agents/ExampleService/api_key"
    ],
    "require_script_sha256": true,
    "commands": [
      "python",
      "scripts/example_client.py"
    ],
    "command_sha256": {
      "scripts/example_client.py": "0000000000000000000000000000000000000000000000000000000000000000"
    }
  }
}
```

> [!WARNING]
> **Wazne — obliczanie sum kontrolnych SHA-256 dla skryptow (`command_sha256`)**:
> To przyklad skladni, nie gotowa polityka dostepu. Plik nie jest ladowany automatycznie; nie zawiera prawdziwego skryptu, danych sejfu ani praw dostepu. Aktywacja rzeczywistej polityki wymaga osobnej, jawnej zgody wlasciciela.
> Szablon `allowlist.example.json` zawiera zastepczy skrot z 64 zer jako przyklad formatu. **Nie kopiuj szablonu bezposrednio do produkcji bez zaktualizowania skrotow**, poniewaz kazde uruchomienie przypietego skryptu zakonczy sie bledem `COMMAND_HASH_MISMATCH`.
> 
> Aby przypiac skrypt, oblicz jego rzeczywisty skrot SHA-256 za pomoca jednego z ponizszych polecen:
> 
> **PowerShell (Windows)**:
> ```powershell
> (Get-FileHash -Algorithm SHA256 "sciezka\do\skryptu.py").Hash.ToLower()
> ```
> 
> **Python (multiplatformowo)**:
> ```bash
> python -c "import hashlib, sys; print(hashlib.sha256(open(sys.argv[1], 'rb').read()).hexdigest())" sciezka/do/skryptu.py
> ```
> 
> **Bash / WSL / Linux**:
> ```bash
> sha256sum sciezka/do/skryptu.py | cut -d ' ' -f 1
> ```
> 
> *Uwaga*: Przypinanie skrotow SHA-256 jest opcjonalne. Jesli w danym projekcie nie jest wymagana weryfikacja sum kontrolnych skryptow, usun klucz `command_sha256` z konfiguracji projektu lub ustaw go jako `{}`.

Brak pliku allowlisty, brak projektu/referencji w pliku lub podanie komendy spoza listy `commands` powoduje natychmiastowe zablokowanie wykonania przed startem procesu.

---

## 4. Skladnia CLI i uzycie

### Uruchomienie komendy z poswiadczeniami
```bash
python shared/delegation/credential-run/secret_run.py run \
  --project <slug> \
  --env NAZWA_ZMIENNEJ=op://<sejf>/<pozycja>/<pole> \
  [--env INNA_ZMIENNA=op://...] \
  -- <komenda> [arg...]
```

### Przyklad
```bash
python shared/delegation/credential-run/secret_run.py run \
  --project example-project \
  --env DB_PASS=op://Agents/ExampleService/db_password \
  -- python scripts/migrate.py --target dev
```

Co sie dzieje podczas wykonania:
1. `secret_run.py` weryfikuje straznika R4 (brak nieautoryzowanego nadpisania allowlisty z rzeczywistym `op`).
2. Sprawdza czy allowlista jest w nowym formacie V2 i czy referencja znajduje sie w `refs` projektu.
3. Weryfikuje komende: zakaz powlok, kodu inline, opcji inspekcji Node (`--inspect*`) i trybu interaktywnego Pythona (`-i`).
4. Weryfikuje cele preload Node (`-r`, `--require`, `--import`, itp.) przeciw allowliscie `commands`.
5. Weryfikuje skroty SHA-256 skryptow i preloadow, jesli zdefiniowano `command_sha256`.
6. Dokonuje sanityzacji srodowiska (R1), usuwajac odziedziczone `op://` oraz niebezpieczne zmienne (`NODE_OPTIONS`, `PYTHONPATH`, itp.).
7. Wstrzykuje referencje do srodowiska procesu potomnego i wywoluje `op run -- ...` bez okna konsoli.
8. 1Password CLI wstrzykuje rzeczywista wartosc sekretu w pamieci procesu potomnego z automatycznym maskowaniem wyjscia.
9. Zwraca wynik i rejestruje fakt wykonania w pliku audytu.

---

## 5. Kody bledow (JSON)

W przypadku odmowy lub bledu `secret_run.py` konczy dzialanie z kodem wyjscia `!= 0` i wypisuje do `stderr` obiekt JSON:

- `COMMAND_NOT_ALLOWED`: Komenda potomna (`argv[0]`), uruchamiany skrypt lub modul preloadu Node (`-r`, `--require`, itp.) nie znajduje sie na liscie `commands` dla danego projektu.
- `SHELL_NOT_ALLOWED`: Proba uruchomienia powloki (`cmd`, `powershell`, `pwsh`, `bash`, `sh`, itp.), wykonania kodu inline (`python -c`, `node -e`), opcji inspekcji Node (`--inspect*`) lub trybu interaktywnego Pythona (`-i`). Blokowane bezwzglednie.
- `COMMAND_HASH_MISMATCH`: Skrot SHA-256 pliku skryptu nie zgadza sie ze skrotem przypietym w `command_sha256` lub plik nie istnieje / brak wymaganego skrotu (`require_script_sha256`). *Rozwiazanie:* Oblicz rzeczywisty skrot pliku za pomoca `(Get-FileHash -Algorithm SHA256 <sciezka>).Hash.ToLower()` i zaktualizuj wpis w `~/.agents/secrets/allowlist.json` (szczegoly w Kroku 4).
- `ALLOWLIST_OVERRIDE_FORBIDDEN`: Proba nadpisania sciezki allowlisty przez zmienna `SECRET_RUN_ALLOWLIST_PATH` w srodowisku produkcyjnym (z binarka inna niz testowa atrapa `.py`).
- `ALLOWLIST_FORMAT_OLD`: Wykryto stary format allowlisty (mapowanie projektu bezposrednio na liste referencji). Wymagana migracja do nowego formatu ze slownikiem `{"refs": [...], "commands": [...]}`.
- `REF_NOT_ALLOWED`: Referencja nie znajduje sie na allowliscie dla danego projektu (lub projekt nie istnieje w pliku).
- `ALLOWLIST_MISSING`: Brak pliku `~/.agents/secrets/allowlist.json`.
- `ALLOWLIST_INVALID`: Plik allowlisty zawiera niepoprawny format JSON lub nieprawidlowa strukture pol.
- `OP_CLI_MISSING`: Nie odnaleziono 1Password CLI `op` w systemie. Komunikat zawiera instrukcje instalacji dla Mateusza.
- `FORBIDDEN_OPERATION`: Proba wywolania zakazanej podkomendy (`read`, `get`, `reveal`, `inject`), uzycia flagi `--reveal` / `--no-masking` lub przekazania tokenu `op://` w argumentach procesu potomnego.
- `INVALID_ARGUMENTS`: Brak wymaganej flagi `--project` lub niepoprawny format opcji `--env`.
- `COMMAND_MISSING`: Brak komendy do wykonania po separatorze `--`.

---

## 6. Dziennik audytu (`~/.agents/secrets/audit.jsonl`)

Kazde udane lub zakonczone bledem procesu potomnego wykonanie komendy dopisuje jeden wiersz JSON do pliku audytu:
```json
{
  "timestamp": "2026-10-21T14:35:12Z",
  "project": "example-project",
  "references": [
    "op://Agents/ExampleService/db_password"
  ],
  "command": "python",
  "exit_code": 0,
  "duration_seconds": 1.2451,
  "duration_ms": 1245.1
}
```

---

## 7. Zmienne srodowiskowe (dla testow i srodowisk CI)

- `SECRET_RUN_OP`: Sciezka do wlasnego pliku wykonywalnego lub skryptu `op` (np. atrapa w testach jednostkowych).
- `SECRET_RUN_ALLOWLIST_PATH`: Nadpisanie sciezki do pliku allowlisty (dozwolone wylacznie z atrapa `.py`, chronione przez R4).
- `SECRET_RUN_AUDIT_PATH`: Nadpisanie sciezki do pliku audytu.
