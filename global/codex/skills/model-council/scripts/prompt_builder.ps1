function Build-CouncilReviewUser {
    param(
        [string]$LensName,
        [string]$LensDescription,
        [AllowNull()][string]$ReviewPlan,
        [AllowNull()][string]$ReviewContext
    )

@"
# ZADANIE / PLAN DO RECENZJI
$ReviewPlan

# KONTEKST
$ReviewContext

# TWOJA SOCZEWKA
${LensName}: $LensDescription

# POLECENIE
Zwroc wylacznie ten format:

## TOP RYZYKA (max 5, od najgrozniejszego)
- [pewnosc] Ryzyko - dlaczego boli - jak/kiedy wybuchnie

## CZEGO BRAKUJE
- [pewnosc] Brak X - konsekwencja

## LEPSZA KOLEJNOSC (albo "sekwencja OK")
- Co przestawic i dlaczego

## ALTERNATYWA (albo "brak lepszej")
- Szkic - zysk - koszt / kiedy sie nie oplaca

## WERDYKT
- GO / GO-Z-POPRAWKAMI / NO-GO
- 1-3 warunki MUST przed startem
- Pewnosc werdyktu + czego zabraklo w kontekscie
"@
}

function Build-CouncilBrainstormRoundOneUser {
    param(
        [string]$LensName,
        [string]$LensDescription,
        [AllowNull()][string]$ReviewPlan,
        [AllowNull()][string]$ReviewContext
    )

@"
# ZADANIE / PROBLEM
$ReviewPlan

# KONTEKST
$ReviewContext

# TWOJA SOCZEWKA
${LensName}: $LensDescription

# POLECENIE: RUNDA 1 - NIEZALEZNA DYWERGENCJA
Wygeneruj falsyfikowalne pomysly, hipotezy, alternatywy i testy rozstrzygajace.
Nie zakladaj, ze istnieja odpowiedzi innych modeli. Nie oceniaj innych odpowiedzi.
Zwroc konkretne propozycje, zalozenia, ryzyka, koszty i kryteria zatrzymania.
Nie glosuj i nie wydawaj decyzji produkcyjnej.
"@
}

function Get-BrainstormRoundTwoCandidates {
    param([array]$HealthyEvidence)

    return @($HealthyEvidence)
}
