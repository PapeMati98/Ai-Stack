# TEMPLATE ONLY. Do not store a live bot token in this repository.
# Parent may copy this file and protect a DPAPI blob offline.
# This template never reads a secret: it shows the intended process env
# hand-off (TELEGRAM_BOT_TOKEN) and then execs run.py.
param(
    [Parameter(Mandatory = $true)][string]$ConfigPath
)
$ErrorActionPreference = "Stop"
# Placeholder: parent replaces this block with a DPAPI unprotect into a local
# variable, then sets $env:TELEGRAM_BOT_TOKEN for this process only.
# $SecureBlobPath = "C:\path\to\protected\blob"
# $env:TELEGRAM_BOT_TOKEN = "<filled by parent DPAPI unprotect, never logged>"
if (-not $env:TELEGRAM_BOT_TOKEN) {
    Write-Error "TELEGRAM_BOT_TOKEN is not set in this process (template is inert)"
    exit 2
}
$Project = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$RunPy = Join-Path $Project "runtime\run.py"
& python $RunPy --config $ConfigPath --serve
