# Migracje i backfille

## Kontrakt wykonania

1. Zidentyfikuj source of truth, zakres, consumers i resource lock.
2. Preferuj `expand -> migrate -> contract`; alternatywa wymaga uzasadnienia.
3. Graph nabywa `data:<asset-id>` locki; druga kolidujaca mutacja czeka albo
   konczy sie `BLOCKED`, nigdy nie omija locka.
4. Pod lockiem zwiaz pre-state/source watermark z subject hash i jednoznacznym
   kluczem idempotencji.
5. Utworz backup tego stanu i wykonaj restore test w izolowanym srodowisku.
6. Zrob dry-run albo bounded slice, potem probe na reprezentatywnej skali.
7. Reconcile row counts, klucze, referential integrity, materialne agregaty,
   constraints, duplikaty i zachowanie konsumentow.
8. Oddziel `rollback` od `roll-forward`. Jesli rollback traci dane, nie nazywaj
   go bezpiecznym; zatrzymaj cutover albo uzyj restore/forward recovery.
9. Contract phase uruchom dopiero po okresie kompatybilnosci i dowodzie braku
   starych konsumentow.

Helper potwierdza komplet i integralnosc artefaktow. Backup, restore,
reconciliation i live-state musza wykonac narzedzia projektu; ich dowod zawiera
content hash, subject hash, czas i niezaleznego reviewera.

## Backfill

Backfill ma byc restartowalny, ograniczony zakresem, obserwowalny i odporny na
duplikaty. Zapisz partition/range, checkpoint, retry policy, late arrivals,
deduplication key, concurrency, throttling i sposob uniewaznienia cache.

## Produkcja

PROD cutover, destrukcyjne DDL/DML, disable constraints, dual-write i zmiana
system of record wymagaja osobnej jawnej zgody Mateusza. PASS helpera nie jest
taka zgoda.
