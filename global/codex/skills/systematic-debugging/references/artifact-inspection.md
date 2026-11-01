# Artifact inspection (optional)

Uzyj, gdy symptom dotyczy pliku binarnego, pakietu, snapshota runtime albo
komunikacji IPC, a zwykla sciezka 1-6 (reprodukcja -> hipoteza -> test ->
poprawka) nie wystarcza, bo kontrakt lezy po stronie formatu pliku, a nie
kodu zrodlowego. To krotka referencja read-only -- nie nowy preflight i
nie instalator. Czytaj ja tylko wtedy, gdy zwykle kroki nie daja przyczyny
albo symbol wskazuje na konkretny artefakt.

## Zasady

- Pracuj na kopii pliku w `task-inputs/<run>/fixture/`. Nie modyfikuj
  oryginalu, nie wykonuj zadnego z binariow, nie uruchamiaj loadera ani
  autoinstalatora upstreamu. Granice sieci, zapisu i wykonania wyznacza
  runtime tego kontraktu; ta referencja ich nie rozszerza i nie
  zastrzega ponad nie. Nie twierdz, ze sam opis je wylacza -- raportuj
  kazde odmowne dzialanie jako `tool_calls_blocked`.
- Nie pobieraj nic przez siec, nie wywoluj autoinstalatora upstreamu
  (zawiera destrukcyjne receptury). Narzedzia ponizej sa juz w naszym
  audytowanym runtime; brak ktoregokolwiek z nich oznacza raport
  UNKNOWN, nie autoinstalacje.
- Format pliku determinuje trase analizy. Najpierw rozpoznaj format
  magic bytes, potem wybierz odczyt i wycinek.
- Kazde twierdzenie opatrz znacznikiem zrodla i statusem:
  - OBSERVED: zrzut z pliku (offset/sciezka/linia), hash lub wycinek.
  - INFERRED: wniosek z budowy formatu, z zaznaczona niepewnoscia.
  - UNKNOWN: brak danych, nie domyslaj sie.

## Mapowanie format -> trasa (read-only)

| Format (Windows)        | Co czytac (read-only)                             | Wskaznik dalszej analizy         |
|-------------------------|---------------------------------------------------|----------------------------------|
| PE (.exe/.dll)          | MZ i PE\0\0 naglowki, sekcje, imports             | obecnosc `.text`, `.rsrc`        |
| .NET (assemblies)       | CLR header, tabele metadanych, IL tylko do podgladu| flaga `ILONLY`, `COMIMAGE_FLAGS` |
| Electron (extracted)    | dostarczone wyciagi syntetyczne, package.json    | brak wbudowanego indeksu FS      |

| Format (Linux)          | Co czytac (read-only)                             | Wskaznik dalszej analizy         |
|-------------------------|---------------------------------------------------|----------------------------------|
| ELF (binaria natywne)   | e_type, sekcje .text/.rodata, dynamic section     | PT_INTERP, RPATH/RUNPATH         |
| Go (binaria natywne)    | sekcja go:buildid, gopclntab, wersja runtime     | buildid hex, wersja runtime      |
| WASM (.wasm)            | magic \0asm, sekcje, imports/exports, name section| deklaracje importu               |
| Bundle (zip/asar/crx)   | EOCD i central directory (zip), naglowek TOC     | rozmiar katalogu vs suma pol     |

| Format (komunikacja)    | Co czytac (read-only)                             | Wskaznik dalszej analizy         |
|-------------------------|---------------------------------------------------|----------------------------------|
| IPC (Electron/main)     | stack trace z ipcRenderer/ipcMain, channel name   | did-finish-load, webContents     |
| Bundle + IPC snapshot   | para (channel, payload) w dostarczonym drzewie    | licznosc type vs handlerow       |

## Narzedzia istniejace

Wszystkie ponizsze komendy sa read-only. Nie uruchamiaj zadnego z
binariow. Jesli narzedzia nie ma w srodowisku, raportuj UNKNOWN -- nie
pobieraj zamiennika.

- `python -B -c "from pathlib import Path;data=Path(p).read_bytes()[:n];..."`
  -- odczyt binarnych naglowkow przez stdlib (PE `MZ`/`PE\0\0`,
  ELF `\x7fELF`, WASM `\0asm`). Bez wywolania subprocess.
- `Get-FileHash -Algorithm SHA256 <path>` -- identyfikator pliku.
  W raporcie jako `file:<sha256>`.
- PowerShell odczyt binarny (bez wczytywania calej tresci do pamieci):
  `$fs=[System.IO.File]::OpenRead($p);$buf=New-Object byte[] $n;$r=$fs.Read($buf,0,$n);$fs.Close()`
  -- ograniczony bufor do inspekcji naglowka.
- `python -B scripts/summarize_evidence.py --format text --input
  <cap.log>` -- agregacja dlugich logow z redakcja sekretow.
  Patrz [large-results.md](large-results.md).

Nie probuj pobierac tabel importu z upstreamu -- nie uruchamiaj klona
repozytorium ani autoinstalatora. Jesli potrzebujesz stalej tabeli
symboli, uzyj audytowanych plikow dostarczonych w `task-inputs/<run>/fixture/`
albo raportuj UNKNOWN.

## Bundlowanie i sledzenie IPC -- procedura

1. Skopiuj plik do `task-inputs/<run>/fixture/<name>` obok logu
   reprodukcji. Oblicz SHA256 i zapisz w raporcie.
2. Rozpoznaj format po magic bytes przez Python stdlib; nie polegaj
   na rozszerzeniu.
3. NIE rozpakowuj binariow samodzielnie. Brak audytowanego narzedzia;
   autoinstalator zabroniony. Wiersz `Bundle (zip/asar/crx)` ogranicza
   odczyt do EOCD i central directory. Jesli dostarczono wyciag w
   `fixture/unpack/`, czytaj tylko z niego; inaczej raportuj UNKNOWN.
4. Electron: nie zakladaj istnienia `index.json` wewnatrz `app.asar` --
   to nie jest czesc specyfikacji Electrona. Inspekcja ogranicza sie
   do dostarczonego wycinka syntetycznego (`package.json`,
   `manifest.json`, `main-stub.txt`); brak danych raportuj jako
   UNKNOWN, nie rozpakowuj asar samodzielnie.
5. Dla IPC: z logu reprodukcji wytnij pary `(channel, payload)` i
   policz `type:`. Niezgodnosc licznika vs dostarczonego drzewa
   handlerow to kandydat do hipotezy, nie wyrok.
6. Zamknij procedure notatka: format, sha256, unpack_root,
   lines_read, redactions_applied, evidence_status.

## Minimalna karta artefaktu

```text
path: <fixture_path>
sha256: <hex>
format: PE | ELF | Go | WASM | asar | zip | unknown
version_or_section: ...
tool_calls_observed: <audytowane read-only komendy>
tool_calls_blocked: <wykonanie binariow, autoinstalator, siec, zapis poza fixture>
evidences:
  - id: E1 ; status: OBSERVED ; source: path:offset ; hash: <chunk-sha256>
  - id: E2 ; status: INFERRED ; basis: <reasoning> ; counter_if_wrong: ...
  - id: E3 ; status: UNKNOWN  ; needed: <data>
scope_limits: ...
next_safe_action: ...
```

status jest obowiazkowy; INFERRED musi miec counter_if_wrong.

## Scenariusze akceptacyjne (syntetyczne, read-only)

A. Electron IPC po remapowaniu kanalu.

Dane (syntetyczne, bez JS do wykonania):
- `task-inputs/<run>/fixture/sample-app/package.json`:
  `{"name":"sample-app","version":"1.2.3","main":"stub.js","ipc_channels":["login","sync","logout"]}`
- `task-inputs/<run>/fixture/sample-app/main-stub.txt` z liniami
  `ipcMain.handle('login', ...)`, `ipcMain.handle('sync', ...)`,
  `ipcMain.handle('logout', ...)`.

Kroki (read-only):
- `Get-FileHash fixture/sample-app/package.json` -- zapisz sha256.
- `python -B -c "import json;from pathlib import Path;p=Path('fixture/sample-app/package.json');d=json.loads(p.read_text(encoding='utf-8'));print(d['name'],d['version'],len(d['ipc_channels']))"`
  -- E1 OBSERVED: name, version, liczba kanalow. Brak wykonania.
- Z logu reprodukcji (osobny plik `fixture/repro.log`) wytnij pary
  `(channel, payload)` regexem; policz `type:`. Niezgodnosc z
  `ipc_channels` (3 vs np. 4 wpisy) -- E2 INFERRED, kandydat hipotezy;
  counter_if_wrong: ten sam kanal pojawia sie wielokrotnie.
- Brak mapowania channel->handler w drzewie -- E3 UNKNOWN, nie domyslaj
  sie; `needed: dostarczone drzewo handlerow`.

Asercja pass: E1 ma hash, E2 ma counter_if_wrong, E3 ma `needed`. Brak
wywolan autoinstalatora, sieci ani wykonania. Sciezki tylko w
`task-inputs/<run>/fixture/`.

B. Inwentaryzacja PE/.NET po awarii loadera.

Dane: dostarczony stub `fixture/sample.dll` (syntetyczny; nie pobieraj
z sieci). Stub trzyma `MZ` na offsecie 0, `e_lfanew` na `0x3C`
wskazujacy na `PE\0\0`, opcjonalny naglowek z `Magic` `0x10b`/`0x20b`.

Kroki (read-only):
- `Get-FileHash fixture/sample.dll` -- zapisz sha256.
- `python -B -c "from pathlib import Path;b=Path('fixture/sample.dll').read_bytes();n=len(b);o=int.from_bytes(b[0x3C:0x40],'little') if n>=0x40 else 0;m=b[o:o+4] if n>=o+4 else b'';print(b[:2] if n>=2 else None,'e_lfanew=',hex(o) if n>=0x40 else None,m)"`
  -- E1 OBSERVED tylko jesli kazdy odczyt miesci sie w pliku; w
  przeciwnym razie E1 = UNKNOWN, `needed: dluzszy plik`.
- E2 INFERRED: `IMAGE_DIRECTORY_ENTRY_COM_DESCRIPTOR.VirtualAddress`
  lezy na `e_lfanew+0xE8` (PE32, `Magic=0x10b`) albo `e_lfanew+0xF8`
  (PE32+, `Magic=0x20b`). `COMIMAGE_FLAGS` to 4 bajty na offsecie 16
  wewnatrz `IMAGE_COR20_HEADER` -- wymaga translacji RVA->plik przez
  tabele sekcji; bez niej raportuj UNKNOWN. counter_if_wrong:
  czytasz `e_lfanew+0xE8` na PE32+ albo `MinorRuntimeVersion` zero
  poza zakresem.
- Brak silnej sygnatury klucza w stubie -- E3 UNKNOWN, nie odgaduj;
  `needed: tabela sekcji albo audytowany header extractor`.

Asercja pass: brak wywolan autoinstalatora, sieci, pip ani npx.
Karta zawiera `tool_calls_blocked` z co najmniej jedna pozycja.

## Antybiurokracja

- Nie dodawaj krokow do zwyklej sesji; ta referencja istnieje tylko
  na wyrazne zadanie. SKILL.md laczy ja warunkowo.
- Nie kopiuj upstreamowego SKILL.md 1:1. Nie instaluj autoinstalatora,
  nie uruchamiaj jego komend.
- Nie pisz nowego MCP, drugiego runtime ani stalego demona.
- Nie twierdz, ze sam opis wylacza siec lub wykonanie -- polegaj na
  runtime i raportuj kazde odmowne dzialanie w `tool_calls_blocked`.
- Nie uzywaj destrukcyjnych komend (`rm`, `del`) na oryginale lub
  plikach zewnetrznych. `task-inputs/<run>/fixture/` jest jedynym
  katalogiem zapisu tej sciezki.

## Granica

To nie jest nowy skill, nie jest framework operacyjny, nie jest
instalator. Brak tu wlasnego pliku konfiguracyjnego, brak stalego
procesu w tle. Gdy symptom wraca do kodu zrodlowego, wroc do
zwyklej sciezki 1-6 z [contract.md](contract.md).