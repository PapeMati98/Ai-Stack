# Wzorce kontrolne wykonania i izolacji (2026-10, wyciag z audytu ECC)

Ponizsze punkty uzupelniaja audyt semantyczny skilli, narzedzi MCP i subagentow CLI.

## 1. HTTP egress i przekierowania
- **Ochrona przed DNS rebinding**: rozwiaz nazwe hosta, zweryfikuj brak adresow prywatnych/loopback/link-local/metadata (`127.0.0.0/8`, `10.0.0.0/8`, `172.16.0.0/12`, `192.168.0.0/16`, `169.254.0.0/16`, `::1`, `fc00::/7`, `fe80::/10`) i powiaz rzeczywiste polaczenie (`connect`) ze zwalidowanym adresem IP.
- **Re-walidacja przekierowan**: kazdy cel przekierowania (`301`, `302`, `303`, `307`, `308`) musi przejsc pelna walidacje hosta/IP przed wykonaniem kolejnego zadania.
- **Izolacja poswiadczen przy redirectach**: przy przekierowaniu cross-origin bezwarunkowo usun naglowki uwierzytelniajace (`Authorization`, `Cookie`, `Proxy-Authorization`, `X-API-Key` oraz podpisy zadan).

## 2. Strumieniowe ograniczanie `tool output`
- **Limit w locie (`stream byte cap`)**: egzekwuj maksymalny rozmiar wyjscia podczas czytania strumienia przed pelnym buforowaniem w pamieci.
- **Sanityzacja i jawne markery**: usuwaj niebezpieczne sekwencje sterujace terminala (ANSI/OSC/CSI); oznaczaj uciecie lub blad jawnym markerem (`[TRUNCATED]`, `[TOOL_ERROR]`) wraz z `SHA-256` dokladnego zakresu bajtow (`byte range`). Skrot `SHA-256` potwierdza integralnosc wycinka (`slice integrity`), a nie bezpieczenstwo semantyczne tresci.
- **Brak cichego sukcesu**: nie zamieniaj nieudanego wykonania narzedzia (`non-zero exit`, timeout, wyjatek) na pusty ciag znakow traktowany jako sukces.

## 3. Separacja `Reader/Extractor -> Actor` dla nieufnych dokumentow
- W przeplywach wysokiego ryzyka przetwarzajacych nieufne zalaczniki, PDF, HTML lub wiadomosci zewnetrzne oddziel komponent czytajacy (`Reader/Extractor`) od komponentu wykonawczego (`Actor`).
- `Reader/Extractor` nie posiada narzedzi zapisu, wykonania kodu ani otwartego egressu i zwraca wylacznie dane w ograniczonym schemacie.
- Wyjscie `Extractor` pozostaje danymi nieufnymi (`untrusted data`), ktore `Actor` waliduje wzgledem autoryzowanego zadania przed wywolaniem narzedzia z efektem ubocznym (wzorzec architektoniczny dla przeplywow wysokiego ryzyka, nie obowiazkowa dodatkowa para agentow dla kazdego pliku).

## 4. Anomalie sciezek Windows / NTFS i dowiazan
- Sprawdzaj surowe komponenty sciezki (w tym posrednie prefiksy sciezki wzglednej i bezwzglednej) przed normalizacja i przed jakimkolwiek `open`/`stat`/`read`:
  - odrzucaj symlinki, junctions i reparse points (`os.lstat` + `FILE_ATTRIBUTE_REPARSE_POINT`, regula `W015-symlink-reparse`),
  - odrzucaj zarezerwowane nazwy urzadzen Windows (`CON`, `PRN`, `AUX`, `NUL`, `CLOCK$`, `COM1..9`, `LPT1..9`, w tym z rozszerzeniem np. `nul.txt` oraz indeksami `\u00b9\u00b2\u00b3`, regula `W016-windows-path-hazard`),
  - odrzucaj komponenty konczace sie kropka lub spacja (`trailing dot/space`) oraz strumienie NTFS ADS (`:` w nazwie komponentu poza litera dysku).

## 5. Izolacja uruchomien CLI (wzorzec doradczy, stan 2026-10)
- Oceniaj izolacje subagentow CLI w 6 niezaleznych osiach:
  1. **Zapis**: sandbox `read-only` / brak narzedzi zapisu.
  2. **Odczyt**: sam tryb `read-only` w CLI nie ogranicza odczytu do katalogu roboczego; sprawdzaj granice odczytu poza workspace.
  3. **Siec**: blokada lub jawna allowlista polaczen wychodzacych.
  4. **MCP**: zerowanie serwerow MCP (`mcp_servers={}`) dla uruchomien czysto analitycznych lub audytowych.
  5. **Instrukcje**: brak automatycznego dziedziczenia niezaufanych plikow instrukcji z badanego repozytorium (np. start z czystego katalogu tymczasowego).
  6. **Srodowisko**: brak pelnego dziedziczenia zmiennych srodowiskowych (`shell_environment_policy.inherit="none"` lub jawna allowlista zmiennych bez sekretow).
- **Wzorzec `outside-directory sentinel`**: do weryfikacji izolacji odczytu uzywaj syntetycznego pliku-kanarka umieszczonego poza dozwolonym katalogiem roboczym i potwierdzaj, ze izolowany proces nie moze go odczytac. Konkretne flagi CLI weryfikuj wzgledem wersji narzedzia przed egzekwowaniem w runtime.
