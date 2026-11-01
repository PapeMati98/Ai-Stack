[CmdletBinding(DefaultParameterSetName = 'Deploy')]
param(
    [Parameter(ParameterSetName = 'Verify', Mandatory)]
    [switch]$Verify,
    [Parameter(ParameterSetName = 'Rollback', Mandatory)]
    [ValidatePattern('^rcu-skill-[0-9]{8}T[0-9]{6}Z-[a-f0-9]{8}$')]
    [string]$RollbackTransaction,
    [Parameter(ParameterSetName = 'Deploy')]
    [switch]$DryRun,
    [ValidateSet('codex', 'claude', 'shared', 'antigravity', 'grok', 'kimi-desktop')]
    [string[]]$Tool = @('codex', 'claude', 'shared', 'antigravity', 'grok', 'kimi-desktop')
)

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
$SkillName = 'remote-computer-use'
$SkillRoot = Split-Path -Parent $PSScriptRoot
$StateRoot = Join-Path $env:USERPROFILE '.agents\remote-computer-use-skill-deploy'
$TransactionsRoot = Join-Path $StateRoot 'transactions'
$AppDataRoot = if ($env:APPDATA) { $env:APPDATA } else { Join-Path $env:USERPROFILE 'AppData\Roaming' }
$TargetRoots = [ordered]@{
    codex = Join-Path $env:USERPROFILE '.codex\skills'
    claude = Join-Path $env:USERPROFILE '.claude\skills'
    shared = Join-Path $env:USERPROFILE '.agents\skills'
    antigravity = Join-Path $env:USERPROFILE '.gemini\config\skills'
    grok = Join-Path $env:USERPROFILE '.grok\skills'
    'kimi-desktop' = Join-Path $AppDataRoot 'kimi-desktop\daimon-share\daimon\skills'
}

function Write-Utf8NoBom([string]$Path, [string]$Content) {
    New-Item -ItemType Directory -Force -Path (Split-Path -Parent $Path) | Out-Null
    $encoding = New-Object System.Text.UTF8Encoding($false)
    [System.IO.File]::WriteAllText($Path, $Content, $encoding)
}

function Get-PayloadFiles([string]$Root) {
    $resolved = (Get-Item -LiteralPath $Root).FullName.TrimEnd('\')
    return @(
        Get-ChildItem -LiteralPath $resolved -File -Recurse -Force |
            Where-Object {
                $_.FullName -notmatch '[\\/]node_modules[\\/]' -and
                $_.FullName -notmatch '[\\/]__pycache__[\\/]' -and
                $_.Extension -ne '.pyc'
            } |
            Sort-Object FullName
    )
}

function Get-PayloadRows([string]$Root) {
    $resolved = (Get-Item -LiteralPath $Root).FullName.TrimEnd('\')
    $rows = @()
    foreach ($file in Get-PayloadFiles $resolved) {
        $relative = $file.FullName.Substring($resolved.Length).TrimStart('\').Replace('\', '/')
        $hash = (Get-FileHash -LiteralPath $file.FullName -Algorithm SHA256).Hash.ToLowerInvariant()
        $rows += "$relative|$($file.Length)|$hash"
    }
    return @($rows)
}

function Get-PayloadFingerprint([string]$Root) {
    $joined = (Get-PayloadRows $Root) -join "`n"
    $bytes = [System.Text.Encoding]::UTF8.GetBytes($joined)
    $sha = [System.Security.Cryptography.SHA256]::Create()
    try {
        return ([BitConverter]::ToString($sha.ComputeHash($bytes))).Replace('-', '').ToLowerInvariant()
    }
    finally {
        $sha.Dispose()
    }
}

function Copy-Payload([string]$Source, [string]$Destination) {
    if (Test-Path -LiteralPath $Destination) {
        throw "Destination already exists: $Destination"
    }
    New-Item -ItemType Directory -Force -Path $Destination | Out-Null
    $sourceRoot = (Get-Item -LiteralPath $Source).FullName.TrimEnd('\')
    foreach ($file in Get-PayloadFiles $sourceRoot) {
        $relative = $file.FullName.Substring($sourceRoot.Length).TrimStart('\')
        $target = Join-Path $Destination $relative
        New-Item -ItemType Directory -Force -Path (Split-Path -Parent $target) | Out-Null
        Copy-Item -LiteralPath $file.FullName -Destination $target -Force
    }
}

function Get-Reversed($Items) {
    $copy = @($Items)
    [array]::Reverse($copy)
    return $copy
}

function Get-TargetRecords([string[]]$SelectedTools, [string]$ExpectedFingerprint) {
    $records = @()
    foreach ($name in $SelectedTools) {
        $target = Join-Path $TargetRoots[$name] $SkillName
        $exists = Test-Path -LiteralPath $target -PathType Container
        $actual = $(if ($exists) { Get-PayloadFingerprint $target } else { $null })
        $records += [ordered]@{
            tool = $name
            target = $target
            exists = [bool]$exists
            payload_fingerprint = $actual
            status = $(if ($exists -and $actual -eq $ExpectedFingerprint) { 'CURRENT' } elseif ($exists) { 'DRIFT' } else { 'ABSENT' })
        }
    }
    return @($records)
}

if (-not (Test-Path -LiteralPath (Join-Path $SkillRoot 'SKILL.md') -PathType Leaf)) {
    throw "Invalid skill source: $SkillRoot"
}
if (@(Get-ChildItem -LiteralPath $SkillRoot -File -Force | Where-Object {
    $_.Name -like '*-managed.json' -or $_.Name -like '*-adapter.json'
}).Count -gt 0) {
    throw 'Bootstrap-owned source must not contain a dedicated-installer marker'
}

$sourceFingerprint = Get-PayloadFingerprint $SkillRoot
$sourceFileCount = @(Get-PayloadRows $SkillRoot).Count
$selectedTools = @($Tool | Select-Object -Unique)

if ($PSCmdlet.ParameterSetName -eq 'Verify') {
    $records = Get-TargetRecords $selectedTools $sourceFingerprint
    $status = $(if (@($records | Where-Object status -ne 'CURRENT').Count -eq 0) { 'PASS' } else { 'FAIL' })
    [ordered]@{
        action = 'verify'
        status = $status
        skill = $SkillName
        source_fingerprint = $sourceFingerprint
        source_file_count = $sourceFileCount
        targets = $records
        writes_performed = 0
    } | ConvertTo-Json -Depth 8
    exit $(if ($status -eq 'PASS') { 0 } else { 3 })
}

if ($PSCmdlet.ParameterSetName -eq 'Rollback') {
    $transactionRoot = Join-Path $TransactionsRoot $RollbackTransaction
    $receiptPath = Join-Path $transactionRoot 'receipt.json'
    if (-not (Test-Path -LiteralPath $receiptPath -PathType Leaf)) {
        throw "Rollback receipt not found: $receiptPath"
    }
    if (Test-Path -LiteralPath (Join-Path $transactionRoot 'rollback.json') -PathType Leaf) {
        throw 'This skill transaction has already been rolled back'
    }
    $receipt = Get-Content -LiteralPath $receiptPath -Raw -Encoding UTF8 | ConvertFrom-Json
    if ($receipt.status -ne 'DEPLOYED' -or $receipt.transaction_id -ne $RollbackTransaction) {
        throw 'Only the matching DEPLOYED transaction can be rolled back'
    }

    foreach ($record in @($receipt.targets)) {
        if (-not (Test-Path -LiteralPath $record.target -PathType Container)) {
            throw "Refusing rollback because the deployed target is absent: $($record.target)"
        }
        if ((Get-PayloadFingerprint $record.target) -ne $receipt.payload_fingerprint) {
            throw "Refusing rollback because the deployed target drifted: $($record.target)"
        }
    }

    $preservedRoot = Join-Path $transactionRoot 'deployed-state-at-rollback'
    $completed = @()
    try {
        New-Item -ItemType Directory -Force -Path $preservedRoot | Out-Null
        foreach ($record in @($receipt.targets)) {
            $preserved = Join-Path $preservedRoot $record.tool
            Move-Item -LiteralPath $record.target -Destination $preserved
            $completed += $record
            if ([bool]$record.target_existed_before) {
                $prior = Join-Path (Join-Path $transactionRoot 'prior') $record.tool
                if (-not (Test-Path -LiteralPath $prior -PathType Container)) {
                    throw "Prior target backup is missing: $prior"
                }
                Move-Item -LiteralPath $prior -Destination $record.target
                if ((Get-PayloadFingerprint $record.target) -ne $record.prior_fingerprint) {
                    throw "Restored target fingerprint mismatch: $($record.target)"
                }
            }
            elseif (Test-Path -LiteralPath $record.target) {
                throw "Exact-absence rollback failed: $($record.target)"
            }
        }
    }
    catch {
        foreach ($record in (Get-Reversed $completed)) {
            if (Test-Path -LiteralPath $record.target) {
                $prior = Join-Path (Join-Path $transactionRoot 'prior') $record.tool
                Move-Item -LiteralPath $record.target -Destination $prior
            }
            $preserved = Join-Path $preservedRoot $record.tool
            if (Test-Path -LiteralPath $preserved) {
                Move-Item -LiteralPath $preserved -Destination $record.target
            }
        }
        throw
    }

    $rollback = [ordered]@{
        action = 'rollback'
        status = 'ROLLED_BACK'
        skill = $SkillName
        transaction_id = $RollbackTransaction
        target_count = @($receipt.targets).Count
        rolled_back_at = [DateTime]::UtcNow.ToString('o')
    }
    Write-Utf8NoBom (Join-Path $transactionRoot 'rollback.json') (($rollback | ConvertTo-Json -Depth 6) + "`n")
    Write-Output ($rollback | ConvertTo-Json -Depth 6 -Compress)
    exit 0
}

$transactionId = 'rcu-skill-' + [DateTime]::UtcNow.ToString('yyyyMMddTHHmmssZ') + '-' + [Guid]::NewGuid().ToString('N').Substring(0, 8)
$current = Get-TargetRecords $selectedTools $sourceFingerprint
if ($DryRun) {
    [ordered]@{
        action = 'deploy'
        status = 'DRY_RUN'
        skill = $SkillName
        transaction_id = $transactionId
        payload_fingerprint = $sourceFingerprint
        source_file_count = $sourceFileCount
        targets = $current
        writes_performed = 0
    } | ConvertTo-Json -Depth 8
    exit 0
}

$transactionRoot = Join-Path $TransactionsRoot $transactionId
if (Test-Path -LiteralPath $transactionRoot) {
    throw "Transaction already exists: $transactionId"
}
New-Item -ItemType Directory -Force -Path $transactionRoot | Out-Null
$records = @()
$deployed = @()
try {
    foreach ($name in $selectedTools) {
        $targetRoot = $TargetRoots[$name]
        $target = Join-Path $targetRoot $SkillName
        $staged = Join-Path (Join-Path $transactionRoot 'staged') $name
        Copy-Payload $SkillRoot $staged
        if ((Get-PayloadFingerprint $staged) -ne $sourceFingerprint) {
            throw "Staged payload fingerprint mismatch for $name"
        }

        $targetExisted = Test-Path -LiteralPath $target -PathType Container
        $priorFingerprint = $(if ($targetExisted) { Get-PayloadFingerprint $target } else { $null })
        $record = [ordered]@{
            tool = $name
            target = $target
            target_existed_before = [bool]$targetExisted
            prior_fingerprint = $priorFingerprint
        }
        $deployed += $record
        if ($targetExisted) {
            $prior = Join-Path (Join-Path $transactionRoot 'prior') $name
            New-Item -ItemType Directory -Force -Path (Split-Path -Parent $prior) | Out-Null
            Move-Item -LiteralPath $target -Destination $prior
        }
        New-Item -ItemType Directory -Force -Path $targetRoot | Out-Null
        Move-Item -LiteralPath $staged -Destination $target
        if ((Get-PayloadFingerprint $target) -ne $sourceFingerprint) {
            throw "Deployed payload fingerprint mismatch for $name"
        }
        $records += $record
    }

    $receipt = [ordered]@{
        action = 'deploy'
        status = 'DEPLOYED'
        skill = $SkillName
        transaction_id = $transactionId
        payload_fingerprint = $sourceFingerprint
        source_file_count = $sourceFileCount
        targets = $records
        deployed_at = [DateTime]::UtcNow.ToString('o')
    }
    Write-Utf8NoBom (Join-Path $transactionRoot 'receipt.json') (($receipt | ConvertTo-Json -Depth 8) + "`n")
    Write-Output ($receipt | ConvertTo-Json -Depth 8 -Compress)
}
catch {
    foreach ($record in (Get-Reversed $deployed)) {
        if (Test-Path -LiteralPath $record.target) {
            Remove-Item -LiteralPath $record.target -Recurse -Force
        }
        if ([bool]$record.target_existed_before) {
            $prior = Join-Path (Join-Path $transactionRoot 'prior') $record.tool
            if (Test-Path -LiteralPath $prior) {
                Move-Item -LiteralPath $prior -Destination $record.target
            }
        }
    }
    $failure = [ordered]@{
        action = 'deploy'
        status = 'FAILED_ROLLED_BACK'
        skill = $SkillName
        transaction_id = $transactionId
        error = $_.Exception.Message
        failed_at = [DateTime]::UtcNow.ToString('o')
    }
    Write-Utf8NoBom (Join-Path $transactionRoot 'failure.json') (($failure | ConvertTo-Json -Depth 6) + "`n")
    Write-Output ($failure | ConvertTo-Json -Depth 6 -Compress)
    exit 1
}
