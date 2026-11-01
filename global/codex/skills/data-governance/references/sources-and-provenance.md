# Zrodla i proweniencja

Skill zostal napisany od zera po izolowanym audycie materialow. Nie zawiera
zewnetrznego runtime, hookow, MCP, installerow ani skopiowanych procedur.

Wzorce inzynieryjne skonfrontowano z:

- OpenLineage: dataset/job/run, facets, ownership i lineage;
- Open Data Contract Standard: schema, quality, ownership i servers;
- PostgreSQL/Supabase guidance: constraints, least privilege, RLS i locki;
- EDPB/GDPR oraz NIST Privacy Framework: privacy-by-design i risk governance.

Lista i przypiete rewizje badanych repozytoriow sa w
`docs/DATA-GOVERNANCE-SOURCE-AUDIT-2026-08-28.md` repozytorium agent-stack.
Agregator lub search result nie jest source of truth. W kontrakcie zapisuj
wersje/commit, czas obserwacji i opcjonalny content hash; high-stakes fakt
sprawdzaj w pierwotnym, aktualnym zrodle.
