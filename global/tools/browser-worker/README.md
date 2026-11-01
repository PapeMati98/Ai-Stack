# Browser Worker

Lokalna przegladarka dla modeli headless. Implementacja wlasna wzorca
OpenMuse `apps/worker`, bez uslug CopilotKit, SDK MCP, telemetrii ani API modeli.
Wymaga Python 3.13, juz zainstalowanego Playwright i Chromium. Nie instaluje
pakietow ani przegladarek. Niczego nie uruchamia automatycznie.

## Granice zaufania

- HTTP slucha wylacznie na `127.0.0.1`; nie ma opcji zmiany interfejsu.
  Kazde wywolanie wymaga Bearer tokenu porownywanego `hmac.compare_digest`.
  Brak tokenu lub nieprawidlowy token daje HTTP 401. Obce `Host` i dowolny
  `Origin` sa odrzucane; nie ma CORS. Klient ignoruje systemowy proxy i redirecty.
- Token powstaje przez `secrets.token_urlsafe(32)` przy kazdym starcie.
  Plik `token` i `endpoint.json` maja POSIX 0600 albo chroniona Windows DACL
  z pojedynczym wpisem dla wlasciciela. Katalogi stanu/profilu maja POSIX 0700
  albo dziedziczona Windows DACL tylko dla wlasciciela. Awaria ustawienia
  uprawnien przerywa start; klient odrzuca nieprywatny plik tokenu.
  Token nie trafia do stdout, stderr, argumentow procesu ani access logu.
- Trwaly profil jest sekretnym stanem: moze zawierac cookies i sesje logowania.
  Nigdy nie udostepniaj katalogu stanu modelowi ani nie wskazuj swojego
  codziennego profilu Chrome. Serwer nie eksportuje cookies i nie udostepnia
  dowolnego JavaScript, systemu plikow, uploadow ani schowka.
- `navigate` dopuszcza wylacznie HTTP/HTTPS bez danych logowania w URL.
  Allowlista to dokladne hosty, bez wildcardow i portow; dotyczy tez
  ramek i zasobow HTTP. Niezaleznie od allowlisty, domyslnie blokowane sa
  literalne adresy niepubliczne: loopback, RFC1918, CGNAT `100.64.0.0/10`,
  link-local, `0.0.0.0/8`, IPv6 `::1`, `fc00::/7`, `fe80::/10`, a takze
  adresy zastrzezone/multicast i nazwy `localhost`/`*.localhost`.
  Formy IPv4 dziesietne, hex, octal, skrocone oraz IPv4-mapped IPv6 i hosty
  zakodowane procentowo sa normalizowane przed porownaniem.
  Osobna, domyslnie pusta `--allow-private-host HOST[:PORT]` jest jawnym
  wyjatkiem wlasciciela. Wpis z portem dopuszcza tylko ten port; wpis bez
  portu dopuszcza wszystkie porty danego hosta. IPv6 z portem wymaga nawiasow,
  np. `[::1]:8000`. Wyjatek nie omija aktywnej `--allow-host` i nie jest
  dostepny w argumentach HTTP/MCP. Preferuj wpis z konkretnym portem.
  Kazde zadanie HTTP oraz kazdy hop redirectu przechodzi te same kontrole.
  Chromium/Playwright omija route po `continue_` dla redirectow, dlatego
  serwer pobiera odpowiedzi pojedynczo (`max_redirects=0`), rozwiazuje
  `Location` wzgledem biezacego URL i sprawdza cel przed kolejnym pobraniem.
  Lancuch ma najwyzej 20 redirectow i laczny timeout 30 s; blad przerywa go.
  Redirect dokumentu przerywa pierwotna nawigacje i ponawia ja pod
  sprawdzonym adresem docelowym. Kazda odpowiedz z ponowienia jest sprawdzana
  ponownie: zmiana `Location` nie omija polityki. Dotyczy tez klikniec,
  nawigacji skryptowych i ramek. Dokument otrzymuje rzeczywisty koncowy URL,
  origin i secure context (takze HTTP -> HTTPS); wzgledne adresy i formularze
  rozwiazuja sie wzgledem celu. `navigate` zwraca koncowy `page.url`.
  Metoda/body redirectow 307/308 sa zachowane przy pobraniu; 301/302 dla POST
  i 303 zmieniaja metode na GET. Ponowienie przez `frame.goto` nie zachowuje
  pelnej natywnej historii redirectow ani POST przy recznym reloadzie.
  Tylko zasoby niedokumentowe pobieraja caly sprawdzony lancuch wewnatrz
  handlera i otrzymuja odpowiedz koncowa pod adresem poczatkowym zasobu.
  Odrzucone zadania sa anulowane jako `aborted`, bez zlecania Chromium
  strony bledu `blockedbyclient`, aby kolejna nawigacja nie konkurowala
  z jej opoznionym zaladowaniem.
  Service workers i WebSockets sa blokowane.
  Klikniecie linku do `file:`, `data:`, `chrome:` lub obcego hosta jest odrzucane.
  Brak `--allow-host` dopuszcza publiczne hosty HTTP/HTTPS; nie wylacza
  domyslnej blokady adresow prywatnych.
  Ryzyko resztkowe SSRF / DNS rebinding: kontrola normalizuje literalne
  adresy i nazwy localhost, ale nie rozwiazuje ani nie przypina DNS.
  Inna nazwa hosta moze rozwiazac sie do prywatnego IP od poczatku lub
  zmienic odpowiedz DNS na prywatna po sprawdzeniu URL. Allowlista hostow
  i prywatne wyjatki nie sa firewallem. Dla nieufnych domen wymagaj
  izolacji sieciowej/egress blokujacej prywatne zakresy poza procesem.
- Kazda przechwycona odpowiedz HTTP ma limit 8 MiB danych w transporcie
  oraz 8 MiB po dekompresji gzip/deflate/brotli, niezaleznie od `Content-Length`.
  Lokalny `bounded_network.cjs`, preloaded w sterowniku Playwright, przerywa
  strumien przed przekazaniem nadmiarowych bajtow do bufora `route.fetch()`.
  Zbyt duzy zadeklarowany `Content-Length` przerywa odpowiedz po naglowkach;
  brak naglowka, chunked, media i attachment podlegaja licznikowi strumienia.
  Limit dotyczy kazdego hopu; blad anuluje zadanie, bez czesciowej tresci.
  Duze legalne strony/zasoby beda zablokowane. Jest to limit jednej odpowiedzi,
  nie lacznej pamieci/czasu wszystkich zasobow strony; nie zastepuje izolacji
  procesu ani limitow zasobow OS. Dzialanie shimu nalezy ponownie sprawdzic
  testami po aktualizacji sterownika Playwright.
- Tresc stron moze zawierac prompt injection. Wynik `text` ma zawsze pole
  `untrusted_content`; instrukcje ze strony nie przyznaja uprawnien.
  Nie przekazuj tokenu ani innych sekretow w polach strony.
- Chromium korzysta z jego sandboxa. Pobierania sa wylaczone przez
  `accept_downloads=False` i CDP `Browser.setDownloadBehavior=deny`.
  Operacje na polach plikow sa odrzucane, a file chooser nie otwiera okna
  systemowego. Init script usuwa API `navigator.clipboard` z kazdego dokumentu
  i blokuje `execCommand` dla copy/cut/paste. `--deny-permission-prompts`
  uniemozliwia odpowiedz na pytania o uprawnienia, takze podczas headed takeover.
  To granica API, nie izolacja VM; nie gwarantuje konkretnych stanow uprawnien
  raportowanych wewnetrznie przez Chromium.
  Jedna zarzadzana strona przypada na profil; nowe popupy sa zamykane.
  Operacja czeka na zamkniecie dodatkowych kart przed odpowiedzia i zwolnieniem
  blokady, takze po bledzie interakcji; sam callback zdarzenia nie jest bariera.
- To granica API i osobny profil, nie izolacja VM. Zlosliwy proces tego samego
  uzytkownika, administrator lub exploit Chromium moze ominac te granice.
  `click` i `type` moga wywolac efekty biznesowe zalogowanej strony; uzywaj
  syntetycznych kont i stron testowych bez osobnej zgody na rzeczywiste efekty.
  Headed takeover umozliwia czlowiekowi reczna obsluge przegladarki poza API.

## Stan i cykl zycia

Domyslnie `~/.agents/browser-worker/`; `BROWSER_WORKER_HOME` albo `--home`
nadpisuje katalog. `profiles/<nazwa>/` zachowuje cookies po zamknieciu i restarcie.
Nazwy: `[a-z0-9-]{1,32}`. `server.lock` blokuje dwa serwery dla tego samego
stanu. `endpoint.json` zawiera adres, PID i tryb, bez tokenu. Po zatrzymaniu
metadane pozostaja, ale `call` nie uruchamia serwera; zwraca blad polaczenia.

Serwer obsluguje zadania HTTP w watkach; Playwright ma wlasna petle zdarzen.
Operacje sa serializowane, aby zachowac limity stron i brak kolizji profilu.
Domyslny limit to 4 profile, maksymalny 32. Nawigacja i interakcje maja timeout
30 s; klient czeka najwyzej 45 s. JSON wejscia: najwyzej 64 KiB.

Python uruchamia sterownik Playwright z `CREATE_NO_WINDOW` na Windows.
Lokalny `hide_children.cjs`, zaladowany w zainstalowany sterownik przez
`NODE_OPTIONS`, wymusza `windowsHide: true` na jego potomkach, w tym headless Chromium.
Przy jawnym `--headed` jedyny wyjatek to GUI `chrome.exe` z argumentem
profilu (bez konsoli); sterownik i procesy pomocnicze nadal sa ukryte.
`bounded_network.cjs` jest ladowany w tym samym sterowniku na kazdym systemie.
Zastepuje ewentualne hostowe `NODE_OPTIONS` tylko w srodowisku sterownika.
Brak harmonogramu, autostartu, zmian MCP konfiguracji i instalacji skilli.

## CLI

Ponizsze argumenty nalezy przekazac do istniejacego ukrytego launchera.
Samo polecenie `serve` dziala do zatrzymania procesu i wypisuje jeden JSON
z adresem przy starcie. Port 0 wybiera wolny port; mozna podac staly port.

```text
python global/tools/browser-worker/browser_worker.py serve --port 0 --allow-host 127.0.0.1 --allow-private-host 127.0.0.1:8000
python global/tools/browser-worker/browser_worker.py call open --profile demo
python global/tools/browser-worker/browser_worker.py call navigate --profile demo --url http://127.0.0.1:8000/
python global/tools/browser-worker/browser_worker.py call text --profile demo
python global/tools/browser-worker/browser_worker.py call screenshot --profile demo
python global/tools/browser-worker/browser_worker.py call type --profile demo --selector "#field" --text "synthetic"
python global/tools/browser-worker/browser_worker.py call click --profile demo --selector "#echo"
python global/tools/browser-worker/browser_worker.py call close --profile demo
```

`--allow-host` i `--allow-private-host` mozna powtarzac. Pomijanie pierwszej
usuwa ograniczenie publicznych hostow; pomijanie drugiej blokuje prywatne cele.
`--max-pages 2` zaweza limit. Klient odczytuje token sam, bez kopiowania go
do terminala. Wynik CLI: `{"ok":true,"result":...}` albo
`{"ok":false,"error":{"code":"..."}}` i kod wyjscia 1.

Przyklad jawnego startu w tle na Windows przez stdlib (uruchomiony przez
ukryty host, metadane w prywatnym katalogu stanu; nie dodaje zadania autostartu):

```python
import subprocess
import sys
from pathlib import Path

tool = Path("global/tools/browser-worker/browser_worker.py").resolve()
child = subprocess.Popen(
    [sys.executable, str(tool), "serve", "--allow-host", "127.0.0.1",
     "--allow-private-host", "127.0.0.1:8000"],
    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
    stderr=subprocess.DEVNULL,
    creationflags=subprocess.CREATE_NO_WINDOW,
)
# Adres i PID mozna odczytac z endpoint.json; nie otwieraj pliku tokenu.
```

## HTTP

`POST /call`, `Content-Type: application/json`,
`Authorization: Bearer <token>`, `Host: 127.0.0.1:<port>`.
Przyklad body: `{"operation":"navigate","arguments":{"profile":"demo","url":"http://127.0.0.1:8000/"}}`.
Nie ma endpointow odczytu plikow, cookies, sekretow, dowolnego eval ani uploadu.
Profil musi byc otwarty przed nawigacja. `close` nie kasuje profilu.

`text` zwraca widoczny tekst (bez ukrytych elementow) jako
`{"untrusted_content":{"text":"...","truncated":false}}`.
Limit: 64 KiB UTF-8, bez uszkadzania znakow wielobajtowych. Limit jest
stosowany w przegladarce przed transportem tekstu do Python.

`screenshot` zwraca `{"mime_type":"image/png","base64":"..."}` dla viewportu
1280 x 720. Zarowno PNG, jak i jego base64 musza miescic sie w 2 MiB;
przekroczenie daje HTTP 413 `screenshot_too_large`, bez obcietego PNG.
`click` i `type` przyjmuja tylko selektory CSS; `type` wypelnia pole tekstem
do 16 KiB UTF-8. Pozostale bledy maja statyczne kody bez tresci strony.

## MCP stdio

```text
python global/tools/browser-worker/browser_worker.py mcp
```

Proxy do juz uruchomionego serwera, nie uruchamia Chromium ani HTTP.
Reczny JSON-RPC 2.0: jedna wiadomosc na linie, bez pakietu `mcp`.
Obsluguje `initialize`, `notifications/initialized`, `tools/list`, `tools/call`
i `ping`. Narzedzia: `open`, `navigate`, `text`, `screenshot`, `click`, `type`,
`close`; takie same argumenty jak API HTTP. Stdout zawiera tylko JSON-RPC.

```json
{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"local","version":"1"}}}
{"jsonrpc":"2.0","method":"notifications/initialized"}
{"jsonrpc":"2.0","id":2,"method":"tools/list"}
{"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"open","arguments":{"profile":"demo"}}}
{"jsonrpc":"2.0","id":4,"method":"tools/call","params":{"name":"text","arguments":{"profile":"demo"}}}
```

Tekst pozostaje w `untrusted_content` wewnatrz bloku tekstowego MCP.
Screenshot jest blokiem obrazu MCP `image/png`. Bledy operacji maja
`isError:true`; bledy protokolu maja standardowe kody JSON-RPC.

## Jawne przejecie przez czlowieka

1. Zatrzymaj dotychczasowy serwer, aby zamknal konteksty i zwolnil blokade.
2. Na jawne polecenie wlasciciela uruchom `serve --headed` z tym samym
   `BROWSER_WORKER_HOME` i allowlista. Podprocesy nadal nie tworza konsoli;
   widoczne jest tylko zamowione okno Chromium po `call open --profile demo`.
3. Po zakonczeniu zatrzymaj serwer i uruchom zwykle `serve` dla headless.

`headed` jest wylacznie argumentem startowym serwera. Narzedzia HTTP/MCP
nie moga zmienic trybu ani uruchomic widocznego okna dodatkowym argumentem.
Token zmienia sie przy kazdym starcie; klienci odczytuja aktualny plik.
Nigdy nie otwieraj tego samego profilu w dwoch procesach rownoczesnie.

## Testy i rollback

```text
python -m pytest -q global/tools/browser-worker/tests
```

Testy uzywaja wylacznie syntetycznej strony HTTP na `127.0.0.1`, profili
w unikalnym katalogu `BROWSER_WORKER_TEST_ROOT` (domyslnie
`~/.agents/browser-worker-tests/`) i juz zainstalowanego Chromium. Nie pobieraja
zadnych danych ani nie instaluja zaleznosci. Pokrywaja auth, bind, ACL,
  schematy URL, prywatne IP/nazwy/porty i aliasy numeryczne,
  allowliste/redirecty/zasoby, tekst i screenshot, interakcje,
blokady download/upload/clipboard, popupy, limit stron, restart/cookies i MCP.
Nowe regresje dokumentow sprawdzaja koncowy URL/origin, rozdzielenie
localStorage, wzgledne URL/formularze i secure context po HTTP -> HTTPS
(transport testowy nadal uzywa tylko HTTP na 127.0.0.1, bez certyfikatow).
Testy limitu sprawdzaja rzeczywisty klient HTTP zainstalowanego sterownika
Node: Content-Length, jego brak, chunked, attachment i gzip/deflate/brotli.
Na potrzeby uruchomienia w worktree ustaw `BROWSER_WORKER_TEST_ROOT` na
ignorowane `global/tools/browser-worker/tests/.runtime/`; artefakty pozostaja
do inspekcji. Testy nie usuwaja profili z poprzednich przebiegow.
Sandbox Windows moze odmowic dostepu do owner-only katalogu lub potokow
sterownika; taki blad nie jest dowodem przejscia testow Chromium.

Rollback: zatrzymaj tylko PID wlasnego serwera po sprawdzeniu polecenia
procesu. Zatrzymaj jego proxy MCP. Nie ma konfiguracji/autostartu do cofania.
W razie zmiany kodu przywroc poprzednia wersje przez `git restore`/`git revert`.
Przywracaj `browser_worker.py` i jego shimy razem z tego samego zestawu.
Zachowaj katalog profili i tokenu; serwer nie ma komendy kasowania danych.
Usuniecie prywatnego stanu wymaga osobnej jawnej zgody wlasciciela.
