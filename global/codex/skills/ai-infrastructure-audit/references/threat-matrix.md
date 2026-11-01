# Macierz zagrożeń

| Obszar | Minimalne pytanie kontrolne |
|---|---|
| Agent i prompt | Czy nieufna treść może zmienić instrukcje lub zakres narzędzi? |
| Skill i plugin | Czy pochodzenie, wersja, pliki i uprawnienia są zweryfikowane przed instalacją? |
| MCP i narzędzia | Czy każde narzędzie ma minimalne uprawnienia, walidację parametrów, jawny owner oraz przypięty manifest i opis odporny na rug pull? |
| Pamięć i RAG | Czy źródła, proweniencja, tenant, filtry metadanych, model embeddingów, `top_k` i instrukcje w dokumentach są separowane? |
| Modele i endpointy | Czy dane, logi, retencja, fallback oraz zmiana modelu są kontrolowane? |
| Wykonanie | Czy shell, sieć, filesystem i procesy są ograniczone poza promptem? |
| Łańcuch dostaw | Czy aktualizacje są przypięte, skanowane, odwracalne i audytowalne oraz czy provenance weryfikuje oczekiwany issuer i identity? |
| Operacje | Czy istnieją rate limit, telemetria, lock, recovery i niezależna bramka jakości? |
