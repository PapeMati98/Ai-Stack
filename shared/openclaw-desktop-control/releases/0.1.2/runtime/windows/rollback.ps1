# Rollback: persist STOP and keep the authority SQLite file.
# Does not delete queues, inbox, pairing, or ledger tables.
param(
    [Parameter(Mandatory = $true)][string]$DbPath
)
$ErrorActionPreference = "Stop"
$Project = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$Cli = Join-Path $Project "transport\openclaw_control\cli.py"
& python $Cli --db $DbPath stop --scope all --reason rollback-preserve-state
Write-Output "STOP persisted. Authority file preserved at $DbPath"
