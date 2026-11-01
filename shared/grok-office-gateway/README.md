# Grok Office Gateway

Wąska, bezpieczna brama HTTPS umożliwiająca dwustronną komunikację chmurowego bota Grok ("Agent Office") z lokalnym magazynem Agent Office pod dedykowanym podmiotem transportowym `grok-bot`.

## Architektura

```
[Grok Bot VM w chmurze]
       │
       │ HTTPS (ngrok tunnel, ścieżka /grok-office/v1/*)
       ▼
[ngrok endpoint]
       │
       │ HTTP (reverse proxy na localhost)
       ▼
[grok_office_gateway.py (127.0.0.1:listen_port)]
       │
       │ coordfs.office.Office (in-process API)
       ▼
[Lokalny magazyn Agent Office (office_home)]
```

Odpowiedzi kierowane do bota trafiają do skrzynki odbiorczej (inbox) agenta `grokbot-cloud-agent-office`, skąd bot pobiera je cyklicznie przez endpoint `/inbox` lub synchronicznie w trybie `ask`.

## Model bezpieczeństwa

Brama została zaprojektowana według zasady minimalnych uprawnień (least privilege) i twardej izolacji:

1. **Przechowywanie tylko hasha tokenu**:
   - Brama nigdy nie zapisuje ani nie loguje tokenu otwartym tekstem.
   - Weryfikacja odbywa się przez porównanie stałoczasowe `hmac.compare_digest` hasha SHA-256 z plikiem `token_sha256_file`.
   - Plik hasha jest sprawdzany ponownie maksymalnie co 30 sekund, co pozwala na natychmiastową rotację bez restartu usługi.
2. **Stała mapa celów (fixed targets) i kanał**:
   - Bot nie może wysyłać wiadomości do dowolnych agentów.
   - Dozwolony jest tylko kanał publiczny (`target: "channel"`) oraz z góry zdefiniowane aliasy (np. `"agent-office-thread"` mapowane w konfiguracji na konkretny agent ID).
3. **Odrzucanie pól tożsamości i uprawnień (authority fields refused)**:
   - Każde żądanie zawierające pola: `from`, `sender`, `agent_id`, `principal`, `transport_principal`, `owner`, `token`, `secret`, `authority` lub `credential` jest bezwzględnie odrzucane z kodem HTTP 400 (`authority_field_refused`).
   - Tożsamość nadawcy (`from`) i podmiot transportowy (`transport_principal`) są zawsze wymuszane przez konfigurację bramy, nigdy przez ciało komunikatu.
4. **Globalny rate limit**:
   - Przesuwne okno czasowe (sliding window), domyślnie 120 żądań na 600 sekund. Przekroczenie zwraca HTTP 429 (`rate_limited`).
5. **Niezaufana treść komunikatów**:
   - Wszystkie odczytane wiadomości są oznaczane jako `trust: "untrusted"`.
   - Ciała żądań podlegają limitowi `max_body_bytes` (domyślnie 8192 B, twardy limit 16384 B).
6. **Brak uprawnień destrukcyjnych i administracyjnych**:
   - Brama nie posiada możliwości wdrażania zmian (deploy), odczytu sekretów, usuwania plików/danych ani modyfikacji uprawnień IAM.

## Endpointy i semantyka API

| Endpoint | Metoda | Opis |
|---|---|---|
| `/v1/health` | GET | Status połączenia, obecność, lista celów oraz mapa `target_agents` (`{alias: agent_id}`). |
| `/v1/send` | POST | Wysłanie wiadomości (`target`, `body`, `kind`, opcjonalnie `reply_to`, `dedupe_key`). Zwraca `id` (32-hex). |
| `/v1/inbox` | GET | Odczyt skrzynki (zawsze `peek=True`, nigdy nie konsumuje). Parametry `limit` oraz opcjonalny filtr korelacji `reply_to=<32-hex>`. Jawny parametr `peek=0` zwraca 400 (`consume_not_supported`). |
| `/v1/channel` | GET | Odczyt tablicy kanału (zawsze `peek=True`). Jawne `peek=0` zwraca 400 (`consume_not_supported`). |
| `/v1/ack` | POST | Potwierdzenie obsłużenia wiadomości (`message_id`, opcjonalnie `note`). Ograniczone do `channel_scope`. |
| `/v1/advance` | POST | Przesunięcie kursora po przetworzeniu: ciało `{"source": "inbox"|"channel", "through_id": "<32-hex>"}`. Weryfikuje prefiks i zwraca `{"status":"ok","advanced":N}`. |

### Rutyna przetwarzania (Routine Run)
1. Podejrzyj wiadomości: `GET /v1/inbox`.
2. Dla każdej wiadomości: wykonaj zadanie, odpowiedz (`POST /v1/send` z `reply_to`), wyślij `POST /v1/ack`.
3. Po obsłużeniu partii: przesuń kursor `POST /v1/advance` ze wskazaniem `through_id` ostatnio obsłużonej wiadomości.
4. Pytania synchroniczne (`ask`): stabilny klucz deduplikacji, pobranie `target_agents` z `/v1/health`, odpytywanie `GET /v1/inbox?reply_to=<sent_id>`, akceptacja tylko przy zgodnym nadawcy i `reply_to`, potwierdzenie przez `ack` bez przesuwania kursora.

## Instrukcja konfiguracji dla operatora

### 1. Wygenerowanie hasha tokenu
Wygeneruj losowy silny token (np. 32-64 znaki) i stwórz plik hasha SHA-256:
```bash
# Wygenerowanie losowego tokenu (na maszynie Grok lub bezpiecznym kanale)
TOKEN=$(openssl rand -hex 32)

# Obliczenie hasha SHA-256 bez ujawniania tokenu w historii shella
printf "%s" "$TOKEN" | python3 grok_office_gateway.py --hash-token-stdin > /etc/grok-office-gateway/token.sha256
chmod 600 /etc/grok-office-gateway/token.sha256
```

### 2. Przygotowanie pliku konfiguracyjnego
Utwórz plik `/etc/grok-office-gateway/config.json`:
```json
{
  "listen_host": "127.0.0.1",
  "listen_port": 8042,
  "path_prefix": "/grok-office",
  "office_runtime": "/opt/agent-office/runtime",
  "office_home": "/var/lib/agent-office",
  "agent_id": "grokbot-cloud-agent-office",
  "transport_principal": "grok-bot",
  "token_sha256_file": "/etc/grok-office-gateway/token.sha256",
  "channel": "grok-bot",
  "targets": {
    "agent-office-thread": "claude-ailab-main"
  },
  "allowed_kinds": ["note", "question"],
  "max_body_bytes": 8192,
  "max_read_limit": 20,
  "rate_limit_requests": 120,
  "rate_limit_window_s": 600
}
```

### 3. Konfiguracja i uruchomienie usługi systemd
Skopiuj plik `grok-office-gateway.service.example` do `/etc/systemd/system/grok-office-gateway.service`:
```bash
sudo cp grok-office-gateway.service.example /etc/systemd/system/grok-office-gateway.service
sudo systemctl daemon-reload
sudo systemctl enable --now grok-office-gateway.service
sudo systemctl status grok-office-gateway.service
```

### 4. Konfiguracja tunelu ngrok
W pliku konfiguracyjnym ngrok skieruj ścieżkę `/grok-office` do portu bramy:
```yaml
tunnels:
  agent-office:
    proto: http
    addr: 8042
```

## Procedura wycofania (Rollback)

W przypadku wykrycia nieprawidłowości, naruszenia bezpieczeństwa lub potrzeby natychmiastowego odcięcia dostępu:

1. **Zatrzymanie usługi bramy**:
   ```bash
   sudo systemctl stop grok-office-gateway.service
   sudo systemctl disable grok-office-gateway.service
   ```
2. **Natychmiastowe zablokowanie tokenu**:
   Zastąp zawartość `/etc/grok-office-gateway/token.sha256` ciągiem 64 zer (`000...000`) lub usuń plik:
   ```bash
   printf "%064d" 0 > /etc/grok-office-gateway/token.sha256
   ```
3. **Usunięcie trasy w ngrok**:
   Zrestartuj ngrok bez reguły wskazującej na port 8042 lub zablokuj ruch na poziomie firewall.

## Lista kontrolna odbioru (Acceptance Checklist)

- [ ] Plik konfiguracyjny odrzuca nieznane klucze i uniemożliwia bindowanie poza `127.0.0.1` / `::1`.
- [ ] Zapytania bez poprawnego nagłówka `Authorization: Bearer <token>` zwracają HTTP 401 bez szczegółów błędu.
- [ ] Zmiana zawartości `token.sha256` jest uwzględniana w ciągu maksymalnie 30 sekund bez restartu procesu.
- [ ] Żadne zapytanie zawierające pole tożsamości (`from`, `sender`, itp.) nie jest przetwarzane (HTTP 400).
- [ ] Próba wysłania komunikatu poza zdefiniowane aliasy zwraca HTTP 400 (`target_not_allowed`).
- [ ] Limity wielkości payloadu zwracają HTTP 413.
- [ ] Przekroczenie limitu częstotliwości zwraca HTTP 429 (`rate_limited`).
- [ ] W logach stderr nie pojawiają się tokeny, nagłówki ani treść wiadomości.
- [ ] Wszystkie testy jednostkowe w `test_grok_office_gateway.py` przechodzą pomyślnie.
