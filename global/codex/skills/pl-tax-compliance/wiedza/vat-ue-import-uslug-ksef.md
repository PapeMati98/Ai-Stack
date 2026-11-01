# VAT-UE, import usług (reverse charge), KSeF wobec faktur zagranicznych, dowód w KPiR

ostatnia_weryfikacja: 2026-06-27
podstawy_prawne: ustawa o VAT (Dz.U.2025.775 t.j.) art. 17 ust. 1 pkt 4, art. 28b, art. 86 ust. 2 pkt 4 lit. a, art. 97 ust. 1 i ust. 3, art. 99, art. 100 ust. 1, art. 109 ust. 3; rozp. MFiG z 6.09.2025 ws. prowadzenia KPiR (Dz.U.2025.1299, od 1.01.2026); ustawa o PIT art. 22 ust. 1, art. 24c; ustawa o KSeF (nowela z 5.08.2025)
pewnosc: wysoka (dokładny nr paragrafu dowodu księgowego w nowym rozp. KPiR: do_potwierdzenia)

## 1. Rejestracja VAT-UE (art. 97)
- Nabycie usług art. 28b OD KONTRAHENTA Z UE (import usług) → OBOWIĄZEK rejestracji VAT-UE przed pierwszą transakcją (art. 97 ust. 1 w zw. z ust. 3 pkt 1).
- Nabycie usług OD KONTRAHENTA SPOZA UE (USA) → BRAK obowiązku rejestracji VAT-UE (art. 97 ust. 3 pkt 1 ogranicza się do transakcji UE). Import usług z USA i tak rozlicza się w JPK_V7M (art. 17 ust. 1 pkt 4, art. 28b).
- Brak VAT-UE NIE zwalnia z rozliczenia importu usług w JPK_V7M (to dwa różne obowiązki).

## 2. Informacja podsumowująca VAT-UE (art. 100 ust. 1) — KATALOG ZAMKNIĘTY
- pkt 1: WDT; pkt 2: WNT towarów; pkt 3: dostawa w proc. uproszczonej (art. 136); pkt 4: ŚWIADCZENIE usług art. 28b NA RZECZ podatnika z UE (sprzedaż, gdzie zobowiązany do zapłaty = usługobiorca).
- IMPORT (NABYCIE) USŁUG NIE WCHODZI do VAT-UE — ani z UE, ani spoza UE. Trafia WYŁĄCZNIE do JPK_V7M (reverse charge: VAT należny + naliczony).
- PUŁAPKA: nie mylić kierunku pkt 4 (sprzedaż NA RZECZ UE) z nabyciem. Nie mylić rejestracji VAT-UE (art. 97, status) z informacją VAT-UE (art. 100, co raportujesz).

## 3. KSeF 2026 — daty i faktury zagraniczne
- Duzi podatnicy (sprzedaż z VAT > 200 mln zł w 2024): obowiązkowy KSeF od 1.02.2026.
- Pozostali: od 1.04.2026. Najmniejsi (sprzedaż <= 10 000 zł/mies.): od 1.01.2027.
- Odbieranie faktur w KSeF: obowiązek od 1.02.2026. Okres przejściowy (faktury <=10 000 zł brutto poza KSeF) i kary za brak nr KSeF od 1.01.2027. Podstawa: ustawa z 5.08.2025.
- Dostawcy zagraniczni bez siedziby i bez stałego miejsca prowadzenia działalności (FE) w PL: NIE wystawiają faktur w KSeF (mogą dobrowolnie). Faktury za import usług nie pojawią się w KSeF → ksef_number = null jest POPRAWNY, nie defekt.
- WNIOSEK: faktury dostawców spoza UE oraz dostawców unijnych bez FE w PL (nazwy dostawców zredagowane w edycji publicznej) — NIGDY nie będą w KSeF.

## 4. Dowód księgowy w KPiR / podstawa odliczenia
- Od 1.01.2026 obowiązuje NOWE rozp. KPiR (Dz.U.2025.1299 z 6.09.2025), zastępuje rozp. z 23.12.2019. Struktura: rozdz. 2 = warunki szczegółowe/dowody (dokładny numer paragrafu = do_potwierdzenia z pełnego PDF).
- Zapisy w KPiR tylko na podstawie rzetelnych i prawidłowych dowodów; faktura (w tym zagraniczna) = dowód księgowy. Dokument w walucie obcej musi mieć przeliczenie na PLN.
- VAT: prawo do odliczenia przy imporcie usług nie zależy od posiadania idealnej faktury, lecz od wykazania VAT należnego (art. 86 ust. 2 pkt 4 lit. a). Koszt PIT: art. 22 ust. 1.
- WNIOSEK PRAKTYCZNY: WYMUSZENIE załącznika PDF dla faktur spoza KSeF (ksef_number=null) jest ZASADNE — PDF to jedyny nośnik dowodu dla tych dostawców (dobra praktyka kontroli kompletności, realizuje obowiązek posiadania dowodu).

## 5. Dane nabywcy na fakturze (JDG)
- Faktura na samo imię (imię i nazwa firmy JDG zredagowane w edycji publicznej) z PL NIP → wystarczająca do kosztu i rozliczenia importu usług (identyfikacja przez NIP). Pełna firma MOŻE być, ale NIE jest warunkiem koniecznym.
- Zalecane (nie wymagane): korekta danych u dostawcy do pełnej nazwy dla spójności z VAT-R.

## Pułapki
- Wymuszanie pozycji VAT-UE dla faktur KOSZTOWYCH (import usług) = BŁĄD; do VAT-UE idzie tylko SPRZEDAŻ usług art. 28b na rzecz UE.
- Traktowanie braku nr KSeF u dostawcy zagranicznego jako defektu = BŁĄD.
- Pominięcie rozliczenia importu usług w JPK_V7M (także z USA) = realny błąd.

## Changelog
- 2026-06-27 — utworzenie tematu. Zweryfikowano: art. 97 ust. 3 pkt 1 (rejestracja VAT-UE tylko import usług z UE), art. 100 ust. 1 (import usług poza VAT-UE), daty KSeF 2026/2027 (ustawa 5.08.2025), zagraniczni dostawcy bez FE poza KSeF, nowe rozp. KPiR Dz.U.2025.1299 od 1.01.2026, dane nabywcy JDG. Źródła: lexlege art. 97/100, ksef.podatki.gov.pl, dziennikustaw.gov.pl/DU/2025/1299, prawo.pl, 360ksiegowosc, podatekvat.pl, GOFIN art. 86.

## Źródła
- art. 97: https://lexlege.pl/ustawa-o-podatku-od-towarow-i-uslug/art-97/
- art. 100: https://lexlege.pl/ustawa-o-podatku-od-towarow-i-uslug/art-100/
- KSeF terminy: https://ksef.podatki.gov.pl/informacje-ogolne-ksef-20/podstawy-prawne-oraz-kluczowe-terminy/
- Zagraniczni a KSeF: https://eztax.pl/czy-podmioty-zagraniczne-bez-stalego-miejsca-prowadzenia-dzialalnosci-w-polsce-musza-uzywac-ksef/
- Rozp. KPiR 2025: https://www.dziennikustaw.gov.pl/DU/2025/1299 ; https://www.prawo.pl/podatki/pkpir-2026-co-sie-zmienia-w-rozporzadzeniu,536250.html
- Import usług a VAT-UE: http://www.podatekvat.pl/artykul,17,19758,czy-import-uslug-wykazuje-sie-w-informacji-podsumowujacej.html
- Odliczenie VAT import usług: https://rozliczeniapodatkowe.pl/artykul,1808,21603,odliczanie-vat-przy-wnt-i-imporcie-uslug.html
- Dane nabywcy/koszt: https://pro.rp.pl/podatki/art38646771-da-sie-rozliczyc-fakture-bez-pelnej-nazwy-przedsiebiorstwa
