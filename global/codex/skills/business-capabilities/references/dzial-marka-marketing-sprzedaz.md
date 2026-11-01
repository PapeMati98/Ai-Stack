# Dział: Marka Osobista, Marketing i Sprzedaż (Personal Brand, Marketing & Sales)

Przewodnik operacyjny (playbook) działu Marki Osobistej, Marketingu i Sprzedaży w ramach prywatnej organizacji agentowej Mateusza. Dokument definiuje przepływ pracy end-to-end, integruje wyspecjalizowane skille, określa twarde bramki zgody właściciela (matryca T2 IN/OUT) oraz zawiera gotowe szablony pakietów pilotażowych.

---

## 1. Architektura Przepływu Pracy End-to-End

Dział łączy specjalistyczne skille w spójny potok realizacyjny — od fundamentów strategicznych, przez widoczność organiczną i płatną, kreację multimedialną, po konwersję, sprzedaż B2B oraz relacje inwestorskie.

```
                      +------------------------------------+
                      |         marketing-context          |
                      | (ICP, JTBD, Positioning, Dowody)   |
                      +-----------------+------------------+
                                        |
       +--------------------------------+--------------------------------+
       |                                |                                |
       v                                v                                v
+----------------------+     +----------------------+        +----------------------+
|   search-visibility  |     | google-ads-strategy  |        |      media briefs    |
| (SEO, GEO, AEO, LLM) |     |  meta-ads-strategy   |        | (video, avatar,      |
+----------+-----------+     +----------+-----------+        |  voice, audio, html) |
           |                            |                    +----------+-----------+
           v                            v                               |
+-----------------------------------------------+                       |
|              growth-experiments               |<----------------------+
|            (Funnel, CRO, Landing)             |
+-----------------------+-----------------------+
                        |
       +----------------+----------------+
       |                                 |
       v                                 v
+-------------------------------+ +------------------------------------+
|             email             | |       fundraising        |
|  (mateusz-style-profile.json  | |    review-investor-materials   |
|   draft -> token -> approved) | |    (Pitch Deck, One-Pager, Outb.)  |
+-------------------------------+ +------------------------------------+
```

### 1.1. Fundamenty Strategiczne i Pozycjonowanie
- **[`marketing-context`](marketing-context)**: Źródło prawdy dla profilu idealnego klienta (ICP), zdefiniowanych zadań do wykonania (JTBD), pozycjonowania rynkowego, propozycji wartości (UVP) oraz unikalnego słownika klienta. Żadna kampania ani kreacja nie powstaje bez zweryfikowanego fundamentu. Zabrania się tworzenia kampanii na niezweryfikowanych założeniach.

### 1.2. Widoczność Organiczna i Cytowania AI
- **[`search-visibility`](search-visibility)**:
  - Techniczne i on-page SEO dla witryn i lądowisk produktów.
  - **GEO (Generative Engine Optimization)** oraz **AEO (Answer Engine Optimization)**: optymalizacja treści pod kątem cytowań w silnikach generatywnych (ChatGPT Search, Perplexity, Gemini, Claude).
  - Audyty i rekomendacje dotyczące widoczności marki osobistej Mateusza w zapytaniach branżowych (read-only, bez samowolnego publikowania).

### 1.3. Płatne Kampanie i Pozyskiwanie Ruchu
- **[`google-ads-strategy`](google-ads-strategy)**: Kampanie w sieci wyszukiwania Google na zapytania o wysokiej intencji zakupowej (high-intent B2B), struktura konta, architektura wykluczających słów kluczowych (negative keywords), teksty reklamowe i analityka konwersji. Wszystkie teksty i struktury powstają w fazie roboczej (draft).
- **[`meta-ads-strategy`](meta-ads-strategy)**: Kampanie Facebook i Instagram — architektura kreacji, copy reklamowe, precyzyjne targetowanie B2B/B2C, plany konfiguracji piksela, retargeting i optymalizacja ROAS.

### 1.4. Optymalizacja Ścieżek i Eksperymenty Wzrostu
- **[`growth-experiments`](growth-experiments)**: Projektowanie lejków konwersji, plany testów A/B landing page'y, plany śledzenia zdarzeń, atrybucja i metryki guardrail. Wszelkie zmiany na ścieżkach traktowane są jako hipotezy badawcze z jawnym kryterium stopu i estymacjami metryk.

### 1.5. Produkcja Multimediów (Media Briefs)
Dział tworzy precyzyjne specyfikacje wykonawcze (briefy), natomiast samą generację zleca dedykowanym narzędziom:
- **[`video-gen`](video-gen)**: Generowanie wideo AI, przejścia, klipy demonstracyjne i plansze montażowe.
- **[`avatar-gen`](avatar-gen)**: Gadające awatary i lip-sync do predefiniowanego audio na potrzeby materiałów edukacyjnych i wideo-prezentacji.
- **[`voice-gen`](voice-gen)**: Profesjonalny lektor AI (ElevenLabs) z zachowaniem lokalnego DPAPI i ochrony kluczy.
- **[`audio-bed-gen`](audio-bed-gen)**: Generowanie bezpiecznych narracyjnie teł muzycznych, dżingli i podkładów dźwiękowych.
- **[`html-video`](html-video)**: Deterministyczne, klatkowo idealne wideo MP4 z kodu HTML/CSS/SVG renderowane w headless Chrome.

### 1.6. Komunikacja Bezpośrednia i E-mail
- **[`email`](email)**:
  - **Lokalny profil stylu (Tone-of-Voice)**: `mateusz-style-profile.json` — ekstrakcja cech stylistycznych Mateusza (zwięzłość, konkret, brak pustosłowia, precyzja inżynierska) wyłącznie z lokalnie dostępnych, wysłanych wiadomości. Przetwarzanie wyłącznie lokalne — bez wysyłania do chmury i bez używania do trenowania modeli publicznych.
  - **Dwufazowy proces wysyłki (Two-Stage Pipeline)**:
    Wymagana sekwencja fail-closed: `draft` -> realny `owner approval token` od Mateusza -> `send-approved`.
    1. Faza 1: `draft` — autonomiczne przygotowanie wersji roboczej e-maila zgodnie z profilem stylu `mateusz-style-profile.json`. Szkic pozostaje lokalnym artefaktem roboczym.
    2. Faza 2: `owner approval token` — weryfikacja realnego tokenu zatwierdzenia przekazanego przez Mateusza (jawna zgoda właściciela z unikalnym identyfikatorem/tokenem akceptacji w kanale komunikacji lub Office). Playbook egzekwuje procedurę fail-closed: bez otrzymania realnego tokenu zatwierdzenia od Mateusza przejście do wysyłki jest bezwzględnie zablokowane.
    3. Faza 3: `send-approved` — dopiero po zweryfikowaniu tokenu zatwierdzenia od Mateusza e-mail uzyskuje status dopuszczonego do wysyłki.

### 1.7. Finansowanie i Relacje z Inwestorami
- **[`fundraising`](fundraising)**: Przygotowywanie decków inwestorskich, memorandów oraz executive one-pagerów na bazie zweryfikowanych liczb i hipotez.
- **[`review-investor-materials`](review-investor-materials)**: Niezależny odbiór materiałów pod kątem spójności finansowej, narracyjnej, poprawności eksportu oraz klauzul poufności. Żaden materiał inwestorski nie jest traktowany jako gotowy bez niezależnego odbioru.

---

## 2. Matryca Autonomii i Zgody T2 (T2 IN/OUT Consent Table)

Zgodnie z decyzjami Mateusza z 2026-10-21 (T2), organizacja działa w reżimie ścisłego rozgraniczenia prac autonomicznych w środowiskach deweloperskich/testowych od operacji wymagających bezwzględnej zgody właściciela.

| Obszar / Działanie | DEV / TEST (Autonomia) | Mateusz IN (Obecny) | Mateusz OUT (Nieobecny) | Klasa Decyzji & Wymagana Zgoda | Ryzyko / Zasada Bezpieczeństwa |
|---|---|---|---|---|---|
| **Research rynkowy, konkurencja, trendy** | Pełna autonomia | Autonomicznie | Autonomicznie | Worker / Manager (W/M) | Brak efektów zewnętrznych |
| **Definiowanie ICP, JTBD i pozycjonowania** | Pełna autonomia | Autonomicznie | Autonomicznie | Worker / Director (W/D) | Zgodność z `marketing-context` |
| **Audyty SEO, GEO, AEO, LLM Citations** | Pełna autonomia | Autonomicznie | Autonomicznie | Worker (W) | Read-only inspekcja domen |
| **Przygotowanie szkiców postów i artykułów** | Pełna autonomia | Autonomicznie | Autonomicznie | Worker (W) | Tylko lokalne pliki szkiców |
| **Opracowanie briefów mediów (wideo/audio)** | Pełna autonomia | Autonomicznie | Autonomicznie | Worker / Manager (W/M) | Brak zużycia płatnych tokenów bez zgody |
| **Szkice kreacji i kampanii Google Ads oraz Meta Ads** | Pełna autonomia | Autonomicznie | Autonomicznie | Worker (W) | Brak konfiguracji aktywnej na koncie live |
| **Generowanie szkiców e-mail (draft)** | Pełna autonomia | Autonomicznie | Autonomicznie | Worker (W) | Zgodność z `mateusz-style-profile.json` |
| **Przygotowanie szkicu One-Pagera / Decku** | Pełna autonomia | Autonomicznie | Autonomicznie | Worker / Director (W/D) | Weryfikacja przez `review-investor-materials` |
| **Publikacja treści (LinkedIn, blog, WWW)** | BLOKADA | Zgoda Mateusza | **BLOKADA (FAIL-CLOSED)** | **Tylko Mateusz** | Reputacja marki, ryzyko prawne |
| **Uruchomienie budżetu reklamowego / Ads live** | BLOKADA | Zgoda Mateusza | **BLOKADA (FAIL-CLOSED)** | **Tylko Mateusz** | Ryzyko finansowe (wydatki z karty) |
| **Wysyłka e-mail / Outbound outreach** | BLOKADA | Zgoda Mateusza (`token`) | **BLOKADA (FAIL-CLOSED)** | **Tylko Mateusz** | Ryzyko antyspamowe i biznesowe |
| **Zobowiązania cenowe, oferty handlowe, umowy** | BLOKADA | Zgoda Mateusza | **BLOKADA (FAIL-CLOSED)** | **Tylko Mateusz** | Skutki prawno-finansowe |

### Twarde Reguły Bramki T2:
1. **Zasada Fail-Closed**: Nieobecność Mateusza (`OUT`) lub brak odpowiedzi na pytanie o zgodę w sprawach publikacji, budżetu, wysyłki maili czy cen **nigdy nie jest interpretowana jako zgoda**. Decyzja Dyrektora (D) nie ma mocy autoryzacji wydatków ani kontaktu z ludźmi.
2. **Kwarantanna Środowiskowa i Dane Syntetyczne**: Wszystkie testy kampanii i lejków w DEV/TEST działają wyłącznie na danych syntetycznych. Blokada wysyłki e-mail, wydatków budżetowych i integracji płatności jest egzekwowana jako reguła odmowy w logice agentowej (fail-closed refusal), bez deklarowania niewdrożonych blokad bramek sieciowych czy sprzętowych interlocków.
3. **Wymóg Tokenu Zatwierdzenia**: Przejście szkicu e-mail ze statusu `draft` do `send-approved` wymaga uzyskania realnego tokenu zatwierdzenia od Mateusza (`owner approval token` przekazanego przez właściciela). Procedura jest ścisłą sekwencją fail-closed: `draft` -> `owner approval token` -> `send-approved`. Bez potwierdzenia i tokenu od Mateusza wysyłka jest bezwzględnie zablokowana.
4. **Zakaz Zmyślonych Danych i Oświadczeń Prawnych**: Zgodnie z wytycznymi `SKILL.md` i `marketing-content.md`, zabrania się tworzenia gotowych do publikacji szkiców zawierających niepotwierdzone metryki, zmyślone dane telemetryczne lub niesprawdzone deklaracje prawne/podatkowe. Wszelkie twierdzenia prawne wymagają weryfikacji ze specjalistami dziedzinowymi (np. `pl-tax-compliance`), a materiały inwestorskie podlegają niezależnemu odbiorowi przez `review-investor-materials`.

---

## 3. Szablony Pakietów Pilotażowych (Pilot Package Templates)

### 3.1. Pilot A: Personal Brand Authority & LinkedIn/Video Brief
*Cel: Budowa autorytetu Mateusza w obszarze architektury systemów agentowych i inżynierii AI bez angażowania jego czasu w pisanie szkiców od zera.*

> **Zasada rzetelności**: Szablon szkicu (draft). Zabrania się wpisywania niezweryfikowanych twierdzeń o redukcji tokenów lub metrykach bez udokumentowanego audytu. Wszelkie punkty kontrolne recenzji pozostają otwarte do czasu rzeczywistej weryfikacji.

```markdown
# Pakiet Pilotażowy A: Marka Osobista & LinkedIn / Video Brief

## 1. Grupa Docelowa i ICP
- **Segment**: CTOs, VP of Engineering, Tech Founderzy, Architekci AI/Cloud w Polsce i CEE.
- **Problem odbiorcy**: Chaos architektoniczny w agentach AI, niekontrolowany rozrost kontekstu, brak odporności produkcyjnej.
- **Teza wiodąca**: Prawdziwa autonomia to nie zabawa pojedynczym promptem, lecz deterministyczna organizacja z bramkami bezpieczeństwa.

## 2. Filary Treści (Content Pillars)
- Filar 1: Architektura Multi-Agent i odporność wykonawcza (Agent Graph, deterministyczny routing, redukcja zbędnych ceremonii).
- Filar 2: Ekonomia i audyt sesji modeli AI (zarządzanie oknem kontekstu, unikanie maratońskich sesji).
- Filar 3: Suwerenność danych i polityka prywatności (lokalne przetwarzanie, brak uczenia modeli na danych klienta).

## 3. Szkic Wpisu LinkedIn (Copy Draft)
- **Haczyk (Hook)**: Jak poradzić sobie z degradacją jakości modeli w długich wątkach? Maratońskie sesje i puchnący kontekst to jeden z głównych problemów współczesnych wdrożeń agentowych.
- **Rozwinięcie**: [Miejsce na zweryfikowane dane telemetryczne z audytu — wpisać wyłącznie potwierdzone liczby po zakończeniu pomiarów]. W architekturze kluczowe są: dekompozycja zadań, deterministyczny graf zależności oraz niezależny odbiór rezultatów.
- **Call to Discussion**: Jak w Waszych zespołach rozwiązujecie problem utraty ostrości modeli w długich wątkach i rosnących kosztów kontekstu?

## 4. Brief Wideo i Audio (Media Brief)
- **Format**: 60-sekundowy vertical wideo (LinkedIn / YouTube Shorts).
- **Lektor (`voice-gen`)**: Spokojny, techniczny, profesjonalny ton; model ElevenLabs; język polski.
- **Podkład dźwiękowy (`audio-bed-gen`)**: Subtelny, nowoczesny beat ambient/techno, miks bezpieczny dla narracji (narration-safe).
- **Wizualia (`video-gen` / `html-video`)**: Czysta infografika architektury terminala i schemat przepływu zadań wyrenderowany z HTML/CSS/SVG.

## 5. Weryfikacja Stylu (Tone-of-Voice Checklist)
Lista kontrolna do weryfikacji przed przedstawieniem Mateuszowi (zgodność z `mateusz-style-profile.json`):
- [ ] Brak pustosłowia, powitań i pochwalnych wstępów.
- [ ] Wszystkie przytoczone twierdzenia techniczne i liczbowe posiadają potwierdzone źródło pierwotne.
- [ ] Zwięzłość, konkret i bezpośredniość inżynierska.

## 6. Bramka Publikacji T2
- Status: `DRAFT_READY` (szkic roboczy).
- Wymagane działanie: Akceptacja treści przez Mateusza w czacie lub na tablicy Office.
- Token autoryzacji publikacji: `PENDING_OWNER_TOKEN` (bezwzględny zakaz publikacji bez zgody właściciela).
```

---

### 3.2. Pilot B: B2B Product Funnel & Search/Ads Draft
*Cel: Zaprojektowanie lejkowej maszyny marketingowej dla produktu B2B (np. Contoso App lub rozwiązania agentowego).*

> **Zasada rzetelności**: Kampanie reklamowe w fazie projektowej (draft). Zabrania się formułowania niesprawdzonych deklaracji prawnych i podatkowych (np. zgodność z przepisami prawa lub KSeF wymaga uprzedniej formalnej weryfikacji ze skillem pl-tax-compliance oraz doradcą). Wszelkie metryki konwersji i CPA są wyłącznie celami modelowymi (target), a nie faktami historycznymi. Zabrania się także deklarowania zmyślonego stanu konta lub konfiguracji stagingu (brak pre-autoryzacji i zakaz uznawania stagingu za zweryfikowany bez rzeczywistego audytu konta).

```markdown
# Pakiet Pilotażowy B: Lejek Produktowy B2B & Kampanie Płatne

## 1. Architektura Lejka (Funnel Stages)
- **Top of Funnel (ToFU)**: Edukacja o automatyzacji i bezpieczeństwie — ruch organiczny i płatny.
- **Middle of Funnel (MoFU)**: Studium przypadku architektury agentowej i kalkulator potencjalnych oszczędności czasu.
- **Bottom of Funnel (BoFU)**: Wersja demonstracyjna w odizolowanym środowisku testowym (sandbox).

## 2. Strategia Widoczności Organicznej i GEO/AEO
- **Frazy kluczowe high-intent**: `bezpieczna automatyzacja procesów B2B`, `agentowa architektura ERP`, `lokalne modele w przetwarzaniu dokumentów`.
- **Optymalizacja GEO/AEO (`search-visibility`)**: Strukturyzacja danych Schema.org (SoftwareApplication, FAQPage), publikacja definicji architektonicznych cytowalnych przez silniki LLM (read-only plan do wdrożenia).

## 3. Kampania Płatna Google Ads (`google-ads-strategy`)
- **Struktura kampanii**: 1 kampania Brand Protection, 2 kampanie Problem-Solution (ścisłe dopasowanie Exact/Phrase).
- **Negatywne słowa kluczowe**: `darmowy`, `praca`, `staż`, `kurs`, `szkolenie`, `crack`.
- **Warianty nagłówków reklamowych**:
  - H1: Prywatna Automatyzacja Procesów B2B
  - H2: Lokalne Przetwarzanie i Bezpieczeństwo Danych
  - H3: Zaprojektowane pod Obieg Dokumentów B2B
  *(Uwaga: oświadczenia prawne/podatkowe wyłączone do czasu formalnej weryfikacji).*
- **Docelowy CPA i ROAS (Estymacje modelowe)**: Docelowy CPA < 120 PLN, docelowy współczynnik konwersji lądowiska > 4.5%.

## 4. Kampania Płatna Meta Ads (`meta-ads-strategy`)
- **Targetowanie**: Właściciele firm, CFO, Dyrektorzy Operacyjni i Finansowi (Polska, firmy 10-250 pracowników).
- **Format kreacji**: Karuzela B2B prezentująca architekturę wdrożenia + wideo demonstracyjne 15s.
- **Copy reklamowe**: Krótka wersja skupiona na eliminacji powtarzalnej pracy manualnej i redukcji błędów przepisywania danych.

## 5. Eksperyment CRO na Lądowisku (`growth-experiments`)
- **Hipoteza**: Zastąpienie statycznego formularza interaktywnym kalkulatorem potencjalnych oszczędności czasu może podnieść współczynnik rozpoczęcia wersji próbnej.
- **Metryki testu**: Primary: % kliknięć "Oblicz potencjalne oszczędności"; Guardrail: czas ładowania strony < 1.2s.

## 6. Bramka Budżetowa i Uruchomienia T2
- Proponowany budżet testowy: 1 500 PLN (7 dni testu) — wyłącznie do decyzji Mateusza.
- Status konfiguracji konta: `OCZEKUJE_NA_WERYFIKACJĘ (PENDING_VERIFICATION)` (konto w fazie roboczej, brak aktywnego wydatkowania; zakaz traktowania konfiguracji staging/live jako pre-zweryfikowanej bez faktycznego audytu konta).
- Wymagane działanie: Zatwierdzenie budżetu i autoryzacja płatności przez Mateusza.
```

---

### 3.3. Pilot C: Investor & Partner One-Pager + Outbound Draft
*Cel: Przygotowanie profesjonalnego pakietu do rozmów ze strategicznymi partnerami i inwestorami, z pełną weryfikacją liczb i bezpiecznym e-mailem.*

> **Zasada rzetelności**: Materiał inwestycyjny i szkic outboundu w fazie roboczej (draft). Zabrania się wpisywania niezweryfikowanych twierdzeń o trakcji ani oznaczania niezależnego odbioru jako zakończonego przed faktyczną procedurą. Niezależny odbiór przez `review-investor-materials` jest obowiązkowy.

```markdown
# Pakiet Pilotażowy C: Investor & Partner One-Pager + Outbound

## 1. Syntetyczny One-Pager (`fundraising`)
- **Problem**: Rosnące koszty operacyjne wdrażania modeli AI oraz brak deterministycznej kontroli nad ich zachowaniem w firmach.
- **Rozwiązanie**: Prywatna platforma agentowa ze sztywną matrycą uprawnień, lokalnym przetwarzaniem i automatycznym pomiarem czasu dostawy.
- **Trakcja i Metryki**: [Do uzupełnienia na podstawie zweryfikowanego raportu telemetrycznego — zabrania się wpisywania niepotwierdzonych wskaźników].
- **Model Biznesowy**: Licencja roczna on-premise / private cloud + wdrożenie integracji kluczowych procesów.
- **Runda / Cel finansowy**: Pozyskanie finansowania na rozwój modułów dziedzinowych i niezależne audyty bezpieczeństwa.

## 2. Niezależny Odbiór Inwestorski (`review-investor-materials`)
Status: `OCZEKUJE NA NIEZALEŻNY ODBIÓR (PENDING_INDEPENDENT_REVIEW)`
Lista kontrolna dla niezależnego recenzenta przed dopuszczeniem do prezentacji:
- [ ] Spójność matematyczna modelu finansowego (brak rozbieżności między arkuszem a tekstem).
- [ ] Weryfikacja tez rynkowych na podstawie źródeł pierwotnych.
- [ ] Sprawdzenie poufności: brak wewnętrznych adresów IP, sekretów, tokenów i danych poufnych.
- [ ] Jakość wizualna i poprawność generowania pliku PDF.

## 3. Szkic E-maila Wychodzącego (`email`)
- **Styl**: Ścisła zgodność z `mateusz-style-profile.json` (maksymalnie 5 konkretnych zdań, jasna propozycja wartości, brak zbędnych formułek).
- **Temat**: Prywatna architektura agentowa B2B — propozycja technologiczna
- **Treść Robocza (Stage 1 Draft)**:
  > Dzień dobry,
  > 
  > Rozwijamy architekturę agentową dla firm, w której modele AI wykonują zadania inżynierskie i biznesowe w kontrolowanych środowiskach, bez przekazywania danych do uczenia modeli publicznych.
  > 
  > Zastosowaliśmy deterministyczne grafy zależności oraz niezależny odbiór jakościowy, co ogranicza problem halucynacji i niekontrolowanego rozrostu kontekstu.
  > 
  > Załączam jednostronicowe podsumowanie technologiczne. Jeśli temat optymalizacji i bezpieczeństwa procesów AI w Państwa organizacji jest aktualny, chętnie przedstawię 15-minutowe demo działania systemu.
  > 
  > Pozdrawiam,
  > Mateusz

## 4. Procedura Bezpieczeństwa E-mail (Two-Stage Fail-Closed Gate)
- Aktualny stan w systemie e-mail: `STATUS = DRAFT` (szkic roboczy).
- Identyfikator szkicu: `draft_inv_partner_20261021_01`.
- Wymagany krok: Uzyskanie realnego tokenu zatwierdzenia od Mateusza (`OWNER_APPROVAL_TOKEN`).
- Zasada operacyjna: Wymagana sekwencja fail-closed: przygotowanie szkicu (`draft`) -> weryfikacja i przekazanie tokenu zgody przez Mateusza (`owner approval token`) -> oznaczenie jako dopuszczone do wysyłki (`send-approved`). Zabronione jest wywołanie wysyłki bez udokumentowanego tokenu zgody właściciela. Playbook nie deklaruje nieistniejących blokad sprzętowych ani zewnętrznych bramek technicznych; egzekwuje bezwzględną regułę odmowy wykonania w logice agentowej (fail-closed refusal).
```

---

## 4. Podsumowanie Wymagań Integracyjnych

1. **Jeden spójny router**: Zadania działu są inicjowane przez `business-capabilities` z wyborem profilu Personal Brand, Marketing & Sales.
2. **Delegacja wykonawcza**: Wykonanie poszczególnych zadań technicznych (SEO, kampanie ads, media, teksty) powierzane jest pracownikom (Workerom) odpowiednich skilli dziedzinowych.
3. **Brak samowolnych działań zewnętrznych**: Żaden agent nie ma uprawnień do publikowania, wydawania budżetu ani wysyłania wiadomości bez jednoznacznej, zarejestrowanej zgody Mateusza.
4. **Zgodność z dyscypliną dowodową**: Żaden szablon ani szkic nie może deklarować zmyślonych metryk, pre-akceptowanych recenzji ani niezweryfikowanych twierdzeń prawnych.
