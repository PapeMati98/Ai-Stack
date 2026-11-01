# Srodowisko i efekty zewnetrzne

## DEV i TEST

- tylko dane syntetyczne;
- recipient policy `synthetic-only`;
- external side effects `blocked`;
- pusta lista realnych destinations;
- email, payment, calendar, CRM i public integration sa jawnie w denied effects;
- dowod blokady wskazuje mechanizm i jego dokladny zakres.

Fake SMTP, sandbox platnosci albo lokalny stub jest dobrym dowodem tylko wtedy,
gdy request nie moze ominac tej granicy. Sam tekst promptu "nie wysylaj" nie
jest mechanizmem egzekwowania.

## STAGING

Uzywaj danych syntetycznych lub zanonimizowanych i jawnej allowlisty. Kazda
destynacja jest przywiazana do kontraktu. Zmiana allowlisty tworzy nowy binding
albo nowy kontrakt, zaleznie od tego, czy zmienia owner intent.

## PROD

Odbior produkcyjny nie wynika ze zgody na implementacje. Wymaga osobnego,
ograniczonego owner approval z zakresem i terminem. Skill nie wykonuje deployu,
maila, platnosci ani mutacji; jedynie waliduje dowody z wykonania majacego
wlasna autoryzacje.

## Zakres egzekwowania

Dozwolone etykiety to `browser-context`, `application`, `network-egress`,
`external-service` i `host`. Dowod Playwright route/offline/service-worker
dotyczy wylacznie `browser-context`. Mechanizm browser-only nie moze zaspokoic
zakresu `application`, `network-egress`, `external-service` ani `host`.

`remote-computer-use` V0.1 daje izolowany, efemeryczny Chromium i kontroluje
ruch tej sesji. Nie daje ogolnego computer use ani gwarancji dla pozostalych
procesow VM.
