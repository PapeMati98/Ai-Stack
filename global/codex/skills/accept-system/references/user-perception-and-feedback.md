# Osiagalnosc, reakcja i jawne bledy

Sprawdz zmienione user-facing sciezki i istotne regresje w przegladarce lub
natywnej aplikacji. Nie uruchamiaj ponownego odbioru wszystkich niezmienionych
workflow ani awarii warstwy, ktorej zmiana nie dotyka. Stosowalne ponizsze
kryteria maja dowod PASS/FAIL w tym samym odbiorze; N/A uzasadnia reviewer.
Jeden niezalezny reviewer moze objac visual i user journey; drugi tylko dla
odrebnego realnego ryzyka.

## 1. `surface-reachability`

Otworz kazda powierzchnie powstajaca w toku scenariusza: okno, modal, drawer,
menu, picker, popover, toast i panel. PASS wymaga, aby powierzchnia przecinala
aktualny widoczny obszar roboczy, a kluczowa tresc, akcja podstawowa i droga
zamkniecia byly dostepne bez zgadywania.

Dla aplikacji desktopowej sprawdz stosowalne zmiany topologii monitorow,
rozdzielczosci, skali/DPI oraz zapisanej pozycji okna. Nieprawidlowa geometria
ma zostac zacisnieta do widocznego obszaru albo bezpiecznie wycentrowana. Dla
przegladarki sprawdz docelowe viewporty, overflow, sticky/fixed UI i zoom.
Focus ma przejsc do otwartej powierzchni i wrocic do logicznego elementu po jej
zamknieciu.

FAIL: calosc lub krytyczna czesc otwiera sie poza ekranem, akcja albo zamkniecie
sa nieosiagalne, focus ginie lub uzytkownik musi znac ukryty workaround.

## 2. `action-feedback`

Kazda akcja powodujaca prace asynchroniczna albo zmiane stanu MUSI dac
natychmiastowy, postrzegalny sygnal przyjecia: press state, pending, progress,
status lub lokalna zmiane stanu. Nastepnie MUSI pojawic sie prawdziwy stan
terminalny: sukces, blad albo anulowanie. Dla operacji nieidempotentnych UI
blokuje przypadkowe ponowne wyslanie, ale nie odbiera bezpiecznego retry.

Kontrakt produktu zamraza wlasciwy prog czasu i rodzaj sygnalu. Nie stosuj
jednego arbitralnego limitu milisekund do wszystkich aplikacji.

FAIL: przycisk wyglada jak martwy, akcja moze zostac wyslana wielokrotnie,
spinner nie ma konca, sukces pojawia sie przed potwierdzeniem albo stan koncowy
nie odpowiada rzeczywistemu wynikowi systemu.

## 3. `visible-failure-recovery`

Wstrzyknij wszystkie stosowalne klasy awarii: walidacje, offline, timeout,
401/403, 404, konflikt 409, rate limit 429, 5xx i blad zadania w tle. PASS wymaga
widocznego, zrozumialego i bezpiecznego komunikatu w kontekscie akcji. Zachowaj
wpisane dane oraz orientacje uzytkownika, pokaz retry albo konkretna korekte i
usun falszywy sukces.

Sygnal widoczny dla uzytkownika skoreluj z dowodem systemowym, na przyklad
odpowiedzia sieciowa, stanem backendu albo zredagowanym logiem/trace. Dowod nie
moze ujawniac sekretow ani danych klienta.

FAIL: blad istnieje tylko w konsoli/logu, dialog znika bez wyjasnienia, UI
udaje sukces, traci formularz, pozostaje w permanentnym pending albo nie daje
bezpiecznej drogi wznowienia.

## Minimalny rekord dowodu

Zapisz dla kazdej bramki:

```text
role:
surface_or_route:
initial_screen: viewport/work-area, zoom, scale/DPI, monitors when applicable
action_trace:
injected_condition:
expected_cue:
observed_cue:
focus_state:
recovery_path:
system_correlation:
artifacts:
reviewer_and_verdict: PASS | FAIL | BLOCKED
```

Screenshot pokazuje polozenie i stan, lecz nie dowodzi czasu reakcji ani
korelacji z systemem. Do `action-feedback` dolacz action trace z pomiarem lub
nagranie; do `visible-failure-recovery` dolacz kontrolowana awarie i dowod
rzeczywistego wyniku. Maker nie wystawia finalnego werdyktu swojej pracy.
