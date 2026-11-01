# Compute backend gate

## Kolejnosc wyboru

1. `local` - domyslny backend. Najpierw ocen VRAM, RAM, czas, zajetosc GPU,
   kompatybilnosc kerneli i koszt utraconego czasu.
2. `hf-jobs` - zarzadzany backend dla przypietego skryptu TRL/uv, gdy lokalny
   sprzet nie miesci runu albo termin uzasadnia jawnie zatwierdzony koszt.
3. `runpod` - elastyczny backend GPU, gdy potrzebny jest konkretny obraz,
   wolumen, dluzszy runtime albo sprzet niedostepny w HF Jobs.

Brak miejsca lokalnie nie jest sam w sobie zgoda na cloud. Zanim powstanie
platny zasob, campaign contract musi zawierac `ALLOW` dla wszystkich bramek.

## Wspolne bramki cloud

- owner approval: dokladny backend, maksymalna kwota i waluta;
- identity: zweryfikowane konto/organizacja bez ujawniania sekretu;
- data: klasyfikacja, licencja, zgoda na upload i minimalny zakres danych;
- source pin: commit/SHA skryptu, rewizja modelu i datasetu, digest obrazu;
- execution: flavor/GPU, timeout, limit liczby runow, kryterium stop;
- persistence: docelowe repo/bucket/wolumen, checkpoint cadence i resume proof;
- teardown: kto i kiedy zatrzymuje/usunie zasob oraz jak potwierdza brak kosztu;
- evidence: job id, czas, koszt, log metryk, lineage i wynik review.

Sekret przekazuj przez mechanizm secret store backendu. Nigdy nie zapisuj go w
campaign contract, repo, command history, MCP registry ani raporcie.

## Hugging Face Jobs / TRL

- To backend CLI/API, nie globalny MCP.
- Wymaga zalogowanego konta i dodatniego salda.
- Domyslny timeout Jobs jest za krotki dla wielu treningow; ustaw go jawnie.
- Srodowisko jest efemeryczne. Wynik i checkpointy musza trafic do zatwierdzonego
  prywatnego repo lub storage przed zakonczeniem joba.
- Nie uruchamiaj skryptu z ruchomego URL. Pobierz go, audytuj i przypnij SHA.
- `push_to_hub` jest zewnetrznym zapisem i wymaga jawnego celu, widocznosci oraz
  zgody; brak zgody oznacza prywatny, kontrolowany artefakt albo inny storage.

Zrodlo: https://huggingface.co/docs/trl/en/jobs_training

## RunPod

- Publiczny `runpod-docs` MCP sluzy tylko do czytania dokumentacji.
- Hosted RunPod API MCP i klucz API maja realne uprawnienia do platnych oraz
  destrukcyjnych operacji. Nie sa globalnym narzedziem agentow.
- Tworzenie Poda, endpointu, wolumenu lub rejestru wymaga osobnego zatwierdzenia
  z kosztem, regionem, obrazem, storage, exposure i teardown.
- Preferuj jednorazowy, waski skrypt/CLI z receipt nad stale aktywnym API MCP.

Zrodlo: https://github.com/runpod/runpod-mcp
