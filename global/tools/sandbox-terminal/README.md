# sandbox-terminal

Lokalna, domyślnie wyłączona powłoka sandbox dla modeli bez własnej powłoki.
Wzorzec: OpenMuse `apps/computer`; implementacja własna, Python 3.13 stdlib.
Nic nie startuje automatycznie — brak usług, zadań harmonogramu i zmian
w konfiguracji agentów.

## Model zagrożeń

Chronione: host (pliki, sieć, procesy, socket Dockera, sekrety ze zmiennych
środowiskowych hosta) przed kodem wykonywanym przez model w kontenerze.

Założenia i ograniczenia:

- Jedyną granicą jest Docker. Ucieczka z kontenera (kernel/daemon exploit)
  jest poza modelem; dlatego kontener dostaje minimum uprawnień.
- `--pull never` + lokalny obraz: brak pobierania i brak ryzyka podmiany
  obrazu z rejestru w czasie uruchomienia. Brak obrazu = błąd, nie pull.
- `--network none`: brak sieci, także DNS i usług hosta po sieci.
- `--user 10001:10001`, `--read-only`, `--cap-drop ALL`,
  `--security-opt no-new-privileges`: zapis tylko do `/workspace` (named
  volume sesji) i `/tmp` (tmpfs 64m, `noexec,nosuid`).
- Limity DoS: `--pids-limit 128`, `--memory 512m`, `--cpus 1`, timeout
  exec 30 s (max 120 s) z zabiciem procesu w kontenerze, wyjście obcinane
  do 64 KiB na strumień, pliki max 1 MiB.
- Brak bind mountów hosta, brak socketu Dockera, brak przekazywania
  zmiennych środowiskowych hosta do kontenera.
- Świeży named volume Dockera należy do roota, a kontener sesji działa jako
  10001:10001, więc `create` po utworzeniu kontenera sesji uruchamia
  jednorazowy kontener inicjujący (`--rm`, ten sam lokalny obraz,
  `--network none`, `--cap-drop ALL` + wyłącznie `CAP_CHOWN`, bez kodu
  użytkownika), który robi `chown 10001:10001 /workspace` na volume sesji.
  Kolejność ma znaczenie: chown przed utworzeniem kontenera byłby cofnięty,
  gdy Docker przy tworzeniu kontenera kopiuje zawartość obrazu do jeszcze
  pustego volume. Kontener sesji nigdy nie działa jako root, a kontener
  inicjujący nie ma dostępu do niczego poza tym jednym volume.
- Operacje działają tylko na kontenerach z etykietą
  `agent-stack.sandbox-terminal=1`; `destroy` odmawia usunięcia kontenera
  bez tej etykiety i usuwa tylko `sbx-<sesja>` oraz `sbx-<sesja>-ws`.
- Ścieżki plików są normalizowane i ograniczone do `/workspace`;
  `..` i ścieżki bezwzględne poza `/workspace` są odrzucane.
- Dane w `/workspace` przetrwają `exec`, ale są usuwane przez `destroy`.
  Nie przechowuj tam niczego, czego nie możesz stracić; sekrety nie powinny
  trafiać do sandboxa w ogóle (nie są logowane przez narzędzie).

## Użycie CLI

Wynik każdej komendy to jeden obiekt JSON na stdout.

```powershell
python sandbox_terminal.py create moja-sesja
python sandbox_terminal.py exec moja-sesja -- echo hello
python sandbox_terminal.py exec moja-sesja --timeout 10 -- python3 -c "print(1+1)"
python sandbox_terminal.py write-file moja-sesja hello.txt "tresc"
python sandbox_terminal.py read-file moja-sesja hello.txt
python sandbox_terminal.py status moja-sesja
python sandbox_terminal.py list
python sandbox_terminal.py destroy moja-sesja
```

Nazwa sesji: `[a-z0-9-]{1,32}`. Obraz nadpisuje `SANDBOX_TERMINAL_IMAGE`
(domyślnie lokalny `python:3.13.7-slim-bookworm`), katalog stanu
`SANDBOX_TERMINAL_HOME` (domyślnie `~/.agents/sandbox-terminal`),
binarkę Dockera `SANDBOX_TERMINAL_DOCKER`.

## Użycie MCP

`python sandbox_terminal.py mcp` uruchamia ręcznie implementowany serwer
MCP stdio (JSON-RPC 2.0, jedna wiadomość na linię): `initialize`,
`tools/list`, `tools/call`, `notifications/initialized`. Narzędzia:
`create`, `exec`, `write_file`, `read_file`, `status`, `destroy`, `list`
— te same operacje co CLI. Serwer działa tylko w foreground na stdio
procesu wywołującego; nic nie nasłuchuje na sieci.

## Testy

```powershell
python -m pytest -q global/tools/sandbox-terminal/tests
```

Testy integracyjne wymagają lokalnego Dockera i lokalnego obrazu; bez nich
są pomijane (`skip`), testy jednostkowe walidacji działają zawsze. Testy
tworzą wyłącznie sesje z unikalnym prefiksem i sprzątają tylko je.

## Rollback

1. Przestań wywoływać narzędzie (nie ma autostartu, zadania harmonogramu ani
   wpisu w konfiguracji agentów, więc nic go nie uruchamia samo).
2. Kod przywróć przez `git revert <commit>` (albo przywrócenie poprzedniej
   wersji pliku z historii), nigdy przez kasowanie katalogu.
3. Sesje: zatrzymaj tylko te, które sam utworzyłeś i chcesz zakończyć,
   poleceniem `destroy <sesja>`. `destroy` działa wyłącznie na kontenerze z
   etykietą narzędzia, a volume usuwa tylko wtedy, gdy też ma tę etykietę;
   obcy lub niejednoznaczny volume powoduje odmowę bez żadnego usuwania.
   Rollback nie wymaga niszczenia sesji ani danych w ich volume.

Narzędzie nie instaluje pakietów, nie zmienia konfiguracji agentów ani
systemu, więc nie ma innych kroków wycofania.

Uwaga dla Git Bash: ścieżki zaczynające się od `/` (np. `/workspace/a.txt`)
są tam zamieniane na ścieżki Windows; ustaw `MSYS_NO_PATHCONV=1` albo podawaj
ścieżki względne (`a.txt`).
