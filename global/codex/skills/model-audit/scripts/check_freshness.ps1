# check_freshness.ps1 - deterministyczna bramka swiezosci audytu rynku modeli.
# Exit 0  = AUDIT_FRESH  (audyt < MaxAgeDays -> agent KONCZY zadanie, nic nie robi)
# Exit 10 = AUDIT_STALE / AUDIT_MISSING (agent musi przeprowadzic audyt i nadpisac plik)
# Czyta date z frontmatter `last_audit: YYYY-MM-DD` (a nie z mtime - kopiowanie pliku nie moze
# udawac swiezosci).
[CmdletBinding()]
param(
    [string]$File = (Join-Path $PSScriptRoot "..\..\..\..\..\MODEL-MARKET.md"),
    [int]$MaxAgeDays = 7
)

if (-not (Test-Path -LiteralPath $File)) {
    Write-Output "AUDIT_MISSING|brak pliku|$File"
    exit 10
}

$head = Get-Content -LiteralPath $File -TotalCount 15 -Encoding UTF8
$line = $head | Where-Object { $_ -match '^\s*last_audit\s*:\s*(\d{4}-\d{2}-\d{2})' } | Select-Object -First 1
if (-not $line) {
    Write-Output "AUDIT_MISSING|brak pola last_audit w naglowku|$File"
    exit 10
}

$null = $line -match '(\d{4}-\d{2}-\d{2})'
$auditDate = [datetime]::ParseExact($Matches[1], 'yyyy-MM-dd', $null)
$age = [int]((Get-Date).Date - $auditDate.Date).TotalDays

if ($age -le $MaxAgeDays) {
    Write-Output "AUDIT_FRESH|wiek=${age}d (limit ${MaxAgeDays}d)|last_audit=$($Matches[1])"
    exit 0
} else {
    Write-Output "AUDIT_STALE|wiek=${age}d (limit ${MaxAgeDays}d)|last_audit=$($Matches[1])"
    exit 10
}
