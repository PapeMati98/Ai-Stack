[CmdletBinding(DefaultParameterSetName = 'Deploy')]
param(
    [Parameter(ParameterSetName = 'Rollback', Mandatory)]
    [ValidatePattern('^rcu-[0-9]{8}T[0-9]{6}Z-[a-f0-9]{8}$')]
    [string]$RollbackTransaction,
    [ValidateSet('ai-lab-uat-vm', 'ai-lab-windows-vm')]
    [string]$HostAlias = 'ai-lab-uat-vm',
    [Parameter(ParameterSetName = 'Deploy')]
    [switch]$AllowEnabledDesktopUpgrade,
    [Parameter(ParameterSetName = 'Deploy')]
    [switch]$ApplyTemplateConfiguration,
    [switch]$DryRun
)

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
$SkillRoot = Split-Path -Parent $PSScriptRoot
. (Join-Path $PSScriptRoot 'remote_staging.ps1')
$RunnerSource = Join-Path $SkillRoot 'assets\vm-runner'
$RemoteInstaller = Join-Path $RunnerSource 'scripts\install-vm.ps1'
$PackageVersion = '0.3.0'
$Manager = 'remote-computer-use-vm-installer'
$LocalStateRoot = Join-Path $env:USERPROFILE '.agents\remote-computer-use-vm-deploy'

function Write-Utf8NoBom([string]$Path, [string]$Content) {
    New-Item -ItemType Directory -Force -Path (Split-Path -Parent $Path) | Out-Null
    $encoding = New-Object System.Text.UTF8Encoding($false)
    [System.IO.File]::WriteAllText($Path, $Content, $encoding)
}

function Get-PayloadRows([string]$Root) {
    $resolved = (Get-Item -LiteralPath $Root).FullName.TrimEnd('\')
    $rows = @()
    foreach ($file in Get-ChildItem -LiteralPath $resolved -File -Recurse -Force) {
        $relative = $file.FullName.Substring($resolved.Length).TrimStart('\').Replace('\', '/')
        if ($relative -eq 'release.manifest.json' -or $relative -like 'node_modules/*') { continue }
        $hash = (Get-FileHash -LiteralPath $file.FullName -Algorithm SHA256).Hash.ToLowerInvariant()
        $rows += "$relative|$($file.Length)|$hash"
    }
    $ordered = [string[]]@($rows)
    [Array]::Sort($ordered, [System.StringComparer]::Ordinal)
    return @($ordered)
}

function Get-PayloadFingerprint([string]$Root) {
    $joined = (Get-PayloadRows $Root) -join "`n"
    $bytes = [System.Text.Encoding]::UTF8.GetBytes($joined)
    $sha = [System.Security.Cryptography.SHA256]::Create()
    try { return ([BitConverter]::ToString($sha.ComputeHash($bytes))).Replace('-', '').ToLowerInvariant() }
    finally { $sha.Dispose() }
}

function Invoke-RemoteInstaller([string[]]$Arguments, [string]$TransactionId, [string]$StagingRoot) {
    if ([string]::IsNullOrWhiteSpace($StagingRoot)) {
        $StagingRoot = Get-RemoteStagingRoot -HostAlias $HostAlias
    }
    $remoteInstallerPath = Join-RemoteStagingPath $StagingRoot "rcu-installer-$TransactionId.ps1"
    try {
        & scp.exe -q -- $RemoteInstaller "${HostAlias}:$remoteInstallerPath"
        if ($null -eq $LASTEXITCODE -or $LASTEXITCODE -ne 0) { throw 'Failed to copy the remote installer' }
        $invocation = Invoke-RemoteStagedScript -HostAlias $HostAlias -ScriptPath $remoteInstallerPath -Arguments $Arguments
        if ($invocation.ExitCode -ne 0) {
            throw "Remote installer failed: $($invocation.Output -join ' ')"
        }
        return [string]($invocation.Output | Select-Object -Last 1)
    }
    finally {
        Remove-RemoteStagedFile -HostAlias $HostAlias -ScriptPath $remoteInstallerPath
    }
}

if (-not (Test-Path -LiteralPath $RunnerSource -PathType Container)) {
    throw "Runner source is missing: $RunnerSource"
}
if (-not (Test-Path -LiteralPath $RemoteInstaller -PathType Leaf)) {
    throw "Remote installer is missing: $RemoteInstaller"
}

if ($PSCmdlet.ParameterSetName -eq 'Rollback') {
    if ($DryRun) {
        [ordered]@{
            action = 'rollback'
            status = 'DRY_RUN'
            host_alias = $HostAlias
            transaction_id = $RollbackTransaction
            writes_performed = 0
        } | ConvertTo-Json -Depth 4
        exit 0
    }
    $json = Invoke-RemoteInstaller -Arguments @('-RollbackTransaction', $RollbackTransaction) `
        -TransactionId $RollbackTransaction
    $result = $json | ConvertFrom-Json
    if ($result.status -ne 'ROLLED_BACK' -or $result.transaction_id -ne $RollbackTransaction) {
        throw 'Remote installer rollback receipt does not match the request'
    }
    $localTransaction = Join-Path (Join-Path $LocalStateRoot 'transactions') $RollbackTransaction
    Write-Utf8NoBom (Join-Path $localTransaction 'rollback.json') (($result | ConvertTo-Json -Depth 8) + "`n")
    Write-Output $json
    exit 0
}

$transactionId = 'rcu-' + [DateTime]::UtcNow.ToString('yyyyMMddTHHmmssZ') + '-' + [Guid]::NewGuid().ToString('N').Substring(0, 8)
$temporaryRoot = Join-Path $env:TEMP $transactionId
$packageRoot = Join-Path $temporaryRoot 'package'
$packageZip = Join-Path $temporaryRoot 'remote-computer-use-vm-runner.zip'
$localTransaction = Join-Path (Join-Path $LocalStateRoot 'transactions') $transactionId

if ($DryRun) {
    $fingerprint = Get-PayloadFingerprint $RunnerSource
    [ordered]@{
        action = 'deploy'
        status = 'DRY_RUN'
        host_alias = $HostAlias
        transaction_id = $transactionId
        package_version = $PackageVersion
        payload_fingerprint = $fingerprint
        file_count = @(Get-PayloadRows $RunnerSource).Count
        writes_performed = 0
    } | ConvertTo-Json -Depth 6
    exit 0
}

try {
    New-Item -ItemType Directory -Force -Path $packageRoot, $localTransaction | Out-Null
    Copy-Item -Path (Join-Path $RunnerSource '*') -Destination $packageRoot -Recurse -Force
    $fingerprint = Get-PayloadFingerprint $packageRoot
    $manifest = [ordered]@{
        schema_version = 1
        managed_by = $Manager
        package_version = $PackageVersion
        payload_fingerprint = $fingerprint
        file_count = @(Get-PayloadRows $packageRoot).Count
    }
    Write-Utf8NoBom (Join-Path $packageRoot 'release.manifest.json') (($manifest | ConvertTo-Json -Depth 6) + "`n")

    Compress-Archive -Path (Join-Path $packageRoot '*') -DestinationPath $packageZip -CompressionLevel Optimal
    $stagingRoot = Get-RemoteStagingRoot -HostAlias $HostAlias
    $remotePackagePath = Join-RemoteStagingPath $stagingRoot ($transactionId + '.zip')
    try {
        & scp.exe -q -- $packageZip "${HostAlias}:$remotePackagePath"
        if ($null -eq $LASTEXITCODE -or $LASTEXITCODE -ne 0) { throw 'Failed to copy the runner package' }
        $remoteArguments = @('-PackageZip', $remotePackagePath, '-TransactionId', $transactionId)
        if ($AllowEnabledDesktopUpgrade) { $remoteArguments += '-AllowEnabledDesktopUpgrade' }
        if ($ApplyTemplateConfiguration) { $remoteArguments += '-ApplyTemplateConfiguration' }
        $json = Invoke-RemoteInstaller `
            -Arguments $remoteArguments `
            -TransactionId $transactionId `
            -StagingRoot $stagingRoot
    }
    finally {
        Remove-RemoteStagedFile -HostAlias $HostAlias -ScriptPath $remotePackagePath
    }
    $receipt = $json | ConvertFrom-Json
    if ($receipt.payload_fingerprint -ne $fingerprint -or $receipt.status -ne 'DEPLOYED' -or
        $receipt.transaction_id -ne $transactionId) {
        throw 'Remote receipt does not match the local package or transaction'
    }
    Write-Utf8NoBom (Join-Path $localTransaction 'receipt.json') (($receipt | ConvertTo-Json -Depth 8) + "`n")
    Write-Output $json
}
finally {
    if (Test-Path -LiteralPath $temporaryRoot) {
        Remove-Item -LiteralPath $temporaryRoot -Recurse -Force
    }
}
