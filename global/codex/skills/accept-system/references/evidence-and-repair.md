# Biezace TODO: dowod i naprawa

Dla nowej pracy stosuj `../SKILL.md`: jeden odbior, aktualny expected/actual i srodowisko. Naprawa wymaga retestu zmienionego zachowania i istotnych regresji. Poprawny dowod niezmienionego artefaktu mozna uzyc ponownie tylko przy zgodnych wejsciach, kryteriach, srodowisku i komendzie. Naprawa formatu/transportu nie wymaga ponownego kodowania ani pelnego retestu. Nie przedstawiaj brakujacego dowodu jako PASS.

> Ponizszy format generacji/bindingu i helpera jest HISTORYCZNY WYLACZNIE: dotyczy przypietych starych kontraktow/recovery, nie nowego TODO.

# Dowody, defekty i naprawa

## Dowod

Kazdy wynik jest przywiazany do aktualnej generacji i bindingu. Manifest ma:
reviewera, czas, oracle expected/actual/method, artefakty z SHA-256 oraz runtime
hash helpera i manifestu skilla.

UAT dodatkowo ma role, surface/route, poczatkowy viewport lub widoczny obszar
roboczy, skale/DPI i topologie monitorow gdy dotyczy, sekwencje akcji,
wstrzykniety warunek, oczekiwany i zaobserwowany sygnal, stan focusu, droge
odzyskania oraz korelacje z backendem/siecia/logiem. Dla user-facing workflow
manifest wskazuje osobne dowody `surface-reachability`, `action-feedback` i
`visible-failure-recovery` zgodnie z `user-perception-and-feedback.md`.
Side-effect safety ma dokladny enforcement scope, mechanizm i evidence ref.
Artefakty sa kopiowane do runu i ponownie hashowane przy zamknieciu.

Dla `surface=cli` dowod user journey nie zawiera viewportu ani route. Zawiera
liste `journey.command` identyczna z zamrozonym `criterion.cli.argv`,
`exit_code` oraz `expected_exit_code` identyczny z zamrozonym oczekiwaniem, a
takze dwa hashowane artefakty stdout/stderr. `PASS` jest mozliwy tylko przy
rownosci exit code; niezerowy zamrozony expected jest legalnym testem
negatywnym. Dowod CLI nie moze pokrywac kryterium graphical.

Nie zapisuj storageState, cookies, credentials, tokenow, hasel, sekretow ani
authorization headers. Nie uzywaj screenshotu jako jedynego dowodu zachowania.

## Defekt

`FAIL` i `BLOCKED` sa append-only. Nie wolno nadpisac wyniku kolejnym PASS w tej
samej generacji. To zapobiega wybieraniu najwygodniejszego runu.

## Naprawa

Naprawa przyjmuje nowy binding oraz resolution obejmujace WSZYSTKIE FAIL/BLOCKED
z aktualnej generacji. Binding musi sie zmienic. Helper:

1. oznacza historyczne porazki jako rozwiazywane przez kolejna generacje;
2. zachowuje stare dowody;
3. uniewaznia kazdy poprzedni PASS;
4. ustawia wszystkie stosowalne kryteria na PENDING;
5. wymaga pelnego retestu.

Brak zmiany bindingu, niepelna lista failure IDs albo przekroczony budzet
generacji blokuje naprawe.

## Evidence-only rebind

`evidence-only-rebind` jest waska sciezka dla zmiany transportu lub formalnego
packetu bez zmiany produktu. Nie jest waiverem ani automatycznym PASS. Helper
przyjmuje jawna liste `preserve_pass_evidence_ids` i zachowuje tylko te PASS,
ktore przejda wszystkie ponizsze testy:

1. nowy binding ma identyczny SHA-256 jak aktywny binding, a jego snapshot nie
   dryfuje;
2. snapshot kryteriow, wymagan i graphu ma identyczne hashe jak przy `init`;
3. manifest dowodu i wszystkie hashowane artefakty sa identyczne i aktualne;
4. dowod ma nadal ten sam criterion, oracle, binding i niezaleznego reviewera.

`changed_paths` musi byc puste; sciezki transportu/formalnego packetu wpisuje
sie jawnie do `packet_paths`. Zmiana produktu, srodowiska, kryteriow,
wymagan, graphu lub bindingu, brak dowodu, tamper, utrata niezaleznosci albo
staleness odrzuca skrot i wymaga zwyklego `repair` z pelnym retestem. PASS nie
wskazane jawnie na liscie przechodzi do nowej generacji jako `PENDING`.

## Zamkniecie

Pierwszy `close` przy brakach zwraca niekoncowe `NOT_ACCEPTED`, jezeli budzet
napraw pozostaje. Finalny receipt powstaje tylko raz. Jest `ACCEPTED` przy
komplecie aktualnych PASS albo `NOT_ACCEPTED` po wyczerpaniu budzetu lub jawnym
stop reason. Nie zawiera sredniej, punktacji ani procentu, ktory moglby
przykryc krytyczny FAIL.

Receipt zawiera `graph_run_id` i moze zamknac tylko ten konkretny run Agent
Graph. Zgodnosc samego graph hash nie wystarcza do ponownego uzycia receiptu.

Finalny odbior jest per scope/run; receipt z innego scope nie jest laczony
automatycznie. Kazdy materialny criterion nadal wymaga wlasnego oracle i
niezaleznego reviewera. Nie ma automatycznej roli `reviewer-of-review`, ktora
zastepowalaby ten dowod.

Budzet tokenow domyslnie ma charakter `estimate`; jawny `hard` pozostaje
twardym limitem. Brak lub `unknown` nie jest szacowany jako koszt, a sama
telemetry estimate nie blokuje odbioru.
