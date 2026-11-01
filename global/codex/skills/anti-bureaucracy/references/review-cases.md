# Przypadki odbioru

| Zmiana / sytuacja | Oczekiwane zachowanie |
|---|---|
| Poprawka jednej sciezki w skillu | Review tej poprawki; bez rady, grafu i dodatkowego receiptu. |
| Nowy wymog: przed kazda odpowiedzia osobny planner + witness + trzy pliki | Wskaz brak konkretnej korzysci i kolizje z obecnym TODO; usun/scal/zawez trigger. |
| Dwa skille wymagaja tego samego odczytu Hubu | Reuse jednego aktualnego odczytu rodzica; rozdziel tylko rzeczywiscie inne projekty lub zmieniony kontekst. |
| Legacy reference opisuje fidelity dla runu sprzed TODO, zwykla sciezka jawnie go pomija | Sam keyword nie jest regresja; sprawdz, czy trigger lub hook rzeczywiscie go przywraca. |
| Skill A obowiazkowo uruchamia B, a B obowiazkowo A | Udowodnij aktywny cykl; usun jedna przyczyne u zrodla, nie dodawaj kolejnej reguly pomijania. |
| Usuniecie kontroli sekretow daje mniej krokow | Koszt nie wystarcza do ACCEPT; zachowaj ochrone albo sprawdz rownowazny mechanizm. |
| Produkt i testy PASS, Hub chwilowo blokuje zapis | Produkt PASS; knowledge_closeout BLOCKED i outbox/owner. Bez omijania bramki lub udawania pelnego zamkniecia. |
| Rada ma 6 glosow, 3 konta wyczerpane, 2 narzedzia bez metryk sesji | Jawny czesciowy zakres i brakujace glosy; uzyteczny plan, bez slowa pelny audyt wykonany. |
| Brak Astry lub brak jej decyzji | Zachowaj opinie; nie wymyslaj syntezy/zgody Astry. |
| Poprawiono format dowodu, hash produktu i wejsc taki sam | Napraw handoff; reuse aktualnych testow. Nowy hash/warunki wymagaja celowanego retestu. |

Przy odbiorze nie zdradzaj recenzentowi oczekiwanego werdyktu przed jego
niezalezna ocena. Sprawdz rzeczywista interpretacje skilla na 2-3 kontrastowych
zadaniach, nie tylko obecnosc fraz w tekscie. Raz wykonany odbior nie tworzy
obowiazku powtarzania wszystkich przypadkow przy literowce.
