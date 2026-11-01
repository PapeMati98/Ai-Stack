# Privacy, retencja i usuwanie

Skill wymusza rozliczalne pytania, ale nie wydaje opinii prawnej.

## Minimalne decyzje

- czy aktywo zawiera personal/special-category albo umozliwia inference;
- purpose i zakazane secondary uses;
- minimalny zakres, pola i okres potrzebny do celu;
- lawful basis lub inna podstawa wskazana przez legal/DPO;
- czy DPIA/PIA jest wymagane i jaki ma status;
- regiony, subprocessors i transfer decision;
- retencja, trigger rozpoczecia okresu, legal hold i decision owner;
- prawo do usuniecia/rektyfikacji oraz propagacja do cache, backupu i eksportu.

`PENDING` blokuje odpowiedni etap. Agent nie zamienia go na `NOT_APPLICABLE`.

## DEV/TEST

Preferuj dane syntetyczne. Pseudonimizacja nie jest anonimizacja, a kopia
produkcji po maskowaniu nadal wymaga oceny ryzyka i ograniczen dostepu. Nie
przesylaj danych restricted do zewnetrznego modelu w celu wygenerowania testow.

## Usuwanie

Delete oracle obejmuje primary store, indeksy, cache, object storage, kolejki,
eksporty, wyszukiwarki i zachowanie backupu. Dowod powinien rozrozniac natychmiastowe
usuniecie, tombstone, wygasniecie backupu oraz legal hold. Sam kod `DELETE 200`
nie dowodzi kompletnego usuniecia.

## Aktualnosc

Przed decyzja odswiez zrodlo pierwotne, np. tekst prawa albo aktualna guidance
organu. Repo z cudzym skillem nie jest autorytatywnym zrodlem prawa.
