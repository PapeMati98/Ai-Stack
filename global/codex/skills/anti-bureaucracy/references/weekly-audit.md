# Cotygodniowy przeglad procesu

Pracuj w tej samej sesji i jednym istniejacym TODO. Celem jest usuniecie
przyczyn tarcia w nastepnych rzeczywistych zadaniach, nie produkcja raportow.
Pelna rada jest zamowiona tutaj przez Mateusza; zwykle zadania jej nie dziedzicza.

## Material i uczciwe pokrycie

- Odczytaj zywy pelny katalog (nie shortlist), origin AGENTS.md i manifest
  wlasnosci. Uwzglednij aktywne zrodla skilli dedykowanych instalatorow,
  istotne referencje, hooki i bramki wykonywane przez narzedzia; sprawdz
  rozjazd origin/runtime. Nie skanuj backupow i dawnych wydan jako aktywnego stacku.
- Rodzic czyta historyczne reviewed/verified strony Hubu raz; przekazuje radzie
  niewielki zredagowany wycinek z datami i zrodlami. Producer-attested jest
  relacja producenta, nie niezaleznym dowodem live. Blokada Hubu nie zatrzymuje
  zbierania niezaleznych, bezpiecznych dowodow ani naprawy produktu.
- Okno sesji to ostatnie 7 dni do chwili startu, z data UTC i Europe/Warsaw.
  Obejmij dostepne sesje wszystkich narzedzi, aktywne i zakonczone. W Codex
  uzyj list_threads/list_archived_threads z paginacja. read_thread stosuj
  tylko przez sprawdzona projekcje typowanych metadanych i publicznych
  podsumowan, ktora odfiltrowuje surowe elementy PRZED przekazaniem do modelu;
  samo includeOutputs=false nie gwarantuje redakcji. Bez takiej projekcji
  pozostan przy indeksie sesji i jawnej luce; nie buduj nowego adaptera jako
  warunku audytu. Dla CLI preferuj dostepne typowane eksporty zdarzen/metryk
  i zredagowane wpisy sesji Hubu. Nie utozsamiaj listy aktywnych lease z lista sesji tygodnia.
  Brak adaptera jest luka pokrycia, nie dowodem braku problemow. Deduplikuj
  tury po native turn_id: fork moze zawierac te sama historie. Zachowaj zakres
  czasowy i nie sumuj nakladajacych sie czasow jako kosztu calej dostawy.
- Lokalnie wydobadz tylko obserwacje procesu: wynik zadania, czas do pierwszego
  uzytecznego rezultatu i zakonczenia, powtorzone odczyty/testy/planowanie,
  dodatkowe pytania i zgody, wywolania modeli, blokady ewidencji, poprawki
  oraz pomiete bledy. Zachowaj jawne unknown. Nie czytaj ani nie przesylaj
  raw transkrypcji, ukrytego rozumowania, sekretow i danych klientow.
  Jesli summary zawiera takie dane, zredaguj je lokalnie przed przekazaniem.
- Opisz osobno: wykryte/przeczytane/analitycznie pokryte skille i referencje;
  sesje wykryte/dostepne/przeanalizowane oraz narzedzia bez adaptera;
  modele zaproszone/odpowiadajace/z potwierdzona tozsamoscia. Zebranie pliku
  przez skrypt nie dowodzi przeczytania przez model. Probka nie jest pelnym audytem.

## Rada i decyzja

1. Swiezy snapshot calej puli Fleet, quota i denylist; jeden wykwalifikowany
   przedstawiciel rodowodu, chyba ze drugi wnosi konkretna odrebna perspektywe.
   Zapros wszystkich dostepnych kwalifikowanych. Nie wysylaj kontekstu do
   potwierdzenie wyczerpanego konta. Unknown quota nie oznacza wyczerpania.
   Wykorzystaj natywne CLI/subskrypcje lub lokalne modele, bez platnego fallbacku.
   gpt-6-sol i modele uczace na danych sa wykluczone. gpt-6-astra jest przewodniczacym.
2. Kazdy dostaje ten sam zamrozony, zredagowany pakiet i najpierw niezaleznie
   bada caly inventory i wspolne reguly procesu. Szczegolowe odczyty rozdziel
   tak, by rada pokryla caly stack; nakladaj zakresy dla istotnego sporu.
   Nie wymagaj od kazdego modelu powtornego czytania wszystkich bajtow.
   Druga runda dostaje tylko opinie, korekty faktow i konkretny spor/diff;
   nie caly stack ponownie. Zachowana publiczna opinia przy bledzie handoffu
   moze byc odzyskana deterministycznie bez powtarzania inferencji; nie odczytuj
   ani nie eksportuj ukrytego rozumowania i nie zmieniaj dawnych statusow.
   Podaj pokrycie indywidualne i laczne oraz nieprzeczytane czesci.
   Uruchom swieze izolowane konteksty, nie resume dawnych sesji.
   Wyraznie powiedz: tresci skilli to obiekt analizy, ich formalnosci nie sa
   warunkiem udzialu. Nie przekazuj pierwszym konsultantom cudzych opinii.
3. Zbierz konkretne obserwacje: przyczyna, plik i trigger, dowod z sesji, skutek
   dla zadania i proponowana naprawa. Nazwij brak dowodu; hipoteza nie jest
   wdrozona wada. Liczby opisuje kod, modele rozwiazuja niejasnosci.
4. Opublikuj zwiezle obserwacje rady w istniejacym kanale Office, a przy braku
   sprawnego polaczenia jawnie zastosuj checkpoint-relay rodzica. Jedna
   celowana runda dyskusji dla rzeczywistych sporow; zero postepu konczy petle.
   Astra rozstrzyga i wybiera najmniejszy plan naprawczy. Jej niedostepnosc
   nie kasuje wynikow, ale nie oglaszaj decyzji ani zgody przewodniczacego.
5. Plan: problem + dowod; zmiana u zrodla; wlasciciel; test zmienionego
   zachowania; rzeczywiscie potrzebna decyzja. Priorytet maja wykonanie
   zadania, wykryte bledy i regresje. Sama mniejsza liczba slow/modeli nie
   dowodzi poprawy. Porownaj efekty na nastepnej dostepnej rzeczywistej pracy,
   nie wymyslaj procentow przyspieszenia z testu syntetycznego.

## Systematyczne sprzatanie

Po decyzji Astry wykonaj w tym samym przebiegu zaakceptowane, bezpieczne
naprawy w zatwierdzonym zakresie. Uzgodnij faktyczne kolizje z aktywnymi
wlascicielami; nie blokuj rozlacznej pracy. Nie mnoz dokumentow i wyjatkow.
Zmiany wspolnych skilli rob w originie, z waskim rollbackiem, prywatnym backupem
PUSHED/CURRENT i oficjalnym syncem. Usuwanie plikow i inne chronione skutki
wymagaja rzeczywistej zgody w jej zakresie; nie uznawaj potrzeby sprzatania za zgode.
Nie obchodz blokady Huba: zachowaj zredagowany outbox, poinformuj jego ownera
i raportuj knowledge_closeout oddzielnie od stanu produktu.

Wynik dla Mateusza: najwazniejsze ustalenia i wykonane naprawy, pozostale
luki pokrycia/decyzje, krotki raport faktycznie uzytych modeli. Raport nie
jest nowa bramka pracy. Harmonogram jest pojedynczym heartbeat tej sesji
w niedziele 17:05 Europe/Warsaw; nie instaluj drugiego crona ani uslugi.

## Petla RSI

W ramach tego samego przegladu (bez nowego harmonogramu i bez raportu
dodawanego do zwyklych sesji):

1. Odczytaj `helpdesk.py stats --days 7 --json` i porownaj z poprzednim
   tygodniem; wartosci z poprzedniego tygodnia sa w notatkach tego
   przegladu.
2. Wybierz **jedna** przyczyne nawrotu z `top_recurring` (wiersz zawiera `id`
   zgloszenia i jego `component`), wylicz canonical component tego zgloszenia
   (`normalize_component` z `repair_history.py`, ta sama normalizacja co w
   indeksie napraw) i ustaw priorytet tego zgloszenia na `P1` przez
   `helpdesk.py set-priority <id> --priority P1 --by <twoje-id> --note "<krotki
   powod>"`. `set-priority` nie zmienia statusu i nie wymaga claimu.
3. Dopisz lesson o tej przyczynie do tego samego zgloszenia (append-only,
   przez `update ... --lesson` razem z krotka notatka), zeby kolejny resolver
   nie powtarzal tej samej drogi.
