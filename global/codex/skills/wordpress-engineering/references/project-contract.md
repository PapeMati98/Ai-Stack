# Kontrakt projektu WordPress

Przed edycją zapisz w roboczym planie:

- typ: core, plugin, theme, block theme, block, REST lub integracja;
- środowisko i dowód, że ścieżka jest właściwa;
- wersje WordPress, PHP, Node i głównych narzędzi;
- publiczny kontrakt, dane trwałe i wymagane capabilities;
- plan testów oraz rollback dla migracji lub operacji WP-CLI;
- czy operacja dotyka produkcji, publikacji, bazy lub zewnętrznego hostingu.

Minimalne kontrole security:

- sanitize przy wejściu i escape przy wyjściu;
- nonce dla intencji, capability dla autoryzacji;
- `permission_callback` dla endpointów REST;
- brak bezpośredniego zaufania do identyfikatora obiektu;
- zapytania parametryzowane i kontrolowana deserializacja;
- brak sekretów i danych osobowych w logach.
