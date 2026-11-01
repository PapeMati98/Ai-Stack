# Start the local OpenClaw controller. Configuration metadata only.
# Token must already be in this process environment as TELEGRAM_BOT_TOKEN.
# Never pass the token as an argument.
param(
    [Parameter(Mandatory = $true)][string]$ConfigPath
)
$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
if (-not $Root) { $Root = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path }
# $PSScriptRoot = runtime/windows -> runtime -> project
$Project = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$RunPy = Join-Path $Project "runtime\run.py"
& python $RunPy --config $ConfigPath --serve
