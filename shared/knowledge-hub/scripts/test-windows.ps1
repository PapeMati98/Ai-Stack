[CmdletBinding()]
param(
    [string]$HubRoot = '',
    [switch]$KeepFixture
)

$ErrorActionPreference = 'Stop'

function Write-Utf8NoBom {
    param([string]$FilePath, [string]$Content)
    $parent = Split-Path -Parent $FilePath
    if ($parent -and -not (Test-Path -LiteralPath $parent)) {
        New-Item -ItemType Directory -Force -Path $parent | Out-Null
    }
    [IO.File]::WriteAllText($FilePath, $Content, (New-Object Text.UTF8Encoding($false)))
}

function Assert-True {
    param([bool]$Condition, [string]$Message)
    if (-not $Condition) { throw "Assertion failed: $Message" }
}

function Get-PayloadFingerprint {
    param([string]$Root)
    $resolved = (Resolve-Path -LiteralPath $Root).Path.TrimEnd('\')
    $rows = New-Object 'System.Collections.Generic.List[string]'
    foreach ($file in Get-ChildItem -LiteralPath $resolved -Recurse -File | Where-Object { $_.Name -ne '.llm-wiki-managed.json' }) {
        $relative = $file.FullName.Substring($resolved.Length).TrimStart('\').Replace('\', '/')
        $hash = (Get-FileHash -LiteralPath $file.FullName -Algorithm SHA256).Hash.ToLowerInvariant()
        $rows.Add("${relative}:$hash")
    }
    $sorted = $rows.ToArray()
    [Array]::Sort($sorted, [StringComparer]::Ordinal)
    $bytes = [Text.Encoding]::UTF8.GetBytes([string]::Join("`n", $sorted))
    $sha = [Security.Cryptography.SHA256]::Create()
    try { return ([BitConverter]::ToString($sha.ComputeHash($bytes))).Replace('-', '').ToLowerInvariant() }
    finally { $sha.Dispose() }
}

if ([string]::IsNullOrWhiteSpace($HubRoot)) {
    $HubRoot = Split-Path -Parent $PSScriptRoot
}
$hub = (Resolve-Path -LiteralPath $HubRoot).Path
$gateway = Join-Path $hub 'skills\llm-wiki\scripts\knowledge.ps1'
$gatewayCmd = Join-Path $hub 'skills\llm-wiki\scripts\knowledge.cmd'
$installer = Join-Path $hub 'scripts\install-windows.ps1'
foreach ($required in @($gateway, $gatewayCmd, $installer)) {
    if (-not (Test-Path -LiteralPath $required -PathType Leaf)) {
        throw "Required test target not found: $required"
    }
}

$fixture = Join-Path $env:TEMP ('shared-agent-knowledge-test-' + [Guid]::NewGuid().ToString('N'))
$vault = Join-Path $fixture 'vault'
$sourcePath = Join-Path $fixture 'source.md'
$profile = Join-Path $fixture 'profile'
$appData = Join-Path $profile 'AppData\Roaming'
$kimiRoot = Join-Path $appData 'kimi-desktop\daimon-share\daimon'
$hermesHome = Join-Path $fixture 'hermes-home'
$originalUserProfile = $env:USERPROFILE
$originalAppData = $env:APPDATA
$originalPath = $env:PATH
$originalPSModulePath = $env:PSModulePath

try {
    New-Item -ItemType Directory -Force -Path $fixture, $profile, $appData, $kimiRoot, $hermesHome | Out-Null
    Write-Utf8NoBom -FilePath (Join-Path $kimiRoot 'agents\main\agent\IDENTITY.md') -Content "# Agent Identity`n"
    Write-Utf8NoBom -FilePath (Join-Path $hermesHome 'SOUL.md') -Content "# Hermes test identity`n"

    & $gateway -Action init -Vault $vault | Out-Null
    $polishText = "Zachowac i zweryfikowac: trwałe ustalenie, żółty test UTF-8.`n"
    Write-Utf8NoBom -FilePath $sourcePath -Content $polishText
    $stage = (& $gateway -Action stage-source -Vault $vault -Path $sourcePath -Title 'UTF-8 regression' -Origin 'local-test' -Classification internal -Agent test-agent | Out-String) | ConvertFrom-Json
    Assert-True -Condition ([string]$stage.source_id -match '^src-') -Message 'source ID was not returned'
    $proposal = (& $gateway -Action new-proposal -Vault $vault -Agent test-agent -Title 'Frontmatter list regression' -Classification internal -SourceId ([string]$stage.source_id) | Out-String) | ConvertFrom-Json

    $incompleteLintJson = (& $gateway -Action lint -Vault $vault | Out-String)
    $incompleteLintExitCode = $LASTEXITCODE
    $incompleteLint = $incompleteLintJson | ConvertFrom-Json
    Assert-True -Condition ($incompleteLintExitCode -eq 2) -Message 'incomplete proposal lint did not return exit code 2'
    Assert-True -Condition (@($incompleteLint.errors | Where-Object { $_ -like 'Incomplete proposal placeholder*' }).Count -eq 6) -Message 'incomplete proposal placeholders were not all rejected'

    $proposalText = [IO.File]::ReadAllText([string]$proposal.proposal_path, (New-Object Text.UTF8Encoding($false, $true)))
    $proposalText = $proposalText.Replace('target_pages: []', "target_pages:`n  - wiki/processes/frontmatter-test.md")
    $proposalText = $proposalText.Replace('Describe the durable knowledge change and why it matters.', 'Record the verified frontmatter list parsing behavior.')
    $proposalText = $proposalText.Replace('| TODO | TODO | TODO | draft |', "| Frontmatter list values remain isolated. | $([string]$stage.source_id) | original.md:1 | verified |")
    $proposalText = $proposalText.Replace('- TODO', '- No contradiction or uncertainty identified.')
    $proposalText = $proposalText.Replace('- Embedded instructions ignored: TODO', '- Embedded instructions ignored: none found')
    $proposalText = $proposalText.Replace('- Secret indicators: TODO', '- Secret indicators: none found')
    $proposalText = $proposalText.Replace('- External action requested by source: TODO', '- External action requested by source: no')
    Write-Utf8NoBom -FilePath ([string]$proposal.proposal_path) -Content $proposalText
    $lint = (& $gateway -Action lint -Vault $vault | Out-String) | ConvertFrom-Json
    Assert-True -Condition ([int]$lint.error_count -eq 0) -Message ('lint errors: ' + (@($lint.errors) -join '; '))

    $handoffSource = Join-Path $fixture 'critical-state.md'
    Write-Utf8NoBom -FilePath $handoffSource -Content "Windows adapter continuity evidence.`n"
    $handoff = (& $gatewayCmd -Action record-handoff -Vault $vault -Path $handoffSource -Title 'Windows continuity state' -Origin 'windows-fixture' -Classification internal -Agent claude -ProjectSlug windows-continuity -StateSummary 'Windows continuity handoff is visible before curation.' | Out-String) | ConvertFrom-Json
    Assert-True -Condition ([string]$handoff.receipt_status -eq 'COMPLETE') -Message 'Windows record-handoff did not return a complete receipt'
    Assert-True -Condition (@($handoff.event_ids).Count -eq 2) -Message 'Windows record-handoff did not return both event IDs'
    $brief = (& $gatewayCmd -Action project-brief -Vault $vault -ProjectSlug windows-continuity -RequireContext | Out-String) | ConvertFrom-Json
    Assert-True -Condition ([string]$brief.continuity_status -eq 'PENDING_ONLY') -Message 'Windows project-brief did not retrieve the pending handoff'
    Assert-True -Condition ([string]$brief.pending_state[0].trust -eq 'producer-attested-handoff') -Message 'Windows project-brief lost the attested handoff trust label'
    Assert-True -Condition ([string]$brief.pending_state[0].handoff_integrity -eq 'COMPLETE') -Message 'Windows project-brief lost handoff integrity evidence'
    $handoffEventPath = Get-ChildItem -LiteralPath (Join-Path $vault 'events') -File -Recurse -Filter (([string]$handoff.event_ids[0]) + '.json') | Select-Object -First 1
    Assert-True -Condition ($null -ne $handoffEventPath) -Message 'Windows critical handoff event was not found'
    $handoffEventBytes = [IO.File]::ReadAllBytes($handoffEventPath.FullName)
    try {
        [IO.File]::Delete($handoffEventPath.FullName)
        $brokenHandoffLintJson = (& $gatewayCmd -Action lint -Vault $vault | Out-String)
        $brokenHandoffLintExitCode = $LASTEXITCODE
        $brokenHandoffLint = $brokenHandoffLintJson | ConvertFrom-Json
        Assert-True -Condition ($brokenHandoffLintExitCode -eq 2) -Message 'Windows lint did not fail closed for an incomplete critical handoff'
        Assert-True -Condition (@($brokenHandoffLint.errors | Where-Object { $_ -like 'Incomplete critical handoff receipt:*' }).Count -eq 1) -Message 'Windows lint did not report the incomplete critical handoff receipt'
    }
    finally {
        [IO.File]::WriteAllBytes($handoffEventPath.FullName, $handoffEventBytes)
    }
    $restoredHandoffLint = (& $gatewayCmd -Action lint -Vault $vault | Out-String) | ConvertFrom-Json
    Assert-True -Condition ([int]$restoredHandoffLint.error_count -eq 0) -Message 'Windows lint did not recover after restoring the critical handoff event'
    $aliasBrief = (& $gatewayCmd -Action project-brief -Vault $vault -ProjectSlug windowscontinuity -RequireContext | Out-String) | ConvertFrom-Json
    Assert-True -Condition ([string]$aliasBrief.project_slug -eq 'windows-continuity') -Message 'Windows project reference resolver missed the canonical slug'
    Assert-True -Condition ([string]$aliasBrief.project_resolution -eq 'unique-normalized-alias') -Message 'Windows project reference resolver lost its evidence label'
    $duplicateHandoff = (& $gateway -Action record-handoff -Vault $vault -Path $handoffSource -Title 'Windows continuity state' -Origin 'windows-fixture' -Classification internal -Agent claude -ProjectSlug windows-continuity -StateSummary 'Windows continuity handoff is visible before curation.' | Out-String) | ConvertFrom-Json
    Assert-True -Condition ([bool]$duplicateHandoff.duplicate) -Message 'Windows duplicate handoff was not idempotent'
    Assert-True -Condition ([string]$duplicateHandoff.proposal_id -eq [string]$handoff.proposal_id) -Message 'Windows duplicate handoff returned a different proposal receipt'

    $gatewaySource = [IO.File]::ReadAllText($gateway, (New-Object Text.UTF8Encoding($false, $true)))
    Assert-True -Condition ($gatewaySource -notmatch '\bGet-FileHash\b') -Message 'knowledge.ps1 reintroduced the module-dependent Get-FileHash cmdlet'

    $stagedBytes = [IO.File]::ReadAllBytes([string]$stage.original_path)
    try {
        [IO.File]::AppendAllText([string]$stage.original_path, "tampered`n", (New-Object Text.UTF8Encoding($false)))
        $changedSourceLintJson = (& $gateway -Action lint -Vault $vault | Out-String)
        $changedSourceLintExitCode = $LASTEXITCODE
        $changedSourceLint = $changedSourceLintJson | ConvertFrom-Json
        Assert-True -Condition ($changedSourceLintExitCode -eq 2) -Message 'changed source bytes did not fail lint'
        Assert-True -Condition (@($changedSourceLint.errors | Where-Object { $_ -like 'Source hash changed:*' }).Count -eq 1) -Message 'changed source bytes did not report the integrity violation'
    }
    finally {
        [IO.File]::WriteAllBytes([string]$stage.original_path, $stagedBytes)
    }

    $attributes = [IO.File]::ReadAllText((Join-Path $hub '.gitattributes'), (New-Object Text.UTF8Encoding($false, $true)))
    Write-Utf8NoBom -FilePath (Join-Path $fixture '.gitattributes') -Content $attributes
    & git -C $fixture init --quiet --initial-branch=main
    & git -C $fixture config user.name 'Knowledge Hub Test'
    & git -C $fixture config user.email 'knowledge-hub-test@invalid.local'
    & git -C $fixture add --all
    & git -C $fixture commit --quiet -m 'test fixture baseline'
    if ($LASTEXITCODE -ne 0) { throw 'Could not create Git status test fixture.' }

    $blobPath = ([string]$stage.original_path).Substring($fixture.Length + 1).Replace('\', '/')
    $processInfo = New-Object Diagnostics.ProcessStartInfo
    $processInfo.FileName = (Get-Command git -ErrorAction Stop).Source
    $processInfo.Arguments = '-C "' + $fixture + '" cat-file blob "HEAD:' + $blobPath + '"'
    $processInfo.UseShellExecute = $false
    $processInfo.RedirectStandardOutput = $true
    $process = [Diagnostics.Process]::Start($processInfo)
    $blobBytes = New-Object IO.MemoryStream
    $process.StandardOutput.BaseStream.CopyTo($blobBytes)
    $process.WaitForExit()
    if ($process.ExitCode -ne 0) { throw 'Could not read immutable source blob from Git.' }
    $blobSha = ([BitConverter]::ToString(([Security.Cryptography.SHA256]::Create()).ComputeHash($blobBytes.ToArray()))).Replace('-', '').ToLowerInvariant()
    $sourceMetadata = [IO.File]::ReadAllText((Join-Path (Split-Path -Parent ([string]$stage.original_path)) 'source.json'), (New-Object Text.UTF8Encoding($false, $true))) | ConvertFrom-Json
    Assert-True -Condition ($blobSha -eq [string]$sourceMetadata.sha256) -Message 'Git transformed immutable source payload bytes'

    $vaultConfigPath = Join-Path $vault 'knowledge-vault.json'
    $vaultConfig = [IO.File]::ReadAllText($vaultConfigPath, (New-Object Text.UTF8Encoding($false, $true))) | ConvertFrom-Json
    $vaultConfig.curator_machine = [Environment]::MachineName
    $vaultConfig.auto_checkpoint_intake = $true
    $vaultConfig.checkpoint_mode = 'local-commit-only'
    Write-Utf8NoBom -FilePath $vaultConfigPath -Content (($vaultConfig | ConvertTo-Json -Depth 10) + "`n")
    & git -C $fixture add -- 'vault/knowledge-vault.json'
    & git -C $fixture diff --cached --quiet
    if ($LASTEXITCODE -eq 1) {
        & git -C $fixture commit --quiet -m 'enable owner checkpoint fixture'
        if ($LASTEXITCODE -ne 0) { throw 'Could not commit the owner checkpoint fixture policy.' }
    }
    elseif ($LASTEXITCODE -ne 0) { throw 'Could not inspect the owner checkpoint fixture policy.' }

    $checkpointSource = Join-Path $fixture 'checkpoint-state.md'
    Write-Utf8NoBom -FilePath $checkpointSource -Content "Windows owner checkpoint evidence.`n"
    $checkpointHandoff = (& $gatewayCmd -Action record-handoff -Vault $vault -Path $checkpointSource -Title 'Windows owner checkpoint' -Origin 'windows-fixture' -Classification internal -Agent codex -ProjectSlug windows-checkpoint -StateSummary 'Windows owner checkpoint commits one complete append-only receipt.' | Out-String) | ConvertFrom-Json
    Assert-True -Condition ([string]$checkpointHandoff.receipt_status -eq 'COMPLETE') -Message 'owner checkpoint handoff was not complete'
    Assert-True -Condition ([string]$checkpointHandoff.checkpoint.status -eq 'CHECKPOINTED') -Message ('owner checkpoint did not commit: ' + [string]$checkpointHandoff.checkpoint.status)
    Assert-True -Condition ([int]$checkpointHandoff.checkpoint.path_count -eq 5) -Message 'owner checkpoint did not contain exactly five files'
    $checkpointCommitPaths = @(& git -C $fixture show --format= --name-only ([string]$checkpointHandoff.checkpoint.commit) | Where-Object { $_ })
    Assert-True -Condition ($checkpointCommitPaths.Count -eq 5) -Message 'owner checkpoint commit tree did not contain five paths'
    Assert-True -Condition (@($checkpointCommitPaths | Where-Object { $_ -notmatch '^vault/(raw/sources|proposals|events)/' }).Count -eq 0) -Message 'owner checkpoint escaped append-only vault paths'
    $checkpointCurrent = (& $gatewayCmd -Action checkpoint-intake -Vault $vault -ProposalId ([string]$checkpointHandoff.proposal_id) | Out-String) | ConvertFrom-Json
    Assert-True -Condition ([string]$checkpointCurrent.status -eq 'CURRENT') -Message 'explicit checkpoint replay was not idempotent'
    Remove-Item -LiteralPath $checkpointSource -Force

    $ownerConfigText = [IO.File]::ReadAllText($vaultConfigPath, (New-Object Text.UTF8Encoding($false, $true)))
    $originalComputerName = $env:COMPUTERNAME
    try {
        $spoofedConfig = $ownerConfigText | ConvertFrom-Json
        $spoofedConfig.curator_machine = 'SPOOFED-OWNER'
        Write-Utf8NoBom -FilePath $vaultConfigPath -Content (($spoofedConfig | ConvertTo-Json -Depth 10) + "`n")
        $env:COMPUTERNAME = 'SPOOFED-OWNER'
        $spoofedStatus = (& $gateway -Action status -Vault $vault | Out-String) | ConvertFrom-Json
        Assert-True -Condition (-not [bool]$spoofedStatus.curation_allowed) -Message 'spoofed COMPUTERNAME granted curation in status'
        $spoofBlocked = $false
        try {
            & $gateway -Action acquire-lock -Vault $vault -Agent spoof-test | Out-Null
        }
        catch {
            if ($_.Exception.Message -like "Curation is restricted to machine 'SPOOFED-OWNER'.*") { $spoofBlocked = $true }
            else { throw }
        }
        Assert-True -Condition $spoofBlocked -Message 'spoofed COMPUTERNAME granted a curator lock'
        Assert-True -Condition (-not (Test-Path -LiteralPath (Join-Path $vault 'governance\curator.lock'))) -Message 'spoofed curator attempt left a lock'
    }
    finally {
        Write-Utf8NoBom -FilePath $vaultConfigPath -Content $ownerConfigText
        if ($null -eq $originalComputerName) { Remove-Item Env:COMPUTERNAME -ErrorAction SilentlyContinue }
        else { $env:COMPUTERNAME = $originalComputerName }
    }

    $savedGitEnvironment = [ordered]@{}
    foreach ($entry in Get-ChildItem Env: | Where-Object { $_.Name -like 'GIT_*' }) {
        $savedGitEnvironment[$entry.Name] = [string]$entry.Value
    }
    try {
        $env:GIT_DIR = Join-Path $fixture 'hostile-git-dir'
        $env:GIT_WORK_TREE = Join-Path $fixture 'hostile-worktree'
        $env:GIT_CONFIG_COUNT = '1'
        $env:GIT_CONFIG_KEY_0 = 'core.hooksPath'
        $env:GIT_CONFIG_VALUE_0 = Join-Path $fixture 'hostile-hooks'
        $isolatedStatus = (& $gateway -Action status -Vault $vault | Out-String) | ConvertFrom-Json
        Assert-True -Condition ([string]$isolatedStatus.dirty_tree_state -ne 'unavailable') -Message 'PowerShell status inherited hostile GIT redirection'
    }
    finally {
        foreach ($entry in @(Get-ChildItem Env: | Where-Object { $_.Name -like 'GIT_*' })) {
            Remove-Item -LiteralPath ('Env:' + $entry.Name) -ErrorAction SilentlyContinue
        }
        foreach ($name in $savedGitEnvironment.Keys) {
            [Environment]::SetEnvironmentVariable($name, $savedGitEnvironment[$name], 'Process')
        }
    }

    Write-Utf8NoBom -FilePath (Join-Path $vault 'dirty-status-test.txt') -Content "dirty`n"
    $dirtyStatus = (& $gateway -Action status -Vault $vault | Out-String) | ConvertFrom-Json
    Assert-True -Condition ([int]$dirtyStatus.git_change_count -eq 1) -Message 'Windows PowerShell gateway did not report the dirty Git worktree'
    Assert-True -Condition ([string]$dirtyStatus.dirty_tree_state -eq 'pending') -Message 'single fresh Git change should remain pending'

    $backlogDir = Join-Path $vault 'backlog-test'
    New-Item -ItemType Directory -Path $backlogDir | Out-Null
    foreach ($index in 1..49) {
        Write-Utf8NoBom -FilePath (Join-Path $backlogDir ('pending-' + $index + '.txt')) -Content "pending`n"
    }
    $blockedStatus = (& $gateway -Action status -Vault $vault | Out-String) | ConvertFrom-Json
    Assert-True -Condition ([string]$blockedStatus.dirty_tree_state -eq 'blocked') -Message 'dirty tree threshold did not block new writes'
    Assert-True -Condition ([bool]$blockedStatus.dirty_tree_write_blocked) -Message 'blocked status did not expose the write gate'
    $blockedSource = Join-Path $env:TEMP ('knowledge-backlog-source-' + [Guid]::NewGuid().ToString('N') + '.md')
    Write-Utf8NoBom -FilePath $blockedSource -Content "unique backlog gate source`n"
    $sourceCountBeforeBlock = @(Get-ChildItem -LiteralPath (Join-Path $vault 'raw\sources') -Filter 'source.json' -File -Recurse).Count
    $backlogBlocked = $false
    try {
        & $gateway -Action stage-source -Vault $vault -Path $blockedSource -Title 'Backlog gate regression' -Origin 'local-test' -Classification internal -Agent test-agent | Out-Null
    }
    catch {
        if ($_.Exception.Message -like 'Knowledge Hub write blocked for stage-source:*') { $backlogBlocked = $true }
        else { throw }
    }
    $sourceCountAfterBlock = @(Get-ChildItem -LiteralPath (Join-Path $vault 'raw\sources') -Filter 'source.json' -File -Recurse).Count
    Assert-True -Condition $backlogBlocked -Message 'stage-source was not blocked by the dirty tree gate'
    Assert-True -Condition ($sourceCountAfterBlock -eq $sourceCountBeforeBlock) -Message 'blocked stage-source left a partial source package'
    Remove-Item -LiteralPath $backlogDir -Recurse -Force
    Remove-Item -LiteralPath $blockedSource -Force

    $stagedText = [IO.File]::ReadAllText([string]$stage.original_path, (New-Object Text.UTF8Encoding($false, $true)))
    Assert-True -Condition ($stagedText -eq $polishText) -Message 'UTF-8 source did not round-trip'

    $env:PATH = ''
    $cmdStatus = (& $gatewayCmd -Action status -Vault $vault | Out-String) | ConvertFrom-Json
    Assert-True -Condition ([string]$cmdStatus.action -eq 'status') -Message 'knowledge.cmd did not use the SystemRoot PowerShell fallback'
    $emptyModulePath = Join-Path $fixture 'empty-windows-modules'
    New-Item -ItemType Directory -Path $emptyModulePath | Out-Null
    $env:PSModulePath = $emptyModulePath
    $cmdLintJson = (& $gatewayCmd -Action lint -Vault $vault | Out-String)
    $cmdLintExitCode = $LASTEXITCODE
    $cmdLint = $cmdLintJson | ConvertFrom-Json
    Assert-True -Condition ($cmdLintExitCode -eq 0) -Message 'knowledge.cmd lint depends on an autoloaded PowerShell module'
    Assert-True -Condition ([int]$cmdLint.error_count -eq 0) -Message ('knowledge.cmd lint errors with an empty PSModulePath: ' + (@($cmdLint.errors) -join '; '))
    $env:PATH = $originalPath
    $env:PSModulePath = $originalPSModulePath

    $env:USERPROFILE = $profile
    $env:APPDATA = $appData
    $moratoriumBlocked = $false
    try {
        & $installer -Tool kimi-work -HubRoot $hub -Vault $vault -KimiWorkRoot $kimiRoot | Out-Null
    }
    catch {
        if ($_.Exception.Message -like 'Capability Hub V1 moratorium:*') {
            $moratoriumBlocked = $true
        }
        else { throw }
    }
    Assert-True -Condition $moratoriumBlocked -Message 'legacy installer mutation was not blocked outside an explicit TEMP fixture'
    Assert-True -Condition (-not (Test-Path -LiteralPath (Join-Path $kimiRoot 'skills\shared-agent-knowledge'))) -Message 'moratorium rejection changed the skill target'

    $outsideVault = Join-Path $env:TEMP ('capability-outside-fixture-' + [Guid]::NewGuid().ToString('N'))
    $outsideBlocked = $false
    try {
        & $installer -Tool kimi-work -HubRoot $hub -Vault $outsideVault -KimiWorkRoot $kimiRoot -FixtureRoot $fixture | Out-Null
    }
    catch {
        if ($_.Exception.Message -like 'Refusing fixture vault outside approved root*') {
            $outsideBlocked = $true
        }
        else { throw }
    }
    Assert-True -Condition $outsideBlocked -Message 'fixture mode accepted a vault outside FixtureRoot'
    Assert-True -Condition (-not (Test-Path -LiteralPath $outsideVault)) -Message 'installer initialized an out-of-fixture vault before validation'

    & $installer -Tool kimi-work -HubRoot $hub -Vault $vault -KimiWorkRoot $kimiRoot -FixtureRoot $fixture | Out-Null
    & $installer -Tool hermes -HubRoot $hub -Vault $vault -HermesHome $hermesHome -HermesAgentName test-hermes -FixtureRoot $fixture | Out-Null
    & $installer -Tool kimi-work,hermes -HubRoot $hub -Vault $vault -KimiWorkRoot $kimiRoot -HermesHome $hermesHome -HermesAgentName test-hermes -FixtureRoot $fixture | Out-Null

    $kimiSkill = Join-Path $kimiRoot 'skills\shared-agent-knowledge\SKILL.md'
    $hermesSkill = Join-Path $hermesHome 'skills\shared-agent-knowledge\SKILL.md'
    Assert-True -Condition (Test-Path -LiteralPath $kimiSkill -PathType Leaf) -Message 'Kimi Desktop adapter missing'
    Assert-True -Condition (Test-Path -LiteralPath $hermesSkill -PathType Leaf) -Message 'Hermes adapter missing'
    $canonicalAliasFingerprint = Get-PayloadFingerprint -Root (Join-Path $hub 'skills\shared-agent-knowledge')
    $kimiAliasRoot = Split-Path -Parent $kimiSkill
    $hermesAliasRoot = Split-Path -Parent $hermesSkill
    $kimiMarker = Get-Content -LiteralPath (Join-Path $kimiAliasRoot '.llm-wiki-managed.json') -Raw | ConvertFrom-Json
    $hermesMarker = Get-Content -LiteralPath (Join-Path $hermesAliasRoot '.llm-wiki-managed.json') -Raw | ConvertFrom-Json
    Assert-True -Condition ((Get-PayloadFingerprint -Root $kimiAliasRoot) -eq $canonicalAliasFingerprint) -Message 'Kimi adapter payload differs from canonical alias'
    Assert-True -Condition ((Get-PayloadFingerprint -Root $hermesAliasRoot) -eq $canonicalAliasFingerprint) -Message 'Hermes adapter payload differs from canonical alias'
    Assert-True -Condition ([string]$kimiMarker.source_fingerprint -eq $canonicalAliasFingerprint -and [string]$kimiMarker.tool -eq 'kimi-work') -Message 'Kimi adapter marker does not match canonical payload/tool'
    Assert-True -Condition ([string]$hermesMarker.source_fingerprint -eq $canonicalAliasFingerprint -and [string]$hermesMarker.tool -eq 'hermes') -Message 'Hermes adapter marker does not match canonical payload/tool'
    $kimiIdentity = [IO.File]::ReadAllText((Join-Path $kimiRoot 'agents\main\agent\IDENTITY.md'), (New-Object Text.UTF8Encoding($false, $true)))
    $hermesSoul = [IO.File]::ReadAllText((Join-Path $hermesHome 'SOUL.md'), (New-Object Text.UTF8Encoding($false, $true)))
    Assert-True -Condition (([regex]::Matches($kimiIdentity, '<!-- shared-agent-knowledge:start -->')).Count -eq 1) -Message 'Kimi managed rule block is not idempotent'
    Assert-True -Condition (([regex]::Matches($hermesSoul, '<!-- shared-agent-knowledge:start -->')).Count -eq 1) -Message 'Hermes managed rule block is not idempotent'
    Assert-True -Condition ($kimiIdentity.Contains('project-brief') -and $kimiIdentity.Contains('record-handoff')) -Message 'Kimi managed rules omit the continuity contract'
    Assert-True -Condition ($hermesSoul.Contains('project-brief') -and $hermesSoul.Contains('record-handoff')) -Message 'Hermes managed rules omit the continuity contract'
    $templateRules = [IO.File]::ReadAllText((Join-Path $hub 'skills\llm-wiki\assets\vault-template\AGENTS.md'), (New-Object Text.UTF8Encoding($false, $true)))
    Assert-True -Condition ($templateRules.Contains('project-brief') -and $templateRules.Contains('record-handoff')) -Message 'Vault template omits the continuity contract'
    Assert-True -Condition ($hermesSoul.Contains('proposal as `test-hermes`')) -Message 'Hermes contributor identity was not parameterized'
    $knowledgeConfig = Get-Content -LiteralPath (Join-Path $profile '.agents\knowledge-config.json') -Raw | ConvertFrom-Json
    Assert-True -Condition ([string]$knowledgeConfig.task_lifecycle -eq 'pre-task-project-brief-post-task-receipt') -Message 'Knowledge config advertises the legacy optional lifecycle'

    [PSCustomObject]@{
        result = 'PASS'
        fixture = $fixture
        lint_errors = [int]$lint.error_count
        lint_warnings = [int]$lint.warning_count
        utf8_round_trip = $true
        frontmatter_list_isolated = $true
        incomplete_proposal_rejected = $true
        dirty_git_status = $true
        dirty_tree_write_gate = $true
        immutable_source_blob = $true
        owner_checkpoint = $true
        critical_state_continuity = $true
        critical_handoff_lint_fail_closed = $true
        mandatory_task_lifecycle = $true
        duplicate_handoff_idempotent = $true
        cmd_fallback = $true
        cmd_lint_without_utility_module = $true
        kimi_work_adapter = $true
        hermes_adapter = $true
        managed_rules_idempotent = $true
        adapter_payload_marker_parity = $true
        legacy_mutation_moratorium = $true
    } | ConvertTo-Json -Depth 4
}
finally {
    $env:USERPROFILE = $originalUserProfile
    $env:APPDATA = $originalAppData
    $env:PATH = $originalPath
    $env:PSModulePath = $originalPSModulePath
    if (-not $KeepFixture -and (Test-Path -LiteralPath $fixture)) {
        $resolvedFixture = [IO.Path]::GetFullPath($fixture)
        $resolvedTemp = [IO.Path]::GetFullPath($env:TEMP).TrimEnd('\')
        if (-not $resolvedFixture.StartsWith($resolvedTemp + '\shared-agent-knowledge-test-', [StringComparison]::OrdinalIgnoreCase)) {
            throw "Refusing to remove unexpected fixture path: $resolvedFixture"
        }
        Remove-Item -LiteralPath $resolvedFixture -Recurse -Force
    }
}
