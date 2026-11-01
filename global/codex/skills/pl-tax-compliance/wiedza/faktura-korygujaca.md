# Wiedza: Faktura korygująca (PL VAT + KSeF FA(3))

> temat: faktura-korygujaca
> ostatnia_weryfikacja: 2026-07-03
> pewnosc: wysoka
> podstawy_prawne: ustawa o VAT (Dz.U.2025.775 t.j.) art. 106e ust. 1, art. 106j ust. 1-3; ustawa o KSeF; struktura logiczna FA(3) (wzór 2025/06/25, wersjaSchemy 1-0E, obowiązkowa od 2026-02-01)

## Kiedy wystawia się korektę (art. 106j ust. 1)
Po wystawieniu faktury, gdy: udzielono obniżki/rabatu, podwyższono cenę lub stwierdzono pomyłkę w cenie/stawce/kwocie podatku/jakiejkolwiek innej pozycji faktury, dokonano zwrotu towaru/zapłaty, zwrócono nabywcy całość/część zapłaty.
Pomyłka w danych formalnych (np. nazwa/identyfikacja towaru, dane nabywcy) = korekta „treści" (NIE wpływa na podstawę opodatkowania ani kwotę podatku).

## MUST-HAVE — elementy obowiązkowe (art. 106j ust. 2)
1. **Numer kolejny oraz data wystawienia** korekty.
2. **Numer identyfikujący w KSeF faktury korygowanej** (dla faktur ustrukturyzowanych) — w FA(3): `NrKSeFFaKorygowanej` (+ `NrKSeF`=1 gdy oryginał miał nr KSeF; `NrKSeFN`=1 gdy nie miał).
3. **Dane z faktury korygowanej**:
   - elementy z **art. 106e ust. 1 pkt 1-5**: (1) data wystawienia oryginału, (2) numer oryginału, (3) nazwy/imiona i adresy sprzedawcy i nabywcy, (4) NIP sprzedawcy, (5) NIP nabywcy;
   - **nazwa (rodzaj) towaru lub usługi objętych korektą** (identyfikacja korygowanej pozycji = „stan przed").
4. Zależnie od typu korekty:
   - **korekta WARTOŚCIOWA** (zmienia podstawę opodatkowania lub kwotę podatku): **kwota korekty podstawy/kwoty podatku** z podziałem na stawki i sprzedaż zwolnioną;
   - **korekta FORMALNA / „treści"** (NIE zmienia podstawy ani podatku): **prawidłowa treść korygowanych pozycji** (czyli „stan po" — poprawna nazwa/dane).

> Wniosek dla korekty formalnej: na dokumencie muszą znaleźć się JEDNOCZEŚNIE: identyfikacja korygowanej pozycji (nazwa „przed") ORAZ prawidłowa treść („po"). W FA(3) realizuje to metoda ze znacznikiem `StanPrzed`.

## NICE-TO-HAVE — elementy fakultatywne (art. 106j ust. 2a)
- Wyraz **„FAKTURA KORYGUJĄCA"** albo **„KOREKTA"** (w FA(3) typ wynika z `RodzajFaktury=KOR`/`KOR_ZAL`/`KOR_ROZ`).
- **Przyczyna korekty** — od nowelizacji **FAKULTATYWNA** (decyzja wystawcy). Zalecane: i tak podać (czytelność, obrona w razie kontroli). W FA(3): `PrzyczynaKorekty`.
- Data dokonania/zakończenia dostawy lub data otrzymania zapłaty, jeśli różna od daty wystawienia.

## Warstwa KSeF — struktura FA(3) dla korekty (kolejność w `<Fa>`)
Potwierdzone na realnej, przyjętej przez KSeF korekcie:
1. `RodzajFaktury` = **KOR** (zwykła) / `KOR_ZAL` (do zaliczkowej) / `KOR_ROZ` (do rozliczeniowej).
2. `PrzyczynaKorekty` (fakultatywna, zalecana).
3. `DaneFaKorygowanej`: `DataWystFaKorygowanej`, `NrFaKorygowanej`, `NrKSeF` (1=miała nr KSeF), `NrKSeFFaKorygowanej`.
4. `P_15ZK` — **tylko** dla korekt faktur **zaliczkowych/rozliczeniowych** (kwota zapłaty/pozostała do zapłaty przed korektą). Dla zwykłej faktury **NIE używać**.
5. `FaWiersz` — pozycje wg jednej z 3 metod (niżej).

### KLUCZOWA ZASADA SUM W KOREKCIE (broszura FA(3) MF, str. 42, 52)
„W przypadku wystawienia faktury korygującej wypełnia się wszystkie pola wg STANU PO KOREKCIE, a pola dotyczące podstaw opodatkowania, podatku oraz NALEŻNOŚCI OGÓŁEM wypełnia się PRZEZ RÓŻNICĘ."
- `P_15` (Kwota należności ogółem) — element **OBOWIĄZKOWY** w `Fa`; w korekcie = **korekta kwoty z faktury korygowanej (RÓŻNICA)**. Korekta bez zmiany kwot → **P_15 = 0**.
- `P_13_1..P_13_11`, `P_14_*` (w tym `P_13_11` dla marży) — w korekcie = **kwota RÓŻNICY** wg stawek. Korekta bez zmiany kwot → **0** (można pominąć pola opcjonalne; P_15=0 obowiązkowe).
- `P_15ZK` — patrz wyżej: TYLKO zaliczki/rozliczeniowe; przy zwykłym KOR NIE używać.

### 3 metody korygowania pozycji (FaWiersz)
- **Metoda 1 — różnicowa**: 1 wiersz z RÓŻNICĄ kwot (brak `StanPrzed`). Dla zmiany samej nazwy bez zmiany kwot — różnica zerowa.
- **Metoda 2 — `StanPrzed`** (ZALECANA, broszura str. 101-103, Przykład 23 met. 2): 2 wiersze o **TYM SAMYM `NrWierszaFa`** — wiersz „przed" ma `StanPrzed`=1 i **WARTOŚCI DODATNIE** (stan z faktury pierwotnej), wiersz „po" ma poprawne dane i **WARTOŚCI DODATNIE** (stan docelowy). Przy korekcie czysto formalnej oba wiersze mają IDENTYCZNE kwoty (potwierdzenie braku zmiany wartości). UWAGA: `StanPrzed` to OSTATNI element w `FaWiersz` (po `P_12`, `KursWaluty` itd.), nie na początku.
- **Metoda 3 — storno**: jak met. 2, ale zamiast znacznika `StanPrzed` wiersz „przed" ma **znak przeciwny (ujemny)**; wiersz „po" dodatni. (Nie mieszać metod 2 i 3 — albo StanPrzed+dodatnie, albo storno-ujemne bez StanPrzed.)

### Procedura marży (P_PMarzy)
Korekta dziedziczy procedurę z oryginału: zachować `Adnotacje/PMarzy` (`P_PMarzy`, `P_PMarzy_3_1`). W marży pozycje używają `P_9B`/`P_11A` (brutto). Pola sumaryczne `P_13_11`/`P_15` = **RÓŻNICA** (0 przy korekcie bez zmiany kwot) — NIE kwota z oryginału.

## Prezentacja (wizualizacja/PDF) vs XML — NIE mylić
- W **XML/KSeF**: `P_15` i pola podstaw = RÓŻNICA (przy korekcie bez zmiany kwot = 0). To NIE znaczy „skorygowano wartość do 0" — znaczy „korekta zmienia należność o 0 zł".
- W **dokumencie dla człowieka** (PDF/wizualizacja) korekty NIE pokazuje się „DO ZAPŁATY 0". Pokazuje się: sekcja **STAN PRZED KOREKTĄ** (pełne kwoty), sekcja **STAN PO KOREKCIE** (pełne kwoty), a na dole **ROZLICZENIE KOREKTY**: należność przed / po / RÓŻNICA = do zapłaty lub zwrotu. Pełne kwoty są w `FaWiersz` (stan przed = StanPrzed, stan po) — różnica liczona jako (po − przed).

## Kolejne korekty tej samej faktury (2., 3., …)
- `DaneFaKorygowanej` wskazuje **ZAWSZE ORYGINAŁ** (nr + nr KSeF faktury pierwotnej), NIGDY poprzedniej korekty (Podręcznik KSeF 2.0 cz. II).
- „Stan przed" kolejnej korekty = **stan PO poprzednich korektach** (merytorycznie), nie stan z oryginału.
- Numeracja własna kolejna (np. KOR/N/ROK/2).

## Korekta pól NAGŁÓWKOWYCH (np. P_6 — data dokonania dostawy)
- Pola nagłówkowe węzła `Fa` (P_6 itd.) koryguje się podając w korekcie wartość wg **STANU PO KOREKCIE** (broszura: wszystkie pola wg stanu po, tylko podstawy/podatek/należność przez różnicę). Brak operacji na P_6 w FaWiersz (wierszowy odpowiednik P_6A — tylko gdy używany).
- Korektę DATY dostawy wystawia się WYŁĄCZNIE gdy data jest FAKTYCZNIE błędna (art. 106j ust. 1 pkt 5). Korekta wpisująca datę nieprawdziwą (np. pod presją urzędu) = faktura NIERZETELNA — art. 62 § 2 KKS (do 720 stawek dziennych albo pozbawienie wolności ≥1 rok); brak skutku fiskalnego NIE uchyla odpowiedzialności.

## NOTY KORYGUJĄCE — NIE ISTNIEJĄ od 1.02.2026
- **Art. 106k UCHYLONY z dniem 1.02.2026** (pakiet KSeF) — całkowita likwidacja not korygujących, w KSeF i poza nim. Jedyna droga poprawienia faktury = faktura korygująca wystawiona przez sprzedawcę.

## Pułapki / ważne
- **Brak nr KSeF korygowanej** = błąd krytyczny (powiązanie z oryginałem).
- **Wskazanie w DaneFaKorygowanej poprzedniej korekty zamiast oryginału** = błąd powiązania.
- **Korekta daty/danych na wartości niezgodne z rzeczywistością** „żeby zadowolić urząd" = nierzetelność (KKS art. 62 § 2) — właściwym narzędziem jest pismo wyjaśniające, nie korekta.
- Korekta formalna **nie może** zmieniać kwot ani powielać przychodu — w księgach/JPK netto korekty = 0 (nie dubluj wartości; nie twórz nowego przychodu w KPiR).
- Dane nabywcy koryguje się przez `Podmiot2K` (pełne, poprawne dane nabywcy), gdy korekta dotyczy nabywcy — nie przez nadpisanie `Podmiot2`.
- KSeF obowiązkowy: duzi podatnicy od 2026-02-01, pozostali od 2026-04-01; jedyna struktura = FA(3) od 2026-02-01.
- Korektę faktury z KSeF wystawia się również w KSeF (nie „poza systemem"), chyba że wyjątek (awaria/offline).

## Changelog (wpisy autoaktualizacji)
- 2026-07-03 — DODANO (case — dane prywatne zredagowane w edycji publicznej): (a) art. 106k UCHYLONY od 1.02.2026 — noty korygujące zlikwidowane całkowicie; (b) kolejne korekty: DaneFaKorygowanej zawsze do ORYGINAŁU, stan przed = stan po poprzednich korektach; (c) korekta pól nagłówkowych (P_6 data dostawy) wg stanu po korekcie; (d) korekta daty tylko przy FAKTYCZNEJ pomyłce — wpisanie daty nieprawdziwej = art. 62 § 2 KKS (nierzetelna, do 720 stawek), nawet bez skutku fiskalnego. Źródła: rachunkowosc.com.pl (uchylenie 106k), pit.pl/podręcznik KSeF 2.0 (kolejne korekty), broszura FA(3).
- 2026-06-23 (popr. 2) — KOREKTA na podstawie autorytatywnej broszury FA(3) MF (wrzesień 2025, str. 42, 52, 86-87, 101-103). SPROSTOWANO błędy seedu: (a) pola sum w korekcie (`P_15` obowiązkowe, `P_13_11` marża) wypełnia się PRZEZ RÓŻNICĘ → przy korekcie bez zmiany kwot = 0 (nie kwota oryginału); (b) metoda `StanPrzed` używa wartości DODATNICH w obu wierszach o tym samym `NrWierszaFa`, a `StanPrzed`=1 jest OSTATNIM elementem `FaWiersz` (wartości ujemne to osobna metoda — storno). Potwierdzono: `Zaplacono`/`DataZaplaty` w `Platnosc`; tytuł na wizualizacji wynika z `RodzajFaktury`.
- 2026-06-23 — utworzenie bazy. Seed na podstawie art. 106e/106j ustawy o VAT (t.j. Dz.U.2025.775), struktury FA(3) 1-0E (obowiązkowej od 2026-02-01) oraz realnej korekty przyjętej przez KSeF. Potwierdzono fakultatywność przyczyny korekty (art. 106j ust. 2a) i wymóg „prawidłowej treści korygowanych pozycji" dla korekt formalnych.

## Źródła (rejestr do weryfikacji aktualności)
- ISAP/ustawa o VAT art. 106j: https://sip.lex.pl/akty-prawne/dzu-dziennik-ustaw/podatek-od-towarow-i-uslug-17086198/art-106-j
- lexlege art. 106j: https://lexlege.pl/ustawa-o-podatku-od-towarow-i-uslug/art-106j/
- Broszura FA(3) (MF): https://ksef.podatki.gov.pl/media/0ivha0ua/broszura-informacyjna-dotyczaca-struktury-logicznej-fa-3.pdf
- Podręcznik KSeF 2.0 cz. II: https://ksef.podatki.gov.pl/media/cark5buy/podrecznik-ksef-20-czesc-ii-wystawianie-i-otrzymywanie-faktur-w-ksef-19022026.pdf
- 3 metody korygowania pozycji: https://ksiegowosc.infor.pl/ksef/7596006,faktura-korygujaca-w-ksef-in-plus-i-in-minus-kiedy-wystawiac-i-jak-rozliczyc-3-metody-korygowania-pozycji-faktury-w-ksef.html
