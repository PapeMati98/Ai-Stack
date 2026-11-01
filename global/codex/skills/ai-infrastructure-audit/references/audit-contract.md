# Kontrakt audytu

Raport musi zawierać:

1. Zakres, wyłączenia i źródła dowodów.
2. Diagram lub tabelę granic zaufania i przepływu danych.
3. Kontrole dla tożsamości, uprawnień, sekretów, sieci, pamięci, narzędzi i aktualizacji.
4. Rozróżnienie: polityka promptowa, walidacja aplikacyjna, izolacja systemowa i kontrola infrastruktury.
5. Tabelę `id | ryzyko | potwierdzone | dowód | severity | remedacja | decyzja właściciela`.
6. Testy wykonane i świadomie pominięte.
7. Werdykt ogólny `SAFE`, `SAFE WITH CAVEATS` albo `NOT SAFE`.

Brak dowodu daje `BLOCKED`, nie domyślne `PASS`.
