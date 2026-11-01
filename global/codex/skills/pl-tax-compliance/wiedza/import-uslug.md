# Import usług (UE i spoza UE) — wymogi formalne

ostatnia_weryfikacja: 2026-06-27
podstawy_prawne: art. 2 pkt 9, art. 17 ust. 1 pkt 4, art. 28b, art. 19a (ust. 1, 3, 8), art. 31a (ust. 1, 1a), art. 29a, art. 86 (ust. 1, 2 pkt 4 lit. a, 10, 10b pkt 3, 10i) ustawy o VAT (Dz.U.2025.775 t.j.); rozporządzenie MF ws. JPK_VAT z deklaracją (JPK_V7M(3) od 1.02.2026); rozp. MF ws. KPiR (kol. 13); art. 11a ust. 2, art. 22 ust. 1 ustawy o PIT
pewnosc: wysoka (mapowanie pól JPK i zasady ogólne); do_potwierdzenia: indywidualne kwalifikacje miejsca świadczenia inne niż art. 28b

## Reguła bazowa (reverse charge u nabywcy)
- Import usług = nabycie usługi, dla której podatnikiem jest nabywca (art. 17 ust. 1 pkt 4, art. 2 pkt 9). Usługodawca bez siedziby/stałego MPD w PL; nabywca = podatnik.
- Miejsce świadczenia usług „na zasadach ogólnych" (SaaS/AI/licencje) = siedziba usługobiorcy (PL), art. 28b ust. 1.
- Traktowanie VAT UE vs spoza UE: IDENTYCZNE co do mechanizmu. Różnica wyłącznie w polu JPK (patrz niżej).

## Obowiązek podatkowy (art. 19a)
- Zasada: z chwilą wykonania usługi (ust. 1).
- Subskrypcje / okresy rozliczeniowe: z upływem każdego okresu (ust. 3).
- Przedpłata / prepaid credits otrzymana przed wykonaniem: z chwilą zapłaty, co do otrzymanej kwoty (ust. 8).
- W imporcie usług data faktury zagranicznej NIE wyznacza obowiązku podatkowego.
- Potwierdzenie subskrypcji: interpretacja KIS 0111-KDIB3-3.4012.606.2024.2.AW (31.03.2025).

## Kurs przeliczenia (art. 31a)
- Kurs średni NBP z ostatniego dnia roboczego poprzedzającego dzień powstania obowiązku podatkowego (D-1), ust. 1.
- Opcja: kurs EBC z ostatniego dnia poprzedzającego (ust. 1a) — wybór konsekwentny.
- Faktura wystawiona w PLN (np. kontrahent unijny — nazwa zredagowana w edycji publicznej) — kwotę bierze się WPROST, art. 31a nie ma zastosowania (dotyczy tylko kwot w walucie obcej).
- PIT/koszt: osobny kurs — średni NBP z dnia roboczego poprzedzającego poniesienie kosztu (art. 11a ust. 2 PIT). Może różnić się od kursu VAT.

## JPK_V7M (od 1.02.2026: JPK_V7M(3))
- VAT NALEŻNY:
  - Import usług od podatnika z UE wg art. 28b: K_29/P_29 (podstawa), K_30/P_30 (podatek).
  - Pozostały import usług — CAŁY spoza UE (USA) + UE przy innym miejscu świadczenia: K_27/P_27 (podstawa), K_28/P_28 (podatek).
- VAT NALICZONY (jeśli odliczenie przysługuje): K_42/P_42 (netto) + K_43/P_43 (podatek) — pozostałe nabycia. Środek trwały: K_40/P_40 + K_41/P_41.
- P_44/P_45 = KOREKTY naliczonego (44 ST, 45 pozostałe); P_46 = korekta art. 89b ust. 1 (ulga na złe długi). NIE do bieżącego importu usług.
- Oznaczenie procedury „IMP" = WYŁĄCZNIE import TOWARÓW (art. 33a), strona naliczonego. NIE dla importu usług. Import usług nie ma kodu procedury.
- GTU (w tym GTU_12) = tylko sprzedaż/dostawy podatnika. NIE dla nabyć skutkujących VAT należnym u nabywcy (import usług, WNT).
- Od 1.02.2026 nowe oznaczenia dowodu w JPK_V7M(3): NrKSeF / OFF (brak nr KSeF) / BFK / DI (dowód inny niż faktura) — dla faktur zagranicznych (poza KSeF) stosować odpowiednio; numeracja pól importu usług bez zmian.

## KPiR i neutralność
- Koszt usług AI/subskrypcji: kolumna 13 „Pozostałe wydatki", kwota NETTO w PLN.
- VAT należny i (odliczalny) naliczony nie są kosztem.
- Neutralność: u czynnego podatnika należny = naliczony w tym samym okresie (art. 86 ust. 1, 2 pkt 4 lit. a, 10b pkt 3), o ile usługa służy czynnościom opodatkowanym.
- Brak prawa do odliczenia → należny bez naliczonego; nieodliczalny VAT może być kosztem.

## Pułapki
- „IMP" do importu usług (BŁĄD — tylko towary).
- GTU_12 do zakupów reverse charge (BŁĄD — tylko sprzedaż).
- Spoza UE w P_29/P_30 (BŁĄD — to pola dla art. 28b od podatnika z UE).
- Przeliczanie faktury w PLN po kursie (BŁĄD).
- Kurs z daty faktury zamiast D-1 do obowiązku; dla prepaid pominięcie, że decyduje data zapłaty.
- Należny bez naliczonego (rozjazd) przy pełnym prawie do odliczenia.

## Changelog
- 2026-06-27: utworzenie tematu. Zweryfikowano art. 17/19a/28b/31a/86 (Dz.U.2025.775), strukturę JPK (broszura MF JPK_VAT z deklaracją od 1.02.2026 — JPK_V7M(3), nowe oznaczenia NrKSeF/OFF/BFK/DI), potwierdzenie IMP=tylko towary oraz GTU=tylko sprzedaż. Źródła: isap/OpenLEX, podatki.gov.pl (broszura), KIS 0111-KDIB3-3.4012.606.2024.2.AW.

## Źródła
- OpenLEX art. 31a / 19a / 86 (Dz.U.2025.775 t.j.)
- przepisy.gofin.pl — brzmienie art. 31a
- podatki.gov.pl — broszura JPK_VAT z deklaracją od 1.02.2026
- KIS 0111-KDIB3-3.4012.606.2024.2.AW (subskrypcje)
- ZrozumVAT / MDDP — mapowanie pól JPK, GTU, IMP
