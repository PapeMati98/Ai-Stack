# Retencja dokumentów kontrahentów B2B w CRM (skany: faktury cudze, umowy, pełnomocnictwa, rejestrowe)

ostatnia_weryfikacja: 2026-07-06
pewnosc: wysoka (okresy podatkowe/cywilne pewne; górne granice RODO = rekomendacja praktyczna, nie sztywny przepis)
podstawy_prawne: art. 70 §1 i art. 86 §1 Ordynacji podatkowej (5 lat od końca roku podatkowego); art. 112 ustawy o VAT; art. 74 ustawy o rachunkowości (5 lat); art. 118 i 125 KC (B2B 3 lata z końcem roku kalendarzowego; stwierdzone prawomocnie 6 lat); RODO art. 5 ust. 1 lit. e (ograniczenie przechowywania), art. 6 ust. 1 lit. b/c/f

## Zasady bazowe
- **Podatkowo (nasze dokumenty):** 5 lat od końca roku kalendarzowego, w którym upłynął termin płatności podatku (Ordynacja 70/86, VAT 112). KSeF przechowuje faktury ustrukturyzowane 10 lat — ale to system MF, nie zwalnia z własnej ewidencji.
- **Cywilnie (roszczenia B2B):** 3 lata, koniec terminu = ostatni dzień roku kalendarzowego (art. 118 KC); roszczenia stwierdzone prawomocnie — 6 lat (art. 125 KC).
- **RODO:** dane reprezentantów/pełnomocników w dokumentach FIRMOWYCH to nadal dane osobowe; podstawa art. 6(1)(b)/(f); retencja nie dłużej niż cel (art. 5(1)(e)). Dokumenty CUDZE (np. faktura klienta od innego sprzedawcy) NIE są naszym dowodem księgowym → brak obowiązku podatkowego przechowywania → decyduje wyłącznie cel + minimalizacja.

## Polityka retencji per kategoria (CRM sprzedażowy B2B)
| Kategoria | Min (przepis) | Rekomendowana retencja (= max wg minimalizacji) | Zdarzenie startowe |
|---|---|---|---|
| (a) dokumenty ofertowe pozyskane od kontrahenta (materiał ofertowy) | brak obowiązku | **bez umowy: 12 mies. od pozyskania; z umową: koniec umowy + 3 lata (do 31.12)** — dokumentują podstawę kalkulacji ceny | pozyskanie / koniec umowy |
| (b) podpisane umowy sprzedaży z nami | 5 lat podatkowo od ostatniego rozliczenia | **koniec obowiązywania umowy + 6 lat** (pokrywa 5 lat podatkowych od ostatniej faktury i spory cywilne) | koniec obowiązywania |
| (c) pełnomocnictwa PZS | akcesoryjne do umowy | **jak umowa: koniec umowy + 6 lat** (dowód umocowania przy sporze o zmianę sprzedawcy) | koniec umowy |
| (d) dokumenty rejestrowe (KRS/CEIDG/NIP) | brak obowiązku (dane jawne z rejestrów) | **czas relacji handlowej; kasacja 2 lata po ostatniej aktywności klienta** (zawsze odtwarzalne z rejestru) | ostatnia aktywność |
| (e) robocze do ofertowania | brak obowiązku | **12 mies. od pozyskania bez umowy; przy umowie przeklasyfikować do (a)/(b)** | pozyskanie |

## Kto kasuje / jak wdrożyć
- Automat (cron) wg kategorii + daty zdarzenia startowego — RODO wymaga REALIZACJI retencji, nie ręcznej decyzji; + ręczna kasacja ADMIN na żądanie.
- Warunek: dokument w CRM musi mieć KATEGORIĘ i datę zdarzenia startowego (koniec umowy → dziedziczy z Agreement.validTo).
- Obowiązek informacyjny wobec reprezentantów/pełnomocników — klauzula w umowie/pełnomocnictwie (standard).

## Pułapki
- Faktura CUDZA ≠ nasz dowód księgowy — nie stosować do niej 5 lat podatkowych „na wszelki wypadek" (to łamie minimalizację RODO).
- Koniec 3-letniego przedawnienia B2B przypada na 31.12 (nie dzień-w-dzień).
- Umowa wieloletnia: 5 lat podatkowych liczy się od ostatniej faktury z umowy, nie od podpisania — stąd bezpieczne „koniec umowy + 6 lat".

## Changelog
- 2026-07-06: utworzenie tematu (case — dane prywatne zredagowane w edycji publicznej: retencja skanów w CRM, aparat mobilny). Weryfikacja web: art. 86 Ordynacji 5 lat + KSeF 10 lat (subiekt123.pl, infakt.pl, gofin.pl); art. 118 KC 3 lata/koniec roku (lex.pl, gofin.pl, prawo.pl).

## Źródła
- https://isap.sejm.gov.pl (Ordynacja podatkowa, KC, ustawa o VAT, ustawa o rachunkowości)
- https://pomoc.infakt.pl/hc/pl/articles/115000636770 ; https://www.gofin.pl/rachunkowosc/17,1,85,213977 ; https://subiekt123.pl/porady/jak-dlugo-trzeba-przechowywac-faktury-2026/
- https://sip.lex.pl/akty-prawne/dzu-dziennik-ustaw/kodeks-cywilny-16785996/art-118 ; https://www.prawo.pl/biznes/termin-przedawnienia-roszczen-jak-obliczyc,525797.html
