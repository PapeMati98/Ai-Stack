# Dokumentacja płacowa — macierz kompletności (audyt naliczania wynagrodzeń PL)

- temat: dokumentacja-placowa-kompletnosc
- zakres: jakie dokumenty FORMALNE muszą istnieć u pracodawcy/zleceniodawcy, by naliczenie wynagrodzenia było udokumentowane; warunek wymagalności wyrażony przez parametry kalkulacji; podstawa prawna; konsekwencja braku (ryzyko kary/rezerwy)
- soczewka: NIE wysokość wynagrodzenia (decyzja biznesowa), tylko LUKI SYSTEMOWE narażające na kary → potencjalna rezerwa
- ostatnia_weryfikacja: 2026-06-23
- pewnosc: PEWNE (z wyjątkami oznaczonymi DO_POTWIERDZENIA)

## Podstawy prawne (rdzeń)
- Kodeks pracy (ustawa z 26.06.1974, t.j. Dz.U. 2025 poz. 277 z późn. zm.):
  - art. 94 pkt 9a–9b — obowiązek prowadzenia i przechowywania dokumentacji pracowniczej
  - art. 149 — ewidencja czasu pracy (oraz wyłączenia)
  - art. 87, 87(1), 88, 89, 91 — potrącenia z wynagrodzenia, kwoty wolne
  - art. 281 § 1 pkt 6, 6a — wykroczenie: brak prowadzenia/przechowywania dokumentacji (sankcja)
- Rozporządzenie MRPiPS z 10.12.2018 w sprawie dokumentacji pracowniczej (Dz.U. 2018 poz. 2369; t.j. obwieszczenie Dz.U. 2024 poz. 535) — zakres akt osobowych (A–E) i dokumentacji w sprawach stosunku pracy: ewidencja czasu pracy, wnioski urlopowe, dokumenty absencji
- Ustawa o PIT (t.j. Dz.U. 2026 poz. 592):
  - art. 31b — oświadczenie o stosowaniu kwoty zmniejszającej zaliczkę (rdzeń „PIT-2")
  - art. 32 — pobór zaliczek, KUP, oświadczenia płatnikowi
  - art. 22 ust. 2 pkt 3 — podwyższone (zamiejscowe) KUP + oświadczenie pracownika
  - art. 22 ust. 9 pkt 3 i ust. 9a/9b — 50% KUP (prawa autorskie), limit roczny 120 000 zł
  - art. 21 ust. 1 pkt 148 — ulga dla młodych (<26 lat)
- Ustawa o systemie ubezpieczeń społecznych (art. 6 ust. 4, art. 9 — zbieg tytułów; status ucznia/studenta <26 = brak ubezpieczeń z umowy zlecenia)
- Ustawa o PPK (z 04.10.2018): deklaracja rezygnacji z wpłat; ponowny autozapis co 4 lata (najbliższy: wpłaty wznawiane 1.04.2027; deklaracje rezygnacji złożone do 28.02.2027 tracą skuteczność od 1.03.2027)
- Ustawa zasiłkowa (z 25.06.1999) + e-ZLA (elektroniczne zwolnienia lekarskie ZUS ZLA) — podstawa wypłaty wynagrodzenia chorobowego/zasiłku

## Reguła legendy
- BEZWARUNKOWY = musi istnieć zawsze dla danego typu umowy (niezależnie od parametrów kalkulacji)
- WARUNKOWY = wymagany tylko, gdy w kalkulacji wystąpił dany parametr (pole listy płac) → idealny do auto-checklisty
- Parametr kalkulacji = pole/flaga z listy płac, które „odpala" wymóg dokumentu

## MACIERZ — UMOWA O PRACĘ (UoP)

### Bezwarunkowe (zawsze)
| Dokument | Warunek (parametr) | Podstawa | Konsekwencja braku | Pewność |
|---|---|---|---|---|
| Umowa o pracę (+ aneksy do stawki/składników) | zawsze; aneks gdy zmiana stawki/składnika w okresie | art. 29 KP; rozp. 2018 (akta cz. B) | brak udokumentowania podstawy naliczenia; wykroczenie art. 281 KP; ryzyko sporu o wysokość → rezerwa | PEWNE |
| Akta osobowe (cz. A–E) prowadzone i przechowywane | zawsze | art. 94 pkt 9a–9b KP; rozp. 2018 | grzywna art. 281 § 1 pkt 6/6a KP: obecnie 1000–30000 zł; OD 8.07.2026 do 60000 zł (szczególne do 90000 zł) | PEWNE |
| Ewidencja czasu pracy | zawsze, gdy pracownik NIE jest wyłączony (patrz niżej) | art. 149 §1 KP; rozp. 2018 §6 | brak dowodu na nadgodziny/dodatki/absencje; spór o wynagrodzenie → ciężar dowodu na pracodawcy; grzywna art. 281 | PEWNE |

Ewidencja czasu pracy — niuans (różnica miesięczny pełny etat vs godzinowy):
- Obowiązek prowadzenia ewidencji dotyczy KAŻDEGO pracownika (art. 149 §1 KP). Nie ma zwolnienia tylko dlatego, że ktoś jest na stałej pensji miesięcznej.
- Wyłączenie z EWIDENCJONOWANIA GODZIN (art. 149 §2 KP) — NIE ewidencjonuje się godzin pracy: (a) zadaniowy czas pracy, (b) pracownicy zarządzający w imieniu pracodawcy zakładem, (c) otrzymujący ryczałt za nadgodziny/pracę w nocy. Ale karta ewidencji (dni pracy, absencje, urlopy) nadal jest wymagana.
- Praktyka audytowa: dla pracownika rozliczanego GODZINOWO ewidencja godzin jest krytyczna (bez niej nie da się zweryfikować podstawy naliczenia ani nadgodzin). Dla pełnoetatowca na stałej pensji bez nadgodzin — karta ewidencji nadal wymagana, ale ryzyko sporu o kwotę niższe, jeśli brak zmiennych składników.

### Warunkowe (zależne od parametrów kalkulacji)
| Dokument | Warunek wymagalności (parametr listy płac) | Podstawa | Konsekwencja braku | Pewność |
|---|---|---|---|---|
| PIT-2 / oświadczenie o kwocie zmniejszającej (część dot. art. 31b) | gdy `kwota_zmniejszajaca > 0` zastosowana w miesiącu (NIE zawsze — ~90% przypadków, ale wymagana tylko gdy stosowana) | art. 31b ust. 1 PIT | zaniżenie zaliczki PIT bez podstawy → zaległość podatkowa płatnika + odsetki; ryzyko sankcji KKS; rezerwa na zaległość | PEWNE |
| Oświadczenie o podwyższonych (zamiejscowych) KUP | gdy `KUP = 300 zł` (zamiejscowe) zamiast 250 zł | art. 22 ust. 2 pkt 3 + art. 32 ust. 6 PIT (oświadczenie pracownika); na formularzu PIT-2(9) | zawyżone KUP → zaniżona zaliczka → zaległość płatnika + odsetki; rezerwa | PEWNE |
| Wniosek/oświadczenie o rezygnacji z ulgi <26 | gdy pracownik <26 lat ALE ulga NIE zastosowana (pobrano zaliczkę) | art. 21 ust. 1 pkt 148 PIT (ulga z automatu); rezygnacja na wniosek | bez wniosku pobór zaliczki <26 jest błędny → nadpłata u pracownika, ale formalna luka; przy stosowaniu ulgi z automatu — brak dokumentu OK | PEWNE |
| Dokumentacja absencji chorobowej: e-ZLA (ZUS ZLA) | gdy `wynagrodzenie_chorobowe > 0` lub `zasilek > 0` | art. 92 KP; ustawa zasiłkowa; e-ZLA wpływa do płatnika z ZUS | wypłata świadczenia bez podstawy = świadczenie nienależne; zawyżenie/zaniżenie podstawy zasiłku; ryzyko zwrotu do ZUS → rezerwa | PEWNE |
| Wnioski urlopowe + ewidencja urlopu | gdy `urlop_wypoczynkowy/inny > 0 dni` w miesiącu | art. 161–167 KP; rozp. 2018 (dokumentacja urlopowa) | brak dowodu udzielenia urlopu; spór o ekwiwalent/zaległy urlop → rezerwa na ekwiwalent | PEWNE |
| Deklaracja rezygnacji z PPK | gdy pracownik w wieku PPK (18–55) ALE `wpłata_PPK = 0` | ustawa o PPK; wzór deklaracji (rozp. wykonawcze) | brak naliczenia wpłat bez ważnej rezygnacji = zaległość wobec instytucji finansowej + sankcje; rezerwa na zaległe wpłaty | PEWNE |
| Deklaracja przystąpienia / wpłaty dodatkowej PPK | gdy `wpłata_PPK_dodatkowa > 0` lub przystąpienie 55–70 lat | ustawa o PPK | naliczenie bez podstawy → korekta | DO_POTWIERDZENIA (zależy od konfiguracji programu PPK) |
| Tytuł wykonawczy / zawiadomienie o zajęciu (komornik/ZUS/US) | gdy `potracenie_egzekucyjne > 0` w liście płac | art. 87 §1 KP (egzekucja na mocy tytułów wykonawczych); art. 88, 89 KP | potrącenie bez tytułu = bezpodstawne → roszczenie pracownika + odpowiedzialność pracodawcy wobec wierzyciela (art. 882 KPC); rezerwa | PEWNE |
| Pisemna zgoda pracownika na potrącenie dobrowolne | gdy `potracenie_dobrowolne > 0` (inne niż egzekucyjne, art. 87 §1) | art. 91 KP (zgoda na piśmie) | potrącenie bez zgody = bezpodstawne, roszczenie pracownika; wykroczenie | PEWNE |
| Oświadczenie/dokument do 50% KUP (utwór, przeniesienie praw) | gdy `KUP_autorskie (50%) > 0` w UoP | art. 22 ust. 9 pkt 3, ust. 9a–9b PIT; ewidencja utworów | zawyżone KUP bez dowodu powstania utworu → zaniżona zaliczka → zaległość płatnika; rezerwa | PEWNE |

## MACIERZ — UMOWA ZLECENIA

### Bezwarunkowe
| Dokument | Warunek | Podstawa | Konsekwencja braku | Pewność |
|---|---|---|---|---|
| Umowa zlecenia (+ stawka godzinowa, aneksy) | zawsze | art. 734 KC; ustawa o min. wynagrodzeniu (stawka godz.) | brak podstawy naliczenia; ryzyko sporu o min. stawkę godzinową | PEWNE |
| Ewidencja liczby godzin wykonania zlecenia | zawsze gdy obowiązuje min. stawka godzinowa (większość zleceń) | art. 8b ustawy o min. wynagrodzeniu | brak dowodu na min. stawkę → roszczenie zleceniobiorcy + wykroczenie (art. 8e ustawy o min. wynagr., grzywna 1000–30000 zł) | PEWNE |
| Oświadczenie zleceniobiorcy do celów ubezpieczeń (ZUS) | zawsze (ustala tytuł/zbieg/zwolnienie) | ustawa o sus art. 6, 9; rozp. zgłoszeniowe | błędne oskładkowanie → zaległość składkowa ZUS + odsetki + opłata dodatkowa; rezerwa | PEWNE |

### Warunkowe
| Dokument | Warunek (parametr) | Podstawa | Konsekwencja braku | Pewność |
|---|---|---|---|---|
| Zaświadczenie/oświadczenie o statusie ucznia/studenta <26 | gdy `skladki_ZUS = 0` z tytułu statusu studenta <26 | art. 6 ust. 4 ustawy o sus | brak dowodu na zwolnienie → ZUS zakwestionuje → zaległość składkowa wstecz + odsetki; rezerwa (wysokie ryzyko) | PEWNE |
| Oświadczenie o zbiegu tytułów (inny tytuł rodzi obowiązek składkowy) | gdy `skladki_spoleczne = 0` z powodu innego tytułu (np. UoP gdzie indziej z podstawą ≥ min. wynagr.) | art. 9 ustawy o sus | brak dowodu → ZUS dolicza składki → zaległość + odsetki; rezerwa | PEWNE |
| PIT-2/oświadczenie o kwocie zmniejszającej (zlecenie) | gdy `kwota_zmniejszajaca > 0` zastosowana (możliwe od 2023 dla zleceń) | art. 31b ust. 1 w zw. z art. 41 ust. 1 PIT | zaniżona zaliczka bez podstawy → zaległość płatnika; rezerwa | PEWNE |
| Wniosek o niestosowanie 20% KUP / oświadczenie | gdy zastosowano KUP inny niż domyślne 20% | art. 22 ust. 9 pkt 4 PIT | błędne KUP → korekta zaliczki | DO_POTWIERDZENIA |
| Oświadczenie do 50% KUP + przeniesienie praw autorskich | gdy `KUP_autorskie (50%) > 0` przy zleceniu twórczym | art. 22 ust. 9 pkt 3 PIT; postanowienia o przeniesieniu praw w umowie | zawyżone KUP → zaniżona zaliczka → zaległość; rezerwa | PEWNE |
| Wniosek o rezygnację z ulgi <26 (zlecenie) | gdy zleceniobiorca <26 ALE pobrano zaliczkę | art. 21 ust. 1 pkt 148 PIT | jak przy UoP — ulga z automatu, rezygnacja na wniosek | PEWNE |
| Tytuł wykonawczy / zajęcie wierzytelności zlecenia | gdy `potracenie_egzekucyjne > 0` | art. 87 §1 KP stosowany odpowiednio (art. 833 §2 KPC — kwota wolna dla świadczeń powtarzających się); tytuł wykonawczy | potrącenie bez tytułu = bezpodstawne; odpowiedzialność wobec wierzyciela; rezerwa | PEWNE |

## MACIERZ — UMOWA O DZIEŁO

### Bezwarunkowe
| Dokument | Warunek | Podstawa | Konsekwencja braku | Pewność |
|---|---|---|---|---|
| Umowa o dzieło (z określonym dziełem/rezultatem) | zawsze | art. 627 KC | ryzyko przekwalifikowania na zlecenie/UoP przez ZUS → zaległość składkowa; rezerwa (kluczowe ryzyko dzieła) | PEWNE |
| Zgłoszenie umowy o dzieło do ZUS (RUD) | zawsze, gdy płatnik składek (formularz RUD w 7 dni) | art. 36 ust. 17 ustawy o sus | brak zgłoszenia RUD = wykroczenie (art. 98 ustawy o sus, grzywna do 5000 zł) | PEWNE |

### Warunkowe
| Dokument | Warunek (parametr) | Podstawa | Konsekwencja braku | Pewność |
|---|---|---|---|---|
| Oświadczenie + zapis o przeniesieniu praw autorskich (50% KUP) | gdy `KUP_autorskie (50%) > 0` (dzieło twórcze) | art. 22 ust. 9 pkt 3, ust. 9b PIT; ustawa o prawie autorskim | zawyżone KUP bez utworu/przeniesienia praw → zaniżona zaliczka → zaległość płatnika; rezerwa | PEWNE |
| Wyodrębnienie honorarium autorskiego w umowie | gdy część wynagrodzenia objęta 50% KUP | art. 22 ust. 9 pkt 3 PIT; interpretacja ogólna MF | bez wyodrębnienia 50% KUP zakwestionowane → korekta; rezerwa | PEWNE |
| Limit 120 000 zł 50% KUP — monitoring narastająco | gdy suma `KUP_autorskie` w roku zbliża się do limitu | art. 22 ust. 9a PIT | przekroczenie limitu → zaniżona zaliczka; rezerwa | PEWNE |

## Pułapki (najczęstsze luki audytowe)
1. PIT-2 traktowany jako „zawsze wymagany" — błąd. Wymagany TYLKO gdy w miesiącu zastosowano kwotę zmniejszającą. Brak PIT-2 przy NIEstosowaniu kwoty zmniejszającej to nie luka. Luka = stosowanie kwoty zmniejszającej BEZ oświadczenia.
2. Ulga <26 — działa z automatu, NIE wymaga dokumentu od pracownika. Dokument (wniosek) potrzebny TYLKO przy rezygnacji. Audytor nie powinien generować braku „brak oświadczenia o uldze <26".
3. Wiek do ulgi <26 i do PPK — weryfikacja przez datę urodzenia (akta osobowe), nie przez osobne oświadczenie.
4. Ewidencja czasu pracy — mit, że „pensja miesięczna = brak ewidencji". Ewidencja (karta) wymagana zawsze; wyłączone jest tylko ewidencjonowanie GODZIN dla zadaniowych/zarządzających/ryczałtowców (art. 149 §2 KP).
5. Status studenta <26 (zlecenie) — bez zaświadczenia/oświadczenia ZUS może zakwestionować zwolnienie wstecz → duża zaległość składkowa. Krytyczny dokument warunkowy.
6. Umowa o dzieło — brak zgłoszenia RUD do ZUS oraz ryzyko przekwalifikowania; to częsta rezerwa.
7. Potrącenia — egzekucyjne wymagają TYTUŁU/zajęcia (art. 87 §1 KP, bez zgody pracownika), dobrowolne wymagają pisemnej ZGODY (art. 91 KP). Pomylenie podstaw = bezpodstawne potrącenie.
8. Zamiejscowe KUP — od 2023 oświadczenie na PIT-2(9), nie odrębny druk; ważne do odwołania.
9. Kara art. 281 KP rośnie od 8.07.2026 (do 60000 zł, szczególne 90000 zł) — przy wycenie rezerwy uwzględnić moment ujawnienia/utrzymywania się naruszenia.

## Changelog
- 2026-06-23 — utworzenie tematu. Zweryfikowano w źródłach pierwotnych:
  - art. 31b PIT (oświadczenie kwota zmniejszająca), kwota zmniejszająca 300 zł / kwota wolna 30000 zł bez zmian w 2026 — sip.lex.pl/.../art-31-b ; pit.pl
  - art. 22 ust. 2 pkt 3 PIT + oświadczenie zamiejscowe na PIT-2(9), ważne do odwołania — pit.pl ; pckp.pl
  - ulga <26 art. 21 ust.1 pkt 148 PIT stosowana z automatu, oświadczenie tylko przy rezygnacji — podatki.biz (objaśnienia MF)
  - 50% KUP art. 22 ust. 9 pkt 3 PIT, limit 120000 zł w 2026 — e-pity.pl ; gofin (interpretacja ogólna MF)
  - PPK: ponowny autozapis — wpłaty wznawiane 1.04.2027, rezygnacje do 28.02.2027 tracą skuteczność 1.03.2027 — mojeppk.pl
  - student <26 / zbieg tytułów ZUS — status studenta = brak ubezpieczeń z umowy zlecenia, zaświadczenie/oświadczenie jako dowód — poradnikprzedsiebiorcy.pl ; podatki.biz
  - potrącenia art. 87, 87(1) KP; kwota wolna = min. wynagr. (4806 zł brutto w 2026) — lexlege.pl/kp/art-87 ; pip.gov.pl
  - rozp. dokumentacja pracownicza Dz.U. 2018 poz. 2369 (t.j. Dz.U. 2024 poz. 535) — isap.sejm.gov.pl
  - art. 281 § 1 pkt 6/6a KP: grzywna 1000–30000 zł OBECNIE; nowelizacja podnosi do 60000/90000 zł od 8.07.2026 (Dz.U. 2025 poz. 277 z późn. zm.) — lexlege.pl/kp/art-281 ; gazetaprawna.pl ; pckp.pl

## Źródła (pierwotne / autorytatywne)
- ISAP: https://isap.sejm.gov.pl (Kodeks pracy; rozp. dokumentacja pracownicza WDU20180002369; t.j. WDU20240000535)
- Ustawa PIT t.j. Dz.U. 2026 poz. 592 — sip.lex.pl (art. 31b, 32, 22)
- Objaśnienia podatkowe MF — ulga <26 (podatki.biz / podatki.gov.pl)
- MojePPK (autozapis, deklaracje): https://www.mojeppk.pl
- PIP (kwota wolna od potrąceń): https://www.pip.gov.pl
- ZUS (status studenta, RUD): https://www.zus.pl
