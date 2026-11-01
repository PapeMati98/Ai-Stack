# Ponowne użycie kontekstu

Pełny inventory i semantyczny dobór wykonaj przy nowym celu albo zmianie zakresu, domeny, ryzyka, QA lub inventory. Niezmieniona kontynuacja/status używa już odczytanego katalogu; nie wymaga nowej komendy ani raportu. Znany drift albo utrata kontekstu wymaga odpowiedniego odświeżenia.

# Kontrakt selekcji

## Wejście i granice

`skill_catalog.py` odkrywa pliki `SKILL.md` z drzewa aktywnego runtime'u, wyznaczonego ze ścieżki własnego skryptu albo przez `SKILL_ACTIVE_ROOT`, oraz z jawnych rootów przekazanych przez `--root`. Root może być katalogiem zawierającym `skills/` albo bezpośrednim katalogiem skilla. `--all-runtimes` służy wyłącznie do diagnostyki rozjazdów; nie wolno używać jego połączonego inventory do doboru skilli bieżącego agenta.

Skille dostarczone natywnie przez plugin runtime'u, ale niewidoczne w aktywnym drzewie plików, dobieraj z natywnej listy nazw i opisów udostępnionej agentowi. Nie skanuj historycznych katalogów cache pluginów.

Parser czyta wyłącznie frontmatter od pierwszego wiersza `---` do następnego wiersza `---`. Nie czyta body, nie importuje znalezionych modułów, nie wykonuje skryptów i nie instaluje pakietów.

## Inventory

Inventory JSON zawiera wersję schematu, rozstrzygnięte rooty, rekordy skilli, konflikty i błędy. Rekord opiera się na nazwie, opisie, metadanych frontmatter, ścieżce i hashu frontmatter. Identyczne `(name, frontmatter_hash)` są deduplikowane. Ta sama nazwa z innym hashem tworzy konflikt i nie jest bezpiecznym kandydatem do routingu.

## Routing

Pula kandydatów jest leksykalna i deterministyczna: znormalizowane tokeny oraz ostrożne dopasowanie wspólnego prefiksu są porównywane z nazwą, opisem i stabilnymi polami frontmatter (`aliases`, `triggers`, `domains`, `role`). Normalizacja składa znaki diakrytyczne (w tym jawne `ł -> l`, bo NFKD go nie rozkłada) i odrzuca tokeny jednoznakowe. Nazwy z myślnikiem są indeksowane także jako części, więc słowo "debugging" dociera do skilla `systematic-debugging`. Przed scoringiem obie strony są oczyszczane z dwujęzycznych wyrazów funkcyjnych i rodzajników zadania bez sygnału domenowego (`STOPWORDS`), a zapytanie jest rozszerzane o zwarte, dwujęzyczne grupy synonimów pojęć domenowych (`SYNONYM_GROUPS`: UI, incydent, wdrożenie, wpływ zmiany, testy, schemat danych). Forma fleksyjna spoza indeksu (np. "wydaniu", "zmienie") trafia do grupy klucza o najdłuższym wspólnym prefiksie (minimum 5 znaków). Jedno słowo zadania daje co najwyżej jedno trafienie synonimiczne na skill (wygrywa najrzadszy token, przy remisie token nazwy), wiec opis powtarzający jedno pojęcie w wielu formach nie przykrywa trafniejszego skilla. Trafienie synonimu waży mniej niż trafienie dokładne, a więcej niż dopasowanie prefiksu; trafienie synonimu w nazwę dostaje dodatkową, mniejszą wagę niż trafienie dokładne w nazwę. To leksykon pojęć ogólnych, nie tabela skilli. Rzadkie terminy są ważone wyżej niż pojęcia ogólne, a trafienie w nazwę dostaje dodatkową wagę. Wynik sortuje się po wyniku malejąco, nazwie i ścieżce i zawiera najwyżej 8 kandydatów. Brak trafień daje pustą listę. Pula tylko ogranicza kandydatów; agent wykonuje finalne dopasowanie semantyczne i wybiera najwyżej 4 skille. Regresję routingu na realnym katalogu pilnuje oryginalny korpus `references/routing-corpus.json` (przypadki pozytywne, negatywne i pusty routing dla prostego statusu), wykonywany przez `tests/test_skill_catalog.py`. Skille z zarządzanych wydań, nieobecne w skanowanym originie (np. `design-ui`, `design-loop`), są w teście korpusu rozwiązywane do rzeczywistego frontmatter wydania wskazanego przez `skills-ownership.json` (`managed_release_skills` w korpusie), więc ich oczekiwania są sprawdzane na aktualnym wydaniu zamiast pomijane albo kopiowane do recznego fixture'u.

Polityka może zawierać wyłącznie stabilne bramki i aliasy, na przykład `security -> governance` oraz `tests -> qa`. Nie zawiera ręcznego spisu wszystkich skilli. Finalny wybór ma 0-4 specjalistów i wymaga uzasadnienia roli: `governance`, `domain`, `execution` lub `qa`.

`search-skills` wolno wybrać tylko po stwierdzeniu, że lokalny inventory nie zawiera wymaganej zdolności. Brak lokalnego kandydata jest informacją, nie poleceniem instalacji.

## Niezmienniki

- permissions pozostają bez zmian;
- katalogowanie jest read-only;
- routing fail-closed dla konfliktu nazw;
- wynik dla identycznego wejścia jest identyczny;
- ścieżki niedostępne są raportowane jako błędy, bez przerywania całego inventory.
