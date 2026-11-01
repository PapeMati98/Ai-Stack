---
temat: sprawozdanie finansowe + złożenie do KRS + uchwała o dalszym istnieniu (art. 233 KSH)
ostatnia_weryfikacja: 2026-06-29
podstawy_prawne: KSH art. 231, 233, 244; uor art. 5 ust. 2, 45 ust. 1g, 49 ust. 5, 52, 53, 69
pewnosc: wysoka (art. 233 KSH i art. 52 uor zweryfikowane w źródłach 2026-06-29)
---

# Sprawozdanie finansowe sp. z o.o. (jednostka mała) → KRS + uchwała o dalszym istnieniu

## WERSJA SCHEMY (krytyczne!)
- **Za rok obrotowy rozpoczęty ≥ 2025-01-01: SFJMAZ wariant 2, wersja 1-0E** (KodSprawozdania SprFinJednostkaMalaWZlotych, WariantSprawozdania = **2**). Stary **SFJMAZ(1)/1-3 dotyczy lat ≤ 2024** i dla SF za 2025 jest NIEAKTUALNY.
- Aplikacja MF e-Sprawozdania w okresie przejściowym (do 30.06.2026) obsługuje obie struktury, ale dla SF za 2025 należy używać NOWEJ (2)/1-0E. Generatory na starej schemie = ryzyko odrzucenia.

## MUST-HAVE — komplet do KRS (RDF)
1. **Sprawozdanie finansowe w strukturze logicznej XML** (SFJMAZ(2)/1-0E dla jednostki małej) — art. 45 ust. 1g uor. Składa się z: wprowadzenie, **bilans (pełne pozycje wg Załącznika Nr 5 — NIE same sumy Aktywa/Pasywa razem)**, RZiS, informacja dodatkowa (rzeczywista, nie nota techniczna).
1a. **Uchwała organu zatwierdzającego o zastosowaniu uproszczeń dla jednostek małych** (Załącznik 5 uor) — podejmowana przez zgromadzenie wspólników (warunek stosowania uproszczeń jednostki małej, art. 3 ust. 1c–1e + Załącznik 5).
2. **Podpis SF** — art. 52 ust. 2/2b uor: kierownik jednostki (zarząd). Zarząd wieloosobowy: podpisują WSZYSCY członkowie, **albo** ≥1 członek po złożeniu przez pozostałych OŚWIADCZENIA, że SF spełnia wymagania uor (odmowa oświadczenia = równoznaczna z odmową podpisu, wymaga pisemnego uzasadnienia załączanego do SF). Podpisuje też **osoba, której powierzono prowadzenie ksiąg** (księgowa). Forma e-podpisu: kwalifikowany / podpis zaufany (ePUAP) / podpis osobisty (e-dowód).
3. **Uchwała o zatwierdzeniu SF** — art. 231 § 2 pkt 1 + art. 53 uor. Termin zatwierdzenia: 6 miesięcy od dnia bilansowego (rok kalendarzowy → do 30.06).
4. **Uchwała o pokryciu straty** (lub podziale zysku) — art. 231 § 2 pkt 2 KSH.
5. **Złożenie do KRS** — art. 69 uor: w ciągu **15 dni od zatwierdzenia** SF, przez Repozytorium Dokumentów Finansowych (RDF, ekrs.ms.gov.pl/rdf/rd) — bezpłatnie. SF trafia automatycznie do Szefa KAS/US (nie składa się odrębnie do US).

### Gdy strata przewyższa próg z art. 233 KSH — DODATKOWO:
6. **Uchwała o dalszym istnieniu spółki** — art. 233 § 1 KSH. Próg: gdy bilans wykaże **stratę przewyższającą sumę kapitałów zapasowego i rezerwowych ORAZ połowę kapitału zakładowego**. Wtedy zarząd jest obowiązany **niezwłocznie** zwołać zgromadzenie wspólników w celu powzięcia uchwały dotyczącej dalszego istnienia spółki. Treść: wspólnicy postanawiają o **dalszym istnieniu / kontynuowaniu działalności** spółki (albo o rozwiązaniu). Uchwała zwykłą większością (chyba że umowa stanowi inaczej). To uchwała ODRĘBNA od zatwierdzenia SF — można podjąć na tym samym zgromadzeniu.

## NICE-TO-HAVE / zależne
- **Sprawozdanie z działalności zarządu** — art. 49 ust. 5 uor: jednostka mała **MOŻE NIE sporządzać**, jeżeli w informacji dodatkowej ujawni dane o nabyciu udziałów własnych (gdy brak — warunek spełniony). Dla mikro/małej sp. z o.o. zwykle pomijane.
- **Uchwała o absolutorium** dla członków zarządu — art. 231 § 2 pkt 3 KSH (standard porządku obrad ZZW). Art. 244 KSH: wspólnik będący członkiem zarządu **nie głosuje** nad własnym absolutorium.

## Going concern (art. 5 ust. 2 uor) a ujemny kapitał własny
- SF sporządza się przy **założeniu kontynuacji działalności**, chyba że jest to niezasadne (zamiar/konieczność likwidacji).
- Ujemny kapitał własny / duża strata = **przesłanka do oceny**, ale NIE przesądza automatycznie o braku kontynuacji. Jeśli kontynuacja zasadna (wsparcie wspólników/pożyczki, rosnące przychody, plan) — zachowuje się założenie kontynuacji.
- W praktyce: we **wprowadzeniu do SF** wskazać, że SF sporządzono przy założeniu kontynuacji; w **informacji dodatkowej** ujawnić ujemny kapitał własny, przesłanki kontynuacji i fakt podjęcia uchwały z art. 233 KSH. Uchwała o dalszym istnieniu wspiera założenie kontynuacji.

## Pułapki (częste błędy → odrzucenie/wada)
- SF złożone jako PDF zamiast XML (SFJMAZ) — KRS RDF wymaga struktury logicznej XML; PDF nieważny.
- Brak podpisu osoby prowadzącej księgi (księgowej) — SF niekompletne.
- Przy 1 podpisie zarządu — brak oświadczeń/odmów pozostałych członków (art. 52 ust. 2b).
- Pominięcie uchwały z art. 233 KSH mimo przekroczenia progu — zaniechanie obowiązku zarządu.
- Złożenie do KRS po terminie 15 dni od zatwierdzenia (lub brak zatwierdzenia w 6 mies.).
- Głosowanie członka zarządu nad własnym absolutorium (art. 244 KSH).

## Zastosowanie do casu — dane prywatne spółki zredagowane w edycji publicznej (rok 06.01–31.12.2025)
- Strata przekraczająca próg (kapitały rezerw. + ½ kapitału zakładowego; kwoty jednostkowe zredagowane w edycji publicznej) → **uchwała z art. 233 KSH WYMAGANA**.
- Zarząd 2-osobowy (dane osobowe członków zarządu zredagowane w edycji publicznej): SF podpisują obaj, albo jeden + oświadczenie drugiego. Plus księgowa.
- Komplet do KRS: SF (XML SFJMAZ) + uchwała zatwierdzająca + uchwała o pokryciu straty + **uchwała o dalszym istnieniu** + (opcj.) absolutoria. Termin: zatwierdzenie do 30.06.2026, złożenie do KRS w 15 dni.

## Źródła
- KSH art. 233: https://lexlege.pl/ksh/art-233/ ; https://przepisy.gofin.pl (art. 233 KSH)
- uor art. 52 (podpis SF): https://lexlege.pl/ustawa-o-rachunkowosci/art-52/ ; MF: gov.pl/web/finanse (oświadczenie członka zarządu)
- uor art. 49/53/69 (sprawozdanie z działalności, zatwierdzenie, złożenie KRS): isap.sejm.gov.pl
- KRS RDF: https://ekrs.ms.gov.pl/rdf/rd/

## Changelog
- 2026-06-29: utworzenie tematu. Zweryfikowano brzmienie art. 233 § 1 KSH oraz art. 52 ust. 2/2b uor (podpis SF: cały zarząd albo ≥1 członek + oświadczenia pozostałych). Źródła: lexlege.pl, gov.pl/finanse, przepisy.gofin.pl.
- 2026-06-29: DODANO sekcję „WERSJA SCHEMY" — dla SF za rok ≥ 2025-01-01 obowiązuje SFJMAZ wariant 2 / 1-0E (stary (1)/1-3 = lata ≤2024); MF app obsługuje obie do 30.06.2026. DODANO: bilan musi mieć pełne pozycje Załącznika 5 (nie same sumy), informacja dodatkowa rzeczywista, uchwała o zastosowaniu uproszczeń jednostki małej. DODANO pułapkę KUP: niezapłacone wynagrodzenia/składki ZUS są KUP dopiero z chwilą zapłaty (art. 15 ust. 4g–4h, art. 16 ust. 1 pkt 57/57a CIT) — w księgach muszą być rozliczone zapłaty, inaczej bilans błędny i ryzyko zakwestionowania KUP. Źródło: podatki.gov.pl (aktualizacja struktur SF), PIBR.
