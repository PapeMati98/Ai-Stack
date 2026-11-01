# Kontrakt decyzji architektonicznej

Każda decyzja powinna zawierać:

1. Problem i zakres, bez proponowania rozwiązania w opisie problemu.
2. Dowody z aktualnego kodu i ograniczeń operacyjnych.
3. Pojęcia domenowe oraz niezmienniki.
4. Obecne i proponowane granice modułów.
5. 2-3 warianty, w tym wariant minimalny.
6. `Fixed diff point` oraz spodziewane przyszłe zmiany, które ma zawierać.
7. Plan pionowych kroków z testem i rollbackiem.
8. Ryzyka, decyzje właściciela i warunek zatrzymania.

Jeśli nie da się wskazać realnej osi zmienności, preferuj prosty kod zamiast nowej abstrakcji.

## Mapa wpływu przed edycją

Przed istotnym usunięciem albo przeniesieniem modułu, zmianą jego nazwy na granicy modułów
lub zmianą publicznego kontraktu sporządź krótką mapę wpływu:

1. **Callerzy przychodzący** — bezpośrednie ścieżki i miejsca wywołania zależne od zmiany.
2. **Zależności wychodzące** — usługi, storage i kontrakty, z których zmieniany moduł korzysta.
3. **Przepływ danych** — istotne wejścia, transformacje lub niezmienniki oraz wyjścia i zapis stanu.
4. **Konsumenci** — moduły, klienci lub procesy odczytujące zmieniany kontrakt albo dane.
5. **Sprawdzenia regresji** — testy obejmujące te ścieżki oraz zauważone luki w pokryciu.

Dla małej zmiany lokalnej bez wpływu na granice modułów lub publiczny kontrakt nie twórz osobnej mapy ani artefaktu architektonicznego.
