# Brainstorm Protocol

`-Brainstorm` jest osobnym trybem `rada.ps1`; domyslny review/GO-NO-GO nie zmienia zachowania.

## Preflight

Przed pierwszym platnym wywolaniem wykonywany jest jeden wspolny preflight Model Radar + roster + aktywne endpointy. Brak swiezosci, zdrowego rosteru lub minimalnego skladu konczy przebieg bez inferencji. Ten sam resolved roster obowiazuje w obu rundach.

## Runda 1

Kazdy resolved member dostaje ten sam problem i swoja soczewke, bez odpowiedzi pozostalych. Odpowiedz ma zawierac niezalezne pomysly, falsyfikowalne hipotezy, ryzyka, koszty i testy rozstrzygajace. Odpowiedzi sa zapisywane jako dowody z tozsamoscia modelu.

## Anonimizacja

Tylko zdrowe odpowiedzi z rundy 1 trafiaja do lokalnego helpera `brainstorm_protocol.py`. Bundle jest numerowany jako `POMYSL 01...`, a nazwy/model IDs, role i soczewki sa redagowane. Bundle nie jest publikowany ani wysylany jako osobna operacja.

## Runda 2

Kazdy zdrowy czlonek z rundy 1 otrzymuje ten sam anonimowy bundle i ocenia kierunki przez swoja soczewke: najmocniejsze elementy, krytyka, integracja, spory i minimalne testy rozstrzygajace. To nie jest glosowanie. Brak minimalnej liczby odpowiedzi konczy sie `INSUFFICIENT_RESPONSES` i nie uruchamia fallbacku.

## Manifest i granice

`_manifest.json` ma `mode=brainstorm`, osobne wpisy `round1`/`round2`, listy plikow dowodowych, tokeny i koszt per odpowiedz oraz sumy. Rodzic pozostaje syntezerem. Tryb nie publikuje, nie wdraza, nie uruchamia decyzji produkcyjnych i nie wykonuje automatycznego fallbacku.
