# Organigram agentow: stanowiska, modele, dzialy, autonomia (Fala 1, 2026-10-21)

Referencja, nie nowa bramka i nie obowiazkowy krok. Zrodla: `references/model-catalog.json`,
`docs/org-fala1-20261021/CEL-NADRZEDNY.md`, `docs/org-fala1-20261021/DECYZJE-WLASCICIELA-20261021.md`.
Nie wymyslono tu zadnego modelu, skilla ani reguly - wstepne sa wylacznie zapisy w tych plikach.

## 1. Stanowiska

| Klasa | Stanowisko | Odpowiedzialnosc (katalog) | Typowa praca | Czego NIE robi |
|---|---|---|---|---|
| D (rank 4) | Dyrektor | plan calosci, rozstrzyganie sporow, odbior pracy Managerow | planuje calosc, rozstrzyga spory, odbiera prace Managerow, wykonuje decyzje techniczne wg T2 | nie jest domyślnym wykonawca rutynowej pracy; nie podejmuje decyzji biznesowych (tylko Mateusz) |
| M (rank 3) | Manager | plan podgrafu, precyzyjne zlecenia, odbior, trudne wezly | planuje podgraf, wydaje precyzyjne zlecenia, odbiera, rozwiazuje trudne wezly | nie planuje calej organizacji, nie rozszyga sporow o calej organizacji |
| K (rank 2) | Kierownik | wykonanie i odbior wymiennie, glowny kon roboczy | wykonuje, odbiera wymiennie, glowny kon roboczy | nie planuje podgrafu, nie jest ostatecznym odbiorem calej pracy |
| W (rank 1) | Worker | wykonanie dobrze okreslonej pracy | wykonuje dobrze okreslona prace | nie planuje, nie rozstrzyga sporow, nie odbiera pracy innych |

Zasada: **najnizsza wystarczajaca klasa; drozsza klasa wymaga powodu w TODO.**
Nizsza klasa znaczy tansza i szybsza realizacja; uzasadnienie wyzszej klasy zapisuje sie w zadaniu.
W katalogu dzialaja tez: konserwacja rodowodow anthropic i openai ponizej Dyrektora (kara 8),
kara za klase wyzsza niz potrzebna (5), kara za model nieklasyfikowany (10), okno swiezosci 168 h
(z polowtrwania 48 h, maks. kara 15), prog JEV 0.004 USD/dolnosc, kolejnosc eskalacji M -> D.
Wylacznie z katalogu: **zasada preferencji M przed D** - najpierw kwalifikujacy sie model klasy Manager,
D tylko gdy zaden Manager nie sie nie kwalifikuje (decyzja wlasciciela 2026-09-29).

## 2. Modele aktywne w katalogu

Kazdy aktywny model z `model-catalog.json` wystepuje dokladnie raz. Role w kolumnie to role z katalogu
(`plan`/`review`/`execute`); kolumna "uwagi" to dosłowne uwagi/slabe strony z katalogu.
Kolumna "Agent (profil)" podaje agenta z katalogu; model profilu jest taki sam jak identyfikator modelu,
z wyjatkiem wiersza Gemini 3.8 Flash, gdzie profil wymienia dodatkowo drugi model (patrz sekcja 3).

### Klasa D (Dyrektor)

| Model | Rodowod | Agent (profil) | Role | Uwagi |
|---|---|---|---|---|
| gpt-6-astra (GPT 6 Astra) | openai | codex | plan, review, execute | planowanie calosci, rozstrzyganie sporow, rygor odbioru; najdrozszy, wolniejszy |
| claude-fable-5-1 (Fable 5.1) | anthropic | claude | plan, review, execute | architektura, synteza, bezpieczenstwo; najdrozszy; adapter Fleet Claude za bramka anthropic-retention |
| k3.1-pro (Kimi K3.1 Pro) | moonshot | kimi | plan, review, execute | Artificial Analysis Intelligence 55 (2026-10-24), odbior, kodowanie Python; bardziej tokenochlonny niz K3, male okno 5 h, kontekst 256k |

### Klasa M (Manager)

| Model | Rodowod | Agent (profil) | Role | Uwagi |
|---|---|---|---|---|
| claude-opus-5-5 (Opus 5.5) | anthropic | claude | plan, review, execute | planowanie wezlow, odbior; subskrypcja Claude do oszczedzania |
| gpt-6-sol (GPT 6 Sol) | openai | codex | plan, review, execute | trudne kodowanie, integracja; subskrypcja Codex do oszczedzania |
| qwen3.8-max (Qwen 3.8 Max) | alibaba-qwen | qwen | plan, review, execute | architect-level, dlugi kontekst; dotad naduzywany do wszystkiego (4198 wywolan we wrzesniu); runtime alias qwen3.8-max, datowany snapshot tylko jako proweniencja |
| gemini-4-argon-high (Gemini 4 Argon) | google | antigravity | plan, review, execute | dlugi kontekst, przeglad duzych repo, knowledge work, multimodal; praca terminalowa slabsza niz Opus/Astra (dane producenta), dlugi prompt w argv (Antigravity), brak lokalnego benchmarku |
| mimo-v2.6-pro (MiMo V2.6 Pro) | xiaomi | mimo | plan, review, execute | kodowanie; AA Intelligence 46 (powyzej Qwen Max 45 i K3 44); najtanszy koszt/zadanie w AA ($0.13); limit krokow natywnych, wolny ~42 tok/s - nie do pilnych sciezek |

### Klasa K (Kierownik)

| Model | Rodowod | Agent (profil) | Role | Uwagi |
|---|---|---|---|---|
| grok-4.7 (Grok 4.7) | xai | grok | review, execute | rzetelny odbior bezpieczenstwa, wzgledne zapisy plikow; limit narzedzi ACP, nowe pliki dopiero od 0.9.52 |
| muse-spark-1.3 (Muse Spark 1.3) | meta | muse | review, execute | kodowanie, szybki; sandbox Windows (ACL), workspace poza evidence w przyszlosci |
| qwen3.8-flash (Qwen 3.8 Flash Next) | alibaba-qwen | qwen | review, execute | rzetelny odbior, ograniczona praca srednia; implement wymaga Dockera |
| hy4-preview (Hy4 Preview) | tencent | codebuddy | review, execute | inzynieria systemowa; kredyty CodeBuddy |
| glm-5.3 (GLM 5.3) | zhipu | opencode | review, execute | odbior, tani; opencode edit/bash deny w czesci profili |
| gemini-3.8-flash-high (Gemini 3.8 Flash Max) | google | antigravity | review, execute | szybki, szeroki kontekst; dlugi prompt w argv (Antigravity) |

### Klasa W (Worker)

| Model | Rodowod | Agent (profil) | Role | Uwagi |
|---|---|---|---|---|
| gpt-6-luna (GPT 6 Luna) | openai | codex | execute, review | szybkie dobrze okreslone zadania; subskrypcja Codex do oszczedzania |
| deepseek-v4-flash-0731 (DeepSeek V4 Flash) | deepseek | qwen | execute, review | tani; poprzednik V4.1 Flash; zastepuje go kandydat DeepSeek V4.1 Flash |
| glm-5.3-flash (GLM 5.3 Flash) | zhipu | opencode | execute, review | tani odbior read-only; timeouty przy duzych odbiorach |
| MiniMax-M3 (MiniMax M3) | minimax | minimax | execute, review | kon roboczy, 1M kontekst, ~1.7B tok/mies. w planie Plus do 2026-10-25; Office tylko relay; retire_after 2026-10-25 |
| vllm/Qwen3.8-27B (OpenClaw lokalny Qwen 3.8 27B) | alibaba-qwen | openclaw-local-qwen | execute | zero kosztu providera; lokalny sprzet |

### Kandydat (nie aktywny, bez przypisania do pracy)

| Model | Klasa | Rodowod | Agent (profil) | Uwagi |
|---|---|---|---|---|
| deepseek-v4.1-flash (DeepSeek V4.1 Flash) | W | deepseek | qwen | lifecycle `candidate`; bardzo tani, dobry kon roboczy; profil Fleet dopiero w 0.9.52 (Codex); supersedes DeepSeek V4 Flash; natywnie potwierdzony 2026-09-29 przez Token Plan Singapore |

## 3. Modele DENY i deprecated (bez przypisania do pracy)

| Model | Rodowod | Agent (profil) | Powod odmowy (z katalogu) |
|---|---|---|---|
| sonnet (Claude Sonnet 5.5) | anthropic | claude | koszt klasy Dyrektora przy nizszej jakosci (owner 2026-09-29) |
| deepseek-v4-pro (DeepSeek V4 Pro) | deepseek | qwen | drozszy i slabszy niz V4.1 Flash (owner 2026-09-29) |
| gpt-5.6-terra (GPT 5.6 Terra) | openai | codex | bez sensu dzis; ocena ponownie przy wersji 6 (owner) |
| gpt-5.6-sol (GPT 5.6 Sol) | openai | codex | starsza generacja niz GPT 6 Sol w tej samej subskrypcji |
| gpt-5.6-luna (GPT 5.6 Luna) | openai | codex | starsza generacja niz GPT 6 Luna w tej samej subskrypcji |
| gemini-3.1-pro-high (Gemini 3.1 Pro) | google | antigravity | starsza generacja niz Gemini 3.8 w tej samej subskrypcji |
| gemini-3.8-flash-medium (Gemini 3.8 Flash medium/low) | google | antigravity | wlasciciel wskazal Gemini 3.8 Flash Max; nizsze poziomy wysilku nie sa osobnymi wykonawcami; profil tego wiersza wymienia dodatkowo model antigravity gemini-3.8-flash-low |

Dodatkowo katalog odcina wzorce modeli (`deny_model_patterns`): nazwa zawierajaca "Sonnet",
generacje Grok 4.0-4.6, generacje Muse Spark 1.0-1.2, DeepSeek V4 Pro, generacje GPT 5.x oraz
kazdy model oznaczony "contributor" (model treningowy/udostepniania danych - twarda regula).
Wszystkie powyzsze sa wylacznie wyliczanka do odrzucenia - nie dostaja zadan.

Uwagi o opt-out i wycofaniu: katalog nie zawiera pola `opt-out` ani `VERIFIED_OPTOUT` dla aktywnych modelow.
Slowo VERIFIED_OPTOUT wystepuje tylko w regule `policy.retirement`: nastepnik przejmuje domyslne
routing po 20 lokalnych zadaniach bez regresji i z VERIFIED_OPTOUT, poprzednik pozostaje aktywny
w niskim priorytecie przez 14 dni, a usuniecie modelu z katalogu wymaga zgody wlasciciela.
Katalog nie zawiera tez statusu "UNVERIFIED" dla aktywnych modeli - nie dopisywano go.
Poza katalogiem (nie wymienione w `model-catalog.json`, wiec nie sa modelami katalogu):
MiniMax M3.1 Flash, Haiku 5.5, Luna 6 i 6.1, Mercury 3 - wzmianki z CEL-NADRZEDNY.

## 4. Dzialy, stanowisko wlasciciela i skille

Nazwy skilli ponizej to istniejace katalogi w `global/codex/skills/` - tylko nazwy, bez wymyslania
nowych. Skille poza `global/codex/skills/` (np. `email` z decyzji T6) nie sa tu wymieniane, bo
w tym katalogu nie istnieja.

| Dzial | Stanowisko wlasciciela (z DECYZJE) | Istniejace skille w `global/codex/skills/` |
|---|---|---|
| Inzynieria | decyzje techniczne - agenci, po akceptacji modelu klasy Dyrektor | design-code-architecture, ml-architecture-selection, systematic-debugging, test-first-engineering, search-code, accept-system, security-audit, model-audit, wordpress-engineering, data-governance, codex-security-cli (DEPRECATED) |
| Stack / RSI | naprawy helpdesku (bledy, bez zmiany logiki biznesowej) - agenci, takze PROD, ale z wieksza kontrola przebudowy PROD | rsi-helpdesk, start-stack, ai-infrastructure-audit, remote-computer-use, webmcp-site-tools, hermes-delegation, cloud-org-operator, cloudflare-operator, ngrok-operator |
| QA / UAT | odbior pracy: agenci sami na DEV/TEST; UAT w przegladarce jako test jak czlowiek | accept-system, skill-ab-testing, test-first-engineering, webmcp-site-tools, remote-computer-use, skill-auditor |
| Wiedza (Hub) | wiedza w Hubie jest zrodlem kontekstu wraz z potwierdzeniem odbioru (context receipt); pakiet to dane bez zgody | search-skills, select-skills, rag-engineering, ml-recon, skill-auditor, skill-evolution, new-training |
| Marka i marketing | zakres poza kodowanie: marka osobista, marketing; kampanie - strategia bez automatycznej zmiany budzetow i ruchu | marketing-context, google-ads-strategy, meta-ads-strategy, search-visibility, market-strategy-research, growth-experiments, idea-maker, business-capabilities |
| Sprzedaz | decyzje biznesowe (zmiana logiki, nowy modul) - wylacznie Mateusz | business-capabilities, idea-maker, growth-experiments, operations-playbook - brak dedykowanego skilla sprzedazy w tym katalogu |
| Finanse | sekrety - na razie Mateusz; dokumenty podatkowe wg aktualnych wymogow PL | pl-tax-compliance, fundraising, review-investor-materials, data-governance |

## 5. Macierz autonomii T2 (z DECYZJE, bez zmian tresci)

| Dzialanie | Kto decyduje |
|---|---|
| Testy i wdrazanie na DEV/TEST | agenci sami |
| Decyzje techniczne | agenci, po akceptacji modelu klasy Dyrektor |
| Analizy i naprawy z helpdeskow (bledy, bez zmiany logiki biznesowej) | agenci; takze PROD, ale z wieksza kontrola przebudowy PROD |
| Przebudowa PROD dla takiej naprawy, Mateusz w trybie **OUT** | zgoda modelu klasy Dyrektor |
| Przebudowa PROD, Mateusz w trybie **IN** | zgoda Mateusza |
| Decyzje biznesowe: zmiana logiki, nowy modul, "koncert zyczen" z helpdeskow | **wylacznie Mateusz** |
| Sekrety | na razie Mateusz |
| Usuniecie jakiegokolwiek pliku z dysku | **zawsze Mateusz, za kazdym razem**; agent nigdy nie usuwa sam. Jedyny wyjatek (potwierdzony 21.10): wlasne pliki **tymczasowe** utworzone przez agenta w tym zadaniu, w jego katalogu roboczym - nie rezultaty, nie kopie, nie cudze pliki |
| Kontakt z ludzmi | zgoda Mateusza |

Granica ta jest wazna takze dla Dyrektora: klasa Dyrektor daje zgode techniczna, nie zgode biznesowa
i nie zgode Mateusza na sekrety, produkcje w trybie IN, kasowanie plikow czy kontakt z ludzmi.
Przyklad hipotetyczny: zmiana oznaczania blednych lub brakujacych umow w aplikacji
moze ukryc problem zamiast go rozwiazac. Zmiana takiej reguly biznesowej wymaga decyzji wlasciciela.

## 6. OpenClaw (straznik) vs Dyrektor (rozdziela i nadzoruje)

| Rola | Odpowiedzialnosc (z DECYZJE T3) | Czego NIE robi |
|---|---|---|
| OpenClaw - straznik wykonania (24/7) | pilnuje, zeby zadania byly wykonywane; gdy zadanie stoi, daje sygnal przez Office do narzedzi desktop; dzieki przypisaniu zadanie -> watek wie, ktory watek wybudzic | nie rozdziela pracy |
| Model klasy Dyrektor - w wybudzonym watku | rozdziela zadania, dobiera grafy, nadzoruje ich wykonanie | nie jest straznikiem 24/7; sam nie zgadza sie na decyzje biznesowe |

Swiadomosc projektu (T3): watek pracujacy nad projektem widzi otwarte zadania tego projektu z tablicy
(np. nowe z maili) i pyta, czy je wykonac. W trybie OUT Dyrektor moze zdecydowac "wykonujemy",
a Mateusz po powrocie dostaje informacje o wykonaniu. Granice T2 obowiazuja - zmiana logiki to pytanie
do Mateusza. Rozwijane jest budzenie narzedzi desktop przez Office (Claude, Codex, Antigravity),
aby kierownik zadania mogl zaangazowac kilka zespolow desktop; jeden zespol jest dominujacy
i zarzadza calym procesem.

## 7. Granice (CEL-NADRZEDNY)

- Sekrety, kasowanie, produkcja, kontakt z ludzmi i platne koszty nadal wymagaja zgody Mateusza w czacie.
- Zakaz uczenia na danych Mateusza, ochrona danych klientow, niezalezny odbior innego rodowodu, brak okien terminala.
- Autonomia rosnie przez lepsze narzedzia i jawne zgody, nie przez ich obchodzenie.
- Ten dokument niczego nie przyznaje: jest opisem stanu katalogu i decyzji Mateusza z 2026-10-21.
