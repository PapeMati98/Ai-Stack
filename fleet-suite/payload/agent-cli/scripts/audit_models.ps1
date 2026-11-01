[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"
$SuiteRoot = $env:AGENT_FLEET_SUITE_ROOT
if ([string]::IsNullOrWhiteSpace($SuiteRoot)) {
    $SuiteRoot = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
}
$Radar = Join-Path $SuiteRoot "model-radar\scripts\model_radar.py"
if (-not (Test-Path -LiteralPath $Radar -PathType Leaf)) {
    throw "Model Radar is not installed: $Radar"
}

& python $Radar check
exit $LASTEXITCODE
