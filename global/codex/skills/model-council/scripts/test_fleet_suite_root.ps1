$ErrorActionPreference = "Stop"

# rada.ps1 wykonuje panel przy dot-source, wiec wyciagamy sama funkcje z AST.
$ast = [System.Management.Automation.Language.Parser]::ParseFile(
    (Join-Path $PSScriptRoot "rada.ps1"), [ref]$null, [ref]$null)
$fn = $ast.Find({ param($node)
    $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and
    $node.Name -eq "Resolve-FleetSuiteRoot" }, $true)
if ($null -eq $fn) { throw "ASSERTION_FAILED: brak funkcji Resolve-FleetSuiteRoot" }
Invoke-Expression $fn.Extent.Text

function Assert-True([bool]$Condition, [string]$Message) {
    if (-not $Condition) { throw "ASSERTION_FAILED: $Message" }
}

function New-Runtime([string]$Parent, [string]$Name, [string]$Version, [switch]$NoRadar, [switch]$NoMarker) {
    $root = Join-Path $Parent $Name
    New-Item -ItemType Directory -Force -Path (Join-Path $root "model-radar\scripts") | Out-Null
    if (-not $NoRadar) { Set-Content -LiteralPath (Join-Path $root "model-radar\scripts\model_radar.py") -Value "" }
    if (-not $NoMarker) {
        Set-Content -LiteralPath (Join-Path $root ".agent-fleet-suite-runtime.json") `
            -Value (@{ package_version = $Version } | ConvertTo-Json)
    }
    return $root
}

# Fikstura w katalogu tymczasowym; test niczego nie kasuje.
$parent = Join-Path ([System.IO.Path]::GetTempPath()) ("rada-fleet-root-" + [guid]::NewGuid().ToString("N"))
New-Item -ItemType Directory -Path $parent | Out-Null
$v9 = New-Runtime $parent "lean-0.9.9" "0.9.9"
$v68 = New-Runtime $parent "lean-0.9.68" "0.9.68"
New-Runtime $parent "lean-0.9.99" "0.9.99" -NoRadar | Out-Null
New-Runtime $parent "lean-1.0.0" "1.0.0" -NoMarker | Out-Null
New-Runtime $parent "lean-2.0.0" "abc" | Out-Null  # marker z wersja nie do odczytania

Assert-True ((Resolve-FleetSuiteRoot -EnvRoot "" -RuntimeParent $parent) -eq $v68) `
    "bez zmiennej wybiera najnowszy runtime z markerem i Model Radar (wersja liczbowo, nie tekstowo)"
Assert-True ((Resolve-FleetSuiteRoot -EnvRoot $v9 -RuntimeParent $parent) -eq $v9) `
    "jawna zmienna wygrywa z wyborem najnowszego"
Assert-True ((Resolve-FleetSuiteRoot -EnvRoot "" -RuntimeParent (Join-Path $parent "brak")) -like "*<brak zainstalowanego runtime>") `
    "brak runtime daje sciezke prowadzaca do MODEL_RADAR_MISSING, nie wyjatek"

Write-Output "test_fleet_suite_root: PASS"
