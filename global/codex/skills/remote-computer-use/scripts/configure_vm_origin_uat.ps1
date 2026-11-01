[CmdletBinding(DefaultParameterSetName = 'Enable')]
param(
    [Parameter(ParameterSetName = 'Enable', Mandatory)]
    [switch]$Enable,
    [Parameter(ParameterSetName = 'Enable', Mandatory)]
    [ValidatePattern('^rcu-baseline-[0-9]{8}T[0-9]{6}Z$')]
    [string]$RecoveryBaseline,
    [Parameter(ParameterSetName = 'Enable', Mandatory)]
    [ValidatePattern('^[A-Za-z0-9._:-]{8,128}$')]
    [string]$ApprovalReference,
    [Parameter(ParameterSetName = 'Rollback', Mandatory)]
    [ValidatePattern('^rcu-origin-[0-9]{8}T[0-9]{6}Z-[a-f0-9]{8}$')]
    [string]$RollbackTransaction,
    [ValidateSet('ai-lab-uat-vm')]
    [string]$HostAlias = 'ai-lab-uat-vm',
    [ValidateSet('lab-server')]
    [string]$HypervisorAlias = 'lab-server',
    [ValidatePattern('^win11-uat$')]
    [string]$HypervisorDomain = 'win11-uat',
    [switch]$DryRun
)

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
$SkillRoot = Split-Path -Parent $PSScriptRoot
. (Join-Path $PSScriptRoot 'remote_staging.ps1')
$RemoteScript = Join-Path $SkillRoot 'assets\vm-runner\scripts\configure-origin-uat.ps1'
$LocalStateRoot = Join-Path $env:USERPROFILE '.agents\remote-computer-use-origin-config'

function Write-Utf8NoBom([string]$Path, [string]$Content) {
    New-Item -ItemType Directory -Force -Path (Split-Path -Parent $Path) | Out-Null
    $encoding = New-Object System.Text.UTF8Encoding($false)
    [System.IO.File]::WriteAllText($Path, $Content, $encoding)
}

function Invoke-RemoteConfig([string[]]$Arguments, [string]$Identity) {
    $remotePath = Join-RemoteStagingPath (Get-RemoteStagingRoot -HostAlias $HostAlias) "rcu-origin-$Identity.ps1"
    try {
        & scp.exe -q -- $RemoteScript "${HostAlias}:$remotePath"
        if ($null -eq $LASTEXITCODE -or $LASTEXITCODE -ne 0) { throw 'Failed to copy origin UAT configuration script' }
        $invocation = Invoke-RemoteStagedScript -HostAlias $HostAlias -ScriptPath $remotePath -Arguments $Arguments
        if ($invocation.ExitCode -ne 0) { throw "Remote origin UAT configuration failed: $($invocation.Output -join ' ')" }
        return [string]($invocation.Output | Select-Object -Last 1)
    }
    finally {
        Remove-RemoteStagedFile -HostAlias $HostAlias -ScriptPath $remotePath
    }
}

if (-not (Test-Path -LiteralPath $RemoteScript -PathType Leaf)) {
    throw "Remote origin UAT configuration script is missing: $RemoteScript"
}

if ($PSCmdlet.ParameterSetName -eq 'Rollback') {
    if ($DryRun) {
        [ordered]@{
            action = 'rollback-origin-uat'
            status = 'DRY_RUN'
            transaction_id = $RollbackTransaction
            writes_performed = 0
        } | ConvertTo-Json -Depth 4
        exit 0
    }
    $json = Invoke-RemoteConfig @('-RollbackTransaction', $RollbackTransaction) $RollbackTransaction
    $result = $json | ConvertFrom-Json
    if ($result.status -ne 'ROLLED_BACK' -or $result.transaction_id -ne $RollbackTransaction) {
        throw 'Remote origin UAT rollback receipt does not match the request'
    }
    $localTransaction = Join-Path (Join-Path $LocalStateRoot 'transactions') $RollbackTransaction
    Write-Utf8NoBom (Join-Path $localTransaction 'rollback.json') (($result | ConvertTo-Json -Depth 8) + "`n")
    Write-Output $json
    exit 0
}

$transactionId = 'rcu-origin-' + [DateTime]::UtcNow.ToString('yyyyMMddTHHmmssZ') + '-' + [Guid]::NewGuid().ToString('N').Substring(0, 8)
$snapshotOutput = & ssh.exe -o BatchMode=yes -o ConnectTimeout=15 $HypervisorAlias `
    virsh snapshot-info $HypervisorDomain $RecoveryBaseline 2>&1
$snapshotExit = $LASTEXITCODE
$snapshotVerified = [bool]($snapshotExit -eq 0 -and ($snapshotOutput -join "`n") -match [regex]::Escape($RecoveryBaseline))
if (-not $snapshotVerified) {
    throw "Recovery checkpoint was not verified on ${HypervisorDomain}: $RecoveryBaseline"
}

if ($DryRun) {
    [ordered]@{
        action = 'enable-origin-uat'
        status = 'DRY_RUN'
        transaction_id = $transactionId
        host_alias = $HostAlias
        hypervisor_domain = $HypervisorDomain
        recovery_baseline = $RecoveryBaseline
        recovery_baseline_verified = $true
        origin = 'http://127.0.0.1:13102'
        writes_performed = 0
    } | ConvertTo-Json -Depth 6
    exit 0
}

$json = Invoke-RemoteConfig @(
    '-Enable',
    '-TransactionId', $transactionId,
    '-RecoveryBaseline', $RecoveryBaseline,
    '-ApprovalReference', $ApprovalReference
) $transactionId
$receipt = $json | ConvertFrom-Json
if ($receipt.status -ne 'ENABLED' -or $receipt.transaction_id -ne $transactionId) {
    throw 'Remote origin UAT configuration receipt does not match the request'
}
$localTransaction = Join-Path (Join-Path $LocalStateRoot 'transactions') $transactionId
Write-Utf8NoBom (Join-Path $localTransaction 'receipt.json') (($receipt | ConvertTo-Json -Depth 8) + "`n")
Write-Output $json
