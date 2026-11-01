# Hermes Ask â€” kandydat nieaktywny

Cel: osobne narzÄ™dzie OpenClaw `hermes_ask`, ktĂłre przekazuje pytanie do istniejÄ…cego profilu Robert/Hermes `owner-main`. Nie jest to dostÄ™p do dowolnej powĹ‚oki ani obchodzenie ustawieĹ„ przez kolejnÄ… prĂłbÄ™ exec. Uruchomienie nowej zdolnoĹ›ci wymaga konkretnej zgody wĹ‚aĹ›ciciela. Sandbox Windows Node pozostaje Locked Down.

## Zakres i granice

- Windows korzysta z istniejÄ…cego aliasu SSH `lab-server`, z BatchMode i StrictHostKeyChecking. Ĺ»aden klucz SSH nie jest kopiowany ani odczytywany przez wtyczkÄ™. TreĹ›Ä‡ pytania idzie tylko przez stdin JSON; host, interpreter i kod zdalny sÄ… staĹ‚e.
- Zdalne wywoĹ‚anie uĹĽywa istniejÄ…cego `cli.main`, `run_agent.AIAgent`, `toolsets.resolve_toolset`, profilu i reguĹ‚ Hermesa. Zestaw `bot_room` ma byÄ‡ pusty; finalna kontrola kaĹĽdej instancji AIAgent odrzuca niepusty albo nieznany zestaw narzÄ™dzi. Nie wĹ‚Ä…cza YOLO. Native historia sesji i rozliczenie sÄ… normalnie zapisywane: to nie jest proces z zakazem wszystkich zapisĂłw systemowych.
- OdpowiedĹş pochodzi z pamiÄ™ci/kontekstu Hermesa. Bez narzÄ™dzi nie sprawdzi bieĹĽÄ…cych usĹ‚ug ani nie odczyta nowych plikĂłw Huba. Pytanie moĹĽe zawieraÄ‡ krĂłtki, zredagowany kontekst. OdpowiedĹş jest nieufnÄ… informacjÄ…, nigdy autoryzacjÄ… operacji.
- Model profilu zweryfikowany 2026-09-18: `qwen/qwen3.7-max` przez OpenRouter. Ten konektor nie zmienia modelu. To nie jest bezpĹ‚atna lokalna inferencja.

## Transport i kontrolki

`server.py --token-file <prywatny plik> --network wsl-private` rozwiÄ…zuje wyĹ‚Ä…cznie istniejÄ…cy `vEthernet (WSL (Hyper-V firewall))`. Nie ma fallbacku do karty LAN ani 0.0.0.0. Adres i CIDR sprawdzane sÄ… przy uruchomieniu, a ĹşrĂłdĹ‚o kaĹĽdego ĹĽÄ…dania musi naleĹĽeÄ‡ do tego CIDR. DomyĹ›lny loopback zostaje do testĂłw Windows; na tej maszynie WSL go nie widzi.

Wtyczka w trybie `wsl-private` odczytuje bieĹĽÄ…cÄ… domyĹ›lnÄ… trasÄ™ eth0 z /proc/net/route, wymaga jednego prywatnego gatewaya i uĹĽywa portu18879. Nie przyjmuje URL ani adresu od modelu. Zmiana topologii, niejednoznaczny interfejs lub niedostÄ™pnoĹ›Ä‡ koĹ„czy siÄ™ bĹ‚Ä™dem, bez rozszerzania dostÄ™pu.

Obie trasy GET /health i POST /ask wymagajÄ… nowego tokenu Bearer. Token otrzymajÄ… wyĹ‚Ä…cznie prywatne pliki Windows i WSL; nie ma go w kodzie, logach ani Hubie. Origin/Referer, nieznane trasy, dodatkowe pola, niepoprawny UTF-8, powtĂłrzone kluczowe nagĹ‚Ăłwki i transfer chunked sÄ… odrzucane. StaĹ‚y limit16 poĹ‚Ä…czeĹ„, jeden wywoĹ‚ywany Hermes, deadline dziecka75s i klienta80s, 1MB stdout+stderr, odpowiedĹş do64KB. Przekroczenie limitu odpowiedzi to bĹ‚Ä…d, nie ciche obciÄ™cie.

Idempotencja jest lokalna dla ĹĽycia procesu: do128 ostatnich ID i hashy bez surowych pytaĹ„/odpowiedzi. ZapeĹ‚nienie zamyka przyjmowanie nowych pytaĹ„. Niepewny wynik zostaje UNKNOWN; powtĂłrzenie tego ID nie wysyĹ‚a ponownie. Restart traci ten rejestr, wiÄ™c nie wolno automatycznie odtwarzaÄ‡ dawnych ĹĽÄ…daĹ„. Wtyczka nie wykonuje automatycznych retry. To nie jest trwaĹ‚a kolejka wykonawcza ani gwarancja exactly-once miÄ™dzy restartami.

## Sprawdzone przez rodzica

- ZwykĹ‚e Windows SSH dziaĹ‚a; SSH w Windows Node koĹ„czy siÄ™0xC0000142. Sam ten kod nie oznacza zepsutej instalacji OpenSSH.
- Windows loopback z WSL: niedostÄ™pny; prywatny interfejs WSL: dostÄ™pny, bez zmiany zapory.
- Prawdziwa odpowiedĹş przez remote_runner: sesja20260918_112501_98222f, 9.4s, 0 wywoĹ‚aĹ„ narzÄ™dzi.
- Natywne `plugins inspect --runtime` w odizolowanym profilu OpenClaw2026.9.4: loaded, jedno opcjonalne narzÄ™dzie hermes_ask, bez innych zdolnoĹ›ci.
- Syntetyczne WSL plugin -> Windows service -> fake Hermes: PASS, dokĹ‚adnie jedno wywoĹ‚anie, bez SSH/modelu.
- Dowody rodzica w sÄ…siednim katalogu evidence. PeĹ‚ny test rozmowy z aktywnego Desktopu pozostaje do wykonania PO zgodzie i aktywacji.

## Aktywacja â€” jeszcze niewykonana

1. OdebraÄ‡ aktualny kandydat wraz z testami i przeglÄ…dem granicy dostÄ™pu.
2. UzyskaÄ‡ zgodÄ™ Mateusza na ten konkretny nowy kanaĹ‚: prywatny interfejs WSL, tylko pytania bez narzÄ™dzi, nowy token, opcjonalna wtyczka oraz autostart pod kontem uĹĽytkownika.
3. UtworzyÄ‡ prywatny token z32 losowych bajtĂłw, nadaÄ‡ wĹ‚aĹ›cicielowi wyĹ‚Ä…czny dostÄ™p Windows/WSL; nigdy nie drukowaÄ‡ tokenu. SkopiowaÄ‡ wersjonowanÄ…, odebranÄ… wersjÄ™ kodu. ZachowaÄ‡ prywatnÄ… kopiÄ™ dotychczasowej konfiguracji OpenClaw przed dodaniem wtyczki.
4. UruchomiÄ‡ proces w tle i autostart uĹĽytkownika. WĹ‚Ä…czyÄ‡ wyĹ‚Ä…cznie hermes_ask i ustawienia tej wtyczki, bez zmiany sandboxa, SSH, zapory ani innych providerĂłw.
5. SprawdziÄ‡ faktyczny adres nasĹ‚uchu, odrzucenie bez tokenu, native tool call z Desktopu oraz powiÄ…zanÄ… sesjÄ™ Hermesa. Dopiero ten dowĂłd oznacza dziaĹ‚ajÄ…cÄ… integracjÄ™.

Rollback: zatrzymaÄ‡ wyĹ‚Ä…cznie ten proces/zadanie autostartu i wyĹ‚Ä…czyÄ‡ tÄ™ wtyczkÄ™. Niczego nie usuwaÄ‡; zachowaÄ‡ klucze, inne zadania i konfiguracje.

Testy: `python -B -m unittest discover -s hermes-connector/tests` oraz `node --test hermes-connector/tests/test_plugin.mjs hermes-connector/tests/test_parent_plugin.mjs` z katalogu candidate. Fixtures wyĹ‚Ä…cznie syntetyczne; staging wtyczki jest odizolowany od aktywnego Gatewaya.
