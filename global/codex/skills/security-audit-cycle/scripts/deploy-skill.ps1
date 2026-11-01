param(
    [string]$BackupRoot = "$env:USERPROFILE\.agents\security-audits\_deploy-backups",
    [string]$RollbackFrom,
    [switch]$SkipRules
)

$ErrorActionPreference = "Stop"
$SkillRoot = Split-Path -Parent $PSScriptRoot
$SkillName = Split-Path -Leaf $SkillRoot
$CodexGlobalRoot = Split-Path -Parent (Split-Path -Parent $SkillRoot)
$AgentStackGlobalRoot = Split-Path -Parent $CodexGlobalRoot
$RulesSource = Join-Path $CodexGlobalRoot "AGENTS.md"
$ClaudeRulesSource = Join-Path $AgentStackGlobalRoot "claude\CLAUDE.md"
$UserRoot = [System.IO.Path]::GetFullPath($env:USERPROFILE).TrimEnd('\') + '\'

$SkillDestinations = [ordered]@{
    codex = "$env:USERPROFILE\.codex\skills\$SkillName"
    claude = "$env:USERPROFILE\.claude\skills\$SkillName"
    agents = "$env:USERPROFILE\.agents\skills\$SkillName"
    antigravity = "$env:USERPROFILE\.gemini\config\skills\$SkillName"
    grok = "$env:USERPROFILE\.grok\skills\$SkillName"
}

$RuleDestinations = [ordered]@{
    codex = [ordered]@{
        source = $RulesSource
        target = "$env:USERPROFILE\.codex\AGENTS.md"
        marker = $null
    }
    claude = [ordered]@{
        source = $ClaudeRulesSource
        target = "$env:USERPROFILE\.claude\CLAUDE.md"
        marker = $null
    }
    agents = [ordered]@{
        source = $RulesSource
        target = "$env:USERPROFILE\.agents\AGENTS.md"
        marker = "$env:USERPROFILE\.agents\AGENTS.md.agent-stack-managed.json"
    }
    antigravity = [ordered]@{
        source = $RulesSource
        target = "$env:USERPROFILE\.gemini\GEMINI.md"
        marker = "$env:USERPROFILE\.gemini\GEMINI.md.agent-stack-managed.json"
    }
    grok = [ordered]@{
        source = $RulesSource
        target = "$env:USERPROFILE\.grok\AGENTS.md"
        marker = "$env:USERPROFILE\.grok\AGENTS.md.agent-stack-managed.json"
    }
}
$ExistingCodexRuleHash = if (Test-Path -LiteralPath $RuleDestinations.codex.target) {
    (Get-FileHash -LiteralPath $RuleDestinations.codex.target -Algorithm SHA256).Hash
}
else {
    $null
}

function Assert-SafeUserTarget([string]$Path) {
    $FullPath = [System.IO.Path]::GetFullPath($Path)
    if (-not $FullPath.StartsWith($UserRoot, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "Unsafe deployment target outside user profile: $FullPath"
    }
}

function Write-Utf8NoBom([string]$Path, [string]$Content) {
    New-Item -ItemType Directory -Force -Path (Split-Path $Path) | Out-Null
    $Encoding = New-Object System.Text.UTF8Encoding($false)
    [System.IO.File]::WriteAllText($Path, $Content, $Encoding)
}

function Copy-FileWithParent([string]$Source, [string]$Destination) {
    New-Item -ItemType Directory -Force -Path (Split-Path $Destination) | Out-Null
    Copy-Item -LiteralPath $Source -Destination $Destination -Force
}

if ($RollbackFrom) {
    $ResolvedBackup = (Resolve-Path -LiteralPath $RollbackFrom).Path
    $ManifestPath = Join-Path $ResolvedBackup "manifest.json"
    if (-not (Test-Path -LiteralPath $ManifestPath)) {
        throw "Rollback manifest not found: $ManifestPath"
    }
    $Manifest = Get-Content -LiteralPath $ManifestPath -Raw | ConvertFrom-Json

    foreach ($State in $Manifest.skills) {
        Assert-SafeUserTarget $State.target
        if (Test-Path -LiteralPath $State.target) {
            Remove-Item -LiteralPath $State.target -Recurse -Force
        }
        if ($State.existed) {
            Copy-Item -LiteralPath (Join-Path $ResolvedBackup $State.backup) `
                -Destination $State.target -Recurse -Force
        }
        Write-Host "RESTORED SKILL $($State.name): existed_before=$($State.existed)"
    }

    foreach ($State in $Manifest.rules) {
        Assert-SafeUserTarget $State.target
        if (Test-Path -LiteralPath $State.target) {
            Remove-Item -LiteralPath $State.target -Force
        }
        if ($State.existed) {
            Copy-FileWithParent (Join-Path $ResolvedBackup $State.backup) $State.target
        }
        if ($State.marker) {
            Assert-SafeUserTarget $State.marker
            if (Test-Path -LiteralPath $State.marker) {
                Remove-Item -LiteralPath $State.marker -Force
            }
            if ($State.marker_existed) {
                Copy-FileWithParent (Join-Path $ResolvedBackup $State.marker_backup) $State.marker
            }
        }
        Write-Host "RESTORED RULE $($State.name): existed_before=$($State.existed)"
    }
    Write-Host "ROLLBACK COMPLETE: $ResolvedBackup"
    exit 0
}

if (-not (Test-Path -LiteralPath $RulesSource)) {
    throw "Global rule source not found: $RulesSource"
}
if (-not (Test-Path -LiteralPath $ClaudeRulesSource)) {
    throw "Claude rule source not found: $ClaudeRulesSource"
}

foreach ($Target in $SkillDestinations.Values) {
    Assert-SafeUserTarget $Target
}
if (-not $SkipRules) {
    foreach ($Entry in $RuleDestinations.GetEnumerator()) {
        $Rule = $Entry.Value
        Assert-SafeUserTarget $Rule.target
        if ($Rule.marker) {
            Assert-SafeUserTarget $Rule.marker
            if ((Test-Path -LiteralPath $Rule.target) -and -not (Test-Path -LiteralPath $Rule.marker)) {
                $SourceHash = (Get-FileHash -LiteralPath $Rule.source -Algorithm SHA256).Hash
                $TargetHash = (Get-FileHash -LiteralPath $Rule.target -Algorithm SHA256).Hash
                $MatchesManagedBaseline = $ExistingCodexRuleHash -and (
                    $TargetHash -eq $ExistingCodexRuleHash
                )
                if (($SourceHash -ne $TargetHash) -and -not $MatchesManagedBaseline) {
                    throw "Refusing to overwrite unmanaged rule file for $($Entry.Key): $($Rule.target)"
                }
            }
        }
    }
}

$Stamp = [DateTime]::UtcNow.ToString("yyyyMMddTHHmmssZ")
$BackupDir = Join-Path $BackupRoot $Stamp
New-Item -ItemType Directory -Force -Path $BackupDir | Out-Null
$Manifest = [ordered]@{
    schema_version = 1
    deployed_at = [DateTime]::UtcNow.ToString("o")
    skill_source = $SkillRoot
    rules_source = $RulesSource
    skills = @()
    rules = @()
}

foreach ($Entry in $SkillDestinations.GetEnumerator()) {
    $Existed = Test-Path -LiteralPath $Entry.Value
    $BackupName = "skill_$($Entry.Key)"
    if ($Existed) {
        Copy-Item -LiteralPath $Entry.Value -Destination (Join-Path $BackupDir $BackupName) `
            -Recurse -Force
        Remove-Item -LiteralPath $Entry.Value -Recurse -Force
    }
    New-Item -ItemType Directory -Force -Path (Split-Path $Entry.Value) | Out-Null
    Copy-Item -LiteralPath $SkillRoot -Destination $Entry.Value -Recurse -Force
    $Manifest.skills += [ordered]@{
        name = $Entry.Key
        target = $Entry.Value
        existed = $Existed
        backup = $BackupName
    }
    Write-Host "DEPLOYED SKILL $($Entry.Key): $($Entry.Value)"
}

if (-not $SkipRules) {
    foreach ($Entry in $RuleDestinations.GetEnumerator()) {
        $Rule = $Entry.Value
        $Existed = Test-Path -LiteralPath $Rule.target
        $MarkerExisted = $Rule.marker -and (Test-Path -LiteralPath $Rule.marker)
        $BackupName = "rule_$($Entry.Key)"
        $MarkerBackupName = "marker_$($Entry.Key)"
        if ($Existed) {
            Copy-Item -LiteralPath $Rule.target -Destination (Join-Path $BackupDir $BackupName) -Force
        }
        if ($MarkerExisted) {
            Copy-Item -LiteralPath $Rule.marker `
                -Destination (Join-Path $BackupDir $MarkerBackupName) -Force
        }
        Copy-FileWithParent $Rule.source $Rule.target
        if ($Rule.marker) {
            $MarkerData = [ordered]@{
                managed_by = "agent-stack"
                tool = $Entry.Key
                origin = $Rule.source
                synced_at = [DateTime]::UtcNow.ToString("o")
            }
            Write-Utf8NoBom $Rule.marker (($MarkerData | ConvertTo-Json -Depth 4) + "`n")
        }
        $Manifest.rules += [ordered]@{
            name = $Entry.Key
            target = $Rule.target
            existed = $Existed
            backup = $BackupName
            marker = $Rule.marker
            marker_existed = [bool]$MarkerExisted
            marker_backup = $MarkerBackupName
        }
        Write-Host "DEPLOYED RULE $($Entry.Key): $($Rule.target)"
    }
}

Write-Utf8NoBom (Join-Path $BackupDir "manifest.json") `
    (($Manifest | ConvertTo-Json -Depth 8) + "`n")
Write-Host "BACKUP: $BackupDir"
