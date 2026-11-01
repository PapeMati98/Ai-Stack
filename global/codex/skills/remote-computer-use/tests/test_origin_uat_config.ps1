[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
$SkillRoot = Split-Path -Parent $PSScriptRoot
$RunnerSource = Join-Path $SkillRoot 'assets\vm-runner'
$ConfigureScript = Join-Path $RunnerSource 'scripts\configure-origin-uat.ps1'
$caseId = [Guid]::NewGuid().ToString('N')
$TargetRoot = Join-Path $env:ProgramData "MateuszAI\rcu-origin-config-test-$caseId"
$TransactionId = 'rcu-origin-' + [DateTime]::UtcNow.ToString('yyyyMMddTHHmmssZ') + '-' + $caseId.Substring(0, 8)
$TransactionRoot = Join-Path $env:ProgramData "MateuszAI\remote-computer-use-origin-config\transactions\$TransactionId"

function Write-Utf8NoBom([string]$Path, [string]$Content) {
    New-Item -ItemType Directory -Force -Path (Split-Path -Parent $Path) | Out-Null
    $encoding = New-Object System.Text.UTF8Encoding($false)
    [System.IO.File]::WriteAllText($Path, $Content, $encoding)
}

try {
    $current = Join-Path $TargetRoot 'current'
    New-Item -ItemType Directory -Force -Path $current | Out-Null
    Copy-Item -Path (Join-Path $RunnerSource '*') -Destination $current -Recurse -Force
    $npmOutput = & npm.cmd ci --omit=dev --ignore-scripts --prefix $current 2>&1
    if ($LASTEXITCODE -ne 0) { throw "npm ci failed: $($npmOutput -join ' ')" }

    $config = Get-Content -LiteralPath (Join-Path $current 'config.example.json') -Raw -Encoding UTF8 | ConvertFrom-Json
    $browserCandidates = @(
        $config.browser_executable,
        (Join-Path $env:LOCALAPPDATA 'Google\Chrome\Application\chrome.exe'),
        (Join-Path $env:PROGRAMFILES 'Microsoft\Edge\Application\msedge.exe'),
        (Join-Path ${env:PROGRAMFILES(X86)} 'Microsoft\Edge\Application\msedge.exe')
    )
    $browser = @($browserCandidates | Where-Object { $_ -and (Test-Path -LiteralPath $_ -PathType Leaf) } | Select-Object -First 1)
    if ($browser.Count -eq 0) { throw 'No local Chromium browser is available for the configuration test' }
    $config.browser_executable = [string]$browser[0]
    $config.allow_network = $false
    $config.allowed_origins = @()
    $config.capture_console_text = $false
    Write-Utf8NoBom (Join-Path $TargetRoot 'config.json') (($config | ConvertTo-Json -Depth 20) + "`n")
    Write-Utf8NoBom (Join-Path $current '.remote-computer-use-managed.json') (([ordered]@{
        schema_version = 1
        managed_by = 'remote-computer-use-vm-installer'
        package_version = '0.3.0'
    } | ConvertTo-Json -Depth 5) + "`n")

    $enableOutput = & powershell.exe -NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass `
        -File $ConfigureScript -Enable -TransactionId $TransactionId `
        -RecoveryBaseline 'rcu-baseline-20260827T000000Z' `
        -ApprovalReference 'test-origin-uat-config' -TargetRoot $TargetRoot
    if ($LASTEXITCODE -ne 0) { throw "Origin configuration enable failed: $($enableOutput -join ' ')" }
    $enabled = $enableOutput | Select-Object -Last 1 | ConvertFrom-Json
    $enabledConfig = Get-Content -LiteralPath (Join-Path $TargetRoot 'config.json') -Raw -Encoding UTF8 | ConvertFrom-Json

    $rollbackOutput = & powershell.exe -NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass `
        -File $ConfigureScript -RollbackTransaction $TransactionId -TargetRoot $TargetRoot
    if ($LASTEXITCODE -ne 0) { throw "Origin configuration rollback failed: $($rollbackOutput -join ' ')" }
    $rolledBack = $rollbackOutput | Select-Object -Last 1 | ConvertFrom-Json
    $rolledBackConfig = Get-Content -LiteralPath (Join-Path $TargetRoot 'config.json') -Raw -Encoding UTF8 | ConvertFrom-Json

    $checks = [ordered]@{
        enabled_status = $enabled.status
        enabled_origin = [string]$enabled.origin
        enabled_network = [bool]$enabledConfig.allow_network
        enabled_origin_count = @($enabledConfig.allowed_origins).Count
        enabled_console_capture = [bool]$enabledConfig.capture_console_text
        rollback_status = $rolledBack.status
        rollback_network = [bool]$rolledBackConfig.allow_network
        rollback_origin_count = @($rolledBackConfig.allowed_origins).Count
        receipt_exists = Test-Path -LiteralPath (Join-Path $TransactionRoot 'receipt.json') -PathType Leaf
        rollback_receipt_exists = Test-Path -LiteralPath (Join-Path $TransactionRoot 'rollback.json') -PathType Leaf
    }
    Write-Output ($checks | ConvertTo-Json -Compress)
    if (
        $checks.enabled_status -ne 'ENABLED' -or
        $checks.enabled_origin -ne 'http://127.0.0.1:13102' -or
        -not $checks.enabled_network -or
        $checks.enabled_origin_count -ne 1 -or
        $checks.enabled_console_capture -or
        $checks.rollback_status -ne 'ROLLED_BACK' -or
        $checks.rollback_network -or
        $checks.rollback_origin_count -ne 0 -or
        -not $checks.receipt_exists -or
        -not $checks.rollback_receipt_exists
    ) {
        throw 'Origin UAT configuration transaction evidence is incomplete'
    }
}
finally {
    $allowedRoot = [System.IO.Path]::GetFullPath((Join-Path $env:ProgramData 'MateuszAI')).TrimEnd('\')
    foreach ($path in @($TargetRoot, $TransactionRoot)) {
        $resolved = [System.IO.Path]::GetFullPath($path)
        if (-not $resolved.StartsWith($allowedRoot + '\', [System.StringComparison]::OrdinalIgnoreCase)) {
            throw "Refusing cleanup outside the managed test root: $resolved"
        }
        if (Test-Path -LiteralPath $resolved) {
            Remove-Item -LiteralPath $resolved -Recurse -Force
        }
    }
}
