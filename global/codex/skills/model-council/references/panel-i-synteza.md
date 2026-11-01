# Rada Modeli - roster i synteza

## Zrodla prawdy

1. `references/roster-policy.json` - wersjonowana polityka rol, rodzin, minimalnych generacji i paneli.
2. `scripts/resolve_roster.py` - deterministyczny resolver oraz circuit breaker.
3. `~/.agents/model-radar` - aktualna charakterystyka rynku i benchmarkow, wazna maksymalnie 7 dni.
4. `~/.agents/model-council/openrouter-model-catalog.json` - uwierzytelniony katalog `models/user`, uwzgledniajacy ustawienia konta i wazny maksymalnie 7 dni.
5. Endpoint `GET /api/v1/models/{author}/{slug}/endpoints` - biezaca dostepnosc wybranego modelu.

Model obecny w katalogu, ale bez aktywnego endpointu, nie jest dopuszczany. Model z endpointem moze nadal zawiesc podczas inferencji; dlatego wynik jest zapisywany w circuit breakerze.

## Algorytm

1. `rada.ps1` wywoluje `model_radar.py check`. Nieaktualny Radar zatrzymuje przebieg przed kosztem.
2. Resolver czyta swiezy cache katalogu albo atomowo pobiera nowy z oficjalnego API.
3. Dla kazdej roli filtruje modele wedlug scislej rodziny, floor czasu dodania, deny-listy, terminu wygasniecia i lokalnej kwarantanny.
4. Wybiera najnowszy zgodny wpis. `preferred_ids` rozstrzyga tylko remis i dokumentuje oczekiwany baseline; nie blokuje przyszlej wersji.
5. Sprawdza aktywne endpointy. Przy awarii kandydata probuje tylko nowszy lub rownorzedny model z tej samej dopuszczonej rodziny, nigdy jawnie zabroniona starsza generacje.
6. Panel rusza, gdy osiagnie minimalny rozmiar. Pominiete role i powod trafiaja do manifestu.
7. Kazdy model odpowiada niezaleznie. Agent prowadzacy odbiera wyniki i robi synteze.

## Role

| Rola | Rodzina | Cel |
|---|---|---|
| `chair` | Claude Fable | calosc planu, dlugi horyzont, sekwencja, rollback, GO/NO-GO |
| `principal` | Claude Opus | drugi gleboki audyt zalozen, dowodow, granic systemu i decyzji najwyzszego rzedu |
| `execution` | Qwen Max | wykonalnosc agentowa, narzedzia, testy, integracje i koszt iteracji |
| `long_context` | Kimi mainline | spojnosci cross-file i cross-system, dlugi kontekst oraz globalne skutki lokalnych decyzji |
| `systems` | Tencent HY mainline | dlugohoryzontowe dowiezienie systemu: plan, narzedzia, artefakty, weryfikacja i utrzymanie |
| `alternative` | GLM mainline | alternatywna architektura i ukryte zaleznosci |
| `correctness` | DeepSeek V4 Flash | edge cases, walidacja i ciche bledy logiczne |
| `independent` | Meta Muse Spark | niezalezna perspektywa innego laboratorium i falsyfikacja zalozen |
| `contrarian` | Grok mainline | atak na najsilniejsze zalozenie i przeciwwaga dla groupthink |
| `red_team` | Nemotron Ultra free | awarie, security, dane, SPOF i rollback |
| `simplicity` | Mistral Medium | prostota, YAGNI i redukcja zakresu |
| `business` | Gemini Flash | prawdziwy cel, wartosc uzytkownika i koszt utrzymania |
| `arbiter` | Fugu Ultra | opcjonalne wazenie sprzecznych sciezek |

## Historyczny snapshot 2026-08-09

To jest dowod migracji, nie konfiguracja do recznego kopiowania. `-ResolveOnly -RefreshRoster` zwrocil:

| Rola | Rozwiazany model | Endpointy |
|---|---|---:|
| chair | `anthropic/claude-fable-5` | 6 |
| principal | `anthropic/claude-opus-5` | 9 |
| execution | `qwen/qwen3.8-max` | 1 |
| long_context | `moonshotai/kimi-k3` | 12 |
| alternative | `z-ai/glm-5.2` | 32 |
| correctness | `deepseek/deepseek-v4-flash-0731` | 24 |
| independent | `meta/muse-spark-1.2` | 1 |
| contrarian | `x-ai/grok-4.5` | 4 |

Przy `MaxTokens=5000` ten domyslny panel raportowal maksymalny koszt samego outputu `0.52861 USD`; input jest liczony osobno. `-Quick` pozostaje tanszym wyborem i raportowal `0.30251 USD` dla tego samego limitu.

Wazne rozstrzygniecia:

- Poprawna nazwa modelu Meta to **Meta Muse Spark 1.2**, slug `meta/muse-spark-1.2`.
- OpenRouter wystawia obecny model Moonshot jako **Kimi K3**, slug `moonshotai/kimi-k3`. Okreslenie `Kimi K3 Max` nie jest osobnym slugiem API i nie wolno go zgadywac w konfiguracji.
- Claude Opus ma osobna role od Claude Fable. Fable przewodniczy i syntetyzuje calosc, a Opus wykonuje drugi gleboki audyt decyzji najwyzszego rzedu.
- `deepseek/deepseek-v4-pro` jest jawnie wykluczony z roli poprawnosci. Obecnosc w katalogu nie kasuje historii niestabilnych wywolan; role przejal nowszy DeepSeek V4 Flash 0731.
- `qwen/qwen3.7-max`, `x-ai/grok-4.3`, `meta/muse-spark-1.1` i `google/gemini-3.5-flash` sa ponizej zaakceptowanych floor i nie moga wrocic jako cichy fallback.
- Domyslny panel zawiera Claude Opus, Kimi, Tencent HY, Meta Muse Spark i Groka. `-Quick` pozostaje oszczedny: zachowuje Meta, ale pomija Opusa, Kimi, HY i Groka; `-Full` dodaje trzy dodatkowe soczewki.

## HY4 Preview - przyjety 2026-08-29

- Kanoniczny slug OpenRouter to `tencent/hy4-preview`; uwierzytelniony katalog konta i endpoint probe potwierdzily jeden aktywny endpoint Tencent bez wywolania inferencji.
- Rola `systems` wykorzystuje mocne strony deklarowane przez Tencent: planowanie, wieloetapowe uzycie narzedzi, ciaglosc kontekstu i weryfikacje pracy inzynierskiej. Nie duplikuje przewodniczacego ani arbitra.
- `HY4 preview` jest wczesna wersja. Tencent wskazuje sklonność do zbyt dlugiego rozumowania i nadweryfikacji, dlatego soczewka wymaga najwyzej trzech materialnych wnioskow.
- Selektor ma floor na HY4 i jawnie blokuje HY3. Nowszy wpis tej samej rodziny moze wejsc dopiero, gdy jest nowszy od floor, widoczny w katalogu konta i ma aktywny endpoint.
- Zrodla: `https://hy.tencent.ai/research/hy4-preview`, `https://huggingface.co/tencent/Hy4-preview`, `https://openrouter.ai/tencent/hy4-preview`.

## Ox Alpha - wycofany alias, zweryfikowano 2026-08-26

- Z.ai oficjalnie ujawnilo, ze anonimowy `stealth/ox-alpha` byl testem GLM-5.3 Flash. Alias zostal wycofany z aktywnego rosteru oraz z panelu niestandardowego, aby ten sam model nie wystepowal pod dwiema tozsamosciami.
- GLM-5.3 Flash jest publicznym modelem i wedlug Z.ai zostal udostepniony wszystkim uzytkownikom Coding Plan. Integracja subskrypcyjna wymaga osobnego potwierdzenia dokladnego runtime ID; rada nie moze po cichu przelaczyc go na platny transport OpenRouter.
- Zrodlo pierwotne: `https://z.ai/blog/glm-5.3-flash`.

## Bledy i kwarantanna

- HTTP 400/401/402/403/404 jest bledem twardym i nie jest ponawiany w tym samym wywolaniu.
- HTTP 408/409/425/429, 5xx i blad sieci sa ponawiane maksymalnie trzy razy z backoffem.
- Pusta odpowiedz jest osobna klasa bledu.
- Dwa kolejne niepowodzenia ustawiaja kwarantanne na 24 godziny. Sukces zeruje licznik.
- Kwarantanna jest stanem lokalnym w `~/.agents/model-council/model-health.json`; zapis jest chroniony blokada miedzyprocesowa i nie modyfikuje skilla.

## Utrzymanie

- Gdy Model Radar jest przeterminowany, agent sam wykonuje jego procedure odswiezenia i wraca do rady.
- Gdy nowa wersja zachowuje nazwe rodziny, resolver wybierze ja po dacie dodania i endpointach. Testy obejmuja przyszle `anthropic/claude-opus-6` i `moonshotai/kimi-k4-max`.
- Automatyzacja jest celowo ograniczona do wersji w zatwierdzonych rodzinach. Nowe laboratorium, nowa rodzina, zmiana schematu slugow albo zmiana charakteru modelu wymaga aktualizacji polityki po swiezym Radarze; sam benchmark nie moze samowolnie powiekszac rady i wydawac pieniedzy.
- Gdy laboratorium zmieni schemat nazwy albo charakter modelu, zaktualizuj selector i floor dopiero po swiezym Radarze, oficjalnym katalogu oraz bezplatnym `-ResolveOnly`.
- Nie dodawaj fallbacku do starszego modelu tylko po to, aby panel mial zielony status.
- Po zmianie uruchom `python -B scripts/test_resolve_roster.py -v`, `python -B scripts/test_brainstorm_protocol.py -v`, `scripts/test_brainstorm_transport.ps1` w Windows PowerShell 5.1 i PowerShell 7, parser PowerShell, walidator skilla oraz live `-ResolveOnly`. `-B` zapobiega kopiowaniu `__pycache__` do runtime'ow. Nie trzeba wykonywac platnej rady do testu rosteru.

## Rubryka syntezy

Pewnosc jest wazniejsza niz liczba glosow, a konkret wazniejszy niz ogolnik. Jednomyslne `GO` bez znalezisk jest sygnalem ostrzegawczym. Finalna odpowiedz powinna zawierac:

- zgodne obawy,
- punkty sporne i rozstrzygniecie,
- najlepsze poprawki,
- odrzucone zarzuty z powodem,
- `GO`, `GO-Z-POPRAWKAMI` albo `NO-GO`,
- `MUST-FIX`, `SHOULD-FIX` i poziom pewnosci.

## Oficjalne API

- `https://openrouter.ai/docs/api/api-reference/models/list-models-user`
- `https://openrouter.ai/docs/api/api-reference/endpoints/list-endpoints`
- `https://openrouter.ai/docs/guides/overview/models`
