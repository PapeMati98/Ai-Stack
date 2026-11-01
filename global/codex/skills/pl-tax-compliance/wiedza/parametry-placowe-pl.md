# Parametry płacowe PL (ZUS / PIT / płaca minimalna / PPK) — 2025 i 2026

ostatnia_weryfikacja: 2026-06-26
pewnosc: pewne (z wyjątkami oznaczonymi w tabelach)
zakres: rule-pack do naliczania wynagrodzeń (UoP / zlecenie / dzieło), okresy 2025 i 2026

> Uwaga: ten plik dotyczy parametrów (stawki, limity, kwoty), nie wymogów dot. FORMY dokumentu.
> Liczby do silnika liczonego na Decimal. Stawki procentowe podane jako % (np. 9,76% = 0.0976).

## Podstawy prawne (skrót)
- u.s.u.s. = ustawa z 13.10.1998 o systemie ubezpieczeń społecznych (Dz.U. 2025 poz. 350 t.j.)
- u.u.w. = ustawa z 30.10.2002 o ubezpieczeniu społ. z tytułu wypadków przy pracy i chorób zawodowych (Dz.U. 2025 poz. 1644 t.j.)
- u.ś.o.z. = ustawa z 27.08.2004 o świadczeniach opieki zdrowotnej finansowanych ze środków publicznych
- u.p.d.o.f. (PIT) = ustawa z 26.07.1991 o podatku dochodowym od osób fizycznych (t.j. Dz.U. 2026 poz. 592)
- u.m.w. = ustawa z 10.10.2002 o minimalnym wynagrodzeniu za pracę (Dz.U. 2024 poz. 1773 t.j.)
- u.PPK = ustawa z 04.10.2018 o pracowniczych planach kapitałowych

---

## 1. ZUS — składki społeczne (stopy stałe, bez zmian 2025↔2026)

Podstawa: art. 22 ust. 1 u.s.u.s. (stopy emer./rent./chor.); rozdział podziału prac./pracodawca — art. 16 u.s.u.s.

| składka | pracownik | pracodawca | łącznie | podstawa | obowiązuje |
|---|---|---|---|---|---|
| emerytalna | 9,76% | 9,76% | 19,52% | art. 22 ust. 1 pkt 1 u.s.u.s. | 2025 i 2026 |
| rentowa | 1,5% | 6,5% | 8,00% | art. 22 ust. 1 pkt 2 u.s.u.s. | 2025 i 2026 |
| chorobowa | 2,45% | — | 2,45% | art. 22 ust. 1 pkt 3 u.s.u.s. | 2025 i 2026 |
| wypadkowa | — | 0,67%–3,33% (wg grupy PKD); domyślnie 1,67% dla zgłaszających <10 ubezpieczonych | jw. | art. 22 ust. 1 pkt 4 + art. 28 u.u.w.; rozp. MRPiPS o różnicowaniu stopy proc. | 2025 i 2026 |

Wypadkowa — szczegóły:
- Stawka 1,67% (= 50% najwyższej stopy dla grup) obowiązuje płatników zgłaszających <10 ubezpieczonych ORAZ płatników niewpisanych do REGON. Pewne dla roku składkowego 01.04.2025–31.03.2026 oraz 01.04.2026–31.03.2027.
- Płatnicy zgłaszający ≥10 ubezpieczonych: stopa indywidualna (ZUS wylicza z ZUS IWA za 3 lata albo płatnik sam ustala wg grupy PKD). Zakres ogólny stóp dla grup działalności: 0,67%–3,33%.
- Rok składkowy wypadkowej: od 1 kwietnia do 31 marca (NIE kalendarzowy).

Fundusze pozaubezpieczeniowe (pracodawca):
| fundusz | stawka | podstawa | uwaga |
|---|---|---|---|
| FP (Fundusz Pracy) + FS (Fundusz Solidarnościowy) | 2,45% łącznie (FP 1,00% + FS 1,45%) | art. 104 ustawy o promocji zatrudnienia (FP); ustawa o Funduszu Solidarnościowym | naliczane razem na jednym koncie składkowym; zwolnienia: m.in. powracający z urlopu macierz., 50+/55+, wynagrodzenie < min. krajowej |
| FGŚP | 0,10% | art. 9–29 ustawy o ochronie roszczeń pracowniczych | stawkę ustala ustawa budżetowa; 0,10% w 2025 i 2026 |

> Składka 2,45% to FP+FS razem (nie dwie odrębne pozycje 2,45%). Chorobowa jest obowiązkowa przy UoP, dobrowolna przy zleceniu.

## 2. Limit 30-krotności (roczna podstawa wymiaru składek emer.-rent.)

Podstawa: art. 19 ust. 1 u.s.u.s.; kwota = 30 × prognozowane przeciętne wynagrodzenie miesięczne (z ustawy budżetowej), ogłaszana obwieszczeniem Ministra właściwego ds. zabezpieczenia społ.

| rok | prognozowane przeciętne wynagr. | limit roczny (30×) | podstawa | pewność |
|---|---|---|---|---|
| 2025 | 8 673 zł | 260 190,00 zł | obwieszczenie MRPiPS z 10.12.2024 (M.P. 2024) | PEWNE |
| 2026 | 9 420 zł | 282 600,00 zł | obwieszczenie MRPiPS z 19.11.2025 | PEWNE |

Skutek przekroczenia: powyżej limitu znikają składki emer. i rent. (obie strony); chorobowa, wypadkowa i ZDROWOTNA naliczane dalej bez ograniczenia.

## 3. Składka zdrowotna

Podstawa: art. 79–81 u.ś.o.z. Stawka 9% podstawy. Bez zmian 2025↔2026.

| tytuł | stawka | podstawa wymiaru | uwagi |
|---|---|---|---|
| UoP / zlecenie | 9% | przychód pomniejszony o składki społ. finansowane przez ubezpieczonego | brak odliczenia od PIT (odliczenie 7,75% zniesione od 01.01.2022 — Polski Ład) |
| działalność (skala) | 9% | dochód (min. podstawa = min. wynagrodzenie) | reguły odrębne; nie wchodzi w zakres prostego rule-packu UoP |

- BRAK odliczenia składki zdrowotnej od podatku PIT dla wszystkich rozliczających wg skali — PEWNE (art. 27b PIT uchylony w zakresie odliczenia od 2022).
- Zasada „obniżenia do zaliczki" (art. 83 u.ś.o.z.): składka zdrowotna obniżana do wysokości hipotetycznej zaliczki PIT liczonej wg zasad z 31.12.2021 — dotyczy m.in. osób korzystających z ulgi <26 (przychód zwolniony z PIT) i niskich podstaw.

## 4. PIT — skala, kwota wolna, kwota zmniejszająca (bez zmian 2025↔2026)

Podstawa: art. 27 ust. 1 PIT (skala), art. 27 ust. 1b–1c (kwota zmniejszająca), art. 31b i 32 PIT (zaliczki / PIT-2).

| parametr | 2025 | 2026 | podstawa | pewność |
|---|---|---|---|---|
| stawka I próg | 12% | 12% | art. 27 ust. 1 PIT | PEWNE |
| stawka II próg (nadwyżka) | 32% | 32% | art. 27 ust. 1 PIT | PEWNE |
| próg (granica I/II) | 120 000 zł | 120 000 zł | art. 27 ust. 1 PIT | PEWNE |
| kwota wolna | 30 000 zł | 30 000 zł | art. 27 ust. 1 PIT | PEWNE |
| kwota zmniejszająca roczna | 3 600 zł | 3 600 zł | art. 27 ust. 1 PIT (30 000 × 12%) | PEWNE |
| miesięczna kwota zmniejszająca (1/12) | 300 zł | 300 zł | art. 31b PIT | PEWNE |

PIT-2 / pomniejszanie zaliczek (art. 31b PIT):
- 1 płatnik: 1/12 = 300 zł/mies.
- 2 płatników: po 1/24 = 150 zł każdy.
- 3 płatników: po 1/36 = 100 zł każdy.
- Maksymalnie 3 płatników; suma pomniejszeń u wszystkich nie może przekroczyć 300 zł/mies. Oświadczenie składa podatnik (PIT-2 lub równoważne).

## 5. Koszty uzyskania przychodu (KUP) — UoP

Podstawa: art. 22 ust. 2 PIT.

| typ | miesięcznie | roczny limit (1 umowa) | roczny limit łączny (wiele umów) | obowiązuje |
|---|---|---|---|---|
| podstawowe (miejscowe) | 250 zł | 3 000 zł | 4 500 zł | 2025 i 2026 |
| podwyższone (zamiejscowe) | 300 zł | 3 600 zł | 5 400 zł | 2025 i 2026 |

Bez zmian 2025↔2026. Limity łączne dotyczą sumy z wielu stosunków pracy w roku.

## 6. Ulga dla młodych (zwolnienie PIT < 26 lat)

Podstawa: art. 21 ust. 1 pkt 148 PIT.
- Zwolnienie z PIT przychodów ze: stosunku pracy/służbowego, spółdzielczego stosunku pracy, umów zlecenia (art. 13 pkt 8), zasiłku macierzyńskiego, praktyk absolwenckich i stażu uczniowskiego — do ukończenia 26 lat.
- Roczny limit zwolnienia: 85 528,00 zł (2025 i 2026). PEWNE.
- Zwolnienie NIE obejmuje umowy o dzieło. Nie zwalnia ze składek ZUS ani zdrowotnej.

## 7. PPK

Podstawa: art. 25–27 u.PPK.

| wpłata | stawka | podstawa | uwagi |
|---|---|---|---|
| podstawowa pracownik | 2,0% wynagrodzenia | art. 27 ust. 1 u.PPK | obniżenie do min. 0,5% gdy łączne wynagr. ≤ 120% min. wynagrodzenia (art. 27 ust. 2) |
| podstawowa pracodawca | 1,5% wynagrodzenia | art. 26 ust. 1 u.PPK | |
| dodatkowa pracownik | do 2,0% | art. 27 ust. 3 u.PPK | dobrowolna |
| dodatkowa pracodawca | do 2,5% | art. 26 ust. 2 u.PPK | dobrowolna |

- Limit „120% min. wynagr." dla obniżenia wpłaty pracownika: 2026 = 5 767,20 zł/mies. (120% × 4 806 zł). 2025 = 5 599,20 zł/mies. (120% × 4 666 zł).
- Wpłata pracodawcy (podstawowa + dodatkowa) jest przychodem pracownika i DOLICZANA do podstawy opodatkowania PIT (art. 12 ust. 1 PIT). NIE jest oskładkowana ZUS (wyłączenie składkowe). PEWNE.

## 8. Płaca minimalna

Podstawa: rozporządzenie RM wydane na podstawie art. 2 ust. 5 u.m.w.

| rok | minimalne wynagr. (pełny etat) | min. stawka godzinowa (umowy cyw.) | podstawa | obowiązuje |
|---|---|---|---|---|
| 2025 | 4 666,00 zł | 30,50 zł | rozp. RM z 12.09.2024 (Dz.U. 2024 poz. 1362) | od 01.01.2025 — JEDNA stawka całoroczna |
| 2026 | 4 806,00 zł | 31,40 zł | rozp. RM z 11.09.2025 (Dz.U. 2025 poz. 1242) | od 01.01.2026 — JEDNA stawka całoroczna |

> W 2025 i 2026 brak zmiany w trakcie roku (jedna stawka 1.I–31.XII). Dwustawkowe lata (np. 2023) tu nie występują.

## 9. Umowa ZLECENIE (art. 734+ KC; tytuł ZUS art. 6 ust. 1 pkt 4 u.s.u.s.)

- Oskładkowanie: emer., rent., wypadkowa OBOWIĄZKOWE; chorobowa DOBROWOLNA (na wniosek); zdrowotna obowiązkowa.
- Zbieg tytułów: jeśli z innego tytułu (np. UoP) podstawa ≥ min. wynagrodzenia → zlecenie obowiązkowo tylko zdrowotne; emer.-rent. dobrowolne. Reguła sumowania podstaw do min. wynagrodzenia (art. 9 u.s.u.s.).
- STUDENT / uczeń do 26 lat: pełne zwolnienie ze składek ZUS i zdrowotnej (art. 6 ust. 4 u.s.u.s.) — zleceniobiorca będący uczniem/studentem do ukończenia 26 lat NIE podlega ubezpieczeniom z tytułu zlecenia.
- KUP: 20% przychodu pomniejszonego o składki społ. (art. 22 ust. 9 pkt 4 PIT); 50% przy przeniesieniu praw autorskich.
- Mały kontrakt: gdy kwota należności z umowy ≤ 200 zł i osoba nie jest pracownikiem płatnika → zryczałtowany PIT 12% od przychodu bez KUP i bez pomniejszeń (art. 30 ust. 1 pkt 5a PIT). Nie stosuje się do praw autorskich (przychód z art. 18 PIT).

## 10. Umowa o DZIEŁO (art. 627+ KC)

- BRAK składek ZUS i zdrowotnej (o ile nie zawarta z własnym pracodawcą / nie wykonywana na rzecz własnego pracodawcy — wtedy oskładkowanie jak UoP). Zgłoszenie umowy o dzieło do ZUS na RUD jest obowiązkiem informacyjnym, ale nie rodzi składek.
- KUP: 20% (art. 22 ust. 9 pkt 4 PIT) albo 50% przy przeniesieniu praw autorskich (art. 22 ust. 9 pkt 3 PIT).
- Limit roczny 50% KUP: 120 000 zł kosztów (art. 22 ust. 9a PIT) — odpowiada przychodowi 240 000 zł.
- Opodatkowanie: zaliczka PIT 12% (od podstawy po KUP); mały kontrakt ≤ 200 zł → ryczałt 12% (art. 30 ust. 1 pkt 5a PIT), z wyłączeniem praw autorskich.

---

## 11. Zaokrąglanie składników listy płac (istotne dla TOLERANCJI w audycie)

| składnik | zaokrąglenie | podstawa prawna |
|---|---|---|
| Podstawa opodatkowania PIT i **zaliczka na PIT** | do PEŁNYCH ZŁOTYCH (końcówka <50 gr → w dół, ≥50 gr → w górę) | **art. 63 § 1 Ordynacji podatkowej** |
| Podstawa wymiaru składek ZUS + **składki społeczne i zdrowotna** | do PEŁNYCH GROSZY (≥0,5 gr → w górę, <0,5 gr → w dół) | praktyka ZUS wg u.s.u.s.; zus.pl „wysokość składek" |
| Ryczałt 19% (podstawa i kwota) | do PEŁNYCH GROSZY | art. 63 § 1a Ordynacji podatkowej |

**Wniosek dla audytu (tolerancja porównania PER POLE):**
- `pit_advance` (zaliczka PIT): tolerancja do **pełnych złotych** — różnica **≤ 1 zł** między wartością wykazaną a przeliczeniem audytora to artefakt zaokrąglenia do pełnych złotych; w granicy tolerancji, można ODNOTOWAĆ jako obserwację, ale **NIE blokuje** zamknięcia audytu. Różnica ≥ 2 zł = realna rozbieżność do wyjaśnienia.
- składki społeczne, zdrowotna, sumy składek, netto-składowe: tolerancja **groszowa** (~0,02 zł).
- **Pułapka metodyczna:** stosowanie tolerancji groszowej do zaliczki PIT generuje FAŁSZYWE rozbieżności rzędu 1 zł — to błąd metody audytu, nie błąd klienta.

---

## Pułapki (najczęstsze błędy naliczeń)
- PIT-2 u kilku płatników: suma pomniejszeń > 300 zł/mies. → niedopłata PIT.
- Przekroczenie limitu 30-krotności: dalsze naliczanie emer.-rent. po przekroczeniu = nadpłata.
- KUP roczny łączny (4 500 / 5 400 zł) przy kilku umowach — przekroczenie zawyża koszty.
- Ulga <26: zwolnienie dotyczy PIT, ale składki ZUS/zdrowotna naliczane normalnie; nie obejmuje dzieła.
- Wypadkowa: rok składkowy 04–03 (nie kalendarzowy); 1,67% tylko dla <10 ubezpieczonych.
- Student na zleceniu: zwolnienie ze składek tylko do ukończenia 26 lat (data urodzenia, nie rocznik).
- FP+FS to 2,45% łącznie, nie dwie osobne składki.
- Wpłata pracodawcy do PPK: doliczana do podstawy PIT (ale nie do ZUS).

## Changelog
- 2026-06-26 — dodano sekcję 11 „Zaokrąglanie składników listy płac" (na potrzeby tolerancji w silniku
  audytu — nazwa modułu wewnętrznego zredagowana w edycji publicznej). Potwierdzono w źródłach pierwotnych: zaliczka PIT + podstawa opodatkowania →
  pełne złote (**art. 63 § 1 Ordynacji podatkowej**, <50 gr w dół / ≥50 gr w górę); składki ZUS/zdrowotna +
  podstawa wymiaru → pełne grosze (≥0,5 gr w górę). Wniosek: per-pole tolerancja — PIT do 1 zł (zaokrąglenie,
  obserwacja NIE blokująca), składki ~0,02 zł. Źródła: arslege.pl/sip.lex.pl (art. 63 OrdPod), zus.pl.
- 2026-06-25 — re-potwierdzono komplet PL_2025 na potrzeby pakietu reguł płacowych (ścieżka wewnętrzna zredagowana w edycji publicznej)
  (audyt klienta 2025 — dane prywatne zredagowane w edycji publicznej). Wszystkie wartości zgodne; `confidence` packa DO_POTWIERDZENIA→PEWNE.
  Bez zmian wartości — 2025 to rok zamknięty, parametry historyczne nie ulegają zmianie.
- 2026-06-23 — utworzenie tematu. Zweryfikowano w źródłach pierwotnych komplet parametrów 2025/2026:
  - Płaca min. 2026: 4 806 zł / 31,40 zł — Dz.U. 2025 poz. 1242 (rozp. RM 11.09.2025): https://isap.sejm.gov.pl/isap.nsf/DocDetails.xsp?id=WDU20250001242 ; https://dziennikustaw.gov.pl/D2025000124201.pdf
  - Płaca min. 2025: 4 666 zł / 30,50 zł — Dz.U. 2024 poz. 1362 (rozp. RM 12.09.2024): https://isap.sejm.gov.pl/isap.nsf/DocDetails.xsp?id=WDU20240001362
  - Limit 30-krotności 2026: 282 600 zł (przeciętne 9 420 zł), obwieszczenie MRPiPS z 19.11.2025.
  - Limit 30-krotności 2025: 260 190 zł (przeciętne 8 673 zł), obwieszczenie MRPiPS z 10.12.2024.
  - PIT, KUP, kwota zmniejszająca, ulga <26: bez zmian 2025↔2026 (art. 27, 31b, 22 ust. 2 i 9, 21 ust. 1 pkt 148 PIT).
  - Składka wypadkowa 1,67% dla <10 ubezp. potwierdzona dla roku składkowego 04.2025–03.2026 i 04.2026–03.2027 (rozp. o różnicowaniu stopy proc., art. 28 u.u.w.).
  - PPK limit 120% min. wynagr. 2026 = 5 767,20 zł (mojeppk.pl).

## Źródła
- ISAP: https://isap.sejm.gov.pl
- Dziennik Ustaw: https://dziennikustaw.gov.pl
- ZUS — wysokość składek: https://www.zus.pl/baza-wiedzy/skladki-wskazniki-odsetki/skladki/wysokosc-skladek-na-ubezpieczenia-spoleczne
- mojePPK (limity): https://www.mojeppk.pl
- u.PIT t.j.: Dz.U. 2026 poz. 592
