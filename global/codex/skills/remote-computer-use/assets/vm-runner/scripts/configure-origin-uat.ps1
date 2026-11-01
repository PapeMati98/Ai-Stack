[CmdletBinding(DefaultParameterSetName = 'Enable')]
param(
    [Parameter(ParameterSetName = 'Enable', Mandatory)]
    [switch]$Enable,
    [Parameter(ParameterSetName = 'Enable', Mandatory)]
    [ValidatePattern('^rcu-origin-[0-9]{8}T[0-9]{6}Z-[a-f0-9]{8}$')]
    [string]$TransactionId,
    [Parameter(ParameterSetName = 'Enable', Mandatory)]
    [ValidatePattern('^rcu-baseline-[0-9]{8}T[0-9]{6}Z$')]
    [string]$RecoveryBaseline,
    [Parameter(ParameterSetName = 'Enable', Mandatory)]
    [ValidatePattern('^[A-Za-z0-9._:-]{8,128}$')]
    [string]$ApprovalReference,
    [Parameter(ParameterSetName = 'Rollback', Mandatory)]
    [ValidatePattern('^rcu-origin-[0-9]{8}T[0-9]{6}Z-[a-f0-9]{8}$')]
    [string]$RollbackTransaction,
    [string]$TargetRoot = (Join-Path $env:ProgramData 'MateuszAI\remote-computer-use')
)

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
$Origin = 'http://127.0.0.1:13102'
$Manager = 'remote-computer-use-origin-uat-config'
$AllowedRoot = [System.IO.Path]::GetFullPath((Join-Path $env:ProgramData 'MateuszAI')).TrimEnd('\')
$TargetRoot = [System.IO.Path]::GetFullPath($TargetRoot).TrimEnd('\')
$ConfigPath = Join-Path $TargetRoot 'config.json'
$MarkerPath = Join-Path $TargetRoot 'current\.remote-computer-use-managed.json'
$DoctorScript = Join-Path $TargetRoot 'current\scripts\invoke-run.ps1'
$PolicyDoctor = Join-Path $TargetRoot 'current\src\doctor.mjs'
$LeasePath = Join-Path $TargetRoot 'state\browser.lease'
$StateRoot = Join-Path $AllowedRoot 'remote-computer-use-origin-config'
$TransactionsRoot = Join-Path $StateRoot 'transactions'

function Assert-UnderRoot([string]$Path, [string]$Root, [string]$Label) {
    $full = [System.IO.Path]::GetFullPath($Path)
    $rootFull = [System.IO.Path]::GetFullPath($Root).TrimEnd('\')
    if (-not $full.StartsWith($rootFull + '\', [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "$Label is outside the allowed root: $full"
    }
}

function Write-Utf8NoBom([string]$Path, [string]$Content) {
    New-Item -ItemType Directory -Force -Path (Split-Path -Parent $Path) | Out-Null
    $encoding = New-Object System.Text.UTF8Encoding($false)
    [System.IO.File]::WriteAllText($Path, $Content, $encoding)
}

function Get-ConfigFingerprint([string]$Path) {
    return (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash.ToLowerInvariant()
}

function Assert-ManagedRunner {
    if (
        -not (Test-Path -LiteralPath $ConfigPath -PathType Leaf) -or
        -not (Test-Path -LiteralPath $MarkerPath -PathType Leaf) -or
        -not (Test-Path -LiteralPath $DoctorScript -PathType Leaf) -or
        -not (Test-Path -LiteralPath $PolicyDoctor -PathType Leaf)
    ) {
        throw 'Managed 0.3.0 runner files are missing'
    }
    $marker = Get-Content -LiteralPath $MarkerPath -Raw -Encoding UTF8 | ConvertFrom-Json
    if (
        $marker.managed_by -ne 'remote-computer-use-vm-installer' -or
        $marker.package_version -ne '0.3.0'
    ) {
        throw 'Origin UAT configuration requires the managed 0.3.0 runner'
    }
    $config = Get-Content -LiteralPath $ConfigPath -Raw -Encoding UTF8 | ConvertFrom-Json
    if ($config.schema_version -ne 2 -or $config.runner_version -ne '0.3.0') {
        throw 'Runner configuration is not version 0.3.0'
    }
    if (Test-Path -LiteralPath $LeasePath -PathType Container) {
        throw 'Refusing origin configuration while the runner lease is active'
    }
    $desktopTask = Get-ScheduledTask -TaskName ([string]$config.desktop_task_name) -ErrorAction SilentlyContinue
    if ($desktopTask -and $desktopTask.State -eq 'Running') {
        throw 'Refusing origin configuration while the desktop broker task is running'
    }
    return $config
}

function Assert-PolicyValid([string]$CandidateConfigPath) {
    $node = Get-Command node.exe -ErrorAction Stop
    $output = & $node.Source $PolicyDoctor $CandidateConfigPath 2>$null
    $exitCode = $LASTEXITCODE
    $result = $output | Select-Object -Last 1 | ConvertFrom-Json
    if ($exitCode -ne 0 -or $result.status -ne 'VALID') {
        throw 'Candidate origin UAT configuration failed policy validation'
    }
}

function Replace-Config([string]$CandidatePath, [string]$BackupPath) {
    if (Test-Path -LiteralPath $BackupPath) {
        throw "Configuration replacement backup already exists: $BackupPath"
    }
    [System.IO.File]::Replace($CandidatePath, $ConfigPath, $BackupPath, $true)
}

Assert-UnderRoot $TargetRoot $AllowedRoot 'TargetRoot'
New-Item -ItemType Directory -Force -Path $TransactionsRoot | Out-Null

if ($PSCmdlet.ParameterSetName -eq 'Rollback') {
    $transactionRoot = Join-Path $TransactionsRoot $RollbackTransaction
    Assert-UnderRoot $transactionRoot $TransactionsRoot 'transaction root'
    $receiptPath = Join-Path $transactionRoot 'receipt.json'
    if (-not (Test-Path -LiteralPath $receiptPath -PathType Leaf)) {
        throw "Origin UAT configuration receipt is missing: $receiptPath"
    }
    if (Test-Path -LiteralPath (Join-Path $transactionRoot 'rollback.json') -PathType Leaf) {
        throw 'This origin UAT configuration transaction was already rolled back'
    }
    $receipt = Get-Content -LiteralPath $receiptPath -Raw -Encoding UTF8 | ConvertFrom-Json
    if ($receipt.status -ne 'ENABLED' -or $receipt.transaction_id -ne $RollbackTransaction) {
        throw 'Only the matching ENABLED origin UAT transaction can be rolled back'
    }
    [void](Assert-ManagedRunner)
    if ((Get-ConfigFingerprint $ConfigPath) -ne [string]$receipt.config_fingerprint_after) {
        throw 'Refusing rollback because the active configuration drifted'
    }
    $priorConfig = Join-Path $transactionRoot 'prior-config.json'
    if (
        -not (Test-Path -LiteralPath $priorConfig -PathType Leaf) -or
        (Get-ConfigFingerprint $priorConfig) -ne [string]$receipt.config_fingerprint_before
    ) {
        throw 'Refusing rollback because the prior configuration backup is missing or drifted'
    }
    $candidate = Join-Path $transactionRoot 'rollback-candidate.json'
    Copy-Item -LiteralPath $priorConfig -Destination $candidate
    Assert-PolicyValid $candidate
    Replace-Config $candidate (Join-Path $transactionRoot 'enabled-config-at-rollback.json')
    $doctorOutput = & powershell.exe -NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass `
        -File $DoctorScript -Action doctor -RequireMode browser
    $doctor = $doctorOutput | Select-Object -Last 1 | ConvertFrom-Json
    if (
        $doctor.status -ne 'READY' -or
        [bool]$doctor.allow_network -or
        [int]$doctor.allowed_origin_count -ne 0
    ) {
        throw 'Runner did not return to the offline baseline after rollback'
    }
    $rollback = [ordered]@{
        schema_version = 1
        action = 'rollback-origin-uat'
        status = 'ROLLED_BACK'
        transaction_id = $RollbackTransaction
        config_fingerprint = Get-ConfigFingerprint $ConfigPath
        allow_network = $false
        allowed_origin_count = 0
        rolled_back_at = [DateTime]::UtcNow.ToString('o')
    }
    Write-Utf8NoBom (Join-Path $transactionRoot 'rollback.json') (($rollback | ConvertTo-Json -Depth 8) + "`n")
    Write-Output ($rollback | ConvertTo-Json -Depth 8 -Compress)
    exit 0
}

$transactionRoot = Join-Path $TransactionsRoot $TransactionId
Assert-UnderRoot $transactionRoot $TransactionsRoot 'transaction root'
if (Test-Path -LiteralPath $transactionRoot) {
    throw "Origin UAT configuration transaction already exists: $TransactionId"
}
New-Item -ItemType Directory -Force -Path $transactionRoot | Out-Null
$priorConfigPath = Join-Path $transactionRoot 'prior-config.json'

try {
    $config = Assert-ManagedRunner
    if (
        [bool]$config.allow_network -or
        @($config.allowed_origins).Count -ne 0 -or
        [bool]$config.capture_console_text
    ) {
        throw 'Origin UAT enablement requires the offline zero-origin baseline'
    }
    Copy-Item -LiteralPath $ConfigPath -Destination $priorConfigPath
    $before = Get-ConfigFingerprint $priorConfigPath
    $config.allow_network = $true
    $config.allowed_origins = @($Origin)
    $config.capture_console_text = $false
    $candidate = Join-Path $transactionRoot 'candidate-config.json'
    Write-Utf8NoBom $candidate (($config | ConvertTo-Json -Depth 20) + "`n")
    Assert-PolicyValid $candidate
    Replace-Config $candidate (Join-Path $transactionRoot 'offline-config-at-enable.json')
    $after = Get-ConfigFingerprint $ConfigPath

    $doctorOutput = & powershell.exe -NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass `
        -File $DoctorScript -Action doctor -RequireMode browser
    $doctor = $doctorOutput | Select-Object -Last 1 | ConvertFrom-Json
    if (
        $doctor.status -ne 'READY' -or
        -not [bool]$doctor.allow_network -or
        [int]$doctor.allowed_origin_count -ne 1 -or
        [bool]$config.capture_console_text
    ) {
        throw 'Runner doctor did not verify the exact-origin UAT configuration'
    }

    $receipt = [ordered]@{
        schema_version = 1
        action = 'enable-origin-uat'
        status = 'ENABLED'
        managed_by = $Manager
        transaction_id = $TransactionId
        recovery_baseline = $RecoveryBaseline
        approval_reference = $ApprovalReference
        origin = $Origin
        config_fingerprint_before = $before
        config_fingerprint_after = $after
        doctor_status = $doctor.status
        allowed_origin_count = [int]$doctor.allowed_origin_count
        capture_console_text = $false
        enabled_at = [DateTime]::UtcNow.ToString('o')
    }
    Write-Utf8NoBom (Join-Path $transactionRoot 'receipt.json') (($receipt | ConvertTo-Json -Depth 8) + "`n")
    Write-Output ($receipt | ConvertTo-Json -Depth 8 -Compress)
}
catch {
    $originalError = $_.Exception.Message
    $rollbackComplete = $true
    $rollbackError = $null
    try {
        if (Test-Path -LiteralPath $priorConfigPath -PathType Leaf) {
            Copy-Item -LiteralPath $priorConfigPath -Destination $ConfigPath -Force
        }
    }
    catch {
        $rollbackComplete = $false
        $rollbackError = $_.Exception.Message
    }
    $failure = [ordered]@{
        schema_version = 1
        action = 'enable-origin-uat'
        status = $(if ($rollbackComplete) { 'FAILED_ROLLED_BACK' } else { 'FAILED_ROLLBACK_INCOMPLETE' })
        transaction_id = $TransactionId
        error = $originalError
        rollback_complete = $rollbackComplete
        rollback_error = $rollbackError
        failed_at = [DateTime]::UtcNow.ToString('o')
    }
    Write-Utf8NoBom (Join-Path $transactionRoot 'failure.json') (($failure | ConvertTo-Json -Depth 8) + "`n")
    Write-Output ($failure | ConvertTo-Json -Depth 8 -Compress)
    exit $(if ($rollbackComplete) { 1 } else { 2 })
}
