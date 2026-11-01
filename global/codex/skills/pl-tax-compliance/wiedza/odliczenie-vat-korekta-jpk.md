# Odliczenie VAT z wadliwej faktury + korekta JPK_V7M + powiadomienie o niezgodnościach

ostatnia_weryfikacja: 2026-07-14
podstawy_prawne: art. 86 ust. 1, ust. 2 pkt 1, ust. 19a; art. 88 ust. 3a pkt 1 lit. a, pkt 2, pkt 4 lit. a, ust. 4; art. 96, art. 108 ust. 1-2, art. 109 ust. 3, 3f, 3g, 3h, art. 29a ust. 13-14 ustawy o VAT (Dz.U.2025.775 t.j.); rozp. MF ws. JPK_VAT z deklaracją (JPK_V7M(3) od 1.02.2026); art. 81 Ordynacji podatkowej; art. 56a Ordynacji (obniżone odsetki); art. 16 i 16a KKS; art. 22 ust. 1, art. 23 ust. 1 pkt 43 lit. a ustawy o PIT; TSUE C-454/98 (Schmeink & Cofreth), C-442/22 (P sp. z o.o.)
pewnosc: wysoka (podstawy prawne odliczenia, korekty JPK, terminy); do_potwierdzenia: dokładny stan wyniku deklaracji (nadwyżka vs dopłata) i łańcuch kolejnych okresów — zależny od danych podatnika

## Reguła bazowa — prawo do odliczenia
- Odliczyć można wyłącznie PODATEK NALICZONY, tj. kwotę podatku wynikającą z faktury (art. 86 ust. 1, ust. 2 pkt 1), w zakresie, w jakim nabycie służy czynnościom opodatkowanym.
- Faktura ZW (od sprzedawcy zwolnionego) NIE zawiera VAT → nie ma podatku naliczonego → NIE MA CZEGO ODLICZAĆ. Kwota „VAT" doliczona samodzielnie przez system nabywcy jest fikcyjna (nie wynika z faktury, nie została zapłacona sprzedawcy).

## Faktury bez prawa do odliczenia (art. 88 ust. 3a)
- pkt 1 lit. a — faktury wystawione przez **podmiot nieistniejący**. UWAGA: „niezarejestrowany do VAT" ≠ „nieistniejący". Podmiot z NIP prowadzący realną działalność ISTNIEJE. Zmiana przepisu po wyroku TSUE C-438/09 (22.12.2010) — sam brak rejestracji sprzedawcy nie przesądza o odmowie odliczenia. Dlatego pkt 1 lit. a to zwykle BŁĘDNA podstawa dla samego faktu niezarejestrowania kontrahenta.
- pkt 2 — transakcja **zwolniona od podatku** albo niepodlegająca opodatkowaniu → brak prawa do odliczenia. To WŁAŚCIWA podstawa, gdy sprzedawca jest „zw" i wystawił fakturę zw.
- pkt 4 lit. a — faktury stwierdzające czynności niedokonane (oraz pkt 7 — kwoty niezgodne z rzeczywistością): dotyczy fikcyjnie doliczonego VAT.
- ust. 4 — obniżenia nie stosuje się do NABYWCÓW niezarejestrowanych jako VAT czynni (dotyczy strony kupującego; nie ma zastosowania, gdy nabywca jest czynny).

## Sprzedawca zwolniony, który wykazał VAT — art. 108 ust. 1 (VAT „z faktury")
- Art. 108 ust. 1: „W przypadku gdy osoba prawna, jednostka organizacyjna niemająca osobowości prawnej lub osoba fizyczna wystawi fakturę, w której wykaże kwotę podatku, jest obowiązana do jego zapłaty." Obowiązek powstaje **z mocy prawa** z chwilą **wprowadzenia faktury do obrotu** (doręczenia nabywcy / nadania nr KSeF) — niezależnie od tego, że sprzedawca jest podatnikiem zwolnionym, a czynność jest zwolniona/niepodlegająca. Podmiot zwolniony, który wpisał 23% VAT, ma zapłacić ten VAT do US „poza deklaracją" (VAT-owy dług z art. 108, nie „podatek należny" z działalności).
- Art. 108 ust. 2: stosuje się odpowiednio, gdy podatnik wykaże kwotę podatku **wyższą** od należnej.
- **Faktura korygująca do „zw" ZDEJMUJE obowiązek z art. 108** — ale nie bezwarunkowo. Warunek: **wyeliminowanie w porę ryzyka uszczuplenia wpływów budżetu** (zasada neutralności VAT; TSUE C-454/98 Schmeink & Cofreth, C-442/22 P sp. z o.o.; utrwalona linia NSA). Ryzyko jest wyeliminowane, gdy nabywca **nie odliczył** VAT z wadliwej faktury albo **skorygował/oddał** odliczenie. Wtedy sprzedawca skutecznie koryguje 23%→zw i uwalnia się od zapłaty art. 108 (lub odzyskuje wpłacony podatek).
- Sekwencyjnie: skuteczność korekty sprzedawcy jest **sprzężona** z cofnięciem odliczenia przez nabywcę — to odwrócenie odliczenia usuwa ryzyko budżetowe i „odblokowuje" zdjęcie art. 108. Dopóki nabywca trzyma odliczenie, ryzyko trwa.

## Timing korekty u NABYWCY — okres pierwotny (art. 88) vs okres otrzymania korekty (art. 86 ust. 19a)
Dwie ROZŁĄCZNE ścieżki — decyduje, czy nabywca w ogóle MIAŁ prawo do odliczenia:
- **Brak prawa do odliczenia OD POCZĄTKU** (transakcja **zwolniona**, sprzedawca „zw" wykazał VAT → art. 88 ust. 3a pkt 2): podatku naliczonego nigdy nie było (art. 86 ust. 2 pkt 1 w zw. z art. 88). Odliczenie było **nienależne ab initio** = BŁĄD, nie „korekta in-minus". Nabywca koryguje **WSTECZ, okres pierwotny** (okres odliczenia) na podstawie **art. 81 Ordynacji** — usuwa wiersz z ewidencji zakupów i zmniejsza P_42/P_43 tamtego okresu. Korekta jest **NIEZALEŻNA od faktury korygującej sprzedawcy** (nie trzeba na nią czekać; powiadomienie/wezwanie KAS dotyczące danego okresu potwierdza obowiązek korekty tego właśnie okresu). **Art. 86 ust. 19a NIE ma zastosowania** — nie ma ważnego podatku naliczonego, który miałby być „zmniejszony" w okresie otrzymania korekty.
- **Prawo do odliczenia BYŁO, a korekta je zmniejsza** (np. **zawyżona stawka 23% zamiast 8% na czynności OPODATKOWANEJ**, rabat, zwrot — art. 29a ust. 13-14): nabywca mógł odliczyć kwotę z faktury; zmniejszenia dokonuje **na bieżąco, w okresie OTRZYMANIA faktury korygującej** — art. 86 ust. 19a (od 1.02.2026: „nabywca jest obowiązany do zmniejszenia kwoty podatku naliczonego w rozliczeniu za okres, w którym otrzymał fakturę korygującą"; dla faktur ustrukturyzowanych data otrzymania = data nadania nr KSeF). Tu NIE ma cofania wstecz.
- **Pułapka rozróżnienia**: „zawyżona stawka na czynności opodatkowanej" ≠ „czynność zwolniona z bezpodstawnym VAT". Pierwsze → art. 86 ust. 19a (bieżąco). Drugie (nasz typowy case: sprzedawca zwolniony) → art. 88 ust. 3a pkt 2 + art. 81 OP (wstecz, okres pierwotny). Nie mieszać.
- **Anty-podwojenie**: gdy nabywca już cofnął odliczenie w okresie pierwotnym, a POTEM wpłynie faktura korygująca sprzedawcy (KSeF auto-import), **NIE stosować ponownie art. 86 ust. 19a** — efekt VAT korekty u nabywcy = **0** (nie ma już czego zmniejszać). KOR zaksięgować jako fakturę zw (bez VAT, poza ewidencją zakupów JPK), tylko dokumentacyjnie; oznaczyć „skorygowano w okresie pierwotnym".

## Ewidencja zakupów w JPK_V7 (część ewidencyjna)
- Ewidencja zakupu obejmuje WYŁĄCZNIE nabycia, z których podatnik korzysta z prawa do odliczenia podatku naliczonego (art. 109 ust. 3; rozp. MF JPK_VAT).
- Faktura ZW / zakup bez VAT do odliczenia NIE jest wykazywana w ewidencji zakupów JPK_V7. Ujmuje się ją tylko dla celów PIT (KPiR), nie w JPK.
- Nabycia pozostałe z prawem do odliczenia: K_42/P_42 (netto) + K_43/P_43 (VAT). Środek trwały: K_40/P_40 + K_41/P_41.

## Powiadomienie o niezgodnościach (miękkie) vs formalne wezwanie
- E-mail/SMS z `powiadomienia.jpk@mf.gov.pl` = MIĘKKIE powiadomienie analityczne (analiza JPK). Termin „7 dni" jest INSTRUKCYJNY, NIE ustawowy. Sam komunikat nie rodzi automatycznie sankcji.
- Formalne WEZWANIE naczelnika US z art. 109 ust. 3f: podatnik ma **14 dni** (art. 109 ust. 3g) na korektę ewidencji ALBO złożenie wyjaśnień. Brak reakcji / spóźnienie / brak wykazania braku błędów → kara **500 zł za każdy błąd** (art. 109 ust. 3h; „może nałożyć" — uznaniowo). Kara 3h nie dotyczy JDG, która za ten sam czyn odpowiada z KKS.
- Ignorowanie miękkiego powiadomienia → eskalacja do wezwania 3f / czynności sprawdzających / kontroli. Reagować w wyznaczonym terminie (korekta albo wyjaśnienie).

## Korekta JPK_V7M
- Podstawa: art. 81 Ordynacji podatkowej (korekta deklaracji) — koryguje się CAŁY JPK_V7M (część ewidencyjna + deklaracyjna) danego okresu, z celem złożenia = korekta.
- Ewidencyjna: USUNĄĆ błędny wiersz zakupu (nie było prawa do odliczenia, faktura zw nie powinna tam być).
- Deklaracyjna: zmniejszyć P_42 (netto nabyć pozostałych) i P_43 (VAT naliczony) o błędne kwoty; przeliczyć P_48 (razem naliczony do odliczenia) i wynik (P_51 do zapłaty / P_53 nadwyżka).
- Skutek: mniej VAT naliczonego → wzrost kwoty do zapłaty LUB spadek nadwyżki/zwrotu o kwotę błędnego VAT. Jeśli nadwyżka była przenoszona na kolejne okresy — może być konieczna korekta łańcucha następnych JPK.

## Odsetki i KKS
- Jeżeli korekta zwiększa zaległość: odsetki za zwłokę od terminu płatności okresu (VAT — 25. dzień miesiąca po okresie).
- Obniżone odsetki **50%** stawki (art. 56a Ordynacji), gdy łącznie: korekta złożona nie później niż 6 miesięcy od upływu terminu deklaracji ORAZ zapłata zaległości w 7 dni od złożenia korekty. Nie przysługują, gdy korekta po zawiadomieniu o kontroli / w wyniku czynności sprawdzających.
- Samodzielna korekta przed formalną kontrolą wyłącza odsetki podwyższone 150% (art. 56b Ordynacji).
- KKS: skuteczna korekta + zapłata całości uszczuplenia „niezwłocznie lub w terminie organu" → NIEPODLEGANIE KARZE (art. 16a KKS). Alternatywnie/uzupełniająco czynny żal (art. 16 KKS). Nie działa, jeśli wcześniej wszczęto postępowanie / ujawniono czyn.

## KPiR / koszt (PIT)
- Koszt netto usługi (przy zw brutto=netto) pozostaje KUP, jeśli wydatek realny, związany z działalnością, właściwie udokumentowany (art. 22 ust. 1 PIT). Status VAT sprzedawcy (zw/niezarejestrowany) NIE wpływa na potrącalność kosztu w PIT.
- Faktura zw brutto = netto → cały wydatek (np. 5000 zł) jest kosztem; nie ma odrębnego VAT.
- Nieodliczalny VAT (gdy realnie był na fakturze, lecz bez prawa do odliczenia) może być kosztem — art. 23 ust. 1 pkt 43 lit. a PIT. NIE dotyczy sytuacji, gdy VAT w ogóle nie istniał (faktura zw) — nie dokłada się fikcyjnej kwoty do kosztu.

## Pułapki
- Utożsamianie „niezarejestrowany do VAT" z „podmiot nieistniejący" (art. 88 ust. 3a pkt 1 lit. a) — błędna kwalifikacja; przy sprzedawcy zw właściwa podstawa to pkt 2 (transakcja zwolniona).
- Doliczanie samodzielnie VAT do faktury zw i wpisywanie do ewidencji zakupów (fikcyjny podatek naliczony).
- Traktowanie 7-dniowego terminu z miękkiego powiadomienia jak ustawowego (mylenie z 14 dniami art. 109 ust. 3g po formalnym wezwaniu).
- Pominięcie odsetek / niezapłacenie zaległości w 7 dni → utrata obniżonych odsetek 50%.
- Korekta tylko części deklaracyjnej bez ewidencyjnej (albo odwrotnie) — JPK_V7M koryguje się jako całość.
- Pominięcie korekty łańcucha kolejnych okresów, gdy nadwyżka z błędnego okresu była przenoszona.
- Zastosowanie art. 86 ust. 19a (korekta „na bieżąco" w okresie otrzymania KOR) do sytuacji braku prawa do odliczenia od początku (transakcja zwolniona) — błąd; tam korekta jest WSTECZ (okres pierwotny, art. 81 OP), niezależna od KOR sprzedawcy.
- Podwojenie korekty u nabywcy: cofnięcie odliczenia w okresie pierwotnym, a potem PONOWNE zmniejszenie po wpłynięciu KOR sprzedawcy przez KSeF (efekt VAT KOR = 0).
- Uznanie, że podatnik zwolniony wykazujący VAT „nic nie płaci" — art. 108 ust. 1 rodzi obowiązek zapłaty tego VAT do US z mocy prawa; zdejmuje go dopiero skuteczna korekta do zw + brak ryzyka budżetowego (nabywca nie odliczył / cofnął odliczenie).

## Changelog
- 2026-07-14 (uzup.): DODANO (case — dane prywatne zredagowane w edycji publicznej: sprzedawca zwolniony wykazał 23% VAT przez KSeF, nabywca odliczył): (a) art. 108 ust. 1-2 — sprzedawca (też zwolniony), który wykaże VAT na fakturze, ma obowiązek zapłaty z mocy prawa od chwili wprowadzenia faktury do obrotu; faktura korygująca do zw zdejmuje ten obowiązek pod warunkiem wyeliminowania w porę ryzyka uszczuplenia budżetu (nabywca nie odliczył/cofnął) — TSUE C-454/98, C-442/22, linia NSA; (b) art. 86 ust. 19a — od 1.02.2026 nabywca zmniejsza podatek naliczony w okresie OTRZYMANIA faktury korygującej (dla ustrukturyzowanej = data nr KSeF) TYLKO gdy odliczenie było należne (zawyżona stawka na czynności opodatkowanej, rabat, zwrot — art. 29a ust. 13-14); przy braku prawa do odliczenia od początku (transakcja zwolniona, art. 88 ust. 3a pkt 2) korekta jest WSTECZ w okresie pierwotnym (art. 81 OP), NIEZALEŻNA od KOR sprzedawcy; (c) anty-podwojenie: po cofnięciu odliczenia w okresie pierwotnym efekt VAT KOR sprzedawcy u nabywcy = 0. Zweryfikowano dosłowne brzmienie art. 108 ust. 1-2 i art. 86 ust. 19a (lexlege). Źródła: lexlege art. 108, art. 86; poltax.pl (art. 108 obowiązek zapłaty); pit.pl/gofin (zawyżona stawka → art. 86 ust. 19a bieżąco); infor.pl (puste faktury można korygować, gdy odbiorca nie odliczył); studio.pwc.pl (TSUE dobra wiara / brak obowiązku zapłaty nienależnego VAT).
- 2026-07-14: utworzenie tematu. Zweryfikowano brzmienie art. 88 ust. 3a pkt 1 lit. a / pkt 2 / pkt 4 lit. a / ust. 4 oraz art. 109 ust. 3f-3h (lexlege, Dz.U.2025.775 t.j.), zasadę miękkiego powiadomienia z powiadomienia.jpk@mf.gov.pl (7 dni instrukcyjny) vs formalnego wezwania (14 dni + kara 500 zł/błąd), art. 56a Ordynacji (obniżone odsetki 50%), art. 16a KKS (niepodleganie karze przy korekcie + zapłacie), rozróżnienie niezarejestrowany vs nieistniejący (TSUE C-438/09). Źródła: lexlege art.88/art.109, art.56a Ordynacji, art.16a KKS (gofin), podatki.gov.pl (powiadomienia JPK), poradnikprzedsiebiorcy.

## Źródła
- lexlege.pl — art. 88, art. 109 ustawy o VAT (brzmienie ust. 3a, ust. 3f-3h)
- lexlege.pl — art. 108 ust. 1-2 (obowiązek zapłaty VAT wykazanego na fakturze) oraz art. 86 ust. 19a (korekta in-minus u nabywcy w okresie otrzymania KOR, brzmienie od 1.02.2026)
- poltax.pl — obowiązek zapłaty VAT z tytułu wystawienia faktury z wykazaną kwotą podatku (art. 108)
- studio.pwc.pl / akademialtca.pl — orzecznictwo TSUE do art. 108 (C-454/98 Schmeink & Cofreth, C-442/22 P sp. z o.o.; dobra wiara, brak ryzyka uszczuplenia)
- infor.pl — puste faktury można korygować, gdy odbiorca nie odliczył VAT; zawyżona stawka a odliczenie
- pit.pl / gofin.pl — zawyżona stawka VAT: korekta u nabywcy na bieżąco (art. 86 ust. 19a)
- lexlege.pl / arslege.pl — art. 56a Ordynacji podatkowej (obniżone odsetki 50%)
- przepisy.gofin.pl — art. 16a KKS (korekta deklaracji, niepodleganie karze)
- podatki.gov.pl — konsekwencje otrzymania powiadomienia o niezgodnościach w JPK_VAT
- poradnikprzedsiebiorcy.pl — powiadomienia o niezgodnościach JPK_VAT; faktura od podmiotu niezarejestrowanego a JPK/odliczenie
- TSUE C-438/09 (22.12.2010) — odliczenie z faktury podmiotu niezarejestrowanego
</content>
</invoke>
