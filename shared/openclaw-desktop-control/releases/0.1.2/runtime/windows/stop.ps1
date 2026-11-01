# Ask the operator to persist STOP. Does not delete the authority database.
# This script does not send Telegram messages and does not read secrets.
param(
    [Parameter(Mandatory = $true)][string]$DbPath
)
$ErrorActionPreference = "Stop"
$Project = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$Cli = Join-Path $Project "transport\openclaw_control\cli.py"
& python $Cli --db $DbPath stop --scope all --reason operator-rollback
